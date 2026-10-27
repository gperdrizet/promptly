'''Smoke tests for the model-gateway.

Cover: health, registration, auth, token metering, admin panel.
The llama-server is NOT called - inference proxy tests are skipped here
(they live in integration tests that run against a real backend).
'''

import re

import pytest
from httpx import AsyncClient


pytestmark = pytest.mark.asyncio


async def _submit_registration(client: AsyncClient, email: str, *, follow_redirects: bool = True):
    '''Submit a registration form with a valid captcha challenge.'''

    r = await client.get('/register')
    assert r.status_code == 200

    text = r.text
    token = re.search(r'name="captcha_token" value="([^"]+)"', text)
    question = re.search(r'Solve this to continue: <strong>([^<]+)</strong>', text)
    assert token is not None
    assert question is not None

    numbers = [int(part) for part in re.findall(r'\d+', question.group(1))]
    answer = sum(numbers)

    return await client.post(
        '/register',
        data={
            'email': email,
            'captcha_token': token.group(1),
            'captcha_answer': answer,
        },
        follow_redirects=follow_redirects,
    )


# --- Health ---

async def test_health(client: AsyncClient):
    '''Health endpoint returns 200 and {status: ok}.'''

    r = await client.get('/health')

    assert r.status_code == 200
    assert r.json() == {'status': 'ok'}


# --- Registration ---

async def test_register_page(client: AsyncClient):
    '''Registration page renders and mentions the trial token amount.'''

    r = await client.get('/register')

    assert r.status_code == 200
    assert b'100k' in r.content or b'100' in r.content


async def test_register_new_user(client: AsyncClient):
    '''Posting a new email registers the user and shows a confirmation page.'''

    r = await _submit_registration(client, 'new@example.com')

    assert r.status_code == 200

    # Same confirmation page regardless (anti-enumeration)
    assert b'new@example.com' in r.content


async def test_register_existing_user_same_response(client: AsyncClient):
    '''Registering twice returns the same page - prevents email enumeration.'''

    r1 = await _submit_registration(client, 'dup@example.com')
    r2 = await _submit_registration(client, 'dup@example.com')

    assert r1.status_code == 200
    assert r2.status_code == 200


async def test_register_rejects_invalid_email(client: AsyncClient):
    '''Malformed or suspicious email input is rejected before account creation.'''

    r = await _submit_registration(client, '../../etc/passwd')

    assert r.status_code == 400
    assert b'valid email address' in r.content.lower()


async def test_register_requires_captcha(client: AsyncClient):
    '''Missing captcha response is rejected before account creation.'''

    r = await client.post('/register', data={'email': 'captcha@example.com'}, follow_redirects=True)

    assert r.status_code == 400
    assert b'captcha' in r.content.lower()


# --- Auth ---

async def test_no_key_returns_401(client: AsyncClient):
    '''Request with no Authorization header is rejected with 401.'''

    r = await client.post('/v1/chat/completions', json={'model': 'x', 'messages': []})

    assert r.status_code == 401


async def test_bad_key_returns_401(client: AsyncClient):
    '''Request with an unrecognized API key is rejected with 401.'''

    r = await client.post(
        '/v1/chat/completions',
        headers={'Authorization': 'Bearer sk-totallyWrongKey12345'},
        json={'model': 'x', 'messages': []},
    )

    assert r.status_code == 401


async def test_valid_key_with_no_balance_returns_402(client: AsyncClient, registered_user):
    '''A user whose trial has 0 remaining and no paid balance gets 402.'''

    from app.db import engine, TrialTokens
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy import update

    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with session_maker() as session:
        await session.execute(
            update(TrialTokens)
            .where(TrialTokens.user_id == registered_user['id'])
            .values(remaining_tokens=0)
        )
        await session.commit()

    r = await client.post(
        '/v1/chat/completions',
        headers={'Authorization': f'Bearer {registered_user["key"]}'},
        json={'model': 'x', 'messages': []},
    )

    assert r.status_code == 402


