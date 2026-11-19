# Configuration

<!-- sources: src/timesheet/config.py, src/timesheet/service/main.py, src/timesheet/service/auth.py, src/timesheet/service/store.py, src/timesheet/service/delivery.py, src/timesheet/collectors/mcp_client.py, src/timesheet/collectors/m365_graph.py, src/timesheet/collectors/m365_mcp.py, src/timesheet/run.py, .env.example -->

Every setting the service reads, with its type, default, and effect. Copy `.env.example` to `.env` as a starting point: only the first group is required.

Values come from the environment. Reconstruction defaults in the last groups are only the starting values a fresh account gets; each account overrides them for itself under **Settings**.

## Web service

The service refuses to start without `SECRET_KEY`. The other three are required as soon as anyone tries to sign in.

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `SECRET_KEY` | string | none | Signs sessions and encrypts all stored tokens with AES-256-GCM. Without it the service refuses to store any credential. Generate one: `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `PUBLIC_URL` | string | `http://localhost:8000` | The base URL the service is reachable at. Sets the OAuth callback to `<PUBLIC_URL>/auth/callback`, makes session cookies `Secure` when the scheme is `https`, and appears in delivered messages. |
| `GITHUB_OAUTH_CLIENT_ID` | string | none | GitHub OAuth app client ID. Register the app at **Settings → Developer settings → OAuth Apps** with `<PUBLIC_URL>/auth/callback` as the callback URL. |
| `GITHUB_OAUTH_CLIENT_SECRET` | string | none | GitHub OAuth app client secret. |
| `GITHUB_HOST` | string | `github.com` | The GitHub hostname to authenticate against. Set to your GHE Cloud domain (e.g. `acme.ghe.com`) or Enterprise Server hostname (e.g. `github.acme.internal`). A `.ghe.com` domain is GitHub-operated and has no `/api/v3`, so it must not be treated as an Enterprise Server. |
| `GITHUB_OAUTH_SCOPES` | string | `read:user user:email repo read:org` | OAuth scopes to request. The `repo` scope lets the commit search see private repositories. Remove it if you only track public work. |

## Access control

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `ACCESS_POLICY` | enum | `single` | Who may hold an account. `single`: the first account to sign in claims the deployment and everyone after is refused. `allowlist`: logins in `ALLOWED_LOGINS`. `org`: members of the orgs in `ALLOWED_ORGS`. `open`: anyone with an account on the GitHub host. The default is deliberate: a fresh deployment must not be an open door to a service that stores delegated mailbox tokens. |
| `ALLOWED_LOGINS` | string | (none) | Comma-separated GitHub logins. Required when `ACCESS_POLICY=allowlist`. |
| `ALLOWED_ORGS` | string | (none) | Comma-separated GitHub organization logins. Required when `ACCESS_POLICY=org`. |

## Storage

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `DATABASE_URL` | string | (none) | PostgreSQL DSN. When absent the service uses a local `FileStore` instead (single-writer, not for production). |
| `DATA_DIR` | path | `./data` | Directory for the `FileStore` when `DATABASE_URL` is not set. |

## Microsoft 365: MCP connector

The default sign-in path. Each person connects their own account from the **Connections** page with a device code, and nothing below needs setting unless a default doesn't fit.

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `M365_MCP_TENANT` | string | `organizations` | Entra authority. `organizations` lets Entra resolve each signer's home tenant automatically. Set to a specific tenant ID only if sign-in lands in the wrong directory (a UPN can be identical across two tenants). |
| `M365_MCP_SENT_FOLDER` | string | `Sent Items` | Mailbox folder to read as sent mail. Change this if your mailbox uses a different name. |
| `M365_MCP_INBOX_FOLDER` | string | `Inbox` | Mailbox folder to read as received mail. |
| `M365_MCP_TOKEN_CACHE` | path | `~/.m365-mcp-token.json` | Where the MCP connector writes its refresh token. This file is a standing credential: set a path that survives restarts and is not world-readable. In containers, use a path on a persistent volume. |
| `M365_MCP_TOKEN_JSON` | string | (none) | Inline JSON token, as an alternative to the cache file. Used in environments where writing a file is not possible. |

## Microsoft 365: direct Graph connector (alternative calendar source)

The Graph connector gives calendar events only: no mail, no chat. Use it when the MCP connector is blocked by your tenant. Requires `pip install ".[graph]"`.

