"""鉴权解冻失败测（红灯先行，修复后转绿）。

纪律：
- 鉴权失败测：持只读令牌打解冻，必须服务端拦截(403)、正式侧零写入、审计留痕。
  只读写库若成功，本测须红。
- 抢解冻失败测：只读与管理两路同时打同一源解冻，须占鉴权锁串行化，
  后交一路只留「鉴权解冻争用」痕；缺锁或缺争用留痕，本测须红。
"""
import threading

import pytest
from fastapi.testclient import TestClient

from app import app, init_db, SESSIONS
import app as app_module


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    SESSIONS.clear()
    init_db()
    yield
    SESSIONS.clear()


def _headers(client, username, password):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _source_mode(client, headers, name):
    items = client.get("/api/sources", headers=headers).json()["items"]
    return [s for s in items if s["name"] == name][0]["output_mode"]


# ---------- 鉴权失败测：只读令牌解冻必须失败且零写 ----------

def test_viewer_unfreeze_blocked_with_zero_write():
    c = TestClient(app)
    admin = _headers(c, "admin", "123456")
    viewer = _headers(c, "viewer", "view123")

    # 前置：物流源为冻结态，正式侧无补齐记录
    assert _source_mode(c, viewer, "物流源") == "冻结"
    catchups_before = c.get("/api/catchups", headers=viewer).json()["items"]
    assert catchups_before == []

    # 只读令牌打解冻写库
    r = c.post("/api/catchup/物流源", headers=viewer)

    # 服务端必须拒绝，而不是靠前端藏按钮
    assert r.status_code == 403, f"只读令牌解冻未被服务端拦截: {r.status_code} {r.text}"

    # 正式侧零写入：输出态不得被只读令牌改动，补采队列不得新增
    assert _source_mode(c, admin, "物流源") == "冻结", "只读令牌改动了正式侧输出态"
    catchups_after = c.get("/api/catchups", headers=admin).json()["items"]
    assert catchups_after == [], "只读令牌向正式补采队列写入了记录"

    # 拦截必须留审计，且审计可读
    audit = c.get("/api/audit", headers=admin).json()["items"]
    blocked = [a for a in audit if a["action"] == "解冻" and a["result"] == "越权拦截"]
    assert len(blocked) == 1
    row = blocked[0]
    assert row["actor"] == "viewer" and row["target"] == "物流源"
    assert row["contended"] == 0


# ---------- 抢解冻失败测：双端并发须占锁，后交留争用痕 ----------

def test_concurrent_unfreeze_takes_lock_and_records_contention(monkeypatch):
    # 两个线程各用独立 TestClient（各自独立 anyio portal），避免共用单事件循环造成假死锁。
    c_admin = TestClient(app)
    c_viewer = TestClient(app)
    admin = _headers(c_admin, "admin", "123456")
    viewer = _headers(c_viewer, "viewer", "view123")

    # 白盒闸门：管理路进入临界区后阻塞，确保只读路必然“后交”等锁
    entered = threading.Event()
    release = threading.Event()

    def gate(source, actor):
        entered.set()
        assert release.wait(timeout=5), "闸门超时"

    monkeypatch.setattr(app_module, "unfreeze_gate", gate)

    results: dict[str, object] = {}

    def admin_call():
        results["admin"] = c_admin.post("/api/catchup/物流源", headers=admin)

    def viewer_call():
        results["viewer"] = c_viewer.post("/api/catchup/物流源", headers=viewer)

    t_admin = threading.Thread(target=admin_call)
    t_admin.start()
    assert entered.wait(timeout=5), "管理路未进入解冻临界区"

    # 管理路持锁停在 gate；只读路此刻发起，必然阻塞在 UNFREEZE_LOCK 上（后交）。
    t_viewer = threading.Thread(target=viewer_call)
    t_viewer.start()

    # 由独立定时器放闸，避免主线程被只读请求阻塞而无法 release（同步死锁）。
    threading.Timer(0.5, release.set).start()
    t_admin.join(timeout=5)
    t_viewer.join(timeout=5)
    assert not t_admin.is_alive() and not t_viewer.is_alive(), "解冻线程未在时限内结束"

    r_admin = results["admin"]
    r_viewer = results["viewer"]

    # 管理路正常解冻；只读路被拦截
    assert r_admin.status_code == 200, r_admin.text
    assert r_viewer.status_code == 403, r_viewer.text

    # 正式侧只允许管理路写一次
    catchups = c_admin.get("/api/catchups", headers=admin).json()["items"]
    assert len(catchups) == 1, "解冻写路径未串行化，正式侧出现重复/越权写入"
    assert catchups[0]["operator"] == "admin"
    assert _source_mode(c_admin, admin, "物流源") == "可写"

    # 后交一路只记一条争用留痕
    audit = c_admin.get("/api/audit", headers=admin).json()["items"]
    contented = [a for a in audit if a["action"] == "解冻" and a["contended"] == 1]
    assert len(contented) == 1, "缺少鉴权解冻争用留痕（或留痕不止一条）"
    row = contented[0]
    assert row["actor"] == "viewer" and row["result"] == "越权拦截"
