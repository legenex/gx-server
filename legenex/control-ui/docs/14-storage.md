# Storage and cleanup

**Control Center → Storage & Cleanup** shows the disk on both nodes and
removes only what is proven disposable.

## Health

| State | Free space on `/` |
|---|---|
| HEALTHY | 150 GiB or more (and below 85 % used) |
| WATCH | under 150 GiB, or 85 % used |
| LOW | under 75 GiB, or 92 % used |
| CRITICAL | under 30 GiB, or 97 % used |

The values are live, never typed in. The old gx10-02 state (19 GB free, 98 %)
would be CRITICAL; 328 GB free is HEALTHY.

## Scan

**Scan storage** runs on both nodes (usually a few seconds) and shows
progress while it works. It breaks usage down into:

* models;
* Docker images, build cache, volumes and containers;
* projects;
* caches;
* logs;
* temporary uploads;
* generated media;
* staging;
* everything else.

Every candidate is cross-checked against:

* the model registry, alias bindings and rollback points;
* gx-max.conf and the media workflows;
* the gx-music weights;
* running containers and their mounts;
* active downloads and builds;
* the Library.

## The three classes

**SAFE TO CLEAN**: temporary data past its retention, rotated logs, download
caches and the Docker build cache. Nothing running, referenced or in progress
uses them. Select items, or use **Select all safe items**, then **Clean
selected**. A dry run shows the total first; afterwards the page shows the
space actually recovered and a post-cleanup check (registry paths, media and
music weights, Library).

**REVIEW**: possibly useful. Examples:

* unused Docker images;
* model files no workflow or alias references;
* old acceptance evidence;
* the Stage A staging tree;
* runtime caches.

These are never removed automatically. Each needs your selection, a typed
confirmation, and **Maintenance mode** (Resource Control), so nothing can
start using them during the cleanup.

**PROTECTED**: cannot be selected. Examples:

* active checkpoints and gx-max;
* gx-image, gx-video and gx-music weights;
* shared text encoders and VAEs;
* rollback points;
* running or configured images and mounted paths;
* active downloads;
* secrets, Git, state and the Library.

Removing a model is a Model Manager workflow, not a disk cleanup.

## Safety model

* **Opaque ids only.** The browser sends only the ids of the latest scan's
  candidates. There is no path, command or free-text field.
* **Re-check at deletion time.** The node that owns the files classifies each
  item again right before removing it. An item that became mounted, active,
  referenced or modified since the scan is refused.
* **Filesystem limits.** Only paths under `/srv/models`, `/srv/cache`,
  `/srv/projects`, `/srv/logs` and `~/.cache` are considered. Symlinks are
  never followed or removed.
* **Docker limits.** Docker objects are removed by id. A running or configured
  image is never removed, and `docker system prune -a` is never used. The
  build cache is removed with `docker builder prune`.
* **Audit.** Scans and cleanups are audited (no secrets).

## Generated media

Images, video and music you created are **not** cache. They appear
separately and are managed in GX-Playground → Library, never by storage
cleanup.

## Model installs

Before any download, the Model Manager shows a **disk preflight**:

* current free space;
* download size;
* staging copy;
* final size;
* whether a cache copy is expected;
* peak requirement;
* free space afterwards;
* the 50 GiB minimum headroom.

An install that would not fit is blocked before it starts, with a link to
Storage & Cleanup.
