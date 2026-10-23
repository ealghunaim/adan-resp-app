# Al-Adan Hospital Respirology app

Standalone Flask app: Respirology **consults** (referring physician -> 1st on-call -> 2nd on-call approval -> WhatsApp / email / print)
and **clinic referrals** (referring physician -> 2nd on-call decides appointment -> WhatsApp / email / print), for patients admitted
or seen at Al-Adan Hospital only. The main Thoracic Surgery app reads this data by Civil ID.

## Render settings
- Build: `pip install -r requirements.txt`   Start: `gunicorn "app:create_app()"`
- Database: Render PostgreSQL, set `DATABASE_URL`
- Environment: `SECRET_KEY`, `DATABASE_URL`, `SESSION_COOKIE_SECURE=true`, `ADAN_ADMIN_EMAIL`, `ADAN_ADMIN_PASSWORD` (creates first admin),
  `MAIN_APP_URL` (optional, shows a link to the Thoracic app), `ADAN_API_KEY` (long random string; same value goes in the main app as `ADAN_LOOKUP_KEY`)

## Run tests
`python3 test_adan.py`
