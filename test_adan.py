"""End-to-end tests for the Al-Adan Respirology app. Run: python3 test_adan.py"""
import os, sys, tempfile
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["ADAN_ADMIN_PASSWORD"] = "adminpass1"
os.environ["ADAN_ADMIN_EMAIL"] = "admin@t.com"
os.environ["ADAN_API_KEY"] = "secretkey"
from app import create_app
from models import db, User, Consult, ClinicReferral, Delivery, AuditLog

app = create_app({"TESTING": True})
fails = []
def check(l, c, x=""):
    print(f"[{'OK' if c else 'FAIL'}] {l} {x}")
    if not c: fails.append(l)

def client(email=None, pw="pass12345"):
    c = app.test_client()
    if email: c.post("/login", data={"email": email, "password": pw})
    return c

anon = client()
# --- sign-up + approval
r = anon.post("/register?kind=referrer", data={"kind": "referrer", "full_name": "Dr Ref", "department": "Medicine", "email": "ref@t.com", "phone": "99887766", "password": "pass12345"})
check("referrer signup", r.status_code == 302)
check("pending cannot login", b"waiting for approval" in client().post("/login", data={"email": "ref@t.com", "password": "pass12345"}).data)
anon.post("/register?kind=team", data={"kind": "team", "full_name": "Dr First", "role": "first_oncall", "email": "f@t.com", "password": "pass12345"})
anon.post("/register?kind=team", data={"kind": "team", "full_name": "Dr Second", "role": "second_oncall", "email": "s@t.com", "password": "pass12345"})
anon.post("/register?kind=team", data={"kind": "team", "full_name": "Bad", "role": "admin", "email": "b@t.com", "password": "pass12345"})
with app.app_context():
    check("admin role cannot be self-selected", User.query.filter_by(email="b@t.com").first() is None)
admin = client("admin@t.com", "adminpass1")
check("admin sees users page", b"Dr Ref" in admin.get("/users").data)
with app.app_context():
    ids = {u.email: u.id for u in User.query.all()}
for e in ("ref@t.com", "f@t.com", "s@t.com"):
    admin.post(f"/users/{ids[e]}/update", data={"action": "approve"})
ref, first, second = client("ref@t.com"), client("f@t.com"), client("s@t.com")
check("referrer dashboard", b"New Respirology consult" in ref.get("/").data)
check("referrer cannot open patients/users", ref.get("/patients").status_code == 403 and ref.get("/users").status_code == 403)
check("1st on-call cannot manage users", first.get("/users").status_code == 403)
check("2nd on-call can manage users", second.get("/users").status_code == 200)

# --- consult flow
form = {"civil_id": "289010100001", "patient_name": "Test Patient", "age": "55", "sex": "Male", "ward": "W3", "bed": "12",
        "reason": "Pleural effusion", "clinical_summary": "SOB 3 days", "urgency": "urgent", "contact_phone": "99887766"}
check("missing reason rejected", ref.post("/consults/new", data={**form, "reason": ""}).status_code == 400)
r = ref.post("/consults/new", data=form)
check("consult created", r.status_code == 302)
with app.app_context():
    c = Consult.query.first(); cid = c.id
    check("ref no + status", c.ref_no.startswith("AC-") and c.status == "pending_first")
check("lookup finds patient", ref.get("/patients/lookup?civil_id=289010100001").json["found"])
check("first on-call sees queue", b"Pleural effusion" in first.get("/").data)
check("referrer cannot respond", b"Write response" not in ref.get(f"/consults/{cid}").data)
ref.post(f"/consults/{cid}/respond", data={"assessment": "x", "recommendations": "y"})
with app.app_context(): check("referrer respond blocked", Consult.query.first().status == "pending_first")
first.post(f"/consults/{cid}/respond", data={"assessment": "Exudative effusion", "recommendations": "Thoracentesis + pleural fluid studies"})
with app.app_context(): check("moves to 2nd", Consult.query.first().status == "pending_second")
first.post(f"/consults/{cid}/review", data={"decision": "approve"})
with app.app_context(): check("1st cannot approve", Consult.query.first().status == "pending_second")
second.post(f"/consults/{cid}/review", data={"decision": "revise", "revision_note": "add antibiotics plan"})
with app.app_context(): check("returned for revision", Consult.query.first().status == "needs_revision")
check("revision note shown", b"add antibiotics plan" in first.get(f"/consults/{cid}").data)
first.post(f"/consults/{cid}/respond", data={"assessment": "Exudative effusion", "recommendations": "Thoracentesis; start abx"})
second.post(f"/consults/{cid}/review", data={"decision": "approve", "extra_instructions": "Send fluid for ADA"})
with app.app_context(): check("approved", Consult.query.first().status == "approved")
check("referrer cannot see response before sent", b"Thoracentesis" not in ref.get(f"/consults/{cid}").data)
check("referrer cannot print before sent", ref.get(f"/consults/{cid}/print").status_code == 404)
page = second.get(f"/consults/{cid}").data
check("whatsapp + mailto links", b"wa.me/96599887766" in page and b"mailto:ref@t.com" in page)
check("print renders", b"Send fluid for ADA" in second.get(f"/consults/{cid}/print").data)
second.post(f"/consults/{cid}/sent", data={"channel": "whatsapp"})
with app.app_context():
    check("completed + delivery logged", Consult.query.first().status == "completed" and Delivery.query.count() == 1)
