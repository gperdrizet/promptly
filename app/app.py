'''
FastAPI gateway app.

Validates per-user API keys, checks token balance, proxies requests to
llama-server, records usage (token counts only - no content).
'''

import logging
import json
import os
import re
import secrets
import time
from ipaddress import ip_address, ip_network
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosmtplib
import httpx
import stripe
from fastapi import FastAPI, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .db import (
    User, UsageEvent, TrialTokens,
    engine, init_db,
    generate_api_key, verify_api_key, get_user_by_key_prefix,
    total_available_tokens, deduct_tokens,
)

from .btcpay import create_invoice, verify_webhook as btcpay_verify_webhook
from .checkout import create_checkout_session, verify_webhook
from .db import TokenPurchase
from .email import send_trial_key_email, TRIAL_TOKENS, TRIAL_EXPIRY_DAYS
from .proxy import proxy_request, proxy_stream

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# The app only accepts connections from nginx (GATEWAY_BIND=127.0.0.1), so the
# first X-Forwarded-For entry is nginx's own $proxy_add_x_forwarded_for value,
# i.e. the real client address, not something an external caller can spoof.
# Set TRUST_PROXY_HEADERS=false to fall back to the raw socket address (tests, local dev).
TRUST_PROXY_HEADERS = os.environ.get('TRUST_PROXY_HEADERS', 'true').lower() != 'false'


def _client_ip(request: Request) -> str:
    '''Return the real client IP for rate limiting and admin access checks.

    Args:
        request: Incoming FastAPI request.

    Returns:
        The client IP from X-Forwarded-For (trusted single-hop proxy), falling
        back to the raw socket address if the header is absent or untrusted.
    '''

    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get('x-forwarded-for')

        if forwarded:
            return forwarded.split(',')[0].strip()

    return request.client.host if request.client else ''

# Simple in-memory rate limit for registration: max attempts per IP per hour.
# Override via REGISTRATION_RATE_LIMIT env var. Stale keys are swept by
# _check_rate_limit() so these stores stay bounded.
_reg_attempts: dict[str, list[float]] = {}
REG_LIMIT = int(os.environ.get('REGISTRATION_RATE_LIMIT', '3'))
REG_WINDOW = int(os.environ.get('REGISTRATION_RATE_WINDOW', '3600'))

# Shorter burst window to slow down repeated spam submissions.
_reg_burst_attempts: dict[str, list[float]] = {}
REG_BURST_LIMIT = int(os.environ.get('REGISTRATION_BURST_LIMIT', '2'))
REG_BURST_WINDOW = int(os.environ.get('REGISTRATION_BURST_WINDOW', '600'))

# Lightweight anti-bot challenge for public signup.
_reg_captcha_challenges: dict[str, tuple[float, int]] = {}
_CAPTCHA_COOKIE_NAME = 'signup_captcha'
_CAPTCHA_TTL = int(os.environ.get('REGISTRATION_CAPTCHA_TTL', '300'))

# Per-IP rate limit on all /v1/* hits (catches unauthenticated hammering).
# Generous enough to allow multiple real users behind the same NAT.
_ip_inf_attempts: dict[str, list[float]] = {}
INF_IP_LIMIT = int(os.environ.get('INFERENCE_IP_RATE_LIMIT', '120'))
INF_IP_WINDOW = 60  # seconds

# Per-user rate limit on authenticated inference requests.
# Catches accidental infinite loops. LLM calls take seconds each, so 60/min
# is generous for legitimate use while blocking runaway loops immediately.
_user_inf_attempts: dict[str, list[float]] = {}
INF_USER_LIMIT = int(os.environ.get('INFERENCE_USER_RATE_LIMIT', '60'))
INF_USER_WINDOW = 60  # seconds

# Pinned default model: served when clients omit the `model` field (or send
# an empty one). The backend router defines a matching `default` alias that
# mirrors the pinned model - see llama.cpp utils/agent-router.ini.
DEFAULT_MODEL = os.environ.get('DEFAULT_MODEL', 'default')

# Optional public model allow-list (comma-separated router aliases). When
# unset, any alias the router knows is accepted (the router rejects unknown
# ones itself). When set, requests naming a model outside the list get a
# clear 400 from the gateway - use this to gate which models are public.
_MODEL_ALLOWLIST = {
    m.strip() for m in os.environ.get('MODEL_ALLOWLIST', '').split(',') if m.strip()
}

# Lightweight email validation for public signup.
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}$"
)


def _is_valid_email(email: str) -> bool:
    '''Return True for a basic, non-suspicious email address.'''

    if not email or len(email) > 254:
        return False

    if any(ch in email for ch in ('/', '\\', '<', '>', '"', "'", '(', ')', ',', ';', ':')):
        return False

    if email.count('@') != 1:
        return False

    local, domain = email.rsplit('@', 1)
    if not local or len(local) > 64 or not domain or len(domain) > 253:
        return False

    if local.startswith('.') or local.endswith('.') or '..' in local:
        return False

    if not _EMAIL_RE.fullmatch(email):
        return False

    return True


# Periodic sweep so the rate-limiter stores stay bounded: a key whose entire
# window has passed (or whose list went empty) is dropped, at most once per
# interval per store.
_last_limiter_sweep: dict[int, float] = {}  # id(store) -> last sweep time
_LIMITER_SWEEP_INTERVAL = 60  # seconds


def _check_rate_limit(store: dict, key: str, limit: int, window: float) -> bool:
    '''Check a sliding-window rate limit and record the request.

    Args:
        store: Module-level mapping of limit key -> request timestamps.
        key: The limit key (client IP or API key prefix).
        limit: Maximum requests allowed within the window.
        window: Window length in seconds.

    Returns:
        True if the request is within the limit, False if rate-limited.
    '''

    now = time.time()

    # A stale key is only ever re-checked if its owner retries, so sweep the
    # whole store periodically to keep it from growing without bound as new
    # IPs and users come and go.
    if now - _last_limiter_sweep.get(id(store), 0.0) >= _LIMITER_SWEEP_INTERVAL:
        _last_limiter_sweep[id(store)] = now
        stale = [k for k, ts in store.items() if not ts or now - ts[-1] >= window]
        for stale_key in stale:
            del store[stale_key]

    attempts = [t for t in store.get(key, ()) if now - t < window]

    if len(attempts) >= limit:
        store[key] = attempts
        return False

    attempts.append(now)
    store[key] = attempts
    return True


def _check_reg_rate_limit(ip: str) -> bool:
    '''Check per-IP rate limit for registration attempts.'''
    return _check_rate_limit(_reg_attempts, ip, REG_LIMIT, REG_WINDOW)


def _check_reg_burst_rate_limit(ip: str) -> bool:
    '''Check a short burst window for registration attempts.'''
    return _check_rate_limit(_reg_burst_attempts, ip, REG_BURST_LIMIT, REG_BURST_WINDOW)


def _prune_captcha_challenges(now: float | None = None) -> None:
    '''Remove stale captcha challenges.'''

    now = time.time() if now is None else now
    expired = [token for token, (expires_at, _) in _reg_captcha_challenges.items() if expires_at <= now]
    for token in expired:
        del _reg_captcha_challenges[token]


