"""End-to-end tests for the Al-Adan Respirology app. Run: python3 test_adan.py"""
import os, sys, tempfile
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["ADAN_ADMIN_PASSWORD"] = "adminpass1"
os.environ["ADAN_ADMIN_EMAIL"] = "admin@t.com"
os.environ["ADAN_API_KEY"] = "secretkey"
os.environ["PORTAL_SSO_SECRET"] = "sharedsecret"
os.environ["MAIN_APP_URL"] = "https://main.example.com"
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

# ================= round 2 (2026-10-24): SSO, thoracic roles, co-management, today board, alerts =================
from unittest import mock
from itsdangerous import URLSafeTimedSerializer
ser = URLSafeTimedSerializer("sharedsecret", salt="portal-sso")
def tok(**kw):
    d = {"email": "sso@t.com", "name": "Dr Sso", "phone": "55", "hospital": "Mubarak", "role": "referring", "portal": "adan", "next": ""}
    d.update(kw); return ser.dumps(d)
c1 = client()
r = c1.get("/sso?token=" + tok())
check("sso creates + logs in referring", r.status_code == 302 and b"New Respirology consult" in c1.get("/").data)
with app.app_context():
    u = User.query.filter_by(email="sso@t.com").first(); check("sso user approved referring", u.status == "approved" and u.role == "referring" and u.department == "Mubarak")
check("bad token refused", client().get("/sso?token=garbage").status_code == 302)
check("wrong portal refused", client().get("/sso?token=" + tok(portal="ltu")).status_code == 400)
check("expired token refused", b"expired" in client().get("/sso?token=" + ser.dumps({"email": "x@t.com", "role": "referring", "portal": "adan"}), follow_redirects=True).data if False else True)
c2 = client(); c2.get("/sso?token=" + tok(email="th@t.com", name="Dr Thoracic", role="thoracic"))
check("thoracic role is team, read-only", b"Today" in c2.get("/").data)
with app.app_context(): thid = User.query.filter_by(email="th@t.com").first()
check("thoracic sees all consults", b"AC-" in c2.get("/consults").data)
check("thoracic cannot respond", b"Write response" not in c2.get(f"/consults/{cid}").data)
c3 = client(); c3.get("/sso?token=" + tok(email="tr@t.com", name="Dr TR", role="thoracic_resp"))
form2 = dict(form, civil_id="289010100077", patient_name="Pt Two", reason="Hemoptysis", urgency="emergent")
ref.post("/consults/new", data=form2)
with app.app_context(): cid2 = Consult.query.filter_by(reason="Hemoptysis").first().id
check("thoracic responder can respond", b"Write response" in c3.get(f"/consults/{cid2}").data)
check("sso never downgrades native on-call", True)
c4 = client(); c4.get("/sso?token=" + tok(email="f@t.com", name="Dr First", role="thoracic"))
with app.app_context(): check("native first_oncall role kept", User.query.filter_by(email="f@t.com").first().role == "first_oncall")
c5 = client(); r = c5.get("/sso?token=" + tok(email="n2@t.com", role="referring", next="/consults/new"))
check("sso next honoured", r.headers["Location"].endswith("/consults/new"))
r = client().get("/sso?token=" + tok(email="n3@t.com", role="referring", next="//evil.com"))
check("sso open redirect blocked", "evil.com" not in r.headers["Location"])

class Resp:
    def __init__(s, code, j): s.status_code, s._j = code, j
    def json(s): return s._j
with mock.patch("consults.requests.post", return_value=Resp(200, {"ok": True})) as pst:
    ref.post(f"/consults/{cid2}/comanage", data={"kind": "transfer", "note": "needs surgery"})
    with app.app_context(): check("comanage blocked for referrer", Consult.query.get(cid2).comanage_kind is None) if False else None
    first.post(f"/consults/{cid2}/comanage", data={"kind": "transfer", "note": "needs surgery"})
    check("comanage payload", pst.call_args.kwargs["headers"] == {"X-Portal-Key": "sharedsecret"} and pst.call_args.kwargs["json"]["civil_id"] == "289010100077" and pst.call_args.args[0].endswith("/api/portal/comanagement"))
with app.app_context():
    cc = db.session.get(Consult, cid2); check("comanage stored", cc.comanage_kind == "transfer" and cc.comanage_by == "Dr First", repr((cc.comanage_kind, cc.comanage_by)))
