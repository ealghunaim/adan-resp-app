from flask import Blueprint, render_template, request, abort, url_for
from flask_login import login_required, current_user

from models import (db, Delivery, OUTCOME_LABELS, Patient, Consult, ClinicReferral, AuditLog, User, USER_PENDING,
                    C_PENDING_FIRST, C_PENDING_SECOND, C_REVISION, C_APPROVED, C_COMPLETED, R_PENDING, R_APPROVED, R_REJECTED, R_COMPLETED)
from core import team_required, portal_alerts, portal_known, norm_civil, audit
from datetime import date

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
@login_required
def dashboard():
    if not current_user.is_team:
        return render_template("dashboard_referrer.html",
                               consults=Consult.query.filter_by(referring_user_id=current_user.id).order_by(Consult.created_at.desc()).limit(50).all(),
                               referrals=ClinicReferral.query.filter_by(referring_user_id=current_user.id).order_by(ClinicReferral.created_at.desc()).limit(50).all())
    first = Consult.query.filter(Consult.status.in_([C_PENDING_FIRST, C_REVISION])).order_by(Consult.created_at).all()
    second = Consult.query.filter_by(status=C_PENDING_SECOND).order_by(Consult.created_at).all()
    to_send = Consult.query.filter_by(status=C_APPROVED).order_by(Consult.created_at).all()
    ref_pending = ClinicReferral.query.filter_by(status=R_PENDING).order_by(ClinicReferral.created_at).all()
    ref_send = ClinicReferral.query.filter(ClinicReferral.status.in_([R_APPROVED, R_REJECTED])).order_by(ClinicReferral.created_at).all()
    pending_users = User.query.filter_by(status=USER_PENDING).count() if current_user.can_manage_users else 0
    return render_template("dashboard.html", first=first, second=second, to_send=to_send, ref_pending=ref_pending,
                           ref_send=ref_send, pending_users=pending_users)


@main_bp.route("/patients")
@team_required
def patients():
    q = request.args.get("q", "").strip()
    qs = Patient.query
    if q:
        like = f"%{q}%"
        qs = qs.filter(db.or_(Patient.full_name.ilike(like), Patient.civil_id.like(like), Patient.mrn.ilike(like)))
    return render_template("patients.html", rows=qs.order_by(Patient.created_at.desc()).limit(50).all(), q=q)


@main_bp.route("/patients/<int:pid>")
@team_required
def patient(pid):
    p = db.session.get(Patient, pid) or abort(404)
    return render_template("patient_detail.html", p=p, alerts=portal_alerts(p.civil_id),
                           consults=Consult.query.filter_by(patient_id=p.id).order_by(Consult.created_at.desc()).all(),
                           referrals=ClinicReferral.query.filter_by(patient_id=p.id).order_by(ClinicReferral.created_at.desc()).all())


@main_bp.route("/patients/lookup")
@login_required
def lookup():
    """JSON: prefill the new consult / referral form from an existing Al-Adan patient record."""
    from flask import jsonify
    p = Patient.query.filter_by(civil_id=norm_civil(request.args.get("civil_id", ""))).first()
    if not p:
        return jsonify({"found": False})
    return jsonify({"found": True, "patient_name": p.full_name, "age": p.age or "", "sex": p.sex or "",
                    "nationality": p.nationality or "", "patient_phone": p.phone or "", "mrn": p.mrn or ""})


@main_bp.route("/audit")
@login_required
def audit_log():
    if current_user.role != "admin":
        abort(403)
    return render_template("audit.html", rows=AuditLog.query.order_by(AuditLog.at.desc()).limit(300).all())


def _today_data():
    open_c = Consult.query.filter(Consult.status != C_COMPLETED).all()
    rank = {"emergent": 0, "urgent": 1, "routine": 2}
    open_c.sort(key=lambda c: (rank.get(c.urgency, 3), c.created_at))
    return dict(
        urgent=[c for c in open_c if c.urgency in ("emergent", "urgent")],
        first=[c for c in open_c if c.status in (C_PENDING_FIRST, C_REVISION)],
        second=[c for c in open_c if c.status == C_PENDING_SECOND],
        to_send=[c for c in open_c if c.status == C_APPROVED],
        comanage=[c for c in open_c if c.comanage_kind],
        ref_pending=ClinicReferral.query.filter_by(status=R_PENDING).order_by(ClinicReferral.created_at).all(),
        ref_send=ClinicReferral.query.filter(ClinicReferral.status.in_([R_APPROVED, R_REJECTED])).order_by(ClinicReferral.created_at).all(),
        clinic_today=ClinicReferral.query.filter(ClinicReferral.appointment_date == date.today(),
                                                 ClinicReferral.status.in_([R_APPROVED, R_COMPLETED])).order_by(ClinicReferral.appointment_time).all(),
        open_total=len(open_c))


