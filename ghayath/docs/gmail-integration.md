# Gmail integration (OpenAPI v1.2)

Gmail is an additive provider. The existing agent pipeline, permission engine,
approval gates, immutable audit log, idempotency service, and unified inbox
remain authoritative. The Gmail adapter is in `app/integrations/gmail.py`; the
application service is in `app/services/gmail_service.py`; routes do not issue
SQL or provider HTTP calls.

## Google Cloud setup

1. Create or select a Google Cloud project.
2. Enable **Gmail API**.
3. Configure the OAuth consent screen and add the account/test users required by
   the Google project. The first release requests only:
   `https://www.googleapis.com/auth/gmail.readonly`.
4. Create an OAuth client of type **Web application**. Keep the client secret in
   a secret manager or deployment environment; never commit it.
5. Add the exact deployment redirect URI. It must match the environment value
   byte-for-byte:
   `https://YOUR_HOST/api/v1/integrations/gmail/oauth/callback`
6. Generate a Fernet key for server-side token encryption using a trusted secret
   manager or an equivalent local command such as `python -c
   "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
   Store the result as a secret, not in source control or logs.

The server performs the authorization-code exchange, stores encrypted token
material in `integrations.config_enc`, refreshes expired access tokens, and
revokes the stored refresh token on disconnect. Access and refresh tokens are
never returned in an API response.

## Environment variables

All names use the `GHAYATH_` prefix from `.env.example`:

- `GHAYATH_GMAIL_CLIENT_ID`
- `GHAYATH_GMAIL_CLIENT_SECRET`
- `GHAYATH_GMAIL_REDIRECT_URI`
- `GHAYATH_EMAIL_TOKEN_ENCRYPTION_KEY` (Fernet key)
- `GHAYATH_GMAIL_API_BASE_URL` (default: Gmail REST API)
- `GHAYATH_GOOGLE_OAUTH_BASE_URL` (default: Google OAuth)
- `GHAYATH_GMAIL_OAUTH_STATE_TTL_SECONDS` (default: 600)

No real `.env` file, client secret, token, or encryption key belongs in Git.

## Connection states and failure behavior

- `REQUIRES_CONNECTION`: no usable server-side token is configured, credentials
  are absent, or Gmail has not completed OAuth. Reads and sync return
  `INTEGRATION_OFFLINE` / HTTP 503; they do not return an empty inbox.
- `CONNECTED`: encrypted token material is available and the provider can be
  used. Expired access tokens are refreshed server-side.
- `DEGRADED`: stored encrypted state cannot be safely used or a provider
  health state requires operator attention.
- `DISCONNECTED`: the owner completed the documented revoke operation. Google
  revocation is confirmed before local encrypted state is removed.

OAuth state is cryptographically random, stored only as a SHA-256 hash, bound to
the initiating owner, short-lived, and consumed once. Invalid, expired, or
replayed state is a validation failure. Provider error bodies and OAuth details
are not exposed to clients.

## Reads, normalization, and sync

`GET /integrations/gmail/messages` first obtains Gmail list-page IDs and then
fetches full details for every ID. Provider `nextPageToken` values are returned
unchanged. Message normalization preserves the original Gmail `messageId` and
`threadId`, and includes sender, recipients, subject, timestamp, labels,
body/snippet, and attachment metadata. `GET /integrations/gmail/threads/{id}`
returns all normalized messages in the provider thread.

`POST /integrations/gmail/sync` requires `Idempotency-Key`. It upserts using the
original Gmail message ID, so retrying a page cannot create a duplicate unified
inbox row. Gmail messages are classified only from their observed content.
Identity is `VERIFIED` only when a future verified identity/contact mapping is
actually present; current messages are `IDENTITY_UNVERIFIED` or `UNKNOWN`, with
`contact_id` and `crm_id` left null when evidence is absent. Name similarity is
never treated as proof.

The additive PostgreSQL migration `sql/002_gmail_oauth.sql` creates the one-time
OAuth-state table and provider-message mapping table. `sql/001_init.sql` is not
modified.

## Real-provider verification

This repository contains transport-boundary tests with synthetic responses for
malformed responses, retries, pagination, normalization, encrypted storage,
state handling, identity labels, deduplication, and truthful unavailable
states. No Google OAuth client credentials are present in this workspace, so a
real Gmail provider test remains **BLOCKED — CREDENTIALS NOT AVAILABLE** until an
operator configures the variables above and performs an authorized connection.
