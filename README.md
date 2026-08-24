# Job Matcher

Job Matcher is a privacy-conscious graduate job-matching web application. Students confirm a resume-derived profile, choose what they want, receive explainable job matches, and can opt into a seven-day Telegram alert period. The bot sends links only after the Telegram account shares the same mobile number registered on the verified Job Matcher account.

The repository includes a deployment-ready baseline for GitHub and Vercel. A real launch still requires managed services, verified sender credentials, monitoring, backups, and legal/security review. Seed vacancies are visibly labelled **DEMO ONLY** and are never presented as real openings.

## What is included

- Email registration, OTP verification, sign-in, and expiring password-reset links
- Optional Google OpenID Connect sign-in with verified-email account linking
- Safe resume upload for PDF, DOCX, ODT, RTF, TXT, Markdown, HTML, JPG, PNG, and WebP
- Review-before-save extraction for education, skills, experience, and graduation year
- Role, location, work-mode, job-type, score, delivery-time, timezone, and 3/5/10-link preferences
- Transparent 100-point matching with matched skills, gaps, reasons, and trust signals
- Authorized job-feed adapters for Adzuna, Himalayas, Greenhouse, Lever, and Ashby
- Permanent per-student send ledger so the same job is not sent twice
- Telegram ownership verification using a short-lived, single-use start token and shared contact
- Seven-day activation/renewal, daily limits, pause/resume, and expiry reminder
- Save/apply/interview/offer/rejection tracker, weekly report, and preparation toolkit
- Job reporting and a protected administrator review console
- Account JSON export, resume deletion, full account deletion, privacy, terms, and scam-safety pages
- Responsive design, accessible controls, reduced-motion support, and lightweight animations

## Quick start (Windows PowerShell)

