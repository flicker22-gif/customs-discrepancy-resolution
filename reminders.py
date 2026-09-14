"""截止时间提醒与升级服务（独立进程运行，与资料/处理流程解耦）。

设计要点：
1. 故障隔离：提醒服务只读业务数据、只写 reminder_event/notification/audit_log；
   通知器（群机器人/邮件/短信）失败只记录 notification.status='failed'，绝不影响
   资料录入与差异处理。提醒服务停了，Web 和 core 照常可用。
2. 不刷屏：每条未解决差异在每个“风险轮次(episode)”内，due_soon / overdue 各只产生
   一条 reminder_event（数据库 UNIQUE 约束兜底）。差异解决或一直未解决都不会重复提醒；
   只有差异解决后再次冲突（episode+1）才会开启新一轮提醒。
3. 升级路径：临近截止（deadline - warn_hours 起）→ @负责人 due_soon；
   超时后 → 升级通知关务主管（escalation_target）overdue。

运行：
    python3 reminders.py --once            # 扫描一遍（cron 可每分钟调）
    python3 reminders.py --loop --interval 60
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from datetime import datetime, timedelta

import core

LEVEL_DUE_SOON = "due_soon"
LEVEL_OVERDUE = "overdue"
LEVEL_LABELS = {LEVEL_DUE_SOON: "临近截止", LEVEL_OVERDUE: "已超时（升级）"}


# ---------------------------------------------------------------- 通知器

class Notifier:
    """通知渠道接口：send 失败应抛异常，由扫描器捕获并记为 failed，可后续重试。"""

    def send(self, target: str, subject: str, content: str) -> None:
        raise NotImplementedError


class LogNotifier(Notifier):
    """默认通知器：打印到标准输出（模拟企业微信群机器人/邮件）。"""

    def __init__(self):
        self.sent = []

    def send(self, target, subject, content):
        line = f"[通知→{target}] {subject} | {content}"
        print(line)
        self.sent.append(line)


class FlakyNotifier(Notifier):
    """测试/演示用：前 fail_times 次发送抛异常，之后恢复——验证失败隔离与重试。"""

    def __init__(self, fail_times=1):
        self.remaining_failures = fail_times
        self.calls = 0

    def send(self, target, subject, content):
        self.calls += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise ConnectionError("群机器人网关超时（模拟故障）")


# ---------------------------------------------------------------- 风险判定

def assess_shipments(conn: sqlite3.Connection, at: datetime | None = None) -> list[dict]:
    """找出当前处于风险窗口、且有未解决差异的货票及每条差异应处的提醒级别。

    返回 [{"shipment": row, "deadline": dt, "level": ..., "discrepancies": [row...]}]

    两类差异不构成风险：已核对一致（仅待人工确认解决）；处于有效豁免期内（限时放行）。
    """
    at = at or datetime.now()
    result = []
    for s in conn.execute("SELECT * FROM shipment WHERE deadline IS NOT NULL"):
        dl = core.parse_dt(s["deadline"])
        if dl is None:
            continue
        if at < dl - timedelta(hours=int(s["warn_hours"])):
            continue  # 还没进入提醒窗口
        level = LEVEL_OVERDUE if at > dl else LEVEL_DUE_SOON
        discs = list(conn.execute(
            "SELECT * FROM discrepancy WHERE shipment_id = ? "
            "AND status != 'resolved' AND is_reconciled = 0 "
            "AND active_waiver_id IS NULL ORDER BY id",
            (s["id"],)))
        if not discs:
            continue
        result.append({"shipment": s, "deadline": dl, "level": level, "discrepancies": discs})
    return result


# ---------------------------------------------------------------- 扫描

DEFAULT_ESCALATION_TARGET = "关务主管"


def _clean_target(escalation_target: str | None) -> str:
    target = (escalation_target or "").strip()
    return target or DEFAULT_ESCALATION_TARGET


def scan_once(conn: sqlite3.Connection, notifier: Notifier,
              escalation_target: str = DEFAULT_ESCALATION_TARGET,
              at: datetime | None = None) -> dict:
    """扫描一轮：去重产生提醒事件 → 投递通知（新事件+失败重试）。

    escalation_target: 超时升级联系人（未认领差异的临近提醒也兜底发给他）。
    统计 {"events_new", "notified", "failed", "retried_ok", "suppressed",
          "waivers_expired", "waivers_reopened"}
    """
    at = at or datetime.now()
    target_name = _clean_target(escalation_target)
    stats = {"events_new": 0, "notified": 0, "failed": 0, "retried_ok": 0, "suppressed": 0,
             "waivers_expired": 0, "waivers_reopened": 0}

    # 先做豁免到期重开：到期且仍冲突的差异复用原行 episode+1，
    # 随后同一轮扫描即按新 episode 恢复提醒（豁免期内完全静默）。
    expired = core.expire_due_waivers(conn, at=at)
    stats["waivers_expired"] = expired["expired"]
    stats["waivers_reopened"] = expired["reopened"]

    for item in assess_shipments(conn, at):
        s, level = item["shipment"], item["level"]
        for d in item["discrepancies"]:
            event = _ensure_event(conn, d, level, at, target_name)
            if event["is_new"]:
                stats["events_new"] += 1
            elif not event["has_pending_or_failed"]:
                stats["suppressed"] += 1  # 本轮同级别已成功通知 → 刷屏拦截

    conn.commit()
    _deliver_pending(conn, notifier, stats)
    return stats


def _ensure_event(conn, discrepancy, level, at,
                  escalation_target: str = DEFAULT_ESCALATION_TARGET) -> dict:
    """幂等创建提醒事件（UNIQUE(discrepancy_id, level, episode) 兜底防重）。"""
    existing = conn.execute(
        "SELECT id FROM reminder_event WHERE discrepancy_id = ? AND level = ? AND episode = ?",
        (discrepancy["id"], level, discrepancy["episode"]),
    ).fetchone()
    if existing is not None:
        pending = conn.execute(
            "SELECT COUNT(*) FROM notification WHERE event_id = ? AND status IN ('pending','failed')",
            (existing["id"],)).fetchone()[0]
        return {"id": existing["id"], "is_new": False, "has_pending_or_failed": bool(pending)}

    cur = conn.execute(
        "INSERT INTO reminder_event (shipment_id, discrepancy_id, level, episode, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (discrepancy["shipment_id"], discrepancy["id"], level, discrepancy["episode"],
         at.isoformat(timespec="seconds")),
    )
    event_id = cur.lastrowid
    _create_notifications(conn, event_id, discrepancy, level, escalation_target)
    core_shipment_log = (
        f"提醒升级：差异【{core.FIELD_LABELS[discrepancy['field']]}】"
        f"{LEVEL_LABELS[level]}（第 {discrepancy['episode']} 轮）")
    conn.execute(
        "INSERT INTO audit_log (shipment_id, actor, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (discrepancy["shipment_id"], "提醒服务", "reminder_raised",
         core_shipment_log, at.isoformat(timespec="seconds")))
    return {"id": event_id, "is_new": True, "has_pending_or_failed": True}


def _create_notifications(conn, event_id, discrepancy, level,
                          escalation_target: str = DEFAULT_ESCALATION_TARGET) -> None:
    """通知规则（不因自定义联系人而改变）：
    - 临近：通知负责人；未认领则兜底通知升级联系人；
    - 超时：通知负责人（如有）+ 必然升级给 escalation_target。
    """
    targets = []
    if level == LEVEL_DUE_SOON:
        targets.append((discrepancy["owner"] or escalation_target, "owner_or_lead"))
    else:
        if discrepancy["owner"]:
            targets.append((discrepancy["owner"], "owner"))
        targets.append((escalation_target, "escalation"))

    s = conn.execute("SELECT ref, deadline FROM shipment WHERE id = ?",
                     (discrepancy["shipment_id"],)).fetchone()
    vals = core.discrepancy_values(discrepancy)
    raw = " / ".join(f"{core.SOURCE_LABELS[k]}:{v['raw']}" for k, v in vals.items()) or "（资料未齐）"
    field_label = core.FIELD_LABELS[discrepancy["field"]]
    for target, role in targets:
        if level == LEVEL_OVERDUE and role == "escalation":
            content = (f"[升级] 货票 {s['ref']} 已超过报关截止 {s['deadline']}，"
                       f"差异【{field_label}】仍未解决（当前值 {raw}，负责人："
                       f"{discrepancy['owner'] or '未认领'}），请立即处理以免误船期。")
        elif level == LEVEL_OVERDUE:
            content = (f"[超时] 货票 {s['ref']} 已超过报关截止 {s['deadline']}，"
                       f"你负责的差异【{field_label}】仍未解决（{raw}）。")
        else:
            who = "你负责的" if discrepancy["owner"] else "有"
            content = (f"[临近截止] 货票 {s['ref']} 报关截止 {s['deadline']}，"
                       f"{who}差异【{field_label}】未解决（{raw}），请尽快补件核对。")
        conn.execute(
            """INSERT INTO notification (event_id, channel, target, content, status, created_at)
               VALUES (?, 'im', ?, ?, 'pending', ?)""",
            (event_id, target, content, core.now()))


def _deliver_pending(conn, notifier, stats) -> None:
    rows = conn.execute(
        "SELECT n.*, e.discrepancy_id FROM notification n JOIN reminder_event e ON n.event_id = e.id "
        "WHERE n.status IN ('pending','failed') ORDER BY n.id").fetchall()
    for n in rows:
        try:
            notifier.send(n["target"], "报关差异提醒", n["content"])
        except Exception as exc:  # 通知渠道故障：记录失败，核心流程不受影响，下次扫描重试
            conn.execute(
                "UPDATE notification SET status='failed', attempts=attempts+1, last_error=? WHERE id=?",
                (f"{type(exc).__name__}: {exc}", n["id"]))
            stats["failed"] += 1
            conn.commit()
            continue
        if n["status"] == "failed":
            stats["retried_ok"] += 1
        conn.execute(
            "UPDATE notification SET status='sent', attempts=attempts+1, last_error='', sent_at=? "
            "WHERE id=?",
            (core.now(), n["id"]))
        stats["notified"] += 1
        conn.commit()


def retry_failed(conn: sqlite3.Connection, notifier: Notifier) -> dict:
    """显式重试所有失败/待发通知（升级联系人在事件创建时已固化在通知记录里）。"""
    stats = {"events_new": 0, "notified": 0, "failed": 0, "retried_ok": 0, "suppressed": 0}
    _deliver_pending(conn, notifier, stats)
    return stats


# ---------------------------------------------------------------- CLI

def main() -> None:
    parser = argparse.ArgumentParser(description="报关差异截止提醒/升级服务")
    parser.add_argument("--once", action="store_true", help="扫描一轮后退出")
    parser.add_argument("--loop", action="store_true", help="持续运行")
    parser.add_argument("--interval", type=int, default=60, help="循环扫描间隔秒数")
    parser.add_argument("--escalation-target",
                        default=os.environ.get("ESCALATION_TARGET", DEFAULT_ESCALATION_TARGET),
                        help="超时升级联系人（默认：关务主管；也可用环境变量 ESCALATION_TARGET）")
    args = parser.parse_args()

    conn = core.connect(os.environ.get("CUSTOMS_DB"))
    core.init_db(conn)
    notifier = LogNotifier()
    target = _clean_target(args.escalation_target)

    def run():
        stats = scan_once(conn, notifier, escalation_target=target)
        print(f"[{core.now()}] 扫描完成（升级联系人：{target}）：新事件 {stats['events_new']}，"
              f"发出 {stats['notified']}，失败 {stats['failed']}，"
              f"重试成功 {stats['retried_ok']}，拦截重复 {stats['suppressed']}，"
              f"豁免到期 {stats['waivers_expired']}（重开 {stats['waivers_reopened']}）")

    if args.loop:
        while True:
            run()
            time.sleep(args.interval)
    else:
        run()


if __name__ == "__main__":
    main()
