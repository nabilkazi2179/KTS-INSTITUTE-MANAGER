# KTS Institute Manager

Student lifecycle management for **Konkan Technology Services** — admissions, batches, fees, attendance, exams, certificates, reporting, and an optional Telegram bot.

Built with Flask. Runs on SQLite for local development and PostgreSQL in production.

---

## ⚠️ Upgrading from an earlier version — read this first

This release closes several critical security holes. Three things **will** change for you:

1. **`SECRET_KEY` is now mandatory in production.** The app refuses to start without it. All existing sessions are invalidated on upgrade (everyone re-logs in — expected).
2. **The default `admin` / `admin123` login is gone.** Set `ADMIN_PASSWORD` before first boot, or read the one-time generated password from the startup logs.
3. **New accounts get random one-time passwords** (shown once to the admin who creates them) instead of the shared `kts123` / `trainer123`. Everyone must change their password on first login.

---

## Quick start (local development)

```bash
git clone https://github.com/KONKANTECH/KTS-INSTITUTE-MANAGER.git
cd KTS-INSTITUTE-MANAGER

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements-dev.txt

cp .env.example .env             # then edit it
python -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))" >> .env
echo "FLASK_ENV=development" >> .env

python app.py
```

Open <http://127.0.0.1:5000>. In development the bootstrap admin is `admin` / `admin123` — fine locally, impossible in production.

## Running the tests

```bash
pytest              # 52 tests
```

---

## Production deployment

### Docker Compose (recommended — includes PostgreSQL)

```bash
export SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
export POSTGRES_PASSWORD='a-strong-database-password'
export ADMIN_PASSWORD='a-strong-admin-password'

docker compose up -d --build
```

The app is on port 8000 with a built-in health check. Put a TLS-terminating reverse proxy (nginx, Caddy, Traefik) in front of it.

### Plain gunicorn

```bash
export FLASK_ENV=production
export SECRET_KEY='...'
export DATABASE_URL='postgresql://user:pass@host:5432/kts'
export ADMIN_PASSWORD='...'

gunicorn --bind 0.0.0.0:8000 --workers 4 --timeout 60 app:app
```

### Vercel

`vercel.json` and `api/index.py` are included, but read the storage warning below.

---

## 🔴 Storage warning — serverless hosts only

