"""Tests for the Notion integration (markdown→blocks + service)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.config import NotionConfig
from src.integrations.notion.service import NotionService, markdown_to_blocks


class TestMarkdownToBlocks:
    def test_empty_content_yields_placeholder(self):
        blocks = markdown_to_blocks("")
        assert len(blocks) == 1
        assert blocks[0]["type"] == "paragraph"

    def test_headings_map_to_levels(self):
        blocks = markdown_to_blocks("# H1\n## H2\n### H3")
        types = [b["type"] for b in blocks]
        assert types == ["heading_1", "heading_2", "heading_3"]

    def test_paragraphs_are_joined_between_blank_lines(self):
        content = "First line\nof paragraph.\n\nSecond paragraph."
        blocks = markdown_to_blocks(content)
        paragraphs = [b for b in blocks if b["type"] == "paragraph"]
        assert len(paragraphs) == 2
        assert "First line\nof paragraph." in paragraphs[0]["paragraph"]["rich_text"][0]["text"]["content"]

    def test_bulleted_and_numbered_lists(self):
        content = "- one\n- two\n1. first\n2. second"
        blocks = markdown_to_blocks(content)
        types = [b["type"] for b in blocks]
        assert types == [
            "bulleted_list_item", "bulleted_list_item",
            "numbered_list_item", "numbered_list_item",
        ]

    def test_todo_detection(self):
        content = "- [ ] open task\n- [x] done task"
        blocks = markdown_to_blocks(content)
        assert blocks[0]["type"] == "to_do"
        assert blocks[0]["to_do"]["checked"] is False
        assert blocks[1]["type"] == "to_do"
        assert blocks[1]["to_do"]["checked"] is True

    def test_todo_takes_precedence_over_plain_bullet(self):
        # Both patterns start with "- "; the to_do matcher must win
        blocks = markdown_to_blocks("- [ ] task\n- plain")
        assert blocks[0]["type"] == "to_do"
        assert blocks[1]["type"] == "bulleted_list_item"

    def test_long_content_splits_rich_text_chunks(self):
        long_line = "a" * 4500
        blocks = markdown_to_blocks(long_line)
        chunks = blocks[0]["paragraph"]["rich_text"]
        # 4500 chars → 3 chunks of 2000/2000/500
        assert len(chunks) == 3
        assert all(len(c["text"]["content"]) <= 2000 for c in chunks)


class TestNotionService:
    @pytest.fixture
    def service(self, monkeypatch):
        cfg = NotionConfig(
            enabled=True,
            integration_token_env="NOTION_TEST_TOKEN",
            vault_mirror_page_id="parent-page-123",
        )
        monkeypatch.setenv("NOTION_TEST_TOKEN", "secret_abc")
        return NotionService(cfg)

    def test_enabled_requires_token(self, monkeypatch):
        cfg = NotionConfig(enabled=True, integration_token_env="UNSET_VAR")
        monkeypatch.delenv("UNSET_VAR", raising=False)
        service = NotionService(cfg)
        assert service.enabled is False

    def test_enabled_false_when_disabled(self, monkeypatch):
        cfg = NotionConfig(enabled=False)
        monkeypatch.setenv("NOTION_TOKEN", "secret")
        service = NotionService(cfg)
        assert service.enabled is False

    @pytest.mark.asyncio
    async def test_write_note_skips_without_parent(self, monkeypatch):
        cfg = NotionConfig(
            enabled=True,
            integration_token_env="NOTION_TEST_TOKEN",
            vault_mirror_page_id="",
        )
        monkeypatch.setenv("NOTION_TEST_TOKEN", "secret_abc")
        service = NotionService(cfg)
        result = await service.write_note("Title", "body", tags=[])
        assert result is None

    @pytest.mark.asyncio
    async def test_write_note_prepends_tags(self, service, monkeypatch):
        captured: dict = {}

        class _FakeClient:
            async def create_page(self, parent_page_id, title, blocks):
                captured["parent"] = parent_page_id
                captured["title"] = title
                captured["blocks"] = blocks
                return "new-page-id"

        service._client = _FakeClient()
        result = await service.write_note(
            "My note", "Body text.", tags=["project/foo", "meeting"],
        )
        assert result is not None
        assert result.page_id == "new-page-id"
        assert captured["parent"] == "parent-page-123"
        # First block is the Tags: paragraph
        first_text = captured["blocks"][0]["paragraph"]["rich_text"][0]["text"]["content"]
        assert "Tags:" in first_text
        assert "project/foo" in first_text

    @pytest.mark.asyncio
    async def test_write_note_swallows_notion_error(self, service, monkeypatch):
        from src.integrations.notion.client import NotionError

        class _BrokenClient:
            async def create_page(self, *args, **kwargs):
                raise NotionError("HTTP 403: insufficient permissions")

        service._client = _BrokenClient()
        # Should not raise — logs + returns None so the sync cycle
        # keeps going for every other integration.
        result = await service.write_note("Title", "body", tags=[])
        assert result is None
