# The backend as a container, for any host that runs Docker (Render, Fly.io,
# DigitalOcean App Platform, AWS, Google Cloud Run, a VPS). Build from the
# repository root:  docker build -f deploy/Dockerfile -t pepa-assistant .
# It lives in deploy/ on purpose: Railway builds with a root-level Dockerfile
# when it finds one, and it should keep building from requirements.txt and the
# Procfile as it always has.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app

# Hosts pass the port in $PORT; 8000 when run by hand.
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
