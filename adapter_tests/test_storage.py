from __future__ import annotations

import aiosqlite

from xiaoheihe.database import MIGRATIONS, Database


async def test_database_migrations_and_pragmas(tmp_path) -> None:
    database = Database(tmp_path / "adapter.db")
    await database.open()
    assert await database.schema_version() == MIGRATIONS[-1][0]
    mode = await database.fetchone("PRAGMA journal_mode")
    foreign_keys = await database.fetchone("PRAGMA foreign_keys")
    assert str(mode[0]).lower() == "wal"
    assert foreign_keys[0] == 1
    tables = await database.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
    names = {row["name"] for row in tables}
    assert {
        "account_state",
        "incoming_events",
        "processed_event_keys",
        "outgoing_replies",
        "self_comment_ids",
        "session_mappings",
        "feed_candidates",
        "runtime_errors",
        "daily_counters",
        "notification_cursors",
        "notification_backfills",
        "visual_contexts",
        "visual_context_event_links",
        "visual_context_comment_links",
        "comment_corpus",
    } <= names
    self_indexes = await database.fetchall("PRAGMA index_list(self_comment_ids)")
    assert {"idx_self_comment_post", "idx_self_comment_created"} <= {
        row["name"] for row in self_indexes
    }
    visual_indexes = await database.fetchall("PRAGMA index_list(visual_contexts)")
    assert {"idx_visual_context_lookup", "idx_visual_context_expiry"} <= {
        row["name"] for row in visual_indexes
    }
    visual_columns = await database.fetchall("PRAGMA table_info(visual_contexts)")
    assert {"owner_uid", "owner_nickname", "owner_role"} <= {row["name"] for row in visual_columns}
    corpus_columns = await database.fetchall("PRAGMA table_info(comment_corpus)")
    corpus_column_map = {row["name"]: row for row in corpus_columns}
    assert {
        "id",
        "profile_id",
        "comment_id",
        "author_level",
        "content",
        "verdict",
        "collected_at",
    } <= corpus_column_map.keys()
    assert corpus_column_map["author_level"]["notnull"] == 0
    await database.execute(
        """
        INSERT INTO comment_corpus(
            profile_id, comment_id, post_id, author_uid, content,
            verdict, created_at, collected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("default", "comment", "post", "author", "内容", "human", 1, 2),
    )
    corpus_record = await database.fetchone(
        "SELECT author_level FROM comment_corpus WHERE comment_id = ?",
        ("comment",),
    )
    assert corpus_record["author_level"] is None
    corpus_indexes = await database.fetchall("PRAGMA index_list(comment_corpus)")
    assert {"idx_corpus_verdict_level", "idx_corpus_author", "idx_corpus_created"} <= {
        row["name"] for row in corpus_indexes
    }
    lookup_columns = await database.fetchall("PRAGMA index_info(idx_visual_context_lookup)")
    assert [row["name"] for row in lookup_columns] == [
        "profile_id",
        "post_id",
        "source",
        "image_fingerprint",
        "expires_at",
        "id",
    ]
    plan = await database.fetchall(
        """
        EXPLAIN QUERY PLAN
        SELECT * FROM visual_contexts
        WHERE profile_id = ? AND post_id = ? AND source = 'original_post'
          AND image_fingerprint = ? AND expires_at > ?
        ORDER BY expires_at DESC, id DESC
        LIMIT 1
        """,
        ("default", "post", "fingerprint", 0),
    )
    details = [str(row["detail"]) for row in plan]
    assert any("idx_visual_context_lookup" in detail for detail in details)
    assert not any("TEMP B-TREE" in detail for detail in details)
    await database.close()


async def test_database_upgrades_v12_to_comment_corpus(tmp_path) -> None:
    path = tmp_path / "upgrade-v12.db"
    connection = await aiosqlite.connect(path)
    for version, script in MIGRATIONS:
        if version >= 13:
            break
        await connection.executescript(script)
    await connection.executemany(
        "INSERT INTO schema_migrations(version) VALUES (?)",
        [(version,) for version, _script in MIGRATIONS if version < 13],
    )
    await connection.commit()
    await connection.close()

    database = Database(path)
    await database.open()
    assert await database.schema_version() == 13
    tables = await database.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
    assert "comment_corpus" in {row["name"] for row in tables}
    columns = {row["name"]: row for row in await database.fetchall("PRAGMA table_info(comment_corpus)")}
    assert {"profile_id", "comment_id", "author_level", "content", "collected_at"} <= columns.keys()
    assert columns["author_level"]["notnull"] == 0
    indexes = await database.fetchall("PRAGMA index_list(comment_corpus)")
    assert {"idx_corpus_verdict_level", "idx_corpus_author", "idx_corpus_created"} <= {
        row["name"] for row in indexes
    }
    verdict_index_columns = await database.fetchall("PRAGMA index_info(idx_corpus_verdict_level)")
    assert [row["name"] for row in verdict_index_columns] == ["verdict", "author_level"]
    await database.close()


async def test_database_close_is_idempotent(tmp_path) -> None:
    database = Database(tmp_path / "adapter.db")
    await database.open()
    await database.close()
    await database.close()


async def test_database_upgrades_v2_to_latest(tmp_path) -> None:
    path = tmp_path / "upgrade.db"
    connection = await aiosqlite.connect(path)
    await connection.executescript(MIGRATIONS[0][1])
    await connection.executescript(MIGRATIONS[1][1])
    await connection.execute("INSERT INTO schema_migrations(version) VALUES (1), (2)")
    await connection.execute(
        """
        INSERT INTO daily_counters(profile_id, day, proactive_count)
        VALUES ('default', '2026-08-01', 20)
        """
    )
    await connection.commit()
    await connection.close()

    database = Database(path)
    await database.open()
    assert await database.schema_version() == MIGRATIONS[-1][0]
    columns = await database.fetchall("PRAGMA table_info(incoming_events)")
    names = {row["name"] for row in columns}
    assert {"retry_count", "next_retry_at"} <= names
    tables = await database.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
    assert "notification_cursors" in {row["name"] for row in tables}
    assert "notification_backfills" in {row["name"] for row in tables}
    counter = await database.fetchone(
        "SELECT proactive_count FROM daily_counters WHERE profile_id = 'default'"
    )
    assert counter["proactive_count"] == 0
    await database.close()


async def test_database_upgrades_v9_visual_records_with_unknown_owner(tmp_path) -> None:
    path = tmp_path / "upgrade-v9.db"
    connection = await aiosqlite.connect(path)
    for _version, script in MIGRATIONS:
        if _version >= 10:
            break
        await connection.executescript(script)
    await connection.executemany(
        "INSERT INTO schema_migrations(version) VALUES (?)",
        [(version,) for version, _script in MIGRATIONS if version < 10],
    )
    await connection.execute(
        """
        INSERT INTO visual_contexts(
            profile_id, post_id, source, image_fingerprint,
            image_hosts_json, image_count, caption, caption_hash,
            provider_id, model, created_at, expires_at
        ) VALUES ('default', 'post-old', 'original_post', 'fingerprint-old',
                  '[]', 1, '旧版图片描述', 'hash-old',
                  'vision', 'model', 1, 9999999999)
        """
    )
    await connection.commit()
    await connection.close()

    database = Database(path)
    await database.open()
    assert await database.schema_version() == MIGRATIONS[-1][0]
    record = await database.fetchone(
        "SELECT owner_uid, owner_nickname, owner_role FROM visual_contexts WHERE post_id = ?",
        ("post-old",),
    )
    assert dict(record) == {
        "owner_uid": "",
        "owner_nickname": "",
        "owner_role": "unknown",
    }
    await database.close()
