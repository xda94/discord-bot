FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_FILE=/data/responses.db \
    LOG_DIR=/data/logs

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
        libcurl4 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY . .

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser \
    && mkdir -p /data/logs \
    && chown -R appuser:appuser /app /data

USER appuser

CMD ["python", "bot.py"]
