# Hosting the backend, and serving more than one shop

The backend is a standard Python web app (FastAPI + uvicorn) configured only by
environment variables, with Postgres reached through `DATABASE_URL`. Nothing in
it depends on Railway, so it runs on any host that runs Python or Docker.

## Serving several shops from one backend

One backend answers every shop the app is installed on, each kept separate: its
own Shopify access, catalogue, caches, saved product readings and chat history.

**Installing is all it takes.** When a merchant installs the app, Shopify opens
the app's page (`/api/v1/shopify/app`) with a signed token naming the shop. The
backend checks the signature, swaps the token for the shop's own access token
and saves the shop in the `shops` table (token encrypted). From then on it
serves that shop. Uninstalling (the `app/uninstalled` webhook) wipes the token.
The merchant then switches the assistant on in Theme editor > App embeds; the
app's page has a button that opens it there.

Optional extras, set as environment variables:

- `SUPPORT_SHOPS`: shops to serve without an install (comma separated
  myshopify domains). Rarely needed now.
- `SUPPORT_SHOP_SETTINGS`: a JSON object keyed by domain with a shop's text -
  `name`, `description`, `welcome_message`, `welcome_collections`,
  `catalogue_filter` - until each shop sets its own in an admin page. A shop
  with none uses its Shopify name and no description.
- `CORS_ORIGINS`: storefronts on `*.myshopify.com` are allowed already; a shop
  served from its own domain (www.example.com) needs that origin here.

The default shop (`SHOPIFY_STORE_URL`) alone uses the handbook file and the
reviewed answer lessons; the other shops answer from their live Shopify data.

**Who can install** depends on the app's distribution in the Dev Dashboard:
custom distribution covers chosen stores; public distribution (Shopify App
Store, after Shopify's review) lets any merchant install it.

The app's address, webhooks and permissions live in `shopify.app.toml` in the
app project and reach Shopify with `shopify app deploy`. If the backend moves
to a new address, update the URLs there and deploy again.

## Moving to another host

Copy every environment variable from Railway (Railway > service > Variables >
Raw Editor shows them all) to the new host, then point the app embed's Chat
endpoint at the new address.

| Host | How |
|---|---|
| Render | New Web Service from the GitHub repo. Build: `pip install -r requirements.txt`. Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Add a Render Postgres and set `DATABASE_URL`. |
| Fly.io, Google Cloud Run, AWS App Runner, DigitalOcean | Build the container: `docker build -f deploy/Dockerfile -t pepa-assistant .` and deploy it; set `DATABASE_URL` to a managed Postgres. |
| A plain server (VPS) | `pip install -r requirements.txt`, then run `uvicorn app.main:app --host 0.0.0.0 --port 8000` behind nginx or Caddy for HTTPS. |

Health check path: `/api/v1/health` (it reports the running commit).

The data to bring along lives in Postgres (chats, saved product readings,
lessons, settings): `pg_dump` from Railway and `pg_restore` into the new
database. Without it the app still starts and rebuilds what it can, but chat
history and reviewed lessons would be lost.

The Dockerfile has not been test-built on the machine it was written on (no
Docker there); build it once on the new host before switching traffic.