def _issue_registration_captcha() -> tuple[str, str]:
    '''Create a simple arithmetic captcha challenge and return its token/question.'''

    _prune_captcha_challenges()
    token = secrets.token_urlsafe(16)
    left = secrets.randbelow(9) + 1
    right = secrets.randbelow(9) + 1
    answer = left + right
    _reg_captcha_challenges[token] = (time.time() + _CAPTCHA_TTL, answer)
    return token, f'{left} + {right}'


def _validate_registration_captcha(token: str | None, answer: str | None) -> bool:
    '''Validate the captcha token from the form. The challenge is single-use.'''

    if not token or not answer:
        return False

    _prune_captcha_challenges()
    challenge = _reg_captcha_challenges.pop(token, None)
    if challenge is None:
        return False

    expires_at, expected = challenge
    if time.time() > expires_at:
        return False

    try:
        provided = int(str(answer).strip())
    except ValueError:
        return False

    return provided == expected


def _render_register_page(request: Request, error: str | None = None, *, status_code: int = 200):
    '''Render the registration form with a fresh captcha challenge.'''

    captcha_token, captcha_question = _issue_registration_captcha()
    response = templates.TemplateResponse(request, 'register.html', {
        'trial_tokens_fmt': _fmt_tokens(TRIAL_TOKENS),
        'trial_expiry_days': TRIAL_EXPIRY_DAYS,
        'error': error,
        'captcha_token': captcha_token,
        'captcha_question': captcha_question,
    }, status_code=status_code)
    response.set_cookie(
        key=_CAPTCHA_COOKIE_NAME,
        value=captcha_token,
        httponly=True,
        samesite='lax',
        max_age=_CAPTCHA_TTL,
        path='/',
    )
    return response


def _check_ip_inference_rate_limit(ip: str) -> bool:
    '''Check per-IP rate limit for inference requests.'''
    return _check_rate_limit(_ip_inf_attempts, ip, INF_IP_LIMIT, INF_IP_WINDOW)


def _check_user_inference_rate_limit(prefix: str) -> bool:
    '''Check per-user rate limit for inference requests (keyed by API prefix).'''
    return _check_rate_limit(_user_inf_attempts, prefix, INF_USER_LIMIT, INF_USER_WINDOW)


def _fmt_tokens(n: int) -> str:
    '''Format a token count as a compact human-readable string.

    Args:
        n: Token count.

    Returns:
        A string such as "1.2M", "500k", or the raw number as a string.
    '''

    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"

    return str(n)

async_session = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    '''Initialize the database on startup.'''

    await init_db()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


# ── Auth ─────────────────────────────────────────────────────────────────────

async def authenticate(request: Request) -> tuple[User, AsyncSession] | None:
    '''Validate the Bearer token from the request Authorization header.

    Args:
        request: Incoming FastAPI request.

    Returns:
        (user, session) if the key is valid, None otherwise.
        The session is left open for the caller to commit and close.
    '''

    auth = request.headers.get('Authorization', '')

    if not auth.startswith('Bearer '):
        return None

    raw_key = auth.removeprefix('Bearer ').strip()

    if not raw_key.startswith('sk-') or len(raw_key) < 12:
        return None

    prefix = raw_key[:12]
    session = async_session()
    user = await get_user_by_key_prefix(session, prefix)

    if user is None or not verify_api_key(raw_key, user.api_key_hash):
        await session.close()
        return None

    return user, session


# ── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    '''Liveness check; always returns 200 if the process is up.'''

    return {'status': 'ok'}


# ── Registration ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    '''Render the landing page.'''

    return templates.TemplateResponse(request, 'index.html', {'key_error': None})


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    '''Render the registration form.'''

    return _render_register_page(request)


@app.post("/register", response_class=HTMLResponse)
async def register_submit(
    request: Request,
    email: str = Form(...),
    captcha_token: str | None = Form(default=None),
    captcha_answer: str | None = Form(default=None),
):
    '''Handle registration form submission. Creates a new user and sends a trial key email.'''

    client_ip = _client_ip(request) or 'unknown'

    if not _check_reg_rate_limit(client_ip) or not _check_reg_burst_rate_limit(client_ip):
        log.warning('Registration rate limit hit from %s', client_ip)
        return _render_register_page(request, 'Too many attempts. Please try again later.', status_code=429)

    if not _validate_registration_captcha(captcha_token, captcha_answer):
        log.warning('Registration captcha failed from %s', client_ip)
        return _render_register_page(request, 'Please solve the captcha correctly.', status_code=400)

    email = email.strip().lower()
    if not _is_valid_email(email):
        log.warning('Registration rejected invalid email from %s: %r', client_ip, email)
        return _render_register_page(request, 'Please enter a valid email address.', status_code=400)

    returning = False

    async with async_session() as session:
        existing = (await session.execute(
            select(User).where(User.email == email)
        )).scalar_one_or_none()

        if existing is None:
            raw_key, key_hash, prefix = generate_api_key()
            user = User(
                email=email,
                api_key_hash=key_hash,
                api_key_prefix=prefix,
                balance_tokens=0,
            )
            session.add(user)
            await session.flush()  # get user.id

            expires_at = datetime.now(timezone.utc) + timedelta(days=TRIAL_EXPIRY_DAYS)
            trial = TrialTokens(
                user_id=user.id,
                tokens_granted=TRIAL_TOKENS,
                remaining_tokens=TRIAL_TOKENS,
                expires_at=expires_at,
            )
            session.add(trial)
            await session.commit()

        else:
            # Existing user: issue a new key (old one is invalidated), no token change.
            returning = True
            raw_key, key_hash, prefix = generate_api_key()
            existing.api_key_hash = key_hash
            existing.api_key_prefix = prefix
            await session.commit()

        # Send email outside the transaction - failure here is non-fatal
        try:
            await send_trial_key_email(email, raw_key)
        except (aiosmtplib.SMTPException, OSError):
            log.exception("Failed to send trial key email to %s", email)

    log.info('Registration succeeded for %s from %s', email, client_ip)

    return templates.TemplateResponse(request, 'registered.html', {
        'email': email,
        'returning': returning,
        'trial_tokens_fmt': _fmt_tokens(TRIAL_TOKENS),
        'trial_expiry_days': TRIAL_EXPIRY_DAYS,
    })


# ── Dashboard ────────────────────────────────────────────────────────────────

PACKS = [
    {'id': '5m',   'label': '5M',   'tokens': 5_000_000,   'price': '0.50', 'cpp': '0.10'},
    {'id': '25m',  'label': '25M',  'tokens': 25_000_000,  'price': '2.00', 'cpp': '0.08'},
    {'id': '100m', 'label': '100M', 'tokens': 100_000_000, 'price': '6.00', 'cpp': '0.06'},
]


# User dashboard sessions - in-memory only, same single-instance assumption
# as the admin sessions below. The API key is accepted once (via the
# ?key= login link) to establish a session; it is never carried in
# subsequent URLs, forms, or provider redirect URLs.
_user_sessions: dict[str, tuple[int, float]] = {}  # token -> (user_id, expiry)
_DASH_SESSION_COOKIE = 'dashboard_session'
_DASH_SESSION_TTL = int(os.environ.get('DASHBOARD_SESSION_TTL_SECONDS', str(12 * 3600)))

