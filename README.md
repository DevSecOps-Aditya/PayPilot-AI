# PayPilot AI

An AI financial copilot: expense tracking, budgets, goals, subscriptions, analytics, monthly summaries, a Gemini-powered assistant, and PayPal Sandbox payment orders.

**Stack:** HTML/CSS/vanilla JS · Python Flask · SQLite · Gemini API (plain REST, no SDK)

## Run it

```bash
cd paypilot
pip install -r requirements.txt
cp .env.example .env        # set a strong SECRET_KEY; GEMINI_API_KEY is optional
python app.py               # http://127.0.0.1:5000
```

Without `GEMINI_API_KEY` the app still works: the assistant and reports use an **offline mode** built from your data, clearly labelled in the UI. Sign up, then click **Load demo data** on the dashboard.

`GEMINI_MODEL` defaults to `gemini-3.8-flash`. Model IDs change over time, so check https://ai.google.dev/gemini-api/docs/models and override it in `.env` if needed.

### PayPal Sandbox orders

From the Dashboard, **Set a payout** opens a dialog where each seller enters their PayPal Sandbox REST app Client ID and Secret. PayPilot validates them with PayPal, stores them encrypted using the app's `SECRET_KEY`, then asks for the merchant ID, amount, and order details. The customer approves the generated PayPal link; PayPilot captures the order server-side and verifies the captured amount, currency, and recipient. Credentials are never returned to the browser after saving. It does not import transactions or handle live payments.

Each merchant needs their own PayPal Sandbox REST app credentials from the PayPal Developer Dashboard. For local development, PayPilot automatically creates a stable secret key in `instance/secret_key` and uses `http://127.0.0.1:5000` as the callback URL. Keep the `instance` folder private and do not delete the key after connecting credentials.

For a hosted deployment, set `PAYPILOT_ENV=production` and configure the app's secure key and public HTTPS callback URL in the server environment (or `.env`):

```env
SECRET_KEY=use-a-stable-random-secret-at-least-32-characters-long
PAYPILOT_ENV=production
PAYPAL_RETURN_BASE_URL=https://your-public-app.example
```

Users enter their own Sandbox Client ID and Secret in the Dashboard dialog; do not enter a PayPal account password. Keep the app's `SECRET_KEY` private and stable because it encrypts credentials at rest. Use HTTPS for hosted deployments and set `PAYPAL_RETURN_BASE_URL` to the public HTTPS origin. Use Sandbox buyer accounts to approve orders. A merchant ID must be permitted to receive payments under the supplied PayPal app; PayPal will reject unauthorized payees.

This direct-credentials model does not require PayPal multiparty partner onboarding. Its external API is fixed to Sandbox; no live payment endpoint is enabled.

## Tests

```bash
python tests/test_api.py    # API checks; forces offline mode, never calls Gemini
```

## Folder structure

```
paypilot/
├── app.py              # Flask app: pages, JSON API, analytics, demo seed
├── ai.py               # PayPilot AI persona, Gemini REST client, offline fallback
├── database.py         # SQLite connection helpers
├── schema.sql          # Database schema
├── requirements.txt
├── .env.example
├── templates/          # base, landing, login, register, dashboard, expenses,
│                       # budgets, goals, subscriptions, assistant, reports, analytics
├── static/
│   ├── css/style.css   # Design system: tokens, components, dark/light themes
│   └── js/app.js       # API client, SVG charts, one module per page
├── tests/test_api.py
└── docs/               # MVP_ROADMAP.md, HACKATHON_PITCH.md
```

## Database schema

`users`, `transactions` (income and expense, split by `type`), `paypal_connections`, `paypal_orders`, `budgets` (unique per user and category), `goals`, `subscriptions`, `chat_messages`, `reports`. All user-owned tables cascade on user delete. See `schema.sql`.

## Routes

**Pages:** `/` · `/login` · `/register` · `/dashboard` · `/expenses` · `/budgets` · `/goals` · `/subscriptions` · `/analytics` · `/reports` · `/assistant`

**API** (all except auth require a session):

| Area | Endpoints |
|---|---|
| Auth | `POST /api/auth/register` · `/login` · `/logout` |
| Transactions | `GET/POST /api/transactions` · `PUT/DELETE /api/transactions/<id>` |
| Payments | `GET /api/payments` · `POST /api/paypal/connect` · `POST /api/payments/orders` · public `POST /api/payments/orders/<id>/capture` (unguessable PayPal return token required) |
| Budgets | `GET/POST /api/budgets` · `DELETE /api/budgets/<id>` · `GET /api/budgets/suggestions` |
| Goals | `GET/POST /api/goals` · `PUT/DELETE /api/goals/<id>` · `POST /api/goals/<id>/contribute` |
| Subscriptions | `GET/POST /api/subscriptions` · `PUT/DELETE /api/subscriptions/<id>` |
| Insights | `GET /api/dashboard` · `/api/summary?month=` · `/api/analytics?months=3\|6\|12` |
| Assistant | `GET /api/assistant/status` · `GET/DELETE /api/assistant/history` · `POST /api/assistant/chat` |
| Reports | `POST /api/reports/generate` · `GET /api/reports` · `GET/DELETE /api/reports/<id>` |
| Demo | `POST /api/demo/seed` (empty accounts only) |

## The PayPilot AI persona

`ai.py` sends your exact system prompt as the Gemini `system_instruction`, followed by data rules and a JSON snapshot of the user's finances. The model is told not to invent numbers.

## Known limits (MVP)

- No CSRF tokens (session cookie is `SameSite=Lax`), no password reset or email verification
- Login rate limiting is in-memory and per-process
- PayPal stores each merchant's app credentials encrypted with the stable app `SECRET_KEY`; only Sandbox is supported
- Automated PayPal tests mock the external API; a real Sandbox run still requires valid merchant credentials and seller/buyer test accounts
- Live Gemini calls were not exercised in testing (no API key in the build environment); the request format follows Google's REST docs, and any failure falls back to offline mode

See `docs/MVP_ROADMAP.md` and `docs/HACKATHON_PITCH.md`.