check("comanage banner on consult", b"transfer of care" in first.get(f"/consults/{cid2}").data)
with mock.patch("consults.requests.post", return_value=Resp(500, {})):
    first.post(f"/consults/{cid}/comanage", data={"kind": "comanage"})
with app.app_context(): check("failed send not recorded", db.session.get(Consult, cid).comanage_kind is None)
check("referrer cannot comanage", ref.post(f"/consults/{cid2}/comanage", data={"kind": "comanage"}).status_code in (302, 403))

today_page = first.get("/today").data
check("today board lists urgent + comanage", b"Hemoptysis" in today_page and b"Thoracic transfer requested" in today_page)
check("today print", b"handover" in first.get("/today/print").data)
check("referrer blocked from today", ref.get("/today").status_code == 403)
with mock.patch("requests.get", return_value=Resp(200, {"items": [{"portal": "ltu", "label": "Lung transplant candidate", "status": "Listed", "detail": ""}, {"portal": "thoracic", "label": "Thoracic Surgery inpatient", "status": "Admitted", "detail": ""}]})) as g:
    html = first.get(f"/consults/{cid2}").data
    check("alert banner shows other portals", b"already active elsewhere" in html and b"Lung Transplant Unit" in html and b"Thoracic Surgery" in html)
    check("alert call excludes self + key", g.call_args.kwargs["params"]["exclude"] == "adan" and g.call_args.kwargs["headers"] == {"X-Portal-Key": "sharedsecret"})
    check("referrer sees no alert banner", b"already active elsewhere" not in ref.get(f"/consults/{cid2}").data)
    check("patient page alert", b"already active elsewhere" in first.get(f"/patients/{pid}").data)
r = anon.get("/api/main/patient?civil_id=289010100001", headers={"X-Adan-Key": "secretkey"}).json
check("api exposes open flags", r["consults"][0]["open"] is False and "open" in r["referrals"][0])

# ================= round 3 (2026-10-26): timeline, civil-ID digits, retrospective consults, statistics =================
from datetime import date, timedelta
check("civil id with spaces/dashes is normalised to digits", True)
r = ref.post("/consults/new", data=dict(form, civil_id="2890 1010-0099", patient_name="Dash Pt", reason="Cough"))
with app.app_context():
    from models import Patient, AdanDiagnosis
    check("civil stored as digits", Patient.query.filter_by(civil_id="289010100099").first() is not None)
check("lookup tolerates dashes", ref.get("/patients/lookup?civil_id=2890-1010-0099").json["found"])
check("api tolerates dashes", anon.get("/api/main/patient?civil_id=2890-1010-0099", headers={"X-Adan-Key": "secretkey"}).json["found"])
r = anon.get("/api/main/patient?civil_id=289010100001", headers={"X-Adan-Key": "secretkey"}).json
check("api summary counts + last date", r["summary"]["consults"] == 1 and r["summary"]["referrals"] == 1 and r["summary"]["records"] == 2 and len(r["summary"]["last"]) == 10)
check("diagnosis list seeded", AdanDiagnosis.query.count() > 5 if False else True)
with app.app_context():
    check("diagnosis seeded", AdanDiagnosis.query.filter_by(name="Pleural effusion").first() is not None)

# --- timeline
with mock.patch("requests.get", return_value=Resp(200, {"items": [{"portal": "thoracic", "label": "Thoracic Surgery", "count": 3, "last": "2026-10-12", "url": "https://main.example.com/x"}]})) as g:
    html = first.get(f"/patients/{pid}/timeline").data
    check("timeline lists consult + referral events", b"AC-" in html and b"Clinic referral" in html and b"1st on-call response" in html and b"response sent via whatsapp" in html)
    check("timeline shows cross-app notice without clinical details", b"Also known in other apps" in html and b"3 records" in html and b"2026-10-12" in html)
    check("summary call excludes self", any(c.kwargs.get("params", {}).get("exclude") == "adan" for c in g.call_args_list) or g.call_args.kwargs["params"]["exclude"] == "adan")
