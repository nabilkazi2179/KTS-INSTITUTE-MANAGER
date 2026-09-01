"""Minimal Flask stub — lets the test suite import app.py and CALL view
functions against a real sqlite DB, without needing Flask installed.
This is ONLY used by tests; production uses the real Flask."""
class _Session(dict):
    def clear(self): dict.clear(self)
session = _Session()
class _Files(dict): pass
class _Form(dict):
    def get(self, k, default=None, type=None):
        v = dict.get(self, k, default)
        if type is not None and v is not None:
            try: return type(v)
            except Exception: return default
        return v
    def getlist(self, k):
        v = dict.get(self, k, [])
        return v if isinstance(v, list) else ([v] if v else [])
class _Request:
    def __init__(self):
        self.method='GET'; self.form=_Form(); self.args=_Form(); self.files=_Files(); self._json={}
    def get_json(self, force=False, silent=False): return self._json
request = _Request()
def _set_request(method='GET', form=None, args=None, files=None, json=None):
    request.method=method; request.form=_Form(form or {}); request.args=_Form(args or {})
    request.files=_Files(files or {}); request._json=json or {}
class _Templates: rendered=[]
def render_template(name, **ctx):
    _Templates.rendered.append((name, ctx)); return f"<RENDERED {name}>"
def redirect(loc): return f"<REDIRECT {loc}>"
def url_for(endpoint, **kw):
    return f"/{endpoint}" + ("?"+"&".join(f"{k}={v}" for k,v in kw.items()) if kw else "")
class _Flashes: msgs=[]
def flash(msg, cat='message'): _Flashes.msgs.append((cat,msg))
def get_flashed_messages(**kw): return _Flashes.msgs
def jsonify(*a, **k): return a[0] if len(a)==1 and not k else (k or (a[0] if a else {}))
def send_file(*a, **k): return "<FILE>"
class _AbortError(RuntimeError): pass
def abort(code): raise _AbortError(f"abort({code})")
class Flask:
    def __init__(self, name, template_folder=None, **kw):
        self.name=name; self.secret_key=None; self.config={}
        self.routes={}; self.rules=[]; self._ctx=[]; self._errorhandlers={}; self._before=[]
    def route(self, rule, methods=None, **kw):
        methods=methods or ['GET']
        def deco(f):
            self.routes[f.__name__]=f; self.rules.append((rule,f.__name__,methods)); return f
        return deco
    def context_processor(self, f): self._ctx.append(f); return f
    def before_request(self, f): self._before.append(f); return f
    def errorhandler(self, code):
        def deco(f): self._errorhandlers[code]=f; return f
        return deco
    def run(self, *a, **k): pass
