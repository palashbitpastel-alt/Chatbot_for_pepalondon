"""Live, customer-safe reads from the Shopify Admin GraphQL API.

Every query here is fixed and parameterised — the agent chooses a search term or
an order number, never the shape of the request — and every response is
projected down to what a shopper may see:

* only ACTIVE products, because drafts and archived items cannot be bought;
* no unit cost, margin, inventory valuation or campaign figures;
* an order is reachable only by its number **and** the email address on it, and
  a wrong email is answered exactly like a number that does not exist, so the
  tool cannot be used to discover which orders are real.
"""

import datetime
import difflib
import logging
import re
import time
from urllib.parse import quote

from app.core.config import settings
from app.services.shopify_client import ShopifyError, graphql, store_domain

logger = logging.getLogger(__name__)

PRODUCT_LIMIT = 10
VARIANT_LIMIT = 10
ORDER_SCAN_PAGE = 250      # orders per page when counting best sellers
ORDER_SCAN_LINES = 50      # line items read per order

STATUS_MEANING = {
    "UNFULFILLED": "We have your order and it is queued for packing.",
    "IN_PROGRESS": "Your order is being packed and will ship shortly.",
    "PARTIALLY_FULFILLED": "Part of your order has shipped; the rest is on its way.",
    "FULFILLED": "Your order has shipped.",
    "ON_HOLD": "Your order is on hold. Our team will be in touch.",
    "SCHEDULED": "Your order is scheduled to ship.",
    "CANCELLED": "This order was cancelled.",
    "RESTOCKED": "This order was returned to stock.",
}

PRODUCT_SEARCH = """
query SupportProductSearch($query: String!, $first: Int!, $variants: Int!) {
  products(first: $first, query: $query, sortKey: RELEVANCE) {
    nodes {
      legacyResourceId
      title
      handle
      productType
      onlineStoreUrl
      totalInventory
      featuredMedia { ... on MediaImage { image { url altText } } }
      variants(first: $variants) {
        nodes {
          legacyResourceId
          sku
          title
          price
          compareAtPrice
          availableForSale
          inventoryQuantity
          media(first: 1) { nodes { ... on MediaImage { image { url } } } }
        }
      }
    }
  }
}
"""

ORDER_BY_NAME = """
query SupportOrderStatus($query: String!) {
  orders(first: 1, query: $query) {
    nodes {
      name
      email
      statusPageUrl
      createdAt
      cancelledAt
      displayFulfillmentStatus
      displayFinancialStatus
      totalPriceSet { shopMoney { amount currencyCode } }
      fulfillments(first: 5) {
        status
        createdAt
        estimatedDeliveryAt
        deliveredAt
        trackingInfo { company number url }
      }
      lineItems(first: 20) {
        nodes {
          title
          quantity
          discountedTotalSet { shopMoney { amount currencyCode } }
          variant {
            legacyResourceId
            title
            media(first: 1) { nodes { ... on MediaImage { image { url } } } }
          }
          product {
            legacyResourceId
            handle
            productType
            tags
            onlineStoreUrl
            featuredMedia { ... on MediaImage { image { url } } }
          }
        }
      }
    }
  }
}
"""

CATEGORY_PRODUCTS = """
query SupportCategoryProducts($query: String!, $first: Int!, $cursor: String) {
  products(first: $first, after: $cursor, query: $query, sortKey: TITLE) {
    pageInfo { hasNextPage endCursor }
    nodes {
      productType
      category { id name }
      featuredMedia { ... on MediaImage { image { url } } }
    }
  }
}
"""

CART_PRODUCTS = """
query SupportCartProducts($query: String!, $first: Int!) {
  products(first: $first, query: $query) {
    nodes {
      legacyResourceId
      title
      handle
      onlineStoreUrl
      featuredMedia { ... on MediaImage { image { url } } }
      variants(first: 100) {
        nodes {
          legacyResourceId
          title
          price
          media(first: 1) { nodes { ... on MediaImage { image { url } } } }
        }
      }
    }
  }
}
"""

ORDERS_BY_EMAIL = """
query SupportOrderHistory($query: String!, $first: Int!) {
  orders(first: $first, query: $query, sortKey: CREATED_AT, reverse: true) {
    nodes {
      name
      createdAt
      cancelledAt
      displayFulfillmentStatus
      totalPriceSet { shopMoney { amount currencyCode } }
      lineItems(first: 20) {
        nodes {
          title
          quantity
          discountedTotalSet { shopMoney { amount currencyCode } }
          variant {
            legacyResourceId
            title
            media(first: 1) { nodes { ... on MediaImage { image { url } } } }
          }
          product {
            legacyResourceId
            handle
            productType
            tags
            onlineStoreUrl
            featuredMedia { ... on MediaImage { image { url } } }
          }
        }
      }
    }
  }
}
"""

BEST_SELLER_ORDERS = """
query SupportBestSellerOrders($query: String!, $first: Int!, $lines: Int!, $cursor: String) {
  orders(first: $first, after: $cursor, query: $query, sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    nodes {
      cancelledAt
      test
      lineItems(first: $lines) {
        nodes {
          currentQuantity
          product { id status }
        }
      }
    }
  }
}
"""

PRODUCTS_BY_IDS = """
query SupportProductsByIds($ids: [ID!]!, $variants: Int!) {
  nodes(ids: $ids) {
    ... on Product {
      id
      legacyResourceId
      title
      handle
      productType
      status
      onlineStoreUrl
      totalInventory
      featuredMedia { ... on MediaImage { image { url altText } } }
      variants(first: $variants) {
        nodes {
          legacyResourceId
          sku
          title
          price
          compareAtPrice
          availableForSale
          inventoryQuantity
          media(first: 1) { nodes { ... on MediaImage { image { url } } } }
        }
      }
    }
  }
}
"""

