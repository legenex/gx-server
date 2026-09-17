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
2. **Plan install** checks access and runs the **disk preflight** on the
   target node:
   * it shows the free space, the download size, the 50 GiB headroom and the
     space left afterwards;
   * the verdict is SAFE (at least 50 GiB stays free at the peak), TIGHT
     (under 50 GiB would remain at the peak) or BLOCKED (under 50 GiB would
     remain afterwards, or the download does not fit). Only SAFE plans can
     be staged;
   * **Open Storage & Cleanup** jumps to the node's cleanup page.
3. **Download and verify** downloads the pinned revision and checks the size
   and SHA-256 of every file, then writes `.gx-manifest.json`. Progress is
   shown live.

## Hugging Face access: two gates, not one

A gated repository gates its **metadata** and its **files** separately. It will
answer a look-up perfectly well and still refuse every file until your account
has been granted access. Reading the model card therefore proves nothing about
whether the download will work, which is why the look-up reports a separate
**File access** row.

The token panel shows only live state: configured or not, valid or rejected,
the Hugging Face user it authenticates as, the token type and name, when it was
created, and whether it **can read gated repos**. The token itself is stored on
gx10-01 at `/srv/projects/gx-cluster/secrets/hf/token` (0600), is never shown
again and is never sent to the browser. A token file that is not 0600 is
reported as invalid, with the reason.

**File access** on a look-up says one of:

| Row | What it means | What to do |
|---|---|---|
| *public — no gate* | Not gated. | Nothing. |
| *granted for this account* | Your token's account may download the files. | Nothing. |
| *REFUSED — no usable token* | No token, or the token was not accepted. | **Save token**. A fine-grained token also needs *"Read access to contents of all public gated repos you can access"*. |
| *REFUSED — this account is not on the authorized list* | The token works and identifies you; the **account** has not been granted access to this repository. | Open the model page in a browser signed in as that user and accept its terms. **A new token cannot fix this.** |

Hugging Face's own message is shown verbatim underneath, with the exact action.
Watch for the difference between the third and fourth rows: they look alike and
need opposite responses, and confusing them is how an afternoon disappears into
minting tokens for a gate no token can open (B-030).

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