# One-time tokens embedded in payment-provider return URLs (Stripe
# success/cancel, BTCPay redirect). Consumed on first use; short-lived.
_checkout_returns: dict[str, tuple[int, float]] = {}  # token -> (user_id, expiry)
_CHECKOUT_RETURN_TTL = 3600


def _prune_user_sessions(now: float | None = None) -> None:
    '''Remove expired dashboard session and checkout-return tokens.'''

    now = time.time() if now is None else now

    for store in (_user_sessions, _checkout_returns):
        for t in [t for t, (_, exp) in store.items() if exp <= now]:
            del store[t]


def _issue_user_session(user_id: int) -> str:
    '''Create a new dashboard session for the given user and return its token.'''

    _prune_user_sessions()
    token = secrets.token_urlsafe(32)
    _user_sessions[token] = (user_id, time.time() + _DASH_SESSION_TTL)

    return token


def _check_user_session(token: str | None) -> int | None:
    '''Return the user_id for a valid dashboard session token, else None.'''

    if not token:
        return None

    _prune_user_sessions()

    entry = _user_sessions.get(token)

    if entry is None:
        return None

    return entry[0]


def _revoke_user_session(token: str | None) -> None:
    '''Remove a dashboard session token, e.g. on logout.'''

    if token:
        _user_sessions.pop(token, None)


def _issue_checkout_return(user_id: int) -> str:
    '''Create a one-time token for a payment-provider return URL.'''

    _prune_user_sessions()
    token = secrets.token_urlsafe(32)
    _checkout_returns[token] = (user_id, time.time() + _CHECKOUT_RETURN_TTL)

    return token


def _consume_checkout_return(token: str | None) -> int | None:
    '''Consume a checkout-return token; returns the user_id or None.

    Tokens are single-use: a consumed token is deleted, so a stale or
    replayed return URL cannot re-open a session.
    '''

    if not token:
        return None

    _prune_user_sessions()
    entry = _checkout_returns.pop(token, None)

    if entry is None:
        return None

    return entry[0]


def _set_user_session_cookie(response: Response, token: str) -> None:
    '''Attach the dashboard session cookie to a response.

    No explicit `secure` flag: matches the existing captcha cookie convention
    so the cookie still works in tests (http://testserver); the production
    domain is HTTPS-only end to end via nginx regardless.
    '''

    response.set_cookie(
        key=_DASH_SESSION_COOKIE,
        value=token,
        max_age=_DASH_SESSION_TTL,
        httponly=True,
        samesite='lax',
        path='/',
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, key: str | None = Query(default=None)):
    '''Render the user dashboard: balance, trial status, 30-day usage, and billing options.

    Authentication: either a one-time `?key=` login (the API key is
    validated once and a dashboard session cookie is established, then the
    user is redirected to a key-free URL) or an existing dashboard session
    cookie. The API key is never required in the URL after the initial
    login.
    '''

    if key is not None:
        # One-time key login: validate, establish a session, redirect clean.
        user_id: int | None = None

        if key.startswith('sk-') and len(key) >= 12:
            prefix = key[:12]

            async with async_session() as session:
                user = await get_user_by_key_prefix(session, prefix)
                user_id = user.id if (
                    user is not None and verify_api_key(key, user.api_key_hash)
                ) else None

        if user_id is None:
            return RedirectResponse('/', status_code=303)

        token = _issue_user_session(user_id)
        response = RedirectResponse('/dashboard', status_code=303)
        _set_user_session_cookie(response, token)
        return response

    user_id = _check_user_session(request.cookies.get(_DASH_SESSION_COOKIE))

    if user_id is None:
        return RedirectResponse('/', status_code=303)

    async with async_session() as session:
        user = (await session.execute(
            select(User).where(User.id == user_id)
        )).scalar_one_or_none()

        if user is None:
            return RedirectResponse('/', status_code=303)

        now = datetime.now(timezone.utc)
        thirty_days_ago = now - timedelta(days=30)

        # Active trial info
        trial_result = await session.execute(
            sql_text(
                "SELECT remaining_tokens, expires_at FROM trial_tokens "
                "WHERE user_id = :uid AND expires_at > :now AND remaining_tokens > 0 "
                "ORDER BY expires_at ASC LIMIT 1"
            ),
            {"uid": user.id, "now": now},
        )

        trial_row = trial_result.mappings().first()
        trial_remaining = trial_row["remaining_tokens"] if trial_row else 0
        trial_expires_raw = trial_row["expires_at"] if trial_row else None
    
        # SQLite returns datetimes as strings; PostgreSQL returns datetime objects
        if isinstance(trial_expires_raw, str):
            trial_expires_dt: datetime | None = datetime.fromisoformat(trial_expires_raw).replace(
                tzinfo=timezone.utc
            )
        else:
            trial_expires_dt = trial_expires_raw

        trial_days_left = (trial_expires_dt - now).days if trial_expires_dt else 0

        # Total available balance
        total_balance = user.balance_tokens + trial_remaining

        # 30-day usage summary
        summary = await session.execute(
            sql_text(
                "SELECT COALESCE(SUM(input_tokens),0) AS inp, "
                "COALESCE(SUM(output_tokens),0) AS out, "
                "COUNT(*) AS reqs "
                "FROM usage_events "
                "WHERE user_id = :uid AND timestamp >= :since"
            ),
            {"uid": user.id, "since": thirty_days_ago},
        )

        s = summary.mappings().first()
        assert s is not None  # aggregate SELECT without GROUP BY always returns one row
        used_in, used_out, req_count = s["inp"], s["out"], s["reqs"]

        # Daily usage for bar chart
        # DATE() is portable across SQLite and PostgreSQL when timestamps are UTC
        from app.db import _is_sqlite as _sqlite

        _date_expr = "DATE(timestamp)" if _sqlite else "DATE(timestamp AT TIME ZONE 'UTC')"

        daily = await session.execute(
            sql_text(
                f"SELECT {_date_expr} AS day, "
                "SUM(input_tokens) AS inp, SUM(output_tokens) AS out "
                "FROM usage_events "
                "WHERE user_id = :uid AND timestamp >= :since "
                "GROUP BY day ORDER BY day DESC LIMIT 30"
            ),
            {"uid": user.id, "since": thirty_days_ago},
        )

        daily_rows = daily.mappings().all()
        max_total = max((r["inp"] + r["out"] for r in daily_rows), default=1)
    
        daily_usage = [
            {
        'date': str(r['day'])[5:],  # MM-DD
                'input_pct': round(r['inp'] / max_total * 100),
                'output_pct': round(r['out'] / max_total * 100),
                'total_fmt': _fmt_tokens(r['inp'] + r['out']),
            }
            for r in daily_rows
        ]

    return templates.TemplateResponse(request, 'dashboard.html', {
        'email': user.email,
        'api_key_prefix': user.api_key_prefix,
        'total_balance': total_balance,
        'total_balance_fmt': _fmt_tokens(total_balance),
        'trial_remaining': trial_remaining,
        'trial_remaining_fmt': _fmt_tokens(trial_remaining),
        'trial_expires': trial_expires_dt.strftime('%b %d') if trial_expires_dt else '',
        'trial_days_left': trial_days_left,
        'used_30d_fmt': _fmt_tokens(used_in + used_out),
        'used_30d_input_fmt': _fmt_tokens(used_in),
        'used_30d_output_fmt': _fmt_tokens(used_out),
        'request_count_30d': req_count,
        'daily_usage': daily_usage,
        'packs': PACKS,
        'payment_success': request.query_params.get('payment') == 'success',
    })