COLLECTIONS = """
query SupportCollections($first: Int!, $query: String) {
  collections(first: $first, query: $query, sortKey: UPDATED_AT, reverse: true) {
    nodes {
      id
      handle
      title
      description
      image { url altText }
      productsCount { count }
      products(first: 1) {
        nodes { featuredMedia { ... on MediaImage { image { url } } } }
      }
    }
  }
}
"""

SHOP_INFO = """
query SupportShopInfo {
  shop { name currencyCode contactEmail url }
}
"""

_shop_cache: dict | None = None


async def shop_info() -> dict:
    """Store name, currency and contact address. Cached — it does not change."""
    global _shop_cache
    if _shop_cache is None:
        shop = (await graphql(SHOP_INFO))["shop"]
        _shop_cache = {
            "name": shop["name"],
            "currency": shop["currencyCode"],
            "contact_email": shop.get("contactEmail"),
            "url": shop.get("url"),
        }
    return _shop_cache


def product_image(node: dict) -> str | None:
    """The product's featured image, if it has one."""
    media = node.get("featuredMedia") or {}
    return (media.get("image") or {}).get("url")


def variant_image(variant: dict) -> str | None:
    """A variant's own photo - the pink shoe rather than the navy one."""
    nodes = (variant.get("media") or {}).get("nodes") or []
    for item in nodes:
        url = (item.get("image") or {}).get("url")
        if url:
            return url
    return None


def product_url(node: dict, variant_id: str | None = None) -> str:
    """A storefront link. onlineStoreUrl is null while the store is password
    protected, so fall back to the canonical /products/<handle> path, and point
    at the exact variant when one was chosen."""
    url = node.get("onlineStoreUrl") or f"https://{store_domain()}/products/{node['handle']}"
    return f"{url}?variant={variant_id}" if variant_id else url


def order_line_card(line: dict) -> dict:
    """One line of an order, with the picture and link for what was bought."""
    variant = line.get("variant") or {}
    product = line.get("product") or {}
    variant_id = str(variant["legacyResourceId"]) if variant.get("legacyResourceId") else None
    money = (line.get("discountedTotalSet") or {}).get("shopMoney") or {}
    quantity = line.get("quantity") or 1
    # Shopify gives the total for the line; the unit price is what a shopper reads.
    line_total = round(float(money["amount"]), 2) if money.get("amount") else None
    unit_price = round(line_total / quantity, 2) if line_total is not None and quantity else None
    option = variant.get("title")
    return {
        "product_id": str(product["legacyResourceId"]) if product.get("legacyResourceId") else None,
        "variant_id": variant_id,
        "title": line.get("title"),
        "option": None if option in (None, "Default Title") else option,
        "quantity": quantity,
        "unit_price": unit_price,
        "line_total": line_total,
        "currency": money.get("currencyCode"),
        "image": variant_image(variant) or product_image(product),
        "url": product_url(product, variant_id) if product.get("handle") else None,
        "category": product.get("productType") or None,
        "tags": product.get("tags") or [],
        "handle": product.get("handle"),
    }


def _availability(variants: list[dict]) -> str:
    if any(v["availableForSale"] for v in variants):
        return "in_stock"
    return "out_of_stock"


def _public_variant(v: dict, node: dict) -> dict:
    quantity = v.get("inventoryQuantity") or 0
    variant_id = v.get("legacyResourceId")
    return {
        "variant_id": variant_id,
        "sku": v.get("sku") or None,
        "option": None if v["title"] == "Default Title" else v["title"],
        "price": v["price"],
        "was_price": v.get("compareAtPrice"),
        "available": bool(v["availableForSale"]),
        "units_available": max(quantity, 0),
        "image": variant_image(v) or product_image(node),
        "url": product_url(node, variant_id),
    }


def _public_product(node: dict, currency: str) -> dict:
    variants = node["variants"]["nodes"]
    prices = [float(v["price"]) for v in variants if v.get("price") is not None]
    return {
        "product_id": node.get("legacyResourceId"),
        "title": node["title"],
        "category": node.get("productType") or None,
        "url": product_url(node),
        "image": product_image(node),
        "currency": currency,
        "price_from": round(min(prices), 2) if prices else None,
        "price_to": round(max(prices), 2) if prices else None,
        "availability": _availability(variants),
        "variants": [_public_variant(v, node) for v in variants],
    }


async def search_products(query: str = "", limit: int = PRODUCT_LIMIT) -> dict:
    """Search the live catalogue. Only ACTIVE products — a shopper cannot buy a draft."""
    term = " ".join(query.split()).strip()
    # The agent supplies only the term; the status filter is ours and always applied.
    search = f"({term}) AND status:ACTIVE" if term else "status:ACTIVE"
    currency = (await shop_info())["currency"]
    data = await graphql(
        PRODUCT_SEARCH,
        {"query": search, "first": max(1, min(limit, PRODUCT_LIMIT)), "variants": VARIANT_LIMIT},
    )
    products = [_public_product(n, currency) for n in data["products"]["nodes"]]
    return {"query": term, "currency": currency, "count": len(products), "products": products}


def order_number_variants(raw: str) -> list[str]:
    """Accept '#1027' or '1027' for the same order."""
    given = raw.strip()
    bare = given.lstrip("#").strip()
    return list(dict.fromkeys([given, f"#{bare}", bare]))


def _fulfillment_status(node: dict) -> str:
    if node.get("cancelledAt"):
        return "CANCELLED"
    return node.get("displayFulfillmentStatus") or "UNFULFILLED"


