"""Tests for OneNote integration."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import OneNoteConfig
from src.integrations.microsoft.onenote import (
    OneNoteClient,
    OneNotePage,
    _inline_format,
    _markdown_to_onenote_html,
)

# =====================
# Markdown → HTML
# =====================

class TestMarkdownConversion:
    def test_basic_heading(self):
        html = _markdown_to_onenote_html("t", "# Heading\n\nBody", [])
        assert "<h1>Heading</h1>" in html
        assert "<p>Body</p>" in html

    def test_multiple_heading_levels(self):
        html = _markdown_to_onenote_html("t", "## Sub\n### Deep", [])
        assert "<h2>Sub</h2>" in html
        assert "<h3>Deep</h3>" in html

    def test_bullets(self):
        html = _markdown_to_onenote_html("t", "- first\n- second", [])
        assert "<ul>" in html
        assert "<li>first</li>" in html
        assert "<li>second</li>" in html
        assert "</ul>" in html

    def test_checkbox_unchecked(self):
        html = _markdown_to_onenote_html("t", "- [ ] task", [])
        assert 'type="checkbox"' in html
        assert "checked" not in html.split('type="checkbox"')[1].split(">")[0]

    def test_checkbox_checked(self):
        html = _markdown_to_onenote_html("t", "- [x] done", [])
        assert "checked" in html

    def test_bold(self):
        html = _markdown_to_onenote_html("t", "**important**", [])
        assert "<strong>important</strong>" in html

    def test_italic(self):
        html = _markdown_to_onenote_html("t", "*emphasis*", [])
        assert "<em>emphasis</em>" in html

    def test_wiki_link(self):
        html = _markdown_to_onenote_html("t", "See [[Other Note]]", [])
        assert "wiki-link" in html
        assert "Other Note" in html

    def test_tags_rendered(self):
        html = _markdown_to_onenote_html("t", "Body", ["alpha", "beta"])
        assert "alpha" in html
        assert "beta" in html

    def test_html_escaping(self):
        html = _markdown_to_onenote_html("t", "plain <script>", [])
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestInlineFormat:
    def test_escape_only(self):
        assert _inline_format("plain text") == "plain text"

    def test_combined(self):
        out = _inline_format("**bold** and *italic*")
        assert "<strong>bold</strong>" in out
        assert "<em>italic</em>" in out


# =====================
# OneNoteClient
# =====================

class TestOneNoteClient:
    @pytest.mark.asyncio
    async def test_get_or_create_notebook_existing(self):
        graph = MagicMock()
        graph.get = AsyncMock(return_value={
            "value": [
                {"id": "nb-1", "displayName": "reMark"},
                {"id": "nb-2", "displayName": "Other"},
            ],
        })
        config = OneNoteConfig(enabled=True, notebook_name="reMark")
        client = OneNoteClient(graph, config)

        nb_id = await client.get_or_create_notebook()
        assert nb_id == "nb-1"
        graph.post.assert_not_called() if hasattr(graph, "post") else None

    @pytest.mark.asyncio
    async def test_get_or_create_notebook_creates(self):
        graph = MagicMock()
        graph.get = AsyncMock(return_value={"value": []})
        graph.post = AsyncMock(return_value={"id": "new-nb"})

        config = OneNoteConfig(enabled=True, notebook_name="reMark")
        client = OneNoteClient(graph, config)

        nb_id = await client.get_or_create_notebook()
        assert nb_id == "new-nb"
        graph.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_or_create_section_existing(self):
        graph = MagicMock()
        graph.get = AsyncMock(side_effect=[
            {"value": [{"id": "nb-1", "displayName": "reMark"}]},
            {"value": [{"id": "sec-1", "displayName": "Inbox"}]},
        ])

        config = OneNoteConfig(enabled=True)
        client = OneNoteClient(graph, config)

        sec_id = await client.get_or_create_section("Inbox")
        assert sec_id == "sec-1"

    @pytest.mark.asyncio
    async def test_get_or_create_section_no_auto(self):
        from src.integrations.microsoft.graph import GraphError

        graph = MagicMock()
        graph.get = AsyncMock(side_effect=[
            {"value": [{"id": "nb-1", "displayName": "reMark"}]},
            {"value": []},
        ])

        config = OneNoteConfig(enabled=True, create_missing_sections=False)
        client = OneNoteClient(graph, config)

        with pytest.raises(GraphError, match="not found"):
            await client.get_or_create_section("Missing")

    @pytest.mark.asyncio
    async def test_section_cache(self):
        graph = MagicMock()
        graph.get = AsyncMock(side_effect=[
            {"value": [{"id": "nb-1", "displayName": "reMark"}]},
            {"value": [{"id": "sec-1", "displayName": "Inbox"}]},
        ])

        config = OneNoteConfig(enabled=True)
        client = OneNoteClient(graph, config)

        a = await client.get_or_create_section("Inbox")
        b = await client.get_or_create_section("Inbox")
        assert a == b
        # graph.get called only for notebook + one section fetch
        assert graph.get.call_count == 2

    def test_resolve_section_via_folder_map(self):
        config = OneNoteConfig(
            enabled=True,
            folder_map={"Work": "Office", "_default": "Inbox"},
        )
        graph = MagicMock()
        client = OneNoteClient(graph, config)
        assert client._resolve_section("Work") == "Office"
        assert client._resolve_section("Unknown") == "Inbox"

    @pytest.mark.asyncio
    async def test_write_page_posts_html(self):
        # Mock graph client with async context via auth token call
        graph = MagicMock()
        graph.get = AsyncMock(side_effect=[
            {"value": [{"id": "nb-1", "displayName": "reMark"}]},
            {"value": [{"id": "sec-1", "displayName": "Inbox"}]},
        ])
        auth = MagicMock()
        auth.get_access_token = AsyncMock(return_value="token-abc")
        graph._auth = auth

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": "page-xyz", "title": "My Page"}

        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        graph.client = mock_http

        config = OneNoteConfig(enabled=True, default_section="Inbox")
        client = OneNoteClient(graph, config)

        page = await client.write_page("My Page", "# Hello\n\nBody", folder="")
        assert page.id == "page-xyz"
        assert page.section_id == "sec-1"

        # Body should contain rendered HTML
        call_kwargs = mock_http.post.call_args
        body = call_kwargs.kwargs["content"].decode()
        assert "<h1>Hello</h1>" in body
        assert "<p>Body</p>" in body

    @pytest.mark.asyncio
    async def test_write_page_error_raises(self):
        from src.integrations.microsoft.graph import GraphError

        graph = MagicMock()
        graph.get = AsyncMock(side_effect=[
            {"value": [{"id": "nb-1", "displayName": "reMark"}]},
            {"value": [{"id": "sec-1", "displayName": "Inbox"}]},
        ])
        auth = MagicMock()
        auth.get_access_token = AsyncMock(return_value="t")
        graph._auth = auth

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "bad request"

        graph.client = MagicMock()
        graph.client.post = AsyncMock(return_value=mock_response)

        config = OneNoteConfig(enabled=True)
        client = OneNoteClient(graph, config)

        with pytest.raises(GraphError, match="400"):
            await client.write_page("t", "body")


class TestOneNotePage:
    def test_dataclass(self):
        p = OneNotePage(id="x", title="t", section_id="s")
        assert p.id == "x"
        assert p.title == "t"
