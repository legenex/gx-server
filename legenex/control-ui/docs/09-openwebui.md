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

## Model identity

Open WebUI lists the aliases straight from the gateway. Without a model entry
it sends no system prompt, and a small model then answers "which model are
you?" from its training data: gx-mini once called itself the official
Qwen3.5, and another time Grok-3. The gateway routing was correct both times.

The Control Center therefore keeps one Open WebUI model entry per text alias
in the Open WebUI on gx10-01 (chat.legenex.co):

* **Name:** the alias.
* **Description:** names the underlying model.
* **System prompt:** a short, factual one built from the model registry
  (`legenex/models/registry.json`). It covers the alias, the underlying
  repository and revision, the base model, the size and precision, the
  runtime and the context limit, and an instruction not to claim to be any
  other model.

For gx-mini the model answers: *"I am gx-mini. My underlying model is
HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive, derived from
Qwen/Qwen3.5-4B."* gx-auto says it is the router and names no model.

**How the entries stay current:**
* **Setup → Open WebUI → Model identity** shows whether each entry matches the
  registry. **Sync identity from the registry** writes the missing or
  outdated ones.
* **Model Manager** re-syncs after an assignment or a rollback. Until the new
  model's facts are verified in the registry, its prompt names only the
  repository, revision and runtime.
* The daily integrity audit reports any drift.
* From a shell: `cd legenex/control-ui && python3 -m gx_control_ui.owui_identity check`
  (or `apply`).

**What the sync never changes:**
* Chats, users, connections or other models.
* An entry that someone created by hand. It is reported as "Edited outside
  the sync" and left alone.
* Visibility. The entries have no access grants, so, as before, the aliases
  are visible to administrators; share them in Open WebUI if other users
  should see them.

**Routing is proven separately:** by the gateway log, llama-swap and the
llama.cpp slot log (see `TEST_RESULTS.md` §21). A system prompt makes the
answer truthful; it does not decide which model answers.
