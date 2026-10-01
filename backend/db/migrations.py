"""Numbered schema migrations for the main database.

MIGRATIONS[n - 1] moves a database from user_version n - 1 to n. Each script runs
inside one transaction that the runner opens, so a script holds no BEGIN, COMMIT
or ROLLBACK of its own. Add a migration by appending a script; never edit or
reorder a released one.

Conventions in every table:
- STRICT tables with foreign keys.
- Ids are lowercase UUID4 text: lowercase hex digits in the 8-4-4-4-12
  groups, version 4 and variant 8, 9, a or b.
- Times are UTC ISO 8601 text with milliseconds, as SQLite's
  strftime('%Y-%m-%dT%H:%M:%fZ') writes them (e.g. 2026-10-02T03:18:00.123Z),
  so they compare correctly as text.
- JSON columns are checked with json_valid.
- Booleans are INTEGER 0 or 1.
"""

_0001 = r"""
PRAGMA application_id = 1094795858;

-- Projects

CREATE TABLE projects (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('general', 'research')),
    sensitivity TEXT NOT NULL DEFAULT 'normal' CHECK (sensitivity IN ('normal', 'private', 'local_only')),
    review_lock INTEGER NOT NULL DEFAULT 0 CHECK (review_lock IN (0, 1)),
    review_venue TEXT,
    target_venue TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at IS strftime('%Y-%m-%dT%H:%M:%fZ', created_at)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (updated_at IS strftime('%Y-%m-%dT%H:%M:%fZ', updated_at))
) STRICT;

CREATE UNIQUE INDEX projects_one_general ON projects (kind) WHERE kind = 'general';

CREATE TRIGGER projects_keep_general BEFORE DELETE ON projects
WHEN OLD.kind = 'general'
BEGIN
    SELECT RAISE(ABORT, 'the General project cannot be deleted');
END;

CREATE TRIGGER projects_keep_general_kind BEFORE UPDATE OF id, kind ON projects
WHEN OLD.kind = 'general' OR NEW.kind = 'general'
BEGIN
    SELECT RAISE(ABORT, 'the General project keeps its id and kind');
END;

INSERT INTO projects (id, name, kind) VALUES (
    lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4'
        || substr(lower(hex(randomblob(2))), 2) || '-' || substr('89ab', 1 + (random() & 3), 1)
        || substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6))),
    'General',
    'general'
);

CREATE TABLE private_routes (
    route_key TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('shipped', 'researcher')),
    required_flags TEXT NOT NULL CHECK (json_valid(required_flags)),
    allowed_features TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(allowed_features)),
    terms_url TEXT,
    checked_on TEXT CHECK (checked_on IS strftime('%Y-%m-%dT%H:%M:%fZ', checked_on)),
    exceptions TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(exceptions)),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
) STRICT;

CREATE TABLE local_declarations (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    provider TEXT NOT NULL,
    base_url TEXT NOT NULL,
    declared_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (declared_at IS strftime('%Y-%m-%dT%H:%M:%fZ', declared_at)),
    statement TEXT NOT NULL
) STRICT;

CREATE TABLE key_attestations (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    provider TEXT NOT NULL,
    key_fingerprint TEXT NOT NULL,
    statement TEXT NOT NULL,
    confirmed_at TEXT NOT NULL CHECK (confirmed_at IS strftime('%Y-%m-%dT%H:%M:%fZ', confirmed_at)),
    expires_at TEXT NOT NULL CHECK (expires_at IS strftime('%Y-%m-%dT%H:%M:%fZ', expires_at))
) STRICT;

-- Runs

CREATE TABLE conversations (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    project_id TEXT NOT NULL REFERENCES projects (id),
    title TEXT,
    title_source TEXT,
    title_rev INTEGER NOT NULL DEFAULT 0 CHECK (title_rev >= 0),
    is_parent INTEGER NOT NULL DEFAULT 0 CHECK (is_parent IN (0, 1)),
    budget_usd REAL CHECK (budget_usd >= 0),
    session_policy TEXT CHECK (json_valid(session_policy)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at IS strftime('%Y-%m-%dT%H:%M:%fZ', created_at)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (updated_at IS strftime('%Y-%m-%dT%H:%M:%fZ', updated_at))
) STRICT;

CREATE UNIQUE INDEX conversations_one_parent ON conversations (project_id) WHERE is_parent = 1;

CREATE TABLE runs (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    project_id TEXT NOT NULL REFERENCES projects (id),
    conversation_id TEXT REFERENCES conversations (id),
    parent_run_id TEXT REFERENCES runs (id),
    kind TEXT NOT NULL CHECK (kind IN ('turn', 'child', 'background')),
    workflow TEXT,
    skills TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(skills)),
    dispatched_by_run_id TEXT REFERENCES runs (id),
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled', 'interrupted')),
    cancel_reason TEXT CHECK (cancel_reason IN ('researcher', 'limit', 'revoked')),
    waiting TEXT CHECK (waiting IN ('approval', 'answer', 'ask')),
    limits TEXT CHECK (json_valid(limits)),
    inputs TEXT CHECK (json_valid(inputs)),
    source_turn_id TEXT REFERENCES turns (run_id),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 2),
    eval_variant TEXT,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (started_at IS strftime('%Y-%m-%dT%H:%M:%fZ', started_at)),
    finished_at TEXT CHECK (finished_at IS strftime('%Y-%m-%dT%H:%M:%fZ', finished_at)),
    summary TEXT CHECK (json_valid(summary)),
    settled_cost_usd REAL CHECK (settled_cost_usd >= 0)
) STRICT;

-- The run record. Rows are never updated, and are deleted only together with
-- their run: the foreign key is checked at commit, so a transaction may delete
-- the run first and then its events, and no other order is accepted.
CREATE TABLE run_events (
    run_id TEXT NOT NULL REFERENCES runs (id) DEFERRABLE INITIALLY DEFERRED,
    seq INTEGER NOT NULL CHECK (seq >= 0),
    at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (at IS strftime('%Y-%m-%dT%H:%M:%fZ', at)),
    type TEXT NOT NULL CHECK (type IN (
        'route', 'step_started', 'model_attempt', 'step_finished', 'tool_call_started',
        'approval_requested', 'approval_decided', 'tool_result', 'steering_note', 'limit_hit',
        'compaction', 'retrieval_trace', 'citation_checks', 'suggestion_set', 'memory_proposed',
        'run_finished', 'skill_activated', 'ask', 'ask_answered', 'subagent_started', 'dispatch',
        'budget_notice', 'budget_decision'
    )),
    data TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(data)),
    body_ref TEXT REFERENCES content_files (sha256),
    PRIMARY KEY (run_id, seq)
) STRICT;

CREATE TRIGGER run_events_no_update BEFORE UPDATE ON run_events
BEGIN
    SELECT RAISE(ABORT, 'run_events is append-only');
END;

CREATE TRIGGER run_events_no_delete BEFORE DELETE ON run_events
WHEN EXISTS (SELECT 1 FROM runs WHERE id = OLD.run_id)
BEGIN
    SELECT RAISE(ABORT, 'run_events is append-only; events are deleted only with their run');
END;

CREATE TABLE turns (
    run_id TEXT PRIMARY KEY REFERENCES runs (id),
    conversation_id TEXT NOT NULL REFERENCES conversations (id),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    author TEXT NOT NULL,
    user_message TEXT NOT NULL CHECK (json_valid(user_message)),
    answer TEXT CHECK (json_valid(answer)),
    retry_of_run_id TEXT REFERENCES runs (id),
    phase TEXT,
    memory_status TEXT CHECK (memory_status IN ('pending', 'indexed', 'failed', 'skipped', 'interrupted', 'unknown')),
    result_saved INTEGER NOT NULL DEFAULT 0 CHECK (result_saved IN (0, 1)),
    reason_code TEXT,
    warnings TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(warnings)),
    prompt_template_revision TEXT,
    session_counted INTEGER NOT NULL DEFAULT 0 CHECK (session_counted IN (0, 1)),
    accounting TEXT CHECK (json_valid(accounting)),
    UNIQUE (conversation_id, seq)
) STRICT;

-- Materials

CREATE TABLE content_files (
    sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64 AND NOT sha256 GLOB '*[^0-9a-f]*'),
    size INTEGER NOT NULL CHECK (size >= 0),
    media_type TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at IS strftime('%Y-%m-%dT%H:%M:%fZ', created_at))
) STRICT;

CREATE TABLE materials (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    project_id TEXT NOT NULL REFERENCES projects (id),
    title TEXT,
    csl TEXT CHECK (json_valid(csl)),
    source TEXT NOT NULL CHECK (source IN ('upload', 'reference_file', 'zotero', 'discovery')),
    source_key TEXT,
    evidence_type TEXT,
    proposed_by TEXT,
    resolved_at TEXT CHECK (resolved_at IS strftime('%Y-%m-%dT%H:%M:%fZ', resolved_at)),
    checked_at TEXT CHECK (checked_at IS strftime('%Y-%m-%dT%H:%M:%fZ', checked_at)),
    checked_by TEXT CHECK (checked_by IN ('lookup', 'researcher')),
    retraction TEXT NOT NULL DEFAULT 'unknown' CHECK (retraction IN ('none', 'retracted', 'unknown')),
    retraction_checked_at TEXT CHECK (retraction_checked_at IS strftime('%Y-%m-%dT%H:%M:%fZ', retraction_checked_at)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at IS strftime('%Y-%m-%dT%H:%M:%fZ', created_at)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (updated_at IS strftime('%Y-%m-%dT%H:%M:%fZ', updated_at))
) STRICT;

CREATE TABLE material_versions (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    material_id TEXT NOT NULL REFERENCES materials (id),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    file_sha256 TEXT REFERENCES content_files (sha256),
    is_current INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at IS strftime('%Y-%m-%dT%H:%M:%fZ', created_at)),
    UNIQUE (material_id, seq)
) STRICT;

CREATE UNIQUE INDEX material_versions_one_current ON material_versions (material_id) WHERE is_current = 1;

CREATE TABLE extractions (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    file_sha256 TEXT NOT NULL REFERENCES content_files (sha256),
    extractor TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    status TEXT NOT NULL,
    pages INTEGER CHECK (pages >= 0),
    ocr_pages INTEGER CHECK (ocr_pages >= 0),
    error TEXT
) STRICT;

CREATE TABLE passages (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    extraction_id TEXT NOT NULL REFERENCES extractions (id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    page INTEGER,
    section_path TEXT CHECK (json_valid(section_path)),
    kind TEXT NOT NULL CHECK (kind IN ('paragraph', 'table', 'caption', 'reference', 'abstract', 'title')),
    text TEXT NOT NULL,
    char_start INTEGER,
    char_end INTEGER,
    boxes TEXT CHECK (json_valid(boxes)),
    UNIQUE (extraction_id, ordinal)
) STRICT;

CREATE TABLE candidates (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    run_id TEXT NOT NULL REFERENCES runs (id),
    project_id TEXT NOT NULL REFERENCES projects (id),
    source TEXT NOT NULL,
    source_ids TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(source_ids)),
    csl TEXT CHECK (json_valid(csl)),
    abstract TEXT,
    oa_url TEXT,
    screening TEXT CHECK (json_valid(screening)),
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'added', 'dismissed')),
    material_id TEXT REFERENCES materials (id)
) STRICT;

CREATE TABLE search_plans (
    run_id TEXT NOT NULL REFERENCES runs (id),
    version INTEGER NOT NULL CHECK (version >= 0),
    plan TEXT NOT NULL CHECK (json_valid(plan)),
    edited INTEGER NOT NULL DEFAULT 0 CHECK (edited IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at IS strftime('%Y-%m-%dT%H:%M:%fZ', created_at)),
    PRIMARY KEY (run_id, version)
) STRICT;

CREATE TABLE budget_reservations (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    run_id TEXT NOT NULL REFERENCES runs (id),
    step_seq INTEGER NOT NULL CHECK (step_seq >= 0),
    paying_conversation_id TEXT REFERENCES conversations (id),
    project_id TEXT NOT NULL REFERENCES projects (id),
    estimate_usd REAL NOT NULL CHECK (estimate_usd >= 0),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'settled', 'released')),
    settled_usd REAL CHECK (settled_usd >= 0),
    basis TEXT CHECK (basis IN ('reported', 'estimated')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at IS strftime('%Y-%m-%dT%H:%M:%fZ', created_at)),
    settled_at TEXT CHECK (settled_at IS strftime('%Y-%m-%dT%H:%M:%fZ', settled_at))
) STRICT;

-- seq never repeats, even after applied rows are removed, because the index
-- records the last sequence it applied. project_id outlives a deleted project
-- (its removals are still queued), so it is not a foreign key.
CREATE TABLE index_queue (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL CHECK (target IN ('passage', 'memory')),
    target_id TEXT NOT NULL,
    project_id TEXT,
    op TEXT NOT NULL CHECK (op IN ('add', 'remove'))
) STRICT;

-- Citations

CREATE TABLE citations (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('answer', 'artifact')),
    owner_id TEXT NOT NULL,
    material_id TEXT REFERENCES materials (id),
    material_version_id TEXT REFERENCES material_versions (id),
    passage_id TEXT REFERENCES passages (id),
    quote TEXT,
    context TEXT CHECK (json_valid(context)),
    page INTEGER,
    char_hint INTEGER,
    evidence_type TEXT,
    existence TEXT NOT NULL CHECK (existence IN ('ok', 'not_found', 'not_read_in_run', 'source_removed')),
    support TEXT NOT NULL DEFAULT 'unchecked' CHECK (support IN ('supported', 'partly', 'unsupported', 'unchecked')),
    support_run_id TEXT REFERENCES runs (id),
    checked_at TEXT CHECK (checked_at IS strftime('%Y-%m-%dT%H:%M:%fZ', checked_at))
) STRICT;

-- Artifacts

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    project_id TEXT NOT NULL REFERENCES projects (id),
    kind TEXT NOT NULL DEFAULT 'manuscript' CHECK (kind IN ('manuscript')),
    title TEXT NOT NULL,
    language TEXT,
    citation_style TEXT,
    template_id TEXT,
    authors TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(authors)),
    venue TEXT,
    source_conversation_id TEXT REFERENCES conversations (id),
    doc TEXT NOT NULL CHECK (json_valid(doc)),
    doc_rev INTEGER NOT NULL DEFAULT 0 CHECK (doc_rev >= 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at IS strftime('%Y-%m-%dT%H:%M:%fZ', created_at)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (updated_at IS strftime('%Y-%m-%dT%H:%M:%fZ', updated_at))
) STRICT;

-- A version's document and artifact never change, and a version is deleted only
-- together with its artifact: the foreign key is checked at commit, so a
-- transaction may delete the artifact first and then its versions, and no other
-- order is accepted. The delete trigger also stops REPLACE from swapping a version.
CREATE TABLE artifact_versions (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    artifact_id TEXT NOT NULL REFERENCES artifacts (id) DEFERRABLE INITIALLY DEFERRED,
    seq INTEGER NOT NULL CHECK (seq >= 0),
    doc TEXT NOT NULL CHECK (json_valid(doc)),
    reason TEXT NOT NULL CHECK (reason IN ('named', 'before_run', 'after_run', 'export')),
    label TEXT,
    run_id TEXT REFERENCES runs (id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at IS strftime('%Y-%m-%dT%H:%M:%fZ', created_at)),
    UNIQUE (artifact_id, seq)
) STRICT;

CREATE TRIGGER artifact_versions_keep_doc BEFORE UPDATE OF artifact_id, doc ON artifact_versions
BEGIN
    SELECT RAISE(ABORT, 'an artifact version''s document and artifact are immutable');
END;

CREATE TRIGGER artifact_versions_no_delete BEFORE DELETE ON artifact_versions
WHEN EXISTS (SELECT 1 FROM artifacts WHERE id = OLD.artifact_id)
BEGIN
    SELECT RAISE(ABORT, 'artifact versions are immutable; they are deleted only with their artifact');
END;

CREATE TABLE suggestion_sets (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    artifact_id TEXT NOT NULL REFERENCES artifacts (id),
    run_id TEXT REFERENCES runs (id),
    section_id TEXT NOT NULL,
    base_rev INTEGER NOT NULL CHECK (base_rev >= 0),
    section_hash TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    status TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('ready', 'applied', 'discarded', 'stale'))
) STRICT;

CREATE TABLE section_leases (
    artifact_id TEXT NOT NULL REFERENCES artifacts (id),
    section_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs (id),
    acquired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (acquired_at IS strftime('%Y-%m-%dT%H:%M:%fZ', acquired_at)),
    PRIMARY KEY (artifact_id, section_id)
) STRICT;

CREATE TABLE comment_threads (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    artifact_id TEXT NOT NULL REFERENCES artifacts (id),
    anchor TEXT NOT NULL CHECK (json_valid(anchor)),
    run_id TEXT REFERENCES runs (id),
    severity TEXT,
    verdict TEXT CHECK (verdict IN ('valid', 'invalid')),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved'))
) STRICT;

CREATE TABLE comments (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    thread_id TEXT NOT NULL REFERENCES comment_threads (id),
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at IS strftime('%Y-%m-%dT%H:%M:%fZ', created_at))
) STRICT;

-- Memory

CREATE TABLE memory_records (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    scope TEXT NOT NULL CHECK (scope IN ('personal', 'project')),
    project_id TEXT REFERENCES projects (id),
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    structured TEXT CHECK (json_valid(structured)),
    status TEXT NOT NULL,
    valid_from TEXT CHECK (valid_from IS strftime('%Y-%m-%dT%H:%M:%fZ', valid_from)),
    valid_to TEXT CHECK (valid_to IS strftime('%Y-%m-%dT%H:%M:%fZ', valid_to)),
    sensitivity TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    supersedes_id TEXT REFERENCES memory_records (id),
    created_by_run_id TEXT REFERENCES runs (id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at IS strftime('%Y-%m-%dT%H:%M:%fZ', created_at)),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (updated_at IS strftime('%Y-%m-%dT%H:%M:%fZ', updated_at)),
    CHECK (NOT (type IN ('progress', 'agent_note') AND status = 'confirmed'))
) STRICT;

CREATE TABLE memory_sources (
    memory_id TEXT NOT NULL REFERENCES memory_records (id),
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    PRIMARY KEY (memory_id, source_kind, source_id)
) STRICT;

-- Governance. Rows here outlive the projects they mention, so project_id is
-- not a foreign key.

CREATE TABLE audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (at IS strftime('%Y-%m-%dT%H:%M:%fZ', at)),
    event TEXT NOT NULL,
    project_id TEXT,
    data TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(data))
) STRICT;

CREATE TABLE tombstones (
    object_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT,
    deleted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (deleted_at IS strftime('%Y-%m-%dT%H:%M:%fZ', deleted_at))
) STRICT;

CREATE TABLE list_checks (
    list TEXT NOT NULL CHECK (list IN ('private_routes', 'venues')),
    entry_id TEXT NOT NULL,
    checked_on TEXT NOT NULL CHECK (checked_on IS strftime('%Y-%m-%dT%H:%M:%fZ', checked_on)),
    PRIMARY KEY (list, entry_id)
) STRICT;

-- Extensions

CREATE TABLE extensions (
    id TEXT NOT NULL,
    version TEXT NOT NULL,
    publisher TEXT,
    license TEXT,
    manifest TEXT NOT NULL CHECK (json_valid(manifest)),
    file_hashes TEXT NOT NULL CHECK (json_valid(file_hashes)),
    approved_at TEXT CHECK (approved_at IS strftime('%Y-%m-%dT%H:%M:%fZ', approved_at)),
    state TEXT NOT NULL CHECK (state IN ('active', 'disabled_changed', 'disabled_range', 'superseded')),
    PRIMARY KEY (id, version)
) STRICT;

CREATE TABLE mcp_servers (
    id TEXT PRIMARY KEY CHECK (id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    name TEXT NOT NULL,
    command TEXT NOT NULL CHECK (json_valid(command)),
    env_allow TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(env_allow)),
    declared_offline INTEGER NOT NULL DEFAULT 0 CHECK (declared_offline IN (0, 1)),
    approved_at TEXT CHECK (approved_at IS strftime('%Y-%m-%dT%H:%M:%fZ', approved_at))
) STRICT;

CREATE TABLE mcp_tool_pins (
    server_id TEXT NOT NULL REFERENCES mcp_servers (id),
    tool_name TEXT NOT NULL,
    definition_sha256 TEXT NOT NULL CHECK (length(definition_sha256) = 64 AND NOT definition_sha256 GLOB '*[^0-9a-f]*'),
    approved_at TEXT NOT NULL CHECK (approved_at IS strftime('%Y-%m-%dT%H:%M:%fZ', approved_at)),
    read_only_by_researcher INTEGER NOT NULL DEFAULT 0 CHECK (read_only_by_researcher IN (0, 1)),
    PRIMARY KEY (server_id, tool_name)
) STRICT;
"""

