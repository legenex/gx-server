# Using the API

All examples use placeholders. Set them once:

```bash
export GX_BASE="http://100.105.214.61:4000/v1"
export GX_API_KEY="<your LiteLLM key>"
```

## API quickstart

The gateway speaks the OpenAI API. Anything that accepts an OpenAI *base URL*
and *API key* works:

| Setting | Value |
|---|---|
| Base URL | `http://100.105.214.61:4000/v1` |
| API key | your LiteLLM key |
| Model | one of the seven `gx-*` aliases |

Endpoints you will use:

| Endpoint | Aliases |
|---|---|
| `GET /v1/models` | lists the seven aliases |
| `POST /v1/chat/completions` | `gx-mini`, `gx-fast`, `gx-reason`, `gx-max`, `gx-auto` |
| `POST /v1/images/generations` | `gx-image` |
| media router `POST /v1/videos` | `gx-video` (see Video generation) |

Rules that matter:

* Set `max_tokens` explicitly. `gx-reason` and `gx-max` spend output tokens
  on reasoning.
* Use long client timeouts for heavy tiers: 10 minutes for `gx-fast` and
  `gx-reason` cold starts, **one hour** for `gx-max`.
* Do not retry a `gx-max` 503 in a tight loop; it means the takeover was
  refused or failed, and the reason is in the message.

### How AgentOS, Hermes and Buzz should call it

These are OpenAI-compatible clients, so they need only base URL, key and
model:

* Point them at the **gateway** (`:4000`), never at llama-swap, SGLang
  (`:30000`) or the orchestrator (`:18900`). Those internal endpoints bypass
  authentication, accounting and the gx-max drain.
* Give each application its own virtual key.
* Use `gx-auto` or a specific single-node tier for routine and scheduled
  work. Scheduled jobs should **not** use `gx-max`: every call to it can take
  over the cluster for 9+ minutes.
* Use `gx-max` only for explicitly requested, long-running work, with a
  one-hour timeout, and handle HTTP 503 as "not available now".
* For tool calling, prefer `gx-fast`.

### Third-party OpenAI-compatible clients

Open WebUI, LibreChat, Continue, Cline, Aider, LangChain, LlamaIndex and the
official `openai` SDKs all work with the settings above. In tools that
auto-discover models, `GET /v1/models` returns exactly the seven aliases.

## curl examples

List models:

```bash
curl -sS "$GX_BASE/models" -H "Authorization: Bearer $GX_API_KEY"
```

Chat:

```bash
curl -sS "$GX_BASE/chat/completions" \
  -H "Authorization: Bearer $GX_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "gx-fast", "temperature": 0.2, "max_tokens": 300,
       "messages": [{"role": "system", "content": "You are concise."},
                    {"role": "user", "content": "Give three uses for a heat pump."}]}'
```

Streaming (Server-Sent Events):

```bash
curl -sSN "$GX_BASE/chat/completions" \
  -H "Authorization: Bearer $GX_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "gx-mini", "stream": true, "messages": [{"role": "user", "content": "Count to ten."}]}'
```

Reasoning tier (the answer is in `content`, the thinking in `reasoning_content`):

```bash
curl -sS "$GX_BASE/chat/completions" \
  -H "Authorization: Bearer $GX_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "gx-reason", "max_tokens": 3000,
       "messages": [{"role": "user", "content": "A bat and a ball cost 1.10 in total. The bat costs 1.00 more than the ball. What does the ball cost?"}]}'
```

gx-max (takes over both nodes if it is not running; allow an hour):

```bash
curl -sS --max-time 3600 "$GX_BASE/chat/completions" \
  -H "Authorization: Bearer $GX_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "gx-max", "max_tokens": 2000, "messages": [{"role": "user", "content": "Write a migration plan for moving a monolith to services."}]}'
```

## Python examples

```python
import os
from openai import OpenAI  # pip install openai

client = OpenAI(base_url=os.environ["GX_BASE"], api_key=os.environ["GX_API_KEY"], timeout=600)

resp = client.chat.completions.create(
    model="gx-auto",
    messages=[{"role": "user", "content": "Summarise the CAP theorem in two sentences."}],
    max_tokens=200,
)
print(resp.model, resp.choices[0].message.content, resp.usage)

# streaming
for chunk in client.chat.completions.create(
    model="gx-mini", stream=True,
    messages=[{"role": "user", "content": "Name five rivers."}],
):
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

gx-max with a long timeout and explicit failure handling:

```python
import openai
big = OpenAI(base_url=os.environ["GX_BASE"], api_key=os.environ["GX_API_KEY"], timeout=3600)
try:
    r = big.chat.completions.create(model="gx-max", max_tokens=4000,
                                    messages=[{"role": "user", "content": "…"}])
    print(r.choices[0].message.content)
except openai.APIStatusError as err:
    if err.status_code == 503:
        print("gx-max is not available right now:", err.message)  # never silently substituted
    else:
        raise
