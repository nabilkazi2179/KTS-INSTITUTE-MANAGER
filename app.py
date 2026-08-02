"""
KTS Institute Management System - Konkan Technology Services
Complete Student Lifecycle Management Application
"""
import os, uuid, json, io, csv, urllib.request, urllib.parse, sqlite3, re
import hmac, secrets, logging, threading, time, base64, mimetypes
from datetime import datetime, date, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
    flash, session, jsonify, send_file, abort, make_response,
    send_from_directory, g as flask_g)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# ── Environment ──────────────────────────────────────────────
def _load_dotenv():
    """Load .env from the project root into os.environ (does not override real env)."""
    path = os.path.join(BASE_DIR, '.env')
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception as e:  # pragma: no cover
        print(f'dotenv load failed: {e}')

_load_dotenv()

ENV = os.environ.get('FLASK_ENV', 'production').lower()
IS_PROD = ENV == 'production'
TESTING = os.environ.get('TESTING', '').lower() in ('1', 'true', 'yes')

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO if IS_PROD else logging.DEBUG,
    format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
)
logger = logging.getLogger('kts')

# ── Telegram Config ──────────────────────────────────────────
def _load_telegram_token():
    """Token comes from the environment, or the DB settings table as a fallback."""
    tok = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    if tok:
        return tok
    try:
        return get_setting('TELEGRAM_BOT_TOKEN', '') or ''
    except Exception:
        return ''

def telegram_api():
    tok = _load_telegram_token()
    return f'https://api.telegram.org/bot{tok}' if tok else ''

# Secret shared with Telegram via setWebhook(secret_token=...) so only
# Telegram can post to our webhook endpoint.
TELEGRAM_WEBHOOK_SECRET = os.environ.get('TELEGRAM_WEBHOOK_SECRET', '').strip()

# ── Database Config ──────────────────────────────────────────
DATABASE_URL = (os.environ.get('DATABASE_URL', '')
                or os.environ.get('POSTGRES_URL_NON_POOLING', '')
                or os.environ.get('POSTGRES_URL', '')
                or os.environ.get('POSTGRES_PRISMA_URL', ''))
DB_MODE = 'sqlite'
DB_PATH = ':memory:'
_last_rowcount = 0

# Serverless platforms give you a read-only filesystem except for /tmp,
# which is wiped between invocations. Everywhere else (your own PC, a VPS,
# Docker) we want a real file that survives restarts.
IS_SERVERLESS = bool(os.environ.get('VERCEL') or os.environ.get('AWS_LAMBDA_FUNCTION_NAME'))

def _pick_sqlite_path():
    # 1. Explicit override always wins.
    explicit = os.environ.get('SQLITE_PATH', '').strip()
    if explicit:
        parent = os.path.dirname(os.path.abspath(explicit)) or '.'
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            pass
        return os.path.abspath(explicit)
    # 2. On a normal machine, keep the DB next to the app so it persists.
    if not IS_SERVERLESS and os.path.isdir(BASE_DIR) and os.access(BASE_DIR, os.W_OK):
        return os.path.join(BASE_DIR, 'kts_institute.db')
    # 3. Serverless / read-only project dir: /tmp is all we get (EPHEMERAL).
    if os.path.isdir('/tmp') and os.access('/tmp', os.W_OK):
        return '/tmp/kts_institute.db'
    return ':memory:'

if DATABASE_URL and DATABASE_URL not in ('sqlite', ''):
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        DB_MODE = 'postgres'
    except ImportError:
        if IS_SERVERLESS or ENV == 'production':
            raise RuntimeError(
                'DATABASE_URL is set but psycopg2 is not installed, so the app '
                'would silently fall back to a temporary SQLite database and lose '
                'all data. Add "psycopg2-binary" to requirements.txt and redeploy.')
        DB_PATH = _pick_sqlite_path()
        logger.warning('psycopg2 not installed - falling back to SQLite at %s', DB_PATH)
else:
    DB_PATH = _pick_sqlite_path()

if DB_MODE == 'sqlite':
    if IS_SERVERLESS and ENV == 'production':
        raise RuntimeError(
            'DATABASE_URL is required when deploying to Vercel/Lambda in production. '
            'The serverless filesystem is wiped between requests, so SQLite would '
            'silently lose every student, payment and certificate. '
            'Create a free Postgres database (Neon, Supabase or Vercel Postgres) and '
            'set DATABASE_URL in your Vercel project settings.')
    if DB_PATH == ':memory:':
        logger.warning('SQLite is in-memory - ALL DATA IS LOST when the process stops.')
    elif DB_PATH.startswith('/tmp'):
        logger.warning('SQLite lives in /tmp (%s) - data will NOT survive. '
                       'Set DATABASE_URL or SQLITE_PATH for persistence.', DB_PATH)
    else:
        logger.info('Using SQLite database at %s', DB_PATH)

def get_db():
    if DB_MODE == 'postgres':
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        conn.autocommit = True
        return conn
    else:
        # timeout: wait rather than instantly raising "database is locked"
        # when another request holds the write lock.
        db = sqlite3.connect(DB_PATH, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        if DB_PATH != ':memory:':
            # WAL lets readers work while a writer is active - essential for
            # a multi-user Flask app on SQLite.
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=NORMAL')
            db.execute('PRAGMA busy_timeout=15000')
        return db

def q(query, args=None, one=False):
    """Run a SELECT. Returns a list of dicts, or a single dict / None when one=True."""
    if DB_MODE == 'postgres':
        query = query.replace('?', '%s')
    if args is None:
        args = ()
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(query, args)
        rows = cur.fetchall() or []
        rows = [dict(r) if hasattr(r, 'keys') else r for r in rows]
        if one:
            return rows[0] if rows else None
        return rows
    finally:
        db.close()

def ex(query, args=()):
    global _last_rowcount
    if DB_MODE == 'postgres':
        query = query.replace('?', '%s')
    db = get_db()
    try:
        cur = db.cursor()
        # PostgreSQL: cur.lastrowid is always 0, so use RETURNING id for INSERTs
        if DB_MODE == 'postgres' and query.strip().upper().startswith('INSERT'):
            cur.execute(query + ' RETURNING id', args)
            _last_rowcount = cur.rowcount
            row = cur.fetchone()
            db.commit()
            return row['id'] if row else None
        cur.execute(query, args)
        _last_rowcount = cur.rowcount
        db.commit()
        return cur.lastrowid
    finally:
        db.close()

def get_setting(key, default=''):
    try:
        r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
        return r['value'] if r else default
    except Exception:
        return default

def set_setting(key, value):
    try:
        ex("INSERT INTO settings (key,value,updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=?,updated_at=?",
           (key, value, datetime.now().isoformat(), value, datetime.now().isoformat()))
        return True
    except Exception as e:
        print(f"set_setting error: {e}")
        return False

UPLOAD = os.environ.get('UPLOAD_FOLDER') or os.path.join(BASE_DIR, 'static', 'uploads')

MAX_UPLOAD_BYTES = int(os.environ.get('MAX_UPLOAD_MB', '16')) * 1024 * 1024

# On serverless the disk is wiped between requests, so uploaded photos and
# ID proofs must live in the database instead. Can be forced either way with
# UPLOAD_STORAGE=db|disk.
_upload_storage = os.environ.get('UPLOAD_STORAGE', '').strip().lower()
if _upload_storage == 'db':
    USE_DB_UPLOADS = True
elif _upload_storage == 'disk':
    USE_DB_UPLOADS = False
else:
    USE_DB_UPLOADS = IS_SERVERLESS

# Same reasoning for rate limiting: serverless instances don't share memory.
_rl_storage = os.environ.get('RATE_LIMIT_STORAGE', '').strip().lower()
if _rl_storage == 'db':
    USE_DB_RATE_LIMIT = True
elif _rl_storage == 'memory':
    USE_DB_RATE_LIMIT = False
else:
    USE_DB_RATE_LIMIT = IS_SERVERLESS
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'))

# ── Secret key ───────────────────────────────────────────────
_secret = os.environ.get('SECRET_KEY', '').strip()
if not _secret:
    if IS_PROD and not TESTING:
        raise RuntimeError(
            'SECRET_KEY environment variable is required in production. '
            'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
        )
    _secret = secrets.token_hex(32)
    logger.warning('No SECRET_KEY set - using an ephemeral key (sessions reset on restart).')
app.secret_key = _secret

app.config.update(
    UPLOAD_FOLDER=UPLOAD,
    MAX_CONTENT_LENGTH=int(os.environ.get('MAX_UPLOAD_MB', '16')) * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=IS_PROD,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=int(os.environ.get('SESSION_HOURS', '12'))),
    TESTING=TESTING,
)

# Create upload directories up front - photo/id_proof saves used to crash
# because these paths never existed. Skipped when files live in the DB.
if not USE_DB_UPLOADS:
    for _sub in ('photos', 'id_proofs'):
        try:
            os.makedirs(os.path.join(UPLOAD, _sub), exist_ok=True)
        except OSError as e:
            logger.warning('Could not create upload dir %s: %s', _sub, e)

ALLOWED = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx'}
ALLOWED_IMAGE = {'png', 'jpg', 'jpeg', 'gif'}
_db_initialized = False
_db_lock = threading.Lock()

# ── CSRF protection ──────────────────────────────────────────
CSRF_EXEMPT = {'telegram_webhook', 'telegram_link_api'}

def csrf_token():
    """Per-session CSRF token, generated lazily."""
    tok = session.get('_csrf_token')
    if not tok:
        tok = secrets.token_urlsafe(32)
        session['_csrf_token'] = tok
    return tok

app.jinja_env.globals['csrf_token'] = csrf_token

@app.before_request
def csrf_protect():
    if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return None
    if app.config.get('TESTING') and os.environ.get('DISABLE_CSRF'):
        return None
    if request.endpoint in CSRF_EXEMPT:
        return None
    sent = (request.form.get('_csrf_token')
            or request.headers.get('X-CSRF-Token', ''))
    expected = session.get('_csrf_token', '')
    if not expected or not sent or not hmac.compare_digest(str(sent), str(expected)):
        logger.warning('CSRF validation failed for %s from %s',
                       request.path, request.remote_addr)
        abort(400, description='CSRF token missing or invalid. Please reload the page and try again.')
    return None

# ── Rate limiting ────────────────────────────────────────────
# In-process buckets are fine on a single server, but on serverless every
# request may land on a fresh instance with empty memory - which would make
# brute-force protection useless. There we persist attempts in the database.
_rate_buckets = {}
_rate_lock = threading.Lock()

