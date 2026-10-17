"""Tier 2: move tool flow (MoveHandler → SqliteVaultFS + workspace files)."""

import pytest


def _kb(kb_id: str) -> dict:
    return {"id": kb_id, "name": "test-workspace", "slug": "test-workspace"}


@pytest.fixture
def handlers(fs):
    from tools.move import MoveHandler
    from tools.write import WriteHandler

    instance, kb_id = fs
    return WriteHandler(instance, _kb(kb_id)), MoveHandler(instance, _kb(kb_id))


async def _page(writer, path: str, name: str, body: str) -> None:
    result = await writer.create(path, name, body, ["test"], "2026-10-10", False)
    assert result.startswith("Created"), result


async def _row(relative_path: str) -> dict | None:
    from vaultfs.sqlite import SqliteVaultFS

    cursor = await SqliteVaultFS._db_or_raise().execute(
        "SELECT id, document_number, content FROM documents WHERE relative_path = ?",
        (relative_path,),
    )
    row = await cursor.fetchone()
    return {"id": row[0], "document_number": row[1], "content": row[2]} if row else None


async def test_page_move_keeps_identity_and_relocates_the_file(handlers, workspace):
    writer, mover = handlers
    await _page(writer, "/wiki/", "ue3", "Unreal Engine 3 notes.")
    before = await _row("wiki/ue3.md")

    result = await mover.move("/wiki/ue3.md", "/wiki/games/")

    assert "Moved 1 page(s):" in result
    assert "/wiki/ue3.md → /wiki/games/ue3.md" in result
    after = await _row("wiki/games/ue3.md")
    assert after["id"] == before["id"]
    assert after["document_number"] == before["document_number"]
    assert await _row("wiki/ue3.md") is None
    assert (workspace / "wiki" / "games" / "ue3.md").is_file()
    assert not (workspace / "wiki" / "ue3.md").exists()


async def test_folder_move_keeps_structure_and_prunes_empty_folders(handlers, workspace):
    writer, mover = handlers
    await _page(writer, "/wiki/unreal-engine/ue3/", "ue3", "UE3.")
    await _page(writer, "/wiki/unreal-engine/lead/", "lead", "LEAD.")

    result = await mover.move("/wiki/unreal-engine/", "/wiki/games/unreal/")

    assert "Moved 2 page(s):" in result
    assert (workspace / "wiki" / "games" / "unreal" / "ue3" / "ue3.md").is_file()
    assert (workspace / "wiki" / "games" / "unreal" / "lead" / "lead.md").is_file()
    assert not (workspace / "wiki" / "unreal-engine").exists()
    assert (workspace / "wiki").is_dir()


async def test_folder_without_trailing_slash_is_treated_as_a_folder(handlers, workspace):
    writer, mover = handlers
    await _page(writer, "/wiki/games/", "alpha", "A.")

    result = await mover.move("/wiki/games", "/wiki/play")

    assert "/wiki/games/alpha.md → /wiki/play/alpha.md" in result
    assert (workspace / "wiki" / "play" / "alpha.md").is_file()


async def test_links_to_and_from_moved_pages_are_rewritten(handlers, fs, workspace):
    writer, mover = handlers
    instance, _ = fs
    # Headings keep the links out of the auto-generated frontmatter description.
    await _page(
        writer, "/wiki/", "root",
        "# Root\n\nSee [UE3](unreal-engine/ue3.md) and [setup](/wiki/unreal-engine/ue3.md#setup).",
    )
    await _page(
        writer, "/wiki/unreal-engine/", "ue3",
        "# UE3\n\n[tools](toolchain.md) and [home](../root.md)",
    )
    await _page(writer, "/wiki/unreal-engine/", "toolchain", "Toolchain.")

    result = await mover.move("/wiki/unreal-engine/", "/wiki/games/unreal/")

    assert "Updated 3 link(s) in 2 page(s):" in result
    root = await _row("wiki/root.md")
    assert "[UE3](games/unreal/ue3.md)" in root["content"]
    assert "[setup](/wiki/games/unreal/ue3.md#setup)" in root["content"]
    assert (workspace / "wiki" / "root.md").read_text(encoding="utf-8") == root["content"]

    ue3 = await _row("wiki/games/unreal/ue3.md")
    # The sibling link survives the folder move unchanged; the parent link
    # would only resolve through the index's root fallback, so it is made explicit.
    assert "[tools](toolchain.md)" in ue3["content"]
    assert "[home](/wiki/root.md)" in ue3["content"]

    backlinks = await instance.get_backlinks(ue3["id"])
    assert [(b["path"], b["filename"]) for b in backlinks] == [("/wiki/", "root.md")]


