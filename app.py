"""Flask 界面：建票、三方资料录入、差异处理（认领/补件/重核/解决）、操作留痕。"""
from __future__ import annotations

import json
import os

from flask import (Flask, Response, flash, g, redirect, render_template, request,
                   url_for)

import core

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-customs-discrepancy")
app.jinja_env.filters["from_json"] = json.loads


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
    from datetime import datetime
    for s in rows:
        open_n = db().execute(
            "SELECT COUNT(*) FROM discrepancy WHERE shipment_id = ? AND status != 'resolved'",
            (s["id"],)).fetchone()[0]
        reconciled_n = db().execute(
            "SELECT COUNT(*) FROM discrepancy WHERE shipment_id = ? "
            "AND status != 'resolved' AND is_reconciled = 1",
            (s["id"],)).fetchone()[0]
        waived_n = db().execute(
            "SELECT COUNT(*) FROM discrepancy WHERE shipment_id = ? "
            "AND status != 'resolved' AND is_reconciled = 0 AND active_waiver_id IS NOT NULL",
            (s["id"],)).fetchone()[0]
        dl = core.parse_dt(s["deadline"])
        overdue = bool((open_n - waived_n) and dl and datetime.now() > dl)
        summary[s["id"]] = {"open": open_n, "reconciled": reconciled_n,
                            "waived": waived_n, "overdue": overdue}
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
            deadline=request.form.get("deadline") or None,
            warn_hours=request.form.get("warn_hours", "24"),
        )
    except Exception as e:
        flash(f"建票失败：{e}", "error")
        return redirect(url_for("index"))
    flash(f"货票已创建（#{sid}），请录入三方资料", "ok")
    return redirect(url_for("detail", shipment_id=sid))


@app.route("/shipments/<int:shipment_id>/deadline", methods=["POST"])
def update_deadline(shipment_id):
    try:
        core.set_deadline(
            db(), shipment_id,
            deadline=request.form.get("deadline") or None,
            warn_hours=request.form.get("warn_hours", "24"),
            actor=request.form.get("actor", ""),
        )
        flash("截止时间已更新", "ok")
    except Exception as e:
        flash(f"截止时间更新失败：{e}", "error")
    return redirect(url_for("detail", shipment_id=shipment_id))


# ---------------------------------------------------------------- 一票货详情

