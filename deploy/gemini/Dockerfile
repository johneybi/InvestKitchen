# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.14-alpine3.22@sha256:1a5f0303fd8941565b5bbda3fac2345148a9fd8245d575c2971ccefcd77c78f6
FROM ${PYTHON_IMAGE}
ARG INVESTKITCHEN_SOURCE_COMMIT
LABEL org.opencontainers.image.title="InvestKitchen read-only HTTP MCP" \
      org.opencontainers.image.revision="$INVESTKITCHEN_SOURCE_COMMIT"
WORKDIR /app
COPY pyproject.toml /app/
COPY protocol /app/protocol
RUN python -m pip install --no-cache-dir '.[remote]' \
    && find /app -type d -exec chmod 0555 {} \; \
    && find /app -type f -exec chmod 0444 {} \;
COPY deploy/gemini/runtime-entrypoint.sh /usr/local/bin/investkitchen-http
RUN chmod 0555 /usr/local/bin/investkitchen-http
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
USER 1000:1000
ENTRYPOINT ["/usr/local/bin/investkitchen-http"]
