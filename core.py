"""报关三方资料比对核心：建票、资料版本、差异比对、认领/补件/重核/解决、操作留痕。

三方来源：supplier(供应商) / forwarder(货代) / warehouse(仓库)
比对字段：product_name(品名) / quantity(数量) / carton_no(箱单号)

差异以 (shipment_id, field) 为唯一键做 upsert：
资料版本更新、重复提交都不会产生第二条差异。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation

SOURCES = ["supplier", "forwarder", "warehouse"]
SOURCE_LABELS = {
    "supplier": "供应商",
    "forwarder": "货代",
    "warehouse": "仓库",
}

FIELDS = ["product_name", "quantity", "carton_no"]
FIELD_LABELS = {
    "product_name": "品名",
    "quantity": "数量",
    "carton_no": "箱单号",
}

STATUS_OPEN = "open"          # 有差异，待处理
STATUS_CLAIMED = "claimed"    # 已认领
STATUS_RESOLVED = "resolved"  # 已解决

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "customs.db")


# ---------------------------------------------------------------- 归一化比对

def _norm_text(v: str) -> str:
    return re.sub(r"\s+", "", str(v)).casefold()


def _norm_quantity(v: str) -> str:
    """1,000 / 1000 / 1000.00 视为一致；非数字退化为去空格文本比较。"""
    s = str(v).strip().replace(",", "").replace("，", "")
    try:
        return str(Decimal(s).normalize())
    except (InvalidOperation, ValueError):
        return _norm_text(s)


NORMALIZERS = {
    "product_name": _norm_text,      # 忽略空格、大小写差异
    "quantity": _norm_quantity,      # 千分位/小数尾零归一
    "carton_no": _norm_text,         # 忽略空格、大小写
}


def values_match(field: str, raw_values: dict) -> bool:
    """raw_values: {source: raw_value}，全部已提交时返回是否一致。"""
    norms = {NORMALIZERS[field](v) for v in raw_values.values() if str(v).strip() != ""}
    return len(norms) <= 1


# ---------------------------------------------------------------- 数据库

SCHEMA = """
CREATE TABLE IF NOT EXISTS shipment (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ref         TEXT UNIQUE NOT NULL,
    customer    TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id  INTEGER NOT NULL REFERENCES shipment(id),
    source       TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    product_name TEXT,
    quantity     TEXT,
    carton_no    TEXT,
    submitted_by TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL,
    UNIQUE(shipment_id, source)
);

