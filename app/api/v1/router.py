from fastapi import APIRouter

from app.api.v1.endpoints import chat, dashboard, health, review, shopify, stores, support

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(dashboard.router)
api_router.include_router(chat.router)
api_router.include_router(support.router)
api_router.include_router(shopify.router)
api_router.include_router(review.router)
api_router.include_router(stores.router)
