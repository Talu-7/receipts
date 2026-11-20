# Receipts hosted service (MVP)

A hosted front for the Receipts agent: researchers and labs submit an arXiv paper, a worker re-runs the
headline claim in a sandbox, and every check ends in an **Ed25519-signed receipt** and a public
**Receipts-verified badge**. The core `receipts` package is used as a library and stays stdlib-only;
this package has its own dependencies (`service/requirements.txt`).

```
browser / API client ──► api (FastAPI) ──► Postgres  ◄── worker ──► per-check child process
                          │  auth, quotas,   (queue,         │      Pipeline(settings, emit).run(...)
                          │  Stripe test,     events,        │            │
                          │  receipts,        receipts,      │            ▼
                          │  badges           usage)         └─ kill on cap   docker run receipts-sandbox
```

## What is in it

| Area | What it does | Where |
|---|---|---|
| Accounts | Email + password sign-up/login (stdlib `scrypt`), HMAC-signed session cookie, magic-link sign-in (**email delivery stubbed**: the link is logged, and returned in dev mode) | `app.py`, `security.py` |
| API keys | `rcpt_…` keys, shown once, stored as SHA-256; `Authorization: Bearer` or `X-API-Key`; revocable | `app.py` |
| Checks | `POST /checks` (arXiv URL or id, optional pinned claim, seeds), `GET /checks/{id}?after=<seq>` (status + progress events), `GET /checks` | `app.py` |
| Queue + worker | The `checks` table is the queue; a conditional `UPDATE … WHERE status='queued'` claims a row atomically on SQLite and Postgres. Each check runs in a child process that streams the pipeline's `emit` events into `check_events`; the parent heartbeats and **kills the child at the plan's wall-clock cap**, then removes the check's sandbox containers. Dead workers' checks are re-queued once, then abandoned. | `worker.py` |
| Honest failures | A cap hit, a crash, a non-zero exit or an abandoned check still ends in a signed **INCONCLUSIVE** receipt that says why and says it is not evidence against the paper. | `worker.py` |
| Quotas | Free: 3 checks/month, 60 GPU-min/month, ≤30 GPU-min and 45 min per check, ≤5 seeds. Lab: 100 checks, 3000 GPU-min, ≤120 GPU-min and 3 h per check, ≤20 seeds. Queued/running checks *reserve* their GPU cap so a burst cannot overrun the month. The per-check GPU cap is passed to the core's feasibility gate (`RECEIPTS_MAX_GPU_MIN`). | `quotas.py` |
| Metering | One `usage_records` row per check: sandbox job-seconds (from the receipt's `infra`), wall seconds, est. cost, LLM tokens. A killed check is metered at wall time. | `quotas.py` |
| Billing | Stripe Checkout (subscription) + webhook, **test mode only**: non-`sk_test_`/`rk_test_` keys and `livemode` events are refused in code. Webhooks are signature-checked and idempotent; `checkout.session.completed` → Lab, subscription `deleted`/`unpaid` → Free, lapsed status → free limits. | `billing.py` |
| Signed receipts | Ed25519 over the ASCII hex of the receipt's canonical SHA-256 (`receipts.receipt.canonical_hash`). Public key at `/.well-known/receipts-pubkey` (raw b64, PEM, JWK). `GET /receipts/{id}/verify`, `POST /verify`. | `signing.py` |
| Public pages | `GET /receipts/{id}` (core HTML receipt + signature footer; JSON with `Accept: application/json` or `/receipts/{id}.json`), `GET /badge/{id}.svg` | `app.py`, `badge.py` |
| Web front end | Server-rendered: sign up/in, submit a paper (optionally pin the claim), watch progress live (polling), view receipt, copy badge markdown, create API keys, upgrade | `templates/` |

### Stubbed / not done yet

- **Email**: magic links and receipts-ready notifications are logged, not sent.
- **Metered Stripe billing**: usage is recorded locally; nothing is reported to Stripe (`billing.report_usage` is a stub). The Lab plan is a flat test-mode subscription.
- **Migrations**: tables are created with `create_all`; add Alembic before the first schema change in production.
- **Rate limiting / CSRF tokens**: forms rely on `SameSite=Lax` cookies; add a CSRF token and per-IP limits before public launch.
- **GPU-minute cap** is enforced as a wall-clock kill plus the core's estimate gate, not a per-second GPU meter.
- **Docker socket**: the compose worker controls the host Docker daemon (root-equivalent). Production should run sandboxes on a separate host or a remote runner.
- **Badge** shows the latest verdict of one check; no "verified at commit X" pinning yet.

## Run locally

### Tests (no LLM, no Docker: the pipeline is faked)

```bash
python -m venv service/.venv
service/.venv/Scripts/pip install -r service/requirements-dev.txt     # Windows (bin/pip on Linux/macOS)
service/.venv/Scripts/python -m pytest service/tests
```

### Docker compose (api + worker + Postgres)

From the repository root:

```bash
docker compose -f service/docker-compose.yml up --build
# open http://localhost:8000, sign up, submit "demo:reproduces"
```

The worker uses `SERVICE_RUNNER=docker` and `RECEIPTS_JOB_IMAGE=receipts-sandbox:local` by default. Build that
image once (it is large: PyTorch + CUDA):

```bash
docker compose -f service/docker-compose.yml --profile sandbox build sandbox
```

or point at an image you already have: `RECEIPTS_JOB_IMAGE=<you>/receipts-sandbox:latest docker compose … up`.
Without any sandbox image, `SERVICE_RUNNER=local` runs jobs as subprocesses inside the worker container
(no isolation; dev only).

Real papers need model and search keys. Put them in `service/.env` (optional, git-ignored, read by compose):

```
NEBIUS_API_KEY=...
TAVILY_API_KEY=...
SERVICE_SECRET_KEY=<random 32+ bytes>
# Stripe TEST MODE ONLY: sk_test_..., whsec_..., price_...
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_LAB=price_...
```

To try billing locally: create a recurring test-mode price in the Stripe dashboard, then forward webhooks with
`stripe listen --forward-to localhost:8000/billing/webhook` and pay with Stripe's published test card.

### Without Docker

```bash
service/.venv/Scripts/python -m service.manage init
service/.venv/Scripts/uvicorn --factory service.app:create_app --port 8000
SERVICE_RUNNER=local service/.venv/Scripts/python -m service.worker      # second terminal
```

### Configuration

| Variable | Default | Notes |
|---|---|---|
| `SERVICE_DATABASE_URL` | `sqlite:///service/data/service.db` | e.g. `postgresql+psycopg://user:pw@host/db` |
| `SERVICE_DATA_DIR` | `service/data` | dev signing key, core receipt files |
| `SERVICE_PUBLIC_URL` | `http://localhost:8000` | used in badge markdown and Stripe redirect URLs |
| `SERVICE_SECRET_KEY` | random per process | session HMAC key; **set it** in any shared deployment |
| `SERVICE_SIGNING_KEY` / `SERVICE_SIGNING_KEY_FILE` | generated into the data dir | Ed25519 PEM or base64 32-byte seed. `python -m service.manage genkey` prints one |
| `SERVICE_RUNNER` | `docker` | `docker`, `nebius` (Serverless Jobs), or `local` |
| `SERVICE_EXECUTOR` | `process` | `inline` runs checks in a thread (tests only: cannot be killed) |
| `SERVICE_DEV_MODE` / `SERVICE_ALLOW_DEMO` | `1` / `1` | dev mode returns magic links in the response; demo inputs allow `demo:*` |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_LAB` | unset | test-mode values only |

Admin: `python -m service.manage set-plan someone@lab.org lab`.

### Verifying a receipt yourself

```python
import base64, json, urllib.request
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from receipts.receipt import canonical_hash
env = json.load(urllib.request.urlopen("https://HOST/receipts/<id>.json"))
pk = json.load(urllib.request.urlopen("https://HOST/.well-known/receipts-pubkey"))
r = env["receipt"]
assert canonical_hash(r) == r["sha256"]                                   # content intact
Ed25519PublicKey.from_public_bytes(base64.b64decode(pk["public_key_b64"])).verify(
    base64.b64decode(env["signature"]["value"]), r["sha256"].encode())    # signed by us (raises if not)
```

## Deployment

The API is small and stateless; the expensive part is sandbox compute. Split them: run the API + a
lightweight queue worker on a PaaS, and the reproductions on GPU capacity billed per second.

### API + database

| Option | Fit | Rough cost (MVP scale) |
|---|---|---|
| **Fly.io** | Docker-native, global, good for a FastAPI container; machines scale to zero. Managed Postgres is a separate product. | API 1 shared CPU / 1 GB ≈ $6/mo; managed Postgres from ≈ $40/mo (or a small self-run Postgres machine ≈ $5–10/mo) |
| **Render** | Simplest: web service + background worker + managed Postgres from one blueprint, deploys from the repo. | Web $7/mo + worker $7/mo + Postgres from ≈ $6–20/mo → ≈ $20–35/mo |
| **Railway** | Usage-based, fast setup, Postgres one click away; fine for a beta, less control at scale. | $5/mo plan incl. $5 usage; a small API + worker + DB typically ≈ $10–25/mo |

**Recommendation:** Render for the beta (fewest moving parts, worker and cron in one place); revisit Fly.io
when you want regions or scale-to-zero.

### GPU workers (where the sandboxes run)

| Option | How it fits Receipts | Rough on-demand price |
|---|---|---|
| **Nebius AI Cloud** (Serverless Jobs) | Already supported by the core (`NebiusRunner`, `SERVICE_RUNNER=nebius`): the worker submits one job per seed with the sandbox image; Nemotron calls also run on Nebius Token Factory, so one vendor and one bill. | L40S ≈ $1.5–2/GPU-h, H100 ≈ $3/GPU-h |
| **RunPod** | Cheapest raw GPUs (community cloud) and per-second serverless endpoints; needs a small RunPod runner (submit job, poll, fetch logs). | L40S ≈ $0.8–1/GPU-h, H100 ≈ $2–3/GPU-h |
| **Modal** | Best developer experience: per-second billing, Python-defined images, fast cold starts; needs a Modal runner wrapping `sandbox_entry.py`. | L40S ≈ $2/GPU-h, H100 ≈ $4/GPU-h; monthly free credits |

Prices are rough list prices for planning; check the providers' pricing pages before committing.

**Unit economics.** A free account is capped at 60 GPU-min/month: worst case ≈ $1.5–4 of GPU per active free
user per month, plus LLM tokens (a check uses on the order of 10⁵ tokens). A Lab at 3000 GPU-min/month on
L40S-class GPUs costs ≈ $75–100 of compute, which a ~$499/mo plan covers with margin. Keep the per-check
wall-clock cap; it is what bounds a runaway reproduction.

**Production checklist:** set `SERVICE_SECRET_KEY` and a stored `SERVICE_SIGNING_KEY` (publish its key id),
`SERVICE_DEV_MODE=0`, Postgres URL, HTTPS `SERVICE_PUBLIC_URL`, `SERVICE_RUNNER=nebius` (or another remote
runner) so the worker does not need the Docker socket, Alembic migrations, a real email sender, CSRF tokens,
rate limits, and live Stripe keys only after the test-mode-only guard in `billing.py` is deliberately changed.
