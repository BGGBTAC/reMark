"""AnthropicClient adapter — verifies it routes to anthropic.AsyncAnthropic."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.llm.anthropic import AnthropicClient
from src.llm.client import LLMMessage


@pytest.fixture
def fake_sdk():
    sdk = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text="response text")]
    msg.usage = MagicMock(input_tokens=12, output_tokens=7)
    sdk.messages = MagicMock()
    sdk.messages.create = AsyncMock(return_value=msg)
    return sdk


async def test_anthropic_complete_text(fake_sdk):
    client = AnthropicClient(sdk=fake_sdk)
    resp = await client.complete(
        system="sys",
        messages=[LLMMessage(role="user", content="hi")],
        model="claude-x",
    )
    assert resp.text == "response text"
    assert resp.input_tokens == 12
    assert resp.output_tokens == 7
    assert resp.provider == "anthropic"
    assert resp.model == "claude-x"
    fake_sdk.messages.create.assert_awaited_once()
    call = fake_sdk.messages.create.await_args
    assert call.kwargs["system"] == "sys"
    assert call.kwargs["model"] == "claude-x"
    assert call.kwargs["messages"] == [{"role": "user", "content": "hi"}]


async def test_anthropic_vision_includes_image(fake_sdk):
    client = AnthropicClient(sdk=fake_sdk)
    await client.complete_vision(
        system="sys", image=b"\x89PNG\r\n", prompt="describe", model="claude-vision",
    )
    call = fake_sdk.messages.create.await_args
    msg_content = call.kwargs["messages"][0]["content"]
    kinds = {part["type"] for part in msg_content}
    assert kinds == {"image", "text"}
