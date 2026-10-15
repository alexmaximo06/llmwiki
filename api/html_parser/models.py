from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Element:
    id: str
    content: str
    kind: str  # "heading", "paragraph", "table", "list", "blockquote", "code", "form", "text"
    content_start_offset: int | None = None
    content_end_offset: int | None = None


@dataclass
class Image:
    url: str
    alt: str = ""
    ref: str = ""
    width: int | None = None
    height: int | None = None
    candidate_urls: list[str] = field(default_factory=list)


@dataclass
class TextAnchor:
    """Plaintext-relative anchor: character offsets into the canonical
    plaintext produced during parsing. Mirrors the API-level TextAnchor type.
    """
    text_start: int
    text_end: int
    text_content: str
    prefix: str | None = None
    suffix: str | None = None


@dataclass
class MappedHighlight:
    """A highlight enriched with a plaintext-relative position.

    Carries the caller-supplied highlight payload alongside the `text_anchor`
    computed by the parser. Returned in ParseResult.highlights when the caller
    passes highlights to Parser.parse(...).
    """
    payload: dict[str, Any]
    text_anchor: TextAnchor | None = None


@dataclass
class ParseResult:
    content: str
    images: list[Image] = field(default_factory=list)
    form_elements: list = field(default_factory=list)
    url: str = ""
    elements: list[Element] = field(default_factory=list)
    plaintext: str = ""
    highlights: list[MappedHighlight] = field(default_factory=list)
