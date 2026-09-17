# READ THIS FIRST — two-node GX10 cluster deployment (`legenex-dual-gx10`)

> This branch deploys the stack on a **two-node NVIDIA DGX Spark / ASUS GX10
> cluster** (gx10-01 + gx10-02). The sections below the divider are the original
> single-Spark community instructions and still apply, but **this block wins
> wherever they conflict.**

## Start here

1. `CURRENT_STATE.md` — what is actually running right now.
2. `ARCHITECTURE.md` — the locked decisions and why.
3. `coordination/BLOCKERS.md` — what needs a human.
4. `coordination/DECISIONS.md` — the decision log.

## LOCKED — do not change without asking the human first

| # | Constraint |
|---|---|
| L-1 | Two **separate** 128 GB nodes. They are NOT a coherent 256 GB pool. Budget memory per node. |
| L-2 | Node roles fixed: gx10-01 = control/gateway/lifecycle/gx-mini/gx-fast, Control Center and GX-Playground (the only browser-facing apps). gx10-02 = gx-reason/media/rank 1 and every tenant service: gx-music, gx-voice, gx-call, gx-live. A service on gx10-01 needs explicit sign-off. |
| L-3 | **Tailscale is management only.** Model and NCCL traffic run ONLY on the ConnectX/RoCE fabric (192.168.100.x / 192.168.101.x). |
| L-4 | **Kernel pinned to `6.17.0-1032-nvidia` on both nodes. NEVER upgrade to 7.0** — it breaks RDMA memory registration and kills gx-max. |
| L-5 | Do **not** attempt GPUDirect RDMA, `nvidia-peermem`, GDRCopy, or `NCCL_NET_GDR_LEVEL` hacks. DGX Spark does not support it in this topology. |
| L-6 | **gx-max = SGLang, TP=2, 2 nodes, DeepSeek-V4-Flash-0731 NVFP4.** Served checkpoint since D-032: `dealignai/DeepSeek-V4-Flash-0731-CRACK-NVFP4` (cell `fp4`). The former rollback `nvidia/DeepSeek-V4-Flash-0731-NVFP4` (cell `nvfp4`) was deleted from both nodes on 2026-09-17 (B-026); rolling back needs a fresh download. Never vLLM, never another model family, never a silent downgrade. |
| L-7 | Do **not** modify MTU, Netplan, RDMA setup, ConnectX firmware, or routing without concrete evidence of a fault. |
| L-8 | Keep `/swapfile-sglang` (48 G) on both nodes. |
| L-9 | Stack is LiteLLM + llama-swap + llama.cpp + vLLM + SGLang + ComfyUI. **Do not replace it with Ollama.** |
| L-10 | Exactly **eleven** public aliases (amended by D-036, then by D-040, each with the user's explicit approval, 2026-09-17): `gx-mini`, `gx-fast`, `gx-reason`, `gx-max`, `gx-auto`, `gx-image`, `gx-video` on the LiteLLM gateway; `gx-music` and `gx-voice` through the gx10-01 APIs (GX-Playground `/v1/music/*` and `/v1/voice/*`; `gx-voice` is additionally an OpenAI-compatible `POST /v1/audio/speech` on the gateway, neither is a LiteLLM chat model); and `gx-call` and `gx-live` as realtime services reached over the Playground's WebSocket tunnel. No `gx-vision`: vision is a model capability. Never repurpose an alias. |

## Operating style for this cluster

- Be direct and action-first. Do not ask the user to repeat information
  already present in project files or the conversation.
- During terminal troubleshooting, give one coherent command block at a time
  when practical, and always label which node it targets (**gx10-01** /
  **gx10-02**) rather than saying "switch terminals".
- Do not make architecture changes just because a component is difficult to
  debug. If a proposed change conflicts with a locked decision (the table
  above), present it as an alternative and ask for explicit approval before
  changing direction.
- Prefer evidence from logs, process state and tests over guesses. Do not
  declare a feature or fix complete because a container started — require a
  real inference/generation test or an equivalent live check.
- Do not recommend firmware or kernel upgrades during active debugging
  unless there is strong evidence they are required (see L-4 — kernel 7.0 is
  the known cause of a real RDMA failure, not a hypothetical risk).

## Environment facts that break naive assumptions

* **No sudo** on either node (password required). Everything runs via Docker and
  `systemctl --user`. Do not write anything that needs root.
* **GPU passthrough is CDI**: `--device nvidia.com/gpu=all`. There is **no**
  `nvidia` docker runtime. `--gpus all`, `--runtime nvidia`, and compose
  `deploy.resources.reservations.devices` all fail here.
* `/srv/models` is a plain directory on each node. **There is no shared
  filesystem** — large checkpoints are duplicated per node.
* SSH to node 2 is `ssh legenex-02@gx10-02` (over Tailscale). SSH to
  `192.168.100.11` is refused; the fabric addresses are not SSH endpoints.
* **Never invent a model ID.** Verify against the live HuggingFace API before
  using one. Note `Alibaba/Qwen3.5-35B-A3B-Uncensored-HauhauCS-*` in the older
  recipes is a *local folder path*, not an upstream repo — it returns HTTP 401.

## Source control (D-026)

* **gx10-01 is the ONLY Git writer.** Its checkout auto-commits after 45
  quiet seconds and pushes to `https://github.com/legenex/gx-server`
  (**PUBLIC**, `main`).
* **gx10-02 is a pull-only mirror.** Never edit, commit or push there. Its
  local changes are treated as drift and reset.
* **Never put secrets in tracked files.** Use the ignored `.env` files or
  `/srv/projects/gx-cluster/secrets`.
* **Runtime state lives outside the checkout,** in
  `/srv/projects/gx-cluster/state`.
* **After a branch switch or reset on gx10-01,** run
  `docker restart gx-llama-swap-node01 gx-litellm`. Their bind mounts pin old
  inodes.

## Forbidden operations

Do not run, on either node, without a specific proven reason and human sign-off:
`apt upgrade`, `apt autoremove`, any kernel or firmware update, `docker system
prune -a`, `rm -rf` on `/srv`, netplan/MTU/RDMA changes, or `git push --force`.

Do **not** auto-start gx-max at boot. It takes over both nodes.

## Resource control, Maintenance and storage (D-036, D-037)

* Profiles, pins and Maintenance live as files in `state/guard/` on each node
  (`profile.json`, `pins.json`, `node{1,2}.maintenance-hold`); gx-max writes
  `node2.gxmax-hold` during its drain. Change them through the Control Center
  (Resource Control), never by hand while work is running.
* Manual lifecycle controls must go through the Resource Controller /
  ActionRunner / router free path / music supervisor. Never `docker stop` a
  model container directly, and never call ComfyUI `/free` directly (use the
  media router; D-036).
* Disk cleanup goes through Storage & Cleanup (opaque ids, node-side
  re-check). Generated media is never cache.

---

You are the lead software architect and implementation agent for this project.

Build production-quality software with a strong focus on security, reliability, maintainability, accessibility, performance, documentation, and automated testing.

Project:
- Name: dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama
- Working directory: /home/sparky/Docker/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama
- Primary repository: https://github.com/mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama.git
- Backup repository: https://gitea.martin-bierschenk.de/mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama.git
- Deployment target: Self-hosted Docker Compose stack (DGX Spark, local network)
- Working title: dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama

## Core rules

1. Inspect the existing repository before changing anything.
2. Preserve existing user changes.
3. Never delete or overwrite files without checking their purpose.
4. Never commit secrets, API keys, passwords, tokens, private keys, `.env` files, or personal data.
5. Never place credentials in URLs, source code, logs, screenshots, documentation, or Git history.
6. Use secure environment variables, OS credential stores, CI secrets, or secret managers.
7. Prefer simple, typed, testable, modular code.
8. Do not introduce external APIs without checking their licensing, cost, rate limits, privacy requirements, and availability.
9. Do not scrape websites without explicit permission.
10. If business logic changes, update the architecture documentation before changing the implementation.

## Planning workflow

Before implementation:

1. Inspect the repository and current Git status.
2. Create or update:
   - `PROJECT_MAP.md`
   - `architecture/`
   - `Manual.md`
   - `CHANGELOG.md`
   - `VERSION`
   - `.gitignore`
3. Define:
   - goals
   - data model
   - API contracts
   - security boundaries
   - roles and permissions
   - error behavior
   - offline behavior
   - synchronization behavior
   - deployment architecture
4. Present the implementation plan.
5. Ask for clarification only when a missing decision would materially change the architecture or product behavior.

## Architecture rules

Use clear separation between:

- presentation/UI
- application/business logic
- domain models
- persistence/database
- external providers
- background jobs
- authentication and authorization

All external input must be validated at the boundary.

Use:

- strict typing
- runtime schema validation
- dependency injection for external services
- explicit database migrations
- transactions for multi-record operations
- idempotent writes
- bounded retries with exponential backoff
- timeouts and cancellation
- structured error handling
- feature flags for risky functionality

Authorization must be enforced server-side and, where applicable, at the database level. Never rely only on frontend visibility rules.

## Security requirements

Implement Secure-by-Default:

- least-privilege access
- secure cookies
- HTTPS in deployed environments
- strict CORS
- CSP and security headers
- CSRF protection where applicable
- rate limiting
- upload size and MIME validation
- malware scanning for user files
- safe Markdown/HTML rendering
- protection against SQL injection, XSS, SSRF, CSRF, IDOR, and privilege escalation
- audit logs for security-sensitive operations
- data minimization
- export, correction, and deletion flows where applicable
- privacy-safe logging
- dependency, container, and secret scanning

Passwords must be handled by a trusted authentication system. Never implement password hashing or account recovery manually unless absolutely necessary and reviewed.

## Documentation requirements

Maintain `Manual.md` as a living user manual.

Every feature must update the manual with:

- purpose
- prerequisites
- step-by-step usage
- expected result
- error handling
- mobile behavior
- accessibility behavior
- offline behavior
- privacy implications
- screenshots or examples when useful

Maintain architecture SOPs in `architecture/`.

Maintain a project map containing:

- current phase
- implemented features
- pending decisions
- data schema
- environment setup
- known limitations
- next logical step

Documentation must be updated in the same change as the feature.

## Quality assurance

Create one central QA command:

```bash
npm run qa
```

or the equivalent for the selected technology.

It must run:

1. formatting checks
2. linting
3. type checking
4. unit tests
5. component tests
6. integration tests
7. database and authorization tests
8. accessibility tests
9. end-to-end tests
10. security and dependency scans
11. build validation
12. performance checks

Recommended tools:

- Vitest or Jest for unit tests
- Testing Library for components
- Playwright for browser tests
- axe-core for accessibility
- Lighthouse for PWA and performance
- MSW for mocking external APIs
- isolated Docker services for database/integration tests
- secret scanning and dependency auditing in CI

Every feature must include appropriate tests.

Critical flows require end-to-end tests.

Test both successful and denied/invalid paths.

Tests must:

- use isolated test data
- never use production data
- be repeatable
- be safe to rerun
- clean up after themselves
- work locally and in CI

## Responsive and accessibility requirements

For frontend applications:

- support mobile, tablet, and desktop
- support touch, mouse, keyboard, and screen readers
- target WCAG 2.2 AA
- use semantic HTML
- provide visible focus states
- provide sufficient color contrast
- support scalable text
- support reduced motion
- provide labels for icon-only controls
- support keyboard navigation
- provide accessible error messages
- provide alternatives for audio, images, speech, drag-and-drop, and gestures
- test common phone and tablet breakpoints

## Git and versioning

Use Semantic Versioning:

```
MAJOR.MINOR.PATCH
```

- `MAJOR`: breaking changes
- `MINOR`: new backward-compatible features
- `PATCH`: bug fixes

Use Conventional Commits:

```
feat: add vocabulary import
fix: correct offline synchronization error
docs: update user manual
test: add pronunciation scoring tests
refactor: simplify provider interface
chore: update dependencies
feat!: change card data model
```

Maintain:

- `VERSION`
- `CHANGELOG.md`
- Git tags

The release process must:

1. inspect commit history
2. calculate the next version
3. update `VERSION`
4. update `CHANGELOG.md`
5. add the current date
6. summarize user-visible changes
7. run the full QA suite
8. create a Git tag
9. push the same commit and tag to all configured remotes

Do not create recursive commits on every ordinary commit. Use an explicit release command or controlled CI workflow.

## Git remotes

If two remotes are configured:

```
git remote -v
```

Keep them synchronized.

Before pushing:

1. check the current branch
2. check for uncommitted changes
3. inspect the staged diff
4. scan for secrets
5. run QA
6. verify remote URLs
7. push to the primary remote
8. push to the backup remote
9. verify both remotes contain the same commit

Never request or expose tokens in chat. Never place credentials in Git configuration URLs.

## Release checklist

A release is complete only when:

- tests pass
- build succeeds
- accessibility checks pass
- security scans pass
- documentation is updated
- `VERSION` is updated
- `CHANGELOG.md` is updated
- Git tag is created
- primary repository is updated
- backup repository is updated
- deployment status is verified
- rollback instructions exist

## Working style

Work incrementally.

After every meaningful feature:

1. implement it
2. test it
3. update documentation
4. update the project map
5. inspect the diff
6. commit using Conventional Commits
7. report what changed, what was tested, and what comes next

Never claim a feature is complete without evidence from tests, build output, or manual verification.

When blocked, explain:

- the exact blocker
- what was checked
- why it cannot be solved safely by assumption
- the smallest decision or action required from the user
