-- D-040 Creative Flows (FLO): flows, their versions, templates, runs, node
-- run state and the output cache. Graph documents are JSON validated by
-- gx_control_ui/flows/schema.py before they are written.
CREATE TABLE IF NOT EXISTS flow_flows (
    id          TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    version     INTEGER NOT NULL,
    graph       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    template_id TEXT,
    deleted     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS flow_flows_owner ON flow_flows(owner, deleted, updated_at);
CREATE TABLE IF NOT EXISTS flow_versions (
    flow_id    TEXT NOT NULL,
    version    INTEGER NOT NULL,
    graph      TEXT NOT NULL,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL,
    author     TEXT NOT NULL,
    PRIMARY KEY (flow_id, version)
);
CREATE TABLE IF NOT EXISTS flow_templates (
    id          TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT 'custom',
    graph       TEXT NOT NULL,
    builtin     INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS flow_templates_owner ON flow_templates(owner, updated_at);
CREATE TABLE IF NOT EXISTS flow_runs (
    id           TEXT PRIMARY KEY,
    flow_id      TEXT NOT NULL,
    flow_version INTEGER NOT NULL,
    owner        TEXT NOT NULL,
    user         TEXT NOT NULL,
    mode         TEXT NOT NULL,
    target_node  TEXT,
    status       TEXT NOT NULL,
    graph        TEXT NOT NULL,
    created_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL,
    error        TEXT,
    summary      TEXT NOT NULL DEFAULT '{}',
    parent_run   TEXT
);
CREATE INDEX IF NOT EXISTS flow_runs_flow ON flow_runs(flow_id, created_at);
CREATE INDEX IF NOT EXISTS flow_runs_status ON flow_runs(status);
CREATE TABLE IF NOT EXISTS flow_node_runs (
    run_id      TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    node_type   TEXT NOT NULL,
    status      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    started_at  REAL,
    finished_at REAL,
    cache_key   TEXT,
    cached      INTEGER NOT NULL DEFAULT 0,
    outputs     TEXT NOT NULL DEFAULT '{}',
    error       TEXT,
    logs        TEXT NOT NULL DEFAULT '[]',
    payload     TEXT NOT NULL DEFAULT '{}',
    model       TEXT,
    jobs        TEXT NOT NULL DEFAULT '[]',
    resource    TEXT,
    progress    REAL,
    PRIMARY KEY (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS flow_cache (
    cache_key  TEXT PRIMARY KEY,
    node_type  TEXT NOT NULL,
    outputs    TEXT NOT NULL,
    meta       TEXT NOT NULL DEFAULT '{}',
    flow_id    TEXT NOT NULL,
    node_id    TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    created_at REAL NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS flow_cache_flow ON flow_cache(flow_id, node_id)
