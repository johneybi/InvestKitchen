# Gemini synthetic trial without a domain or hosted identity account

Optional advisory Plan create/replace/close uses the existing independent browser approval profile. See [native Plan lifecycle](../../docs/native-plan-lifecycle.md). Deployment and actual client verification remain separate.


See the [Korean runbook](README.ko.md) for complete commands and the
[client guide](../../docs/gemini-client-usage.md) for the read-only contract.

This optional trial runs Keycloak locally, an isolated Caddy edge, the
authenticated HTTP MCP, and an account-free Cloudflare Quick Tunnel. It creates
only an empty synthetic portfolio. Generated credentials and authentication DBs
must stay outside Git and outside production runtime/data/secret/backup trees.
The trial does not import personal data or mount existing production state.

Verified on 2026-10-14: local TLS and temporary public HTTPS OAuth/MCP tests
passed, including rejected writes and out-of-scope reads. The actual Gemini
account completed OAuth, registered InvestKitchen, discovered six read tools, and
called `list_portfolios`, `get_portfolio_state` and `get_current_knowledge` in the
English UI with Argon. Results showed only the empty `portfolio-alpha` and
synthetic Knowledge; authenticated HTTP success was also observed. Korean chat
omitted the connector from its mention menu, and Pro returned a generic error.
These results do not verify production deployment or personal-portfolio access.

Build identified product commits with `build-images.sh <commit>` on a trusted
builder. The script sends an allowlisted Git archive, never the current operator
workspace. Bootstrap a new external directory with `trial.py bootstrap --out
<directory> --image <commit-image> --edge-image <commit-edge-image>`.

Start `edge tunnel` with `compose.trial.yml`, obtain the temporary HTTPS origin,
and use Gemini Apps' custom-app dialog to enter that origin's `/mcp` URL. Expand
additional settings and copy the redirect URI before pressing Next. That URI is
bound to the actual user and MCP hostname; do not reuse it across accounts or
temporary origins. Configure the new trial using `trial.py configure --out <directory> --origin
<assigned-origin> --redirect-uri <verified-exact-callback>`. Do not guess the
Gemini Apps callback or replace it with a CLI/Enterprise callback. Reload Caddy
without recreating the edge/tunnel namespace, then start `auth`.

`verify_trial.py --out <directory> --oauth-only` verifies TLS, discovery, exact
callback restriction, PKCE S256, a real RS256 token, and the subject/client/read
grant. It stores only public RS256 signing keys in `oauth/public-jwks.json`.
Run the runtime with `INVESTKITCHEN_PREFLIGHT_ONLY=1`, then start it and run
`verify_trial.py --out <directory>` to exercise authenticated MCP reads, blocked
write calls and portfolio isolation. No token or password is printed.

The local-only override `compose.local.yml` uses `https://localhost:18443` and a
private test CA. Copy only its public certificate with `compose exec -T edge cat
/data/caddy/pki/authorities/local/root.crt`; pass the certificate with `--ca`.
Do not disable TLS verification or install the test CA in the system trust store.

In Gemini Apps, use Customize → Connectors → Create custom app where available.
Enter the public `/mcp` URL and the trial client ID/secret under additional
settings, then sign in using the synthetic owner. The external
`credentials.json` contains these values. Skill registration and MCP app
connection are separate. Verify actual tool use before claiming a live Gemini
connection; local tests alone do not establish client compatibility.

After Next, Gemini also shows a custom-app trust and privacy consent screen.
It explains that information available to Gemini, including skills and connected
services, may be sent to this MCP server. The server's read-only synthetic grant
limits what the server can return; it does not restrict what Gemini may send.
Review that separate consent before connecting, and use only synthetic prompts
for this trial.

If the app is registered but missing from the chat mention menu, check the
English Gemini page at `https://gemini.google.com/app?hl=en`. In the trial, that
page exposed the connector while the Korean page exposed only the skill. Type
`@InvestKitchen` and choose **Connectors → InvestKitchen** before entering the
synthetic read prompt. This does not establish regional eligibility.

Stop only the trial services to roll back. Preserve volumes and external
credentials unless the operator deliberately removes them. A restarted Quick
Tunnel gets a new origin; create a fresh trial rather than silently rewriting an
existing identity realm. Keycloak realm import skips an existing realm.

Keycloak development mode/H2 and temporary tunnels are not an operational
deployment. Quick Tunnels have no uptime guarantee and do not support SSE; this
MCP uses JSON responses. Production use still needs stable HTTPS, a supported
identity/DB deployment, secret management, backups, key rotation, real owner
grants and the repository's deployment preflight/cutover/rollback process.

Primary documentation:

- [Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
- [Keycloak MCP authorization](https://www.keycloak.org/securing-apps/mcp-authz-server)
- [Keycloak realm import](https://www.keycloak.org/server/importExport)
- [Google custom connected apps](https://support.google.com/gemini/answer/17209137)
