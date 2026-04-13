"""Tests for Obsidian vault operations, frontmatter, templates, and git sync."""



from src.obsidian.frontmatter import generate_frontmatter, update_frontmatter
from src.obsidian.git_sync import GitSync
from src.obsidian.templates import format_action_index, format_note_content
from src.obsidian.vault import (
    ObsidianVault,
    _format_note,
    _parse_note,
    _sanitize_filename,
)
from src.ocr.pipeline import PageText
from src.processing.actions import ActionItem
from src.processing.summarizer import NoteSummary
from src.remarkable.formats import Notebook, PageContent, TextBlock

# =====================
# _sanitize_filename
# =====================

class TestSanitizeFilename:
    def test_basic_name(self):
        assert _sanitize_filename("Meeting Notes") == "Meeting Notes"

    def test_removes_special_chars(self):
        assert _sanitize_filename('File: "test" <data>') == "File test data"

    def test_collapses_whitespace(self):
        assert _sanitize_filename("too   many   spaces") == "too many spaces"

    def test_truncates_long_names(self):
        long = "A" * 250
        result = _sanitize_filename(long)
        assert len(result) <= 200

    def test_empty_string(self):
        assert _sanitize_filename("") == "Untitled"

    def test_only_special_chars(self):
        assert _sanitize_filename(':<>"/\\|?*') == "Untitled"


# =====================
# _format_note / _parse_note
# =====================

class TestFormatParseNote:
    def test_roundtrip(self):
        fm = {"title": "Test", "tags": ["a", "b"], "source": "remarkable"}
        content = "# Hello\n\nSome content here."

        formatted = _format_note(fm, content)
        parsed_fm, parsed_content = _parse_note(formatted)

        assert parsed_fm["title"] == "Test"
        assert parsed_fm["tags"] == ["a", "b"]
        assert "Some content here" in parsed_content

    def test_parse_no_frontmatter(self):
        fm, content = _parse_note("Just plain text without frontmatter.")
        assert fm == {}
        assert "plain text" in content

    def test_parse_empty_string(self):
        fm, content = _parse_note("")
        assert fm == {}
        assert content == ""

    def test_parse_only_frontmatter(self):
        text = "---\ntitle: Only FM\n---\n"
        fm, content = _parse_note(text)
        assert fm["title"] == "Only FM"
        assert content == ""


# =====================
# ObsidianVault
# =====================