async def find_order(order_number: str, email: str) -> dict:
    """Look one order up by number, released only when the email on it matches.

    A wrong email returns the same ``found: False`` as an order that does not
    exist, so this cannot be used to probe which order numbers are real. Only on
    a match does the response carry ``status_page_url`` - Shopify's tokenised
    order page, which anyone holding the link can open.
    """
    given_email = email.strip().casefold()
    if not given_email or "@" not in given_email:
        return {"found": False, "reason": "email_required"}

    node = None
    for candidate in order_number_variants(order_number):
        data = await graphql(ORDER_BY_NAME, {"query": f"name:{candidate}"})
        nodes = data["orders"]["nodes"]
        if nodes:
            node = nodes[0]
            break

    if node is None or (node.get("email") or "").strip().casefold() != given_email:
        return {"found": False, "reason": "no_match"}

    status = _fulfillment_status(node)
    money = node["totalPriceSet"]["shopMoney"]
    tracking = [
        {"carrier": t.get("company"), "number": t.get("number"), "url": t.get("url")}
        for f in node.get("fulfillments") or []
        for t in f.get("trackingInfo") or []
    ]
    estimated = next(
        (f.get("estimatedDeliveryAt") for f in node.get("fulfillments") or [] if f.get("estimatedDeliveryAt")),
        None,
    )
    return {
        "found": True,
        "order_number": node["name"],
        "placed_on": (node.get("createdAt") or "")[:10] or None,
        "status": status,
        "status_meaning": STATUS_MEANING.get(status, "Please contact support for the current status."),
        "payment_status": node.get("displayFinancialStatus"),
        "total": round(float(money["amount"]), 2),
        "currency": money["currencyCode"],
        "items": [order_line_card(line) for line in node["lineItems"]["nodes"]],
        "tracking": tracking,
        "estimated_delivery": estimated,
        # Shopify's tokenised order page: the token IS the authentication, so it
        # is only ever returned on this branch, where the email already matched.
        "status_page_url": node.get("statusPageUrl"),
    }


async def customer_orders(email: str, limit: int = 5) -> dict:
    """Recent orders for one email address, newest first.

    The caller must have established that the shopper really is this person -
    ``email:`` matches on the address alone, so this would otherwise read any
    customer's history from a guessed address.
    """
    address = (email or "").strip()
    if not address or "@" not in address:
        return {"found": False, "reason": "email_required", "orders": []}

    data = await graphql(ORDERS_BY_EMAIL, {"query": f'email:"{address}"', "first": max(1, min(limit, 20))})
    orders = []
    for node in data["orders"]["nodes"]:
        status = _fulfillment_status(node)
        money = node["totalPriceSet"]["shopMoney"]
        orders.append(
            {
                "order_number": node["name"],
                "placed_on": (node.get("createdAt") or "")[:10] or None,
                "status": status,
                "status_meaning": STATUS_MEANING.get(status, ""),
                "total": round(float(money["amount"]), 2),
                "currency": money["currencyCode"],
                "items": [order_line_card(line) for line in node["lineItems"]["nodes"]],
            }
        )
    return {"found": bool(orders), "count": len(orders), "orders": orders}


def _fresh(stamped: tuple | None, minutes: int) -> bool:
    """True while a cached (timestamp, value) pair is still worth reusing."""
    return bool(stamped) and time.monotonic() - stamped[0] < max(0, minutes) * 60


# ── Categories ─────────────────────────────────────────────────────────────
# A category here is the product type Shopify already stores on each product -
# Dress, Boots, Romper. Shopify gives a product type no id of its own, so the id
# below is a slug derived from the name: stable, url-safe, and what a client
# sends back to filter by. Where the products also carry Shopify's own taxonomy
# category, its real id and name ride along beside it.

# One entry: the whole grouping. `limit` only slices it, so asking for five
# categories must not send the scan round again.
_categories_cache: tuple[float, list[dict]] | None = None

CATEGORY_SCAN_PAGE = 250   # products per page while grouping
CATEGORY_SCAN_PAGES = 8    # ...so at most 2000 products are looked at
CATEGORY_LIMIT = 60        # most a caller may ask for

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    return _SLUG_RE.sub("-", name.lower()).strip("-")


def category_url(product_type: str) -> str:
    """The store's own catalogue, filtered to this product type."""
    return (f"https://{store_domain()}/collections/all"
            f"?filter.p.product_type={quote(product_type)}")


def _pick_taxonomy(counts: dict) -> tuple:
    """The Shopify taxonomy category most of this type's products agree on."""
    return max(counts, key=counts.get) if counts else (None, None)


async def categories(limit: int = CATEGORY_LIMIT) -> dict:
    """Every category a shopper can actually buy from, with a picture for each.

    Grouped from ACTIVE products only, so a draft or archived line never raises a
    category tile that leads nowhere. Biggest categories come first. The picture
    is the first product in the category that has one, taken in a fixed order so
    a tile does not change image between calls.
    """
    limit = max(1, min(limit, CATEGORY_LIMIT))
    ranked = await _grouped_categories()
    chosen = ranked[:limit]
    return {"count": len(chosen), "total": len(ranked), "categories": chosen}


