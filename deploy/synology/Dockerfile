# syntax=docker/dockerfile:1.7

# Synology reference image: use a single linux/amd64 Python base manifest so
# DSM/Container Manager does not have to traverse modern multi-platform OCI
# indexes or provenance attestations during the build.
ARG PYTHON_IMAGE=python:3.14-alpine3.22@sha256:1a5f0303fd8941565b5bbda3fac2345148a9fd8245d575c2971ccefcd77c78f6
FROM ${PYTHON_IMAGE}

ARG TRADEMIND_SOURCE_COMMIT=unknown
LABEL org.opencontainers.image.title="TradeMind Runtime" \
      org.opencontainers.image.revision="$TRADEMIND_SOURCE_COMMIT"

# OpenAI publishes a narrow run-only tunnel-client-runtime archive for Linux
# amd64. Pin and verify the official release artifact before installing it.
ARG TUNNEL_RUNTIME_URL=https://github.com/openai/tunnel-client/releases/download/v0.0.14/tunnel-client-runtime-v0.0.14-linux-amd64.zip
ARG TUNNEL_RUNTIME_SHA256=29d29cf860ada54e4d3c82c715f4fbfcff2abcdc2584c0fc26431308dfa2505b

RUN apk add --no-cache ca-certificates \
    && apk add --no-cache --virtual .fetch-deps curl unzip \
    && curl -fL --retry 3 --retry-delay 2 -o /tmp/tunnel-runtime.zip "$TUNNEL_RUNTIME_URL" \
    && printf '%s  %s\n' "$TUNNEL_RUNTIME_SHA256" /tmp/tunnel-runtime.zip | sha256sum -c - \
    && unzip -p /tmp/tunnel-runtime.zip tunnel-client-runtime > /usr/bin/tunnel-client-runtime \
    && chmod 0555 /usr/bin/tunnel-client-runtime \
    && rm -f /tmp/tunnel-runtime.zip \
    && apk del .fetch-deps

RUN addgroup -g 1000 -S trademind \
    && adduser -u 1000 -S -D -G trademind trademind

WORKDIR /app
COPY protocol /app/protocol
COPY deploy/synology/container-entrypoint.sh /usr/local/bin/trademind-runtime-entrypoint

RUN chmod 0555 /usr/local/bin/trademind-runtime-entrypoint \
    && find /app -type d -exec chmod 0555 {} \; \
    && find /app -type f -exec chmod 0444 {} \;

ENV HOME=/tmp
USER 1000:1000

EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/trademind-runtime-entrypoint"]
