FROM caddy:2.11.7@sha256:f2a1290d0463aad60660d4ec134943f183ee2a5f6c3eb7bf32dd984f2f020772
ARG INVESTKITCHEN_SOURCE_COMMIT
LABEL org.opencontainers.image.title="InvestKitchen synthetic trial edge" \
      org.opencontainers.image.revision="$INVESTKITCHEN_SOURCE_COMMIT"
# The trial uses unprivileged ports. Remove the upstream binary's privileged
# port capability so it can execute with ALL capabilities dropped.
RUN setcap -r /usr/bin/caddy
USER 1000:1000
