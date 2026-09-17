# Application database migrations (D-040)

Each feature owns its `NNN_<name>.sql` files here. `MediaLibrary` applies
every file that is not yet listed in `schema_migrations`, in name order, once,
inside a transaction, after writing `library.pre-<name>.db` next to the
database.

Rules:

* Only add tables, columns and indexes. Never rebuild or rewrite `assets`.
* Never edit a file after it has been applied on gx10-01; add a new one.
* Prefix every table with its feature (`flow_`, `wan_`, `voice_`, `call_`,
  `live_`, `image_`).
* Comments must be whole lines starting with `--`; no semicolons inside
  string literals.

| Prefix | Feature |
|---|---|
| 010 | assets provenance (lead) |
| 020 | Wan 2.2 LoRA library, pairs, presets, video history |
| 030 | Creative Flows |
| 040 | gx-voice |
| 050 | gx-call / Call Agents |
| 060 | gx-live |
| 070 | image models / edits |
| 080 | platform: Playground preferences (PLT) |
