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

# 差异“不再构成冲突”的两种依据
CLOSE_MATCHED = "matched"        # 严格归一化后三方一致
CLOSE_TOLERATED = "tolerated"    # 仍有数值差，但命中当前容差规则

# 容差规则生命周期（append-only：改规则=新版本，旧版本保留作证据）
RULE_ACTIVE = "active"
RULE_SUPERSEDED = "superseded"
RULE_REVOKED = "revoked"
RULE_STATUS_LABELS = {RULE_ACTIVE: "现行", RULE_SUPERSEDED: "已被新版本取代", RULE_REVOKED: "已停用"}

# 豁免生命周期
WAIVER_ACTIVE = "active"
WAIVER_EXPIRED = "expired"
WAIVER_REVOKED = "revoked"
WAIVER_SUPERSEDED = "superseded"   # 资料升版/重核后冲突消失，豁免自动终结
WAIVER_STATUS_LABELS = {
    WAIVER_ACTIVE: "豁免中（限时放行）",
    WAIVER_EXPIRED: "已到期",
    WAIVER_REVOKED: "已撤销",
    WAIVER_SUPERSEDED: "重核后冲突消失，自动终结",
}

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "customs.db")

# 申报包会签/放行状态机：
#   pending_review 冻结完成，等待关务复核人 + 关务主管会签（两人、不同人、各留意见与时间）
#   approved       两人均通过；等待指定操作人放行
#   released       指定操作人放行，可作为正式申报依据导出
#   rejected       任一会签人驳回（必须写原因），终态：不能放行、不能改、不能复用旧审批；
#                  业务重新处理后只能冻结一个新包，新包重新走完整会签
PACKAGE_PENDING = "pending_review"
PACKAGE_APPROVED = "approved"
PACKAGE_RELEASED = "released"
PACKAGE_REJECTED = "rejected"
PACKAGE_STATUS_LABELS = {
    PACKAGE_PENDING: "待复核（仅供内部复核，不得申报）",
    PACKAGE_APPROVED: "会签通过，待放行（仅供内部复核，不得申报）",
    PACKAGE_RELEASED: "已放行（正式申报包）",
    PACKAGE_REJECTED: "已驳回（作废，需重新处理后冻结新包）",
}
# 会签角色：两人必须不同
REVIEW_CUSTOMS = "customs_reviewer"   # 关务复核人
REVIEW_SUPERVISOR = "supervisor"      # 关务主管
REVIEW_ROLE_LABELS = {REVIEW_CUSTOMS: "关务复核人", REVIEW_SUPERVISOR: "关务主管"}
REVIEW_APPROVE = "approve"
REVIEW_REJECT = "reject"
# 未放行包导出件的统一水印
INTERNAL_REVIEW_WATERMARK = "【仅供内部复核 · 非正式申报依据】"


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


# ---------------------------------------------------------------- 容差判定

QUANTITY_MODES = {"abs", "pct"}
TEXT_MODE = "alias"


def _quantity_decimal(v: str):
    s = str(v).strip().replace(",", "").replace("，", "")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def parse_alias_groups(text: str) -> list[list[str]]:
    """每行一组别名，行内用 / 或 | 分隔。例如 '不锈钢杯/保温杯|真空保温杯'。"""
    groups = []
    for line in str(text or "").splitlines():
        parts = [p.strip() for p in re.split(r"[/|｜]", line) if p.strip()]
        if len(parts) >= 2:
            groups.append(parts)
    return groups


def validate_rule_config(field: str, mode: str, config: dict) -> None:
    """校验规则参数；不合法抛 ValueError。"""
    if field == "quantity":
        if mode not in QUANTITY_MODES:
            raise ValueError("数量容差模式必须是 abs（绝对数量）或 pct（百分比）")
        if mode == "abs":
            val = config.get("abs")
            try:
                d = Decimal(str(val))
                if d < 0:
                    raise ValueError
            except (InvalidOperation, ValueError, TypeError):
                raise ValueError("绝对容差必须是非负数字，如 50")
        else:
            val = config.get("pct")
            try:
                d = Decimal(str(val))
                if d < 0 or d > 100:
                    raise ValueError
            except (InvalidOperation, ValueError, TypeError):
                raise ValueError("百分比容差必须是 0~100 的数字（按百分数填，如 2 表示 2%）")
    elif field in ("product_name", "carton_no"):
        if mode != TEXT_MODE:
            raise ValueError("文本字段容差模式必须是 alias（别名组）")
        groups = parse_alias_groups(config.get("aliases", ""))
        if not groups:
            raise ValueError("至少填写一行别名组，组内用 / 分隔，如 不锈钢杯/保温杯")
    else:
        raise ValueError(f"未知字段：{field}")
    if config.get("adopt_source", "supplier") not in SOURCES:
        raise ValueError("容差采用值来源必须是 supplier / forwarder / warehouse 之一")
    if field == "quantity" and config.get("basis", "max") not in ("max", "min", "mean"):
        raise ValueError("百分比基准必须是 max / min / mean")


def evaluate_tolerance(field: str, mode: str, config: dict, present: dict) -> dict | None:
    """命中容差时返回依据 dict；不命中（或无法按容差判定）返回 None。

    present: {source: {"raw": ..., "norm": ..., "version": ..., "submitted_at": ...}}
    严格归一化已一致时调用方不会走到这里；本函数只在“仍有差”时判断差是否在允许范围内。
    任一参与值无法按规则解释（如数量里混进文本）→ None，退回严格差异。
    """
    if len(present) < 2:
        return None
    if field == "quantity":
        nums = {src: _quantity_decimal(v["raw"]) for src, v in present.items()}
        if any(d is None for d in nums.values()):
            return None
        vals = list(nums.values())
        lo, hi = min(vals), max(vals)
        spread = hi - lo
        if spread < 0:
            spread = -spread
        if mode == "abs":
            limit = Decimal(str(config.get("abs", "0")))
            within = spread <= limit
            evidence = {"mode": "abs", "limit": str(limit), "spread": str(spread)}
        else:
            pct_input = Decimal(str(config.get("pct", "0")))
            limit_frac = pct_input / Decimal(100)
            basis_key = config.get("basis", "max")
            if basis_key == "max":
                base = hi
                basis_desc = "最大值"
            elif basis_key == "min":
                base = lo
                basis_desc = "最小值"
            else:
                base = sum(vals, Decimal(0)) / Decimal(len(vals))
                basis_desc = "平均值"
            if base == 0:
                within = spread == 0
                ratio = None
            else:
                ratio = spread / base
                within = ratio <= limit_frac
            evidence = {"mode": "pct", "limit_pct_input": str(pct_input),
                        "basis": basis_key, "basis_desc": basis_desc,
                        "base": str(base), "spread": str(spread),
                        "ratio_pct": (str((ratio * 100).quantize(Decimal("0.01")))
                                      if ratio is not None else None)}
        if not within:
            return None
        evidence["within"] = True
        return evidence

    # 文本：归一化后每个值必须落在同一个别名组内
    if mode != TEXT_MODE:
        return None
    groups = parse_alias_groups(config.get("aliases", ""))
    norm_values = {src: v["norm"] for src, v in present.items()}
    for group in groups:
        group_norms = {NORMALIZERS[field](g) for g in group}
        if all(nv in group_norms for nv in norm_values.values()):
            return {"mode": "alias", "within": True,
                    "group": group, "matched_raw": {src: v["raw"] for src, v in present.items()}}
    return None


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
    rule_version     INTEGER,              -- 最近一次判定依据的规则版本（NULL=严格比对/历史数据）
    rule_evidence_json TEXT NOT NULL DEFAULT '{}',  -- 命中依据：模式/参数/基准/偏差
    close_reason     TEXT,                 -- matched / tolerated；未消除冲突时为 NULL
    active_waiver_id INTEGER,              -- 当前有效豁免（waiver.id），仅作活数据指针，证据在 waiver 表
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
-- 冻结后三方再来新版本也不会改动该包（只新增新的包，从不 UPDATE/DELETE payload）。
-- 会签/放行是活数据状态机，写在 payload_json 之外：
-- payload 永不修改，审批/放行只能推进包的状态、不能挪动或改写旧包内容。
CREATE TABLE IF NOT EXISTS declaration_package (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id     INTEGER NOT NULL REFERENCES shipment(id),
    package_no      INTEGER NOT NULL,
    frozen_by       TEXT NOT NULL DEFAULT '',
    frozen_at       TEXT NOT NULL,
    declared_fields TEXT NOT NULL DEFAULT '',
    open_items      INTEGER NOT NULL DEFAULT 0,
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending_review',
    designated_operator TEXT NOT NULL DEFAULT '',   -- 指定的放行操作人（空=不限定）
    released_by     TEXT NOT NULL DEFAULT '',
    released_at     TEXT,
    idempotency_key TEXT,                            -- 冻结防重键（同键只产一个包）
    UNIQUE(shipment_id, package_no)
);