def _db_rate_check(bucket_key, max_attempts, window_seconds):
    """Shared-state rate limit. Returns retry-after seconds, or 0 if allowed."""
    now = time.time()
    cutoff = now - window_seconds
    try:
        ex("DELETE FROM rate_limits WHERE ts < ?", (cutoff,))
        rows = q("SELECT ts FROM rate_limits WHERE bucket=? AND ts >= ? ORDER BY ts",
                 (bucket_key, cutoff))
        if len(rows) >= max_attempts:
            return int(window_seconds - (now - float(rows[0]['ts']))) + 1
        ex("INSERT INTO rate_limits (bucket, ts) VALUES (?,?)", (bucket_key, now))
        return 0
    except Exception as e:
        # Never lock everyone out because the limiter itself broke.
        logger.error('Rate limit check failed for %s: %s', bucket_key, e)
        return 0

def rate_limit(max_attempts, window_seconds, key_func=None):
    """Reject requests once an IP exceeds max_attempts within the window."""
    def decorator(f):
        @wraps(f)
        def wrapper(*a, **k):
            if request.method != 'POST':
                return f(*a, **k)
            ident = key_func() if key_func else (request.remote_addr or 'anon')
            bucket_key = f'{f.__name__}:{ident}'
            now = time.time()

            if USE_DB_RATE_LIMIT:
                retry = _db_rate_check(bucket_key, max_attempts, window_seconds)
                if retry:
                    logger.warning('Rate limit hit on %s by %s', f.__name__, ident)
                    if request.is_json:
                        return jsonify({'ok': False, 'error': 'too many requests',
                                        'retry_after': retry}), 429
                    flash(f'Too many attempts. Please wait {retry} seconds and try again.', 'danger')
                    return redirect(request.path)
                return f(*a, **k)

            with _rate_lock:
                hits = [t for t in _rate_buckets.get(bucket_key, []) if now - t < window_seconds]
                if len(hits) >= max_attempts:
                    retry = int(window_seconds - (now - hits[0])) + 1
                    _rate_buckets[bucket_key] = hits
                    logger.warning('Rate limit hit on %s by %s', f.__name__, ident)
                    if request.is_json:
                        return jsonify({'ok': False, 'error': 'too many requests',
                                        'retry_after': retry}), 429
                    flash(f'Too many attempts. Please wait {retry} seconds and try again.', 'danger')
                    return redirect(request.path)
                hits.append(now)
                _rate_buckets[bucket_key] = hits
                # opportunistic cleanup so the dict cannot grow unbounded
                if len(_rate_buckets) > 5000:
                    for kk in [kk for kk, vv in _rate_buckets.items()
                               if not vv or now - vv[-1] > window_seconds]:
                        _rate_buckets.pop(kk, None)
            return f(*a, **k)
        return wrapper
    return decorator

def clear_rate_limit(func_name, ident=None):
    ident = ident or (request.remote_addr or 'anon')
    with _rate_lock:
        _rate_buckets.pop(f'{func_name}:{ident}', None)

# ── Security headers ─────────────────────────────────────────
@app.after_request
def security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    resp.headers.setdefault('Permissions-Policy', 'geolocation=(), microphone=(), camera=()')
    if IS_PROD:
        resp.headers.setdefault('Strict-Transport-Security',
                                'max-age=31536000; includeSubDomains')
    return resp

def init_db():
    global _db_initialized
    with _db_lock:
        if _db_initialized:
            return
        _db_initialized = True
    db = get_db()
    cur = db.cursor()
    AID = 'SERIAL PRIMARY KEY' if DB_MODE == 'postgres' else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    
    # Create tables if they don't exist (NO drop — preserves data across deploys)
    # Users
    cur.execute(f'''CREATE TABLE IF NOT EXISTS users (
        id {AID}, username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, full_name TEXT NOT NULL,
        email TEXT, phone TEXT, role TEXT NOT NULL DEFAULT 'student',
        is_active INTEGER DEFAULT 1,
        created_at TEXT, updated_at TEXT)''')
    # Courses
    cur.execute(f'''CREATE TABLE IF NOT EXISTS courses (
        id {AID}, course_code TEXT UNIQUE NOT NULL,
        course_name TEXT NOT NULL, duration TEXT, fees REAL DEFAULT 0,
        description TEXT, syllabus TEXT, certificate_template TEXT,
        is_active INTEGER DEFAULT 1, created_at TEXT)''')
    # Batches
    cur.execute(f'''CREATE TABLE IF NOT EXISTS batches (
        id {AID}, batch_name TEXT NOT NULL,
        course_id INTEGER, trainer_id INTEGER, timing TEXT,
        start_date TEXT, end_date TEXT, max_strength INTEGER DEFAULT 30,
        status TEXT DEFAULT 'active', created_at TEXT)''')
    # Students
    cur.execute(f'''CREATE TABLE IF NOT EXISTS students (
        id {AID}, student_id TEXT UNIQUE NOT NULL,
        full_name TEXT NOT NULL, guardian_name TEXT, dob TEXT, gender TEXT,
        mobile TEXT, whatsapp TEXT, email TEXT, address TEXT, city TEXT,
        state TEXT, country TEXT DEFAULT 'India', id_number TEXT,
        qualification TEXT, photo TEXT, id_proof TEXT, joining_date TEXT,
        course_id INTEGER, batch_id INTEGER, counselor_id INTEGER,
        remarks TEXT, user_id INTEGER, status TEXT DEFAULT 'active',
        created_at TEXT, updated_at TEXT)''')
    # Fee Structures
    cur.execute(f'''CREATE TABLE IF NOT EXISTS fee_structures (
        id {AID}, student_id INTEGER,
        registration_fee REAL DEFAULT 0, admission_fee REAL DEFAULT 0,
        course_fee REAL DEFAULT 0, exam_fee REAL DEFAULT 0,
        certificate_fee REAL DEFAULT 0, misc_fee REAL DEFAULT 0,
        total_fee REAL DEFAULT 0, created_at TEXT)''')
    # Fee Payments
    cur.execute(f'''CREATE TABLE IF NOT EXISTS fee_payments (
        id {AID}, student_id INTEGER,
        receipt_no TEXT, amount REAL NOT NULL, payment_method TEXT DEFAULT 'Cash',
        payment_date TEXT, installment_no INTEGER,
        remarks TEXT, collected_by INTEGER, created_at TEXT)''')
    # Attendance
    cur.execute(f'''CREATE TABLE IF NOT EXISTS attendance (
        id {AID}, student_id INTEGER, batch_id INTEGER,
        attendance_date TEXT, status TEXT DEFAULT 'present', remarks TEXT,
        marked_by INTEGER, created_at TEXT,
        UNIQUE(student_id,batch_id,attendance_date))''')
    # Exams
    cur.execute(f'''CREATE TABLE IF NOT EXISTS exams (
        id {AID}, exam_name TEXT NOT NULL,
        course_id INTEGER, batch_id INTEGER, exam_date TEXT,
        max_marks REAL DEFAULT 100, passing_marks REAL DEFAULT 40,
        exam_type TEXT DEFAULT 'theory', created_at TEXT)''')
    # Exam Results
    cur.execute(f'''CREATE TABLE IF NOT EXISTS exam_results (
        id {AID}, exam_id INTEGER, student_id INTEGER,
        theory_marks REAL, practical_marks REAL, total_marks REAL,
        percentage REAL, grade TEXT, status TEXT, remarks TEXT,
        created_at TEXT)''')
    # Certificates
    cur.execute(f'''CREATE TABLE IF NOT EXISTS certificates (
        id {AID}, certificate_no TEXT UNIQUE NOT NULL,
        student_id INTEGER, course_id INTEGER, grade TEXT, percentage REAL,
        completion_date TEXT, issue_date TEXT,
        template_used TEXT, qr_code TEXT, is_revoked INTEGER DEFAULT 0,
        created_at TEXT)''')
    # Trainers
    cur.execute(f'''CREATE TABLE IF NOT EXISTS trainers (
        id {AID}, user_id INTEGER,
        qualification TEXT, experience TEXT, salary REAL DEFAULT 0,
        specialization TEXT, joining_date TEXT, is_active INTEGER DEFAULT 1)''')
    # Audit Logs
    cur.execute(f'''CREATE TABLE IF NOT EXISTS audit_logs (
        id {AID}, user_id INTEGER, action TEXT,
        table_name TEXT, record_id INTEGER, details TEXT,
        created_at TEXT)''')
    # Notifications
    cur.execute(f'''CREATE TABLE IF NOT EXISTS notifications (
        id {AID}, recipient_id INTEGER,
        recipient_type TEXT, type TEXT, subject TEXT, message TEXT,
        channel TEXT DEFAULT 'system', is_sent INTEGER DEFAULT 0,
        sent_at TEXT, created_at TEXT)''')
    # Settings (key/value store, e.g. secrets that can't go in code)
    cur.execute(f'''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)''')
    # Uploaded files, base64-encoded. Used when the filesystem is not durable
    # (Vercel/Lambda), so student photos and ID proofs survive.
    cur.execute(f'''CREATE TABLE IF NOT EXISTS uploads (
        id {AID}, file_key TEXT UNIQUE, filename TEXT, subdir TEXT,
        mime_type TEXT, data TEXT, created_at TEXT)''')
    # Rate-limit attempts, shared across serverless instances.
    cur.execute(f'''CREATE TABLE IF NOT EXISTS rate_limits (
        id {AID}, bucket TEXT, ts DOUBLE PRECISION)''' if DB_MODE == 'postgres'
        else '''CREATE TABLE IF NOT EXISTS rate_limits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bucket TEXT, ts REAL)''')
    # Ensure optional columns exist (safe on re-runs / existing DBs)
    def _add_col(tbl, col, typ):
        try: cur.execute(f'ALTER TABLE {tbl} ADD COLUMN {col} {typ}')
        except Exception: pass
    _add_col('users','telegram_id','BIGINT')
    _add_col('users','tg_link_code','TEXT')
    _add_col('users','tg_link_expires','TEXT')
    _add_col('users','must_change_password','INTEGER DEFAULT 0')
    _add_col('users','failed_logins','INTEGER DEFAULT 0')
    _add_col('users','locked_until','TEXT')

    # Indexes for the columns we filter/join on most (big win once data grows)
    for idx in (
        'CREATE INDEX IF NOT EXISTS idx_students_course ON students(course_id)',
        'CREATE INDEX IF NOT EXISTS idx_ratelimit_bucket ON rate_limits(bucket, ts)',
        'CREATE INDEX IF NOT EXISTS idx_uploads_key ON uploads(file_key)',
        'CREATE INDEX IF NOT EXISTS idx_students_batch ON students(batch_id)',
        'CREATE INDEX IF NOT EXISTS idx_students_status ON students(status)',
        'CREATE INDEX IF NOT EXISTS idx_students_user ON students(user_id)',
        'CREATE INDEX IF NOT EXISTS idx_payments_student ON fee_payments(student_id)',
        'CREATE INDEX IF NOT EXISTS idx_feestruct_student ON fee_structures(student_id)',
        'CREATE INDEX IF NOT EXISTS idx_attendance_student ON attendance(student_id)',
        'CREATE INDEX IF NOT EXISTS idx_attendance_batch_date ON attendance(batch_id,attendance_date)',
        'CREATE INDEX IF NOT EXISTS idx_results_exam ON exam_results(exam_id)',
        'CREATE INDEX IF NOT EXISTS idx_results_student ON exam_results(student_id)',
        'CREATE INDEX IF NOT EXISTS idx_certs_student ON certificates(student_id)',
        'CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id)',
    ):
        try:
            cur.execute(idx)
        except Exception as e:
            logger.debug('index skipped: %s', e)
    
    db.commit()
    # Admin
    cur2 = db.cursor()
    def _run(qry, args=()):
        if DB_MODE == 'postgres':
            qry = qry.replace('?', '%s')
        cur2.execute(qry, args)
    _run("SELECT id FROM users WHERE username=?", ('admin',))
    if not cur2.fetchone():
        admin_user = os.environ.get('ADMIN_USERNAME', 'admin').strip() or 'admin'
        admin_pw = os.environ.get('ADMIN_PASSWORD', '').strip()
        if not admin_pw:
            if IS_PROD and not TESTING:
                # Never silently create a known-password super admin in production.
                admin_pw = secrets.token_urlsafe(18)
                logger.warning(
                    'ADMIN_PASSWORD not set. Generated a one-time admin password: %s\n'
                    'Log in and change it immediately, or set ADMIN_PASSWORD and redeploy.',
                    admin_pw)
            else:
                admin_pw = 'admin123'
                logger.warning('Dev bootstrap admin created with password "admin123".')
        _run("INSERT INTO users (username,password_hash,full_name,email,role,must_change_password) VALUES (?,?,?,?,?,?)",
            (admin_user, generate_password_hash(admin_pw), 'Super Admin',
             os.environ.get('ADMIN_EMAIL', 'admin@kts.com'), 'super_admin', 1))
    # Courses
    dc = [
        ('TP','Tally Prime','3 Months',8000,'Complete Tally Prime with GST accounting'),
        ('DM','Digital Marketing','4 Months',12000,'SEO, SEM, Social Media, Email Marketing'),
        ('GD','Graphic Designing','6 Months',15000,'Photoshop, Illustrator, CorelDraw'),
        ('WD','Web Designing','6 Months',18000,'HTML, CSS, JS, PHP, WordPress'),
        ('AC','Advanced Computer','6 Months',10000,'MS Office, Internet, Hardware Basics'),
        ('EX','Advanced Excel','2 Months',6000,'Advanced formulas, VBA, Macros, Dashboards'),
        ('PY','Python Programming','4 Months',15000,'Python, Django, Data Science basics'),
        ('AU','AutoCAD','3 Months',12000,'2D & 3D drafting, architectural drawings'),
    ]
    for c in dc:
        _run("SELECT id FROM courses WHERE course_code=?", (c[0],))
        if not cur2.fetchone():
            _run("INSERT INTO courses (course_code,course_name,duration,fees,description) VALUES (?,?,?,?,?)", c)
    # Backfill created_at for any existing students missing it (report grouping fix)
    now = datetime.now().isoformat()
    _run("UPDATE students SET created_at=COALESCE(joining_date,?) WHERE created_at IS NULL OR created_at=''", (now,))
    db.commit()
    db.close()

