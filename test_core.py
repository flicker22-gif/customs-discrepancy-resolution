"""单元测试：比对引擎、幂等（版本更新/重复提交不产生第二条差异）、认领/补件/解决约束、Flask 冒烟。"""
import os
import tempfile
import unittest

import core


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
