"""鉴权面补充测试：采集写越权、策略读数、可读性、未登录区分、正式写仍可用。"""
import pytest
from fastapi.testclient import TestClient

from app import app, init_db, SESSIONS, AUTH_PATH_LOCKED, UNFREEZE_LOCK
import app as app_module


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    SESSIONS.clear()
    init_db()
    yield
    SESSIONS.clear()


def _login(client, username, password):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    h = {"Authorization": "Bearer " + r.json()["token"]}
    return h, r.json()["role"]


def test_locked_path_is_jia_and_lock_exists():
    assert AUTH_PATH_LOCKED == "甲"
    assert UNFREEZE_LOCK is app_module.UNFREEZE_LOCK
    # 锁定行已落库，与专册一致
    with app_module.connect() as c:
        row = c.execute("SELECT locked_path FROM auth_ledger WHERE id=1").fetchone()
    assert row["locked_path"] == "甲"


def test_viewer_arrival_write_blocked_zero_write():
    c = TestClient(app)
    admin, _ = _login(c, "admin", "123456")
    viewer, _ = _login(c, "viewer", "view123")
    before = len(c.get("/api/arrivals", headers=viewer).json()["items"])

    r = c.post("/api/arrivals", headers=viewer, json={
        "source": "订单源", "source_clock": "2026-11-16T10:00:00", "ingest_clock": "2026-11-16T11:00:00"})
    assert r.status_code == 403
    # 正式侧零写入：arrivals 不增，输出态不变
    after = c.get("/api/arrivals", headers=admin).json()["items"]
    assert len(after) == before
    modes = {s["name"]: s["output_mode"] for s in c.get("/api/sources", headers=admin).json()["items"]}
    assert modes["订单源"] == "可写"
    audit = c.get("/api/audit", headers=admin).json()["items"]
    assert any(a["action"] == "到达登记" and a["result"] == "越权拦截" and a["actor"] == "viewer" for a in audit)


def test_admin_arrival_and_catchup_still_write():
    c = TestClient(app)
    admin, _ = _login(c, "admin", "123456")
    r = c.post("/api/arrivals", headers=admin, json={
        "source": "订单源", "source_clock": "2026-11-16T10:00:00", "ingest_clock": "2026-11-16T11:00:00"})
    assert r.status_code == 200 and r.json()["output_mode"] == "冻结"
    r2 = c.post("/api/catchup/订单源", headers=admin)
    assert r2.status_code == 200 and r2.json()["operator"] == "admin"
    catchups = c.get("/api/catchups", headers=admin).json()["items"]
    assert len(catchups) == 1 and catchups[0]["operator"] == "admin"


def test_viewer_can_read_all_five_views_consistent_with_admin():
    c = TestClient(app)
    admin, _ = _login(c, "admin", "123456")
    viewer, _ = _login(c, "viewer", "view123")
    # 管理路先产生一条正式数据与一条审计
    c.post("/api/catchup/物流源", headers=admin)
    c.post("/api/catchup/物流源", headers=viewer)  # 越权，留审计不写正式

    # 两路读数一致：源目录、补采队列、审计、策略、水位（冻结计数走 sources）
    for path in ("/api/sources", "/api/catchups", "/api/audit", "/api/auth-policy"):
        ra, rv = c.get(path, headers=admin), c.get(path, headers=viewer)
        assert ra.status_code == 200 and rv.status_code == 200
        if path != "/api/auth-policy":
            assert ra.json() == rv.json(), f"{path} 只读与管理读数不一致"

    pa, pv = c.get("/api/auth-policy", headers=admin).json(), c.get("/api/auth-policy", headers=viewer).json()
    assert pa["locked_path"] == pv["locked_path"] == "甲"
    assert pa["can_write"] is True and pv["can_write"] is False
    assert pv["write_actions"] == []
    # 补采队列只含管理那条，越权不产生队列行
    catchups = c.get("/api/catchups", headers=viewer).json()["items"]
    assert len(catchups) == 1 and catchups[0]["operator"] == "admin"
    audit = c.get("/api/audit", headers=viewer).json()["items"]
    assert any(a["result"] == "越权拦截" for a in audit)


def test_anonymous_write_is_401_not_403():
    c = TestClient(app)
    assert c.post("/api/catchup/物流源").status_code == 401
    assert c.post("/api/arrivals", json={"source": "订单源",
              "source_clock": "2026-11-16T10:00:00", "ingest_clock": "2026-11-16T11:00:00"}).status_code == 401
    assert c.get("/api/audit").status_code == 401