# ── Helpers ──
def allowed_file(fn, allowed=None):
    allowed = allowed or ALLOWED
    return '.' in fn and fn.rsplit('.',1)[1].lower() in allowed

def parse_money(value, default=0.0):
    """Parse a currency form field. Never raises, never returns negative."""
    try:
        v = float(str(value).replace(',', '').strip() or default)
    except (TypeError, ValueError):
        return float(default)
    return max(0.0, v)

def parse_int(value, default=0, minimum=None, maximum=None):
    try:
        v = int(str(value).strip() or default)
    except (TypeError, ValueError):
        v = int(default)
    if minimum is not None:
        v = max(minimum, v)
    if maximum is not None:
        v = min(maximum, v)
    return v

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$')
def valid_email(e):
    return bool(e) and bool(EMAIL_RE.match(e.strip()))

def save_upload(file_storage, subdir, base_name, allowed=None):
    """Save an uploaded file safely. Returns the stored filename, or '' on skip.

    On serverless hosts (Vercel/Lambda) the filesystem is wiped between
    requests, so files are stored in the database instead of on disk.

    The original code built the filename first and only passed it through
    secure_filename() afterwards, so the extension was never really validated.
    """
    if not file_storage or not file_storage.filename:
        return ''
    original = secure_filename(file_storage.filename)
    if not original or not allowed_file(original, allowed):
        return ''
    ext = original.rsplit('.', 1)[1].lower()
    safe_base = secure_filename(base_name) or uuid.uuid4().hex[:12]
    filename = f'{safe_base}.{ext}'

    if USE_DB_UPLOADS:
        try:
            data = file_storage.read()
            if not data:
                return ''
            if len(data) > MAX_UPLOAD_BYTES:
                logger.warning('Upload %s exceeds size cap', filename)
                return ''
            mime = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
            key = f'{subdir}/{filename}'
            payload = base64.b64encode(data).decode('ascii')
            ex("DELETE FROM uploads WHERE file_key=?", (key,))
            ex("INSERT INTO uploads (file_key,filename,subdir,mime_type,data,created_at)"
               " VALUES (?,?,?,?,?,?)",
               (key, filename, subdir, mime, payload, datetime.now().isoformat()))
            return filename
        except Exception as e:
            logger.error('DB upload failed for %s: %s', filename, e)
            return ''

    target_dir = os.path.join(UPLOAD, subdir)
    try:
        os.makedirs(target_dir, exist_ok=True)
        dest = os.path.abspath(os.path.join(target_dir, filename))
        # Defence in depth: never write outside the upload root.
        if not dest.startswith(os.path.abspath(UPLOAD) + os.sep):
            logger.warning('Rejected upload path traversal attempt: %s', dest)
            return ''
        file_storage.save(dest)
        return filename
    except OSError as e:
        logger.error('Upload save failed for %s: %s', filename, e)
        return ''


def gen_student_id(code):
    y = datetime.now().year
    r = q("SELECT student_id FROM students WHERE student_id LIKE ? ORDER BY id DESC LIMIT 1", (f'KTS-{code}-{y}-%',))
    seq = int(r[0]['student_id'].split('-')[-1])+1 if r else 1
    return f'KTS-{code}-{y}-{seq:04d}'

def gen_receipt():
    r = q("SELECT receipt_no FROM fee_payments ORDER BY id DESC LIMIT 1")
    return f'RCP{int(r[0]["receipt_no"].replace("RCP",""))+1:06d}' if r and r[0]['receipt_no'] else 'RCP000001'

def gen_cert_no():
    y = datetime.now().year
    r = q("SELECT certificate_no FROM certificates WHERE certificate_no LIKE ? ORDER BY id DESC LIMIT 1", (f'KTS-CERT-{y}-%',))
    seq = int(r[0]['certificate_no'].split('-')[-1])+1 if r else 1
    return f'KTS-CERT-{y}-{seq:05d}'

def paid_amt(sid): return q("SELECT COALESCE(SUM(amount),0) as t FROM fee_payments WHERE student_id=?",(sid,))[0]['t'] or 0
def fee_total(sid):
    r = q("SELECT total_fee FROM fee_structures WHERE student_id=?",(sid,))
    return r[0]['total_fee'] if r else 0

def log(uid, action, tbl, rid, det=''):
    try: ex("INSERT INTO audit_logs (user_id,action,table_name,record_id,details) VALUES (?,?,?,?,?)",(uid,action,tbl,rid,det))
    except: pass

ROLE_PERMS = {
    'super_admin':['all'],'admin':['all'],
    'accountant':['fees','payments','receipts','reports','students_view'],
    'counselor':['students','admissions','attendance','reports_view'],
    'trainer':['students_view','attendance','exams','results','batches_view'],
    'student':['student_portal']
}
def has_perm(role, perm): return 'all' in ROLE_PERMS.get(role,[]) or perm in ROLE_PERMS.get(role,[])

def login_required(f):
    @wraps(f)
    def d(*a,**k):
        if 'user_id' not in session: flash('Please login first.','warning'); return redirect(url_for('login'))
        return f(*a,**k)
    return d

def role_required(*roles):
    def dec(f):
        @wraps(f)
        def d(*a,**k):
            if session.get('role') not in roles: flash('Access denied.','danger'); return redirect(url_for('dashboard'))
            return f(*a,**k)
        return d
    return dec

STAFF_ROLES = ('super_admin','admin','accountant','counselor','trainer','staff')

def staff_required(f):
    """Block the 'student' role from staff-facing pages."""
    @wraps(f)
    def d(*a, **k):
        if session.get('role') not in STAFF_ROLES:
            flash('Access denied.','danger')
            return redirect(url_for('student_portal') if session.get('role')=='student'
                            else url_for('login'))
        return f(*a, **k)
    return d

@app.route('/uploads/<path:subdir>/<path:filename>')
@login_required
def serve_upload(subdir, filename):
    """Serve an uploaded file from the database or disk.

    Login-gated: student photos and ID proofs are personal data and must not
    be readable by anonymous visitors.
    """
    if subdir not in ('photos', 'id_proofs'):
        abort(404)
    safe_name = secure_filename(filename)
    if not safe_name:
        abort(404)

    if USE_DB_UPLOADS:
        row = q("SELECT data, mime_type FROM uploads WHERE file_key=?",
                (f'{subdir}/{safe_name}',), one=True)
        if not row:
            abort(404)
        try:
            blob = base64.b64decode(row['data'])
        except Exception:
            abort(404)
        resp = make_response(blob)
        resp.headers['Content-Type'] = row['mime_type'] or 'application/octet-stream'
        resp.headers['Cache-Control'] = 'private, max-age=3600'
        resp.headers['Content-Disposition'] = f'inline; filename="{safe_name}"'
        return resp

    directory = os.path.abspath(os.path.join(UPLOAD, subdir))
    if not os.path.isfile(os.path.join(directory, safe_name)):
        abort(404)
    return send_from_directory(directory, safe_name)

@app.context_processor
def g():
    return {'now':datetime.now(),'app_name':'KTS Institute Manager',
            'institute':'Konkan Technology Services',
            'urole':session.get('role',''),'uname':session.get('full_name','')}

MAX_FAILED_LOGINS = int(os.environ.get('MAX_FAILED_LOGINS', '8'))
LOCKOUT_MINUTES = int(os.environ.get('LOCKOUT_MINUTES', '15'))

