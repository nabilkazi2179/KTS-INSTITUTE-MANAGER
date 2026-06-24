"""
KTS Institute Management System - Konkan Technology Services
Complete Student Lifecycle Management Application
"""
import os, uuid, json, io, csv, urllib.request, urllib.parse
from datetime import datetime, date, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
    flash, session, jsonify, send_file, abort)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

# ── Telegram Config ──────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_API = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}' if TELEGRAM_BOT_TOKEN else ''

# Also try reading from Hermes .env if not set
if not TELEGRAM_BOT_TOKEN:
    hermes_env = os.path.join(os.path.expanduser('~'), 'AppData', 'Local', 'hermes', '.env')
    if os.path.exists(hermes_env):
        with open(hermes_env) as f:
            for line in f:
                line = line.strip()
                if line.startswith('TELEGRAM_BOT_TOKEN='):
                    TELEGRAM_BOT_TOKEN = line.split('=', 1)[1].strip()
                    TELEGRAM_API = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}'
                    break

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# ── Database Config ──────────────────────────────────────────
DATABASE_URL = os.environ.get('DATABASE_URL', '')
DB_MODE = 'sqlite'
DB_PATH = ':memory:'

if DATABASE_URL and DATABASE_URL not in ('sqlite', ''):
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        DB_MODE = 'postgres'
        print(f'Using PostgreSQL: {DATABASE_URL[:30]}...')
    except ImportError as e:
        import sqlite3
        DB_MODE = 'sqlite'
        DB_PATH = ':memory:'
        print(f'WARNING: psycopg2 import failed: {e}')
        print(f'Falling back to in-memory SQLite')
    except Exception as e:
        import sqlite3
        DB_MODE = 'sqlite'
        DB_PATH = ':memory:'
        print(f'WARNING: Database init failed: {e}')
        print(f'Falling back to in-memory SQLite')
else:
    # SQLite (local development) - use file if writable
    import sqlite3
    DB_MODE = 'sqlite'
    if os.access('/tmp', os.W_OK):
        DB_PATH = '/tmp/kts_institute.db'
    elif os.path.isdir(BASE_DIR) and os.access(BASE_DIR, os.W_OK):
        DB_PATH = os.path.join(BASE_DIR, 'kts_institute.db')
    else:
        DB_PATH = ':memory:'
    print(f'Using SQLite: {DB_PATH}')

def get_db():
    if DB_MODE == 'postgres':
        db = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return db
    else:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        return db

def q(query, args=None, one=False):
    # Convert SQLite ? placeholders to PostgreSQL %s
    if DB_MODE == 'postgres':
        query = query.replace('?', '%s')
    if args is None:
        args = ()
    db = get_db()
    try:
        cur = db.cursor()
        if args:
            cur.execute(query, args)
        else:
            cur.execute(query)
        rows = cur.fetchall()
        if DB_MODE == 'sqlite' and rows:
            result_rows = []
            for r in rows:
                if hasattr(r, 'keys'):
                    result_rows.append(dict(r))
                else:
                    result_rows.append(r)
            rows = result_rows
            if one:
                return rows[0] if rows else None
        return rows[0] if one and rows else rows
    finally:
        db.close()

def ex(query, args=()):
    if DB_MODE == 'postgres':
        query = query.replace('?', '%s')
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(query, args)
        db.commit()
        return cur.lastrowid
    finally:
        db.close()

UPLOAD   = os.path.join(BASE_DIR, 'static', 'uploads')

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'))
app.secret_key = 'kts-secret-key-2026-change-in-production'
app.config['UPLOAD_FOLDER'] = UPLOAD
app.config['MAX_CONTENT_LENGTH'] = 16*1024*1024

ALLOWED = {'png','jpg','jpeg','gif','pdf','doc','docx'}

# ── Init DB ──
import threading
_db_initialized = False
_db_lock = threading.Lock()

