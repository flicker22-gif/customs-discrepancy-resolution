"""单元测试：比对引擎、幂等（版本更新/重复提交不产生第二条差异）、认领/补件/解决约束、
截止提醒/升级（防刷屏、故障隔离）、供应商受限门户、申报包会签/放行状态机、Flask 冒烟。"""
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta

import core
import reminders


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = core.connect(os.path.join(self.tmp, "test.db"))
        core.init_db(self.conn)
        self.sid = core.create_shipment(self.conn, "T-001", actor="tester")

    def tearDown(self):
        self.conn.close()

    def submit_all(self, **kw):
        defaults = dict(product_name="杯子", quantity="100", carton_no="BOX-1")
        defaults.update(kw)
        for src in core.SOURCES:
            core.submit_document(self.conn, self.sid, src, actor="t", **defaults)

    # ---- 归一化 ----
    def test_normalization(self):
        self.assertTrue(core.values_match("quantity", {"a": "1,000", "b": "1000.00", "c": "1000"}))
        self.assertTrue(core.values_match("carton_no", {"a": "CBMU123", "b": "cbmu123"}))
        self.assertTrue(core.values_match("product_name", {"a": "不 锈 钢 杯", "b": "不锈钢杯"}))
        self.assertFalse(core.values_match("quantity", {"a": "1000", "b": "1020"}))

    # ---- 基本比对 ----
    def test_all_consistent_no_discrepancy(self):
        self.submit_all()
        self.assertEqual(core.list_discrepancies(self.conn, self.sid), [])
        self.assertEqual(core.get_shipment(self.conn, self.sid)["status"], "resolved")

    def test_single_source_no_discrepancy(self):
        core.submit_document(self.conn, self.sid, "supplier",
                             product_name="X", quantity="1", carton_no="C1", actor="t")
        self.assertEqual(core.list_discrepancies(self.conn, self.sid), [])

    def test_two_conflicts(self):
        core.submit_document(self.conn, self.sid, "supplier", "保温杯", "1000", "C1", actor="t")
        core.submit_document(self.conn, self.sid, "forwarder", "真空保温杯", "1020", "C1", actor="t")
        core.submit_document(self.conn, self.sid, "warehouse", "保温杯", "1000", "C1", actor="t")
        fields = [d["field"] for d in core.list_discrepancies(self.conn, self.sid)]
        self.assertEqual(fields, ["product_name", "quantity"])

    # ---- 幂等：重复提交 / 版本更新 ----
    def test_duplicate_submission_ignored(self):
        self.submit_all(product_name="X", quantity="9", carton_no="C9")
        result = core.submit_document(self.conn, self.sid, "supplier", "X", "9", "C9", actor="t")
        self.assertTrue(result["duplicate"])
        ver = self.conn.execute(
            "SELECT version FROM document WHERE shipment_id=? AND source='supplier'",
            (self.sid,)).fetchone()["version"]
        self.assertEqual(ver, 1)

    def test_version_update_reuses_discrepancy_row(self):
        core.submit_document(self.conn, self.sid, "supplier", "A", "1", "C", actor="t")
        core.submit_document(self.conn, self.sid, "forwarder", "B", "2", "C", actor="t")
        before = {d["field"]: d["id"] for d in core.list_discrepancies(self.conn, self.sid)}
        self.assertEqual(len(before), 2)
        # 货代连续升 3 个版本，差异始终冲突
        for i, (name, qty) in enumerate([("B2", "3"), ("B3", "4"), ("B4", "5")], start=2):
            r = core.submit_document(self.conn, self.sid, "forwarder", name, qty, "C", actor="t")
            self.assertEqual(r["version"], i)
        after = {d["field"]: d["id"] for d in core.list_discrepancies(self.conn, self.sid)}
        self.assertEqual(before, after)  # 行 id 完全不变

    # ---- 认领 / 补件 / 重核 / 解决 ----
    def test_full_resolution_flow(self):
        core.submit_document(self.conn, self.sid, "supplier", "A", "1", "C", actor="t")
        core.submit_document(self.conn, self.sid, "forwarder", "B", "2", "C", actor="t")
        d = core.list_discrepancies(self.conn, self.sid)[0]
        # 未核对一致不能解决
        with self.assertRaises(ValueError):
            core.resolve_discrepancy(self.conn, d["id"], actor="李")
        core.claim_discrepancy(self.conn, d["id"], owner="李")
        core.add_supplement(self.conn, d["id"], note="已索要更正件", actor="李")
        # 补件改齐 → 自动重核（在 submit 内触发）
        core.submit_document(self.conn, self.sid, "forwarder", "A", "1", "C", actor="t")
        discs = {x["field"]: x for x in core.list_discrepancies(self.conn, self.sid)}
        self.assertTrue(all(x["is_reconciled"] for x in discs.values()))
        for x in discs.values():
            core.resolve_discrepancy(self.conn, x["id"], actor="李")
        self.assertEqual(core.get_shipment(self.conn, self.sid)["status"], "resolved")

    def test_reopen_after_resolved(self):
        core.submit_document(self.conn, self.sid, "supplier", "A", "1", "C", actor="t")
        core.submit_document(self.conn, self.sid, "forwarder", "A", "1", "C", actor="t")
        self.assertEqual(core.list_discrepancies(self.conn, self.sid), [])
        # 制造冲突 → 解决 → 新版本再次冲突，必须复用原行且重新打开
        core.submit_document(self.conn, self.sid, "warehouse", "B", "1", "C", actor="t")
        d = core.list_discrepancies(self.conn, self.sid)[0]
        did = d["id"]
        core.claim_discrepancy(self.conn, did, owner="李")
        core.submit_document(self.conn, self.sid, "warehouse", "A", "1", "C", actor="t")
        core.resolve_discrepancy(self.conn, did, actor="李")
        core.submit_document(self.conn, self.sid, "warehouse", "ZZ", "1", "C", actor="t")
        d2 = core.list_discrepancies(self.conn, self.sid)[0]
        self.assertEqual(d2["id"], did)
        self.assertEqual(d2["status"], "open")
        self.assertIsNone(d2["owner"])
        self.assertEqual(d2["is_reconciled"], 0)

    def test_manual_recompute(self):
        core.submit_document(self.conn, self.sid, "supplier", "A", "1", "C", actor="t")
        core.submit_document(self.conn, self.sid, "forwarder", "B", "1", "C", actor="t")
        # 直接改库模拟外部纠正，再手动重核
        self.conn.execute("UPDATE document SET product_name='A', version=2 WHERE source='forwarder'")
        self.conn.commit()
        stats = core.recompute(self.conn, self.sid, actor="t")
        self.assertEqual(stats["reconciled"], 1)

    def test_audit_trail_records_everything(self):
        core.submit_document(self.conn, self.sid, "supplier", "A", "1", "C", actor="t")
        core.submit_document(self.conn, self.sid, "forwarder", "B", "1", "C", actor="t")
        d = core.list_discrepancies(self.conn, self.sid)[0]
        core.claim_discrepancy(self.conn, d["id"], owner="李")
        core.add_supplement(self.conn, d["id"], note="说明", actor="李")
        actions = [l["action"] for l in core.list_logs(self.conn, self.sid)]
        for a in ("shipment_created", "doc_submitted", "discrepancy_opened",
                  "discrepancy_claimed", "discrepancy_supplemented"):
            self.assertIn(a, actions)


class RecordingNotifier(reminders.Notifier):
    """记录 (target, content) 的通知器，可按需抛错。"""

    def __init__(self, fail_times=0):
        self.sent_to = []
        self.remaining = fail_times

    def send(self, target, subject, content):
        if self.remaining > 0:
            self.remaining -= 1
            raise ConnectionError("网关超时（模拟）")
        self.sent_to.append((target, content))


class DeadlineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = core.connect(os.path.join(self.tmp, "d.db"))
        core.init_db(self.conn)
        self.t0 = datetime(2026, 9, 15, 9, 0)
        self.deadline = self.t0 + timedelta(hours=2)
        self.sid = core.create_shipment(
            self.conn, "DL-1", actor="t",
            deadline=self.deadline.isoformat(timespec="minutes"), warn_hours=1)
        core.submit_document(self.conn, self.sid, "supplier", "A", "1", "C", actor="t")
        core.submit_document(self.conn, self.sid, "forwarder", "B", "1", "C", actor="t")

    def tearDown(self):
        self.conn.close()

    def _disc_ids(self):
        return [d["id"] for d in core.list_discrepancies(self.conn, self.sid)]

    def test_no_reminder_before_window(self):
        stats = reminders.scan_once(self.conn, reminders.LogNotifier(), at=self.t0)
        self.assertEqual(stats["events_new"], 0)

    def test_due_soon_and_escalation_no_spam(self):
        # 临近：每条差异一条 due_soon 事件
        s1 = reminders.scan_once(self.conn, reminders.LogNotifier(),
                                 at=self.t0 + timedelta(hours=1))
        self.assertEqual((s1["events_new"], s1["notified"], s1["failed"]), (1, 1, 0))
        # 重复扫描：不产生新事件、不发消息
        s2 = reminders.scan_once(self.conn, reminders.LogNotifier(),
                                 at=self.t0 + timedelta(hours=1, minutes=10))
        self.assertEqual((s2["events_new"], s2["notified"]), (0, 0))
        self.assertEqual(s2["suppressed"], 1)
        # 超时：升级，每差异一条 overdue 事件
        s3 = reminders.scan_once(self.conn, reminders.LogNotifier(),
                                 at=self.deadline + timedelta(minutes=1))
        self.assertEqual(s3["events_new"], 1)
        self.assertEqual(s3["notified"], 1)  # 未认领 → 只通知主管
        # 再扫：拦截
        s4 = reminders.scan_once(self.conn, reminders.LogNotifier(),
                                 at=self.deadline + timedelta(minutes=5))
        self.assertEqual(s4["notified"], 0)

    def test_claimed_discrepancy_notifies_owner_and_lead_when_overdue(self):
        did = self._disc_ids()[0]
        core.claim_discrepancy(self.conn, did, owner="小李")
        s = reminders.scan_once(self.conn, reminders.LogNotifier(),
                                at=self.deadline + timedelta(minutes=1))
        targets = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT n.target FROM notification n JOIN reminder_event e ON n.event_id=e.id "
            "WHERE e.discrepancy_id=? AND e.level='overdue'", (did,))}
        self.assertEqual(targets, {"小李", "关务主管"})

    def test_resolved_discrepancy_silent(self):
        did = self._disc_ids()[0]
        core.claim_discrepancy(self.conn, did, owner="小李")
        core.submit_document(self.conn, self.sid, "forwarder", "A", "1", "C", actor="t")
        d = next(x for x in core.list_discrepancies(self.conn, self.sid) if x["id"] == did)
        core.resolve_discrepancy(self.conn, did, actor="小李")
        s = reminders.scan_once(self.conn, reminders.LogNotifier(),
                                at=self.deadline + timedelta(hours=1))
        self.assertEqual(s["events_new"], 0)

    def test_notifier_failure_isolated_and_retried(self):
        flaky = reminders.FlakyNotifier(fail_times=1)
        s1 = reminders.scan_once(self.conn, flaky, at=self.deadline + timedelta(minutes=1))
        self.assertGreaterEqual(s1["failed"], 1)
        # 失败期间核心流程照常用
        did = self._disc_ids()[0]
        core.claim_discrepancy(self.conn, did, owner="小李")
        core.add_supplement(self.conn, did, note="处理中", actor="小李")
        # 下次扫描自动重试失败通知
        s2 = reminders.scan_once(self.conn, reminders.LogNotifier(),
                                 at=self.deadline + timedelta(minutes=10))
        self.assertGreaterEqual(s2["retried_ok"], 1)
        self.assertEqual(s2["failed"], 0)

    def test_new_episode_after_reopen_reminds_once(self):
        did = self._disc_ids()[0]
        # 第一轮超时升级
        reminders.scan_once(self.conn, reminders.LogNotifier(),
                            at=self.deadline + timedelta(minutes=1))
        # 改齐并解决
        core.submit_document(self.conn, self.sid, "forwarder", "A", "1", "C", actor="t")
        core.resolve_discrepancy(self.conn, did, actor="t")
        # 再冲突 → 复用行，episode 2，允许再提醒一次
        core.submit_document(self.conn, self.sid, "forwarder", "Z", "1", "C", actor="t")
        row = self.conn.execute("SELECT episode,status FROM discrepancy WHERE id=?",
                                (did,)).fetchone()
        self.assertEqual(row["episode"], 2)
        self.assertEqual(row["status"], "open")
        s = reminders.scan_once(self.conn, reminders.LogNotifier(),
                                at=self.deadline + timedelta(hours=2))
        self.assertEqual(s["events_new"], 1)
        s2 = reminders.scan_once(self.conn, reminders.LogNotifier(),
                                 at=self.deadline + timedelta(hours=3))
        self.assertEqual(s2["events_new"], 0)

    # ---- 自定义超时升级联系人 ----
    def _notification_targets(self, did, level):
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT n.target FROM notification n "
            "JOIN reminder_event e ON n.event_id=e.id "
            "WHERE e.discrepancy_id=? AND e.level=?", (did, level))}

    def test_custom_escalation_target_overdue_claimed(self):
        did = self._disc_ids()[0]
        core.claim_discrepancy(self.conn, did, owner="小李")
        n = RecordingNotifier()
        s = reminders.scan_once(self.conn, n, escalation_target="值班主管周",
                                at=self.deadline + timedelta(minutes=1))
        self.assertEqual(s["notified"], 2)
        self.assertEqual(self._notification_targets(did, "overdue"),
                         {"小李", "值班主管周"})
        # 固定默认联系人不得出现
        self.assertTrue(all(t != reminders.DEFAULT_ESCALATION_TARGET for t, _ in n.sent_to))

    def test_custom_target_unclaimed_due_soon_fallback(self):
        did = self._disc_ids()[0]  # 未认领
        n = RecordingNotifier()
        reminders.scan_once(self.conn, n, escalation_target="值班主管周",
                            at=self.t0 + timedelta(hours=1, minutes=30))
        targets = {t for t, _ in n.sent_to}
        self.assertEqual(targets, {"值班主管周"})  # 临近+未认领 → 兜底给自定义主管

    def test_default_escalation_target_unchanged(self):
        did = self._disc_ids()[0]
        n = RecordingNotifier()
        reminders.scan_once(self.conn, n,
                            at=self.deadline + timedelta(minutes=1))
        self.assertEqual({t for t, _ in n.sent_to}, {"关务主管"})
        self.assertEqual(self._notification_targets(did, "overdue"), {"关务主管"})

    def test_custom_target_dedup_on_repeated_scans(self):
        did = self._disc_ids()[0]
        n = RecordingNotifier()
        at = self.deadline + timedelta(minutes=1)
        s1 = reminders.scan_once(self.conn, n, escalation_target="值班主管周", at=at)
        s2 = reminders.scan_once(self.conn, n, escalation_target="值班主管周",
                                 at=at + timedelta(minutes=5))
        s3 = reminders.scan_once(self.conn, n, escalation_target="值班主管周",
                                 at=at + timedelta(minutes=10))
        self.assertEqual(s1["events_new"], 1)
        self.assertEqual((s2["events_new"], s2["notified"], s2["suppressed"]), (0, 0, 1))
        self.assertEqual((s3["events_new"], s3["notified"]), (0, 0))
        # 无论扫几遍，值班主管只收到一条
        self.assertEqual([t for t, _ in n.sent_to], ["值班主管周"])

    def test_custom_target_failed_then_retried(self):
        did = self._disc_ids()[0]
        at = self.deadline + timedelta(minutes=1)
        flaky = RecordingNotifier(fail_times=1)
        s1 = reminders.scan_once(self.conn, flaky, escalation_target="值班主管周", at=at)
        self.assertEqual(s1["failed"], 1)
        self.assertEqual(flaky.sent_to, [])
        # 下一轮用新通知器重试：目标必须仍是创建时固化的自定义联系人
        n2 = RecordingNotifier()
        s2 = reminders.scan_once(self.conn, n2, escalation_target="值班主管周",
                                 at=at + timedelta(minutes=5))
        self.assertEqual(s2["retried_ok"], 1)
        self.assertEqual([t for t, _ in n2.sent_to], ["值班主管周"])
        # 新差异（数量开始冲突）产生新事件，按本轮传入的新主管投递；旧事件不重复
        n3 = RecordingNotifier()
        core.submit_document(self.conn, self.sid, "forwarder", "B", "2", "C", actor="t")
        s3 = reminders.scan_once(self.conn, n3, escalation_target="代班主管吴",
                                 at=at + timedelta(minutes=10))
        self.assertEqual(s3["events_new"], 1)
        targets3 = {t for t, _ in n3.sent_to}
        self.assertEqual(targets3, {"代班主管吴"})  # 本轮只有新事件待发
        # 旧事件的通知记录目标在创建时固化，换主管也不被改写
        old_targets = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT n.target FROM notification n "
            "JOIN reminder_event e ON n.event_id=e.id "
            "WHERE e.discrepancy_id=? AND e.level='overdue' AND e.episode=1", (did,))}
        self.assertEqual(old_targets, {"值班主管周"})


class PortalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = core.connect(os.path.join(self.tmp, "p.db"))
        core.init_db(self.conn)
        self.sid = core.create_shipment(self.conn, "P-1", actor="t")
        core.submit_document(self.conn, self.sid, "supplier", "保温杯", "1000", "C1", actor="t")
        core.submit_document(self.conn, self.sid, "forwarder", "真空保温杯", "1020", "C1", actor="t")
        core.submit_document(self.conn, self.sid, "warehouse", "保温杯", "1000", "C9", actor="t")

    def tearDown(self):
        self.conn.close()

    def test_portal_context_hides_other_sources(self):
        token = core.create_supplement_token(self.conn, self.sid, ["quantity"],
                                             contact="小王", actor="小张")
        ctx = core.portal_context(self.conn, token)
        blob = repr(ctx)
        self.assertNotIn("真空保温杯", blob)   # 货代品名不可见
        self.assertNotIn("C9", blob)          # 仓库箱单号不可见
        self.assertNotIn("1020", blob)        # 货代数量不可见
        self.assertEqual(ctx["allowed_fields"], ["quantity"])
        # 只暴露数量差异（品名/箱单虽冲突但不在授权字段内）
        self.assertEqual([d["field"] for d in ctx["open_discrepancies"]], ["quantity"])

    def test_forbidden_field_rejected_and_logged(self):
        token = core.create_supplement_token(self.conn, self.sid, ["quantity"], actor="小张")
        with self.assertRaises(core.TokenError):
            core.supplier_submit(self.conn, token, {"carton_no": "C99"}, actor="供应商")
        # 供应商资料未被动到
        self.assertEqual(core.get_document(self.conn, self.sid, "supplier")["carton_no"], "C1")
        actions = [l["action"] for l in core.list_logs(self.conn, self.sid)]
        self.assertIn("portal_denied", actions)

    def test_submit_attaches_and_recomputes_and_dedupes(self):
        token = core.create_supplement_token(
            self.conn, self.sid, ["product_name", "quantity"], actor="小张")
        r1 = core.supplier_submit(self.conn, token, {"quantity": "1020"}, actor="小王")
        self.assertFalse(r1["duplicate"])
        self.assertEqual(r1["version"], 2)
        # 只更新授权字段，其他字段保留
        doc = core.get_document(self.conn, self.sid, "supplier")
        self.assertEqual((doc["product_name"], doc["quantity"], doc["carton_no"]),
                         ("保温杯", "1020", "C1"))
        # 数量差异（货代也是 1020，仓库仍是 1000）——三方未齐，不会误判为一致
        qty = next(d for d in core.list_discrepancies(self.conn, self.sid)
                   if d["field"] == "quantity")
        self.assertEqual(qty["is_reconciled"], 0)

        # 仓库改齐后自动核对一致
        core.submit_document(self.conn, self.sid, "warehouse", "保温杯", "1020", "C9", actor="t")
        qty = next(d for d in core.list_discrepancies(self.conn, self.sid)
                   if d["field"] == "quantity")
        self.assertEqual(qty["is_reconciled"], 1)

        # 重复提交：不升版
        r2 = core.supplier_submit(self.conn, token, {"quantity": "1020"}, actor="小王")
        self.assertTrue(r2["duplicate"])
        self.assertEqual(core.get_document(self.conn, self.sid, "supplier")["version"], 2)
        tok = self.conn.execute("SELECT used_count FROM supplement_token WHERE token=?",
                                (token,)).fetchone()
        self.assertEqual(tok["used_count"], 1)

    def test_revoked_and_expired_token(self):
        token = core.create_supplement_token(self.conn, self.sid, ["quantity"], actor="t")
        row = self.conn.execute("SELECT id FROM supplement_token WHERE token=?",
                                (token,)).fetchone()
        core.revoke_token(self.conn, row["id"], actor="t")
        with self.assertRaises(core.TokenError):
            core.supplier_submit(self.conn, token, {"quantity": "1"}, actor="t")

        token2 = core.create_supplement_token(
            self.conn, self.sid, ["quantity"], actor="t", expires_in_hours=-1)
        with self.assertRaises(core.TokenError):
            core.portal_context(self.conn, token2)

    def test_blank_fields_not_cleared(self):
        token = core.create_supplement_token(self.conn, self.sid, ["quantity"], actor="t")
        with self.assertRaises(core.TokenError):
            core.supplier_submit(self.conn, token, {"quantity": "  "}, actor="小王")
        self.assertEqual(core.get_document(self.conn, self.sid, "supplier")["quantity"], "1000")


class PackageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = core.connect(os.path.join(self.tmp, "pkg.db"))
        core.init_db(self.conn)
        self.sid = core.create_shipment(self.conn, "PK-1", customer="甲", actor="t",
                                        deadline="2026-09-20T10:00", warn_hours=24)

    def tearDown(self):
        self.conn.close()

    def _submit_all(self, pname=("保温杯",) * 3, qty=("1000",) * 3, cno=("C1",) * 3):
        core.submit_document(self.conn, self.sid, "supplier", pname[0], qty[0], cno[0], actor="王")
        core.submit_document(self.conn, self.sid, "forwarder", pname[1], qty[1], cno[1], actor="陈")
        core.submit_document(self.conn, self.sid, "warehouse", pname[2], qty[2], cno[2], actor="赵")

    def test_freeze_blocked_with_open_discrepancy(self):
        self._submit_all(qty=("1000", "1020", "1000"))
        with self.assertRaises(ValueError):
            core.freeze_package(self.conn, self.sid, actor="李")
        # 显式确认可带差异冻结
        r = core.freeze_package(self.conn, self.sid, actor="李", confirm=True)
        self.assertEqual(r["open_items"], 1)
        pkgs = core.list_packages(self.conn, self.sid)
        self.assertEqual(len(pkgs), 1)
        self.assertEqual(pkgs[0]["open_items"], 1)

    def test_clean_freeze_snapshot_content_and_sources(self):
        self._submit_all()
        r = core.freeze_package(self.conn, self.sid, actor="李")
        self.assertEqual(r["open_items"], 0)
        pid = core.list_packages(self.conn, self.sid)[0]["id"]
        pkg = core.get_package(self.conn, pid)
        for f in core.FIELDS:
            e = pkg["declared_fields"][f]
            self.assertTrue(e["all_agree"])
            self.assertEqual(len(e["basis_sources"]), 3)
        self.assertEqual(pkg["declared_fields"]["quantity"]["adopted_value"], "1000")
        # 来源、版本、提交人、时间都在快照里
        fwd = pkg["documents"]["forwarder"]
        self.assertEqual((fwd["submitted_by"], fwd["version"]), ("陈", 1))
        self.assertTrue(fwd["submitted_at"])

    def test_frozen_package_immutable_after_new_versions(self):
        self._submit_all()
        core.freeze_package(self.conn, self.sid, actor="李")
        pid = core.list_packages(self.conn, self.sid)[0]["id"]
        before = core.get_package(self.conn, pid)

        # 冻结后：产生差异、改齐、三方一致换值，多次升版
        self._submit_all(qty=("999",) * 3)              # 全部更新到 999
        self._submit_all(qty=("1000",) * 3)             # 再改回 1000（重复判定在全表上，这里确有变化）
        self._submit_all(cno=("C2",) * 3)               # 箱单号整体改成 C2

        after = core.get_package(self.conn, pid)
        self.assertEqual(after, before)                # 旧包字节级不变
        self.assertEqual(after["declared_fields"]["carton_no"]["adopted_value"], "C1")

    def test_multiple_packages_independent(self):
        self._submit_all(cno=("C1",) * 3)
        core.freeze_package(self.conn, self.sid, actor="李")
        self._submit_all(cno=("C2",) * 3)
        core.freeze_package(self.conn, self.sid, actor="李")
        pkgs = core.list_packages(self.conn, self.sid)
        self.assertEqual([p["package_no"] for p in pkgs], [1, 2])
        p1 = core.get_package(self.conn, pkgs[0]["id"])
        p2 = core.get_package(self.conn, pkgs[1]["id"])
        self.assertEqual(p1["declared_fields"]["carton_no"]["adopted_value"], "C1")
        self.assertEqual(p2["declared_fields"]["carton_no"]["adopted_value"], "C2")
        self.assertEqual(p1["frozen_at"], p1["frozen_at"])

    def test_snapshot_records_resolution_conclusion(self):
        self._submit_all(qty=("1000", "1020", "1000"))
        did = next(d["id"] for d in core.list_discrepancies(self.conn, self.sid))
        core.claim_discrepancy(self.conn, did, owner="李")
        core.add_supplement(self.conn, did, note="已核实为 1020", actor="李")
        self._submit_all(qty=("1020", "1020", "1020"))
        core.resolve_discrepancy(self.conn, did, actor="李")
        core.freeze_package(self.conn, self.sid, actor="李")
        pkg = core.get_package(self.conn, core.list_packages(self.conn, self.sid)[0]["id"])
        qty = next(d for d in pkg["discrepancies"] if d["field"] == "quantity")
        self.assertEqual(qty["status"], "resolved")
        self.assertEqual(qty["owner"], "李")
        self.assertIn("1020", qty["supplement_note"])
        actions = {t["action"] for t in qty["timeline"]}
        self.assertIn("discrepancy_claimed", actions)
        self.assertIn("discrepancy_resolved", actions)

    def test_markdown_export_contains_provenance(self):
        self._submit_all()
        core.freeze_package(self.conn, self.sid, actor="李")
        pkg = core.get_package(self.conn, core.list_packages(self.conn, self.sid)[0]["id"])
        md = core.export_markdown(pkg)
        self.assertIn("申报核对单 — PK-1", md)
        self.assertIn("v1", md)
        self.assertIn("陈", md)
        self.assertIn("不可变快照", md)

    def test_freeze_logged(self):
        self._submit_all()
        core.freeze_package(self.conn, self.sid, actor="李")
        actions = [l["action"] for l in core.list_logs(self.conn, self.sid)]
        self.assertIn("package_frozen", actions)


