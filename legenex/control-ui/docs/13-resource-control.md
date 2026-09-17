# Resource Control

**Control Center → Resource Control** shows what every model is doing on both
nodes and lets you steer it without SSH. Automatic scheduling stays the
default. Manual controls go through the same admission checks as everything
else, so they can never push a node below its safety reserve.

## Resource profiles

The active profile is shown at the top. It sets **priorities**; it does not
simply stop everything else.

| Profile | What it prefers | What may happen to other work |
|---|---|---|
| **Auto** (default) | nothing in particular | idle work is unloaded when something else needs the memory; the rest waits in a queue |
| **Text / Agent** | gx-mini, gx-fast, gx-reason | image, video and music wait instead of unloading gx-reason |
| **Media** | gx-image, gx-video | an idle gx-reason or music engine on gx10-02 is unloaded for a media job; gx10-01 is not touched |
| **Music** | gx-music | idle ComfyUI weights are handed over (through the media router); an idle gx-reason is unloaded only if music still does not fit; video waits |
| **Max** | gx-max | both nodes are drained through the existing gx-max takeover; everything else waits until it is released |
| **Maintenance** | nothing new starts | running jobs finish, idle on-demand models unload, queued jobs wait; SSH, Tailscale, LiteLLM, this UI, watchdogs and Git sync keep running |

Before a switch the page shows what stays loaded, what is unloaded now, what
may be unloaded later, what is working and what is queued. Harmless switches
apply at once. A switch that stops running work asks for confirmation; Max
asks you to type `gx-max`. Switching from Max back to another profile
releases gx-max gracefully. When gx-max is released by any other path, the
profile returns to Auto by itself.

## Live resource map

One column per node with MemAvailable, swap, pressure, the admission ledger
and any holds. Every runtime shows one of these states:

| State | Meaning |
|---|---|
| READY | loaded (or, for media, weights warm) |
| UNLOADED | not loaded; it loads with the next request or job |
| LOADING | loading now |
| GENERATING | working on a request or job |
| WAITING | a job is queued for it and cannot start yet (the reason is shown) |
| DRAINING | unloading |
| BLOCKED | not allowed to start now (gx-max owns the cluster, or Maintenance) |
| ERROR | its service does not answer |

Memory figures are the **measured** node-level footprints (MemAvailable before
and after loading). Container memory is not used: on these unified-memory nodes
it leaves out the GPU pool.

## Manual controls

**LOAD**, **UNLOAD**, **DRAIN**, **PIN** and **UNPIN** appear only where a safe
lifecycle exists:

* **Load** checks admission first. If a load does not fit, the page explains
  why, for example: "gx-video needs 72 GiB plus the 30 GiB reserve on gx10-02,
  so 102 GiB must be available; 67 GiB is now. Holding memory: gx-reason". If
  idle tenants can make room, it
  offers **Unload gx-reason and continue**. Work that is running is never
  interrupted to make room.
* **Unload** stops the model. For a model that is generating, the page asks
  first; **Drain** waits until the current work is done, then unloads.
* **Pin** keeps a model loaded past its idle timer while memory allows. A pin
  never overrides the 30 GiB reserve, gx-max, Maintenance or admission. When
  a pin cannot be honoured, the tile says why ("suspended").

## Compatibility

The matrix computes, from live memory and the measured footprints, whether
two models can be loaded together:

* **Coexist**: both fit, in either order (for example gx-reason with
  gx-music).
* **Scheduler**: one order fits, so the scheduler waits or hands memory over.
* **Serialized**: one engine runs one job at a time (gx-image with gx-video).
* **Exclusive**: never together (gx-reason or gx-music with a cold gx-video
  job, gx-reason with a cold image, and anything with gx-max).

Click a cell for the numbers behind the verdict.

## The 30 GiB reserve (D-038)

Every single-node load keeps at least **30 GiB MemAvailable** on its node.
For gx10-02 the check is:

    available now − memory another load has not taken yet − this job's memory ≥ 30 GiB

* **Measured memory per job:** image 57 GiB, video 72 GiB, gx-music 32 GiB,
  gx-reason 45 GiB.
* **Loads in progress count.** The media router and the music supervisor
  each publish the memory their running job has not taken yet, so two loads
  that start together never both pass on the same free memory.
* **gx-video and gx-music** normally do not fit together (a cold video would
  leave about 16 GiB). A video therefore unloads an idle music engine first,
  and that unload is verified. Otherwise it waits: while music is working,
  while music is pinned, or while the Music profile is active. A music job
  submitted during a video waits for it.
* **An image next to loaded music fits** when the numbers allow. Admission
  is computed each time, not taken from a fixed table.
* **The keyframe video edit** (about 107 GiB) can never keep the reserve and
  is refused (B-028).

## Why am I waiting?

Every queued creative job carries a plain-language reason:

* Waiting for gx-reason to unload
* Waiting for gx-music to release enough gx10-02 memory
* Waiting for gx-video to finish on gx10-02 (a music job)
* Waiting for gx-max to release the cluster
* Waiting for enough gx10-02 memory
* Waiting for the current media job to finish
* Waiting for Maintenance mode to finish

The reason includes the memory needed and available, the 30 GiB reserve,
what blocks the job and what happens next. No ETA is ever shown.

## Maintenance mode

**Enter Maintenance** writes a hold file on both nodes. From then on:

* the admission guard refuses new launches;
* gx-reason's start command refuses to start;
* the media router refuses new jobs;
* gx-music refuses to load and unloads its idle engine;
* this UI queues new creative jobs.

A running job finishes first. **End Maintenance** removes the holds, returns
to Auto and lets queued jobs start. Nothing is loaded eagerly.

## Advanced resource policy

A read-only table lists, per alias:

* node and priority;
* idle TTL;
* cold and resident memory;
* residency, queueing and preemption;
* exclusive behaviour;
* where each number was measured.

These are the architecture's defaults; normal operation needs no changes here.
