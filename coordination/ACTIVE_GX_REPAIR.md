# GX Repair Active Plan

## PHASE 1 — ESTABLISH CURRENT GIT + LIVE CONFIG
[PASS] Git status: clean
[PASS] Git HEAD: aae3098cba900cb072692d5f50d915a2f9e11159
[PASS] Git origin/main: aae3098cba900cb072692d5f50d915a2f9e11159
[PASS] node02.yaml: exists, valid, contains DFlash speculative-config
[FAIL] GX10-02 gx-reason: DFlash NOT loaded (speculative_config=None in vLLM logs)
[INFO] GX10-02 model: wyattearp/Qwen3.8-27B-Uncensored-NVFP4 @ 91ec573a3d8e660b78b7161395e4a5b6247c2c8b
[INFO] GX10-02 DFlash2 downloaded: model.safetensors 3.85GB, total 4.1GB
[INFO] GX10-02 DFlash2 config: DFlash2DraftModel, 5 layers, block_size 8

## PHASE 2 — CAPTURE REAL PRE-DFLASH BASELINE
[FAIL] Pre-DFlash baseline not captured yet — gx-reason has been running without DFlash

## PHASE 3 — COMPLETE DFLASH2 DOWNLOAD
[PASS] DFlash2 model.safetensors exists on GX10-02: 3848817896 bytes
[PASS] Total dir size: 4.1G
[PASS] config.json exists
[PASS] README.md exists
[INFO] DFlash2 downloaded by unknown agent at Sep 18 22:09

## PHASE 4 — GX10-01 LOCAL WORK
[IN PROGRESS] OpenWebUI identity validation
[IN PROGRESS] Control Center Load/Unload/Restart CSRF

## PHASE 5 — DEPLOY DFLASH CONFIG
[PENDING] Fix node02.yaml --speculative-config parsing in llama-swap
[PENDING] Commit/push GX10-01
[PENDING] Reconcile GX10-02
[PENDING] Restart gx-reason with DFlash enabled

## PHASE 6 — START DFLASH AND PROVE IT IS REAL
[PENDING] Load gx-reason with DFlash2
[PENDING] Verify speculative decoding from logs/metrics

## PHASE 7 — TUNE DFLASH BUDGET
[PENDING] Benchmark budgets 7/8/12/16

## PHASE 8 — SGLANG COMPARISON
[PENDING] Conditional on vLLM+DFlash performance

## PHASE 9 — ADAPTIVE REQUEST BEHAVIOUR
[PENDING] Tool exposure scoping

## PHASE 10 — GX-MINI RESIDENCY
[PENDING] Verify gx-mini persistence

## PHASE 11 — GX-MAX TAKEOVER UX
[PENDING] Lifecycle state reporting

## PHASE 12 — OPENWEBUI NATIVE TOOL PATH
[PENDING] Fix DSML/tool_calls conversion

## PHASE 13 — ASK_USER PICKER
[PENDING] Interactive selection UI

## PHASE 14 — IMAGE_URL ERROR
[PENDING] Fix image payload injection

## PHASE 15 — CONTROL CENTER TRUTHFUL METADATA
[PENDING] Update model metadata

## PHASE 16 — REAL ACCEPTANCE TESTING
[PENDING] Full acceptance test suite

## PHASE 17 — RESOURCE SAFETY
[PENDING] Memory/swap monitoring

## PHASE 18 — REVIEW + TEST
[PENDING] Independent review pass

## PHASE 19 — GIT + DEPLOYMENT
[PENDING] Commit/push/pull sync

## PHASE 20 — FINAL SAFE STATE
[PENDING] Verify all services healthy