class PackageCountersignTest(unittest.TestCase):
    """冻结→待复核→两人会签（不同人）→指定操作人放行，及全部异常路径。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "wf2.db")
        self.conn = core.connect(self.db_path)
        core.init_db(self.conn)
        self.sid = core.create_shipment(self.conn, "CS-1", customer="甲", actor="t",
                                        deadline="2026-09-20T10:00", warn_hours=24)
        for src, qty in (("supplier", "1000"), ("forwarder", "1000"), ("warehouse", "1000")):
            core.submit_document(self.conn, self.sid, src, "保温杯", qty, "C1", actor="t")

    def tearDown(self):
        self.conn.close()

    def _freeze(self, **kw):
        r = core.freeze_package(self.conn, self.sid, actor="制单员", **kw)
        row = self.conn.execute("SELECT * FROM declaration_package WHERE id=?",
                                (r["id"],)).fetchone()
        return r, row

    def _approve_both(self, pid, customs="关务复核人赵", supervisor="关务主管钱",
                      comments=("", "同意")):
        core.review_package(self.conn, pid, core.REVIEW_CUSTOMS, customs,
                            core.REVIEW_APPROVE, comments[0])
        return core.review_package(self.conn, pid, core.REVIEW_SUPERVISOR, supervisor,
                                   core.REVIEW_APPROVE, comments[1])

    def test_freeze_enters_pending_and_blocks_release_before_countersign(self):
        r, row = self._freeze()
        self.assertEqual(r["status"], core.PACKAGE_PENDING)
        self.assertEqual(row["status"], core.PACKAGE_PENDING)
        # 会签未完成：放行被拒
        with self.assertRaises(core.PackageStateError):
            core.release_package(self.conn, row["id"], actor="操作人孙")
        # 单方会签后仍不能放行
        core.review_package(self.conn, row["id"], core.REVIEW_CUSTOMS, "赵",
                            core.REVIEW_APPROVE)
        with self.assertRaises(core.PackageStateError):
            core.release_package(self.conn, row["id"], actor="操作人孙")
        self.assertEqual(self._status(row["id"]), core.PACKAGE_PENDING)

    def test_reviewer_and_supervisor_must_differ(self):
        _, row = self._freeze()
        core.review_package(self.conn, row["id"], core.REVIEW_CUSTOMS, "同一个人",
                            core.REVIEW_APPROVE)
        # 同一人想兼任主管 → 拒绝，包仍停在待复核
        with self.assertRaises(core.PackageStateError):
            core.review_package(self.conn, row["id"], core.REVIEW_SUPERVISOR, "同一个人",
                                core.REVIEW_APPROVE)
        self.assertEqual(self._status(row["id"]), core.PACKAGE_PENDING)
        self.assertEqual(len(core.list_package_reviews(self.conn, row["id"])), 1)
        actions = [l["action"] for l in core.list_logs(self.conn, self.sid)]
        self.assertIn("package_review_denied", actions)
        # 换个不同的主管即可通过
        r = core.review_package(self.conn, row["id"], core.REVIEW_SUPERVISOR, "主管钱",
                                core.REVIEW_APPROVE)
        self.assertEqual(r["status"], core.PACKAGE_APPROVED)

    def test_reverse_role_order_same_rule(self):
        _, row = self._freeze()
        core.review_package(self.conn, row["id"], core.REVIEW_SUPERVISOR, "主管钱",
                            core.REVIEW_APPROVE)
        with self.assertRaises(core.PackageStateError):
            core.review_package(self.conn, row["id"], core.REVIEW_CUSTOMS, "主管钱",
                                core.REVIEW_APPROVE)

    def test_reject_requires_reason_and_blocks_release(self):
        _, row = self._freeze()
        # 驳回不填原因 → 拒绝，状态不变
        with self.assertRaises(ValueError):
            core.review_package(self.conn, row["id"], core.REVIEW_CUSTOMS, "赵",
                                core.REVIEW_REJECT)
        self.assertEqual(self._status(row["id"]), core.PACKAGE_PENDING)
        # 主管先通过，复核人驳回 → 终态 rejected
        core.review_package(self.conn, row["id"], core.REVIEW_SUPERVISOR, "钱",
                            core.REVIEW_APPROVE, "同意")
        r = core.review_package(self.conn, row["id"], core.REVIEW_CUSTOMS, "赵",
                                core.REVIEW_REJECT, "三方箱单号未复核，需补仓库签章件")
        self.assertTrue(r["rejected"])
        self.assertEqual(self._status(row["id"]), core.PACKAGE_REJECTED)
        # 驳回后：不能放行、不能再会签
        with self.assertRaises(core.PackageStateError):
            core.release_package(self.conn, row["id"], actor="孙")
        with self.assertRaises(core.PackageStateError):
            core.review_package(self.conn, row["id"], core.REVIEW_CUSTOMS, "赵",
                                core.REVIEW_APPROVE)

    def test_rejected_package_is_terminal_new_package_starts_afresh(self):
        _, row1 = self._freeze()
        pid1 = row1["id"]
        core.review_package(self.conn, pid1, core.REVIEW_CUSTOMS, "赵",
                            core.REVIEW_REJECT, "数量依据不足")
        # 业务重新处理后冻结新包：新包待复核、包号递增，旧审批不会挪过来
        r2 = core.freeze_package(self.conn, self.sid, actor="制单员")
        self.assertEqual(r2["package_no"], 2)
        self.assertFalse(r2["deduped"])
        self.assertEqual(r2["status"], core.PACKAGE_PENDING)
        self.assertEqual(core.list_package_reviews(self.conn, r2["id"]), [])
        # 旧包仍旧是驳回终态，旧包内容未被修改
        self.assertEqual(self._status(pid1), core.PACKAGE_REJECTED)
        old_payload = json.loads(self.conn.execute(
            "SELECT payload_json FROM declaration_package WHERE id=?", (pid1,)).fetchone()[0])
        self.assertEqual(old_payload["package_no"], 1)

    def test_repeated_click_same_role_is_idempotent_then_conflict(self):
        _, row = self._freeze()
        pid = row["id"]
        core.review_package(self.conn, pid, core.REVIEW_CUSTOMS, "赵",
                            core.REVIEW_APPROVE, "已核对")
        # 完全相同的重复点击：幂等
        r = core.review_package(self.conn, pid, core.REVIEW_CUSTOMS, "赵",
                                core.REVIEW_APPROVE, "已核对")
        self.assertTrue(r.get("duplicate"))
        self.assertEqual(len(core.list_package_reviews(self.conn, pid)), 1)
        # 同人同角色但换了结论/意见：拒绝
        with self.assertRaises(core.PackageStateError):
            core.review_package(self.conn, pid, core.REVIEW_CUSTOMS, "赵",
                                core.REVIEW_REJECT, "改主意了")
        # 另一个人想顶掉该角色：也拒绝
        with self.assertRaises(core.PackageStateError):
            core.review_package(self.conn, pid, core.REVIEW_CUSTOMS, "王",
                                core.REVIEW_APPROVE)

    def test_designated_operator_enforced_and_release_idempotent(self):
        _, row = self._freeze(designated_operator="放行员孙")
        pid = row["id"]
        self._approve_both(pid)
        # 非指定操作人不能放行
        with self.assertRaises(core.PackageStateError):
            core.release_package(self.conn, pid, actor="别人李")
        self.assertEqual(self._status(pid), core.PACKAGE_APPROVED)
        denied = [l["action"] for l in core.list_logs(self.conn, self.sid)]
        self.assertIn("package_release_denied", denied)
        # 指定操作人本人放行
        r = core.release_package(self.conn, pid, actor="放行员孙")
        self.assertEqual(r["status"], core.PACKAGE_RELEASED)
        # 重复点击放行：本人幂等
        r2 = core.release_package(self.conn, pid, actor="放行员孙")
        self.assertTrue(r2["duplicate"])
        # 别人再点：报错而非二次放行
        with self.assertRaises(core.PackageStateError):
            core.release_package(self.conn, pid, actor="别人李")

    def test_release_requires_actor_name(self):
        _, row = self._freeze()
        self._approve_both(row["id"])
        with self.assertRaises(ValueError):
            core.release_package(self.conn, row["id"], actor="  ")

    def test_released_package_is_terminal(self):
        _, row = self._freeze()
        pid = row["id"]
        self._approve_both(pid)
        core.release_package(self.conn, pid, actor="孙")
        # 放行后不能再会签
        with self.assertRaises(core.PackageStateError):
            core.review_package(self.conn, pid, core.REVIEW_CUSTOMS, "赵",
                                core.REVIEW_APPROVE)

    def test_payload_json_never_mutated_by_workflow(self):
        r = core.freeze_package(self.conn, self.sid, actor="制单员",
                                designated_operator="孙")
        pid = r["id"]
        raw_before = self.conn.execute(
            "SELECT payload_json FROM declaration_package WHERE id=?", (pid,)).fetchone()[0]
        self._approve_both(pid)
        core.release_package(self.conn, pid, actor="孙")
        raw_after = self.conn.execute(
            "SELECT payload_json FROM declaration_package WHERE id=?", (pid,)).fetchone()[0]
        self.assertEqual(raw_after, raw_before)  # 会签/放行只写活表，不回改快照
        pkg = core.get_package(self.conn, pid)
        self.assertEqual(pkg["workflow"]["status"], core.PACKAGE_RELEASED)
        self.assertFalse(pkg["workflow"]["is_internal"])
        self.assertEqual(pkg["_document_class"], "released_declaration")
        self.assertEqual(len(pkg["workflow"]["reviews"]), 2)

    def test_freeze_idempotency_key_dedupes_double_click(self):
        key = "form-instance-abc"
        r1 = core.freeze_package(self.conn, self.sid, actor="制单员", idempotency_key=key)
        r2 = core.freeze_package(self.conn, self.sid, actor="制单员", idempotency_key=key)
        self.assertTrue(r2["deduped"])
        self.assertEqual(r2["id"], r1["id"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM declaration_package WHERE shipment_id=?",
            (self.sid,)).fetchone()[0], 1)
        # 无防重键的两次冻结 → 两个包
        r3 = core.freeze_package(self.conn, self.sid, actor="制单员")
        self.assertFalse(r3["deduped"])
        self.assertNotEqual(r3["id"], r1["id"])

    def test_freeze_with_open_discrepancy_still_requires_confirm(self):
        core.submit_document(self.conn, self.sid, "forwarder", "保温杯", "1099", "C1",
                             actor="t")
        with self.assertRaises(ValueError):
            core.freeze_package(self.conn, self.sid, actor="制单员")
        r = core.freeze_package(self.conn, self.sid, actor="制单员", confirm=True)
        pkg = core.get_package(self.conn, r["id"])
        self.assertEqual(pkg["open_items"], 1)
        self.assertEqual(pkg["workflow"]["status"], core.PACKAGE_PENDING)

    def test_full_workflow_audit_trail(self):
        _, row = self._freeze(designated_operator="孙")
        pid = row["id"]
        self._approve_both(pid)
        core.release_package(self.conn, pid, actor="孙")
        actions = [l["action"] for l in core.list_logs(self.conn, self.sid)]
        for a in ("package_frozen", "package_review_approved", "package_countersigned",
                  "package_released"):
            self.assertIn(a, actions)
        # 每条会签都留下操作人和时间
        reviews = core.list_package_reviews(self.conn, pid)
        self.assertEqual({r["role"] for r in reviews},
                         {core.REVIEW_CUSTOMS, core.REVIEW_SUPERVISOR})
        self.assertTrue(all(r["actor"] and r["created_at"] for r in reviews))
        self.assertEqual({r["actor"] for r in reviews}, {"关务复核人赵", "关务主管钱"})

    def test_concurrent_same_role_review_only_one_wins(self):
        r = core.freeze_package(self.conn, self.sid, actor="制单员")
        pid = r["id"]
        errors = []

        def submit(actor_name):
            conn = core.connect(self.db_path)
            try:
                core.review_package(conn, pid, core.REVIEW_CUSTOMS, actor_name,
                                    core.REVIEW_APPROVE)
            except Exception as e:  # noqa: BLE001
                errors.append((actor_name, type(e).__name__))
            finally:
                conn.close()

        threads = [threading.Thread(target=submit, args=(f"复核人{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 恰好一条意见落库；另一个请求拿到明确错误，没有静默覆盖
        rows = core.list_package_reviews(self.conn, pid)
        same_role = [x for x in rows if x["role"] == core.REVIEW_CUSTOMS]
        self.assertEqual(len(same_role), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][1], "PackageStateError")

    def test_concurrent_release_only_first_succeeds(self):
        r = core.freeze_package(self.conn, self.sid, actor="制单员")
        pid = r["id"]
        self._approve_both(pid)
        outcomes = []

        def release():
            conn = core.connect(self.db_path)
            try:
                outcomes.append(core.release_package(conn, pid, actor="孙"))
            except Exception as e:  # noqa: BLE001
                outcomes.append(e)
            finally:
                conn.close()

        threads = [threading.Thread(target=release) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sum(1 for o in outcomes if isinstance(o, dict)), 2)
        self.assertEqual(sum(1 for o in outcomes if isinstance(o, dict) and o.get("duplicate")),
                         1)
        self.assertEqual(self.conn.execute(
            "SELECT released_by FROM declaration_package WHERE id=?", (pid,)).fetchone()[0],
            "孙")

    def test_export_marks_internal_vs_released(self):
        r = core.freeze_package(self.conn, self.sid, actor="制单员",
                                designated_operator="孙")
        pid = r["id"]
        # 待复核：md/json 都带内部水印
        pkg = core.get_package(self.conn, pid)
        md = core.export_markdown(pkg)
        self.assertIn(core.INTERNAL_REVIEW_WATERMARK, md)
        self.assertEqual(pkg["_document_class"], "internal_review_only")
        # 驳回包：明确作废，禁止申报
        core.review_package(self.conn, pid, core.REVIEW_CUSTOMS, "赵",
                            core.REVIEW_REJECT, "依据不足")
        md = core.export_markdown(core.get_package(self.conn, pid))
        self.assertIn("已驳回", md)
        self.assertIn("禁止申报", md)
        # 新包走完会签+放行：正式版无水印、有放行人与时间
        r2 = core.freeze_package(self.conn, self.sid, actor="制单员")
        self._approve_both(r2["id"])
        core.release_package(self.conn, r2["id"], actor="孙")
        pkg2 = core.get_package(self.conn, r2["id"])
        md2 = core.export_markdown(pkg2)
        self.assertIn("正式申报包（已放行）", md2)
        self.assertNotIn(core.INTERNAL_REVIEW_WATERMARK, md2)
        self.assertIn("孙", md2)
        self.assertIn("关务复核人赵", md2)  # 会签意见与时间随正式件输出

    def _status(self, pid):
        return self.conn.execute("SELECT status FROM declaration_package WHERE id=?",
                                 (pid,)).fetchone()[0]


class LegacyPackageMigrationTest(unittest.TestCase):
    """旧数据库（无 status 列、无 package_review 表）迁移：旧包不失效、归档为已放行并审计。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "legacy.db")
        conn = core.connect(self.db_path)
        # 手工建一套“会签功能上线前”的最小旧结构并塞一个旧冻结包
        conn.executescript("""
        CREATE TABLE shipment (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT UNIQUE NOT NULL,
            customer TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open', deadline TEXT,
            warn_hours INTEGER NOT NULL DEFAULT 24, created_at TEXT NOT NULL);
        CREATE TABLE document (
            id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id INTEGER NOT NULL,
            source TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
            product_name TEXT, quantity TEXT, carton_no TEXT,
            submitted_by TEXT NOT NULL DEFAULT '', submitted_at TEXT NOT NULL,
            UNIQUE(shipment_id, source));
        CREATE TABLE discrepancy (
            id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id INTEGER NOT NULL,
            field TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
            values_json TEXT NOT NULL DEFAULT '{}', owner TEXT,
            supplement_note TEXT NOT NULL DEFAULT '', is_reconciled INTEGER NOT NULL DEFAULT 0,
            episode INTEGER NOT NULL DEFAULT 1, rule_version INTEGER,
            rule_evidence_json TEXT NOT NULL DEFAULT '{}', close_reason TEXT,
            active_waiver_id INTEGER, first_found_at TEXT NOT NULL,
            last_changed_at TEXT NOT NULL, reconciled_at TEXT, resolved_at TEXT,
            UNIQUE(shipment_id, field));
        CREATE TABLE supplement_token (
            id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL,
            shipment_id INTEGER NOT NULL, source TEXT NOT NULL, fields TEXT NOT NULL,
            contact TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, expires_at TEXT, used_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT, revoked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE reminder_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id INTEGER NOT NULL,
            discrepancy_id INTEGER, level TEXT NOT NULL, episode INTEGER NOT NULL,
            created_at TEXT NOT NULL, UNIQUE(discrepancy_id, level, episode));
        CREATE TABLE notification (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL,
            channel TEXT NOT NULL, target TEXT NOT NULL, content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, sent_at TEXT);
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id INTEGER NOT NULL,
            actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL,
            created_at TEXT NOT NULL);
        CREATE TABLE declaration_package (
            id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id INTEGER NOT NULL,
            package_no INTEGER NOT NULL, frozen_by TEXT NOT NULL DEFAULT '',
            frozen_at TEXT NOT NULL, declared_fields TEXT NOT NULL DEFAULT '',
            open_items INTEGER NOT NULL DEFAULT 0, payload_json TEXT NOT NULL,
            UNIQUE(shipment_id, package_no));
        CREATE TABLE tolerance_rule (
            id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id INTEGER NOT NULL,
            field TEXT NOT NULL, version INTEGER NOT NULL, mode TEXT NOT NULL,
            config_json TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active', created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, revoked_by TEXT NOT NULL DEFAULT '', revoked_at TEXT,
            UNIQUE(shipment_id, field, version));
        CREATE TABLE waiver (
            id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id INTEGER NOT NULL,
            discrepancy_id INTEGER NOT NULL, episode INTEGER NOT NULL,
            reason TEXT NOT NULL, granted_by TEXT NOT NULL, granted_at TEXT NOT NULL,
            expires_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            revoked_by TEXT NOT NULL DEFAULT '', ended_at TEXT,
            basis_json TEXT NOT NULL DEFAULT '{}');
        """)
        conn.execute(
            "INSERT INTO shipment (ref, status, created_at) VALUES ('OLD-1', 'resolved', ?)",
            (core.now(),))
        for src in core.SOURCES:
            conn.execute(
                "INSERT INTO document (shipment_id, source, version, product_name, quantity, "
                "carton_no, submitted_by, submitted_at) VALUES (1, ?, 1, '保温杯', '1000', "
                "'C1', '老', ?)",
                (src, core.now()))
        # 用与上线版本相同的快照构造器生成旧包内容（旧库尚无 status 列）
        legacy_snapshot = core.build_snapshot(conn, 1)
        legacy_snapshot["package_no"] = 1
        legacy_snapshot["frozen_by"] = "老关务"
        legacy_snapshot["frozen_at"] = core.now()
        conn.execute(
            "INSERT INTO declaration_package (shipment_id, package_no, frozen_by, frozen_at, "
            "declared_fields, open_items, payload_json) VALUES (1, 1, '老关务', ?, 'x', 0, ?)",
            (core.now(), json.dumps(legacy_snapshot, ensure_ascii=False)))
        conn.commit()
        conn.close()

    def test_legacy_packages_backfilled_and_exportable(self):
        conn = core.connect(self.db_path)
        core.init_db(conn)  # 触发迁移
        row = conn.execute("SELECT status, released_by FROM declaration_package WHERE id=1"
                           ).fetchone()
        self.assertEqual(row["status"], core.PACKAGE_RELEASED)
        self.assertEqual(row["released_by"], "系统迁移")
        # 旧包仍可正常导出（现有冻结包/导出入口不失效）
        pkg = core.get_package(conn, 1)
        self.assertFalse(pkg["workflow"]["is_internal"])
        md = core.export_markdown(pkg)
        self.assertIn("正式申报包（已放行）", md)
        actions = [l["action"] for l in core.list_logs(conn, 1)]
        self.assertIn("package_legacy_released", actions)
        # 迁移幂等：再 init 一次不重复审计、不动状态
        core.init_db(conn)
        n = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='package_legacy_released'").fetchone()[0]
        self.assertEqual(n, 1)
        conn.close()

    def test_freeze_after_migration_starts_pending(self):
        conn = core.connect(self.db_path)
        core.init_db(conn)
        # setUp 已用旧结构写入三方一致资料，迁移后冻新包应正常走待复核流程
        r = core.freeze_package(conn, 1, actor="新关务")
        self.assertEqual(r["package_no"], 2)
        self.assertEqual(r["status"], core.PACKAGE_PENDING)
        rows = conn.execute("SELECT package_no, status FROM declaration_package ORDER BY package_no"
                            ).fetchall()
        self.assertEqual([(r0["package_no"], r0["status"]) for r0 in rows],
                         [(1, core.PACKAGE_RELEASED), (2, core.PACKAGE_PENDING)])
        conn.close()


class ToleranceRuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = core.connect(os.path.join(self.tmp, "tol.db"))
        core.init_db(self.conn)
        self.sid = core.create_shipment(self.conn, "TOL-1", actor="t")

    def tearDown(self):
        self.conn.close()

    def _qty_conflict(self, a="1000", b="1020", c="1000"):
        core.submit_document(self.conn, self.sid, "supplier", "保温杯", a, "C1", actor="t")
        core.submit_document(self.conn, self.sid, "forwarder", "保温杯", b, "C1", actor="t")
        core.submit_document(self.conn, self.sid, "warehouse", "保温杯", c, "C1", actor="t")

    def _disc(self, field="quantity"):
        return next(d for d in core.list_discrepancies(self.conn, self.sid)
                    if d["field"] == field)

    # ---- 判定 ----
    def test_rule_before_conflict_within_abs_no_discrepancy(self):
        core.save_rule(self.conn, self.sid, "quantity", "abs", {"abs": "50"}, actor="关务")
        self._qty_conflict()
        self.assertEqual(core.list_discrepancies(self.conn, self.sid), [])
        actions = [l["action"] for l in core.list_logs(self.conn, self.sid)]
        self.assertIn("discrepancy_tolerated", actions)

    def test_rule_after_conflict_within_abs_marks_tolerated_with_evidence(self):
        self._qty_conflict()
        d = self._disc()
        self.assertEqual(d["rule_version"], None)
        core.save_rule(self.conn, self.sid, "quantity", "abs", {"abs": "50"}, actor="关务")
        d = self._disc()
        self.assertEqual(d["is_reconciled"], 1)
        self.assertEqual(d["close_reason"], core.CLOSE_TOLERATED)
        self.assertEqual(d["rule_version"], 1)
        ev = __import__("json").loads(d["rule_evidence_json"])
        self.assertEqual((ev["spread"], ev["limit"]), ("20", "50"))
        # 容差内即可确认解决，无需改值
        core.resolve_discrepancy(self.conn, d["id"], actor="关务")
        self.assertEqual(self._disc()["status"], "resolved")

    def test_rule_exceeded_stays_open(self):
        core.save_rule(self.conn, self.sid, "quantity", "abs", {"abs": "10"}, actor="关务")
        self._qty_conflict()
        d = self._disc()
        self.assertEqual(d["is_reconciled"], 0)
        self.assertIsNone(d["close_reason"])
        with self.assertRaises(ValueError):
            core.resolve_discrepancy(self.conn, d["id"], actor="关务")

    def test_pct_tolerance_boundary_inclusive(self):
        present = {s: {"raw": v, "norm": core.NORMALIZERS["quantity"](v)}
                   for s, v in zip(core.SOURCES, ["1000", "1020", "1000"])}
        hit = core.evaluate_tolerance("quantity", "pct", {"pct": "2", "basis": "max"}, present)
        self.assertIsNotNone(hit)                       # 恰好 2.00%，≤ 即命中
        present["forwarder"] = {"raw": "1021", "norm": "1021"}
        self.assertIsNone(core.evaluate_tolerance(
            "quantity", "pct", {"pct": "2", "basis": "max"}, present))

    def test_alias_tolerance_text(self):
        core.save_rule(self.conn, self.sid, "product_name", "alias",
                       {"aliases": "不锈钢保温杯/保温杯"}, actor="关务")
        core.submit_document(self.conn, self.sid, "supplier", "保温杯", "1000", "C1", actor="t")
        core.submit_document(self.conn, self.sid, "forwarder", "不锈钢保温杯", "1000", "C1", actor="t")
        core.submit_document(self.conn, self.sid, "warehouse", "保温杯", "1000", "C1", actor="t")
        self.assertEqual(core.list_discrepancies(self.conn, self.sid), [])

    def test_non_numeric_quantity_falls_back_to_strict(self):
        core.save_rule(self.conn, self.sid, "quantity", "abs", {"abs": "9999"}, actor="关务")
        core.submit_document(self.conn, self.sid, "supplier", "杯", "1000", "C1", actor="t")
        core.submit_document(self.conn, self.sid, "forwarder", "杯", "约1000", "C1", actor="t")
        self.assertEqual(self._disc()["is_reconciled"], 0)

    # ---- 版本化 / 收紧 / 停用 ----
    def test_rule_version_chain_append_only(self):
        core.save_rule(self.conn, self.sid, "quantity", "abs", {"abs": "50"}, actor="甲")
        core.save_rule(self.conn, self.sid, "quantity", "abs", {"abs": "10"},
                       note="收紧", actor="乙")
        rules = core.list_rules(self.conn, self.sid)
        self.assertEqual([r["version"] for r in rules], [2, 1])
        self.assertEqual([r["status"] for r in rules],
                         [core.RULE_ACTIVE, core.RULE_SUPERSEDED])

    def test_rule_tightening_reopens_tolerated_discrepancy(self):
        self._qty_conflict()
        d = self._disc()
        core.save_rule(self.conn, self.sid, "quantity", "abs", {"abs": "50"}, actor="甲")
        d = self._disc()
        self.assertEqual(d["is_reconciled"], 1)
        core.claim_discrepancy(self.conn, d["id"], owner="小李")
        # 收紧到 ±10：仍冲突 → 复用原行重开、进入新轮次、清空命中依据
        core.save_rule(self.conn, self.sid, "quantity", "abs", {"abs": "10"}, actor="乙")
        d = self._disc()
        self.assertEqual(d["is_reconciled"], 0)
        self.assertIsNone(d["close_reason"])
        self.assertIsNone(d["rule_version"])
        self.assertEqual(d["episode"], 2)
        self.assertEqual(d["owner"], "小李")  # 重开不清负责人（与资料升版口径区分）

    def test_rule_revoke_restores_strict_and_reopens(self):
        self._qty_conflict()
        core.save_rule(self.conn, self.sid, "quantity", "abs", {"abs": "50"}, actor="甲")
        self.assertEqual(self._disc()["is_reconciled"], 1)
        core.revoke_rule(self.conn, self.sid, "quantity", actor="甲")
        d = self._disc()
        self.assertEqual(d["is_reconciled"], 0)
        self.assertIsNone(d["rule_version"])
        self.assertEqual(d["episode"], 2)
        self.assertIsNone(core.active_rule(self.conn, self.sid, "quantity"))
        # 版本链保留作证据
        self.assertEqual(len(core.list_rules(self.conn, self.sid)), 1)

    def test_loosening_rule_re_tolerates_and_updates_evidence_version(self):
        core.save_rule(self.conn, self.sid, "quantity", "abs", {"abs": "10"}, actor="甲")
        self._qty_conflict()
        self.assertEqual(self._disc()["is_reconciled"], 0)
        core.save_rule(self.conn, self.sid, "quantity", "abs", {"abs": "30"}, actor="甲")
        d = self._disc()
        self.assertEqual(d["is_reconciled"], 1)
        self.assertEqual(d["close_reason"], core.CLOSE_TOLERATED)
        self.assertEqual(d["rule_version"], 2)

    def test_legacy_shipment_without_rule_stays_strict(self):
        self._qty_conflict()
        d = self._disc()
        self.assertEqual(d["is_reconciled"], 0)
        self.assertIsNone(d["rule_version"])
        self.assertEqual(__import__("json").loads(d["rule_evidence_json"]), {})

    def test_invalid_rule_config_rejected(self):
        with self.assertRaises(ValueError):
            core.save_rule(self.conn, self.sid, "quantity", "abs", {"abs": "-1"}, actor="甲")
        with self.assertRaises(ValueError):
            core.save_rule(self.conn, self.sid, "quantity", "pct", {"pct": "120"}, actor="甲")
        with self.assertRaises(ValueError):
            core.save_rule(self.conn, self.sid, "product_name", "alias",
                           {"aliases": "只有一个别名"}, actor="甲")


class WaiverLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = core.connect(os.path.join(self.tmp, "w.db"))
        core.init_db(self.conn)
        self.t0 = datetime(2026, 9, 15, 9, 0)
        self.deadline = self.t0 + timedelta(hours=48)
        self.sid = core.create_shipment(
            self.conn, "WV-1", actor="t",
            deadline=self.deadline.isoformat(timespec="minutes"), warn_hours=48)
        core.submit_document(self.conn, self.sid, "supplier", "A", "1000", "C", actor="t")
        core.submit_document(self.conn, self.sid, "forwarder", "A", "1020", "C", actor="t")
        self.did = core.list_discrepancies(self.conn, self.sid)[0]["id"]

    def tearDown(self):
        self.conn.close()

    def _grant(self, expiry, reason="船期不等人，先放行", by="关务小李"):
        return core.grant_waiver(self.conn, self.did, reason=reason,
                                 granted_by=by,
                                 expires_at=expiry.isoformat(timespec="minutes"))

    def _disc(self):
        return self.conn.execute("SELECT * FROM discrepancy WHERE id=?",
                                 (self.did,)).fetchone()

    # ---- 授予校验 ----
    def test_grant_requires_reason_operator_future_expiry(self):
        future = (self.t0 + timedelta(hours=2)).isoformat()
        with self.assertRaises(ValueError):
            core.grant_waiver(self.conn, self.did, reason="", granted_by="甲",
                              expires_at=future)
        with self.assertRaises(ValueError):
            core.grant_waiver(self.conn, self.did, reason="有原因", granted_by="",
                              expires_at=future)
        with self.assertRaises(ValueError):
            core.grant_waiver(self.conn, self.did, reason="有原因", granted_by="甲",
                              expires_at="2000-01-01T00:00")

    def test_grant_basis_freezes_raw_values_and_rule_version(self):
        wid = self._grant(self.t0 + timedelta(hours=2))
        w = core.get_waiver(self.conn, wid)
        basis = __import__("json").loads(w["basis_json"])
        self.assertIn("supplier", basis["values"])
        self.assertEqual(basis["values"]["forwarder"]["raw"], "1020")
        self.assertEqual(basis["rule_version"], None)  # 无规则 → 严格比对依据
        self.assertEqual(self._disc()["active_waiver_id"], wid)

    def test_cannot_grant_on_tolerated_or_duplicate_active(self):
        # 容差内视为一致的差异不能豁免
        core.save_rule(self.conn, self.sid, "quantity", "abs", {"abs": "50"}, actor="甲")
        self.assertEqual(self._disc()["is_reconciled"], 1)
        with self.assertRaises(ValueError):
            self._grant(self.t0 + timedelta(hours=2))
        # 收紧规则重新暴露冲突后授豁免，再授第二次应拒绝
        core.save_rule(self.conn, self.sid, "quantity", "abs", {"abs": "5"}, actor="甲")
        self._grant(self.t0 + timedelta(hours=2))
        with self.assertRaises(ValueError):
            self._grant(self.t0 + timedelta(hours=4))

    # ---- 豁免期静默 + 取消待发 ----
    def test_waiver_silences_reminders_throughout_window(self):
        self._grant(self.t0 + timedelta(hours=72))  # 有效期覆盖临近窗口与超时时刻
        s1 = reminders.scan_once(self.conn, reminders.LogNotifier(),
                                 at=self.t0 + timedelta(hours=1))
        s2 = reminders.scan_once(self.conn, reminders.LogNotifier(),
                                 at=self.deadline + timedelta(hours=1))
        self.assertEqual((s1["events_new"], s2["events_new"]), (0, 0))
        self.assertEqual(reminders.assess_shipments(self.conn, at=self.deadline), [])

    def test_grant_cancels_pending_notification(self):
        reminders.scan_once(self.conn, reminders.LogNotifier(), at=self.t0)
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM notification n JOIN reminder_event e ON n.event_id=e.id "
            "WHERE e.discrepancy_id=? AND n.status='sent'", (self.did,)).fetchone()[0]
        self.assertEqual(pending, 1)
        # 再构造一条 pending：失败通知
        flaky = reminders.FlakyNotifier(fail_times=1)
        # 新事件需新 episode，这里直接验证授予时 pending/failed 被撤销：手工补一条 failed
        self.conn.execute(
            "INSERT INTO notification (event_id, channel, target, content, status, created_at) "
            "SELECT id, 'im', '关务主管', 'x', 'pending', ? FROM reminder_event "
            "WHERE discrepancy_id=? LIMIT 1", (core.now(), self.did))
        self.conn.commit()
        self._grant(self.t0 + timedelta(hours=2))
        cancelled = self.conn.execute(
            "SELECT COUNT(*) FROM notification n JOIN reminder_event e ON n.event_id=e.id "
            "WHERE e.discrepancy_id=? AND n.status='cancelled'", (self.did,)).fetchone()[0]
        self.assertEqual(cancelled, 1)

    # ---- 到期重开 ----
    def test_expiry_reopens_reuses_row_and_resumes_reminder_same_scan(self):
        self._grant(self.t0 + timedelta(hours=2))
        # 豁免期扫描：静默
        s = reminders.scan_once(self.conn, reminders.LogNotifier(),
                                at=self.t0 + timedelta(hours=1))
        self.assertEqual(s["events_new"], 0)
        # 到期后同一轮扫描：先重开 episode 2，再恢复提醒
        s = reminders.scan_once(self.conn, reminders.LogNotifier(),
                                at=self.t0 + timedelta(hours=3))
        self.assertEqual(s["waivers_expired"], 1)
        self.assertEqual(s["waivers_reopened"], 1)
        self.assertEqual(s["events_new"], 1)
        self.assertEqual(self._disc()["episode"], 2)
        w = self.conn.execute("SELECT * FROM waiver WHERE discrepancy_id=?",
                              (self.did,)).fetchone()
        self.assertEqual(w["status"], core.WAIVER_EXPIRED)
        # 再扫不重复
        s2 = reminders.scan_once(self.conn, reminders.LogNotifier(),
                                 at=self.t0 + timedelta(hours=4))
        self.assertEqual((s2["events_new"], s2["waivers_expired"]), (0, 0))

    def test_expiry_when_conflict_gone_does_not_bump(self):
        self._grant(self.t0 + timedelta(hours=2))
        # 到期前资料改齐：豁免自动终结（superseded），不 bump
        core.submit_document(self.conn, self.sid, "forwarder", "A", "1000", "C", actor="t")
        d = self._disc()
        self.assertEqual(d["is_reconciled"], 1)
        self.assertEqual(d["episode"], 1)
        stats = core.expire_due_waivers(self.conn, at=self.t0 + timedelta(hours=3))
        self.assertEqual((stats["expired"], stats["reopened"]), (0, 0))
        w = self.conn.execute("SELECT * FROM waiver WHERE discrepancy_id=?",
                              (self.did,)).fetchone()
        self.assertEqual(w["status"], core.WAIVER_SUPERSEDED)

    # ---- 撤销重开 ----
    def test_manual_revoke_reopens_and_reminds(self):
        wid = self._grant(self.t0 + timedelta(hours=20))
        result = core.revoke_waiver(self.conn, wid, actor="关务主管")
        self.assertTrue(result["bumped"])
        self.assertEqual(self._disc()["episode"], 2)
        w = core.get_waiver(self.conn, wid)
        self.assertEqual(w["status"], core.WAIVER_REVOKED)
        self.assertEqual(w["revoked_by"], "关务主管")
        s = reminders.scan_once(self.conn, reminders.LogNotifier(), at=self.t0)
        self.assertEqual(s["events_new"], 1)  # 新 episode 恢复催办

    def test_revoke_non_active_rejected(self):
        wid = self._grant(self.t0 + timedelta(hours=20))
        core.revoke_waiver(self.conn, wid, actor="甲")
        with self.assertRaises(ValueError):
            core.revoke_waiver(self.conn, wid, actor="甲")

    # ---- 资料升版 ----
    def test_doc_upgrade_still_conflicting_ends_waiver_and_bumps(self):
        self._grant(self.t0 + timedelta(hours=20))
        # 供应商数量变化但仍冲突（1005 vs 1020）
        core.submit_document(self.conn, self.sid, "supplier", "A", "1005", "C", actor="t")
        d = self._disc()
        self.assertEqual(d["is_reconciled"], 0)
        self.assertEqual(d["episode"], 2)
        self.assertIsNone(d["active_waiver_id"])
        w = self.conn.execute("SELECT * FROM waiver WHERE discrepancy_id=?",
                              (self.did,)).fetchone()
        self.assertEqual(w["status"], core.WAIVER_SUPERSEDED)
        s = reminders.scan_once(self.conn, reminders.LogNotifier(), at=self.t0)
        self.assertEqual(s["events_new"], 1)

    def test_doc_upgrade_aligned_ends_waiver_without_bump(self):
        self._grant(self.t0 + timedelta(hours=20))
        core.submit_document(self.conn, self.sid, "forwarder", "A", "1000", "C", actor="t")
        d = self._disc()
        self.assertEqual(d["is_reconciled"], 1)
        self.assertEqual(d["episode"], 1)
        self.assertIsNone(d["active_waiver_id"])
        # 无新风险
        s = reminders.scan_once(self.conn, reminders.LogNotifier(),
                                at=self.deadline + timedelta(hours=1))
        self.assertEqual(s["events_new"], 0)

    def test_duplicate_submit_keeps_waiver_alive(self):
        self._grant(self.t0 + timedelta(hours=20))
        r = core.submit_document(self.conn, self.sid, "supplier", "A", "1000", "C", actor="t")
        self.assertTrue(r["duplicate"])
        self.assertIsNotNone(self._disc()["active_waiver_id"])

    def test_unrelated_field_change_keeps_waiver_alive(self):
        self._grant(self.t0 + timedelta(hours=20))
        core.submit_document(self.conn, self.sid, "supplier", "A", "1000", "C-NEW", actor="t")
        self.assertIsNotNone(self._disc()["active_waiver_id"])

    def test_manual_recompute_keeps_active_waiver(self):
        self._grant(self.t0 + timedelta(hours=20))
        core.recompute(self.conn, self.sid, actor="t")  # 手动重核不传 changed_fields
        self.assertIsNotNone(self._disc()["active_waiver_id"])
        self.assertEqual(self._disc()["episode"], 1)

    def test_new_conflict_after_superseded_waiver_reuses_row_episode3(self):
        # 豁免→改齐(superseded, ep1)→再冲突(ep2)→再豁免→到期重开(ep3)
        self._grant(self.t0 + timedelta(hours=20))
        core.submit_document(self.conn, self.sid, "forwarder", "A", "1000", "C", actor="t")
        self.assertEqual(self._disc()["episode"], 1)
        core.submit_document(self.conn, self.sid, "forwarder", "A", "1099", "C", actor="t")
        self.assertEqual(self._disc()["episode"], 2)
        wid = self._grant(self.t0 + timedelta(hours=40))
        self.assertEqual(core.get_waiver(self.conn, wid)["episode"], 2)
        stats = core.expire_due_waivers(self.conn, at=self.t0 + timedelta(hours=41))
        self.assertEqual(stats["reopened"], 1)
        self.assertEqual(self._disc()["episode"], 3)


class WaiverFreezeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = core.connect(os.path.join(self.tmp, "wf.db"))
        core.init_db(self.conn)
        self.sid = core.create_shipment(self.conn, "WF-1", actor="t",
                                        deadline="2026-09-20T10:00", warn_hours=24)
        for src, qty in (("supplier", "1000"), ("forwarder", "1020"), ("warehouse", "1000")):
            core.submit_document(self.conn, self.sid, src, "保温杯", qty, "C1", actor="t")
        self.did = next(d["id"] for d in core.list_discrepancies(self.conn, self.sid)
                        if d["field"] == "quantity")
        # 无规则严格比对下仍冲突 → 可豁免；规则在各用例里按需配置
        self.wid = core.grant_waiver(
            self.conn, self.did, reason="海关允许短装，限时放行",
            granted_by="关务小李", expires_at="2026-09-21T18:00")

    def tearDown(self):
        self.conn.close()

    def test_waived_open_item_does_not_block_freeze(self):
        # 豁免中的差异不算未解决：无需 confirm
        r = core.freeze_package(self.conn, self.sid, actor="关务小李")
        self.assertEqual((r["open_items"],), (0,))
        pkg = core.get_package(self.conn, core.list_packages(self.conn, self.sid)[0]["id"])
        self.assertEqual(pkg["waived_items"], 1)

    def test_package_holds_rule_and_waiver_evidence(self):
        # 配一个不覆盖当前差异（差 20 > ±10）的规则：豁免继续有效，规则版本链随包固化
        core.save_rule(self.conn, self.sid, "quantity", "abs", {"abs": "10"}, actor="关务")
        self.assertIsNotNone(self.conn.execute(
            "SELECT active_waiver_id FROM discrepancy WHERE id=?", (self.did,)).fetchone()[0])
        core.freeze_package(self.conn, self.sid, actor="关务小李")
        pkg = core.get_package(self.conn, core.list_packages(self.conn, self.sid)[0]["id"])
        rules = pkg["tolerance_rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual((rules[0]["version"], rules[0]["mode"], rules[0]["status"]),
                         (1, "abs", core.RULE_ACTIVE))
        d = next(x for x in pkg["discrepancies"] if x["field"] == "quantity")
        w = next(x for x in d["waivers"] if x["id"] == self.wid)
        self.assertEqual(w["reason"], "海关允许短装，限时放行")
        self.assertEqual(w["granted_by"], "关务小李")
        self.assertEqual(w["status"], core.WAIVER_ACTIVE)
        self.assertEqual(w["basis"]["values"]["forwarder"]["raw"], "1020")
        self.assertTrue(any("限时豁免期内" in x for x in pkg["warnings"]))

    def test_frozen_evidence_immutable_after_rule_change_and_revoke(self):
        core.freeze_package(self.conn, self.sid, actor="关务小李")
        pid = core.list_packages(self.conn, self.sid)[0]["id"]
        before = core.get_package(self.conn, pid)
        # 冻结后：撤销豁免、规则收紧出新版本、停用规则、资料升版——旧包字节级不变
        core.revoke_waiver(self.conn, self.wid, actor="别人")
        core.save_rule(self.conn, self.sid, "quantity", "abs", {"abs": "50"}, actor="别人")
        core.revoke_rule(self.conn, self.sid, "quantity", actor="别人")
        core.submit_document(self.conn, self.sid, "forwarder", "保温杯", "1099", "C1", actor="t")
        after = core.get_package(self.conn, pid)
        self.assertEqual(after, before)
        frozen_d = next(x for x in after["discrepancies"] if x["field"] == "quantity")
        frozen_w = frozen_d["waivers"][0]
        self.assertEqual(frozen_w["status"], core.WAIVER_ACTIVE)  # 撤销不改写包内证据
        self.assertEqual(after["tolerance_rules"], [])             # 冻结时无规则，后来配的不进包

    def test_tolerated_adopt_source_in_package(self):
        core.revoke_waiver(self.conn, self.wid, actor="关务小李")
        # 货代改到 1008（与 1000 差 8），规则 ±10 且采用货代值
        core.save_rule(self.conn, self.sid, "quantity", "abs",
                       {"abs": "10", "adopt_source": "forwarder"}, actor="关务")
        core.submit_document(self.conn, self.sid, "forwarder", "保温杯", "1008", "C1", actor="t")
        core.freeze_package(self.conn, self.sid, actor="关务小李")
        pkg = core.get_package(self.conn, core.list_packages(self.conn, self.sid)[0]["id"])
        entry = pkg["declared_fields"]["quantity"]
        self.assertEqual(entry["adoption_basis"], core.CLOSE_TOLERATED)
        self.assertEqual(entry["adopted_value"], "1008")
        self.assertEqual(entry["basis_sources"], ["forwarder"])
        d = next(x for x in pkg["discrepancies"] if x["field"] == "quantity")
        self.assertEqual(d["close_reason"], core.CLOSE_TOLERATED)
        self.assertEqual(d["rule_version"], 1)
        md = core.export_markdown(pkg)
        self.assertIn("容差规则 v", md)
        self.assertIn("豁免记录", md)
        self.assertIn("海关允许短装", md)


