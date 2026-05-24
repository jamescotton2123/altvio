# Deploying Altvio Demo (Fly.io + Supabase)

Minimal guide to ship a **read-only-ish demo** recruiters can click. Uses fictional seed data only.

---

## Overview

| Component | Service | Cost |
|-----------|---------|------|
| Postgres + Auth | Supabase (new project `altvio-demo`) | Free tier |
| FastAPI API | Fly.io | ~$0–5/mo |
| Frontend (optional) | Vercel | Free tier |

**Do not reuse your private dev Supabase project** — create a separate demo project.

---

## 1. Supabase demo project

1. Create project at [supabase.com](https://supabase.com) named `altvio-demo`.
2. SQL Editor → run every file in `supabase/migrations/` **in filename order**.
3. Run `supabase/seed_dev_data.sql`.
4. Settings → API → copy:
   - `SUPABASE_URL`
   - `service_role` key → `SUPABASE_KEY` (server only; never expose in frontend)

**Smoke test:**

```sql
SELECT name FROM firms WHERE id = '2cf19464-0fb6-460b-a283-09fab02d4ced';
-- Dev RIA
```

---

## 2. Fly.io API deploy

### Prerequisites

```bash
brew install flyctl   # or curl install from fly.io/docs
fly auth login
```

### Create app

From repo root (or `altvio-public-release` after bootstrap):

```bash
fly launch --name altvio-demo --no-deploy
```

When prompted:
- Region: `lax` (or closest to you)
- Do not add Postgres (using Supabase)
- Dockerfile already exists in repo

### Set secrets

```bash
fly secrets set \
  SUPABASE_URL="https://YOUR_PROJECT.supabase.co" \
  SUPABASE_KEY="YOUR_SERVICE_ROLE_KEY" \
  API_PUBLIC_BASE_URL="https://altvio-demo.fly.dev" \
  PLATFORM_BASE_URL="https://altvio-demo.fly.dev" \
  PORTAL_BASE_URL="https://altvio-demo.fly.dev"
```

Optional (only if demoing integrations):

```bash
fly secrets set OPENAI_API_KEY="..." DOCUSIGN_INTEGRATION_KEY="..."
```

For a **static demo**, Supabase + firm ID header is enough — `/deals/active`, `/docs`, `/health` work without OpenAI/DocuSign keys.

### Deploy

```bash
fly deploy
fly open /docs
```

---

## 3. Demo API key for README

The demo uses header auth (no OAuth in Phase 0):

```bash
curl -H "X-Firm-ID: 2cf19464-0fb6-460b-a283-09fab02d4ced" \
  https://altvio-demo.fly.dev/deals/active
```

Document this in README under **Demo** — recruiters can poke Swagger at `/docs`.

### Rate limiting (recommended)

Add Fly.io `[[services.concurrency]]` limits or Cloudflare in front if the URL gets hammered. Seed data is fictional; abuse is annoying but not catastrophic.

---

## 4. Health checks

```bash
curl https://altvio-demo.fly.dev/health
# {"status":"ok"} or similar

curl -H "X-Firm-ID: 2cf19464-0fb6-460b-a283-09fab02d4ced" \
  https://altvio-demo.fly.dev/deals/active | head -c 500
```

---

## 5. Loom walkthrough script (5 min)

1. **0:00–0:30** — Intro: personal learning project, alt-investments ops
2. **0:30–1:30** — `/deals/active` → Meridian hub, commitments, KYC statuses
3. **1:30–2:30** — `POST /query` — NL question → allowlisted RPC (show in Swagger)
4. **2:30–3:30** — Code: DocuSign webhook HMAC + idempotency (`api/routes/docusign_webhook.py`)
5. **3:30–4:30** — KYC parser + pending-change queue (`core/kyc_parser.py`)
6. **4:30–5:00** — Repo tour: `api/routes/`, `core/`, `supabase/migrations/`

Embed Loom URL in README after recording.

---

## 6. Troubleshooting

| Issue | Fix |
|-------|-----|
| 500 on all routes | Check `SUPABASE_URL` / `SUPABASE_KEY` secrets on Fly |
| Empty deals list | Re-run seed; verify firm UUID header |
| Scheduler errors on boot | Expected without Graph/DocuSign keys — check logs; core routes still work |
| Migrations out of order | Re-create Supabase project; apply migrations sequentially |

---

## 7. Public + Quiet reminder

- Put demo URL on resume and in warm-intro DMs
- Do **not** LinkedIn-post the demo until you have a signed offer
- MIT-license the repo **before** signing any future employer offer letter