def _field_table(conn, shipment_id):
    """组装三方 × 三字段对照表，并标出每个单元格的冲突/容差命中情况。"""
    docs = {r["source"]: r for r in core.list_documents(conn, shipment_id)}
    discrepancies = {r["field"]: r for r in core.list_discrepancies(conn, shipment_id)}

    table = []
    for field in core.FIELDS:
        present = {src: docs[src][field] for src in core.SOURCES
                   if src in docs and docs[src][field] not in (None, "")}
        norms = {core.NORMALIZERS[field](v) for v in present.values()}
        strict_conflict = len(norms) >= 2
        tolerated = False
        rule_hit = None
        if strict_conflict:
            rule = core.active_rule(conn, shipment_id, field)
            if rule is not None:
                snap_present = {src: {"raw": present[src], "norm": core.NORMALIZERS[field](present[src])}
                                for src in present}
                hit = core.evaluate_tolerance(
                    field, rule["mode"], core.rule_config(rule), snap_present)
                if hit:
                    tolerated = True
                    rule_hit = {"version": rule["version"], "evidence": hit,
                                "mode": rule["mode"],
                                "config": core.rule_config(rule)}
        d = discrepancies.get(field)
        table.append({
            "field": field,
            "label": core.FIELD_LABELS[field],
            "conflict": strict_conflict and not tolerated,
            "strict_conflict": strict_conflict,
            "tolerated": tolerated,
            "rule_hit": rule_hit,
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
    # 页面加载时惰性处理到期豁免（幂等）：到期仍冲突的差异复用原行 episode+1
    core.expire_due_waivers(conn)
    docs, table, discrepancies = _field_table(conn, shipment_id)

    cards = []
    for d in core.list_discrepancies(conn, shipment_id):
        active_waiver = None
        if d["active_waiver_id"]:
            w = conn.execute("SELECT * FROM waiver WHERE id = ?",
                             (d["active_waiver_id"],)).fetchone()
            if w and w["status"] == core.WAIVER_ACTIVE:
                active_waiver = w
        cards.append({
            "row": d,
            "values": json.loads(d["values_json"]),
            "rule_evidence": json.loads(d["rule_evidence_json"] or "{}"),
            "active_waiver": active_waiver,
            "waivers": core.list_waivers(conn, discrepancy_id=d["id"]),
        })
    logs = core.list_logs(conn, shipment_id)
    tokens = core.list_tokens(conn, shipment_id)
    packages = core.list_packages(conn, shipment_id)
    rules = core.list_rules(conn, shipment_id)
    rule_current = {}
    rule_history = {}
    for r in rules:
        if r["status"] == core.RULE_ACTIVE:
            rule_current[r["field"]] = r
        rule_history.setdefault(r["field"], []).append(r)
    reminder_rows = list(conn.execute(
        """SELECT e.*, n.target, n.status AS nstatus, n.attempts
           FROM reminder_event e JOIN notification n ON n.event_id = e.id
           WHERE e.shipment_id = ? ORDER BY e.id DESC, n.id""",
        (shipment_id,)))
    # 截止时间风险状态（与 reminders.py 同一判定口径，豁免期内不报警）
    deadline_risk = _deadline_risk(shipment, cards)
    return render_template(
        "detail.html",
        s=shipment, docs=docs, table=table, cards=cards, logs=logs,
        tokens=tokens, packages=packages, reminders=reminder_rows,
        deadline_risk=deadline_risk,
        rules=rules, rule_current=rule_current, rule_history=rule_history,
        sources=core.SOURCES, source_labels=core.SOURCE_LABELS,
        field_labels=core.FIELD_LABELS,
        status_labels={"open": "待认领", "claimed": "处理中", "resolved": "已解决"},
    )


def _deadline_risk(shipment, cards):
    dl = core.parse_dt(shipment["deadline"])
    if dl is None:
        return None
    unresolved = [c for c in cards if c["row"]["status"] != "resolved"
                  and not c["active_waiver"]]
    if not unresolved:
        return None
    from datetime import datetime, timedelta
    now_dt = datetime.now()
    if now_dt > dl:
        return {"level": "overdue", "text": "已超过报关截止，差异仍未解决（已升级主管）",
                "cls": "b-open"}
    if now_dt >= dl - timedelta(hours=int(shipment["warn_hours"])):
        return {"level": "due_soon",
                "text": f"已进入 {shipment['warn_hours']} 小时提醒窗口，临近报关截止",
                "cls": "b-claimed"}
    return None


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


# ------------------------------------------------------------ 容差规则（版本化）

def _rule_form_to_config(field: str, form) -> tuple[str, dict]:
    """把规则表单解析为 (mode, config)；参数不合法抛 ValueError。"""
    if field == "quantity":
        mode = form.get("qty_mode", "abs")
        config = {"adopt_source": form.get("adopt_source", "supplier")}
        if mode == "pct":
            config["basis"] = form.get("basis", "max")
            config["pct"] = form.get("pct", "").strip()
        else:
            config["abs"] = form.get("abs", "").strip()
        return mode, config
    mode = "alias"
    return mode, {
        "aliases": form.get("aliases", ""),
        "adopt_source": form.get("adopt_source", "supplier"),
    }


@app.route("/shipments/<int:shipment_id>/rules", methods=["POST"])
def save_rule(shipment_id):
    field = request.form.get("field", "")
    try:
        mode, config = _rule_form_to_config(field, request.form)
        version = core.save_rule(
            db(), shipment_id, field=field, mode=mode, config=config,
            note=request.form.get("note", ""), actor=request.form.get("actor", ""))
        flash(f"容差规则【{core.FIELD_LABELS.get(field, field)}】v{version} 已生效"
              "（旧版本保留为证据），并已按新规则重新核对", "ok")
    except Exception as e:
        flash(f"规则保存失败：{e}", "error")
    return redirect(url_for("detail", shipment_id=shipment_id))


@app.route("/shipments/<int:shipment_id>/rules/revoke", methods=["POST"])
def revoke_rule(shipment_id):
    field = request.form.get("field", "")
    try:
        core.revoke_rule(db(), shipment_id, field=field,
                         actor=request.form.get("actor", ""))
        flash(f"容差规则【{core.FIELD_LABELS.get(field, field)}】已停用，该字段恢复严格比对并重核", "ok")
    except Exception as e:
        flash(f"停用失败：{e}", "error")
    return redirect(url_for("detail", shipment_id=shipment_id))


# ------------------------------------------------------------ 豁免（限时放行）

@app.route("/discrepancies/<int:discrepancy_id>/waivers", methods=["POST"])
def grant_waiver(discrepancy_id):
    conn = db()
    row = conn.execute("SELECT shipment_id FROM discrepancy WHERE id = ?",
                       (discrepancy_id,)).fetchone()
    sid = row["shipment_id"] if row else None
    try:
        wid = core.grant_waiver(
            conn, discrepancy_id,
            reason=request.form.get("reason", ""),
            granted_by=request.form.get("granted_by", ""),
            expires_at=request.form.get("expires_at", ""))
        flash(f"豁免 #{wid} 已生效：豁免期内不再催办；到期、撤销或资料升版后若仍冲突，将自动重开提醒", "ok")
    except Exception as e:
        flash(f"豁免申请失败：{e}", "error")
    return redirect(url_for("detail", shipment_id=sid))


@app.route("/waivers/<int:waiver_id>/revoke", methods=["POST"])
def revoke_waiver(waiver_id):
    conn = db()
    try:
        w = core.get_waiver(conn, waiver_id)
        sid = w["shipment_id"]
    except LookupError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))
    try:
        result = core.revoke_waiver(conn, waiver_id, actor=request.form.get("actor", ""))
        if result["bumped"]:
            flash("豁免已撤销：差异仍未一致，已复用原差异行开启新一轮提醒", "ok")
        else:
            flash("豁免已撤销（差异当前已一致，无需重开）", "ok")
    except Exception as e:
        flash(f"撤销失败：{e}", "error")
    return redirect(url_for("detail", shipment_id=sid))


