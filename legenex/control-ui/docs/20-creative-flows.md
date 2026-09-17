# Creative Flows

GX-Playground → **Creative Flows** chains this cluster's creative services into
one repeatable graph: write a brief, turn it into prompts, generate images,
animate them, add a voice-over and a music bed, and export a finished video -
in one run, with every step visible.

Everything a flow does runs on gx10-01/gx10-02 through the same services the
other pages use (media router, `gx-music`, `gx-voice`, `gx-image`, `gx-video`,
the gateway text models). A flow never adds a new model, and it never calls a
third-party service.

## Prerequisites

* You are signed in to GX-Playground.
* The services a flow uses must be usable. Nodes whose backend is missing are
  listed but disabled, with the reason on the card (for example "No
  background-removal model is installed in ComfyUI on gx10-02").
* Flows and their runs are stored in the Library database on gx10-01. The
  media a run produces lands in the **Library** like any other generation,
  tagged with the flow and the run it came from.

## The page

**Creative Flows** opens on the **flow browser**: every flow with its size,
when it was last edited and how its last run ended.

| Action | What it does |
|---|---|
| **New flow** | an empty canvas |
| **From template** | a copy of a built-in (or your own) template |
| **Create with AI** | describe the result; a gateway model drafts the graph, which is then checked against the node catalogue and the connection rules before it opens for editing |
| **Open** / **Duplicate** / **Delete** | on each card. Deleting a flow keeps its runs' media in the Library |

Opening a flow puts its id in the address (`#/flows?flow=flow_…`), so a flow
can be bookmarked and shared with someone who uses the same Playground.

## The editor

Four areas:

1. **Toolbar** - flow name, save state, undo/redo, Run, the view switch, and
   the Templates, AI, Runs, Versions, Variables and Secrets dialogs.
2. **Node library** (left) - every node type, grouped by category, with a
   search box. Click a node to add it to the right of the flow, or drag it
   onto the canvas to drop it exactly where you want it.
3. **Canvas** (middle) - the graph. Pan, zoom, fit, minimap. Each card shows
   its typed input and output ports, its key settings inline, its live status
   while running and a preview of what it produced.
4. **Inspector** (right, a bottom sheet on phones) - every setting of the
   selected node plus **Run**, **Outputs**, **Logs** and **Payload** tabs.
   The Payload tab shows exactly what was sent to the service.

### Connections are typed

Ports carry a type: text, JSON, image, video, audio, voice or LoRA preset. An
output can only be connected to an input that accepts its type, an input that
takes a single connection refuses a second one, and a connection that would
create a loop is refused. When you try one anyway, the editor says why, for
example:

> Prompt accepts text, not audio. Add a conversion node in between.

The same rules run on the server when the flow is saved and again when it is
run, so a graph that reaches the engine is always well-formed.

### Working without a mouse

The canvas is not the only way to build a flow. **Outline** (toolbar, or the
`O` key) shows the whole graph as a list in execution order, and every canvas
action has an equivalent there: inspect, connect (a dialog that lists only the
connections that are allowed), disconnect, run, bypass, lock and delete.
Shortcuts are listed under the keyboard button, or press `?`.

| Keys | Action |
|---|---|
| `Ctrl + Z` / `Ctrl + Shift + Z` | undo / redo |
| `Ctrl + S` | save now |
| `Ctrl + Enter` / `Ctrl + Shift + Enter` | run the selected node / the whole flow |
| `Delete`, `Ctrl + D` | delete / duplicate the selection |
| `C`, `I`, `B`, `L` | connect, inspect, bypass, lock the selected node |
| `/`, `F`, `O`, `?` | search the library, fit the view, outline, shortcuts |

### Saving

Changes are saved automatically about a second after you stop typing, with the
flow's version number attached:

* **Offline** - the draft is kept in this browser and saved when the
  connection is back.
* **Changed elsewhere** - the save is stopped and you choose between reloading
  the server's version and saving yours on top.
* **Refused** - the server lists exactly what is wrong with the graph.

Every save makes a version. **Versions** lists them and restores any of them.

## Running a flow

**Run flow** runs everything. The Inspector and the node menu also offer
**Run node**, **Run from here**, **Run downstream**, **Regenerate (ignore
cache)** and, after a failure, **Run failed again**.

* Nodes run in dependency order; independent branches wait for the resources
  they need rather than competing for them.
* A node whose inputs and settings have not changed since its last successful
  run is **reused from cache** instead of generating again. **Regenerate**
  forces it.
* **Bypass** skips a node; **Lock** freezes it (its settings are read-only and
  its last result is reused).
* **Stop** cancels the run; a single node can be cancelled from its card.
* A Control Center restart marks a run **interrupted**; *Run failed again*
  picks it up from where it stopped.

The status bar shows what still has to be filled in before the flow can run
("2 thing(s) to fix before running: …"), and Run is disabled until it can.

**Runs** lists this flow's history: mode, result, duration, the nodes that ran,
the models used, how many assets came out and any resource waits.

## Flow variables and HTTP secrets

* **Variables** are named values (`{{name}}`) you can reuse in prompts, so one
  flow can be re-run for a different product, city or name without editing
  every node.
* **Secrets** are credentials for the Webhook and API Request nodes. The value
  is stored on gx10-01 with mode 0600, is referenced by name in the node's
  headers, and is never sent back to the browser or written to a log.

## Templates

**Templates** lists the built-in flows and your own. *Use template* creates a
new flow from it; *Save the current flow as a template* (in the editor) makes
one from what is on the canvas. Built-in templates can be duplicated but not
deleted.

## Errors

| What you see | What it means |
|---|---|
| A node card outlined in amber with a note | that node's backend is not installed or its choices could not be loaded. The flow still saves; the node cannot run. |
| "The server refused this graph" | the saved graph broke a rule; the exact issues are listed. |
| "This flow was changed somewhere else" | someone (or another tab) saved a newer version. Reload it, or save yours on top. |
| "Offline: changes are kept in this browser" | the browser cannot reach the Playground. Editing continues; the save is retried with a growing delay. |
| A red node with a message | that node's service failed. The Logs and Payload tabs show what was sent and what came back. |
| "waited: …" in the run history | the run queued behind another tenant for memory. Nothing was lost. |

## Accessibility and mobile

* Every control has a visible focus ring, a text label (icon-only buttons have
  an accessible name) and a keyboard path; the canvas is never the only way to
  do something.
* Node status is announced, not only coloured; the running state is also written out
  ("Running", "Done", "Failed").
* Below 1100 px the Inspector becomes a bottom sheet, below 760 px the node
  library does too; the toolbar wraps and the page never scrolls sideways.
* Animations (the running pulse, the fit-view tween) follow the reduced-motion
  preference from Settings or the operating system.

## Privacy

Prompts, node settings, run logs and payloads stay on gx10-01 in the Library
database. Generated media is stored in the Library. Nothing is sent to a
third-party service, and no credential ever reaches the browser.

## For maintainers

* Backend: `legenex/control-ui/gx_control_ui/flows/` (catalogue, schema,
  graph, engine, services, templates, AI drafting) with the routes in
  `routes_flo.py` and the tables in migration `030_flows.sql`.
* Browser API: `/api/flows…`, `/api/flow-runs…` (session cookie + CSRF, same
  origin). Public API: `/v1/flows`, `/v1/flow-runs`, `/v1/assets` with a
  gateway virtual key.
* Frontend: the only React page. Source in `legenex/playground/flows-ui/`
  (React + TypeScript + `@xyflow/react`, built by Vite), committed output in
  `legenex/playground/web/flows/`, loaded by the vanilla page wrapper
  `web/js/pages/flows.js`.
* After changing anything under `flows-ui/src`, run
  `(cd legenex/playground/flows-ui && npm run build && npm run qa)` and commit
  the rebuilt `web/flows/`. `npm run check:fresh` fails when the committed
  bundle is older than the sources.
