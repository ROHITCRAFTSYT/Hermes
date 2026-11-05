"""Chat-completion providers: Groq, NVIDIA NIM and Gnani Evon, behind one fallback chain."""

import logging
import re
import time

import httpx

import net

log = logging.getLogger("hermes.llm")

_THINK = re.compile(r"<think>.*?</think>\s*", re.S)


def tool_args(force: str | None, tools_spec: list) -> dict:
    # force=None: model may pick a tool; a tool name: must call it; "none": no tools offered at all
    # (some providers ignore tool_choice="none", so the tools are left out entirely).
    if force == "none":
        return {}
    choice = {"type": "function", "function": {"name": force}} if force else "auto"
    return {"tools": tools_spec, "tool_choice": choice}


def retry_delay_seconds(exc: Exception) -> float | None:
    m = re.search(r"try again in (?:(\d+)m)?\s*([\d.]+)s", str(exc), re.I)
    if not m:
        return None
    return int(m.group(1) or 0) * 60 + float(m.group(2))


def is_rate_limit(exc: Exception) -> bool:
    if isinstance(exc, net.UpstreamError) and exc.rate_limited:
        return True
    text = str(exc).lower()
    return "rate_limit" in text or "rate limit" in text or retry_delay_seconds(exc) is not None


class GroqLLM:
    name = "groq"

    def __init__(self, client, model: str, max_tokens: int, reasoning_effort: str | None):
        self.client, self.model, self.max_tokens = client, model, max_tokens
        self.reasoning_effort = reasoning_effort

    def chat(self, messages: list[dict], extra: dict) -> dict:
        kw = {}
        # gpt-oss thinks before answering; unchecked, the thinking eats the whole token budget.
        if "gpt-oss" in self.model.lower() and self.reasoning_effort:
            kw["reasoning_effort"] = self.reasoning_effort
        resp = self.client.chat.completions.create(model=self.model, messages=messages, temperature=0.4,
                                                   max_tokens=self.max_tokens, **extra, **kw)
        msg = resp.choices[0].message
        return {"provider": self.name, "content": msg.content or "", "tool_calls": [
            {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments or "{}"}
            for tc in (msg.tool_calls or [])]}


class OpenAICompatLLM:
    """Any /v1/chat/completions endpoint. Used for NVIDIA NIM and for Gnani Evon.

    Evon (HF gnani/gnani-evon-v3.3-30B-A3B) has no Gnani-hosted chat API that I could find
    documented, so EVON_BASE_URL points at wherever you serve it, e.g. vLLM with
    `--enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser nano_v3`.
    Without a reasoning parser the model's <think> block lands in `content`, so it is stripped.
    """

    def __init__(self, name: str, base_url: str, api_key: str, model: str, max_tokens: int,
                 client: httpx.Client, retries: int = 1, reasoning_effort: str | None = None):
        self.name, self.base_url, self.api_key, self.model = name, base_url, api_key, model
        self.max_tokens, self.client, self.retries = max_tokens, client, retries
        self.reasoning_effort = reasoning_effort

    def chat(self, messages: list[dict], extra: dict) -> dict:
        payload = {"model": self.model, "messages": messages, "temperature": 0.4,
                   "max_tokens": self.max_tokens, "stream": False, **extra}
        if "gpt-oss" in self.model.lower() and self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        # 429 is not retried here: the chain moves to the next provider instead of stalling the ESP32.
        r = net.request(self.client, "POST", f"{self.base_url}/chat/completions", service=self.name,
                        retries=self.retries, retry_status=frozenset({502, 503, 504}),
                        headers=headers, json=payload)
        try:
            msg = (r.json().get("choices") or [{}])[0].get("message") or {}
        except ValueError as e:
            raise net.UpstreamError(self.name, f"non-JSON response: {r.text[:200]}") from e
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            calls.append({"id": tc.get("id") or f"{self.name}_tool_{i}", "name": fn.get("name", ""),
                          "arguments": fn.get("arguments") or "{}"})
        content = _THINK.sub("", msg.get("content") or "").strip()
        return {"provider": self.name, "content": content, "tool_calls": calls}


class LLMChain:
    def __init__(self, providers: list, tools_spec: list, short_retries: int = 2):
        self.providers, self.tools_spec, self.short_retries = providers, tools_spec, short_retries

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.providers]

    def __bool__(self) -> bool:
        return bool(self.providers)

    def chat(self, messages: list[dict], force: str | None = None) -> dict:
        extra = tool_args(force, self.tools_spec)
        errors = []
        for i, p in enumerate(self.providers):
            is_last = i == len(self.providers) - 1
            for attempt in range(self.short_retries + 1):
                try:
                    return p.chat(messages, extra)
                except Exception as e:
                    delay = retry_delay_seconds(e)
                    # Only wait out a short rate limit when there is nobody else to ask.
                    if is_last and delay is not None and delay <= 8 and attempt < self.short_retries:
                        time.sleep(max(0.5, delay + 0.25))
                        continue
                    log.warning("llm provider failed", extra={"provider": p.name, "error": str(e)[:300],
                                                              "rate_limited": is_rate_limit(e)})
                    errors.append((p.name, e))
                    break
        if not errors:
            raise RuntimeError("No LLM provider is configured")
        if len(errors) == 1:
            raise errors[0][1]
        last = errors[-1][1]
        summary = "; ".join(f"{n} failed: {str(e)[:200]}" for n, e in errors)
        raise RuntimeError(summary) from last
