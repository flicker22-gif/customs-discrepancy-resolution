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
import secrets
import sqlite3
from datetime import datetime, timedelta
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
    deadline    TEXT,
    warn_hours  INTEGER NOT NULL DEFAULT 24,
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
    episode          INTEGER NOT NULL DEFAULT 1,
    first_found_at   TEXT NOT NULL,
    last_changed_at  TEXT NOT NULL,
    reconciled_at    TEXT,
    resolved_at      TEXT,
    UNIQUE(shipment_id, field)
);

-- 供应商受限补件入口：一个令牌只对应一票货、一个来源、若干指定字段
CREATE TABLE IF NOT EXISTS supplement_token (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT UNIQUE NOT NULL,
    shipment_id INTEGER NOT NULL REFERENCES shipment(id),
    source      TEXT NOT NULL,
    fields      TEXT NOT NULL,
    contact     TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    expires_at  TEXT,
    used_count  INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    revoked     INTEGER NOT NULL DEFAULT 0
);

-- 提醒/升级事件：同一差异同一“风险轮次(episode)”同一级别只产生一条，天然防刷屏
CREATE TABLE IF NOT EXISTS reminder_event (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id     INTEGER NOT NULL,
    discrepancy_id  INTEGER,
    level           TEXT NOT NULL,            -- due_soon / overdue
    episode         INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(discrepancy_id, level, episode)
);

