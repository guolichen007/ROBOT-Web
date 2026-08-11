FROM mcr.microsoft.com/playwright:v1.62.1-noble

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg=7:6.1.1-3ubuntu5 \
    && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libx264 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /source
COPY services/media-test-source/main.mjs ./main.mjs

USER pwuser
CMD ["node", "/source/main.mjs"]
