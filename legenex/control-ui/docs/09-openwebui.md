# Open WebUI setup

Open WebUI connects to the gateway as an OpenAI-compatible connection. The
**Setup → Open WebUI** page shows these values live, with copy buttons and a
connection test. Verified against Open WebUI 0.11.3 (the container on
gx10-01).

1. Create a key: **Setup → Open WebUI → Create API key** (or **API Keys**).
   Allow `gx-auto`, `gx-mini`, `gx-fast`, `gx-reason` (and `gx-max` only if
   Open WebUI users may take over both nodes). Copy the key; it is shown once.
   Never use the gateway master key.
2. In Open WebUI: user menu → **Admin Panel** → **Settings** (a settings
   window opens), then in its sidebar **Admin → AI → Connections**.
3. Switch on **OpenAI API**. Under **Manage OpenAI API Connections** click
   **+** (Add Connection).
4. Fill in the **Add Connection** dialog:

   | Field | Value |
   |---|---|
   | Connection Type | External |
   | URL | `http://100.105.214.61:4000/v1` (Open WebUI on gx10-01 itself may use `http://127.0.0.1:4000/v1`) |
   | Auth | Bearer, then paste the key into **API Key** |
   | API Type | Chat Completions |
   | Advanced → Provider | Default |
   | Model IDs | `gx-auto`, `gx-mini`, `gx-fast`, `gx-reason` |

5. Click **Verify Connection**, then **Save**. Open WebUI lists models with
   `GET /v1/models`, so exactly the aliases your key allows appear.

```text
http://100.105.214.61:4000/v1
```

Leave **Provider** on Default: the LiteLLM option only changes how Open
WebUI proxies Anthropic-style requests. If you revoke a key, Open WebUI
receives 401 until you paste a new one.

| Model | Good for |
|---|---|
| `gx-auto` | let the cluster pick (recommended) |
| `gx-mini` | quick chat, summaries, simple image questions |
| `gx-fast` | code, longer answers, tool use |
| `gx-reason` | hard problems (slower to start) |
| `gx-max` | the hardest work; takes over both nodes |

Images, video and music are made in **GX-Playground**
(`http://100.105.214.61:8090/`). This setup connects Open WebUI for chat.