!!! note
    There is no fallback between Microsoft sources on failure. A broken connector looks like a week with no meetings, and that reconstructs into a plausible but entirely wrong all-admin sheet.

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `M365_ICS_URL` | string | (none) | A published Outlook `.ics` URL (OWA → Settings → Calendar → Shared calendars). Calendar events only. The URL is effectively a credential. The "availability only" detail level hides every subject behind a bare "Busy". |
| `M365_CLIENT_ID` | string | Azure CLI app ID | Entra application client ID for the device-code Graph sign-in. The default is the Microsoft Azure CLI application (`04b07795-…`), which is present in most tenants. |
| `M365_TENANT` | string | (none) | Entra tenant ID or domain. When absent, the domain part of `M365_UPN` is used; if that is also absent, `organizations` is used. |
| `M365_UPN` | string | (none) | User principal name. The domain part is used as the Entra authority when `M365_TENANT` is not set. |
| `M365_TOKEN_CACHE` | path | `m365.token.json` | Where the Graph connector writes its token. |

## Single-user / CLI

For `python -m timesheet.collectors.github` and single-user installs that skip the web sign-in. The service reads each account's own stored credentials instead.

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `GITHUB_USER` | string | (none) | GitHub login. |
| `GITHUB_TOKEN` | string | (none) | GitHub personal access token. |

## Reconstruction defaults

The values below are starting defaults for every new account. Each account overrides them under **Settings**, so changing these after the first sign-in has no effect on existing accounts.

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `LOCALE` | enum | `en` | `en` or `nl`. |
| `TZ` | string | `UTC` | IANA timezone, e.g. `Europe/Amsterdam`. |
| `DAY_START` | string | `08:30` | Normal day-open time (`HH:MM`). Opens earlier if the day's activity shows it, but never before `EARLIEST_START_FLOOR`. |
| `EARLIEST_START_FLOOR` | string | `06:00` | The day never opens before this time regardless of activity. |
| `DAY_ROLLOVER_HOUR` | int | `5` | Work before this hour is credited to the previous calendar day (a 01:00 commit is last night's work). |
| `MIN_DAY_MINUTES` | int | `480` | Minimum day length in minutes. A day driven by heavy activity is allowed to run longer. |
| `ROTA_ENABLED` | bool | `false` | Add a fixed morning on-call slot. Off by default because most people don't have one. |
| `ROTA_MINUTES` | int | `75` | Duration of the on-call slot in minutes. |
| `INCLUDE_REVIEWS` | bool | `true` | Credit PR reviews. Reviews require a per-PR API call, so they run in the background refresh only. |
| `INCLUDE_AUTHORED` | bool | `true` | Credit PRs opened and issues authored. |
| `INCLUDE_EMAIL` | bool | `true` | Credit sent and received mail. |
| `INCLUDE_CHAT` | bool | `true` | Credit chat messages sent. |

## Scheduled job

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `BACKFILL_WEEKS` | int | `6` | How many recent weeks the scheduled run reconstructs if they are not yet stored. |
| `LIVE_MAX_AGE_SECONDS` | int | `900` | How long the refresh job's copy of the current week is served before a page view rebuilds it live. Set this to roughly your refresh interval. |

## Delivery

Mail belongs to the deployment, not to any individual account. Set `REPORT_EMAIL_FROM` and either the SMTP block or the Mailgun block. When both are set, Mailgun's API takes precedence.

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `REPORT_EMAIL_FROM` | string | (none) | Envelope sender address. Each delivered message has the account holder's address as `Reply-To`. |
| `SMTP_HOST` | string | (none) | SMTP server hostname. |
| `SMTP_PORT` | int | `587` | SMTP port. |
| `SMTP_STARTTLS` | bool | `true` | Use STARTTLS after connecting. |
| `SMTP_USER` | string | (none) | SMTP username. |
| `SMTP_PASSWORD` | string | (none) | SMTP password. |
| `MAILGUN_API_KEY` | string | (none) | Mailgun domain sending key. Mailgun's API wins over SMTP when both are configured. |
| `MAILGUN_DOMAIN` | string | (none) | Mailgun sending domain. |
| `MAILGUN_BASE_URL` | string | `https://api.mailgun.net` | Mailgun API base URL. Use `https://api.eu.mailgun.net` for EU domains: a key for the wrong region fails exactly like a wrong key. |
| `DELIVERY_CHAT_ENABLED` | bool | `false` | Enable Teams delivery. The shared Microsoft connector does not carry `ChatMessage.Send`, so leave this off unless your tenant grants it. |
| `DOWNLOAD_TTL_SECONDS` | int | `604800` | How long a signed `.xlsx` download link (the one emailed to a manager) stays valid. Default is 7 days. |

## AI label polish

Optional. When `LLM_MODEL` is empty the deterministic rule-based summariser runs instead. AI label polish only ever runs in the background refresh, never during a web request.

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `LLM_BASE_URL` | string | (none) | OpenAI-compatible API endpoint. |
| `LLM_API_KEY` | string | (none) | API key for the endpoint. |
| `LLM_MODEL` | string | (none) | Model name. An empty value disables AI. |
| `LLM_TIMEOUT` | float | `30.0` | Request timeout in seconds. |
| `LLM_REASONING_EFFORT` | string | `low` | Reasoning effort level, passed when non-empty. Set to empty if the model rejects the field. |