@app.route('/login',methods=['GET','POST'])
@rate_limit(max_attempts=int(os.environ.get('LOGIN_RATE_LIMIT', '10')), window_seconds=300)
def login():
    if request.method=='POST':
        u=request.form.get('username','').strip(); p=request.form.get('password','')
        if not u or not p:
            flash('Fill all fields.','danger'); return redirect(url_for('login'))
        user=q("SELECT * FROM users WHERE username=? AND is_active=1",(u,),one=True)

        # Account lockout after repeated failures
        if user and user.get('locked_until'):
            try:
                if datetime.fromisoformat(user['locked_until']) > datetime.now():
                    flash('Account temporarily locked due to failed login attempts. '
                          f'Try again in {LOCKOUT_MINUTES} minutes.','danger')
                    return redirect(url_for('login'))
            except (ValueError, TypeError):
                pass

        if user and check_password_hash(user['password_hash'],p):
            # Prevent session fixation: start from a clean session on login.
            session.clear()
            session.permanent = True
            session['user_id']=user['id']; session['username']=user['username']
            session['full_name']=user['full_name']; session['role']=user['role']
            session['email']=user['email']
            ex("UPDATE users SET failed_logins=0, locked_until=NULL WHERE id=?", (user['id'],))
            clear_rate_limit('login')
            log(user['id'],'login','users',user['id'])
            logger.info('Login success: user=%s ip=%s', user['username'], request.remote_addr)
            flash(f'Welcome, {user["full_name"]}!','success')
            if user.get('must_change_password'):
                flash('Please set a new password before continuing.','warning')
                return redirect(url_for('change_password'))
            if user['role']=='student':
                return redirect(url_for('student_portal'))
            return redirect(url_for('dashboard'))

        if user:
            fails = (user.get('failed_logins') or 0) + 1
            if fails >= MAX_FAILED_LOGINS:
                until = (datetime.now() + timedelta(minutes=LOCKOUT_MINUTES)).isoformat()
                ex("UPDATE users SET failed_logins=?, locked_until=? WHERE id=?", (fails, until, user['id']))
                logger.warning('Account locked: user=%s ip=%s', u, request.remote_addr)
            else:
                ex("UPDATE users SET failed_logins=? WHERE id=?", (fails, user['id']))
        logger.warning('Login failed: user=%s ip=%s', u, request.remote_addr)
        # Generic message - never reveal whether the username exists.
        flash('Invalid credentials.','danger')
    return render_template('login.html')

@app.route('/change_password', methods=['GET','POST'])
@login_required
def change_password():
    if request.method == 'POST':
        cur_pw = request.form.get('current_password','')
        new_pw = request.form.get('new_password','')
        confirm = request.form.get('confirm_password','')
        user = q("SELECT * FROM users WHERE id=?", (session['user_id'],), one=True)
        if not user or not check_password_hash(user['password_hash'], cur_pw):
            flash('Current password is incorrect.','danger')
        elif len(new_pw) < 8:
            flash('New password must be at least 8 characters.','danger')
        elif new_pw != confirm:
            flash('New passwords do not match.','danger')
        elif new_pw == cur_pw:
            flash('New password must be different from the current one.','danger')
        else:
            ex("UPDATE users SET password_hash=?, must_change_password=0, updated_at=? WHERE id=?",
               (generate_password_hash(new_pw), datetime.now().isoformat(), session['user_id']))
            log(session['user_id'],'change_password','users',session['user_id'])
            flash('Password updated successfully.','success')
            return redirect(url_for('dashboard'))
        return redirect(url_for('change_password'))
    return render_template('change_password.html')

@app.route('/logout', methods=['GET','POST'])
def logout():
    log(session.get('user_id'),'logout','users',session.get('user_id'))
    session.clear(); flash('Logged out.','info'); return redirect(url_for('login'))

@app.route('/')
@login_required
@staff_required
def dashboard():
    ts=q("SELECT COUNT(*) as c FROM students")[0]['c']
    acs=q("SELECT COUNT(*) as c FROM students WHERE status='active'")[0]['c']
    tc=q("SELECT COUNT(*) as c FROM courses WHERE is_active=1")[0]['c']
    tb=q("SELECT COUNT(*) as c FROM batches WHERE status='active'")[0]['c']
    tt=q("SELECT COUNT(*) as c FROM trainers")[0]['c']
    tcol=q("SELECT COALESCE(SUM(amount),0) as s FROM fee_payments")[0]['s'] or 0
    # Pending fees in ONE aggregate query. Previously this ran a separate
    # paid_amt() query per student inside a Python loop (classic N+1).
    pf_row=q("""SELECT COALESCE(SUM(CASE WHEN fs.total_fee - COALESCE(p.paid,0) > 0
                                          THEN fs.total_fee - COALESCE(p.paid,0) ELSE 0 END),0) as pending
                FROM fee_structures fs
                LEFT JOIN (SELECT student_id, SUM(amount) as paid
                           FROM fee_payments GROUP BY student_id) p
                  ON p.student_id = fs.student_id""", one=True)
    pf=(pf_row or {}).get('pending') or 0
    rs=q("SELECT s.*,c.course_name FROM students s LEFT JOIN courses c ON s.course_id=c.id ORDER BY s.id DESC LIMIT 5")
    if DB_MODE == 'postgres':
        ue=q("SELECT e.*,c.course_name FROM exams e JOIN courses c ON e.course_id=c.id WHERE e.exam_date>=CURRENT_DATE::text ORDER BY e.exam_date LIMIT 5")
    else:
        ue=q("SELECT e.*,c.course_name FROM exams e JOIN courses c ON e.course_id=c.id WHERE e.exam_date>=date('now') ORDER BY e.exam_date LIMIT 5")
    ci=q("SELECT COUNT(*) as c FROM certificates")[0]['c']
    ce=q("SELECT c.course_name,COUNT(s.id) as count FROM courses c LEFT JOIN students s ON c.id=s.course_id GROUP BY c.id ORDER BY count DESC")
    if DB_MODE == 'postgres':
        mf=q("SELECT TO_CHAR(payment_date::date,'YYYY-MM') as month,SUM(amount) as total FROM fee_payments GROUP BY month ORDER BY month DESC LIMIT 6")
    else:
        mf=q("SELECT strftime('%Y-%m',payment_date) as month,SUM(amount) as total FROM fee_payments GROUP BY month ORDER BY month DESC LIMIT 6")
    return render_template('dashboard.html',ts=ts,acs=acs,tc=tc,tb=tb,tt=tt,tcol=tcol,pf=pf,ci=ci,rs=rs,ue=ue,ce=ce,mf=mf)

PAGE_SIZE = int(os.environ.get('PAGE_SIZE', '50'))

@app.route('/students')
@login_required
@staff_required
def students_list():
    s=request.args.get('search','').strip(); cf=request.args.get('course',''); sf=request.args.get('status','')
    page=parse_int(request.args.get('page'),1,minimum=1)
    where=" WHERE 1=1"; p=[]
    if s:
        where+=" AND (s.full_name LIKE ? OR s.student_id LIKE ? OR s.mobile LIKE ?)"; p+=[f'%{s}%']*3
    if cf:
        where+=" AND s.course_id=?"; p.append(cf)
    if sf:
        where+=" AND s.status=?"; p.append(sf)
    total=q(f"SELECT COUNT(*) as c FROM students s{where}", p, one=True)['c']
    pages=max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)
    page=min(page,pages)
    rows=q("SELECT s.*,c.course_name FROM students s LEFT JOIN courses c ON s.course_id=c.id"
           f"{where} ORDER BY s.id DESC LIMIT ? OFFSET ?", p+[PAGE_SIZE,(page-1)*PAGE_SIZE])
    return render_template('students_list.html',students=rows,
        courses=q("SELECT * FROM courses WHERE is_active=1 ORDER BY course_name"),
        page=page,pages=pages,total=total)

