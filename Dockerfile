# Discord bot + Flask API share this image. Run `bot.py` or `api.py` via
# `docker compose` command override. See docker-compose.yml.
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_FILE=/data/responses.db \
    LOG_DIR=/data/logs

# Predictable chart typography plus the runtime library used by curl_cffi
# wheels on slim images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
        libcurl4 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser \
    && mkdir -p /data/logs \
    && chown -R appuser:appuser /app /data

USER appuser

# Overridden by docker-compose for the API service.
CMD ["python", "bot.py"]
