# Kilo Code setup

Connect Kilo Code (VS Code extension or CLI) to the cluster as a custom
OpenAI-compatible provider. The **Setup → Kilo Code** page in the Control
Center shows the same values live, with copy buttons and a connection test.

**Use `gx-auto`.** It is the recommended Kilo model: simple or trivial
requests go to gx-mini, coding, action and tool work goes to gx-fast, and hard
reasoning goes to gx-reason. gx-auto never starts gx-max.

You need a gateway key first: **Setup → Kilo Code → Create API key** (or
**API Keys**). It pre-selects `gx-auto`, `gx-mini`, `gx-fast` and
`gx-reason`; add `gx-max` only if this client may take over both nodes. The
key is shown once; copy it into Kilo.

Verified against Kilo Code 7.7.2 (VS Code extension) and the 7.5.14 CLI on
gx10-01. Kilo 7 is built on opencode; older Roo-style settings
(`apiProvider`, `openAiBaseUrl`) no longer exist.

## Provider fields

In Kilo: **Settings → Providers**, then on the **Custom provider** card
("Add a custom provider by base URL.") click **Connect**.

| Field | Value |
|---|---|
| Provider ID | `gx-cluster` |
| Display name | `GX Cluster` |
| Provider API | `OpenAI Compatible` |
| Base URL | `http://100.105.214.61:4000/v1` |
| API key | the key from API Keys (optional in the form; `{env:GX_API_KEY}` also works) |
| Headers (optional) | leave empty |
| Models | **Add model** for each alias: ID and Name `gx-auto`, `gx-mini`, `gx-fast`, `gx-reason` (and `gx-max` if allowed). Tick **Image** for all except gx-max, **Reasoning** for gx-reason and gx-max. |

Click **Submit** and choose `gx-cluster / gx-auto`. The form has only the
**Reasoning** and **Image** toggles; tool calling and the context and output
limits are set in the JSON config ("Edit advanced settings in the JSON config
file", or the file below).

Copy these values one at a time:

```text
gx-cluster
```

```text
GX Cluster
```

```text
http://100.105.214.61:4000/v1
```

## Models and capability toggles

| Model | Use it for | Tool calling | Images (attachments) | Reasoning | Context | Max output |
|---|---|---|---|---|---|---|
| `gx-mini` | fastest answers, questions, small edits | on | on | off | 57344 | 8192 |
| `gx-fast` | **normal Kilo work**: coding, tools, agents, debugging | on | on | off | 98304 | 32768 |
| `gx-reason` | hard reasoning, architecture, difficult debugging | on | on | on | 49152 | 16384 |
| `gx-auto` | let the cluster choose per request | on | on | off | 57344 | 8192 |
| `gx-max` | explicit hardest work; **takes over both nodes** (about 10 minutes to start) | on | off | on | 262144 | 65536 |

What each one does:

* **gx-mini** is always loaded and answers in well under a second.
* **gx-fast** is the everyday Kilo model. It stays loaded, so a normal prompt
  starts immediately.
* **gx-reason** runs on gx10-02 and loads on first use (several minutes),
  then unloads after 15 idle minutes.
* **gx-auto** reads the *task* in Kilo's `<task>` block and ignores the size
  of Kilo's tool definitions: "are you there?" goes to gx-mini, repository
  changes go to gx-fast, hard debugging goes to gx-reason. It uses gx-max only
  when gx-max is already running; it never starts it.
* **gx-max** stops gx-mini, gx-fast, gx-reason and the media stack on both
  nodes while it runs. Choose it deliberately.

## Config file (optional)

Kilo reads `~/.config/kilo/kilo.jsonc` (global) or `.kilo/kilo.jsonc` in a
project. Keep the key in the environment variable `GX_API_KEY` (set before
VS Code or `kilo` starts), not in the file. The Setup page has a copy button
for the complete file:

```json
{
  "$schema": "https://app.kilo.ai/config.json",
  "model": "gx-cluster/gx-auto",
  "provider": {
    "gx-cluster": {
      "name": "GX Cluster",
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://100.105.214.61:4000/v1",
        "apiKey": "{env:GX_API_KEY}",
        "timeout": 900000
      },
      "models": {
        "gx-mini":   { "name": "gx-mini",   "tool_call": true, "attachment": true,  "reasoning": false, "limit": { "context": 57344,  "output": 8192 } },
        "gx-fast":   { "name": "gx-fast",   "tool_call": true, "attachment": true,  "reasoning": false, "limit": { "context": 98304,  "output": 32768 } },
        "gx-reason": { "name": "gx-reason", "tool_call": true, "attachment": true,  "reasoning": true,  "limit": { "context": 49152,  "output": 16384 } },
        "gx-auto":   { "name": "gx-auto",   "tool_call": true, "attachment": true,  "reasoning": false, "limit": { "context": 57344,  "output": 8192 } },
        "gx-max":    { "name": "gx-max",    "tool_call": true, "attachment": false, "reasoning": true,  "limit": { "context": 262144, "output": 65536 } }
      }
    }
  }
}
```

The settings form writes the same structure (it adds `"npm":
"@ai-sdk/openai-compatible"`, which is also the default).

## Check it works

```bash
curl -s http://100.105.214.61:4000/v1/models -H "Authorization: Bearer $GX_API_KEY"
```

You should see the `gx-*` aliases your key allows. In Kilo, send "are you
there?" with `gx-auto`: the answer comes from gx-mini within a few seconds.
**Setup → Kilo Code → Test connection** does the same through the gateway and
shows which tier gx-auto chose.

## Problems

| Symptom | Cause and fix |
|---|---|
| 401 Authentication Error | the key was revoked or mistyped; create a new one in API Keys |
| 403 "key not allowed to access model" | the key does not include that alias; replace it with one that does |
| gx-reason waits several minutes | it is loading on gx10-02; the next requests are fast |
| gx-max returns 503 | it could not take over the cluster; see Operations |
| Very long "Thinking" on gx-auto | should no longer happen (D-030); report the time and the Kilo mode used |
