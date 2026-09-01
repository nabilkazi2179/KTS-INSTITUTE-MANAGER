# Deploying KonkanTech Manager on a VPS

Flask app served by **gunicorn**, behind **Nginx**, using **PostgreSQL**.
Domain: `konkantechnologies.in`.

> ⚠️ Always use PostgreSQL in production. Without `DATABASE_URL` the app falls back
> to SQLite and warns you — data would not be safe across restarts.

---

## 1. Server prep (Ubuntu/Debian)

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip postgresql nginx git
```

## 2. Get the code

```bash
sudo mkdir -p /var/www/konkantech
sudo chown $USER:$USER /var/www/konkantech
git clone https://github.com/nabilkazi2179/KTS-INSTITUTE-MANAGER.git /var/www/konkantech
cd /var/www/konkantech
```

## 3. Python environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 4. PostgreSQL database

```bash
sudo -u postgres psql <<'SQL'
CREATE DATABASE kts_db;
CREATE USER kts_user WITH PASSWORD 'CHANGE_ME_STRONG';
GRANT ALL PRIVILEGES ON DATABASE kts_db TO kts_user;
ALTER DATABASE kts_db OWNER TO kts_user;
SQL
```

## 5. Configure environment

```bash
cp .env.example .env
nano .env      # fill in real values
```

Generate a strong `SECRET_KEY`:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Set at minimum: `SECRET_KEY`, `DATABASE_URL`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`.

Example `DATABASE_URL`:
```
DATABASE_URL=postgresql://kts_user:CHANGE_ME_STRONG@localhost:5432/kts_db
```

## 6. First run / smoke test (verify it works)

```bash
source venv/bin/activate
gunicorn --workers 3 --bind 127.0.0.1:8000 wsgi:app
```

In another terminal, confirm it's healthy (should show `"db":"postgres"` and `"write_test":true`):

```bash
curl -s http://127.0.0.1:8000/health
```

Stop it with Ctrl+C once verified, then run the automated test suite anytime:

```bash
python tests/test_app.py     # expect: ALL TESTS PASSED
```

## 7. Run as a service (gunicorn + systemd)

```bash
sudo cp deploy/konkantech.service /etc/systemd/system/
# edit the file if your path/user differ (default: /var/www/konkantech, www-data)
sudo systemctl daemon-reload
sudo systemctl enable --now konkantech
sudo systemctl status konkantech
```

## 8. Nginx reverse proxy + HTTPS

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/konkantech
sudo ln -s /etc/nginx/sites-available/konkantech /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# HTTPS (Let's Encrypt)
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d konkantechnologies.in -d www.konkantechnologies.in
```

Point your domain's DNS **A record** to the VPS IP first, then run certbot.

## 9. Log in

Open `https://konkantechnologies.in/login` and sign in with the
`ADMIN_USERNAME` / `ADMIN_PASSWORD` you set in `.env`.
**Change the admin password after first login.**

---

## Access model (admin vs user)

- **Admin** — full access to everything, including **Users** (create accounts,
  reset passwords, enable/disable) and **Company** settings.
- **User** — sees only the modules an admin grants. Manage this at
  **Admin → Users → Permissions** (tick the modules each user may access).

Grantable modules: Students, Courses, Batches, Trainers, Fees, Expenses,
Finance, Services, Attendance, Exams, Certificates, Reports.

## Updating later

```bash
cd /var/www/konkantech
git pull
source venv/bin/activate && pip install -r requirements.txt
sudo systemctl restart konkantech
```
