FROM python:3.12.13-alpine3.24

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/workspace/apps/api:/workspace

WORKDIR /workspace

COPY requirements.lock /workspace/requirements.lock
RUN pip install --no-cache-dir -r /workspace/requirements.lock

COPY apps/api /workspace/apps/api
COPY services /workspace/services
COPY packages /workspace/packages

RUN addgroup -g 10001 -S firebot \
    && adduser -u 10001 -S -D -H -s /sbin/nologin -G firebot firebot \
    && mkdir -p /data/assets \
    && chown -R firebot:firebot /workspace /data

WORKDIR /workspace/apps/api
USER 10001:10001
