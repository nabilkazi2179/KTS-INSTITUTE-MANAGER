"""KonkanTech Manager — functional test suite.

Runs app.py's REAL view functions against a REAL sqlite database using a
lightweight Flask/Werkzeug stub (so no web server or Flask install needed).

Run:   python tests/test_app.py
Exit code 0 = all passed, 1 = failures.
"""
import sys, os

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, '_stubs'))   # stub flask/werkzeug
sys.path.insert(0, APP_DIR)                          # the app

DBFILE = os.path.join(HERE, '_test.sqlite')
if os.path.exists(DBFILE): os.remove(DBFILE)
os.environ['ALLOW_SQLITE'] = '1'
os.environ['SECRET_KEY'] = 'test-secret'

import app as A
A.DB_MODE = 'sqlite'; A.DB_PATH = DBFILE
import flask as F

PASS, FAIL = [], []
def check(name, cond, extra=''):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else " FAIL ") + name + (('  -> '+str(extra)) if extra and not cond else ''))

def login_as(username, password):
    F.session.clear()
    F._set_request('POST', form={'username': username, 'password': password})
    A.login()
    return F.session.get('role')

# ---- setup ----
A.init_db()
admin = A.q("SELECT * FROM users WHERE username='admin'", one=True)

print("\n[1] Schema & seed")
tables = {r['name'] for r in A.q("SELECT name FROM sqlite_master WHERE type='table'")}
for t in ['users','courses','expenses','clients','service_docs','service_items','user_permissions','settings','attendance']:
    check(f"table {t} exists", t in tables)
check("admin seeded as super_admin", admin and admin['role']=='super_admin')

print("\n[2] Admin login sees ALL modules")
role = login_as('admin', 'admin123')
check("admin logs in", role=='super_admin')
check("admin session has all modules", set(F.session.get('modules',[]))==set(A.MODULES.keys()))
check("is_admin() true", A.is_admin())
F.session['user_id']=admin['id']; F.session['role']='super_admin'; F.session['full_name']='Admin'
F.session['modules']=list(A.MODULES.keys())

print("\n[3] Admin creates a limited USER (only fees + services)")
F._set_request('POST', form={'full_name':'Ravi Clerk','username':'ravi','password':'pass123','role':'user','modules':['fees','services']})
A.add_staff()
ravi = A.q("SELECT * FROM users WHERE username='ravi'", one=True)
check("user 'ravi' created", bool(ravi))
check("ravi role is 'user'", ravi and ravi['role']=='user')
granted = A.get_user_modules(ravi['id'])
check("ravi granted exactly {fees, services}", granted=={'fees','services'}, granted)

print("\n[4] User login loads only granted modules + permission enforcement")
role = login_as('ravi', 'pass123')
check("ravi logs in", role=='user')
check("ravi session modules == {fees, services}", set(F.session.get('modules',[]))=={'fees','services'}, F.session.get('modules'))
check("is_admin() false for ravi", not A.is_admin())
check("has_module('fees') true", A.has_module('fees'))
check("has_module('services') true", A.has_module('services'))
check("has_module('expenses') FALSE (not granted)", not A.has_module('expenses'))
check("has_module('students') FALSE (not granted)", not A.has_module('students'))
# perm_required should redirect ravi away from expenses
res = A.expenses()
check("ravi blocked from expenses() (redirected)", isinstance(res,str) and 'REDIRECT' in res, res)
# but allowed into fees
res = A.fees()
check("ravi allowed into fees()", isinstance(res,str) and 'RENDERED fees.html' in res, res)

print("\n[5] Admin-only routes blocked for user")
res = A.staff()  # user mgmt
check("ravi blocked from user management", isinstance(res,str) and 'REDIRECT' in res, res)

# back to admin for the rest
F.session.clear(); F.session['user_id']=admin['id']; F.session['role']='super_admin'
F.session['full_name']='Admin'; F.session['modules']=list(A.MODULES.keys())

print("\n[6] Permissions can be changed by admin")
F._set_request('POST', form={'modules':['fees','services','expenses','finance']})
A.set_user_permissions(ravi['id'])
check("ravi now has 4 modules", A.get_user_modules(ravi['id'])=={'fees','services','expenses','finance'})

