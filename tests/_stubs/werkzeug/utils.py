import re
def secure_filename(fn):
    fn = str(fn).strip().replace(' ', '_')
    return re.sub(r'[^A-Za-z0-9_.-]', '', fn)
