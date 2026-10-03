"""The deletion service: the one way projects, conversations, materials, artifacts
and memory records are deleted.

delete() removes the object and everything that belongs to it in one
transaction: it follows every foreign key by the policy in ON_DELETE, applies
the rules a foreign key cannot express (RULES), revokes active runs whose scope
includes a deleted record, queues search index removals, and writes tombstones
and one audit record. Foreign keys are checked at commit, so a deletion either
leaves no dangling reference or changes nothing. Rows are never reinserted, and
a protected record (run events, artifact versions) is deleted only after its
parent, in the same transaction. A tombstone keeps its id even under "remove all
trace", and migration 0002 refuses any later row that would take that id.

After the commit, the WAL is checkpointed and truncated, so deleted content does
not stay in old WAL frames, and unreferenced content files are collected.
"""

import contextlib
import json
import logging
import weakref

log = logging.getLogger(__name__)

# The outbound gate open over a database, if any, by database: delete() marks its write with the
# gate's revoking_from_thread, for the project of what it deletes, so that no request of that
# project enters the transport after the deletion commits (slice-1 spec section 10).
REVOKING = weakref.WeakKeyDictionary()

# What can be deleted: kind -> (table, column holding the tombstone's title).
KINDS = {
    "project": ("projects", "name"),
    "conversation": ("conversations", "title"),
    "material": ("materials", "title"),
    "artifact": ("artifacts", "title"),
    "memory": ("memory_records", None),
}

DELETE = None
_SOURCE_REMOVED = "material_id = NULL, material_version_id = NULL, passage_id = NULL, existence = 'source_removed'"
# For every foreign key into a table this service deletes from, what happens to a
# referencing row when the row it references is deleted: DELETE, or the SET
# clause that keeps the row and clears its link. A test checks that this covers
# every such foreign key in the schema.
ON_DELETE = {
    # A project's own records. Project memory goes by RULES; personal memory only loses the link.
    ("conversations", "project_id"): DELETE,
    ("runs", "project_id"): DELETE,
    ("materials", "project_id"): DELETE,
    ("candidates", "project_id"): DELETE,
    ("budget_reservations", "project_id"): DELETE,
    ("artifacts", "project_id"): DELETE,
    ("memory_records", "project_id"): "project_id = NULL",
    # A conversation's runs and turns. Artifacts are kept unless deleted themselves.
    ("runs", "conversation_id"): DELETE,
    ("turns", "conversation_id"): DELETE,
    ("budget_reservations", "paying_conversation_id"): "paying_conversation_id = NULL",
    ("artifacts", "source_conversation_id"): "source_conversation_id = NULL",
    # A run's record, its child runs, the post-answer runs keyed to its turn, and its
    # own working rows. A turn it dispatched elsewhere is revoked and kept.
    ("runs", "parent_run_id"): DELETE,
    ("runs", "source_turn_id"): DELETE,
    ("runs", "dispatched_by_run_id"): "dispatched_by_run_id = NULL",
    ("run_events", "run_id"): DELETE,
    ("turns", "run_id"): DELETE,
    ("turns", "retry_of_run_id"): "retry_of_run_id = NULL",
    ("candidates", "run_id"): DELETE,
    ("search_plans", "run_id"): DELETE,
    ("budget_reservations", "run_id"): "run_id = NULL",  # spending outlives the run; the project still counts it
    ("section_leases", "run_id"): DELETE,
    ("citations", "support_run_id"): "support_run_id = NULL",
    ("artifact_versions", "run_id"): "run_id = NULL",
    ("suggestion_sets", "run_id"): "run_id = NULL",
    ("comment_threads", "run_id"): "run_id = NULL",
    ("memory_records", "created_by_run_id"): "created_by_run_id = NULL",
    # A material's versions; extractions shared by file go by RULES. A citation of a
    # deleted source keeps its quote, loses every link to the source (a passage may
    # live on in another project's material) and shows that the source was removed.
    ("material_versions", "material_id"): DELETE,
    ("passages", "extraction_id"): DELETE,
    ("candidates", "material_id"): "material_id = NULL",
    ("citations", "material_id"): _SOURCE_REMOVED,
    ("citations", "material_version_id"): _SOURCE_REMOVED,
    ("citations", "passage_id"): _SOURCE_REMOVED,
    # An artifact's versions, suggestions, leases and comments.
    ("artifact_versions", "artifact_id"): DELETE,
    ("suggestion_sets", "artifact_id"): DELETE,
    ("section_leases", "artifact_id"): DELETE,
    ("comment_threads", "artifact_id"): DELETE,
    ("comments", "thread_id"): DELETE,
    # Memory.
    ("memory_records", "supersedes_id"): "supersedes_id = NULL",
    ("memory_sources", "memory_id"): DELETE,
}
# Rows in these tables are never deleted here: content files are collected by the
# content store once nothing references them, and MCP servers are not deletable yet.
NOT_DELETED = {"content_files", "mcp_servers"}
# Deleted after every other table, once their parents are gone.
PROTECTED = ("run_events", "artifact_versions")