@app.route('/students/add',methods=['GET','POST'])
@login_required
@role_required('super_admin','admin','counselor')
def add_student():
    cl=q("SELECT * FROM courses WHERE is_active=1 ORDER BY course_name")
    bl=q("SELECT b.*,c.course_name FROM batches b JOIN courses c ON b.course_id=c.id WHERE b.status='active'")
    co=q("SELECT * FROM users WHERE role IN ('counselor','admin','super_admin') AND is_active=1")
    if request.method=='POST':
        fn=request.form.get('full_name','').strip(); cid=request.form.get('course_id')
        if not fn or not cid:
            flash('Name & Course required.','danger'); return redirect(url_for('add_student'))
        if len(fn) > 120:
            flash('Name is too long.','danger'); return redirect(url_for('add_student'))
        cr=q("SELECT course_code FROM courses WHERE id=?",(cid,),one=True)
        if not cr:
            flash('Selected course does not exist.','danger'); return redirect(url_for('add_student'))
        em=request.form.get('email','').strip()
        if em and not valid_email(em):
            flash('Please enter a valid email address.','danger'); return redirect(url_for('add_student'))
        sid=gen_student_id(cr['course_code'])
        ph=save_upload(request.files.get('photo'), 'photos', f'{sid}_photo', ALLOWED_IMAGE)
        ip=save_upload(request.files.get('id_proof'), 'id_proofs', f'{sid}_id')
        # Student login account. Password is random unless STUDENT_DEFAULT_PASSWORD is set,
        # and must be changed on first login either way.
        student_pw = os.environ.get('STUDENT_DEFAULT_PASSWORD', '').strip() or secrets.token_urlsafe(9)
        suid=ex("INSERT INTO users (username,password_hash,full_name,email,phone,role,must_change_password,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid,generate_password_hash(student_pw),fn,em,request.form.get('mobile','').strip(),'student',1,datetime.now().isoformat()))
        iid=ex("INSERT INTO students (student_id,full_name,guardian_name,dob,gender,mobile,whatsapp,email,address,city,state,country,id_number,qualification,photo,id_proof,joining_date,course_id,batch_id,counselor_id,remarks,user_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid,fn,request.form.get('guardian_name',''),request.form.get('dob',''),request.form.get('gender',''),
             request.form.get('mobile',''),request.form.get('whatsapp',''),em,
             request.form.get('address',''),request.form.get('city',''),request.form.get('state',''),
             request.form.get('country','India'),request.form.get('id_number',''),request.form.get('qualification',''),
             ph,ip,request.form.get('joining_date',date.today().isoformat()),
             cid,request.form.get('batch_id') or None,request.form.get('counselor_id') or None,
             request.form.get('remarks',''),suid,datetime.now().isoformat()))
        rf=parse_money(request.form.get('registration_fee'))
        af=parse_money(request.form.get('admission_fee'))
        cf2=parse_money(request.form.get('course_fee'))
        ef=parse_money(request.form.get('exam_fee'))
        cef=parse_money(request.form.get('certificate_fee'))
        mf=parse_money(request.form.get('misc_fee'))
        tot=rf+af+cf2+ef+cef+mf
        ex("INSERT INTO fee_structures (student_id,registration_fee,admission_fee,course_fee,exam_fee,certificate_fee,misc_fee,total_fee,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (iid,rf,af,cf2,ef,cef,mf,tot,datetime.now().isoformat()))
        log(session['user_id'],'create','students',iid,f'Admitted {sid}')
        notify_users(f"🎓 New student admitted: {fn} ({sid}) — by {session.get('full_name','Admin')}", session['user_id'])
        flash(f'Student admitted! ID: {sid}','success')
        flash(f'Student portal login — username: {sid} · temporary password: {student_pw} '
              f'(they must change it on first login). Note it down now, it will not be shown again.','info')
        return redirect(url_for('view_student',id=iid))
    return render_template('add_student.html',courses=cl,batches=bl,counselors=co)

@app.route('/students/<int:id>')
@login_required
@staff_required
def view_student(id):
    st=q("SELECT s.*,c.course_name,c.duration,c.course_code FROM students s LEFT JOIN courses c ON s.course_id=c.id WHERE s.id=?",(id,),one=True)
    if not st: abort(404)
    fs=q("SELECT * FROM fee_structures WHERE student_id=?",(id,),one=True)
    pay=q("SELECT * FROM fee_payments WHERE student_id=? ORDER BY payment_date DESC",(id,))
    tp=paid_amt(id); tf=fs['total_fee'] if fs else 0; pn=max(0,tf-tp)
    ar=q("SELECT * FROM attendance WHERE student_id=? ORDER BY attendance_date DESC LIMIT 30",(id,))
    rr=q("SELECT er.*,e.exam_name,e.exam_type FROM exam_results er JOIN exams e ON er.exam_id=e.id WHERE er.student_id=? ORDER BY e.exam_date DESC",(id,))
    ct=q("SELECT * FROM certificates WHERE student_id=? ORDER BY id DESC",(id,))
    ap=round(sum(1 for a in ar if a['status']=='present')/len(ar)*100) if ar else 0
    return render_template('view_student.html',student=st,fs=fs,pay=pay,tp=tp,tf=tf,pn=pn,ar=ar,rr=rr,ct=ct,ap=ap)

@app.route('/students/<int:id>/edit',methods=['GET','POST'])
@login_required
@role_required('super_admin','admin','counselor')
def edit_student(id):
    st=q("SELECT * FROM students WHERE id=?",(id,),one=True)
    if not st: abort(404)
    if request.method=='POST':
        ex("UPDATE students SET full_name=?,guardian_name=?,dob=?,gender=?,mobile=?,whatsapp=?,email=?,address=?,city=?,state=?,country=?,qualification=?,batch_id=?,counselor_id=?,remarks=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (request.form.get('full_name',''),request.form.get('guardian_name',''),request.form.get('dob',''),request.form.get('gender',''),request.form.get('mobile',''),request.form.get('whatsapp',''),request.form.get('email',''),request.form.get('address',''),request.form.get('city',''),request.form.get('state',''),request.form.get('country','India'),request.form.get('qualification',''),request.form.get('batch_id') or None,request.form.get('counselor_id') or None,request.form.get('remarks',''),request.form.get('status','active'),id))
        flash('Updated!','success'); return redirect(url_for('view_student',id=id))
    cl=q("SELECT * FROM courses WHERE is_active=1"); bl=q("SELECT b.*,c.course_name FROM batches b JOIN courses c ON b.course_id=c.id WHERE b.status='active'")
    co=q("SELECT * FROM users WHERE role IN ('counselor','admin','super_admin') AND is_active=1")
    return render_template('edit_student.html',student=st,courses=cl,batches=bl,counselors=co)

@app.route('/student_portal')
@login_required
@role_required('student')
def student_portal():
    st=q("SELECT s.*,c.course_name,c.duration,c.course_code FROM students s LEFT JOIN courses c ON s.course_id=c.id WHERE s.user_id=?",(session['user_id'],),one=True)
    if not st:
        flash('No student profile linked to your account.','danger'); return redirect(url_for('dashboard'))
    sid=st['id']
    fs=q("SELECT * FROM fee_structures WHERE student_id=?",(sid,),one=True)
    pay=q("SELECT * FROM fee_payments WHERE student_id=? ORDER BY payment_date DESC",(sid,))
    tp=sum(p['amount'] for p in pay); tf=fs['total_fee'] if fs else 0; pn=max(0,tf-tp)
    ar=q("SELECT * FROM attendance WHERE student_id=? ORDER BY attendance_date DESC LIMIT 30",(sid,))
    rr=q("SELECT er.*,e.exam_name,e.exam_type FROM exam_results er JOIN exams e ON er.exam_id=e.id WHERE er.student_id=? ORDER BY e.exam_date DESC",(sid,))
    ct=q("SELECT * FROM certificates WHERE student_id=? ORDER BY id DESC",(sid,))
    return render_template('student_portal.html',student=st,tf=tf,tp=tp,pn=pn,pay=pay,ar=ar,rr=rr,ct=ct)

TG_CODE_TTL_MINUTES = int(os.environ.get('TG_CODE_TTL_MINUTES', '10'))

@app.route('/link_telegram', methods=['GET','POST'])
@login_required
@rate_limit(max_attempts=10, window_seconds=300)
def link_telegram():
    uid = session['user_id']
    usr = q("SELECT telegram_id,tg_link_code,tg_link_expires FROM users WHERE id=?", (uid,), one=True)
    if request.method == 'POST':
        act = request.form.get('action')
        if act == 'generate':
            # Cryptographically secure, and it expires.
            code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(8))
            expires = (datetime.now() + timedelta(minutes=TG_CODE_TTL_MINUTES)).isoformat()
            ex("UPDATE users SET tg_link_code=?, tg_link_expires=? WHERE id=?", (code, expires, uid))
            flash(f'Code generated: {code} — valid for {TG_CODE_TTL_MINUTES} minutes. '
                  'Send it to the bot on Telegram.', 'success')
        elif act == 'confirm':
            code = request.form.get('code', '').strip().upper()
            stored = (usr or {}).get('tg_link_code') or ''
            expires = (usr or {}).get('tg_link_expires')
            expired = True
            if expires:
                try:
                    expired = datetime.fromisoformat(expires) < datetime.now()
                except (ValueError, TypeError):
                    expired = True
            chat_id = parse_int(request.form.get('chat_id'), 0)
            if expired:
                flash('That code has expired. Generate a new one.', 'danger')
            elif not chat_id:
                flash('Enter the numeric chat ID the bot gave you.', 'danger')
            elif code and stored and hmac.compare_digest(code, stored):
                ex("UPDATE users SET telegram_id=?, tg_link_code=NULL, tg_link_expires=NULL WHERE id=?",
                   (chat_id, uid))
                log(uid, 'link_telegram', 'users', uid)
                flash('Telegram linked! 🎉', 'success')
            else:
                flash('Invalid code. Try again.', 'danger')
        elif act == 'unlink':
            ex("UPDATE users SET telegram_id=NULL, tg_link_code=NULL, tg_link_expires=NULL WHERE id=?", (uid,))
            log(uid, 'unlink_telegram', 'users', uid)
            flash('Telegram unlinked.', 'info')
        return redirect(url_for('link_telegram'))
    return render_template('link_telegram.html', user=usr)

@app.route('/telegram/link', methods=['POST'])
@rate_limit(max_attempts=5, window_seconds=300)
def telegram_link_api():
    """Called by the bot flow to bind a chat_id to the user holding that code."""
    body = request.get_json(force=True, silent=True) or {}
    code = (body.get('code') or '').strip().upper()
    chat_id = parse_int(body.get('chat_id'), 0)
    if not code or not chat_id:
        return jsonify({'ok': False, 'error': 'missing'}), 400
    u = q("SELECT id,full_name,tg_link_expires FROM users WHERE tg_link_code=? AND is_active=1",
          (code,), one=True)
    if not u:
        logger.warning('Invalid telegram link code attempt from %s', request.remote_addr)
        return jsonify({'ok': False, 'error': 'invalid code'}), 404
    try:
        if not u.get('tg_link_expires') or datetime.fromisoformat(u['tg_link_expires']) < datetime.now():
            return jsonify({'ok': False, 'error': 'code expired'}), 410
    except (ValueError, TypeError):
        return jsonify({'ok': False, 'error': 'code expired'}), 410
    ex("UPDATE users SET telegram_id=?, tg_link_code=NULL, tg_link_expires=NULL WHERE id=?", (chat_id, u['id']))
    log(u['id'], 'link_telegram', 'users', u['id'])
    tg_send(chat_id, f'✅ Linked! Welcome {u["full_name"]}. Type /help for commands.')
    return jsonify({'ok': True})


@app.route('/courses')
@login_required
@staff_required
def courses():
    return render_template('courses.html',courses=q("SELECT * FROM courses ORDER BY course_name"))

@app.route('/courses/add',methods=['POST'])
@login_required
@role_required('super_admin','admin')
def add_course():
    code=request.form.get('course_code','').upper().strip()
    name=request.form.get('course_name','').strip()
    if not code or not name:
        flash('Course code and name are required.','danger'); return redirect(url_for('courses'))
    if q("SELECT id FROM courses WHERE course_code=?",(code,),one=True):
        flash(f'Course code {code} already exists.','danger'); return redirect(url_for('courses'))
    ex("INSERT INTO courses (course_code,course_name,duration,fees,description,created_at) VALUES (?,?,?,?,?,?)",
        (code,name,request.form.get('duration','').strip(),
         parse_money(request.form.get('fees')),
         request.form.get('description','').strip(),datetime.now().isoformat()))
    log(session['user_id'],'create','courses',None,code)
    flash('Course added!','success'); return redirect(url_for('courses'))

@app.route('/courses/<int:id>/edit',methods=['POST'])
@login_required
@role_required('super_admin','admin')
def edit_course(id):
    # BUGFIX: was request.get('fees',0) -> AttributeError 500 on every edit.
    ex("UPDATE courses SET course_code=?,course_name=?,duration=?,fees=?,description=? WHERE id=?",
        (request.form.get('course_code','').upper().strip(),
         request.form.get('course_name','').strip(),
         request.form.get('duration','').strip(),
         parse_money(request.form.get('fees'), 0),
         request.form.get('description','').strip(), id))
    log(session['user_id'],'update','courses',id)
    flash('Course updated!','success'); return redirect(url_for('courses'))

@app.route('/courses/<int:id>/toggle',methods=['POST'])
@login_required
@role_required('super_admin','admin')
def toggle_course(id):
    ex("UPDATE courses SET is_active=CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?",(id,))
    return redirect(url_for('courses'))

@app.route('/batches')
@login_required
@staff_required
def batches():
    return render_template('batches.html',batches=q("SELECT b.*,c.course_name,(SELECT COUNT(*) FROM students WHERE batch_id=b.id) as strength FROM batches b JOIN courses c ON b.course_id=c.id ORDER BY b.id DESC"),courses=q("SELECT * FROM courses WHERE is_active=1 ORDER BY course_name"))

@app.route('/batches/get/<int:course_id>')
@login_required
@staff_required
def get_batches(course_id):
    return jsonify([dict(b) for b in q("SELECT id,batch_name FROM batches WHERE course_id=? AND status='active'",(course_id,))])

@app.route('/batches/add',methods=['POST'])
@login_required
@role_required('super_admin','admin')
def add_batch():
    name=request.form.get('batch_name','').strip()
    cid=request.form.get('course_id')
    if not name or not cid:
        flash('Batch name and course are required.','danger'); return redirect(url_for('batches'))
    if not q("SELECT id FROM courses WHERE id=?",(cid,),one=True):
        flash('Selected course does not exist.','danger'); return redirect(url_for('batches'))
    ex("INSERT INTO batches (batch_name,course_id,trainer_id,timing,start_date,end_date,max_strength,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (name,cid,request.form.get('trainer_id') or None,request.form.get('timing','').strip(),
         request.form.get('start_date',''),request.form.get('end_date',''),
         parse_int(request.form.get('max_strength'),30,minimum=1,maximum=1000),
         'active',datetime.now().isoformat()))
    log(session['user_id'],'create','batches',None,name)
    flash('Batch created!','success'); return redirect(url_for('batches'))

