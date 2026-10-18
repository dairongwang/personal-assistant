# Account & Sync Service + (optionally) the built PWA, in one image.
# Build from the project root:   docker build -f services/account/Dockerfile -t personal-assistant .
# In Azure the PWA is served by the Static Web App/CDN instead; this image then only runs the API
# (WEB_DIST unset). Multi-arch friendly (amd64 + arm64).

# ---- stage 1: build the Angular PWA (release.json → release-info.ts + ngsw appData)
FROM node:22-alpine AS web
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY apps/web/ ./
RUN npm run build

# ---- stage 2: the Python service
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data WEB_DIST=/web APP_ENV=prod
WORKDIR /app
COPY services/account/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/account/ /app/
COPY --from=web /web/dist/web/browser /web
RUN useradd --create-home --uid 10001 app && mkdir -p /app/data && chown -R app:app /app /web
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2).status == 200 else 1)"
# Migrate, then serve. (Alembic is a no-op when the schema is current.)
CMD ["sh", "-c", "alembic upgrade head && uvicorn main:app --host 0.0.0.0 --port 8000"]
