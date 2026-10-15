"""Text chunker for wiki pages + notes created via MCP.

Mirrors api/services/chunker.py so MCP-created content shows up in
keyword search alongside API-created content. Splits content into
~512 token chunks with ~128 token overlap, tracking markdown headers
for breadcrumb context. Persists into `document_chunks` (Postgres via
asyncpg, SQLite via aiosqlite — chunks_fts is kept in sync by triggers
in the local schema).
"""

import hashlib
import logging
import re
from dataclasses import dataclass

import aiosqlite
import asyncpg

logger = logging.getLogger(__name__)

CHUNK_SIZE = 512
CHUNK_OVERLAP = 128
MIN_CHUNK_TOKENS = 32
MAX_CHUNK_CHARS = 10_000  # matches DB constraint chk_chunks_content_length

SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s+")
HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass
class Chunk:
    index: int
    content: str
    page: int | None
    start_char: int
    token_count: int
    header_breadcrumb: str = ""


def chunk_text(
    content: str,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
    page: int | None = None,
) -> list[Chunk]:
    if not content or not content.strip():
        return []

    paragraphs = _split_paragraphs(content)
    header_stack: list[tuple[int, str]] = []
    chunks: list[Chunk] = []
    current_blocks: list[str] = []
    current_tokens = 0
    current_start = 0
    char_pos = 0

    for para in paragraphs:
        para_tokens = _estimate_tokens(para)
        breadcrumb = " > ".join(t for _, t in header_stack)

        header_match = HEADER_RE.match(para)
        if header_match:
            level = len(header_match.group(1))
            heading = header_match.group(2).strip()
            header_stack = [(existing_level, t) for existing_level, t in header_stack if existing_level < level]
            header_stack.append((level, heading))

        pending_text = "\n\n".join(current_blocks)
        if current_tokens + para_tokens > chunk_size and _estimate_tokens(pending_text) >= MIN_CHUNK_TOKENS:
            chunks.append(Chunk(
                index=len(chunks),
                content=pending_text,
                page=page,
                start_char=current_start,
                token_count=_estimate_tokens(pending_text),
                header_breadcrumb=breadcrumb,
            ))

            overlap_blocks, overlap_tokens = _get_overlap(current_blocks, overlap)
            current_blocks = overlap_blocks
            current_tokens = overlap_tokens
            current_start = char_pos - sum(len(b) + 2 for b in overlap_blocks)

        current_blocks.append(para)
        current_tokens += para_tokens
        char_pos += len(para) + 2

    if current_blocks:
        final_text = "\n\n".join(current_blocks)
        chunks.append(Chunk(
            index=len(chunks),
            content=final_text,
            page=page,
            start_char=current_start,
            token_count=_estimate_tokens(final_text),
            header_breadcrumb=" > ".join(t for _, t in header_stack),
        ))

    return _enforce_max_chars(chunks)


def _enforce_max_chars(chunks: list[Chunk]) -> list[Chunk]:
    """Split any chunk whose content exceeds MAX_CHUNK_CHARS.

    Paragraph-based chunking emits one chunk per paragraph when the paragraph
    is bigger than CHUNK_SIZE. CJK text and long code blocks routinely blow
    past the 10k-char DB constraint. Split on sentence boundaries; hard-slice
    only if no break is available.
    """
    if not any(len(c.content) > MAX_CHUNK_CHARS for c in chunks):
        return chunks

    result: list[Chunk] = []
    for c in chunks:
        if len(c.content) <= MAX_CHUNK_CHARS:
            result.append(Chunk(
                index=len(result), content=c.content, page=c.page,
                start_char=c.start_char, token_count=c.token_count,
                header_breadcrumb=c.header_breadcrumb,
            ))
            continue
        # Each split piece gets its own start_char so text-anchor highlight
        # mapping can compute correct end offsets per piece. Mirrors the
        # fix in api/services/chunker.py.
        base = c.start_char or 0
        offset = 0
        for piece in _split_oversized(c.content):
            result.append(Chunk(
                index=len(result), content=piece, page=c.page,
                start_char=base + offset, token_count=_estimate_tokens(piece),
                header_breadcrumb=c.header_breadcrumb,
            ))
            offset += len(piece)
    return result