@app.post("/dashboard/logout")
async def dashboard_logout(request: Request):
    '''End the dashboard session and redirect to the landing page.'''

    _revoke_user_session(request.cookies.get(_DASH_SESSION_COOKIE))
    response = RedirectResponse('/', status_code=303)
    response.delete_cookie(_DASH_SESSION_COOKIE, path='/')
    return response


# ── Billing ──────────────────────────────────────────────────────────────────

@app.post("/checkout")
async def checkout_redirect(
    request: Request,
    pack: str = Form(...),
    method: str = Form(default='stripe'),   # 'stripe' | 'btcpay'
):
    '''Create a payment session for the dashboard-logged-in user and redirect
    to the provider.

    Auth is the dashboard session cookie; the API key is never transmitted
    or embedded in any URL. Provider return URLs carry a short-lived
    one-time token (consumed at /checkout/return) instead.
    '''

    user_id = _check_user_session(request.cookies.get(_DASH_SESSION_COOKIE))

    if user_id is None:
        return RedirectResponse('/', status_code=303)

    async with async_session() as session:
        user = (await session.execute(
            select(User).where(User.id == user_id)
        )).scalar_one_or_none()

        if user is None:
            return RedirectResponse('/', status_code=303)

        user_email = user.email

    base_url = os.environ.get('BASE_URL', '').rstrip('/')

    if not base_url:
        log.error('Checkout requested but BASE_URL is not set')
        return JSONResponse(
            status_code=503,
            content={'error': 'Payment system not configured.'}
        )

    return_token = _issue_checkout_return(user_id)
    base_return = f'{base_url}/checkout/return?token={return_token}'

    try:
        if method == 'btcpay':
            _, checkout_url = await create_invoice(
                pack_id=pack,
                user_id=user_id,
                user_email=user_email,
                redirect_url=f'{base_return}&status=success',
            )
        else:
            checkout_url = await create_checkout_session(
                pack_id=pack,
                user_id=user_id,
                user_email=user_email,
                success_url=f'{base_return}&status=success',
                cancel_url=base_return,
            )
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={'error': str(exc)}
        )

    except RuntimeError as exc:
        log.error("Payment provider not configured (%s): %s", method, exc)

        return JSONResponse(
            status_code=503,
            content={'error': 'Payment system not configured.'}
        )

    except (httpx.HTTPError, stripe.StripeError):
        log.exception("Checkout creation failed (method=%s)", method)

        return JSONResponse(
            status_code=502,
            content={'error': 'Could not create checkout session.'}
        )

    return RedirectResponse(checkout_url, status_code=303)


@app.get("/checkout/return")
async def checkout_return(
    request: Request,
    token: str = Query(...),
    status: str = Query(default=''),
):
    '''Landing route after a payment provider redirects back.

    Consumes the one-time return token, re-establishes the dashboard
    session, and forwards to the dashboard (with a payment-success flash
    when the provider reports success). Actual token crediting happens
    asynchronously in the provider webhook.
    '''

    user_id = _consume_checkout_return(token)

    if user_id is None:
        return RedirectResponse('/', status_code=303)

    session_token = _issue_user_session(user_id)
    target = '/dashboard?payment=success' if status == 'success' else '/dashboard'
    response = RedirectResponse(target, status_code=303)
    _set_user_session_cookie(response, session_token)
    return response


@app.post("/btcpay/webhook")
async def btcpay_webhook(request: Request):
    '''Receive and verify BTCPay webhooks; credit balance on InvoiceSettled.'''

    payload = await request.body()
    sig = request.headers.get('BTCPay-Sig', '')

    try:
        event = btcpay_verify_webhook(payload, sig)

    except ValueError:
        return JSONResponse(status_code=400, content={'error': 'invalid signature'})

    except RuntimeError as exc:
        log.error("BTCPay webhook: %s", exc)
    
        return JSONResponse(status_code=503, content={'error': 'not configured'})

    if event.get('type') == 'InvoiceSettled':
        await _fulfill_btcpay(event)

    return {'ok': True}


async def _fulfill_btcpay(event: dict) -> None:
    '''Credit user balance for a settled BTCPay invoice.

    Idempotent via the payment_ref unique constraint.

    Args:
        event: Parsed BTCPay webhook event dict (type: InvoiceSettled).
    '''

    try:
        metadata = event['metadata']
        invoice_id = event['invoiceId']
        user_id = int(metadata['user_id'])
        tokens = int(metadata['tokens'])
        pack_id = metadata.get('pack_id', '')
    
        # Derive amount_cents from pack catalog
        from .btcpay import PACK_CATALOG as _BTC_PACKS
        amount_cents = int(float(_BTC_PACKS.get(pack_id, {}).get('price_usd', '0')) * 100)
    
    except (KeyError, ValueError):
        log.error("BTCPay webhook: malformed event: %s", event.get('invoiceId'))
    
        return

    async with async_session() as db:
        user = await db.get(User, user_id)

        if user is None:
            log.error("BTCPay webhook: user_id %s not found", user_id)

            return

        existing = (await db.execute(
            select(TokenPurchase).where(TokenPurchase.payment_ref == invoice_id)
        )).scalar_one_or_none()

        if existing is not None:
            return

        user.balance_tokens += tokens
    
        db.add(TokenPurchase(
            user_id=user_id,
            payment_method='btcpay',
            payment_ref=invoice_id,
            tokens_added=tokens,
            amount_cents=amount_cents,
        ))

        await db.commit()
        log.info("BTCPay: credited %s tokens to user %s (invoice %s)", tokens, user_id, invoice_id)



@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    '''Receive and verify Stripe events; credit token balance on successful payment.'''

    payload = await request.body()
    sig = request.headers.get('stripe-signature', '')

    try:
        event = verify_webhook(payload, sig)

    except stripe.SignatureVerificationError:
        return JSONResponse(status_code=400, content={'error': 'invalid signature'})

    except RuntimeError as exc:
        log.error("Stripe webhook: %s", exc)

        return JSONResponse(status_code=503, content={'error': 'not configured'})

    if event['type'] == 'checkout.session.completed':
        obj = event['data']['object']

        if obj.get('payment_status') == 'paid':
            await _fulfill_checkout(obj)

    return {'ok': True}


