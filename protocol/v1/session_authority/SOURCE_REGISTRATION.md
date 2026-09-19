# Native session source registration

`source_registration.py` is the personal-use replacement for the legacy
session-input registration step. It does not extract trading ideas and makes no
model or provider calls. The caller supplies both the raw source file and the
already structured SessionBrief/Amendment/MaterialContext bundle.

The registration root is explicitly chosen by the caller and must live outside
the InvestKitchen Git repository. Each accepted source is copied byte-for-byte
into an immutable registration directory. Directories are `0700`; source and
metadata files are `0600`. Metadata stores hashes and authority identifiers only,
not the original local source path.

Registration binds one `document_id` to the raw SHA-256, session date,
`SESSION_TRADING_INPUT` authority, and canonical structured-bundle SHA-256. An
exact replay is idempotent. Reusing the same document id or raw source hash with
a different source/bundle binding fails closed rather than rewriting history.

Bundle publication is a separate gate. Before calling the existing
`SessionAuthorityProducer`, validation requires every SessionBrief source document
and every candidate, mapping, ground, amendment, and context provenance document
id to have a matching private registration for that exact bundle. This permits a
multi-source bundle to be registered one raw source at a time while publication
remains blocked until all referenced sources are present.

Local operator examples:

```bash
python3 protocol/v1/deployment/session_source_registration_cli.py register \
  --source /private/inbox/checkpoint.txt \
  --bundle /private/inbox/session-bundle.json \
  --document-id checkpoint-2026-09-17 \
  --registration-root /private/investkitchen/session-source-registrations

python3 protocol/v1/deployment/session_source_registration_cli.py validate \
  --bundle /private/inbox/session-bundle.json \
  --registration-root /private/investkitchen/session-source-registrations

python3 protocol/v1/deployment/session_source_registration_cli.py publish \
  --bundle /private/inbox/session-bundle.json \
  --registration-root /private/investkitchen/session-source-registrations \
  --exchange-root /private/investkitchen/session-exchange
```

The registration store contains private raw material and must not be placed in
Git, repository fixtures, or the neutral runtime exchange.