def _doomed(table):
    return f"(SELECT rid FROM temp.doomed WHERE tbl = '{table}')"


# Deletions a foreign key cannot express. Each adds rows to temp.doomed and is
# applied until nothing more is added. {deleted_ids} is the ids of every doomed row.
RULES = (
    # A turn and its run are one object.
    f"""INSERT OR IGNORE INTO temp.doomed (tbl, rid)
    SELECT 'runs', rowid FROM runs WHERE id IN (SELECT run_id FROM turns WHERE rowid IN {_doomed('turns')})""",
    # An extraction is shared by every version over the same file, so it goes with the last of them.
    f"""INSERT OR IGNORE INTO temp.doomed (tbl, rid)
    SELECT 'extractions', e.rowid FROM extractions e
    WHERE e.file_sha256 IN (SELECT file_sha256 FROM material_versions WHERE rowid IN {_doomed('material_versions')})
    AND NOT EXISTS (SELECT 1 FROM material_versions v
                    WHERE v.file_sha256 = e.file_sha256 AND v.rowid NOT IN {_doomed('material_versions')})""",
    # The citations in a deleted answer or artifact.
    f"""INSERT OR IGNORE INTO temp.doomed (tbl, rid)
    SELECT 'citations', rowid FROM citations
    WHERE (owner_kind = 'answer' AND owner_id IN (SELECT run_id FROM turns WHERE rowid IN {_doomed('turns')}))
    OR (owner_kind = 'artifact' AND owner_id IN (SELECT id FROM artifacts WHERE rowid IN {_doomed('artifacts')}))""",
    # A project's own memory.
    f"""INSERT OR IGNORE INTO temp.doomed (tbl, rid)
    SELECT 'memory_records', rowid FROM memory_records
    WHERE scope = 'project' AND project_id IN (SELECT id FROM projects WHERE rowid IN {_doomed('projects')})""",
    # A memory record loses each deleted source, and is deleted with its last one.
    """INSERT OR IGNORE INTO temp.doomed (tbl, rid)
    SELECT 'memory_sources', rowid FROM memory_sources WHERE source_id IN ({deleted_ids})""",
    f"""INSERT OR IGNORE INTO temp.doomed (tbl, rid)
    SELECT 'memory_records', m.rowid FROM memory_records m
    WHERE m.id IN (SELECT memory_id FROM memory_sources WHERE rowid IN {_doomed('memory_sources')})
    AND NOT EXISTS (SELECT 1 FROM memory_sources s
                    WHERE s.memory_id = m.id AND s.rowid NOT IN {_doomed('memory_sources')})""",
)

# The seam for scope links the schema does not record yet. Each entry is a SELECT
# of the ids of runs whose scope includes a record being deleted, reading the
# doomed rows as RULES do (for example a run whose context holds a deleted
# material or memory record). Those runs, and the runs they started, are revoked
# with the rest. The change that records such a link adds its query here.
SCOPE_LINKS: tuple[str, ...] = ()