async def _fulfill_checkout(session_obj: dict) -> None:
    '''Credit the user's balance after a completed Stripe Checkout Session.

    Idempotent via the unique constraint on payment_ref (Stripe session id).

    Args:
        session_obj: The Stripe checkout.session object from the webhook event.
    '''

    try:
        user_id = int(session_obj['metadata']['user_id'])
        tokens = int(session_obj['metadata']['tokens'])
        amount_cents = int(session_obj.get('amount_total') or 0)
        session_id = session_obj['id']  # cs_xxx - used as idempotency key

    except (KeyError, ValueError):
        log.error("Stripe webhook: malformed session object: %s", session_obj.get('id'))

        return

    async with async_session() as db:
        user = await db.get(User, user_id)

        if user is None:
            log.error("Stripe webhook: user_id %s not found", user_id)

            return

        # Idempotency - skip if this session was already fulfilled
        existing = (await db.execute(
            select(TokenPurchase).where(TokenPurchase.payment_ref == session_id)
        )).scalar_one_or_none()

        if existing is not None:
            return

        user.balance_tokens += tokens

        db.add(TokenPurchase(
            user_id=user_id,
            payment_method='stripe',
            payment_ref=session_id,
            tokens_added=tokens,
            amount_cents=amount_cents,
        ))

        await db.commit()
        log.info("Credited %s tokens to user %s (session %s)", tokens, user_id, session_id)


# ── Responses API shim ──────────────────────────────────────────────────────

def _responses_error(message: str, status_code: int = 400) -> JSONResponse:
    '''Return an OpenAI-style error payload for /v1/responses validation errors.'''

    return JSONResponse(
        status_code=status_code,
        content={'error': {'message': message, 'type': 'invalid_request_error'}},
    )


def _response_object_id(chat_id: str | None) -> str:
    '''Return a stable response id derived from the upstream chat completion id.'''

    if chat_id:
        return chat_id.replace('chatcmpl', 'resp', 1)

    return f'resp_{int(time.time() * 1000)}'


def _message_content_to_text(content: str | list | dict | None) -> str:
    '''Extract plain text content from a supported Responses API content value.'''

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                raise ValueError('Promptly /v1/responses currently supports text-only content.')

            item_type = item.get('type')
            if item_type in ('input_text', 'output_text', 'text'):
                text_value = item.get('text')
                if not isinstance(text_value, str):
                    raise ValueError('Text content items must include a string `text` field.')
                parts.append(text_value)
                continue

            raise ValueError(
                'Promptly /v1/responses currently supports text-only requests. '
                'Multimodal inputs and tools are not supported.'
            )

        return ''.join(parts)

    raise ValueError('Promptly /v1/responses currently supports text-only content.')


def _responses_input_to_messages(input_value: str | list) -> list[dict[str, str]]:
    '''Convert a supported Responses API input shape into chat completion messages.'''

    if isinstance(input_value, str):
        return [{'role': 'user', 'content': input_value}]

    if not isinstance(input_value, list):
        raise ValueError('`input` must be a string or a list of text messages.')

    messages: list[dict[str, str]] = []
    for item in input_value:
        if not isinstance(item, dict):
            raise ValueError('Each item in `input` must be an object.')

        item_type = item.get('type')
        if item_type and item_type != 'message':
            raise ValueError(
                'Promptly /v1/responses currently supports message-style text input only.'
            )

        role = item.get('role', 'user')
        if role not in ('system', 'developer', 'user', 'assistant'):
            raise ValueError(f'Unsupported message role: {role}')

        content_text = _message_content_to_text(item.get('content'))
        messages.append({'role': role, 'content': content_text})

    return messages


def _responses_to_chat_body(body: dict) -> dict:
    '''Translate a supported Responses API request into a chat completions request.'''

    unsupported_fields = [
        field for field in ('tools', 'tool_choice', 'parallel_tool_calls', 'modalities')
        if body.get(field)
    ]
    if unsupported_fields:
        raise ValueError(
            'Promptly /v1/responses currently supports text-only requests. '
            f'Unsupported fields: {", ".join(unsupported_fields)}.'
        )

    if 'input' not in body:
        raise ValueError('`input` is required.')

    messages: list[dict[str, str]] = []
    instructions = body.get('instructions')
    if instructions is not None:
        if not isinstance(instructions, str):
            raise ValueError('`instructions` must be a string.')
        messages.append({'role': 'system', 'content': instructions})

    messages.extend(_responses_input_to_messages(body['input']))

    chat_body = {
        'model': body.get('model', 'default'),
        'messages': messages,
        'stream': bool(body.get('stream', False)),
    }

    if 'temperature' in body:
        chat_body['temperature'] = body['temperature']
    if 'max_output_tokens' in body:
        chat_body['max_tokens'] = body['max_output_tokens']

    return chat_body


def _chat_to_responses_payload(data: dict) -> dict:
    '''Translate a chat completion response into a minimal Responses API payload.'''

    choice = (data.get('choices') or [{}])[0]
    message = choice.get('message') or {}
    text_content = message.get('content', '') or ''
    response_id = _response_object_id(data.get('id'))

    return {
        'id': response_id,
        'object': 'response',
        'created_at': data.get('created'),
        'model': data.get('model'),
        'status': 'completed',
        'output': [{
            'id': f'msg_{response_id}',
            'type': 'message',
            'role': 'assistant',
            'content': [{
                'type': 'output_text',
                'text': text_content,
            }],
        }],
        'output_text': text_content,
        'usage': {
            'input_tokens': (data.get('usage') or {}).get('prompt_tokens', 0),
            'output_tokens': (data.get('usage') or {}).get('completion_tokens', 0),
            'total_tokens': (data.get('usage') or {}).get('total_tokens', 0),
        },
    }


def _parse_chat_stream_chunk(chunk: bytes) -> tuple[str | None, str | None]:
    '''Extract a text delta and model name from a chat-completions SSE chunk.'''

    try:
        chunk_text = chunk.decode('utf-8', errors='ignore')
    except UnicodeDecodeError:
        return None, None

    delta_text = None
    model_name = None
    for line in chunk_text.splitlines():
        if not line.startswith('data:'):
            continue
        payload = line[5:].strip()
        if payload == '[DONE]':
            continue
        try:
            data = json.loads(payload)
        except ValueError:
            continue

        model_name = data.get('model') or model_name
        choices = data.get('choices') or []
        if not choices:
            continue
        delta = choices[0].get('delta') or {}
        if isinstance(delta.get('content'), str):
            delta_text = delta['content']

    return delta_text, model_name


def _responses_sse(payload: dict) -> bytes:
    '''Encode a Responses API event payload as SSE bytes.'''

    return f'data: {json.dumps(payload)}\n\n'.encode()


