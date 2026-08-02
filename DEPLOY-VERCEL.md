# Deploying to Vercel

This app is configured for Vercel. Follow these steps in order — **step 2 is not optional**, the app will refuse to start without it.

---

## 1. Create a Postgres database

Vercel's filesystem is wiped between requests, so the app **cannot** store data in a local file. You need a Postgres database. All of these have a free tier:

- **Vercel Postgres** — Storage tab in your Vercel dashboard (simplest)
- **Neon** — <https://neon.tech>
- **Supabase** — <https://supabase.com>

Copy the connection string. It looks like:

```
postgresql://user:password@host.region.aws.neon.tech/dbname?sslmode=require
```

## 2. Set environment variables

In Vercel: **Project → Settings → Environment Variables**. Add these for the *Production* environment.

| Name | Value | Notes |
|---|---|---|
| `DATABASE_URL` | your Postgres connection string | **Required.** App won't boot without it. |
| `SECRET_KEY` | a long random string | **Required.** See below. |
| `ADMIN_PASSWORD` | a strong password | Your first login. |
| `FLASK_ENV` | `production` | |

Generate a `SECRET_KEY`:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Optional, only if you use the Telegram bot:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token from @BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | another random string |

## 3. Deploy

```bash
npm i -g vercel
vercel --prod
```

Or connect the GitHub repo in the Vercel dashboard and it deploys on every push.

## 4. First login

Go to your Vercel URL and log in with `admin` and the `ADMIN_PASSWORD` you set. You will be required to change the password immediately.

## 5. Check everything is healthy

Visit `/health` — you should see:

```json
{"status": "ok", "db": "postgres"}
```

If it says `"db": "sqlite"`, your `DATABASE_URL` is not being read and **data will not be saved**.

Logged in as super admin, visit `/health/detail` for the full picture:

```json
{
  "status": "ok",
  "db": "postgres",
  "persistent": true,
  "serverless": true,
  "upload_storage": "database",
  "rate_limit_storage": "database"
}
```

`persistent: true` is the one that matters.

---

## How serverless mode differs

Vercel sets the `VERCEL` environment variable automatically. The app detects it and changes three behaviours, because the normal approach would silently lose data:

| | Normal server | On Vercel |
|---|---|---|
| Database | local SQLite file | Postgres (required) |
| Photos / ID proofs | saved to disk | stored in the database |
| Rate limiting | in process memory | stored in the database |

Uploads move into the database because files written to disk vanish between requests. Rate limiting moves too, because each request can hit a different instance with its own empty memory — an attacker could otherwise bypass the login limit entirely by just retrying.

Override with `UPLOAD_STORAGE=db|disk` and `RATE_LIMIT_STORAGE=db|memory` if you ever need to.

---

## Limits to be aware of

- **Uploads are capped at ~4 MB** by Vercel's request body limit, and stored as base64 in Postgres (~33% overhead). Fine for photos and ID scans. For large volumes of files, move to S3 or Cloudflare R2.
- **Free Postgres tiers sleep** when idle, so the first request after a quiet period may take a few seconds.
- **Serverless cold starts** add a second or two to the first request.
- **Back up your database.** Managed Postgres usually has automated backups — check yours is enabled. Vercel does not back up your data for you.

## Troubleshooting

**"DATABASE_URL is required when deploying to Vercel/Lambda in production"**
You skipped step 2, or set the variable for Preview but not Production. Add it and redeploy.

**"SECRET_KEY environment variable is required in production"**
Same — set `SECRET_KEY` and redeploy.

**"DATABASE_URL is set but psycopg2 is not installed"**
`psycopg2-binary` is missing from `requirements.txt`. It is included by default; restore it if removed.

**Everyone logged out after a deploy**
Expected if `SECRET_KEY` changed. Set it once and leave it.

**Photos don't appear**
Confirm `/health/detail` shows `upload_storage: "database"`. Photos uploaded before switching to Postgres are gone — the old filesystem no longer exists.