async def test_responses_text_request_returns_response_shape(client: AsyncClient, registered_user, monkeypatch):
    '''/v1/responses accepts text input and returns a minimal Responses payload.'''

    import httpx
    import app.app as gateway_app

    async def fake_proxy_request(method, path, headers, body):
        assert method == 'POST'
        assert path == '/chat/completions'
        assert 'authorization' in {k.lower() for k in headers}
        assert body['messages'] == [{'role': 'user', 'content': 'Hello!'}]

        return (
            httpx.Response(200, json={
                'id': 'chatcmpl_test123',
                'created': 1234567890,
                'model': 'gpt-oss-20b-mxfp4.gguf',
                'choices': [{
                    'index': 0,
                    'message': {'role': 'assistant', 'content': 'Hello back!'},
                    'finish_reason': 'stop',
                }],
                'usage': {
                    'prompt_tokens': 5,
                    'completion_tokens': 2,
                    'total_tokens': 7,
                },
            }),
            5,
            2,
        )

    monkeypatch.setattr(gateway_app, 'proxy_request', fake_proxy_request)

    r = await client.post(
        '/v1/responses',
        headers={'Authorization': f'Bearer {registered_user["key"]}'},
        json={'model': 'default', 'input': 'Hello!'},
    )

    assert r.status_code == 200
    data = r.json()
    assert data['object'] == 'response'
    assert data['output_text'] == 'Hello back!'
    assert data['output'][0]['content'][0]['text'] == 'Hello back!'
    assert data['usage']['input_tokens'] == 5
    assert data['usage']['output_tokens'] == 2


async def test_responses_rejects_multimodal_input(client: AsyncClient, registered_user):
    '''/v1/responses rejects image input rather than pretending to support it.'''

    r = await client.post(
        '/v1/responses',
        headers={'Authorization': f'Bearer {registered_user["key"]}'},
        json={
            'model': 'default',
            'input': [{
                'role': 'user',
                'content': [
                    {'type': 'input_text', 'text': 'What is in this image?'},
                    {'type': 'input_image', 'image_url': 'https://example.com/cat.png'},
                ],
            }],
        },
    )

    assert r.status_code == 400
    assert 'text-only requests' in r.json()['error']['message']


async def test_responses_streaming_translates_chat_sse(client: AsyncClient, registered_user, monkeypatch):
    '''/v1/responses stream emits text delta events translated from chat SSE.'''

    import app.app as gateway_app

    async def fake_proxy_stream(method, path, headers, body):
        assert method == 'POST'
        assert path == '/chat/completions'
        assert 'authorization' in {k.lower() for k in headers}
        assert body['stream'] is True
        yield (
            b'data: {"id":"chatcmpl_1","model":"gpt-oss-20b-mxfp4.gguf","choices":[{"delta":{"content":"Hello"}}]}\n\n',
            0,
            0,
        )
        yield (
            b'data: {"id":"chatcmpl_1","model":"gpt-oss-20b-mxfp4.gguf","choices":[{"delta":{"content":" world"}}]}\n\n',
            0,
            0,
        )
        yield b'', 4, 2

    monkeypatch.setattr(gateway_app, 'proxy_stream', fake_proxy_stream)

    r = await client.post(
        '/v1/responses',
        headers={'Authorization': f'Bearer {registered_user["key"]}'},
        json={'model': 'default', 'input': 'Hello?', 'stream': True},
    )

    assert r.status_code == 200
    assert 'response.created' in r.text
    assert 'response.output_text.delta' in r.text
    assert 'response.completed' in r.text
    assert 'Hello world' in r.text


# --- Model selection (default + opt-in) ---