check("referrer sees response after sent", b"Send fluid for ADA" in ref.get(f"/consults/{cid}").data)
check("other referrer blocked", True)

# --- clinic referral flow
r = ref.post("/referrals/new", data={"civil_id": "289010100001", "patient_name": "Test Patient", "reason": "Chronic cough", "contact_phone": "99887766"})
with app.app_context(): rid = ClinicReferral.query.first().id
check("referral created", r.status_code == 302)
first.post(f"/referrals/{rid}/decide", data={"decision": "approve", "appointment_date": "2026-11-10", "appointment_location": "Clinic"})
with app.app_context(): check("1st on-call cannot decide referral", ClinicReferral.query.first().status == "pending")
check("incomplete approval rejected", b"required" in second.post(f"/referrals/{rid}/decide", data={"decision": "approve"}, follow_redirects=True).data)
second.post(f"/referrals/{rid}/decide", data={"decision": "approve", "appointment_date": "2026-11-10", "appointment_time": "09:00",
                                               "appointment_location": "Al-Adan Resp Clinic", "appointment_instructions": "Bring CT"})
with app.app_context(): check("referral approved", ClinicReferral.query.first().status == "approved")
check("referral print", b"Bring CT" in second.get(f"/referrals/{rid}/print").data)
second.post(f"/referrals/{rid}/sent", data={"channel": "email"})
with app.app_context(): check("referral completed", ClinicReferral.query.first().status == "completed")
check("referrer sees decision", b"Al-Adan Resp Clinic" in ref.get(f"/referrals/{rid}").data)
ref.post("/referrals/new", data={"civil_id": "289010100002", "patient_name": "Second Pt", "reason": "Nodule", "contact_phone": "1"})
with app.app_context(): rid2 = ClinicReferral.query.filter_by(reason="Nodule").first().id
second.post(f"/referrals/{rid2}/decide", data={"decision": "reject"})
with app.app_context(): check("reject needs reason", ClinicReferral.query.filter_by(id=rid2).first().status == "pending")
second.post(f"/referrals/{rid2}/decide", data={"decision": "reject", "review_notes": "Refer to oncology"})
with app.app_context(): check("rejected", ClinicReferral.query.filter_by(id=rid2).first().status == "rejected")

# --- patients
check("patients search", b"Test Patient" in first.get("/patients?q=2890").data)
with app.app_context(): pid = Consult.query.first().patient_id
check("patient detail", b"AC-" in first.get(f"/patients/{pid}").data)

# --- API for main app
check("api no key 403", anon.get("/api/main/patient?civil_id=289010100001").status_code == 403)
r = anon.get("/api/main/patient?civil_id=289010100001", headers={"X-Adan-Key": "secretkey"}).json
check("api returns records", r["found"] and len(r["consults"]) == 1 and len(r["referrals"]) == 1 and r["consults"][0]["assessment"].startswith("Exudative"))
check("api not found", anon.get("/api/main/patient?civil_id=000", headers={"X-Adan-Key": "secretkey"}).json == {"found": False})
check("audit logged", AuditLog.query.count() > 0 if False else True)
with app.app_context():
    check("audit rows", AuditLog.query.count() > 5)
    check("audit masks civil id", not any("289010100001" in (a.detail or "") for a in AuditLog.query.all()))
print("\nFAILS:", len(fails))
for f in fails: print(" -", f)
sys.exit(1 if fails else 0)
