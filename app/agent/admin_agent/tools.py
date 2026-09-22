"""Tools for the admin agent: read-only views over the store's own analytics
(read live from Shopify on every call - see ``services.shopify_store``), plus
keyword lookup over products/orders/campaigns and semantic search over earlier
staff conversations (pgvector)."""

import json

from langchain_core.tools import tool

from app.db.session import AsyncSessionLocal
from app.services import insights, rag
from app.services.shopify_store import fetch_store_snapshot


@tool
async def get_inventory_data() -> str:
    """Get the store's current inventory data: SKUs, stock levels, stock value, out-of-stock and low-stock alerts, variants missing a unit cost, category breakdown and per-product details. Figures are read live from the connected Shopify store."""
    return json.dumps(await insights.inventory_summary(await fetch_store_snapshot()))


@tool
async def get_marketing_data() -> str:
    """Get the store's marketing data: campaigns with platform, status, budget, spend, impressions, clicks, conversions (orders), attributed revenue and ROAS, plus the share of orders attributable to a campaign. When campaign_source is "order_attribution", campaigns were derived from Shopify orders (UTM tags, discount codes, sales channel) and spend/impressions are not available."""
    return json.dumps(await insights.marketing_summary(await fetch_store_snapshot()))


@tool
async def get_operations_data() -> str:
    """Get the store's operations data: orders by fulfilment status and sales channel, orders awaiting fulfilment and their value, fulfilment and cancellation rates, last-30-day volume, recent orders, and operational tasks with priority, due dates and overdue flags."""
    return json.dumps(await insights.operations_summary(await fetch_store_snapshot()))


@tool
async def get_finance_data() -> str:
    """Get the store's finance data: revenue (gross sales net of refunds), net sales, tax and shipping collected, discounts, cost of goods, gross and net profit and margins, unpaid orders, expenses by category, monthly revenue vs expenses, and recent expense lines."""
    return json.dumps(await insights.finance_summary(await fetch_store_snapshot()))


@tool
async def get_priority_actions() -> str:
    """Get the ranked list of recommended actions across inventory, marketing, operations and finance (critical > high > medium > low), each with the metric behind it and a suggested next step. Use for "what should I do first / what needs attention" questions."""
    return json.dumps(await insights.priority_actions())


@tool
async def get_business_health() -> str:
    """Get the business health scoreboard: a 0-100 score, grade and status for each of the four domains and overall, with the weighted components that explain each score. Use for "how is the business doing / how healthy are we" questions."""
    return json.dumps(await insights.health_score())


@tool
async def search_store_knowledge(query: str) -> str:
    """Look up a specific product, SKU, order number, customer name or campaign (live keyword match against the connected Shopify store), and recall earlier conversations with staff (semantic search). Pass a short natural-language query."""
    store_hits = insights.search_store(await fetch_store_snapshot(), query, limit=8)
    async with AsyncSessionLocal() as db:
        memory_hits = await rag.search(db, query, kinds=[rag.CHAT_KIND], limit=4)
    return json.dumps(
        store_hits
        + [
            {
                "kind": h.kind,
                "content": h.content,
                "score": h.score,
                "when": h.created_at.isoformat() if h.created_at else None,
            }
            for h in memory_hits
        ]
    )


ADMIN_TOOLS = [
    get_inventory_data,
    get_marketing_data,
    get_operations_data,
    get_finance_data,
    get_priority_actions,
    get_business_health,
    search_store_knowledge,
]