def delete(db, content, kind, object_id, *, remove_all_trace=False, on_committed=None):
    """Delete one object and everything that belongs to it. Returns the ids of the active runs it revoked.
    on_committed, if given, is called with those ids as soon as the deletion commits, before
    the cleanup that follows, so the caller can stop that work at once.

    kind is a key of KINDS. remove_all_trace drops the titles from the
    tombstones. An active (running) run whose scope included a deleted record is
    revoked: deleted with its conversation or project, or else kept with
    cancel_reason 'revoked', which its next dispatch must honor. The returned
    ids let the caller stop that work in memory now.

    Raises ValueError for an unknown kind or the General project, and
    LookupError when the object does not exist; nothing is changed then.
    """
    if kind not in KINDS:
        raise ValueError(f"cannot delete a {kind!r}")
    barrier = REVOKING.get(db)
    project_id = db.read(lambda conn: _project_of(conn, kind, object_id)) if barrier else None
    with barrier(project_id) if barrier else contextlib.nullcontext():
        revoked = db.write(lambda conn: _delete(conn, kind, object_id, remove_all_trace))
    if on_committed is not None:
        on_committed(revoked)
    # The deletion is committed. These finish removing its traces, and the next
    # deletion or collection retries them if they fail now. Collection goes first,
    # since the rows it removes would otherwise stay in the WAL.
    try:
        content.collect_garbage()
    except Exception as error:
        log.warning("content collection after a deletion failed (%s)", type(error).__name__)
    try:
        if not db.checkpoint():
            log.warning("the WAL could not be truncated after a deletion; a reader still needed it")
    except Exception as error:
        log.warning("WAL truncation after a deletion failed (%s)", type(error).__name__)
    return revoked


def _project_of(conn, kind, object_id):
    """The project whose runs deleting the object may revoke; None (any project) for a personal
    memory record, or an object that does not exist."""
    table, _ = KINDS[kind]
    row = conn.execute(f"SELECT {'id' if kind == 'project' else 'project_id'} FROM {table} WHERE id = ?",
                       (object_id,)).fetchone()
    return row[0] if row else None


def _delete(conn, kind, object_id, remove_all_trace):
    table, _ = KINDS[kind]
    project_column, general = ("id", "kind = 'general'") if kind == "project" else ("project_id", "0")
    root = conn.execute(f"SELECT rowid, {project_column}, {general} FROM {table} WHERE id = ?", (object_id,)).fetchone()
    if root is None:
        raise LookupError(f"no {kind} with id {object_id}")
    rowid, project_id, is_general = root
    if is_general:
        raise ValueError("the General project cannot be deleted")

    conn.execute("PRAGMA defer_foreign_keys = ON")  # checked at COMMIT, so no row may be left dangling
    conn.execute("CREATE TEMP TABLE doomed (seq INTEGER PRIMARY KEY, tbl TEXT NOT NULL, rid INTEGER NOT NULL, UNIQUE (tbl, rid))")
    conn.execute("INSERT INTO temp.doomed (tbl, rid) VALUES (?, ?)", (table, rowid))
    edges = _edges(conn)
    _close_over(conn, edges)

    revoked = _revoke(conn)
    _queue_index_removals(conn)
    for tombstone_kind, (kind_table, title) in KINDS.items():
        conn.execute(
            "INSERT INTO tombstones (object_id, kind, title)"
            f" SELECT id, ?, {title if title and not remove_all_trace else 'NULL'} FROM {kind_table}"
            f" WHERE rowid IN {_doomed(kind_table)}",
            (tombstone_kind,),
        )
    for (child, column), (parent, key) in edges.items():
        action = ON_DELETE[child, column]
        if action is not DELETE:
            conn.execute(
                f"UPDATE {child} SET {action} WHERE {column} IN (SELECT {key} FROM {parent} WHERE rowid IN {_doomed(parent)})"
                f" AND rowid NOT IN {_doomed(child)}"
            )
    counts = dict(conn.execute("SELECT tbl, count(*) FROM temp.doomed GROUP BY tbl ORDER BY tbl").fetchall())
    conn.execute(
        "INSERT INTO audit_log (event, project_id, data) VALUES ('deletion', ?, ?)",
        (
            project_id,
            json.dumps({
                "kind": kind,
                "object_id": object_id,
                "remove_all_trace": remove_all_trace,
                "deleted": counts,
                "revoked_runs": len(revoked),
            }),
        ),
    )
    for doomed_table in sorted(counts, key=lambda name: name in PROTECTED):  # parents first
        conn.execute(f"DELETE FROM {doomed_table} WHERE rowid IN {_doomed(doomed_table)}")
    conn.execute("DROP TABLE temp.doomed")
    return revoked