@pytest.mark.parametrize('payload', [
    pytest.param(
        {'messages': [{'role': 'user', 'content': 'Hello!'}]},
        id='model-missing',
    ),
    pytest.param(
        {'model': '', 'messages': [{'role': 'user', 'content': 'Hello!'}]},
        id='model-empty',
    ),
])
async def test_chat_missing_or_empty_model_defaults_to_pinned_default(
    client: AsyncClient, registered_user, monkeypatch, payload
):
    '''A missing or empty `model` is replaced with the pinned default alias
    before the request is forwarded to the backend.'''

    import httpx
    import app.app as gateway_app

    async def fake_proxy_request(method, path, headers, body):
        assert body['model'] == 'default'

        return (
            httpx.Response(200, json={
                'id': 'chatcmpl_test123',
                'created': 1234567890,
                'model': 'Qwen3.8-27B-UD-Q4_K_M.gguf',
                'choices': [{
                    'index': 0,
                    'message': {'role': 'assistant', 'content': 'Hello back!'},
                    'finish_reason': 'stop',
                }],
                'usage': {
                    'prompt_tokens': 5,
                    'completion_tokens': 2,
                    'total_tokens': 7,
                },
            }),
            5,
            2,
        )

    monkeypatch.setattr(gateway_app, 'proxy_request', fake_proxy_request)

    r = await client.post(
        '/v1/chat/completions',
        headers={'Authorization': f'Bearer {registered_user["key"]}'},
        json=payload,
    )

    assert r.status_code == 200
    assert r.json()['model'] == 'Qwen3.8-27B-UD-Q4_K_M.gguf'


async def test_chat_allowlist_rejects_unknown_model(client: AsyncClient, registered_user, monkeypatch):
    '''With MODEL_ALLOWLIST set, a model outside the list is rejected with a
    clear 400 and never reaches the backend.'''

    import app.app as gateway_app

    monkeypatch.setattr(gateway_app, '_MODEL_ALLOWLIST', {'default', 'Qwen3.8-27B'})

    r = await client.post(
        '/v1/chat/completions',
        headers={'Authorization': f'Bearer {registered_user["key"]}'},
        json={'model': 'Qwen3.5-9B', 'messages': [{'role': 'user', 'content': 'Hello!'}]},
    )

    assert r.status_code == 400
    error = r.json()['error']
    assert error['type'] == 'invalid_request_error'
    assert 'not available' in error['message']
    assert 'Qwen3.8-27B' in error['message']


async def test_chat_allowlist_passes_known_model(client: AsyncClient, registered_user, monkeypatch):
    '''A model inside MODEL_ALLOWLIST is forwarded to the backend unchanged.'''

    import httpx
    import app.app as gateway_app

    async def fake_proxy_request(method, path, headers, body):
        assert body['model'] == 'Qwen3.8-27B'

        return (
            httpx.Response(200, json={
                'id': 'chatcmpl_test123',
                'created': 1234567890,
                'model': 'Qwen3.8-27B-UD-Q4_K_M.gguf',
                'choices': [{
                    'index': 0,
                    'message': {'role': 'assistant', 'content': 'Hello back!'},
                    'finish_reason': 'stop',
                }],
                'usage': {
                    'prompt_tokens': 5,
                    'completion_tokens': 2,
                    'total_tokens': 7,
                },
            }),
            5,
            2,
        )

    monkeypatch.setattr(gateway_app, '_MODEL_ALLOWLIST', {'default', 'Qwen3.8-27B'})
    monkeypatch.setattr(gateway_app, 'proxy_request', fake_proxy_request)

    r = await client.post(
        '/v1/chat/completions',
        headers={'Authorization': f'Bearer {registered_user["key"]}'},
        json={'model': 'Qwen3.8-27B', 'messages': [{'role': 'user', 'content': 'Hello!'}]},
    )

    assert r.status_code == 200