-- 会签意见：一个包每个角色最多一条生效意见；approve×2（且两人不同）→ approved；任一 reject → rejected
CREATE TABLE IF NOT EXISTS package_review (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id  INTEGER NOT NULL REFERENCES declaration_package(id),
    role        TEXT NOT NULL,            -- customs_reviewer / supervisor
    actor       TEXT NOT NULL,
    decision    TEXT NOT NULL,            -- approve / reject
    comment     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    UNIQUE(package_id, role)
);

-- 容差规则：一票货一个字段一条“版本链”，append-only。
-- 修改规则 = 新增一行 version+1 并把旧版本置 superseded；停用则置 revoked。
-- 差异重核时只读取 active 版本，并把命中的 rule_version / config_json 固化到差异行。
CREATE TABLE IF NOT EXISTS tolerance_rule (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id  INTEGER NOT NULL REFERENCES shipment(id),
    field        TEXT NOT NULL,
    version      INTEGER NOT NULL,
    mode         TEXT NOT NULL,            -- quantity: abs / pct；文本: alias
    config_json  TEXT NOT NULL,            -- 规则参数（abs值/百分比/别名组/基准/采用来源）
    note         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active',
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    revoked_by   TEXT NOT NULL DEFAULT '',
    revoked_at   TEXT,
    UNIQUE(shipment_id, field, version)
);

-- 豁免：对“仍不满足规则”的差异申请限时放行。一个差异可有多个 episode 的多条豁免。
-- 豁免期间提醒服务不再催办；到期/撤销/资料升版终结后若仍冲突，复用原差异行 episode+1 恢复提醒。
CREATE TABLE IF NOT EXISTS waiver (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id    INTEGER NOT NULL REFERENCES shipment(id),
    discrepancy_id INTEGER NOT NULL REFERENCES discrepancy(id),
    episode        INTEGER NOT NULL,       -- 授予时差异所处的风险轮次
    reason         TEXT NOT NULL,
    granted_by     TEXT NOT NULL,
    granted_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',  -- active/expired/revoked/superseded
    revoked_by     TEXT NOT NULL DEFAULT '',
    ended_at       TEXT,
    basis_json     TEXT NOT NULL DEFAULT '{}'      -- 授予时的三方值/规则版本快照
);

CREATE INDEX IF NOT EXISTS idx_document_shipment ON document(shipment_id);
CREATE INDEX IF NOT EXISTS idx_discrepancy_shipment ON discrepancy(shipment_id);
CREATE INDEX IF NOT EXISTS idx_notification_status ON notification(status);
CREATE INDEX IF NOT EXISTS idx_token_token ON supplement_token(token);
CREATE INDEX IF NOT EXISTS idx_audit_shipment ON audit_log(shipment_id, id);
CREATE INDEX IF NOT EXISTS idx_rule_lookup ON tolerance_rule(shipment_id, field, status);
CREATE INDEX IF NOT EXISTS idx_waiver_active ON waiver(discrepancy_id, status);
"""

# 旧库轻量迁移：缺列即补（幂等）。
# 旧差异行的 rule_version/close_reason 均为 NULL：重核时按其实际状态补证据；
# 未配置规则的字段永远严格比对（历史货票行为不变）。
_MIGRATIONS = {
    "shipment": [("deadline", "TEXT"), ("warn_hours", "INTEGER NOT NULL DEFAULT 24")],
    "discrepancy": [
        ("episode", "INTEGER NOT NULL DEFAULT 1"),
        ("rule_version", "INTEGER"),
        ("rule_evidence_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("close_reason", "TEXT"),
        ("active_waiver_id", "INTEGER"),
    ],
    "declaration_package": [
        ("status", "TEXT NOT NULL DEFAULT 'pending_review'"),
        ("designated_operator", "TEXT NOT NULL DEFAULT ''"),
        ("released_by", "TEXT NOT NULL DEFAULT ''"),
        ("released_at", "TEXT"),
        ("idempotency_key", "TEXT"),
    ],
}


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    # 依赖迁移补列之后才能建的索引：冻结防重（NULL 互不冲突，旧包不受约束）
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_package_idempotency "
        "ON declaration_package(idempotency_key) WHERE idempotency_key IS NOT NULL")
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    legacy_packages = False
    for table, cols in _MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                # status 列本次才补上 = 会签功能上线前的旧库：既有冻结包全部需要归档
                if table == "declaration_package" and name == "status":
                    legacy_packages = True
    if legacy_packages:
        _backfill_legacy_packages(conn)


def _backfill_legacy_packages(conn: sqlite3.Connection) -> None:
    """会签功能上线前冻结的旧包：视为放行前历史归档为 released 并补审计，
    保证现有冻结包与导出入口不失效，同时在留痕里标明是迁移放行而非操作人放行。"""
    legacy = list(conn.execute("SELECT * FROM declaration_package"))
    for p in legacy:
        conn.execute("UPDATE declaration_package SET status = ?, released_by = ?, released_at = ? "
                     "WHERE id = ?",
                     (PACKAGE_RELEASED, "系统迁移", p["frozen_at"], p["id"]))
        _log(conn, p["shipment_id"], "系统迁移", "package_legacy_released",
             f"申报包 #{p['package_no']} 为会签功能上线前冻结的历史包，迁移归档为已放行"
             "（不可变快照，导出入口保留）")


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
    返回 {"version": int, "duplicate": bool, "changed_fields": [中文标签],
          "changed_field_keys": [字段键]}。
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

    changed_keys = [f for f in FIELDS
                    if f in fields and (existing is None or existing[f] != target[f])]
    changed = [FIELD_LABELS[f] for f in changed_keys]

    if existing is not None and not changed:
        return {"version": existing["version"], "duplicate": True,
                "changed_fields": [], "changed_field_keys": []}

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
    return {"version": version, "duplicate": False, "changed_fields": changed,
            "changed_field_keys": changed_keys}


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
    recompute(conn, shipment_id, actor=actor,
              changed_fields=result.get("changed_field_keys"))
    return result


def get_document(conn: sqlite3.Connection, shipment_id: int, source: str):
    return conn.execute(
        "SELECT * FROM document WHERE shipment_id = ? AND source = ?",
        (shipment_id, source),
    ).fetchone()


# ------------------------------------------------------------ 容差规则（版本化）

def active_rule(conn: sqlite3.Connection, shipment_id: int, field: str):
    return conn.execute(
        "SELECT * FROM tolerance_rule WHERE shipment_id = ? AND field = ? AND status = 'active'",
        (shipment_id, field),
    ).fetchone()


def list_rules(conn: sqlite3.Connection, shipment_id: int) -> list[sqlite3.Row]:
    """返回每字段版本链（最新版本在前）。"""
    return list(conn.execute(
        "SELECT * FROM tolerance_rule WHERE shipment_id = ? ORDER BY field, version DESC, id DESC",
        (shipment_id,)))


def rule_config(row: sqlite3.Row) -> dict:
    return json.loads(row["config_json"])


def save_rule(conn: sqlite3.Connection, shipment_id: int, field: str, mode: str,
              config: dict, note: str = "", actor: str = "") -> int:
    """维护容差规则：append-only 新版本，旧 active 版本置 superseded；随后整票重核。

    返回新版本号。历史上从未配置规则的字段继续严格比对，直到出现第一个版本。
    """
    get_shipment(conn, shipment_id)
    if field not in FIELDS:
        raise ValueError(f"未知字段：{field}")
    config = dict(config or {})
    config.setdefault("adopt_source", "supplier")
    if field == "quantity":
        config.setdefault("basis", "max")
    validate_rule_config(field, mode, config)

    prev = active_rule(conn, shipment_id, field)
    version = (prev["version"] + 1) if prev else 1
    conn.execute(
        """INSERT INTO tolerance_rule
               (shipment_id, field, version, mode, config_json, note, status,
                created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
        (shipment_id, field, version, mode, json.dumps(config, ensure_ascii=False),
         (note or "").strip(), actor or "系统", now()),
    )
    if prev:
        conn.execute("UPDATE tolerance_rule SET status = ? WHERE id = ?",
                     (RULE_SUPERSEDED, prev["id"]))
    _log(conn, shipment_id, actor, "rule_version_created",
         f"维护容差规则【{FIELD_LABELS[field]}】v{version}（{_describe_rule(mode, config)}"
         + (f"，备注：{note.strip()}" if note.strip() else "")
         + (f"），原 v{prev['version']} 转为历史版本" if prev else "）"))
    conn.commit()
    recompute(conn, shipment_id, actor=actor or "系统")
    return version


