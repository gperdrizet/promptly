# Promptly API gateway

Promptly is an authenticated, metered API gateway for LLM inference.

## What you get

- Email-based registration and API key delivery
- Token-metered access to inference
- User dashboard with usage and balance
- Stripe and BTCPay top-up flows (planned; not yet available to users)
- OpenAI-compatible API surface for chat completions
- Text-focused Responses API compatibility profile

## Base URL

`https://promptlyapi.com`

## Authentication

Use a Bearer token in the `Authorization` header:

```http
Authorization: Bearer sk-<your-key>
```

## Core endpoints

- `POST /v1/chat/completions`
- `POST /v1/responses` (text profile)
- `GET /v1/models`
- `GET /health`

## Model selection

Promptly fronts a multi-model inference backend. The `model` field selects which model serves your request:

- **Omit it** (or send `""`) - the pinned default model is served.
- **Send any model name listed by `GET /v1/models`** - that model is served.

Query the live list rather than hardcoding, since the catalog changes as the backend is tuned.

**Latency note:** the backend holds one model resident at a time. Requesting a different model than the one currently loaded evicts it and pays the new model's load time (a few seconds for small models, several minutes for the largest ones), and the first request after 5 minutes of silence is always a cold load. The default model is the resident workhorse; clients that send many requests should stick to one model.

**Reasoning:** the models in the catalog are reasoning models. They think by default and return their chain-of-thought in a non-standard `reasoning_content` field (billed as regular output). Thinking-control fields are forwarded to the backend unchanged and are model-specific: Qwen models use `chat_template_kwargs: {"reasoning_effort": "low|medium|high|xhigh"}` to set reasoning depth and `chat_template_kwargs: {"enable_thinking": false}` to turn thinking off (a top-level `reasoning_effort` field is ignored by them); harmony-style models (e.g. gpt-oss) use the top-level field instead.

## Example use cases

- [Using Zed with Promptly](zed-coding-agent.md): set up the Zed editor's AI assistant to use Promptly as its backend.
- [Using Cline with Promptly](cline.md): configure the Cline autonomous coding agent extension for VS Code.
- [Using GitHub Copilot Chat with Promptly](copilot-vscode.md): add Promptly as a Bring Your Own Key model in VS Code's Copilot Chat.
- [Using OpenClaw with Promptly](openclaw.md): point your OpenClaw personal assistant at Promptly.

Continue with the API quickstart for copy-paste examples.
