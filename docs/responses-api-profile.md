# Responses API profile

Promptly supports a text-generation compatibility profile for `POST /v1/responses`.

## Supported request features

- `input` as a plain string
- `input` as message-style text content
- `instructions` as a system-style instruction string
- `stream: true` for text output events
- `temperature`
- `max_output_tokens`

## Not currently enabled

- Multimodal input (`input_image`, audio, or other non-text content types)
- Tool calling and hosted tool features (`tools`, `tool_choice`, `parallel_tool_calls`)

Unsupported features are rejected with a clear `400` error.

## Example request

```bash
curl https://promptlyapi.com/v1/responses \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "default",
    "input": "Hello!"
  }'
```

## Notes on model name and output

- The `model` field selects the model: omit it (or send `""` / `default`) for the pinned default, or send any name from `GET /v1/models`.
- The backend holds one model resident at a time; switching models pays the new model's load time, and the first request after 5 minutes of silence is a cold load.
- For chat completions, read `choices[0].message.content`.
- Responses may include non-standard provider fields, such as `reasoning_content` in chat payloads.
