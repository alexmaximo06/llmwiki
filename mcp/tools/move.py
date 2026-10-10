"""Move tool — relocate wiki pages and folders without breaking their links."""

import re
from dataclasses import dataclass

from vaultfs import VaultFS
from vaultfs.base import MoveConflictError

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from .helpers import deep_link, resolve_path
from .references import WIKI_LINK_RE, lookup_wiki_path, resolve_wiki_href, update_references

_WIKI_ROOT = "/wiki/"
_PROTECTED_PATHS = {"/wiki/overview.md", "/wiki/log.md", "/wiki/index.json"}
# Characters Windows rejects in file names; the workspace may live on any OS.
_INVALID_SEGMENT_RE = re.compile(r'[\x00-\x1f<>:"|?*\\]')


def _normalize(path: str) -> str:
    return "/" + path.strip().replace("\\", "/").lstrip("/")


def _path_error(path: str) -> str | None:
    if not path.startswith(_WIKI_ROOT):
        return (
            f"Error: `{path}` is outside `/wiki/`. Only wiki pages can be moved; "
            "sources are read-only."
        )
    rest = path[len(_WIKI_ROOT):].removesuffix("/")
    if not rest:
        return None
    for segment in rest.split("/"):
        if (
            not segment
            or segment in (".", "..")
            or segment != segment.strip()
            or segment.endswith(".")
            or _INVALID_SEGMENT_RE.search(segment)
        ):
            return f"Error: `{path}` is not a valid wiki path."
    return None


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _wiki_relative(dir_path: str, filename: str) -> str:
    return (dir_path + filename)[len(_WIKI_ROOT):]


def _dir_of(wiki_relative: str) -> str:
    return wiki_relative.rsplit("/", 1)[0] + "/" if "/" in wiki_relative else ""


def _format_href(target: str, page_dir: str, absolute: bool) -> str:
    """Spell a link so the reference index and the web viewer both resolve it to `target`.

    Both treat a path containing `/` as wiki-root-relative, and a bare filename
    as relative to the page — so a root-level target linked from a subfolder
    needs the explicit `/wiki/` prefix.
    """
    if absolute or ("/" not in target and page_dir):
        return _WIKI_ROOT + target
    return target


def _rewrite_page_links(
    content: str,
    old_dir: str,
    new_dir: str,
    old_map: dict[str, str],
    new_map: dict[str, str],
    moved: set[str],
) -> tuple[str, int]:
    """Re-point links whose target moved, or that this page's own move broke."""
    page_moved = old_dir != new_dir
    replacements: list[tuple[int, int, str]] = []
    for match in WIKI_LINK_RE.finditer(content):
        target_href, hashmark, fragment = match.group(1).partition("#")
        if not target_href:
            continue
        resolved = resolve_wiki_href(target_href, old_dir)
        target = lookup_wiki_path(resolved, old_map) if resolved else None
        if not target or not (page_moved or target in moved):
            continue
        # Exact match only: the web viewer has no root-level fallback, so a link
        # the index would still resolve that way can be broken in the UI.
        still_resolved = resolve_wiki_href(target_href, new_dir)
        if still_resolved and new_map.get(still_resolved.lower()) == target:
            continue
        new_href = _format_href(target, new_dir, absolute=target_href.startswith(_WIKI_ROOT))
        replacements.append((match.start(1), match.end(1), new_href + hashmark + fragment))

    for start, end, new_href in reversed(replacements):
        content = content[:start] + new_href + content[end:]
    return content, len(replacements)


@dataclass(frozen=True)
class PlannedMove:
    doc: dict
    new_dir: str
    new_filename: str

    @property
    def old_path(self) -> str:
        return self.doc["path"] + self.doc["filename"]

    @property
    def new_path(self) -> str:
        return self.new_dir + self.new_filename


