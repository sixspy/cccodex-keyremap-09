"""端到端夹具：两种口径各建独立 SQLite，走 m1 入正式 -> 推 m2 -> 再重映。

用 TestClient 模拟真实 HTTP，断言所有只读核证接口与界面所需数据。
"""

import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def run(policy: str):
    tmp = Path(tempfile.mkdtemp(prefix=f"e2e_{policy}_"))
    os.environ["PIN_POLICY"] = policy
    import app as appmod
    appmod.PIN_POLICY = policy
    appmod.DB_PATH = tmp / "e2e.db"
    appmod.init_db()
    c = TestClient(appmod.app, raise_server_exceptions=False)
    h = {"Authorization": "Bearer " + c.post(
        "/api/login", json={"username": "admin", "password": "123456"}
    ).json()["token"]}

    print(f"=== 口径 {policy} ===")
    print("health:", c.get("/api/health").json())

    r1 = c.post("/api/remap", headers=h, json={"old_key": "OLD-B1"})
    print("m1 重映 OLD-B1:", r1.json())

    push = c.post("/api/map-versions/push", headers=h, json={
        "version": "m2", "effective_from": "2026-11-20",
        "mappings": [
            {"old_key": "OLD-A1", "new_key": "NEW-100", "kind": "一对一"},
            {"old_key": "OLD-B1", "new_key": "NEW-201", "kind": "一对一"},
            {"old_key": "OLD-D1", "new_key": "NEW-300", "kind": "一对一"},
        ]})
    print("推 m2:", push.status_code, push.json())

    r2 = c.post("/api/remap", headers=h, json={"old_key": "OLD-D1"})
    print("m2 重映 OLD-D1:", r2.json())

    pins = c.get("/api/formal/pins", headers=h).json()
    print("挂版核证:", pins)
    audit = c.get("/api/audit", headers=h).json()
    print("审计流水条数:", len(audit["items"]))
    for a in audit["items"]:
        print("  ", a["kind"], a.get("entity_key"), a.get("old_version"),
              "->", a.get("new_version"), a.get("detail", ""))
    rec = c.get("/api/reconciliation", headers=h).json()
    print("对账卡 balanced:", rec["balanced"], "checks:",
          [(x["name"], x["ok"]) for x in rec["checks"]])
    inbound = c.get("/api/inbound", headers=h).json()
    print("入站列表:", [(i["old_key"], i["status"]) for i in inbound["items"]])

    # 核心硬约束断言
    old = next(p for p in pins["items"] if p["formal_row_id"] == r1.json()["formal_row_id"])
    if policy == "freeze":
        assert old["freeze_version"] == "m1", "冻结口径旧行不得变成 m2"
        assert rec["drift_rejects"] >= 1 and rec["audit_generations"] == 0
    else:
        assert old["freeze_version"] == "m2", "追随口径旧行应改挂 m2"
        assert old["generation"] == 2 and rec["audit_generations"] >= 2
        assert rec["drift_rejects"] == 0
    assert rec["balanced"] is True
    assert rec["unpinned_rows"] == 0 and rec["drift_rows"] == 0
    print(f"[{policy}] 硬约束全部通过\n")


if __name__ == "__main__":
    policy = sys.argv[1] if len(sys.argv) > 1 else "freeze"
    assert policy in ("freeze", "follow")
    run(policy)
