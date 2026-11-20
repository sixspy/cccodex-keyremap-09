"""生成本地 SQLite 夹具：m1 入正式后再推 m2，两种正确性口径各一库。

输出（本目录下）：
  fixture_freeze.db —— 甲路径冻结挂版：推 m2 后旧正式行仍挂 m1，附拒漂留痕
  fixture_follow.db —— 乙路径追随挂版：旧正式行改挂 m2，remap_generation 可查

用法： python build_fixtures.py
"""

import os
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import formal_version_pin as fvp  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent
M2_MAPPINGS = [
    {"old_key": "OLD-A1", "new_key": "NEW-100", "kind": "一对一"},
    {"old_key": "OLD-B1", "new_key": "NEW-201", "kind": "一对一"},
    {"old_key": "OLD-D1", "new_key": "NEW-300", "kind": "一对一"},
]


def build(policy: str, out: Path):
    if out.exists():
        out.unlink()
    conn = sqlite3.connect(out)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE map_versions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, version TEXT UNIQUE NOT NULL,
      status TEXT NOT NULL, effective_from TEXT NOT NULL);
    CREATE TABLE mappings(
      id INTEGER PRIMARY KEY AUTOINCREMENT, version TEXT NOT NULL,
      old_key TEXT NOT NULL, new_key TEXT NOT NULL, kind TEXT NOT NULL);
    CREATE TABLE formal_entities(
      id INTEGER PRIMARY KEY AUTOINCREMENT, entity_key TEXT NOT NULL,
      mapped_from TEXT NOT NULL, version TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE conflicts(
      id INTEGER PRIMARY KEY AUTOINCREMENT, old_key TEXT NOT NULL,
      reason TEXT NOT NULL, status TEXT NOT NULL);
    """)
    conn.executescript(fvp.schema_ddl(policy))
    fvp.init_policy(conn, policy)

    ts1 = "2026-11-10T09:00:00"
    ts2 = "2026-11-20T10:00:00"
    # m1 映册
    conn.execute("INSERT INTO map_versions VALUES(1,'m1','生效','2026-11-01')")
    conn.executemany("INSERT INTO mappings(version,old_key,new_key,kind) VALUES(?,?,?,?)", [
        ("m1", "OLD-A1", "NEW-100", "一对一"),
        ("m1", "OLD-B1", "NEW-200", "一对一"),
    ])
    # m1 期间两行入正式，挂版证据同事务落库
    cur = conn.execute("INSERT INTO formal_entities VALUES(1,'NEW-100','OLD-A1','m1',?)", (ts1,))
    conn.execute("INSERT INTO entity_map_freeze VALUES(1,'NEW-100','m1',?)", (ts1,))
    cur2 = conn.execute("INSERT INTO formal_entities VALUES(2,'NEW-200','OLD-B1','m1',?)", (ts1,))
    conn.execute("INSERT INTO entity_map_freeze VALUES(2,'NEW-200','m1',?)", (ts1,))
    if policy == fvp.POLICY_FOLLOW:
        for rid, key in ((1, "NEW-100"), (2, "NEW-200")):
            conn.execute("INSERT INTO remap_generation(formal_row_id,entity_key,"
                         "old_version,new_version,generation,reason,audit_ts)"
                         " VALUES(?,?,?,?,?,?,?)",
                         (rid, key, "m1", "m1", 1, "入正式挂版", ts1))
    conn.commit()

    # 推 m2（锁内同事务）
    fvp.push_version(conn, policy, "m2", "2026-11-20", M2_MAPPINGS, ts2)
    conn.commit()

    # 夹具断言（硬约束）
    pins = {r["formal_row_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM entity_map_freeze")}
    if policy == fvp.POLICY_FREEZE:
        assert all(p["freeze_version"] == "m1" for p in pins.values()), \
            "冻结架构推版后旧挂版不得变成 m2"
        assert conn.execute(
            "SELECT COUNT(*) c FROM freeze_drift_reject").fetchone()["c"] == 2
        assert conn.execute(
            "SELECT COUNT(*) c FROM remap_generation").fetchone()["c"] == 0
    else:
        assert all(p["freeze_version"] == "m2" for p in pins.values()), \
            "追随架构旧行应改挂 m2"
        gens = conn.execute(
            "SELECT formal_row_id, generation FROM remap_generation ORDER BY id").fetchall()
        assert [dict(g) for g in gens] == [
            {"formal_row_id": 1, "generation": 1},
            {"formal_row_id": 2, "generation": 1},
            {"formal_row_id": 1, "generation": 2},
            {"formal_row_id": 2, "generation": 2},
        ]
        assert conn.execute(
            "SELECT COUNT(*) c FROM freeze_drift_reject").fetchone()["c"] == 0
    conn.close()
    print(f"[ok] {out.name} ({policy})")


if __name__ == "__main__":
    build(fvp.POLICY_FREEZE, FIXTURE_DIR / "fixture_freeze.db")
    build(fvp.POLICY_FOLLOW, FIXTURE_DIR / "fixture_follow.db")
