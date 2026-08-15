"""Legacy local DBs predate the highlight-search columns and the singleton
document layout — startup migration and document lookups must work on both."""

import json
import sqlite3
import uuid

USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

LEGACY_CHUNKS_DDL = """
CREATE TABLE document_chunks (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    document_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    page INTEGER,
    start_char INTEGER,
    token_count INTEGER NOT NULL,
    header_breadcrumb TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(document_id, chunk_index)
)
"""


async def _seed_doc(db, metadata: dict | None = None) -> str:
    ws_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO workspace (id, name, description, user_id) VALUES (?, 'ws', '', ?)",
        (ws_id, USER_ID),
    )
    doc_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO documents (id, user_id, filename, title, path, relative_path, source_kind, "
        "file_type, status, content, metadata, tags, version, document_number) "
        "VALUES (?, ?, 'doc.md', 'Doc', '/', 'doc.md', 'source', 'md', 'ready', 'body', ?, '[]', 0, 1)",
        (doc_id, USER_ID, json.dumps(metadata or {})),
    )
    await db.commit()
    return doc_id


async def test_create_pool_migrates_legacy_chunk_columns(tmp_path):
    from infra.db.sqlite import create_pool

    path = tmp_path / "index.db"
    legacy = sqlite3.connect(path)
    legacy.execute(LEGACY_CHUNKS_DDL)
    legacy.execute(
        "INSERT INTO document_chunks (document_id, chunk_index, content, token_count) "
        "VALUES ('d1', 0, 'hello world', 3)",
    )
    legacy.commit()
    legacy.close()

    db = await create_pool(str(path))

    cursor = await db.execute("PRAGMA table_info(document_chunks)")
    columns = {row[1] for row in await cursor.fetchall()}
    assert {"source_content", "annotations_text", "has_highlight"} <= columns

    cursor = await db.execute(
        "SELECT source_content, has_highlight FROM document_chunks WHERE document_id = 'd1'",
    )
    source_content, has_highlight = await cursor.fetchone()
    assert source_content == "hello world"
    assert has_highlight == 0

    await db.close()


async def test_find_by_path_uses_local_schema(tmp_path):
    from infra.db.sqlite import create_pool, SQLiteDocumentRepository

    db = await create_pool(str(tmp_path / "index.db"))
    doc_id = await _seed_doc(db)
    repo = SQLiteDocumentRepository(db)

    found = await repo.find_by_path("ignored-kb-id", USER_ID, "doc.md", "/")
    assert found is not None
    assert found["id"] == doc_id

    assert await repo.find_by_path("ignored-kb-id", USER_ID, "missing.md", "/") is None

    await db.close()


async def test_get_by_source_url_resolves_singleton_kb_id(tmp_path):
    from infra.db.sqlite import create_pool, SQLiteDocumentRepository

    db = await create_pool(str(tmp_path / "index.db"))
    doc_id = await _seed_doc(db, metadata={"source_url": "https://example.com/a"})
    repo = SQLiteDocumentRepository(db)

    found = await repo.get_by_source_url("https://example.com/a")
    assert found is not None
    assert found["id"] == doc_id

    cursor = await db.execute("SELECT id FROM workspace LIMIT 1")
    ws_id = (await cursor.fetchone())[0]
    assert found["knowledge_base_id"] == ws_id

    await db.close()
