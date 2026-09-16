# GX10 kernel lock — what it does and how to undo it

Applies to **gx10-01** and **gx10-02** (configuration is identical on both).

Locked production kernel: **6.17.0-1032-nvidia**
Kernel that must not become active: **7.0.0-1019-nvidia** (RDMA/NCCL memory-registration failures)

## What is held

Mechanism: `apt-mark hold` only. No files under `/etc/apt/preferences.d/` were added or
changed, and `/etc/default/grub` was not touched.

**Group 1 — NVIDIA HWE meta-packages** (all pinned at `7.0.0-1019.19~24.04.2`):

    linux-nvidia-hwe-24.04
    linux-image-nvidia-hwe-24.04
    linux-headers-nvidia-hwe-24.04
    linux-tools-nvidia-hwe-24.04
    linux-modules-nvidia-580-open-nvidia-hwe-24.04

**Group 2 — locked 6.17 runtime set** (all at `6.17.0-1032.32`):

    linux-image-6.17.0-1032-nvidia
    linux-modules-6.17.0-1032-nvidia
    linux-modules-nvidia-580-open-6.17.0-1032-nvidia
    linux-modules-nvidia-fs-6.17.0-1032-nvidia
    linux-headers-6.17.0-1032-nvidia
    linux-tools-6.17.0-1032-nvidia
    linux-nvidia-6.17-headers-6.17.0-1032
    linux-nvidia-6.17-tools-6.17.0-1032

## Read this before assuming the hold "keeps you on 6.17"

The HWE meta-packages had **already advanced to 7.0.0-1019 before the hold was applied.**
A hold freezes them where they are; it does not roll them back to 6.17, and nothing here
removes the 7.0 kernel (deliberately — it stays on disk).

So the two mechanisms do different jobs:

| Mechanism | What it actually protects |
|---|---|
| `GRUB_DEFAULT` pin | **This is what keeps 6.17 the booted kernel.** It is a name-based menu path, not an index, so adding kernels does not shift it. |
| Group 1 hold | Stops apt advancing the meta to a *newer* ABI (7.0.0-1020, 7.1.x, …) and dragging in more kernel images. |
| Group 2 hold | Stops `apt remove` / `apt autoremove` deleting the 6.17 kernel the GRUB pin names. If that kernel were removed, the pin would dangle and the next boot would fall through to 7.0. |

## Reverse it

Per node, as root. Either the script:

    sudo ~/gx-kernel-lock/apply-kernel-hold.sh --unhold

or by hand:

    sudo apt-mark unhold \
      linux-nvidia-hwe-24.04 \
      linux-image-nvidia-hwe-24.04 \
      linux-headers-nvidia-hwe-24.04 \
      linux-tools-nvidia-hwe-24.04 \
      linux-modules-nvidia-580-open-nvidia-hwe-24.04 \
      linux-image-6.17.0-1032-nvidia \
      linux-modules-6.17.0-1032-nvidia \
      linux-modules-nvidia-580-open-6.17.0-1032-nvidia \
      linux-modules-nvidia-fs-6.17.0-1032-nvidia \
      linux-headers-6.17.0-1032-nvidia \
      linux-tools-6.17.0-1032-nvidia \
      linux-nvidia-6.17-headers-6.17.0-1032 \
      linux-nvidia-6.17-tools-6.17.0-1032

To reverse only the meta freeze but keep the 6.17 kernel protected from removal,
unhold Group 1 only.

Confirm afterwards:

    apt-mark showhold

`apt-mark hold` writes nothing but the dpkg selection state, so unholding returns the
node exactly to its pre-change condition. Nothing needs a reboot, in either direction.

## When you do intend to move to 7.0

Unholding alone will not change the booted kernel — the GRUB pin still names 6.17.
A real migration means, in order: unhold Group 1, `apt full-upgrade`, then repoint
`GRUB_DEFAULT` in `/etc/default/grub` at the 7.0 menu entry, `update-grub`, reboot,
and re-validate RDMA/NCCL memory registration before any production workload.

## Limits of `apt-mark hold`

Respected by `apt`, `apt-get` and `aptitude`. It is **not** a security control — it is
bypassed by `apt install --allow-change-held-packages`, by a direct `dpkg -i`, and by
anything that calls dpkg itself. It is a guard against routine/accidental upgrades,
which is what was asked for.

## Checking state at any time

    ~/gx-kernel-lock/verify-kernel-lock.sh

Read-only, exits non-zero if any check fails.

## Also checked, left alone deliberately

- `nvidia-spark-run-apt-upgrade-once.service` is `enabled` on both nodes but **inert** —
  its `/var/lib/nvidia-spark-run-apt-upgrade-once/done` flag is set, so every boot since
  has logged `skipped because of an unmet condition check`. Left as-is. Worth knowing
  that if that flag is ever cleared, the service runs `apt-get full-upgrade` **and then
  reboots** unprompted.
- `apt-daily-upgrade.timer` is enabled, but `unattended-upgrades` is not installed and
  there is no `/etc/apt/apt.conf.d/20auto-upgrades`, so `apt.systemd.daily install`
  performs no upgrades. No automatic upgrade path is currently live on either node.
- User lingering: left enabled, per instruction. No conflict found with any of the above.
