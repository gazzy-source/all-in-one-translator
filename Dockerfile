FROM python:3.13-slim

LABEL org.opencontainers.image.title="All-in-One Translator" \
      org.opencontainers.image.description="Private, per-person Telegram group translator (ephemeral messages)" \
      org.opencontainers.image.source="https://github.com/gazzy-source/all-in-one-translator" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# ffmpeg: video/GIF/voice decoding; libgl1/libglib2.0-0: OpenCV runtime used by the local OCR gate
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install ".[media]"

USER app
VOLUME ["/app/data"]
ENV DATABASE_PATH=/app/data/translator.sqlite3 \
    HEALTH_PORT=8081 \
    LISTEN_HOST=0.0.0.0 \
    LOG_JSON=true

# /healthz is 503 when a queue worker died or the database doesn't answer -> Docker marks it unhealthy
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.environ.get('HEALTH_PORT') or os.environ.get('LISTEN_PORT','8080'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=8).status==200 else 1)"

STOPSIGNAL SIGTERM
CMD ["python", "-m", "translator_bot"]
