FROM python:3.14-alpine3.22@sha256:1a5f0303fd8941565b5bbda3fac2345148a9fd8245d575c2971ccefcd77c78f6
ARG INVESTKITCHEN_SOURCE_COMMIT
LABEL org.opencontainers.image.title="InvestKitchen Market Edition Intake" \
      org.opencontainers.image.revision="$INVESTKITCHEN_SOURCE_COMMIT"
WORKDIR /app
RUN pip install --no-cache-dir \
      'jsonschema==4.26.0' 'rfc3339-validator==0.1.4' 'rfc3986-validator==0.1.1' \
      'cryptography==50.0.2'
COPY protocol/v1/runtime/market_edition_store.py /app/protocol/v1/runtime/
COPY protocol/v1/schemas/market-edition-ingest.schema.json /app/protocol/v1/schemas/
COPY protocol/v1/transport/market_edition_https.py /app/protocol/v1/transport/
ENV PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
ENTRYPOINT ["python", "-m", "protocol.v1.transport.market_edition_https"]
