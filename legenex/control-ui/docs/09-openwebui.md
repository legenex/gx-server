# Open WebUI setup

Open WebUI connects to the gateway as an OpenAI-compatible connection.

1. Create a key: **Control UI → API Keys → Create key**. Allow the aliases
   you want in Open WebUI (for example `gx-mini`, `gx-fast`, `gx-reason`,
   `gx-auto`, `gx-image`). Copy the key; it is shown once.
2. In Open WebUI: **Admin Panel → Settings → Connections → OpenAI API → +**
   (add connection).
3. Fill in:

   | Field | Value |
   |---|---|
   | URL | `http://100.105.214.61:4000/v1` |
   | Key | the key from step 1 |
   | Connection type | External |

4. Save, then use the connection's refresh / verify button. Open WebUI lists
   models with `GET /v1/models`, so exactly the aliases your key allows
   appear in the model picker.
5. Optional, images: **Admin Panel → Settings → Images** → engine
   *OpenAI*, API base URL `http://100.105.214.61:4000/v1`, the same key,
   model `gx-image`, size `1024x1024`.

```text
http://100.105.214.61:4000/v1
```

Model discovery is automatic: never hunt for a master key in `.env` files.
If you revoke a key in the Control UI, Open WebUI starts receiving 401 until
you paste a new one.

| Model | Good for |
|---|---|
| `gx-mini` | quick chat, summaries, simple image questions |
| `gx-fast` | code, longer answers, tool use |
| `gx-reason` | hard problems (slower to start) |
| `gx-auto` | let the cluster pick |
| `gx-image` | image generation (Images settings) |
