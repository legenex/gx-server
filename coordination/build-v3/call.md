# gx-call — voice chat engine (Build V3, workstream CALL)

Owner: call specialist. Status: **engine Docker image building, acceptance pending**

## Status
- **gx-call Docker build**: building (bgp_0b15f784e0012E8MYXJWxmzp47)
- **gx-call-engine:voicechat-097dfe9-t214**: building
- **Acceptance tests**: pending (call_accept.py created)

## Acceptance Plan
1. Docker image build completion
2. Engine health check (/health returns "ready")
3. Call lifecycle test (create → join → audio → tool → response → end)
4. Real-time performance (latency, throughput)

## Relevant Files
- Engine: /legenex/call/engine/
- Acceptance test: /legenex/call/tools/call_accept.py
- Engine supervisor: /legenex/call/gx_call/engine.py
