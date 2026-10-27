FROM python:3.12.13-alpine3.24

ARG SOURCE_REVISION=unknown
ARG PIP_INDEX_URL=
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=5 \
    PYTHONPATH=/workspace/apps/api:/workspace

WORKDIR /workspace

COPY requirements.lock /workspace/requirements.lock
# PIP_INDEX_URL 为空时走默认 pypi.org（CI/旧现场不变）；U-Dao 经 compose build.args
# 从 runtime env 注入腾讯云 PyPI 镜像（pypi.org 直连在中国大陆网络不稳定）。
RUN if [ -n "$PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir -i "$PIP_INDEX_URL" -r /workspace/requirements.lock; \
    else \
        pip install --no-cache-dir -r /workspace/requirements.lock; \
    fi

COPY apps/api /workspace/apps/api
COPY services /workspace/services
COPY packages /workspace/packages

RUN addgroup -g 10001 -S firebot \
    && adduser -u 10001 -S -D -H -s /sbin/nologin -G firebot firebot \
    && mkdir -p /data/assets \
    && chown -R firebot:firebot /workspace /data

WORKDIR /workspace/apps/api
USER 10001:10001
