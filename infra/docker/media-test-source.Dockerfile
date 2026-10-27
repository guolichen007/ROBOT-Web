# 合成视频测试源（仅验收用，测完即停）。
# 原 mcr.microsoft.com/playwright 基座在中国大陆网络持续断连（实测 connection reset），
# 改为 node:alpine + apk ffmpeg（Alpine 镜像可经 APK_REPOSITORY_BASE 指向国内源）。
ARG NODE_IMAGE=node:24.19.0-alpine3.24

FROM ${NODE_IMAGE}

# FROM 之前的 ARG 在 RUN 中不可见，必须在此重新声明（Dockerfile 语义）
ARG APK_REPOSITORY_BASE=dl-cdn.alpinelinux.org/alpine

USER root
# ffmpeg 在 community 仓库，其依赖（xcb 等）在 main 仓库——必须同时替换两个仓库源
# （dl-cdn 在中国大陆网络 TLS 被断；U-Dao 构建经 compose args 指向腾讯 Alpine 镜像）。
# libx264 校验保证 codec 合同（H.264+yuv420p）可产出。
RUN printf 'https://%s/v3.24/main\nhttps://%s/v3.24/community\n' \
        "$APK_REPOSITORY_BASE" "$APK_REPOSITORY_BASE" > /etc/apk/repositories \
    && apk add --no-cache ffmpeg \
    && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libx264

WORKDIR /source
# lib/redact.mjs 为 main.mjs 的脱敏依赖（P0-B），必须一并拷贝
COPY services/media-test-source/ ./

USER node
CMD ["node", "/source/main.mjs"]