-- 事件的通知投递记录（可多条：负责人、关务主管…），失败可重试，与核心流程解耦
CREATE TABLE IF NOT EXISTS notification (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES reminder_event(id),
    channel     TEXT NOT NULL,
    target      TEXT NOT NULL,
    content     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending / sent / failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    sent_at     TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id INTEGER NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- 申报包：申报前冻结的不可变快照。内容整体存在 payload_json 里，
-- 冻结后三方再来新版本也不会改动该包（只新增新的包，从不 UPDATE/DELETE）。
CREATE TABLE IF NOT EXISTS declaration_package (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id     INTEGER NOT NULL REFERENCES shipment(id),
    package_no      INTEGER NOT NULL,
    frozen_by       TEXT NOT NULL DEFAULT '',
    frozen_at       TEXT NOT NULL,
    declared_fields TEXT NOT NULL DEFAULT '',
    open_items      INTEGER NOT NULL DEFAULT 0,
    payload_json    TEXT NOT NULL,
    UNIQUE(shipment_id, package_no)
);

CREATE INDEX IF NOT EXISTS idx_document_shipment ON document(shipment_id);
CREATE INDEX IF NOT EXISTS idx_discrepancy_shipment ON discrepancy(shipment_id);
CREATE INDEX IF NOT EXISTS idx_notification_status ON notification(status);
CREATE INDEX IF NOT EXISTS idx_token_token ON supplement_token(token);
CREATE INDEX IF NOT EXISTS idx_audit_shipment ON audit_log(shipment_id, id);
"""

# 旧库轻量迁移：缺列即补（幂等）
_MIGRATIONS = {
    "shipment": [("deadline", "TEXT"), ("warn_hours", "INTEGER NOT NULL DEFAULT 24")],
    "discrepancy": [("episode", "INTEGER NOT NULL DEFAULT 1")],
}


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_dt(value: str | None) -> datetime | None:
    """兼容 datetime-local（不带时区）与 ISO 字符串。"""
    if not value:
        return None
    value = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except ValueError:
        return None


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
                    description: str = "", actor: str = "",
                    deadline: str | None = None, warn_hours: int | str = 24) -> int:
    ref = ref.strip()
    if not ref:
        raise ValueError("货票编号不能为空")
    dl = parse_dt(deadline)
    if deadline and dl is None:
        raise ValueError("截止时间格式不正确")
    try:
        warn_hours = int(warn_hours)
        if warn_hours <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("提前提醒小时数必须是正整数")
    cur = conn.execute(
        "INSERT INTO shipment (ref, customer, description, deadline, warn_hours, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ref, customer.strip(), description.strip(),
         dl.isoformat(timespec="minutes") if dl else None, warn_hours, now()),
    )
    sid = cur.lastrowid
    detail = f"创建货票 {ref}"
    if dl:
        detail += f"，报关截止 {dl:%Y-%m-%d %H:%M}（提前 {warn_hours} 小时提醒）"
    _log(conn, sid, actor, "shipment_created", detail)
    conn.commit()
    return sid


def set_deadline(conn: sqlite3.Connection, shipment_id: int,
                 deadline: str | None, warn_hours: int | str, actor: str = "") -> None:
    get_shipment(conn, shipment_id)
    dl = parse_dt(deadline)
    if deadline and dl is None:
        raise ValueError("截止时间格式不正确")
    try:
        warn_hours = int(warn_hours)
        if warn_hours <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("提前提醒小时数必须是正整数")
    conn.execute("UPDATE shipment SET deadline = ?, warn_hours = ? WHERE id = ?",
                 (dl.isoformat(timespec="minutes") if dl else None, warn_hours, shipment_id))
    if dl:
        _log(conn, shipment_id, actor, "deadline_set",
             f"设置报关截止 {dl:%Y-%m-%d %H:%M}，提前 {warn_hours} 小时提醒")
    else:
        _log(conn, shipment_id, actor, "deadline_cleared", "清除报关截止时间")
    conn.commit()


def _format_values(fields: dict) -> str:
    return "，".join(f"{FIELD_LABELS[f]}={fields[f] or '空'}" for f in fields)


def _apply_document(conn, shipment_id, source, fields, actor, kind_label="资料"):
    """对某方资料做一次版本化写入（内部全量提交与供应商门户部分提交共用）。

    fields: 本次允许更新的字段白名单 -> 新值；未列入的字段保留原值。
    与现行版本相比白名单内字段都未变 → 判定重复提交，不升版本。
    返回 {"version": int, "duplicate": bool, "changed_fields": [中文标签]}。
    """
    existing = conn.execute(
        "SELECT * FROM document WHERE shipment_id = ? AND source = ?",
        (shipment_id, source),
    ).fetchone()

    target = {}
    for f in FIELDS:
        if f in fields:
            target[f] = (fields[f] or "").strip()
        else:
            target[f] = existing[f] if existing else None

    changed = [FIELD_LABELS[f] for f in FIELDS
               if f in fields and (existing is None or existing[f] != target[f])]

    if existing is not None and not changed:
        return {"version": existing["version"], "duplicate": True, "changed_fields": []}

    if existing is None:
        conn.execute(
            """INSERT INTO document (shipment_id, source, version, product_name, quantity,
                                     carton_no, submitted_by, submitted_at)
               VALUES (?, ?, 1, ?, ?, ?, ?, ?)""",
            (shipment_id, source, target["product_name"], target["quantity"],
             target["carton_no"], actor, now()),
        )
        version = 1
        _log(conn, shipment_id, actor, "doc_submitted",
             f"{SOURCE_LABELS[source]}提交{kind_label} v1："
             + _format_values({f: target[f] for f in fields}))
    else:
        version = existing["version"] + 1
        conn.execute(
            """UPDATE document SET version = ?, product_name = ?, quantity = ?, carton_no = ?,
                                   submitted_by = ?, submitted_at = ?
               WHERE shipment_id = ? AND source = ?""",
            (version, target["product_name"], target["quantity"], target["carton_no"],
             actor, now(), shipment_id, source),
        )
        _log(conn, shipment_id, actor, "doc_updated",
             f"{SOURCE_LABELS[source]}{kind_label}更新至 v{version}，"
             f"变更字段：{'、'.join(changed) or '无'}；"
             + _format_values({f: target[f] for f in fields}))
    return {"version": version, "duplicate": False, "changed_fields": changed}


def submit_document(conn: sqlite3.Connection, shipment_id: int, source: str,
                    product_name: str, quantity: str, carton_no: str,
                    actor: str = "") -> dict:
    """录入或更新某方资料。

    - 重复提交（三字段与现行版本完全一致）：忽略，不升版本、不重算。
    - 有变化：version+1（同一行覆盖更新），随后重新比对（差异按字段 upsert）。
    """
    if source not in SOURCES:
        raise ValueError(f"未知资料来源：{source}")
    get_shipment(conn, shipment_id)

    result = _apply_document(conn, shipment_id, source, {
        "product_name": product_name, "quantity": quantity, "carton_no": carton_no,
    }, actor)

    if result["duplicate"]:
        _log(conn, shipment_id, actor, "doc_duplicate_ignored",
             f"{SOURCE_LABELS[source]}重复提交，内容与 v{result['version']} 完全一致，已忽略")
        conn.commit()
        return result

    conn.commit()
    recompute(conn, shipment_id, actor=actor)
    return result


def get_document(conn: sqlite3.Connection, shipment_id: int, source: str):
    return conn.execute(
        "SELECT * FROM document WHERE shipment_id = ? AND source = ?",
        (shipment_id, source),
    ).fetchone()


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
        values_json = json.dumps(present, ensure_ascii=False)

        if mismatching:
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
            was_settled = row["status"] == STATUS_RESOLVED or row["is_reconciled"]
            conn.execute(
                "UPDATE discrepancy SET values_json = ?, is_reconciled = 0, reconciled_at = NULL, "
                "episode = episode + ?, last_changed_at = ? WHERE id = ?",
                (values_json, 1 if was_settled else 0, now(), row["id"]),
            )
            if row["status"] == STATUS_RESOLVED:
                # 新版本导致已解决的差异再次冲突：重新打开并退回待认领，进入新一轮风险(episode+1)
                conn.execute(
                    "UPDATE discrepancy SET status = 'open', owner = NULL WHERE id = ?",
                    (row["id"],),
                )
                _log(conn, shipment_id, actor, "discrepancy_reopened",
                     f"差异【{FIELD_LABELS[field]}】在新版本中再次冲突，重新打开（第 {row['episode'] + 1} 轮）："
                     + _describe_conflict(field, present))
                stats["reopened"] += 1
            elif was_settled:
                # 已核对一致但尚未确认解决，又出现新冲突：同一差异行进入新一轮风险
                _log(conn, shipment_id, actor, "discrepancy_reopened",
                     f"差异【{FIELD_LABELS[field]}】核对一致后再次冲突，进入新一轮提醒（第 {row['episode'] + 1} 轮）："
                     + _describe_conflict(field, present))
                stats["reopened"] += 1
            elif changed_values:
                _log(conn, shipment_id, actor, "discrepancy_updated",
                     f"差异【{FIELD_LABELS[field]}】内容随资料版本更新："
                     + _describe_conflict(field, present))
                stats["updated"] += 1
            continue

        # 不再冲突（含补件后改齐）：已有的未解决差异标记"核对一致，待确认"，快照刷新为当前值
        if row is not None and row["status"] != STATUS_RESOLVED and not row["is_reconciled"]:
            conn.execute(
                "UPDATE discrepancy SET is_reconciled = 1, reconciled_at = ?, values_json = ?, "
                "last_changed_at = ? WHERE id = ?",
                (now(), values_json, now(), row["id"]),
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


# ------------------------------------------------- 供应商受限补件入口（令牌）

class TokenError(Exception):
    """令牌无效/过期/吊销/字段越权等。"""


def create_supplement_token(conn: sqlite3.Connection, shipment_id: int, fields: list[str],
                            contact: str = "", actor: str = "",
                            expires_in_hours: int | None = 72) -> str:
    """内部为某票货签发一个供应商补件令牌，限定可补字段。"""
    get_shipment(conn, shipment_id)
    fields = [f for f in dict.fromkeys(fields) if f in FIELDS]
    if not fields:
        raise ValueError("至少指定一个有效字段")
    token = secrets.token_urlsafe(18)
    expires = (datetime.now() + timedelta(hours=expires_in_hours)) if expires_in_hours else None
    conn.execute(
        """INSERT INTO supplement_token (token, shipment_id, source, fields, contact,
                                         created_by, created_at, expires_at)
           VALUES (?, ?, 'supplier', ?, ?, ?, ?, ?)""",
        (token, shipment_id, ",".join(fields), contact.strip(), actor, now(),
         expires.isoformat(timespec="minutes") if expires else None),
    )
    _log(conn, shipment_id, actor, "token_created",
         f"为供应商{('（' + contact.strip() + '）') if contact.strip() else ''}签发补件链接，"
         f"允许字段：{'、'.join(FIELD_LABELS[f] for f in fields)}"
         + (f"，有效期至 {expires:%Y-%m-%d %H:%M}" if expires else ""))
    conn.commit()
    return token


def list_tokens(conn: sqlite3.Connection, shipment_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM supplement_token WHERE shipment_id = ? ORDER BY id", (shipment_id,)))


def revoke_token(conn: sqlite3.Connection, token_id: int, actor: str = "") -> None:
    row = conn.execute("SELECT * FROM supplement_token WHERE id = ?", (token_id,)).fetchone()
    if row is None:
        raise LookupError("链接不存在")
    conn.execute("UPDATE supplement_token SET revoked = 1 WHERE id = ?", (token_id,))
    _log(conn, row["shipment_id"], actor, "token_revoked",
         f"吊销供应商补件链接（允许字段：{'、'.join(FIELD_LABELS[f] for f in row['fields'].split(','))}）")
    conn.commit()


def _load_active_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM supplement_token WHERE token = ?", (token,)).fetchone()
    if row is None or row["revoked"]:
        raise TokenError("补件链接无效或已被吊销")
    if row["expires_at"] and parse_dt(row["expires_at"]) < datetime.now():
        raise TokenError("补件链接已过期")
    return row


def portal_context(conn: sqlite3.Connection, token: str) -> dict:
    """供应商门户页所需的最小信息：货票基本信息 + 本方资料 + 允许补的字段。

    刻意不返回货代/仓库的任何数据。
    """
    tok = _load_active_token(conn, token)
    shipment = get_shipment(conn, tok["shipment_id"])
    allowed = tok["fields"].split(",")
    doc = get_document(conn, tok["shipment_id"], "supplier")

    # 仅暴露与本方相关、且在允许字段内的差异状态（不包含他方具体值）
    my_open = []
    for d in list_discrepancies(conn, tok["shipment_id"]):
        if d["field"] in allowed and d["status"] != STATUS_RESOLVED:
            my_open.append({
                "field": d["field"],
                "label": FIELD_LABELS[d["field"]],
                "is_reconciled": bool(d["is_reconciled"]),
                "our_value": json.loads(d["values_json"]).get("supplier", {}).get("raw"),
            })
    return {
        "token": tok,
        "shipment": shipment,
        "allowed_fields": allowed,
        "document": doc,
        "open_discrepancies": my_open,
    }


def supplier_submit(conn: sqlite3.Connection, token: str, values: dict,
                    actor: str = "") -> dict:
    """供应商通过令牌提交补件：只能改令牌白名单内字段。

    - 越权字段（哪怕篡改表单）一律拒绝；
    - 与现行版本一致 → 重复提交，不多出版本/资料；
    - 有变化 → 升版本、自动挂回对应差异并触发整票重核。
    """
    tok = _load_active_token(conn, token)
    allowed = tok["fields"].split(",")
    bad = [f for f in values if f not in FIELDS]
    if bad:
        raise TokenError("包含非法字段")
    forbidden = [f for f in values if f not in allowed]
    if forbidden:
        _log(conn, tok["shipment_id"], actor or "供应商", "portal_denied",
             f"供应商补件尝试提交未授权字段：{'、'.join(FIELD_LABELS.get(f, f) for f in forbidden)}，已拒绝")
        conn.commit()
        raise TokenError("该链接不允许修改这些字段："
                         + "、".join(FIELD_LABELS.get(f, f) for f in forbidden))

    payload = {f: values[f] for f in allowed if str(values.get(f, "")).strip() != ""}
    if not payload:
        raise TokenError("请至少填写一个允许修改的字段")

    actor = actor.strip() or tok["contact"] or "供应商"
    result = _apply_document(conn, tok["shipment_id"], "supplier", payload, actor,
                             kind_label="通过补件链接")

    if result["duplicate"]:
        _log(conn, tok["shipment_id"], actor, "doc_duplicate_ignored",
             "供应商补件链接重复提交，内容与现行版本一致，已忽略")
        conn.commit()
        return result

    conn.execute(
        "UPDATE supplement_token SET used_count = used_count + 1, last_used_at = ? WHERE id = ?",
        (now(), tok["id"]))
    _log(conn, tok["shipment_id"], actor, "portal_submitted",
         f"供应商通过补件链接更新字段：{'、'.join(result['changed_fields'])}，已自动挂回对应差异并重新核对")
    conn.commit()
    recompute(conn, tok["shipment_id"], actor=actor)
    return result


# ------------------------------------------------------------- 申报包冻结与导出

STATUS_LABELS = {
    STATUS_OPEN: "待认领",
    STATUS_CLAIMED: "处理中",
    STATUS_RESOLVED: "已解决",
}


def build_snapshot(conn: sqlite3.Connection, shipment_id: int) -> dict:
    """组装一票货当前采用资料 + 差异处理结论的快照（尚未落库，落库后不可变）。"""
    shipment = dict(get_shipment(conn, shipment_id))
    docs = {r["source"]: dict(r) for r in list_documents(conn, shipment_id)}
    discs = list_discrepancies(conn, shipment_id)
    all_logs = list_logs(conn, shipment_id)

    # 逐字段确定“申报采用值”：三方一致即可采用；仍有分歧则不替业务拍板，留空并列入风险
    declared_fields = {}
    warnings = []
    for field in FIELDS:
        present = {}
        for src in SOURCES:
            doc = docs.get(src)
            if doc and str(doc.get(field) or "").strip():
                present[src] = doc[field]
        norms = {NORMALIZERS[field](v) for v in present.values()}
        entry = {"field": field, "label": FIELD_LABELS[field],
                 "values_by_source": present, "all_agree": len(norms) <= 1}
        if len(present) < len(SOURCES):
            missing = [SOURCE_LABELS[s] for s in SOURCES if s not in present]
            entry["missing_sources"] = missing
            warnings.append(f"【{FIELD_LABELS[field]}】缺少 {'、'.join(missing)} 的资料")
        if entry["all_agree"] and present:
            entry["adopted_value"] = next(iter(present.values()))
            entry["basis_sources"] = list(present.keys())
        else:
            entry["adopted_value"] = None
            if len(norms) >= 2:
                warnings.append(f"【{FIELD_LABELS[field]}】三方仍不一致，申报包未冻结采用值")
        declared_fields[field] = entry

    discrepancies_out = []
    for d in discs:
        d = dict(d)
        field = d["field"]
        d["field_label"] = FIELD_LABELS[field]
        d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
        d["values"] = {src: {"raw": v["raw"], "version": v["version"],
                             "submitted_at": v.get("submitted_at", "")}
                       for src, v in json.loads(d["values_json"]).items()}
        # 该差异的处理轨迹：认领/补件/重开/解决等（按字段标签从全量留痕中筛出）
        tag = f"【{FIELD_LABELS[field]}】"
        d["timeline"] = [{"actor": l["actor"], "action": l["action"],
                          "detail": l["detail"], "created_at": l["created_at"]}
                         for l in all_logs if tag in l["detail"]]
        if d["status"] != STATUS_RESOLVED and not d["is_reconciled"]:
            warnings.append(f"【{FIELD_LABELS[field]}】存在{d['status_label']}的未解决差异，"
                            f"冻结前请确认是否带差异申报")
        discrepancies_out.append(d)

    open_items = sum(1 for d in discrepancies_out
                     if d["status"] != STATUS_RESOLVED and not d["is_reconciled"])
    return {
        "generated_at": now(),
        "shipment": shipment,
        "documents": {src: {
            "source": src,
            "source_label": SOURCE_LABELS[src],
            "version": docs[src]["version"],
            "product_name": docs[src]["product_name"],
            "quantity": docs[src]["quantity"],
            "carton_no": docs[src]["carton_no"],
            "submitted_by": docs[src]["submitted_by"],
            "submitted_at": docs[src]["submitted_at"],
        } for src in SOURCES if src in docs},
        "declared_fields": declared_fields,
        "discrepancies": discrepancies_out,
        "warnings": warnings,
        "open_items": open_items,
    }


def freeze_package(conn: sqlite3.Connection, shipment_id: int, actor: str = "",
                   confirm: bool = False) -> dict:
    """把当前状态冻结为一个不可变申报包。

    默认拒绝带未解决差异冻结（confirm=True 表示负责人确认“带差异申报”并留痕）。
    返回 {"package_no", "open_items"}。
    """
    get_shipment(conn, shipment_id)
    snapshot = build_snapshot(conn, shipment_id)
    if snapshot["open_items"] and not confirm:
        raise ValueError(f"还有 {snapshot['open_items']} 处未解决差异；"
                         "确认要带差异冻结时需显式确认（confirm=True）")

    next_no = conn.execute(
        "SELECT COALESCE(MAX(package_no), 0) + 1 FROM declaration_package WHERE shipment_id = ?",
        (shipment_id,)).fetchone()[0]
    snapshot["package_no"] = next_no
    snapshot["frozen_by"] = (actor or "").strip() or "未知"
    snapshot["frozen_at"] = now()

    declared_desc = "，".join(
        f"{e['label']}={'采用「' + e['adopted_value'] + '」' if e['adopted_value'] else '未采用（分歧）'}"
        for e in snapshot["declared_fields"].values())
    conn.execute(
        """INSERT INTO declaration_package
               (shipment_id, package_no, frozen_by, frozen_at, declared_fields,
                open_items, payload_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (shipment_id, next_no, snapshot["frozen_by"], snapshot["frozen_at"],
         declared_desc, snapshot["open_items"],
         json.dumps(snapshot, ensure_ascii=False, indent=2)),
    )
    note = f"冻结申报包 #{next_no}：{declared_desc}"
    if snapshot["warnings"]:
        note += f"；风险提示 {len(snapshot['warnings'])} 条"
    if snapshot["open_items"]:
        note += f"；负责人确认带 {snapshot['open_items']} 处未解决差异申报"
    _log(conn, shipment_id, actor, "package_frozen", note)
    conn.commit()
    return {"package_no": next_no, "open_items": snapshot["open_items"]}


def list_packages(conn: sqlite3.Connection, shipment_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT id, package_no, frozen_by, frozen_at, declared_fields, open_items "
        "FROM declaration_package WHERE shipment_id = ? ORDER BY package_no",
        (shipment_id,)))


