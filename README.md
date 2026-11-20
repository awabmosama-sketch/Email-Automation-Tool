# Email Automation

A FastAPI application for managing contacts, generating personalized email previews with Groq, and sending approved messages through Gmail or Zoho SMTP.

## Project Structure

```text
email-automation/
├── backend/
│   ├── app.py          # FastAPI routes, contact/send workflows, pitch + campaign APIs
│   ├── outreach.py     # Campaigns, senders, brands, templates, queue, reporting
│   ├── sheets.py       # Google Sheets parsing, header mapping, phone exclusion
│   ├── credential_crypto.py # Fernet encryption for stored sender secrets
│   ├── companies.py    # Pitching company profiles, services, and brand tones
│   ├── config.py       # Environment and file configuration
│   ├── database.py     # Neon PostgreSQL persistence
│   ├── deliverability.py # SPF/DKIM/DMARC DNS checks
│   ├── migrate_to_neon.py # One-time local data migration
│   ├── llm.py          # Groq email & pitch generation, spam scoring
│   ├── main.py         # Command-line batch sender (Neon-aware)
│   ├── sender.py       # SMTP delivery (Gmail, Zoho, Outlook, custom)
│   ├── suppressions.py # Unsubscribe suppression list
│   ├── unsubscribe.py  # Signed unsubscribe links
│   └── verifier.py     # Email format, domain, and MX checks
├── frontend/           # Dashboard, sign-in page, and campaign console
├── .local-data/        # Local CSV/JSON fallback when DATABASE_URL is unset
├── api/index.py        # Vercel FastAPI entrypoint
├── vercel.json         # Vercel routing configuration
├── .env.example        # Safe environment variable template
├── .env                # Local secrets, not committed
└── requirements.txt
```

## Setup

Runtime dependencies live in `requirements.txt`; test-only dependencies (pytest, httpx) live in `requirements-dev.txt`.

Run `ruff check backend` before committing; `pytest tests -q` runs the suite.

Use the existing virtual environment or create one:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and add your values:

```powershell
Copy-Item .env.example .env
```

The default provider is Groq. To use Anthropic instead, set `LLM_PROVIDER=anthropic` and provide `ANTHROPIC_API_KEY` plus an optional `ANTHROPIC_MODEL`.

### Running locally

Start the server from the project root:

```bash
venv/Scripts/python.exe -u -m uvicorn main:app --host 127.0.0.1 --port 8000 --log-level info > .local-data/uvicorn.log 2>&1
```

Error-response references (e.g. "Reference: cfcdcf06") correspond to traceback entries in `.local-data/uvicorn.log`.

For local iteration, add `--reload`:

```bash
venv/Scripts/python.exe -u -m uvicorn main:app --host 127.0.0.1 --port 8000 --log-level info --reload
```

## Email Sending (SMTP)

`SMTP_PROVIDER` chooses the mail service and its defaults:

| Provider | Host | Port | Security |
|---|---|---|---|
| `zoho` (default) | `smtp.zoho.com` | 465 | SSL |
| `gmail` | `smtp.gmail.com` | 465 | SSL |
| `custom` | set `SMTP_HOST` | set `SMTP_PORT` | set `SMTP_SECURITY` |

For Zoho, fill in only these two values in `.env`:

```dotenv
SMTP_PROVIDER=zoho
SMTP_USER=you@yourdomain.com
SMTP_PASSWORD=your_zoho_app_password
```

Notes:

- `SMTP_USER` must be the full Zoho mailbox address, and the `From` address comes from it. It can be the account address or a verified alias.
- If 2-step verification is enabled on the Zoho account, create an **App Password** (Zoho Accounts → Security → App Passwords) and use that as `SMTP_PASSWORD`.
- Zoho has regional data centres. If your account is not on the global domain, uncomment and set `SMTP_HOST` to `smtp.zoho.in`, `smtp.zoho.eu`, `smtp.zoho.com.au`, or `smtp.zoho.jp`.
- Port `587` with STARTTLS is also supported: set `SMTP_PORT=587` and `SMTP_SECURITY=starttls`.
- Legacy `GMAIL_USER` / `GMAIL_APP_PASSWORD` still work, but only when `SMTP_PROVIDER=gmail`.
- `/api/health` reports `smtp_configured`, the active `smtp_provider`, and the `sender_domain`.
- The dashboard topbar has a provider toggle that switches between Zoho and Gmail for the current session without editing `.env`. The pitch form can also override the SMTP user, app password, and From name per send.

Sign-in is enabled when both `APP_EMAIL` and `APP_PASSWORD` are set. Visitors then get a dedicated login page, and the dashboard/API require a signed session cookie (valid for 7 days). Set `SESSION_SECRET` to keep sessions valid across restarts. With either value unset, authentication is disabled, which is only appropriate for local development.

