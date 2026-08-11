FROM python:3.12.13-alpine3.24

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=5 \
    PYTHONPATH=/workspace/apps/api:/workspace

WORKDIR /workspace

COPY requirements.lock requirements-dev.txt /workspace/
RUN pip install --no-cache-dir -r /workspace/requirements-dev.txt

COPY apps/api /workspace/apps/api
COPY services /workspace/services
COPY packages /workspace/packages
COPY scripts /workspace/scripts

WORKDIR /workspace