# A deleted project, conversation, material, artifact or memory record leaves a
# tombstone, and its id is never used again: neither a new row (INSERT, REPLACE
# or upsert) nor a changed id may take it.
_0002 = r"""
CREATE TRIGGER projects_not_after_deletion BEFORE INSERT ON projects
WHEN EXISTS (SELECT 1 FROM tombstones WHERE object_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'a deleted record''s id cannot be used again');
END;

CREATE TRIGGER projects_id_not_after_deletion BEFORE UPDATE OF id ON projects
WHEN EXISTS (SELECT 1 FROM tombstones WHERE object_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'a deleted record''s id cannot be used again');
END;

CREATE TRIGGER conversations_not_after_deletion BEFORE INSERT ON conversations
WHEN EXISTS (SELECT 1 FROM tombstones WHERE object_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'a deleted record''s id cannot be used again');
END;

CREATE TRIGGER conversations_id_not_after_deletion BEFORE UPDATE OF id ON conversations
WHEN EXISTS (SELECT 1 FROM tombstones WHERE object_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'a deleted record''s id cannot be used again');
END;

CREATE TRIGGER materials_not_after_deletion BEFORE INSERT ON materials
WHEN EXISTS (SELECT 1 FROM tombstones WHERE object_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'a deleted record''s id cannot be used again');
END;

CREATE TRIGGER materials_id_not_after_deletion BEFORE UPDATE OF id ON materials
WHEN EXISTS (SELECT 1 FROM tombstones WHERE object_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'a deleted record''s id cannot be used again');
END;

CREATE TRIGGER artifacts_not_after_deletion BEFORE INSERT ON artifacts
WHEN EXISTS (SELECT 1 FROM tombstones WHERE object_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'a deleted record''s id cannot be used again');
END;

CREATE TRIGGER artifacts_id_not_after_deletion BEFORE UPDATE OF id ON artifacts
WHEN EXISTS (SELECT 1 FROM tombstones WHERE object_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'a deleted record''s id cannot be used again');
END;

CREATE TRIGGER memory_records_not_after_deletion BEFORE INSERT ON memory_records
WHEN EXISTS (SELECT 1 FROM tombstones WHERE object_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'a deleted record''s id cannot be used again');
END;

CREATE TRIGGER memory_records_id_not_after_deletion BEFORE UPDATE OF id ON memory_records
WHEN EXISTS (SELECT 1 FROM tombstones WHERE object_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'a deleted record''s id cannot be used again');
END;
"""

MIGRATIONS: tuple[str, ...] = (_0001, _0002)
