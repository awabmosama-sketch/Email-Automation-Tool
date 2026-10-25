# Email Automation

A FastAPI application for managing contacts, generating personalized email previews with Groq, and sending approved messages through Gmail or Zoho SMTP.

## Project Structure

```text
email-automation/
├── backend/
│   ├── app.py          # FastAPI routes, contact/send workflows, pitch endpoints
│   ├── companies.py    # Pitching company profiles, services, and brand tones
│   ├── config.py       # Environment and file configuration
│   ├── database.py     # Neon PostgreSQL persistence
│   ├── deliverability.py # SPF/DKIM/DMARC DNS checks
│   ├── migrate_to_neon.py # One-time local data migration
│   ├── llm.py          # Groq email & pitch generation, spam scoring
│   ├── main.py         # Command-line batch sender (Neon-aware)
│   ├── sender.py       # SMTP delivery (Gmail, Zoho, or custom)
│   ├── suppressions.py # Unsubscribe suppression list
│   ├── unsubscribe.py  # Signed unsubscribe links
│   └── verifier.py     # Email format, domain, and MX checks
├── frontend/           # Dashboard + sign-in page (Pitch Generator & Preview)
├── .local-data/        # Local CSV/JSON fallback when DATABASE_URL is unset
├── api/index.py        # Vercel FastAPI entrypoint
├── vercel.json         # Vercel routing configuration
├── .env.example        # Safe environment variable template
├── .env                # Local secrets, not committed
└── requirements.txt
```

## Setup

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
2. Publish SPF for the service that sends mail. For Zoho, this is normally `include:zoho.com` (or the regional variant shown in Zoho's admin panel); for Google Workspace it normally includes `include:_spf.google.com`.
3. Enable DKIM signing in Zoho Mail Admin or Google Workspace and publish the DKIM DNS record it provides.
4. Publish a DMARC record for the sender domain, starting with a monitoring policy such as `p=none` while verifying reports.
5. Keep the `From` domain aligned with SPF/DKIM, send only to opted-in recipients, remove bounces, and ramp volume gradually. Cold outreach is safer from a dedicated, warmed subdomain.
6. Test with Gmail's **Show original** and a mail-tester service to confirm SPF, DKIM, and DMARC pass. Gmail's Promotions tab is also influenced by content and engagement, so keep first emails short and personal.

The SMTP credentials authenticate delivery, but they cannot override a receiver's spam policy or a domain's missing DNS authentication.

To manually clear an address from the suppression list, delete its row from the `suppressions` table in Neon or from `.local-data/suppressions.csv`.

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
```

Keep `.env`, `venv/`, `.local-data/`, and generated caches out of version control. Do not commit API keys or SMTP/app passwords.

## Deploy On Vercel

The repository includes a Vercel Python function adapter in `api/index.py` and the routing configuration in `vercel.json`.

1. Import the repository into Vercel.
2. Add `DATABASE_URL`, `LLM_PROVIDER`, the matching `GROQ_API_KEY` or `ANTHROPIC_API_KEY`, `SMTP_PROVIDER`, `SMTP_USER`, `SMTP_PASSWORD`, `SENDER_NAME`, `APP_BASE_URL`, `APP_EMAIL`, `APP_PASSWORD`, and `SESSION_SECRET` as project environment variables. Add `SMTP_HOST` only when using a regional Zoho host or `SMTP_PROVIDER=custom`, and `DKIM_SELECTOR` if your DKIM selector is custom. Redeploy after changing any environment variable.
3. Deploy with the default Vercel build settings.

When `DATABASE_URL` is configured, Neon is used for durable shared contacts, company profiles, and send logs. The file-based fallback remains available for local development.