# Other clients

Every client uses the same two values:

| Setting | Value |
|---|---|
| Base URL | `http://100.105.214.61:4000/v1` |
| API key | a virtual key from **Control UI → API Keys** |

```bash
export GX_BASE="http://100.105.214.61:4000/v1"
export GX_API_KEY="<key from Control UI → API Keys>"
```

## curl

```bash
curl -s "$GX_BASE/models" -H "Authorization: Bearer $GX_API_KEY"

curl -s "$GX_BASE/chat/completions" \
  -H "Authorization: Bearer $GX_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "gx-fast", "messages": [{"role": "user", "content": "Write a haiku about GPUs"}]}'
```

## Python (OpenAI SDK)

```python
import os
from openai import OpenAI

client = OpenAI(base_url=os.environ["GX_BASE"], api_key=os.environ["GX_API_KEY"], timeout=900)
reply = client.chat.completions.create(
    model="gx-auto",
    messages=[{"role": "user", "content": "Explain the CAP theorem in three sentences."}],
)
print(reply.choices[0].message.content)
```

## JavaScript (OpenAI SDK)

```js
import OpenAI from 'openai';

const client = new OpenAI({ baseURL: process.env.GX_BASE, apiKey: process.env.GX_API_KEY, timeout: 900_000 });
const reply = await client.chat.completions.create({
  model: 'gx-fast',
  messages: [{ role: 'user', content: 'Refactor this function to be pure: ...' }],
});
console.log(reply.choices[0].message.content);
```

## Kilo Code and Open WebUI

See **Kilo Code setup** and **Open WebUI setup**.

## Hermes, AgentOS, Buzz and other agent frameworks

Choose the framework's *OpenAI-compatible* (or "custom OpenAI") provider and
set:

| Field | Value |
|---|---|
| Base URL / API base | `http://100.105.214.61:4000/v1` |
| API key | a dedicated key from API Keys (one per application, so you can revoke it alone) |
| Model | `gx-fast` for agents with tools, `gx-auto` to let the cluster route, `gx-mini` for cheap classification steps |

Notes that apply to all agent frameworks:

* Tool calling works on `gx-mini`, `gx-fast`, `gx-reason`, `gx-auto` and `gx-max`.
* Streaming is supported on every text alias.
* Give agents a long timeout (15 minutes): gx-reason and gx-max can need
  minutes to load.
* Do not give an unattended agent `gx-max` unless it may take over both nodes.

## Images and video

See **Media: generate and edit** for `/v1/images/*` and `/v1/videos*`.
