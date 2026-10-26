import time
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, abort, current_app
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import secrets
from flask_login import login_user, logout_user, login_required, current_user

from models import (db, User, ROLE_LABELS, ROLE_REFERRING, ROLE_ADMIN, ROLE_FIRST, ROLE_SECOND, ROLE_THORACIC, ROLE_THORACIC_RESP,
                    SSO_ROLES, USER_PENDING, USER_APPROVED, USER_DISABLED)
from core import safe_next, audit

auth_bp = Blueprint("auth", __name__)
_FAILS = {}


def _throttled(email):
    c, t = _FAILS.get(email, (0, 0))
    if c >= 6 and time.time() - t < 900:
        return True
    if time.time() - t >= 900:
        _FAILS.pop(email, None)
    return False


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        if _throttled(email):
            flash("Too many failed attempts. Try again in 15 minutes.", "error")
            return render_template("login.html"), 429
        u = User.query.filter_by(email=email).first()
        if u and u.check_password(pw):
            if u.status == USER_PENDING:
                flash("Your account is waiting for approval.", "warning")
                return render_template("login.html")
            if u.status == USER_DISABLED:
                flash("This account is disabled.", "error")
                return render_template("login.html")
            _FAILS.pop(email, None)
            login_user(u)
            session["last_active"] = time.time()
            return redirect(safe_next(request.args.get("next"), url_for("main.dashboard")))
        c, t = _FAILS.get(email, (0, time.time()))
        _FAILS[email] = (c + 1, t)
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@auth_bp.route("/logout")
def logout():
    logout_user()
    session.clear()
    return redirect(url_for("auth.login"))


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    """Self sign-up. Every account is pending until approved by the Respirology team."""
    kind = request.values.get("kind", "referrer")
    kind = kind if kind in ("referrer", "team") else "referrer"
    team_roles = [(ROLE_FIRST, ROLE_LABELS[ROLE_FIRST]), (ROLE_SECOND, ROLE_LABELS[ROLE_SECOND])]
    if request.method == "POST":
        f = request.form
        email = f.get("email", "").strip().lower()
        dept = f.get("department", "").strip()
        role = ROLE_REFERRING
        if kind == "team":
            role = f.get("role")
            dept = dept or "Respirology"
        if kind == "team" and role not in dict(team_roles):
            flash("Choose a valid role.", "error")
        elif not (f.get("full_name", "").strip() and email and len(f.get("password", "")) >= 8 and dept):
            flash("Name, department, email and a password of at least 8 characters are required.", "error")
        elif User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "error")
        else:
            u = User(full_name=f["full_name"].strip(), email=email, role=role, department=dept,
                     phone=f.get("phone", "").strip(), status=USER_PENDING)
            u.set_password(f["password"])
            db.session.add(u)
            db.session.commit()
            flash("Account created. It will be approved by the Respirology team, after which you can log in.", "success")
            return redirect(url_for("auth.login"))
    return render_template("register.html", kind=kind, team_roles=team_roles)


@auth_bp.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        if not current_user.check_password(request.form.get("current", "")):
            flash("Current password is wrong.", "error")
        elif len(request.form.get("new", "")) < 8:
            flash("New password must be at least 8 characters.", "error")
        else:
            current_user.set_password(request.form["new"])
            db.session.commit()
            flash("Password changed.", "success")
            return redirect(url_for("main.dashboard"))
    return render_template("password.html")


@auth_bp.route("/users")
@login_required
def users():
    if not current_user.can_manage_users:
        abort(403)
    return render_template("users.html", users=User.query.order_by(User.status.desc(), User.full_name).all())


@auth_bp.route("/users/new", methods=["POST"])
@login_required
def user_new():
    if not current_user.can_manage_users:
        abort(403)
    f = request.form
    email = f.get("email", "").strip().lower()
    if not (f.get("full_name") and email and len(f.get("password", "")) >= 8 and f.get("role") in ROLE_LABELS):
        flash("Name, email, role and a password of 8+ characters are required.", "error")
    elif f.get("role") == ROLE_ADMIN and current_user.role != ROLE_ADMIN:
        flash("Only an administrator can create administrators.", "error")
    elif User.query.filter_by(email=email).first():
        flash("Email already registered.", "error")
    else:
        u = User(full_name=f["full_name"].strip(), email=email, role=f["role"], department=f.get("department", "").strip(), status=USER_APPROVED)
        u.set_password(f["password"])
        db.session.add(u)
        audit("user_created", f"{email} as {f['role']}")
        db.session.commit()
        flash("Account created.", "success")
    return redirect(url_for("auth.users"))


@auth_bp.route("/users/<int:uid>/update", methods=["POST"])
@login_required
def user_update(uid):
    if not current_user.can_manage_users:
        abort(403)
    u = db.session.get(User, uid) or abort(404)
    action = request.form.get("action")
    if u.id == current_user.id and action == "disable":
        flash("You cannot disable your own account.", "error")
    elif u.role == ROLE_ADMIN and current_user.role != ROLE_ADMIN:
        flash("Only an administrator can change an administrator.", "error")
    elif action == "approve" or action == "enable":
        u.status = USER_APPROVED
    elif action == "disable":
        u.status = USER_DISABLED
    elif action == "role" and request.form.get("role") in ROLE_LABELS:
        if request.form["role"] == ROLE_ADMIN and current_user.role != ROLE_ADMIN:
            flash("Only an administrator can grant the administrator role.", "error")
        else:
            u.role = request.form["role"]
    elif action == "reset" and len(request.form.get("password", "")) >= 8:
        u.set_password(request.form["password"])
    audit("user_" + str(action), u.email)
    db.session.commit()
    return redirect(url_for("auth.users"))


