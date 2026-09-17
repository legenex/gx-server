-- D-040 VOI: gx-voice (Qwen3-TTS) saved voices, their versions, the consent
-- record for cloned voices, voice jobs and their takes.
-- A voice never holds model weights: a preset voice is a speaker name plus
-- style defaults; a designed or cloned voice is a reference clip (a Library
-- asset) plus its transcript. gx10-02 keeps a replica and a cached prompt.
CREATE TABLE IF NOT EXISTS voice_voices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('preset', 'designed', 'cloned')),
    description TEXT NOT NULL DEFAULT '',
    speaker TEXT,
    instructions TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'auto',
    reference_asset_id TEXT,
    reference_text TEXT,
    reference_sha256 TEXT,
    node_reference_id TEXT,
    x_vector_only INTEGER NOT NULL DEFAULT 0,
    source_job_id TEXT,
    source_take INTEGER,
    model_repo TEXT,
    model_revision TEXT,
    style_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    consent_id TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    synced_version INTEGER NOT NULL DEFAULT 0,
    deleted_at REAL
);
CREATE INDEX IF NOT EXISTS voice_voices_live ON voice_voices(deleted_at, name);
CREATE TABLE IF NOT EXISTS voice_versions (
    voice_id TEXT NOT NULL REFERENCES voice_voices(id),
    version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (voice_id, version)
);
-- Who confirmed permission to clone which recording, and what they confirmed.
CREATE TABLE IF NOT EXISTS voice_consents (
    id TEXT PRIMARY KEY,
    voice_id TEXT,
    job_id TEXT,
    reference_asset_id TEXT NOT NULL,
    reference_sha256 TEXT NOT NULL,
    statement TEXT NOT NULL,
    confirmed_by TEXT NOT NULL,
    via TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS voice_consents_asset ON voice_consents(reference_asset_id);
CREATE TABLE IF NOT EXISTS voice_jobs (
    id TEXT PRIMARY KEY,
    node_job_id TEXT UNIQUE,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    voice_id TEXT,
    request_json TEXT NOT NULL,
    node_request_json TEXT NOT NULL,
    user TEXT NOT NULL,
    via TEXT NOT NULL,
    owner TEXT,
    auto_save INTEGER NOT NULL DEFAULT 0,
    consent_id TEXT,
    flow_id TEXT,
    flow_run_id TEXT,
    flow_node_id TEXT,
    detail TEXT NOT NULL DEFAULT '',
    progress REAL,
    error_json TEXT,
    notes_json TEXT NOT NULL DEFAULT '[]',
    timings_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL,
    imported INTEGER NOT NULL DEFAULT 0,
    deleted_at REAL
);
CREATE INDEX IF NOT EXISTS voice_jobs_recent ON voice_jobs(deleted_at, created_at);
CREATE INDEX IF NOT EXISTS voice_jobs_open ON voice_jobs(imported, status);
CREATE INDEX IF NOT EXISTS voice_jobs_flow ON voice_jobs(flow_id, flow_run_id);
CREATE TABLE IF NOT EXISTS voice_takes (
    job_id TEXT NOT NULL REFERENCES voice_jobs(id),
    take_index INTEGER NOT NULL,
    seed INTEGER,
    duration_s REAL NOT NULL,
    sample_rate INTEGER NOT NULL,
    rms_dbfs REAL,
    peak REAL,
    waveform_json TEXT NOT NULL DEFAULT '[]',
    files_json TEXT NOT NULL DEFAULT '{}',
    asset_id TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (job_id, take_index)
);
CREATE INDEX IF NOT EXISTS voice_takes_asset ON voice_takes(asset_id)
