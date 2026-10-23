"""Data model for the Al-Adan Hospital Respirology consult / clinic-referral app."""
from datetime import datetime, date

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

ROLE_ADMIN = "admin"
ROLE_FIRST = "first_oncall"
ROLE_SECOND = "second_oncall"
ROLE_REFERRING = "referring"
ROLE_LABELS = {
    ROLE_ADMIN: "Administrator",
    ROLE_FIRST: "Respirology 1st on-call",
    ROLE_SECOND: "Respirology 2nd on-call (consultant)",
    ROLE_REFERRING: "Referring physician (Al-Adan Hospital)",
}
TEAM_ROLES = [ROLE_ADMIN, ROLE_FIRST, ROLE_SECOND]
USER_PENDING, USER_APPROVED, USER_DISABLED = "pending", "approved", "disabled"


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(300), nullable=False)
    role = db.Column(db.String(40), nullable=False, default=ROLE_REFERRING)
    department = db.Column(db.String(200))
    phone = db.Column(db.String(50))
    status = db.Column(db.String(20), default=USER_PENDING, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    @property
    def is_active(self):
        return self.status == USER_APPROVED

    @property
    def is_team(self):
        return self.role in TEAM_ROLES

    @property
    def can_respond(self):          # 1st step: draft the consult response
        return self.role in (ROLE_FIRST, ROLE_SECOND, ROLE_ADMIN)

    @property
    def can_approve(self):          # 2nd step: approve / add instructions / decide referrals
        return self.role in (ROLE_SECOND, ROLE_ADMIN)

    @property
    def can_manage_users(self):
        return self.role in (ROLE_ADMIN, ROLE_SECOND)


class Patient(db.Model):
    __tablename__ = "patients"
    id = db.Column(db.Integer, primary_key=True)
    civil_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(200), nullable=False)
    age = db.Column(db.String(20))
    sex = db.Column(db.String(20))
    nationality = db.Column(db.String(100))
    phone = db.Column(db.String(50))
    mrn = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ---- consults --------------------------------------------------------------
C_PENDING_FIRST, C_PENDING_SECOND, C_REVISION, C_APPROVED, C_COMPLETED = (
    "pending_first", "pending_second", "needs_revision", "approved", "completed")
CONSULT_STATUS = {
    C_PENDING_FIRST: "Waiting for 1st on-call", C_PENDING_SECOND: "Waiting for 2nd on-call approval",
    C_REVISION: "Returned for revision", C_APPROVED: "Approved - ready to send", C_COMPLETED: "Sent to referring physician",
}
URGENCY = {"routine": "Routine", "urgent": "Urgent", "emergent": "Emergent"}


class Consult(db.Model):
    __tablename__ = "consults"
    id = db.Column(db.Integer, primary_key=True)
    ref_no = db.Column(db.String(30), unique=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False)
    referring_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    referring_department = db.Column(db.String(200))
    consultant_in_charge = db.Column(db.String(200))
    contact_phone = db.Column(db.String(50))
    contact_email = db.Column(db.String(200))
    ward = db.Column(db.String(100))
    bed = db.Column(db.String(50))
    urgency = db.Column(db.String(20), default="routine")
    reason = db.Column(db.String(300), nullable=False)
    clinical_summary = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), default=C_PENDING_FIRST)
    # step 1 (1st on-call)
    assessment = db.Column(db.Text)
    recommendations = db.Column(db.Text)
    first_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    first_at = db.Column(db.DateTime)
    # step 2 (2nd on-call)
    extra_instructions = db.Column(db.Text)
    revision_note = db.Column(db.Text)
    second_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    second_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    patient = db.relationship("Patient")
    referring_user = db.relationship("User", foreign_keys=[referring_user_id])
    first_by = db.relationship("User", foreign_keys=[first_by_id])
    second_by = db.relationship("User", foreign_keys=[second_by_id])
    deliveries = db.relationship("Delivery", primaryjoin="and_(Delivery.kind=='consult', foreign(Delivery.record_id)==Consult.id)",
                                 order_by="Delivery.at", viewonly=True)

    @property
    def status_label(self):
        return CONSULT_STATUS.get(self.status, self.status)


# ---- clinic referrals ------------------------------------------------------
R_PENDING, R_APPROVED, R_REJECTED, R_COMPLETED = "pending", "approved", "rejected", "completed"
REFERRAL_STATUS = {R_PENDING: "Waiting for review", R_APPROVED: "Approved - appointment given",
                   R_REJECTED: "Not accepted", R_COMPLETED: "Sent to referring physician"}


class ClinicReferral(db.Model):
    __tablename__ = "clinic_referrals"
    id = db.Column(db.Integer, primary_key=True)
    ref_no = db.Column(db.String(30), unique=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False)
    referring_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    contact_phone = db.Column(db.String(50))
    contact_email = db.Column(db.String(200))
    urgency = db.Column(db.String(20), default="routine")
    reason = db.Column(db.String(300), nullable=False)
    clinical_notes = db.Column(db.Text)
    status = db.Column(db.String(20), default=R_PENDING)
    appointment_date = db.Column(db.Date)
    appointment_time = db.Column(db.String(20))
    appointment_location = db.Column(db.String(300))
    appointment_instructions = db.Column(db.Text)
    review_notes = db.Column(db.Text)       # rejection reason / comments
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    patient = db.relationship("Patient")
    referring_user = db.relationship("User", foreign_keys=[referring_user_id])
    reviewed_by = db.relationship("User", foreign_keys=[reviewed_by_id])
    deliveries = db.relationship("Delivery", primaryjoin="and_(Delivery.kind=='referral', foreign(Delivery.record_id)==ClinicReferral.id)",
                                 order_by="Delivery.at", viewonly=True)

    @property
    def status_label(self):
        return REFERRAL_STATUS.get(self.status, self.status)


class Delivery(db.Model):
    """How a finished response was sent back (whatsapp / email / print)."""
    __tablename__ = "deliveries"
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(10), nullable=False)       # consult | referral
    record_id = db.Column(db.Integer, nullable=False, index=True)
    channel = db.Column(db.String(20), nullable=False)
    by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    at = db.Column(db.DateTime, default=datetime.utcnow)
    by_user = db.relationship("User")


class AuditLog(db.Model):
    __tablename__ = "audit_log"
    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    action = db.Column(db.String(60), nullable=False)
    detail = db.Column(db.String(500))
    user = db.relationship("User")
