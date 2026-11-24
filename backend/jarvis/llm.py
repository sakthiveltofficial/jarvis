"""Pluggable LLM providers.

Two adapters cover everything:
  * AnthropicProvider  - Claude via the official Anthropic SDK
  * OpenAICompatProvider - OpenAI, DeepSeek, OpenRouter, Groq, Ollama, LM Studio, vLLM,
                           or anything else speaking the OpenAI chat-completions API.

Each adapter owns its own history format, so a conversation is reset when you switch provider.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import config
from .tools import ToolOutput, ToolSpec

# Claude models that support server-side refusal fallbacks
_FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Step:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop: str = ""


class Provider:
    vision = False

    def __init__(self, settings: dict):
        self.settings = settings
        self.history: list = []

    def reset(self):
        self.history = []

    def add_user(self, text: str): ...
    def step(self, system: str, tools: list[ToolSpec]) -> Step: ...
    def add_tool_results(self, results: list[tuple[ToolCall, ToolOutput]]): ...


# --------------------------------------------------------------------------- Anthropic
class AnthropicProvider(Provider):
    def __init__(self, settings: dict):
        super().__init__(settings)
        import anthropic
        llm = settings["llm"]
        kw = {"api_key": config.api_key(settings, "anthropic") or None}
        if llm.get("base_url"):
            kw["base_url"] = llm["base_url"]
        self.client = anthropic.Anthropic(**kw)
        self.model = llm.get("model") or "claude-opus-5-5"
        self.vision = bool(llm.get("vision", True))

    def add_user(self, text: str):
        self.history.append({"role": "user", "content": text})

    def step(self, system: str, tools: list[ToolSpec]) -> Step:
        llm = self.settings["llm"]
        params = dict(
            model=self.model,
            max_tokens=int(llm.get("max_tokens") or 8000),
            system=system,
            messages=self.history,
            tools=[{"name": t.name, "description": t.description, "input_schema": t.schema()} for t in tools],
        )
        if llm.get("effort"):
            params["output_config"] = {"effort": llm["effort"]}
        if self.model in _FALLBACK_MODELS:
            # Route refusals to a fallback model instead of failing the turn.
            resp = self.client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
        else:
            resp = self.client.messages.create(**params)

        # Keep the assistant content exactly as returned (thinking blocks included).
        self.history.append({"role": "assistant", "content": resp.content})
        step = Step(stop=resp.stop_reason or "")
        if resp.stop_reason == "refusal":
            step.text = "I'm afraid I can't help with that one."
            return step
        for b in resp.content:
            if b.type == "text":
                step.text += b.text
            elif b.type == "tool_use":
                step.tool_calls.append(ToolCall(b.id, b.name, dict(b.input or {})))
        return step

    def add_tool_results(self, results):
        blocks = []
        for call, out in results:
            content = [{"type": "text", "text": out.text or "(no output)"}]
            if out.image_b64 and self.vision:
                content.append({"type": "image",
                                "source": {"type": "base64", "media_type": "image/png", "data": out.image_b64}})
            blocks.append({"type": "tool_result", "tool_use_id": call.id, "content": content,
                           "is_error": out.is_error})
        self.history.append({"role": "user", "content": blocks})


# --------------------------------------------------------------------------- OpenAI-compatible
class OpenAICompatProvider(Provider):
    KEEP_IMAGES = 2   # older screenshots are replaced by a note to save tokens

    def __init__(self, settings: dict):
        super().__init__(settings)
        from openai import OpenAI
        llm = settings["llm"]
        provider = llm.get("provider", "custom")
        key = config.api_key(settings, provider) or "not-needed"   # Ollama/LM Studio ignore the key
        self.client = OpenAI(api_key=key, base_url=llm.get("base_url") or None)
        self.model = llm.get("model")
        self.vision = bool(llm.get("vision", False))
        self.tools_supported = True

    def add_user(self, text: str):
        self.history.append({"role": "user", "content": text})

    def step(self, system: str, tools: list[ToolSpec]) -> Step:
        msgs = [{"role": "system", "content": system}] + self.history
        kw = dict(model=self.model, messages=msgs, temperature=float(self.settings["llm"].get("temperature", 0.7)))
        if self.tools_supported and tools:
            kw["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                             "parameters": t.schema()}} for t in tools]
        try:
            resp = self.client.chat.completions.create(**kw)
        except Exception as e:  # some local models don't do tool calling
            if "tools" in kw and ("tool" in str(e).lower() or "function" in str(e).lower()):
                self.tools_supported = False
                kw.pop("tools")
                resp = self.client.chat.completions.create(**kw)
            else:
                raise
        m = resp.choices[0].message
        entry = {"role": "assistant", "content": m.content or ""}
        step = Step(text=m.content or "", stop=resp.choices[0].finish_reason or "")
        if m.tool_calls:
            entry["tool_calls"] = [{"id": tc.id, "type": "function",
                                    "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                                   for tc in m.tool_calls]
            for tc in m.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                step.tool_calls.append(ToolCall(tc.id, tc.function.name, args))
        self.history.append(entry)
        return step

    def add_tool_results(self, results):
        images = []
        for call, out in results:
            self.history.append({"role": "tool", "tool_call_id": call.id, "content": out.text or "(no output)"})
            if out.image_b64 and self.vision:
                images.append(out.image_b64)
        if images:
            # Chat completions can't put images in tool messages, so attach them as a user turn.
            self._prune_images()
            self.history.append({"role": "user", "content": [{"type": "text", "text": "[screenshot from tool]"}] + [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]})

    def _prune_images(self):
        idx = [i for i, m in enumerate(self.history)
               if m["role"] == "user" and isinstance(m.get("content"), list)]
        for i in idx[:-(self.KEEP_IMAGES - 1) or None]:
            self.history[i] = {"role": "user", "content": "[older screenshot removed]"}


def make_provider(settings: dict) -> Provider:
    kind = settings["llm"].get("kind") or config.LLM_PRESETS.get(settings["llm"].get("provider"), {}).get("kind", "openai")
    return AnthropicProvider(settings) if kind == "anthropic" else OpenAICompatProvider(settings)
