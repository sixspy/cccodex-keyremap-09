"""挂版冻结/追随口径夹具：本地 SQLite，m1 入正式后再推 m2。

每类口径独立建库，单库内只贯彻一种正确性口径，不混用。
"""

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

import app as appmod
from formal_version_pin import (
    CONTENTION_BUSY_TIMEOUT_MS,
    POLICY_FOLLOW,
    POLICY_FREEZE,
)


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    def _make(policy: str):
        db = tmp_path / f"{policy}.db"
        monkeypatch.setenv("PIN_POLICY", policy)
        monkeypatch.setattr(appmod, "PIN_POLICY", policy)
        monkeypatch.setattr(appmod, "DB_PATH", db)
        appmod.init_db()
        client = TestClient(appmod.app, raise_server_exceptions=False)
        headers = {"Authorization": "Bearer " + client.post(
            "/api/login", json={"username": "admin", "password": "123456"}
        ).json()["token"]}
        return client, headers

    return _make


def push_m2(client, headers):
    return client.post("/api/map-versions/push", headers=headers, json={
        "version": "m2",
        "effective_from": "2026-11-20",
        "mappings": [
            {"old_key": "OLD-A1", "new_key": "NEW-100", "kind": "一对一"},
            {"old_key": "OLD-B1", "new_key": "NEW-201", "kind": "一对一"},
            {"old_key": "OLD-D1", "new_key": "NEW-300", "kind": "一对一"},
        ],
    })


def pins_by_row(client, headers):
    items = client.get("/api/formal/pins", headers=headers).json()["items"]
    return {p["formal_row_id"]: p for p in items}


# ---------- 甲路径：冻结挂版 ----------

def test_freeze_policy_keeps_old_row_on_m1_after_push(make_client):
    client, headers = make_client(POLICY_FREEZE)

    # m1 期间入正式
    r = client.post("/api/remap", headers=headers, json={"old_key": "OLD-B1"})
    assert r.status_code == 200
    old_row_id = r.json()["formal_row_id"]
    assert r.json()["freeze_version"] == "m1"

    # 推 m2
    pushed = push_m2(client, headers)
    assert pushed.status_code == 200, pushed.text
    assert pushed.json()["policy"] == POLICY_FREEZE
    assert pushed.json()["affected_formal_rows"] == 2  # 种子行 + 重映行

    pins = pins_by_row(client, headers)
    # 硬约束：冻结架构下推版后旧正式挂版仍是 m1，不得变成 m2
    for pin in pins.values():
        assert pin["freeze_version"] == "m1"
        assert pin["formal_column_version"] == "m1"  # 列与证据无漂
    assert pins[old_row_id]["freeze_version"] == "m1"

    # 仅新重映使用 m2
    r2 = client.post("/api/remap", headers=headers, json={"old_key": "OLD-D1"})
    assert r2.status_code == 200
    assert r2.json()["freeze_version"] == "m2"
    new_pins = pins_by_row(client, headers)
    assert new_pins[r2.json()["formal_row_id"]]["freeze_version"] == "m2"
    # 旧行依然 m1
    assert new_pins[old_row_id]["freeze_version"] == "m1"

    # 拒漂留痕与对账
    audit = client.get("/api/audit", headers=headers).json()["items"]
    rejects = [a for a in audit if a["kind"] == "drift_reject"]
    assert len(rejects) == 2
    assert {a["old_version"] for a in rejects} == {"m1"}
    assert {a["new_version"] for a in rejects} == {"m2"}
    assert not any(a["kind"] == "generation" for a in audit)

    rec = client.get("/api/reconciliation", headers=headers).json()
    assert rec["policy"] == POLICY_FREEZE
    assert rec["balanced"] is True
    assert rec["unpinned_rows"] == 0
    assert rec["drift_rows"] == 0
    assert rec["drift_rejects"] == 2
    assert rec["audit_generations"] == 0


def test_freeze_table_is_immutable(make_client):
    client, headers = make_client(POLICY_FREEZE)
    db = appmod.DB_PATH
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE entity_map_freeze SET freeze_version='m9'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM entity_map_freeze")
    conn.close()
    # 应用层读数不受影响
    pins = client.get("/api/formal/pins", headers=headers).json()["items"]
    assert all(p["freeze_version"] == "m1" for p in pins)


# ---------- 乙路径：追随挂版 ----------

def test_follow_policy_rehangs_with_generation_audit(make_client):
    client, headers = make_client(POLICY_FOLLOW)

    r = client.post("/api/remap", headers=headers, json={"old_key": "OLD-B1"})
    old_row_id = r.json()["formal_row_id"]

    pushed = push_m2(client, headers)
    assert pushed.status_code == 200, pushed.text
    assert pushed.json()["policy"] == POLICY_FOLLOW

    pins = pins_by_row(client, headers)
    # 追随架构：旧行改挂 m2，且 generation 可查
    for pin in pins.values():
        assert pin["freeze_version"] == "m2"
        assert pin["formal_column_version"] == "m2"
        assert pin["generation"] == 2

    audit = client.get("/api/audit", headers=headers).json()["items"]
    gens = [a for a in audit if a["kind"] == "generation"]
    assert len(gens) == 4  # 每行先有入正式 gen1，推版追加 gen2，共 2 行
    row_gens = [g for g in gens if g["formal_row_id"] == old_row_id]
    assert [g["generation"] for g in row_gens] == [1, 2]
    assert [g["new_version"] for g in row_gens] == ["m1", "m2"]
    assert not any(a["kind"] == "drift_reject" for a in audit)

    rec = client.get("/api/reconciliation", headers=headers).json()
    assert rec["balanced"] is True
    assert rec["audit_generations"] == 4
    assert rec["drift_rejects"] == 0