**This section applies only to Vercel / AWS Lambda.** If you run on your own PC, an office server, or Docker, skip it — see [Running on one computer](#running-on-one-computer-no-cloud-database) below.

On Vercel and Lambda the filesystem is **ephemeral**. There the app cannot keep a local file, so:

- every student, payment, and certificate **silently disappears** between invocations;
- uploaded photos and ID proofs **do not persist**.

If you deploy serverless, **set `DATABASE_URL` to a managed PostgreSQL instance** (Neon, Supabase, RDS) and use object storage (S3, Cloudflare R2) for uploads. `GET /health/detail` reports `persistent: false` when you are in this danger zone.

---

## Running on one computer (no cloud database)

**You do not need Neon, Supabase, or any cloud service.** If the app runs on a single machine — your PC or an office server — the built-in SQLite database is a legitimate production choice for a single institute.

Leave `DATABASE_URL` blank. The app creates `kts_institute.db` next to `app.py` and it **persists across restarts and reboots**.

### Easiest way (Windows, no terminal)

Double-click **`KTS-Start.bat`**. It launches the app, opens your browser, and stores data in `kts_institute.db` in the same folder. Keep the black window open while using the app; close it when finished.

Double-click **`KTS-Backup-Data.bat`** to snapshot the database into `backups/`.

### Is SQLite really OK in production?

For one machine, yes. This app enables:

- **WAL mode** — readers don't block writers, so staff can browse while someone records a payment;
- **15-second busy timeout** — concurrent writes wait instead of failing with *"database is locked"*;
- **foreign keys** — enforced, not silently ignored.

Verified: 8 threads writing simultaneously completed 120/120 writes with zero lock errors.

**Choose SQLite when:** one computer or one server, up to roughly 20–30 concurrent users, one institute branch.

**Move to PostgreSQL when:** you need multiple app servers, several branches sharing live data, or you deploy serverless.

Switching later is just setting `DATABASE_URL` — no code changes.

### Back up your data

With a local database, **that one file is your entire institute record.** A dead disk means everything is gone.

```bash
python backup_db.py                      # -> backups/kts_YYYYMMDD_HHMMSS.db
python backup_db.py --dir D:/backups     # custom location
python backup_db.py --keep 30            # retain the newest 30
```

It uses SQLite's online backup API, so it is safe to run while the app is serving traffic, and it verifies the copy afterwards.

To automate on Windows: Task Scheduler → daily → run `KTS-Backup-Data.bat`. **Copy the `backups` folder to a USB drive or cloud storage regularly** — a backup on the same disk does not protect against disk failure.

### Where is my data?

| | |
|---|---|
| Database | `kts_institute.db` next to `app.py` (override with `SQLITE_PATH`) |
| Uploads | `static/uploads/photos/` and `static/uploads/id_proofs/` |
| Backups | `backups/` |

Confirm at any time: log in as super admin and open `/health/detail` — `persistent: true` means your data is safe on disk.

---

## Configuration

Every setting is an environment variable. See [`.env.example`](.env.example) for the full annotated list.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SECRET_KEY` | **yes (prod)** | — | Signs session cookies. App won't boot without it. |
| `FLASK_ENV` | no | `production` | `development` relaxes prod-only guards. |
| `DATABASE_URL` | no | — | Postgres DSN. **Leave blank for a local SQLite file** (persists on a normal machine). |
| `SQLITE_PATH` | no | `./kts_institute.db` | Where to keep the SQLite file. |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | first boot | `admin` / random | Bootstrap super admin. |
| `TELEGRAM_BOT_TOKEN` | no | — | Enables the bot. |
| `TELEGRAM_WEBHOOK_SECRET` | if bot used | — | Proves webhook calls come from Telegram. |
| `LOGIN_RATE_LIMIT` | no | `10` | Login attempts per IP per 5 min. |
| `MAX_FAILED_LOGINS` | no | `8` | Failures before account lockout. |
| `LOCKOUT_MINUTES` | no | `15` | Lockout duration. |
| `SESSION_HOURS` | no | `12` | Session lifetime. |
| `MAX_UPLOAD_MB` | no | `16` | Upload size cap. |
| `PAGE_SIZE` | no | `50` | Rows per page. |

---

## Security model

- **Sessions** — signed cookies, `HttpOnly`, `SameSite=Lax`, `Secure` in production, cleared on login to prevent session fixation.
- **CSRF** — every state-changing form carries a per-session token, verified with a constant-time compare. The Telegram endpoints are exempt and authenticated by shared secret instead.
- **Rate limiting** — sliding window on login and Telegram linking. *In-process*: with multiple gunicorn workers each has its own counter. For strict enforcement put a limiter at your reverse proxy or move the buckets to Redis.
- **Passwords** — PBKDF2 via Werkzeug, 8-character minimum, forced change on first login.
- **Authorization** — role decorators on every route; students are blocked from staff pages; only a super admin can create another super admin; the last active super admin cannot be deactivated.
- **Errors** — users see a generic page with a reference ID; the full traceback goes only to the server log.
- **Uploads** — extension allow-list applied *after* `secure_filename`, path-traversal guard, size cap.
- **Headers** — `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, plus HSTS in production.

### Telegram webhook setup

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://YOUR-DOMAIN/telegram/webhook&secret_token=<TELEGRAM_WEBHOOK_SECRET>"
```

Without `TELEGRAM_WEBHOOK_SECRET` the endpoint accepts unauthenticated POSTs and logs a warning on every call.

---

## Roles

| Role | Access |
|---|---|
| `super_admin` | Everything, including staff management and diagnostics |
| `admin` | Everything except creating super admins |
| `accountant` | Fees, payments, receipts, reports, read-only students |
| `counselor` | Students, admissions, attendance, reports |
| `trainer` | Attendance, exams, results, read-only students |
| `student` | Own portal only |

## Health endpoints

| Endpoint | Auth | Returns |
|---|---|---|
| `GET /health` | public | `{"status":"ok","db":"postgres"}` — safe for load balancers |
| `GET /health/detail` | super admin | Write test, row counts, DB host, ephemeral-storage warning |

---

## Project layout

```
app.py                  Application (routes, DB layer, auth, Telegram)
api/index.py            Vercel WSGI entry point
templates/              Jinja2 templates
tests/test_app.py       Test suite
Dockerfile              Production image (non-root, health check)
docker-compose.yml      App + PostgreSQL
.github/workflows/ci.yml  Tests on 3.11/3.12, bandit, pip-audit
.env.example            Annotated configuration reference
```

## Known limitations

- Rate limiting is per-process — use Redis or proxy-level limiting for multi-worker strictness.
- Schema changes are applied idempotently at startup rather than through versioned migrations. Consider Alembic as the schema grows.
- `app.py` is a single module. It is now safe and tested, but splitting into blueprints is the natural next refactor.
- Uploads go to the local filesystem — use object storage on serverless or multi-node deployments.

## License

Proprietary — Konkan Technology Services.
