from flask import Blueprint, render_template, request, abort
from flask_login import login_required, current_user

from models import (db, Patient, Consult, ClinicReferral, AuditLog, User, USER_PENDING,
                    C_PENDING_FIRST, C_PENDING_SECOND, C_REVISION, C_APPROVED, R_PENDING, R_APPROVED, R_REJECTED)
from core import team_required

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
    return render_template("patient_detail.html", p=p,
                           consults=Consult.query.filter_by(patient_id=p.id).order_by(Consult.created_at.desc()).all(),
                           referrals=ClinicReferral.query.filter_by(patient_id=p.id).order_by(ClinicReferral.created_at.desc()).all())


@main_bp.route("/patients/lookup")
@login_required
def lookup():
    """JSON: prefill the new consult / referral form from an existing Al-Adan patient record."""
    from flask import jsonify
    p = Patient.query.filter_by(civil_id=request.args.get("civil_id", "").strip()).first()
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
