# Hosting the backend, and serving more than one shop

The backend is a standard Python web app (FastAPI + uvicorn) configured only by
environment variables, with Postgres reached through `DATABASE_URL`. Nothing in
it depends on Railway, so it runs on any host that runs Python or Docker.

## Serving several shops from one backend

One backend answers every shop the app is installed on. Each shop is kept
separate: its own Shopify access, catalogue, caches, saved product readings and
chat history.

1. **Install the app on the shop.** Dev Dashboard > Pepa AI Chatbot >
   Distribution / Install app, choose the shop, approve the permissions. The
   backend then gets that shop's access with the same `SHOPIFY_CLIENT_ID` and
   `SHOPIFY_CLIENT_SECRET` (client credentials; the shop must be in the same
   organisation as the app).
2. **Allow the shop on the backend.** Add its permanent myshopify domain to
   `SUPPORT_SHOPS` (comma separated):

   ```
   SUPPORT_SHOPS=second-store.myshopify.com,third-store.myshopify.com
   ```

   `SHOPIFY_STORE_URL` stays the default shop. A shop that is not listed is
   refused.
3. **Give it its own text (optional).** `SUPPORT_SHOP_SETTINGS` is a JSON
   object keyed by domain. Every field is optional; a shop with none uses its
   Shopify name and no description.

   ```
   SUPPORT_SHOP_SETTINGS={"second-store.myshopify.com": {"name": "Second Store", "description": "toys and gifts for children", "welcome_message": "", "welcome_collections": "", "catalogue_filter": ""}}
   ```

4. **Turn the assistant on in that shop's theme.** Online Store > Themes >
   Customize > App embeds > Pepa Assistant. The Chat endpoint is the same for
   every shop. The widget tells the backend which shop it is on by itself.
5. **Custom domains.** Storefronts on `*.myshopify.com` are allowed already.
   A shop served from its own domain (www.example.com) needs that origin added
   to `CORS_ORIGINS`.

For now the default shop alone uses the handbook file and the reviewed answer
lessons; the other shops answer from their live Shopify data.

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