def _notify(u, text):
    from models import UserNotice
    db.session.add(UserNotice(user_id=u.id, message=text[:500]))


@auth_bp.route("/users/<int:uid>/edit", methods=["GET", "POST"])
@login_required
def user_edit(uid):
    """Admin edits an account. Passwords are stored only as one-way hashes, so they can never be *viewed*;
    the admin can instead set/generate a new one. Every change is audited and the user gets an on-screen notice."""
    if not current_user.can_manage_users:
        abort(403)
    u = db.session.get(User, uid) or abort(404)
    if u.role == ROLE_ADMIN and current_user.role != ROLE_ADMIN:
        abort(403)
    if request.method == "POST":
        f = request.form
        email = f.get("email", "").strip().lower()
        other = User.query.filter(User.email == email, User.id != u.id).first() if email else None
        if not (f.get("full_name", "").strip() and email) or other:
            flash("Name and a unique email are required.", "error")
            return render_template("user_edit.html", u=u, new_password=None), 400
        changed = []
        for attr, val in (("full_name", f["full_name"].strip()), ("email", email), ("department", f.get("department", "").strip()),
                          ("phone", f.get("phone", "").strip())):
            if (getattr(u, attr) or "") != val:
                changed.append(attr.replace("_", " "))
                setattr(u, attr, val)
        role = f.get("role")
        if role in ROLE_LABELS and role != u.role:
            if role == ROLE_ADMIN and current_user.role != ROLE_ADMIN:
                flash("Only an administrator can grant the administrator role.", "error")
            else:
                u.role = role
                changed.append("role")
        if f.get("status") in (USER_APPROVED, USER_DISABLED, USER_PENDING) and f["status"] != u.status and u.id != current_user.id:
            u.status = f["status"]
            changed.append("status")
        new_pw = None
        if f.get("password_action") == "generate":
            new_pw = secrets.token_urlsafe(9)
        elif f.get("password_action") == "set":
            if len(f.get("password", "")) < 8:
                flash("Password must be 8+ characters.", "error")
                return render_template("user_edit.html", u=u, new_password=None), 400
            new_pw = f["password"]
        if new_pw:
            u.set_password(new_pw)
            changed.append("password (reset)")
        if changed:
            who = f"{current_user.full_name} ({ROLE_LABELS.get(current_user.role, current_user.role)})"
            _notify(u, f"Administrator {who} changed your account: {', '.join(changed)}."
                       + (" Your password was reset - sign in with the new password you were given." if new_pw else ""))
            audit("user_edited", f"{u.email}: {', '.join(changed)}")
        db.session.commit()
        flash("Account updated." if changed else "No changes.", "success")
        if new_pw and f.get("password_action") == "generate":
            return render_template("user_edit.html", u=u, new_password=new_pw)   # shown once, never stored in clear
        return redirect(url_for("auth.users"))
    return render_template("user_edit.html", u=u, new_password=None)


@auth_bp.route("/notices/<int:nid>/seen", methods=["POST"])
@login_required
def notice_seen(nid):
    from models import UserNotice
    from datetime import datetime
    n = db.session.get(UserNotice, nid)
    if n and n.user_id == current_user.id:
        n.seen_at = datetime.utcnow()
        db.session.commit()
    return redirect(request.referrer or url_for("main.dashboard"))


_SSO_MAP = {"referring": ROLE_REFERRING, "thoracic": ROLE_THORACIC, "thoracic_resp": ROLE_THORACIC_RESP, "admin": ROLE_ADMIN}


@auth_bp.route("/sso")
def sso():
    """Single sign-on from the main Thoracic app (signed, 90-second token). Creates the account on first use."""
    secret = current_app.config.get("PORTAL_SSO_SECRET", "")
    if not secret:
        abort(404)
    try:
        d = URLSafeTimedSerializer(secret, salt="portal-sso").loads(request.args.get("token", ""), max_age=90)
    except (BadSignature, SignatureExpired):
        flash("The sign-in link expired. Open this portal again from the main app.", "error")
        return redirect(url_for("auth.login"))
    role, email = _SSO_MAP.get(d.get("role")), (d.get("email") or "").strip().lower()
    if d.get("portal") != "adan" or not role or not email:
        abort(400)
    u = User.query.filter_by(email=email).first()
    if u is None:
        u = User(full_name=d.get("name") or email, email=email, role=role, department=d.get("hospital") or "",
                 phone=d.get("phone") or "", status=USER_APPROVED)
        u.set_password(secrets.token_urlsafe(24))
        db.session.add(u)
    else:
        if u.status == USER_DISABLED:
            flash("This account is disabled.", "error")
            return redirect(url_for("auth.login"))
        if u.role in SSO_ROLES:      # never downgrade a native admin / Respirology on-call account
            u.role = role
        u.status = USER_APPROVED
        u.full_name = d.get("name") or u.full_name
        u.phone = d.get("phone") or u.phone
        if d.get("hospital") and u.role == ROLE_REFERRING:
            u.department = d["hospital"]
    audit("sso_login", f"{email} as {u.role}")
    db.session.commit()
    login_user(u)
    session["last_active"] = time.time()
    return redirect(safe_next(d.get("next"), url_for("main.dashboard")))
