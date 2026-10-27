"""Respirology consults: referring physician submits -> 1st on-call drafts -> 2nd on-call approves
(or returns / adds instructions) -> sent back by WhatsApp / email / print."""
from datetime import datetime, date, time as dtime

import requests
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app
from flask_login import login_required, current_user

from models import (db, Consult, Delivery, URGENCY, CONSULT_STATUS, C_PENDING_FIRST, C_PENDING_SECOND, C_REVISION,
                    C_APPROVED, C_COMPLETED, ROLE_ADMIN, OUTCOMES, AdanDiagnosis, Patient)
from core import parse_date, team_required, role_required, audit, next_ref, get_or_create_patient, wa_link, mailto_link, portal_alerts, portal_known

consults_bp = Blueprint("consults", __name__, url_prefix="/consults")
CHANNELS = ("whatsapp", "email", "print")


def _can_see(c):
    return current_user.is_team or c.referring_user_id == current_user.id


def message_text(c):
    """Plain-text reply used for WhatsApp / email."""
    parts = [f"Respirology consult response - Al-Adan Hospital", f"Ref: {c.ref_no}",
             f"Patient: {c.patient.full_name} (Civil ID {c.patient.civil_id})", f"Reason: {c.reason}", ""]
    if c.assessment:
        parts += ["ASSESSMENT:", c.assessment, ""]
    if c.recommendations:
        parts += ["RECOMMENDATIONS:", c.recommendations, ""]
    if c.extra_instructions:
        parts += ["ADDITIONAL INSTRUCTIONS (consultant):", c.extra_instructions, ""]
    if c.comanage_kind:
        parts += ["THORACIC SURGERY: " + ("transfer of care" if c.comanage_kind == "transfer" else "co-management") + " requested.", ""]
    by = c.second_name_display
    parts.append(f"Approved by: {by}" if by else "")
    return "\n".join(parts).strip()


@consults_bp.route("")
@login_required
def listing():
    q = Consult.query
    if not current_user.is_team:
        q = q.filter_by(referring_user_id=current_user.id)
    st = request.args.get("status")
    if st in CONSULT_STATUS:
        q = q.filter_by(status=st)
    dx = request.args.get("dx", "")
    if dx == "none":
        q = q.filter(db.or_(Consult.diagnosis.is_(None), Consult.diagnosis == ""))
    elif dx:
        q = q.filter_by(diagnosis=dx)
    if request.args.get("retro") == "1":
        q = q.filter_by(is_retrospective=True)
    return render_template("consults.html", rows=q.order_by(Consult.created_at.desc()).limit(300).all(), st=st, dx=dx,
                           diagnoses=_dx_names())


@consults_bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        f = request.form
        p = get_or_create_patient(f)
        if not p:
            flash("Civil ID and patient name are required.", "error")
        elif not f.get("reason", "").strip() or not f.get("clinical_summary", "").strip():
            flash("Reason for consult and clinical summary are required.", "error")
        elif not (f.get("contact_phone", "").strip() or current_user.phone):
            flash("A contact phone number is required so the response can reach you.", "error")
        else:
            c = Consult(ref_no=next_ref("AC", Consult), patient_id=p.id, referring_user_id=current_user.id,
                        referring_department=f.get("referring_department", "").strip() or current_user.department,
                        consultant_in_charge=f.get("consultant_in_charge", "").strip(),
                        contact_phone=f.get("contact_phone", "").strip() or current_user.phone,
                        contact_email=f.get("contact_email", "").strip() or current_user.email,
                        ward=f.get("ward", "").strip(), bed=f.get("bed", "").strip(),
                        urgency=f.get("urgency") if f.get("urgency") in URGENCY else "routine",
                        reason=f["reason"].strip()[:300], clinical_summary=f["clinical_summary"].strip())
            db.session.add(c)
            db.session.flush()
            audit("consult_created", f"{c.ref_no} civil {p.civil_id[:4]}***")
            db.session.commit()
            flash(f"Consult {c.ref_no} sent to the Respirology team.", "success")
            return redirect(url_for("consults.view", cid=c.id))
        return render_template("consult_new.html", form=f), 400
    return render_template("consult_new.html", form={"civil_id": request.args.get("civil_id", "")})


def _dx_names(extra=None):
    names = [d.name for d in AdanDiagnosis.query.filter_by(active=True).order_by(AdanDiagnosis.name).all()]
    # keep "Other" last
    names = [n for n in names if n != "Other"] + (["Other"] if "Other" in names else [])
    if extra and extra not in names:
        names.append(extra)
    return names