async def test_responses_allowlist_rejects_unknown_model(client: AsyncClient, registered_user, monkeypatch):
    '''The /v1/responses shim enforces the same model allow-list.'''

    import app.app as gateway_app

    monkeypatch.setattr(gateway_app, '_MODEL_ALLOWLIST', {'default', 'Qwen3.8-27B'})

    r = await client.post(
        '/v1/responses',
        headers={'Authorization': f'Bearer {registered_user["key"]}'},
        json={'model': 'Qwen3.5-9B', 'input': 'Hello!'},
    )

    assert r.status_code == 400
    assert 'not available' in r.json()['error']['message']


async def test_models_list_proxied_to_backend(client: AsyncClient, registered_user, monkeypatch):
    '''GET /v1/models is authenticated and passed through from the backend.'''

    import httpx
    import app.app as gateway_app

    async def fake_proxy_request(method, path, headers, body):
        assert method == 'GET'
        assert path == '/models'
        assert body is None

        return (
            httpx.Response(200, json={
                'object': 'list',
                'data': [
                    {'id': 'default', 'object': 'model'},
                    {'id': 'Qwen3.8-27B', 'object': 'model'},
                ],
            }),
            0,
            0,
        )

    monkeypatch.setattr(gateway_app, 'proxy_request', fake_proxy_request)

    r = await client.get(
        '/v1/models',
        headers={'Authorization': f'Bearer {registered_user["key"]}'},
    )

    assert r.status_code == 200
    ids = [m['id'] for m in r.json()['data']]
    assert 'default' in ids
    assert 'Qwen3.8-27B' in ids


# --- Dashboard ---

async def test_dashboard_valid_key(client: AsyncClient, registered_user):
    '''A valid key logs the user in: the one-time ?key= login redirects to a
    key-free URL and the dashboard renders from the session cookie.'''

    r = await client.get(f'/dashboard?key={registered_user["key"]}', follow_redirects=True)

    assert r.status_code == 200
    assert registered_user['key'] not in str(r.url)
    assert registered_user['email'].encode() in r.content


async def test_dashboard_bad_key_redirects(client: AsyncClient, _db_session):
    '''Dashboard redirects to the landing page when an invalid key is supplied.'''

    r = await client.get(
        '/dashboard?key=sk-notavalidkey12345678901234567890',
        follow_redirects=False,
    )

    assert r.status_code in (302, 303)
    assert r.headers['location'] == '/'


async def test_dashboard_requires_session(client: AsyncClient, _db_session):
    '''GET /dashboard with no session cookie redirects to the landing page.'''

    r = await client.get('/dashboard', follow_redirects=False)

    assert r.status_code in (302, 303)
    assert r.headers['location'] == '/'


async def test_dashboard_session_renders_without_key(client: AsyncClient, registered_user):
    '''Once logged in, the dashboard renders from the cookie alone.'''

    await client.get(f'/dashboard?key={registered_user["key"]}', follow_redirects=False)

    r = await client.get('/dashboard')

    assert r.status_code == 200
    assert registered_user['email'].encode() in r.content
    assert registered_user['key'] not in r.text


async def test_dashboard_logout_revokes_session(client: AsyncClient, registered_user):
    '''POST /dashboard/logout ends the session; the next /dashboard redirects.'''

    await client.get(f'/dashboard?key={registered_user["key"]}', follow_redirects=False)

    r = await client.post('/dashboard/logout', follow_redirects=False)

    assert r.status_code in (302, 303)

    r = await client.get('/dashboard', follow_redirects=False)

    assert r.status_code in (302, 303)
    assert r.headers['location'] == '/'


# --- Admin panel ---

async def test_admin_bad_key_forbidden(client: AsyncClient, _db_session):
    '''Admin login rejects an incorrect admin key.'''

    r = await client.post('/admin/login', data={'key': 'wrong'})

    assert r.status_code == 403


