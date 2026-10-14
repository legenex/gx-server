# Team speech backend

A private, authenticated CPU service on GX10-02 for the team voice studio.
It is separate from the existing gx-voice Qwen service and uses no CUDA.
The deployed Docker worker was found to trigger GX's exclusive-container guard.
The [native staging path](native/README.md) addresses this conflict without
changing gx-auto/gx-max admission rules or the two-node inference runtime.
Production now uses the reviewed native backend (initial accepted source382a1af)
through a private Unix socket and the existing restricted SSH relay. The old
Docker boot unit is disabled and only its speech container is stopped; its pinned
image/config remain available for explicit rollback. Text inference still depends
on the unchanged canonical GX memory/admission guards.
Kokoro ONNX 0.6.1 uses the upstream pinned v1.0 model; faster-whisper 1.2.1
uses Systran's base.en at immutable revision
`3d3d5dee26484f91867d81cb899cfcf72b96be6c`.

The upstream kokoro-onnx project is MIT licensed, Kokoro weights Apache 2.0;
faster-whisper is MIT licensed. Models are downloaded separately from Git,
Kokoro assets checked against published release SHA256s. Runtime model files
are read-only and networking for model downloads is disabled by HF_HUB_OFFLINE.

The image is built from the pinned Python multiarchitecture base and a complete
resolved Python dependency lock. Deployment supplies an immutable local image tag and
records its image ID. Service limits: 2 CPU cores, 3 GiB memory, 128 tasks,
one inference slot, no GPU access, 512 MiB temporary storage, rotated logs.
The container stops after five consecutive failed starts; the enabled user
unit brings up the pinned image on host boot. A native inference deadline kills
only this isolated process after300seconds; client jobs fail explicitly and are
never automatically replayed. Only loopback port 18835 is published. A separately authenticated private SSH
tunnel transports the studio's requests from KVM2. Never publish this port.

`GET /health` reveals only worker health. All inference/voices endpoints
require a service bearer credential mounted read-only from the user's private
configuration directory. It never lives in this repository. The studio has
its own human and agent authentication and per-user asset/job ownership.

`POST /v1/audio/speech`: JSON `input`, allowlisted preset `voice`, optional
`speed`. Returns 24kHz PCM16 WAV. Text is limited to 6000 characters.
`POST /v1/audio/transcriptions`: multipart WAV file, maximum 20MiB and ten
minutes, at most stereo. Returns English text. Audio is temporary and removed
after inference. Inference errors expose only error types, never input or keys.
Concurrent inference returns 429 and must be retried through the studio queue.

Voice cloning is unsupported by these selected models. The UI must clearly
identify preset voices and cannot claim cloning or accept arbitrary reference
recordings as a clone. Adding cloning requires a separate reviewed consent
and ownership boundary with a supported backend.

Primary sources:
- https://github.com/thewh1teagle/kokoro-onnx
- https://github.com/SYSTRAN/faster-whisper
- https://huggingface.co/Systran/faster-whisper-base.en

Source working copies remain on GX10-01. Deploy only packaged runtime files
and pinned models to GX10-02; build a reviewed image there without changing
the GX mirror checkout or its automatic reconciliation policy.
