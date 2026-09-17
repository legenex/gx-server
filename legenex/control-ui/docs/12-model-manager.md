# Model Manager

**Model Manager** installs, verifies, tests and assigns models, and removes
files nothing uses any more.

## Find and inspect

* Search Hugging Face, or paste `owner/name`, `owner/name@revision` or a full
  `https://huggingface.co/...` URL and choose **Inspect**.
* The page shows the exact commit SHA (every later step uses it, never a
  moving branch), size, files, licence, gated state, quantization, MoE,
  vision, context, likely runtime and candidate aliases.
* Adapters (LoRA), VAEs, text encoders and patch files (for example refusal
  directions) are labelled; they are not complete models.
* `trust_remote_code` models are flagged. Model-card text is shown as plain
  text and is never executed.

## Install

1. Choose the node and folder (`gguf`, `vllm`, `deepseek`, `staging`) and,
   optionally, file patterns (`*Q4_K_M.gguf, mmproj-*`).
2. **Plan install** checks disk space (download + 20 GiB margin) and access.
3. **Download and verify** downloads the pinned revision and checks the size
   and SHA-256 of every file, then writes `.gx-manifest.json`. Progress is
   shown live.

Gated models need a Hugging Face read token from an account that accepted the
model's terms: **Hugging Face access → Save token**. The token is stored on
gx10-01 (0600) and never shown again.

## Test, assign, roll back

1. **Test-serve** starts a temporary container (llama.cpp or vLLM) after the
   memory admission check and asks a real question. The production alias is
   not touched.
2. **Assign to gx-mini / gx-fast / gx-reason** is available only after a
   correct test answer. It shows the binding change, asks for confirmation,
   restarts that node's llama-swap and sends a real request through LiteLLM.
   If that fails, the previous binding is restored automatically.
3. The previous model stays on disk as the rollback point. **Roll back**
   restores it; **Accept** confirms the new model and makes the old files
   deletable.

gx-max is not assigned from this page: changing it needs both nodes and a
full two-node acceptance (Operations).

## Delete

**Delete** is refused while anything references the files: a current alias
binding, an unaccepted rollback point, `gx-max.conf` or a media workflow. You
must type the directory name to confirm. Deletion is permanent.

## Media components

gx-image and gx-video are built from checkpoints, LoRAs, text encoders and
VAEs referenced by vetted workflows. They appear in the inventory with their
verified revision and the workflows that use them.