async def test_admin_valid_key(client: AsyncClient, _db_session):
    '''Admin login establishes a session, and the dashboard renders for it.'''

    import os

    admin_key = os.environ['ADMIN_KEY']
    login_r = await client.post('/admin/login', data={'key': admin_key}, follow_redirects=False)

    assert login_r.status_code == 303
    assert 'admin_session' in login_r.cookies

    r = await client.get('/admin')

    assert r.status_code == 200
    assert b'Admin' in r.content


async def test_admin_no_session_shows_login(client: AsyncClient, _db_session):
    '''Admin panel without a session cookie renders the login form, not the dashboard.'''

    r = await client.get('/admin')

    assert r.status_code == 200
    assert b'Admin key' in r.content


async def test_admin_logout_revokes_session(client: AsyncClient, _db_session):
    '''After logout, the admin panel no longer renders for the old session cookie.'''

    import os

    admin_key = os.environ['ADMIN_KEY']
    await client.post('/admin/login', data={'key': admin_key})
    await client.post('/admin/logout')

    r = await client.get('/admin')

    assert r.status_code == 200
    assert b'Admin key' in r.content


async def test_admin_adjust_tokens(client: AsyncClient, registered_user):
    '''Admin adjust endpoint credits tokens to a user paid balance.'''

    import os

    admin_key = os.environ['ADMIN_KEY']
    await client.post('/admin/login', data={'key': admin_key})
    r = await client.post(
        '/admin/adjust',
        data={'user_id': registered_user['id'], 'delta': '1000000'},
        follow_redirects=False,
    )

    assert r.status_code in (302, 303)

    # Confirm balance was updated
    from app.db import engine, User as UserModel
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with session_maker() as session:
        user = await session.get(UserModel, registered_user['id'])
        assert user.balance_tokens == 1_000_000


async def test_admin_grant_trial(client: AsyncClient, registered_user):
    '''Admin grant endpoint issues a trial token grant to a user.'''

    import os

    admin_key = os.environ['ADMIN_KEY']
    await client.post('/admin/login', data={'key': admin_key})
    r = await client.post(
        '/admin/grant',
        data={
            'email': registered_user['email'],
            'tokens': '100000',
            'days': '7',
        },
        follow_redirects=False,
    )

    assert r.status_code in (302, 303)


async def test_admin_delete_user(client: AsyncClient, registered_user):
    '''Admin delete endpoint removes the user row from the database.'''

    import os

    admin_key = os.environ['ADMIN_KEY']
    await client.post('/admin/login', data={'key': admin_key})
    r = await client.post(
        '/admin/delete',
        data={'user_id': registered_user['id']},
        follow_redirects=False,
    )

    assert r.status_code in (302, 303)

    from app.db import engine, User as UserModel
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with session_maker() as session:
        user = await session.get(UserModel, registered_user['id'])
        assert user is None


# --- Checkout (session-based; no API key in any URL) ---


