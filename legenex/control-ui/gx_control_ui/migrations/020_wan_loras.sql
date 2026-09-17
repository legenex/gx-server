-- D-040 WAN: Wan 2.2 LoRA library, manual pairs, presets and video generation history.
-- The LoRA files themselves live on gx10-02 and are described by the media
-- router (GET /v1/loras). These tables only hold what the user decided and
-- what the application observed. No file is ever renamed or modified.
--
-- Every discovered file: when it was first and last seen (date discovered).
CREATE TABLE IF NOT EXISTS wan_lora_files (
    name        TEXT PRIMARY KEY,
    root        TEXT NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    missing     INTEGER NOT NULL DEFAULT 0
);
-- A user-made pair: this high-noise file goes with this low-noise file.
CREATE TABLE IF NOT EXISTS wan_lora_pairs (
    id          TEXT PRIMARY KEY,
    high_name   TEXT NOT NULL UNIQUE,
    low_name    TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL,
    created_by  TEXT NOT NULL,
    CHECK (high_name <> low_name)
);
-- Files whose automatic pairing the user split (unpair of an auto pair).
CREATE TABLE IF NOT EXISTS wan_lora_unpaired (
    name        TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    created_by  TEXT NOT NULL
);
-- Per library entry (a pair or a single file): names, notes and defaults.
CREATE TABLE IF NOT EXISTS wan_lora_settings (
    entry_id      TEXT PRIMARY KEY,
    display_name  TEXT,
    description   TEXT,
    tags          TEXT NOT NULL DEFAULT '[]',
    default_high  REAL,
    default_low   REAL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    allow_unknown INTEGER NOT NULL DEFAULT 0,
    position      INTEGER,
    updated_at    REAL NOT NULL
);
-- Saved generation settings. Ids are stable (Creative Flows reference them).
CREATE TABLE IF NOT EXISTS wan_presets (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    builtin     INTEGER NOT NULL DEFAULT 0,
    data        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    created_by  TEXT NOT NULL
);
-- One row per Wan text-to-video generation submitted through the LoRA-aware path.
CREATE TABLE IF NOT EXISTS wan_generations (
    id                TEXT PRIMARY KEY,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    user              TEXT NOT NULL,
    status            TEXT NOT NULL,
    prompt            TEXT NOT NULL,
    negative_prompt   TEXT,
    seed              INTEGER,
    model             TEXT NOT NULL,
    high_model        TEXT,
    low_model         TEXT,
    loras             TEXT NOT NULL DEFAULT '[]',
    chains            TEXT NOT NULL DEFAULT '{}',
    width             INTEGER,
    height            INTEGER,
    frames            INTEGER,
    fps               REAL,
    seconds           REAL,
    settings          TEXT NOT NULL DEFAULT '{}',
    request           TEXT NOT NULL DEFAULT '{}',
    preset_id         TEXT,
    title             TEXT,
    workflow_version  TEXT,
    workflow_json     TEXT,
    comfy_prompt_id   TEXT,
    router_job        TEXT,
    started_at        REAL,
    finished_at       REAL,
    duration_seconds  REAL,
    asset_id          TEXT,
    output_path       TEXT,
    error_code        TEXT,
    error_message     TEXT,
    error_detail      TEXT,
    flow_id           TEXT,
    flow_run_id       TEXT,
    flow_node_id      TEXT
);
CREATE INDEX IF NOT EXISTS wan_generations_created ON wan_generations(created_at);
CREATE INDEX IF NOT EXISTS wan_generations_status ON wan_generations(status, created_at);
CREATE INDEX IF NOT EXISTS wan_generations_asset ON wan_generations(asset_id);
-- Neutral example presets. They reference no LoRA files (none are shipped).
INSERT OR IGNORE INTO wan_presets (id, name, description, builtin, data, created_at, updated_at, created_by) VALUES
('wp_5c1e7a2b90d34f01', 'Cinematic Realism', 'Wide frame, film-style wording and a realism-oriented negative prompt.', 1,
 '{"version": 1, "loras": [], "size": "832x480", "seconds": 3.0, "fps": 16, "seed_mode": "random", "seed": null, "prompt_suffix": "cinematic lighting, shallow depth of field, natural film grain, realistic skin texture", "negative_prompt": "blurry, low quality, distorted, watermark, text, static, frozen frame, cartoon, oversaturated", "negative_mode": "replace", "advanced": {"shift": 5.0, "cfg": 1.0, "steps": 4, "boundary": 2, "sampler_name": "euler", "scheduler": "simple"}}',
 0, 0, 'system'),
('wp_7f3a9c4d12e84b02', 'High Detail', 'Square frame, six sampling steps and detail-oriented wording (slower).', 1,
 '{"version": 1, "loras": [], "size": "704x704", "seconds": 3.0, "fps": 16, "seed_mode": "random", "seed": null, "prompt_suffix": "highly detailed, sharp focus, intricate textures", "negative_prompt": "blurry, low quality, distorted, watermark, text, static, frozen frame, lowres, jpeg artifacts", "negative_mode": "replace", "advanced": {"shift": 5.0, "cfg": 1.0, "steps": 6, "boundary": 3, "sampler_name": "euler", "scheduler": "simple"}}',
 0, 0, 'system'),
('wp_2b8d6e1f47a54c03', 'Character Consistency', 'Portrait frame and a fixed seed so repeated runs keep the same look.', 1,
 '{"version": 1, "loras": [], "size": "480x832", "seconds": 3.0, "fps": 16, "seed_mode": "fixed", "seed": 424242, "prompt_suffix": "consistent character appearance, same face, same outfit", "negative_prompt": "blurry, low quality, distorted, watermark, text, static, frozen frame, extra limbs, deformed face", "negative_mode": "replace", "advanced": {"shift": 5.0, "cfg": 1.0, "steps": 4, "boundary": 2, "sampler_name": "euler", "scheduler": "simple"}}',
 0, 0, 'system'),
('wp_9e4c0b7a35f14d04', 'Motion Style', '24 fps and motion-oriented wording.', 1,
 '{"version": 1, "loras": [], "size": "832x480", "seconds": 3.0, "fps": 24, "seed_mode": "random", "seed": null, "prompt_suffix": "dynamic camera movement, fluid natural motion", "negative_prompt": "blurry, low quality, distorted, watermark, text, static, frozen frame, jittery motion", "negative_mode": "replace", "advanced": {"shift": 5.0, "cfg": 1.0, "steps": 4, "boundary": 2, "sampler_name": "euler", "scheduler": "simple"}}',
 0, 0, 'system'),
('wp_1a6f8e2c59b74e05', 'Custom 1', 'The standard Wan 2.2 settings, ready to adjust and save.', 1,
 '{"version": 1, "loras": [], "size": "640x640", "seconds": 3.0, "fps": 16, "seed_mode": "random", "seed": null, "prompt_suffix": "", "negative_prompt": "blurry, low quality, distorted, watermark, text, static, frozen frame", "negative_mode": "replace", "advanced": {"shift": 5.0, "cfg": 1.0, "steps": 4, "boundary": 2, "sampler_name": "euler", "scheduler": "simple"}}',
 0, 0, 'system')