class MoveHandler:
    """Moves wiki pages and folders, then repairs links affected by the move."""

    def __init__(self, fs: VaultFS, kb: dict):
        self.fs = fs
        self.kb = kb
        self.kb_id = str(kb["id"])

    async def move(self, source: str, destination: str) -> str:
        if not (source or "").strip() or not (destination or "").strip():
            return "Error: source and destination are required."
        source = _normalize(source)
        destination = _normalize(destination)
        for path in (source, destination):
            error = _path_error(path)
            if error:
                return error

        docs = await self.fs.list_documents(self.kb_id)
        plan = self._plan(source, destination, docs)
        if isinstance(plan, str):
            return plan

        try:
            await self.fs.move_documents(
                [(str(m.doc["id"]), m.new_dir, m.new_filename) for m in plan]
            )
        except NotImplementedError:
            return "Error: moving pages is only supported in local mode."
        except MoveConflictError as e:
            return f"Error: `{e.path}` already exists. Nothing was moved."
        except (LookupError, ValueError, OSError) as e:
            return f"Error: {e}. Nothing was moved."

        rewrites = await self._rewrite_links(plan)
        return self._format_response(plan, rewrites)

    def _plan(self, source: str, destination: str, docs: list[dict]) -> list[PlannedMove] | str:
        by_path = {d["path"] + d["filename"]: d for d in docs}
        is_folder = source.endswith("/") or (
            source not in by_path and any(d["path"].startswith(source + "/") for d in docs)
        )
        if is_folder:
            planned = self._plan_folder(
                source.rstrip("/") + "/", destination.rstrip("/") + "/", docs
            )
        else:
            planned = self._plan_file(source, destination, by_path)
        if isinstance(planned, str):
            return planned
        return self._check_conflicts(planned, docs)

    @staticmethod
    def _plan_folder(source: str, destination: str, docs: list[dict]) -> list[PlannedMove] | str:
        if source == _WIKI_ROOT:
            return "Error: the wiki root can't be moved. Move its pages or subfolders instead."
        if destination == source:
            return f"Error: `{source}` is already at that location."
        if destination.startswith(source):
            return f"Error: can't move `{source}` into itself."
        members = [d for d in docs if d["path"].startswith(source)]
        if not members:
            return f"No pages found under `{source}`."
        return [
            PlannedMove(d, destination + d["path"][len(source):], d["filename"])
            for d in members
        ]

    @staticmethod
    def _plan_file(source: str, destination: str, by_path: dict[str, dict]) -> list[PlannedMove] | str:
        doc = by_path.get(source)
        if not doc:
            return f"Page `{source}` not found."
        if source in _PROTECTED_PATHS:
            return f"Error: `{source}` is a structural wiki page and stays in place."

        # A destination without an extension names a folder, so the page keeps its filename.
        if destination.endswith("/") or "." not in destination.rsplit("/", 1)[-1]:
            return [PlannedMove(doc, destination.rstrip("/") + "/", doc["filename"])]

        new_dir, new_filename = resolve_path(destination)
        if _extension(new_filename) != _extension(doc["filename"]):
            return (
                f"Error: renaming can't change the file type "
                f"(`{doc['filename']}` → `{new_filename}`)."
            )
        return [PlannedMove(doc, new_dir, new_filename)]

    @staticmethod
    def _check_conflicts(planned: list[PlannedMove], docs: list[dict]) -> list[PlannedMove] | str:
        moving_ids = {str(m.doc["id"]) for m in planned}
        # casefold: the workspace may be on a case-insensitive filesystem.
        occupied = {
            (d["path"] + d["filename"]).casefold(): d["path"] + d["filename"]
            for d in docs
            if str(d["id"]) not in moving_ids
        }
        for m in planned:
            if m.new_path == m.old_path:
                return f"Error: `{m.old_path}` is already at that location."
            if m.new_path in _PROTECTED_PATHS:
                return f"Error: `{m.new_path}` is reserved for a structural wiki page."
            existing = occupied.get(m.new_path.casefold())
            if existing:
                return f"Error: `{existing}` already exists. Nothing was moved."
        return planned

    async def _rewrite_links(self, plan: list[PlannedMove]) -> list[tuple[str, int]]:
        """Rewrite links across the wiki; returns (page path, links changed) per rewritten page."""
        new_to_old = {
            _wiki_relative(m.new_dir, m.new_filename): _wiki_relative(m.doc["path"], m.doc["filename"])
            for m in plan
        }
        pages = await self.fs.list_documents_with_content(self.kb_id, path_glob=_WIKI_ROOT + "*")

        # Lowercased wiki path -> current path, keyed as before and after the move.
        old_map: dict[str, str] = {}
        new_map: dict[str, str] = {}
        for page in pages:
            new_rel = _wiki_relative(page["path"], page["filename"])
            old_map[new_to_old.get(new_rel, new_rel).lower()] = new_rel
            new_map[new_rel.lower()] = new_rel

        rewrites: list[tuple[str, int]] = []
        for page in pages:
            if page.get("file_type") != "md":
                continue
            new_rel = _wiki_relative(page["path"], page["filename"])
            old_rel = new_to_old.get(new_rel, new_rel)
            content = page.get("content") or ""
            updated, changed = _rewrite_page_links(
                content, _dir_of(old_rel), _dir_of(new_rel), old_map, new_map, set(new_to_old),
            )
            if changed:
                self.fs.write_to_disk(page["path"], page["filename"], updated)
                await self.fs.update_document(str(page["id"]), updated)
                rewrites.append((page["path"] + page["filename"], changed))
            if changed or new_rel in new_to_old:
                await update_references(self.fs, self.kb_id, str(page["id"]), updated, page["path"])
        return rewrites

    def _format_response(self, plan: list[PlannedMove], rewrites: list[tuple[str, int]]) -> str:
        lines = [f"Moved {len(plan)} page(s):"]
        lines.extend(f"  {m.old_path} → {m.new_path}" for m in plan)
        if rewrites:
            total = sum(count for _, count in rewrites)
            lines.append(f"\nUpdated {total} link(s) in {len(rewrites)} page(s):")
            lines.extend(f"  {path} ({count})" for path, count in rewrites)
        if len(plan) == 1:
            m = plan[0]
            lines.append(f"\n[View]({deep_link(self.kb['slug'], m.new_dir, m.new_filename)})")
        return "\n".join(lines)


def register(mcp: FastMCP, get_user_id, fs_factory) -> None:

    @mcp.tool(
        name="move",
        description=(
            "Move or rename a wiki page, or move a whole wiki folder.\n\n"
            "Examples:\n"
            "- `source=\"/wiki/ue3.md\", destination=\"/wiki/games/\"` — move into a folder, keeping the filename\n"
            "- `source=\"/wiki/ue3.md\", destination=\"/wiki/games/unreal-3.md\"` — move and rename\n"
            "- `source=\"/wiki/unreal-engine/\", destination=\"/wiki/games/unreal/\"` — move a folder and everything in it\n\n"
            "A destination without a file extension is treated as a folder. Links in other pages that "
            "point at moved pages, and relative links inside moved pages, are rewritten automatically. "
            "Comments, citations, history and page numbers are preserved.\n"
            "Only pages under `/wiki/` can be moved; overview.md and log.md stay in place. "
            "Nothing changes if any destination already exists."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    )
    async def move(ctx: Context, knowledge_base: str, source: str, destination: str) -> str:
        user_id = get_user_id(ctx)
        fs = fs_factory(user_id)
        kb = await fs.resolve_kb(knowledge_base)
        if not kb:
            return f"Knowledge base '{knowledge_base}' not found."
        return await MoveHandler(fs, kb).move(source, destination)