Set `DATABASE_URL` to a Neon PostgreSQL connection string to persist contacts, company profiles, and send logs in the database. The required tables are created automatically on the first database request. Without `DATABASE_URL`, the app keeps using the local CSV and JSON files.

To migrate the existing local contacts and company profiles after setting `DATABASE_URL`, run:

```powershell
.\venv\Scripts\python.exe -m backend.migrate_to_neon
```

## Run The Web App

Run this command from the project root:

```powershell
.\venv\Scripts\python.exe -m uvicorn backend.app:app --reload
```

The project also includes a root `main.py` entrypoint, so this shorter command works too:

```powershell
uvicorn main:app --reload
```

Open <http://127.0.0.1:8000> for the dashboard or <http://127.0.0.1:8000/docs> for the API documentation.

The dashboard can:

- Add and select contacts with the `+ New` action.
- Edit a selected contact and save changes without creating a duplicate.
- Remove a selected contact after confirmation.
- Remove unwanted pitching companies from the company selector.
- Validate an email address.
- Generate an AI-powered preview from the contact context.
- Send the approved preview to the selected contact.

## Run The Batch Sender

```powershell
.\venv\Scripts\python.exe -m backend.main
```

The dashboard and the batch sender both observe `DAILY_LIMIT` (default 15, minimum 15) by counting today's successful sends in Neon or `.local-data/sent_log.csv`. The batch sender also validates contacts and checks spam scores.

## Deliverability Requirements

No application can guarantee that every receiving provider will place a message in the inbox, but the app now does the application-side work:

- Standards-compliant multipart (`text/plain` + `text/html`) messages with `Date`, `Message-ID` on the sender's domain, and `Reply-To`.
- One-click unsubscribe: `List-Unsubscribe` and `List-Unsubscribe-Post: List-Unsubscribe=One-Click` headers plus a visible footer link, all pointing at signed `/unsubscribe` links.
- A suppression list. Unsubscribed addresses are stored in Neon (`suppressions` table) or `.local-data/suppressions.csv` and rejected by the dashboard, the pitch endpoint, and the batch sender.
- Subject lines are included in the AI spam score, not just the body.
- A **DNS authentication** check in the dashboard (topbar **DNS** button or **Check DNS authentication** in the pitch form) that validates SPF, DKIM, and DMARC for the active sending domain via `/api/deliverability`.

Set `APP_BASE_URL` to the public URL of the deployment so unsubscribe links work from production email. If it is unset, the app falls back to the request host; emails only get a mailto unsubscribe fallback.

For reliable delivery, configure the sending domain as well:

