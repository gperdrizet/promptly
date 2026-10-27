# Promptly API

[![Tests](https://github.com/gperdrizet/model-gateway/actions/workflows/test.yml/badge.svg)](https://github.com/gperdrizet/model-gateway/actions/workflows/test.yml)
[![Deploy Staging](https://github.com/gperdrizet/model-gateway/actions/workflows/deploy-staging.yml/badge.svg)](https://github.com/gperdrizet/model-gateway/actions/workflows/deploy-staging.yml)
[![Deploy Production](https://github.com/gperdrizet/model-gateway/actions/workflows/deploy-prod.yml/badge.svg)](https://github.com/gperdrizet/model-gateway/actions/workflows/deploy-prod.yml)
[![Python](https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Docker](https://img.shields.io/badge/Docker-compose-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

An authenticated, metered API gateway for LLM inference.

## Documentation

Full documentation: https://gperdrizet.github.io/promptly

## How it works

- Users register at **[https://promptlyapi.com](https://promptlyapi.com)** and receive a trial allocation (1M tokens, 7 days)
- API calls are made to `https://promptlyapi.com/v1` with a bearer token
- Each request deducts tokens from the user's balance; requests are rejected with 402 when exhausted
- Top up via Stripe (card) or BTCPay Server (Bitcoin) is planned but not yet available to users; the dashboard shows disabled buttons for this in the meantime
- All usage is recorded for metering and display on the dashboard

## Stack

- **FastAPI** + uvicorn: API server
- **PostgreSQL**: user accounts, token balances, usage events, purchases
- **Docker Compose**: gateway + db + adminer
- **nginx**: TLS termination and reverse proxy on the gateway server

## Using the API

### 1. Register

Go to **[https://promptlyapi.com](https://promptlyapi.com)** and click **Create an account**. Enter your email address and your API key will arrive by email within a few seconds.

Your account starts with a **free trial: 1,000,000 tokens valid for 7 days**.

> **Lost your key?** Go back to [https://promptlyapi.com/register](https://promptlyapi.com/register) and enter the same email address. A new key will be issued and sent to you; your token balance is preserved, but the old key is immediately invalidated.

### 2. Make your first request

The API is compatible with the current OpenAI Python SDK. Promptly supports both OpenAI-style Chat Completions (`/v1/chat/completions`) and a text-focused Responses API profile (`/v1/responses`).

Use this pattern with the Python SDK:

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="https://promptlyapi.com/v1",
    api_key=os.environ["PROMPTLY_API_KEY"],
)

completion = client.chat.completions.create(
    model="default",
    messages=[{"role": "user", "content": "Hello!"}],
)

print(completion.choices[0].message.content)
```

Or with **LangChain**:

```python
import os

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

llm = ChatOpenAI(
    base_url="https://promptlyapi.com/v1",
    api_key=os.environ["PROMPTLY_API_KEY"],
    model="default",
)

response = llm.invoke([HumanMessage(content="Hello!")])
print(response.content)
```

Or with `curl`:

```bash
curl https://promptlyapi.com/v1/chat/completions \
  -H "Authorization: Bearer sk-your-key-here" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "default",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### 3. Check your balance

Go to **[https://promptlyapi.com](https://promptlyapi.com)**, enter your API key in the **Already have a key?** box, and click **View dashboard**. You can also go directly to `https://promptlyapi.com/dashboard?key=sk-your-key-here`.

### 4. Top up

**Not yet available.** Top-up via **Stripe** (card) or **BTCPay Server** (Bitcoin) is planned; the dashboard shows disabled buttons for it as a preview. Once your trial runs out, wait for top-up to launch or contact the operator directly.

### API notes

- All `/v1/*` routes accept OpenAI-compatible request bodies and return OpenAI-compatible responses
- Streaming is supported (`"stream": true`)
- Requests are rejected with **402 Payment Required** when your balance is exhausted
- Rate limits: 120 requests/min per IP, 60 requests/min per API key

### Responses API compatibility profile

Promptly supports a text-generation profile of the OpenAI Responses API at `/v1/responses`.

Supported:

- `input` as a plain string
- `input` as message-style text content
- `instructions` as a system-style instruction string
- `stream: true` for text output events
- Usage reporting in the response payload

Not currently enabled:

- Multimodal input (`input_image`, audio, or other non-text content types)
- Tool calling and hosted tool features (`tools`, `tool_choice`, `parallel_tool_calls`)

Unsupported features are rejected with a clear `400` error instead of being silently ignored.

### Model selection

Promptly fronts a multi-model inference backend. The `model` field selects which model serves your request:

- **Omit it** (or send `""`) - the pinned default model is served.
- **Send any model name listed by `GET /v1/models`** - that model is served.

Query the live list rather than hardcoding, since the catalog changes as the backend is tuned:

```bash
curl https://promptlyapi.com/v1/models \
  -H "Authorization: Bearer sk-your-key-here"
```

**Latency note:** the backend holds one model resident at a time. Requesting a different model than the one currently loaded evicts it and pays the new model's load time (a few seconds for small models, several minutes for the largest ones). The first request after 5 minutes of silence always pays a cold load. The default model is the resident workhorse; heavy model switching is best avoided by clients that send many requests.

**Reasoning / thinking:** reasoning models think by default and return their chain-of-thought in a non-standard `reasoning_content` field (below), which bills as normal output tokens. Thinking-control fields are forwarded to the backend unchanged and are model-specific: Qwen models disable thinking with `chat_template_kwargs: {"enable_thinking": false}` and set reasoning depth with `chat_template_kwargs: {"reasoning_effort": "low|medium|high|xhigh"}`, while harmony-style models (e.g. gpt-oss) use a top-level `reasoning_effort` field. Control fields a model doesn't recognize are ignored - the Qwen models ignore the top-level `reasoning_effort` field, so use the `chat_template_kwargs` forms for them.

Responses include a non-standard `reasoning_content` field alongside the standard `content` field:

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "Hello!",
      "reasoning_content": "The user says: \"Say hello.\" ..."
    }
  }]
}
```

Always read `choices[0].message.content`. The `reasoning_content` field contains the model's internal chain-of-thought and is not part of the OpenAI spec; most clients will ignore it automatically.

For a compact reference designed to be dropped into an AI agent's context, see [AGENTS.md](AGENTS.md).


## Development

### Requirements

- Python 3.12
- Docker + Docker Compose

### Setup

```bash
git clone git@github.com:gperdrizet/model-gateway.git
cd model-gateway
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.template .env
# Edit .env: set DATABASE_URL, LLAMA_BASE_URL, etc.
```

### Run locally

```bash
docker compose up -d db          # start postgres only
uvicorn app.app:app --reload --port 8503
```

Or run the full stack:

```bash
docker compose up --build
```

### Run tests

```bash
pytest tests/ -v
```

Tests use an in-memory SQLite database; no Docker required. The full suite should pass.

## Deployment

### Infrastructure

- **Model server**: multi-model llama.cpp router on `:8502`, accessible over a private WireGuard tunnel
- **Gateway server**: VPS running nginx + Docker; model-gateway runs here behind nginx

### Production stack on the gateway server

```
/opt/model-gateway/          # production git repo and .env
/opt/model-gateway-staging/  # staging git repo and .env
```

nginx proxies `https://<your-domain>` to `http://127.0.0.1:8503` (production gateway).

### Environment files

Copy `.env.template` and fill in values. Key production overrides vs. defaults:

| Variable | Production value |
|---|---|
| `BASE_URL` | `https://promptlyapi.com` |
| `LLAMA_BASE_URL` | `http://100.64.0.2:8502` |
| `ADMINER_BIND_HOST` | `100.64.0.1` (tailnet only) |
| `GATEWAY_BIND` | `127.0.0.1` (behind nginx) |
| `GATEWAY_PORT` | `8503` |

Staging `.env` is the same but with `GATEWAY_PORT=8505`, `ADMINER_PORT=8506`, and `BASE_URL=http://127.0.0.1:8505`.

## CI/CD

### Branches

- **`dev`**: active development branch. All work happens here.
- **`main`**: production-ready code only. Protected; direct pushes are blocked.

### Workflow

1. Work on `dev`, commit and push changes
2. Open a pull request from `dev` to `main`
3. GitHub Actions runs the test suite automatically on the PR
4. Branch protection blocks merge until all tests pass
5. Merge the PR; staging deploy triggers automatically
6. Verify staging, then trigger production deploy manually

### On every push to `main` (after PR merge)

1. GitHub Actions runs the test suite (`pytest tests/ -v`)
2. If tests pass, SSHs to the gateway server and deploys to staging at port `8505`
3. Smoke tests `http://127.0.0.1:8505/health`

### Production deploy

Manual trigger only: go to **Actions, then Deploy to Production, then Run workflow**, enter a version number (e.g. `1.0.0`) and type `deploy` to confirm.

The workflow:
1. SSHs to the gateway server, pulls the latest commit into `/opt/model-gateway/`
2. Runs `docker compose up --build -d`
3. Smoke tests the health endpoint
4. Tags the commit as `v<version>` and creates a GitHub release with auto-generated notes

### Required GitHub secrets

| Secret | Value |
|---|---|
| `GATEKEEPER_HOST` | Gateway server public IP |
| `GATEKEEPER_USER` | SSH username on the gateway server |
| `GATEKEEPER_SSH_KEY` | Private key (matching public key in gateway server's `authorized_keys`) |

Generate a dedicated deploy key:
```bash
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/github_deploy -N ""
# Add ~/.ssh/github_deploy.pub to the gateway server's authorized_keys
# Add contents of ~/.ssh/github_deploy as the GATEKEEPER_SSH_KEY secret
```

## Staging environment

Staging runs on the same gateway server at port `8505`, accessible over the private WireGuard/tailnet network. It is deployed automatically on every merge to `main`.

**Tailnet address**: `http://100.64.0.1:8505`

### Manually testing the registration flow on staging

1. SSH into any tailnet machine (or use the gateway server itself):
   ```bash
   ssh user@100.64.0.1 -p 44441
   ```

2. Open the registration page in a browser pointed at the tailnet address, or use curl:
   ```bash
   curl -s http://100.64.0.1:8505/register
   ```

3. Submit a registration:
   ```bash
   curl -s -X POST http://100.64.0.1:8505/register \
     -d "email=test@example.com" \
     --include
   ```

4. The API key is sent by email. If SMTP is configured in the staging `.env`, check the inbox. Otherwise retrieve it directly from the admin panel or Adminer.

5. Test an authenticated request:
   ```bash
   curl http://100.64.0.1:8505/v1/chat/completions \
     -H "Authorization: Bearer sk-your-key-here" \
     -H "Content-Type: application/json" \
     -d '{"model": "default", "messages": [{"role": "user", "content": "ping"}]}'
   ```

6. Check the dashboard:
   ```bash
   curl -s http://100.64.0.1:8505/dashboard?key=sk-your-key-here
   ```

### Smoke test

`scripts/smoke-test.py` is a stdlib-only Python script that runs a full end-to-end check against any gateway deployment. It covers: health, registration, auth, admin panel, inference (optional), rate limiting, and cleanup.

**Quick start** (reads `ADMIN_KEY` from `.env` automatically):

```bash
python3 scripts/smoke-test.py
```

**With inference test** (supply an existing valid API key):

```bash
SMOKE_API_KEY=sk-your-key-here python3 scripts/smoke-test.py
```

**Against production** (skip the slow rate-limit hammer):

```bash
python3 scripts/smoke-test.py \
  --url http://100.64.0.1:8503 \
  --skip-rate-limit
```

**All options**:

```
  --url URL           Base URL (default: http://100.64.0.1:8505)
  --admin-key KEY     Admin key (default: $ADMIN_KEY or .env file)
  --skip-rate-limit   Skip the 130-request rate-limit stress test
  --verbose           Print extra detail on failures
```

The script creates a uniquely-named test user, runs all checks, then deletes the user via the admin panel. If cleanup fails, the user email is printed so you can remove it manually.

> **Note on inference testing**: the raw API key is emailed on registration and cannot be recovered from the admin panel (only the key prefix is stored). Set `SMOKE_API_KEY` to any existing valid key to enable the inference phase.

## Admin panel

The admin panel is at `/admin?key=<ADMIN_KEY>`.

Access is restricted by two independent layers:
1. **`ADMIN_KEY`**: must match the `ADMIN_KEY` env var (compared in constant time)
2. **IP CIDR check**: request must originate from the private WireGuard/tailnet range, localhost, or Docker bridge. Configured via `ADMIN_ALLOWED_CIDRS` in `.env`.

From outside the private network, `/admin` returns 403 regardless of key.

### Admin panel features

- View all users: email, key prefix, token balance, trial status, 30-day usage, join date
- Email filter search box for finding users quickly
- **Adjust tokens**: add or subtract from any user's paid token balance
- **Grant trial**: give a user a new trial allocation (tokens + days)
- **Delete user**: permanently removes the user and all associated records

### Admin operations via API

All admin actions can also be scripted directly against the API from any tailnet machine:

**Adjust a user's paid token balance** (positive = add, negative = deduct):
```bash
curl -X POST http://100.64.0.1:8503/admin/adjust \
  -d "key=<ADMIN_KEY>&email=user@example.com&delta=1000000" \
  --data-urlencode ""
```

**Grant a trial allocation**:
```bash
curl -X POST http://100.64.0.1:8503/admin/grant \
  -d "key=<ADMIN_KEY>&email=user@example.com&tokens=100000&days=7"
```

**Delete a user** (requires the numeric user ID from the admin panel):
```bash
curl -X POST http://100.64.0.1:8503/admin/delete \
  -d "key=<ADMIN_KEY>&user_id=42"
```

All three endpoints accept `application/x-www-form-urlencoded`. They return 200 on success, 403 if the key is wrong or the request IP is not in the allowed CIDR range.

### Adminer (database GUI)

Adminer runs at port `8504` (production) or `8506` (staging), bound to the private WireGuard/tailnet IP on the gateway server; not accessible from the public internet.

Access from a machine on the tailnet:
```
http://100.64.0.1:8504
Server:   db
Username: gateway
Password: (POSTGRES_PASSWORD from .env)
Database: gateway
```

#### Useful queries

View all users and balances:
```sql
SELECT u.email, u.key_prefix, b.paid_tokens, b.trial_tokens, b.trial_expiry
FROM users u
JOIN token_purchases b ON b.user_id = u.id
ORDER BY u.created_at DESC;
```

View recent usage events:
```sql
SELECT u.email, e.prompt_tokens, e.completion_tokens, e.created_at
FROM usage_events e
JOIN users u ON u.id = e.user_id
ORDER BY e.created_at DESC
LIMIT 50;
```

Manually credit a user (paid balance):
```sql
UPDATE token_purchases
SET paid_tokens = paid_tokens + 1000000
WHERE user_id = (SELECT id FROM users WHERE email = 'user@example.com');
```

## Billing

> **Status: not yet launched.** The backend, webhooks, and dashboard buttons for both payment methods exist, but the buttons are disabled in the UI until Stripe is out of test mode and BTCPay Server is deployed for real use.

### Stripe

Set `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` in `.env`. Register a webhook at `https://<your-domain>/stripe/webhook` in the Stripe dashboard with the `checkout.session.completed` event.

### BTCPay Server (Bitcoin)

A separate compose stack (`docker-compose.btcpay.yml`) runs BTCPay Server bound to your private network IP. Set `BTCPAY_URL`, `BTCPAY_API_KEY`, `BTCPAY_STORE_ID`, and `BTCPAY_WEBHOOK_SECRET` in `.env` after configuring the store.

## Token packs

| Pack id | Tokens | Price |
|---|---|---|
| `5m` | 5M | $0.50 |
| `25m` | 25M | $2.00 |
| `100m` | 100M | $6.00 |

Top-ups are currently **disabled in the UI** (the buy buttons show "Coming soon"); the packs above are the catalog that will be offered.