async def test_links_unrelated_to_the_move_are_left_alone(handlers):
    writer, mover = handlers
    await _page(writer, "/wiki/", "overview", "Hub.")
    await _page(writer, "/wiki/notes/", "page", "Back to [hub](overview.md).")
    await _page(writer, "/wiki/", "other", "Other.")
    before = await _row("wiki/notes/page.md")

    result = await mover.move("/wiki/other.md", "/wiki/archive/")

    assert "Updated" not in result
    assert (await _row("wiki/notes/page.md"))["content"] == before["content"]


async def test_rename_keeps_the_file_type(handlers, workspace):
    writer, mover = handlers
    await _page(writer, "/wiki/notes/", "draft", "Draft.")

    renamed = await mover.move("/wiki/notes/draft.md", "/wiki/notes/final.md")
    retyped = await mover.move("/wiki/notes/final.md", "/wiki/notes/final.txt")

    assert "/wiki/notes/draft.md → /wiki/notes/final.md" in renamed
    assert "can't change the file type" in retyped
    assert (workspace / "wiki" / "notes" / "final.md").is_file()


@pytest.mark.parametrize(
    ("source", "destination", "expected"),
    [
        ("/wiki/a.md", "/wiki/games/", "`/wiki/games/a.md` already exists"),
        ("/wiki/overview.md", "/wiki/games/", "structural wiki page"),
        ("/wiki/a.md", "/wiki/overview.md", "reserved for a structural wiki page"),
        ("/notes.md", "/wiki/", "outside `/wiki/`"),
        ("/wiki/a.md", "/wiki/../escape.md", "not a valid wiki path"),
        ("/wiki/a.md", "/wiki/.hidden/", "not a valid wiki path"),
        ("/wiki/games/", "/wiki/games/sub/", "into itself"),
        ("/wiki/", "/wiki/elsewhere/", "wiki root can't be moved"),
        ("/wiki/missing.md", "/wiki/games/", "not found"),
        ("/wiki/a.md", "/wiki/", "already at that location"),
    ],
)
async def test_rejected_moves_change_nothing(handlers, workspace, source, destination, expected):
    writer, mover = handlers
    await _page(writer, "/wiki/", "overview", "Hub.")
    await _page(writer, "/wiki/", "a", "Root A.")
    await _page(writer, "/wiki/games/", "a", "Games A.")

    result = await mover.move(source, destination)

    assert expected in result
    assert (await _row("wiki/a.md"))["content"].endswith("Root A.")
    assert (workspace / "wiki" / "a.md").is_file()
    assert (workspace / "wiki" / "games" / "a.md").is_file()


async def test_disk_failure_rolls_back_index_and_earlier_renames(handlers, workspace):
    writer, mover = handlers
    await _page(writer, "/wiki/f/", "a", "A.")
    await _page(writer, "/wiki/f/sub/", "b", "B.")
    # An unindexed file where the second page's destination folder must go.
    (workspace / "wiki" / "g").mkdir()
    (workspace / "wiki" / "g" / "sub").write_text("not a folder", encoding="utf-8")

    result = await mover.move("/wiki/f/", "/wiki/g/")

    assert "Nothing was moved" in result
    assert await _row("wiki/f/a.md") is not None
    assert await _row("wiki/f/sub/b.md") is not None
    assert await _row("wiki/g/a.md") is None
    assert (workspace / "wiki" / "f" / "a.md").is_file()
    assert not (workspace / "wiki" / "g" / "a.md").exists()


async def test_backends_without_move_support_report_it():
    from tools.move import MoveHandler
    from vaultfs.base import VaultFS

    class HostedLikeFS:
        async def list_documents(self, kb_id):
            return [{"id": "1", "path": "/wiki/", "filename": "a.md"}]

        move_documents = VaultFS.move_documents

    result = await MoveHandler(HostedLikeFS(), _kb("kb")).move("/wiki/a.md", "/wiki/b/")

    assert "only supported in local mode" in result


async def test_move_tool_is_registered_as_non_destructive():
    from tools.move import register

    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    register(mcp, lambda _ctx: "user", lambda _user_id: None)

    tool = next(t for t in await mcp.list_tools() if t.name == "move")
    assert set(tool.inputSchema["required"]) == {"knowledge_base", "source", "destination"}
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.readOnlyHint is False
