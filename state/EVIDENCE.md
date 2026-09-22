# EVIDENCE

## Connectivity (2026-09-23)

- gx10-01 hostname `gx10-01`, Tailscale `100.105.214.61`
- gx10-02 Tailscale `100.73.238.4`, SSH `Host gx10-02` as `legenex-02` works
- RoCE A `192.168.100.10` ↔ `192.168.100.11` ping 0.14 ms, ethtool **200000Mb/s**
- RoCE B `192.168.101.10` ↔ `192.168.101.11` ping 0.14 ms, ethtool **200000Mb/s**
- RDMA: `rocep1s0f0` ACTIVE, `roceP2p1s0f0` ACTIVE

## Hugging Face (live API 2026-09-23)

- `junafinity/Ornith-1.5-9B-uncensored-GGUF-8bit` HTTP 200, sha `10d5524ca225…`, Q8_0 9527501056 B, oid `f22694e52003…1573`
- `0xKitkat/Ornith-1.5-35B-A3B-Uncensored-GGUF` HTTP 200, sha `ab0eed77c738…`, Q5_K_M 25347532768 B oid `6580229b119b…b061`, Q4_K_M 21713463264 B oid `081fa0babd0e…5bc9`

## Live services on gx10-01 (HTTP)

- :8088 Control UI 200
- :8090 Playground 200
- :3000 OpenWebUI 200
- :4000 LiteLLM 200 (401 without key — expected)
- :18900 orchestrator 200

## GitHub CLI

`gh auth status` — logged in as `legenex`, scopes include `repo`. Private repo creation is expected to work.
