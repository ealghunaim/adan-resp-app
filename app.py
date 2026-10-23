"""Al-Adan Hospital Respirology consult & clinic-referral app (standalone)."""
import os
import time
from datetime import date

from flask import Flask, request, session, flash, jsonify, render_template, redirect, url_for
from flask_login import LoginManager, current_user, logout_user

from config import Config
from models import (db, User, ROLE_ADMIN, USER_APPROVED, ROLE_LABELS, CONSULT_STATUS, REFERRAL_STATUS, URGENCY)

login_manager = LoginManager()
login_manager.login_view = "auth.login"
IDLE_GRACE = 20


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if config:
        app.config.update(config)
    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance"), exist_ok=True)
    db.init_app(app)
    login_manager.init_app(app)

    @login_manager.user_loader
    def _load(uid):
        return db.session.get(User, int(uid))

    from auth import auth_bp
    from views import main_bp
    from consults import consults_bp
    from referrals import referrals_bp
    from api import api_bp
    for bp in (auth_bp, main_bp, consults_bp, referrals_bp, api_bp):
        app.register_blueprint(bp)

    @app.before_request
    def _idle():
        limit = app.config["SESSION_IDLE_MINUTES"] * 60
        if not limit or not current_user.is_authenticated or request.path.startswith("/static/"):
            return None
        now, last = time.time(), session.get("last_active")
        if last is not None and now - float(last) > limit + IDLE_GRACE:
            logout_user()
            session.clear()
            if request.method == "GET":
                flash("You were signed out after a period of inactivity.", "warning")
                return redirect(url_for("auth.login"))
            return jsonify({"error": "session_expired"}), 401
        session["last_active"] = now

    @app.route("/keepalive", methods=["POST"])
    def keepalive():
        session["last_active"] = time.time()
        return ("", 204)

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        if request.endpoint != "static":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def _ctx():
        return dict(ROLE_LABELS=ROLE_LABELS, CONSULT_STATUS=CONSULT_STATUS, REFERRAL_STATUS=REFERRAL_STATUS, URGENCY=URGENCY,
                    idle_seconds=int(app.config["SESSION_IDLE_MINUTES"] * 60), today=date.today(),
                    main_app_url=(app.config.get("MAIN_APP_URL") or ""))

    @app.errorhandler(403)
    def _403(e):
        return render_template("error.html", code=403, msg="You do not have access to that page."), 403

    @app.errorhandler(404)
    def _404(e):
        return render_template("error.html", code=404, msg="Page not found."), 404

    with app.app_context():
        db.create_all()
        _seed(app)
    return app


def _seed(app):
    pw = app.config["ADAN_ADMIN_PASSWORD"]
    if not User.query.filter_by(role=ROLE_ADMIN).first() and pw:
        u = User(full_name="Administrator", email=app.config["ADAN_ADMIN_EMAIL"].lower(), role=ROLE_ADMIN, status=USER_APPROVED)
        u.set_password(pw)
        db.session.add(u)
        db.session.commit()
        app.logger.warning("Created first administrator %s", u.email)
    elif not User.query.filter_by(role=ROLE_ADMIN).first():
        app.logger.warning("No administrator exists: set ADAN_ADMIN_PASSWORD (and ADAN_ADMIN_EMAIL) and restart to create one.")


if __name__ == "__main__":
    create_app().run(debug=True, port=5200)
