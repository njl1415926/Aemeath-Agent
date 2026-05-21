CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    emotion_label TEXT,
    importance_score INTEGER
);

CREATE INDEX IF NOT EXISTS idx_messages_session_created
    ON messages (session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS long_term_memories (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    importance INTEGER NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL,
    source_message_id TEXT,
    related_memory_ids TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0,
    decay_score REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    expiry TEXT,
    is_active INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_memories_user_status
    ON long_term_memories (user_id, status, is_active, importance DESC);

CREATE TABLE IF NOT EXISTS relationship_states (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    mood TEXT,
    user_emotion TEXT,
    trust INTEGER,
    affection INTEGER,
    familiarity INTEGER,
    current_scene TEXT,
    last_topic TEXT,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_relationship_user_character
    ON relationship_states (user_id, character_id);