async def _grouped_categories() -> list[dict]:
    """Every category, best first. Cached whole; callers take the slice they need."""
    global _categories_cache
    if _fresh(_categories_cache, settings.SUPPORT_CATEGORY_CACHE_MINUTES):
        return _categories_cache[1]

    groups: dict[str, dict] = {}
    cursor: str | None = None

    for _ in range(CATEGORY_SCAN_PAGES):
        page = (
            await graphql(
                CATEGORY_PRODUCTS,
                {"query": "status:ACTIVE", "first": CATEGORY_SCAN_PAGE, "cursor": cursor},
            )
        )["products"]

        for node in page["nodes"]:
            name = (node.get("productType") or "").strip()
            if not name:
                continue                    # uncategorised: nothing to draw a tile for
            group = groups.setdefault(name, {"count": 0, "image": None, "taxonomy": {}})
            group["count"] += 1
            if group["image"] is None:
                image = (node.get("featuredMedia") or {}).get("image") or {}
                if image.get("url"):
                    group["image"] = image["url"]
            taxon = node.get("category")
            if taxon and taxon.get("id"):
                key = (taxon["id"], taxon.get("name"))
                group["taxonomy"][key] = group["taxonomy"].get(key, 0) + 1

        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    ranked = sorted(groups.items(), key=lambda kv: (-kv[1]["count"], kv[0]))

    out = []
    used: set[str] = set()
    for name, group in ranked:
        # Two type names could in principle slug to the same thing, and a client
        # keys on the id, so it has to stay unique.
        base = slugify(name) or "category"
        ident, n = base, 2
        while ident in used:
            ident, n = f"{base}-{n}", n + 1
        used.add(ident)

        taxon_id, taxon_name = _pick_taxonomy(group["taxonomy"])
        out.append(
            {
                "id": ident,
                "name": name,
                "image": group["image"],
                "image_alt": name,
                "url": category_url(name),
                "product_count": group["count"],
                "taxonomy_id": taxon_id,
                "taxonomy_name": taxon_name,
            }
        )

    _categories_cache = (time.monotonic(), out)
    return out


# ── Collections ────────────────────────────────────────────────────────────

_collections_cache: dict[tuple[int, tuple[str, ...]], tuple[float, dict]] = {}

COLLECTION_SCAN = 250      # how many collections to look at before ranking them
COLLECTION_LIMIT = 100     # most a caller may ask for


def collection_url(handle: str) -> str:
    return f"https://{store_domain()}/collections/{handle}"


def _public_collection(node: dict) -> dict:
    """A collection as a card: a name, a picture and somewhere to click.

    ``id``/``name`` are the same fields /support/categories returns, so a client
    can draw either kind of tile with one component. ``handle``/``title`` are
    kept beside them because that is what Shopify calls these and what the
    welcome screen already sends.

    Falls back to the first product's photo when the collection has no image of
    its own, so the storefront never has to draw an empty tile.
    """
    image = (node.get("image") or {}).get("url")
    if not image:
        first = (node.get("products") or {}).get("nodes") or []
        if first:
            image = ((first[0].get("featuredMedia") or {}).get("image") or {}).get("url")
    return {
        "id": node["handle"],
        "name": node["title"],
        "handle": node["handle"],
        "title": node["title"],
        "image": image,
        "image_alt": node["title"],
        "url": collection_url(node["handle"]),
        "product_count": (node.get("productsCount") or {}).get("count") or 0,
    }


async def collections(limit: int = 8, handles: list[str] | None = None) -> dict:
    """Collections to offer a shopper who has not asked for anything yet.

    ``handles`` pins an exact, ordered list - what the merchant wants shown.
    Without it they come back alphabetically, which is the order the store's own
    /collections page uses, so the widget and the storefront agree. Empty
    collections are never offered: they are a dead end.
    """
    limit = max(1, min(limit, COLLECTION_LIMIT))
    wanted = tuple(h.strip() for h in (handles or []) if h and h.strip())

    key = (limit, wanted)
    if _fresh(_collections_cache.get(key), settings.SUPPORT_WELCOME_CACHE_MINUTES):
        return _collections_cache[key][1]

    query = (" OR ".join(f"handle:{h}" for h in wanted) if wanted
             else "published_status:published")
    data = await graphql(
        COLLECTIONS,
        {"first": max(len(wanted) * 2, COLLECTION_SCAN) if wanted else COLLECTION_SCAN, "query": query},
    )
    found = [_public_collection(n) for n in data["collections"]["nodes"] if (n.get("productsCount") or {}).get("count")]

    if wanted:
        # Shopify answers a handle search in its own order; the merchant's order
        # is the one that matters here.
        by_handle = {c["handle"]: c for c in found}
        chosen = [by_handle[h] for h in wanted if h in by_handle][:limit]
    else:
        chosen = sorted(found, key=lambda c: c["name"].casefold())[:limit]

    result = {"count": len(chosen), "total": len(found), "collections": chosen}
    _collections_cache[key] = (time.monotonic(), result)
    return result


# ── How the collections nest ───────────────────────────────────────────────
# Worked out from what each collection holds, so no naming convention has to be
# kept up in Shopify. A per-type collection (Bibs) holds every product of exactly
# one type and nothing else; a broad one (Baby Accessories & Gifts) holds every
# product of several types. Anything else - a curated edit - is neither, and is
# left out of the tree.

PRODUCT_COLLECTIONS = """
query SupportProductCollections($cursor: String) {
  products(first: 30, after: $cursor, query: "status:ACTIVE") {
    pageInfo { hasNextPage endCursor }
    nodes {
      legacyResourceId
      title
      handle
      productType
      collections(first: 25) { nodes { handle } }
    }
  }
}
"""

TREE_SCAN_PAGES = 10        # 30 products a page, so up to 300 products

_tree_cache: tuple[float, dict] | None = None