@app.route('/fees')
@login_required
@staff_required
def fees():
    fd=q("SELECT s.id,s.student_id,s.full_name,s.mobile,c.course_name,fs.total_fee,COALESCE((SELECT SUM(amount) FROM fee_payments WHERE student_id=s.id),0) as paid,(SELECT payment_date FROM fee_payments WHERE student_id=s.id ORDER BY payment_date DESC LIMIT 1) as last_payment_date FROM students s LEFT JOIN courses c ON s.course_id=c.id LEFT JOIN fee_structures fs ON fs.student_id=s.id WHERE s.status='active' ORDER BY s.full_name")
    tp=sum(max(0,(f['total_fee'] or 0)-f['paid']) for f in fd); tc=sum(f['paid'] for f in fd)
    return render_template('fees.html',fee_data=fd,tp=tp,tc=tc)

@app.route('/fees/pay/<int:student_id>',methods=['POST'])
@login_required
@role_required('super_admin','admin','accountant')
def record_payment(student_id):
    amt=parse_money(request.form.get('amount'))
    if amt<=0:
        flash('Amount > 0 required.','danger'); return redirect(url_for('view_student',id=student_id))
    stn=q("SELECT full_name FROM students WHERE id=?",(student_id,),one=True)
    if not stn:
        flash('Student not found.','danger'); return redirect(url_for('fees'))
    method=request.form.get('payment_method','Cash')
    if method not in ('Cash','Card','UPI','Bank Transfer','Cheque','Online'):
        method='Cash'
    rcp=gen_receipt()
    ex("INSERT INTO fee_payments (student_id,receipt_no,amount,payment_date,payment_method,installment_no,remarks,collected_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (student_id,rcp,amt,date.today().isoformat(),method,
         parse_int(request.form.get('installment_no'),0,minimum=0),
         request.form.get('remarks',''),session['user_id'],datetime.now().isoformat()))
    log(session['user_id'],'fee_payment','fee_payments',student_id,f'Rs.{amt} - {rcp}')
    notify_users(f"💰 Fee received: ₹{amt:,.0f} from {stn['full_name']} (Receipt {rcp}) — by {session.get('full_name','Admin')}", session['user_id'])
    flash(f'Payment recorded! Receipt: {rcp}','success'); return redirect(url_for('view_student',id=student_id))

@app.route('/fees/receipt/<int:payment_id>')
@login_required
@staff_required
def view_receipt(payment_id):
    p=q("SELECT fp.id as payment_fk,fp.student_id as student_fk,fp.*,s.full_name,s.student_id as sid,s.mobile,s.address,c.course_name FROM fee_payments fp JOIN students s ON fp.student_id=s.id LEFT JOIN courses c ON s.course_id=c.id WHERE fp.id=?",(payment_id,),one=True)
    if not p: abort(404)
    return render_template('receipt.html',payment=p)

@app.route('/attendance',methods=['GET','POST'])
@login_required
@role_required('super_admin','admin','trainer','counselor')
def attendance():
    bl=q("SELECT b.id,b.batch_name,c.course_name FROM batches b JOIN courses c ON b.course_id=c.id WHERE b.status='active'")
    ds=request.args.get('date',date.today().isoformat()); bid=request.args.get('batch_id','')
    if request.method=='POST' and request.form.get('batch_id'):
        bid=request.form.get('batch_id'); ds=request.form.get('date',date.today().isoformat())
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', ds or ''):
            flash('Invalid date.','danger'); return redirect(url_for('attendance'))
        # Single connection + executemany instead of 2 queries per student.
        roster=q("SELECT id FROM students WHERE batch_id=? AND status='active'",(bid,))
        marked=0
        for att in roster:
            st=request.form.get(f"status_{att['id']}",'absent')
            if st not in ('present','absent','late','excused'):
                st='absent'
            ex("UPDATE attendance SET status=?,remarks=? WHERE student_id=? AND batch_id=? AND attendance_date=?",
               (st,request.form.get(f"remarks_{att['id']}",''),att['id'],bid,ds))
            if _last_rowcount==0:
                ex("INSERT INTO attendance (student_id,batch_id,attendance_date,status,marked_by,created_at) VALUES (?,?,?,?,?,?)",
                   (att['id'],bid,ds,st,session['user_id'],datetime.now().isoformat()))
            marked+=1
        log(session['user_id'],'mark_attendance','attendance',parse_int(bid),f'{marked} students on {ds}')
        flash(f'Attendance saved for {marked} students!','success')
        return redirect(url_for('attendance',batch_id=bid,date=ds))
    sib=[]
    if bid: sib=q("SELECT s.id,s.full_name,s.student_id,a.status as att_status FROM students s LEFT JOIN attendance a ON a.student_id=s.id AND a.batch_id=? AND a.attendance_date=? WHERE s.batch_id=? AND s.status='active'",(bid,ds,bid))
    return render_template('attendance.html',batches=bl,selected_batch=bid,date=ds,students=sib)

@app.route('/exams')
@login_required
@staff_required
def exams():
    return render_template('exams.html',exams=q("SELECT e.*,c.course_name,(SELECT COUNT(*) FROM exam_results WHERE exam_id=e.id) as results_entered FROM exams e JOIN courses c ON e.course_id=c.id ORDER BY e.exam_date DESC"),courses=q("SELECT * FROM courses WHERE is_active=1 ORDER BY course_name"))

@app.route('/exams/add',methods=['POST'])
@login_required
@role_required('super_admin','admin','trainer')
def add_exam():
    name=request.form.get('exam_name','').strip()
    cid=request.form.get('course_id')
    if not name or not cid:
        flash('Exam name and course are required.','danger'); return redirect(url_for('exams'))
    mm=parse_money(request.form.get('max_marks'),100) or 100
    pm=parse_money(request.form.get('passing_marks'),40)
    if pm > mm:
        flash('Passing marks cannot exceed maximum marks.','danger'); return redirect(url_for('exams'))
    etype=request.form.get('exam_type','theory')
    if etype not in ('theory','practical','both'):
        etype='theory'
    ex("INSERT INTO exams (exam_name,course_id,batch_id,exam_date,max_marks,passing_marks,exam_type,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (name,cid,request.form.get('batch_id') or None,request.form.get('exam_date',''),
         mm,pm,etype,datetime.now().isoformat()))
    log(session['user_id'],'create','exams',None,name)
    flash('Exam created!','success'); return redirect(url_for('exams'))

@app.route('/exams/<int:eid>/results',methods=['GET','POST'])
@login_required
@role_required('super_admin','admin','trainer')
def exam_results(eid):
    exm=q("SELECT e.*,c.course_name FROM exams e JOIN courses c ON e.course_id=c.id WHERE e.id=?",(eid,),one=True)
    if not exm: abort(404)
    if request.method=='POST':
        mm=exm['max_marks'] or 100
        for st in q("SELECT id FROM students WHERE course_id=? AND status='active'",(exm['course_id'],)):
            th=parse_money(request.form.get(f"theory_{st['id']}"))
            pr=parse_money(request.form.get(f"practical_{st['id']}"))
            tot2=th+pr
            if tot2 > mm:
                flash(f'Marks for one student exceeded the maximum ({mm}). That entry was capped.','warning')
                tot2=mm
            pct=round(tot2/mm*100,2) if mm else 0
            if pct>=75: gr='A+'; sr='Distinction'
            elif pct>=60: gr='A'; sr='First Class'
            elif pct>=50: gr='B'; sr='Second Class'
            elif pct>=40: gr='C'; sr='Pass'
            else: gr='F'; sr='Fail'
            exi=q("SELECT id FROM exam_results WHERE exam_id=? AND student_id=?",(eid,st['id']),one=True)
            if exi:
                ex("UPDATE exam_results SET theory_marks=?,practical_marks=?,total_marks=?,percentage=?,grade=?,status=? WHERE id=?",(th,pr,tot2,pct,gr,sr,exi['id']))
            else:
                ex("INSERT INTO exam_results (exam_id,student_id,theory_marks,practical_marks,total_marks,percentage,grade,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",(eid,st['id'],th,pr,tot2,pct,gr,sr,datetime.now().isoformat()))
        log(session['user_id'],'save_results','exam_results',eid)
        flash('Results saved!','success'); return redirect(url_for('exam_results',eid=eid))
    studs=q("SELECT s.id,s.full_name,s.student_id,er.theory_marks,er.practical_marks,er.total_marks,er.percentage,er.grade,er.status FROM students s LEFT JOIN exam_results er ON er.student_id=s.id AND er.exam_id=? WHERE s.course_id=? AND s.status='active'",(eid,exm['course_id']))
    return render_template('exam_results.html',exam=exm,students=studs)

@app.route('/certificates')
@login_required
@staff_required
def certificates():
    ct=q("SELECT cert.*,s.full_name,s.student_id,c.course_name FROM certificates cert JOIN students s ON cert.student_id=s.id JOIN courses c ON cert.course_id=c.id ORDER BY cert.id DESC")
    sl=q("SELECT s.id,s.full_name,s.student_id,c.course_name,c.id as course_id FROM students s JOIN courses c ON s.course_id=c.id WHERE s.status='active' ORDER BY s.full_name")
    return render_template('certificates.html',certificates=ct,students=sl)

