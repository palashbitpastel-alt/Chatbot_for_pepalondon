"""Reads the live store from Shopify: products, orders, and everything the
dashboard derives from them. Nothing here is persisted - every call re-fetches
from Shopify fresh (or, when no store is connected, returns a static demo
snapshot so there is still something to look at).

The dashboard, analysis endpoints and the admin agent all call
``fetch_store_snapshot()`` for this. The customer support agent does not go
through here - it reads Shopify live via a different path, ``shopify_storefront``.

What comes from where (the app's scopes are read_products, read_inventory,
read_orders, read_all_orders):

* **Products** - every product and variant, paginated, with stock and unit cost.
* **Orders** - every order, paginated, with financial breakdown, line items,
  payment fees, sales channel and marketing attribution (UTM / discount code).
* **Campaigns** - Shopify's marketing activities when the app has
  ``read_marketing_events``; otherwise campaigns are derived from order
  attribution (utm_campaign, then discount code, then sales channel), each with
  its real order count and revenue. Ad spend and impressions are not in Shopify
  order data, so those stay 0 for derived campaigns.
* **Expenses** - derived per order: cost of goods sold (line quantity x unit
  cost), payment processing fees and sales tax collected. Shopify has no
  general-ledger API, so overheads such as payroll or software are not covered.
* **Tasks** - derived from store state: unfulfilled orders, stock-outs,
  unpaid orders, variants missing a unit cost.

Test-mode checkout orders (Shopify's Bogus Gateway) are fetched and tagged
(``OrderRecord.is_test``) but currently treated identically to real orders
everywhere - included in every count/revenue figure and derived campaign/
expense/task, by deliberate choice while the store is still in a demo/testing
phase and storefront-test checkouts are how orders get placed.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx

from app.core.config import settings
from app.services.shopify_client import ShopifyError, graphql, is_configured  # noqa: F401  (re-exported)

logger = logging.getLogger(__name__)

PAGE_SIZE = 50
DEFAULT_REORDER_LEVEL = 10
ACTIVE_WINDOW_DAYS = 30

SHOP_QUERY = """
{
  shop { name myshopifyDomain currencyCode ianaTimezone primaryDomain { url } }
  currentAppInstallation { accessScopes { handle } }
}
"""

PRODUCTS_QUERY = """
query Products($cursor: String) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        title
        handle
        productType
        vendor
        status
        variants(first: 100) {
          edges {
            node {
              id
              sku
              title
              price
              inventoryQuantity
              inventoryItem { unitCost { amount } }
            }
          }
        }
      }
    }
  }
}
"""

ORDERS_QUERY = """
query Orders($cursor: String) {
  orders(first: 50, after: $cursor, sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        createdAt
        cancelledAt
        test
        sourceName
        displayFinancialStatus
        displayFulfillmentStatus
        totalPriceSet { shopMoney { amount } }
        subtotalPriceSet { shopMoney { amount } }
        totalTaxSet { shopMoney { amount } }
        totalShippingPriceSet { shopMoney { amount } }
        totalDiscountsSet { shopMoney { amount } }
        totalRefundedSet { shopMoney { amount } }
        discountCodes
        shippingAddress { name }
        billingAddress { name }
        customerJourneySummary {
          firstVisit { source utmParameters { campaign source medium } }
          lastVisit { source utmParameters { campaign source medium } }
        }
        lineItems(first: 50) {
          edges {
            node {
              title
              quantity
              sku
              originalUnitPriceSet { shopMoney { amount } }
              variant { inventoryItem { unitCost { amount } } }
            }
          }
        }
        transactions(first: 20) {
          kind
          status
          gateway
          amountSet { shopMoney { amount } }
          fees { amount { amount } }
        }
      }
    }
  }
}
"""

MARKETING_QUERY = """
{
  marketingActivities(first: 100) {
    edges {
      node {
        id
        title
        status
        marketingChannelType
        tactic
        createdAt
        budget { total { amount } }
        utmParameters { campaign source medium }
      }
    }
  }
}
"""

# Scopes that would make more of the dashboard first-party Shopify data.
OPTIONAL_SCOPES = {
    "read_marketing_events": "campaigns come straight from Shopify Marketing instead of order attribution",
    "read_customers": "orders show the customer's name",
    "read_discounts": "discount codes appear as campaigns even before they are used",
}

CHANNEL_LABELS = {
    "web": "Online Store",
    "pos": "Point of Sale",
    "shopify_draft_order": "Draft orders",
    "draft_order": "Draft orders",
    "iphone": "Shopify mobile app",
    "android": "Shopify mobile app",
}


# --------------------------------------------------------------------------- #
# records - what a live (or demo) fetch returns, in place of DB rows
# --------------------------------------------------------------------------- #


@dataclass
class ProductRecord:
    sku: str
    has_sku: bool  # False when `sku` is a synthetic VAR-<id> fallback
    name: str
    product_title: str
    variant_title: str | None
    category: str
    vendor: str | None
    status: str
    price: float
    cost: float
    has_cost: bool
    stock_qty: int
    reorder_level: int
    shopify_product_id: str | None
    shopify_variant_id: str | None
    handle: str | None


@dataclass
class OrderLineRecord:
    sku: str | None
    title: str
    quantity: int
    unit_price: float
    unit_cost: float | None


@dataclass
class OrderRecord:
    order_number: str
    customer_name: str
    created_at: datetime
    status: str
    financial_status: str | None
    channel: str | None
    total: float
    subtotal: float
    tax: float
    shipping: float
    discounts: float
    refunded: float
    discount_code: str | None
    utm_campaign: str | None
    utm_source: str | None
    utm_medium: str | None
    shopify_order_id: str | None
    is_test: bool
    payment_fees: float
    cogs: float
    item_count: int
    lines: list[OrderLineRecord] = field(default_factory=list)


@dataclass
class CampaignRecord:
    name: str
    platform: str
    status: str
    budget: float
    spend: float
    impressions: int
    clicks: int
    conversions: int
    revenue: float
    attribution: str | None
    attribution_key: str | None
    first_order_at: datetime | None
    last_order_at: datetime | None


@dataclass
class ExpenseRecord:
    category: str
    description: str
    amount: float
    expense_date: date
    order_number: str | None


@dataclass
class OpsTaskRecord:
    title: str
    priority: str
    status: str
    due_date: date | None
    domain: str


@dataclass
class StoreSnapshot:
    connected: bool  # False when this is the static demo snapshot (no store configured)
    shop_name: str | None
    currency: str
    timezone: str | None
    scopes: list[str]
    missing_scopes: dict[str, str]
    storefront_url: str | None
    admin_url: str | None
    campaign_source: str
    products: list[ProductRecord]
    orders: list[OrderRecord]  # all orders, including test ones - filter by is_test as needed
    campaigns: list[CampaignRecord]
    expenses: list[ExpenseRecord]
    tasks: list[OpsTaskRecord]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _amount(money_set: dict | None) -> float:
    try:
        return float(((money_set or {}).get("shopMoney") or {}).get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _gid_tail(gid: str | None) -> str | None:
    return gid.rsplit("/", 1)[-1] if gid else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)


def _order_status(node: dict) -> str:
    if node.get("cancelledAt"):
        return "cancelled"
    fulfillment = (node.get("displayFulfillmentStatus") or "").upper()
    if fulfillment == "FULFILLED":
        return "fulfilled"
    if fulfillment in ("IN_PROGRESS", "PARTIALLY_FULFILLED", "SCHEDULED"):
        return "processing"
    return "pending"


def _channel_label(channel: str | None) -> str:
    if not channel:
        return "Online Store"
    return CHANNEL_LABELS.get(channel, channel.replace("_", " ").title())


async def _paginate(client: httpx.AsyncClient, query: str, root: str) -> list[dict]:
    nodes: list[dict] = []
    cursor: str | None = None
    while True:
        data = await graphql(query, {"cursor": cursor}, client=client)
        page = data[root]
        nodes.extend(edge["node"] for edge in page["edges"])
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        cursor = page["pageInfo"]["endCursor"]


# --------------------------------------------------------------------------- #
# products
# --------------------------------------------------------------------------- #


def fetch_products(nodes: list[dict]) -> list[ProductRecord]:
    seen: set[str] = set()
    products: list[ProductRecord] = []
    for p in nodes:
        for v_edge in p["variants"]["edges"]:
            v = v_edge["node"]
            real_sku = (v.get("sku") or "").strip()
            sku = real_sku or f"VAR-{_gid_tail(v['id'])}"
            if sku in seen:  # Shopify allows duplicate SKUs across variants; keep the first
                sku = f"{sku}-{_gid_tail(v['id'])}"
            seen.add(sku)
            variant_title = None if v.get("title") in (None, "Default Title") else v["title"]
            unit_cost = ((v.get("inventoryItem") or {}).get("unitCost") or {}).get("amount")
            products.append(
                ProductRecord(
                    sku=sku,
                    has_sku=bool(real_sku),
                    name=p["title"] if not variant_title else f"{p['title']} - {variant_title}",
                    product_title=p["title"],
                    variant_title=variant_title,
                    category=p.get("productType") or "Uncategorized",
                    vendor=p.get("vendor"),
                    status=(p.get("status") or "ACTIVE").lower(),
                    price=float(v.get("price") or 0),
                    cost=float(unit_cost) if unit_cost is not None else 0.0,
                    has_cost=unit_cost is not None,
                    stock_qty=int(v.get("inventoryQuantity") or 0),
                    reorder_level=DEFAULT_REORDER_LEVEL,
                    shopify_product_id=_gid_tail(p["id"]),
                    shopify_variant_id=_gid_tail(v["id"]),
                    handle=p.get("handle"),
                )
            )
    return products


# --------------------------------------------------------------------------- #
# orders
# --------------------------------------------------------------------------- #


def _attribution(node: dict) -> tuple[str | None, str | None, str | None]:
    journey = node.get("customerJourneySummary") or {}
    for visit_key in ("lastVisit", "firstVisit"):
        visit = journey.get(visit_key) or {}
        utm = visit.get("utmParameters") or {}
        if utm.get("campaign") or utm.get("source"):
            return utm.get("campaign"), utm.get("source") or visit.get("source"), utm.get("medium")
    return None, None, None


def fetch_orders(nodes: list[dict]) -> list[OrderRecord]:
    orders: list[OrderRecord] = []
    for o in nodes:
        ship = (o.get("shippingAddress") or {}).get("name")
        bill = (o.get("billingAddress") or {}).get("name")
        customer_name = ship or bill or ("Walk-in customer" if o.get("sourceName") == "pos" else "Guest")

        fees = 0.0
        for tx in o.get("transactions") or []:
            if (tx.get("status") or "").upper() != "SUCCESS":
                continue
            for fee in tx.get("fees") or []:
                fees += float(((fee.get("amount") or {}).get("amount")) or 0)

        lines: list[OrderLineRecord] = []
        cogs = 0.0
        items = 0
        for li_edge in (o.get("lineItems") or {}).get("edges", []):
            li = li_edge["node"]
            qty = int(li.get("quantity") or 0)
            unit_cost = (((li.get("variant") or {}).get("inventoryItem") or {}).get("unitCost") or {}).get("amount")
            unit_cost_f = float(unit_cost) if unit_cost is not None else None
            lines.append(
                OrderLineRecord(
                    sku=li.get("sku") or None,
                    title=li.get("title") or "Item",
                    quantity=qty,
                    unit_price=_amount(li.get("originalUnitPriceSet")),
                    unit_cost=unit_cost_f,
                )
            )
            items += qty
            if unit_cost_f is not None:
                cogs += unit_cost_f * qty

        codes = o.get("discountCodes") or []
        utm_campaign, utm_source, utm_medium = _attribution(o)

        orders.append(
            OrderRecord(
                order_number=o["name"],
                customer_name=customer_name,
                created_at=_parse_dt(o.get("createdAt")) or datetime.utcnow(),
                status=_order_status(o),
                financial_status=(o.get("displayFinancialStatus") or "").lower() or None,
                channel=o.get("sourceName") or "web",
                total=_amount(o.get("totalPriceSet")),
                subtotal=_amount(o.get("subtotalPriceSet")),
                tax=_amount(o.get("totalTaxSet")),
                shipping=_amount(o.get("totalShippingPriceSet")),
                discounts=_amount(o.get("totalDiscountsSet")),
                refunded=_amount(o.get("totalRefundedSet")),
                discount_code=codes[0] if codes else None,
                utm_campaign=utm_campaign,
                utm_source=utm_source,
                utm_medium=utm_medium,
                shopify_order_id=_gid_tail(o.get("id")),
                is_test=bool(o.get("test")),
                payment_fees=round(fees, 2),
                cogs=round(cogs, 2),
                item_count=items,
                lines=lines,
            )
        )
    return orders


# --------------------------------------------------------------------------- #
# campaigns
# --------------------------------------------------------------------------- #


async def _fetch_marketing_activities(client: httpx.AsyncClient) -> list[dict] | None:
    """Shopify Marketing activities, or None when the app lacks the scope."""
    try:
        data = await graphql(MARKETING_QUERY, client=client)
    except ShopifyError as exc:
        if "access denied" in str(exc).lower() or "read_marketing_events" in str(exc):
            return None
        raise
    return [edge["node"] for edge in data["marketingActivities"]["edges"]]


def _campaign_status(last_order_at: datetime | None) -> str:
    if last_order_at and last_order_at >= datetime.utcnow() - timedelta(days=ACTIVE_WINDOW_DAYS):
        return "active"
    return "inactive"


def derive_campaigns(orders: list[OrderRecord], activities: list[dict] | None) -> tuple[list[CampaignRecord], str]:
    """Pure function of ``orders`` (pass only non-test orders in) - no persistence involved."""
    live = [o for o in orders if o.status != "cancelled"]

    groups: dict[tuple[str, str], dict] = {}

    def add(key: tuple[str, str], name: str, platform: str, order: OrderRecord) -> None:
        g = groups.setdefault(
            key,
            {"name": name, "platform": platform, "orders": 0, "revenue": 0.0, "first": None, "last": None},
        )
        g["orders"] += 1
        g["revenue"] += order.total - order.refunded
        g["first"] = min(g["first"], order.created_at) if g["first"] else order.created_at
        g["last"] = max(g["last"], order.created_at) if g["last"] else order.created_at

    if activities is not None:
        # First-party campaigns; attribute orders to them by utm_campaign.
        by_utm = {
            (a.get("utmParameters") or {}).get("campaign"): a for a in activities if (a.get("utmParameters") or {}).get("campaign")
        }
        matched: set[str] = set()
        for a in activities:
            utm = (a.get("utmParameters") or {}).get("campaign")
            key = ("marketing_activity", _gid_tail(a["id"]) or a["title"])
            groups[key] = {
                "name": a["title"],
                "platform": (a.get("marketingChannelType") or "Shopify Marketing").replace("_", " ").title(),
                "orders": 0,
                "revenue": 0.0,
                "first": _parse_dt(a.get("createdAt")),
                "last": None,
                "budget": float(((a.get("budget") or {}).get("total") or {}).get("amount") or 0),
                "status_override": (a.get("status") or "").lower() or None,
            }
            if utm:
                for o in live:
                    if o.utm_campaign == utm:
                        add(key, a["title"], groups[key]["platform"], o)
                        matched.add(o.order_number)
        for o in live:
            if o.order_number not in matched:
                if o.utm_campaign and o.utm_campaign not in by_utm:
                    add(("utm_campaign", o.utm_campaign), f"UTM: {o.utm_campaign}", o.utm_source or "Web", o)
                elif o.discount_code:
                    add(("discount_code", o.discount_code), f"Code: {o.discount_code}", "Promo code", o)
                else:
                    add(("channel", o.channel or "web"), f"{_channel_label(o.channel)} - direct", "Shopify", o)
        campaign_source = "shopify_marketing"
    else:
        for o in live:
            if o.utm_campaign:
                add(("utm_campaign", o.utm_campaign), f"UTM: {o.utm_campaign}", (o.utm_source or "Web").title(), o)
            elif o.discount_code:
                add(("discount_code", o.discount_code), f"Code: {o.discount_code}", "Promo code", o)
            else:
                add(("channel", o.channel or "web"), f"{_channel_label(o.channel)} - direct", "Shopify", o)
        campaign_source = "order_attribution"

    campaigns = [
        CampaignRecord(
            name=g["name"],
            platform=g["platform"],
            status=g.get("status_override") or _campaign_status(g["last"]),
            budget=round(g.get("budget", 0.0), 2),
            spend=0.0,
            impressions=0,
            clicks=0,
            conversions=g["orders"],
            revenue=round(g["revenue"], 2),
            attribution=attribution,
            attribution_key=str(key)[:200],
            first_order_at=g["first"],
            last_order_at=g["last"],
        )
        for (attribution, key), g in groups.items()
    ]
    return campaigns, campaign_source


# --------------------------------------------------------------------------- #
# expenses and tasks
# --------------------------------------------------------------------------- #


def derive_expenses(orders: list[OrderRecord]) -> list[ExpenseRecord]:
    """Pure function of ``orders`` (pass only non-test orders in) - no persistence involved."""
    expenses: list[ExpenseRecord] = []
    for o in orders:
        if o.status == "cancelled":
            continue
        when = (o.created_at or datetime.utcnow()).date()
        # No "Refunds" or "Discounts" rows here: order.total is already net of both
        # (Shopify's totalPriceSet excludes discounts and finance_summary subtracts
        # refunds separately), so writing them as expenses too would double-count
        # them against total_revenue instead of representing a real cash outflow.
        parts = [
            ("Cost of goods sold", f"Landed cost of {o.item_count} item(s) on {o.order_number}", o.cogs),
            ("Payment processing", f"Gateway fees on {o.order_number}", o.payment_fees),
            ("Sales tax (to remit)", f"Tax collected on {o.order_number}", o.tax),
        ]
        for category, description, amount in parts:
            if amount and amount > 0:
                expenses.append(
                    ExpenseRecord(
                        category=category,
                        description=description,
                        amount=round(amount, 2),
                        expense_date=when,
                        order_number=o.order_number,
                    )
                )
    return expenses


def derive_tasks(products: list[ProductRecord], orders: list[OrderRecord], currency: str) -> list[OpsTaskRecord]:
    """Pure function of ``products``/``orders`` (pass only non-test orders in) - no persistence involved."""
    today = date.today()
    tasks: list[OpsTaskRecord] = []

    open_orders = [o for o in orders if o.status in ("pending", "processing")]
    if open_orders:
        value = sum(o.total for o in open_orders)
        tasks.append(
            OpsTaskRecord(
                title=f"Fulfil {len(open_orders)} open order(s) worth {value:,.2f} {currency}",
                priority="high",
                status="open",
                due_date=today + timedelta(days=1),
                domain="operations",
            )
        )
    unpaid = [o for o in orders if o.status != "cancelled" and (o.financial_status or "") in ("pending", "authorized", "partially_paid")]
    if unpaid:
        tasks.append(
            OpsTaskRecord(
                title=f"Collect payment on {len(unpaid)} unpaid order(s)",
                priority="medium",
                status="open",
                due_date=today + timedelta(days=3),
                domain="finance",
            )
        )
    active = [p for p in products if p.status == "active"]
    out = [p for p in active if p.stock_qty <= 0]
    low = [p for p in active if 0 < p.stock_qty <= p.reorder_level]
    if out:
        tasks.append(
            OpsTaskRecord(
                title=f"Restock {len(out)} variant(s) that are out of stock",
                priority="high",
                status="open",
                due_date=today + timedelta(days=2),
                domain="inventory",
            )
        )
    if low:
        tasks.append(
            OpsTaskRecord(
                title=f"Reorder {len(low)} variant(s) at or below reorder level",
                priority="medium",
                status="open",
                due_date=today + timedelta(days=7),
                domain="inventory",
            )
        )
    no_cost = [p for p in active if not p.has_cost]
    if no_cost:
        tasks.append(
            OpsTaskRecord(
                title=f"Add unit cost in Shopify for {len(no_cost)} variant(s) so margins are accurate",
                priority="low",
                status="open",
                due_date=today + timedelta(days=14),
                domain="finance",
            )
        )
    live = [o for o in orders if o.status != "cancelled"]
    unattributed = [o for o in live if not o.utm_campaign and not o.discount_code]
    if live and len(unattributed) / len(live) > 0.5:
        tasks.append(
            OpsTaskRecord(
                title=f"Tag campaign links with UTM parameters - {len(unattributed)} of {len(live)} orders have no campaign attribution",
                priority="low",
                status="open",
                due_date=today + timedelta(days=14),
                domain="marketing",
            )
        )
    return tasks


# --------------------------------------------------------------------------- #
# demo snapshot - used only when no Shopify store is configured
# --------------------------------------------------------------------------- #

_DEMO_PRODUCTS: list[ProductRecord] = [
    ProductRecord(sku="WAL-001", has_sku=True, name="Classic Leather Wallet", product_title="Classic Leather Wallet", variant_title=None, category="Accessories", vendor=None, status="active", price=49.0, cost=18.0, has_cost=True, stock_qty=120, reorder_level=30, shopify_product_id=None, shopify_variant_id=None, handle=None),
    ProductRecord(sku="TOT-002", has_sku=True, name="Canvas Tote Bag", product_title="Canvas Tote Bag", variant_title=None, category="Bags", vendor=None, status="active", price=29.0, cost=9.5, has_cost=True, stock_qty=8, reorder_level=25, shopify_product_id=None, shopify_variant_id=None, handle=None),
    ProductRecord(sku="BOT-003", has_sku=True, name="Stainless Water Bottle", product_title="Stainless Water Bottle", variant_title=None, category="Drinkware", vendor=None, status="active", price=24.0, cost=7.0, has_cost=True, stock_qty=310, reorder_level=50, shopify_product_id=None, shopify_variant_id=None, handle=None),
    ProductRecord(sku="EAR-004", has_sku=True, name="Wireless Earbuds Pro", product_title="Wireless Earbuds Pro", variant_title=None, category="Electronics", vendor=None, status="active", price=129.0, cost=54.0, has_cost=True, stock_qty=15, reorder_level=20, shopify_product_id=None, shopify_variant_id=None, handle=None),
    ProductRecord(sku="TSH-005", has_sku=True, name="Organic Cotton T-Shirt", product_title="Organic Cotton T-Shirt", variant_title=None, category="Apparel", vendor=None, status="active", price=35.0, cost=11.0, has_cost=True, stock_qty=240, reorder_level=60, shopify_product_id=None, shopify_variant_id=None, handle=None),
    ProductRecord(sku="MUG-006", has_sku=True, name="Ceramic Coffee Mug", product_title="Ceramic Coffee Mug", variant_title=None, category="Drinkware", vendor=None, status="active", price=18.0, cost=5.0, has_cost=True, stock_qty=4, reorder_level=40, shopify_product_id=None, shopify_variant_id=None, handle=None),
    ProductRecord(sku="FIT-007", has_sku=True, name="Fitness Resistance Bands", product_title="Fitness Resistance Bands", variant_title=None, category="Sports", vendor=None, status="active", price=22.0, cost=6.5, has_cost=True, stock_qty=95, reorder_level=25, shopify_product_id=None, shopify_variant_id=None, handle=None),
    ProductRecord(sku="DSK-008", has_sku=True, name="Bamboo Desk Organizer", product_title="Bamboo Desk Organizer", variant_title=None, category="Home Office", vendor=None, status="active", price=42.0, cost=16.0, has_cost=True, stock_qty=58, reorder_level=15, shopify_product_id=None, shopify_variant_id=None, handle=None),
]

_DEMO_ORDER_INPUTS = list(
    zip(
        ["Ava Patel", "Liam Chen", "Noah Garcia", "Emma Wilson", "Olivia Brown", "Ethan Davis", "Mia Martinez", "Lucas Kim", "Sophia Lee", "Jackson Wright", "Amelia Clark", "Harper Lewis", "Elijah Walker", "Isabella Hall"],
        [129.0, 78.5, 245.0, 49.0, 322.0, 64.0, 158.0, 89.0, 41.0, 210.0, 96.0, 132.0, 57.0, 74.0],
        ["fulfilled"] * 6 + ["processing"] * 3 + ["pending"] * 4 + ["cancelled"],
    )
)

_DEMO_CAMPAIGNS: list[CampaignRecord] = [
    CampaignRecord(name="Summer Sale Blast", platform="Meta", status="active", budget=5000, spend=3620, impressions=412000, clicks=9800, conversions=430, revenue=15480, attribution=None, attribution_key=None, first_order_at=None, last_order_at=None),
    CampaignRecord(name="Google Shopping Core", platform="Google", status="active", budget=8000, spend=6150, impressions=530000, clicks=12400, conversions=610, revenue=24900, attribution=None, attribution_key=None, first_order_at=None, last_order_at=None),
    CampaignRecord(name="TikTok Creator Push", platform="TikTok", status="active", budget=3000, spend=2210, impressions=890000, clicks=15600, conversions=280, revenue=7840, attribution=None, attribution_key=None, first_order_at=None, last_order_at=None),
    CampaignRecord(name="Retargeting Q3", platform="Meta", status="paused", budget=2000, spend=1980, impressions=150000, clicks=4300, conversions=190, revenue=6650, attribution=None, attribution_key=None, first_order_at=None, last_order_at=None),
    CampaignRecord(name="Email Win-back", platform="Email", status="active", budget=800, spend=420, impressions=54000, clicks=6100, conversions=350, revenue=9100, attribution=None, attribution_key=None, first_order_at=None, last_order_at=None),
]


def _demo_expenses(today: date) -> list[ExpenseRecord]:
    return [
        ExpenseRecord(category="Advertising", description="Meta + Google ad spend", amount=9770, expense_date=today - timedelta(days=5), order_number=None),
        ExpenseRecord(category="Logistics", description="3PL fulfillment fees", amount=2140, expense_date=today - timedelta(days=7), order_number=None),
        ExpenseRecord(category="Software", description="SaaS subscriptions (Shopify, tools)", amount=640, expense_date=today - timedelta(days=10), order_number=None),
        ExpenseRecord(category="Payroll", description="Part-time warehouse staff", amount=3800, expense_date=today - timedelta(days=12), order_number=None),
        ExpenseRecord(category="Logistics", description="Inbound freight for restock", amount=1275, expense_date=today - timedelta(days=15), order_number=None),
    ]


def _demo_tasks(today: date) -> list[OpsTaskRecord]:
    return [
        OpsTaskRecord(title="Restock low inventory from supplier", priority="high", status="open", due_date=today + timedelta(days=2), domain="operations"),
        OpsTaskRecord(title="Ship pending orders batch", priority="high", status="in_progress", due_date=today + timedelta(days=1), domain="operations"),
        OpsTaskRecord(title="Quarterly supplier contract review", priority="medium", status="open", due_date=today + timedelta(days=14), domain="operations"),
        OpsTaskRecord(title="Update SOP for returns handling", priority="low", status="open", due_date=today + timedelta(days=21), domain="operations"),
        OpsTaskRecord(title="Warehouse cycle count - Zone B", priority="medium", status="done", due_date=today - timedelta(days=3), domain="operations"),
    ]


def _demo_snapshot() -> StoreSnapshot:
    """A store worth looking at before Shopify is connected. Hand-authored, not
    derived from the demo orders (there's no UTM/discount data to derive from) -
    mirrors what the old ``db.seed`` fixture used to insert."""
    now = datetime.utcnow()
    orders = [
        OrderRecord(
            order_number=f"ORD-10{i:02d}",
            customer_name=name,
            created_at=now - timedelta(days=i),
            status=status,
            financial_status=None,
            channel=None,
            total=total,
            subtotal=0.0,
            tax=0.0,
            shipping=0.0,
            discounts=0.0,
            refunded=0.0,
            discount_code=None,
            utm_campaign=None,
            utm_source=None,
            utm_medium=None,
            shopify_order_id=None,
            is_test=False,
            payment_fees=0.0,
            cogs=0.0,
            item_count=0,
            lines=[],
        )
        for i, (name, total, status) in enumerate(_DEMO_ORDER_INPUTS)
    ]
    today = date.today()
    return StoreSnapshot(
        connected=False,
        shop_name=None,
        currency="USD",
        timezone=None,
        scopes=[],
        missing_scopes={},
        storefront_url=None,
        admin_url=None,
        campaign_source="demo",
        products=_DEMO_PRODUCTS,
        orders=orders,
        campaigns=_DEMO_CAMPAIGNS,
        expenses=_demo_expenses(today),
        tasks=_demo_tasks(today),
    )


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


async def fetch_store_snapshot() -> StoreSnapshot:
    """The whole store, fetched fresh from Shopify - or a static demo snapshot
    when no store is configured. No caching: every call is a fresh read, by
    design (see the "zero caching" decision in the migration plan)."""
    if not is_configured():
        return _demo_snapshot()

    async with httpx.AsyncClient() as client:
        shop_data = await graphql(SHOP_QUERY, client=client)
        product_nodes = await _paginate(client, PRODUCTS_QUERY, "products")
        order_nodes = await _paginate(client, ORDERS_QUERY, "orders")
        scopes = sorted(s["handle"] for s in shop_data["currentAppInstallation"]["accessScopes"])
        activities = await _fetch_marketing_activities(client) if "read_marketing_events" in scopes else None

    shop = shop_data["shop"]
    currency = shop.get("currencyCode") or "USD"

    products = fetch_products(product_nodes)
    orders = fetch_orders(order_nodes)
    # Shopify checkout-test orders are treated the same as real orders everywhere on
    # purpose right now (demo phase - storefront testing IS how orders get placed).
    campaigns, campaign_source = derive_campaigns(orders, activities)
    expenses = derive_expenses(orders)
    tasks = derive_tasks(products, orders, currency)

    missing = {s: why for s, why in OPTIONAL_SCOPES.items() if s not in scopes}
    # The admin lives under the handle the API is reached on (a renamed store
    # keeps answering on its old myshopifyDomain, but the admin uses the new one).
    configured_host = settings.SHOPIFY_STORE_URL.removeprefix("https://").removeprefix("http://").strip("/")
    handle = (configured_host or shop.get("myshopifyDomain") or "").split(".")[0]
    storefront = ((shop.get("primaryDomain") or {}).get("url") or f"https://{configured_host}").rstrip("/")

    return StoreSnapshot(
        connected=True,
        shop_name=shop.get("name"),
        currency=currency,
        timezone=shop.get("ianaTimezone"),
        scopes=scopes,
        missing_scopes=missing,
        storefront_url=storefront,
        admin_url=f"https://admin.shopify.com/store/{handle}" if handle else None,
        campaign_source=campaign_source,
        products=products,
        orders=orders,
        campaigns=campaigns,
        expenses=expenses,
        tasks=tasks,
    )
