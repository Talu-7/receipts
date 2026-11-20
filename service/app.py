"""The hosted Receipts API and web front end.

    uvicorn --factory service.app:create_app --port 8000
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from receipts.arxiv_src import parse_arxiv_id
from receipts.receipt import render_html

from . import badge, billing, quotas, security
from .config import ServiceSettings
from .db import ApiKey, Check, CheckEvent, Database, MagicLink, User, get_user_by_email, utcnow
from .signing import ALG, load_signer, verify

log = logging.getLogger("receipts.service")
HERE = Path(__file__).resolve().parent
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------- request models
def _norm_email(v: str) -> str:
    v = str(v).strip().lower()
    if not EMAIL_RE.match(v):
        raise ValueError("not a valid email address")
    return v


class Credentials(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=256)

    _email = field_validator("email")(_norm_email)


class MagicRequest(BaseModel):
    email: str

    _email = field_validator("email")(_norm_email)


class PinnedClaim(BaseModel):
    statement: str = Field(min_length=3, max_length=1000)
    metric: str = Field(min_length=1, max_length=200)
    value: float
    dataset: str = Field(default="", max_length=300)
    unit: str = Field(default="", max_length=20)
    where: str = Field(default="", max_length=200)
    higher_is_better: bool = True


class CheckRequest(BaseModel):
    arxiv_url: str = Field(min_length=4, max_length=500)
    claim: PinnedClaim | None = None
    seeds: int | None = Field(default=None, ge=1, le=64)


class KeyRequest(BaseModel):
    name: str = Field(default="default", max_length=100)


class VerifyRequest(BaseModel):
    receipt: dict
    signature: str | None = None


# ---------------------------------------------------------------- helpers
def _check_input(raw: str, allow_demo: bool) -> str:
    raw = raw.strip()
    if raw.lower().startswith("demo:"):
        if not allow_demo:
            raise HTTPException(422, "demo inputs are disabled on this server")
        return raw.lower()
    aid = parse_arxiv_id(raw)
    if not aid:
        raise HTTPException(422, "expected an arXiv URL or id, e.g. https://arxiv.org/abs/2106.09685")
    return f"https://arxiv.org/abs/{aid}"


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="seconds")


def check_json(s: ServiceSettings, c: Check, events: list[CheckEvent] | None = None) -> dict:
    d: dict[str, Any] = {
        "id": c.id, "status": c.status, "verdict": c.verdict, "input": c.arxiv_url, "seeds": c.seeds,
        "claim_override": c.claim_override, "failure_reason": c.failure_reason,
        "caps": {"gpu_minutes": c.gpu_minutes_cap, "wall_seconds": c.wall_seconds_cap},
        "created_at": _iso(c.created_at), "started_at": _iso(c.started_at), "finished_at": _iso(c.finished_at),
        "links": {"self": f"{s.public_base_url}/checks/{c.id}", "watch": f"{s.public_base_url}/c/{c.id}",
                  "badge": f"{s.public_base_url}/badge/{c.id}.svg"},
    }
    if c.receipt:
        d["links"]["receipt"] = f"{s.public_base_url}/receipts/{c.id}"
        d["links"]["receipt_json"] = f"{s.public_base_url}/receipts/{c.id}.json"
        d["badge_markdown"] = badge.markdown(s.public_base_url, c.id)
        d["receipt_sha256"] = c.receipt_sha256
    if events is not None:
        d["events"] = [{"seq": e.id, "t": e.t, "stage": e.stage, "status": e.status, "msg": e.msg, "data": e.data}
                       for e in events]
    return d


# ---------------------------------------------------------------- app factory
def create_app(settings: ServiceSettings | None = None) -> FastAPI:
    s = settings or ServiceSettings.from_env()
    s.ensure_data_dir()
    if not s.secret_key_from_env:
        log.warning("SERVICE_SECRET_KEY is not set: using a random key, sessions will not survive a restart")
    db = Database(s.database_url)
    db.create_all()
    signer = load_signer(s)
    templates = Jinja2Templates(directory=str(HERE / "templates"))

    app = FastAPI(title="Receipts", version="0.1.0",
                  description="Independent, signed reproduction checks for ML papers.")
    app.state.settings, app.state.db, app.state.signer = s, db, signer
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # ------------------------------------------------ dependencies
    def get_db():
        with db.session() as session:
            yield session

    def current_user(request: Request, session: Session = Depends(get_db)) -> User | None:
        auth = request.headers.get("authorization", "")
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "").strip()
        if key:
            k = session.scalar(select(ApiKey).where(ApiKey.key_hash == security.sha256_hex(key), ApiKey.revoked_at.is_(None)))
            if not k:
                raise HTTPException(401, "invalid or revoked API key")
            k.last_used_at = utcnow()
            return session.get(User, k.user_id)
        uid = security.read_session(request.cookies.get(security.SESSION_COOKIE), s.secret_key)
        return session.get(User, uid) if uid else None

    def require_user(user: User | None = Depends(current_user)) -> User:
        if user is None:
            raise HTTPException(401, "sign in or pass an API key (Authorization: Bearer rcpt_...)")
        return user

    def _login(resp: Response, user: User) -> None:
        resp.set_cookie(security.SESSION_COOKIE, security.sign_session(user.id, s.secret_key), httponly=True,
                        samesite="lax", secure=s.public_base_url.startswith("https://"), max_age=security.SESSION_TTL_S)

    def _wants_html(request: Request) -> bool:
        ct = request.headers.get("content-type", "")
        return "application/x-www-form-urlencoded" in ct or "multipart/form-data" in ct

    async def _body(request: Request) -> dict:
        if _wants_html(request):
            return dict(await request.form())
        try:
            return await request.json()
        except Exception:
            raise HTTPException(422, "expected a JSON body")

    def _page(request: Request, name: str, user: User | None, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"user": user, "settings": s, **ctx})

    # ------------------------------------------------ auth
    @app.post("/auth/signup", tags=["auth"])
    async def signup(request: Request, session: Session = Depends(get_db)):
        try:
            cred = Credentials(**(await _body(request)))
        except Exception as e:
            if _wants_html(request):
                return _page(request, "login.html", None, mode="signup", error=str(e).splitlines()[-1] if str(e) else "invalid input")
            raise HTTPException(422, str(e))
        if get_user_by_email(session, cred.email):
            if _wants_html(request):
                return _page(request, "login.html", None, mode="signup", error="that email already has an account")
            raise HTTPException(409, "that email already has an account")
        user = User(email=cred.email, password_hash=security.hash_password(cred.password))
        session.add(user)
        session.flush()
        resp: Response = RedirectResponse("/app", 303) if _wants_html(request) else JSONResponse(
            {"id": user.id, "email": user.email, "plan": user.plan}, 201)
        _login(resp, user)
        return resp

    @app.post("/auth/login", tags=["auth"])
    async def login(request: Request, session: Session = Depends(get_db)):
        b = await _body(request)
        user = get_user_by_email(session, str(b.get("email", "")))
        if not user or not security.verify_password(str(b.get("password", "")), user.password_hash):
            if _wants_html(request):
                return _page(request, "login.html", None, mode="login", error="wrong email or password")
            raise HTTPException(401, "wrong email or password")
        resp: Response = RedirectResponse("/app", 303) if _wants_html(request) else JSONResponse(
            {"id": user.id, "email": user.email, "plan": user.plan})
        _login(resp, user)
        return resp

    @app.post("/auth/logout", tags=["auth"])
    def logout():
        resp = RedirectResponse("/", 303)
        resp.delete_cookie(security.SESSION_COOKIE)
        return resp

    @app.post("/auth/magic-link", tags=["auth"])
    async def magic_link(request: Request, session: Session = Depends(get_db)):
        """Stub: email delivery is not wired up. The link is logged, and returned in dev mode."""
        try:
            req = MagicRequest(**(await _body(request)))
        except Exception as e:
            raise HTTPException(422, str(e))
        tok, h = security.new_magic_token()
        session.add(MagicLink(token_hash=h, email=req.email, expires_at=utcnow() + timedelta(minutes=15)))
        link = f"{s.public_base_url}/auth/magic/{tok}"
        log.info("magic link for %s: %s", req.email, link if s.dev_mode else "(sent)")
        out = {"sent": True, "note": "email delivery is stubbed in the MVP"}
        if s.dev_mode:
            out["dev_link"] = link
        if _wants_html(request):
            return _page(request, "login.html", None, mode="login", info="Magic link created." +
                         (f" Dev mode: {link}" if s.dev_mode else " Check your email."))
        return out

    @app.get("/auth/magic/{token}", tags=["auth"])
    def magic_login(token: str, session: Session = Depends(get_db)):
        ml = session.get(MagicLink, security.sha256_hex(token))
        exp = ml.expires_at.replace(tzinfo=timezone.utc) if ml and ml.expires_at.tzinfo is None else (ml.expires_at if ml else None)
        if not ml or ml.used or exp < utcnow():
            raise HTTPException(400, "this sign-in link is invalid or has expired")
        ml.used = True
        user = get_user_by_email(session, ml.email)
        if not user:
            user = User(email=ml.email)
            session.add(user)
            session.flush()
        resp = RedirectResponse("/app", 303)
        _login(resp, user)
        return resp

    @app.get("/me", tags=["account"])
    def me(user: User = Depends(require_user), session: Session = Depends(get_db)):
        return {"id": user.id, "email": user.email, "plan": user.plan, "plan_status": user.plan_status,
                "usage": quotas.usage(session, user)}

    @app.get("/usage", tags=["account"])
    def usage(user: User = Depends(require_user), session: Session = Depends(get_db)):
        return quotas.usage(session, user)

    # ------------------------------------------------ API keys
    @app.post("/api-keys", tags=["account"], status_code=201)
    async def create_key(request: Request, user: User = Depends(require_user), session: Session = Depends(get_db)):
        b = await _body(request) if request.headers.get("content-length") not in (None, "0") else {}
        req = KeyRequest(**b)
        key, prefix, h = security.new_api_key()
        k = ApiKey(user_id=user.id, name=req.name or "default", prefix=prefix, key_hash=h)
        session.add(k)
        session.flush()
        if _wants_html(request):
            return _page(request, "app.html", user, **_dashboard(session, user), new_key=key)
        return {"id": k.id, "name": k.name, "key": key, "prefix": prefix, "note": "store this key now; it is not shown again"}

    @app.get("/api-keys", tags=["account"])
    def list_keys(user: User = Depends(require_user), session: Session = Depends(get_db)):
        return [{"id": k.id, "name": k.name, "prefix": k.prefix, "created_at": _iso(k.created_at),
                 "last_used_at": _iso(k.last_used_at), "revoked": k.revoked_at is not None}
                for k in session.scalars(select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at))]

    @app.delete("/api-keys/{key_id}", tags=["account"])
    def revoke_key(key_id: str, user: User = Depends(require_user), session: Session = Depends(get_db)):
        k = session.get(ApiKey, key_id)
        if not k or k.user_id != user.id:
            raise HTTPException(404, "no such key")
        k.revoked_at = k.revoked_at or utcnow()
        return {"id": k.id, "revoked": True}

    # ------------------------------------------------ checks
    def _submit(session: Session, user: User, req: CheckRequest) -> Check:
        inp = _check_input(req.arxiv_url, s.allow_demo_inputs)
        try:
            seeds, gpu_cap, wall_cap = quotas.admit(session, user, req.seeds)
        except quotas.QuotaExceeded as e:
            raise HTTPException(402, f"{e}. Upgrade to the Lab plan for more checks.")
        c = Check(user_id=user.id, arxiv_url=inp, seeds=seeds, gpu_minutes_cap=gpu_cap, wall_seconds_cap=wall_cap,
                  claim_override=req.claim.model_dump() if req.claim else None, period=quotas.period_of())
        session.add(c)
        session.flush()
        session.add(CheckEvent(check_id=c.id, t=utcnow().timestamp(), stage="queue", status="start",
                               msg="Queued", data={"seeds": seeds, "gpu_minutes_cap": gpu_cap}))
        return c

    @app.post("/checks", tags=["checks"], status_code=202)
    async def create_check(request: Request, user: User = Depends(require_user), session: Session = Depends(get_db)):
        b = await _body(request)
        if _wants_html(request):  # the web form sends flat fields
            claim = None
            if str(b.get("claim_statement", "")).strip():
                try:
                    claim = PinnedClaim(statement=b["claim_statement"], metric=b.get("claim_metric") or "metric",
                                        value=float(b.get("claim_value") or 0), dataset=b.get("claim_dataset") or "",
                                        unit=b.get("claim_unit") or "")
                except Exception as e:
                    return _page(request, "app.html", user, **_dashboard(session, user), error=f"pinned claim: {e}")
            try:
                c = _submit(session, user, CheckRequest(arxiv_url=b.get("arxiv_url", ""), claim=claim,
                                                        seeds=int(b["seeds"]) if b.get("seeds") else None))
            except HTTPException as e:
                return _page(request, "app.html", user, **_dashboard(session, user), error=e.detail)
            return RedirectResponse(f"/c/{c.id}", 303)
        try:
            req = CheckRequest(**b)
        except Exception as e:
            raise HTTPException(422, str(e))
        c = _submit(session, user, req)
        return JSONResponse(check_json(s, c), 202)

    @app.get("/checks", tags=["checks"])
    def list_checks(user: User = Depends(require_user), session: Session = Depends(get_db)):
        cs = session.scalars(select(Check).where(Check.user_id == user.id).order_by(Check.created_at.desc()).limit(100))
        return [check_json(s, c) for c in cs]

    @app.get("/checks/{check_id}", tags=["checks"])
    def get_check(check_id: str, after: int = 0, user: User = Depends(require_user), session: Session = Depends(get_db)):
        c = session.get(Check, check_id)
        if not c or c.user_id != user.id:
            raise HTTPException(404, "no such check")
        ev = list(session.scalars(select(CheckEvent).where(CheckEvent.check_id == c.id, CheckEvent.id > after)
                                  .order_by(CheckEvent.id)))
        return check_json(s, c, ev)

    # ------------------------------------------------ public receipts, badges, keys
    def _public_check(session: Session, check_id: str) -> Check:
        c = session.get(Check, check_id)
        if not c or not c.receipt:
            raise HTTPException(404, "no receipt with that id (yet)")
        return c

    def _envelope(c: Check) -> dict:
        return {"receipt": c.receipt,
                "signature": {"alg": ALG, "key_id": c.signing_key_id, "signed": "receipt.sha256 (ASCII hex)",
                              "value": c.signature},
                "public_key_url": f"{s.public_base_url}/.well-known/receipts-pubkey",
                "check": {"id": c.id, "status": c.status, "verdict": c.verdict}}

    @app.get("/receipts/{check_id}.json", tags=["receipts"])
    def receipt_json(check_id: str, session: Session = Depends(get_db)):
        return _envelope(_public_check(session, check_id))

    @app.get("/receipts/{check_id}", tags=["receipts"])
    def receipt_view(check_id: str, request: Request, format: str | None = None, session: Session = Depends(get_db)):
        c = _public_check(session, check_id)
        if format == "json" or "application/json" in request.headers.get("accept", ""):
            return _envelope(c)
        html = render_html(c.receipt)
        bmd = badge.markdown(s.public_base_url, c.id)
        footer = templates.get_template("receipt_footer.html").render(c=c, settings=s, badge_md=bmd)
        return HTMLResponse(html.replace("</main>", footer + "</main>", 1))

    @app.get("/receipts/{check_id}/verify", tags=["receipts"])
    def verify_stored(check_id: str, session: Session = Depends(get_db)):
        c = _public_check(session, check_id)
        return {"check_id": c.id, "key_id": c.signing_key_id, **verify(c.receipt, c.signature, signer.public_key)}

    @app.post("/verify", tags=["receipts"])
    def verify_any(req: VerifyRequest):
        """Verify a receipt you hold: send {"receipt": {...}, "signature": "<base64>"} (or a /receipts/{id}.json envelope)."""
        sig = req.signature
        rec = req.receipt
        if "receipt" in rec and isinstance(rec.get("receipt"), dict):  # a whole envelope was posted as the receipt
            sig = sig or ((rec.get("signature") or {}).get("value"))
            rec = rec["receipt"]
        return {"key_id": signer.key_id, **verify(rec, sig, signer.public_key)}

    @app.get("/badge/{check_id}.svg", tags=["receipts"])
    def badge_svg(check_id: str, session: Session = Depends(get_db)):
        c = session.get(Check, check_id)
        state = (c.verdict or "inconclusive") if c and c.status in ("done", "failed") else (c.status if c else "unknown")
        return Response(badge.render(state), media_type="image/svg+xml",
                        headers={"Cache-Control": "max-age=300" if c and c.receipt else "no-cache"})

    @app.get("/.well-known/receipts-pubkey", tags=["receipts"])
    def pubkey():
        return {"alg": ALG, "key_id": signer.key_id, "public_key_b64": signer.public_raw_b64,
                "public_key_pem": signer.public_pem, "jwk": signer.jwk(),
                "signed_message": "the ASCII bytes of the receipt's `sha256` field (hex of receipts.receipt.canonical_hash)"}

    # ------------------------------------------------ billing (Stripe TEST MODE)
    @app.post("/billing/checkout", tags=["billing"])
    def checkout(request: Request, user: User = Depends(require_user), session: Session = Depends(get_db)):
        try:
            url = billing.create_checkout(s, user)
        except billing.BillingError as e:
            if _wants_html(request) or "text/html" in request.headers.get("accept", ""):
                return _page(request, "app.html", user, **_dashboard(session, user), error=str(e))
            raise HTTPException(503, str(e))
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(url, 303)
        return {"url": url}

    @app.post("/billing/webhook", tags=["billing"])
    async def webhook(request: Request, session: Session = Depends(get_db)):
        try:
            return billing.handle_webhook(s, session, await request.body(), request.headers.get("stripe-signature"))
        except billing.BillingError as e:
            raise HTTPException(400, str(e))

    @app.get("/plans", tags=["billing"])
    def plans():
        return {k: vars(p) for k, p in quotas.PLANS.items()}

    @app.get("/healthz", include_in_schema=False)
    def healthz(session: Session = Depends(get_db)):
        session.execute(select(1))
        return {"ok": True}

    # ------------------------------------------------ web pages
    def _dashboard(session: Session, user: User) -> dict:
        cs = list(session.scalars(select(Check).where(Check.user_id == user.id).order_by(Check.created_at.desc()).limit(50)))
        keys = list(session.scalars(select(ApiKey).where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))))
        return {"checks": cs, "keys": keys, "usage": quotas.usage(session, user), "plans": quotas.PLANS}

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home(request: Request, user: User | None = Depends(current_user)):
        if user:
            return RedirectResponse("/app", 303)
        return _page(request, "index.html", None, plans=quotas.PLANS)

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_page(request: Request, mode: str = "login"):
        return _page(request, "login.html", None, mode="signup" if mode == "signup" else "login")

    @app.get("/app", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(request: Request, user: User | None = Depends(current_user),
                  session: Session = Depends(get_db)):
        if not user:
            return RedirectResponse("/login", 303)
        info = {"success": "Subscription started (Stripe test mode). It applies as soon as the webhook arrives.",
                "cancelled": "Checkout cancelled."}.get(request.query_params.get("billing", ""))
        return _page(request, "app.html", user, **_dashboard(session, user), info=info)

    @app.get("/c/{check_id}", response_class=HTMLResponse, include_in_schema=False)
    def watch(check_id: str, request: Request, user: User | None = Depends(current_user), session: Session = Depends(get_db)):
        if not user:
            return RedirectResponse("/login", 303)
        c = session.get(Check, check_id)
        if not c or c.user_id != user.id:
            raise HTTPException(404, "no such check")
        return _page(request, "watch.html", user, c=c, badge_md=badge.markdown(s.public_base_url, c.id))

    return app