def _apply_retro(c, f, creating):
    """Fill a retrospective consult from the form. Returns an error message or None."""
    d = parse_date(f.get("consult_date"))
    if not d or d > date.today():
        return "Enter the date the consult was requested (today or earlier)."
    reason, summary = f.get("reason", "").strip(), f.get("clinical_summary", "").strip()
    assessment, recs = f.get("assessment", "").strip(), f.get("recommendations", "").strip()
    if not reason:
        return "Reason for consult is required."
    summary = summary or "(not recorded)"
    d1 = parse_date(f.get("first_date")) or d
    d2 = parse_date(f.get("second_date")) or d1
    if d1 < d or d2 < d1 or d2 > date.today():
        return "Response dates must be on/after the consult date, in order, and not in the future."
    c.created_at = datetime.combine(d, dtime(9, 0))
    c.first_at, c.second_at = datetime.combine(d1, dtime(12, 0)), datetime.combine(d2, dtime(15, 0))
    c.referring_name = f.get("referring_name", "").strip() or None
    c.referring_department = f.get("referring_department", "").strip() or None
    c.consultant_in_charge = f.get("consultant_in_charge", "").strip() or None
    c.ward, c.bed = f.get("ward", "").strip(), f.get("bed", "").strip()
    c.urgency = f.get("urgency") if f.get("urgency") in URGENCY else "routine"
    c.reason, c.clinical_summary = reason[:300], summary
    c.assessment, c.recommendations = assessment or None, recs or None
    c.extra_instructions = f.get("extra_instructions", "").strip() or None
    c.first_by_name = f.get("first_by_name", "").strip() or None
    c.second_by_name = f.get("second_by_name", "").strip() or None
    dx = f.get("diagnosis", "").strip()
    if dx == "__other__":
        dx = f.get("diagnosis_other", "").strip()
    c.diagnosis = dx[:200] or None
    c.outcome = f.get("outcome") if f.get("outcome") in OUTCOMES else None
    ck = f.get("comanage_kind")
    c.comanage_kind = ck if ck in ("comanage", "transfer") else None
    if c.comanage_kind:
        c.comanage_at, c.comanage_by = c.second_at, (c.second_by_name or c.first_by_name or "")
        c.comanage_note = "Entered retrospectively - Thoracic Surgery was not notified by this entry."
    else:
        c.comanage_at = c.comanage_by = c.comanage_note = None
    c.status, c.is_retrospective = C_COMPLETED, True
    return None


@consults_bp.route("/retro/new", methods=["GET", "POST"])
@team_required
def retro_new():
    """Enter a past consult by hand. Completed immediately; nothing is sent to anyone (no WhatsApp / email / Thoracic push)."""
    if request.method == "POST":
        f = request.form
        p = get_or_create_patient(f)
        err = "Civil ID (digits) and patient name are required." if not p else None
        c = Consult(patient_id=p.id if p else 0, referring_user_id=current_user.id, entered_by_id=current_user.id,
                    entered_at=datetime.utcnow())
        if not err:
            err = _apply_retro(c, f, True)
        if err:
            db.session.rollback()
            flash(err, "error")
            return render_template("consult_retro.html", form=f, c=None, diagnoses=_dx_names(f.get("diagnosis_other"))), 400
        c.ref_no = next_ref("ACR", Consult)
        c.contact_phone, c.contact_email = current_user.phone, current_user.email
        db.session.add(c)
        db.session.flush()
        audit("consult_retro_created", f"{c.ref_no} civil {p.civil_id[:4]}*** dated {c.created_at.date()}")
        db.session.commit()
        flash(f"Past consult {c.ref_no} recorded (no notifications were sent).", "success")
        return redirect(url_for("consults.view", cid=c.id))
    return render_template("consult_retro.html", form={"civil_id": request.args.get("civil_id", "")}, c=None, diagnoses=_dx_names())


