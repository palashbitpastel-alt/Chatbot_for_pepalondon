from pydantic_settings import BaseSettings, SettingsConfigDict


def _async_database_url(url: str) -> str:
    """Turn a plain Postgres URL into one SQLAlchemy's async engine can use.

    Railway injects ``postgresql://...`` (some providers still use the legacy
    ``postgres://``), but the async engine needs an explicit driver. asyncpg also
    rejects the libqp-style query parameters that some providers append, so those
    are dropped — asyncpg negotiates TLS on its own.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgresql+asyncpg://") and "?" in url:
        base, _, query = url.partition("?")
        kept = [
            part
            for part in query.split("&")
            if part and not part.startswith(("sslmode=", "channel_binding=", "options="))
        ]
        url = base + ("?" + "&".join(kept) if kept else "")
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    PROJECT_NAME: str = "Upselling Product API"
    API_V1_PREFIX: str = "/api/v1"
    # Postgres in production (Railway sets this); SQLite is the local default.
    DATABASE_URL: str = "sqlite+aiosqlite:///./upselling.db"
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]
    # Storefronts are a moving target: every dev store is a new origin, and a
    # browser blocks the whole request before the widget sees a reply, which
    # surfaces as "I could not reach our support system" rather than as a CORS
    # error anyone would recognise. So match myshopify hosts by pattern instead
    # of listing them, and keep CORS_ORIGINS for custom domains.
    # Set to "" to allow nothing but the list above.
    CORS_ORIGIN_REGEX: str = r"https://[a-z0-9][a-z0-9-]*\.myshopify\.com"

    # LLM (DeepSeek, OpenAI-compatible)
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL: str = "deepseek-chat"

    # The storefront widget can send who is signed in, but that block comes from
    # the browser and can say anything. Off by default: with it on, anyone who
    # knows a shopper's email can POST it and read that shopper's order history.
    # Only turn it on where the request itself is authenticated (a Shopify App
    # Proxy signature, say), or for a closed demo.
    TRUST_STOREFRONT_CUSTOMER: bool = False

    # Letting a shopper cancel an order or move its delivery address from the
    # chat. These are writes - one refunds money, the other redirects a paid-for
    # parcel - so they are gated harder than a status lookup. Leave the
    # verification on unless the request itself is authenticated: an order number
    # and an email are both guessable, and together they are a weak secret.
    SUPPORT_ORDER_CHANGES: bool = True
    SUPPORT_VERIFY_ORDER_CHANGES: bool = True
    SUPPORT_CHANGE_TTL_MINUTES: int = 15
    SUPPORT_CHANGE_MAX_ATTEMPTS: int = 3
    # Past this, a cancellation is really a return, and a human should handle it.
    SUPPORT_CANCEL_WINDOW_DAYS: int = 14

    # Best sellers are counted from real orders, because the Admin API has no
    # best-selling sort for products. The scan is capped so one chat can never
    # walk the whole order history, and the answer is cached for everyone.
    SUPPORT_BEST_SELLER_DAYS: int = 365
    SUPPORT_BEST_SELLER_ORDER_PAGES: int = 8      # x250 orders = 2000 scanned at most
    SUPPORT_BEST_SELLER_CACHE_MINUTES: int = 15
    # Orders paid through Shopify's test gateway. A live store should leave this
    # off; a demo or dev store has nothing else to rank and needs it on.
    SUPPORT_BEST_SELLERS_COUNT_TEST_ORDERS: bool = True

    # The opening screen. A support chat call with an empty message is the
    # storefront widget saying "a shopper just opened me" - it gets a greeting
    # and a set of collections to tap instead of a trip through the agent.
    # Leave the message blank to greet with the store's own name.
    SUPPORT_WELCOME_MESSAGE: str = ""
    # Exact collections to offer, by handle, in this order. Empty means pick the
    # fullest ones automatically.
    SUPPORT_WELCOME_COLLECTIONS: str = ""
    SUPPORT_WELCOME_COLLECTION_LIMIT: int = 8
    SUPPORT_WELCOME_CACHE_MINUTES: int = 30
    # How the agent introduces the shop when someone says hello. Blank name means
    # Shopify's own shop name; set it when the widget shows a different brand.
    SUPPORT_STORE_NAME: str = ""
    SUPPORT_STORE_DESCRIPTION: str = "clothes, shoes and accessories for babies and young children"

    # Categories are grouped from the live catalogue, which barely moves.
    SUPPORT_CATEGORY_CACHE_MINUTES: int = 30

    # Shopify (New Shop)
    SHOPIFY_CLIENT_ID: str = ""
    SHOPIFY_CLIENT_SECRET: str = ""
    SHOPIFY_STORE_URL: str = ""
    SHOPIFY_ACCESS_TOKEN: str = ""
    # Kept level with .env.example. It is not cosmetic: orderCancel took a
    # boolean `refund` on older versions and an OrderCancelRefundMethodInput on
    # current ones, so a stale default here fails the cancellation at runtime.
    SHOPIFY_API_VERSION: str = "2026-07"

    # Embeddings for the admin agent's RAG memory (pgvector). Any OpenAI-compatible
    # /embeddings endpoint works; with no key set a deterministic local hashing
    # embedder is used so retrieval still works in development.
    EMBEDDING_PROVIDER: str = "auto"  # auto | openai | local
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_BASE_URL: str = "https://api.openai.com/v1"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSIONS: int = 1536
    RAG_MEMORY_RESULTS: int = 4

    @property
    def async_database_url(self) -> str:
        """The DATABASE_URL with an async driver, ready for create_async_engine."""
        return _async_database_url(self.DATABASE_URL)

    @property
    def is_postgres(self) -> bool:
        return self.async_database_url.startswith("postgresql")

    @property
    def embedding_provider(self) -> str:
        if self.EMBEDDING_PROVIDER == "auto":
            return "openai" if self.EMBEDDING_API_KEY else "local"
        return self.EMBEDDING_PROVIDER


settings = Settings()