Requirements: Python 3.13 and [uv](https://docs.astral.sh/uv/).

```powershell
Set-Location "C:\path\to\friendly jobs"
Copy-Item .env.example .env
uv sync --python 3.13 --extra dev
uv run flask --app run.py init-db
uv run flask --app run.py run --debug
```

Open `http://127.0.0.1:5000`. In development without SMTP, verification and password-reset emails are captured by the in-memory mail backend; OTP codes are also shown as a development-only flash message.

`init-db` applies the checked-in Alembic migration before adding optional demo data. For later schema changes, create and review a new `flask db migrate` revision and run `flask db upgrade` during deployment.

Generate a real secret before any shared deployment:

```powershell
py -3.13 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Set that value as `SECRET_KEY`. Production startup intentionally fails unless `APP_ENV=production`, a strong secret, `FLASK_DEBUG=0`, `COOKIE_SECURE=true`, an HTTPS `APP_BASE_URL`, managed PostgreSQL, shared rate-limit storage, and separate scheduler and Telegram webhook secrets are configured.

For a typical deployment behind one trusted reverse proxy:

```dotenv
RATELIMIT_STORAGE_URI=rediss://username:password@redis.example.com:6379/0
TRUSTED_PROXY_HOPS=1
```

Set `TRUSTED_PROXY_HOPS` to the exact number of proxies you operate; leave it at `0` when clients connect directly. Trusting too many forwarded hops lets clients spoof their address.

## Telegram setup

1. Create a bot with Telegram's `@BotFather` and set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_USERNAME` in `.env`.
2. Generate a separate random `TELEGRAM_WEBHOOK_SECRET`.
3. Deploy the application at the HTTPS origin in `APP_BASE_URL`.
4. Register the webhook with Telegram's official Bot API:

```text
POST https://api.telegram.org/bot<BOT_TOKEN>/setWebhook
url=https://your-domain.example/integrations/telegram/webhook
secret_token=<TELEGRAM_WEBHOOK_SECRET>
allowed_updates=["message","callback_query"]
```

The bot deliberately rejects unlinked users. A valid connection requires all of these conditions:

1. The student registered an email and mobile number on Job Matcher.
2. The email is verified and the student is signed in.
3. The student creates a short-lived Telegram link from the website.
4. Telegram confirms ownership by sharing that Telegram user's own contact.
5. The normalized contact number exactly matches the registered number and is not bound elsewhere.

After connection, `/jobs`, `/pause`, `/resume`, `/settings`, and job-card callbacks are available. An expired seven-day period must be renewed on the authenticated website; bot commands cannot silently extend it.

## Google sign-in setup

Google sign-in is optional; email/password registration remains available. Create an OAuth 2.0 Client in Google Auth Platform using the **Web application** type, configure the consent screen, and add exact authorised redirect URIs:

```text
http://127.0.0.1:5000/auth/google/callback
https://your-production-domain.example/auth/google/callback
```

Add the generated values locally and in Vercel:

```dotenv
GOOGLE_CLIENT_ID=<your-web-client-id>.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=<your-web-client-secret>
```

Restart the application after changing these values. New Google users are asked for their mobile number and must accept the Terms and Privacy Policy before the account is created. Existing accounts with the same Google-verified email are linked once to Google's stable subject identifier. Job Matcher does not store Google passwords, access tokens, or refresh tokens.

## Job sources and API costs

The application works without paid job API credits by using clearly labelled demo data and any explicitly configured public employer boards. Greenhouse, Lever, and Ashby expose public company-scoped job-board endpoints; they are not global search engines, so add selected employer board identifiers in `.env`. Himalayas can be enabled as an authorized public feed. Adzuna requires its own application ID/key and is optional.

```dotenv
GREENHOUSE_BOARDS=company_board_token,another_board
LEVER_SITES=companysite
ASHBY_BOARDS=company-board-name
ENABLE_HIMALAYAS=true
ADZUNA_APP_ID=
ADZUNA_APP_KEY=
```

No LinkedIn or Indeed scraping is used. The collector runs server-side, preserves provider IDs and functional application parameters, revalidates current listings, and marks jobs inactive only after a successful complete poll. Review every provider's and employer's terms before commercial redistribution.

Run a source refresh manually:

```powershell
uv run flask --app run.py sync-jobs
```

Use one scheduler in production (cron, a platform scheduler, or a worker timer) for these commands:

```powershell
uv run flask --app run.py sync-jobs
uv run flask --app run.py dispatch-alerts
uv run flask --app run.py cleanup-private-files
```

`dispatch-alerts` is timezone-aware. It sends only after the user's preferred local time, never exceeds that student's daily limit, and reserves the `(student, job)` ledger row before delivery. Already delivered jobs remain excluded permanently, including after renewal.

`cleanup-private-files` retries the durable private-file erasure queue. Account/resume database deletion and cleanup intent commit together; a temporary filesystem failure therefore remains visible and retryable instead of silently orphaning a resume.

## Resume handling

Files are signature-checked, size-limited, and parsed without executing embedded content. In local mode, files are stored outside the public static directory with generated names and durable deletion tracking. In production/Vercel mode, `RESUME_STORAGE_MODE=discard` parses each upload in memory and immediately discards the original; only non-content metadata and structured details shown to the student for review are retained. Text-based PDF/DOCX/ODT/RTF/TXT/Markdown/HTML files can be extracted. Legacy `.doc`, scanned PDFs, and images are accepted safely but may require conversion, OCR integration, or manual confirmation. The product never invents missing resume details.

If retained originals become a future requirement, move them to encrypted private object storage, add malware scanning and isolated document conversion, define retention rules, and record user-visible deletion completion before changing the storage mode.

## Deploy with GitHub and Vercel

1. Create a Git repository from this folder, commit the project, and push it to a private GitHub repository. `.env`, local databases, uploaded files, caches, and Vercel metadata are excluded by `.gitignore`.
2. Provision a managed PostgreSQL database and a Redis-compatible service. Enable automated database backups before launch.
3. Import the GitHub repository at Vercel and leave the framework/build/output settings on automatic detection. `pyproject.toml` points Vercel to `run:app`, pins Python 3.13, and `uv.lock` supplies reproducible dependencies.
4. Add the following production environment variables in Vercel. Never copy `.env` into GitHub.

```dotenv
APP_ENV=production
FLASK_DEBUG=0
SECRET_KEY=<unique-random-value-at-least-32-characters>
COOKIE_SECURE=true
APP_BASE_URL=https://your-production-domain.example
DATABASE_URL=postgresql://<managed-postgres-connection>
RATELIMIT_STORAGE_URI=rediss://<managed-redis-connection>
TRUSTED_PROXY_HOPS=1
RESUME_STORAGE_MODE=discard
MAX_CONTENT_LENGTH_MB=4
CRON_SECRET=<unique-random-value-at-least-16-characters>
SEED_DEMO_JOBS=false
TELEGRAM_WEBHOOK_SECRET=<different-random-value-at-least-16-characters>
```

Also add the configured SMTP, Telegram bot, job-source, and administrator variables from `.env.example`. Vercel supplies `VERCEL=1`; the application then enforces serverless-safe resume settings.

5. Apply the checked-in database migration from a trusted machine before sending users to the deployment. In PowerShell, temporarily set the production `DATABASE_URL`, keep `APP_ENV=development` for this maintenance command, and run:

```powershell
$env:DATABASE_URL = "postgresql://<managed-postgres-connection>"
$env:APP_ENV = "development"
uv run flask --app run.py db upgrade
Remove-Item Env:DATABASE_URL
Remove-Item Env:APP_ENV
```

6. Deploy, open `/health`, and expect `{"database":"ok","status":"ok"}`. Then register the Telegram webhook using the production domain as described above.
7. In the GitHub repository, create Actions secrets named `APP_BASE_URL` and `CRON_SECRET`. They must exactly match the Vercel production values. The checked-in workflow invokes the protected alert dispatcher every five minutes; GitHub schedules are best-effort and public repositories disable inactive schedules after 60 days. For strict delivery timing, use a dedicated scheduler or Vercel Pro cron and disable the GitHub dispatcher so only one scheduler runs.

`vercel.json` schedules job-source refresh and private-file cleanup once per day, which is compatible with Vercel Hobby. Vercel automatically sends `CRON_SECRET` as a Bearer token. The alert dispatcher is intentionally not a Vercel Hobby cron because that plan only permits one invocation per day.

For rollback, use Vercel's previous production deployment and keep schema migrations backward-compatible. Vercel rollback does not roll back the database or automatically change registered cron definitions, so verify both after a rollback.

## Administration

Register and verify an account whose email is listed in `ADMIN_EMAILS`, or grant access from the CLI:

```powershell
uv run flask --app run.py create-admin --email admin@example.com
```

Administrators can sync sources, pause/reactivate questionable listings, resolve reports, and inspect high-level audit events. Admin actions do not bypass the permanent student send ledger.

## Tests and quality checks

```powershell
uv run pytest
uv run ruff check app tests
py -3.13 -m compileall -q app tests run.py
```

## Production checklist

- Use PostgreSQL in production and apply the checked-in Alembic migrations during every deployment.
- Use Redis or another shared backend for rate limiting instead of `memory://`; production startup enforces this.
- Configure SMTP, TLS, HTTPS, secure cookies, managed backups, uptime checks on `/health`, and error monitoring.
- Keep `RESUME_STORAGE_MODE=discard` on Vercel unless encrypted private object storage is implemented.
- Run only one alert scheduler; the send ledger and workflow concurrency reduce duplicate-delivery risk.
- Keep Telegram and job-source secrets outside version control and rotate them after exposure.
- Add provider-specific rate limits, retry queues, observability, and terms/licensing review.
- Complete an accessibility, privacy, threat-model, and legal review for the intended launch region.

## Project layout

```text
app/
  auth/            registration, OTP, sign-in, recovery
  onboarding/      resume, profile review, preferences, Telegram linking
  jobs/            matches, job details, tracker actions, reporting
  integrations/    Telegram webhook
  admin/           review and source operations
  services/        parsing, matching, feeds, delivery, email, OTP
  static/          professional visual system and generated hero artwork
  templates/       responsive Jinja pages
data/seed_jobs.json
tests/
```

Job Matcher assists decisions; it does not guarantee selection, impersonate employers, or submit applications on a student's behalf.