class TestObsidianVault:
    def test_resolve_path_mapped(self, tmp_path):
        vault = ObsidianVault(tmp_path, {"Work": "Notes/Work", "_default": "Inbox"})
        path = vault.resolve_path("Work", "Sprint Planning")
        assert path == tmp_path / "Notes" / "Work" / "Sprint Planning.md"

    def test_resolve_path_default(self, tmp_path):
        vault = ObsidianVault(tmp_path, {"_default": "Inbox"})
        path = vault.resolve_path("Unknown Folder", "Random Note")
        assert path == tmp_path / "Inbox" / "Random Note.md"

    def test_write_and_read_note(self, tmp_path):
        vault = ObsidianVault(tmp_path, {"_default": "Inbox"})
        path = tmp_path / "Inbox" / "test.md"

        fm = {"title": "Test Note", "source": "remarkable"}
        vault.write_note(path, fm, "# Content\n\nHello world.")

        assert path.exists()
        result = vault.read_note(path)
        assert result is not None

        read_fm, read_content = result
        assert read_fm["title"] == "Test Note"
        assert "Hello world" in read_content

    def test_write_preserves_manual_fields(self, tmp_path):
        vault = ObsidianVault(tmp_path, {})
        path = tmp_path / "note.md"

        # First write with manual field
        vault.write_note(path, {"title": "V1", "my_custom": "keep me"}, "Content v1")

        # Second write without the manual field
        vault.write_note(path, {"title": "V2", "source": "remarkable"}, "Content v2")

        fm, _ = vault.read_note(path)
        assert fm["title"] == "V2"
        assert fm["my_custom"] == "keep me"  # preserved
        assert fm["source"] == "remarkable"

    def test_write_creates_directories(self, tmp_path):
        vault = ObsidianVault(tmp_path, {})
        path = tmp_path / "deep" / "nested" / "note.md"
        vault.write_note(path, {"title": "Deep"}, "Content")
        assert path.exists()

    def test_read_nonexistent(self, tmp_path):
        vault = ObsidianVault(tmp_path, {})
        assert vault.read_note(tmp_path / "nope.md") is None

    def test_write_action_items(self, tmp_path):
        vault = ObsidianVault(tmp_path, {"_default": "Inbox"})
        source_path = tmp_path / "Inbox" / "Meeting Notes.md"

        actions = [
            ActionItem(task="Send report", type="task", priority="high", assignee="Alice"),
            ActionItem(task="What's the deadline?", type="question"),
            ActionItem(task="Review PR", type="task", deadline="2026-04-15"),
        ]

        result = vault.write_action_items(actions, "Meeting Notes", source_path)

        assert result.exists()
        content = result.read_text()
        assert "Send report" in content
        assert "@Alice" in content
        assert "deadline" in content.lower()
        assert "[?]" in content  # question marker
        assert "#priority-high" in content

    def test_list_notes_by_source(self, tmp_path):
        vault = ObsidianVault(tmp_path, {})

        # Write a remarkable-sourced note
        vault.write_note(
            tmp_path / "note1.md",
            {"title": "RM Note", "source": "remarkable"},
            "Content",
        )
        # Write a non-remarkable note
        vault.write_note(
            tmp_path / "note2.md",
            {"title": "Manual Note", "source": "manual"},
            "Content",
        )

        results = vault.list_notes_by_source("remarkable")
        assert len(results) == 1
        assert results[0].name == "note1.md"

    def test_ensure_structure(self, tmp_path):
        vault = ObsidianVault(tmp_path, {
            "Work": "Notes/Work",
            "Personal": "Notes/Personal",
            "_default": "Inbox",
        })
        vault.ensure_structure()

        assert (tmp_path / "Notes" / "Work").is_dir()
        assert (tmp_path / "Notes" / "Personal").is_dir()
        assert (tmp_path / "Inbox").is_dir()
        assert (tmp_path / "Actions").is_dir()
        assert (tmp_path / "Templates").is_dir()


# =====================
# generate_frontmatter
# =====================

class TestGenerateFrontmatter:
    def test_basic_frontmatter(self):
        notebook = Notebook(
            id="abc-123",
            name="Weekly Standup",
            folder="Work",
            modified="2026-04-13",
            pages=[
                PageContent(page_id="p1", text_blocks=[TextBlock(text="text")]),
                PageContent(page_id="p2"),
            ],
        )
        ocr_results = [
            PageText(page_id="p1", text="text", confidence=0.95, engine_used="crdt"),
            PageText(page_id="p2", text="", confidence=0.0, engine_used="none"),
        ]
        actions = [ActionItem(task="Do thing")]
        tags = ["meeting", "standup"]

        fm = generate_frontmatter(notebook, ocr_results, actions, tags)

        assert fm["title"] == "Weekly Standup"
        assert fm["source"] == "remarkable"
        assert fm["remarkable_id"] == "abc-123"
        assert fm["pages"] == 2
        assert fm["action_items"] == 1
        assert fm["tags"] == ["meeting", "standup"]
        assert fm["status"] == "transcribed"
        assert fm["ocr_confidence"] == 0.95  # only p1 has confidence > 0

    def test_multiple_engines(self):
        notebook = Notebook(id="x", name="N", folder="", modified="", pages=[])
        ocr_results = [
            PageText(page_id="p1", text="a", confidence=0.9, engine_used="crdt"),
            PageText(page_id="p2", text="b", confidence=0.8, engine_used="google_vision"),
        ]

        fm = generate_frontmatter(notebook, ocr_results, [], [])
        assert isinstance(fm["ocr_engine"], list)
        assert "crdt" in fm["ocr_engine"]
        assert "google_vision" in fm["ocr_engine"]