@app.post('/v1/responses')
async def responses_api(request: Request):
    '''Text-only shim for the OpenAI Responses API backed by chat completions.'''

    client_ip = _client_ip(request)

    if not _check_ip_inference_rate_limit(client_ip):
        return JSONResponse(
            status_code=429,
            content={'error': {
                'message': 'Too many requests from your IP.',
                'type': 'rate_limit_error'
            }},
        )

    auth = await authenticate(request)
    if auth is None:
        return JSONResponse(
            status_code=401,
            content={'error': {'message': 'Invalid or missing API key.', 'type': 'auth_error'}},
        )
    user, session = auth

    if not _check_user_inference_rate_limit(user.api_key_prefix):
        await session.close()
        return JSONResponse(
            status_code=429,
            content={'error': {
                'message': 'Rate limit exceeded. Slow down and try again.',
                'type': 'rate_limit_error'
            }},
        )

    try:
        body = await request.json()
    except ValueError:
        await session.close()
        return _responses_error('Invalid JSON body.')

    if not isinstance(body, dict):
        await session.close()
        return _responses_error('JSON body must be an object.')

    try:
        chat_body = _responses_to_chat_body(body)
    except ValueError as exc:
        await session.close()
        return _responses_error(str(exc))

    if _MODEL_ALLOWLIST and chat_body.get('model') not in _MODEL_ALLOWLIST:
        await session.close()
        return _responses_error(
            f"Model {chat_body.get('model')!r} is not available. Available models: "
            + ', '.join(sorted(_MODEL_ALLOWLIST))
        )

    async with session:
        available = await total_available_tokens(session, user)
        if available <= 0:
            return JSONResponse(
                status_code=402,
                content={
                    'error': {
                        'message': 'Insufficient token balance. Top-ups are coming soon; '
                                   f'manage your account at {os.environ.get("BASE_URL", "")}/dashboard',
                        'type': 'insufficient_quota',
                    }
                },
            )

        headers = dict(request.headers)

        if chat_body.get('stream'):
            response_id = f'resp_{int(time.time() * 1000)}'
            created_at = int(time.time())
            final_model = chat_body.get('model', 'default')
            deltas: list[str] = []
            final_input_tokens = 0
            final_output_tokens = 0

            async def generate():
                nonlocal final_model, final_input_tokens, final_output_tokens

                yield _responses_sse({
                    'type': 'response.created',
                    'response': {
                        'id': response_id,
                        'object': 'response',
                        'created_at': created_at,
                        'model': final_model,
                        'status': 'in_progress',
                    },
                })

                async for chunk, in_tok, out_tok in proxy_stream(
                    'POST', '/chat/completions', headers, chat_body,
                ):
                    if in_tok or out_tok:
                        final_input_tokens, final_output_tokens = in_tok, out_tok
                        continue

                    delta_text, model_name = _parse_chat_stream_chunk(chunk)
                    if model_name:
                        final_model = model_name
                    if delta_text:
                        deltas.append(delta_text)
                        yield _responses_sse({
                            'type': 'response.output_text.delta',
                            'delta': delta_text,
                        })

                final_text = ''.join(deltas)
                yield _responses_sse({
                    'type': 'response.output_text.done',
                    'text': final_text,
                })

                yield _responses_sse({
                    'type': 'response.completed',
                    'response': {
                        'id': response_id,
                        'object': 'response',
                        'created_at': created_at,
                        'model': final_model,
                        'status': 'completed',
                        'output': [{
                            'id': f'msg_{response_id}',
                            'type': 'message',
                            'role': 'assistant',
                            'content': [{
                                'type': 'output_text',
                                'text': final_text,
                            }],
                        }],
                        'output_text': final_text,
                        'usage': {
                            'input_tokens': final_input_tokens,
                            'output_tokens': final_output_tokens,
                            'total_tokens': final_input_tokens + final_output_tokens,
                        },
                    },
                })

                await _record_usage(session, user, final_input_tokens, final_output_tokens, chat_body)
                await session.close()

            return StreamingResponse(generate(), media_type='text/event-stream')

        upstream, input_tokens, output_tokens = await proxy_request(
            'POST', '/chat/completions', headers, chat_body,
        )

        if upstream.status_code >= 400:
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                headers=dict(upstream.headers),
                media_type=upstream.headers.get('content-type'),
            )

        await _record_usage(session, user, input_tokens, output_tokens, chat_body)

        try:
            chat_data = upstream.json()
        except ValueError:
            return JSONResponse(
                status_code=502,
                content={'error': {'message': 'Upstream returned invalid JSON.'}},
            )

        return JSONResponse(_chat_to_responses_payload(chat_data))


# ── Inference proxy ──────────────────────────────────────────────────────────

@app.api_route('/v1/{path:path}', methods=['GET', 'POST', 'DELETE', 'OPTIONS'])
async def proxy(request: Request, path: str):
    '''Authenticate, check balance, and proxy the request to llama-server.'''

    client_ip = _client_ip(request)

    if not _check_ip_inference_rate_limit(client_ip):
        return JSONResponse(
            status_code=429,
            content={'error': {
                'message': 'Too many requests from your IP.',
                'type': 'rate_limit_error'
            }},
        )

    auth = await authenticate(request)

    if auth is None:
        return JSONResponse(
            status_code=401,
            content={'error': {'message': 'Invalid or missing API key.', 'type': 'auth_error'}},
        )
    user, session = auth

    if not _check_user_inference_rate_limit(user.api_key_prefix):
        await session.close()

        return JSONResponse(
            status_code=429,
            content={'error': {
                'message': 'Rate limit exceeded. Slow down and try again.',
                'type': 'rate_limit_error'
            }},
        )

    async with session:
        available = await total_available_tokens(session, user)

        if available <= 0:

            return JSONResponse(
                status_code=402,
                content={
                    'error': {
                        'message': 'Insufficient token balance. Top-ups are coming soon; '
                               f'manage your account at {os.environ.get("BASE_URL", "")}/dashboard',
                        'type': 'insufficient_quota',
                    }
                },
            )

        body: dict | None = None

        if request.method == 'POST':
            try:
                body = await request.json()

            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={'error': {'message': 'Invalid JSON body.'}}
                )

        forward_path = f"/{path}"
        method = request.method
        headers = dict(request.headers)

        # Chat completions: default a missing/empty model to the pinned
        # default alias, and (if MODEL_ALLOWLIST is configured) reject
        # models outside the public allow-list with a clear 400.
        if (
            request.method == 'POST'
            and isinstance(body, dict)
            and forward_path.lstrip('/').startswith('chat/completions')
        ):
            model = body.get('model')
            if not isinstance(model, str) or not model:
                model = DEFAULT_MODEL

            if _MODEL_ALLOWLIST and model not in _MODEL_ALLOWLIST:
                return JSONResponse(
                    status_code=400,
                    content={'error': {
                        'message': (
                            f"Model {model!r} is not available. Available models: "
                            + ', '.join(sorted(_MODEL_ALLOWLIST))
                        ),
                        'type': 'invalid_request_error',
                    }},
                )

            body['model'] = model

        if isinstance(body, dict) and body.get('stream', False):
            return await _handle_stream(session, user, method, forward_path, headers, body)

        return await _handle_standard(session, user, method, forward_path, headers, body)


async def _handle_standard(
    session: AsyncSession,
    user: User,
    method: str,
    path: str,
    headers: dict,
    body: dict | None,
) -> Response:
    '''Handle a non-streaming request: proxy to llama-server,
    record usage, and return the response.'''

    response, input_tokens, output_tokens = await proxy_request(method, path, headers, body)

    await _record_usage(session, user, input_tokens, output_tokens, body)

    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.headers.get('content-type'),
    )


async def _handle_stream(
    session: AsyncSession,
    user: User,
    method: str,
    path: str,
    headers: dict,
    body: dict,
) -> StreamingResponse:
    '''Handle a streaming request: proxy to llama-server, stream the response,
    and record usage after the stream ends.'''

    final_input = 0
    final_output = 0

    async def generate():
        '''Stream chunks from llama-server as they arrive, and capture the final
        token counts for usage recording after the stream ends.'''

        nonlocal final_input, final_output

        async for chunk, in_tok, out_tok in proxy_stream(method, path, headers, body):
            if in_tok or out_tok:
                final_input, final_output = in_tok, out_tok

            if chunk:
                yield chunk

        # After stream ends, record usage
        await _record_usage(session, user, final_input, final_output, body)
        await session.close()

    return StreamingResponse(generate(), media_type="text/event-stream")


