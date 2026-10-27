#!/usr/bin/env python3
'''Staging smoke test.

Run this from any tailnet machine to validate a deployment before promoting
it to production.

Usage:
    python3 scripts/smoke-test.py [--url URL] [--admin-key KEY] [--verbose]

Defaults:
    --url       http://100.64.0.1:8505  (staging)
    --admin-key read from ADMIN_KEY env var or .env file

Example:
    python3 scripts/smoke-test.py                              # reads .env
    ADMIN_KEY=mykey python3 scripts/smoke-test.py              # env var wins
    python3 scripts/smoke-test.py --url http://100.64.0.1:8503 # prod
'''

import argparse
import http.cookiejar
import os
import re
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import json


# --- Helpers ---

_DOTENV_KEYS = ('ADMIN_KEY', 'SMOKE_API_KEY')


def _load_dotenv() -> None:
    '''Read ADMIN_KEY and SMOKE_API_KEY from the nearest .env file.

    Searches from the script location upward (scripts/ -> project root).
    Existing env vars are never overwritten.
    '''
    script_dir = os.path.dirname(os.path.abspath(__file__))

    for directory in (script_dir, os.path.dirname(script_dir)):
        env_path = os.path.join(directory, '.env')

        if not os.path.isfile(env_path):
            continue

        with open(env_path) as fh:
            for raw in fh:
                line = raw.strip()

                # Skip blank lines and comments
                if not line or line.startswith('#'):
                    continue

                if '=' not in line:
                    continue

                key, _, value = line.partition('=')
                key = key.strip()

                if key not in _DOTENV_KEYS:
                    continue

                # Strip inline comments and surrounding quotes
                value = value.split('#')[0].strip().strip('"\'')

                # Never overwrite a value already in the environment
                if value and key not in os.environ:
                    os.environ[key] = value

        break  # Stop after the first .env found


_load_dotenv()


RESET  = '\033[0m'
GREEN  = '\033[32m'
RED    = '\033[31m'
YELLOW = '\033[33m'
BOLD   = '\033[1m'
DIM    = '\033[2m'


def _col(color: str, text: str) -> str:
    return f'{color}{text}{RESET}' if sys.stdout.isatty() else text


def ok(label: str, detail: str = '') -> None:
    suffix = f'  {_col(DIM, detail)}' if detail else ''
    print(f'  {_col(GREEN, "OK")} {label}{suffix}')


def fail(label: str, detail: str = '') -> None:
    suffix = f'\n    {_col(DIM, detail)}' if detail else ''
    print(f'  {_col(RED, "FAIL")} {label}{suffix}')


def skip(label: str, detail: str = '') -> None:
    suffix = f'  {_col(DIM, detail)}' if detail else ''
    print(f'  {_col(YELLOW, "-")} {label}{suffix}')


def section(title: str) -> None:
    print(f'\n{_col(BOLD, title)}')


def request(
    url: str,
    *,
    method: str = 'GET',
    data: dict | bytes | None = None,
    headers: dict | None = None,
    form: bool = False,
    timeout: int = 30,
) -> tuple[int, bytes, dict]:
    '''Make an HTTP request without third-party libraries.

    Returns:
        (status_code, body_bytes, response_headers)
    '''
    body: bytes | None = None

    if data is not None:
        if isinstance(data, bytes):
            body = data
        elif form:
            body = urllib.parse.urlencode(data).encode()
        else:
            body = json.dumps(data).encode()

    req = urllib.request.Request(url, data=body, method=method)

    if headers:
        for k, v in headers.items():
            req.add_header(k, v)

    if body and not form:
        req.add_header('Content-Type', 'application/json')
    elif body and form:
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')

    try:
        # Shared cookie jar: the dashboard login (?key=) sets a session
        # cookie that the followed redirect needs.
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


_cookie_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookie_jar))


# --- Test cases ---

def test_health(base: str, verbose: bool) -> bool:
    '''GET /health returns 200 {"status":"ok"}.'''

    status, body, _ = request(f'{base}/health')

    if status == 200 and json.loads(body).get('status') == 'ok':
        ok('health endpoint', f'HTTP {status}')
        return True

    fail('health endpoint', f'HTTP {status}: {body[:200]}')
    return False


def test_register_page(base: str, verbose: bool) -> bool:
    '''GET /register returns 200 HTML.'''

    status, body, _ = request(f'{base}/register')

    if status == 200 and b'<form' in body:
        ok('registration page renders')
        return True

    fail('registration page', f'HTTP {status}')
    return False


def test_register_new_user(base: str, email: str, verbose: bool) -> bool:
    '''POST /register with a fresh email returns 200 confirmation page.'''

    status, body, _ = request(
        f'{base}/register',
        method='POST',
        data={'email': email},
        form=True,
    )

    if status == 200 and email.encode() in body:
        ok('register new user', email)
        return True

    fail('register new user', f'HTTP {status}')
    return False


