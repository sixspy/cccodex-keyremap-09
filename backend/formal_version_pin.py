"""formal_version_pin — 正式实体挂版证据与推版正确性口径。

两种口径按库级 pin_policy 二选一，单库内永不混用：

* freeze（甲·冻结挂版）：推版不触碰已正式行的挂版，同事务写拒漂留痕
  （freeze_drift_reject）。entity_map_freeze 附触发器，UPDATE/DELETE 直接 ABORT。
* follow（乙·追随挂版）：推版可把旧行改挂新版，但必须同事务追加
  remap_generation 审计行；任一步失败整体回滚，不存在无审计的改挂。

所有 entity_map_freeze 写操作必须经 locked_write() 拿 BEGIN IMMEDIATE 显式锁；
抢锁失败一律写 write_contention 留痕后抛 PinContention，禁止无声争用。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime

POLICY_FREEZE = "freeze"
POLICY_FOLLOW = "follow"
POLICIES = (POLICY_FREEZE, POLICY_FOLLOW)

# 写锁等待窗口：超时即判定并发抢写，留痕后拒绝，而不是无限排队。
BUSY_TIMEOUT_MS = 150
# 留痕连接的等待窗口：等持锁线程把事务走完后补写争用记录。
CONTENTION_BUSY_TIMEOUT_MS = 5000

SCOPE_FREEZE = "freeze"  # 重映线程写挂版
SCOPE_PUSH = "version_push"  # 推版线程写映册/挂版


class PinContention(Exception):
    """抢写锁失败，争用已落库留痕。"""

    def __init__(self, scope: str, contention_id: int):
        self.scope = scope
        self.contention_id = contention_id
        super().__init__(f"写争用:{scope} contention_id={contention_id}")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def schema_ddl(policy: str) -> str:
    """新建挂版证据/审计/争用/入站表。freeze 口径额外挂不可变触发器。"""
    if policy not in POLICIES:
        raise ValueError(f"未知 pin_policy: {policy}")
    triggers = ""
    if policy == POLICY_FREEZE:
        triggers = """
        CREATE TRIGGER IF NOT EXISTS trg_freeze_pin_no_update
        BEFORE UPDATE ON entity_map_freeze
        BEGIN
            SELECT RAISE(ABORT, '冻结口径下 entity_map_freeze 不可变');
        END;
        CREATE TRIGGER IF NOT EXISTS trg_freeze_pin_no_delete
        BEFORE DELETE ON entity_map_freeze
        BEGIN
            SELECT RAISE(ABORT, '冻结口径下 entity_map_freeze 不可变');
        END;
        """
    return f"""
    CREATE TABLE IF NOT EXISTS app_meta(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS entity_map_freeze(
        formal_row_id INTEGER PRIMARY KEY,
        entity_key TEXT NOT NULL,
        freeze_version TEXT NOT NULL,
        freeze_ts TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS remap_generation(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        formal_row_id INTEGER NOT NULL,
        entity_key TEXT NOT NULL,
        old_version TEXT NOT NULL,
        new_version TEXT NOT NULL,
        generation INTEGER NOT NULL,
        reason TEXT NOT NULL,
        audit_ts TEXT NOT NULL,
        UNIQUE(formal_row_id, generation)
    );
    CREATE TABLE IF NOT EXISTS freeze_drift_reject(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        formal_row_id INTEGER NOT NULL,
        entity_key TEXT NOT NULL,
        pinned_version TEXT NOT NULL,
        pushed_version TEXT NOT NULL,
        reject_ts TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS write_contention(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL,
        op TEXT NOT NULL,
        entity_key TEXT,
        detail TEXT,
        ts TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS inbound_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        old_key TEXT NOT NULL,
        entity_key TEXT,
        used_version TEXT,
        status TEXT NOT NULL,
        detail TEXT,
        ts TEXT NOT NULL
    );
    {triggers}
    """


def init_policy(conn: sqlite3.Connection, policy: str) -> str:
    """建库时定型口径；已存在的库永不被环境变量翻转，防止口径混用。"""
    if policy not in POLICIES:
        raise ValueError(f"未知 pin_policy: {policy}")
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key='pin_policy'"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO app_meta(key, value) VALUES('pin_policy', ?)", (policy,)
        )
        return policy
    return row["value"]


def get_policy(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key='pin_policy'"
    ).fetchone()
    if row is None:
        raise RuntimeError("pin_policy 未初始化，挂版口径缺失")
    if row["value"] not in POLICIES:
        raise RuntimeError(f"非法 pin_policy: {row['value']}")
    return row["value"]


def _record_contention(connect, scope: str, op: str, entity_key, detail) -> int:
    """抢锁失败后的留痕：独立连接等持锁事务结束后补写，保证争用必有痕。"""
    conn = connect()
    try:
        conn.execute(f"PRAGMA busy_timeout={CONTENTION_BUSY_TIMEOUT_MS}")
        cur = conn.execute(
            "INSERT INTO write_contention(scope, op, entity_key, detail, ts)"
            " VALUES(?,?,?,?,?)",
            (scope, op, entity_key, detail, now_iso()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


@contextmanager
def locked_write(connect, scope: str, op: str, entity_key=None, detail=None):
    """所有挂版/映册写操作的唯一入口：BEGIN IMMEDIATE 显式锁。

    抢不到锁 → 留 write_contention 痕迹 → 抛 PinContention，调用方回 409。
    """
    conn = connect()
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        conn.close()
        contention_id = _record_contention(connect, scope, op, entity_key, detail)
        raise PinContention(scope, contention_id)
    try:
        yield conn
    except Exception:
        conn.rollback()
        conn.close()
        raise
    else:
        conn.commit()
        conn.close()


def write_remap(conn: sqlite3.Connection, policy: str, old_key: str,
                mapping: sqlite3.Row, ts: str) -> int:
    """入站重映入正式：正式行 + 挂版证据同事务落库。

    follow 口径额外写 generation=1 审计；freeze 口径不碰审计表。
    """
    cur = conn.execute(
        "INSERT INTO formal_entities(entity_key, mapped_from, version, created_at)"
        " VALUES(?,?,?,?)",
        (mapping["new_key"], old_key, mapping["version"], ts),
    )
    formal_row_id = cur.lastrowid
    conn.execute(
        "INSERT INTO entity_map_freeze(formal_row_id, entity_key, freeze_version, freeze_ts)"
        " VALUES(?,?,?,?)",
        (formal_row_id, mapping["new_key"], mapping["version"], ts),
    )
    if policy == POLICY_FOLLOW:
        conn.execute(
            "INSERT INTO remap_generation(formal_row_id, entity_key, old_version,"
            " new_version, generation, reason, audit_ts) VALUES(?,?,?,?,?,?,?)",
            (formal_row_id, mapping["new_key"], mapping["version"],
             mapping["version"], 1, "入正式挂版", ts),
        )
    conn.execute(
        "INSERT INTO inbound_events(old_key, entity_key, used_version, status, detail, ts)"
        " VALUES(?,?,?,?,?,?)",
        (old_key, mapping["new_key"], mapping["version"], "formal", None, ts),
    )
    return formal_row_id


def push_version(conn: sqlite3.Connection, policy: str, version: str,
                 effective_from: str, mappings: list[dict], ts: str) -> int:
    """推新版映册。调用方必须已持 locked_write 锁，整段同事务。

    freeze：旧挂版一行不改，逐行写拒漂留痕。
    follow：旧挂版改挂新版并逐行追加 remap_generation，失败由调用方整体回滚。
    返回受影响（拒漂/改挂）的已正式行数。
    """
    if conn.execute(
        "SELECT 1 FROM map_versions WHERE version=?", (version,)
    ).fetchone():
        raise ValueError("映册版本已存在")
    if not mappings:
        raise ValueError("映册映射不能为空")

    conn.execute("UPDATE map_versions SET status='历史' WHERE status='生效'")
    conn.execute(
        "INSERT INTO map_versions(version, status, effective_from) VALUES(?,?,?)",
        (version, "生效", effective_from),
    )
    conn.executemany(
        "INSERT INTO mappings(version, old_key, new_key, kind) VALUES(?,?,?,?)",
        [(version, m["old_key"], m["new_key"], m["kind"]) for m in mappings],
    )

    pins = conn.execute(
        "SELECT formal_row_id, entity_key, freeze_version"
        " FROM entity_map_freeze"
    ).fetchall()

    if policy == POLICY_FREEZE:
        # 甲路径：挂版冻结。仅写拒漂痕迹，绝不 UPDATE entity_map_freeze。
        conn.executemany(
            "INSERT INTO freeze_drift_reject(formal_row_id, entity_key,"
            " pinned_version, pushed_version, reject_ts) VALUES(?,?,?,?,?)",
            [(p["formal_row_id"], p["entity_key"], p["freeze_version"], version, ts)
             for p in pins],
        )
    else:
        # 乙路径：追随改挂，挂版 UPDATE 与 generation 审计必须同生共死。
        for p in pins:
            generation = conn.execute(
                "SELECT COALESCE(MAX(generation), 0) + 1 AS g"
                " FROM remap_generation WHERE formal_row_id=?",
                (p["formal_row_id"],),
            ).fetchone()["g"]
            conn.execute(
                "UPDATE entity_map_freeze SET freeze_version=?, freeze_ts=?"
                " WHERE formal_row_id=?",
                (version, ts, p["formal_row_id"]),
            )
            conn.execute(
                "INSERT INTO remap_generation(formal_row_id, entity_key, old_version,"
                " new_version, generation, reason, audit_ts) VALUES(?,?,?,?,?,?,?)",
                (p["formal_row_id"], p["entity_key"], p["freeze_version"],
                 version, generation, "推版追随改挂", ts),
            )
            conn.execute(
                "UPDATE formal_entities SET version=? WHERE id=?",
                (version, p["formal_row_id"]),
            )
    return len(pins)


def list_pins(conn: sqlite3.Connection) -> list[dict]:
    """挂版证据只读核证接口：正式行列值与冻结证据并排，便于对漂。"""
    rows = conn.execute(
        """
        SELECT f.formal_row_id, f.entity_key, f.freeze_version, f.freeze_ts,
               fe.mapped_from, fe.version AS formal_column_version,
               (SELECT MAX(generation) FROM remap_generation g
                  WHERE g.formal_row_id = f.formal_row_id) AS generation
        FROM entity_map_freeze f
        JOIN formal_entities fe ON fe.id = f.formal_row_id
        ORDER BY f.formal_row_id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def audit_flow(conn: sqlite3.Connection) -> list[dict]:
    """审计流水：generation 改挂 / drift_reject 拒漂 / contention 争用 统一出口。"""
    items: list[dict] = []
    for r in conn.execute(
        "SELECT * FROM remap_generation ORDER BY id"
    ).fetchall():
        d = dict(r)
        d["kind"] = "generation"
        d["ts"] = d.pop("audit_ts")
        d["detail"] = d.pop("reason")
        items.append(d)
    for r in conn.execute(
        "SELECT * FROM freeze_drift_reject ORDER BY id"
    ).fetchall():
        d = dict(r)
        d["kind"] = "drift_reject"
        d["ts"] = d.pop("reject_ts")
        d["old_version"] = d.pop("pinned_version")
        d["new_version"] = d.pop("pushed_version")
        d["detail"] = "拒漂：挂版保持原版本"
        items.append(d)
    for r in conn.execute(
        "SELECT * FROM write_contention ORDER BY id"
    ).fetchall():
        d = dict(r)
        d["kind"] = "contention"
        d["ts"] = d.pop("ts")
        items.append(d)
    items.sort(key=lambda x: (x["ts"], x.get("id", 0)))
    return items


def reconciliation(conn: sqlite3.Connection) -> dict:
    """版级对账卡：按入正式瞬间挂版证据口径核对映册与正式区。"""
    policy = get_policy(conn)
    by_version = [
        dict(r)
        for r in conn.execute(
            """
            SELECT v.version,
                   (SELECT COUNT(*) FROM formal_entities fe
                      WHERE fe.version = v.version) AS formal_col_rows,
                   (SELECT COUNT(*) FROM entity_map_freeze f
                      WHERE f.freeze_version = v.version) AS pinned_rows
            FROM map_versions v ORDER BY v.id
            """
        ).fetchall()
    ]
    total_formal = conn.execute(
        "SELECT COUNT(*) c FROM formal_entities"
    ).fetchone()["c"]
    total_pins = conn.execute(
        "SELECT COUNT(*) c FROM entity_map_freeze"
    ).fetchone()["c"]
    unpinned = conn.execute(
        "SELECT COUNT(*) c FROM formal_entities fe"
        " LEFT JOIN entity_map_freeze f ON f.formal_row_id = fe.id"
        " WHERE f.formal_row_id IS NULL"
    ).fetchone()["c"]
    drift = conn.execute(
        "SELECT COUNT(*) c FROM formal_entities fe"
        " JOIN entity_map_freeze f ON f.formal_row_id = fe.id"
        " WHERE f.freeze_version != fe.version"
    ).fetchone()["c"]
    generations = conn.execute(
        "SELECT COUNT(*) c FROM remap_generation"
    ).fetchone()["c"]
    rejects = conn.execute(
        "SELECT COUNT(*) c FROM freeze_drift_reject"
    ).fetchone()["c"]
    contentions = conn.execute(
        "SELECT COUNT(*) c FROM write_contention"
    ).fetchone()["c"]
    inbound = conn.execute(
        "SELECT COUNT(*) c FROM inbound_events"
    ).fetchone()["c"]

    checks = [
        {"name": "正式行均有挂版证据", "ok": unpinned == 0,
         "detail": f"unpinned={unpinned}"},
        {"name": "挂版证据与正式列无漂移", "ok": drift == 0,
         "detail": f"drift={drift}"},
        {"name": "正式行与证据行数相等", "ok": total_formal == total_pins,
         "detail": f"formal={total_formal} pins={total_pins}"},
    ]
    if policy == POLICY_FREEZE:
        checks.append({
            "name": "冻结口径无追随审计写入", "ok": generations == 0,
            "detail": f"generations={generations}",
        })
    else:
        checks.append({
            "name": "追随口径无拒漂写入", "ok": rejects == 0,
            "detail": f"drift_rejects={rejects}",
        })
    return {
        "policy": policy,
        "balanced": all(x["ok"] for x in checks),
        "by_version": by_version,
        "total_formal": total_formal,
        "total_pins": total_pins,
        "unpinned_rows": unpinned,
        "drift_rows": drift,
        "audit_generations": generations,
        "drift_rejects": rejects,
        "contention_events": contentions,
        "inbound_events": inbound,
        "checks": checks,
    }
