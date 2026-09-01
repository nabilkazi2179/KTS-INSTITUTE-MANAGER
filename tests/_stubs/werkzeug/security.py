import hashlib
def generate_password_hash(pw, *a, **k):
    return 'sha$' + hashlib.sha256(pw.encode()).hexdigest()
def check_password_hash(h, pw):
    return h == 'sha$' + hashlib.sha256(pw.encode()).hexdigest()