def get_package(conn: sqlite3.Connection, package_id: int) -> dict:
    row = conn.execute("SELECT * FROM declaration_package WHERE id = ?",
                       (package_id,)).fetchone()
    if row is None:
        raise LookupError(f"申报包 #{package_id} 不存在")
    payload = json.loads(row["payload_json"])
    payload["_id"] = row["id"]
    return payload


def export_markdown(package: dict) -> str:
    """把申报包渲染成带来源与时间的 Markdown 申报核对单。"""
    s = package["shipment"]
    lines = []
    lines.append(f"# 申报核对单 — {s['ref']}")
    lines.append("")
    lines.append(f"- 申报包编号：#{package['package_no']}（不可变快照）")
    lines.append(f"- 冻结时间：{package['frozen_at']}")
    lines.append(f"- 冻结操作人：{package['frozen_by']}")
    lines.append(f"- 客户：{s.get('customer') or '—'}　货描：{s.get('description') or '—'}")
    if s.get("deadline"):
        lines.append(f"- 报关截止：{s['deadline']}")
    lines.append("")

    lines.append("## 一、申报采用值")
    lines.append("")
    lines.append("| 字段 | 申报采用值 | 依据来源 | 供应商 | 货代 | 仓库 |")
    lines.append("|---|---|---|---|---|---|")
    for field in FIELDS:
        e = package["declared_fields"][field]
        by = e["values_by_source"]
        basis = "、".join(SOURCE_LABELS[x] for x in e.get("basis_sources", [])) or "—（三方分歧，未采用）"
        adopted = e["adopted_value"] or "**未冻结（仍有分歧）**"
        cells = []
        for src in SOURCES:
            doc = package["documents"].get(src)
            cells.append(f"{doc[field]}（v{doc['version']}，{doc['submitted_by'] or '无名'}，{doc['submitted_at']}）"
                         if doc and doc.get(field) else "未提交")
        lines.append(f"| {FIELD_LABELS[field]} | {adopted} | {basis} | {cells[0]} | {cells[1]} | {cells[2]} |")
    lines.append("")

    lines.append("## 二、差异处理结论")
    lines.append("")
    if not package["discrepancies"]:
        lines.append("本票货未产生任何字段差异。")
    for d in package["discrepancies"]:
        state = d["status_label"]
        if d["status"] != STATUS_RESOLVED and d["is_reconciled"]:
            state = "核对一致，待确认"
        lines.append(f"### 【{d['field_label']}】— {state}（风险轮次 第{d['episode']}轮）")
        lines.append("")
        lines.append("| 来源 | 冻结时的值 | 资料版本 | 提交时间 |")
        lines.append("|---|---|---|---|")
        for src in SOURCES:
            v = d["values"].get(src)
            if v:
                lines.append(f"| {SOURCE_LABELS[src]} | {v['raw']} | v{v['version']} | {v.get('submitted_at') or ''} |")
            else:
                lines.append(f"| {SOURCE_LABELS[src]} | 未提交 | — | — |")
        lines.append("")
        lines.append(f"- 负责人：{d['owner'] or '未认领'}")
        if d["supplement_note"]:
            lines.append("- 补件记录：")
            for n in d["supplement_note"].splitlines():
                lines.append(f"  - {n}")
        if d["timeline"]:
            lines.append("- 处理留痕：")
            for t in d["timeline"]:
                lines.append(f"  - {t['created_at']} {t['actor']}：{t['detail']}")
        lines.append("")

    if package["warnings"]:
        lines.append("## 三、冻结时风险提示")
        lines.append("")
        for w in package["warnings"]:
            lines.append(f"- ⚠️ {w}")
        lines.append("")
    lines.append("---")
    lines.append("本文件由冻结时刻的数据库快照生成，后续资料版本更新不影响本申报包内容。")
    return "\n".join(lines)
