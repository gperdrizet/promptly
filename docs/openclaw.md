# Using Promptly with OpenClaw

[OpenClaw](https://openclaw.ai) is an open-source personal AI assistant that runs on your own machine and meets you in the chat apps you already use (WhatsApp, Telegram, Discord, Slack, Signal, iMessage, and more). It connects to a range of hosted and local model providers through one Gateway - you can point that Gateway at Promptly.

## 1. Get a Promptly API key

If you don't already have one, go to `https://promptlyapi.com`, create an account, and you'll receive an API key by email.

## 2. Install OpenClaw

```bash
# macOS / Linux / WSL2
curl -fsSL https://openclaw.ai/install.sh | bash
```

```powershell
# Windows PowerShell
iwr -useb https://openclaw.ai/install.ps1 | iex
```

Then run onboarding:

```bash
openclaw onboard --install-daemon
```

Complete the onboarding wizard. See OpenClaw's [getting started guide](https://docs.openclaw.ai/start/getting-started) for channel setup (WhatsApp, Telegram, etc.) beyond model configuration.

## 3. Add Promptly as a custom model provider

Promptly isn't one of OpenClaw's built-in provider plugins, but OpenClaw supports arbitrary OpenAI-compatible endpoints through `models.providers` - the same mechanism it documents for local proxies like LM Studio and vLLM. Add this to your OpenClaw config:

```json5
{
  agents: {
    defaults: {
      model: { primary: "promptly/default" },
    },
  },
  models: {
    mode: "merge",
    providers: {
      promptly: {
        baseUrl: "https://promptlyapi.com/v1",
        apiKey: "${PROMPTLY_API_KEY}",
        api: "openai-completions",
        models: [
          {
            id: "default",
            name: "Promptly",
            reasoning: true,
            contextWindow: 65536,
            maxTokens: 8192,
          },
        ],
      },
    },
  },
}
```

Set `PROMPTLY_API_KEY` in your environment (or OpenClaw's `env.vars`) rather than hardcoding the key in config.

## 4. Verify and select the model

```bash
openclaw models list
openclaw models set promptly/default
```

`openclaw models list` should show `promptly/default` in the catalog. `openclaw models set` makes it the primary model without overwriting other provider config.

## What you're actually talking to

- **Model:** whichever model is currently loaded (query `/v1/models`); currently a Qwen3 reasoning model
- **Reasoning:** the model thinks by default and returns its chain-of-thought in `reasoning_content`; those tokens bill the same as output. Disable thinking with `chat_template_kwargs: {"enable_thinking": false}`.

## Controlling reasoning

The loaded model thinks by default. Disable it with `chat_template_kwargs: {"enable_thinking": false}`, or set reasoning depth with `chat_template_kwargs: {"reasoning_effort": "low|medium|high|xhigh"}`. (A top-level `reasoning_effort` field is ignored by the current Qwen model; only the `chat_template_kwargs` form takes effect.)

OpenClaw's generic `openai-completions` provider route forwards `params.chat_template_kwargs` into the outbound request body (verified against 2026.9.2). Set it globally at `agents.defaults.models["promptly/default"].params.chat_template_kwargs`, or per agent:

```json5
{
  agents: {
    entries: {
      main: {
        models: {
          "promptly/default": {
            params: { chat_template_kwargs: { enable_thinking: false } },
          },
        },
      },
    },
  },
}
```

Per-agent entries win over `agents.defaults` for that agent's runs. Thinking is on by default, so an agent that needs it off must say so explicitly - and agents that benefit from it (pair programming, complex ops tasks) should leave it on.

## Tool profiles: the big pitfall with local models

This is the one that costs the most debugging time. OpenClaw's default local onboarding sets `tools.profile: "coding"`, which registers a large tool catalog with the model. For a hosted API (Anthropic, OpenAI) that's invisible. For a local llama.cpp backend it is not:

- OpenClaw sends the tool definitions to the backend on every turn. When the session uses constrained tool calls, llama.cpp compiles the full JSON-Schema-to-GBNF grammar of **every registered tool** before decoding.
- llama.cpp hard-caps grammar repetition at 2000 (`MAX_REPETITION_THRESHOLD` in `src/llama-grammar.cpp`). Any string field with `maxLength > 2000` in the tool schema generates a `{char}{min,max}` rule that trips this and the whole request fails with HTTP 500.
- On a local server the tool payload also inflates the prompt and slows constrained decoding, so even schemas that *do* compile can push latency past OpenClaw's client timeout (`agents.defaults.timeoutSeconds`).

The concrete offender found in practice (OpenClaw 2026.9.x, `coding` profile): the **`automations`** tool (legacy alias `cron`) - its `trigger.script` field declares `minLength: "1", maxLength: "65536"`, so every turn's grammar blows past the 2000 limit and *every* tool-bearing request 500s, while plain text chats still work fine. That asymmetry is what makes it look flaky.

**Fix: trim the tool profiles per agent.** Tool policy pipeline is `global profile` -> `per-agent allow/deny` (deny wins; `*` wildcards supported). You don't need custom profiles - deny what each job doesn't use:

```json5
{
  tools: { profile: "coding" },
  agents: {
    entries: {
      // chat/research agent: no files, no shell, no session plumbing
      main: {
        tools: {
          deny: ["group:fs", "group:runtime", "group:sessions", "automations", "group:media"],
        },
      },
      // pair programming agent: full surface except the broken tool + media
      coder: { tools: { deny: ["automations", "group:media"] } },
      // sysadmin agent: files + shell, no session/goal machinery
      ops: {
        tools: { deny: ["group:sessions", "automations", "group:media"] },
      },
    },
  },
}
```

Built-in tool groups (2026.9.x, from OpenClaw's config-tools reference): `group:fs` = read/write/edit/apply_patch, `group:runtime` = exec/process/code_execution, `group:web` = web_search/web_fetch/x_search, `group:memory` = memory_search/memory_get, `group:sessions` = the 16 session tools, `group:automation` = heartbeat_respond/cron/gateway, `group:media` = image/tts/video/music generation, `group:ui`, `group:nodes`, `group:agents`, `group:messaging`.

General rule of thumb: an agent's tool schema should fit the job. Every tool you deny shrinks the grammar and the prompt on every turn.

## Three-agent Discord setup (reference)

A working multi-agent layout for the pair-programming + assistant use case: one Discord bot, three lanes, one channel per specialist agent.

- **Agents** live under `agents.entries.*` - each gets its own workspace, memory DB, and sessions. Agent `main` is the default agent (`default: true`) and owns unbound traffic.
- **Bindings** route a channel to an agent. Channel IDs are required (Discord: Settings -> Advanced -> Developer Mode, then right-click the channel -> Copy ID). Routing is read at gateway startup: `systemctl --user restart openclaw-gateway` after changing `bindings`.

```json5
{
  bindings: [
    {
      agentId: "coder",
      match: { channel: "discord", accountId: "default", peer: { kind: "channel", id: "<channel-id>" } },
    },
    {
      agentId: "ops",
      match: { channel: "discord", accountId: "default", peer: { kind: "channel", id: "<channel-id>" } },
    },
  ],
}
```

Verify with:

```bash
openclaw agents list --bindings
openclaw channels status
```

Gotchas discovered along the way:

- **Agents are lane-isolated by design.** Workspaces and memory indexes are per-agent (per-agent SQLite DB); there is no built-in context sharing. If two agents should know the same standing facts, share *files* (e.g. a `PROJECTS.md` in a location each agent can `read`), not memory.
- **Scheduled jobs belong to an agent.** `openclaw cron list --all` shows each job's owner; a job that runs `systemctl`/`journalctl` needs to be owned by an agent that still has `group:runtime` - reassign with `openclaw automations edit <job-id> --agent <id>`.
- **Recurring heartbeat** posts a "health sweep" summary to your channel every 30 minutes (OAuth) / 1 hour (API key) by default. It's separate from any explicit cron jobs you configured. Disable: `agents.defaults.heartbeat: { every: "0m" }`, then check `openclaw cron list --all` shows the `heartbeat:<agent>` row as disabled.
- Each config edit should be backed up first (`cp openclaw.json openclaw.json.bak-<change>`), and `openclaw doctor` will flag schema mistakes that would otherwise misroute silently.

## Notes

- Promptly ignores whatever `id` you send and always serves whichever model is currently loaded, so `"id": "default"` above will keep working if the backend model changes.
- Check your token balance any time at `https://promptlyapi.com/dashboard?key=sk-your-key-here`.
- OpenClaw's security model treats inbound messages (from WhatsApp, Telegram, etc.) as untrusted input by default - see OpenClaw's [security guide](https://docs.openclaw.ai/gateway/security) before connecting other users or exposing your Gateway remotely. This is independent of Promptly and applies regardless of which model provider you use.
