
import hashlib, secrets, sqlite3, threading
from datetime import datetime
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# === 鉴权零写说明（docs/auth-zero-write-ledger.md）锁定口径 ===
# 甲路径：越权请求服务端拦截并记审计，正式侧零写入。当天锁定，禁止改口混用乙路径。
AUTH_PATH_LOCKED = "甲"
ADMIN_ROLE = "管理员"
READONLY_ROLE = "只读"

# 解冻鉴权锁：把「鉴权判定 + 输出态更新 + 补采队列写入」串行化为一个临界区。
UNFREEZE_LOCK = threading.Lock()
# 元锁保护下面的「在临界区中的源」集合，保证争用判定与登记原子。
_META_LOCK = threading.Lock()
_in_critical: set[str] = set()


class unfreeze_section:
    """串行化同一源解冻，并让后交一路在阻塞取锁前判定争用。"""

    def __init__(self, source: str):
        self.source = source
        self.contended = False

    def __enter__(self):
        with _META_LOCK:
            # 在真正等待操作锁之前判定：该源已有在先解冻在临界区，即争用（后交）。
            self.contended = self.source in _in_critical
            _in_critical.add(self.source)
        UNFREEZE_LOCK.acquire()  # 后交一路在此串行等待在先请求放锁
        return self

    def __exit__(self, exc_type, exc, tb):
        UNFREEZE_LOCK.release()
        with _META_LOCK:
            _in_critical.discard(self.source)
        return False

# 测试/诊断白盒闸门：进入解冻临界区后、落库前回调。生产为空操作。
def unfreeze_gate(source: str, actor: str) -> None:  # noqa: ARG001
    return None


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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