print("\n[7] edit_course no longer crashes (regression)")
F._set_request('POST', form={'course_code':'TP','course_name':'Tally X','duration':'3M','fees':'8500','description':'x'})
A.edit_course(1)
check("course fees updated to 8500", A.q("SELECT fees FROM courses WHERE id=1", one=True)['fees']==8500)

print("\n[8] Services: quotation -> auto invoice")
F._set_request('POST', form={'name':'Acme','company':'Acme Ltd','email':'a@a.com','phone':'1','address':'Mumbai'})
A.add_client(); cid = A.q("SELECT id FROM clients WHERE name='Acme'", one=True)['id']
F._set_request('POST', form={'client_id':str(cid),'title':'Website','doc_date':'2026-09-01',
    'item_desc[]':['Design','Dev'],'item_qty[]':['1','1'],'item_rate[]':['15000','35000'],'discount':'5000'})
A.new_service_doc('quotation')
doc = A.q("SELECT * FROM service_docs WHERE doc_type='quotation' ORDER BY id DESC LIMIT 1", one=True)
check("quotation total = 45000", doc['total']==45000, doc['total'])
check("doc_no format KTS-QUO-YYYY-####", doc['doc_no'].startswith('KTS-QUO-'))
F._set_request('POST', form={}); A.convert_to_invoice(doc['id'])
inv = A.q("SELECT * FROM service_docs WHERE doc_type='invoice' AND source_doc_id=?", (doc['id'],), one=True)
check("invoice generated from quotation", bool(inv) and inv['total']==45000)
check("quotation marked accepted", A.q("SELECT status FROM service_docs WHERE id=?", (doc['id'],), one=True)['status']=='accepted')
F._set_request('POST', form={}); A.convert_to_invoice(doc['id'])
check("no duplicate invoice on re-convert", A.q("SELECT COUNT(*) c FROM service_docs WHERE source_doc_id=?", (doc['id'],))[0]['c']==1)

print("\n[9] Expenses + Finance")
F._set_request('POST', form={'category':'Rent','amount':'25000','expense_date':'2026-09-01'}); A.add_expense()
F._set_request('POST', form={'category':'Salary','amount':'40000','expense_date':'2026-09-01'}); A.add_expense()
check("expenses total 65000", A.q("SELECT COALESCE(SUM(amount),0) t FROM expenses")[0]['t']==65000)
A.ex("UPDATE service_docs SET status='paid' WHERE doc_type='invoice'")
F._Templates.rendered.clear(); A.finance()
_, ctx = F._Templates.rendered[-1]
check("finance service_income 45000", ctx.get('service_income')==45000, ctx.get('service_income'))
check("finance net = income - expenses", ctx.get('net')==ctx.get('total_income')-ctx.get('total_expenses'))

print("\n[10] Attendance upsert (no duplicates)")
A.ex("INSERT INTO batches (batch_name,course_id,status) VALUES (?,?,?)", ('B1',1,'active'))
bid = A.q("SELECT id FROM batches ORDER BY id DESC LIMIT 1", one=True)['id']
A.ex("INSERT INTO students (student_id,full_name,course_id,batch_id,status) VALUES (?,?,?,?,?)", ('S1','Stud',1,bid,'active'))
sid = A.q("SELECT id FROM students WHERE student_id='S1'", one=True)['id']
F._set_request('POST', form={'batch_id':str(bid),'date':'2026-09-01',f'status_{sid}':'present'}); A.attendance()
F._set_request('POST', form={'batch_id':str(bid),'date':'2026-09-01',f'status_{sid}':'absent'}); A.attendance()
check("attendance single row after re-submit", A.q("SELECT COUNT(*) c FROM attendance WHERE student_id=? AND attendance_date='2026-09-01'", (sid,))[0]['c']==1)
check("attendance status updated to absent", A.q("SELECT status FROM attendance WHERE student_id=? AND attendance_date='2026-09-01'", (sid,), one=True)['status']=='absent')

print("\n[11] /health does not pollute courses")
A.health()
check("no __WT__ junk in courses", A.q("SELECT COUNT(*) c FROM courses WHERE course_code='__WT__'")[0]['c']==0)

# ---- summary ----
print("\n" + "="*44)
print(f"PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if os.path.exists(DBFILE): os.remove(DBFILE)
if FAIL:
    print("FAILED:", FAIL); sys.exit(1)
print("ALL TESTS PASSED ✔")