def _edges(conn):
    """{(child table, column): (parent table, referenced column)} for every foreign key
    into a table rows may be deleted from. Raises if one has no policy in ON_DELETE."""
    edges = {}
    for child, column, parent, key in conn.execute(
        'SELECT m.name, f."from", f."table", f."to" FROM sqlite_schema m, pragma_foreign_key_list(m.name) f'
        " WHERE m.type = 'table'"
    ):
        if parent in NOT_DELETED:
            continue
        if (child, column) not in ON_DELETE or key is None:
            raise RuntimeError(f"no deletion policy for {child}.{column}")
        edges[child, column] = (parent, key)
    return edges


def _close_over(conn, edges):
    """Add to temp.doomed every row that must go with the rows already there."""
    deleted_ids = " UNION ALL ".join(
        f"SELECT id FROM {table} WHERE rowid IN {_doomed(table)}"
        for (table,) in conn.execute(
            "SELECT m.name FROM sqlite_schema m, pragma_table_info(m.name) c"
            " WHERE m.type = 'table' AND c.name = 'id'"
        )
    )
    done = 0
    while True:
        (top,) = conn.execute("SELECT max(seq) FROM temp.doomed").fetchone()
        if top == done:  # the foreign keys are followed; apply the other rules
            for rule in RULES:
                conn.execute(rule.format(deleted_ids=deleted_ids))
            if conn.execute("SELECT max(seq) FROM temp.doomed").fetchone() == (top,):
                return
            continue
        new_tables = [table for (table,) in conn.execute(
            "SELECT DISTINCT tbl FROM temp.doomed WHERE seq > ? AND seq <= ?", (done, top))]
        for (child, column), (parent, key) in edges.items():
            if parent in new_tables and ON_DELETE[child, column] is DELETE:
                conn.execute(
                    f"INSERT OR IGNORE INTO temp.doomed (tbl, rid) SELECT '{child}', rowid FROM {child}"
                    f" WHERE {column} IN (SELECT {key} FROM {parent} WHERE rowid IN"
                    f" (SELECT rid FROM temp.doomed WHERE tbl = '{parent}' AND seq > ? AND seq <= ?))",
                    (done, top),
                )
        done = top


def _revoke(conn):
    """Revoke the active runs in scope of the deletion. Returns their ids.

    Deleted runs are revoked by their deletion. Kept runs started by a revoked
    run, or linked by SCOPE_LINKS, and theirs in turn, get cancel_reason
    'revoked' while their status stays running for the run's owner to end it.
    """
    seeds = " UNION ".join((f"SELECT id FROM runs WHERE rowid IN {_doomed('runs')}", *SCOPE_LINKS))
    in_scope = f"""WITH RECURSIVE scope (id) AS (
        {seeds}
        UNION
        SELECT r.id FROM runs r JOIN scope s ON r.parent_run_id = s.id OR r.dispatched_by_run_id = s.id
    )"""
    revoked = sorted(run_id for (run_id,) in conn.execute(
        f"{in_scope} SELECT id FROM runs WHERE status = 'running' AND id IN (SELECT id FROM scope)"))
    conn.execute(
        f"{in_scope} UPDATE runs SET cancel_reason = 'revoked'"
        f" WHERE status = 'running' AND id IN (SELECT id FROM scope) AND rowid NOT IN {_doomed('runs')}"
    )
    return revoked


def _queue_index_removals(conn):
    """Queue the search index removals for the deleted passages and memory records.

    Index rows are kept per project, so a material's passages are removed for its
    project once no other material version there uses the same file.
    """
    conn.execute(f"""INSERT INTO index_queue (target, target_id, project_id, op)
        SELECT DISTINCT 'passage', p.id, m.project_id, 'remove'
        FROM material_versions v
        JOIN materials m ON m.id = v.material_id
        JOIN extractions e ON e.file_sha256 = v.file_sha256
        JOIN passages p ON p.extraction_id = e.id
        WHERE v.rowid IN {_doomed('material_versions')}
        AND NOT EXISTS (
            SELECT 1 FROM material_versions w JOIN materials n ON n.id = w.material_id
            WHERE w.file_sha256 = v.file_sha256 AND n.project_id = m.project_id
            AND w.rowid NOT IN {_doomed('material_versions')})
        ORDER BY p.id""")
    conn.execute(f"""INSERT INTO index_queue (target, target_id, project_id, op)
        SELECT 'memory', id, project_id, 'remove' FROM memory_records
        WHERE rowid IN {_doomed('memory_records')} ORDER BY id""")
