-- Vanilla Dream Radio schema (authoritative, see contract section 3).
-- The script is idempotent: it is executed on every start and PRAGMA user_version
-- records the applied schema version.

CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY,
    sha256 TEXT UNIQUE NOT NULL,
    path TEXT NOT NULL,
    ext TEXT,
    title TEXT NOT NULL,
    artist TEXT,
    album TEXT,
    genre TEXT,
    year INTEGER,
    duration REAL,
    bitrate INTEGER,
    size INTEGER,
    cover_path TEXT,
    source_title TEXT,
    source_artist TEXT,
    source_album TEXT,
    source_genre TEXT,
    play_count INTEGER DEFAULT 0,
    pending_delete INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS playlists (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    cover_path TEXT,
    shuffle INTEGER DEFAULT 0,
    repeat_mode TEXT DEFAULT 'none',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id TEXT,
    track_id TEXT,
    position INTEGER,
    PRIMARY KEY (playlist_id, track_id)
);

CREATE TABLE IF NOT EXISTS queue_positions (
    id TEXT PRIMARY KEY,
    track_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_ref TEXT,
    manual INTEGER DEFAULT 0,
    seq INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT,
    started_at TEXT,
    engine_ref TEXT
);

CREATE TABLE IF NOT EXISTS playback_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    position_id TEXT,
    track_id TEXT,
    started_at TEXT,
    source_kind TEXT,
    source_ref TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id TEXT,
    position_id TEXT,
    started_at TEXT,
    ended_at TEXT,
    source_kind TEXT,
    source_ref TEXT,
    title TEXT,
    artist TEXT,
    album TEXT
);

CREATE TABLE IF NOT EXISTS schedule_rules (
    id TEXT PRIMARY KEY,
    name TEXT,
    playlist_id TEXT,
    rotation INTEGER DEFAULT 1,
    days TEXT NOT NULL,
    start_min INTEGER NOT NULL,
    end_min INTEGER NOT NULL,
    enabled INTEGER DEFAULT 1,
    priority INTEGER DEFAULT 0,
    transition TEXT DEFAULT 'after_track',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS rotations (
    id TEXT PRIMARY KEY,
    name TEXT,
    mode TEXT DEFAULT 'sequential',
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS rotation_items (
    id TEXT PRIMARY KEY,
    rotation_id TEXT,
    playlist_id TEXT,
    position INTEGER,
    enabled INTEGER DEFAULT 1,
    track_count INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS rotation_state (
    id TEXT PRIMARY KEY,
    item_id TEXT,
    played INTEGER DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS overrides (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    playlist_id TEXT,
    scope TEXT NOT NULL,
    expires_at TEXT,
    active INTEGER DEFAULT 1,
    created_at TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    token_hash TEXT UNIQUE NOT NULL,
    created_at TEXT,
    expires_at TEXT,
    last_seen TEXT
);

CREATE TABLE IF NOT EXISTS admin (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT,
    level TEXT,
    kind TEXT,
    message TEXT,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_tracks_sha256 ON tracks(sha256);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_order ON playlist_tracks(playlist_id, position);
CREATE INDEX IF NOT EXISTS idx_queue_positions_state_seq ON queue_positions(state, seq);
CREATE INDEX IF NOT EXISTS idx_history_started_at ON history(started_at);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);