async def collection_tree() -> dict:
    """The published collections as shelves and the broader shelves they sit on.

    Returns:
      type_of          product id, or lowercased title -> product type
      home             product type -> the collection holding exactly that type
      parent           product type -> the narrowest broad collection it sits in
      children         broad collection -> its per-type collections, fullest first
      collection_type  per-type collection -> its product type
      cards            handle -> the public collection card
      ids, titles      lowercased title -> product id, and back to the real title,
                       for resolving a product a shopper names loosely
    """
    global _tree_cache
    if _fresh(_tree_cache, settings.SUPPORT_WELCOME_CACHE_MINUTES):
        return _tree_cache[1]

    published = {c["handle"]: c for c in (await collections(COLLECTION_LIMIT))["collections"]}
    type_of: dict[str, str] = {}
    ids: dict[str, str] = {}
    titles: dict[str, str] = {}
    handles: dict[str, str] = {}
    type_products: dict[str, set[str]] = {}
    coll_products: dict[str, set[str]] = {}
    coll_types: dict[str, set[str]] = {}

    cursor: str | None = None
    for _ in range(TREE_SCAN_PAGES):
        page = (await graphql(PRODUCT_COLLECTIONS, {"cursor": cursor}))["products"]
        for node in page["nodes"]:
            ptype = (node.get("productType") or "").strip()
            pid = str(node.get("legacyResourceId") or "")
            if not ptype or not pid:
                continue
            type_of[pid] = ptype
            type_of[node["title"].strip().lower()] = ptype
            ids[node["title"].strip().lower()] = pid
            titles[pid] = node["title"].strip()
            handles[pid] = node["handle"]
            type_products.setdefault(ptype, set()).add(pid)
            for coll in node["collections"]["nodes"]:
                if coll["handle"] in published:
                    coll_products.setdefault(coll["handle"], set()).add(pid)
                    coll_types.setdefault(coll["handle"], set()).add(ptype)
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    home: dict[str, str] = {}
    collection_type: dict[str, str] = {}
    broad: dict[str, set[str]] = {}
    for handle, types in coll_types.items():
        if coll_products[handle] != set().union(*(type_products[t] for t in types)):
            continue                        # holds part of a type: a curated edit
        if len(types) == 1:
            (only,) = types
            home.setdefault(only, handle)
            collection_type[handle] = only
        else:
            broad[handle] = types

    parent: dict[str, str] = {}
    for handle, types in sorted(broad.items(), key=lambda kv: len(kv[1])):
        for t in types:
            parent.setdefault(t, handle)     # narrowest first, so it wins

    def fullest(h: str) -> tuple:
        return (-published[h]["product_count"], published[h]["title"])

    children = {
        handle: sorted((home[t] for t in types if t in home), key=fullest)
        for handle, types in broad.items()
    }
    tree = {"type_of": type_of, "ids": ids, "titles": titles, "handles": handles,
            "home": home, "parent": parent, "children": children,
            "collection_type": collection_type, "cards": published}
    _tree_cache = (time.monotonic(), tree)
    return tree


# ── Best sellers ───────────────────────────────────────────────────────────
# Shopify's Admin API has no "best selling" sort for products, so the ranking is
# counted here from what has actually been ordered. It is the most expensive read
# the agent can make, so the answer is cached for everyone rather than recomputed
# per shopper.

_best_sellers_cache: dict[tuple[int, int], tuple[float, dict]] = {}
_order_tally_cache: dict[int, tuple[float, tuple[dict, dict, int]]] = {}


def _order_scan_window(days: int) -> str:
    since = datetime.date.today() - datetime.timedelta(days=max(1, days))
    return f"created_at:>={since.isoformat()}"


async def _count_units_sold(days: int) -> tuple[dict[str, int], dict[str, int], int]:
    """Tally units per product id across recent orders.

    Returns (units, orders_containing, orders_scanned). Only line items whose
    product still exists and is ACTIVE are counted, which drops archived and
    deleted lines out of the ranking on its own. ``currentQuantity`` is what is
    left on the line after refunds and removals, so a returned item stops
    counting as a sale.
    """
    units: dict[str, int] = {}
    appearances: dict[str, int] = {}
    scanned = 0
    cursor: str | None = None
    query = _order_scan_window(days)

    for _ in range(max(1, settings.SUPPORT_BEST_SELLER_ORDER_PAGES)):
        page = (
            await graphql(
                BEST_SELLER_ORDERS,
                {"query": query, "first": ORDER_SCAN_PAGE, "lines": ORDER_SCAN_LINES, "cursor": cursor},
            )
        )["orders"]

        for order in page["nodes"]:
            scanned += 1
            if order.get("cancelledAt"):
                continue
            if order.get("test") and not settings.SUPPORT_BEST_SELLERS_COUNT_TEST_ORDERS:
                continue
            in_this_order = set()
            for line in order["lineItems"]["nodes"]:
                product = line.get("product")
                if not product or product.get("status") != "ACTIVE":
                    continue
                quantity = line.get("currentQuantity") or 0
                if quantity <= 0:
                    continue
                units[product["id"]] = units.get(product["id"], 0) + quantity
                in_this_order.add(product["id"])
            for pid in in_this_order:
                appearances[pid] = appearances.get(pid, 0) + 1

        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    return units, appearances, scanned


async def best_sellers(limit: int = 5, days: int | None = None) -> dict:
    """The products ordered most often, newest ``days`` of orders, best first.

    Counted from real orders rather than from a tag, so it stays true as the
    store sells. Products are returned in the same shape as a search result, plus
    ``units_sold`` and ``orders`` so the agent can say how popular something is.
    """
    limit = max(1, min(limit, PRODUCT_LIMIT))
    days = settings.SUPPORT_BEST_SELLER_DAYS if days is None else max(1, days)

    key = (limit, days)
    if _fresh(_best_sellers_cache.get(key), settings.SUPPORT_BEST_SELLER_CACHE_MINUTES):
        return _best_sellers_cache[key][1]

    # The order scan does not depend on how many products were asked for, so it
    # is cached per window and shared by every limit.
    if _fresh(_order_tally_cache.get(days), settings.SUPPORT_BEST_SELLER_CACHE_MINUTES):
        units, appearances, scanned = _order_tally_cache[days][1]
    else:
        units, appearances, scanned = await _count_units_sold(days)
        _order_tally_cache[days] = (time.monotonic(), (units, appearances, scanned))
    currency = (await shop_info())["currency"]

    if not units:
        result = {
            "found": False,
            "reason": "no_sales_yet",
            "days": days,
            "orders_scanned": scanned,
            "currency": currency,
            "count": 0,
            "products": [],
        }
        _best_sellers_cache[key] = (time.monotonic(), result)
        return result

    # Most units first; a tie goes to the product that appeared in more orders,
    # which is the better signal of breadth over one large basket.
    ranked = sorted(units, key=lambda pid: (-units[pid], -appearances.get(pid, 0)))[:limit]

    data = await graphql(PRODUCTS_BY_IDS, {"ids": ranked, "variants": VARIANT_LIMIT})
    by_id = {n["id"]: n for n in data["nodes"] if n and n.get("status") == "ACTIVE"}

    products = []
    for rank, pid in enumerate((p for p in ranked if p in by_id), start=1):
        card = _public_product(by_id[pid], currency)
        card["rank"] = rank
        card["units_sold"] = units[pid]
        card["orders"] = appearances.get(pid, 0)
        products.append(card)

    result = {
        "found": bool(products),
        "days": days,
        "orders_scanned": scanned,
        "currency": currency,
        "count": len(products),
        "products": products,
    }
    _best_sellers_cache[key] = (time.monotonic(), result)
    return result


