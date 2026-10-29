-- fatfish core schema (version 1)

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', '4');

CREATE TABLE IF NOT EXISTS agent_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL,
    since REAL NOT NULL,
    reason TEXT
);

INSERT OR IGNORE INTO agent_state(id, state, since, reason)
VALUES (1, 'idle', CAST(strftime('%s', 'now') AS REAL), 'init');

CREATE TABLE IF NOT EXISTS persona (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    core_json TEXT NOT NULL DEFAULT '{}',
    learned_json TEXT NOT NULL DEFAULT '{}',
    relationship_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_items (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL DEFAULT 'episodic',
    scope TEXT NOT NULL DEFAULT 'global',
    session_id TEXT,
    event_id TEXT,
    episode_id TEXT,
    gist TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    event_time_start REAL,
    event_time_end REAL,
    time_granularity TEXT NOT NULL DEFAULT 'unknown',
    time_confidence REAL NOT NULL DEFAULT 0.5,
    observed_at REAL NOT NULL,
    valid_from REAL,
    valid_to REAL,
    importance REAL NOT NULL DEFAULT 0.4,
    confidence REAL NOT NULL DEFAULT 0.7,
    stability REAL NOT NULL DEFAULT 1.0,
    tier INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    trust_level TEXT NOT NULL DEFAULT 'internal',
    origin_scope TEXT NOT NULL DEFAULT 'self',
    source_type TEXT,
    source_id TEXT,
    evidence TEXT,
    subject TEXT NOT NULL DEFAULT '',
    about_json TEXT NOT NULL DEFAULT '[]',
    entities_json TEXT NOT NULL DEFAULT '[]',
    tags_json TEXT NOT NULL DEFAULT '[]',
    embedding TEXT,
    audience TEXT NOT NULL DEFAULT 'owner',
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_at REAL,
    supersedes TEXT,
    superseded_by TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_type ON memory_items(type);
CREATE INDEX IF NOT EXISTS idx_memory_status ON memory_items(status);
CREATE INDEX IF NOT EXISTS idx_memory_tier ON memory_items(tier);
CREATE INDEX IF NOT EXISTS idx_memory_event_start ON memory_items(event_time_start);
CREATE INDEX IF NOT EXISTS idx_memory_event_end ON memory_items(event_time_end);
CREATE INDEX IF NOT EXISTS idx_memory_observed ON memory_items(observed_at);
CREATE INDEX IF NOT EXISTS idx_memory_session ON memory_items(session_id);
-- Both memory search and the meta directory filter on status first; the
-- composite lets SQLite walk rows in the requested order instead of sorting
-- the whole active set for every turn.
CREATE INDEX IF NOT EXISTS idx_memory_status_importance
    ON memory_items(status, importance DESC, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_status_observed
    ON memory_items(status, observed_at DESC);

CREATE TABLE IF NOT EXISTS memory_links (
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    created_at REAL NOT NULL,
    PRIMARY KEY (from_id, to_id, relation)
);

CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    start_at REAL,
    end_at REAL,
    summary TEXT NOT NULL DEFAULT '',
    memory_ids_json TEXT NOT NULL DEFAULT '[]',
    session_id TEXT,
    event_ids_json TEXT NOT NULL DEFAULT '[]',
    summary_voice TEXT NOT NULL DEFAULT '',
    turn_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS rag_chunks (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    embedding TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    hash TEXT,
    created_at REAL NOT NULL,
    UNIQUE(source_type, source_id, chunk_index, hash)
);

CREATE INDEX IF NOT EXISTS idx_rag_source ON rag_chunks(source_type, source_id);
-- RAG search reads the newest chunks first; without this it sorts every row
-- before applying LIMIT.
CREATE INDEX IF NOT EXISTS idx_rag_created ON rag_chunks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rag_source_created
    ON rag_chunks(source_type, created_at DESC);

CREATE TABLE IF NOT EXISTS conversation_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    used_refs_json TEXT NOT NULL DEFAULT '[]',
    ephemeral INTEGER NOT NULL DEFAULT 0,
    event_id TEXT,
    episode_id TEXT,
    archived_at REAL,
    -- Who sent this turn (v13, T-0057).  A group session is shared by every
    -- member, so ``session_id`` alone cannot answer "who said this"; the
    -- platform account id and the display name at send time are stored per
    -- turn.  Assistant turns and sessions without a per-message sender (web
    -- chat, system prompts) leave them NULL/''.  sender_id is a local-only
    -- identifier: it is never rendered in prompts or CLI/Web output.
    sender_id TEXT,
    sender_name TEXT NOT NULL DEFAULT '',
    -- ADR-0014: the roam trip this turn belongs to, if any.  NULL for every
    -- ordinary turn; a trip summary reads only its own ``trip_id`` rows, so
    -- roaming never mixes another room's conversation in.
    trip_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_turn_session ON conversation_turns(session_id, id);
-- History reads only permanent turns, newest first; the runtime context also
-- asks for the last two user turns and today's user-turn count.
CREATE INDEX IF NOT EXISTS idx_turn_session_ephemeral
    ON conversation_turns(session_id, ephemeral, id);
CREATE INDEX IF NOT EXISTS idx_turn_session_role_id
    ON conversation_turns(session_id, role, id);
CREATE INDEX IF NOT EXISTS idx_turn_session_role_time
    ON conversation_turns(session_id, role, created_at);
-- idx_turn_session_sender is created by migrate(), not here: on an old
-- database this script runs before the sender columns are added, and an
-- index over a column that does not exist yet would fail the whole script.

CREATE TABLE IF NOT EXISTS conversation_events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    start_at REAL NOT NULL,
    end_at REAL,
    status TEXT NOT NULL DEFAULT 'open',
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    turn_ids_json TEXT NOT NULL DEFAULT '[]',
    memory_ids_json TEXT NOT NULL DEFAULT '[]',
    topics_json TEXT NOT NULL DEFAULT '[]',
    importance REAL NOT NULL DEFAULT 0.4,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived_at REAL
);

CREATE INDEX IF NOT EXISTS idx_event_session ON conversation_events(session_id, start_at);
CREATE INDEX IF NOT EXISTS idx_event_status ON conversation_events(status, end_at);

CREATE TABLE IF NOT EXISTS wakeups (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    created_at REAL NOT NULL,
    fire_at REAL NOT NULL,
    expires_at REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    intent TEXT NOT NULL DEFAULT 'think',
    reason TEXT NOT NULL DEFAULT '',
    source_turn_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0,
    chain_depth INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_wakeup_due ON wakeups(status, fire_at);
CREATE INDEX IF NOT EXISTS idx_wakeup_session ON wakeups(session_id, status);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    available_at REAL NOT NULL,
    processed_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    lease_until REAL,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_ready ON outbox(status, available_at, id);
CREATE INDEX IF NOT EXISTS idx_outbox_status_processed
    ON outbox(status, processed_at);

CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    input_hash TEXT,
    output_json TEXT NOT NULL DEFAULT '{}',
    action TEXT,
    outcome TEXT,
    latency_ms REAL,
    model TEXT,
    session_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_decision_kind ON decision_log(kind, at);
CREATE INDEX IF NOT EXISTS idx_decision_at ON decision_log(at);

CREATE TABLE IF NOT EXISTS life_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    at REAL NOT NULL,
    mood TEXT NOT NULL DEFAULT 'calm',
    energy REAL NOT NULL DEFAULT 70,
    social REAL NOT NULL DEFAULT 0.5,
    phase TEXT NOT NULL DEFAULT 'idle',
    current_activity TEXT NOT NULL DEFAULT 'idle',
    activity_started_at REAL,
    planned_minutes INTEGER NOT NULL DEFAULT 30,
    note TEXT NOT NULL DEFAULT '',
    interests_json TEXT NOT NULL DEFAULT '{}',
    budget_json TEXT NOT NULL DEFAULT '{}'
);

INSERT OR IGNORE INTO life_state(id, at, mood, energy, social, phase, current_activity, activity_started_at, planned_minutes, note)
VALUES (1, CAST(strftime('%s', 'now') AS REAL), 'calm', 70, 0.5, 'idle', 'idle', CAST(strftime('%s', 'now') AS REAL), 30, 'init');

CREATE TABLE IF NOT EXISTS day_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_date TEXT NOT NULL,
    start_min INTEGER NOT NULL,
    end_min INTEGER NOT NULL,
    activity TEXT NOT NULL,
    intent TEXT NOT NULL DEFAULT '',
    energy_cost REAL NOT NULL DEFAULT 0,
    planned_by TEXT NOT NULL DEFAULT 'bot',
    state TEXT NOT NULL DEFAULT 'planned',
    summary TEXT NOT NULL DEFAULT '',
    stats_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_day_plan_date ON day_plans(plan_date, start_min);

CREATE TABLE IF NOT EXISTS open_loops (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    due_at REAL,
    kind TEXT NOT NULL DEFAULT 'follow_up',
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    source_ref TEXT,
    session_id TEXT,
    subject TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_open_loops_status ON open_loops(status, due_at);

CREATE TABLE IF NOT EXISTS stickers (
    id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    caption TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    emotion_json TEXT NOT NULL DEFAULT '{}',
    identity_json TEXT NOT NULL DEFAULT '{}',
    content_json TEXT NOT NULL DEFAULT '{}',
    intent_json TEXT NOT NULL DEFAULT '[]',
    intensity REAL NOT NULL DEFAULT 2.0,
    style TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'library',
    usage_count INTEGER NOT NULL DEFAULT 0,
    last_used_at REAL,
    embedding_text TEXT,
    embedding_image TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sticker_active ON stickers(active);

CREATE TABLE IF NOT EXISTS sticker_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sticker_id TEXT NOT NULL,
    session_id TEXT,
    need TEXT NOT NULL DEFAULT '',
    sent_at REAL NOT NULL,
    reaction TEXT
);

CREATE TABLE IF NOT EXISTS voice_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    session_id TEXT,
    chars INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'voice'
);

CREATE INDEX IF NOT EXISTS idx_voice_at ON voice_usage(at);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    key TEXT NOT NULL,
    value REAL NOT NULL DEFAULT 0,
    tags_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_metrics_key ON metrics(key, at);
CREATE INDEX IF NOT EXISTS idx_metrics_at ON metrics(at);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    id UNINDEXED,
    gist,
    summary,
    tokenize = 'unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(
    id UNINDEXED,
    text,
    tokenize = 'unicode61'
);

CREATE TRIGGER IF NOT EXISTS memory_items_ai AFTER INSERT ON memory_items BEGIN
    INSERT INTO memory_fts(id, gist, summary) VALUES (new.id, new.gist, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS memory_items_ad AFTER DELETE ON memory_items BEGIN
    DELETE FROM memory_fts WHERE id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS memory_items_au AFTER UPDATE OF gist, summary ON memory_items BEGIN
    DELETE FROM memory_fts WHERE id = old.id;
    INSERT INTO memory_fts(id, gist, summary) VALUES (new.id, new.gist, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS rag_chunks_ai AFTER INSERT ON rag_chunks BEGIN
    INSERT INTO rag_fts(id, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS rag_chunks_ad AFTER DELETE ON rag_chunks BEGIN
    DELETE FROM rag_fts WHERE id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS rag_chunks_au AFTER UPDATE OF text ON rag_chunks BEGIN
    DELETE FROM rag_fts WHERE id = old.id;
    INSERT INTO rag_fts(id, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS turn_traces (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'private',
    trust TEXT NOT NULL DEFAULT 'private',
    at REAL NOT NULL,
    user_hash TEXT,
    user_len INTEGER NOT NULL DEFAULT 0,
    stable_prefix_hash TEXT,
    dynamic_hash TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL NOT NULL DEFAULT 0,
    model TEXT,
    actions_json TEXT NOT NULL DEFAULT '[]',
    result_json TEXT NOT NULL DEFAULT '{}',
    security_risk REAL NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_trace_at ON turn_traces(at);
CREATE INDEX IF NOT EXISTS idx_trace_session ON turn_traces(session_id, at);

CREATE TABLE IF NOT EXISTS tool_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    trace_id TEXT,
    tool TEXT NOT NULL,
    trust TEXT NOT NULL,
    scope TEXT NOT NULL,
    args_hash TEXT,
    decision TEXT NOT NULL,
    reason TEXT,
    result TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_audit_at ON tool_audit(at);

CREATE TABLE IF NOT EXISTS capabilities (
    token TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    tool TEXT NOT NULL,
    caller_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'issued',
    consumed_at REAL
);

CREATE TABLE IF NOT EXISTS behavior_counters (
    bucket TEXT NOT NULL,
    window_key TEXT NOT NULL,
    count REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (bucket, window_key)
);

-- Photo prompts live here only: never in conversation_turns, memory_items or
-- RAG.  This is what keeps a generated image prompt out of the next turn and
-- out of recall.
CREATE TABLE IF NOT EXISTS photo_jobs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL DEFAULT 'running',
    positive TEXT NOT NULL,
    negative TEXT NOT NULL DEFAULT '',
    caption TEXT NOT NULL DEFAULT '',
    prompt_source TEXT NOT NULL DEFAULT '',
    image_path TEXT,
    error TEXT,
    comfyui_started INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_photo_jobs_created ON photo_jobs(created_at);

-- ---------------------------------------------------------------------------
-- Schema v5: atomic details and evidence.
--
-- A summary is an index, not the fact.  Each row here is one clause-sized
-- statement, optionally with the exact quote it came from.  ``quote_turn_id``
-- points back to conversation_turns, so "what exactly was said" can always be
-- reopened without trusting the summary.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_details (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    episode_id TEXT,
    session_id TEXT NOT NULL DEFAULT '',
    seq INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'fact',
    subject TEXT NOT NULL DEFAULT '',
    predicate TEXT NOT NULL DEFAULT '',
    object TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    speaker TEXT,
    quote_turn_id INTEGER,
    quote_text TEXT,
    quote_hash TEXT,
    occurred_at REAL,
    time_granularity TEXT,
    time_confidence REAL,
    valid_from REAL,
    valid_to REAL,
    emotion TEXT,
    emotion_intensity REAL,
    importance REAL NOT NULL DEFAULT 0.4,
    confidence REAL NOT NULL DEFAULT 0.7,
    source TEXT NOT NULL DEFAULT 'nightly',
    detail_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(detail_hash)
);

CREATE INDEX IF NOT EXISTS idx_detail_event ON event_details(event_id, seq);
CREATE INDEX IF NOT EXISTS idx_detail_time ON event_details(occurred_at);
CREATE INDEX IF NOT EXISTS idx_detail_turn ON event_details(quote_turn_id);
CREATE INDEX IF NOT EXISTS idx_detail_session ON event_details(session_id, occurred_at);

CREATE VIRTUAL TABLE IF NOT EXISTS detail_fts USING fts5(
    detail_id UNINDEXED,
    text,
    quote_text,
    tokenize = 'unicode61'
);

CREATE TRIGGER IF NOT EXISTS event_details_ai AFTER INSERT ON event_details BEGIN
    INSERT INTO detail_fts(detail_id, text, quote_text)
    VALUES (new.id, new.text, COALESCE(new.quote_text, ''));
END;

CREATE TRIGGER IF NOT EXISTS event_details_ad AFTER DELETE ON event_details BEGIN
    DELETE FROM detail_fts WHERE detail_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS event_details_au AFTER UPDATE OF text, quote_text ON event_details BEGIN
    DELETE FROM detail_fts WHERE detail_id = old.id;
    INSERT INTO detail_fts(detail_id, text, quote_text)
    VALUES (new.id, new.text, COALESCE(new.quote_text, ''));
END;

-- Idempotency ledger for nightly detail extraction: re-running a day must not
-- duplicate facts, and an unchanged event can be skipped without reading turns.
CREATE TABLE IF NOT EXISTS archive_runs (
    id TEXT PRIMARY KEY,
    event_id TEXT,
    content_hash TEXT NOT NULL,
    detail_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

-- ---------------------------------------------------------------------------
-- Schema v6: entities and namespaced tags (ADR-0003 §1).
--
-- Free-form ``tags_json`` and the all-empty ``entities_json`` cannot answer
-- "who was there" or support cross-event lookups.  Tags live in a closed set
-- of namespaces and start as ``candidate`` until they recur; entities carry
-- their own mention log with turn/char anchors so every attribution can be
-- traced back to the original sentence.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    namespace TEXT NOT NULL CHECK(namespace IN ('domain','relation','emotion','time','topic')),
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES tags(id),
    aliases_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'candidate',
    usage_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE(namespace, name)
);

CREATE INDEX IF NOT EXISTS idx_tags_status ON tags(status, namespace);

CREATE TABLE IF NOT EXISTS tag_links (
    tag_id INTEGER NOT NULL REFERENCES tags(id),
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL DEFAULT 'extract',
    created_at REAL NOT NULL,
    PRIMARY KEY (tag_id, target_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_tag_links_target ON tag_links(target_type, target_id);

CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    attrs_json TEXT NOT NULL DEFAULT '{}',
    first_seen_at REAL,
    last_seen_at REAL,
    merged_into TEXT REFERENCES entities(id),
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind, canonical_name);

CREATE TABLE IF NOT EXISTS entity_mentions (
    id INTEGER PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(id),
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    surface TEXT NOT NULL,
    turn_id INTEGER REFERENCES conversation_turns(id),
    char_start INTEGER,
    char_end INTEGER,
    confidence REAL NOT NULL DEFAULT 0.8,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entity_mentions_entity ON entity_mentions(entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_source
    ON entity_mentions(source_type, source_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_entity_mention
    ON entity_mentions(entity_id, source_type, source_id, surface, turn_id);

CREATE TABLE IF NOT EXISTS entity_edges (
    id INTEGER PRIMARY KEY,
    src_id TEXT NOT NULL REFERENCES entities(id),
    dst_id TEXT NOT NULL REFERENCES entities(id),
    relation TEXT NOT NULL,
    valid_from REAL,
    valid_to REAL,
    confidence REAL NOT NULL DEFAULT 0.7,
    evidence_detail_id TEXT,
    created_at REAL NOT NULL
);

-- ---------------------------------------------------------------------------
-- Schema v7: QQ Zone (QQ空间) read path (was v6 in isolation; v6 is
-- taken by the entity/tag layer, so the read path lands as v7).
--
-- One row per feed entry as seen remotely.  ``raw_json`` keeps the original
-- payload for re-parsing and debugging, but third-party feed payloads can
-- carry identifiers the author never intended to store ( commenter names,
-- location fields ): treat ``raw_json`` as sensitive-at-rest, never render it
-- in the Web UI or CLI output, and never send it to the LLM.  Privacy rides on
-- ``author_scope``: only rows marked ``self`` may be promoted into the prompt
-- by default (collector maps them to origin_scope='qzone'; friend/other rows
-- become origin_scope='qzone_friend' and stay local-only).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS qzone_feed_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL DEFAULT 'qzone',
    external_id TEXT NOT NULL,
    author_uin TEXT NOT NULL DEFAULT '',
    author_name TEXT NOT NULL DEFAULT '',
    author_scope TEXT NOT NULL DEFAULT 'other',
    content TEXT NOT NULL DEFAULT '',
    media_json TEXT NOT NULL DEFAULT '[]',
    url TEXT NOT NULL DEFAULT '',
    published_at REAL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    seen_at REAL NOT NULL,
    ingested INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    UNIQUE(platform, external_id)
);

CREATE INDEX IF NOT EXISTS idx_qzone_feed_seen ON qzone_feed_items(seen_at);
CREATE INDEX IF NOT EXISTS idx_qzone_feed_ingested ON qzone_feed_items(ingested);

-- ---------------------------------------------------------------------------
-- Schema v8: QQ Zone write path (T-0007).
--
-- One row per shuoshuo the persona wanted to publish. Nothing here posts by
-- itself: a draft is handed to the outbox either by the owner's explicit
-- approval (web/CLI) or, since v9, when the persona's own ``desire`` self-
-- rating reaches ``qzone.auto_post_desire_threshold`` -- and the outbox
-- handler is still the only writer that calls the network. ``media_json`` is
-- reserved for a later task (image upload is explicitly out of scope); text-
-- only for now. Sensitive-content hits are stored with the *redacted* text so
-- cookie values never reach this table.
-- Status machine: draft -> approved -> posted | failed; draft -> rejected;
-- draft -> blocked (sensitive content, never publishable).
--
-- Schema v9 (T-0023): ``desire`` keeps the persona's 0-1 self-rating at draft
-- time (NULL = not given / invalid, i.e. below threshold), and ``decided_by``
-- records who let the row through: 'owner' (explicit review) or
-- 'desire-threshold' (automatic). Together with decision_log they make every
-- auto-publish auditable after the fact.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS qzone_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    media_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft',
    created_by TEXT NOT NULL DEFAULT 'llm',
    session_id TEXT,
    trace_id TEXT,
    idempotency_key TEXT UNIQUE,
    post_result_json TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    decided_at REAL,
    posted_at REAL,
    desire REAL,
    decided_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_qzone_posts_status ON qzone_posts(status, decided_at);
CREATE INDEX IF NOT EXISTS idx_qzone_posts_created ON qzone_posts(created_at);


-- ---------------------------------------------------------------------------
-- Schema v10: on-demand singing jobs (T-0003).
--
-- One row per "唱首歌" request.  The audio is written to
-- ``data/songs/<job_id>/full.wav``; this table is the audit trail (emotion,
-- the four lyric lines that were actually sung, elapsed time, and whether the
-- shared VRAM lease had to unload TTS).  Lyrics are stored as a compact JSON
-- array so "what did she sing" survives a restart -- the song file alone does
-- not carry the words in a readable form.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sing_jobs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL DEFAULT 'running',
    emotion TEXT NOT NULL DEFAULT 'soft',
    lyrics_json TEXT NOT NULL DEFAULT '[]',
    audio_path TEXT,
    error TEXT,
    tts_unloaded INTEGER NOT NULL DEFAULT 0,
    elapsed_ms REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sing_jobs_created ON sing_jobs(created_at);

-- ---------------------------------------------------------------------------
-- Schema v11: 大肥鱼间聊天 audit trail (T-0041).
--
-- One row per message exchanged with the *bound* peer (another instance of
-- 大肥鱼), both directions.  This table is an audit log, not a rate limiter:
-- ``decay_messages_per_hour`` is a decay baseline ("how many messages' worth
-- of threshold lift an hour of silence erases"), so the row count is read for
-- statistics only.  Nothing here posts by itself -- the outbox handler is the
-- only writer that touches the network.
--
-- ``desire`` / ``threshold`` are the values in force when the message was
-- produced (NULL for inbound rows, which never pass the desire gate);
-- ``turn_id`` links back to conversation_turns when the turn was recorded.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_uin TEXT NOT NULL,
    direction TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    desire REAL,
    threshold REAL,
    turn_id INTEGER,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_chat_peer_created
    ON ai_chat_messages(peer_uin, created_at);

-- ---------------------------------------------------------------------------
-- Schema v12: 自主动作的证据池 (T-0052 / T-0042).
--
-- 自主动作（搜记忆 / 搜视频 / 读动态 ...）产生的外部内容不能直接进回复：
-- L2/L3 把结果写成一行证据（``source``/``title``/``content`` 都是不可信外部
-- 文本，读的时候必须 wrap_untrusted），L4 在下一轮按 ``consumed_at IS NULL``
-- 取走并标记 ``consumed_at``，保证同一条证据只被引用一次。
--
-- 这张表只存证据，不存动作决策、不存 desire/threshold —— 阈值状态住
-- ``schema_meta[self_action:state:<scope>]``，两者生命周期不同。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS self_action_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    action TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    consumed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_self_action_evidence_pending
    ON self_action_evidence(session_id, consumed_at, created_at);

-- ---------------------------------------------------------------------------
-- Schema v14: 多会话漫游 (ADR-0014).
--
-- ``session_threads`` is one persistent "thread card" per room: the partial
-- memory she carries between rooms.  It holds a summary, open loops and
-- structured key facts -- never the room's raw transcript.  The transcript
-- stays in ``conversation_turns`` under its own session id.
--
-- ``roam_trips`` is the append-only trip ledger: proposed -> entering ->
-- active -> leaving -> returned (or cancelled/expired/interrupted).  A room
-- must be a bound private session; groups are excluded by default.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session_threads (
    session_id TEXT PRIMARY KEY,
    role TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    target_uin TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'idle',        -- open/idle/paused/blocked
    summary TEXT NOT NULL DEFAULT '',
    last_topic TEXT NOT NULL DEFAULT '',
    open_loops_json TEXT NOT NULL DEFAULT '[]',
    commitments_json TEXT NOT NULL DEFAULT '[]',
    key_facts_json TEXT NOT NULL DEFAULT '[]',
    mood TEXT NOT NULL DEFAULT '',
    unread_count INTEGER NOT NULL DEFAULT 0,
    attention_score REAL NOT NULL DEFAULT 0,
    last_seen_at REAL,
    last_summary_at REAL,
    last_turn_id INTEGER,
    cooldown_until REAL,
    last_trip_at REAL,
    trip_count INTEGER NOT NULL DEFAULT 0,
    total_seconds REAL NOT NULL DEFAULT 0,
    thread_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS roam_trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id TEXT NOT NULL UNIQUE,
    thread_session TEXT NOT NULL,
    home_session TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    purpose_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'proposed',    -- proposed/entering/active/leaving/returned/cancelled/expired/interrupted
    score REAL NOT NULL DEFAULT 0,
    proposal_expires_at REAL,
    approved_at REAL,
    authorization_json TEXT NOT NULL DEFAULT '{}',
    started_at REAL,
    ended_at REAL,
    close_reason TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    summary_json TEXT NOT NULL DEFAULT '{}',
    message_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_threads_status
    ON session_threads(status, attention_score);
CREATE INDEX IF NOT EXISTS idx_trips_thread_status
    ON roam_trips(thread_session, status);
CREATE INDEX IF NOT EXISTS idx_trips_home_status
    ON roam_trips(home_session, status);
