# Getting started

This is the operator and user guide for the **gx-cluster**, two ASUS GX10 /
NVIDIA DGX Spark systems (`gx10-01` and `gx10-02`) that serve a set of
OpenAI-compatible model aliases. It is rendered inside the control UI and is
also readable on GitHub under `legenex/control-ui/docs/`.

## Getting started

The cluster exposes **one** client endpoint: the LiteLLM gateway on gx10-01.

| What | Value |
|---|---|
| Base URL (from the tailnet) | `http://100.105.214.61:4000/v1` |
| Base URL (on gx10-01 itself) | `http://127.0.0.1:4000/v1` |
| Authentication | `Authorization: Bearer <your key>` |
| Model names | `gx-mini`, `gx-fast`, `gx-reason`, `gx-max`, `gx-auto`, `gx-image`, `gx-video` |
| Control UI | `http://100.105.214.61:8088/` (Tailscale or loopback only) |

Your first request:

```bash
export GX_API_KEY="<your LiteLLM key>"
curl -sS http://100.105.214.61:4000/v1/chat/completions \
  -H "Authorization: Bearer $GX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "gx-mini", "messages": [{"role": "user", "content": "Say hello in five words."}]}'
```

Expected result: a JSON body whose `choices[0].message.content` holds the
answer, and `usage` with token counts. `gx-mini` is always loaded, so this
answers in well under a second.

**Which alias?** Start with `gx-auto` if you do not want to choose. See
[Which model should I use?](/#/docs/models) for the full table.

**Getting a key.** Open **Control UI → API Keys → Create key**. Pick a name
and the aliases the client may use; the key is shown once, with a Copy
button. Create one key per application so you can revoke it alone. Never
paste a key into a ticket, a chat or a Git commit.

Setup guides: [Kilo Code](/#/docs/kilo-code), [Open WebUI](/#/docs/openwebui),
[other clients](/#/docs/clients).

## Remote access

* **Tailscale is the only remote path.** Join the tailnet, then use
  `100.105.214.61` (gx10-01). Nothing in this stack is published to the LAN
  or the internet.
* Traffic over Tailscale is encrypted by WireGuard even though the URLs are
  plain `http://`.
* The ConnectX addresses (`192.168.100.x`, `192.168.101.x`) are the private
  node-to-node fabric. They are not reachable from your laptop and are not
  SSH endpoints for people.
* SSH for administrators: `ssh legenex@gx10-01` and, from gx10-01,
  `ssh legenex-02@gx10-02`.

| Service | Address | Who may use it |
|---|---|---|
| LiteLLM gateway | `100.105.214.61:4000` | any tailnet client with a key |
| Control UI | `100.105.214.61:8088` | administrators (password) |
| Open WebUI | on gx10-01 (separate app) | people who prefer a chat UI |
| Orchestrator `:18900` | loopback + docker bridge | internal only |
| llama-swap `:28080` | loopback (node 1), fabric (node 2) | internal only |
| Media router `:18800` | fabric only | internal only |

> **Note** The control UI is a management surface that can unload models and
> take over both nodes. It is bound to loopback and the Tailscale address
> only, requires a password, and must never be port-forwarded.
