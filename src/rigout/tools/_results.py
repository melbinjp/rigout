import re
from collections.abc import Mapping
from typing import Any

from mcp.types import CallToolResult, TextContent

# A carriage return is the one that actually broke a live session, and it is the one an
# obvious "strip control characters except tab and newline" rule leaves behind. The
# Server-Sent Events grammar ends a line on CR, LF, or CRLF alike, so a bare CR inside a
# JSON message ends the event early; the message arrives truncated mid-string and the
# client waits for a reply that can never come. Reading a binary file did this at every
# size from 500 KB up, with no error and no timeout.
#
# CRLF collapses to LF and a lone CR becomes LF, because output that used them meant a
# line break and should still read as one. Windows command output is full of them.
CARRIAGE_RETURNS = re.compile(r"\r\n?")

# Terminal escape sequences are removed whole, before the character-level pass below.
# Ordinary tools colour their output when they think a terminal is watching - pytest,
# npm, cargo, git - and every one of those sequences starts with an ESC that the pass
# below would replace, turning `\x1b[31mFAILED\x1b[0m` into `?[31mFAILED?[0m`: noisier
# than what arrived. Removing the sequence leaves `FAILED`, which is what the caller
# wanted from it. Three forms: CSI (colour, cursor movement), OSC (window titles, and
# hyperlinks, terminated by BEL or ST), and the two-character escapes.
ANSI_ESCAPE_SEQUENCES = re.compile(
    r"""
    \x1b \[ [0-?]* [ -/]* [@-~]      # CSI: ESC [ ... final byte
    | \x1b \] .*? (?: \x07 | \x1b\\ )  # OSC: ESC ] ... BEL or ST
    | \x1b [@-Z\\-_]                 # two-character escapes
    """,
    re.VERBOSE | re.DOTALL,
)

# The rest carry no meaning in a tool result. They are replaced rather than dropped, so
# output that contained them still shows that something was there.
UNSAFE_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
CONTROL_REPLACEMENT = "�"

# A backstop, not the working limit: individual tools bound their own output to sizes
# that suit them. This exists so that no single response can be large enough to be a
# problem in itself, however it was produced. A live server returned five million
# characters from one `execute_command` before this.
MAX_RESULT_CHARS = 2_000_000


def transport_safe_text(text: str) -> str:
    """Make one tool result string safe to send and bounded in size."""
    cleaned = ANSI_ESCAPE_SEQUENCES.sub("", text)
    cleaned = UNSAFE_CONTROL_CHARACTERS.sub(CONTROL_REPLACEMENT, CARRIAGE_RETURNS.sub("\n", cleaned))
    if len(cleaned) > MAX_RESULT_CHARS:
        dropped = len(cleaned) - MAX_RESULT_CHARS
        cleaned = f"{cleaned[:MAX_RESULT_CHARS]}\n\n[output truncated: {dropped} more characters]"
    return cleaned


def transport_safe_result(result: CallToolResult) -> CallToolResult:
    """Return `result` with every text block made safe to send.

    Applied once at the dispatch point rather than in each handler, so a tool added
    later inherits it without having to remember to.
    """
    for block in result.content or []:
        # Only text blocks carry a string that can break the framing. Image, audio and
        # resource blocks are base64 or a URI, and are left exactly as the tool built them.
        if isinstance(block, TextContent):
            safe = transport_safe_text(block.text)
            if safe != block.text:
                block.text = safe
    return result


def build_result(**kwargs: Any) -> CallToolResult:
    """Build a CallToolResult on either mcp major.

    Callers spell the flag `isError`. 2.x renamed the field to `is_error` and kept
    `isError` as a construction alias, so this changes nothing that runs - it exists
    because the type checker reads the field list and not the aliases. The read side of
    the same rename is `result_is_error` just below.
    """
    if "isError" in kwargs and "isError" not in CallToolResult.model_fields:
        kwargs["is_error"] = kwargs.pop("isError")
    return CallToolResult(**kwargs)


def error_result(message: str) -> CallToolResult:
    """Return a tool result that MCP clients can reliably identify as failed."""
    return build_result(content=[TextContent(type="text", text=message)], isError=True)


def failure_detail(result: Mapping[str, Any], fallback: str = "Operation failed") -> str:
    """Build a non-empty diagnostic from a failed command result."""
    error = str(result.get("error") or "").strip()
    stderr = str(result.get("stderr") or "").strip()

    if error and stderr and stderr != error:
        return f"{error}\nStderr: {stderr}"
    if error:
        return error
    if stderr:
        return stderr

    exit_code = result.get("exit_code")
    if exit_code is not None:
        return f"Command exited with status {exit_code}"
    return fallback


def result_is_error(result: CallToolResult) -> bool:
    """Whether a tool result is an error, on either mcp major.

    2.x renamed the field to `is_error` while keeping `isError` as a construction alias,
    so building a result works on both but reading `.isError` off one raises on 2.x. The
    read is what has to be spelled carefully; the writes elsewhere do not.
    """
    flag = getattr(result, "isError", None)
    if flag is None:
        flag = getattr(result, "is_error", None)
    return bool(flag)
