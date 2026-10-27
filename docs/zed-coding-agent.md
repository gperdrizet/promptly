# Using Promptly with Zed

[Zed](https://zed.dev) is a fast, open-source code editor with a built-in AI coding assistant. Zed can talk to any OpenAI-compatible API, so you can point it at Promptly and use it as your inference backend for chat, edits, and agentic coding.

## 1. Get a Promptly API key

If you don't already have one, go to `https://promptlyapi.com`, create an account, and you'll receive an API key by email. New accounts start with a free trial (1,000,000 tokens, 7 days).

## 2. Install Zed

Download Zed from [zed.dev/download](https://zed.dev/download) (macOS, Linux, and Windows are supported), or install it with your platform's package manager.

## 3. Add Promptly as an OpenAI-compatible provider

In Zed, open **Settings** (`Cmd+,` / `Ctrl+,`) and add an OpenAI-compatible provider pointed at Promptly. In `settings.json`, this looks like:

```json
{
  "language_models": {
    "openai_compatible": {
      "Promptly": {
        "api_url": "https://promptlyapi.com/v1",
        "available_models": [
          {
            "name": "default",
            "display_name": "Promptly",
            "max_tokens": 65536
          }
        ]
      }
    }
  }
}
```

Zed's exact settings UI and JSON schema can shift a bit between versions - if the fields above don't match what you see, search Zed's docs for "OpenAI-compatible provider" and use the same `api_url` and model values.

Set your API key via the environment variable Zed's provider setup expects (usually `PROMPTLY_API_KEY`), or paste it directly into the provider's API key field if Zed prompts for it. Don't commit your key to a repo or settings file that gets shared.

## 4. Pick the model in Zed's assistant panel

The `"name": "default"` placeholder above selects the pinned default model — Zed will keep working even as the backend catalog changes. If you'd rather pin a specific model, add another entry to `available_models` with that model's exact name from `GET /v1/models` (expect a load delay the first time Zed switches to it, since the backend holds one model resident at a time). Select "Promptly" as the provider and "default" as the model in Zed's assistant panel, then start chatting or ask it to make edits.

## What you're actually talking to

- **Model:** the model you request; `default` is the pinned default (currently a Qwen3 reasoning model). The full catalog is at `GET /v1/models`.
- **Reasoning:** reasoning models think by default and return their chain-of-thought in `reasoning_content`; those tokens bill the same as regular output. Disable thinking with `chat_template_kwargs: {"enable_thinking": false}`.

You can check the model catalog at any time:

```bash
curl https://promptlyapi.com/v1/models \
  -H "Authorization: Bearer sk-your-key-here"
```

## Suggested settings for interactive coding

For normal editor-driven coding work (not heavy agentic/batch use), a reasonable starting point is:

- Low temperature (e.g. `0.2`) for more predictable edits and completions
- Leave thinking on for most tasks; for faster, cheaper responses on simple edits, disable it with `chat_template_kwargs: {"enable_thinking": false}` if Zed lets you pass extra request fields, or set reasoning depth with `chat_template_kwargs: {"reasoning_effort": "low|medium|high|xhigh"}` (a top-level `reasoning_effort` field is ignored by the Qwen models)
- No need to change `max_tokens`/context settings from the default above unless you're working with unusually large files or long conversations

## Notes

- Promptly accepts `POST /v1/chat/completions` and `POST /v1/responses`; Zed uses chat completions.
- Because `default` always maps to the pinned default model, you don't need to update your Zed config if the backend catalog changes later. Only if you pin a specific model name and it is later removed from the catalog would you need to pick another.
- Check your token balance any time at `https://promptlyapi.com/dashboard?key=sk-your-key-here`.
