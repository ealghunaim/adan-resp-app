"""Al-Adan Respirology statistics + admin-managed diagnosis list."""
from collections import Counter, defaultdict
from datetime import date, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from models import (db, Consult, ClinicReferral, AdanDiagnosis, CONSULT_STATUS, URGENCY, OUTCOME_LABELS, ROLE_ADMIN)
from core import team_required, parse_date, audit

stats_bp = Blueprint("stats", __name__)


def _rank(counter, top=None):
    rows = counter.most_common(top)
    return rows


def compute_stats(d_from, d_to, retro="all"):
    cq = Consult.query.filter(Consult.created_at >= d_from, Consult.created_at < d_to + timedelta(days=1))
    if retro == "only":
        cq = cq.filter(Consult.is_retrospective.is_(True))
    elif retro == "exclude":
        cq = cq.filter(db.or_(Consult.is_retrospective.is_(False), Consult.is_retrospective.is_(None)))
    consults = cq.all()
    referrals = ClinicReferral.query.filter(ClinicReferral.created_at >= d_from, ClinicReferral.created_at < d_to + timedelta(days=1)).all()
    months = defaultdict(lambda: [0, 0])
    for c in consults:
        months[c.created_at.strftime("%Y-%m")][0] += 1
    for r in referrals:
        months[r.created_at.strftime("%Y-%m")][1] += 1
    dx = Counter((c.diagnosis or "").strip() or "Not set" for c in consults)
    out = Counter(c.outcome_key for c in consults)
    return dict(
        total=len(consults), referrals=len(referrals), retro=sum(1 for c in consults if c.is_retrospective),
        open=sum(1 for c in consults if c.status != "completed"),
        completed=sum(1 for c in consults if c.status == "completed"),
        comanage=sum(1 for c in consults if c.comanage_kind == "comanage"), transfer=sum(1 for c in consults if c.comanage_kind == "transfer"),
        months=sorted(months.items()), diagnosis=_rank(dx), dx_unset=dx.get("Not set", 0),
        urgency=[(URGENCY.get(k, k), v) for k, v in Counter(c.urgency or "routine" for c in consults).most_common()],
        status=[(CONSULT_STATUS.get(k, k), v) for k, v in Counter(c.status for c in consults).most_common()],
        physicians=_rank(Counter(c.referrer_name or "-" for c in consults), 15),
        departments=_rank(Counter((c.referring_department or "").strip() or "Not stated" for c in consults), 15),
        wards=_rank(Counter((c.ward or "").strip() or "Not stated" for c in consults), 15),
        ref_physicians=_rank(Counter(r.referring_user.full_name if r.referring_user else "-" for r in referrals), 15),
        outcomes=[(OUTCOME_LABELS.get(k, k), v) for k, v in out.most_common()],
    )


@stats_bp.route("/statistics")
@team_required
def statistics():
    today = date.today()
    d_to = parse_date(request.args.get("to")) or today
    d_from = parse_date(request.args.get("from")) or date(today.year, 1, 1)
    retro = request.args.get("retro") if request.args.get("retro") in ("only", "exclude") else "all"
    s = compute_stats(d_from, d_to, retro)
    mx = max([a + b for _, (a, b) in s["months"]] or [1])
    return render_template("statistics.html", s=s, d_from=d_from, d_to=d_to, retro=retro, mx=mx)


@stats_bp.route("/diagnoses", methods=["GET", "POST"])
@login_required
def diagnoses():
    if current_user.role != ROLE_ADMIN:
        abort(403)
    if request.method == "POST":
        act = request.form.get("action")
        if act == "add":
            n = request.form.get("name", "").strip()[:200]
            if n and not AdanDiagnosis.query.filter(db.func.lower(AdanDiagnosis.name) == n.lower()).first():
                db.session.add(AdanDiagnosis(name=n))
                audit("diagnosis_added", n)
                db.session.commit()
            else:
                flash("Enter a new diagnosis name.", "error")
        elif act == "toggle":
            d = db.session.get(AdanDiagnosis, int(request.form.get("id", 0) or 0)) or abort(404)
            d.active = not d.active
            audit("diagnosis_toggled", f"{d.name} active={d.active}")
            db.session.commit()
        return redirect(url_for("stats.diagnoses"))
    counts = Counter(c.diagnosis for c in Consult.query.all() if c.diagnosis)
    return render_template("diagnoses.html", rows=AdanDiagnosis.query.order_by(AdanDiagnosis.name).all(), counts=counts)