async def _login_dashboard(client: AsyncClient, key: str):
    '''Establish a dashboard session via the one-time ?key= login.'''

    r = await client.get(f'/dashboard?key={key}', follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers['location'] == '/dashboard'


async def test_checkout_requires_session(client: AsyncClient, _db_session):
    '''POST /checkout without a dashboard session redirects to the landing page.'''

    r = await client.post(
        '/checkout', data={'pack': '5m', 'method': 'stripe'}, follow_redirects=False,
    )

    assert r.status_code in (302, 303)
    assert r.headers['location'] == '/'


async def test_checkout_stripe_not_configured(client: AsyncClient, registered_user):
    '''Checkout returns 503 when Stripe is not configured (placeholder keys).'''

    await _login_dashboard(client, registered_user['key'])

    r = await client.post(
        '/checkout', data={'pack': '5m', 'method': 'stripe'}, follow_redirects=False,
    )

    assert r.status_code == 503


async def test_checkout_btcpay_not_configured(client: AsyncClient, registered_user):
    '''Checkout returns 503 when BTCPay is not configured (placeholder keys).'''

    await _login_dashboard(client, registered_user['key'])

    r = await client.post(
        '/checkout', data={'pack': '5m', 'method': 'btcpay'}, follow_redirects=False,
    )

    assert r.status_code == 503


async def test_checkout_invalid_pack(client: AsyncClient, registered_user):
    '''Checkout rejects an unknown pack identifier with 400 or 503.'''

    await _login_dashboard(client, registered_user['key'])

    r = await client.post(
        '/checkout', data={'pack': '999zz', 'method': 'stripe'}, follow_redirects=False,
    )

    assert r.status_code in (400, 503)


async def test_checkout_return_urls_carry_token_not_key(client: AsyncClient, registered_user, monkeypatch):
    '''Provider return URLs contain a one-time token, never the API key;
    the token re-opens a dashboard session and is single-use.'''

    import app.app as gateway_app

    captured: dict[str, str] = {}

    async def fake_create_checkout_session(pack_id, user_id, user_email, success_url, cancel_url):
        captured['success_url'] = success_url
        captured['cancel_url'] = cancel_url
        return 'https://stripe.example/checkout/test'

    monkeypatch.setattr(gateway_app, 'create_checkout_session', fake_create_checkout_session)

    await _login_dashboard(client, registered_user['key'])

    r = await client.post(
        '/checkout', data={'pack': '5m', 'method': 'stripe'}, follow_redirects=False,
    )

    assert r.status_code == 303
    assert 'stripe.example' in r.headers['location']
    assert registered_user['key'] not in captured['success_url']
    assert registered_user['key'] not in captured['cancel_url']

    token = re.search(r'token=([A-Za-z0-9_\-]+)', captured['success_url'])
    assert token is not None

    # Provider redirects back: token is consumed, session re-established.
    r = await client.get(
        f'/checkout/return?token={token.group(1)}&status=success',
        follow_redirects=False,
    )

    assert r.status_code in (302, 303)
    assert r.headers['location'] == '/dashboard?payment=success'

    r = await client.get('/dashboard')

    assert r.status_code == 200
    assert registered_user['email'].encode() in r.content

    # Replaying the same token does not re-open a session.
    r = await client.get(f'/checkout/return?token={token.group(1)}', follow_redirects=False)

    assert r.status_code in (302, 303)
    assert r.headers['location'] == '/'


# --- Streaming (main proxy) ---

async def test_chat_streaming_proxies_sse_and_records_usage(client: AsyncClient, registered_user, monkeypatch):
    '''POST /v1/chat/completions with stream:true passes the backend SSE
    chunks through unchanged and records token usage when the stream ends.'''

    import app.app as gateway_app
    from app.db import engine, TrialTokens
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async def fake_proxy_stream(method, path, headers, body):
        assert method == 'POST'
        assert path == '/chat/completions'
        assert body['stream'] is True
        assert body['model'] == 'default'
        yield (
            b'data: {"id":"chatcmpl_1","model":"Qwen3.8-27B-UD-Q4_K_M.gguf",'
            b'"choices":[{"delta":{"content":"Hello"}}]}\n\n',
            0,
            0,
        )
        yield (
            b'data: {"id":"chatcmpl_1","model":"Qwen3.8-27B-UD-Q4_K_M.gguf",'
            b'"choices":[{"delta":{"content":" world"}}]}\n\n',
            0,
            0,
        )
        yield (
            b'data: {"id":"chatcmpl_1","model":"Qwen3.8-27B-UD-Q4_K_M.gguf",'
            b'"choices":[{"delta":{},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}\n\n',
            4,
            2,
        )
        yield b'data: [DONE]\n\n', 0, 0

    monkeypatch.setattr(gateway_app, 'proxy_stream', fake_proxy_stream)

    r = await client.post(
        '/v1/chat/completions',
        headers={'Authorization': f'Bearer {registered_user["key"]}'},
        json={
            'model': 'default',
            'messages': [{'role': 'user', 'content': 'Hello!'}],
            'stream': True,
        },
    )

    assert r.status_code == 200
    assert r.headers['content-type'].startswith('text/event-stream')
    assert 'Hello' in r.text
    assert ' world' in r.text
    assert '[DONE]' in r.text

    # Usage was recorded once the stream ended.
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        trial = (await session.execute(
            select(TrialTokens).where(TrialTokens.user_id == registered_user['id'])
        )).scalars().first()

        assert trial is not None
        assert trial.remaining_tokens == 500_000 - 6


# --- Rate limiting ---

async def test_ip_rate_limit_returns_429(client: AsyncClient, _db_session, monkeypatch):
    '''Requests from one IP over the per-IP inference limit get 429; the IP
    limiter runs before auth, so unauthenticated requests count too.'''

    import app.app as gateway_app

    monkeypatch.setattr(gateway_app, 'INF_IP_LIMIT', 3)

    for _ in range(3):
        r = await client.post('/v1/chat/completions', json={'model': 'x', 'messages': []})
        assert r.status_code == 401

    r = await client.post('/v1/chat/completions', json={'model': 'x', 'messages': []})

    assert r.status_code == 429
    assert r.json()['error']['type'] == 'rate_limit_error'


async def test_user_rate_limit_returns_429(client: AsyncClient, registered_user, monkeypatch):
    '''Authenticated requests over the per-user limit get 429.'''

    import httpx
    import app.app as gateway_app

    monkeypatch.setattr(gateway_app, 'INF_USER_LIMIT', 3)

    async def fake_proxy_request(method, path, headers, body):
        return (
            httpx.Response(200, json={
                'id': 'chatcmpl_test123',
                'created': 1234567890,
                'model': 'Qwen3.8-27B-UD-Q4_K_M.gguf',
                'choices': [{
                    'index': 0,
                    'message': {'role': 'assistant', 'content': 'ok'},
                    'finish_reason': 'stop',
                }],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
            }),
            1,
            1,
        )

    monkeypatch.setattr(gateway_app, 'proxy_request', fake_proxy_request)

    for _ in range(3):
        r = await client.post(
            '/v1/chat/completions',
            headers={'Authorization': f'Bearer {registered_user["key"]}'},
            json={'model': 'default', 'messages': [{'role': 'user', 'content': 'hi'}]},
        )
        assert r.status_code == 200

    r = await client.post(
        '/v1/chat/completions',
        headers={'Authorization': f'Bearer {registered_user["key"]}'},
        json={'model': 'default', 'messages': [{'role': 'user', 'content': 'hi'}]},
    )

    assert r.status_code == 429
    assert r.json()['error']['type'] == 'rate_limit_error'


async def test_rate_limit_sweeps_stale_keys():
    '''The shared limiter drops keys whose window has fully passed, so the
    in-memory store cannot grow without bound as IPs/users come and go.'''

    import time
    import app.app as gateway_app

    store = {
        'stale-ip': [time.time() - 7200],  # older than the 60s window
        'fresh-ip': [time.time() - 10],
    }

    # A first check against an unseen store triggers the periodic sweep.
    assert gateway_app._check_rate_limit(store, 'new-ip', 10, 60) is True

    assert 'stale-ip' not in store
    assert 'fresh-ip' in store
    assert store['new-ip']


async def test_rate_limit_enforces_window():
    '''Within the window the limit is enforced; once the window passes,
    requests are allowed again.'''

    import time
    import app.app as gateway_app

    store = {}

    assert gateway_app._check_rate_limit(store, 'ip', 2, 60) is True
    assert gateway_app._check_rate_limit(store, 'ip', 2, 60) is True
    assert gateway_app._check_rate_limit(store, 'ip', 2, 60) is False

    # Simulate the window expiring.
    store['ip'] = [time.time() - 120]

    assert gateway_app._check_rate_limit(store, 'ip', 2, 60) is True