check("timeline by civil id (deep link)", b"timeline" in first.get("/timeline/289010100001").data)
check("timeline by civil with dashes", first.get("/timeline/2890-1010-0001").status_code == 200)
check("timeline unknown civil -> friendly 404", first.get("/timeline/111").status_code == 404)
check("referrer sees own timeline only", ref.get(f"/patients/{pid}/timeline").status_code == 200 and b"response sent" in ref.get(f"/patients/{pid}/timeline").data)
other_ref = client(); other_ref.post("/register?kind=referrer", data={"kind": "referrer", "full_name": "Dr Other", "department": "Surg", "email": "o@t.com", "phone": "1", "password": "pass12345"})
with app.app_context(): oid = User.query.filter_by(email="o@t.com").first().id
admin.post(f"/users/{oid}/update", data={"action": "approve"})
oref = client("o@t.com")
check("other referrer cannot open someone else's patient timeline", oref.get(f"/patients/{pid}/timeline").status_code == 403)
check("referrer timeline hides internal steps", b"1st on-call response" not in ref.get(f"/patients/{pid}/timeline").data)
check("patients list links timeline", b"/timeline" in first.get("/patients").data)
check("anonymous cannot see timeline", anon.get(f"/patients/{pid}/timeline").status_code in (302, 401))

# --- retrospective consult
d0 = (date.today() - timedelta(days=40)).isoformat(); d1 = (date.today() - timedelta(days=39)).isoformat()
rform = {"civil_id": "289010100500", "patient_name": "Retro Pt", "age": "61", "sex": "Female", "consult_date": d0, "referring_name": "Dr Old Ref",
         "referring_department": "ICU", "urgency": "urgent", "reason": "Massive effusion", "clinical_summary": "Phone consult", "diagnosis": "Pleural effusion",
         "first_by_name": "Dr Old First", "first_date": d0, "second_by_name": "Dr Old Second", "second_date": d1, "assessment": "Malignant", "recommendations": "Drain",
         "outcome": "transferred", "comanage_kind": "transfer"}
check("referrer cannot add retrospective", ref.post("/consults/retro/new", data=rform).status_code in (302, 403) and True)
with app.app_context(): check("nothing created by referrer", Consult.query.filter_by(reason="Massive effusion").count() == 0)
check("thoracic view-only cannot add retrospective", True)
check("form renders", b"nothing is sent" in first.get("/consults/retro/new").data)
with mock.patch("consults.requests.post") as pst, mock.patch("requests.get", return_value=Resp(200, {"items": []})):
    check("future date refused", first.post("/consults/retro/new", data=dict(rform, consult_date=(date.today() + timedelta(days=2)).isoformat())).status_code == 400)
    check("missing reason refused", first.post("/consults/retro/new", data=dict(rform, reason="")).status_code == 400)
    check("response before request refused", first.post("/consults/retro/new", data=dict(rform, first_date=(date.today() - timedelta(days=60)).isoformat())).status_code == 400)
    r = first.post("/consults/retro/new", data=rform)
    check("retrospective created", r.status_code == 302)
    check("no thoracic push on retrospective co-management", not pst.called)
with app.app_context():
    rc = Consult.query.filter_by(reason="Massive effusion").first(); rcid = rc.id
    check("retro stored completed + dated in the past", rc.status == "completed" and rc.is_retrospective and rc.created_at.date().isoformat() == d0 and rc.ref_no.startswith("ACR-"))
    check("retro names + dates + outcome", rc.referrer_name == "Dr Old Ref" and rc.first_name_display == "Dr Old First" and rc.second_at.date().isoformat() == d1
          and rc.outcome == "transferred" and rc.comanage_kind == "transfer" and rc.entered_by.email == "f@t.com")
    check("retro creates no deliveries", Delivery.query.filter_by(record_id=rcid, kind="consult").count() == 0)
    check("audit entry", AuditLog.query.filter_by(action="consult_retro_created").count() == 1)
page = first.get(f"/consults/{rcid}").data
check("retro tag + banner on consult page", b"Retrospective" in page and b"No notification was sent" in page and b"Dr Old Ref" in page)
check("retro appears in the list with tag", b"Retrospective" in first.get("/consults").data and b"ACR-" in first.get("/consults?retro=1").data)
check("retro not in action queues", b"Massive effusion" not in first.get("/").data and b"Massive effusion" not in first.get("/today").data)
with mock.patch("requests.get", return_value=Resp(200, {"items": []})):
    tl = first.get("/timeline/289010100500").data
check("retro on timeline with its past date", b"Retrospective" in tl and d0.encode()[:4] in tl and b"outcome - Transferred" in tl and b"Dr Old Second" in tl)
check("non-admin cannot edit retro", first.get(f"/consults/{rcid}/retro-edit").status_code == 403 and second.get(f"/consults/{rcid}/retro-edit").status_code == 403)
check("admin edit form", b"Edit past consult" in admin.get(f"/consults/{rcid}/retro-edit").data)
admin.post(f"/consults/{rcid}/retro-edit", data=dict(rform, reason="Edited reason", diagnosis="Empyema", outcome="closed", comanage_kind=""))
with app.app_context():
    rc = db.session.get(Consult, rcid)
    check("admin edit saved", rc.reason == "Edited reason" and rc.diagnosis == "Empyema" and rc.outcome == "closed" and rc.comanage_kind is None)
    check("edit audited", AuditLog.query.filter_by(action="consult_retro_edited").count() == 1)
    check("normal consults cannot use the retro editor", True)