def test_follow_rehang_rolls_back_without_audit_when_audit_write_fails(make_client):
    client, headers = make_client(POLICY_FOLLOW)
    r = client.post("/api/remap", headers=headers, json={"old_key": "OLD-B1"})
    old_row_id = r.json()["formal_row_id"]

    # 注入失败：审计行写入即 ABORT，模拟同事务中审计失败。
    conn = sqlite3.connect(appmod.DB_PATH)
    conn.execute(
        "CREATE TRIGGER fail_generation BEFORE INSERT ON remap_generation"
        " BEGIN SELECT RAISE(ABORT, '审计写入故障'); END")
    conn.commit()
    conn.close()

    resp = push_m2(client, headers)
    assert resp.status_code == 500

    # 挂版与审计一并回滚：仍是 m1，没有任何 gen2 残留
    pins = pins_by_row(client, headers)
    assert pins[old_row_id]["freeze_version"] == "m1"
    assert pins[old_row_id]["formal_column_version"] == "m1"
    conn = sqlite3.connect(appmod.DB_PATH)
    gens = conn.execute(
        "SELECT * FROM remap_generation WHERE formal_row_id=? AND generation>=2",
        (old_row_id,)).fetchall()
    assert gens == []
    # 推版整体回滚：m2 映册不存在、m1 仍生效
    assert conn.execute(
        "SELECT status FROM map_versions WHERE version='m1'").fetchone()[0] == "生效"
    assert conn.execute(
        "SELECT 1 FROM map_versions WHERE version='m2'").fetchone() is None
    conn.close()

    # 故障解除后可正常推版，口径不变
    conn = sqlite3.connect(appmod.DB_PATH)
    conn.execute("DROP TRIGGER fail_generation")
    conn.commit()
    conn.close()
    assert push_m2(client, headers).status_code == 200
    assert pins_by_row(client, headers)[old_row_id]["freeze_version"] == "m2"


# ---------- 并发抢写：后到者记争用 ----------

def _open_holder(db_path):
    # check_same_thread=False：锁由定时器线程在另一线程释放。
    holder = sqlite3.connect(db_path, timeout=0, check_same_thread=False)
    holder.execute(f"PRAGMA busy_timeout={CONTENTION_BUSY_TIMEOUT_MS}")
    holder.execute("BEGIN IMMEDIATE")
    return holder


def test_concurrent_remap_after_push_lock_records_contention(make_client):
    client, headers = make_client(POLICY_FREEZE)
    holder = _open_holder(appmod.DB_PATH)
    try:
        timer = threading.Timer(1.2, holder.rollback)
        timer.start()
        resp = client.post("/api/remap", headers=headers, json={"old_key": "OLD-B1"})
        timer.join()
    finally:
        holder.close()

    assert resp.status_code == 409
    assert "争用留痕" in resp.json()["detail"]

    audit = client.get("/api/audit", headers=headers).json()["items"]
    contends = [a for a in audit if a["kind"] == "contention"]
    assert len(contends) == 1
    assert contends[0]["scope"] == "freeze"
    assert contends[0]["op"] == "remap"
    assert contends[0]["entity_key"] == "OLD-B1"

    # 被拒请求未留半截数据
    rec = client.get("/api/reconciliation", headers=headers).json()
    assert rec["contention_events"] == 1
    assert rec["unpinned_rows"] == 0
    assert rec["balanced"] is True


def test_concurrent_push_after_remap_lock_records_contention(make_client):
    client, headers = make_client(POLICY_FREEZE)
    holder = _open_holder(appmod.DB_PATH)
    try:
        timer = threading.Timer(1.2, holder.rollback)
        timer.start()
        resp = push_m2(client, headers)
        timer.join()
    finally:
        holder.close()

    assert resp.status_code == 409
    audit = client.get("/api/audit", headers=headers).json()["items"]
    contends = [a for a in audit if a["kind"] == "contention"]
    assert len(contends) == 1
    assert contends[0]["scope"] == "version_push"
    # 推版未半写：m2 不存在
    conn = sqlite3.connect(appmod.DB_PATH)
    assert conn.execute(
        "SELECT 1 FROM map_versions WHERE version='m2'").fetchone() is None
    conn.close()


# ---------- 口径定型与只读核证协同 ----------

def test_policy_is_fixed_at_db_creation(make_client):
    client, headers = make_client(POLICY_FREEZE)
    assert client.get("/api/health").json()["pin_policy"] == POLICY_FREEZE
    # 环境变量翻转不影响已建库
    appmod.PIN_POLICY = POLICY_FOLLOW
    appmod.init_db()
    assert client.get("/api/health").json()["pin_policy"] == POLICY_FREEZE
    rec = client.get("/api/reconciliation", headers=headers).json()
    assert rec["policy"] == POLICY_FREEZE


def test_inbound_list_and_pins_readonly_views(make_client):
    client, headers = make_client(POLICY_FREEZE)
    client.post("/api/remap", headers=headers, json={"old_key": "OLD-B1"})
    client.post("/api/remap", headers=headers, json={"old_key": "OLD-ZZ"})
    push_m2(client, headers)

    inbound = client.get("/api/inbound", headers=headers).json()["items"]
    statuses = {(i["old_key"], i["status"]) for i in inbound}
    assert ("OLD-B1", "formal") in statuses
    assert ("OLD-ZZ", "conflict") in statuses

    pins = client.get("/api/formal/pins", headers=headers).json()["items"]
    assert all({"formal_row_id", "entity_key", "freeze_version",
                "freeze_ts", "formal_column_version"} <= set(p) for p in pins)
