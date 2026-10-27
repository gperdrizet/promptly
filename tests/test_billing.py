'''Billing deduction tests: floor and cap semantics for deduct_tokens.

These exercise the deduction logic directly rather than through the HTTP
stack. True cross-process concurrency is covered by the FOR UPDATE row
locks that deduct_tokens emits on Postgres (the production database);
the in-memory SQLite test database serializes writes at the database
level, so the floor semantics tested here are deterministic.
'''

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import (
    User, TrialTokens, engine, generate_api_key, deduct_tokens,
    total_available_tokens,
)


pytestmark = pytest.mark.asyncio

_session_maker = async_sessionmaker(engine, expire_on_commit=False)


async def _make_user(balance: int = 0, trial: int = 0, trial_days: int = 14) -> int:
    '''Create a user with the given paid balance and (optional) trial grant.'''

    raw, key_hash, prefix = generate_api_key()
    async with _session_maker() as session:
        user = User(
            email='billing@example.com',
            api_key_hash=key_hash,
            api_key_prefix=prefix,
            balance_tokens=balance,
        )
        session.add(user)
        await session.flush()

        if trial > 0:
            session.add(TrialTokens(
                user_id=user.id,
                tokens_granted=trial,
                remaining_tokens=trial,
                expires_at=datetime.now(timezone.utc) + timedelta(days=trial_days),
            ))

        await session.commit()
        return user.id


async def _deduct(user_id: int, tokens: int) -> tuple[int, int, int]:
    '''Run one deduction in its own session; return (deducted, balance, trial).'''

    async with _session_maker() as session:
        user = (await session.execute(
            select(User).where(User.id == user_id)
        )).scalar_one()

        deducted = await deduct_tokens(session, user, tokens)
        await session.commit()

        # Re-read committed values (fresh session state).
        user = (await session.execute(
            select(User).where(User.id == user_id)
        )).scalar_one()

        trial = (await session.execute(
            select(TrialTokens.remaining_tokens)
            .where(TrialTokens.user_id == user_id)
            .where(TrialTokens.expires_at > datetime.now(timezone.utc))
        )).scalar_one_or_none() or 0

        return deducted, user.balance_tokens, trial


async def test_deduct_caps_at_available_no_negative_balance(_db_session):
    '''Deducting more than is available caps at the available amount;
    no balance goes negative.'''

    uid = await _make_user(balance=0, trial=500_000)

    deducted, balance, trial = await _deduct(uid, 800_000)

    assert deducted == 500_000
    assert balance == 0
    assert trial == 0


async def test_deduct_prefers_trial_over_paid(_db_session):
    '''Trial tokens are consumed before paid tokens.'''

    uid = await _make_user(balance=100_000, trial=500_000)

    deducted, balance, trial = await _deduct(uid, 300_000)

    assert deducted == 300_000
    assert trial == 200_000
    assert balance == 100_000


async def test_deduct_paid_floor(_db_session):
    '''With no trial, an overdraft caps at the paid balance.'''

    uid = await _make_user(balance=500, trial=0)

    deducted, balance, trial = await _deduct(uid, 800)

    assert deducted == 500
    assert balance == 0
    assert trial == 0


async def test_deduct_zero_is_noop(_db_session):
    '''Deducting zero changes nothing.'''

    uid = await _make_user(balance=100, trial=200)

    deducted, balance, trial = await _deduct(uid, 0)

    assert deducted == 0
    assert balance == 100
    assert trial == 200


async def test_repeated_overdraw_never_negative(_db_session):
    '''A series of overdrawn deductions (as produced by concurrent
    billing) never drives any balance negative, and the total deducted
    never exceeds what was available.'''

    uid = await _make_user(balance=1_000, trial=500_000)
    available = await _available(uid)

    total_deducted = 0
    for _ in range(5):
        deducted, balance, trial = await _deduct(uid, 400_000)
        total_deducted += deducted
        assert balance >= 0
        assert trial >= 0

    assert total_deducted == available
    assert total_deducted <= 501_000


async def _available(user_id: int) -> int:
    async with _session_maker() as session:
        user = (await session.execute(
            select(User).where(User.id == user_id)
        )).scalar_one()
        return await total_available_tokens(session, user)
