"""Read-only API for the main Thoracic app: everything Al-Adan Respirology holds for a Civil ID.

Protected by a shared secret (env ADAN_API_KEY, sent as X-Adan-Key). If ADAN_API_KEY is unset the
endpoint is disabled (404). Every call is audit-logged (Civil ID masked)."""
import hmac

from flask import Blueprint, jsonify, request, abort, current_app

from models import db, Patient, Consult, ClinicReferral, AuditLog

api_bp = Blueprint("api", __name__)


@api_bp.route("/api/main/patient")
def patient_records():
    key = current_app.config.get("ADAN_API_KEY", "")
    if not key:
        abort(404)
    if not hmac.compare_digest(request.headers.get("X-Adan-Key", "").encode(), key.encode()):
        abort(403)
    cid = (request.args.get("civil_id") or "").strip()
    p = Patient.query.filter_by(civil_id=cid).first() if cid else None
    try:
        db.session.add(AuditLog(user_id=None, action="main_app_lookup", detail=f"civil {cid[:4]}*** found={bool(p)}"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    if not p:
        return jsonify({"found": False})
    base = (current_app.config.get("PUBLIC_URL") or request.host_url).rstrip("/")
    consults = Consult.query.filter_by(patient_id=p.id).order_by(Consult.created_at.desc()).all()
    referrals = ClinicReferral.query.filter_by(patient_id=p.id).order_by(ClinicReferral.created_at.desc()).all()
    return jsonify({
        "found": True,
        "patient": {"full_name": p.full_name, "age": p.age or "", "sex": p.sex or "", "nationality": p.nationality or "",
                    "phone": p.phone or "", "mrn": p.mrn or ""},
        "consults": [{"ref_no": c.ref_no, "date": c.created_at.strftime("%Y-%m-%d"), "urgency": c.urgency, "status": c.status_label,
                      "reason": c.reason, "clinical_summary": c.clinical_summary, "ward": c.ward or "", "bed": c.bed or "",
                      "referring": c.referring_user.full_name if c.referring_user else "", "department": c.referring_department or "",
                      "assessment": c.assessment or "", "recommendations": c.recommendations or "",
                      "extra_instructions": c.extra_instructions or "",
                      "url": f"{base}/consults/{c.id}"} for c in consults],
        "referrals": [{"ref_no": r.ref_no, "date": r.created_at.strftime("%Y-%m-%d"), "urgency": r.urgency, "status": r.status_label,
                       "reason": r.reason, "clinical_notes": r.clinical_notes or "",
                       "appointment": (r.appointment_date.isoformat() + " " + (r.appointment_time or "")).strip() if r.appointment_date else "",
                       "location": r.appointment_location or "", "url": f"{base}/referrals/{r.id}"} for r in referrals],
    })
