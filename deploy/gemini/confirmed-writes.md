# Optional confirmed HTTP writes

This profile adds native Knowledge and Portfolio observation updates. The default
HTTP server remains read-only. Implementation and synthetic tests do not establish
live NAS deployment or Gemini write compatibility. See the
[Korean usage guide](../../docs/gemini-confirmed-writes.ko.md).

## Authorization and review

A write requires all of the following:

- A verified OAuth token for the exact existing resource, subject and client,
  including `investkitchen.read` and **`investkitchen.write`**. Existing required
  read/OIDC scopes remain required; write scope is checked per write operation.
- A private, expiring server grant whose permissions and portfolio IDs are inside
  the corresponding read grant. Do not copy the example expiry as a live policy.
- A fresh native preview and a separate authenticated browser approval of its
  exact canonical content. The browser username must match the configured grant.
- The matching single-use apply call and successful native authorization,
  freshness checks, persistence and read-back.

The adapter accepts only the configured browser session cookie, sending it to a
fixed loopback Authelia ForwardAuth endpoint. It never forwards caller bearer
credentials, identity headers or proxy metadata to that check. Authelia must
return the authenticated `Remote-User`. See its official
[proxy authorization guide](https://www.authelia.com/reference/guides/proxy-authorization/).
MCP annotations and Gemini UI confirmations are not server-verifiable receipts.

Review HTML must use `Referrer-Policy: same-origin`. The Fetch standard makes a
native form POST with `no-referrer` send `Origin: null`, so the strict Origin
check rejects even the user's valid click. `same-origin` preserves the expected
Origin for this form and withholds the review URL from other origins. Keep the
exact Origin, browser identity, CSRF token and content-type checks; never accept
`null` or missing Origin as a workaround. Verify a native browser form submission
as well as the HTTP tests, which provide Origin explicitly. See the
[Fetch Origin algorithm](https://fetch.spec.whatwg.org/#append-a-request-origin-header).

## Prepare an identified source and private configuration

Use a reviewed commit or verified image. Keep source, personal input, canonical
state, secrets, approval evidence and backups separate. Never develop inside the
active runtime checkout. Keep private deployment values out of Git.

Start from `write-config.example.json`, replacing every synthetic identity and
scope, and selecting a short operational expiry. Store it in an owner-only
operator directory. The OAuth resource read grant remains the upper boundary.
The issuer must allow the new scope on the exact client, and both issuer and
protected-resource discovery must accurately advertise it. Keep PKCE, exact
callbacks, audience, issuer, signatures, expiry and refresh validation intact.
Re-authorize Gemini and refresh tool discovery after activation; an existing
read-only token will not expose write tools.

Run the HTTP process with the same authoritative stores plus:

```sh
python -m protocol.v1.transport.mcp_http \
  --oauth-config /private/operator/oauth-resource.json \
  --public-jwks /private/operator/public-jwks.json \
  --manifest /product/protocol/v1/fixtures/full-reference.instance.json \
  --personal-data-root /private/personal \
  --native-store-root /private/state/native-write \
  --portfolio-checkpoint-root /private/state/portfolio-checkpoints \
  --write-config /private/operator/write-config.json \
  --approval-store-root /private/approvals \
  --check-config
```

These are synthetic paths. Use the deployment's actual identified runtime root.
Preflight opens configured stores and may initialize lock files; it does not
apply updates. For a client that reshapes nested preview arguments, add
`--write-input-format json-string` to both preflight and the exact service
command. It adds a grant-filtered contract reader and flat JSON-string preview
declarations; it changes neither native validation nor apply/approval rules.
Keep `object` as the default for other deployments. Compare the submitted update
digest with the actual preview and changed fields, and retain the old command
for rollback. Activation clears in-memory previews; obtain fresh links afterward.

Remove `--check-config` only for the service startup. Continue to
bind MCP and authentication to loopback. The trial Keycloak Compose profile is
read-only and does not provide this Authelia approval profile.

## Narrow HTTPS and browser authentication routes

Preserve the current public origin, private routes, and OAuth endpoints. Add only
the review route to the MCP upstream, before the default authentication portal:

```caddyfile
@review path_regexp review ^/approve/[A-Za-z0-9_-]{43}$
handle @review {
    request_body {
        max_size 4KB
    }
    reverse_proxy 127.0.0.1:19090
}
```

Use the actual MCP listener. Retain exact Host gating and `Cache-Control:
no-store`. Do not log review URLs, cookies, tokens or bodies in proxy access logs.
Keep `/api/authz/*`, `/api/verify`, metrics, debug and other private endpoints
unavailable through the public proxy. The application itself checks browser
identity and CSRF before processing POST; unauthenticated GET redirects to the
same-origin login portal with the exact return URL.

Insert a narrow Authelia ACL before any catch-all deny rule:

```yaml
access_control:
  default_policy: deny
  rules:
    - domain: mcp.example.test
      policy: one_factor
      subject: [user:synthetic-owner]
      methods: [GET, POST]
      resources: ['^/approve/[A-Za-z0-9_-]{43}$']
```

Retain other existing deny rules. This ACL applies to cookie authorization, not
OIDC consent. Use the actual host and explicitly approved browser username, with
no bypass rule. Validate Authelia and Caddy candidates before replacing active
files. Check the actual cookie name and HTTPS domain against the write config.

## Verification before cutover

Run `python -m pytest protocol/v1/tests -q`. It exercises signed HTTP discovery,
native persistence and read-back, scope and identity binding, expiry, restart,
concurrency, stale previews, replay, CSRF, rejection, escaping and audit failure.

Test the exact installed Authelia binary in a separate synthetic instance:

```sh
PYTHONPATH=. python deploy/gemini/verify_authelia_approval.py \
  --authelia /verified/tools/authelia
```

This creates temporary synthetic credentials and SQLite state, selects a separate
loopback port, tests a real login and cookie authorization, and stops and removes
its own instance. It never reads the active identity database or publishes a
route. Do not treat this as actual Gemini verification.

Prepare private snapshots of the active service unit, issuer config, edge config,
and relevant metadata. Record hashes and current source ID, validate the new
source and candidate configuration, and preserve an exact rollback. Obtain live
write authorization before cutover when the existing grant was read-only.

## Cutover and rollback

Stop MCP during the configuration swap. Install the identified source and private
config, restart authentication and edge as needed, then start MCP. On failure,
restore exact configuration/unit snapshots and restart the former read service.
Leave existing Tailscale identities and routes unchanged. Do not reset private
Serve/Funnel state as part of enabling writes.

Verify public HTTPS, unchanged issuer/resource, unauthenticated MCP rejection,
private endpoint blocking, read-only token write denial and read regression.
The optional write profile includes `investkitchen.write` in its initial OAuth
`WWW-Authenticate` scope guidance as well as resource metadata. Clients prioritize
challenge scopes; advertising write scope only in metadata can leave reconnections
read-only. Existing read-only tokens still expose only reads, and requesting a
scope does not create a server grant. If Google reuses the old scope set, unlink
the MCP server in Google Account connections and reconnect the existing app.
If the scope request still omits write scope, obtain explicit approval to remove
the existing custom-app registration and recreate it with the same MCP URL and
client credentials. An existing registration can block a duplicate URL. Preserve
the exact callback allowlist and keep the registration settings private; do not
weaken token validation or broaden the grant to repair a stale scope request.

Re-authorize the actual Gemini app for write scope, inspect the four new tools,
and run the complete preview → manual NAS approval → apply → read-back flow on
an explicitly approved disposable payload. Do not create real portfolio edits
merely to test connectivity. Record deployment and actual operation separately.

To disable writes, restore the read-only service/config and remove the client
write scope. Restarting MCP discards pending approvals. Preserve approval evidence
and canonical journals. Code rollback does **not** undo already committed data;
review any data correction as a new authorized native update. Never restore a
stale state backup automatically over newer writes.
