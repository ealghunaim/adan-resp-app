"""Respirology consults: referring physician submits -> 1st on-call drafts -> 2nd on-call approves
(or returns / adds instructions) -> sent back by WhatsApp / email / print."""
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from models import (db, Consult, Delivery, URGENCY, CONSULT_STATUS, C_PENDING_FIRST, C_PENDING_SECOND, C_REVISION,
                    C_APPROVED, C_COMPLETED, ROLE_ADMIN)
from core import team_required, role_required, audit, next_ref, get_or_create_patient, wa_link, mailto_link

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
    by = c.second_by.full_name if c.second_by else ""
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
    return render_template("consults.html", rows=q.order_by(Consult.created_at.desc()).limit(300).all(), st=st)


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
    return render_template("consult_view.html", c=c, show_response=show_response, others=others, wa=wa, mailto=mailto)


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
