-- D-040: where an asset came from beyond its parent: the Creative Flow run and
-- node that produced it, and the feature record (voice take, video job,
-- call session) it belongs to. Existing rows keep NULLs.
ALTER TABLE assets ADD COLUMN flow_id TEXT;
ALTER TABLE assets ADD COLUMN flow_run_id TEXT;
ALTER TABLE assets ADD COLUMN flow_node_id TEXT;
ALTER TABLE assets ADD COLUMN source_kind TEXT;
ALTER TABLE assets ADD COLUMN source_ref TEXT;
CREATE INDEX IF NOT EXISTS assets_flow ON assets(flow_id, flow_run_id);
CREATE INDEX IF NOT EXISTS assets_source ON assets(source_kind, source_ref)
