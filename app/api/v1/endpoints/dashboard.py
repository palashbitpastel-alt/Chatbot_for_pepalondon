from fastapi import APIRouter, HTTPException

from app.services import insights
from app.services.insights import SUMMARY_FUNCS
from app.services.shopify_store import fetch_store_snapshot

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/summary")
async def dashboard_summary() -> dict:
    """Headline KPIs for all four domains, shown in the top section of the dashboard."""
    snapshot = await fetch_store_snapshot()
    s = await insights.all_summaries(snapshot)
    inventory, marketing, operations, finance = s["inventory"], s["marketing"], s["operations"], s["finance"]
    return {
        "meta": insights.store_meta(snapshot),
        "inventory": {
            k: inventory[k]
            for k in ("total_skus", "total_units", "stock_value", "retail_value", "low_stock_count", "out_of_stock_count", "missing_cost_count")
        },
        "marketing": {
            k: marketing[k]
            for k in (
                "total_campaigns",
                "active_campaigns",
                "total_spend",
                "total_revenue",
                "total_conversions",
                "roas",
                "attribution_rate_pct",
                "campaign_source",
            )
        },
        "operations": {
            k: operations[k]
            for k in (
                "total_orders",
                "pending_orders",
                "pending_value",
                "fulfillment_rate_pct",
                "orders_last_30d",
                "revenue_last_30d",
                "open_tasks",
                "high_priority_tasks",
                "overdue_tasks",
            )
        },
        "finance": {
            k: finance[k]
            for k in (
                "total_revenue",
                "net_sales",
                "cogs",
                "gross_profit",
                "gross_margin_pct",
                "total_expenses",
                "net_profit",
                "profit_margin_pct",
                "unpaid_orders",
                "refunds",
            )
        },
        "health": insights.derive_health_score(s),
    }


@router.get("/dashboard/priority-actions")
async def dashboard_priority_actions() -> dict:
    """Ranked, explainable actions across all four domains."""
    return await insights.priority_actions()


@router.get("/dashboard/health")
async def dashboard_health() -> dict:
    """The business health scoreboard: per-domain and overall scores with their components."""
    return await insights.health_score()


@router.get("/analysis/{domain}")
async def domain_analysis(domain: str) -> dict:
    """Full detail for one domain, loaded when its accordion is opened."""
    func = SUMMARY_FUNCS.get(domain)
    if func is None:
        raise HTTPException(status_code=404, detail=f"Unknown domain '{domain}'")
    return await func(await fetch_store_snapshot())
