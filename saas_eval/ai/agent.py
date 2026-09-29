"""Shared driver for Claude sessions via claude-agent-sdk (adapted from agente-qa
`agent/loop.py`): runs the `claude` CLI with the subscription login (no API key),
exposes only our in-process MCP tools, and turns SDK failures into actionable
RuntimeErrors."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLINotFoundError,
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    ResultError,
    TextBlock,
    query,
)

# Built-in Claude Code tools make no sense here — the judge must only use our tools.
BUILTIN_TOOLS = [
    "Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "MultiEdit",
    "NotebookEdit", "Glob", "Grep", "WebFetch", "WebSearch", "TodoWrite", "Task",
    "ExitPlanMode", "SlashCommand", "Skill", "Agent",
]

CLI_HELP = (
    "The AI layer uses claude-agent-sdk, which needs the Claude Code CLI logged in with your subscription:\n"
    "  npm install -g @anthropic-ai/claude-code\n"
    "  claude        # log in once\n"
    "Or run with --deterministic to skip the AI layer."
)


class PromptNotFoundError(FileNotFoundError):
    pass


def load_prompt(path: Path) -> tuple[str, str]:
    """(content, short sha256) — the hash goes into run.json so every report says
    which prompt version produced its AI verdicts."""
    if not path.exists():
        raise PromptNotFoundError(f"Prompt not found: {path}")
    content = path.read_text(encoding="utf-8")
    return content, hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


async def run_session(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
    max_turns: int,
    timeout_s: int,
    mcp_server: Any = None,
    server_name: str = "",
    tool_names: list[str] | None = None,
    on_message: Callable[[Any], None] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> str:
    """Runs one Claude session to completion; returns the concatenated assistant text."""
    allowed = {f"mcp__{server_name}__{n}" for n in (tool_names or [])}

    async def can_use_tool(tool_name: str, tool_input: dict, context: object):
        if tool_name in allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(message="Only the evaluator tools are available in this session.")

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers={server_name: mcp_server} if mcp_server is not None else {},
        disallowed_tools=BUILTIN_TOOLS,
        can_use_tool=can_use_tool,
        permission_mode="default",
        model=model,
        max_turns=max_turns,
        setting_sources=[],
        # Screenshots travel as base64 inside tool results; the default 1 MB
        # stdout buffer is too small for them.
        max_buffer_size=20 * 1024 * 1024,
    )

    texts: list[str] = []
    start = time.time()
    stream = query(prompt=user_prompt, options=options)
    try:
        async for message in stream:
            if on_message is not None:
                on_message(message)
            if isinstance(message, AssistantMessage):
                texts += [b.text for b in message.content if isinstance(b, TextBlock)]
            if should_stop() or (time.time() - start) > timeout_s:
                break
    except CLINotFoundError as exc:
        raise RuntimeError(f"'claude' CLI not found.\n\n{CLI_HELP}") from exc
    except ResultError as exc:
        if getattr(exc, "subtype", None) != "error_max_turns":
            raise RuntimeError(f"The 'claude' CLI ended with an error:\n{exc}\n\n{CLI_HELP}") from exc
    except ProcessError as exc:
        raise RuntimeError(f"The 'claude' CLI failed:\n{exc}\n\nIf it's an authentication error, run `claude` and log in.\n{CLI_HELP}") from exc
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            await aclose()
    return "\n".join(t for t in texts if t.strip())