# ------------------------------------------------------------ 供应商补件链接

@app.route("/shipments/<int:shipment_id>/tokens", methods=["POST"])
def create_token(shipment_id):
    fields = request.form.getlist("fields")
    try:
        token = core.create_supplement_token(
            db(), shipment_id, fields=fields,
            contact=request.form.get("contact", ""),
            actor=request.form.get("actor", ""),
        )
        link = url_for("portal", token=token, _external=True)
        flash(f"补件链接已生成：{link}（只含被授权字段，可发给供应商）", "ok")
    except Exception as e:
        flash(f"生成补件链接失败：{e}", "error")
    return redirect(url_for("detail", shipment_id=shipment_id))


@app.route("/tokens/<int:token_id>/revoke", methods=["POST"])
def revoke_token(token_id):
    conn = db()
    row = conn.execute("SELECT shipment_id FROM supplement_token WHERE id = ?",
                       (token_id,)).fetchone()
    sid = row["shipment_id"] if row else None
    try:
        core.revoke_token(conn, token_id, actor=request.form.get("actor", ""))
        flash("补件链接已吊销", "ok")
    except Exception as e:
        flash(f"吊销失败：{e}", "error")
    return redirect(url_for("detail", shipment_id=sid))


# ------------------------------------------------------------ 供应商受限门户

def _portal_page(token, error=None, just_submitted=None):
    conn = db()
    try:
        ctx = core.portal_context(conn, token)
    except core.TokenError as e:
        return render_template("portal_error.html", message=str(e)), 403
    return render_template("portal.html", ctx=ctx, error=error,
                           just_submitted=just_submitted,
                           field_labels=core.FIELD_LABELS)


@app.route("/portal/<token>")
def portal(token):
    return _portal_page(token)


@app.route("/portal/<token>/submit", methods=["POST"])
def portal_submit(token):
    # 只收集三个已知字段；任何额外字段名若混入会在 core 层按越权/非法拒绝
    values = {f: request.form.get(f, "") for f in core.FIELDS if f in request.form}
    try:
        result = core.supplier_submit(db(), token, values, actor=request.form.get("actor", ""))
    except core.TokenError as e:
        return _portal_page(token, error=str(e)), 403
    if result["duplicate"]:
        return _portal_page(token, error="提交内容与现行版本一致，已按重复提交忽略，未新增资料版本。")
    return _portal_page(token, just_submitted=result["changed_fields"])


# ------------------------------------------------------------ 申报包冻结与导出

@app.route("/shipments/<int:shipment_id>/packages", methods=["POST"])
def freeze_package(shipment_id):
    confirm = request.form.get("confirm_open") == "1"
    try:
        result = core.freeze_package(
            db(), shipment_id, actor=request.form.get("actor", ""), confirm=confirm)
        flash(f"申报包 #{result['package_no']} 已冻结（不可变），可导出留档", "ok")
    except ValueError as e:
        flash(f"冻结被阻止：{e}", "error")
    except Exception as e:
        flash(f"冻结失败：{e}", "error")
    return redirect(url_for("detail", shipment_id=shipment_id))


@app.route("/packages/<int:package_id>/export.<fmt>")
def export_package(package_id, fmt):
    try:
        package = core.get_package(db(), package_id)
    except LookupError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))
    shipment_id = package["shipment"]["id"]
    ref = package["shipment"]["ref"]
    no = package["package_no"]
    if fmt == "json":
        body = json.dumps(package, ensure_ascii=False, indent=2)
        return Response(
            body, mimetype="application/json",
            headers={"Content-Disposition":
                     f'attachment; filename="{ref}-pkg{no}.json"'})
    if fmt == "md":
        body = core.export_markdown(package)
        return Response(
            body, content_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="{ref}-pkg{no}.md"'})
    flash("不支持的导出格式", "error")
    return redirect(url_for("detail", shipment_id=shipment_id))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