1. Use a domain-based sender address rather than a new or mismatched free mailbox when possible.
2. Publish SPF for the service that sends mail. For Zoho, this is normally `include:zoho.com` (or the regional variant shown in Zoho's admin panel); for Google Workspace it normally includes `include:_spf.google.com`. The panel's SPF field only renders `include:` entries, so add any `ip4:`/`ip6:` terms manually at your DNS provider.
3. Enable DKIM signing in Zoho Mail Admin or Google Workspace and publish the DKIM DNS record it provides.
4. Publish a DMARC record for the sender domain, starting with a monitoring policy such as `p=none` while verifying reports.
5. Keep the `From` domain aligned with SPF/DKIM, send only to opted-in recipients, remove bounces, and ramp volume gradually. Cold outreach is safer from a dedicated, warmed subdomain.
6. Test with Gmail's **Show original** and a mail-tester service to confirm SPF, DKIM, and DMARC pass. Gmail's Promotions tab is also influenced by content and engagement, so keep first emails short and personal.

The SMTP credentials authenticate delivery, but they cannot override a receiver's spam policy or a domain's missing DNS authentication.

To manually clear an address from the suppression list, delete its row from the `suppressions` table in Neon or from `.local-data/suppressions.csv`.

## Campaign Console

Open <http://127.0.0.1:8000/campaigns> (the **Campaigns** button lives in the composer topbar). The console manages:

- **Senders** — one or more SMTP accounts (Zoho, Gmail, or custom). Microsoft 365 disabled basic SMTP authentication for Exchange Online, so Outlook/M365 senders require OAuth, which is not implemented — the provider is no longer offered (existing Outlook rows stay editable but cannot authenticate). Google Workspace admins may also disable app passwords, in which case a Gmail sender needs the admin to allow an app-specific password. Secrets are encrypted with Fernet using `SENDER_CREDENTIAL_KEY` (falling back to `SESSION_SECRET`). Each sender can have its own hourly/daily cap, timezone, and per-domain DNS setup: DKIM selector, SPF include, and DMARC policy (`none`, `quarantine`, or `reject`). **Test** checks the SMTP login, or sends a real test email when a recipient is entered. **DNS** shows SPF, DKIM, and DMARC status plus the exact records to publish for that domain, using the sender's configured include, selector, and policy.
- **Brands** — reusable domain, description, and signature.
- **Templates** — subject/body with `{{First Name}}`, `{{Position}}`, `{{Company Name}}`, `{{signature}}`, and other placeholders. An optional AI prompt is used only when the campaign enables AI personalisation.
- **Campaigns** — each campaign is isolated: its own Google Sheet URL/tab/gid, column mapping, template, brand, sender, timezone, hourly cap, daily cap, randomised delay window (default 4–12 minutes), working hours, and weekday-only flag.

### Upgrading from a pre-SESSION_SECRET deploy

Sender passwords are encrypted with a key derived from `SENDER_CREDENTIAL_KEY` (falling back to `SESSION_SECRET`). Two different situations leave stored rows unreadable by the current key, and they need opposite responses.

**Case A — you set a custom `SESSION_SECRET` and have since rotated it.** Those credentials are still private, so tell the app the old secret once:

1. Set `SENDER_CREDENTIAL_KEY_LEGACY` to that previous custom `SESSION_SECRET` and deploy. Only if this deployment also wrote rows before you ever set `SESSION_SECRET` (using the built-in fallback) should you comma-append that old built-in value, taken from your own deployment history — never from this README.
2. Re-save or **Test** each sender listed by the startup warning. Each read decrypts with the legacy key, re-encrypts under the current key, and persists the new ciphertext, so the list drains as senders are used.
3. When the warning reports no ids and a count of zero, unset `SENDER_CREDENTIAL_KEY_LEGACY` and redeploy.
4. If a sender cannot be decrypted with the current key or any legacy key, the app raises `CredentialDecryptError` naming that sender id and sends nothing for it. Re-enter that sender's password in the dashboard.

**Case B — you never set `SESSION_SECRET` and relied on the built-in fallback.** Every sender password written in that period was encrypted with a key that is public in this repository's history, so treat those passwords as compromised. Pointing `SENDER_CREDENTIAL_KEY_LEGACY` at that key does **not** fix this — it would keep decrypting with a value anyone can read.

1. Do **not** set `SENDER_CREDENTIAL_KEY_LEGACY`. If the startup log reports it as the public built-in fallback, unset it and redeploy.
2. Rotate each sender's SMTP or app password at the provider (Zoho, Google Workspace, or your custom host).
3. Save the new password on each sender in the campaign console. Saving a password re-encrypts it under the current key, so nothing needs to be decrypted from the old rows.
4. Do this before the next send. Senders still holding old ciphertext fail with `CredentialDecryptError` naming them instead of sending.

If you are not sure which case applies, treat it as Case B: rotating a password is always safe, and no legacy key is needed afterwards.

The startup audit and the warning only ever handle ciphertexts and sender ids; keys and plaintext are never logged. If `SENDER_CREDENTIAL_KEY_LEGACY` holds a publicly known value, startup logs an unmissable error calling it a compromise path rather than a recovery path; the app still starts so an operator mid-migration is not stranded.

### Google Sheets ingestion

Paste a Google Sheets URL. The app maps only five columns: **First Name**, **Position / Job Title**, **Company Name**, **Personal Email**, and **Business Email**. Phone/mobile columns are detected and ignored. Use **Preview sheet** to inspect headers and the auto-detected mapping before saving.

Two read modes are supported:

1. **Public link** — set the sheet to "Anyone with the link can view". No Google credentials are needed; the app reads the CSV export.
2. **Service account** — set `GOOGLE_SERVICE_ACCOUNT_FILE` (path) or `GOOGLE_SERVICE_ACCOUNT_JSON` (inline JSON), then share the sheet with the service account's `client_email` as a Viewer.

**Sync** imports new rows, skips duplicates/invalid addresses, and removes addresses on the suppression list. Emails cannot be imported twice into the same campaign.

### Queue, rate limits, and scheduling

The queue is durable without Redis/Celery: campaign state is stored as one JSON document in PostgreSQL (`app_meta`) when `DATABASE_URL` is set, with an atomic row lock, or in `.local-data/outreach.json` for local development. That file fallback is development-only: outside development the app refuses to start the worker and refuses a queue tick without `DATABASE_URL`, because separate processes would claim the same recipient and double-send. A claim marks a recipient `sending` under the lock, so parallel campaigns, worker threads, and serverless ticks cannot send the same recipient twice. Stale claims are handled in two phases instead of being reported failed immediately: after 45 minutes an unconfirmed claim becomes `stale` (not counted as failed and never retried), and only after 3 hours total is it marked `failed` with reason `unconfirmed`. The 45-minute first window covers a worst-case connect + TLS + DATA + provider acknowledgement with one retry, with roughly ten times margin for provider throttling.

Each campaign enforces:

- `hourly_limit` and `daily_limit` in the campaign's timezone.
- A randomised delay between sends (`delay_min_minutes`–`delay_max_minutes`, default 4–12 minutes).
- A working-hour window and optional weekdays-only schedule.

Long-running processes start an in-process worker (`QUEUE_WORKER_ENABLED=true`, tick every `QUEUE_TICK_SECONDS`). On Vercel the project includes a one-minute cron for `/api/queue/tick`; set `CRON_SECRET` in the project (Vercel sends it as `Authorization: Bearer …`). The same endpoint accepts `X-Queue-Secret` or `?secret=`. The **Run queue now** button calls it from the dashboard.

### Reporting

A row reported `failed` can mean "sent but unconfirmed": it was claimed, no result came back within the 45-minute first window, and the 3-hour second window expired before it was marked failed. Treat such rows as uncertain before retrying; the requeue action only retries `failed` and `bounced` rows and never touches `stale` rows, which may already be in the recipient's inbox.

The console reports **Sent**, **Queued**, **Bounced**, **Inboxed**, and **Spam Alert** per campaign, plus bounce/spam rates, a recipient table (with manual status correction), recent events, and a 14-day send series. Provider or mailbox feedback can be posted to `POST /api/outreach/webhook` with `{ "email": "...", "status": "inboxed|spam_alert|bounced", "campaign_id": "..." }` and the shared secret header.

Before a campaign starts, the UI checks the selected sender's SPF/DKIM/DMARC and asks for confirmation when authentication is incomplete. DNS is advisory, not a hard block, so a resolver outage cannot strand a campaign.


## Company-Service Pitching Email Generator

The system can generate personalized B2B service pitch emails on behalf of a client company (such as **F-Tune**, **CloudPulse**, or **GrowthWave**):

- **Company Profiles (`backend/companies.py` & `.local-data/companies.json`)**: Persistent storage for pitching brands defining offered services, value proposition, tone, and signature block.
- **Personalized Pitch Generation (`backend/llm.py`)**: The LLM analyzes the recipient's role and industry, chooses only the 1-2 most relevant services, enforces brand voice and signature, and avoids spam trigger words.
- **Endpoints**:
  - `GET /companies`: Returns available company profiles.
  - `POST /companies`: Registers a new pitching company with custom services, value proposition, tone, and signature.
  - `DELETE /companies/{company_name}`: Removes an unwanted pitching company after confirmation in the dashboard.
  - `PUT /api/contacts/{email}`: Saves changes to an existing contact.
  - `DELETE /api/contacts/{email}`: Removes an existing contact.
  - `POST /pitch/generate`: Validates email and generates personalized pitch with spam scoring.
  - `POST /pitch/send`: Delivers pitch email via the configured SMTP provider after recomputing the spam score server-side. A score of 7+ requires an explicit `confirm_high_spam` override. Delivery is tagged as `pitch:sent` or `pitch:failed` in Neon or `.local-data/sent_log.csv`.
- **Manual Company Registration UI**: Use the **"+ New"** button next to the company dropdown to open the modal and register any client company on the fly.
- **Live Spam Guard & Safety**: Displays color-coded spam scores (green &le; 3, yellow 4-6, red &ge; 7) and enforces explicit confirmation dialogs before sending high-risk messages.

## Push Checklist

Before pushing changes:

```powershell
pip install -r requirements.txt
python -m compileall -q backend
node --check frontend\app.js
node --check frontend\campaigns.js
```

Keep `.env`, `venv/`, `.local-data/`, and generated caches out of version control. Do not commit API keys or SMTP/app passwords.

## Deploy On Vercel

The repository includes a Vercel Python function adapter in `api/index.py` and the routing configuration in `vercel.json`.

1. Import the repository into Vercel.
2. Add `DATABASE_URL`, `LLM_PROVIDER`, the matching `GROQ_API_KEY` or `ANTHROPIC_API_KEY`, `SMTP_PROVIDER`, `SMTP_USER`, `SMTP_PASSWORD`, `SENDER_NAME`, `APP_BASE_URL`, `APP_EMAIL`, `APP_PASSWORD`, `SESSION_SECRET`, and `CRON_SECRET` as project environment variables. Add `SMTP_HOST` only when using a regional Zoho host or `SMTP_PROVIDER=custom`, and `DKIM_SELECTOR` if your DKIM selector is custom. For the campaign console, optionally add `SENDER_CREDENTIAL_KEY`, `GOOGLE_SERVICE_ACCOUNT_FILE`/`GOOGLE_SERVICE_ACCOUNT_JSON`, `DEFAULT_*` campaign limits, and set `QUEUE_WORKER_ENABLED=false`. Redeploy after changing any environment variable.
3. Deploy with the default Vercel build settings.

When `DATABASE_URL` is configured, Neon is used for durable shared contacts, company profiles, and send logs. The file-based fallback remains available for local development.