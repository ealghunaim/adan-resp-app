"""Respirology clinic referrals: referring physician submits -> 2nd on-call decides in one step
(appointment details, or not accepted) -> sent back by WhatsApp / email / print."""
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from models import db, ClinicReferral, Delivery, URGENCY, REFERRAL_STATUS, R_PENDING, R_APPROVED, R_REJECTED, R_COMPLETED
from core import team_required, role_required, audit, next_ref, get_or_create_patient, wa_link, mailto_link, parse_date

referrals_bp = Blueprint("referrals", __name__, url_prefix="/referrals")
CHANNELS = ("whatsapp", "email", "print")


def _can_see(r):
    return current_user.is_team or r.referring_user_id == current_user.id


def message_text(r):
    lines = ["Respirology clinic referral - Al-Adan Hospital", f"Ref: {r.ref_no}",
             f"Patient: {r.patient.full_name} (Civil ID {r.patient.civil_id})", ""]
    if r.status == R_REJECTED:
        lines += ["The referral could not be accepted.", f"Reason: {r.review_notes or '-'}"]
    else:
        when = r.appointment_date.strftime("%A %d-%b-%Y") if r.appointment_date else "to be confirmed"
        lines += [f"Appointment: {when} {r.appointment_time or ''}".strip(), f"Location: {r.appointment_location or '-'}"]
        if r.appointment_instructions:
            lines += ["Instructions:", r.appointment_instructions]
    return "\n".join(lines)


@referrals_bp.route("")
@login_required
def listing():
    q = ClinicReferral.query
    if not current_user.is_team:
        q = q.filter_by(referring_user_id=current_user.id)
    st = request.args.get("status")
    if st in REFERRAL_STATUS:
        q = q.filter_by(status=st)
    return render_template("referrals.html", rows=q.order_by(ClinicReferral.created_at.desc()).limit(300).all(), st=st)


@referrals_bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        f = request.form
        p = get_or_create_patient(f)
        if not p:
            flash("Civil ID and patient name are required.", "error")
        elif not f.get("reason", "").strip():
            flash("Reason for referral is required.", "error")
        elif not (f.get("contact_phone", "").strip() or current_user.phone):
            flash("A contact phone number is required so the response can reach you.", "error")
        else:
            r = ClinicReferral(ref_no=next_ref("AR", ClinicReferral), patient_id=p.id, referring_user_id=current_user.id,
                               contact_phone=f.get("contact_phone", "").strip() or current_user.phone,
                               contact_email=f.get("contact_email", "").strip() or current_user.email,
                               urgency=f.get("urgency") if f.get("urgency") in URGENCY else "routine",
                               reason=f["reason"].strip()[:300], clinical_notes=f.get("clinical_notes", "").strip())
            db.session.add(r)
            db.session.flush()
            audit("referral_created", f"{r.ref_no} civil {p.civil_id[:4]}***")
            db.session.commit()
            flash(f"Referral {r.ref_no} sent to the Respirology clinic.", "success")
            return redirect(url_for("referrals.view", rid=r.id))
        return render_template("referral_new.html", form=f), 400
    return render_template("referral_new.html", form={"civil_id": request.args.get("civil_id", "")})


@referrals_bp.route("/<int:rid>")
@login_required
def view(rid):
    r = db.session.get(ClinicReferral, rid) or abort(404)
    if not _can_see(r):
        abort(403)
    show = current_user.is_team or r.status == R_COMPLETED
    wa = mailto = ""
    if r.status in (R_APPROVED, R_REJECTED, R_COMPLETED) and current_user.is_team:
        txt = message_text(r)
        wa, mailto = wa_link(r.contact_phone, txt), mailto_link(r.contact_email, f"Respirology clinic referral {r.ref_no}", txt)
    if current_user.is_team:
        audit("referral_viewed", r.ref_no)
        db.session.commit()
    return render_template("referral_view.html", r=r, show=show, wa=wa, mailto=mailto)


@referrals_bp.route("/<int:rid>/decide", methods=["POST"])
@role_required("can_approve")
def decide(rid):
    r = db.session.get(ClinicReferral, rid) or abort(404)
    if r.status not in (R_PENDING, R_APPROVED, R_REJECTED):
        flash("Already sent; it can no longer be changed.", "error")
        return redirect(url_for("referrals.view", rid=r.id))
    f = request.form
    if f.get("decision") == "reject":
        if not f.get("review_notes", "").strip():
            flash("Give a reason for not accepting the referral.", "error")
            return redirect(url_for("referrals.view", rid=r.id))
        r.status, r.review_notes = R_REJECTED, f["review_notes"].strip()
    else:
        d = parse_date(f.get("appointment_date"))
        if not d or not f.get("appointment_location", "").strip():
            flash("Appointment date and location are required.", "error")
            return redirect(url_for("referrals.view", rid=r.id))
        r.status, r.appointment_date = R_APPROVED, d
        r.appointment_time = f.get("appointment_time", "").strip()
        r.appointment_location = f["appointment_location"].strip()
        r.appointment_instructions = f.get("appointment_instructions", "").strip() or None
        r.review_notes = f.get("review_notes", "").strip() or None
    r.reviewed_by_id, r.reviewed_at = current_user.id, datetime.utcnow()
    audit("referral_" + r.status, r.ref_no)
    db.session.commit()
    flash("Decision saved - ready to send.", "success")
    return redirect(url_for("referrals.view", rid=r.id))


@referrals_bp.route("/<int:rid>/sent", methods=["POST"])
@team_required
def sent(rid):
    r = db.session.get(ClinicReferral, rid) or abort(404)
    ch = request.form.get("channel")
    if r.status not in (R_APPROVED, R_REJECTED, R_COMPLETED) or ch not in CHANNELS:
        abort(400)
    db.session.add(Delivery(kind="referral", record_id=r.id, channel=ch, by_user_id=current_user.id))
    r.status = R_COMPLETED
    audit("referral_sent", f"{r.ref_no} via {ch}")
    db.session.commit()
    flash(f"Marked as sent via {ch}.", "success")
    return redirect(url_for("referrals.view", rid=r.id))


@referrals_bp.route("/<int:rid>/print")
@login_required
def print_view(rid):
    r = db.session.get(ClinicReferral, rid) or abort(404)
    if not _can_see(r) or (r.status == R_PENDING):
        abort(404 if r.status == R_PENDING else 403)
    if not current_user.is_team and r.status != R_COMPLETED:
        abort(404)
    audit("referral_printed", r.ref_no)
    db.session.commit()
    return render_template("print_referral.html", r=r)
