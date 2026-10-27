# Using Promptly with GitHub Copilot Chat in VS Code

VS Code's Copilot Chat supports "Bring Your Own Key" (BYOK), including a **Custom Endpoint** provider for any API that speaks Chat Completions, Responses, or the Anthropic Messages format. Promptly speaks Chat Completions, so it plugs in directly - and this works even without a Copilot subscription, since BYOK models don't require signing into GitHub.

## 1. Get a Promptly API key

If you don't already have one, go to `https://promptlyapi.com`, create an account, and you'll receive an API key by email.

## 2. Open the Language Models editor

In VS Code, open the model picker in the Chat view and select **Manage Language Models** (gear icon), or run **Chat: Manage Language Models** from the Command Palette (`Cmd+Shift+P` / `Ctrl+Shift+P`).

## 3. Add Promptly as a Custom Endpoint

1. Select **Add Models**, then **Custom Endpoint** from the list.
2. Enter a group name, e.g. `Promptly`.
3. Enter your Promptly API key.
4. Select **Chat Completions** as the API type.
5. VS Code opens a `chatLanguageModels.json` file. Replace its contents (or add to it) with:

```json5
[
  {
    "name": "Promptly",
    "vendor": "customendpoint",
    "apiKey": "${input:promptlyApiKey}",
    "apiType": "chat-completions",
    "models": [
      {
        "id": "default",
        "name": "Promptly",
        "url": "https://promptlyapi.com/v1/chat/completions",
        "toolCalling": true,
        "vision": false,
        "contextWindow": 65536,
        "maxOutputTokens": 8192,
        "thinking": true,
        "modelOptions": {
          "temperature": 0.2
        }
      }
    ]
  }
]
```

6. Save the file, then select **Promptly** from the model picker in chat.

The `"id": "default"` entry selects the pinned default model, which keeps working regardless of backend catalog changes. To pin a specific model instead, add another entry to `models` with that model's exact name from `GET /v1/models` (expect a load delay the first time you switch to it, since the backend holds one model resident at a time).

> If the model doesn't appear right away, restart VS Code.

## Reasoning

The current model (Qwen3) is a reasoning model that thinks by default; its chain-of-thought comes back in a non-standard `reasoning_content` field and bills the same as regular output tokens. VS Code's **Thinking Effort** menu maps to a top-level `reasoning_effort` field, which this model ignores - it's forwarded but has no effect - so `supportsReasoningEffort` is intentionally left out of the config above. If your client can send extra body fields, set reasoning depth with `chat_template_kwargs: {"reasoning_effort": "low|medium|high|xhigh"}`, or turn thinking off with `chat_template_kwargs: {"enable_thinking": false}`.

## Tool calling caveat

`toolCalling: true` is set above so the model appears when using agent mode. If you find tool-calling behavior unreliable through this path, set it to `false` - the model will still work for regular chat and inline suggestions, it just won't be offered for agent sessions that require tool use.

## Notes

- This is entirely independent of GitHub Copilot's own hosted models; you don't need a Copilot subscription for BYOK models to work.
- If you're a Copilot Business/Enterprise user, your organization's administrator must have the **Bring Your Own Language Model Key in VS Code** policy enabled for this to be available.
- Check your token balance any time at `https://promptlyapi.com/dashboard?key=sk-your-key-here`.
