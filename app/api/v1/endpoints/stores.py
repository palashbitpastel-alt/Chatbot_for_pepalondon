"""The stores page: switch each client's shop on or off and set its chat limit.

GET  /stores        the page itself (asks for the review key)
GET  /stores/list   every shop with its plan and this month's chats
POST /stores/plan   save one shop's switch, limit and note

Partner billing: we bill clients ourselves (see services.billing). Every data
call needs the X-Review-Key header to equal REVIEW_KEY, like the review page.
"""

from pathlib import Path

from fastapi import APIRouter, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.api.v1.endpoints.review import _check
from app.services import billing, shops

router = APIRouter(tags=["stores"])

PAGE = Path(__file__).with_name("stores_page.html")


class Plan(BaseModel):
    shop: str = Field(max_length=255)
    active: bool = True
    chat_limit: int | None = Field(default=None, ge=0, le=10_000_000)
    note: str = Field(default="", max_length=300)


@router.get("/stores", response_class=HTMLResponse, include_in_schema=False)
async def stores_page() -> HTMLResponse:
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


@router.get("/stores/list")
async def stores_list(x_review_key: str | None = Header(default=None)) -> dict:
    _check(x_review_key)
    return {"stores": await billing.overview()}


@router.post("/stores/plan")
async def stores_plan(plan: Plan, x_review_key: str | None = Header(default=None)) -> dict:
    _check(x_review_key)
    saved = await billing.set_plan(shops.normalise(plan.shop), plan.active, plan.chat_limit, plan.note)
    return {"shop": shops.normalise(plan.shop), **saved,
            "chats_this_month": await billing.chats_this_month(shops.normalise(plan.shop))}
