#!/usr/bin/env python3
"""
dispatcher.py - Local Ollama-powered smart routing layer.

Routes messages through local LLMs to decide which cloud agent(s) should
handle each message, absorb trivial messages locally, and provide
summarization / validation services.

Zero external dependencies — uses urllib.request for HTTP.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from typing import Any


def _ollama_post(url: str, payload: dict[str, Any], timeout: int = 15) -> dict[str, Any]:
    """Synchronous POST to Ollama HTTP API. Returns parsed JSON response."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ollama_get(url: str, timeout: int = 5) -> dict[str, Any]:
    """Synchronous GET to Ollama HTTP API."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def ollama_chat(
    model: str,
    messages: list[dict[str, str]],
    host: str = "http://localhost:11434",
    timeout: int = 15,
) -> str:
    """Send a chat completion to Ollama and return the assistant reply text."""
    url = f"{host}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    result = await asyncio.to_thread(_ollama_post, url, payload, timeout)
    return result.get("message", {}).get("content", "").strip()


ROUTER_SYSTEM_PROMPT = """\
You are a message router for a multi-agent coding system with three cloud agents: Agent-1 (best at code writing/architecture), Agent-2 (best at code execution/debugging), and Agent-3 (best at research/documentation). These agents are identified as CLAUDE, CODEX, and GEMINI respectively.

Analyze the user message and recent context, then reply with ONLY a JSON object (no markdown, no explanation):
{
  "action": "route" | "absorb" | "clarify",
  "targets": ["CLAUDE"] | ["CODEX"] | ["GEMINI"] | ["CLAUDE","CODEX","GEMINI"],
  "task_type": "code" | "debug" | "research" | "chat" | "meta",
  "priority": "low" | "medium" | "high" | "critical",
  "reply": null or "string if action is absorb/clarify"
}

Rules:
- "absorb": Handle trivially yourself (greetings, thanks, acknowledgments, simple questions about the system). Set "reply" to your response.
- "route": Send to specific agent(s). Only include agents that are genuinely needed.
- "clarify": The message is ambiguous. Set "reply" to a clarification question.
- Default to routing to ALL agents only when the task genuinely needs multiple perspectives.
- For code tasks, prefer CLAUDE. For execution/testing, prefer CODEX. For research, prefer GEMINI.
"""


async def classify_message(
    body: str,
    context: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Send message + context to router model, return routing decision."""
    host = config.get("ollama_host", "http://localhost:11434")
    model = config.get("router_model", "qwen3.5:latest")
    timeout = config.get("router_timeout", 15)

    messages = [
        {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
        {"role": "user", "content": f"Recent context (last 800 chars):\n{context[-800:]}\n\nNew message:\n{body}"},
    ]

    try:
        raw = await ollama_chat(model, messages, host, timeout)
        # Strip markdown code fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
        cleaned = cleaned.strip()

        decision = json.loads(cleaned)
        # Validate required fields
        action = decision.get("action", "route")
        if action not in ("route", "absorb", "clarify"):
            action = "route"
        targets = decision.get("targets", [])
        if not isinstance(targets, list) or not targets:
            targets = ["CLAUDE", "CODEX", "GEMINI"]
        return {
            "action": action,
            "targets": [t.upper() for t in targets],
            "task_type": decision.get("task_type", "code"),
            "priority": decision.get("priority", "medium"),
            "reply": decision.get("reply"),
        }
    except (json.JSONDecodeError, urllib.error.URLError, TimeoutError, OSError):
        # Fallback: route to all agents
        return {
            "action": "route",
            "targets": ["CLAUDE", "CODEX", "GEMINI"],
            "task_type": "code",
            "priority": "medium",
            "reply": None,
        }


SUMMARIZER_SYSTEM_PROMPT = """\
You are a context summarizer for a multi-agent coding room. Read the conversation and produce a single paragraph (under 120 words) summarizing: key decisions made, features built, open questions, and next actions. Be precise and factual."""


async def summarize_context(
    context: str,
    config: dict[str, Any],
) -> str:
    """Call summarizer model to compact conversation context."""
    host = config.get("ollama_host", "http://localhost:11434")
    model = config.get("summarizer_model", "qwen3.5:9b")
    timeout = config.get("summarizer_timeout", 30)

    messages = [
        {"role": "system", "content": SUMMARIZER_SYSTEM_PROMPT},
        {"role": "user", "content": f"Conversation to summarize:\n{context}"},
    ]

    try:
        return await ollama_chat(model, messages, host, timeout)
    except (urllib.error.URLError, TimeoutError, OSError):
        return ""


VALIDATOR_SYSTEM_PROMPT = """\
You are a reply validator. Check if the agent's reply is relevant, correct, and helpful given the original message. Reply with ONLY a JSON object:
{"valid": true/false, "reason": "brief explanation"}"""


async def validate_reply(
    reply: str,
    original_message: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Call validator model to check reply quality."""
    host = config.get("ollama_host", "http://localhost:11434")
    model = config.get("validator_model", "rnj-1:8b")
    timeout = config.get("validator_timeout", 10)

    messages = [
        {"role": "system", "content": VALIDATOR_SYSTEM_PROMPT},
        {"role": "user", "content": f"Original message:\n{original_message}\n\nAgent reply:\n{reply}"},
    ]

    try:
        raw = await ollama_chat(model, messages, host, timeout)
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
        return json.loads(cleaned.strip())
    except (json.JSONDecodeError, urllib.error.URLError, TimeoutError, OSError):
        return {"valid": True, "reason": "validation unavailable"}


async def health_check(host: str = "http://localhost:11434") -> dict[str, Any]:
    """Ping Ollama to verify it's running and check loaded models."""
    try:
        url = f"{host}/api/tags"
        result = await asyncio.to_thread(_ollama_get, url, 5)
        models = [m.get("name", "") for m in result.get("models", [])]
        return {"available": True, "models": models}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"available": False, "models": []}
