# PROJECT_DESCRIPTION.md

**Project:** AI-driven customer support chatbot for a Shopify store
**Store:** PalashStor
**Myshopify domain:** `palashstor.myshopify.com`
**Storefront theme branding:** "Pepa London" (children's / babywear theme)
**Last updated:** 2026-09-02

> **How to read this file.** Lines marked ✅ are confirmed. Lines marked ⚠️ are *assumed from
> naming conventions* and must be verified in the Shopify admin before the bot uses them —
> a broken link sent to a customer is worse than no link. Sections marked 🔲 are empty and
> must be filled in by you; do not let the bot invent this content.

---

## 1. What this project is

A chat widget embedded on the storefront, answered by an AI assistant, with a human agent
fallback. The widget currently handles:

- Order status lookups (order number + email)
- Product and category questions
- Policy questions (refunds, shipping, returns)
- General store navigation

Planned / in progress:

- Add-to-cart from within the chat
- Product recommendations from the live catalog

**Widget footer text in use:** "AI assistant — a human agent is one message away."
**Widget header disclaimer:** "This chat is answered by an AI assistant and is subject to the
terms of our Privacy Notice."

---

## 2. Base URLs

| Purpose | URL |
|---|---|
| Myshopify domain ✅ | `https://palashstor.myshopify.com` |
| Custom/primary domain 🔲 | *fill in, or note "none — myshopify is primary"* |
| Admin ✅ | `https://admin.shopify.com/store/palashstor` |
| App config (Admin API) ✅ | `https://admin.shopify.com/store/palashstor/settings/apps/development/417832599553/configuration/admin_api_integration` |

**Rule:** the bot must always build links from one configured base URL constant, never
hardcode `palashstor.myshopify.com` in multiple places. When the custom domain goes live,
one variable changes.

---

## 3. Order history & customer account paths

### 3.1 Which account system is this store on?

🔲 **VERIFY FIRST:** Shopify Admin → Settings → Customer accounts. It says either:

- **Classic customer accounts** → the paths in 3.2 work exactly as written.
- **Customer accounts (new)** → `/account` redirects to
  `https://shopify.com/{shop_id}/account`, and `/account/orders/{order_id}` does **not**
  resolve the same way. In that case the bot should link only to `/account` and let Shopify
  handle the redirect.

Record the answer here: 🔲 `__________`

### 3.2 Account paths (classic)

| Purpose | Path | Login required |
|---|---|---|
| Login ⚠️ | `/account/login` | no |
| Register ⚠️ | `/account/register` | no |
| Password reset ⚠️ | `/account/login#recover` | no |
| Account home + order history ⚠️ | `/account` | yes |
| A single order ⚠️ | `/account/orders/{order_id}` | yes |
| Saved addresses ⚠️ | `/account/addresses` | yes |

`{order_id}` is the **numeric order ID**, not the display name. `#1027` is the *name*;
the ID is a different number entirely. Do not interpolate the order number here.

### 3.3 The order status URL — the link the bot should actually send

This is the single most important link in the project. Every Shopify order carries its own
tokenized status page that works **without the customer logging in**.

- REST field: `order.order_status_url`
- GraphQL field: `order { statusPageUrl }`

Shape:

```
https://palashstor.myshopify.com/72345/orders/9f2c8a1e4b7d3f...
```

**Never construct this URL yourself.** Read it off the order object and pass it through.

**Security:** the token *is* the authentication. Anyone holding that link can see the order.
Only return it after the bot has matched **both** the order number **and** the email on the
order. See §9.

---

## 4. Categories / collections

Navigation labels observed on the storefront ✅, with handles derived by Shopify's standard
rule (lowercase, spaces → hyphens). **All handles below are ⚠️ assumed.**

| Nav label ✅ | Assumed path ⚠️ |
|---|---|
| SALE | `/collections/sale` |
| NEW IN | `/collections/new-in` |
| SPRING SUMMER | `/collections/spring-summer` |
| NEWBORN | `/collections/newborn` |
| BABY GIRL | `/collections/baby-girl` |
| BABY BOY | `/collections/baby-boy` |
| GIRL | `/collections/girl` |
| BOY | `/collections/boy` |
| SHOES | `/collections/shoes` |
| CELEBRATION | `/collections/celebration` |
| (label truncated in screenshot — 🔲 confirm) | 🔲 |

**How to get the real list.** Don't guess in production. Either:

- Admin → Products → Collections, and read the handle from each collection's URL, or
- query them, which also lets the bot stay current automatically:

```graphql
query {
  collections(first: 50) {
    edges { node { handle title description productsCount { count } } }
  }
}
```

Best practice: have the bot fetch collections on a schedule and cache handle→title, so a
renamed collection doesn't break its links.

### 4.1 Other catalog paths

| Purpose | Path |
|---|---|
| All products ⚠️ | `/collections/all` |
| Single product ⚠️ | `/products/{handle}` |
| Specific variant ⚠️ | `/products/{handle}?variant={variant_id}` |
| Search ⚠️ | `/search?q={term}` |
| Filtered collection ⚠️ | `/collections/{handle}?filter.v.price.gte=0` |

---

## 5. Cart & checkout paths

| Purpose | Path |
|---|---|
| Cart page ⚠️ | `/cart` |
| Cart permalink (prefilled) ⚠️ | `/cart/{variant_id}:{qty}` |
| Checkout ⚠️ | `/checkout` |

Cart permalink example: `/cart/44123456789:2` drops the shopper into a cart already holding
2 units. No API call needed — useful as a first-pass "add to cart" for the bot.

---

## 6. Policy pages

Shopify auto-generates these paths once the policy has content in
**Settings → Policies**. If a policy is blank, the URL 404s.

| Policy | Path | Has content? |
|---|---|---|
| Refund / return policy ⚠️ | `/policies/refund-policy` | 🔲 |
| Shipping policy ⚠️ | `/policies/shipping-policy` | 🔲 |
| Privacy policy ⚠️ | `/policies/privacy-policy` | 🔲 |
| Terms of service ⚠️ | `/policies/terms-of-service` | 🔲 |
| Contact information ⚠️ | `/policies/contact-information` | 🔲 |
| Contact page ⚠️ | `/pages/contact` | 🔲 |

### 6.1 Refund policy — actual terms

🔲 **TO BE FILLED IN.** The bot must never improvise these numbers.

- Return window: 🔲 ____ days from delivery
- Condition required: 🔲 (unworn, tags attached, original packaging?)
- Who pays return shipping: 🔲
- Refund method and timing: 🔲 (original payment method, ____ business days)
- Exchanges offered: 🔲 yes / no
- Non-returnable items: 🔲 (sale items? underwear? personalised?)
- How to start a return: 🔲 (self-serve portal / email / contact form)

### 6.2 Shipping policy — actual terms

🔲 **TO BE FILLED IN.**

- Ships to: 🔲 (domestic only / international)
- Processing time: 🔲 ____ business days
- Standard delivery time & cost: 🔲
- Express delivery time & cost: 🔲
- Free shipping threshold: 🔲
- Carriers used: 🔲
- Tracking provided: 🔲 yes / no — if yes, the bot returns the status page URL from §3.3
- Customs/duties for international: 🔲

> **Hard rule for the bot:** if a policy value is not present in this file, the answer is
> "let me connect you to a human" — not a plausible-sounding guess. Wrong refund windows
> create real disputes.

---

## 7. API access

### 7.1 Three different APIs, three different jobs

| API | Runs where | Auth | Use it for |
|---|---|---|---|
| **AJAX Cart API** | Browser, on the storefront | Session cookie | Adding to cart when the widget is embedded in the theme |
| **Storefront API** | Server or off-site | Storefront access token | Cart building for off-site channels (WhatsApp, etc.), product lookup |
| **Admin API** | Server only | Admin access token | Order lookup, catalog reads |

**The Admin API has no add-to-cart endpoint.** Carts are a storefront concept. This was the
original point of confusion on the project — see §7.4.

### 7.2 Admin API scopes needed

| Scope | Why |
|---|---|
| `read_orders` | Order status lookup — **required** |
| `read_products` | Resolve a product name to a variant ID |
| `write_orders` | Only if shoppers may cancel or re-address their own orders — see §7.2.1 |
| `read_customers` | Only if the bot needs profile data — adds PII risk, skip if unused |
| `write_draft_orders` | Only if the bot builds draft orders / payment links |

#### 7.2.1 `write_orders` — what it costs you

`write_orders` is the one scope on this list that lets the bot *do* something rather than
report something. It backs two tools, `request_order_change` and `confirm_order_change`,
which cancel an order (refunding and restocking it) or move its delivery address.

That is a real blast radius, so the pair is gated well beyond a status lookup:

- The order number **and** the email on the order must match, and a wrong email is
  indistinguishable from an order that does not exist — the same non-probing behaviour as
  `check_order_status`.
- Nothing is written by step one. It returns a short-lived token and a **challenge**: the
  postcode already on the order (or the order total, where there is no address). The answer
  is never returned to the agent, only compared server-side.
- Three wrong answers burn the ticket. It expires on its own after
  `SUPPORT_CHANGE_TTL_MINUTES`, and it is bound to the chat session it was minted in, so a
  token that leaks out of one transcript cannot be spent in another.
- Cancellation is refused once an order is fulfilled or older than
  `SUPPORT_CANCEL_WINDOW_DAYS`; an address change is refused once anything has shipped.
- The shopper's reason — a preset code, their own words, or both — is written to the order as
  a staff note, so a human can always see why it happened.

The challenge exists because the widget sends `verified: false` on purpose. An order number
is close to sequential and an email address leaks; together they are a weak secret. Fine for
telling someone where their parcel is, not for redirecting it. Set
`SUPPORT_VERIFY_ORDER_CHANGES=false` only where the request itself is authenticated (a signed
App Proxy), and `SUPPORT_ORDER_CHANGES=false` to remove the tools entirely — at which point
`write_orders` can come off the token too.

### 7.3 Scopes that do **not** do what they sound like

- `read_cart_transforms` / `write_cart_transforms` — these are for **Shopify Functions**,
  which *modify* line items already in a cart (bundling, merging, expanding). They cannot
  create a cart or add a product. Not needed for this project.
- `read_all_cart_transforms` — same family, also not needed.
- `read_customer_events` — read-only browsing/analytics data. Includes PII. Not an
  add-to-cart mechanism.

### 7.4 Add to cart — the three implementation paths

**A. Widget embedded in the theme (recommended)** — no token, no scopes:

```js
await fetch('/cart/add.js', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ items: [{ id: VARIANT_ID, quantity: 1 }] })
});
```

Companions: `GET /cart.js` to read, `POST /cart/change.js` to update quantity,
`POST /cart/clear.js` to empty.

**B. Off-site bot (server-side)** — Storefront API mutations `cartCreate` then
`cartLinesAdd`; return the resulting `checkoutUrl` to the user. Requires a Storefront token
with `unauthenticated_write_checkouts` and `unauthenticated_read_product_listings`.

**C. Quick version** — cart permalink from §5. Zero API work.

> **Variant ID gotcha:** the Storefront API uses base64 GIDs
> (`gid://shopify/ProductVariant/44123456789`), the AJAX API uses the plain numeric ID.
> Pick the path before building the product-lookup step, or normalise explicitly.

### 7.5 Order lookup query

```graphql
query($q: String!) {
  orders(first: 1, query: $q) {
    edges {
      node {
        name
        email
        displayFulfillmentStatus
        displayFinancialStatus
        statusPageUrl
        createdAt
        fulfillments { trackingInfo { number url company } }
      }
    }
  }
}
```

Variable: `q = "name:#1027 email:buyer@example.com"`

Normalisation before querying:

- Customers type `1027`, `#1027`, and `PAL1027`. Normalise to one form.
- Lowercase and trim the email before comparing.
- Re-verify the returned `email` against the supplied email in your own code. Don't trust
  the search filter alone to have matched both fields.

---

## 8. Chatbot conversation flows

### 8.1 Order status

1. Ask for **order number** and **email on the order**, together, in one message.
2. Mention the self-serve option up front, so customers who don't want to share details
   have a path: *"You can also see it anytime at palashstor.myshopify.com/account."*
3. On **match**: return fulfillment status, order date, and the `statusPageUrl`.
4. On **no match**: return a single generic message. Do **not** say which field was wrong.
5. After 🔲 ___ failed attempts, hand off to a human.

Current widget copy ✅ (works, but sends no link — add the `/account` link):

> To check your order, I just need two things:
> 1. Your order number (e.g. "#1027" or "1027")
> 2. The email address you placed the order with
> Could you share both with me? Then I'll look it up right away.

### 8.2 Category browsing

Map the customer's words to a collection handle from §4, then link. Keep a synonym map:
"newborn" / "0-3 months" / "infant" → `newborn`; "party" / "wedding" / "occasion" →
`celebration`. If no confident match, fall back to `/search?q={their words}`.

### 8.3 Policy questions

Answer from §6 only. Always include the policy page link so the customer can read the
authoritative version.

---

## 9. Security & privacy rules

1. **Never reveal an order status URL without matching order number + email.** The token is
   the auth; leaking it leaks the order.
2. **Never confirm which field was wrong** on a failed lookup — that turns the bot into an
   order-number enumeration tool.
3. **Rate-limit lookup attempts** per session. 🔲 Set limit: ____
4. **Don't echo full PII back into chat.** Partial address ("delivering to a Kolkata
   address ending 700091") beats the full line.
5. **Keep Admin tokens server-side only.** Never in theme JS, never in the widget bundle.
6. **Scope minimally.** Every extra scope is extra breach surface — this is also Shopify's
   stated requirement: only select the scopes your app needs.
7. **Log lookups** (order name, timestamp, session) for abuse review. 🔲 Retention: ____

---

## 10. Open items

- [ ] Confirm classic vs new customer accounts (§3.1) — blocks all `/account` links
- [ ] Export the real collection handles (§4) and replace the assumed table
- [ ] Confirm the truncated nav label after CELEBRATION
- [ ] Write the refund policy terms into §6.1
- [ ] Write the shipping policy terms into §6.2
- [ ] Confirm which policy pages have content (blank ones 404)
- [ ] Decide add-to-cart path: A, B, or C (§7.4)
- [ ] Confirm custom domain, or record that myshopify is primary
- [ ] Set the failed-lookup handoff threshold and rate limit
- [ ] Test every ⚠️ link in this file and promote it to ✅

---

## 11. Changelog

| Date | Change |
|---|---|
| 2026-09-02 | Initial document created |
