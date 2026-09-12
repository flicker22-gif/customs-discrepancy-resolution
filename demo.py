#!/usr/bin/env python3
"""端到端演示：一票货两处冲突 → 认领 → 重复提交不新增差异 → 补件 → 重新核对 → 解决。

直接运行：python3 demo.py
"""
from __future__ import annotations

import os
import tempfile

import core

SEP = "=" * 72


def show(title: str) -> None:
    print(f"\n{SEP}\n{title}\n{SEP}")


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="customs-demo-")
    db_path = os.path.join(tmp, "demo.db")
    conn = core.connect(db_path)
    core.init_db(conn)

    # 1) 建票
    show("步骤 1：建票 SH-20260912-01")
    sid = core.create_shipment(conn, "SH-20260912-01", customer="深圳远航贸易",
                               description="宁波 → 洛杉矶 / 家居用品", actor="操作员小林")
    print(f"货票 #{sid} 已建")

    # 2) 三方提交：品名、数量两处冲突；箱单号三方一致
    show("步骤 2：录入三方资料（供应商/货代/仓库）——预期两处冲突：品名、数量")
    core.submit_document(conn, sid, "supplier",
                         product_name="不锈钢保温杯", quantity="1000",
                         carton_no="CBMU1234567", actor="供应商小王")
    core.submit_document(conn, sid, "forwarder",
                         product_name="不锈钢真空保温杯", quantity="1020",
                         carton_no="CBMU1234567", actor="货代小陈")
    core.submit_document(conn, sid, "warehouse",
                         product_name="不锈钢保温杯", quantity="1000",
                         carton_no="cbmu1234567", actor="仓库小赵")

    discs = core.list_discrepancies(conn, sid)
    print(f"差异条数：{len(discs)}（应为 2）")
    assert [d["field"] for d in discs] == ["product_name", "quantity"], "冲突字段应为品名、数量"
    for d in discs:
        vals = core.discrepancy_values(d)
        raw = "，".join(f"{core.SOURCE_LABELS[k]}「{v['raw']}」" for k, v in vals.items())
        print(f"  - 【{core.FIELD_LABELS[d['field']]}】状态={d['status']}  {raw}")
    print("箱单号三方写法 CBMU1234567 / CBMU1234567 / cbmu1234567（大小写不同）→ 归一化一致，无差异 ✓")

    # 3) 负责人认领两条差异
    show("步骤 3：关务小李认领两处差异")
    for d in discs:
        core.claim_discrepancy(conn, d["id"], owner="关务小李")
    discs = core.list_discrepancies(conn, sid)
    assert all(d["status"] == "claimed" and d["owner"] == "关务小李" for d in discs)
    print("两处差异均已认领，负责人=关务小李 ✓")

    # 4) 货代重复提交完全相同的内容
    show("步骤 4：货代重复提交完全相同的资料（防重复：不升版本、不新增差异）")
    before = len(core.list_discrepancies(conn, sid))
    result = core.submit_document(conn, sid, "forwarder",
                                  product_name="不锈钢真空保温杯", quantity="1020",
                                  carton_no="CBMU1234567", actor="货代小陈")
    after = len(core.list_discrepancies(conn, sid))
    fwd = conn.execute("SELECT version FROM document WHERE shipment_id=? AND source='forwarder'",
                       (sid,)).fetchone()
    print(f"返回 duplicate={result['duplicate']}，货代资料版本仍为 v{fwd['version']}，"
          f"差异条数 {before} → {after}")
    assert result["duplicate"] is True and fwd["version"] == 1 and before == after == 2
    print("重复提交被忽略，仍只有两条差异 ✓")

    # 5) 再用同一来源"版本更新"但冲突依旧（数量写成 1,020 千分位）
    show("步骤 5：货代更新版本但内容实质未变（1020 → 1,020，归一化相同）——差异行复用，不新增")
    core.submit_document(conn, sid, "forwarder",
                         product_name="不锈钢真空保温杯", quantity="1,020",
                         carton_no="CBMU1234567", actor="货代小陈")
    discs = core.list_discrepancies(conn, sid)
    assert len(discs) == 2
    qty = next(d for d in discs if d["field"] == "quantity")
    print(f"差异仍为 {len(discs)} 条；数量差异行 id={qty['id']} 复用，"
          f"货代版本 v{core.discrepancy_values(qty)['forwarder']['version']}，仍未核对一致 ✓")

    # 6) 补件
    show("步骤 6：补件——供应商核实后确认实际以货代 1020 为准，品名以供应商为准")
    qty_id = qty["id"]
    name_id = next(d for d in discs if d["field"] == "product_name")["id"]
    core.add_supplement(conn, qty_id,
                        note="已与供应商电话核实，实际装箱 1020 个，等待供应商更正版 PL",
                        actor="关务小李")
    core.add_supplement(conn, name_id,
                        note="货代承认多写“真空”二字，将在补料中删去",
                        actor="关务小李")
    print("补件说明已追加到两条差异（全部留痕）✓")

    # 7) 补件后资料改齐：供应商数量改 1020；货代品名改“不锈钢保温杯”；仓库确认 1020
    show("步骤 7：补件到位——供应商 v2（数量 1020）、货代 v3（品名改为不锈钢保温杯）、仓库 v2（确认 1020）")
    core.submit_document(conn, sid, "supplier",
                         product_name="不锈钢保温杯", quantity="1020",
                         carton_no="CBMU1234567", actor="供应商小王")
    core.submit_document(conn, sid, "forwarder",
                         product_name="不锈钢保温杯", quantity="1,020",
                         carton_no="CBMU1234567", actor="货代小陈")
    core.submit_document(conn, sid, "warehouse",
                         product_name="不锈钢保温杯", quantity="1020",
                         carton_no="CBMU1234567", actor="仓库小赵")
    discs = core.list_discrepancies(conn, sid)
    assert len(discs) == 2 and all(d["is_reconciled"] for d in discs), "两字段应已核对一致"
    print("自动重新比对：两条差异均变为「✓ 核对一致，待确认」，差异没有被删除/新建 ✓")

    # 8) 未核对一致时不能解决——此处已一致，负责人确认解决
    show("步骤 8：负责人将两条差异标记已解决")
    for d in discs:
        core.resolve_discrepancy(conn, d["id"], actor="关务小李")
    shipment = core.get_shipment(conn, sid)
    assert shipment["status"] == "resolved"
    print(f"两条差异均已解决，货票整体状态={shipment['status']}（已完结）✓")

    # 9) 回归场景：新版本再次冲突 → 同一差异行重新打开（不新增）
    show("步骤 9（回归验证）：仓库突然改报数量 1010 → 同一差异行重新打开，仍只有 2 条")
    core.submit_document(conn, sid, "warehouse",
                         product_name="不锈钢保温杯", quantity="1010",
                         carton_no="CBMU1234567", actor="仓库小赵")
    discs = core.list_discrepancies(conn, sid)
    qty_row = next(d for d in discs if d["field"] == "quantity")
    assert len(discs) == 2 and qty_row["id"] == qty_id
    assert qty_row["status"] == "open" and qty_row["owner"] is None
    print(f"数量差异复用原行 id={qty_row['id']}，状态重新打开并退回待认领，差异总数仍为 {len(discs)} ✓")

    # 10) 留痕
    show("步骤 10：全量操作留痕（节选 12 条）")
    logs = core.list_logs(conn, sid)
    for l in logs[:12]:
        print(f"[{l['created_at']}] {l['actor']:<8} {l['detail']}")
    print(f"\n共 {len(logs)} 条留痕")

    print(f"\n{SEP}\n演示通过：两处冲突 → 认领 → 重复提交/版本更新均不产生第二条差异\n"
          "          → 补件 → 重新核对一致 → 解决；回归冲突复用原行。\n"
          f"演示数据库：{db_path}{SEP}")


if __name__ == "__main__":
    main()
