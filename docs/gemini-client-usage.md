# Gemini client — read-only development profile

An optional [confirmed update profile](gemini-confirmed-writes.md) adds native writes with separate grants and browser approval. It is not automatically enabled on an existing read connection.


[한국어 가이드](gemini-client-usage.ko.md)

Tracked by [issue #3](https://github.com/johneybi/InvestKitchen/issues/3).
This profile adds an OAuth-protected Streamable HTTP resource server and a
portable skill. It reuses the same InvestKitchen Gateway and authoritative state
as the existing clients. The ChatGPT tunnel and stdio adapter retain their own
deployment lifecycle.

Status: implemented and tested with synthetic data through self-hosted Keycloak,
local TLS and a temporary public HTTPS tunnel. On 2026-10-14, Gemini completed
OAuth, discovered six read tools and read synthetic Portfolio and Knowledge in
the English UI with Argon. Server tests refuse writes and out-of-scope reads.
On 2026-10-15, a dedicated Tailscale node's HTTPS 443 passed public DNS/TLS checks
and Gemini OAuth registration. The English UI with Flash returned
`get_capabilities`, `list_portfolios`, `get_portfolio_state` and
`get_current_knowledge` against approved authoritative state. Authenticated HTTP
success and `refresh_token` exchange were also verified. Intermittent 401s
succeeded on retry; long-term reliability remains unverified. PR merge and
deployment of product changes remain separate from these operational checks.
See the [domain-free trial runbook](../deploy/gemini/README.md).

## Transition to authoritative NAS data

The synthetic trial establishes authenticated Gemini tool use. Actual use needs
a separately managed read-only HTTP service on the NAS, connected to the existing
authoritative stores, stable HTTPS and an approved real-owner grant. Do not copy
production data into a trial directory or describe synthetic results as production.

On 2026-10-14, an internal NAS preflight read real checkpoint-authoritative
Portfolio and native Knowledge through an identified product commit. Owner-only
scope, denial of another portfolio, absent write tools and unchanged source
records passed. This establishes data compatibility, not transmission to Google
or an operational Gemini connection. After switching to a dedicated node's
HTTPS 443 and verifying public DNS, Google account consent, NAS OAuth
login/consent and app saving completed on 2026-10-15. Two approved real portfolios
and native Knowledge were read through Gemini with authenticated HTTP success.
The identity store recorded authorization-code exchange followed by
`refresh_token` exchange beyond the access-token lifetime. No credentials or
actual user responses are included here.

Earlier session-expiry, URL-validation and public-DNS failures preceded the
successful registration. This does not establish a restriction on the earlier
port as the cause. Some read requests returned 401 and succeeded on retry. If
this repeats, compare request and refresh timing without logging secrets or
weakening authorization or token validation.

OAuth discovery must preserve the issuer identifier exactly, including whether
the final `/` is present, and accurately advertise the configured read scope.
The HTTP transport preserves the configured issuer instead of serializing a
normalized URL object. This change is locally tested; the activated runtime uses
an operator-generated ingress metadata correction. Check the identity provider's
public metadata independently: configured custom scopes may be absent from its
advertised `scopes_supported`. Verify contract correctness, public DNS/TLS,
app saving, actual reads and refresh independently.

An existing Tailscale NAS can consider [Funnel](https://tailscale.com/docs/features/tailscale-funnel)
for stable `*.ts.net` HTTPS on a separate allowed port, preserving existing private
Serve routes. Public exposure requires operator authorization and the MCP still
requires OAuth. Measure capacity before adding an identity server to a small NAS.
[Authelia](https://www.authelia.com/configuration/identity-providers/openid-connect/clients/)
is a lightweight candidate with SQLite, RS256 JWT access tokens, exact resource
audience and PKCE S256. Login, token exchange/refresh and actual Gemini compatibility
are separate checks; the Keycloak trial does not prove another provider's Gemini use.

Approve the real portfolio scope and instance-wide Knowledge access before
activation. Prepare service supervision, backups and rollback, then verify
actual Gemini tool calls and refresh before recording operational completion.

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
subsequently succeeded in that account's English UI with Argon, and real NAS
reads succeeded with Flash. A Pro attempt
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

## Start a chat with the registered app

Open a [new English-UI chat](https://gemini.google.com/app?hl=en); the prompt itself
can be Korean. Type `@` directly in the composer and click the actual app under
**Connectors**, such as `InvestKitchen NAS`. The similarly named entry under
**Skills** supplies instructions, so select the connector separately. Pasting a
complete `@App name` string may not open or select a mention.

Once the selected-app marker appears, ask an ordinary question such as "show my
accounts". Verify account-level holdings and cash for each requested portfolio;
IDs alone do not establish a detailed read. Continue with ordinary follow-ups
such as "show all of them"; users do not need API names. Flash is the verified
mode. If
Gemini says no MCP tools are available, check the connector selection for that
chat first. Registration or an enabled skill alone does not establish tool use
for a response. If the app is absent, check the English UI and connection
settings. An authentication error from an actual call is a separate connection
failure; do not infer private state or report success.

The page language may differ between PCs signed into the same account. In the
observed account, the same conversation and Flash mode offered only the skill
in the Korean mention menu; appending `?hl=en` to that conversation URL exposed
the NAS connector. An existing conversation can use the English UI while prompts
remain Korean. A model response saying no MCP tools are available does not by
itself establish a NAS outage or PC-bound access. Distinguish absent tools from
an authentication error returned by an actual call.

On 2026-10-16, a fresh English-UI general chat reproduced the missing-tools
response to an ordinary Korean account question while showing that the skill
had been applied. Authenticated discovery in the same runtime configuration
included the detail tool. After explicitly selecting the connected app in a
fresh chat, the original ordinary question retrieved accounts, holdings and
cash for both authorized portfolios. Instructions now distinguish app discovery,
IDs and detailed reads, handle requests for all authorized portfolios, and forbid
unsupported server-outage diagnoses. **An unselected fresh chat still failed to
load app tools after the instruction update.** Neither the English UI nor a
skill edit establishes that automatic app selection is repaired. A separate-PC
retest subsequently reported another failure: a broker-named account question
received generic financial-security advice instead of a saved-state read. A
successful broad account question does not establish natural account selection
or cross-PC reliability. Automatic app selection remains unresolved.

Questions such as "내 토스 계좌를 조회해 줘" normally ask for observations
already saved in InvestKitchen. The broker name filters accounts; it does not
request a new brokerage login. The skill now directs clients to match names,
identifiers, providers and account types from actual authorized portfolio reads.
Return matches separately by portfolio, without inventing aliases, inferring
ownership or merging balances across owners. Distinguish no matching account
and unloaded tools from financial authentication restrictions. An explicit
request for a live brokerage refresh is a separate capability; claim it only
when an available authorized tool supports it, and always label saved-state
source dates.

After updating the account skill's description and instructions on the same
day, a fresh Flash chat with the actual app selected returned the matching
accounts, holdings, cash and source dates for both authorized portfolios in
response to "다시 내 토스 계좌를 조회해 줘", without API names. An unselected
control chat still reported unloaded app tools. A follow-up, "그럼 내 ISA 계좌는?",
also returned the matching account details without app reselection; its server
call was not independently traced. This verifies those questions in the
selected-app setup, not automatic cross-PC connection or every natural-language
expression.

Skill activation and actual app use are separate signals. Automatic app use in
an unselected fresh chat is not guaranteed. The registration and skill belong to
the Google account; this is not a per-PC local MCP installation.

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

For providers such as Authelia that put scopes in a JWT `scp` array, explicitly
set `"scope_claim": "scp"`. Every array member must be a nonempty scope string
without whitespace. The default remains the string `scope` claim; the verifier
does not combine claims or use token-supplied permissions as a data grant.

`required_scopes` defaults to `["investkitchen.read"]`. For a provider requiring
OIDC scopes to issue refresh tokens, set
`"required_scopes": ["investkitchen.read", "openid", "offline_access"]` in the
operator configuration. Resource metadata advertises these scopes and HTTP
requests must contain all of them. The read scope cannot be removed and write
scopes cannot be added. Additional OIDC scopes do not widen portfolio or Knowledge
access; the server-owned data grant remains authoritative.

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
