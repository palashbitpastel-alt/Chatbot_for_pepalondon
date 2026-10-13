import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.core.security import SecurityMiddleware
from app.services.shops import ShopMiddleware
from sqlalchemy import text

from app.api.v1.router import api_router
from app.core.config import settings
from app.db import models  # noqa: F401  (register models on Base.metadata)
from app.db.base import Base
from app.db.migrate import ensure_columns, ensure_vector_index
from app.db.session import AsyncSessionLocal, engine
from app.services import handbook


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def enable_pgvector() -> None:
    """Make the pgvector extension available, in its own transaction.

    Runs before create_all so a table may declare a Vector column. A failure is
    logged rather than fatal — the rest of the API works without vector search,
    and keeping it in a separate transaction stops a failure here from aborting
    table creation on Postgres.
    """
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        logger.info("pgvector extension is available")
    except Exception:
        logger.warning("Could not enable the pgvector extension", exc_info=True)


async def _read_products(shop: str | None = None) -> None:
    """Read one shop's products in the background (each shop on its own)."""
    from app.services import shops
    shops.set_current(shop)  # this task's own context
    try:
        from app.services import outfit, store_kind, suits

        # What kind of shop it is first: the product readings below say it.
        await store_kind.profile()

        catalogue = await outfit.browse_catalogue()
        await suits.learn([p for p in catalogue["products"] if p["in_stock"]])
    except Exception:  # noqa: BLE001 - a shopper's turn reads them anyway
        logger.warning("Could not read the products at startup", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.is_postgres:
        await enable_pgvector()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await ensure_columns(conn)
        await ensure_vector_index(conn)

    # Shopify data (products, orders, everything derived from them) is read live
    # on every request - see services.shopify_store - so there is nothing to
    # sync or seed at startup.

    # The store handbook is versioned with the code, so re-embed it whenever the
    # file has changed. Unchanged, this costs one hash and no embeddings.
    try:
        async with AsyncSessionLocal() as session:
            logger.info("Handbook index: %s", await handbook.reindex(session))
    except Exception:
        logger.exception("Could not index the store handbook")

    # Read what every product is for, worn when, and made of - in the background,
    # so the first shopper does not wait for it. Kept in the database after.
    # Shops that installed the app, saved by their installs.
    from app.services import installs, shops
    await installs.load()
    # Several copies of this server can run side by side: each re-reads the shops
    # every 30 s so billing and settings saved by one reach the others.
    asyncio.create_task(installs.keep_fresh())
    # Old shopper conversations are deleted daily (SUPPORT_CHAT_RETENTION_DAYS).
    from app.services import retention
    asyncio.create_task(retention.keep_swept())
    # Every shop this backend serves, so a newly added shop's first question
    # does not wait for its whole catalogue to be read.
    for shop in sorted(shops.allowed()):
        asyncio.create_task(_read_products(shop))

    logger.info(
        "Retrieval backend: %s (%s)",
        "pgvector" if settings.is_postgres else "SQLite JSON fallback",
        settings.embedding_provider,
    )
    yield


# The interactive docs list every endpoint; they open only where API_DOCS is on.
app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan,
              docs_url="/docs" if settings.API_DOCS else None,
              redoc_url="/redoc" if settings.API_DOCS else None,
              openapi_url="/openapi.json" if settings.API_DOCS else None)


@app.exception_handler(RequestValidationError)
async def _invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    # What was wrong and where - never the caller's input echoed back.
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]})

# Which shop each request is for (see services/shops). Added before CORS so
# CORS wraps it and a refused shop still gets a readable answer.
app.add_middleware(ShopMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    # `allow_origins=["*"]` is not an option here - the spec forbids pairing it
    # with credentials - so storefronts are matched by pattern.
    allow_origin_regex=settings.CORS_ORIGIN_REGEX or None,
    # No cookies are used anywhere: every call carries its own header (the shop,
    # a Shopify session token, the staff key), so browsers send no credentials.
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", "Authorization", "X-Shop-Domain", "X-Review-Key"],
)

# Outermost: sets the caller, applies the rate limits and adds the security
# headers to every response, refusals included.
app.add_middleware(SecurityMiddleware)

app.include_router(api_router, prefix=settings.API_V1_PREFIX)
