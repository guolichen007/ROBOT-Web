FROM node:24.19.0-alpine3.24 AS build
WORKDIR /app
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY apps/web/ ./
RUN npm run build

# WEB-R1-MIN-FINAL P0-01：nginx 1.30.4-r1 存在 5 个 CRITICAL（CVE-2026-113425/121810/123007/
# 123633/97205，修复于 nginx 1.30.7-r0）。升级到 1.30.7-alpine3.24 并固定 index digest；
# Trivy 0.59.1 本地对最终镜像 rootfs 复核 CRITICAL=0（含 unfixed）。
FROM nginx:1.30.7-alpine3.24@sha256:caf4e1e032a20f9c3c5e28fc7f25ffd768ad8677b4975c54193075997b3a97e3
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}
# G1-R0 A4：最小修复最终运行镜像 brotli-libs CVE-2026-111497（→ >=1.2.1-r0）。
# 保持 Trivy CRITICAL 门禁，不增加忽略项、不降低安全等级。
RUN apk add --no-cache --upgrade "brotli-libs>=1.2.1-r0"
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
