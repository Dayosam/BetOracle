"""Cache. Uses Upstash Redis when UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN are set (survives Render restarts),
otherwise a local SQLite file. A cache failure never breaks a request: it just calls the real API."""
import os, json, time, sqlite3, threading, httpx

URL = os.getenv("UPSTASH_REDIS_REST_URL")
TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
MAX_BYTES = 900_000  # skip caching anything bigger than Upstash's request limit
_lock = threading.Lock()
if not URL:
    _db = sqlite3.connect(os.getenv("CACHE_DB", "cache.db"), check_same_thread=False)
    _db.execute("create table if not exists c(k text primary key, v text, exp real)")

def _redis(*cmd):
    r = httpx.post(URL, json=list(cmd), headers={"Authorization": f"Bearer {TOKEN}"}, timeout=8)
    r.raise_for_status()
    return r.json().get("result")

def cached(key, ttl, fn):
    """Return a stored result if still fresh, otherwise call fn() once and store it. Errors are never stored."""
    if URL:
        try:
            v = _redis("GET", key)
            if v is not None: return json.loads(v)
        except Exception: pass
        val = fn()
        try:
            s = json.dumps(val, default=str)
            if len(s) < MAX_BYTES: _redis("SET", key, s, "EX", int(ttl))
        except Exception: pass
        return val
    with _lock:
        row = _db.execute("select v from c where k=? and exp>?", (key, time.time())).fetchone()
    if row: return json.loads(row[0])
    val = fn()
    with _lock:
        _db.execute("insert or replace into c values(?,?,?)", (key, json.dumps(val, default=str), time.time() + ttl)); _db.commit()
    return val

def count_call():
    day = time.strftime("calls:%Y-%m-%d", time.gmtime())
    if URL:
        try:
            n = _redis("INCR", day); _redis("EXPIRE", day, 172800); return int(n)
        except Exception: return 0
    with _lock:
        n = _db.execute("select v from c where k=?", (day,)).fetchone()
        n = int(n[0]) + 1 if n else 1
        _db.execute("insert or replace into c values(?,?,?)", (day, str(n), time.time() + 172800)); _db.commit()
    return n
py