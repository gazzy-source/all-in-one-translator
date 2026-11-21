FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[media]"

USER app
VOLUME ["/app/data"]
ENV DATABASE_PATH=/app/data/translator.sqlite3
CMD ["python", "-m", "translator_bot"]