async def _record_usage(
    session: AsyncSession,
    user: User,
    input_tokens: int,
    output_tokens: int,
    body: dict | None,
) -> None:
    '''Write a UsageEvent row and deduct tokens from the user's balance.

    No prompt or response content is stored - only token counts.

    Args:
        session: Active SQLAlchemy async session.
        user: The authenticated User ORM object.
        input_tokens: Number of prompt tokens consumed.
        output_tokens: Number of completion tokens consumed.
        body: Original request body (used only to extract the model name).
    '''

    total = input_tokens + output_tokens

    if total <= 0:
        return

    model = (body or {}).get('model', '')

    event = UsageEvent(
        user_id=user.id,
        timestamp=datetime.now(timezone.utc),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model=model,
    )

    session.add(event)
    await deduct_tokens(session, user, total)
    await session.commit()


# ── Admin panel ──────────────────────────────────────────────────────────────

def _admin_key() -> str:
    '''Return the ADMIN_KEY env var, or empty string if unset.'''

    return os.environ.get('ADMIN_KEY', '')


# CIDRs allowed to reach /admin - tailnet + localhost + the Docker NAT
# source for operator access from the gateway host (Docker allocates
# user-defined networks from 172.16.0.0/12, typically 172.17.0.0/16; if
# this stack's network got a different subnet, set ADMIN_ALLOWED_CIDRS).
# Override via ADMIN_ALLOWED_CIDRS env var (comma-separated) if needed.
_DEFAULT_ADMIN_CIDRS = ["100.64.0.0/10", "127.0.0.1/32", "::1/128", "172.17.0.0/16"]

def _admin_allowed_cidrs() -> list[str]:
    '''Return the list of CIDRs permitted to access the admin panel.

    Returns:
        CIDRs from ADMIN_ALLOWED_CIDRS env var (comma-separated), or the
        default set covering tailnet, localhost, and Docker bridge.
    '''

    raw = os.environ.get('ADMIN_ALLOWED_CIDRS', '')

    if raw:
        return [c.strip() for c in raw.split(',') if c.strip()]

    return _DEFAULT_ADMIN_CIDRS


def _check_admin_ip(request: Request) -> bool:
    '''Return True if the request IP falls within an allowed admin CIDR.

    Args:
        request: Incoming FastAPI request.

    Returns:
        True if the client IP is within any of the allowed CIDRs.
    '''

    client_ip = _client_ip(request)

    try:
        addr = ip_address(client_ip)

    except ValueError:
        return False

    return any(addr in ip_network(cidr, strict=False) for cidr in _admin_allowed_cidrs())


def _check_admin(key: str) -> bool:
    '''Verify an admin key using a constant-time comparison.

    Args:
        key: Key string supplied by the caller.

    Returns:
        True if key matches ADMIN_KEY, False otherwise or if ADMIN_KEY is unset.
    '''

    expected = _admin_key()

    if not expected:
        return False

    return secrets.compare_digest(key.encode(), expected.encode())


# Admin sessions - in-memory only, so a restart or multi-replica deployment
# invalidates/won't-share sessions. Acceptable for this single-instance gateway.
_admin_sessions: dict[str, float] = {}
_ADMIN_SESSION_COOKIE = 'admin_session'
_ADMIN_SESSION_TTL = int(os.environ.get('ADMIN_SESSION_TTL_SECONDS', str(12 * 3600)))


def _prune_admin_sessions(now: float | None = None) -> None:
    '''Remove expired admin session tokens.'''

    now = time.time() if now is None else now
    expired = [t for t, exp in _admin_sessions.items() if exp <= now]

    for t in expired:
        del _admin_sessions[t]


def _issue_admin_session() -> str:
    '''Create a new admin session token and return it.'''

    _prune_admin_sessions()
    token = secrets.token_urlsafe(32)
    _admin_sessions[token] = time.time() + _ADMIN_SESSION_TTL

    return token


def _check_admin_session(token: str | None) -> bool:
    '''Return True if the given admin session token is valid and unexpired.'''

    if not token:
        return False

    _prune_admin_sessions()

    return _admin_sessions.get(token, 0) > time.time()


def _revoke_admin_session(token: str | None) -> None:
    '''Remove a session token, e.g. on logout.'''

    if token:
        _admin_sessions.pop(token, None)


