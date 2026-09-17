-- Build V3 CAL: Call Agents, call sessions and their authoritative state.
-- Agents are configuration objects; every save is a new immutable version.
CREATE TABLE IF NOT EXISTS call_agents (
    agent_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    use_case TEXT NOT NULL DEFAULT 'general',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    created_by TEXT,
    updated_by TEXT,
    cloned_from TEXT
);
CREATE INDEX IF NOT EXISTS call_agents_status ON call_agents(status, updated_at);
CREATE TABLE IF NOT EXISTS call_agent_versions (
    agent_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    config TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    note TEXT,
    created_at REAL NOT NULL,
    created_by TEXT,
    PRIMARY KEY (agent_id, version)
);
-- One row per call. Content (transcripts, state, tool payloads) lives in the
-- tables below and is deleted when retain_until passes; this row keeps only
-- metadata and timings.
CREATE TABLE IF NOT EXISTS call_sessions (
    session_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    agent_version INTEGER NOT NULL,
    owner TEXT NOT NULL,
    via TEXT NOT NULL,
    mode TEXT NOT NULL,
    state TEXT NOT NULL,
    disposition TEXT,
    end_reason TEXT,
    created_at REAL NOT NULL,
    live_at REAL,
    ended_at REAL,
    duration_s REAL,
    external_ref TEXT,
    event_cursor INTEGER NOT NULL DEFAULT 0,
    metrics TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    record INTEGER NOT NULL DEFAULT 0,
    recording_asset_id TEXT,
    transfer TEXT NOT NULL DEFAULT '{}',
    retain_until REAL,
    content_purged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS call_sessions_agent ON call_sessions(agent_id, created_at);
CREATE INDEX IF NOT EXISTS call_sessions_owner ON call_sessions(owner, created_at);
CREATE INDEX IF NOT EXISTS call_sessions_retain ON call_sessions(retain_until);
CREATE TABLE IF NOT EXISTS call_state (
    session_id TEXT PRIMARY KEY,
    schema_id TEXT NOT NULL,
    data TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS call_state_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    at REAL NOT NULL,
    source TEXT NOT NULL,
    fields TEXT NOT NULL,
    revision INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS call_state_changes_session ON call_state_changes(session_id, id);
CREATE TABLE IF NOT EXISTS call_transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    speaker TEXT NOT NULL,
    turn INTEGER,
    text TEXT NOT NULL,
    stream_ms INTEGER,
    at REAL NOT NULL,
    interrupted INTEGER NOT NULL DEFAULT 0,
    UNIQUE (session_id, seq)
);
CREATE INDEX IF NOT EXISTS call_transcripts_session ON call_transcripts(session_id, seq);
CREATE TABLE IF NOT EXISTS call_tool_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    name TEXT NOT NULL,
    arguments TEXT NOT NULL,
    result TEXT,
    ok INTEGER,
    latency_ms INTEGER,
    error TEXT,
    at REAL NOT NULL,
    finished_at REAL,
    UNIQUE (session_id, call_id)
);
CREATE INDEX IF NOT EXISTS call_tool_events_session ON call_tool_events(session_id, id);
-- Metadata-only event log (status, timings, interruptions, transfer): no text.
CREATE TABLE IF NOT EXISTS call_events (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    at REAL NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS call_results (
    session_id TEXT PRIMARY KEY,
    disposition TEXT,
    qualification_status TEXT,
    completion REAL,
    missing_required TEXT NOT NULL DEFAULT '[]',
    structured TEXT NOT NULL DEFAULT '{}',
    summary TEXT,
    post_call TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);
