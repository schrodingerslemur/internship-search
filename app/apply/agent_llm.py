"""Write application answers with the Claude subscription, through the CLI.

The worker runs on GitHub Actions with ``CLAUDE_CODE_OAUTH_TOKEN`` (made by
``claude setup-token``), so the "why this company" answers come out of the
Claude plan already being paid for rather than a metered API key. When the CLI
is absent or fails, the OpenAI-compatible client in app/pipeline/llm.py (Groq
in CI) takes over, and failing that the question is left for the user.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from app.logging_setup import get_logger
from app.pipeline.llm import APPLY_SYSTEM, LlmClient, _apply_prompt, _parse_json

log = get_logger("agent_llm")


class ClaudeCodeAnswerer:
    """``answer_application_questions`` backed by ``claude -p``."""

    def __init__(self, binary: str, *, model: str = "sonnet", timeout: float = 180) -> None:
        self.binary = binary
        self.model = model
        self.timeout = timeout
        self.calls = 0

    def answer_application_questions(
        self, *, facts: str, resume: str, posting: str, questions: list[dict]
    ) -> dict[str, dict]:
        self.calls += 1
        prompt = _apply_prompt(facts, resume, posting, questions)
        try:
            done = subprocess.run(
                [
                    self.binary, "-p",
                    "--model", self.model,
                    "--output-format", "text",
                    "--system-prompt", APPLY_SYSTEM,
                    # Answering from the text given needs no tools at all.
                    "--tools", "",
                    "--strict-mcp-config",
                    "--no-session-persistence",
                ],
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("agent_llm.failed", error=type(exc).__name__)
            return {}
        if done.returncode != 0:
            log.warning("agent_llm.exit", code=done.returncode, stderr=(done.stderr or "")[:200])
            return {}
        return parse_answers(_parse_json(done.stdout or ""))

    def close(self) -> None:
        pass


def parse_answers(payload: dict | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in (payload or {}).get("answers") or []:
        if isinstance(item, dict) and item.get("name"):
            out[str(item["name"])] = {
                "answer": item.get("answer"),
                "confident": item.get("confident") is True,
            }
    return out


class ChainedAnswerer:
    """Try each answerer in turn until one returns anything."""

    def __init__(self, answerers: list) -> None:
        self.answerers = answerers

    @property
    def names(self) -> list[str]:
        return [type(a).__name__ for a in self.answerers]

    def answer_application_questions(self, **kwargs) -> dict[str, dict]:
        for answerer in self.answerers:
            replies = answerer.answer_application_questions(**kwargs)
            if replies:
                return replies
        return {}

    def close(self) -> None:
        for answerer in self.answerers:
            answerer.close()


def apply_answerer(max_calls: int = 60) -> ChainedAnswerer:
    """The best available writer: Claude subscription first, then the API LLM."""
    chain: list = []
    binary = shutil.which("claude")
    if binary and os.environ.get("APPLY_LLM", "").lower() != "api":
        chain.append(ClaudeCodeAnswerer(binary, model=os.environ.get("APPLY_CLAUDE_MODEL", "sonnet")))
    client = LlmClient(max_calls=max_calls)
    if client.enabled:
        chain.append(client)
    return ChainedAnswerer(chain)