@consults_bp.route("/<int:cid>/retro-edit", methods=["GET", "POST"])
@login_required
def retro_edit(cid):
    c = db.session.get(Consult, cid) or abort(404)
    if current_user.role != ROLE_ADMIN or not c.is_retrospective:
        abort(403)
    if request.method == "POST":
        before = f"{c.reason}|{c.diagnosis}|{c.created_at.date()}"
        err = _apply_retro(c, request.form, False)
        if err:
            db.session.rollback()
            flash(err, "error")
            return render_template("consult_retro.html", form=request.form, c=c, diagnoses=_dx_names(c.diagnosis)), 400
        audit("consult_retro_edited", f"{c.ref_no} was {before}"[:480])
        db.session.commit()
        flash("Saved.", "success")
        return redirect(url_for("consults.view", cid=c.id))
    form = dict(consult_date=c.created_at.date().isoformat(), first_date=c.first_at.date().isoformat() if c.first_at else "",
                second_date=c.second_at.date().isoformat() if c.second_at else "", referring_name=c.referring_name or "",
                referring_department=c.referring_department or "", consultant_in_charge=c.consultant_in_charge or "",
                ward=c.ward or "", bed=c.bed or "", urgency=c.urgency, reason=c.reason, clinical_summary=c.clinical_summary,
                assessment=c.assessment or "", recommendations=c.recommendations or "", extra_instructions=c.extra_instructions or "",
                first_by_name=c.first_by_name or "", second_by_name=c.second_by_name or "", diagnosis=c.diagnosis or "",
                outcome=c.outcome or "", comanage_kind=c.comanage_kind or "", civil_id=c.patient.civil_id,
                patient_name=c.patient.full_name, age=c.patient.age or "", sex=c.patient.sex or "",
                nationality=c.patient.nationality or "", patient_phone=c.patient.phone or "", mrn=c.patient.mrn or "")
    return render_template("consult_retro.html", form=form, c=c, diagnoses=_dx_names(c.diagnosis))


@consults_bp.route("/<int:cid>/diagnosis", methods=["POST"])
@role_required("can_respond")
def set_diagnosis(cid):
    c = db.session.get(Consult, cid) or abort(404)
    dx = request.form.get("diagnosis", "").strip()
    if dx == "__other__":
        dx = request.form.get("diagnosis_other", "").strip()
    if dx and dx not in _dx_names() and request.form.get("diagnosis") != "__other__":
        abort(400)
    c.diagnosis = dx[:200] or None
    audit("consult_diagnosis_set", f"{c.ref_no} -> {c.diagnosis or 'cleared'}")
    db.session.commit()
    flash("Diagnosis saved.", "success")
    return redirect(request.referrer if (request.referrer or "").startswith(request.host_url) else url_for("consults.view", cid=c.id))


@consults_bp.route("/<int:cid>")
@login_required
def view(cid):
    c = db.session.get(Consult, cid) or abort(404)
    if not _can_see(c):
        abort(403)
    if current_user.is_team:
        audit("consult_viewed", c.ref_no)
        db.session.commit()
    show_response = current_user.is_team or c.status == C_COMPLETED
    others = []
    if current_user.is_team:
        others = Consult.query.filter(Consult.patient_id == c.patient_id, Consult.id != c.id).order_by(Consult.created_at.desc()).all()
    wa = mailto = ""
    if c.status in (C_APPROVED, C_COMPLETED) and current_user.is_team:
        txt = message_text(c)
        wa = wa_link(c.contact_phone, txt)
        mailto = mailto_link(c.contact_email, f"Respirology consult response {c.ref_no}", txt)
    alerts = portal_alerts(c.patient.civil_id) if current_user.is_team else []
    known = portal_known(c.patient.civil_id) if current_user.is_team else []
    return render_template("consult_view.html", c=c, show_response=show_response, others=others, wa=wa, mailto=mailto, alerts=alerts, known=known,
                           diagnoses=_dx_names(c.diagnosis) if current_user.is_team else [])


@consults_bp.route("/<int:cid>/respond", methods=["POST"])
@role_required("can_respond")
def respond(cid):
    """Step 1: 1st on-call writes assessment + recommendations and submits to the 2nd on-call."""
    c = db.session.get(Consult, cid) or abort(404)
    if c.status not in (C_PENDING_FIRST, C_REVISION):
        flash("This consult is not waiting for a first response.", "error")
        return redirect(url_for("consults.view", cid=c.id))
    f = request.form
    c.assessment = f.get("assessment", "").strip()
    c.recommendations = f.get("recommendations", "").strip()
    if not (c.assessment and c.recommendations):
        flash("Assessment and recommendations are required.", "error")
        return redirect(url_for("consults.view", cid=c.id))
    c.first_by_id, c.first_at = current_user.id, datetime.utcnow()
    c.revision_note = None
    c.status = C_PENDING_SECOND
    audit("consult_first_response", c.ref_no)
    db.session.commit()
    flash("Response submitted to the 2nd on-call for approval.", "success")
    return redirect(url_for("consults.view", cid=c.id))