def revoke_rule(conn: sqlite3.Connection, shipment_id: int, field: str,
                actor: str = "") -> None:
    """停用字段的现行规则（版本链保留作证据），随后整票重核——容差内的差异重新暴露。"""
    rule = active_rule(conn, shipment_id, field)
    if rule is None:
        raise ValueError(f"【{FIELD_LABELS.get(field, field)}】没有现行容差规则可停用")
    conn.execute(
        "UPDATE tolerance_rule SET status = ?, revoked_by = ?, revoked_at = ? WHERE id = ?",
        (RULE_REVOKED, actor or "系统", now(), rule["id"]))
    _log(conn, shipment_id, actor, "rule_revoked",
         f"停用容差规则【{FIELD_LABELS[field]}】v{rule['version']}"
         f"（{_describe_rule(rule['mode'], json.loads(rule['config_json']))}），恢复严格比对")
    conn.commit()
    recompute(conn, shipment_id, actor=actor or "系统")


def _describe_rule(mode: str, config: dict) -> str:
    if mode == "abs":
        return f"数量绝对容差 ±{config.get('abs')}"
    if mode == "pct":
        basis_label = {"max": "最大值", "min": "最小值", "mean": "平均值"}.get(
            config.get("basis", "max"), config.get("basis", "最大值"))
        return f"数量百分比容差 ±{config.get('pct')}%（按{basis_label}）"
    if mode == "alias":
        groups = parse_alias_groups(config.get("aliases", ""))
        return "文本别名容差：" + "；".join("/".join(g) for g in groups)
    return mode


# ---------------------------------------------------------------- 豁免（限时放行）