async def cart_cards(lines: list[dict], currency: str | None = None) -> list[dict]:
    """Give the shopper's own cart lines a picture and a link.

    The widget sends handles and variant ids but no imagery, so look the products
    up and attach the image for the exact variant in the cart - the blue hairband
    rather than whichever one happens to be featured.
    """
    handles = [str(line.get("handle")).strip() for line in lines if line.get("handle")]
    products: dict[str, dict] = {}
    if handles:
        joined = " OR ".join(f"handle:{h}" for h in dict.fromkeys(handles))
        data = await graphql(CART_PRODUCTS, {"query": joined, "first": min(len(handles) + 5, 50)})
        products = {node["handle"]: node for node in data["products"]["nodes"]}

    cards = []
    for line in lines:
        handle = str(line.get("handle") or "")
        node = products.get(handle)
        variant_id = str(line.get("variant_id") or "") or None
        image = None
        if node:
            variant = next(
                (v for v in node["variants"]["nodes"] if str(v.get("legacyResourceId")) == variant_id),
                None,
            )
            image = (variant_image(variant) if variant else None) or product_image(node)
        cards.append(
            {
                "product_id": str(line.get("product_id")) if line.get("product_id") else (
                    node.get("legacyResourceId") if node else None
                ),
                "variant_id": variant_id,
                "title": line.get("title") or (node["title"] if node else None),
                "option": line.get("variant_title"),
                "quantity": line.get("quantity") or 1,
                "price": minor_to_major(line.get("line_price")),
                "currency": currency,
                "image": image,
                "url": product_url(node, variant_id) if node else None,
            }
        )
    return cards


def minor_to_major(value) -> float | None:
    """Shopify sends cart money in the currency's minor unit: 2635 is 26.35."""
    if value is None:
        return None
    try:
        return round(int(value) / 100, 2)
    except (TypeError, ValueError):
        return None


# ── One category's products ────────────────────────────────────────────────
# A client draws tiles from /categories and from the welcome collections, so a
# shopper can arrive holding any of four things: the slug id of a product type,
# a collection handle, a numeric collection id, or just the words they typed.
# All four have to reach the same products, so the reference is resolved here
# rather than in the tool, and the caller never has to know which kind it was.

CATEGORY_PRODUCT_LIMIT = 24    # most products one category may return
CATEGORY_SUGGESTIONS = 12      # categories offered back when the name misses

_DIGITS_RE = re.compile(r"^\d+$")
_COLLECTION_GID_RE = re.compile(r"^gid://shopify/Collection/(\d+)$", re.I)

COLLECTION_LOOKUP = """
query SupportCollectionLookup($query: String!, $first: Int!) {
  collections(first: $first, query: $query) {
    nodes { legacyResourceId handle title image { url altText } productsCount { count } }
  }
}
"""

COLLECTION_BY_ID = """
query SupportCollectionById($id: ID!) {
  collection(id: $id) {
    legacyResourceId handle title image { url altText } productsCount { count }
  }
}
"""


# RELEVANCE ranks against a search term, and a category browse has none: with a
# bare collection_id filter Shopify answers it with nothing at all. Alphabetical
# is both correct here and stable, so a grid keeps its order between calls.
CATEGORY_PRODUCT_LIST = """
query SupportCategoryProductList($query: String!, $first: Int!, $variants: Int!) {
  products(first: $first, query: $query, sortKey: TITLE) {
    nodes {
      legacyResourceId
      title
      handle
      productType
      onlineStoreUrl
      totalInventory
      featuredMedia { ... on MediaImage { image { url altText } } }
      variants(first: $variants) {
        nodes {
          legacyResourceId
          sku
          title
          price
          compareAtPrice
          availableForSale
          inventoryQuantity
          media(first: 1) { nodes { ... on MediaImage { image { url } } } }
        }
      }
    }
  }
}
"""


