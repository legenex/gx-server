-- Build V3 platform (PLT): per-user GX-Playground preferences (Settings page).
-- Values are validated against a fixed allow-list in routes_plt.PREF_KEYS.
CREATE TABLE IF NOT EXISTS plt_preferences (
    username   TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (username, key)
);
