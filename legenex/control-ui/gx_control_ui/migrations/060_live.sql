-- Build V3 LIV: gx-live (MiniCPM-o 4.5) realtime sessions on the Control Center.
-- The audio, camera frames and model context never reach this database: node 2
-- holds them in memory for the current turn only (PROTOCOL.md section 6).
-- What is kept here is the session record (who, when, how long, how it ended),
-- per-turn timings, the tool calls the Control Center executed, an optional
-- transcript the owner chose to save, and the Library assets a session touched.
-- One row per live session. Metadata and timings only.
CREATE TABLE IF NOT EXISTS live_sessions (
    session_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    user_label TEXT,
    via TEXT NOT NULL,
    state TEXT NOT NULL,
    end_reason TEXT,
    created_at REAL NOT NULL,
    ready_at REAL,
    ended_at REAL,
    duration_s REAL,
    language TEXT NOT NULL DEFAULT 'en',
    tools_enabled INTEGER NOT NULL DEFAULT 1,
    output_audio INTEGER NOT NULL DEFAULT 1,
    camera_used INTEGER NOT NULL DEFAULT 0,
    has_instructions INTEGER NOT NULL DEFAULT 0,
    model_alias TEXT NOT NULL DEFAULT 'gx-live',
    model_repo TEXT,
    model_revision TEXT,
    load_ms INTEGER,
    wait_ms INTEGER,
    turns INTEGER NOT NULL DEFAULT 0,
    responses INTEGER NOT NULL DEFAULT 0,
    interrupted INTEGER NOT NULL DEFAULT 0,
    text_inputs INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    metrics TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    event_seq INTEGER NOT NULL DEFAULT 0,
    transcript_saved INTEGER NOT NULL DEFAULT 0,
    content_purged INTEGER NOT NULL DEFAULT 0,
    retain_until REAL
);
CREATE INDEX IF NOT EXISTS live_sessions_owner ON live_sessions(owner, created_at);
CREATE INDEX IF NOT EXISTS live_sessions_state ON live_sessions(state, created_at);
CREATE INDEX IF NOT EXISTS live_sessions_retain ON live_sessions(retain_until);
-- Metadata-only lifecycle log: created, waiting for memory, ready, interrupted,
-- errors, ended. Never transcript, prompt, audio or image content.
CREATE TABLE IF NOT EXISTS live_events (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    at REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'server',
    data TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (session_id, seq)
);
CREATE INDEX IF NOT EXISTS live_events_session ON live_events(session_id, seq);
-- One row per assistant response (a conversational turn). The numbers come
-- from the session owner's own client, which is the only party that sees the
-- whole turn, and from the node-2 summary at the end. No text is stored here.
CREATE TABLE IF NOT EXISTS live_turns (
    session_id TEXT NOT NULL,
    response INTEGER NOT NULL,
    turn INTEGER,
    trigger TEXT,
    status TEXT,
    at REAL NOT NULL,
    first_audio_ms INTEGER,
    first_text_ms INTEGER,
    turn_ms INTEGER,
    audio_ms INTEGER,
    interrupt_ms INTEGER,
    interrupt_reason TEXT,
    user_chars INTEGER,
    assistant_chars INTEGER,
    camera INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, response)
);
CREATE INDEX IF NOT EXISTS live_turns_session ON live_turns(session_id, at);
-- Tool calls the Control Center executed for a session. PROTOCOL.md section 5:
-- names, timings and outcome are recorded, the arguments' content is not, so
-- only the argument names and their size are kept.
CREATE TABLE IF NOT EXISTS live_tool_calls (
    session_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    name TEXT NOT NULL,
    arg_keys TEXT NOT NULL DEFAULT '[]',
    arg_bytes INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    routed_to TEXT,
    ok INTEGER,
    latency_ms INTEGER,
    error_code TEXT,
    result_chars INTEGER,
    at REAL NOT NULL,
    finished_at REAL,
    PRIMARY KEY (session_id, call_id)
);
CREATE INDEX IF NOT EXISTS live_tool_calls_session ON live_tool_calls(session_id, at);
CREATE INDEX IF NOT EXISTS live_tool_calls_name ON live_tool_calls(name, at);
-- A transcript exists only when the owner pressed Save transcript. It is their
-- own copy of the conversation, stored in this database (the Media Library
-- holds image, video and audio assets only) and deleted with the session.
CREATE TABLE IF NOT EXISTS live_transcripts (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    speaker TEXT NOT NULL,
    turn INTEGER,
    text TEXT NOT NULL,
    at REAL NOT NULL,
    interrupted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, seq)
);
CREATE INDEX IF NOT EXISTS live_transcripts_session ON live_transcripts(session_id, seq);
-- Provenance: which Library assets a live session touched, and how. A row is
-- written when search_library surfaced an asset to the assistant (relation
-- 'referenced') and when a session produces one (relation 'created'), which
-- lets the Library show where an asset was used and the session show what it
-- used. The asset id is the Library id; a deleted asset simply has no row in
-- assets any more, which is why this is not a foreign key.
CREATE TABLE IF NOT EXISTS live_session_assets (
    session_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'referenced',
    call_id TEXT,
    at REAL NOT NULL,
    PRIMARY KEY (session_id, asset_id, relation)
);
CREATE INDEX IF NOT EXISTS live_session_assets_asset ON live_session_assets(asset_id, at);
