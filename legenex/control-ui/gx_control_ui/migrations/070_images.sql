-- Build V3 IMG: durable history, per-model provenance and edit lineage for
-- gx-image. One row per image job in img_generations, one row per produced
-- asset in img_outputs, and one row per (model, checkpoint, workflow) actually
-- observed in img_checkpoints. The Library keeps the pixels; these tables keep
-- the answer to "which model made this, from what, with which edit plan".
CREATE TABLE IF NOT EXISTS img_generations (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    user TEXT,
    status TEXT NOT NULL,
    kind TEXT NOT NULL,
    image_model TEXT,
    image_model_label TEXT,
    image_model_family TEXT,
    model_repository TEXT,
    model_revision TEXT,
    workflow TEXT,
    edit_mode TEXT,
    edit_quality TEXT,
    denoise REAL,
    strength_applied REAL,
    masked INTEGER NOT NULL DEFAULT 0,
    mask_coverage REAL,
    mask_sha256 TEXT,
    mask_source TEXT,
    prompt TEXT NOT NULL DEFAULT '',
    prompt_sent TEXT,
    prompt_suffix TEXT,
    negative_prompt TEXT,
    adapter_strength REAL,
    uncensored INTEGER,
    quality_tags INTEGER,
    quality TEXT,
    width INTEGER,
    height INTEGER,
    seed INTEGER,
    steps INTEGER,
    guidance REAL,
    batch INTEGER NOT NULL DEFAULT 1,
    title TEXT,
    source_asset_id TEXT,
    router_job TEXT,
    started_at REAL,
    finished_at REAL,
    duration_seconds REAL,
    request TEXT NOT NULL DEFAULT '{}',
    router TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    error_detail TEXT,
    flow_id TEXT,
    flow_run_id TEXT,
    flow_node_id TEXT
);
CREATE INDEX IF NOT EXISTS img_generations_created ON img_generations(created_at);
CREATE INDEX IF NOT EXISTS img_generations_model ON img_generations(image_model, created_at);
CREATE INDEX IF NOT EXISTS img_generations_source ON img_generations(source_asset_id);
CREATE INDEX IF NOT EXISTS img_generations_status ON img_generations(status, created_at);
CREATE INDEX IF NOT EXISTS img_generations_flow ON img_generations(flow_id, flow_run_id);
-- One row per image the job produced. For an edit or a variation the row also
-- carries the lineage (which asset it came from) and how far the result moved
-- from it, so "the edit came back as a copy" is a number, not an impression.
-- similarity_method records how those numbers were obtained; it is NULL when
-- no comparison ran.
CREATE TABLE IF NOT EXISTS img_outputs (
    asset_id TEXT PRIMARY KEY,
    generation_id TEXT NOT NULL,
    idx INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    operation TEXT NOT NULL,
    image_model TEXT,
    workflow TEXT,
    width INTEGER,
    height INTEGER,
    bytes INTEGER,
    sha256 TEXT,
    dhash TEXT,
    source_asset_id TEXT,
    source_dhash TEXT,
    similarity_method TEXT,
    similarity_distance INTEGER,
    similarity_score REAL,
    near_duplicate INTEGER
);
CREATE INDEX IF NOT EXISTS img_outputs_generation ON img_outputs(generation_id, idx);
CREATE INDEX IF NOT EXISTS img_outputs_source ON img_outputs(source_asset_id, created_at);
CREATE INDEX IF NOT EXISTS img_outputs_model ON img_outputs(image_model, created_at);
-- Per-model provenance as observed at run time: the checkpoint repository and
-- revision the registry reported for the workflow that actually ran. It is a
-- record of what the cluster did, not a copy of the registry.
CREATE TABLE IF NOT EXISTS img_checkpoints (
    image_model TEXT NOT NULL,
    workflow TEXT NOT NULL,
    repository TEXT NOT NULL DEFAULT '',
    revision TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    generations INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (image_model, workflow, repository, revision)
)