def _split_oversized(text: str) -> list[str]:
    parts = SENTENCE_RE.split(text)
    pieces: list[str] = []
    current = ""
    for part in parts:
        candidate = (current + " " + part).strip() if current else part
        if len(candidate) <= MAX_CHUNK_CHARS:
            current = candidate
        else:
            if current:
                pieces.append(current)
            if len(part) <= MAX_CHUNK_CHARS:
                current = part
            else:
                for i in range(0, len(part), MAX_CHUNK_CHARS):
                    pieces.append(part[i:i + MAX_CHUNK_CHARS])
                current = ""
    if current:
        pieces.append(current)
    return pieces


async def store_chunks_pg(
    conn: asyncpg.Connection,
    document_id: str,
    user_id: str,
    knowledge_base_id: str,
    chunks: list[Chunk],
) -> None:
    # ponytail: keeps only the unchanged prefix; mid-document edits still rewrite the tail.
    kept = await _count_unchanged_prefix_pg(conn, document_id, chunks)
    await conn.execute(
        "DELETE FROM document_chunks WHERE document_id = $1 AND chunk_index >= $2", document_id, kept,
    )
    changed = chunks[kept:]
    if not changed:
        return
    # source_content mirrors content at ingest; highlight CRUD may later
    # rewrite content with annotations appended. See api/services/highlight_chunks.
    await conn.execute(
        "INSERT INTO document_chunks "
        "(document_id, user_id, knowledge_base_id, chunk_index, content, source_content, page, start_char, "
        " token_count, header_breadcrumb) "
        "SELECT $1::uuid, $2::uuid, $3::uuid, row.chunk_index, row.content, row.content, "
        "       row.page, row.start_char, row.token_count, row.header_breadcrumb "
        "FROM UNNEST($4::int[], $5::text[], $6::int[], $7::int[], $8::int[], $9::text[]) "
        "AS row(chunk_index, content, page, start_char, token_count, header_breadcrumb)",
        document_id,
        user_id,
        knowledge_base_id,
        [c.index for c in changed],
        [c.content for c in changed],
        [c.page for c in changed],
        [c.start_char for c in changed],
        [c.token_count for c in changed],
        [c.header_breadcrumb for c in changed],
    )


async def _count_unchanged_prefix_pg(conn: asyncpg.Connection, document_id: str, chunks: list[Chunk]) -> int:
    """Count leading stored chunks identical to the new ones, so they can be left in place."""
    rows = await _fetch_chunk_fingerprints_pg(conn, document_id)
    kept = 0
    for row, chunk in zip(rows, chunks, strict=False):
        if tuple(row) != _chunk_fingerprint(chunk):
            break
        kept += 1
    return kept


def _chunk_fingerprint(chunk: Chunk) -> tuple:
    digest = hashlib.md5(chunk.content.encode()).hexdigest()
    return (chunk.index, digest, chunk.page, chunk.start_char, chunk.token_count, chunk.header_breadcrumb)


async def _fetch_chunk_fingerprints_pg(conn: asyncpg.Connection, document_id: str) -> list[asyncpg.Record]:
    return await conn.fetch(
        "SELECT chunk_index, md5(source_content), page, start_char, token_count, header_breadcrumb "
        "FROM document_chunks WHERE document_id = $1 ORDER BY chunk_index",
        document_id,
    )


async def store_chunks_sqlite(
    db: aiosqlite.Connection,
    document_id: str,
    chunks: list[Chunk],
) -> None:
    """SQLite variant. Triggers on chunks_fts keep the FTS index in sync."""
    await db.execute("DELETE FROM document_chunks WHERE document_id = ?", (document_id,))
    if chunks:
        await db.executemany(
            "INSERT INTO document_chunks "
            "(document_id, chunk_index, content, source_content, page, start_char, token_count, header_breadcrumb) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (document_id, c.index, c.content, c.content, c.page, c.start_char, c.token_count,
                 c.header_breadcrumb)
                for c in chunks
            ],
        )


def _split_paragraphs(text: str) -> list[str]:
    parts = re.split(r"\n\s*\n", text)
    return [p.strip() for p in parts if p.strip()]


def _get_overlap(blocks: list[str], target_tokens: int) -> tuple[list[str], int]:
    result: list[str] = []
    tokens = 0
    for block in reversed(blocks):
        bt = _estimate_tokens(block)
        if tokens + bt > target_tokens:
            break
        result.insert(0, block)
        tokens += bt
    return result, tokens