@app.route('/certificates/generate',methods=['POST'])
@login_required
@role_required('super_admin','admin')
def generate_certificate():
    sid=request.form.get('student_id',type=int)
    if not sid: flash('Select student.','danger'); return redirect(url_for('certificates'))
    st=q("SELECT s.*,c.course_name,c.duration FROM students s JOIN courses c ON s.course_id=c.id WHERE s.id=?",(sid,),one=True)
    if not st: flash('Not found.','danger'); return redirect(url_for('certificates'))
    exi=q("SELECT * FROM certificates WHERE student_id=? AND course_id=?",(sid,st['course_id']),one=True)
    if exi: flash(f'Already exists: {exi["certificate_no"]}','warning'); return redirect(url_for('certificates'))
    r=q("SELECT AVG(er.percentage) as avg_pct FROM exam_results er JOIN exams e ON er.exam_id=e.id WHERE er.student_id=? AND e.course_id=?",(sid,st['course_id']),one=True)
    ap=round(r['avg_pct'] or 0,2) if r else 0
    gr='A+' if ap>=75 else 'A' if ap>=60 else 'B' if ap>=50 else 'C' if ap>=40 else 'F'
    cn=gen_cert_no()
    ex("INSERT INTO certificates (certificate_no,student_id,course_id,grade,percentage,completion_date) VALUES (?,?,?,?,?,?)",(cn,sid,st['course_id'],gr,ap,date.today().isoformat()))
    log(session['user_id'],'generate_certificate','certificates',sid,cn)
    flash(f'Certificate: {cn}','success'); return redirect(url_for('certificates'))

@app.route('/certificates/view/<int:cid>')
@login_required
@staff_required
def view_certificate(cid):
    ct=q("SELECT cert.*,s.full_name,s.student_id,s.photo,c.course_name,c.duration FROM certificates cert JOIN students s ON cert.student_id=s.id JOIN courses c ON cert.course_id=c.id WHERE cert.id=?",(cid,),one=True)
    if not ct: abort(404)
    return render_template('certificate_view.html',cert=ct)

@app.route('/verify',methods=['GET','POST'])
def verify_certificate():
    result=None
    if request.method=='POST':
        term=request.form.get('search_term','').strip()
        if term:
            ct=q("SELECT cert.*,s.full_name,s.student_id,c.course_name FROM certificates cert JOIN students s ON cert.student_id=s.id JOIN courses c ON cert.course_id=c.id WHERE cert.certificate_no=? OR s.student_id=?",(term,term),one=True)
            result=ct
    return render_template('verify_cert.html',result=result)

@app.route('/trainers')
@login_required
@staff_required
def trainers():
    tr=q("SELECT t.*,u.full_name,u.email,u.phone FROM trainers t JOIN users u ON t.user_id=u.id ORDER BY u.full_name")
    return render_template('trainers.html',trainers=tr)

@app.route('/trainers/<int:id>')
@login_required
@staff_required
def view_trainer(id):
    t=q("SELECT t.*,u.full_name,u.email,u.phone FROM trainers t JOIN users u ON t.user_id=u.id WHERE t.id=?",(id,),one=True)
    if not t: abort(404)
    return render_template('trainer_view.html',t=t)