def test_register_duplicate(base: str, email: str, verbose: bool) -> bool:
    '''POST /register with same email again returns 200 (anti-enumeration).'''

    status, body, _ = request(
        f'{base}/register',
        method='POST',
        data={'email': email},
        form=True,
    )

    if status == 200:
        ok('duplicate registration returns same page (anti-enumeration)')
        return True

    fail('duplicate registration', f'HTTP {status}')
    return False


def test_no_key_401(base: str, verbose: bool) -> bool:
    '''POST /v1/chat/completions with no key returns 401.'''

    status, body, _ = request(
        f'{base}/v1/chat/completions',
        method='POST',
        data={'model': 'default', 'messages': []},
    )

    if status == 401:
        ok('missing key: 401')
        return True

    fail('missing key check', f'expected 401, got {status}')
    return False


def test_bad_key_401(base: str, verbose: bool) -> bool:
    '''POST /v1/chat/completions with garbage key returns 401.'''

    status, _, _ = request(
        f'{base}/v1/chat/completions',
        method='POST',
        data={'model': 'default', 'messages': []},
        headers={'Authorization': 'Bearer sk-totallyinvalidkey12345678'},
    )

    if status == 401:
        ok('invalid key: 401')
        return True

    fail('invalid key check', f'expected 401, got {status}')
    return False


def test_grant_and_infer(
    base: str,
    admin_key: str,
    email: str,
    user_id: int,
    verbose: bool,
) -> tuple[bool, bool]:
    '''Grant trial tokens via admin, then send a real inference request.

    Returns:
        (grant_ok, inference_ok)
    '''
    # Grant 10k tokens via admin
    status, body, _ = request(
        f'{base}/admin/grant',
        method='POST',
        data={
            'key': admin_key,
            'email': email,
            'tokens': '10000',
            'days': '1',
        },
        form=True,
    )

    if status not in (200, 302, 303):
        fail('admin grant tokens', f'HTTP {status}: {body[:200]}')
        return False, False

    ok('admin grant tokens', '10k tokens, 1 day')

    # Actual inference: this call goes all the way to llama-server
    status, body, _ = request(
        f'{base}/v1/chat/completions',
        method='POST',
        data={
            'model': 'default',
            'messages': [{'role': 'user', 'content': 'Reply with exactly one word: pong'}],
            'max_tokens': 8,
        },
        headers={'Authorization': f'Bearer {_api_key}'},
        timeout=120,
    )

    if status == 200:
        try:
            resp = json.loads(body)
            content = resp['choices'][0]['message']['content'].strip()
            ok('inference request', repr(content))
            return True, True
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            fail('inference response parsing', str(exc))
            return True, False

    # 402 means the grant didn't propagate yet or the model server is down
    if status == 402:
        fail('inference: 402 (token balance problem or model server unreachable)')
        return True, False

    fail('inference request', f'HTTP {status}: {body[:300]}')
    return True, False


def test_dashboard(base: str, api_key: str, email: str, verbose: bool) -> bool:
    '''GET /dashboard?key=... establishes a session (one-time key login) and
    the redirected request renders the dashboard page.'''

    status, body, _ = request(f'{base}/dashboard?key={api_key}')

    if status == 200 and b'<title>Dashboard</title>' in body:
        ok('dashboard renders for user')
        return True

    fail('dashboard', f'HTTP {status}')
    return False


def test_admin_panel(base: str, admin_key: str, verbose: bool) -> bool:
    '''GET /admin?key=... returns 200.'''

    status, body, _ = request(f'{base}/admin?key={admin_key}')

    if status == 200:
        ok('admin panel accessible')
        return True

    if status == 403:
        fail('admin panel: 403 (wrong key, or IP not in allowed CIDRs?)')
        return False

    fail('admin panel', f'HTTP {status}')
    return False


def test_rate_limit(base: str, verbose: bool) -> bool:
    '''Hammer /v1/* without a key - the IP rate limiter should fire before auth.'''

    # Per-IP limit is 120/min by default; fire 130 requests quickly.
    limit_hit = False

    for i in range(130):
        status, _, _ = request(
            f'{base}/v1/chat/completions',
            method='POST',
            data={'model': 'default', 'messages': []},
            timeout=5,
        )

        if status == 429:
            limit_hit = True
            ok(f'IP rate limiter fires at request {i + 1}')
            break

    if not limit_hit:
        fail('IP rate limiter never fired after 130 requests')
        return False

    return True


def get_user_id_from_admin(base: str, admin_key: str, email: str) -> int | None:
    '''Scrape the user ID from the admin panel by finding the email row.'''

    status, body, _ = request(f'{base}/admin?key={admin_key}')

    if status != 200:
        return None

    text = body.decode('utf-8', errors='ignore')
    idx = text.find(email)

    if idx == -1:
        return None

    # Search forwards from the email for the user_id hidden input
    snippet = text[idx:idx + 2000]

    m = re.search(r'name="user_id"\s+value="(\d+)"', snippet)
    if m:
        return int(m.group(1))

    return None


