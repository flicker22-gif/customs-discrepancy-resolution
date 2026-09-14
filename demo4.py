#!/usr/bin/env python3
"""演示第四期：容差规则（版本化）→ 限时豁免 → 到期/撤销/资料升版重开，同一条流程。

时序覆盖：
  规则命中（保留原始三方值/规则版本/命中依据）→ 收紧重开 → 豁免（原因/操作人/到期）
  → 豁免期静默、待发通知撤销 → 资料升版仍冲突→复用原行 episode+1 恢复提醒
  → 再豁免 → 到期同轮重开 → 冻结申报包固化规则/豁免证据 → 后来配置改不动旧包

直接运行：python3 demo4.py
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

import core
import reminders

SEP = "=" * 72


def show(title: str) -> None:
    print(f"\n{SEP}\n{title}\n{SEP}")


def disc_row(conn, sid):
    return next(d for d in core.list_discrepancies(conn, sid) if d["field"] == "quantity")


def scan(conn, at):
    return reminders.scan_once(conn, reminders.LogNotifier(), at=at)


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="customs-demo4-")
    conn = core.connect(os.path.join(tmp, "demo4.db"))
    core.init_db(conn)

    t0 = datetime(2026, 9, 18, 9, 0)
    deadline = t0 + timedelta(hours=24)

    show("步骤 1：建票（带截止），三方数量 1000 / 1020 / 1000 → 严格比对出差异")
    sid = core.create_shipment(
        conn, "SH-20260918-04", customer="远航贸易", actor="小林",
        deadline=deadline.isoformat(timespec="minutes"), warn_hours=24)
    core.submit_document(conn, sid, "supplier", "不锈钢保温杯", "1000", "CBMU1", actor="供应商小王")
    core.submit_document(conn, sid, "forwarder", "不锈钢保温杯", "1020", "CBMU1", actor="货代小陈")
    core.submit_document(conn, sid, "warehouse", "不锈钢保温杯", "1000", "CBMU1", actor="仓库小赵")
    d = disc_row(conn, sid)
    print(f"差异 #{d['id']}（数量），episode={d['episode']}，严格不一致，原始三方值已保留")
    # 用一次失败的通知制造“待重试”记录，后面验证豁免会把它撤掉
    flaky = reminders.FlakyNotifier(fail_times=1)
    s = reminders.scan_once(conn, flaky, at=t0)
    pending_n = conn.execute(
        "SELECT COUNT(*) FROM notification n JOIN reminder_event e ON n.event_id=e.id "
        "WHERE e.discrepancy_id=? AND n.status='failed'", (d["id"],)).fetchone()[0]
    print(f"窗口扫描：新事件 {s['events_new']}，通知失败待重试 {pending_n} 条（模拟网关抖动）")

    show("步骤 2：关务维护带版本的容差规则——数量 ±20（绝对差），v1")
    core.save_rule(conn, sid, "quantity", "abs", {"abs": "20", "adopt_source": "supplier"},
                   note="海关允许 2% 以内短装", actor="关务小李")
    d = disc_row(conn, sid)
    ev = __import__("json").loads(d["rule_evidence_json"])
    print(f"重核结果：is_reconciled={d['is_reconciled']}，close_reason={d['close_reason']}，"
          f"命中规则 v{d['rule_version']}")
    print(f"命中依据：实际偏差 {ev['spread']} ≤ 允许 ±{ev['limit']}；"
          f"申报采用「{core.SOURCE_LABELS[ev['adopt_source']]}」值 {ev['adopted_value']}")
    vals = __import__("json").loads(d["values_json"])
    print("原始三方值未被抹平：", {k: v["raw"] for k, v in vals.items()})

    show("步骤 3：规则收紧为 ±5（v2，旧 v1 保留）→ 不再命中，复用原差异行重开 episode 2")
    core.save_rule(conn, sid, "quantity", "abs", {"abs": "5"}, note="口岸口径收紧",
                   actor="关务小张")
    d = disc_row(conn, sid)
    print(f"差异 id 仍是 #{d['id']}：is_reconciled={d['is_reconciled']}，"
          f"close_reason={d['close_reason']}，rule_version={d['rule_version']}，episode={d['episode']}")
    print("规则版本链：", [(r["version"], core.RULE_STATUS_LABELS[r["status"]], r["config_json"])
                          for r in core.list_rules(conn, sid)])

    show("步骤 4：现场必须限时放行 → 申请豁免（原因/操作人/到期必填）")
    wid = core.grant_waiver(
        conn, d["id"], reason="船期不等人，海关口头同意先放行后补正",
        granted_by="关务主管周", expires_at=(t0 + timedelta(hours=30)).isoformat(timespec="minutes"))
    print(f"豁免 #{wid} 生效；差异行 active_waiver_id={disc_row(conn, sid)['active_waiver_id']}")
    cancelled = conn.execute(
        "SELECT COUNT(*) FROM notification n JOIN reminder_event e ON n.event_id=e.id "
        "WHERE e.discrepancy_id=? AND n.status='cancelled'", (d["id"],)).fetchone()[0]
    print(f"授予时已排队/待重试的通知被撤销 {cancelled} 条")
    s1 = scan(conn, at=t0 + timedelta(hours=1))
    s2 = scan(conn, at=deadline + timedelta(minutes=1))
    print(f"豁免期内两轮扫描（含超时点）：新事件 {s1['events_new']}/{s2['events_new']}，不催办")

    show("步骤 5：货代资料升版 1020→1018，仍超出 ±5 → 豁免自动终结，episode 3，恢复提醒")
    core.submit_document(conn, sid, "forwarder", "不锈钢保温杯", "1018", "CBMU1", actor="货代小陈")
    d = disc_row(conn, sid)
    w = core.get_waiver(conn, wid)
    print(f"豁免状态={w['status']}；差异 episode={d['episode']}，active_waiver_id={d['active_waiver_id']}")
    s = scan(conn, at=deadline + timedelta(minutes=10))
    print("本轮扫描新事件：", s["events_new"], "（第 3 轮提醒恢复，同轮仍只发一次）")

    show("步骤 6：再次豁免；到期那一刻的扫描——同轮先重开 episode 4 再提醒")
    wid2 = core.grant_waiver(
        conn, d["id"], reason="等更正版 PL，限时到 26 日 09:00",
        granted_by="关务主管周", expires_at=(t0 + timedelta(hours=26)).isoformat(timespec="minutes"))
    quiet = scan(conn, at=t0 + timedelta(hours=25))
    due = scan(conn, at=t0 + timedelta(hours=26, minutes=1))
    print(f"豁免期扫描：新事件 {quiet['events_new']}")
    print(f"到期扫描：豁免到期 {due['waivers_expired']}（重开 {due['waivers_reopened']}），"
          f"新事件 {due['events_new']}")
    d = disc_row(conn, sid)
    print(f"差异 episode={d['episode']}；豁免 #{wid2} 状态="
          f"{core.WAIVER_STATUS_LABELS[core.get_waiver(conn, wid2)['status']]}")

    show("步骤 7：第三次豁免后冻结申报包——规则版本链 + 豁免证据全部随包固化")
    wid3 = core.grant_waiver(
        conn, d["id"], reason="更正件次晨到，先申报",
        granted_by="关务主管周", expires_at=(t0 + timedelta(hours=36)).isoformat(timespec="minutes"))
    r = core.freeze_package(conn, sid, actor="关务小李")
    print("冻结结果：", r, "（豁免中差异不算未解决，无需强制确认）")
    pid = core.list_packages(conn, sid)[0]["id"]
    pkg = core.get_package(conn, pid)
    print("包内容差规则版本：", [(x["field"], x["version"], x["status_label"])
                               for x in pkg["tolerance_rules"]])
    frozen_d = next(x for x in pkg["discrepancies"] if x["field"] == "quantity")
    print("包内豁免证据：", [(x["granted_by"], x["reason"][:12], x["status_label"])
                            for x in frozen_d["waivers"]])

    show("步骤 8：冻结后撤销豁免、放宽规则 v3 又停用、资料再升版——旧包字节级不变")
    core.revoke_waiver(conn, wid3, actor="别人")
    core.save_rule(conn, sid, "quantity", "abs", {"abs": "50"}, actor="别人")
    core.revoke_rule(conn, sid, "quantity", actor="别人")
    core.submit_document(conn, sid, "forwarder", "不锈钢保温杯", "1300", "CBMU1", actor="货代小陈")
    pkg_after = core.get_package(conn, pid)
    assert pkg_after == pkg, "旧包被改写！"
    print("旧包规则版本仍为：", [(x["version"], x["status_label"]) for x in pkg_after["tolerance_rules"]])
    fw = next(x for x in pkg_after["discrepancies"] if x["field"] == "quantity")["waivers"]
    print("旧包内第三条豁免状态仍为：",
          next(x["status_label"] for x in fw if x["id"] == wid3), "（活数据已撤销，包内不动）")
    print("活数据规则链：", [(x["version"], core.RULE_STATUS_LABELS[x["status"]])
                            for x in core.list_rules(conn, sid)])

    show("留痕节选（规则/豁免/重开）")
    for l in core.list_logs(conn, sid):
        if l["action"].startswith(("rule_", "waiver_")) or "豁免" in l["detail"]:
            print(f"[{l['created_at']}] {l['actor']:<6} {l['detail']}")

    print(f"\n{SEP}演示通过：判定—豁免—到期重开是同一条流程；原始值/规则版本/命中依据全程保留；\n"
          f"          豁免期静默、三种终结方式都复用原差异行；冻结证据不被后来配置改写。\n"
          f"演示数据库：{os.path.join(tmp, 'demo4.db')}{SEP}")


if __name__ == "__main__":
    main()
