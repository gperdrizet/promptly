# Operations

## Health check

```bash
curl -fsS https://promptlyapi.com/health
```

Expected response:

```json
{"status":"ok"}
```

## Rate limits

- 120 requests/min per IP
- 60 requests/min per API key

Counters live in the gateway process's memory (override with
`INFERENCE_IP_RATE_LIMIT` / `INFERENCE_USER_RATE_LIMIT`); stale keys are
swept periodically so the counters cannot grow without bound. Because they
are per-process, the gateway must run with exactly one uvicorn worker —
see [Deployment](#deployment).

## Balance behavior

When a user runs out of tokens, inference requests return `402 Payment Required`.

## Dashboard

Users can view balance and usage at:

`https://promptlyapi.com/dashboard?key=sk-your-key-here`

The key is shown in the URL only once: it establishes a short-lived session
cookie and then redirects to a key-free `/dashboard`.

## Deployment

Run the gateway with exactly **one** uvicorn worker (the `docker-compose.yml`
default; the Dockerfile `CMD` documents this too). Rate limits,
admin/dashboard sessions, and one-time checkout-return tokens are held in
process memory, so multiple workers would fragment the rate limits and drop
sessions between requests.