def init_db():
    global _db_initialized
    with _db_lock:
        if _db_initialized:
            return
        _db_initialized = True
    
    db = get_db()
    cur = db.cursor()
    
    # Users
    cur.execute('''CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, full_name TEXT NOT NULL,
        email TEXT, phone TEXT, role TEXT NOT NULL DEFAULT 'student',
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Courses
    cur.execute('''CREATE TABLE IF NOT EXISTS courses (
        id SERIAL PRIMARY KEY, course_code TEXT UNIQUE NOT NULL,
        course_name TEXT NOT NULL, duration TEXT, fees REAL DEFAULT 0,
        description TEXT, syllabus TEXT, certificate_template TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Batches
    cur.execute('''CREATE TABLE IF NOT EXISTS batches (
        id SERIAL PRIMARY KEY, batch_name TEXT NOT NULL,
        course_id INTEGER, trainer_id INTEGER, timing TEXT,
        start_date TEXT, end_date TEXT, max_strength INTEGER DEFAULT 30,
        status TEXT DEFAULT 'active',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Students
    cur.execute('''CREATE TABLE IF NOT EXISTS students (
        id SERIAL PRIMARY KEY, student_id TEXT UNIQUE NOT NULL,
        full_name TEXT NOT NULL, guardian_name TEXT, dob TEXT, gender TEXT,
        mobile TEXT, whatsapp TEXT, email TEXT, address TEXT, city TEXT,
        state TEXT, country TEXT DEFAULT 'India', id_number TEXT,
        qualification TEXT, photo TEXT, id_proof TEXT, joining_date TEXT,
        course_id INTEGER, batch_id INTEGER, counselor_id INTEGER,
        remarks TEXT, user_id INTEGER, status TEXT DEFAULT 'active',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Fee Structures
    cur.execute('''CREATE TABLE IF NOT EXISTS fee_structures (
        id SERIAL PRIMARY KEY, student_id INTEGER,
        registration_fee REAL DEFAULT 0, admission_fee REAL DEFAULT 0,
        course_fee REAL DEFAULT 0, exam_fee REAL DEFAULT 0,
        certificate_fee REAL DEFAULT 0, misc_fee REAL DEFAULT 0,
        total_fee REAL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Fee Payments
    cur.execute('''CREATE TABLE IF NOT EXISTS fee_payments (
        id SERIAL PRIMARY KEY, student_id INTEGER,
        receipt_no TEXT, amount REAL NOT NULL,
        payment_method TEXT DEFAULT 'Cash',
        payment_date DATE DEFAULT CURRENT_DATE,
        installment_no INTEGER, remarks TEXT, collected_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Attendance
    cur.execute('''CREATE TABLE IF NOT EXISTS attendance (
        id SERIAL PRIMARY KEY, student_id INTEGER, batch_id INTEGER,
        attendance_date DATE, status TEXT DEFAULT 'present',
        remarks TEXT, marked_by INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Exams
    cur.execute('''CREATE TABLE IF NOT EXISTS exams (
        id SERIAL PRIMARY KEY, exam_name TEXT NOT NULL,
        course_id INTEGER, batch_id INTEGER, exam_date DATE,
        max_marks REAL DEFAULT 100, passing_marks REAL DEFAULT 40,
        exam_type TEXT DEFAULT 'theory',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Exam Results
    cur.execute('''CREATE TABLE IF NOT EXISTS exam_results (
        id SERIAL PRIMARY KEY, exam_id INTEGER, student_id INTEGER,
        theory_marks REAL, practical_marks REAL, total_marks REAL,
        percentage REAL, grade TEXT, status TEXT, remarks TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Certificates
    cur.execute('''CREATE TABLE IF NOT EXISTS certificates (
        id SERIAL PRIMARY KEY, certificate_no TEXT UNIQUE NOT NULL,
        student_id INTEGER, course_id INTEGER, grade TEXT, percentage REAL,
        completion_date DATE, issue_date DATE DEFAULT CURRENT_DATE,
        template_used TEXT, qr_code TEXT, is_revoked INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Trainers
    cur.execute('''CREATE TABLE IF NOT EXISTS trainers (
        id SERIAL PRIMARY KEY, user_id INTEGER,
        qualification TEXT, experience TEXT, salary REAL DEFAULT 0,
        specialization TEXT, joining_date DATE, is_active INTEGER DEFAULT 1)''')
    
    # Audit Logs
    cur.execute('''CREATE TABLE IF NOT EXISTS audit_logs (
        id SERIAL PRIMARY KEY, user_id INTEGER, action TEXT,
        table_name TEXT, record_id INTEGER, details TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Notifications
    cur.execute('''CREATE TABLE IF NOT EXISTS notifications (
        id SERIAL PRIMARY KEY, recipient_id INTEGER,
        recipient_type TEXT, type TEXT, subject TEXT, message TEXT,
        channel TEXT DEFAULT 'system', is_sent INTEGER DEFAULT 0,
        sent_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    db.commit()
    
    # Default admin
    cur2 = db.cursor()
    cur2.execute("SELECT id FROM users WHERE username=?", ('admin',))
    admin = cur2.fetchone()
    if not admin:
        cur2.execute("INSERT INTO users (username,password_hash,full_name,email,role) VALUES (?,?,?,?,?)",
            ('admin', generate_password_hash('admin123'), 'Super Admin', 'admin@kts.com', 'super_admin'))
    
    # Default courses
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
        cur2 = db.cursor()
        cur2.execute("SELECT id FROM courses WHERE course_code=?", (c[0],))
        if not cur2.fetchone():
            cur2.execute("INSERT INTO courses (course_code,course_name,duration,fees,description) VALUES (?,?,?,?,?)", c)
    
    db.commit()
    db.close()
# ── Helpers ──
def allowed_file(fn): return '.' in fn and fn.rsplit('.',1)[1].lower() in ALLOWED

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

@app.context_processor
def g():
    return {'now':datetime.now(),'app_name':'KTS Institute Manager',
            'institute':'Konkan Technology Services',
            'urole':session.get('role',''),'uname':session.get('full_name','')}

# ════════════════════════════════════════════════════════
#  AUTH
# ════════════════════════════════════════════════════════
@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        u=request.form.get('username','').strip(); p=request.form.get('password','')
        if not u or not p: flash('Fill all fields.','danger'); return redirect(url_for('login'))
        user=q("SELECT * FROM users WHERE username=? AND is_active=1",(u,),one=True)
        if user and check_password_hash(user['password_hash'],p):
            session['user_id']=user['id']; session['username']=user['username']
            session['full_name']=user['full_name']; session['role']=user['role']
            session['email']=user['email']
            log(user['id'],'login','users',user['id'])
            flash(f'Welcome, {user["full_name"]}!','success'); return redirect(url_for('dashboard'))
        flash('Invalid credentials.','danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    log(session.get('user_id'),'logout','users',session.get('user_id'))
    session.clear(); flash('Logged out.','info'); return redirect(url_for('login'))

# ════════════════════════════════════════════════════════
#  DASHBOARD
# ════════════════════════════════════════════════════════
@app.route('/')
@login_required
def dashboard():
    ts=q("SELECT COUNT(*) as c FROM students")[0]['c']
    acs=q("SELECT COUNT(*) as c FROM students WHERE status='active'")[0]['c']
    tc=q("SELECT COUNT(*) as c FROM courses WHERE is_active=1")[0]['c']
    tb=q("SELECT COUNT(*) as c FROM batches WHERE status='active'")[0]['c']
    tt=q("SELECT COUNT(*) as c FROM trainers WHERE is_active=1")[0]['c']
    td=date.today().isoformat(); ms=date.today().replace(day=1).isoformat()
    tcol=q("SELECT COALESCE(SUM(amount),0) as s FROM fee_payments")[0]['s'] or 0
    tod=q("SELECT COALESCE(SUM(amount),0) as s FROM fee_payments WHERE payment_date=?",(td,))[0]['s'] or 0
    mon=q("SELECT COALESCE(SUM(amount),0) as s FROM fee_payments WHERE payment_date>=?",(ms,))[0]['s'] or 0
    pf=0
    for fs in q("SELECT student_id,total_fee FROM fee_structures"):
        pf+=max(0,(fs['total_fee'] or 0)-paid_amt(fs['student_id']))
    rs=q("SELECT s.*,c.course_name FROM students s LEFT JOIN courses c ON s.course_id=c.id ORDER BY s.id DESC LIMIT 5")
    if DB_MODE == 'postgres':
        ue=q("SELECT e.*,c.course_name FROM exams e JOIN courses c ON e.course_id=c.id WHERE e.exam_date>=CURRENT_DATE ORDER BY e.exam_date LIMIT 5")
    else:
        ue=q("SELECT e.*,c.course_name FROM exams e JOIN courses c ON e.course_id=c.id WHERE e.exam_date>=date('now') ORDER BY e.exam_date LIMIT 5")
    ci=q("SELECT COUNT(*) as c FROM certificates")[0]['c']
    ce=q("SELECT c.course_name,COUNT(s.id) as count FROM courses c LEFT JOIN students s ON c.id=s.course_id GROUP BY c.id ORDER BY count DESC")
    if DB_MODE == 'postgres':
        mf=q("SELECT TO_CHAR(payment_date,'YYYY-MM') as month,SUM(amount) as total FROM fee_payments GROUP BY month ORDER BY month DESC LIMIT 6")
    else:
        mf=q("SELECT strftime('%Y-%m',payment_date) as month,SUM(amount) as total FROM fee_payments GROUP BY month ORDER BY month DESC LIMIT 6")
    return render_template('dashboard.html',ts=ts,acs=acs,tc=tc,tb=tb,tt=tt,tcol=tcol,tod=tod,mon=mon,pf=pf,ci=ci,rs=rs,ue=ue,ce=ce,mf=mf)

# ════════════════════════════════════════════════════════
#  STUDENTS
# ════════════════════════════════════════════════════════
@app.route('/students')
@login_required
def students_list():
    s=request.args.get('search',''); cf=request.args.get('course',''); sf=request.args.get('status','')
    qr="SELECT s.*,c.course_name FROM students s LEFT JOIN courses c ON s.course_id=c.id WHERE 1=1"; p=[]
    if s: qr+=" AND (s.full_name LIKE ? OR s.student_id LIKE ? OR s.mobile LIKE ?)"; p+=[f'%{s}%']*3
    if cf: qr+=" AND s.course_id=?"; p.append(cf)
    if sf: qr+=" AND s.status=?"; p.append(sf)
    qr+=" ORDER BY s.id DESC"
    return render_template('students_list.html',students=q(qr,p),courses=q("SELECT * FROM courses WHERE is_active=1 ORDER BY course_name"))

@app.route('/students/add',methods=['GET','POST'])
@login_required
@role_required('super_admin','admin','counselor')
def add_student():
    cl=q("SELECT * FROM courses WHERE is_active=1 ORDER BY course_name")
    bl=q("SELECT b.*,c.course_name FROM batches b JOIN courses c ON b.course_id=c.id WHERE b.status='active'")
    co=q("SELECT * FROM users WHERE role IN ('counselor','admin','super_admin') AND is_active=1")
    if request.method=='POST':
        fn=request.form.get('full_name','').strip(); cid=request.form.get('course_id')
        if not fn or not cid: flash('Name & Course required.','danger'); return redirect(url_for('add_student'))
        cr=q("SELECT course_code FROM courses WHERE id=?",(cid,),one=True)
        sid=gen_student_id(cr['course_code']); ph=''; ip=''
        if 'photo' in request.files:
            f=request.files['photo']
            if f and f.filename and allowed_file(f.filename):
                ph=f"{sid}_photo.{f.filename.rsplit('.',1)[1].lower()}"
                f.save(os.path.join(UPLOAD,'photos',secure_filename(ph)))
        if 'id_proof' in request.files:
            f=request.files['id_proof']
            if f and f.filename and allowed_file(f.filename):
                ip=f"{sid}_id.{f.filename.rsplit('.',1)[1].lower()}"
                f.save(os.path.join(UPLOAD,'id_proofs',secure_filename(ip)))
        suid=None
        em=request.form.get('email','').strip()
        if em:
            suid=ex("INSERT INTO users (username,password_hash,full_name,email,phone,role) VALUES (?,?,?,?,?,?)",
                (sid,generate_password_hash('kts123'),fn,em,request.form.get('mobile',''),'student'))
        iid=ex("INSERT INTO students (student_id,full_name,guardian_name,dob,gender,mobile,whatsapp,email,address,city,state,country,id_number,qualification,photo,id_proof,joining_date,course_id,batch_id,counselor_id,remarks,user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid,fn,request.form.get('guardian_name',''),request.form.get('dob',''),request.form.get('gender',''),request.form.get('mobile',''),request.form.get('whatsapp',''),em,request.form.get('address',''),request.form.get('city',''),request.form.get('state',''),request.form.get('country','India'),request.form.get('id_number',''),request.form.get('qualification',''),ph,ip,request.form.get('joining_date',date.today().isoformat()),cid,request.form.get('batch_id') or None,request.form.get('counselor_id') or None,request.form.get('remarks',''),suid))
        rf=float(request.form.get('registration_fee',0) or 0)
        af=float(request.form.get('admission_fee',0) or 0)
        cf2=float(request.form.get('course_fee',0) or 0)
        ef=float(request.form.get('exam_fee',0) or 0)
        cef=float(request.form.get('certificate_fee',0) or 0)
        mf=float(request.form.get('misc_fee',0) or 0)
        tot=rf+af+cf2+ef+cef+mf
        ex("INSERT INTO fee_structures (student_id,registration_fee,admission_fee,course_fee,exam_fee,certificate_fee,misc_fee,total_fee) VALUES (?,?,?,?,?,?,?,?)",(iid,rf,af,cf2,ef,cef,mf,tot))
        log(session['user_id'],'create','students',iid,f'Admitted {sid}')
        flash(f'Student admitted! ID: {sid}','success'); return redirect(url_for('view_student',id=iid))
    return render_template('add_student.html',courses=cl,batches=bl,counselors=co)

@app.route('/students/<int:id>')
@login_required
def view_student(id):
    st=q("SELECT s.*,c.course_name,c.duration,c.course_code FROM students s LEFT JOIN courses c ON s.course_id=c.id WHERE s.id=?",(id,),one=True)
    if not st: abort(404)
    fs=q("SELECT * FROM fee_structures WHERE student_id=?",(id,),one=True)
    pay=q("SELECT * FROM fee_payments WHERE student_id=? ORDER BY payment_date DESC",(id,))
    tp=paid_amt(id); tf=fs['total_fee'] if fs else 0; pn=max(0,tf-tp)
    ar=q("SELECT * FROM attendance WHERE student_id=? ORDER BY attendance_date DESC LIMIT 30",(id,))
    rr=q("SELECT er.*,e.exam_name,e.exam_date FROM exam_results er JOIN exams e ON er.exam_id=e.id WHERE er.student_id=? ORDER BY e.exam_date DESC",(id,))
    ct=q("SELECT * FROM certificates WHERE student_id=? ORDER BY id DESC",(id,))
    ap=0
    if ar: ap=round(sum(1 for a in ar if a['status']=='present')/len(ar)*100)
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

# ════════════════════════════════════════════════════════
#  COURSES
# ════════════════════════════════════════════════════════
@app.route('/courses')
@login_required
def courses():
    return render_template('courses.html',courses=q("SELECT * FROM courses ORDER BY course_name"))

@app.route('/courses/add',methods=['POST'])
@login_required
@role_required('super_admin','admin')
def add_course():
    ex("INSERT INTO courses (course_code,course_name,duration,fees,description) VALUES (?,?,?,?,?)",
        (request.form.get('course_code','').upper(),request.form.get('course_name',''),request.form.get('duration',''),float(request.form.get('fees',0)),request.form.get('description','')))
    flash('Course added!','success'); return redirect(url_for('courses'))

@app.route('/courses/<int:id>/edit',methods=['POST'])
@login_required
@role_required('super_admin','admin')
def edit_course(id):
    ex("UPDATE courses SET course_code=?,course_name=?,duration=?,fees=?,description=? WHERE id=?",
        (request.form.get('course_code','').upper(),request.form.get('course_name',''),request.form.get('duration',''),float(request.form.get('fees',0)),request.form.get('description',''),id))
    flash('Course updated!','success'); return redirect(url_for('courses'))

@app.route('/courses/<int:id>/toggle',methods=['POST'])
@login_required
@role_required('super_admin','admin')
def toggle_course(id):
    ex("UPDATE courses SET is_active=CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE id=?",(id,))
    return redirect(url_for('courses'))

# ════════════════════════════════════════════════════════
#  BATCHES
# ════════════════════════════════════════════════════════
@app.route('/batches')
@login_required
def batches():
    return render_template('batches.html',batches=q("SELECT b.*,c.course_name,(SELECT COUNT(*) FROM students WHERE batch_id=b.id) as strength FROM batches b JOIN courses c ON b.course_id=c.id ORDER BY b.id DESC"))

@app.route('/batches/get/<int:course_id>')
@login_required
def get_batches(course_id):
    return jsonify([dict(b) for b in q("SELECT id,batch_name FROM batches WHERE course_id=? AND status='active'",(course_id,))])

@app.route('/batches/add',methods=['POST'])
@login_required
@role_required('super_admin','admin')
def add_batch():
    ex("INSERT INTO batches (batch_name,course_id,trainer_id,timing,start_date,end_date,max_strength,status) VALUES (?,?,?,?,?,?,?,?)",
        (request.form.get('batch_name',''),request.form.get('course_id'),request.form.get('trainer_id') or None,request.form.get('timing',''),request.form.get('start_date',''),request.form.get('end_date',''),int(request.form.get('max_strength',30)),'active'))
    flash('Batch created!','success'); return redirect(url_for('batches'))

# ════════════════════════════════════════════════════════
#  FEES
# ════════════════════════════════════════════════════════
@app.route('/fees')
@login_required
def fees():
    fd=q("SELECT s.id,s.student_id,s.full_name,s.mobile,c.course_name,fs.total_fee,COALESCE((SELECT SUM(amount) FROM fee_payments WHERE student_id=s.id),0) as paid FROM students s LEFT JOIN courses c ON s.course_id=c.id LEFT JOIN fee_structures fs ON fs.student_id=s.id WHERE s.status='active' ORDER BY s.full_name")
    tp=sum(max(0,(f['total_fee'] or 0)-f['paid']) for f in fd); tc=sum(f['paid'] for f in fd)
    return render_template('fees.html',fee_data=fd,tp=tp,tc=tc)

@app.route('/fees/pay/<int:student_id>',methods=['POST'])
@login_required
@role_required('super_admin','admin','accountant')
def record_payment(student_id):
    amt=float(request.form.get('amount',0))
    if amt<=0: flash('Amount > 0 required.','danger'); return redirect(url_for('view_student',id=student_id))
    rcp=gen_receipt()
    ex("INSERT INTO fee_payments (student_id,receipt_no,amount,payment_method,installment_no,remarks,collected_by) VALUES (?,?,?,?,?,?,?)",
        (student_id,rcp,amt,request.form.get('payment_method','Cash'),int(request.form.get('installment_no',0) or 0),request.form.get('remarks',''),session['user_id']))
    log(session['user_id'],'fee_payment','fee_payments',student_id,f'Rs.{amt} - {rcp}')
    flash(f'Payment recorded! Receipt: {rcp}','success'); return redirect(url_for('view_student',id=student_id))

@app.route('/fees/receipt/<int:payment_id>')
@login_required
def view_receipt(payment_id):
    p=q("SELECT fp.*,s.full_name,s.student_id,s.mobile,s.address,c.course_name FROM fee_payments fp JOIN students s ON fp.student_id=s.id LEFT JOIN courses c ON s.course_id=c.id WHERE fp.id=?",(payment_id,),one=True)
    return render_template('receipt.html',payment=p)

# ════════════════════════════════════════════════════════
#  ATTENDANCE
# ════════════════════════════════════════════════════════
@app.route('/attendance',methods=['GET','POST'])
@login_required
@role_required('super_admin','admin','trainer','counselor')
def attendance():
    bl=q("SELECT b.id,b.batch_name,c.course_name FROM batches b JOIN courses c ON b.course_id=c.id WHERE b.status='active'")
    ds=request.args.get('date',date.today().isoformat()); bid=request.args.get('batch_id','')
    if request.method=='POST' and request.form.get('batch_id'):
        bid=request.form.get('batch_id'); ds=request.form.get('date',date.today().isoformat())
        for att in q("SELECT id FROM students WHERE batch_id=? AND status='active'",(bid,)):
            st=request.form.get(f'status_{att["id"]}','absent')
            exi=q("SELECT id FROM attendance WHERE student_id=? AND batch_id=? AND attendance_date=?",(att['id'],bid,ds),one=True)
            if exi: ex("UPDATE attendance SET status=? WHERE id=?",(st,exi['id']))
            else: ex("INSERT INTO attendance (student_id,batch_id,attendance_date,status,marked_by) VALUES (?,?,?,?,?)",(att['id'],bid,ds,st,session['user_id']))
        flash('Attendance saved!','success'); return redirect(url_for('attendance',batch_id=bid,date=ds))
    sib=[]
    if bid: sib=q("SELECT s.id,s.full_name,s.student_id,a.status as att_status FROM students s LEFT JOIN attendance a ON a.student_id=s.id AND a.batch_id=? AND a.attendance_date=? WHERE s.batch_id=? AND s.status='active'",(bid,ds,bid))
    return render_template('attendance.html',batches=bl,selected_batch=bid,date=ds,students=sib)

# ════════════════════════════════════════════════════════
#  EXAMS
# ════════════════════════════════════════════════════════
@app.route('/exams')
@login_required
def exams():
    return render_template('exams.html',exams=q("SELECT e.*,c.course_name,(SELECT COUNT(*) FROM exam_results WHERE exam_id=e.id) as results_entered FROM exams e JOIN courses c ON e.course_id=c.id ORDER BY e.exam_date DESC"))

@app.route('/exams/add',methods=['POST'])
@login_required
@role_required('super_admin','admin','trainer')
def add_exam():
    ex("INSERT INTO exams (exam_name,course_id,batch_id,exam_date,max_marks,passing_marks,exam_type) VALUES (?,?,?,?,?,?,?)",
        (request.form.get('exam_name',''),request.form.get('course_id'),request.form.get('batch_id') or None,request.form.get('exam_date',''),float(request.form.get('max_marks',100)),float(request.form.get('passing_marks',40)),request.form.get('exam_type','theory')))
    flash('Exam created!','success'); return redirect(url_for('exams'))

@app.route('/exams/<int:eid>/results',methods=['GET','POST'])
@login_required
@role_required('super_admin','admin','trainer')
def exam_results(eid):
    exm=q("SELECT e.*,c.course_name FROM exams e JOIN courses c ON e.course_id=c.id WHERE e.id=?",(eid,),one=True)
    if not exm: abort(404)
    if request.method=='POST':
        for st in q("SELECT id FROM students WHERE course_id=? AND status='active'",(exm['course_id'],)):
            th=float(request.form.get(f'theory_{st["id"]}',0) or 0)
            pr=float(request.form.get(f'practical_{st["id"]}',0) or 0)
            tot=th+pr; mm=exm['max_marks']; pct=round(tot/mm*100,2) if mm else 0
            if pct>=75: gr='A+'; sr='Distinction'
            elif pct>=60: gr='A'; sr='First Class'
            elif pct>=50: gr='B'; sr='Second Class'
            elif pct>=40: gr='C'; sr='Pass'
            else: gr='F'; sr='Fail'
            exi=q("SELECT id FROM exam_results WHERE exam_id=? AND student_id=?",(eid,st['id']),one=True)
            if exi: ex("UPDATE exam_results SET theory_marks=?,practical_marks=?,total_marks=?,percentage=?,grade=?,status=? WHERE id=?",(th,pr,tot,pct,gr,sr,exi['id']))
            else: ex("INSERT INTO exam_results (exam_id,student_id,theory_marks,practical_marks,total_marks,percentage,grade,status) VALUES (?,?,?,?,?,?,?,?)",(eid,st['id'],th,pr,tot,pct,gr,sr))
        flash('Results saved!','success'); return redirect(url_for('exam_results',eid=eid))
    studs=q("SELECT s.id,s.full_name,s.student_id,er.theory_marks,er.practical_marks,er.total_marks,er.percentage,er.grade,er.status FROM students s LEFT JOIN exam_results er ON er.student_id=s.id AND er.exam_id=? WHERE s.course_id=? AND s.status='active'",(eid,exm['course_id']))
    return render_template('exam_results.html',exam=exm,students=studs)

# ════════════════════════════════════════════════════════
#  CERTIFICATES
# ════════════════════════════════════════════════════════
@app.route('/certificates')
@login_required
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
    ap=round(r['avg_pct'] or 0,2)
    gr='A+' if ap>=75 else 'A' if ap>=60 else 'B' if ap>=50 else 'C' if ap>=40 else 'F'
    cn=gen_cert_no()
    ex("INSERT INTO certificates (certificate_no,student_id,course_id,grade,percentage,completion_date) VALUES (?,?,?,?,?,?)",(cn,sid,st['course_id'],gr,ap,date.today().isoformat()))
    log(session['user_id'],'generate_certificate','certificates',sid,cn)
    flash(f'Certificate: {cn}','success'); return redirect(url_for('certificates'))

@app.route('/certificates/view/<int:cid>')
@login_required
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

# ════════════════════════════════════════════════════════
#  TRAINERS
# ════════════════════════════════════════════════════════
@app.route('/trainers')
@login_required
def trainers():
    tr=q("SELECT t.*,u.full_name,u.email,u.phone FROM trainers t JOIN users u ON t.user_id=u.id ORDER BY u.full_name")
    return render_template('trainers.html',trainers=tr)

@app.route('/trainers/add',methods=['POST'])
@login_required
@role_required('super_admin','admin')
def add_trainer():
    un=request.form.get('email','').replace('@','_').replace('.','_') or f"trainer_{uuid.uuid4().hex[:6]}"
    uid=ex("INSERT INTO users (username,password_hash,full_name,email,phone,role) VALUES (?,?,?,?,?,?)",
        (un,generate_password_hash('trainer123'),request.form.get('full_name',''),request.form.get('email',''),request.form.get('phone',''),'trainer'))
    ex("INSERT INTO trainers (user_id,qualification,experience,salary,specialization,joining_date) VALUES (?,?,?,?,?,?)",
        (uid,request.form.get('qualification',''),request.form.get('experience',''),float(request.form.get('salary',0)),request.form.get('specialization',''),request.form.get('joining_date',date.today().isoformat())))
    flash('Trainer added!','success'); return redirect(url_for('trainers'))

# ════════════════════════════════════════════════════════
#  STAFF / USERS
# ════════════════════════════════════════════════════════
@app.route('/staff')
@login_required
@role_required('super_admin','admin')
def staff():
    users=q("SELECT * FROM users ORDER BY full_name")
    return render_template('staff.html',users=users)

@app.route('/staff/add',methods=['POST'])
@login_required
@role_required('super_admin','admin')
def add_staff():
    un=request.form.get('username','').strip()
    if not un: un=request.form.get('email','').replace('@','_').replace('.','_')
    ex("INSERT INTO users (username,password_hash,full_name,email,phone,role) VALUES (?,?,?,?,?,?)",
        (un,generate_password_hash('kts123'),request.form.get('full_name',''),request.form.get('email',''),request.form.get('phone',''),request.form.get('role','staff')))
    flash('Staff added!','success'); return redirect(url_for('staff'))

# ════════════════════════════════════════════════════════
#  REPORTS
# ════════════════════════════════════════════════════════
@app.route('/reports')
@login_required
def reports():
    return render_template('reports.html')

@app.route('/reports/admissions')
@login_required
def report_admissions():
    period=request.args.get('period','monthly')
    if period=='daily':
        if DB_MODE == 'postgres':
            rows=q("SELECT DATE(created_at) as period,COUNT(*) as count FROM students GROUP BY period ORDER BY period DESC LIMIT 30")
        else:
            rows=q("SELECT date(created_at) as period,COUNT(*) as count FROM students GROUP BY period ORDER BY period DESC LIMIT 30")
    else:
        if DB_MODE == 'postgres':
            rows=q("SELECT TO_CHAR(created_at,'YYYY-MM') as period,COUNT(*) as count FROM students GROUP BY period ORDER BY period DESC LIMIT 12")
        else:
            rows=q("SELECT strftime('%Y-%m',created_at) as period,COUNT(*) as count FROM students GROUP BY period ORDER BY period DESC LIMIT 12")
    return render_template('report_admissions.html',rows=rows,period=period)

@app.route('/reports/fees')
@login_required
def report_fees():
    rows=q("SELECT s.student_id,s.full_name,c.course_name,fs.total_fee,COALESCE((SELECT SUM(amount) FROM fee_payments WHERE student_id=s.id),0) as paid,fs.total_fee-COALESCE((SELECT SUM(amount) FROM fee_payments WHERE student_id=s.id),0) as pending FROM students s JOIN fee_structures fs ON fs.student_id=s.id LEFT JOIN courses c ON s.course_id=c.id WHERE s.status='active' ORDER BY pending DESC")
    return render_template('report_fees.html',rows=rows)

@app.route('/reports/export/<rtype>')
@login_required
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

# ════════════════════════════════════════════════════════
#  STUDENT PORTAL
# ════════════════════════════════════════════════════════
@app.route('/my-portal')
@login_required
@role_required('student')
def student_portal():
    st=q("SELECT s.*,c.course_name,c.duration FROM students s LEFT JOIN courses c ON s.course_id=c.id WHERE s.user_id=?",(session['user_id'],),one=True)
    if not st: flash('No student record found.','warning'); return redirect(url_for('dashboard'))
    fs=q("SELECT * FROM fee_structures WHERE student_id=?",(st['id'],),one=True)
    pay=q("SELECT * FROM fee_payments WHERE student_id=? ORDER BY payment_date DESC",(st['id'],))
    tp=paid_amt(st['id']); tf=fs['total_fee'] if fs else 0
    ar=q("SELECT * FROM attendance WHERE student_id=? ORDER BY attendance_date DESC LIMIT 30",(st['id'],))
    rr=q("SELECT er.*,e.exam_name FROM exam_results er JOIN exams e ON er.exam_id=e.id WHERE er.student_id=? ORDER BY e.exam_date DESC",(st['id'],))
    ct=q("SELECT * FROM certificates WHERE student_id=? ORDER BY id DESC",(st['id'],))
    return render_template('student_portal.html',student=st,fs=fs,pay=pay,tp=tp,tf=tf,pn=max(0,tf-tp),ar=ar,rr=rr,ct=ct)

# ════════════════════════════════════════════════════════
#  TELEGRAM BOT INTEGRATION
# ════════════════════════════════════════════════════════

def tg_send(chat_id, text, parse_mode='HTML'):
    """Send a message via Telegram Bot API"""
    if not TELEGRAM_API:
        return False
    try:
        data = json.dumps({'chat_id': chat_id, 'text': text, 'parse_mode': parse_mode}).encode()
        req = urllib.request.Request(f'{TELEGRAM_API}/sendMessage', data=data, headers={'Content-Type': 'application/json'})
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read()).get('ok', False)
    except Exception as e:
        print(f'Telegram send error: {e}')
        return False

def tg_send_to_admin(text):
    """Send notification to all admin users"""
    admins = q("SELECT * FROM users WHERE role IN ('super_admin','admin') AND is_active=1")
    for a in admins:
        if a.get('phone'):
            tg_send(a['phone'], text)

def tg_notify_fee(student_name, amount, receipt_no):
    msg = f'&#128176; <b>Fee Received</b>\n\nStudent: {student_name}\nAmount: &#8377;{amount:,.0f}\nReceipt: {receipt_no}'
    tg_send_to_admin(msg)

def tg_notify_admission(student_name, student_id, course):
    msg = f'&#127381; <b>New Admission</b>\n\nName: {student_name}\nID: {student_id}\nCourse: {course}'
    tg_send_to_admin(msg)

def tg_notify_certificate(student_name, cert_no, course):
    msg = f'&#127942; <b>Certificate Generated</b>\n\nStudent: {student_name}\nCourse: {course}\nCert No: {cert_no}'
    tg_send_to_admin(msg)

@app.route('/telegram/webhook', methods=['POST'])
def telegram_webhook():
    """Handle incoming Telegram messages"""
    if not TELEGRAM_BOT_TOKEN:
        return jsonify({'ok': False, 'error': 'Telegram not configured'})
    
    data = request.get_json(force=True)
    message = data.get('message', {})
    chat_id = message.get('chat', {}).get('id')
    text = message.get('text', '').strip()
    user_id = message.get('from', {}).get('id')
    
    if not chat_id or not text:
        return jsonify({'ok': True})
    
    # Check if user is authorized
    user = q("SELECT * FROM users WHERE phone=? AND is_active=1", (str(user_id),), one=True)
    if not user:
        tg_send(chat_id, '&#10060; You are not authorized. Please contact the admin.')
        return jsonify({'ok': True})
    
    role = user['role']
    cmd = text.lower().split()[0] if text else ''
    
    if cmd == '/start':
        tg_send(chat_id, f'&#127979; <b>Welcome to KTS Institute Manager!</b>\n\nHello {user["full_name"]}!\n\nAvailable commands:\n/students - List all students\n/fees - Fee status\n/courses - List courses\n/batches - List batches\n/certificates - List certificates\n/reports - View reports\n/help - Show help')
    
    elif cmd == '/help':
        tg_send(chat_id, '<b>&#128203; KTS Bot Commands</b>\n\n/students - List students\n/student [ID] - View student details\n/fees - Fee summary\n/courses - List courses\n/batches - List batches\n/certificates - List certificates\n/reports - Reports dashboard\n/notify [message] - Send notification')
    
    elif cmd == '/students':
        students = q("SELECT student_id, full_name, course_id FROM students WHERE status='active' ORDER BY id DESC LIMIT 10")
        if students:
            lines = ['<b>&#127891; Recent Students</b>\n']
            for s in students:
                course = q("SELECT course_name FROM courses WHERE id=?", (s['course_id'],), one=True)
                cname = course['course_name'] if course else 'N/A'
                lines.append(f"&#8226; <b>{s['full_name']}</b> ({s['student_id']}) - {cname}")
            tg_send(chat_id, '\n'.join(lines))
        else:
            tg_send(chat_id, 'No students found.')
    
    elif cmd == '/fees':
        total_collected = q("SELECT COALESCE(SUM(amount),0) as s FROM fee_payments")[0]['s'] or 0
        total_pending = 0
        for fs in q("SELECT student_id, total_fee FROM fee_structures"):
            paid = paid_amt(fs['student_id'])
            total_pending += max(0, (fs['total_fee'] or 0) - paid)
        active_students = q("SELECT COUNT(*) as c FROM students WHERE status='active'")[0]['c']
        tg_send(chat_id, f'<b>&#128176; Fee Summary</b>\n\nActive Students: {active_students}\nTotal Collected: &#8377;{total_collected:,.0f}\nTotal Pending: &#8377;{total_pending:,.0f}')
    
    elif cmd == '/courses':
        courses = q("SELECT * FROM courses WHERE is_active=1 ORDER BY course_name")
        if courses:
            lines = ['<b>&#128218; Active Courses</b>\n']
            for c in courses:
                lines.append(f"&#8226; <b>{c['course_name']}</b> ({c['course_code']}) - &#8377;{c['fees'] or 0:,.0f}")
            tg_send(chat_id, '\n'.join(lines))
        else:
            tg_send(chat_id, 'No courses found.')
    
    elif cmd == '/batches':
        batches = q("SELECT b.*, c.course_name, (SELECT COUNT(*) FROM students WHERE batch_id=b.id) as strength FROM batches b JOIN courses c ON b.course_id=c.id WHERE b.status='active' ORDER BY b.id DESC LIMIT 10")
        if batches:
            lines = ['<b>&#128203; Active Batches</b>\n']
            for b in batches:
                lines.append(f"&#8226; <b>{b['batch_name']}</b> - {b['course_name']} ({b['strength']}/{b['max_strength']})")
            tg_send(chat_id, '\n'.join(lines))
        else:
            tg_send(chat_id, 'No active batches.')
    
    elif cmd == '/certificates':
        certs = q("SELECT cert.*, s.full_name, c.course_name FROM certificates cert JOIN students s ON cert.student_id=s.id JOIN courses c ON cert.course_id=c.id ORDER BY cert.id DESC LIMIT 10")
        if certs:
            lines = ['<b>&#127942; Recent Certificates</b>\n']
            for c in certs:
                lines.append(f"&#8226; <b>{c['full_name']}</b> - {c['course_name']}\n  Cert: {c['certificate_no']} | Grade: {c['grade']}")
            tg_send(chat_id, '\n'.join(lines))
        else:
            tg_send(chat_id, 'No certificates issued yet.')
    
    elif cmd == '/reports':
        total_students = q("SELECT COUNT(*) as c FROM students")[0]['c']
        active = q("SELECT COUNT(*) as c FROM students WHERE status='active'")[0]['c']
        total_courses = q("SELECT COUNT(*) as c FROM courses WHERE is_active=1")[0]['c']
        total_certs = q("SELECT COUNT(*) as c FROM certificates")[0]['c']
        tg_send(chat_id, f'<b>&#128200; KTS Institute Report</b>\n\nTotal Students: {total_students}\nActive Students: {active}\nActive Courses: {total_courses}\nCertificates Issued: {total_certs}')
    
    elif cmd.startswith('/student'):
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            sid = parts[1].strip()
            st = q("SELECT s.*, c.course_name FROM students s LEFT JOIN courses c ON s.course_id=c.id WHERE s.student_id=? OR s.full_name LIKE ?", (sid, f'%{sid}%'), one=True)
            if st:
                fs = q("SELECT * FROM fee_structures WHERE student_id=?", (st['id'],), one=True)
                paid = paid_amt(st['id'])
                total = fs['total_fee'] if fs else 0
                pending = max(0, total - paid)
                tg_send(chat_id, f'<b>&#127891; Student Details</b>\n\nName: {st["full_name"]}\nID: {st["student_id"]}\nCourse: {st["course_name"] or "N/A"}\nMobile: {st["mobile"] or "N/A"}\nStatus: {st["status"]}\n\n&#128176; Fee: &#8377;{paid:,.0f} / &#8377;{total:,.0f}\nPending: &#8377;{pending:,.0f}')
            else:
                tg_send(chat_id, 'Student not found.')
        else:
            tg_send(chat_id, 'Usage: /student [ID or Name]')
    
    elif cmd == '/notify':
        if role in ('super_admin', 'admin'):
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                tg_send_to_admin(f'&#128227; <b>Admin Notification</b>\n\n{parts[1]}')
                tg_send(chat_id, '&#10004; Notification sent to all admins.')
            else:
                tg_send(chat_id, 'Usage: /notify [message]')
        else:
            tg_send(chat_id, '&#10060; Only admins can send notifications.')
    
    else:
        tg_send(chat_id, 'Unknown command. Type /help for available commands.')
    
    return jsonify({'ok': True})

# ════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════
if __name__=='__main__':
    init_db()
    # Auto-start Telegram webhook
    if TELEGRAM_BOT_TOKEN:
        try:
            webhook_url = f'http://localhost:5000/telegram/webhook'
            req = urllib.request.urlopen(f'{TELEGRAM_API}/setWebhook?url={webhook_url}&drop_pending_updates=true', timeout=10)
            wh_result = json.loads(req.read())
            if wh_result.get('ok'):
                print(f'Telegram webhook set: {webhook_url}')
            else:
                print(f'Telegram webhook failed: {wh_result}')
        except Exception as e:
            print(f'Telegram webhook error: {e}')
            print('Set TELEGRAM_BOT_TOKEN in .env to enable Telegram integration')
    print('='*50)
    print('KTS Institute Manager')
    print('Konkan Technology Services')
    print('='*50)
    print('Running at: http://localhost:5000')
    print('Login: admin / admin123')
    print('='*50)
    app.run(debug=True,host='0.0.0.0',port=5000)

# ── Auto-init DB on every cold start (for Vercel serverless) ──
_db_ready = False

@app.before_request
def ensure_db():
    global _db_ready
    if not _db_ready:
        init_db()
        _db_ready = True

# ── Error handlers for debugging ─────────────────────────────
import traceback as _traceback
import sys

@app.route('/health')
def health():
    init_db()
    try:
        courses = q("SELECT * FROM courses")
        admin = q("SELECT id, username FROM users WHERE username=?", ('admin',), one=True)
        return jsonify({'status': 'ok', 'db_mode': DB_MODE, 'db_path': DB_PATH, 'courses_count': len(courses), 'admin': admin, 'sample_course': courses[0] if courses else None})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e), 'db_mode': DB_MODE, 'db_path': DB_PATH})

@app.errorhandler(500)
def internal_error(error):
    error_trace = _traceback.format_exc()
    sys.stderr.write(f'500 ERROR: {error_trace}\n')
    return '<h1>500 Error</h1><p>' + str(error) + '</p><pre style="background:#f8f8f8;padding:16px;overflow:auto;font-size:12px">' + error_trace + '</pre>', 500

@app.errorhandler(Exception)
def handle_exception(error):
    error_trace = _traceback.format_exc()
    sys.stderr.write(f'EXCEPTION: {error_trace}\n')
    return '<h1>' + type(error).__name__ + '</h1><p>' + str(error) + '</p><pre style="background:#f8f8f8;padding:16px;overflow:auto;font-size:12px">' + error_trace + '</pre>', 500