class TestUpdateFrontmatter:
    def test_merges_updates(self):
        existing = {"title": "Old", "custom_field": "keep"}
        updates = {"title": "New", "source": "remarkable"}

        merged = update_frontmatter(existing, updates)

        assert merged["title"] == "New"
        assert merged["custom_field"] == "keep"
        assert merged["source"] == "remarkable"
        assert "last_synced" in merged


# =====================
# format_note_content
# =====================

class TestFormatNoteContent:
    def test_basic_content(self):
        result = format_note_content("# Title\n\nSome content")
        assert "# Title" in result
        assert "Some content" in result

    def test_with_summary(self):
        summary = NoteSummary(
            one_line="Quick summary here",
            key_points=["Point A", "Point B"],
            topics=["topic"],
        )
        result = format_note_content("Content", summary=summary)
        assert "Quick summary here" in result
        assert "Point A" in result

    def test_with_actions(self):
        actions = [
            ActionItem(task="Do thing", type="task", assignee="Bob", deadline="Friday"),
            ActionItem(task="Ask about X", type="question"),
        ]
        result = format_note_content("Content", actions=actions)
        assert "## Action Items" in result
        assert "Do thing" in result
        assert "@Bob" in result
        assert "[?]" in result

    def test_empty_content(self):
        result = format_note_content("")
        assert result.strip() == ""


class TestFormatActionIndex:
    def test_builds_index(self):
        actions_by_note = {
            "Meeting A": [
                ActionItem(task="Task 1", priority="high"),
                ActionItem(task="Task 2"),
            ],
            "Meeting B": [
                ActionItem(task="Ask question", type="question"),
            ],
        }

        result = format_action_index(actions_by_note)

        assert "# Action Items" in result
        assert "[[Meeting A]]" in result
        assert "[[Meeting B]]" in result
        assert "Task 1" in result
        assert "#priority-high" in result
        assert "3 open items" in result


# =====================
# GitSync
# =====================

class TestGitSync:
    def test_init_with_valid_repo(self, tmp_path):
        # Init a real git repo
        from git import Repo
        Repo.init(tmp_path)

        gs = GitSync(tmp_path)
        assert gs.is_git_repo()

    def test_init_with_non_repo(self, tmp_path):
        gs = GitSync(tmp_path)
        assert not gs.is_git_repo()

    def test_has_changes(self, tmp_path):
        from git import Repo
        Repo.init(tmp_path)

        gs = GitSync(tmp_path)
        assert not gs.has_changes()

        # Create a file
        (tmp_path / "test.md").write_text("content")
        assert gs.has_changes()

    def test_commit(self, tmp_path):
        from git import Repo
        repo = Repo.init(tmp_path)
        # Need at least one commit for HEAD to be valid
        (tmp_path / "init.md").write_text("init")
        repo.index.add(["init.md"])
        repo.index.commit("initial")

        # Now add a new file
        (tmp_path / "note.md").write_text("new note")

        gs = GitSync(tmp_path)
        commit_hash = gs.commit(1, "sync: 1 note")

        assert commit_hash is not None
        assert len(commit_hash) == 8
        assert not gs.has_changes()

    def test_commit_nothing(self, tmp_path):
        from git import Repo
        repo = Repo.init(tmp_path)
        (tmp_path / "init.md").write_text("init")
        repo.index.add(["init.md"])
        repo.index.commit("initial")

        gs = GitSync(tmp_path)
        assert gs.commit(0) is None

    def test_status(self, tmp_path):
        from git import Repo
        repo = Repo.init(tmp_path)
        (tmp_path / "init.md").write_text("init")
        repo.index.add(["init.md"])
        repo.index.commit("initial")

        gs = GitSync(tmp_path)
        status = gs.status()

        assert status["branch"] == "master" or status["branch"] == "main"
        assert status["dirty"] is False
        assert status["last_commit"] is not None
