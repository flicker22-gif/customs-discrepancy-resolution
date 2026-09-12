"""Flask 界面：建票、三方资料录入、差异处理（认领/补件/重核/解决）、操作留痕。"""
from __future__ import annotations

import json
import os

from flask import Flask, flash, g, redirect, render_template, request, url_for

import core

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-customs-discrepancy")


def db():
    if "db" not in g:
        g.db = core.connect(os.environ.get("CUSTOMS_DB"))
    return g.db


@app.teardown_appcontext
def _close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@app.before_request
def _ensure_db():
    core.init_db(db())


# ---------------------------------------------------------------- 列表 / 建票

@app.route("/")
def index():
    rows = core.list_shipments(db())
    summary = {}
    for s in rows:
        summary[s["id"]] = {
            "open": db().execute(
                "SELECT COUNT(*) FROM discrepancy WHERE shipment_id = ? AND status != 'resolved'",
                (s["id"],)).fetchone()[0],
            "reconciled": db().execute(
                "SELECT COUNT(*) FROM discrepancy WHERE shipment_id = ? "
                "AND status != 'resolved' AND is_reconciled = 1",
                (s["id"],)).fetchone()[0],
        }
    return render_template("index.html", shipments=rows, summary=summary,
                           status_labels={"open": "处理中", "resolved": "已完结"})


@app.route("/shipments/new", methods=["POST"])
def new_shipment():
    try:
        sid = core.create_shipment(
            db(),
            ref=request.form.get("ref", ""),
            customer=request.form.get("customer", ""),
            description=request.form.get("description", ""),
            actor=request.form.get("actor", ""),
        )
    except Exception as e:
        flash(f"建票失败：{e}", "error")
        return redirect(url_for("index"))
    flash(f"货票已创建（#{sid}），请录入三方资料", "ok")
    return redirect(url_for("detail", shipment_id=sid))


# ---------------------------------------------------------------- 一票货详情

def _field_table(conn, shipment_id):
    """组装三方 × 三字段对照表，并标出每个单元格的冲突情况。"""
    docs = {r["source"]: r for r in core.list_documents(conn, shipment_id)}
    discrepancies = {r["field"]: r for r in core.list_discrepancies(conn, shipment_id)}

    table = []
    for field in core.FIELDS:
        present = {src: docs[src][field] for src in core.SOURCES
                   if src in docs and docs[src][field] not in (None, "")}
        norms = {core.NORMALIZERS[field](v) for v in present.values()}
        conflict = len(norms) >= 2
        d = discrepancies.get(field)
        table.append({
            "field": field,
            "label": core.FIELD_LABELS[field],
            "conflict": conflict,
            "discrepancy": d,
            "cells": [{
                "source": src,
                "label": core.SOURCE_LABELS[src],
                "doc": docs.get(src),
                "value": present.get(src),
                "missing": src not in present,
            } for src in core.SOURCES],
        })
    return docs, table, discrepancies


@app.route("/shipments/<int:shipment_id>")
def detail(shipment_id):
    conn = db()
    shipment = core.get_shipment(conn, shipment_id)
    docs, table, discrepancies = _field_table(conn, shipment_id)

    cards = []
    for d in core.list_discrepancies(conn, shipment_id):
        cards.append({
            "row": d,
            "values": json.loads(d["values_json"]),
        })
    logs = core.list_logs(conn, shipment_id)
    return render_template(
        "detail.html",
        s=shipment, docs=docs, table=table, cards=cards, logs=logs,
        sources=core.SOURCES, source_labels=core.SOURCE_LABELS,
        field_labels=core.FIELD_LABELS,
        status_labels={"open": "待认领", "claimed": "处理中", "resolved": "已解决"},
    )


# ---------------------------------------------------------------- 资料录入

@app.route("/shipments/<int:shipment_id>/documents", methods=["POST"])
def submit_doc(shipment_id):
    try:
        result = core.submit_document(
            db(), shipment_id,
            source=request.form["source"],
            product_name=request.form.get("product_name", ""),
            quantity=request.form.get("quantity", ""),
            carton_no=request.form.get("carton_no", ""),
            actor=request.form.get("actor", ""),
        )
    except Exception as e:
        flash(f"资料提交失败：{e}", "error")
        return redirect(url_for("detail", shipment_id=shipment_id))

    src_label = core.SOURCE_LABELS[request.form["source"]]
    if result["duplicate"]:
        flash(f"{src_label}资料与现行版本完全一致，按重复提交忽略（不新增差异）", "ok")
    else:
        flash(f"{src_label}资料已保存为 v{result['version']}，并完成自动比对", "ok")
    return redirect(url_for("detail", shipment_id=shipment_id))


@app.route("/shipments/<int:shipment_id>/recompute", methods=["POST"])
def recompute(shipment_id):
    stats = core.recompute(db(), shipment_id, actor=request.form.get("actor", "系统"))
    msg = (f"重新核对完成：新发现 {stats['opened']} 处，更新 {stats['updated']} 处，"
           f"核对一致 {stats['reconciled']} 处，重新打开 {stats['reopened']} 处")
    flash(msg, "ok")
    return redirect(url_for("detail", shipment_id=shipment_id))


# ---------------------------------------------------------------- 差异处理

@app.route("/discrepancies/<int:discrepancy_id>/claim", methods=["POST"])
def claim(discrepancy_id):
    conn = db()
    row = conn.execute("SELECT shipment_id FROM discrepancy WHERE id = ?",
                       (discrepancy_id,)).fetchone()
    sid = row["shipment_id"] if row else None
    try:
        core.claim_discrepancy(conn, discrepancy_id, owner=request.form.get("owner", ""))
        flash("已认领", "ok")
    except Exception as e:
        flash(f"认领失败：{e}", "error")
    return redirect(url_for("detail", shipment_id=sid))


@app.route("/discrepancies/<int:discrepancy_id>/supplement", methods=["POST"])
def supplement(discrepancy_id):
    conn = db()
    row = conn.execute("SELECT shipment_id FROM discrepancy WHERE id = ?",
                       (discrepancy_id,)).fetchone()
    sid = row["shipment_id"] if row else None
    try:
        core.add_supplement(conn, discrepancy_id,
                            note=request.form.get("note", ""),
                            actor=request.form.get("actor", ""))
        flash("补件记录已添加（留痕）；请更新对应资料后点“重新核对”", "ok")
    except Exception as e:
        flash(f"补件失败：{e}", "error")
    return redirect(url_for("detail", shipment_id=sid))


@app.route("/discrepancies/<int:discrepancy_id>/resolve", methods=["POST"])
def resolve(discrepancy_id):
    conn = db()
    row = conn.execute("SELECT shipment_id FROM discrepancy WHERE id = ?",
                       (discrepancy_id,)).fetchone()
    sid = row["shipment_id"] if row else None
    try:
        core.resolve_discrepancy(conn, discrepancy_id, actor=request.form.get("actor", ""))
        flash("差异已标记为解决", "ok")
    except Exception as e:
        flash(f"解决失败：{e}", "error")
    return redirect(url_for("detail", shipment_id=sid))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
