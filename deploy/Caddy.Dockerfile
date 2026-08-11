# syntax=docker/dockerfile:1.7

FROM caddy:2.10.2-alpine@sha256:d8c17a862962def15cde69863a3a463f25a2664942eafd7bdbf050e9c3116b83

RUN addgroup -S -g 10002 caddy \
    && adduser -S -D -H -u 10002 -G caddy caddy \
    && setcap -r /usr/bin/caddy \
    && chown -R 10002:10002 /data /config

COPY --chown=10002:10002 deploy/Caddyfile /etc/caddy/Caddyfile
COPY --chown=10002:10002 web/dist /srv/aemeath-web

USER 10002:10002