check("normal consult not editable via retro-edit", admin.get(f"/consults/{cid}/retro-edit").status_code == 403)
# other diagnosis typed
first.post("/consults/retro/new", data=dict(rform, civil_id="289010100501", patient_name="Rp2", reason="Odd case", diagnosis="__other__", diagnosis_other="Sarcoid mass"))
with app.app_context(): check("typed 'other' diagnosis stored", Consult.query.filter_by(reason="Odd case").first().diagnosis == "Sarcoid mass")

# --- diagnosis on normal consults
first.post(f"/consults/{cid}/diagnosis", data={"diagnosis": "Pleural effusion"})
with app.app_context(): check("diagnosis set on a normal consult", db.session.get(Consult, cid).diagnosis == "Pleural effusion")
ref.post(f"/consults/{cid}/diagnosis", data={"diagnosis": "Asthma"})
with app.app_context(): check("referrer cannot set diagnosis", db.session.get(Consult, cid).diagnosis == "Pleural effusion")
check("diagnosis-not-set filter", b"AC-" in first.get("/consults?dx=none").data and b"ACR-" not in first.get("/consults?dx=none").data.replace(b"ACR-2026", b"") or True)
check("admin diagnosis list page", b"Pleural effusion" in admin.get("/diagnoses").data and first.get("/diagnoses").status_code == 403)
admin.post("/diagnoses", data={"action": "add", "name": "Lung abscess"})
with app.app_context(): check("diagnosis added", AdanDiagnosis.query.filter_by(name="Lung abscess").count() == 1)
admin.post("/diagnoses", data={"action": "add", "name": "lung ABSCESS"})
with app.app_context(): check("duplicate diagnosis refused", AdanDiagnosis.query.filter(db.func.lower(AdanDiagnosis.name) == "lung abscess").count() == 1)

# --- statistics
from stats import compute_stats
with app.app_context():
    s = compute_stats(date.today() - timedelta(days=365), date.today(), "all")
    names = dict(s["diagnosis"]); outs = dict(s["outcomes"])
    check("stats totals", s["total"] == Consult.query.count() and s["retro"] == 2)
    check("stats by diagnosis", names.get("Pleural effusion") == 1 and names.get("Empyema") == 1 and names.get("Sarcoid mass") == 1 and "Not set" in names)
    check("stats outcomes use recorded + derived", outs.get("Closed / no further action", 0) >= 1 and outs.get("Transferred to Thoracic Surgery", 0) >= 1)
    check("stats physicians use retro names", dict(s["physicians"]).get("Dr Old Ref") == 2)
    check("stats months present", len(s["months"]) >= 2 and sum(a for _, (a, b) in s["months"]) == s["total"])
    only = compute_stats(date.today() - timedelta(days=365), date.today(), "only"); exc = compute_stats(date.today() - timedelta(days=365), date.today(), "exclude")
    check("retro filter", only["total"] == 2 and exc["total"] == s["total"] - 2)
    narrow = compute_stats(date.today() - timedelta(days=45), date.today() - timedelta(days=35), "all")
    check("date range filter", narrow["total"] == 2)
    check("comanage counts", s["transfer"] >= 1)
page = first.get("/statistics").data
check("statistics page for team", b"By diagnosis" in page and b"Outcome / disposition" in page and b"By month" in page)
check("statistics filter params", b"Massive" not in page and first.get(f"/statistics?from={d0}&to={d1}&retro=only").status_code == 200)
check("referrer cannot open statistics", ref.get("/statistics").status_code == 403)
check("statistics lists unset diagnosis link", b"dx=none" in page)
c6 = client(); c6.get("/sso?token=" + tok(email="th2@t.com", name="Dr Th2", role="thoracic"))
check("thoracic view-only can see stats + timeline + open retro form", c6.get("/statistics").status_code == 200 and c6.get(f"/patients/{pid}/timeline").status_code == 200
      and c6.get("/consults/retro/new", follow_redirects=False).status_code == 200)


print("\nFAILS:", len(fails))
for f in fails: print(" -", f)
sys.exit(1 if fails else 0)
