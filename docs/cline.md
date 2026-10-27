# Using Promptly with Cline

[Cline](https://cline.bot) is a popular open-source autonomous coding agent extension for VS Code (and compatible editors). It has a dedicated **OpenAI Compatible** provider mode built for exactly this kind of setup: pointing it at any API that speaks the OpenAI Chat Completions format.

## 1. Get a Promptly API key

If you don't already have one, go to `https://promptlyapi.com`, create an account, and you'll receive an API key by email.

## 2. Install Cline

Install the **Cline** extension from the VS Code Marketplace (or your editor's equivalent extension marketplace).

## 3. Configure the OpenAI Compatible provider

Open Cline's settings (gear icon in the Cline panel) and set:

- **API Provider:** `OpenAI Compatible`
- **Base URL:** `https://promptlyapi.com/v1`
- **API Key:** your Promptly key (`sk-...`)
- **Model:** `default`

## 4. Set model configuration

Cline's OpenAI Compatible provider lets you fill in model metadata manually since it has no way to discover it automatically. Set:

- **Context Window:** `65536`
- **Max Output Tokens:** a reasonable ceiling for your use, e.g. `8192`
- **Image Support:** off (the current backend is text-only)
- **Computer Use / tool calling:** on, if you want Cline's agentic file-editing and command features to work

Click **Verify** (or equivalent) to confirm the connection works before starting a task.

## What you're actually talking to

- **Model:** the model you request. The `default` ID above selects the pinned default model (currently a Qwen3 reasoning model); send any other name from `GET /v1/models` to use that model instead.
- **Reasoning:** reasoning models think by default and return their chain-of-thought in `reasoning_content`; those thinking tokens bill the same as regular output. Disable thinking by sending `chat_template_kwargs: {"enable_thinking": false}`.

**Latency note:** the backend holds one model resident at a time. Switching to a model that isn't currently loaded pays its load time (a few seconds for small models, several minutes for the largest ones), and the first request after 5 minutes of silence is always a cold load. Leave Cline on `default` for steady workloads.

## Notes

- The `model` field is honored: `default` always maps to the pinned default model, so it keeps working even as the backend catalog changes. If you want a specific model, use its exact name from `GET /v1/models` — and expect a load delay the first time you switch to it.
- Cline's OpenAI Compatible provider forwards your request body largely as-is, so extra fields like `chat_template_kwargs` reach the backend. (A top-level `reasoning_effort` field is ignored by the Qwen models; to set reasoning depth pass `chat_template_kwargs: {"reasoning_effort": "low|medium|high|xhigh"}`, and use `enable_thinking` as above to turn thinking off.)
- Check your token balance any time at `https://promptlyapi.com/dashboard?key=sk-your-key-here`.
