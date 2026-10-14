# Native CPU speech staging

This is the reviewed alternative to the speech Docker deployment, which was
found to block GX's exclusive-container admission check. It preserves the same
server and complete Python lock, CPU models and private service key. It never
changes the canonical GX guard, AppArmor, sysctls, other workloads or root access.
The Docker backend stays authoritative until independent review and an explicit
runtime transition. Staging alone does not establish GX model coexistence.

GX02's user manager cannot apply mount namespace sandboxing. Existing bubblewrap
also fails user-ID mapping; no privileged workaround is used. The native process
therefore uses maintained upstream Landrun v0.1.17, commit
`62823c05e58ec22c1f91b4c8468318c1f97f2d32` (MIT), and upstream Go-Landlock v0.9.0.
The recorded one-line patch omits only the unsupported ABI9 pathname Unix-socket
right. All filesystem, TCP and IPC scope rights supported by ABI7 stay handled.
`--log-enable-subprocesses` requires ABI7 strictly; there is no best-effort mode,
unrestricted access flag or custom syscall enforcement implementation.

Landlock grants read-only code, Python environment, CPU models, required native
libraries/data, selected nonprivate proc/sys metadata, and this service's own key.
Only its private RuntimeDirectory and /dev/null are writable. Launcher exec
environment is reduced to fixed LANG/PATH before Landrun; the backend receives
only explicitly allowlisted environment values, excluding manager credentials. Native inference
loads the same offline local models. The process listens on a private Unix socket,
not TCP. Standard systemd seccomp permits only AF_UNIX sockets and denies connect,
ptrace and cross-process memory syscalls. This closes outbound IPv4/IPv6 TCP/UDP
and pathname Unix connections; Landlock also confines external signals and
abstract Unix IPC. The SSH relay runs outside this sandbox and connects to the
private socket as the same owner, with the unchanged app-scoped authentication.

Actual systemd cgroup bounds are 3 GiB memory, zero swap, 200% CPU and 128 tasks.
Temporary files live in the user's tmpfs RuntimeDirectory and count against the
3 GiB cgroup limit; this is **not a separately enforced 512 MiB tmpfs quota**.
The per-file limit is 512 MiB; HTTP inputs remain limited by the server. Recovery
uses at most five starts per five minutes, with 20-second backoff. The existing
300-second inference deadline exits only this worker; requests are never replayed.

`build_landrun.sh` makes a user-private source build with a checksum-verified,
pinned official Go ARM64 SDK. `package.py` accepts only committed clean source
and packages runtime files, license and checksums. Copy that package to GX02
without a Git checkout, then run its `install.py` as legenex-02. It verifies every
package file, builds and checks the venv in a private partial directory, atomically
publishes the complete immutable release (repairing relocated entrypoints), and
installs only the explicit stage instance and its stage-only override. It never
modifies a shared template or production instance, and never starts/enables
production. The production transition installs its reviewed instance separately.

Start `gx-team-speech-native@stage.service`, then run `check_inference.py` outside
the sandbox. Run `check_isolation.py` through the launcher's exact Landlock argv
and the same unit seccomp/cgroup controls, with only a dedicated disposable process
as its target. Capture real cgroup files and stage receipts. Remove canaries and
disposable processes. Keep production Docker running throughout these checks.

After independent approval, a transition must: hold new Voice jobs; point
`current` at the reviewed release; explicitly install that release's reviewed
unit as `gx-team-speech-native@production.service` (never via the stage installer);
start and enable native@production; verify its
UDS health; replace only the existing speech relay's local destination with
`-R 127.0.0.1:14010:/run/user/1000/legenex-team-speech-production/backend.sock`;
restart that relay; verify real KVM2 TTS/STT and isolation; disable the old Docker
user unit and stop only its speech container. Native production and the relay
must recover across worker restarts. Retain the existing image, compose, key and
models. On failure, stop/disable native production, restore the relay destination
to 127.0.0.1:18835, and re-enable/start only the original speech Docker service.
Never remove other containers or weaken admission rules to obtain acceptance.

Finally repeat an actual canonical GX call. Removal of the speech container may
clear the container guard while the independent available-memory guard still
refuses a model. Report the actual outcome without claiming successful text
inference from speech success or source inspection.
