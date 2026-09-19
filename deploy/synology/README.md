# InvestKitchen Synology reference deployment

This profile runs the InvestKitchen compatibility runtime continuously on a Synology NAS so
ChatGPT access does not depend on the development Mac being online.

The existing `/volume1/docker/trademind/...`, `TRADEMIND_*`, and
`trademind-runtime:*` names are retained as rollback-safe operational
compatibility identifiers. They are not the current product name.

## Runtime layout

```text
/volume1/docker/trademind/
  runtime-data/
    personal/       # migrated Portfolio + Knowledge bundle, read-only to MCP
    state/          # native write + approval + Portfolio checkpoint state
    backups/
  secrets/
    trademind-runtime-api-key
    toss-market-readonly.json
```

The InvestKitchen source repository contains no real personal data or secret values.

Portfolio authority remains an explicit deployment flag. The currently verified
production profile uses checkpoint authority; `personal-data` remains the rollback
mode:

```text
INVESTKITCHEN_PORTFOLIO_AUTHORITY=personal-data  # rollback
INVESTKITCHEN_PORTFOLIO_AUTHORITY=checkpoint     # current production authority
INVESTKITCHEN_CONNECTOR_WRITES=disabled          # MCP read-only surface
INVESTKITCHEN_CONNECTOR_WRITES=enabled           # bounded Knowledge/Portfolio write actions
INVESTKITCHEN_MARKET_PROVIDER=none               # market handler disabled
INVESTKITCHEN_MARKET_PROVIDER=toss-native-subprocess  # read-only quote/OHLCV
```

## First deployment

1. Copy this repository to the NAS or clone the private deployment repository.
2. Run `deploy/synology/bootstrap.sh` as the NAS deployment account. The script
   uses that account's numeric UID/GID by default and does not require `chown`
   unless it is executed as root.
3. Copy the verified personal-data bundle into
   `/volume1/docker/trademind/runtime-data/personal`.
4. Put the Tunnel runtime API key in
   `/volume1/docker/trademind/secrets/trademind-runtime-api-key` with mode `0600`.
5. For market reads, put the Toss read-only credential JSON at
   `/volume1/docker/trademind/secrets/toss-market-readonly.json` with only
   `client_id` and `client_secret`, mode `0600`, and set
   `INVESTKITCHEN_MARKET_PROVIDER=toss-native-subprocess`. The secret value is
   never placed in MCP arguments or environment variables.
6. Set `TRADEMIND_RUNTIME_UID` and `TRADEMIND_RUNTIME_GID` in the external deploy
   env to the NAS account that owns the runtime files. On the currently verified
   NAS account this is `1026:100`; do not assume `1000:1000` on every Synology.
7. Copy `deploy.env.example` outside Git and replace the Tunnel ID. Do not
   `source` the file before `sudo`; Synology sudo may drop exported variables.
   Use the wrapper, which passes the env file directly to Compose:

Production NAS deployment does not build images. Build and verify the exact
`linux/amd64` image on a trusted builder first:

```sh
./deploy/synology/build-image.sh
```

The script pins the source commit into the image label, verifies the image
platform and Tunnel runtime version, and writes an archive plus SHA-256 sidecar
under `dist/`. Transfer that exact archive to
`/volume1/docker/trademind/runtime-images/`, then install it:

```sh
sudo ./deploy/synology/install-image.sh /volume1/docker/trademind/runtime-images/trademind-runtime.tar
```

Only after the Mac Tunnel has been stopped, perform the actual cutover:

```sh
sudo ./deploy/synology/cutover.sh
```

For the one-time Portfolio authority migration, first update
`TRADEMIND_RUNTIME_IMAGE_REF` to the newly built exact image while leaving
`INVESTKITCHEN_PORTFOLIO_AUTHORITY=personal-data`. Then one root command performs
image installation, idempotent checkpoint seed/verification, a pre-cutover v2
backup, and the authority restart:

```sh
sudo ./deploy/synology/deploy-portfolio-authority.sh \
  /volume1/docker/trademind/runtime-images/trademind-runtime-<commit>.tar
```

The seed refuses divergent production shadow state, a changed personal snapshot,
or a native Transaction delta that appeared during cutover preparation. On a
runtime health failure the cutover restores the previous deploy env. After a
healthy cutover, this explicit command returns Portfolio reads to the unchanged
personal-data path without deleting the checkpoint journal:

```sh
sudo ./deploy/synology/portfolio-authority-rollback.sh
```

The Compose profile publishes no host port. The Tunnel client makes the outbound
connection to OpenAI and its health endpoint is used only inside the container.
The service uses `restart: unless-stopped`, so after a successful Container
Manager deployment it is intended to return automatically after a NAS reboot.

## Read-only market provider