@app.route('/trainers/add',methods=['POST'])
@login_required
@role_required('super_admin','admin')
def add_trainer():
    fn=request.form.get('full_name','').strip()
    em=request.form.get('email','').strip()
    if not fn:
        flash('Trainer name is required.','danger'); return redirect(url_for('trainers'))
    if em and not valid_email(em):
        flash('Please enter a valid email address.','danger'); return redirect(url_for('trainers'))
    un=(em.replace('@','_').replace('.','_') if em else '') or f"trainer_{uuid.uuid4().hex[:6]}"
    if q("SELECT id FROM users WHERE username=?",(un,),one=True):
        flash(f'A user with username "{un}" already exists.','danger'); return redirect(url_for('trainers'))
    pw = os.environ.get('TRAINER_DEFAULT_PASSWORD','').strip() or secrets.token_urlsafe(9)
    uid=ex("INSERT INTO users (username,password_hash,full_name,email,phone,role,must_change_password,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (un,generate_password_hash(pw),fn,em,request.form.get('phone','').strip(),'trainer',1,datetime.now().isoformat()))
    ex("INSERT INTO trainers (user_id,qualification,experience,salary,specialization,joining_date) VALUES (?,?,?,?,?,?)",
        (uid,request.form.get('qualification','').strip(),request.form.get('experience','').strip(),
         parse_money(request.form.get('salary')),request.form.get('specialization','').strip(),
         request.form.get('joining_date',date.today().isoformat())))
    log(session['user_id'],'create','trainers',uid,fn)
    flash('Trainer added!','success')
    flash(f'Login — username: {un} · temporary password: {pw} (must be changed on first login).','info')
    return redirect(url_for('trainers'))

@app.route('/staff')
@login_required
@role_required('super_admin','admin')
def staff():
    # Never send password hashes to the template layer.
    users=q("SELECT id,username,full_name,email,phone,role,is_active,created_at FROM users ORDER BY full_name")
    return render_template('staff.html',users=users)

ASSIGNABLE_ROLES = {'admin','accountant','counselor','trainer','staff','student'}
PRIVILEGED_ROLES = {'super_admin'}

@app.route('/staff/add',methods=['POST'])
@login_required
@role_required('super_admin','admin')
def add_staff():
    un=request.form.get('username','').strip()
    em=request.form.get('email','').strip()
    fn=request.form.get('full_name','').strip()
    if not fn:
        flash('Full name is required.','danger'); return redirect(url_for('staff'))
    if em and not valid_email(em):
        flash('Please enter a valid email address.','danger'); return redirect(url_for('staff'))
    if not un:
        un=em.replace('@','_').replace('.','_')
    if not un:
        flash('Username or email is required.','danger'); return redirect(url_for('staff'))
    if not re.fullmatch(r'[A-Za-z0-9._-]{3,64}', un):
        flash('Username must be 3-64 chars: letters, numbers, dot, underscore, hyphen.','danger')
        return redirect(url_for('staff'))
    if q("SELECT id FROM users WHERE username=?",(un,),one=True):
        flash(f'Username "{un}" is already taken.','danger'); return redirect(url_for('staff'))

    # Privilege escalation guard: only a super_admin may mint another super_admin.
    role=request.form.get('role','staff')
    if role in PRIVILEGED_ROLES and session.get('role') != 'super_admin':
        flash('Only a super admin can create super admin accounts.','danger')
        return redirect(url_for('staff'))
    if role not in ASSIGNABLE_ROLES and role not in PRIVILEGED_ROLES:
        role='staff'

    pw = request.form.get('password','').strip() or secrets.token_urlsafe(9)
    if len(pw) < 8:
        flash('Password must be at least 8 characters.','danger'); return redirect(url_for('staff'))
    ex("INSERT INTO users (username,password_hash,full_name,email,phone,role,must_change_password,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (un,generate_password_hash(pw),fn,em,request.form.get('phone','').strip(),role,1,datetime.now().isoformat()))
    log(session['user_id'],'create','users',None,f'{un} as {role}')
    flash('Staff added!','success')
    flash(f'Login — username: {un} · temporary password: {pw} (must be changed on first login).','info')
    return redirect(url_for('staff'))

@app.route('/staff/<int:id>/toggle',methods=['POST'])
@login_required
@role_required('super_admin','admin')
def toggle_staff(id):
    target=q("SELECT id,username,role,is_active FROM users WHERE id=?",(id,),one=True)
    if not target: abort(404)
    if target['id'] == session['user_id']:
        flash('You cannot deactivate your own account.','danger'); return redirect(url_for('staff'))
    if target['role'] == 'super_admin' and session.get('role') != 'super_admin':
        flash('Only a super admin can modify a super admin account.','danger'); return redirect(url_for('staff'))
    if target['role'] == 'super_admin' and target['is_active']:
        remaining = q("SELECT COUNT(*) as c FROM users WHERE role='super_admin' AND is_active=1")[0]['c']
        if remaining <= 1:
            flash('Cannot deactivate the last active super admin.','danger'); return redirect(url_for('staff'))
    ex("UPDATE users SET is_active=CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?",(id,))
    log(session['user_id'],'toggle_active','users',id,target['username'])
    flash('Staff status updated.','success'); return redirect(url_for('staff'))

@app.route('/admin/telegram_token', methods=['GET','POST'])
@login_required
@role_required('super_admin')
def admin_telegram_token():
    if request.method == 'POST':
        tok = request.form.get('token','').strip()
        if tok and re.fullmatch(r'\d{6,12}:[A-Za-z0-9_-]{30,}', tok):
            set_setting('TELEGRAM_BOT_TOKEN', tok)
            log(session['user_id'],'update_setting','settings',None,'TELEGRAM_BOT_TOKEN')
            flash('Telegram bot token saved!','success')
        else:
            flash('Invalid token format. Expected something like 123456789:AA...','danger')
        return redirect(url_for('admin_telegram_token'))
    cur = get_setting('TELEGRAM_BOT_TOKEN', '')
    masked = (cur[:6] + '…' + cur[-4:]) if len(cur) > 12 else ('set' if cur else '')
    return render_template('admin_telegram.html', masked=masked,
                           from_env=bool(os.environ.get('TELEGRAM_BOT_TOKEN')),
                           webhook_secret_set=bool(TELEGRAM_WEBHOOK_SECRET))

@app.route('/reports')
@login_required
@staff_required
def reports():
    return render_template('reports.html')

@app.route('/reports/admissions')
@login_required
@staff_required
def report_admissions():
    period=request.args.get('period','monthly')
    if period=='daily':
        if DB_MODE == 'postgres': rows=q("SELECT DATE(created_at) as period,COUNT(*) as count FROM students GROUP BY period ORDER BY period DESC LIMIT 30")
        else: rows=q("SELECT date(created_at) as period,COUNT(*) as count FROM students GROUP BY period ORDER BY period DESC LIMIT 30")
    else:
        if DB_MODE == 'postgres': rows=q("SELECT TO_CHAR(created_at::date,'YYYY-MM') as period,COUNT(*) as count FROM students GROUP BY period ORDER BY period DESC LIMIT 12")
        else: rows=q("SELECT strftime('%Y-%m',created_at) as period,COUNT(*) as count FROM students GROUP BY period ORDER BY period DESC LIMIT 12")
    return render_template('report_admissions.html',rows=rows,period=period)

@app.route('/reports/fees')
@login_required
@staff_required
def report_fees():
    rows=q("SELECT s.student_id,s.full_name,c.course_name,fs.total_fee,COALESCE((SELECT SUM(amount) FROM fee_payments WHERE student_id=s.id),0) as paid,fs.total_fee-COALESCE((SELECT SUM(amount) FROM fee_payments WHERE student_id=s.id),0) as pending FROM students s JOIN fee_structures fs ON fs.student_id=s.id LEFT JOIN courses c ON s.course_id=c.id WHERE s.status='active' ORDER BY pending DESC")
    return render_template('report_fees.html',rows=rows)

@app.route('/reports/export/<rtype>')
@login_required
@staff_required
def export_report(rtype):
    output=io.StringIO(); w=csv.writer(output)
    if rtype=='students':
        w.writerow(['Student ID','Name','Course','Mobile','Email','Status','Joining Date'])
        for s in q("SELECT s.*,c.course_name FROM students s LEFT JOIN courses c ON s.course_id=c.id"):
            w.writerow([s['student_id'],s['full_name'],s['course_name'],s['mobile'],s['email'],s['status'],s['joining_date']])
    elif rtype=='fees':
        w.writerow(['Student ID','Name','Total Fee','Paid','Pending'])
        for s in q("SELECT s.student_id,s.full_name,fs.total_fee,COALESCE((SELECT SUM(amount) FROM fee_payments WHERE student_id=s.id),0) as paid FROM students s LEFT JOIN fee_structures fs ON fs.student_id=s.id"):
            w.writerow([s['student_id'],s['full_name'],s['total_fee'],s['paid'],(s['total_fee'] or 0)-s['paid']])
    output.seek(0)
    return send_file(io.BytesIO(output.getvalue().encode()),mimetype='text/csv',as_attachment=True,download_name=f'{rtype}_report.csv')
# ── Telegram ──
def tg_send(chat_id, text, parse_mode='HTML'):
    api = telegram_api()
    if not api:
        return False
    try:
        data = json.dumps({'chat_id': chat_id, 'text': text, 'parse_mode': parse_mode}).encode()
        req = urllib.request.Request(f'{api}/sendMessage', data=data, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("ok", False)
    except Exception as e:
        logger.error('Telegram send error: %s', e)
        return False

def tg_broadcast(text, parse_mode='HTML'):
    """Send a message to every KTS user who has linked their Telegram."""
    sent = 0
    for u in q("SELECT telegram_id FROM users WHERE telegram_id IS NOT NULL AND telegram_id<>0 AND is_active=1"):
        if tg_send(u['telegram_id'], text, parse_mode):
            sent += 1
    return sent

def notify_users(message, exclude_user_id=None):
    """Notify all linked Telegram users except the one who triggered the action."""
    for u in q("SELECT telegram_id FROM users WHERE telegram_id IS NOT NULL AND telegram_id<>0 AND is_active=1 AND id<>?", (exclude_user_id or -1,)):
        tg_send(u['telegram_id'], message)

@app.route('/telegram/webhook', methods=['POST'])
def telegram_webhook():
    if not _load_telegram_token():
        return jsonify({'ok': False, 'error': 'not configured'}), 503
    # Verify the request really came from Telegram. Set this secret when you
    # register the webhook:  setWebhook?secret_token=<TELEGRAM_WEBHOOK_SECRET>
    if TELEGRAM_WEBHOOK_SECRET:
        sent = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if not hmac.compare_digest(sent, TELEGRAM_WEBHOOK_SECRET):
            logger.warning('Rejected unauthenticated Telegram webhook from %s', request.remote_addr)
            abort(403)
    else:
        logger.warning('TELEGRAM_WEBHOOK_SECRET is not set - webhook is unauthenticated.')
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        return jsonify({'ok': True})
    message = data.get('message', {}) or {}
    chat = message.get('chat', {})
    chat_id = chat.get('id')
    text = (message.get('text') or '').strip()
    user_id = message.get('from', {}).get('id')
    if not chat_id or not text: return jsonify({'ok': True})
    # Lookup by linked telegram_id
    user = q("SELECT * FROM users WHERE telegram_id=? AND is_active=1", (user_id,), one=True)
    if not user:
        tg_send(chat_id, '🔒 Not linked. Open KTS in your browser → click <b>Link Telegram</b> in the top-right menu, then send the code shown there here.')
        return jsonify({'ok': True})
    fn = user['full_name']
    cmd = text.lower().split()[0] if text else ''
    if cmd == '/start':
        tg_send(chat_id, f'🏫 <b>Welcome, {fn}!</b>\nYou are connected to KTS Institute Manager.\n\nCommands:\n/students\n/fees\n/courses\n/batches\n/reports\n/help')
    elif cmd == '/help':
        tg_send(chat_id, '<b>📋 Commands</b>\n/students - Recent students\n/fees - Fee summary\n/courses - Courses\n/batches - Batches\n/reports - Reports\n/me - Your profile')
    elif cmd == '/students':
        lines = ['<b>🎓 Recent Students</b>']
        for s in q("""SELECT s.student_id,s.full_name,c.course_code
                      FROM students s LEFT JOIN courses c ON s.course_id=c.id
                      WHERE s.status='active' ORDER BY s.id DESC LIMIT 10"""):
            lines.append(f"• {s['full_name']} ({s['student_id']}) {s['course_code'] or ''}")
        tg_send(chat_id, '\n'.join(lines))
    elif cmd == '/fees':
        tc = q("SELECT COALESCE(SUM(amount),0) as s FROM fee_payments")[0]['s'] or 0
        ac = q("SELECT COUNT(*) as c FROM students WHERE status='active'")[0]['c']
        tg_send(chat_id, f'<b>💰 Fees</b>\n\nActive students: {ac}\nCollected total: ₹{tc:,.0f}')
    elif cmd == '/courses':
        lines = ['<b>📚 Courses</b>']
        for c in q("SELECT course_code,course_name,fees FROM courses WHERE is_active=1"):
            lines.append(f"• {c['course_name']} ({c['course_code']}) - ₹{c['fees']:,.0f}")
        tg_send(chat_id, '\n'.join(lines))
    elif cmd == '/batches':
        lines = ['<b>📦 Batches</b>']
        for b in q("SELECT b.batch_name,c.course_name,b.status FROM batches b JOIN courses c ON b.course_id=c.id ORDER BY b.id DESC LIMIT 10"):
            lines.append(f"• {b['batch_name']} ({b['course_name']}) - {b['status']}")
        tg_send(chat_id, '\n'.join(lines))
    elif cmd == '/reports':
        ts = q("SELECT COUNT(*) as c FROM students")[0]['c']
        cs = q("SELECT COUNT(*) as c FROM certificates")[0]['c']
        ac = q("SELECT COUNT(*) as c FROM students WHERE status='active'")[0]['c']
        tg_send(chat_id, f'<b>📊 Report</b>\n\nTotal students: {ts}\nActive: {ac}\nCertificates: {cs}')
    elif cmd == '/me':
        tg_send(chat_id, f'<b>👤 {fn}</b>\nRole: {user["role"]}\nTelegram: linked ✅')
    elif re.fullmatch(r'[A-Z0-9]{6,8}', text.upper()):
        # Linking code from the web "Link Telegram" page
        u = q("SELECT id,full_name FROM users WHERE tg_link_code=? AND is_active=1", (text.upper(),), one=True)
        if u:
            tg_send(chat_id, f'✅ Code accepted! Your Telegram chat ID is:\n\n<code>{chat_id}</code>\n\nNow paste this number into the <b>Link Telegram</b> page in KTS and click Confirm. (Or just tell your admin.)')
        else:
            tg_send(chat_id, '🔒 That code is not valid or already used. Generate one from the Link Telegram page in KTS.')
    else:
        tg_send(chat_id, 'Unknown command. Type /help')
    return jsonify({'ok': True})

# ── Auto-init DB & Error handlers ──
@app.before_request
def ensure_db():
    global _db_initialized
    if not _db_initialized:
        init_db()

@app.route('/health')
def health():
    """Liveness/readiness probe. Deliberately minimal - it must not leak
    infrastructure details to the public internet."""
    try:
        init_db()
        q("SELECT 1 as ok")
        return jsonify({'status': 'ok', 'db': DB_MODE}), 200
    except Exception as e:
        logger.exception('Health check failed')
        return jsonify({'status': 'error'}), 503

@app.route('/health/detail')
@login_required
@role_required('super_admin')
def health_detail():
    """Full diagnostics - super admin only."""
    try:
        co = q("SELECT COUNT(*) as c FROM courses")[0]['c']
        probe = f'__WT__{uuid.uuid4().hex[:6]}'
        ex("INSERT INTO courses (course_code,course_name,duration,fees,description) VALUES (?,?,?,?,?)",
           (probe, 'writetest', '1d', 1, 'probe'))
        wid = q("SELECT id FROM courses WHERE course_code=?", (probe,), one=True)
        wrote = wid is not None
        if wid:
            ex("DELETE FROM courses WHERE id=?", (wid['id'],))
        parsed = urllib.parse.urlparse(DATABASE_URL) if DATABASE_URL else None
        ephemeral = DB_MODE == 'sqlite' and (DB_PATH.startswith('/tmp') or DB_PATH == ':memory:')
        return jsonify({
            'status': 'ok', 'db': DB_MODE, 'courses': co, 'write_test': wrote,
            'db_host': parsed.hostname if parsed else None,
            'db_path': DB_PATH if DB_MODE == 'sqlite' else None,
            'persistent': not ephemeral,
            'ephemeral_storage': ephemeral,
            'serverless': IS_SERVERLESS,
            'upload_storage': 'database' if USE_DB_UPLOADS else 'disk',
            'rate_limit_storage': 'database' if USE_DB_RATE_LIMIT else 'memory',
            'env': ENV,
        })
    except Exception as e:
        logger.exception('Detailed health check failed')
        return jsonify({'status': 'error', 'error': str(e)}), 500

@app.errorhandler(400)
def e400(error):
    desc = getattr(error, 'description', 'Bad request.')
    return render_template('error.html', code=400, title='Bad Request', message=desc), 400

@app.errorhandler(403)
def e403(error):
    return render_template('error.html', code=403, title='Forbidden',
                           message='You do not have permission to access this page.'), 403

@app.errorhandler(404)
def e404(error):
    return render_template('error.html', code=404, title='Not Found',
                           message='The page you are looking for does not exist.'), 404

@app.errorhandler(413)
def e413(error):
    return render_template('error.html', code=413, title='File Too Large',
                           message='The uploaded file exceeds the maximum allowed size.'), 413

@app.errorhandler(429)
def e429(error):
    return render_template('error.html', code=429, title='Too Many Requests',
                           message='Please slow down and try again shortly.'), 429

@app.errorhandler(500)
@app.errorhandler(Exception)
def e500(error):
    from werkzeug.exceptions import HTTPException
    if isinstance(error, HTTPException) and error.code and error.code < 500:
        return error
    # Log the full traceback server-side; show the user only a reference id.
    ref = uuid.uuid4().hex[:10]
    logger.exception('Unhandled error [ref=%s] on %s', ref, request.path if request else '?')
    if not IS_PROD:
        import traceback
        return render_template('error.html', code=500, title='Internal Server Error',
                               message=f'Reference: {ref}',
                               detail=traceback.format_exc()), 500
    return render_template('error.html', code=500, title='Internal Server Error',
                           message=f'Something went wrong. Quote reference {ref} '
                                   'when contacting your administrator.'), 500

if __name__=='__main__':
    init_db()
    # Debug mode is opt-in via FLASK_DEBUG and can never be on in production.
    debug = (not IS_PROD) and os.environ.get('FLASK_DEBUG', '').lower() in ('1','true','yes')
    port = int(os.environ.get('PORT', '5000'))
    host = os.environ.get('HOST', '127.0.0.1')
    if debug:
        logger.warning('Running in DEBUG mode - never do this in production.')
    app.run(debug=debug, host=host, port=port)
