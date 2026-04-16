"""Tests for note processing modules (structurer, actions, tagger, summarizer)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.llm.client import LLMClient, LLMMessage, LLMResponse
from src.processing.actions import (
    ActionExtractor,
    ActionItem,
    _extract_by_pattern,
    _merge_actions,
    _parse_action_response,
)
from src.processing.structurer import NoteStructurer, StructuredNote, _extract_title
from src.processing.summarizer import NoteSummarizer, NoteSummary, _fallback_summary
from src.processing.tagger import NoteTagger, _extract_keyword_tags, _parse_tag_response


class _StubLLM(LLMClient):
    """Test helper — records complete() calls and returns canned text."""

    provider = "stub"

    def __init__(self, text: str = ""):
        self._text = text
        self.calls: list = []

    async def complete(self, system, messages, model, max_tokens=4096):
        self.calls.append((system, messages, model, max_tokens))
        return LLMResponse(
            text=self._text, input_tokens=1, output_tokens=1,
            provider=self.provider, model=model,
        )

    async def complete_vision(self, system, image, prompt, model, max_tokens=2048):
        raise NotImplementedError


# -- Helper to mock Anthropic client (used by actions, tagger, summarizer tests) --

def mock_anthropic_response(text: str) -> AsyncMock:
    """Create a mock Anthropic client that returns the given text."""
    client = AsyncMock()
    response = MagicMock()
    response.content = [MagicMock(text=text)]
    client.messages.create = AsyncMock(return_value=response)
    return client


# =====================
# NoteStructurer
# =====================

class TestNoteStructurer:
    @pytest.mark.asyncio
    async def test_structure_returns_result(self):
        llm = _StubLLM(text="# Meeting Notes\n\nDiscussed project timeline.")
        structurer = NoteStructurer(llm=llm, model="claude-sonnet-4-20250514")

        result = await structurer.structure(
            "meeting notes discussed project timeline",
            "Weekly Standup",
        )

        assert isinstance(result, StructuredNote)
        assert result.title == "Meeting Notes"
        assert "project timeline" in result.content_md

    @pytest.mark.asyncio
    async def test_structure_empty_text(self):
        llm = _StubLLM(text="")
        structurer = NoteStructurer(llm=llm, model="claude-sonnet-4-20250514")

        result = await structurer.structure("", "Empty Notebook")

        assert result.title == "Empty Notebook"
        assert result.content_md == ""

    @pytest.mark.asyncio
    async def test_structure_incremental_empty_new(self):
        llm = _StubLLM(text="")
        structurer = NoteStructurer(llm=llm, model="claude-sonnet-4-20250514")

        result = await structurer.structure_incremental("Existing content", "")
        assert result == "Existing content"

    @pytest.mark.asyncio
    async def test_structure_uses_correct_model(self):
        llm = _StubLLM(text="# Title\n\nContent")
        structurer = NoteStructurer(llm=llm, model="claude-sonnet-4-20250514")

        await structurer.structure("some text", "Test")

        assert llm.calls
        # third element of the recorded tuple is model
        assert llm.calls[0][2] == "claude-sonnet-4-20250514"

    @pytest.mark.asyncio
    async def test_structurer_uses_llm_client(self):
        llm = _StubLLM(text="# Title\n\nBody")
        structurer = NoteStructurer(llm=llm, model="llama3.1")

        out = await structurer.structure("handwritten text here", "My Notebook")

        assert "Title" in out.content_md or "Body" in out.content_md
        assert llm.calls
        assert llm.calls[0][2] == "llama3.1"  # model forwarded correctly


class TestExtractTitle:
    def test_h1_heading(self):
        assert _extract_title("# My Title\n\nContent") == "My Title"

    def test_no_heading(self):
        assert _extract_title("Just text without headings") is None

    def test_h2_not_matched(self):
        assert _extract_title("## Sub heading\nContent") is None

    def test_empty(self):
        assert _extract_title("") is None


# =====================
# ActionExtractor
# =====================

class TestExtractByPattern:
    def test_todo_pattern(self):
        actions = _extract_by_pattern("TODO: send the report")
        assert len(actions) == 1
        assert actions[0].task == "send the report"
        assert actions[0].type == "task"
        assert actions[0].priority == "high"

    def test_action_pattern(self):
        actions = _extract_by_pattern("ACTION: review PR #42")
        assert len(actions) == 1
        assert actions[0].task == "review PR #42"

    def test_follow_up_pattern(self):
        actions = _extract_by_pattern("FOLLOW UP: check with design team")
        assert len(actions) == 1
        assert actions[0].task == "check with design team"

    def test_question_pattern(self):
        actions = _extract_by_pattern("Q: What is the deployment schedule?")
        assert len(actions) == 1
        assert actions[0].type == "question"

    def test_checkbox_pattern(self):
        actions = _extract_by_pattern("- [ ] Write tests for auth module")
        assert len(actions) == 1
        assert actions[0].task == "Write tests for auth module"

    def test_priority_marker(self):
        actions = _extract_by_pattern("! Fix the production bug immediately")
        assert len(actions) == 1
        assert actions[0].priority == "high"

    def test_question_mark_line(self):
        actions = _extract_by_pattern("Should we migrate to the new framework?")
        assert len(actions) == 1
        assert actions[0].type == "question"

    def test_short_question_ignored(self):
        actions = _extract_by_pattern("Why?")
        assert len(actions) == 0  # too short

    def test_multiple_patterns(self):
        text = "TODO: fix login bug\nQ: who owns the API?\n- [ ] update docs"
        actions = _extract_by_pattern(text)
        assert len(actions) == 3

    def test_no_patterns(self):
        actions = _extract_by_pattern("Just regular notes about the meeting.")
        assert len(actions) == 0


class TestParseActionResponse:
    def test_valid_json(self):
        raw = json.dumps([
            {"task": "Send report", "type": "task", "priority": "high"},
            {"task": "Check budget", "type": "followup", "assignee": "Alice"},
        ])
        actions = _parse_action_response(raw)
        assert len(actions) == 2
        assert actions[0].task == "Send report"
        assert actions[1].assignee == "Alice"

    def test_code_block_wrapped(self):
        raw = "```json\n" + json.dumps([{"task": "Do thing"}]) + "\n```"
        actions = _parse_action_response(raw)
        assert len(actions) == 1

    def test_invalid_json(self):
        actions = _parse_action_response("not valid json at all")
        assert len(actions) == 0

    def test_empty_array(self):
        actions = _parse_action_response("[]")
        assert len(actions) == 0

    def test_missing_task_field(self):
        raw = json.dumps([{"type": "task", "priority": "low"}])
        actions = _parse_action_response(raw)
        assert len(actions) == 0  # no "task" field


class TestMergeActions:
    def test_deduplicates_by_task(self):
        api = [ActionItem(task="Send report", type="task")]
        pattern = [ActionItem(task="send report", type="task")]  # same, different case
        color = []

        merged = _merge_actions(api, pattern, color)
        assert len(merged) == 1

    def test_color_always_added(self):
        api = [ActionItem(task="Send report")]
        pattern = []
        color = [ActionItem(task="[red annotation on page abc]", color="red")]

        merged = _merge_actions(api, pattern, color)
        assert len(merged) == 2

    def test_unique_pattern_added(self):
        api = [ActionItem(task="Task A")]
        pattern = [ActionItem(task="Task B")]
        color = []

        merged = _merge_actions(api, pattern, color)
        assert len(merged) == 2


class TestActionExtractor:
    @pytest.mark.asyncio
    async def test_extract_combines_sources(self):
        api_response = json.dumps([
            {"task": "Review architecture", "type": "task", "priority": "high"},
        ])
        client = mock_anthropic_response(api_response)
        extractor = ActionExtractor(client, "claude-sonnet-4-20250514")

        actions = await extractor.extract("TODO: fix the tests\nReview architecture for v2")

        # Should have both: pattern-matched TODO + API result
        tasks = [a.task for a in actions]
        assert "fix the tests" in tasks
        assert "Review architecture" in tasks

    @pytest.mark.asyncio
    async def test_extract_empty_text(self):
        client = mock_anthropic_response("[]")
        extractor = ActionExtractor(client, "claude-sonnet-4-20250514")

        actions = await extractor.extract("")
        assert len(actions) == 0


# =====================
# NoteTagger
# =====================

class TestExtractKeywordTags:
    def test_meeting_detected(self):
        tags = _extract_keyword_tags("Standup meeting with the team today")
        assert "meeting" in tags

    def test_planning_detected(self):
        tags = _extract_keyword_tags("Sprint planning: timeline and milestones")
        assert "planning" in tags

    def test_technical_detected(self):
        tags = _extract_keyword_tags("Debug the API server deployment issue")
        assert "technical" in tags

    def test_no_matches(self):
        tags = _extract_keyword_tags("Quick random thoughts about lunch")
        assert len(tags) == 0

    def test_multiple_tags(self):
        tags = _extract_keyword_tags(
            "Meeting about the API deployment timeline for next sprint"
        )
        assert "meeting" in tags
        assert "technical" in tags
        assert "planning" in tags


class TestParseTagResponse:
    def test_valid_json_array(self):
        tags = _parse_tag_response('["tag-one", "tag-two", "tag-three"]')
        assert tags == ["tag-one", "tag-two", "tag-three"]

    def test_code_block(self):
        tags = _parse_tag_response('```json\n["a", "b"]\n```')
        assert tags == ["a", "b"]

    def test_invalid_json(self):
        tags = _parse_tag_response("not json")
        assert tags == []


class TestNoteTagger:
    @pytest.mark.asyncio
    async def test_tag_merges_sources(self):
        client = mock_anthropic_response('["backend", "performance"]')
        tagger = NoteTagger(client, "claude-sonnet-4-20250514")

        tags = await tagger.tag("Meeting about the API performance issues")

        assert "meeting" in tags  # keyword
        assert "technical" in tags  # keyword
        assert "backend" in tags  # API
        assert "performance" in tags  # API

    @pytest.mark.asyncio
    async def test_tag_empty_text(self):
        client = mock_anthropic_response("[]")
        tagger = NoteTagger(client, "claude-sonnet-4-20250514")

        tags = await tagger.tag("")
        assert tags == []

    @pytest.mark.asyncio
    async def test_tag_deduplicates(self):
        client = mock_anthropic_response('["meeting", "planning"]')
        tagger = NoteTagger(client, "claude-sonnet-4-20250514")

        tags = await tagger.tag("Meeting about sprint planning and timeline")
        # "meeting" and "planning" come from both keyword and API
        assert tags.count("meeting") == 1
        assert tags.count("planning") == 1

    @pytest.mark.asyncio
    async def test_hierarchical_uses_different_prompt(self):
        from src.processing.tagger import (
            HIERARCHICAL_TAGGING_PROMPT,
            TAGGING_PROMPT,
        )

        client = mock_anthropic_response(
            '["project/remark/search", "technical/python/fastapi"]'
        )
        tagger = NoteTagger(
            client, "claude-sonnet-4-20250514", hierarchical=True,
        )
        tags = await tagger.tag("Notes on the FastAPI search layer")
        call = client.messages.create.call_args
        assert call.kwargs["system"] == HIERARCHICAL_TAGGING_PROMPT
        assert call.kwargs["system"] != TAGGING_PROMPT
        # Hierarchical tags must make it through the merge unchanged
        assert "project/remark/search" in tags
        assert "technical/python/fastapi" in tags

    @pytest.mark.asyncio
    async def test_flat_mode_keeps_flat_prompt(self):
        from src.processing.tagger import TAGGING_PROMPT

        client = mock_anthropic_response('["flat-tag"]')
        tagger = NoteTagger(client, "claude-sonnet-4-20250514")
        await tagger.tag("some text")
        assert client.messages.create.call_args.kwargs["system"] == TAGGING_PROMPT


# =====================
# NoteSummarizer
# =====================

class TestNoteSummarizer:
    @pytest.mark.asyncio
    async def test_summarize_returns_result(self):
        response = json.dumps({
            "one_line": "Team discussed Q2 OKRs",
            "key_points": ["Revenue target set", "Hiring plan approved"],
            "topics": ["okrs", "hiring"],
        })
        client = mock_anthropic_response(response)
        summarizer = NoteSummarizer(client, "claude-sonnet-4-20250514")

        result = await summarizer.summarize("Long meeting notes...", "Q2 Planning")

        assert isinstance(result, NoteSummary)
        assert result.one_line == "Team discussed Q2 OKRs"
        assert len(result.key_points) == 2
        assert len(result.topics) == 2

    @pytest.mark.asyncio
    async def test_summarize_empty_text(self):
        client = mock_anthropic_response("")
        summarizer = NoteSummarizer(client, "claude-sonnet-4-20250514")

        result = await summarizer.summarize("", "Empty Note")
        assert "Empty" in result.one_line


class TestFallbackSummary:
    def test_extracts_from_content(self):
        text = "# Project Kickoff\n\n- Define scope\n- Set timeline\n- Assign roles"
        summary = _fallback_summary(text, "Kickoff")

        assert summary.one_line == "Project Kickoff"
        assert len(summary.key_points) >= 2

    def test_empty_text(self):
        summary = _fallback_summary("", "My Notebook")
        assert "My Notebook" in summary.one_line

    def test_no_bullets(self):
        summary = _fallback_summary("Just some plain text here.", "Test")
        assert summary.one_line == "Just some plain text here."
        assert summary.key_points == []
