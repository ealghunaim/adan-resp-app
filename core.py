import re
from datetime import date, datetime
from functools import wraps
from urllib.parse import quote

from flask import abort, flash, redirect, url_for, request
from flask_login import current_user, login_required

from models import db, AuditLog, Patient, Consult, ClinicReferral


def team_required(view):
    @wraps(view)
    @login_required
    def wrapped(*a, **kw):
        if not current_user.is_team:
            abort(403)
        return view(*a, **kw)
    return wrapped


def role_required(attr):
    """attr: 'can_respond' | 'can_approve' | 'can_manage_users'"""
    def deco(view):
        @wraps(view)
        @login_required
        def wrapped(*a, **kw):
            if not getattr(current_user, attr):
                flash("You do not have permission to do that.", "error")
                return redirect(request.referrer or url_for("main.dashboard"))
            return view(*a, **kw)
        return wrapped
    return deco


def audit(action, detail=""):
    db.session.add(AuditLog(user_id=current_user.id if current_user.is_authenticated else None,
                            action=action, detail=(detail or "")[:500]))


def next_ref(prefix, model):
    year = date.today().year
    n = model.query.filter(model.ref_no.like(f"{prefix}-{year}-%")).count() + 1
    while model.query.filter_by(ref_no=f"{prefix}-{year}-{n:04d}").first():
        n += 1
    return f"{prefix}-{year}-{n:04d}"


def get_or_create_patient(f):
    """Find by Civil ID, or create from the submitted form. Existing demographics are refreshed with non-empty values."""
    cid = re.sub(r"\s+", "", f.get("civil_id", ""))
    name = f.get("patient_name", "").strip()
    if not cid or not name:
        return None
    p = Patient.query.filter_by(civil_id=cid).first()
    if not p:
        p = Patient(civil_id=cid, full_name=name)
        db.session.add(p)
    for attr, key in (("full_name", "patient_name"), ("age", "age"), ("sex", "sex"), ("nationality", "nationality"),
                      ("phone", "patient_phone"), ("mrn", "mrn")):
        v = f.get(key, "").strip()
        if v:
            setattr(p, attr, v)
    db.session.flush()
    return p


def parse_date(s):
    try:
        return date.fromisoformat((s or "").strip())
    except ValueError:
        return None


def safe_next(nxt, default):
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else default


def wa_link(phone, text):
    """wa.me link; 8-digit numbers are treated as Kuwait (+965)."""
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("00"):
        d = d[2:]
    if len(d) == 8:
        d = "965" + d
    if not d:
        return ""
    return f"https://wa.me/{d}?text={quote(text)}"


def mailto_link(email, subject, body):
    if not email:
        return ""
    return f"mailto:{email}?subject={quote(subject)}&body={quote(body)}"