@main_bp.route("/today")
@team_required
def today():
    return render_template("today.html", **_today_data())


@main_bp.route("/today/print")
@team_required
def today_print():
    from core import audit
    audit("today_board_printed")
    db.session.commit()
    return render_template("today_print.html", **_today_data())


def build_timeline(p, team=True, user_id=None):
    """Chronological events for one patient from Al-Adan's own records (newest first)."""
    ev = []
    def add(dt, kind, title, detail="", url="", retro=False):
        if dt:
            ev.append(dict(dt=dt, kind=kind, title=title, detail=detail, url=url, retro=retro))
    cq = Consult.query.filter_by(patient_id=p.id)
    rq = ClinicReferral.query.filter_by(patient_id=p.id)
    if not team:
        cq, rq = cq.filter_by(referring_user_id=user_id), rq.filter_by(referring_user_id=user_id)
    for c in cq.all():
        u = url_for("consults.view", cid=c.id)
        r = bool(c.is_retrospective)
        add(c.created_at, "consult", f"Consult {c.ref_no} - {c.reason}",
            f"{URGENCY_LABEL.get(c.urgency, c.urgency)} - from {c.referrer_name}" + (f" ({c.referring_department})" if c.referring_department else "")
            + (f" - diagnosis: {c.diagnosis}" if c.diagnosis and team else ""), u, r)
        if team:
            add(c.first_at, "consult", f"{c.ref_no}: 1st on-call response", c.first_name_display, u, r)
            add(c.second_at, "consult", f"{c.ref_no}: approved by 2nd on-call", c.second_name_display, u, r)
            if c.comanage_at:
                add(c.comanage_at, "consult", f"{c.ref_no}: Thoracic Surgery {'transfer of care' if c.comanage_kind == 'transfer' else 'co-management'} requested", c.comanage_by or "", u, r)
            if c.outcome and r:
                add(c.second_at or c.first_at or c.created_at, "consult", f"{c.ref_no}: outcome - {OUTCOME_LABELS.get(c.outcome, c.outcome)}", "", u, r)
        for d in c.deliveries:
            if team or c.status == "completed":
                add(d.at, "consult", f"{c.ref_no}: response sent via {d.channel}", d.by_user.full_name if d.by_user else "", u, r)
    for rf in rq.all():
        u = url_for("referrals.view", rid=rf.id)
        add(rf.created_at, "referral", f"Clinic referral {rf.ref_no} - {rf.reason}", f"{URGENCY_LABEL.get(rf.urgency, rf.urgency)}", u)
        if team and rf.reviewed_at:
            txt = "accepted" if rf.status in ("approved", "completed") else "not accepted"
            add(rf.reviewed_at, "referral", f"{rf.ref_no}: {txt}" + (f" - appointment {rf.appointment_date}" if rf.appointment_date else ""),
                rf.reviewed_by.full_name if rf.reviewed_by else "", u)
        for d in rf.deliveries:
            if team or rf.status == "completed":
                add(d.at, "referral", f"{rf.ref_no}: response sent via {d.channel}", d.by_user.full_name if d.by_user else "", u)
    ev.sort(key=lambda e: e["dt"], reverse=True)
    return ev


URGENCY_LABEL = {"routine": "Routine", "urgent": "Urgent", "emergent": "Emergent"}


def _timeline_response(p):
    if current_user.is_team:
        audit("timeline_viewed", f"civil {p.civil_id[:4]}***")
        db.session.commit()
    ev = build_timeline(p, current_user.is_team, current_user.id)
    return render_template("timeline.html", p=p, events=ev, alerts=portal_alerts(p.civil_id) if current_user.is_team else [],
                           known=portal_known(p.civil_id) if current_user.is_team else [])


@main_bp.route("/patients/<int:pid>/timeline")
@login_required
def timeline(pid):
    p = db.session.get(Patient, pid) or abort(404)
    if not current_user.is_team and not Consult.query.filter_by(patient_id=p.id, referring_user_id=current_user.id).first() \
            and not ClinicReferral.query.filter_by(patient_id=p.id, referring_user_id=current_user.id).first():
        abort(403)
    return _timeline_response(p)


@main_bp.route("/timeline/<civil>")
@login_required
def timeline_by_civil(civil):
    """Deep link used by the main Thoracic app (via single sign-on): /timeline/<Civil ID>."""
    p = Patient.query.filter_by(civil_id=norm_civil(civil)).first()
    if not p:
        return render_template("timeline_none.html", civil=norm_civil(civil)), 404
    return timeline(p.id)
