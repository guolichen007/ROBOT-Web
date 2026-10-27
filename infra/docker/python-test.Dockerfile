FROM python:3.12.13-alpine3.24

ARG PIP_INDEX_URL=
ARG APK_REPOSITORY_BASE=dl-cdn.alpinelinux.org/alpine

# 中国大陆网络下 dl-cdn 的 TLS 被断；docker-cli 在 community 仓库，需同时替换两源
RUN printf 'https://%s/v3.24/main\nhttps://%s/v3.24/community\n' \
        "$APK_REPOSITORY_BASE" "$APK_REPOSITORY_BASE" > /etc/apk/repositories \
    && apk add --no-cache git bash docker-cli

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=5 \
    PYTHONPATH=/workspace/apps/api:/workspace

WORKDIR /workspace

COPY requirements.lock requirements-dev.txt /workspace/
# PIP_INDEX_URL 为空走默认 pypi.org（CI 不变）；U-Dao 构建注入腾讯云 PyPI 镜像
RUN if [ -n "$PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir -i "$PIP_INDEX_URL" -r /workspace/requirements-dev.txt; \
    else \
        pip install --no-cache-dir -r /workspace/requirements-dev.txt; \
    fi

COPY apps/api /workspace/apps/api
COPY services /workspace/services
COPY packages /workspace/packages
COPY scripts /workspace/scripts

WORKDIR /workspace