def cleanup(base: str, admin_key: str, user_id: int | None, email: str) -> None:
    '''Delete the smoke-test user via admin.'''

    if user_id is None:
        skip('cleanup (could not determine user id)')
        return

    status, _, _ = request(
        f'{base}/admin/delete',
        method='POST',
        data={'key': admin_key, 'user_id': str(user_id)},
        form=True,
    )

    if status in (200, 302, 303):
        ok(f'cleaned up test user ({email})')
    else:
        skip(f'cleanup failed (HTTP {status}); delete {email!r} from admin panel manually')


# --- Entry point ---

# Module-level so test_grant_and_infer can reference it after registration
_api_key: str = ''


def main() -> int:
    global _api_key

    parser = argparse.ArgumentParser(
        description='Smoke-test a model-gateway deployment.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent('''
            Examples:
              # Staging (default)
              ADMIN_KEY=mykey python3 scripts/smoke-test.py

              # Production
              ADMIN_KEY=mykey python3 scripts/smoke-test.py --url http://100.64.0.1:8503

              # Skip the rate-limit hammering (slow)
              ADMIN_KEY=mykey python3 scripts/smoke-test.py --skip-rate-limit
        '''),
    )

    parser.add_argument(
        '--url',
        default='http://100.64.0.1:8505',
        help='Base URL of the gateway (default: staging at :8505)',
    )
    parser.add_argument(
        '--admin-key',
        default=os.environ.get('ADMIN_KEY', ''),
        help='Admin key (default: $ADMIN_KEY env var or .env file)',
    )
    parser.add_argument(
        '--skip-rate-limit',
        action='store_true',
        help='Skip the rate-limit stress test (sends 130 requests)',
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Print extra detail on failures',
    )

    args = parser.parse_args()
    base = args.url.rstrip('/')
    admin_key = args.admin_key
    verbose = args.verbose

    print(f'\n{_col(BOLD, "model-gateway smoke test")}'
          f'  {_col(DIM, base)}\n')

    if not admin_key:
        print(_col(RED, 'Error: --admin-key or $ADMIN_KEY is required.'))
        return 1

    # Unique email so we never collide with real users
    email = f'smoketest-{int(time.time())}@smoke.invalid'
    passed = 0
    failed = 0
    user_id: int | None = None

    def record(result: bool) -> bool:
        nonlocal passed, failed
        if result:
            passed += 1
        else:
            failed += 1
        return result

    # --- Phase 1: Infrastructure ---
    section('1. Infrastructure')
    if not record(test_health(base, verbose)):
        print(_col(RED, '\nHealth check failed; is the service up?'))
        return 1

    record(test_register_page(base, verbose))

    # --- Phase 2: Registration ---
    section('2. Registration')
    reg_ok = record(test_register_new_user(base, email, verbose))
    record(test_register_duplicate(base, email, verbose))

    # Retrieve the user_id from the admin panel for later cleanup
    if reg_ok:
        user_id = get_user_id_from_admin(base, admin_key, email)

    # --- Phase 3: Auth ---
    section('3. Authentication')
    record(test_no_key_401(base, verbose))
    record(test_bad_key_401(base, verbose))

    # --- Phase 4: Admin panel ---
    section('4. Admin panel')
    record(test_admin_panel(base, admin_key, verbose))

    # --- Phase 5: Inference ---
    section('5. Inference (requires API key)')

    # We can't recover the raw API key from the registered user (it's emailed).
    # Ask the operator to supply one, or if they set SMOKE_API_KEY, use it.
    smoke_key = os.environ.get('SMOKE_API_KEY', '')

    if not smoke_key:
        skip(
            'inference skipped',
            'set SMOKE_API_KEY=sk-... to test inference end-to-end',
        )
    else:
        _api_key = smoke_key
        smoke_email = os.environ.get('SMOKE_API_EMAIL', email)

        grant_ok, inf_ok = test_grant_and_infer(
            base, admin_key, smoke_email, user_id, verbose,
        )
        record(grant_ok)
        record(inf_ok)

        if grant_ok and inf_ok:
            record(test_dashboard(base, smoke_key, smoke_email, verbose))

    # --- Cleanup ---
    section('6. Cleanup')
    cleanup(base, admin_key, user_id, email)

    # --- Phase 7: Rate limiting ---
    # Run last: deliberately fills the IP rate-limit bucket, which would
    # cause false 429s in any sections that follow within the same 60s window.
    section('7. Rate limiting')

    if args.skip_rate_limit:
        skip('rate limit test skipped (--skip-rate-limit)')
    else:
        print(f'  {_col(DIM, "sending 130 unauthenticated requests, this takes a few seconds...")}')
        record(test_rate_limit(base, verbose))

    # --- Summary ---
    total = passed + failed
    color = GREEN if failed == 0 else RED
    print(f'\n{_col(color, _col(BOLD, f"{passed}/{total} checks passed"))}\n')

    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
