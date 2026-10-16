# Build from the project root so the frontend is included:
#   docker build -f backend/Dockerfile -t personal-assistant .
# Multi-arch friendly (amd64 + arm64: Intel/AMD PCs, Apple silicon, Raspberry Pi).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data \
    FRONTEND_DIR=/frontend

WORKDIR /app

# Dependencies first for better layer caching.
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ /app/
COPY frontend/ /frontend/

# Run as an unprivileged user; the data volume must be writable by it.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data \
    && chown -R app:app /app /frontend
USER app

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