def role_of(conn, username: str) -> str:
    row = conn.execute("SELECT role FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        raise HTTPException(401, "登录已失效")
    return row["role"]

def write_audit(conn, actor: str, action: str, target: str, result: str, contended: bool, note: str = ""):
    """审计落库（auth_audit 属鉴权侧，不属于正式补采队列，不构成业务写入）。"""
    conn.execute(
        "INSERT INTO auth_audit(actor,action,target,result,contended,note,created_at) VALUES (?,?,?,?,?,?,?)",
        (actor, action, target, result, 1 if contended else 0, note, datetime.now().isoformat(timespec="seconds")),
    )

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "源到达SLA"
SKELETON = "多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE NOT NULL,
          sla_minutes INTEGER NOT NULL,
          output_mode TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS arrivals(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          source_clock TEXT NOT NULL,
          ingest_clock TEXT NOT NULL,
          lag_minutes INTEGER NOT NULL,
          level TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS catchups(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          status TEXT NOT NULL,
          restored_mode TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_audit(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          actor TEXT NOT NULL,
          action TEXT NOT NULL,
          target TEXT NOT NULL,
          result TEXT NOT NULL,
          contended INTEGER NOT NULL DEFAULT 0,
          note TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_ledger(
          id INTEGER PRIMARY KEY CHECK (id = 1),
          locked_path TEXT NOT NULL,
          zero_write_rule TEXT NOT NULL,
          pinned_at TEXT NOT NULL
        );
        """)
        # 旧库补列：补采队列须能读出操作人，证明越权不会产生队列行。
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(catchups)").fetchall()}
        if "operator" not in cols:
            conn.execute("ALTER TABLE catchups ADD COLUMN operator TEXT NOT NULL DEFAULT 'admin'")
        if conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0:
            conn.executemany("INSERT INTO sources(name,sla_minutes,output_mode) VALUES (?,?,?)", [
                ("订单源", 15, "可写"), ("支付源", 20, "降级只读"), ("物流源", 30, "冻结"),
            ])
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("支付源","2026-11-16T08:00:00","2026-11-16T08:35:00",35,"告警"))
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("物流源","2026-11-16T07:00:00","2026-11-16T09:10:00",130,"违约"))
        # 钉死选型：专册与库内锁定行一致，当天不得改口。
        if conn.execute("SELECT COUNT(*) c FROM auth_ledger WHERE id=1").fetchone()["c"] == 0:
            conn.execute(
                "INSERT INTO auth_ledger(id,locked_path,zero_write_rule,pinned_at) VALUES (1,?,?,?)",
                (AUTH_PATH_LOCKED, "越权服务端拦截并记审计，正式侧零写入", datetime.now().isoformat(timespec="seconds")),
            )

def level_of(lag, sla):
    if lag <= sla: return "正常"
    if lag <= sla * 2: return "告警"
    return "违约"

def mode_of(level):
    return {"正常":"可写", "告警":"降级只读", "违约":"冻结"}[level]

@app.on_event("startup")
def _s(): init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "sources": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/auth-policy")
def auth_policy(authorization: str | None = Header(default=None)):
    """权限附页读数：锁定路径与当前角色的可写边界，五处对齐的统一来源。"""
    username = require_user(authorization)
    with connect() as c:
        role = role_of(c, username)
        locked = c.execute("SELECT locked_path,zero_write_rule FROM auth_ledger WHERE id=1").fetchone()
    return {
        "locked_path": locked["locked_path"],
        "zero_write_rule": locked["zero_write_rule"],
        "username": username,
        "role": role,
        "can_write": role == ADMIN_ROLE,
        "write_actions": ["解冻", "到达登记"] if role == ADMIN_ROLE else [],
    }

@app.get("/api/sources")
def sources(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM sources ORDER BY id").fetchall()]}

class ArrivalIn(BaseModel):
    source: str
    source_clock: str
    ingest_clock: str

@app.post("/api/arrivals")
def add_arrival(body: ArrivalIn, authorization: str | None = Header(default=None)):
    # 采集写路径同口径：服务端强制，只读令牌零写并留审计。
    username = require_user(authorization)
    with connect() as c:
        actor_role = role_of(c, username)
        if actor_role != ADMIN_ROLE:
            write_audit(c, username, "到达登记", body.source, "越权拦截", False,
                        "只读令牌写采集路径，服务端拦截，正式侧零写入")
            c.commit()  # 拦截分支先落审计，再在上下文外拒绝，避免随异常回滚
            raise HTTPException(403, "只读令牌禁止写入（服务端拦截）")
        src = c.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        def parse(t):
            return datetime.fromisoformat(t)
        lag = int((parse(body.ingest_clock) - parse(body.source_clock)).total_seconds() // 60)
        if lag < 0: lag = 0
        lv = level_of(lag, src["sla_minutes"])
        mode = mode_of(lv)
        c.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                  (body.source, body.source_clock, body.ingest_clock, lag, lv))
        c.execute("UPDATE sources SET output_mode=? WHERE name=?", (mode, body.source))
        write_audit(c, username, "到达登记", body.source, "放行", False)
        return {"lag_minutes": lag, "level": lv, "output_mode": mode}

@app.get("/api/arrivals")
def arrivals(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM arrivals ORDER BY id DESC").fetchall()]}

@app.post("/api/catchup/{source}")
def catchup(source: str, authorization: str | None = Header(default=None)):
    """解冻写路径（甲路径，已锁定）。

    全程持 UNFREEZE_LOCK：鉴权判定 → 输出态更新 → 补采队列写入，串行化。
    只读角色：403 + 审计，正式侧零写入；与管理路并发后交时 contended=1。
    """
    username = require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    with unfreeze_section(source) as section:
        contended = section.contended
        pending_error: HTTPException | None = None
        with connect() as c:
            actor_role = role_of(c, username)
            # 白盒闸门：进入临界区之后、落库之前；并发测试据此让后交一路稳定等锁。
            unfreeze_gate(source, username)
            src = c.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
            if not src:
                write_audit(c, username, "解冻", source, "鉴权偏了", contended, "未知源，拒绝解冻")
                c.commit()
                pending_error = HTTPException(404, "未知源")
            elif actor_role != ADMIN_ROLE:
                # 甲路径：服务端拦截 + 审计，正式侧零写入（不动 output_mode，不写 catchups）。
                write_audit(c, username, "解冻", source, "越权拦截", contended,
                            "只读令牌解冻，服务端拦截，正式侧零写入")
                c.commit()  # 先落审计，避免随 403 回滚
                pending_error = HTTPException(403, "只读令牌禁止解冻（服务端拦截）")
            else:
                c.execute("UPDATE sources SET output_mode='可写' WHERE name=?", (source,))
                c.execute(
                    "INSERT INTO catchups(source,status,restored_mode,note,created_at,operator) VALUES (?,?,?,?,?,?)",
                    (source, "成功", "可写", "补齐后恢复可写", now, username),
                )
                write_audit(c, username, "解冻", source, "放行", contended)
                result = {"ok": True, "output_mode": "可写", "operator": username, "contended": contended}
        if pending_error is not None:
            raise pending_error
        return result

@app.get("/api/catchups")
def catchups(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM catchups ORDER BY id DESC").fetchall()]}

@app.get("/api/audit")
def audit(authorization: str | None = Header(default=None)):
    """审计列表读数；只读与管理均可读，但只读永远不可写。"""
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM auth_audit ORDER BY id DESC").fetchall()]}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