def _quoted(value: str) -> str:
    """A value safe to sit inside double quotes in a Shopify search query."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _type_as_category(entry: dict) -> dict:
    """A product-type category, plus the filter that finds its products.

    The listing calls the type name; product_type is only carried when a
    caller kept it, so the name is what this trusts.
    """
    product_type = entry.get("product_type") or entry["name"]
    return {
        **entry,
        "kind": "product_type",
        "filter": f'product_type:"{_quoted(product_type)}"',
    }


def _collection_as_category(node: dict) -> dict:
    """A collection wearing the same shape as a product-type category."""
    image = node.get("image") or {}
    return {
        "id": node["handle"],
        "name": node["title"],
        "kind": "collection",
        "image": image.get("url"),
        "image_alt": image.get("altText") or node["title"],
        "url": collection_url(node["handle"]),
        "product_count": (node.get("productsCount") or {}).get("count") or 0,
        "filter": f'collection_id:{node["legacyResourceId"]}',
    }


async def _collection_named(term: str) -> dict | None:
    """A collection from a numeric id, a handle, or its name."""
    gid = _COLLECTION_GID_RE.match(term)
    numeric = gid.group(1) if gid else (term if _DIGITS_RE.match(term) else None)
    if numeric:
        node = (
            await graphql(COLLECTION_BY_ID, {"id": f"gid://shopify/Collection/{numeric}"})
        )["collection"]
        return _collection_as_category(node) if node else None

    slug = slugify(term)
    for search in (f"handle:{slug}", f'title:"{_quoted(term)}"'):
        nodes = (
            await graphql(COLLECTION_LOOKUP, {"query": search, "first": 5})
        )["collections"]["nodes"]
        # Shopify answers a title search loosely, so prefer a real match over
        # merely the first thing it offered.
        exact = [
            n for n in nodes
            if n["handle"].lower() == slug or n["title"].strip().lower() == term.lower()
        ]
        chosen = exact or nodes
        if chosen:
            return _collection_as_category(chosen[0])
    return None


# What a shopper calls it -> the category id the store keeps it under. Only
# words for things actually stocked: a synonym pointing at a category the shop
# does not have resolves to nothing, which is the same as not listing it.
_CATEGORY_SYNONYMS = {
    "pant": "trousers", "pants": "trousers", "trouser": "trousers",
    "legging": "trousers", "leggings": "trousers", "bottoms": "trousers",
    "pyjama": "sleepsuit", "pyjamas": "sleepsuit", "pajama": "sleepsuit",
    "pajamas": "sleepsuit", "pjs": "sleepsuit", "sleepwear": "sleepsuit",
    "nightwear": "sleepsuit", "onesie": "sleepsuit", "babygrow": "sleepsuit",
    "babygro": "sleepsuit", "sleepsuits": "sleepsuit",
    "sneaker": "shoes", "sneakers": "shoes", "trainer": "shoes",
    "trainers": "shoes", "plimsoll": "shoes", "plimsolls": "shoes",
    "pump": "shoes", "pumps": "shoes", "footwear": "shoes",
    "jumper": "sweater", "jumpers": "sweater", "pullover": "sweater",
    "knit": "sweater", "knitwear": "sweater", "sweatshirt": "sweater",
    "wellies": "boots", "wellingtons": "boots", "bootie": "boots",
    "booties": "boots",
    "tee": "shirt", "tshirt": "shirt", "top": "shirt", "tops": "shirt",
    "beanie": "hat", "cap": "hat", "sunhat": "hat", "bonnet": "hat",
    "hats": "hat",
    "headband": "hairband", "bow": "hairband", "bows": "hairband",
    "hairbow": "hairband", "clip": "hairband", "clips": "hairband",
    "shades": "sunglasses", "sunnies": "sunglasses", "glasses": "sunglasses",
    "teddy": "toys", "bear": "toys", "softtoy": "toys", "comforter": "toys",
    "toy": "toys",
    "blankie": "blanket", "swaddle": "blanket", "throw": "blanket",
    "playsuit": "romper", "dungarees": "romper", "rompers": "romper",
    "purse": "bag", "tote": "bag", "bags": "bag",
    "glove": "mittens", "gloves": "mittens", "mitten": "mittens",
    "sock": "socks",
    "pinafore": "dress", "frock": "dress", "gown": "dress",
}


def _synonym_target(term: str, listed: list[dict]) -> dict | None:
    """The category a shopper's own word points at, if we stock it."""
    slug = slugify(term)
    for candidate in [slug, *slug.split("-")]:
        target = _CATEGORY_SYNONYMS.get(candidate)
        if not target:
            continue
        for entry in listed:
            if entry["id"] == target:
                return entry
    return None


# Close enough to be a typo, far enough that "laptops" is not a Blanket. Raising
# this rejects real slips; lowering it starts inventing categories, which is the
# worse failure - a shopper told we stock something we do not.
_TYPO_RATIO = 0.82
_TYPO_MIN_LENGTH = 4


def _typo_target(term: str, listed: list[dict]) -> dict | None:
    """The category a misspelling was reaching for - "paijamas", "dreses".

    Matched against our own category names and every word a shopper might use
    for one, so a slip in either vocabulary still lands.
    """
    vocabulary: dict[str, str] = {e["id"]: e["id"] for e in listed}
    vocabulary.update({slugify(e["name"]): e["id"] for e in listed})
    vocabulary.update({alias: target for alias, target in _CATEGORY_SYNONYMS.items()
                       if any(e["id"] == target for e in listed)})

    for candidate in [slugify(term), *slugify(term).split("-")]:
        if len(candidate) < _TYPO_MIN_LENGTH:
            continue
        near = difflib.get_close_matches(candidate, vocabulary, n=1, cutoff=_TYPO_RATIO)
        if not near:
            continue
        wanted = vocabulary[near[0]]
        for entry in listed:
            if entry["id"] == wanted:
                logger.info("Read %r as the category %r", term, entry["name"])
                return entry
    return None


