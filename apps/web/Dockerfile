FROM node:24.19.0-alpine3.24 AS build
WORKDIR /app
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY apps/web/ ./
RUN npm run build

FROM nginx:1.30.4-alpine
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}
# G1-R0 A4：最小修复最终运行镜像 brotli-libs CVE-2026-111497（→ >=1.2.1-r0）。
# 保持 Trivy CRITICAL 门禁，不增加忽略项、不降低安全等级。
RUN apk add --no-cache --upgrade "brotli-libs>=1.2.1-r0"
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
