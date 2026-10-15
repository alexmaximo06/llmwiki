"""Editing a document's text must never overwrite a binary source file on disk."""

import uuid
from pathlib import Path

import pytest

USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


async def _seed(db, filename: str, file_type: str) -> str:
    await db.execute(
        "INSERT OR IGNORE INTO workspace (id, name, description, user_id) VALUES ('ws', 'ws', '', ?)",
        (USER_ID,),
    )
    doc_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO documents (id, user_id, filename, title, path, relative_path, source_kind, "
        "file_type, status, content, highlights, tags, version, document_number) "
        "VALUES (?, ?, ?, ?, '/', ?, 'source', ?, 'ready', 'old text', '[]', '[]', 0, 1)",
        (doc_id, USER_ID, filename, filename, filename, file_type),
    )
    await db.commit()
    return doc_id


@pytest.fixture
async def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from config import settings
    from infra.db.sqlite import create_pool
    from services.local import LocalDocumentService

    monkeypatch.setattr(settings, "WORKSPACE_PATH", str(tmp_path))
    db = await create_pool(str(tmp_path / "index.db"))
    yield LocalDocumentService(db, USER_ID), db
    await db.close()


async def test_pdf_text_edit_leaves_the_pdf_untouched(service, tmp_path: Path) -> None:
    svc, db = service
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.7 original")
    doc_id = await _seed(db, "paper.pdf", "pdf")

    await svc.update_content(doc_id, "corrected extracted text")

    assert (tmp_path / "paper.pdf").read_bytes() == b"%PDF-1.7 original"
    assert (await svc.get_content(doc_id))["content"] == "corrected extracted text"


async def test_markdown_edit_still_writes_the_file(service, tmp_path: Path) -> None:
    svc, db = service
    (tmp_path / "note.md").write_text("old text")
    doc_id = await _seed(db, "note.md", "md")

    await svc.update_content(doc_id, "new text")

    assert (tmp_path / "note.md").read_text() == "new text"