CREATE TABLE IF NOT EXISTS discrepancy (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id      INTEGER NOT NULL REFERENCES shipment(id),
    field            TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open',
    values_json      TEXT NOT NULL DEFAULT '{}',
    owner            TEXT,
    supplement_note  TEXT NOT NULL DEFAULT '',
    is_reconciled    INTEGER NOT NULL DEFAULT 0,
    first_found_at   TEXT NOT NULL,
    last_changed_at  TEXT NOT NULL,
    reconciled_at    TEXT,
    resolved_at      TEXT,
    UNIQUE(shipment_id, field)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id INTEGER NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_document_shipment ON document(shipment_id);
CREATE INDEX IF NOT EXISTS idx_discrepancy_shipment ON discrepancy(shipment_id);
CREATE INDEX IF NOT EXISTS idx_audit_shipment ON audit_log(shipment_id, id);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _log(conn: sqlite3.Connection, shipment_id: int, actor: str, action: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO audit_log (shipment_id, actor, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (shipment_id, actor or "系统", action, detail, now()),
    )


# ---------------------------------------------------------------- 查询

def get_shipment(conn: sqlite3.Connection, shipment_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM shipment WHERE id = ?", (shipment_id,)).fetchone()
    if row is None:
        raise LookupError(f"货票 #{shipment_id} 不存在")
    return row


def list_shipments(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM shipment ORDER BY id DESC"))


def list_documents(conn: sqlite3.Connection, shipment_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM document WHERE shipment_id = ? ORDER BY id", (shipment_id,)
    ))


def list_discrepancies(conn: sqlite3.Connection, shipment_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM discrepancy WHERE shipment_id = ? ORDER BY id", (shipment_id,)
    ))


def list_logs(conn: sqlite3.Connection, shipment_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM audit_log WHERE shipment_id = ? ORDER BY id", (shipment_id,)
    ))


def discrepancy_values(row: sqlite3.Row) -> dict:
    return json.loads(row["values_json"])


# ---------------------------------------------------------------- 建票 / 资料

def create_shipment(conn: sqlite3.Connection, ref: str, customer: str = "",
                    description: str = "", actor: str = "") -> int:
    ref = ref.strip()
    if not ref:
        raise ValueError("货票编号不能为空")
    cur = conn.execute(
        "INSERT INTO shipment (ref, customer, description, created_at) VALUES (?, ?, ?, ?)",
        (ref, customer.strip(), description.strip(), now()),
    )
    sid = cur.lastrowid
    _log(conn, sid, actor, "shipment_created", f"创建货票 {ref}")
    conn.commit()
    return sid


def submit_document(conn: sqlite3.Connection, shipment_id: int, source: str,
                    product_name: str, quantity: str, carton_no: str,
                    actor: str = "") -> dict:
    """录入或更新某方资料。

    - 首次：version=1；重复提交（三个字段与现行版本完全一致）：忽略，不升版本、不重算。
    - 有变化：version+1（同一行覆盖更新），随后重新比对（差异按字段 upsert）。
    """
    if source not in SOURCES:
        raise ValueError(f"未知资料来源：{source}")
    get_shipment(conn, shipment_id)

    fields = {
        "product_name": (product_name or "").strip(),
        "quantity": (quantity or "").strip(),
        "carton_no": (carton_no or "").strip(),
    }
    existing = conn.execute(
        "SELECT * FROM document WHERE shipment_id = ? AND source = ?",
        (shipment_id, source),
    ).fetchone()

    if existing is not None and all(existing[f] == fields[f] for f in FIELDS):
        _log(conn, shipment_id, actor, "doc_duplicate_ignored",
             f"{SOURCE_LABELS[source]}重复提交，内容与 v{existing['version']} 完全一致，已忽略")
        conn.commit()
        return {"version": existing["version"], "duplicate": True, "changed_fields": []}

    changed = [FIELD_LABELS[f] for f in FIELDS
               if existing is None or existing[f] != fields[f]]

    if existing is None:
        conn.execute(
            """INSERT INTO document (shipment_id, source, version, product_name, quantity,
                                     carton_no, submitted_by, submitted_at)
               VALUES (?, ?, 1, ?, ?, ?, ?, ?)""",
            (shipment_id, source, fields["product_name"], fields["quantity"],
             fields["carton_no"], actor, now()),
        )
        _log(conn, shipment_id, actor, "doc_submitted",
             f"{SOURCE_LABELS[source]}提交资料 v1：" + _format_values(fields))
        version = 1
    else:
        version = existing["version"] + 1
        conn.execute(
            """UPDATE document SET version = ?, product_name = ?, quantity = ?, carton_no = ?,
                                   submitted_by = ?, submitted_at = ?
               WHERE shipment_id = ? AND source = ?""",
            (version, fields["product_name"], fields["quantity"], fields["carton_no"],
             actor, now(), shipment_id, source),
        )
        _log(conn, shipment_id, actor, "doc_updated",
             f"{SOURCE_LABELS[source]}资料更新至 v{version}，变更字段：{'、'.join(changed)}；"
             + _format_values(fields))

    conn.commit()
    recompute(conn, shipment_id, actor=actor)
    return {"version": version, "duplicate": False, "changed_fields": changed}


def _format_values(fields: dict) -> str:
    return "，".join(f"{FIELD_LABELS[f]}={fields[f] or '空'}" for f in FIELDS)


# ---------------------------------------------------------------- 比对引擎

def _snapshot(docs_by_source: dict) -> dict:
    """{field: {source: {raw, norm, version, submitted_at}}}，只含已提交且非空的值。"""
    snap = {f: {} for f in FIELDS}
    for src, doc in docs_by_source.items():
        if doc is None:
            continue
        for f in FIELDS:
            raw = doc[f]
            if raw is not None and str(raw).strip() != "":
                snap[f][src] = {
                    "raw": raw,
                    "norm": NORMALIZERS[f](raw),
                    "version": doc["version"],
                    "submitted_at": doc["submitted_at"],
                }
    return snap


def recompute(conn: sqlite3.Connection, shipment_id: int, actor: str = "系统") -> dict:
    """按最新资料版本重算全部字段，差异 upsert 到唯一行。

    返回 {"opened": n, "updated": n, "reconciled": n, "reopened": n}
    """
    get_shipment(conn, shipment_id)
    docs = {r["source"]: r for r in list_documents(conn, shipment_id)}
    snap = _snapshot(docs)
    existing = {r["field"]: r for r in list_discrepancies(conn, shipment_id)}
    stats = {"opened": 0, "updated": 0, "reconciled": 0, "reopened": 0}

    for field in FIELDS:
        present = snap[field]
        distinct_norms = {v["norm"] for v in present.values()}
        mismatching = len(distinct_norms) >= 2
        row = existing.get(field)

        if mismatching:
            values_json = json.dumps(present, ensure_ascii=False)
            if row is None:
                conn.execute(
                    """INSERT INTO discrepancy (shipment_id, field, status, values_json,
                                                is_reconciled, first_found_at, last_changed_at)
                       VALUES (?, ?, 'open', ?, 0, ?, ?)""",
                    (shipment_id, field, values_json, now(), now()),
                )
                _log(conn, shipment_id, actor, "discrepancy_opened",
                     f"发现差异【{FIELD_LABELS[field]}】：{_describe_conflict(field, present)}")
                stats["opened"] += 1
                continue

            changed_values = row["values_json"] != values_json
            conn.execute(
                "UPDATE discrepancy SET values_json = ?, is_reconciled = 0, reconciled_at = NULL, "
                "last_changed_at = ? WHERE id = ?",
                (values_json, now(), row["id"]),
            )
            if row["status"] == STATUS_RESOLVED:
                # 新版本导致已解决的差异再次冲突：重新打开并退回待认领
                conn.execute(
                    "UPDATE discrepancy SET status = 'open', owner = NULL WHERE id = ?",
                    (row["id"],),
                )
                _log(conn, shipment_id, actor, "discrepancy_reopened",
                     f"差异【{FIELD_LABELS[field]}】在新版本中再次冲突，重新打开："
                     + _describe_conflict(field, present))
                stats["reopened"] += 1
            elif changed_values:
                _log(conn, shipment_id, actor, "discrepancy_updated",
                     f"差异【{FIELD_LABELS[field]}】内容随资料版本更新："
                     + _describe_conflict(field, present))
                stats["updated"] += 1
            continue

        # 不再冲突（含补件后改齐）：已有的未解决差异标记"核对一致，待确认"
        if row is not None and row["status"] != STATUS_RESOLVED and not row["is_reconciled"]:
            conn.execute(
                "UPDATE discrepancy SET is_reconciled = 1, reconciled_at = ?, last_changed_at = ? "
                "WHERE id = ?",
                (now(), now(), row["id"]),
            )
            _log(conn, shipment_id, actor, "discrepancy_reconciled",
                 f"重新核对：差异【{FIELD_LABELS[field]}】三方已一致，待负责人确认解决")
            stats["reconciled"] += 1

    _refresh_shipment_status(conn, shipment_id)
    conn.commit()
    return stats


def _describe_conflict(field: str, present: dict) -> str:
    parts = []
    for src in SOURCES:
        if src in present:
            v = present[src]
            parts.append(f"{SOURCE_LABELS[src]}「{v['raw']}」(v{v['version']})")
        else:
            parts.append(f"{SOURCE_LABELS[src]}未提交")
    return "，".join(parts)


def _refresh_shipment_status(conn: sqlite3.Connection, shipment_id: int) -> None:
    unresolved = conn.execute(
        "SELECT COUNT(*) FROM discrepancy WHERE shipment_id = ? AND status != 'resolved'",
        (shipment_id,),
    ).fetchone()[0]
    conn.execute(
        "UPDATE shipment SET status = ? WHERE id = ?",
        (STATUS_RESOLVED if unresolved == 0 else STATUS_OPEN, shipment_id),
    )


# ---------------------------------------------------------------- 认领 / 补件 / 解决

def claim_discrepancy(conn: sqlite3.Connection, discrepancy_id: int, owner: str) -> None:
    owner = (owner or "").strip()
    if not owner:
        raise ValueError("认领人不能为空")
    row = _get_discrepancy(conn, discrepancy_id)
    if row["status"] == STATUS_RESOLVED:
        raise ValueError("差异已解决，无需认领")
    if not row["is_reconciled"] or row["status"] != STATUS_CLAIMED or row["owner"] != owner:
        conn.execute(
            "UPDATE discrepancy SET status = 'claimed', owner = ?, last_changed_at = ? WHERE id = ?",
            (owner, now(), discrepancy_id),
        )
        if row["owner"] != owner or row["status"] != STATUS_CLAIMED:
            _log(conn, row["shipment_id"], owner, "discrepancy_claimed",
                 f"{owner} 认领差异【{FIELD_LABELS[row['field']]}】")
        conn.commit()


def add_supplement(conn: sqlite3.Connection, discrepancy_id: int, note: str,
                   actor: str = "") -> None:
    note = (note or "").strip()
    if not note:
        raise ValueError("补件说明不能为空")
    row = _get_discrepancy(conn, discrepancy_id)
    merged = f"[{actor or '未知'}] {note}"
    new_note = (row["supplement_note"] + "\n" + merged).strip() if row["supplement_note"] else merged
    conn.execute(
        "UPDATE discrepancy SET supplement_note = ?, last_changed_at = ? WHERE id = ?",
        (new_note, now(), discrepancy_id),
    )
    _log(conn, row["shipment_id"], actor, "discrepancy_supplemented",
         f"差异【{FIELD_LABELS[row['field']]}】补件记录：{note}")
    conn.commit()


def resolve_discrepancy(conn: sqlite3.Connection, discrepancy_id: int, actor: str = "") -> None:
    row = _get_discrepancy(conn, discrepancy_id)
    if row["status"] == STATUS_RESOLVED:
        raise ValueError("差异已是已解决状态")
    if not row["is_reconciled"]:
        raise ValueError("重新核对尚未一致，不能标记解决；请先补件并重新核对")
    conn.execute(
        "UPDATE discrepancy SET status = 'resolved', resolved_at = ?, last_changed_at = ? "
        "WHERE id = ?",
        (now(), now(), discrepancy_id),
    )
    _log(conn, row["shipment_id"], actor, "discrepancy_resolved",
         f"差异【{FIELD_LABELS[row['field']]}】由 {actor or '负责人'} 标记已解决")
    _refresh_shipment_status(conn, row["shipment_id"])
    conn.commit()


def _get_discrepancy(conn: sqlite3.Connection, discrepancy_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM discrepancy WHERE id = ?", (discrepancy_id,)).fetchone()
    if row is None:
        raise LookupError(f"差异 #{discrepancy_id} 不存在")
    return row