def _set_admin_session_cookie(response: Response, token: str) -> None:
    '''Attach the admin session cookie to a response.

    No explicit `secure` flag: matches the existing captcha cookie convention so
    the cookie still works in tests (http://testserver); the production domain
    is HTTPS-only end to end via nginx regardless.
    '''

    response.set_cookie(
        key=_ADMIN_SESSION_COOKIE,
        value=token,
        max_age=_ADMIN_SESSION_TTL,
        httponly=True,
        samesite='strict',
        path='/admin',
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    '''Render the admin panel, or the login form if there's no valid session.'''

    if not _check_admin_ip(request):
        return JSONResponse(status_code=403, content={'error': 'forbidden'})

    if not _check_admin_session(request.cookies.get(_ADMIN_SESSION_COOKIE)):
        return templates.TemplateResponse(request, 'admin_login.html', {'error': None})

    flash = request.query_params.get('flash')
    flash_type = request.query_params.get('ft', 'ok')
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)

    async with async_session() as session:
        # All users
        users_rows = (
            await session.execute(select(User).order_by(User.created_at.desc()))
        ).scalars().all()

        # Active trial counts per user
        trial_result = await session.execute(
            sql_text(
                "SELECT user_id, "
                "SUM(CASE WHEN expires_at > :now AND remaining_tokens > 0 THEN remaining_tokens ELSE 0 END) AS rem, "
                "MIN(CASE WHEN expires_at > :now AND remaining_tokens > 0 THEN expires_at END) AS exp, "
                "COUNT(*) AS grant_count "
                "FROM trial_tokens GROUP BY user_id"
            ),
            {"now": now},
        )

        trial_map = {r["user_id"]: r for r in trial_result.mappings().all()}

        # 30-day usage per user
        usage_result = await session.execute(
            sql_text(
                "SELECT user_id, "
                "COALESCE(SUM(input_tokens+output_tokens),0) AS total "
                "FROM usage_events WHERE timestamp >= :since GROUP BY user_id"
            ),
            {"since": thirty_days_ago},
        )

        usage_map = {r["user_id"]: r["total"] for r in usage_result.mappings().all()}

        # All-time usage per user
        all_time_usage_result = await session.execute(
            sql_text(
                "SELECT user_id, "
                "COALESCE(SUM(input_tokens+output_tokens),0) AS total "
                "FROM usage_events GROUP BY user_id"
            )
        )
        all_time_usage_map = {
            r["user_id"]: r["total"] for r in all_time_usage_result.mappings().all()
        }

        # Last request per user and current free-vs-paid breakdown
        last_request_result = await session.execute(
            sql_text(
                "SELECT user_id, MAX(timestamp) AS last_req "
                "FROM usage_events GROUP BY user_id"
            )
        )
        last_request_map = {
            r["user_id"]: r["last_req"] for r in last_request_result.mappings().all()
        }

        # Global stats
        active_trials = len(trial_map)
        total_paid = sum(u.balance_tokens for u in users_rows)
    
        requests_30d_r = await session.execute(
            sql_text("SELECT COUNT(*) FROM usage_events WHERE timestamp >= :since"),
            {"since": thirty_days_ago},
        )
    
        requests_30d = requests_30d_r.scalar()
        tokens_30d_r = await session.execute(
            sql_text(
                "SELECT COALESCE(SUM(input_tokens+output_tokens),0) "
                "FROM usage_events WHERE timestamp >= :since"
            ),
            {"since": thirty_days_ago},
        )
        tokens_30d = int(tokens_30d_r.scalar() or 0)

        user_list = []
    
        for u in users_rows:
            tr = trial_map.get(u.id)
            free_tokens = max(0, tr['rem'] if tr else 0)
            paid_tokens = u.balance_tokens
            last_req = last_request_map.get(u.id)
            if isinstance(last_req, str):
                last_req_dt: datetime | None = datetime.fromisoformat(last_req).replace(tzinfo=timezone.utc)
            else:
                last_req_dt = last_req

            # exp is null when a user has grant history but no currently active grant
            trial_exp = tr['exp'] if tr else None
            if isinstance(trial_exp, str):
                trial_exp_dt: datetime | None = datetime.fromisoformat(trial_exp)
            else:
                trial_exp_dt = trial_exp

            user_list.append({
                'id': u.id,
                'email': u.email,
                'prefix': u.api_key_prefix,
                'balance': u.balance_tokens,
                'balance_fmt': _fmt_tokens(u.balance_tokens),
                'free_tokens': free_tokens,
                'free_tokens_fmt': _fmt_tokens(free_tokens),
                'paid_tokens': paid_tokens,
                'paid_tokens_fmt': _fmt_tokens(paid_tokens),
                'trial_remaining': free_tokens,
                'trial_remaining_fmt': _fmt_tokens(free_tokens),
                'trial_expires': trial_exp_dt.strftime('%b %d') if trial_exp_dt else '',
                'trial_grant_count': tr['grant_count'] if tr else 0,
                'has_free_grant_history': (tr['grant_count'] if tr else 0) > 0,
                'used_30d_fmt': _fmt_tokens(usage_map.get(u.id, 0)),
                'used_total_fmt': _fmt_tokens(all_time_usage_map.get(u.id, 0)),
                'joined': u.created_at.strftime('%Y-%m-%d'),
                'last_request': last_req_dt.strftime('%Y-%m-%d') if last_req_dt else '—',
            })

    return templates.TemplateResponse(request, "admin.html", {
        'user_count': len(users_rows),
        'active_trials': active_trials,
        'total_paid_fmt': _fmt_tokens(total_paid),
        'requests_30d': requests_30d,
        'tokens_30d_fmt': _fmt_tokens(tokens_30d),
        'users': user_list,
        'flash': flash,
        'flash_type': flash_type,
    })


@app.post("/admin/login")
async def admin_login(request: Request, key: str = Form(...)):
    '''Verify the admin key and start a cookie-based admin session.

    Keeping the key in a POST form body (instead of a GET query string) keeps
    it out of nginx/proxy access logs and browser history.
    '''

    if not _check_admin_ip(request):
        return JSONResponse(status_code=403, content={'error': 'forbidden'})

    if not _check_admin(key):
        return templates.TemplateResponse(
            request, 'admin_login.html',
            {'error': 'Incorrect admin key.'},
            status_code=403,
        )

    token = _issue_admin_session()
    response = RedirectResponse('/admin', status_code=303)
    _set_admin_session_cookie(response, token)

    return response


@app.post("/admin/logout")
async def admin_logout(request: Request):
    '''End the current admin session.'''

    _revoke_admin_session(request.cookies.get(_ADMIN_SESSION_COOKIE))
    response = RedirectResponse('/admin', status_code=303)
    response.delete_cookie(_ADMIN_SESSION_COOKIE, path='/admin')

    return response


@app.post("/admin/adjust")
async def admin_adjust(
    request: Request,
    user_id: int = Form(...),
    delta: int = Form(...),
):
    '''Add or subtract tokens from a user's paid balance.'''

    if not _check_admin_ip(request) or not _check_admin_session(request.cookies.get(_ADMIN_SESSION_COOKIE)):
        return JSONResponse(status_code=403, content={'error': 'forbidden'})

    async with async_session() as session:
        user = await session.get(User, user_id)

        if user is None:
            return RedirectResponse(
                '/admin?flash=User+not+found&ft=err',
                status_code=303
            )

        user.balance_tokens = max(0, user.balance_tokens + delta)
        await session.commit()
        action = f'+{delta:,}' if delta >= 0 else f'{delta:,}'

    return RedirectResponse(
        f'/admin?flash={action}+tokens+applied+to+{user.email}&ft=ok',
        status_code=303,
    )


@app.post("/admin/delete")
async def admin_delete(
    request: Request,
    user_id: int = Form(...),
):
    '''Permanently delete a user and all associated records.'''

    if not _check_admin_ip(request) or not _check_admin_session(request.cookies.get(_ADMIN_SESSION_COOKIE)):
        return JSONResponse(status_code=403, content={'error': 'forbidden'})

    async with async_session() as session:
        user = await session.get(User, user_id)

        if user is None:
            return RedirectResponse(
                '/admin?flash=User+not+found&ft=err',
                status_code=303
            )

        email = user.email

        await session.execute(
            sql_text('DELETE FROM trial_tokens WHERE user_id = :uid'),
            {'uid': user_id}
        )

        await session.execute(
            sql_text('DELETE FROM usage_events WHERE user_id = :uid'),
            {'uid': user_id}
        )

        await session.execute(
            sql_text('DELETE FROM token_purchases WHERE user_id = :uid'),
            {'uid': user_id}
        )

        await session.delete(user)
        await session.commit()

    return RedirectResponse(
        f'/admin?flash=Deleted+{email}&ft=ok',
        status_code=303,
    )


@app.post("/admin/grant")
async def admin_grant(
    request: Request,
    email: str = Form(...),
    tokens: int = Form(...),
    days: int = Form(...),
):
    '''Grant a new trial allocation (tokens + expiry) to an existing user.'''

    if not _check_admin_ip(request) or not _check_admin_session(request.cookies.get(_ADMIN_SESSION_COOKIE)):
        return JSONResponse(status_code=403, content={'error': 'forbidden'})

    email = email.strip().lower()

    async with async_session() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()

        if user is None:
            return RedirectResponse(
                f'/admin?flash=No+user+found+with+email+{email}&ft=err',
                status_code=303,
            )

        expires_at = datetime.now(timezone.utc) + timedelta(days=days)

        session.add(TrialTokens(
            user_id=user.id,
            tokens_granted=tokens,
            remaining_tokens=tokens,
            expires_at=expires_at,
        ))
        await session.commit()

    return RedirectResponse(
        f'/admin?flash=Granted+{tokens:,}+tokens+to+{email}&ft=ok',
        status_code=303,
    )
