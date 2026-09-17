# Kilo Code setup

Connect Kilo Code (VS Code extension or CLI) to the cluster as a custom
OpenAI-compatible provider. You need a gateway key first:
**Control UI → API Keys → Create key** (allow `gx-mini`, `gx-fast`,
`gx-reason`, `gx-auto`, and `gx-max` only if this client may take over both
nodes). The key is shown once; copy it into Kilo.

## Provider fields

In Kilo: **Settings → Providers → Add custom provider**.

| Field | Value |
|---|---|
| Provider ID | `gx-cluster` |
| Display name | `GX Cluster` |
| Provider API | `OpenAI Compatible` |
| Base URL | `http://100.105.214.61:4000/v1` |
| API key | the key from Control UI → API Keys |
| Headers | leave empty |
| Models | pick from the fetched list (Kilo reads `GET /v1/models`) or add the five below |

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

Kilo can also read the provider from `kilo.jsonc`. Keep the key in an
environment variable, not in the file:

```json
{
  "$schema": "https://app.kilo.ai/config.json",
  "model": "gx-cluster/gx-fast",
  "provider": {
    "gx-cluster": {
      "name": "GX Cluster",
      "options": {
        "baseURL": "http://100.105.214.61:4000/v1",
        "apiKey": "{env:GX_API_KEY}"
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

If your Kilo version does not accept a custom provider id in the file, use
the settings form above: it produces the same configuration.

## Check it works

```bash
curl -s http://100.105.214.61:4000/v1/models -H "Authorization: Bearer $GX_API_KEY"
```

You should see the seven `gx-*` aliases your key allows. In Kilo, send
"are you there?" with `gx-auto`: the answer comes from gx-mini within a few
seconds.

## Problems

| Symptom | Cause and fix |
|---|---|
| 401 Authentication Error | the key was revoked or mistyped; create a new one in API Keys |
| 403 "key not allowed to access model" | the key does not include that alias; replace it with one that does |
| gx-reason waits several minutes | it is loading on gx10-02; the next requests are fast |
| gx-max returns 503 | it could not take over the cluster; see Operations |
| Very long "Thinking" on gx-auto | should no longer happen (D-030); report the time and the Kilo mode used |
