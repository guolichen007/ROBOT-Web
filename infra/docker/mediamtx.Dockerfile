FROM busybox:1.37.0-musl AS health-tool

FROM bluenviron/mediamtx:1.18.2
COPY --from=health-tool /bin/busybox /busybox
