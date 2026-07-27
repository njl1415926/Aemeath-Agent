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

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT,
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
    roles_json TEXT NOT NULL DEFAULT '["user"]',
    dev_token_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_accounts_dev_token_hash
    ON accounts (dev_token_hash);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'ended', 'archived')),
    title TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    ended_at TEXT,
    archived_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_status_updated
    ON sessions (user_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_sessions_user_character_status_updated
    ON sessions (user_id, character_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS long_term_memories (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'user_global',
    scope_character_id TEXT,
    scope_session_id TEXT,
    namespace TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    importance INTEGER NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL,
    source_message_id TEXT,
    source_session_id TEXT,
    source_job_id TEXT,
    source_message_ids_json TEXT NOT NULL DEFAULT '[]',
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

CREATE TABLE IF NOT EXISTS memory_extraction_jobs (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    character_id TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    status TEXT NOT NULL,
    source_message_ids_json TEXT NOT NULL,
    input_json TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    locked_by TEXT,
    locked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_jobs_status_created
    ON memory_extraction_jobs (status, created_at);

CREATE TABLE IF NOT EXISTS long_term_memory_embeddings (
    memory_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    vector_dim INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_embeddings_user
    ON long_term_memory_embeddings (user_id);

CREATE TABLE IF NOT EXISTS long_term_memory_embedding_artifacts (
    memory_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    embedding_signature_json TEXT NOT NULL,
    embedding_signature_fingerprint TEXT NOT NULL,
    embedding_purpose TEXT NOT NULL CHECK (embedding_purpose = 'document'),
    embedding_content_hash TEXT NOT NULL,
    embedding_vector_format TEXT NOT NULL CHECK (embedding_vector_format = 'f32le-v1'),
    vector_dim INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    embedding_vector_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    PRIMARY KEY (memory_id, embedding_signature_fingerprint, embedding_purpose)
);

CREATE INDEX IF NOT EXISTS idx_memory_embedding_artifacts_user_signature
    ON long_term_memory_embedding_artifacts (
        user_id, embedding_signature_fingerprint, embedding_purpose
    );

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