The production market path is deliberately separate from the Portfolio and
Knowledge authority paths:

```text
ChatGPT / Codex
  → market.quote / market.ohlcv
  → scrubbed provider subprocess
  → Toss Securities Open API
```

The provider supports bounded current quotes plus OHLCV through Toss's unified
security-symbol namespace: Korean numeric symbols, Korean alphanumeric ETF
identifiers, U.S. tickers, and the supported Korean index symbols. Quote results
that include USD instruments also carry a synchronized USD/KRW exchange-rate
observation when available. It has no account, order, execution, or mutation
method. Provider or FX failures are returned as partial/unavailable capability
results rather than fabricated prices or silently mixed fallback data.

The Toss credential is mounted as a Compose secret for the ChatGPT Tunnel
runtime. The Codex host launcher reads the same private host secret file and only
passes its path to the provider subprocess. This is still a personal single-host
trust boundary, not OS-level secret isolation; the current container/host-user
boundaries must not be described as stronger than they are.

When `INVESTKITCHEN_CONNECTOR_WRITES=enabled`, the entrypoint derives the allowed
Portfolio scope from the private personal-data manifest, creates a private
server-side write principal/grant on tmpfs, and enables only the native Knowledge
and Portfolio observation preview/apply actions. It does not expose Decision,
Transaction, order, execution, or market-data writes. The control-plane key is
still never forwarded to the MCP child.

For ChatGPT plans that do not expose custom MCP write actions, use the host
fallback without Docker/root access:

```sh
/volume1/docker/trademind/advisory-write.sh knowledge-preview --input -
/volume1/docker/trademind/advisory-write.sh knowledge-apply --pending-id <id> --confirmation '<phrase>'
/volume1/docker/trademind/advisory-write.sh portfolio-preview --input -
/volume1/docker/trademind/advisory-write.sh portfolio-apply --pending-id <id> --confirmation '<phrase>'
```

The wrapper runs Synology Python 3.10 against a pinned InvestKitchen source tree
and the live state root. Pending previews are private, one-shot, and stale-checked.

## Codex MCP client

Codex can use the same authoritative state without enabling writes in the
ChatGPT Tunnel container. `codex-mcp.sh` is a separate host-side stdio launcher
intended to be invoked over private SSH:

```sh
ssh -T -o BatchMode=yes -o ClearAllForwardings=yes \
  user@nas /volume1/docker/trademind/codex-mcp.sh
```

The launcher creates a short-lived server-owned Codex Principal/Grant with only
`knowledge.commit`, `portfolio.update`, and `operation.approve`, derives Portfolio
scope from the private manifest, requires the checkpoint journal, and reuses the
native Knowledge/write/checkpoint/approval stores. It does not need Docker or
sudo and does not change the running ChatGPT container.

InvestKitchen speaks modern MCP `2026-07-28`. Current Codex requires explicit
opt-in for modern stdio MCP:

```toml
[features]
mcp_2026_07_28 = true

[mcp_servers.investkitchen]
command = "ssh"
args = ["-T", "-o", "BatchMode=yes", "-o", "ClearAllForwardings=yes", "user@nas", "/volume1/docker/trademind/codex-mcp.sh"]
env = { CODEX_MCP_PROTOCOL_VERSION = "2026-07-28" }
default_tools_approval_mode = "writes"
```

Keep Codex write-tool approval enabled. The server confirmation phrase binds an
apply call to an exact preview but is not a substitute for the client's human
approval UI. The UI gate is not a server-verifiable human-attestation; the server
independently enforces preview binding, scope, freshness, expiry, and replay
checks. The current private SSH account is a trusted personal operator boundary;
use a dedicated forced-command SSH key for a hardened Codex-specific boundary.
See `docs/codex-client-usage.md` for the full operating contract.

DSM/Container Manager may bind-mount Compose secrets without applying requested
`uid`/`gid` metadata. Keep the host secret owned by the same numeric UID/GID used
for the runtime container and mode `0600`; the runtime user is configurable for
that reason.

## Important current security boundary

The current MCP transport is stdio, so the OpenAI Tunnel runtime and the
InvestKitchen Python MCP child share one container. The control-plane key is passed
to tunnel-client only as a secret-file reference and is not placed in the MCP
command or environment. However, this is not yet an OS-level secret isolation
boundary because both processes share the same container filesystem. Before
third-party Extensions are allowed, move Tunnel and MCP into separate containers
using an authenticated HTTP/Unix-socket MCP transport or another enforceable
sandbox boundary.

## Cutover rule

Only one active `tunnel-client` instance may use a Tunnel ID with a stdio MCP
binding. Stop the Mac runtime before starting the Synology runtime; do not use a
rolling overlap. After Synology is ready, verify a ChatGPT Tool call before
considering the Mac runtime retired.