class FlaskSmokeTest(unittest.TestCase):
    def setUp(self):
        os.environ["CUSTOMS_DB"] = os.path.join(tempfile.mkdtemp(), "http.db")
        import app as flask_app
        flask_app.db  # noqa: F841 - 确保导入
        self.client = flask_app.app.test_client()

    def test_create_and_resolve_via_http(self):
        r = self.client.post("/shipments/new", data={
            "ref": "WEB-1", "customer": "客户甲", "description": "", "actor": "林"},
            follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("WEB-1", r.get_data(as_text=True))

        sid = 1
        self.client.post(f"/shipments/{sid}/documents", data={
            "source": "supplier", "product_name": "保温杯", "quantity": "1000",
            "carton_no": "C1", "actor": "王"})
        self.client.post(f"/shipments/{sid}/documents", data={
            "source": "forwarder", "product_name": "真空保温杯", "quantity": "1020",
            "carton_no": "C1", "actor": "陈"})
        page = self.client.get(f"/shipments/{sid}").get_data(as_text=True)
        self.assertIn("不一致", page)

        # 重复提交提示
        r = self.client.post(f"/shipments/{sid}/documents", data={
            "source": "forwarder", "product_name": "真空保温杯", "quantity": "1020",
            "carton_no": "C1", "actor": "陈"}, follow_redirects=True)
        self.assertIn("重复提交", r.get_data(as_text=True))

        # 未核对一致时，页面不应出现"确认已解决"
        self.assertNotIn("确认已解决", page)

        # 补件改齐
        self.client.post(f"/shipments/{sid}/documents", data={
            "source": "forwarder", "product_name": "保温杯", "quantity": "1000",
            "carton_no": "C1", "actor": "陈"})
        page = self.client.get(f"/shipments/{sid}").get_data(as_text=True)
        self.assertIn("核对一致，待确认", page)

        # 认领后解决
        import core
        dids = [d["id"] for d in core.list_discrepancies(core.connect(os.environ["CUSTOMS_DB"]), sid)]
        for did in dids:
            self.client.post(f"/discrepancies/{did}/claim", data={"owner": "李"})
        page = self.client.get(f"/shipments/{sid}").get_data(as_text=True)
        self.assertIn("确认已解决", page)
        for did in dids:
            self.client.post(f"/discrepancies/{did}/resolve", data={"actor": "李"})
        page = self.client.get(f"/shipments/{sid}").get_data(as_text=True)
        self.assertIn("已完结", page)


class ToleranceWaiverFlaskTest(unittest.TestCase):
    def setUp(self):
        os.environ["CUSTOMS_DB"] = os.path.join(tempfile.mkdtemp(), "tw.db")
        import app as flask_app
        self.app = flask_app.app
        self.client = flask_app.app.test_client()

    def test_rule_waiver_freeze_http_flow(self):
        c = self.client
        c.post("/shipments/new", data={"ref": "WEB-TW", "actor": "林"})
        sid = 1
        for src, qty in (("supplier", "1000"), ("forwarder", "1020"),
                         ("warehouse", "1000")):
            c.post(f"/shipments/{sid}/documents",
                   data={"source": src, "product_name": "保温杯", "quantity": qty,
                         "carton_no": "C1", "actor": "王"})
        page = c.get(f"/shipments/{sid}").get_data(as_text=True)
        self.assertIn("不一致", page)
        self.assertIn("容差规则", page)

        # 规则过窄 → 仍是不一致；放宽后容差内待确认
        r = c.post(f"/shipments/{sid}/rules",
                   data={"field": "quantity", "qty_mode": "abs", "abs": "10",
                         "adopt_source": "supplier", "actor": "关务"},
                   follow_redirects=True)
        self.assertIn("v1", r.get_data(as_text=True))
        self.assertIn("不一致", r.get_data(as_text=True))
        r = c.post(f"/shipments/{sid}/rules",
                   data={"field": "quantity", "qty_mode": "abs", "abs": "50",
                         "adopt_source": "supplier", "actor": "关务"},
                   follow_redirects=True)
        self.assertIn("容差内一致（v2），待确认", r.get_data(as_text=True))

        # 停用规则 → 恢复严格，差异重新打开
        r = c.post(f"/shipments/{sid}/rules/revoke",
                   data={"field": "quantity", "actor": "关务"},
                   follow_redirects=True)
        self.assertIn("✗ 不一致", r.get_data(as_text=True))

        # 缺字段的豁免申请被拒（400 业务错误→flash 重定向）
        r = c.post("/discrepancies/1/waivers",
                   data={"reason": "", "granted_by": "", "expires_at": ""},
                   follow_redirects=True)
        self.assertIn("豁免必须填写原因", r.get_data(as_text=True))

        # 完整豁免 → 页面显示豁免中、不催办
        r = c.post("/discrepancies/1/waivers",
                   data={"reason": "船期不等人", "granted_by": "关务小李",
                         "expires_at": "2026-12-31T18:00"},
                   follow_redirects=True)
        page = r.get_data(as_text=True)
        self.assertIn("限时豁免中", page)
        self.assertIn("船期不等人", page)
        self.assertIn("第 2 轮", page)

        # 豁免中可直接冻结，包导出含规则/豁免证据
        r = c.post(f"/shipments/{sid}/packages",
                   data={"actor": "关务小李"}, follow_redirects=True)
        self.assertIn("已冻结", r.get_data(as_text=True))
        md = c.get("/packages/1/export.md").get_data(as_text=True)
        self.assertIn("豁免记录", md)
        self.assertIn("船期不等人", md)
        blob = c.get("/packages/1/export.json").get_data(as_text=True)
        self.assertIn("tolerance_rules", blob)

        # 撤销豁免 → 提示重开新一轮
        r = c.post("/waivers/1/revoke", data={"actor": "关务主管"},
                   follow_redirects=True)
        self.assertIn("已复用原差异行开启新一轮提醒", r.get_data(as_text=True))


class PackageWorkflowFlaskTest(unittest.TestCase):
    def setUp(self):
        os.environ["CUSTOMS_DB"] = os.path.join(tempfile.mkdtemp(), "pw.db")
        import app as flask_app
        self.app = flask_app.app
        self.c = flask_app.app.test_client()
        self.c.post("/shipments/new", data={"ref": "WEB-PW", "actor": "林"})
        for src in ("supplier", "forwarder", "warehouse"):
            self.c.post("/shipments/1/documents",
                        data={"source": src, "product_name": "保温杯", "quantity": "1000",
                              "carton_no": "C1", "actor": "王"})

    def _freeze(self, actor="制单员", designated="放行员孙"):
        page = self.c.get("/shipments/1").get_data(as_text=True)
        import re
        m = re.search(r'name="idem_key" value="([^"]+)"', page)
        self.assertIsNotNone(m)
        r = self.c.post("/shipments/1/packages",
                        data={"actor": actor, "designated_operator": designated,
                              "idem_key": m.group(1)},
                        follow_redirects=True)
        self.assertIn("进入待复核", r.get_data(as_text=True))

    def test_review_release_http_happy_path_and_watermark(self):
        self._freeze()
        # 未放行：下载件带内部水印，文件名带“内部复核稿”
        md = self.c.get("/packages/1/export.md")
        self.assertIn(core.INTERNAL_REVIEW_WATERMARK, md.get_data(as_text=True))
        self.assertIn("内部复核稿", md.headers["Content-Disposition"])
        blob = self.c.get("/packages/1/export.json").get_data(as_text=True)
        self.assertIn('"internal_review_only"', blob)

        page = self.c.get("/shipments/1").get_data(as_text=True)
        self.assertIn("待复核", page)
        # 同一人兼任两角色 → 被拒
        self.c.post("/packages/1/reviews",
                    data={"role": "customs_reviewer", "actor": "赵", "decision": "approve",
                          "comment": "已核对"})
        r = self.c.post("/packages/1/reviews",
                        data={"role": "supervisor", "actor": "赵", "decision": "approve"},
                        follow_redirects=True)
        self.assertIn("必须是不同人员", r.get_data(as_text=True))
        # 驳回必须填原因（先试空原因驳回另一角色）
        # 不同主管通过 → 会签完成
        r = self.c.post("/packages/1/reviews",
                        data={"role": "supervisor", "actor": "钱", "decision": "approve",
                              "comment": "同意"},
                        follow_redirects=True)
        self.assertIn("两方会签均已通过", r.get_data(as_text=True))
        # 非指定操作人放行 → 拒绝
        r = self.c.post("/packages/1/release", data={"actor": "外人"},
                        follow_redirects=True)
        self.assertIn("无权放行", r.get_data(as_text=True))
        # 指定操作人放行
        r = self.c.post("/packages/1/release", data={"actor": "放行员孙"},
                        follow_redirects=True)
        self.assertIn("申报包已放行", r.get_data(as_text=True))
        # 已放行：正式件无水印、文件名无内部稿后缀、JSON 标记为正式
        md = self.c.get("/packages/1/export.md").get_data(as_text=True)
        self.assertIn("正式申报包（已放行）", md)
        self.assertNotIn(core.INTERNAL_REVIEW_WATERMARK, md)
        headers = self.c.get("/packages/1/export.md").headers["Content-Disposition"]
        self.assertNotIn("内部复核稿", headers)
        blob = self.c.get("/packages/1/export.json").get_data(as_text=True)
        self.assertIn('"released_declaration"', blob)
        self.assertIn("放行员孙", blob)
        # 放行后重复点击：幂等提示
        r = self.c.post("/packages/1/release", data={"actor": "放行员孙"},
                        follow_redirects=True)
        self.assertIn("重复点击已忽略", r.get_data(as_text=True))

    def test_reject_http_blocks_and_new_package_resets(self):
        self._freeze()
        r = self.c.post("/packages/1/reviews",
                        data={"role": "customs_reviewer", "actor": "赵",
                              "decision": "reject", "comment": ""},
                        follow_redirects=True)
        self.assertIn("驳回必须填写原因", r.get_data(as_text=True))
        r = self.c.post("/packages/1/reviews",
                        data={"role": "customs_reviewer", "actor": "赵",
                              "decision": "reject", "comment": "缺仓库签章"},
                        follow_redirects=True)
        self.assertIn("已驳回", r.get_data(as_text=True))
        # 驳回包的下载件明确作废
        md = self.c.get("/packages/1/export.md").get_data(as_text=True)
        self.assertIn("作废包", md)
        self.assertIn("缺仓库签章", md)
        # 放行尝试被拒
        r = self.c.post("/packages/1/release", data={"actor": "放行员孙"},
                        follow_redirects=True)
        self.assertIn("已被驳回", r.get_data(as_text=True))
        # 冻结新包：#2 待复核，没有挪过来的旧审批
        self._freeze(designated="")
        page = self.c.get("/shipments/1").get_data(as_text=True)
        self.assertIn("#2", page)
        pkg2 = json.loads(self.c.get("/packages/2/export.json").get_data(as_text=True))
        self.assertEqual(pkg2["workflow"]["status"], core.PACKAGE_PENDING)
        self.assertEqual(pkg2["workflow"]["reviews"], [])

    def test_freeze_double_submit_with_same_key_dedupes(self):
        import re
        import core as _core
        page = self.c.get("/shipments/1").get_data(as_text=True)
        key = re.search(r'name="idem_key" value="([^"]+)"', page).group(1)
        # 同一表单实例（同防重键）连续 POST 两次，模拟双击/重放
        self.c.post("/shipments/1/packages",
                    data={"actor": "制单员", "idem_key": key})
        r = self.c.post("/shipments/1/packages",
                        data={"actor": "制单员", "idem_key": key},
                        follow_redirects=True)
        self.assertIn("冻结请求重复", r.get_data(as_text=True))
        pkgs = _core.list_packages(_core.connect(os.environ["CUSTOMS_DB"]), 1)
        self.assertEqual(len(pkgs), 1)
        # 新页面给出新 key → 再冻得到 #2
        page = self.c.get("/shipments/1").get_data(as_text=True)
        key2 = re.search(r'name="idem_key" value="([^"]+)"', page).group(1)
        self.assertNotEqual(key2, key)
        self.c.post("/shipments/1/packages",
                    data={"actor": "制单员", "idem_key": key2})
        pkgs = _core.list_packages(_core.connect(os.environ["CUSTOMS_DB"]), 1)
        self.assertEqual([p["package_no"] for p in pkgs], [1, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
