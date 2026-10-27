'''Pytest configuration and shared fixtures.

Tests use an in-memory SQLite database so they run without Docker or PostgreSQL.
The llama-server backend is mocked via httpx.MockTransport.
'''

import os
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

# --- Set env vars before importing the app ---
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("LLAMA_BASE_URL", "http://llama-mock")
os.environ.setdefault("LLAMA_API_KEY", "test-llama-key")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_placeholder")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_placeholder")
os.environ.setdefault("BTCPAY_URL", "http://placeholder")
os.environ.setdefault("BTCPAY_API_KEY", "placeholder")
os.environ.setdefault("BTCPAY_STORE_ID", "placeholder")
os.environ.setdefault("BTCPAY_WEBHOOK_SECRET", "placeholder")
os.environ.setdefault("SMTP_HOST", "localhost")
os.environ.setdefault("SMTP_PORT", "587")
os.environ.setdefault("SMTP_USER", "test@example.com")
os.environ.setdefault("SMTP_PASSWORD", "placeholder")
os.environ.setdefault("SMTP_FROM", "test@example.com")
os.environ.setdefault("ADMIN_KEY", "test-admin-key")
os.environ.setdefault("BASE_URL", "http://testserver")
os.environ.setdefault("TRIAL_TOKENS", "100000")
os.environ.setdefault("TRIAL_EXPIRY_DAYS", "7")
os.environ.setdefault("REGISTRATION_RATE_LIMIT", "50")
os.environ.setdefault("REGISTRATION_RATE_WINDOW", "3600")
os.environ.setdefault("REGISTRATION_BURST_LIMIT", "50")
os.environ.setdefault("REGISTRATION_BURST_WINDOW", "600")

from app.app import app  # noqa: E402 - must import after env vars are set
import app.app as gateway_app
from app.db import engine, Base, generate_api_key, User, TrialTokens
from sqlalchemy.ext.asyncio import async_sessionmaker
from datetime import datetime, timedelta, timezone


# --- Fixtures ---


@pytest_asyncio.fixture(scope='function')
async def _db_session():
    '''Provide a fresh in-memory SQLite database for each test.

    Yields:
        None; sets up and tears down the schema around each test.
    '''

    gateway_app._reg_attempts.clear()
    gateway_app._reg_burst_attempts.clear()
    gateway_app._reg_captcha_challenges.clear()
    gateway_app._ip_inf_attempts.clear()
    gateway_app._user_inf_attempts.clear()
    gateway_app._last_limiter_sweep.clear()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    gateway_app._reg_attempts.clear()
    gateway_app._reg_burst_attempts.clear()
    gateway_app._reg_captcha_challenges.clear()
    gateway_app._ip_inf_attempts.clear()
    gateway_app._user_inf_attempts.clear()
    gateway_app._last_limiter_sweep.clear()


@pytest_asyncio.fixture(scope='function')
async def client(_db_session):
    '''Provide an ASGI test client backed by a fresh database.

    Yields:
        An httpx.AsyncClient configured against the FastAPI app.
    '''

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as c:
        yield c


@pytest_asyncio.fixture(scope='function')
async def registered_user(_db_session):
    '''Create a real user with 500k trial tokens and return their credentials.

    Yields:
        A dict with keys: id, email, key, prefix.
    '''
    raw_key, key_hash, prefix = generate_api_key()
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        user = User(
            email='fixture@example.com',
            api_key_hash=key_hash,
            api_key_prefix=prefix,
            balance_tokens=0,
        )
        session.add(user)
        await session.flush()

        trial = TrialTokens(
            user_id=user.id,
            tokens_granted=500_000,
            remaining_tokens=500_000,
            expires_at=datetime.now(timezone.utc) + timedelta(days=14),
        )
        session.add(trial)
        await session.commit()

    return {'id': user.id, 'email': user.email, 'key': raw_key, 'prefix': prefix}
