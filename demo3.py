#!/usr/bin/env python3
"""演示第三期：申报包冻结与导出——不可变快照，冻结后新版本改不动它。

直接运行：python3 demo3.py
"""
from __future__ import annotations

import json
import os
import tempfile

import core

SEP = "=" * 72


def show(title: str) -> None:
    print(f"\n{SEP}\n{title}\n{SEP}")


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="customs-demo3-")
    conn = core.connect(os.path.join(tmp, "demo3.db"))
    core.init_db(conn)

    show("步骤 1：建票并录入三方资料——数量一处冲突")
    sid = core.create_shipment(conn, "SH-20260918-03", customer="远航贸易",
                               description="宁波 → 洛杉矶", actor="小林",
                               deadline="2026-09-18T10:00", warn_hours=24)
    core.submit_document(conn, sid, "supplier", "不锈钢保温杯", "1000", "CBMU1", actor="供应商小王")
    core.submit_document(conn, sid, "forwarder", "不锈钢保温杯", "1020", "CBMU1", actor="货代小陈")
    core.submit_document(conn, sid, "warehouse", "不锈钢保温杯", "1000", "CBMU1", actor="仓库小赵")
    did = core.list_discrepancies(conn, sid)[0]["id"]
    core.claim_discrepancy(conn, did, owner="关务小李")
    core.add_supplement(conn, did, note="已与供应商核实，实装 1020 个，等更正版 PL", actor="关务小李")
    print("数量差异已认领并补件")

    show("步骤 2：有未解决差异时尝试冻结 → 默认阻止")
    try:
        core.freeze_package(conn, sid, actor="关务小李")
        raise AssertionError("应当阻止")
    except ValueError as e:
        print("已阻止：", e)

    show("步骤 3：供应商更正 v2、仓库确认 v2，三方一致并解决差异")
    core.submit_document(conn, sid, "supplier", "不锈钢保温杯", "1020", "CBMU1", actor="供应商小王")
    core.submit_document(conn, sid, "warehouse", "不锈钢保温杯", "1020", "CBMU1", actor="仓库小赵")
    d = core.list_discrepancies(conn, sid)[0]
    core.resolve_discrepancy(conn, d["id"], actor="关务小李")
    print("差异已解决，可以冻结干净的申报包")

    show("步骤 4：冻结申报包 #1")
    r = core.freeze_package(conn, sid, actor="关务小李")
    print("冻结结果：", r)
    pkg_id = conn.execute("SELECT id FROM declaration_package WHERE package_no=1").fetchone()["id"]
    pkg1 = core.get_package(conn, pkg_id)
    print("采用值：", {label: e["adopted_value"]
                      for label, e in
                      ((core.FIELD_LABELS[k], v) for k, v in pkg1["declared_fields"].items())})
    print("依据来源（数量）：",
          [core.SOURCE_LABELS[s] for s in pkg1["declared_fields"]["quantity"]["basis_sources"]])

    show("步骤 5：导出申报核对单（Markdown，带来源/版本/提交人/时间）")
    md = core.export_markdown(pkg1)
    print("\n".join(md.splitlines()[:26]))
    md_path = os.path.join(tmp, "SH-20260918-03-pkg1.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"\n完整核对单已写出：{md_path}")

    show("步骤 6：冻结后货代数量一度改错再改回、三方一致换箱单号——申报包 #1 不得被改动")
    core.submit_document(conn, sid, "forwarder", "不锈钢保温杯", "999", "CBMU1", actor="货代小陈")
    core.submit_document(conn, sid, "forwarder", "不锈钢保温杯", "1020", "CBMU1", actor="货代小陈")
    # 数量一度再冲突又改回：确认一致后内部把该差异重新解决
    d2 = next(x for x in core.list_discrepancies(conn, sid) if x["field"] == "quantity")
    core.resolve_discrepancy(conn, d2["id"], actor="关务小李")
    # 三方一致改用新箱单号（正常改单，不产生差异），各方都升了版本
    for src, actor in (("supplier", "供应商小王"), ("forwarder", "货代小陈"),
                       ("warehouse", "仓库小赵")):
        core.submit_document(conn, sid, src, "不锈钢保温杯", "1020", "CBMU1-FINAL", actor=actor)

    pkg1_after = core.get_package(conn, pkg_id)
    assert pkg1_after == pkg1, "申报包 #1 内容被新版本改动！"
    qty_in_pkg = pkg1_after["declared_fields"]["quantity"]
    print("申报包 #1 当前数量采用值仍为：", qty_in_pkg["adopted_value"])
    print("申报包 #1 中三方版本：",
          {core.SOURCE_LABELS[k]: v["version"] for k, v in pkg1_after["documents"].items()})
    print("而数据库现行货代版本：", core.get_document(conn, sid, "forwarder")["version"],
          "，现行箱单号：", core.get_document(conn, sid, "warehouse")["carton_no"])
    assert qty_in_pkg["adopted_value"] == "1020"
    frozen_versions = {k: v["version"] for k, v in pkg1["documents"].items()}
    assert {k: v["version"] for k, v in pkg1_after["documents"].items()} == frozen_versions
    print("申报包 #1 与现行数据已隔离 ✓（旧快照不可变，库里也没有 UPDATE/DELETE 该包）")

    show("步骤 7：按当前新版本再冻一包 #2——两包并存、内容各自独立")
    r2 = core.freeze_package(conn, sid, actor="关务小张")
    print("冻结结果：", r2)
    pkg2_id = conn.execute("SELECT id FROM declaration_package WHERE package_no=2").fetchone()["id"]
    pkg2 = core.get_package(conn, pkg2_id)
    assert pkg2["declared_fields"]["carton_no"]["adopted_value"] == "CBMU1-FINAL"
    assert pkg1["declared_fields"]["carton_no"]["adopted_value"] == "CBMU1"
    print("包 #1 箱单号采用值：", pkg1["declared_fields"]["carton_no"]["adopted_value"])
    print("包 #2 箱单号采用值：", pkg2["declared_fields"]["carton_no"]["adopted_value"])
    print("两个申报包内容独立、都可导出 ✓")

    show("步骤 8：风险留痕——带未解决差异强制冻结（确认制），风险与确认人写入包内和日志")
    core.submit_document(conn, sid, "forwarder", "不锈钢保温杯", "777", "CBMU1-NEW", actor="货代小陈")
    r3 = core.freeze_package(conn, sid, actor="关务小张", confirm=True)
    print("带差异冻结结果：", r3)
    pkg3 = core.get_package(conn,
                            conn.execute("SELECT id FROM declaration_package WHERE package_no=3")
                            .fetchone()["id"])
    open_issues = [d for d in pkg3["discrepancies"]
                   if d["status"] != "resolved" and not d["is_reconciled"]]
    print("包 #3 中未解决差异字段：", [d["field_label"] for d in open_issues])
    print("包 #3 风险提示：")
    for w in pkg3["warnings"]:
        print("  ⚠️", w)
    assert any("仍不一致" in w or "未解决差异" in w for w in pkg3["warnings"])

    show("留痕节选（申报包相关）")
    for l in core.list_logs(conn, sid):
        if l["action"].startswith("package_"):
            print(f"[{l['created_at']}] {l['actor']:<8} {l['detail']}")

    print(f"\n{SEP}演示通过：申报包冻结当前采用版本与差异结论；来源/版本/提交人/时间齐备；\n"
          f"          冻结后新版本改不动旧包；多包并存；带差异冻结有确认、有风险提示、有留痕。\n"
          f"演示数据库：{os.path.join(tmp, 'demo3.db')}{SEP}")


if __name__ == "__main__":
    main()
