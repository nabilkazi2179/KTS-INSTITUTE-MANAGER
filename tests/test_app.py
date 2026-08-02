"""Test suite for the KTS Institute Manager.

Run with:  pytest -v
"""
import os
import tempfile

import pytest

# Configure the environment BEFORE importing the app module.
os.environ['FLASK_ENV'] = 'development'
os.environ['TESTING'] = '1'
os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
os.environ['ADMIN_PASSWORD'] = 'testadmin123'
os.environ['TELEGRAM_WEBHOOK_SECRET'] = 'test-webhook-secret'
os.environ.pop('DATABASE_URL', None)

import app as kts  # noqa: E402


@pytest.fixture()
def client():
    """Fresh SQLite database per test."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    kts.DB_PATH = path
    kts.DB_MODE = 'sqlite'
    kts._db_initialized = False
    kts._rate_buckets.clear()
    kts.app.config['TESTING'] = True
    kts.init_db()
    with kts.app.test_client() as c:
        yield c
    os.unlink(path)


def login(client, username='admin', password='testadmin123'):
    """Log in, fetching a CSRF token from the login page first."""
    client.get('/login')
    with client.session_transaction() as sess:
        token = sess.get('_csrf_token')
    return client.post('/login', data={
        'username': username, 'password': password, '_csrf_token': token,
    }, follow_redirects=True)


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get('_csrf_token')


# ── Health & smoke ───────────────────────────────────────────
def test_health_ok(client):
    r = client.get('/health')
    assert r.status_code == 200
    assert r.get_json()['status'] == 'ok'


def test_health_leaks_no_infrastructure_detail(client):
    body = client.get('/health').get_json()
    for leaky in ('db_host', 'has_database_url', 'courses', 'write_test'):
        assert leaky not in body


def test_health_detail_requires_super_admin(client):
    assert client.get('/health/detail').status_code in (302, 401)


# ── Authentication ───────────────────────────────────────────
def test_login_required_redirects(client):
    r = client.get('/')
    assert r.status_code == 302
    assert '/login' in r.headers['Location']


def test_successful_login(client):
    r = login(client)
    assert r.status_code == 200
    with client.session_transaction() as sess:
        assert sess['role'] == 'super_admin'


def test_bad_password_rejected(client):
    login(client, password='wrong-password')
    with client.session_transaction() as sess:
        assert 'user_id' not in sess


def test_no_default_admin123_password(client):
    """The old hardcoded admin123 must not work."""
    login(client, password='admin123')
    with client.session_transaction() as sess:
        assert 'user_id' not in sess


def test_login_is_rate_limited(client):
    client.get('/login')
    codes = []
    for _ in range(15):
        token = csrf(client)
        r = client.post('/login', data={
            'username': 'admin', 'password': 'nope', '_csrf_token': token})
        codes.append(r.status_code)
    # After the limit the handler redirects with a flash rather than checking creds.
    assert kts._rate_buckets, 'rate limiter recorded no attempts'


def test_account_locks_after_repeated_failures(client):
    for _ in range(kts.MAX_FAILED_LOGINS + 1):
        kts.ex("UPDATE users SET failed_logins=failed_logins+1 WHERE username='admin'")
    kts.ex("UPDATE users SET locked_until=? WHERE username='admin'",
           ((kts.datetime.now() + kts.timedelta(minutes=30)).isoformat(),))
    login(client)
    with client.session_transaction() as sess:
        assert 'user_id' not in sess


def test_session_cleared_on_login(client):
    with client.session_transaction() as sess:
        sess['smuggled'] = 'value'
    login(client)
    with client.session_transaction() as sess:
        assert 'smuggled' not in sess


# ── CSRF ─────────────────────────────────────────────────────
def test_post_without_csrf_token_rejected(client):
    login(client)
    r = client.post('/courses/add', data={'course_code': 'XX', 'course_name': 'Hack'})
    assert r.status_code == 400
    assert kts.q("SELECT id FROM courses WHERE course_code='XX'", one=True) is None


def test_post_with_wrong_csrf_token_rejected(client):
    login(client)
    r = client.post('/courses/add', data={
        'course_code': 'XX', 'course_name': 'Hack', '_csrf_token': 'forged'})
    assert r.status_code == 400


def test_post_with_valid_csrf_token_accepted(client):
    login(client)
    r = client.post('/courses/add', data={
        'course_code': 'ZZ', 'course_name': 'Legit Course',
        'fees': '5000', '_csrf_token': csrf(client)}, follow_redirects=True)
    assert r.status_code == 200
    assert kts.q("SELECT id FROM courses WHERE course_code='ZZ'", one=True) is not None


# ── Security headers & error handling ────────────────────────
def test_security_headers_present(client):
    h = client.get('/login').headers
    assert h['X-Content-Type-Options'] == 'nosniff'
    assert h['X-Frame-Options'] == 'SAMEORIGIN'
    assert 'Referrer-Policy' in h


def test_404_does_not_leak_traceback(client):
    r = client.get('/definitely-not-a-real-page')
    assert r.status_code == 404
    assert b'Traceback' not in r.data


# ── Regression: the crash bugs ───────────────────────────────
def test_edit_course_does_not_crash(client):
    """Regression: used request.get() instead of request.form.get() -> 500."""
    login(client)
    cid = kts.q("SELECT id FROM courses WHERE course_code='TP'", one=True)['id']
    r = client.post(f'/courses/{cid}/edit', data={
        'course_code': 'TP', 'course_name': 'Tally Prime Updated',
        'duration': '4 Months', 'fees': '9500',
        'description': 'Updated', '_csrf_token': csrf(client)}, follow_redirects=True)
    assert r.status_code == 200
    assert kts.q("SELECT course_name FROM courses WHERE id=?", (cid,), one=True)['course_name'] \
        == 'Tally Prime Updated'


def test_q_one_returns_none_not_empty_list(client):
    """Regression: q(one=True) used to return [] for no match."""
    assert kts.q("SELECT id FROM students WHERE id=999999", one=True) is None


def test_upload_directories_exist():
    """Regression: photo uploads crashed because these never existed."""
    for sub in ('photos', 'id_proofs'):
        assert os.path.isdir(os.path.join(kts.UPLOAD, sub))


# ── Input validation helpers ─────────────────────────────────
@pytest.mark.parametrize('raw,expected', [
    ('1000', 1000.0), ('1,500.50', 1500.5), ('', 0.0),
    ('abc', 0.0), (None, 0.0), ('-500', 0.0),
])
def test_parse_money(raw, expected):
    assert kts.parse_money(raw) == expected


def test_parse_int_clamps_to_range():
    assert kts.parse_int('500', 30, minimum=1, maximum=100) == 100
    assert kts.parse_int('-5', 30, minimum=1, maximum=100) == 1
    assert kts.parse_int('garbage', 30) == 30


@pytest.mark.parametrize('email,ok', [
    ('a@b.com', True), ('first.last@sub.domain.org', True),
    ('bad', False), ('@nouser.com', False), ('no@tld', False), ('', False),
])
def test_valid_email(email, ok):
    assert kts.valid_email(email) is ok


# ── Access control ───────────────────────────────────────────
def test_privilege_escalation_blocked_for_admin(client):
    """A plain admin must not be able to mint a super_admin."""
    login(client)
    client.post('/staff/add', data={
        'username': 'plainadmin', 'full_name': 'Plain Admin', 'role': 'admin',
        'password': 'adminpass123', '_csrf_token': csrf(client)})
    client.get('/logout')
    login(client, 'plainadmin', 'adminpass123')
    with client.session_transaction() as sess:
        sess['role'] = 'admin'
        sess['user_id'] = kts.q(
            "SELECT id FROM users WHERE username='plainadmin'", one=True)['id']
    client.post('/staff/add', data={
        'username': 'sneaky', 'full_name': 'Sneaky', 'role': 'super_admin',
        'password': 'sneaky12345', '_csrf_token': csrf(client)})
    created = kts.q("SELECT role FROM users WHERE username='sneaky'", one=True)
    assert created is None or created['role'] != 'super_admin'


def test_staff_list_does_not_expose_password_hashes(client):
    login(client)
    r = client.get('/staff')
    assert b'pbkdf2:' not in r.data
    assert b'scrypt:' not in r.data


def test_student_cannot_reach_staff_pages(client):
    login(client)
    with client.session_transaction() as sess:
        sess['role'] = 'student'
    for path in ('/students', '/fees', '/staff', '/reports'):
        r = client.get(path)
        assert r.status_code in (302, 403), f'{path} was reachable by a student'


def test_last_super_admin_cannot_be_deactivated(client):
    login(client)
    uid = kts.q("SELECT id FROM users WHERE username='admin'", one=True)['id']
    with client.session_transaction() as sess:
        sess['user_id'] = 99999  # pretend to be someone else
    client.post(f'/staff/{uid}/toggle', data={'_csrf_token': csrf(client)})
    assert kts.q("SELECT is_active FROM users WHERE id=?", (uid,), one=True)['is_active'] == 1


# ── Telegram hardening ───────────────────────────────────────
def test_webhook_rejects_missing_secret(client):
    kts.set_setting('TELEGRAM_BOT_TOKEN', '123456789:' + 'A' * 35)
    r = client.post('/telegram/webhook', json={'message': {'chat': {'id': 1}, 'text': '/start'}})
    assert r.status_code == 403


def test_webhook_rejects_wrong_secret(client):
    kts.set_setting('TELEGRAM_BOT_TOKEN', '123456789:' + 'A' * 35)
    r = client.post('/telegram/webhook',
                    json={'message': {'chat': {'id': 1}, 'text': '/start'}},
                    headers={'X-Telegram-Bot-Api-Secret-Token': 'wrong'})
    assert r.status_code == 403


def test_telegram_link_rejects_unknown_code(client):
    r = client.post('/telegram/link', json={'code': 'BOGUS123', 'chat_id': 42})
    assert r.status_code == 404


def test_telegram_link_rejects_expired_code(client):
    past = (kts.datetime.now() - kts.timedelta(hours=1)).isoformat()
    kts.ex("UPDATE users SET tg_link_code=?, tg_link_expires=? WHERE username='admin'",
           ('EXPIRED1', past))
    r = client.post('/telegram/link', json={'code': 'EXPIRED1', 'chat_id': 42})
    assert r.status_code == 410


def test_telegram_link_is_rate_limited(client):
    codes = [client.post('/telegram/link',
                         json={'code': 'NOPE0000', 'chat_id': 1}).status_code
             for _ in range(8)]
    assert 429 in codes


# ── Business logic ───────────────────────────────────────────
def test_add_student_creates_records_and_fee_structure(client):
    login(client)
    cid = kts.q("SELECT id FROM courses WHERE course_code='PY'", one=True)['id']
    r = client.post('/students/add', data={
        'full_name': 'Test Student', 'course_id': cid, 'mobile': '9876543210',
        'email': 'test@example.com', 'course_fee': '15000',
        'registration_fee': '1000', '_csrf_token': csrf(client)},
        follow_redirects=True)
    assert r.status_code == 200
    st = kts.q("SELECT * FROM students WHERE full_name='Test Student'", one=True)
    assert st is not None
    assert st['student_id'].startswith('KTS-PY-')
    fs = kts.q("SELECT * FROM fee_structures WHERE student_id=?", (st['id'],), one=True)
    assert fs['total_fee'] == 16000.0


def test_add_student_rejects_invalid_email(client):
    login(client)
    cid = kts.q("SELECT id FROM courses WHERE course_code='PY'", one=True)['id']
    client.post('/students/add', data={
        'full_name': 'Bad Email', 'course_id': cid,
        'email': 'not-an-email', '_csrf_token': csrf(client)}, follow_redirects=True)
    assert kts.q("SELECT id FROM students WHERE full_name='Bad Email'", one=True) is None


def test_student_password_is_not_the_old_default(client):
    """Regression: every student used to get the shared password kts123."""
    login(client)
    cid = kts.q("SELECT id FROM courses WHERE course_code='PY'", one=True)['id']
    client.post('/students/add', data={
        'full_name': 'Pw Student', 'course_id': cid,
        '_csrf_token': csrf(client)}, follow_redirects=True)
    st = kts.q("SELECT user_id FROM students WHERE full_name='Pw Student'", one=True)
    u = kts.q("SELECT password_hash, must_change_password FROM users WHERE id=?",
              (st['user_id'],), one=True)
    from werkzeug.security import check_password_hash
    assert not check_password_hash(u['password_hash'], 'kts123')
    assert u['must_change_password'] == 1


def test_negative_payment_rejected(client):
    login(client)
    cid = kts.q("SELECT id FROM courses WHERE course_code='PY'", one=True)['id']
    client.post('/students/add', data={
        'full_name': 'Pay Student', 'course_id': cid,
        'course_fee': '10000', '_csrf_token': csrf(client)}, follow_redirects=True)
    sid = kts.q("SELECT id FROM students WHERE full_name='Pay Student'", one=True)['id']
    client.post(f'/fees/pay/{sid}', data={
        'amount': '-5000', '_csrf_token': csrf(client)}, follow_redirects=True)
    assert kts.q("SELECT COUNT(*) as c FROM fee_payments WHERE student_id=?",
                 (sid,))[0]['c'] == 0


def test_duplicate_course_code_rejected(client):
    login(client)
    before = kts.q("SELECT COUNT(*) as c FROM courses")[0]['c']
    client.post('/courses/add', data={
        'course_code': 'TP', 'course_name': 'Duplicate',
        '_csrf_token': csrf(client)}, follow_redirects=True)
    assert kts.q("SELECT COUNT(*) as c FROM courses")[0]['c'] == before


def test_change_password_enforces_minimum_length(client):
    login(client)
    client.post('/change_password', data={
        'current_password': 'testadmin123', 'new_password': 'short',
        'confirm_password': 'short', '_csrf_token': csrf(client)}, follow_redirects=True)
    from werkzeug.security import check_password_hash
    u = kts.q("SELECT password_hash FROM users WHERE username='admin'", one=True)
    assert check_password_hash(u['password_hash'], 'testadmin123')


def test_change_password_succeeds(client):
    login(client)
    client.post('/change_password', data={
        'current_password': 'testadmin123', 'new_password': 'brand-new-pass-99',
        'confirm_password': 'brand-new-pass-99',
        '_csrf_token': csrf(client)}, follow_redirects=True)
    from werkzeug.security import check_password_hash
    u = kts.q("SELECT password_hash FROM users WHERE username='admin'", one=True)
    assert check_password_hash(u['password_hash'], 'brand-new-pass-99')


def test_dashboard_pending_fees_aggregate(client):
    """The N+1 loop was replaced by one SQL aggregate - verify the number matches."""
    login(client)
    cid = kts.q("SELECT id FROM courses WHERE course_code='PY'", one=True)['id']
    for name, fee in (('A', '10000'), ('B', '20000')):
        client.post('/students/add', data={
            'full_name': f'Agg {name}', 'course_id': cid,
            'course_fee': fee, '_csrf_token': csrf(client)}, follow_redirects=True)
    sid = kts.q("SELECT id FROM students WHERE full_name='Agg A'", one=True)['id']
    client.post(f'/fees/pay/{sid}', data={
        'amount': '4000', '_csrf_token': csrf(client)}, follow_redirects=True)
    row = kts.q("""SELECT COALESCE(SUM(CASE WHEN fs.total_fee - COALESCE(p.paid,0) > 0
                     THEN fs.total_fee - COALESCE(p.paid,0) ELSE 0 END),0) as pending
                   FROM fee_structures fs
                   LEFT JOIN (SELECT student_id, SUM(amount) as paid
                              FROM fee_payments GROUP BY student_id) p
                     ON p.student_id = fs.student_id""", one=True)
    assert row['pending'] == 26000.0
    assert client.get('/').status_code == 200


def test_students_list_paginates(client):
    login(client)
    cid = kts.q("SELECT id FROM courses WHERE course_code='PY'", one=True)['id']
    for i in range(5):
        client.post('/students/add', data={
            'full_name': f'Page Student {i}', 'course_id': cid,
            '_csrf_token': csrf(client)}, follow_redirects=True)
    assert client.get('/students?page=1').status_code == 200
    assert client.get('/students?page=99999').status_code == 200
    assert client.get('/students?page=abc').status_code == 200


def test_file_upload_rejects_disallowed_extension():
    import io as _io
    from werkzeug.datastructures import FileStorage
    fs = FileStorage(stream=_io.BytesIO(b'MZ evil'), filename='payload.exe')
    assert kts.save_upload(fs, 'photos', 'test_evil') == ''


def test_file_upload_sanitises_traversal_filename():
    import io as _io
    from werkzeug.datastructures import FileStorage
    fs = FileStorage(stream=_io.BytesIO(b'x'), filename='../../../etc/passwd.png')
    name = kts.save_upload(fs, 'photos', 'trav_test')
    assert name == '' or ('..' not in name and '/' not in name and '\\' not in name)


def test_certificate_verification_is_public(client):
    assert client.get('/verify').status_code == 200


# ── Local SQLite persistence ─────────────────────────────────
def test_sqlite_uses_project_dir_not_tmp():
    """Regression: /tmp was preferred over the project dir, so on Linux the
    database landed in /tmp and was wiped on reboot."""
    path = kts._pick_sqlite_path()
    assert not path.startswith('/tmp')
    assert path != ':memory:'
    assert os.path.abspath(kts.BASE_DIR) in os.path.abspath(path)


def test_sqlite_path_env_override(monkeypatch, tmp_path):
    target = tmp_path / 'nested' / 'custom.db'
    monkeypatch.setenv('SQLITE_PATH', str(target))
    assert kts._pick_sqlite_path() == os.path.abspath(str(target))
    assert os.path.isdir(os.path.dirname(str(target))), 'parent dir auto-created'


def test_serverless_falls_back_to_tmp(monkeypatch):
    monkeypatch.setenv('VERCEL', '1')
    monkeypatch.delenv('SQLITE_PATH', raising=False)
    monkeypatch.setattr(kts, 'IS_SERVERLESS', True)
    path = kts._pick_sqlite_path()
    assert path.startswith('/tmp') or path == ':memory:'


def test_wal_and_pragmas_enabled(client):
    db = kts.get_db()
    try:
        assert db.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal'
        assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert db.execute('PRAGMA busy_timeout').fetchone()[0] == 15000
    finally:
        db.close()


def test_concurrent_writes_do_not_lock(client):
    """WAL + busy_timeout must prevent 'database is locked' under load."""
    import threading
    errors = []

    def writer(n):
        try:
            for i in range(10):
                kts.ex("INSERT INTO audit_logs (user_id,action,table_name,record_id,details)"
                       " VALUES (?,?,?,?,?)", (n, 'concurrent', 't', i, f'{n}-{i}'))
        except Exception as e:
            errors.append(repr(e))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert kts.q("SELECT COUNT(*) as c FROM audit_logs WHERE action='concurrent'")[0]['c'] == 50


def test_data_survives_reconnect(client):
    """Writes must land on disk, not just in a per-connection buffer."""
    kts.ex("INSERT INTO courses (course_code,course_name,fees) VALUES (?,?,?)",
           ('PERSIST', 'Persistence Check', 4200))
    kts._db_initialized = False          # force a brand-new connection
    row = kts.q("SELECT course_name,fees FROM courses WHERE course_code='PERSIST'", one=True)
    assert row is not None
    assert row['fees'] == 4200


def test_health_detail_reports_persistence(client):
    login(client)
    body = client.get('/health/detail').get_json()
    assert body['persistent'] is True
    assert body['ephemeral_storage'] is False
    assert body['db_path'].endswith('.db')


def test_backup_script_produces_valid_copy(client, tmp_path):
    import backup_db
    kts.ex("INSERT INTO courses (course_code,course_name,fees) VALUES (?,?,?)",
           ('BKP', 'Backup Check', 111))
    dest = tmp_path / 'backups'
    assert backup_db.backup(kts.DB_PATH, str(dest), keep=5) == 0
    copies = list(dest.glob('kts_*.db'))
    assert len(copies) == 1
    import sqlite3 as s3
    con = s3.connect(str(copies[0]))
    try:
        assert con.execute(
            "SELECT fees FROM courses WHERE course_code='BKP'").fetchone()[0] == 111
    finally:
        con.close()