async def find_category(reference: str) -> dict | None:
    """The category a shopper meant, whatever they called it.

    Product types are tried first: they are what ``/categories`` hands a client,
    so an id coming back is far likelier to be one of those than a collection.
    """
    term = " ".join((reference or "").split()).strip()
    if not term:
        return None
    lowered, slug = term.lower(), slugify(term)

    listed = await _grouped_categories()
    for entry in listed:
        names = (entry["id"].lower(), entry["name"].lower(), (entry.get("product_type") or "").lower())
        if lowered in names or (entry.get("taxonomy_id") and term == entry["taxonomy_id"]):
            return _type_as_category(entry)
    # "dresses" should still reach "dress"; two letters would match far too much.
    if len(slug) >= 3:
        for entry in listed:
            if slug.startswith(entry["id"]):
                return _type_as_category(entry)
            # Matching the other way needs a longer word to be meant: at three
            # letters "car" reached Cardigan, and "bel" would reach Belt.
            if len(slug) >= 4 and entry["id"].startswith(slug):
                return _type_as_category(entry)

    try:
        named = await _collection_named(term)
    except (ShopifyError, KeyError, ValueError):
        logger.warning("Collection lookup failed for %r", term, exc_info=True)
        named = None
    if named is not None:
        return named

    # The shopper's own word for one of our categories - "pants" for Trousers,
    # "pyjamas" for a Sleepsuit. Checked before the loose word match below so
    # the mapping wins over an accidental prefix collision.
    synonym = _synonym_target(term, listed)
    if synonym is not None:
        return _type_as_category(synonym)

    typo = _typo_target(term, listed)
    if typo is not None:
        return _type_as_category(typo)

    # Last resort: a category word sitting anywhere in the phrase. The prefix
    # rule above only fires when it comes first, so "dresses in white" resolved
    # and "white dresses" did not - the same request, answered two ways. A real
    # collection has already had its chance, so nothing is stolen from one here.
    for word in dict.fromkeys(slugify(term).split("-")):
        if len(word) < 3:
            continue
        for entry in listed:
            if word == entry["id"] or word.startswith(entry["id"]):
                return _type_as_category(entry)
            # The other direction only for a word long enough to mean it: three
            # letters prefix far too much, and "car" was answering with Cardigan.
            if len(word) >= 4 and entry["id"].startswith(word):
                return _type_as_category(entry)
    return None


# Spellings that differ but mean the same shelf. Shopify's search matches the
# word as written - "pyjamas" does not find "Pyjama" - so the variants are tried
# explicitly rather than hoped for.
_SPELLINGS = [("pyjama", "pajama"), ("grey", "gray"), ("colour", "color")]


def search_variants(term: str) -> list[str]:
    """The forms of a word worth searching titles for.

    Singular as well as plural, because Shopify does not stem: a shopper asking
    for "pyjamas" would otherwise miss a product called "Pyjama Trousers".
    """
    base = " ".join((term or "").split()).lower()
    if not base:
        return []
    forms = {base}
    for word in list(forms):
        if word.endswith("s") and len(word) > 3:
            forms.add(word[:-1])
    for word in list(forms):
        for left, right in _SPELLINGS:
            if left in word:
                forms.add(word.replace(left, right))
            if right in word:
                forms.add(word.replace(right, left))
    # Singularise the newly spelled forms too.
    for word in list(forms):
        if word.endswith("s") and len(word) > 3:
            forms.add(word[:-1])
    return [f for f in forms if len(f) >= 3]


async def _named_like(term: str, currency: str, limit: int) -> list[dict]:
    """Products whose own name carries the shopper's word."""
    variants = search_variants(term)
    if not variants:
        return []
    joined = " OR ".join(f'title:*{v}*' for v in variants)
    try:
        data = await graphql(
            PRODUCT_SEARCH,
            {"query": f"({joined}) AND status:ACTIVE", "first": limit, "variants": VARIANT_LIMIT},
        )
    except (ShopifyError, KeyError, ValueError):
        logger.warning("Name search failed for %r", term, exc_info=True)
        return []
    return [_public_product(node, currency) for node in data["products"]["nodes"]]


async def category_products(category: str, limit: int = 12) -> dict:
    """Everything buyable in one category, for a shopper who named or tapped it.

    Only ACTIVE products, like every other read here. found=false carries the
    categories that do exist, so a caller can offer real ones rather than
    apologising into a void.
    """
    limit = max(1, min(limit, CATEGORY_PRODUCT_LIMIT))
    found = await find_category(category)
    if found is None:
        listed = await _grouped_categories()
        return {
            "found": False,
            "asked_for": category,
            "reason": "no_such_category",
            # The whole card, not just the name: this list is drawn as tiles, so
            # dropping the picture and the link left the shopper reading words.
            "categories": [
                {k: c[k] for k in ("id", "name", "image", "image_alt", "url", "product_count")}
                for c in listed[:CATEGORY_SUGGESTIONS]
            ],
        }

    currency = (await shop_info())["currency"]
    data = await graphql(
        CATEGORY_PRODUCT_LIST,
        {
            "query": f'{found["filter"]} AND status:ACTIVE',
            "first": limit,
            "variants": VARIANT_LIMIT,
        },
    )
    products = [_public_product(node, currency) for node in data["products"]["nodes"]]

    # The shelf is not the only place the word appears. Anything actually called
    # what they asked for belongs in the answer too, whatever category it is
    # filed under - Pyjama Trousers are a fair answer to "pyjamas" even though
    # they live in Trousers.
    known = {p["product_id"] for p in products}
    also_named = []
    for extra in await _named_like(category, currency, limit):
        if extra["product_id"] not in known:
            known.add(extra["product_id"])
            also_named.append(extra)

    return {
        "found": True,
        "category": {k: found[k] for k in ("id", "name", "kind", "image", "url", "product_count")},
        "currency": currency,
        "count": len(products) + len(also_named),
        "more_available": len(products) == limit,
        # Named-for matches are flagged so the agent can say why they are here:
        # they are not in the category the shopper's word resolved to.
        "also_named_like_this": [p["title"] for p in also_named],
        "products": products + also_named,
    }


__all__ = ["ShopifyError", "best_sellers", "cart_cards", "categories", "category_products", "find_category", "category_url", "collections", "collection_url", "customer_orders", "minor_to_major", "order_line_card", "find_order", "product_image", "product_url",
           "search_products", "shop_info", "variant_image"]
