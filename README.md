# Email Automation

A FastAPI application for managing contacts, generating personalized email previews with Groq, and sending approved messages through Gmail SMTP.

## Project Structure

```text
email-automation/
├── backend/
│   ├── app.py          # FastAPI routes, contact/send workflows, pitch endpoints
│   ├── companies.py    # Pitching company profiles, services, and brand tones
│   ├── config.py       # Environment and file configuration
│   ├── database.py     # Neon PostgreSQL persistence
│   ├── migrate_to_neon.py # One-time local data migration
│   ├── llm.py          # Groq email & pitch generation, spam scoring
│   ├── main.py         # Command-line batch sender
│   ├── sender.py       # Gmail SMTP delivery
│   └── verifier.py     # Email format, domain, and MX checks
├── frontend/           # Browser dashboard assets (Pitch Generator & Preview)
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

The batch sender observes `DAILY_LIMIT`, validates contacts, checks spam scores, and writes delivery results to Neon when `DATABASE_URL` is configured.

## Deliverability Requirements

No application can guarantee that every receiving provider will place a message in the inbox. The code now sends a standards-compliant multipart message, includes normal `Date` and `Message-ID` headers, provides unsubscribe metadata, escapes HTML, and blocks messages with a spam score of 7 or higher.

For reliable delivery, configure the sending domain as well:

1. Use a domain-based sender address rather than a new or mismatched free mailbox when possible.
2. Publish SPF for the service that sends mail. For Google Workspace, this normally includes `include:_spf.google.com`.
3. Enable DKIM signing in Google Workspace and publish the DKIM DNS record it provides.
4. Publish a DMARC record for the sender domain, starting with a monitoring policy such as `p=none` while verifying reports.
5. Keep the `From` domain aligned with SPF/DKIM, send only to opted-in recipients, remove bounces, and ramp volume gradually.
6. Test with Gmail's **Show original** and a mail-tester service to confirm SPF, DKIM, and DMARC pass.

The SMTP credentials authenticate delivery, but they cannot override a receiver's spam policy or a domain's missing DNS authentication.

## Company-Service Pitching Email Generator

The system can generate personalized B2B service pitch emails on behalf of a client company (such as **F-Tune**, **CloudPulse**, or **GrowthWave**):

- **Company Profiles (`backend/companies.py` & `data/companies.json`)**: Persistent JSON storage for pitching brands defining offered services, value proposition, tone, and signature block.
- **Personalized Pitch Generation (`backend/llm.py`)**: The LLM analyzes the recipient's role and industry, chooses only the 1-2 most relevant services, enforces brand voice and signature, and avoids spam trigger words.
- **Endpoints**:
  - `GET /companies`: Returns available company profiles.
  - `POST /companies`: Registers a new pitching company with custom services, value proposition, tone, and signature.
  - `DELETE /companies/{company_name}`: Removes an unwanted pitching company after confirmation in the dashboard.
  - `PUT /api/contacts/{email}`: Saves changes to an existing contact.
  - `DELETE /api/contacts/{email}`: Removes an existing contact.
  - `POST /pitch/generate`: Validates email and generates personalized pitch with spam scoring.
  - `POST /pitch/send`: Delivers pitch email via Gmail SMTP and logs delivery tagged as `pitch:sent` or `pitch:failed` in `data/sent_log.csv`.
- **Manual Company Registration UI**: Use the **"+ New"** button next to the company dropdown to open the modal and register any client company on the fly.
- **Live Spam Guard & Safety**: Displays color-coded spam scores (green &le; 3, yellow 4-6, red &ge; 7) and enforces explicit confirmation dialogs before sending high-risk messages.

## Push Checklist

Before pushing changes:

```powershell
pip install -r requirements.txt
python -m compileall -q backend
node --check frontend\app.js
```

Keep `.env`, `venv/`, `.local-data/`, and generated caches out of version control. Do not commit API keys or Gmail app passwords.

## Deploy On Vercel

The repository includes a Vercel Python function adapter in `api/index.py` and the routing configuration in `vercel.json`.

1. Import the repository into Vercel.
2. Add `DATABASE_URL`, `LLM_PROVIDER`, the matching `GROQ_API_KEY` or `ANTHROPIC_API_KEY`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, and `SENDER_NAME` as project environment variables.
3. Deploy with the default Vercel build settings.

When `DATABASE_URL` is configured, Neon is used for durable shared contacts, company profiles, and send logs. The file-based fallback remains available for local development.