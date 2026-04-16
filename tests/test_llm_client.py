"""Contract tests for the LLMClient interface."""

from __future__ import annotations

from src.llm.client import LLMClient, LLMMessage, LLMResponse


class _Stub(LLMClient):
    provider = "stub"

    async def complete(self, system, messages, model, max_tokens=4096):
        return LLMResponse(
            text="hi",
            input_tokens=1,
            output_tokens=1,
            provider=self.provider,
            model=model,
        )

    async def complete_vision(self, system, image, prompt, model, max_tokens=2048):
        return LLMResponse(
            text="seen",
            input_tokens=1,
            output_tokens=1,
            provider=self.provider,
            model=model,
        )


async def test_llm_client_text_roundtrip():
    client = _Stub()
    resp = await client.complete(
        system="sys",
        messages=[LLMMessage(role="user", content="hello")],
        model="m",
    )
    assert resp.text == "hi"
    assert resp.provider == "stub"
    assert resp.model == "m"


async def test_llm_client_vision_roundtrip():
    client = _Stub()
    resp = await client.complete_vision(
        system="sys",
        image=b"\x00\x01",
        prompt="describe",
        model="m-vision",
    )
    assert resp.text == "seen"
    assert resp.model == "m-vision"
