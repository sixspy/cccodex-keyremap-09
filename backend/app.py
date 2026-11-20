
import hashlib, os, secrets, sqlite3
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

from formal_version_pin import (
    BUSY_TIMEOUT_MS,
    PinContention,
    SCOPE_FREEZE,
    SCOPE_PUSH,
    audit_flow,
    get_policy,
    init_policy,
    list_pins,
    locked_write,
    now_iso,
    push_version,
    reconciliation,
    schema_ddl,
    write_remap,
    POLICY_FREEZE,
    POLICY_FOLLOW,
    POLICIES,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}
# 口径在首次建库时定型（freeze 甲 / follow 乙），建库后不随环境变量翻转。
PIN_POLICY = os.environ.get("PIN_POLICY", POLICY_FREEZE)
if PIN_POLICY not in POLICIES:
    raise RuntimeError(f"PIN_POLICY 仅支持 {POLICIES}，收到 {PIN_POLICY!r}")

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "实体键桥"
SKELETON = "实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS map_versions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT UNIQUE NOT NULL,
          status TEXT NOT NULL,
          effective_from TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mappings(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT NOT NULL,
          old_key TEXT NOT NULL,
          new_key TEXT NOT NULL,
          kind TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formal_entities(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          entity_key TEXT NOT NULL,
          mapped_from TEXT NOT NULL,
          version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conflicts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL
        );
        """)
        # 先建口径/挂版证据/审计/争用/入站表，再定型（建库后口径只读）。
        conn.executescript(schema_ddl(PIN_POLICY))
        policy = init_policy(conn, PIN_POLICY)
        if conn.execute("SELECT COUNT(*) c FROM map_versions").fetchone()["c"] == 0:
            ts = now_iso()
            conn.execute("INSERT INTO map_versions(version,status,effective_from) VALUES (?,?,?)", ("m1","生效","2026-11-01"))
            conn.executemany("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)", [
                ("m1","OLD-A1","NEW-100","一对一"),
                ("m1","OLD-B1","NEW-200","多对一"),
                ("m1","OLD-B2","NEW-200","多对一"),
            ])
            cur = conn.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                         ("NEW-100","OLD-A1","m1","2026-11-10T09:00:00"))
            # 种子正式行同样必须带持久挂版证据。
            conn.execute("INSERT INTO entity_map_freeze(formal_row_id,entity_key,freeze_version,freeze_ts) VALUES (?,?,?,?)",
                         (cur.lastrowid, "NEW-100", "m1", "2026-11-10T09:00:00"))
            if policy == POLICY_FOLLOW:
                conn.execute("INSERT INTO remap_generation(formal_row_id,entity_key,old_version,new_version,generation,reason,audit_ts) VALUES (?,?,?,?,?,?,?)",
                             (cur.lastrowid, "NEW-100", "m1", "m1", 1, "入正式挂版", "2026-11-10T09:00:00"))
            conn.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", ("OLD-C9","无映射","挂起"))
        else:
            # 旧库兼容：为历史正式行补挂版证据，一次写死，杜绝无证据行。
            missing = conn.execute(
                "SELECT fe.id, fe.entity_key, fe.version, fe.created_at FROM formal_entities fe"
                " LEFT JOIN entity_map_freeze f ON f.formal_row_id=fe.id"
                " WHERE f.formal_row_id IS NULL").fetchall()
            for fe in missing:
                conn.execute("INSERT INTO entity_map_freeze(formal_row_id,entity_key,freeze_version,freeze_ts) VALUES (?,?,?,?)",
                             (fe["id"], fe["entity_key"], fe["version"], fe["created_at"]))
        conn.commit()

@app.on_event("startup")
def _s(): init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM mappings").fetchone()["c"]
        policy = get_policy(c)
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON,
            "mappings": n, "db": str(DB_PATH), "pin_policy": policy}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/map-versions")
def versions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM map_versions ORDER BY id").fetchall()]}

@app.get("/api/mappings")
def mappings(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM mappings ORDER BY id").fetchall()]}

class RemapIn(BaseModel):
    old_key: str

class PushMappingIn(BaseModel):
    old_key: str
    new_key: str
    kind: str = "一对一"

class PushIn(BaseModel):
    version: str
    effective_from: str | None = None
    mappings: list[PushMappingIn]

@app.post("/api/remap")
def remap(body: RemapIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    ts = now_iso()
    # 全段在 BEGIN IMMEDIATE 锁内：版本选取、正式行与挂版证据原子写入。
    try:
        with locked_write(connect, SCOPE_FREEZE, "remap", entity_key=body.old_key,
                          detail="重映入正式") as c:
            policy = get_policy(c)
            ver = c.execute("SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1").fetchone()
            if not ver:
                raise HTTPException(404, "无生效映射版本")
            m = c.execute("SELECT * FROM mappings WHERE version=? AND old_key=?", (ver["version"], body.old_key)).fetchone()
            if not m:
                c.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", (body.old_key, "无映射", "挂起"))
                c.execute("INSERT INTO inbound_events(old_key, entity_key, used_version, status, detail, ts) VALUES(?,?,?,?,?,?)",
                          (body.old_key, None, ver["version"], "conflict", "无映射，进冲突队列", ts))
                c.commit()
                raise HTTPException(409, "无映射，已进冲突队列")
            row_id = write_remap(c, policy, body.old_key, m, ts)
            return {"entity_key": m["new_key"], "version": m["version"],
                    "policy": policy, "formal_row_id": row_id,
                    "freeze_version": m["version"], "freeze_ts": ts}
    except PinContention as e:
        raise HTTPException(409, f"映册推版正在写入，重映抢锁失败（争用留痕 #{e.contention_id}）")

@app.post("/api/map-versions/push")
def push(body: PushIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    ts = now_iso()
    try:
        with locked_write(connect, SCOPE_PUSH, "push_version",
                          entity_key=body.version, detail="推版") as c:
            policy = get_policy(c)
            try:
                affected = push_version(
                    c, policy, body.version,
                    body.effective_from or ts[:10],
                    [m.model_dump() for m in body.mappings], ts,
                )
            except ValueError as e:
                raise HTTPException(409, str(e))
            return {"version": body.version, "policy": policy,
                    "affected_formal_rows": affected,
                    "freeze_policy": policy == POLICY_FREEZE}
    except PinContention as e:
        raise HTTPException(409, f"重映线程持锁，推版抢锁失败（争用留痕 #{e.contention_id}）")

@app.get("/api/formal/pins")
def formal_pins(authorization: str | None = Header(default=None)):
    """挂版冻结核证（只读）：正式列版号与冻结证据并排。"""
    require_user(authorization)
    with connect() as c:
        return {"policy": get_policy(c), "items": list_pins(c)}

@app.get("/api/audit")
def audit(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"policy": get_policy(c), "items": audit_flow(c)}

@app.get("/api/reconciliation")
def reconcile(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return reconciliation(c)

@app.get("/api/inbound")
def inbound(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute(
            "SELECT * FROM inbound_events ORDER BY id DESC").fetchall()]}

@app.get("/api/formal")
def formal(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM formal_entities ORDER BY id DESC").fetchall()]}

@app.get("/api/conflicts")
def conflicts(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM conflicts ORDER BY id DESC").fetchall()]}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
