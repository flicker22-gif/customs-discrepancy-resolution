#!/usr/bin/env python3
"""演示第二阶段：截止提醒/升级（防刷屏、故障隔离）+ 供应商受限补件入口。

时间线完全由注入的 `at` 参数驱动，无需真的等待。
直接运行：python3 demo2.py
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


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="customs-demo2-")
    conn = core.connect(os.path.join(tmp, "demo2.db"))
    core.init_db(conn)

    t0 = datetime(2026, 9, 15, 9, 0)
    deadline = t0 + timedelta(hours=2)          # 11:00 截止
    show(f"步骤 1：建票，报关截止 {deadline:%H:%M}，提前 1 小时（{t0 + timedelta(hours=1):%H:%M} 起）进入提醒窗口")
    sid = core.create_shipment(
        conn, "SH-20260915-07", customer="远航贸易", actor="小林",
        deadline=deadline.isoformat(timespec="minutes"), warn_hours=1)

    core.submit_document(conn, sid, "supplier", "保温杯", "1000", "CBMU1", actor="供应商小王")
    core.submit_document(conn, sid, "forwarder", "真空保温杯", "1020", "CBMU1", actor="货代小陈")
    core.submit_document(conn, sid, "warehouse", "保温杯", "1000", "CBMU1", actor="仓库小赵")
    discs = {d["field"]: d for d in core.list_discrepancies(conn, sid)}
    name_did, qty_did = discs["product_name"]["id"], discs["quantity"]["id"]
    print(f"两处差异：品名 #{name_did}、数量 #{qty_did}")
    core.claim_discrepancy(conn, qty_did, owner="关务小李")
    print("数量差异由关务小李认领；品名差异暂无人认领")

    show("步骤 2：t0 扫描——尚未进入提醒窗口，无任何提醒")
    stats = reminders.scan_once(conn, reminders.LogNotifier(), at=t0)
    print("统计：", stats)
    assert stats["events_new"] == 0 and stats["notified"] == 0

    show("步骤 3：t0+1h 进入窗口——两条差异各产生一条【临近】提醒，不刷屏")
    t1 = t0 + timedelta(hours=1)
    stats = reminders.scan_once(conn, reminders.LogNotifier(), at=t1)
    print("统计：", stats, "（品名未认领→通知关务主管；数量→通知负责人小李）")
    assert stats["events_new"] == 2 and stats["notified"] == 2 and stats["failed"] == 0

    stats = reminders.scan_once(conn, reminders.LogNotifier(), at=t1 + timedelta(minutes=5))
    print("5 分钟后再扫：", stats, "→ 同轮同级已通知，全部拦截")
    assert stats["events_new"] == 0 and stats["notified"] == 0 and stats["suppressed"] == 2

    show("步骤 4：截止后 10 分钟已超时——每条差异升级【超时】通知关务主管（负责人也保留一份）")
    t2 = deadline + timedelta(minutes=10)
    stats = reminders.scan_once(conn, reminders.LogNotifier(), at=t2)
    print("统计：", stats)
    assert stats["events_new"] == 2 and stats["notified"] == 3  # 品名只给主管；数量给小李+主管
    events = conn.execute(
        "SELECT level, COUNT(*) c FROM reminder_event GROUP BY level").fetchall()
    print("事件表：", {r["level"]: r["c"] for r in events})

    show("步骤 5：通知渠道故障——群机器人超时；资料与处理流程不受影响，失败可重试")
    flaky = reminders.FlakyNotifier(fail_times=1)
    # 新制造一条差异（箱单号），制造新的通知让故障渠道投递
    core.submit_document(conn, sid, "warehouse", "保温杯", "1000", "CBMU9", actor="仓库小赵")
    stats = reminders.scan_once(conn, flaky, at=t2 + timedelta(minutes=10))
    print("故障轮统计：", stats)
    assert stats["failed"] >= 1 and flaky.calls >= 1

    # 与此同时：核心流程照常可用（提醒服务故障期间）
    core.claim_discrepancy(conn, name_did, owner="关务小张")
    core.add_supplement(conn, name_did, note="已发供应商补件链接", actor="关务小张")
    print("故障期间：认领、补件记录等核心操作全部正常 ✓")

    stats = reminders.scan_once(conn, reminders.LogNotifier(), at=t2 + timedelta(minutes=15))
    print("渠道恢复后再扫：", stats, "→ 之前失败的通知重试成功")
    assert stats["retried_ok"] >= 1 and stats["failed"] == 0

    # ------------------------------------------------------------- 供应商门户
    show("步骤 6：内部为供应商签发受限补件链接——只允许补【品名】和【数量】")
    token = core.create_supplement_token(conn, sid, fields=["product_name", "quantity"],
                                         contact="供应商小王", actor="关务小张")
    ctx = core.portal_context(conn, token)
    print("门户可见字段：", [core.FIELD_LABELS[f] for f in ctx["allowed_fields"]])
    print("门户拿到的本方资料：",
          {f: ctx["document"][f] for f in core.FIELDS})
    print("门户待核实项（不含他方具体数值）：",
          [(d["label"], d["our_value"]) for d in ctx["open_discrepancies"]])
    # 安全断言：门户上下文里没有任何货代/仓库数据
    ctx_keys = str(ctx)
    assert "真空保温杯" not in ctx_keys and "1020" not in ctx_keys and "CBMU9" not in ctx_keys
    print("门户上下文中不含货代/仓库的任何数据 ✓")

    show("步骤 7：供应商篡改表单尝试改箱单号（未授权）→ 拒绝并留痕")
    try:
        core.supplier_submit(conn, token, {"carton_no": "CBMU9"}, actor="供应商小王")
        raise AssertionError("应当拒绝越权字段")
    except core.TokenError as e:
        print("已拒绝：", e)

    show("步骤 8：供应商合规补件——品名改为一致，数量确认 1020；自动挂回差异并重核")
    r1 = core.supplier_submit(conn, token, {"product_name": "保温杯", "quantity": "1020"},
                              actor="供应商小王")
    print("提交结果：", r1)
    assert not r1["duplicate"] and r1["version"] == 2

    show("步骤 9：供应商重复点提交（内容没变）→ 忽略，不多出一份资料")
    r2 = core.supplier_submit(conn, token, {"product_name": "保温杯", "quantity": "1020"},
                              actor="供应商小王")
    ver = core.get_document(conn, sid, "supplier")["version"]
    print(f"结果 duplicate={r2['duplicate']}，供应商资料版本仍为 v{ver}")
    assert r2["duplicate"] and ver == 2

    # 品名差异中本就是供应商正确、货代多写“真空”二字：内部督促货代更正；
    # 仓库箱单号改回、数量确认 1020 后，三字段全部一致
    core.submit_document(conn, sid, "forwarder", "保温杯", "1020", "CBMU1", actor="货代小陈")
    core.submit_document(conn, sid, "warehouse", "保温杯", "1020", "CBMU1", actor="仓库小赵")
    discs = {d["field"]: d for d in core.list_discrepancies(conn, sid)}
    assert all(d["is_reconciled"] for d in discs.values())
    print("供应商补件自动挂回了数量差异；货代内部更正品名、仓库确认数量/箱单后，三字段全部【核对一致】✓")

    show("步骤 10：核对一致期间提醒应停止；若资料再次冲突则开启新一轮提醒（仍不重复）")
    t3 = t2 + timedelta(hours=1)
    stats = reminders.scan_once(conn, reminders.LogNotifier(), at=t3)
    print("全部一致后扫描：", stats, "（无新事件、无新消息；扫描到未确认项也只是拦截不发）")
    assert stats["events_new"] == 0 and stats["notified"] == 0

    # 负责人确认解决全部“核对一致”的差异；随后货代新版本又把数量改错 → 同一差异行 episode 2
    for d in discs.values():
        if d["is_reconciled"] and d["status"] != "resolved":
            core.resolve_discrepancy(conn, d["id"], actor="关务小张")
    core.submit_document(conn, sid, "forwarder", "保温杯", "999", "CBMU1", actor="货代小陈")
    qty_row = conn.execute("SELECT * FROM discrepancy WHERE id=?", (qty_did,)).fetchone()
    print(f"数量差异复用原行 #{qty_did}，episode={qty_row['episode']}，状态={qty_row['status']}")
    assert qty_row["episode"] == 2 and qty_row["status"] == "open"

    t4 = t3 + timedelta(hours=1)
    stats = reminders.scan_once(conn, reminders.LogNotifier(), at=t4)
    print("新一轮超时扫描：", stats, "→ 第 2 轮 overdue 事件只产生一次")
    assert stats["events_new"] == 1
    stats = reminders.scan_once(conn, reminders.LogNotifier(), at=t4)
    assert stats["events_new"] == 0 and stats["suppressed"] == 1
    print("再扫一次：新事件 0、刷屏拦截 1 ✓")

    show("留痕节选（提醒与门户相关）")
    logs = [l for l in core.list_logs(conn, sid)
            if l["action"] in ("reminder_raised", "token_created", "portal_denied",
                               "portal_submitted", "doc_duplicate_ignored",
                               "discrepancy_reopened")]
    for l in logs:
        print(f"[{l['created_at']}] {l['actor']:<8} {l['detail']}")

    print(f"\n{SEP}演示通过：临近/超时两级提醒且不刷屏；通知故障不影响核心、可重试；\n"
          f"          供应商只能补指定货的指定字段、看不到他方数据、自动挂回重核、重复提交不多版。\n"
          f"演示数据库：{os.path.join(tmp, 'demo2.db')}{SEP}")


if __name__ == "__main__":
    main()
