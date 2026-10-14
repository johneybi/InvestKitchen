# Gemini client — read-only development profile

[한국어 가이드](gemini-client-usage.ko.md)

Tracked by [issue #3](https://github.com/johneybi/InvestKitchen/issues/3).
This profile adds an OAuth-protected Streamable HTTP resource server and a
portable skill. It reuses the same InvestKitchen Gateway and authoritative state
as the existing clients. The ChatGPT tunnel and stdio adapter retain their own
deployment lifecycle.

Status: implemented and tested with synthetic data through self-hosted Keycloak,
local TLS and a temporary public HTTPS tunnel. On 2026-10-14, the actual Gemini
account completed OAuth, discovered six read tools, and called `list_portfolios`,
`get_portfolio_state` and `get_current_knowledge` in the English UI with Argon.
The empty `portfolio-alpha` and synthetic Knowledge results were checked along
with authenticated HTTP success. Server tests also refuse writes and out-of-scope
portfolios. Production ingress, NAS deployment and personal-portfolio Gemini use
remain unverified. A merged PR alone does not change those statuses. See the
[domain-free trial runbook](../deploy/gemini/README.md).

## Surface and boundaries

The explicit surface contains nine tools: `get_capabilities`, `list_portfolios`,
`get_portfolio_state`, `get_current_knowledge`, `search_knowledge`,
`resolve_krx_securities`, `get_market_quote`, `get_market_ohlcv`, and
`build_decision_context`. Runtime-unavailable or unauthorized tools are filtered
from discovery and refused on invocation. Security-name resolution is exposed
only if the product's tool catalog includes `resolve_krx_securities`; older
catalogs can serve the other reads without that optional tool. All preview/apply actions are refused,
even if the underlying runtime supports writes.

The remote context composer permits only Portfolio, Knowledge and Market reads.
Every requested capability (including those introduced by `capability_inputs`
and `required_capabilities`) must independently be usable under the grant.
Portfolio scope is checked both on direct reads and on every composed input.
`list_portfolios` returns only the grant's portfolios. Knowledge authorization is
instance-wide; this profile does not claim per-portfolio Knowledge isolation.

The official Python MCP SDK 2.3.0 handles both the `2026-07-28` discovery protocol
and the `2025-11-25` initialize/session protocol on `/mcp`. Legacy sessions are
bounded and tied to their authenticated principal. Responses use `no-store`;
tool discovery uses private caching with zero TTL. Requests are capped at 64 KiB.

## Gemini availability and app connection

Google's [custom app documentation](https://support.google.com/gemini/answer/17209137)
currently specifies US availability, English, a personal Google account,
age 18+ and Keep Activity enabled. Confirm that the actual account offers Custom
apps before provisioning ingress. These restrictions are separate from whether
the InvestKitchen implementation works. An account's custom-app registration UI
and redirect-URI copy control were observed; authenticated synthetic reads
subsequently succeeded in that account's English UI with Argon. A Pro attempt
returned a generic error, so the trial does not claim Pro compatibility.

In the observed account, registration and authenticated tool discovery succeeded
in Korean, but the custom connector was absent from the Korean chat mention
menu. The English page (`https://gemini.google.com/app?hl=en`) exposed
**Connectors → InvestKitchen** when typing `@InvestKitchen`. Select the connector
entry, not the similarly named skill. Registration alone is not a chat tool-use
check. This page-local language selection does not change the Google account
language or establish regional eligibility.

In Gemini web, open Connected Apps, add the public MCP URL, and complete the OAuth
flow. If Dynamic Client Registration is unavailable, Google's UI permits entering
operator-provisioned OAuth client credentials under Advanced features. Record
the actual authenticated client ID in the server-side grant; do not assume a
display name such as "Gemini" is authenticated identity.

## Register the skill; ZIP is optional

The connected app supplies authenticated tools; the skill supplies instructions
for using them. Registering a skill does not connect the server, log in or grant
portfolio access. Complete the app connection separately.

For manual registration in Gemini web, use **Customize → Skills → Create → Create
manually** and fill in the following fields:

| Field | Value from [`SKILL.md`](../skills/investkitchen-read/SKILL.md) |
| --- | --- |
| Name | `investkitchen-read` |
| Description | The YAML `description` value |
| Instructions | The Markdown body below the closing YAML `---`; omit the YAML block |

The [Korean guide](gemini-client-usage.ko.md#스킬-수동-등록-권장) also includes
Korean text ready to paste into those fields.

Google's [skills guide](https://support.google.com/gemini/answer/17094296?co=GENIE.Platform%3DDesktop)
also supports uploading a `SKILL.md`, its folder or a ZIP. Review the imported
fields before creating the skill. ZIP is an optional distribution container;
the source is the text file above. The previously provided archive contains only
`investkitchen-read/SKILL.md`, so uploading that file directly has the same content.
This skill has no scripts, credentials or private data. Skills are rolling out
separately from custom apps; check the actual account's UI. This guide targets
Gemini Apps, and does not install or configure Gemini CLI.

## OAuth configuration

The MCP process is a resource server, not an authorization server. An external
OAuth provider must supply its discovery metadata, login/consent, authorization
code flow with PKCE, client registration or a static client, and access tokens.
Configure that provider for:

- RS256 access-token signatures and a public RSA JWKS with unique `kid` values;
- an exact issuer matching `issuer_url`;
- an audience matching the **exact public HTTPS MCP URL**, including `/mcp`;
- integer `iat`/`exp`, a `sub`, a client identity claim (default `client_id`; set
  `client_id_claim` to the provider's documented alternative such as `azp`), and
  a space-separated `scope` containing `investkitchen.read`.

This profile does not accept generic Google sign-in ID tokens, opaque tokens,
shared-secret JWTs, arbitrary issuer/audience values or caller-supplied grants.
Use a provider that can issue the stated access-token contract. A provider that
does not support this contract needs a separately reviewed verifier adapter.

Copy [`oauth-resource.example.json`](../deploy/gemini/oauth-resource.example.json)
to an operator-controlled configuration directory outside Git. Replace only the
synthetic values with the actual issuer, public resource, authenticated
subject/client pair and server-known portfolio scope. Keep the public verification
JWKS in that external directory as well; the process rejects private key material.
JWT scope does not add permissions: permissions and portfolio scope come from
the operator-owned grant, and write permissions are invalid configuration.

The RFC 9728 protected-resource metadata at
`/.well-known/oauth-protected-resource/mcp` and the unauthenticated 401
`WWW-Authenticate` response point the client to the external authorization server.
The OAuth client secret, login page and token endpoints belong to that provider.
Never put a token or credential in an MCP argument or URL.

Verification uses a local public JWKS snapshot and performs no network fetch.
Use short-lived access tokens. To rotate keys, update the snapshot and restart
the separate HTTP process. To remove access, remove the grant and restart; tokens
are checked on every HTTP request. Provider-side revocation alone is not an
immediate revocation mechanism for this offline verifier.

## Run the separate process

Use Python 3.11+ with the optional remote dependencies installed. The existing
host Python or tunnel image is not automatically upgraded by this profile.
From the verified product checkout:

```sh
python3 -m venv /srv/investkitchen/remote-venv
/srv/investkitchen/remote-venv/bin/python -m pip install '.[remote]'

/srv/investkitchen/remote-venv/bin/python -m protocol.v1.transport.mcp_http \
  --oauth-config /srv/investkitchen/operator/oauth-resource.json \
  --public-jwks /srv/investkitchen/operator/public-jwks.json \
  --manifest /srv/investkitchen/operator/instance.json \
  --personal-data-root /srv/investkitchen/data/personal \
  --native-store-root /srv/investkitchen/state/native-write \
  --advisory-state-root /srv/investkitchen/state/advisory \
  --portfolio-checkpoint-root /srv/investkitchen/state/portfolio-checkpoints
```

All paths above are synthetic deployment examples. Use the existing authoritative
state and Portfolio authority configuration; do not create a second data store.
For optional market reads, pass `--market-provider toss-native-subprocess` and
`--market-secret-file` pointing to the existing external provider credential.
No legacy workspace, account-sync credentials or write principal/grant is accepted
by this launcher. No write authority is injected into the Gateway.

The listener is loopback-only (`127.0.0.1:8090` by default). A separately managed
HTTPS ingress must forward `/mcp` and protected-resource metadata, preserve the
public Host and MCP headers, pass Authorization, avoid caching bodies, cap/rate
limit requests and preserve legacy-session routing to the same process. Origin
checks permit the configured public origin. Do not enable wildcard CORS or bypass
token verification to solve connectivity problems. Access-body/token logging must
stay disabled. Uvicorn access logging is disabled in this launcher.

## Preflight, rollout and rollback

1. Review the exact product commit and changed files; install pinned dependencies
   in the separate environment. Keep source, personal data, state, operator
   configuration, provider secrets and backups separate.
2. Confirm the account's Custom apps availability and the external OAuth provider
   contract. Check grants against `list_portfolios`; startup rejects unknown IDs.
3. Validate metadata discovery, 401/403 behavior, allowed reads and refused writes
   with synthetic data before connecting production state. Run the protocol tests.
4. Provision a supervised HTTPS ingress and the separate process for the identified
   commit. Verify an allowed read and a denied out-of-scope read before Gemini use.
5. Connect Gemini, discover tools, read Portfolio/Knowledge/Market where usable,
   and verify that no write/preview action appears. Record this separately as
   deployed and operationally verified only when it actually succeeds.

Rollback: remove the Gemini app connection, stop the separate HTTP process and
remove its ingress route or restore its prior identified commit/configuration.
Do not roll back the shared Portfolio/Knowledge state or restart the existing
ChatGPT tunnel as part of this client rollback. Revoke the OAuth grant at the
provider as well when retiring the connection.

## Local verification

```sh
python3 -m pip install '.[dev]'
python3 -m pytest protocol/v1/tests/test_mcp_http.py -q
python3 -m pytest protocol/v1/tests -q
```

Tests generate synthetic signing keys and tokens in memory. They exercise actual
ASGI HTTP routing for both protocol eras, authentication discovery, issuer,
audience, signature and time validation, missing scopes, legacy-session identity
isolation, discovery filtering, composed authorization, bounds, write refusal,
Host/Origin rejection, request size and redacted failures. They do not establish
Gemini UI availability or a successful production OAuth redirect.

Implementation references: [SDK resource-server authorization](https://py.sdk.modelcontextprotocol.io/run/authorization/)
and [legacy-client support](https://py.sdk.modelcontextprotocol.io/run/legacy-clients/).
