# Promptly API: quick reference for AI agents

Base URL: `https://promptlyapi.com`
Auth: `Authorization: Bearer sk-<your-key>`
Protocol: OpenAI-compatible REST

Supported compatibility surfaces:
- `/v1/chat/completions` (full primary path)
- `/v1/responses` (text-generation profile)

## Chat completions

```bash
curl https://promptlyapi.com/v1/chat/completions \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "default",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

**Model selection:** the backend is multi-model. Omit `model` (or send `""`) to use the pinned default; send any name from `GET /v1/models` to use that specific model. The served model name is returned in the response.

**Latency:** the backend holds one model resident at a time. Requesting a different model than the one currently loaded pays that model's load time (a few seconds for small models, several minutes for the largest), and the first request after 5 minutes of silence is always a cold load. Clients that send many requests should stick to one model.

**Context window:** large (well over 100k tokens); the effective limit depends on the selected model, so query `/v1/models` rather than hardcoding a value.

**Reasoning:** reasoning models think by default and return their chain-of-thought in a non-standard `reasoning_content` field (ignore it; read `content`). Thinking-control fields are forwarded to the model unchanged - Qwen models disable thinking with `chat_template_kwargs: {"enable_thinking": false}` and set reasoning depth with `chat_template_kwargs: {"reasoning_effort": "low|medium|high|xhigh"}` (`none` errors - use `enable_thinking: false` to turn thinking off). A top-level `reasoning_effort` field is ignored by Qwen models - only the `chat_template_kwargs` form takes effect; harmony-style models (e.g. gpt-oss) use the top-level field instead.

## Responses API (text profile)

```bash
curl https://promptlyapi.com/v1/responses \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "default",
    "input": "Hello!"
  }'
```

This endpoint supports text-generation requests only. Multimodal input and tools are rejected with a clear `400` error.

## Response format

Standard OpenAI shape. Always read `choices[0].message.content`.

The models in the catalog are reasoning models. Responses include a non-standard `reasoning_content` field; ignore it, it is the model's internal chain-of-thought, not the answer:

```json
{
  "choices": [{
    "finish_reason": "stop",
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "Hello!",
      "reasoning_content": "The user says: \"Say hello.\" ..."
    }
  }],
  "model": "Qwen3.8-27B-UD-Q4_K_M.gguf",
  "usage": {
    "prompt_tokens": 70,
    "completion_tokens": 46,
    "total_tokens": 116
  }
}
```

## Streaming

Pass `"stream": true` in the request body. Responses are server-sent events (SSE), identical to the OpenAI streaming format.

## List models

```bash
curl https://promptlyapi.com/v1/models \
  -H "Authorization: Bearer sk-your-key"
```

## Error codes

| Status | Meaning |
|--------|---------|
| 400 | Invalid request (malformed JSON, unknown model, unsupported field) |
| 401 | Missing or invalid API key |
| 402 | Token balance exhausted; purchase more at `https://promptlyapi.com/dashboard?key=sk-your-key` |
| 429 | Rate limit exceeded (120 req/min per IP, 60 req/min per key) |
| 502 | Upstream inference server error |

## Python (OpenAI SDK)

This is the current `OpenAI(...)` client pattern. Both examples below are supported.

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

# Use .content; ignore .reasoning_content if present
print(completion.choices[0].message.content)
```

```python
import os

from openai import OpenAI

client = OpenAI(
    base_url="https://promptlyapi.com/v1",
    api_key=os.environ["PROMPTLY_API_KEY"],
)

response = client.responses.create(
    model="default",
    input="Hello!",
)

print(response.output_text)
```