```

## JavaScript examples

```js
// Node 18+ / browsers (only from a server you control: never ship the key to a browser)
const res = await fetch(`${process.env.GX_BASE}/chat/completions`, {
  method: "POST",
  headers: {
    Authorization: `Bearer ${process.env.GX_API_KEY}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    model: "gx-fast",
    max_tokens: 300,
    messages: [{ role: "user", content: "Write a haiku about unified memory." }],
  }),
});
const data = await res.json();
console.log(data.choices[0].message.content, data.usage);
```

With the official SDK:

```js
import OpenAI from "openai"; // npm install openai
const client = new OpenAI({ baseURL: process.env.GX_BASE, apiKey: process.env.GX_API_KEY, timeout: 600_000 });
const stream = await client.chat.completions.create({
  model: "gx-mini",
  stream: true,
  messages: [{ role: "user", content: "List three prime numbers." }],
});
for await (const part of stream) process.stdout.write(part.choices[0]?.delta?.content ?? "");
```

## Vision input

`gx-mini`, `gx-fast` and `gx-reason` accept images (and `gx-auto` routes
image requests to a vision tier). `gx-max` does not. Send the image as an
`image_url` content part; a base64 data URL works everywhere:

```bash
IMG=$(base64 -w0 chart.png)
curl -sS "$GX_BASE/chat/completions" \
  -H "Authorization: Bearer $GX_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "gx-mini", "max_tokens": 200, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "What shapes and numbers are in this image?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,'"$IMG"'"}}]}]}'
```

```python
import base64
b64 = base64.b64encode(open("chart.png", "rb").read()).decode()
resp = client.chat.completions.create(model="gx-fast", max_tokens=300, messages=[{
    "role": "user",
    "content": [
        {"type": "text", "text": "Describe this image."},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ],
}])
```

Keep images under a few megabytes; the playground accepts up to 8 MiB.

## Tool calling

Standard OpenAI `tools` / `tool_calls`. `gx-fast` is the recommended tier.

```python
tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {"type": "object",
                       "properties": {"city": {"type": "string"}},
                       "required": ["city"]},
    },
}]
msgs = [{"role": "user", "content": "What's the weather in Cape Town?"}]
r = client.chat.completions.create(model="gx-fast", messages=msgs, tools=tools, tool_choice="auto")
call = r.choices[0].message.tool_calls[0]
print(call.function.name, call.function.arguments)   # get_weather {"city": "Cape Town"}

msgs += [r.choices[0].message,
         {"role": "tool", "tool_call_id": call.id, "content": '{"temp_c": 18, "sky": "clear"}'}]
final = client.chat.completions.create(model="gx-fast", messages=msgs, tools=tools)
print(final.choices[0].message.content)
```

`gx-mini` supports tools too; the gateway buffers its streaming output
(`fake_stream`) because llama.cpp's streaming tool parser is unreliable.

## Image generation

```bash
curl -sS "$GX_BASE/images/generations" \
  -H "Authorization: Bearer $GX_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "gx-image", "prompt": "a lighthouse at dusk, oil painting",
       "size": "1024x1024", "n": 1, "response_format": "b64_json"}' \
  | python3 -c 'import sys,json,base64; d=json.load(sys.stdin); open("out.png","wb").write(base64.b64decode(d["data"][0]["b64_json"]))'
```

| Field | Default | Notes |
|---|---|---|
| `prompt` | required | up to 4,000 characters |
| `size` | `1328x1328` | `WxH`, each 256–2048 and a multiple of 16 |
| `n` | 1 | 1–4 |
| `quality` | `standard` | `standard` = 4-step Lightning; `hd` = full sampling (~4 min) |
| `seed` | random | set it to reproduce an image |
| `negative_prompt` | — | what to avoid |

One image generates at a time across the cluster; others wait.

## Video generation

gx-video is asynchronous and is served by the media router on the fabric, so
run these from **gx10-01** (or use the control UI playground from anywhere).
The key is `GX_MEDIA_API_KEY` from the gateway `.env`.

```bash
MEDIA=http://192.168.100.11:18800/v1
ID=$(curl -sS -X POST "$MEDIA/videos" -H "Authorization: Bearer $GX_MEDIA_API_KEY" \
       -H "Content-Type: application/json" \
       -d '{"prompt": "waves rolling onto a beach at sunset", "seconds": 2, "size": "640x640"}' \
     | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
# poll until "completed" (about a minute)
curl -sS "$MEDIA/videos/$ID" -H "Authorization: Bearer $GX_MEDIA_API_KEY"
curl -sS "$MEDIA/videos/$ID/content" -H "Authorization: Bearer $GX_MEDIA_API_KEY" -o clip.mp4
```

| Field | Default | Notes |
|---|---|---|
| `prompt` | required | |
| `seconds` | 3 | 0.5–20 (frames = seconds × fps, snapped to 4k+1, max 161) |
| `length` | — | frame count instead of seconds |
| `fps` | 16 | 4–30 |
| `size` | `640x640` | multiples of 16 |
| `seed` | random | |

Status values: `queued`, `running`, `completed`, `failed`.