def list_waivers(conn: sqlite3.Connection, *, shipment_id: int | None = None,
                 discrepancy_id: int | None = None,
                 active_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM waiver WHERE 1=1"
    args = []
    if shipment_id is not None:
        sql += " AND shipment_id = ?"
        args.append(shipment_id)
    if discrepancy_id is not None:
        sql += " AND discrepancy_id = ?"
        args.append(discrepancy_id)
    if active_only:
        sql += " AND status = 'active'"
    sql += " ORDER BY id DESC"
    return list(conn.execute(sql, args))


def get_waiver(conn: sqlite3.Connection, waiver_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM waiver WHERE id = ?", (waiver_id,)).fetchone()
    if row is None:
        raise LookupError(f"豁免 #{waiver_id} 不存在")
    return row


def grant_waiver(conn: sqlite3.Connection, discrepancy_id: int, reason: str,
                 granted_by: str, expires_at: str) -> int:
    """对仍不满足规则的差异授予限时豁免：原因、操作人、到期时间三者必填。

    豁免期内提醒服务不再催办；该差异已排队待发/失败待重试的通知一并撤销。
    """
    reason = (reason or "").strip()
    granted_by = (granted_by or "").strip()
    if not reason:
        raise ValueError("豁免必须填写原因")
    if not granted_by:
        raise ValueError("豁免必须填写操作人")
    exp = parse_dt(expires_at)
    if exp is None:
        raise ValueError("豁免到期时间格式不正确")
    if exp <= datetime.now():
        raise ValueError("豁免到期时间必须晚于当前时间")

    # 防御：直接走 API（绕过详情页/提醒扫描）时也先把已到期豁免处理掉
    expire_due_waivers(conn)
    row = _get_discrepancy(conn, discrepancy_id)
    if row["status"] == STATUS_RESOLVED or row["is_reconciled"]:
        raise ValueError("差异已一致或已解决，无需豁免")
    if list_waivers(conn, discrepancy_id=discrepancy_id, active_only=True):
        raise ValueError("该差异已有有效豁免；如需改期请先撤销原豁免")

    values = json.loads(row["values_json"])
    basis = {
        "values": values,
        "conflict": _describe_conflict(row["field"], values),
        "rule_version": row["rule_version"],
        "rule_evidence": json.loads(row["rule_evidence_json"] or "{}"),
        "granted_at": now(),
    }
    cur = conn.execute(
        """INSERT INTO waiver
               (shipment_id, discrepancy_id, episode, reason, granted_by, granted_at,
                expires_at, status, basis_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
        (row["shipment_id"], discrepancy_id, row["episode"], reason, granted_by, now(),
         exp.isoformat(timespec="seconds"), json.dumps(basis, ensure_ascii=False)),
    )
    wid = cur.lastrowid
    conn.execute("UPDATE discrepancy SET active_waiver_id = ? WHERE id = ?", (wid, discrepancy_id))
    _cancel_pending_notifications(conn, discrepancy_id)
    _log(conn, row["shipment_id"], granted_by, "waiver_granted",
         f"差异【{FIELD_LABELS[row['field']]}】批准限时豁免（第 {row['episode']} 轮），"
         f"到期 {exp:%Y-%m-%d %H:%M}，原因：{reason}")
    conn.commit()
    return wid


def revoke_waiver(conn: sqlite3.Connection, waiver_id: int, actor: str = "") -> dict:
    """人工撤销豁免。若差异仍冲突：复用原差异行 episode+1，恢复催办。"""
    actor = (actor or "").strip() or "未知"
    w = get_waiver(conn, waiver_id)
    if w["status"] != WAIVER_ACTIVE:
        raise ValueError("只能撤销有效中的豁免")
    bumped = _end_waiver(conn, w, WAIVER_REVOKED, actor,
                         log_action="waiver_revoked",
                         log_detail=f"豁免被 {actor} 撤销")
    conn.commit()
    return {"bumped": bumped}


def expire_due_waivers(conn: sqlite3.Connection, at: datetime | None = None,
                       actor: str = "系统") -> dict:
    """把已到点的 active 豁免置 expired；仍冲突的差异 episode+1 恢复提醒。

    由提醒服务每轮扫描前调用；Web 详情页加载时也调用一次（幂等）。
    """
    at = at or datetime.now()
    stats = {"expired": 0, "reopened": 0}
    due = list(conn.execute(
        "SELECT * FROM waiver WHERE status = 'active' AND expires_at <= ? ORDER BY id",
        (at.isoformat(timespec="seconds"),)))
    for w in due:
        bumped = _end_waiver(conn, w, WAIVER_EXPIRED, actor,
                             log_action="waiver_expired",
                             log_detail=f"豁免到期（{w['expires_at'].replace('T', ' ')}）",
                             reopen_reason="豁免到期后仍有冲突",
                             actor_for_log=actor)
        stats["expired"] += 1
        if bumped:
            stats["reopened"] += 1
    if due:
        conn.commit()
    return stats


def _end_waiver(conn, w, new_status: str, actor: str, *, log_action: str,
                log_detail: str, bump: bool = True,
                reopen_reason: str | None = None,
                actor_for_log: str | None = None) -> bool:
    """终结一条豁免；bump=True 且差异仍未一致时复用原行开启新 episode。返回是否 bump。"""
    conn.execute(
        "UPDATE waiver SET status = ?, ended_at = ?, revoked_by = COALESCE(NULLIF(?, ''), revoked_by) "
        "WHERE id = ?",
        (new_status, now(), actor if new_status == WAIVER_REVOKED else "", w["id"]))
    conn.execute("UPDATE discrepancy SET active_waiver_id = NULL WHERE id = ? AND active_waiver_id = ?",
                 (w["discrepancy_id"], w["id"]))

    d = conn.execute("SELECT * FROM discrepancy WHERE id = ?", (w["discrepancy_id"],)).fetchone()
    _log(conn, w["shipment_id"], actor_for_log or actor, log_action,
         f"差异【{FIELD_LABELS[d['field']] if d is not None else '?'}】{log_detail}")
    bumped = False
    if bump and d is not None and d["status"] != STATUS_RESOLVED and not d["is_reconciled"]:
        # 仍有冲突：复用原差异行开启新 episode（撤销/到期/资料升版同一套口径），恢复提醒
        conn.execute(
            "UPDATE discrepancy SET episode = episode + 1, last_changed_at = ? WHERE id = ?",
            (now(), d["id"]))
        reason = reopen_reason or log_detail
        _log(conn, d["shipment_id"], actor_for_log or actor, "discrepancy_reopened",
             f"差异【{FIELD_LABELS[d['field']]}】{reason}，重新打开（第 {d['episode'] + 1} 轮）："
             + _describe_conflict(d["field"], json.loads(d["values_json"])))
        bumped = True
    return bumped


def _cancel_pending_notifications(conn, discrepancy_id: int) -> None:
    """撤销该差异当前所有 episode 已排队/待重试的通知（已发送的不可撤回）。"""
    conn.execute(
        """UPDATE notification SET status='cancelled', last_error='豁免期间不催办'
           WHERE id IN (
               SELECT n.id FROM notification n
               JOIN reminder_event e ON n.event_id = e.id
               WHERE e.discrepancy_id = ? AND n.status IN ('pending','failed'))""",
        (discrepancy_id,))


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


def recompute(conn: sqlite3.Connection, shipment_id: int, actor: str = "系统",
              changed_fields: list[str] | None = None) -> dict:
    """按最新资料版本 + 当前容差规则重算全部字段，差异 upsert 到唯一行。

    判定链：严格一致 → 命中容差（tolerated，记录规则版本/依据）→ 仍冲突（保留原始三方值）。
    changed_fields 非 None（资料升版提交）时，该字段上处于豁免期的差异会被终结：
    仍冲突 → 复用原行 episode+1 恢复提醒；冲突消失 → 豁免 superseded，不开新轮。
    changed_fields 为 None（手动重核/规则变更）不动豁免。
    返回 {"opened", "updated", "reconciled", "reopened", "tolerated", "waivers_ended"}
    """
    get_shipment(conn, shipment_id)
    docs = {r["source"]: r for r in list_documents(conn, shipment_id)}
    snap = _snapshot(docs)
    existing = {r["field"]: r for r in list_discrepancies(conn, shipment_id)}
    rules = {f: active_rule(conn, shipment_id, f) for f in FIELDS}
    stats = {"opened": 0, "updated": 0, "reconciled": 0, "reopened": 0,
             "tolerated": 0, "waivers_ended": 0}

    for field in FIELDS:
        present = snap[field]
        distinct_norms = {v["norm"] for v in present.values()}
        strict_conflict = len(distinct_norms) >= 2
        row = existing.get(field)
        values_json = json.dumps(present, ensure_ascii=False)

        rule = rules[field]
        verdict = None        # "matched" / "tolerated" / None(仍冲突)
        evidence = {}
        if not strict_conflict:
            verdict = CLOSE_MATCHED
        elif rule is not None:
            hit = evaluate_tolerance(
                field, rule["mode"], json.loads(rule["config_json"]), present)
            if hit:
                verdict = CLOSE_TOLERATED
                evidence = hit

        # ---- 仍不满足（严格不一致且未命中容差）----
        if verdict is None:
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
            # 资料升版终结豁免：提交涉及该字段时，原豁免置 superseded；仍冲突 → 下方统一 episode+1
            waiver_due = bool(row["active_waiver_id"] and changed_fields is not None
                              and field in changed_fields)
            if waiver_due:
                w = get_waiver(conn, row["active_waiver_id"])
                _end_waiver(conn, w, WAIVER_SUPERSEDED, actor or "系统",
                            log_action="waiver_superseded",
                            log_detail="资料升版后仍有冲突，豁免自动终结",
                            bump=False)
                stats["waivers_ended"] += 1
            conn.execute(
                "UPDATE discrepancy SET values_json = ?, is_reconciled = 0, reconciled_at = NULL, "
                "close_reason = NULL, rule_version = NULL, rule_evidence_json = '{}', "
                "episode = episode + ?, last_changed_at = ? WHERE id = ?",
                (values_json, 1 if (was_settled or waiver_due) else 0, now(), row["id"]),
            )
            if row["status"] == STATUS_RESOLVED:
                # 新版本/规则变化导致已解决的差异再次冲突：重新打开、退回待认领、新一轮风险
                conn.execute(
                    "UPDATE discrepancy SET status = 'open', owner = NULL WHERE id = ?",
                    (row["id"],),
                )
                _log(conn, shipment_id, actor, "discrepancy_reopened",
                     f"差异【{FIELD_LABELS[field]}】在新版本中再次冲突，重新打开（第 {row['episode'] + 1} 轮）："
                     + _describe_conflict(field, present))
                stats["reopened"] += 1
            elif was_settled or waiver_due:
                if waiver_due:
                    reason = "资料升版后仍有冲突，豁免已终结"
                elif row["close_reason"] == CLOSE_TOLERATED:
                    reason = "规则停用/收紧后仍超出容差"
                else:
                    reason = "核对一致后再次冲突"
                _log(conn, shipment_id, actor, "discrepancy_reopened",
                     f"差异【{FIELD_LABELS[field]}】{reason}，重新打开（第 {row['episode'] + 1} 轮）："
                     + _describe_conflict(field, present))
                stats["reopened"] += 1
            elif changed_values:
                _log(conn, shipment_id, actor, "discrepancy_updated",
                     f"差异【{FIELD_LABELS[field]}】内容随资料版本更新："
                     + _describe_conflict(field, present))
                stats["updated"] += 1
            continue

        # ---- 冲突已消除（严格一致或容差内）----
        rule_version = rule["version"] if (rule is not None and verdict == CLOSE_TOLERATED) else None
        verdict_evidence = (json.dumps(evidence, ensure_ascii=False)
                            if verdict == CLOSE_TOLERATED else "{}")
        if verdict == CLOSE_TOLERATED:
            adopted_src = json.loads(rule["config_json"]).get("adopt_source", "supplier")
            evidence["rule_version"] = rule["version"]
            evidence["rule_mode"] = rule["mode"]
            evidence["rule_config"] = json.loads(rule["config_json"])
            evidence["adopt_source"] = adopted_src
            evidence["adopted_value"] = present.get(adopted_src, {}).get("raw")
            verdict_evidence = json.dumps(evidence, ensure_ascii=False)

        if row is None:
            # 规则在冲突产生前就配好：容差内不产生差异行、不催办，仅在审计里留判定依据
            if verdict == CLOSE_TOLERATED:
                _log(conn, shipment_id, actor, "discrepancy_tolerated",
                     f"【{FIELD_LABELS[field]}】三方存在差异但在容差规则 v{rule['version']} 内，"
                     f"不列为差异：{_describe_conflict(field, present)}")
                stats["tolerated"] += 1
            continue

        # 已解决的差异行：历史结论保留，不回改（与原系统一致）
        if row["status"] == STATUS_RESOLVED:
            continue

        newly_settled = not row["is_reconciled"]
        basis_changed = ((row["close_reason"] or CLOSE_MATCHED) != verdict
                         or (row["rule_version"] or 0) != (rule_version or 0))

        if newly_settled:
            # 豁免中的差异若因资料升版而改齐/落入容差：自动终结豁免，不开新 episode
            if row["active_waiver_id"]:
                w = get_waiver(conn, row["active_waiver_id"])
                _end_waiver(conn, w, WAIVER_SUPERSEDED, actor or "系统",
                            log_action="waiver_superseded",
                            log_detail="资料升版/重核后冲突已消失，豁免自动终结",
                            bump=False)
                stats["waivers_ended"] += 1
            conn.execute(
                "UPDATE discrepancy SET is_reconciled = 1, reconciled_at = ?, values_json = ?, "
                "close_reason = ?, rule_version = ?, rule_evidence_json = ?, last_changed_at = ? "
                "WHERE id = ?",
                (now(), values_json, verdict, rule_version, verdict_evidence, now(), row["id"]),
            )
            if verdict == CLOSE_TOLERATED:
                _log(conn, shipment_id, actor, "discrepancy_tolerated",
                     f"重新核对：差异【{FIELD_LABELS[field]}】未完全一致，但命中现行容差规则 "
                     f"v{rule['version']}（{_describe_rule(rule['mode'], json.loads(rule['config_json']))}），"
                     f"按容差视为核对一致，待负责人确认")
                stats["tolerated"] += 1
            else:
                _log(conn, shipment_id, actor, "discrepancy_reconciled",
                     "重新核对：差异【" + FIELD_LABELS[field] + "】三方已一致，待负责人确认解决")
            stats["reconciled"] += 1
        elif basis_changed:
            # 已 settled：判定依据变化（规则升版/停用后恰好严格一致/容差口径变化），刷新固化依据
            conn.execute(
                "UPDATE discrepancy SET values_json = ?, close_reason = ?, rule_version = ?, "
                "rule_evidence_json = ?, last_changed_at = ? WHERE id = ?",
                (values_json, verdict, rule_version, verdict_evidence, now(), row["id"]),
            )
            if verdict == CLOSE_TOLERATED:
                _log(conn, shipment_id, actor, "discrepancy_tolerated",
                     f"差异【{FIELD_LABELS[field]}】继续在容差内，判定依据更新为规则 v{rule['version']}")
            else:
                _log(conn, shipment_id, actor, "discrepancy_reconciled",
                     f"差异【{FIELD_LABELS[field]}】三方已严格一致，判定依据更新为严格比对"
                     + (f"（原依据规则 v{row['rule_version']}）" if row["rule_version"] else ""))
        elif row["values_json"] != values_json:
            conn.execute(
                "UPDATE discrepancy SET values_json = ?, last_changed_at = ? WHERE id = ?",
                (values_json, now(), row["id"]),
            )

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
    recompute(conn, tok["shipment_id"], actor=actor,
              changed_fields=result.get("changed_field_keys"))
    return result


# ------------------------------------------------------------- 申报包冻结与导出

STATUS_LABELS = {
    STATUS_OPEN: "待认领",
    STATUS_CLAIMED: "处理中",
    STATUS_RESOLVED: "已解决",
}


def build_snapshot(conn: sqlite3.Connection, shipment_id: int) -> dict:
    """组装一票货当前采用资料 + 差异处理结论的快照（尚未落库，落库后不可变）。

    快照同时固化：容差规则完整版本链（含已停用/被取代版本）、每条差异的判定依据
    （严格一致 / 命中的规则版本与参数 / 偏差）、豁免全历史（原因、操作人、到期、终结方式）。
    """
    shipment = dict(get_shipment(conn, shipment_id))
    docs = {r["source"]: dict(r) for r in list_documents(conn, shipment_id)}
    discs = list_discrepancies(conn, shipment_id)
    all_logs = list_logs(conn, shipment_id)

    # 容差规则：冻结当时的完整版本链 + 现行版本指针（后来改配置不影响本包）
    rules_out = [dict(r) for r in list_rules(conn, shipment_id)]
    for r in rules_out:
        r["config"] = json.loads(r.pop("config_json"))
        r["status_label"] = RULE_STATUS_LABELS.get(r["status"], r["status"])
    active_rules = {r["field"]: r for r in rules_out if r["status"] == RULE_ACTIVE}

    # 豁免全历史，按差异归集（冻结时是 active 的，在包内永远能看到当时的授权证据）
    waivers_by_disc: dict[int, list] = {}
    for w in list_waivers(conn, shipment_id=shipment_id):
        item = dict(w)
        item["basis"] = json.loads(w["basis_json"] or "{}")
        item.pop("basis_json", None)
        item["status_label"] = WAIVER_STATUS_LABELS.get(w["status"], w["status"])
        waivers_by_disc.setdefault(w["discrepancy_id"], []).append(item)

    # 逐字段确定“申报采用值”：严格一致即可采用；差异仍在但命中容差时按规则指定来源采用；
    # 既不一致也未命中容差则不替业务拍板，留空并列入风险
    declared_fields = {}
    warnings = []
    for field in FIELDS:
        present = {}
        for src in SOURCES:
            doc = docs.get(src)
            if doc and str(doc.get(field) or "").strip():
                present[src] = doc[field]
        norms = {NORMALIZERS[field](v) for v in present.values()}
        strict_agree = len(norms) <= 1
        rule = active_rules.get(field)
        entry = {"field": field, "label": FIELD_LABELS[field],
                 "values_by_source": present, "all_agree": strict_agree,
                 "adopted_value": None, "basis_sources": [], "adoption_basis": None}
        if len(present) < len(SOURCES):
            missing = [SOURCE_LABELS[s] for s in SOURCES if s not in present]
            entry["missing_sources"] = missing
            warnings.append(f"【{FIELD_LABELS[field]}】缺少 {'、'.join(missing)} 的资料")
        if strict_agree and present:
            entry["adopted_value"] = next(iter(present.values()))
            entry["basis_sources"] = list(present.keys())
            entry["adoption_basis"] = CLOSE_MATCHED
        elif rule is not None and len(present) >= 2:
            snap_present = {src: {"raw": present[src], "norm": NORMALIZERS[field](present[src])}
                            for src in present}
            hit = evaluate_tolerance(field, rule["mode"], rule["config"], snap_present)
            if hit:
                adopt_src = rule["config"].get("adopt_source", "supplier")
                entry["tolerance_rule_version"] = rule["version"]
                entry["tolerance_evidence"] = hit
                entry["adoption_basis"] = CLOSE_TOLERATED
                entry["adopted_value"] = present.get(adopt_src) or next(iter(present.values()))
                entry["basis_sources"] = [adopt_src] if adopt_src in present else list(present.keys())
        if entry.get("adopted_value") is None:
            if len(norms) >= 2:
                warnings.append(f"【{FIELD_LABELS[field]}】三方仍不一致且未命中容差规则，申报包未冻结采用值")
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
        d["rule_evidence"] = json.loads(d["rule_evidence_json"] or "{}")
        d["close_reason_label"] = {CLOSE_MATCHED: "严格比对一致",
                                   CLOSE_TOLERATED: "容差内视为一致"}.get(d["close_reason"])
        d["waivers"] = waivers_by_disc.get(d["id"], [])
        active_waiver = next((w for w in d["waivers"] if w["status"] == WAIVER_ACTIVE), None)
        d["active_waiver"] = active_waiver
        # 该差异的处理轨迹：认领/补件/重开/解决/豁免等（按字段标签从全量留痕中筛出）
        tag = f"【{FIELD_LABELS[field]}】"
        d["timeline"] = [{"actor": l["actor"], "action": l["action"],
                          "detail": l["detail"], "created_at": l["created_at"]}
                         for l in all_logs if tag in l["detail"]]
        if d["status"] != STATUS_RESOLVED and not d["is_reconciled"]:
            if active_waiver:
                warnings.append(
                    f"【{FIELD_LABELS[field]}】差异在限时豁免期内（{active_waiver['granted_by']} 批准，"
                    f"到期 {active_waiver['expires_at'].replace('T', ' ')}，原因：{active_waiver['reason']}），"
                    f"到期后若仍冲突将自动重开并恢复催办")
            else:
                warnings.append(f"【{FIELD_LABELS[field]}】存在{d['status_label']}的未解决差异，"
                                f"冻结前请确认是否带差异申报")
        discrepancies_out.append(d)

    # 未豁免、未一致的才算“遗留未解决”（豁免是已授权的限时放行，不再阻止冻结）
    open_items = sum(1 for d in discrepancies_out
                     if d["status"] != STATUS_RESOLVED and not d["is_reconciled"]
                     and not d["active_waiver"])
    waived_items = sum(1 for d in discrepancies_out if d["active_waiver"])
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
        "tolerance_rules": rules_out,
        "declared_fields": declared_fields,
        "discrepancies": discrepancies_out,
        "warnings": warnings,
        "open_items": open_items,
        "waived_items": waived_items,
    }


class PackageStateError(Exception):
    """申报包状态机非法操作（状态不对/角色越权/同人双角色/重复提交等）。"""


def _immediate_tx(conn):
    """开启写事务并立即拿库级写锁：把“检查—写入”变成临界区，挡住并发审批/放行/冻结。
    SQLite 默认惰性 BEGIN 在第一条写语句才加锁，显式 BEGIN IMMEDIATE 后
    第二个并发请求会阻塞至 busy_timeout，随后看到前一个请求的提交结果。"""
    conn.execute("BEGIN IMMEDIATE")


def _get_package_row(conn: sqlite3.Connection, package_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM declaration_package WHERE id = ?",
                       (package_id,)).fetchone()
    if row is None:
        raise LookupError(f"申报包 #{package_id} 不存在")
    return row


def freeze_package(conn: sqlite3.Connection, shipment_id: int, actor: str = "",
                   confirm: bool = False, designated_operator: str = "",
                   idempotency_key: str | None = None) -> dict:
    """把当前状态冻结为一个不可变申报包，冻结后进入【待复核】状态。

    - 默认拒绝带未解决差异冻结（confirm=True 表示负责人确认“带差异申报”并留痕）；
    - designated_operator 指定唯一放行操作人（空字符串=不限定，后续任何登录操作人可放行）；
    - idempotency_key 非空时，同键重复提交只返回已冻结的包（防重复点击/表单重放）；
    - 并发冻结同一票货由 (shipment_id, package_no) 唯一约束 + 写锁兜底；
    - 驳回/待复核中的旧包不阻止冻结新包；新包编号继续递增、审批从零开始，
      旧包内容与旧审批永远不会被挪动或修改。
    返回 {"id", "package_no", "open_items", "status", "deduped"}。
    """
    get_shipment(conn, shipment_id)
    actor = (actor or "").strip()
    designated_operator = (designated_operator or "").strip()
    idem = idempotency_key.strip() if idempotency_key else None

    # 防重键先在事务外查一次（命中即直接返回，避免无谓写锁）
    if idem:
        existing = conn.execute(
            "SELECT * FROM declaration_package WHERE idempotency_key = ?", (idem,)).fetchone()
        if existing:
            return {"id": existing["id"], "package_no": existing["package_no"],
                    "open_items": existing["open_items"], "status": existing["status"],
                    "deduped": True}

    try:
        _immediate_tx(conn)
        if idem:
            existing = conn.execute(
                "SELECT * FROM declaration_package WHERE idempotency_key = ?", (idem,)).fetchone()
            if existing:
                conn.commit()
                return {"id": existing["id"], "package_no": existing["package_no"],
                        "open_items": existing["open_items"], "status": existing["status"],
                        "deduped": True}
        snapshot = build_snapshot(conn, shipment_id)
        if snapshot["open_items"] and not confirm:
            conn.rollback()
            raise ValueError(f"还有 {snapshot['open_items']} 处未解决差异；"
                             "确认要带差异冻结时需显式确认（confirm=True）")

        next_no = conn.execute(
            "SELECT COALESCE(MAX(package_no), 0) + 1 FROM declaration_package WHERE shipment_id = ?",
            (shipment_id,)).fetchone()[0]
        snapshot["package_no"] = next_no
        snapshot["frozen_by"] = actor or "未知"
        snapshot["frozen_at"] = now()
        snapshot["designated_operator"] = designated_operator
        snapshot["workflow"] = {
            "status": PACKAGE_PENDING,
            "status_label": PACKAGE_STATUS_LABELS[PACKAGE_PENDING],
            "designated_operator": designated_operator,
            "reviews": [],
            "released_by": "",
            "released_at": None,
        }

        declared_desc = "，".join(
            f"{e['label']}={'采用「' + e['adopted_value'] + '」' if e['adopted_value'] else '未采用（分歧）'}"
            for e in snapshot["declared_fields"].values())
        try:
            cur = conn.execute(
                """INSERT INTO declaration_package
                       (shipment_id, package_no, frozen_by, frozen_at, declared_fields,
                        open_items, payload_json, status, designated_operator, idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (shipment_id, next_no, snapshot["frozen_by"], snapshot["frozen_at"],
                 declared_desc, snapshot["open_items"],
                 json.dumps(snapshot, ensure_ascii=False, indent=2),
                 PACKAGE_PENDING, designated_operator, idem),
            )
        except sqlite3.IntegrityError as e:
            conn.rollback()
            if idem:
                existing = conn.execute(
                    "SELECT * FROM declaration_package WHERE idempotency_key = ?",
                    (idem,)).fetchone()
                if existing:
                    return {"id": existing["id"], "package_no": existing["package_no"],
                            "open_items": existing["open_items"], "status": existing["status"],
                            "deduped": True}
            raise PackageStateError(f"申报包冻结冲突（可能有重复提交），请刷新后重试：{e}")
        note = f"冻结申报包 #{next_no}（待复核）：{declared_desc}"
        if designated_operator:
            note += f"；指定放行操作人：{designated_operator}"
        if snapshot["waived_items"]:
            note += f"；{snapshot['waived_items']} 处差异在限时豁免期内（豁免证据随包固化）"
        if snapshot["warnings"]:
            note += f"；风险提示 {len(snapshot['warnings'])} 条"
        if snapshot["open_items"]:
            note += f"；负责人确认带 {snapshot['open_items']} 处未解决差异申报"
        _log(conn, shipment_id, actor, "package_frozen", note)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"id": cur.lastrowid, "package_no": next_no,
            "open_items": snapshot["open_items"], "status": PACKAGE_PENDING, "deduped": False}


def list_packages(conn: sqlite3.Connection, shipment_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT id, package_no, frozen_by, frozen_at, declared_fields, open_items, "
        "status, designated_operator, released_by, released_at "
        "FROM declaration_package WHERE shipment_id = ? ORDER BY package_no",
        (shipment_id,)))


def list_package_reviews(conn: sqlite3.Connection, package_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM package_review WHERE package_id = ? ORDER BY id", (package_id,)))


def review_package(conn: sqlite3.Connection, package_id: int, role: str, actor: str,
                   decision: str, comment: str = "") -> dict:
    """会签一个待复核包：关务复核人、关务主管各一条意见，两人必须不同。

    - 只能在 pending_review 状态会签；角色只能是 customs_reviewer / supervisor；
    - 同一角色重复点击：若决策/意见/人完全一致按幂等处理，否则拒绝（后来者看到的是既成结果）；
    - 两个角色不能是同一人（后一个角色提交时与已存在的另一角色比对，挡住兼任）；
    - 驳回必须填写原因；任一角色驳回 → rejected 终态，阻止放行；
    - 两人均通过 → approved。全部变化（含被拦截的非法尝试）写审计。
    返回 {"status", "rejected": bool}。
    """
    actor = (actor or "").strip()
    comment = (comment or "").strip()
    if not actor:
        raise ValueError("会签必须填写操作人姓名")
    if role not in REVIEW_ROLE_LABELS:
        raise ValueError(f"会签角色必须是 {REVIEW_CUSTOMS}/{REVIEW_SUPERVISOR}")
    if decision not in (REVIEW_APPROVE, REVIEW_REJECT):
        raise ValueError("会签决策必须是 approve / reject")
    if decision == REVIEW_REJECT and not comment:
        raise ValueError("驳回必须填写原因")

    try:
        _immediate_tx(conn)
        row = _get_package_row(conn, package_id)
        shipment_id = row["shipment_id"]
        if row["status"] != PACKAGE_PENDING:
            conn.rollback()
            raise PackageStateError(
                f"申报包 #{row['package_no']} 当前状态为"
                f"“{PACKAGE_STATUS_LABELS.get(row['status'], row['status'])}”，不能再会签")
        existing = {r["role"]: r for r in list_package_reviews(conn, package_id)}

        mine = existing.get(role)
        if mine is not None:
            # 同一角色重复提交：完全一致 → 幂等成功；不一致 → 拒绝（并发/重复点击的明确行为）
            if (mine["actor"] == actor and mine["decision"] == decision
                    and mine["comment"] == comment):
                conn.rollback()
                return {"status": row["status"], "rejected": decision == REVIEW_REJECT,
                        "duplicate": True}
            conn.rollback()
            raise PackageStateError(
                f"{REVIEW_ROLE_LABELS[role]}已由 {mine['actor']} 于 {mine['created_at']}"
                f"{'通过' if mine['decision'] == REVIEW_APPROVE else '驳回'}，"
                "同一角色不能重复会签；请刷新查看最新结果")

        other = next((r for r_role, r in existing.items() if r_role != role), None)
        if other is not None and other["actor"] == actor:
            _log(conn, shipment_id, actor, "package_review_denied",
                 f"申报包 #{row['package_no']}：{actor} 已以{REVIEW_ROLE_LABELS[other['role']]}"
                 f"身份{'通过' if other['decision'] == REVIEW_APPROVE else '驳回'}，"
                 f"不得再兼任{REVIEW_ROLE_LABELS[role]}，已拒绝")
            conn.commit()
            raise PackageStateError(
                f"关务复核人与主管必须是不同人员：{actor} 已担任{REVIEW_ROLE_LABELS[other['role']]}，"
                f"不能再担任{REVIEW_ROLE_LABELS[role]}")

        ts = now()
        conn.execute(
            "INSERT INTO package_review (package_id, role, actor, decision, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (package_id, role, actor, decision, comment, ts))

        new_status = PACKAGE_PENDING
        if decision == REVIEW_REJECT:
            new_status = PACKAGE_REJECTED
            conn.execute("UPDATE declaration_package SET status = ? WHERE id = ?",
                         (new_status, package_id))
            _log(conn, shipment_id, actor, "package_review_rejected",
                 f"申报包 #{row['package_no']} 被{REVIEW_ROLE_LABELS[role]} {actor} 驳回，"
                 f"放行已阻止；驳回原因：{comment}。业务重新处理后只能冻结新包，"
                 "旧包内容与本审批记录不可修改、不可复用")
        else:
            _log(conn, shipment_id, actor, "package_review_approved",
                 f"申报包 #{row['package_no']} {REVIEW_ROLE_LABELS[role]} {actor} 复核通过"
                 + (f"，意见：{comment}" if comment else ""))
            # 另一角色也已通过 → 会签完成
            if other is not None and other["decision"] == REVIEW_APPROVE:
                new_status = PACKAGE_APPROVED
                conn.execute("UPDATE declaration_package SET status = ? WHERE id = ?",
                             (new_status, package_id))
                _log(conn, shipment_id, actor, "package_countersigned",
                     f"申报包 #{row['package_no']} 会签通过"
                     f"（{other['actor']}/{REVIEW_ROLE_LABELS[other['role']]}、"
                     f"{actor}/{REVIEW_ROLE_LABELS[role]}），等待"
                     f"{('指定操作人 ' + row['designated_operator']) if row['designated_operator'] else '操作人'}"
                     "放行")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"status": new_status, "rejected": new_status == PACKAGE_REJECTED}


def release_package(conn: sqlite3.Connection, package_id: int, actor: str) -> dict:
    """由指定操作人把会签通过的包执行放行。

    - 只有 approved 可放行：待复核/已驳回一律拒绝（驳回即终态，不能“补签放行”）；
    - 冻结时指定了放行操作人的，必须本人放行；未指定则任何非空操作人可放行；
    - 并发重复放行由写锁 + 状态复查挡住，只有第一次生效；
    - 放行后不可撤回；放行时间/人写入审计。返回 {"status": "released", "duplicate": bool}。
    """
    actor = (actor or "").strip()
    if not actor:
        raise ValueError("放行必须填写操作人姓名")
    try:
        _immediate_tx(conn)
        row = _get_package_row(conn, package_id)
        if row["status"] == PACKAGE_RELEASED:
            conn.rollback()
            if row["released_by"] == actor:
                return {"status": PACKAGE_RELEASED, "duplicate": True}
            raise PackageStateError(
                f"申报包 #{row['package_no']} 已由 {row['released_by']} 于 "
                f"{row['released_at']} 放行，不能重复放行")
        if row["status"] == PACKAGE_REJECTED:
            conn.rollback()
            raise PackageStateError(
                f"申报包 #{row['package_no']} 已被驳回，不能放行；"
                "请按驳回原因重新处理后冻结新包")
        if row["status"] != PACKAGE_APPROVED:
            conn.rollback()
            raise PackageStateError(
                f"申报包 #{row['package_no']} 尚在待复核，关务复核人与主管会签通过前不能放行")
        designated = row["designated_operator"]
        if designated and designated != actor:
            _log(conn, row["shipment_id"], actor, "package_release_denied",
                 f"申报包 #{row['package_no']} 指定放行操作人为 {designated}，"
                 f"{actor} 尝试放行被拒绝")
            conn.commit()
            raise PackageStateError(
                f"该申报包指定的放行操作人是 {designated}，{actor} 无权放行")
        ts = now()
        conn.execute(
            "UPDATE declaration_package SET status = ?, released_by = ?, released_at = ? WHERE id = ?",
            (PACKAGE_RELEASED, actor, ts, package_id))
        _log(conn, row["shipment_id"], actor, "package_released",
             f"申报包 #{row['package_no']} 由 {actor} 放行，成为正式申报依据；"
             "未放行包仅为内部复核稿")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"status": PACKAGE_RELEASED, "duplicate": False}


def get_package(conn: sqlite3.Connection, package_id: int) -> dict:
    """取包内容：payload_json 永不回改，活数据的会签/放行状态从外部表合并进去。"""
    row = _get_package_row(conn, package_id)
    payload = json.loads(row["payload_json"])
    payload["_id"] = row["id"]
    reviews = []
    for r in list_package_reviews(conn, package_id):
        reviews.append({
            "role": r["role"], "role_label": REVIEW_ROLE_LABELS.get(r["role"], r["role"]),
            "actor": r["actor"], "decision": r["decision"],
            "decision_label": "通过" if r["decision"] == REVIEW_APPROVE else "驳回",
            "comment": r["comment"], "created_at": r["created_at"],
        })
    workflow = {
        "status": row["status"],
        "status_label": PACKAGE_STATUS_LABELS.get(row["status"], row["status"]),
        "designated_operator": row["designated_operator"],
        "released_by": row["released_by"],
        "released_at": row["released_at"],
        "reviews": reviews,
        "rejection_reason": next((r["comment"] for r in reviews
                                  if r["decision"] == REVIEW_REJECT), None),
    }
    workflow["is_internal"] = row["status"] != PACKAGE_RELEASED
    payload["workflow"] = workflow
    # JSON 导出也能一眼区分内部复核稿与正式放行包
    payload["_document_class"] = ("released_declaration"
                                  if row["status"] == PACKAGE_RELEASED
                                  else "internal_review_only")
    payload["_document_class_label"] = (
        "正式申报包（已放行）" if row["status"] == PACKAGE_RELEASED else INTERNAL_REVIEW_WATERMARK)
    return payload


def export_markdown(package: dict) -> str:
    """把申报包渲染成带来源与时间的 Markdown 申报核对单。

    头部和结尾醒目标注文件性质：未放行包（待复核/会签通过待放行/已驳回）每页都带
    “仅供内部复核·非正式申报依据”水印；已放行包标注正式放行信息。
    """
    s = package["shipment"]
    wf = package.get("workflow") or {}
    status = wf.get("status", PACKAGE_RELEASED)
    is_released = status == PACKAGE_RELEASED
    banner = ("✅ 正式申报包（已放行）" if is_released
              else ("⛔ 已驳回 · 作废包（仅供内部复核，禁止申报）"
                    if status == PACKAGE_REJECTED
                    else f"⛔ {INTERNAL_REVIEW_WATERMARK}（{wf.get('status_label') or ''}）"))
    lines = []
    lines.append(banner)
    lines.append("")
    lines.append(f"# 申报核对单 — {s['ref']}")
    lines.append("")
    lines.append(f"- 申报包编号：#{package['package_no']}（不可变快照）")
    lines.append(f"- 包状态：**{wf.get('status_label') or PACKAGE_STATUS_LABELS[status]}**")
    lines.append(f"- 冻结时间：{package['frozen_at']}")
    lines.append(f"- 冻结操作人：{package['frozen_by']}")
    if is_released:
        lines.append(f"- 放行操作人：{wf.get('released_by') or '—'}　放行时间：{wf.get('released_at') or '—'}")
    elif wf.get("designated_operator"):
        lines.append(f"- 指定放行操作人：{wf['designated_operator']}（会签通过后仅其本人可放行）")
    lines.append(f"- 客户：{s.get('customer') or '—'}　货描：{s.get('description') or '—'}")
    if s.get("deadline"):
        lines.append(f"- 报关截止：{s['deadline']}")
    lines.append("")

    # 会签记录：两人、不同人、各自意见与时间；驳回原因单列
    reviews = wf.get("reviews") or []
    if reviews or not is_released:
        lines.append("## 〇、会签与放行记录")
        lines.append("")
        if reviews:
            lines.append("| 角色 | 操作人 | 结论 | 意见/驳回原因 | 时间 |")
            lines.append("|---|---|---|---|---|")
            for r in reviews:
                lines.append(f"| {r['role_label']} | {r['actor']} | {r['decision_label']} "
                             f"| {r['comment'] or '—'} | {r['created_at']} |")
            lines.append("")
        if status == PACKAGE_PENDING:
            done = {r["role"] for r in reviews}
            waiting = [REVIEW_ROLE_LABELS[r] for r in (REVIEW_CUSTOMS, REVIEW_SUPERVISOR)
                       if r not in done]
            if waiting:
                lines.append(f"> ⛔ {INTERNAL_REVIEW_WATERMARK} 尚待：{'、'.join(waiting)}会签"
                             "（两人必须不同）。")
                lines.append("")
        elif status == PACKAGE_APPROVED:
            who = f"指定操作人 {wf['designated_operator']}" if wf.get("designated_operator") else "操作人"
            lines.append(f"> ⛔ {INTERNAL_REVIEW_WATERMARK} 会签已通过，尚待 {who} 执行放行。")
            lines.append("")
        elif status == PACKAGE_REJECTED:
            reason = wf.get("rejection_reason") or "（未填写）"
            lines.append(f"> ⛔ 本包已被驳回并作废，禁止放行、禁止申报。驳回原因：{reason}")
            lines.append(">")
            lines.append("> 业务按驳回原因重新处理后，只能冻结新的申报包重新会签；"
                         "旧包内容不可修改，旧会签意见不可挪用到新包。")
            lines.append("")

    lines.append("## 一、申报采用值")
    lines.append("")
    lines.append("| 字段 | 申报采用值 | 依据 | 供应商 | 货代 | 仓库 |")
    lines.append("|---|---|---|---|---|---|")
    for field in FIELDS:
        e = package["declared_fields"][field]
        by = e["values_by_source"]
        if e.get("adoption_basis") == CLOSE_TOLERATED:
            basis = (f"容差规则 v{e.get('tolerance_rule_version')}（采用"
                     f"{SOURCE_LABELS[(e.get('basis_sources') or [''])[0]] if e.get('basis_sources') else '—'}值）")
        else:
            basis = "、".join(SOURCE_LABELS[x] for x in e.get("basis_sources", [])) or "—（三方分歧，未采用）"
        adopted = e["adopted_value"] or "**未冻结（仍有分歧）**"
        cells = []
        for src in SOURCES:
            doc = package["documents"].get(src)
            cells.append(f"{doc[field]}（v{doc['version']}，{doc['submitted_by'] or '无名'}，{doc['submitted_at']}）"
                         if doc and doc.get(field) else "未提交")
        lines.append(f"| {FIELD_LABELS[field]} | {adopted} | {basis} | {cells[0]} | {cells[1]} | {cells[2]} |")
    lines.append("")

    rules = package.get("tolerance_rules") or []
    if rules:
        lines.append("## 二、冻结时的容差规则（版本化，按冻结当时固化）")
        lines.append("")
        lines.append("| 字段 | 版本 | 状态 | 规则 | 维护人 | 时间 |")
        lines.append("|---|---|---|---|---|---|")
        for r in rules:
            lines.append(f"| {FIELD_LABELS[r['field']]} | v{r['version']} | {r['status_label']} "
                         f"| {_describe_rule(r['mode'], r['config'])} "
                         f"| {r['created_by'] or '—'} | {r['created_at']} |")
        lines.append("")
        lines.append("> 本包只按冻结当时的现行版本判定；冻结后规则被修改/停用不影响本包内容。")
        lines.append("")

    lines.append("## 三、差异处理结论")
    lines.append("")
    if not package["discrepancies"]:
        lines.append("本票货未产生任何字段差异。")
    for d in package["discrepancies"]:
        state = d["status_label"]
        if d["status"] != STATUS_RESOLVED and d["is_reconciled"]:
            if d.get("close_reason") == CLOSE_TOLERATED:
                state = f"容差内视为一致（规则 v{d['rule_version']}），待确认"
            else:
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
        if d.get("close_reason_label"):
            lines.append(f"- 消除依据：{d['close_reason_label']}"
                         + (f"，命中规则 v{d['rule_version']}" if d["close_reason"] == CLOSE_TOLERATED else ""))
            ev = d.get("rule_evidence") or {}
            if ev.get("mode") == "abs":
                lines.append(f"- 容差命中依据：绝对偏差 {ev.get('spread')} ≤ 允许 ±{ev.get('limit')}")
            elif ev.get("mode") == "pct":
                lines.append(f"- 容差命中依据：相对偏差 {ev.get('ratio_pct')}%（按{ev.get('basis_desc')} "
                             f"{ev.get('base')}，差 {ev.get('spread')}）≤ 允许 ±{ev.get('limit_pct_input')}%")
        if d.get("waivers"):
            lines.append("- 豁免记录（限时放行证据）：")
            for w in d["waivers"]:
                lines.append(f"  - {w['granted_at']} 由 {w['granted_by']} 批准至 "
                             f"{w['expires_at'].replace('T', ' ')}：{w['reason']}"
                             f" → {w['status_label']}"
                             + (f"（{w['ended_at']}）" if w.get("ended_at") else "")
                             + (f"，撤销人：{w['revoked_by']}" if w.get("revoked_by") else ""))
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
        lines.append("## 四、冻结时风险提示")
        lines.append("")
        for w in package["warnings"]:
            lines.append(f"- ⚠️ {w}")
        lines.append("")
    lines.append("---")
    lines.append("本文件由冻结时刻的数据库快照生成，后续资料版本更新不影响本申报包内容。")
    if is_released:
        lines.append(f"本包已经关务复核人、关务主管会签并由 {wf.get('released_by') or '—'} "
                     f"于 {wf.get('released_at') or '—'} 放行，属正式申报依据。")
    else:
        lines.append(f"⛔ {INTERNAL_REVIEW_WATERMARK}：本包{'已被驳回作废、' if status == PACKAGE_REJECTED else ''}"
                     "尚未完成会签放行流程，不得用于申报；放行后请重新下载正式版本。")
    return "\n".join(lines)