@consults_bp.route("/<int:cid>/review", methods=["POST"])
@role_required("can_approve")
def review(cid):
    """Step 2: 2nd on-call approves (optionally adding instructions / editing) or returns for revision."""
    c = db.session.get(Consult, cid) or abort(404)
    if c.status != C_PENDING_SECOND:
        flash("This consult is not waiting for approval.", "error")
        return redirect(url_for("consults.view", cid=c.id))
    f = request.form
    if f.get("decision") == "revise":
        note = f.get("revision_note", "").strip()
        if not note:
            flash("Say what needs to be revised.", "error")
            return redirect(url_for("consults.view", cid=c.id))
        c.revision_note, c.status = note, C_REVISION
        audit("consult_returned", c.ref_no)
    else:
        c.extra_instructions = f.get("extra_instructions", "").strip() or None
        c.second_by_id, c.second_at, c.status = current_user.id, datetime.utcnow(), C_APPROVED
        audit("consult_approved", c.ref_no)
    db.session.commit()
    flash("Consult approved - ready to send." if c.status == C_APPROVED else "Returned to the 1st on-call.", "success")
    return redirect(url_for("consults.view", cid=c.id))


@consults_bp.route("/<int:cid>/sent", methods=["POST"])
@team_required
def sent(cid):
    """Record how the approved response was delivered; marks the consult completed."""
    c = db.session.get(Consult, cid) or abort(404)
    ch = request.form.get("channel")
    if c.status not in (C_APPROVED, C_COMPLETED) or ch not in CHANNELS:
        abort(400)
    db.session.add(Delivery(kind="consult", record_id=c.id, channel=ch, by_user_id=current_user.id))
    c.status = C_COMPLETED
    audit("consult_sent", f"{c.ref_no} via {ch}")
    db.session.commit()
    flash(f"Marked as sent via {ch}.", "success")
    return redirect(url_for("consults.view", cid=c.id))


@consults_bp.route("/<int:cid>/print")
@login_required
def print_view(cid):
    c = db.session.get(Consult, cid) or abort(404)
    if not _can_see(c):
        abort(403)
    if not current_user.is_team and c.status != C_COMPLETED:
        abort(404)
    audit("consult_printed", c.ref_no)
    db.session.commit()
    return render_template("print_consult.html", c=c, draft=c.status not in (C_APPROVED, C_COMPLETED))


@consults_bp.route("/<int:cid>/comanage", methods=["POST"])
@role_required("can_respond")
def comanage(cid):
    """Hot link to Thoracic Surgery: request co-management or transfer of care. Notifies the Thoracic team members
    the admin has chosen in the main app, and records it on the consult."""
    c = db.session.get(Consult, cid) or abort(404)
    kind = request.form.get("kind")
    if kind not in ("comanage", "transfer"):
        flash("Choose co-management or transfer of care.", "error")
        return redirect(url_for("consults.view", cid=c.id))
    note = request.form.get("note", "").strip()
    base, key = current_app.config.get("MAIN_APP_URL"), current_app.config.get("PORTAL_SSO_SECRET")
    if not (base and key):
        flash("The link to the Thoracic Surgery app is not configured (MAIN_APP_URL / PORTAL_SSO_SECRET).", "error")
        return redirect(url_for("consults.view", cid=c.id))
    public = (current_app.config.get("PUBLIC_URL") or request.host_url).rstrip("/")
    payload = {"source_ref": c.ref_no, "source_path": f"/consults/{c.id}", "civil_id": c.patient.civil_id,
               "patient_name": c.patient.full_name, "reason": c.reason, "urgency": c.urgency, "kind": kind, "note": note,
               "requested_by": current_user.full_name, "requested_by_phone": current_user.phone or "", "source_url": public}
    try:
        r = requests.post(base + "/api/portal/comanagement", json=payload, headers={"X-Portal-Key": key}, timeout=8)
        ok = r.status_code == 200 and r.json().get("ok")
    except (requests.RequestException, ValueError):
        ok = False
    if not ok:
        flash("Could not reach the Thoracic Surgery app. Nothing was sent - please try again or phone the on-call team.", "error")
        return redirect(url_for("consults.view", cid=c.id))
    c.comanage_kind, c.comanage_note, c.comanage_by, c.comanage_at = kind, note, current_user.full_name, datetime.utcnow()
    audit("consult_comanage_requested", f"{c.ref_no} {kind}")
    db.session.commit()
    flash("Thoracic Surgery has been notified.", "success")
    return redirect(url_for("consults.view", cid=c.id))
