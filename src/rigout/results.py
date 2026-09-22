"""Tool results that are safe to send, on either mcp major."""

from __future__ import annotations

import re
from typing import Any

from mcp.types import CallToolResult, TextContent

# A bare carriage return ends a Server-Sent Events line, so one inside a message cut it off
# mid-string and the client waited for a reply that never came. CRLF and lone CR become LF.
CARRIAGE_RETURNS = re.compile(r"\r\n?")

# Colour and cursor codes from tools that think a terminal is watching (pytest, npm, git).
ANSI_ESCAPE_SEQUENCES = re.compile(
    r"""
    \x1b \[ [0-?]* [ -/]* [@-~]        # CSI: ESC [ ... final byte
    | \x1b \] .*? (?: \x07 | \x1b\\ )  # OSC: ESC ] ... BEL or ST
    | \x1b [@-Z\\-_]                   # two-character escapes
    """,
    re.VERBOSE | re.DOTALL,
)

UNSAFE_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
CONTROL_REPLACEMENT = "�"

# A backstop. Each tool bounds its own output well below this.
MAX_RESULT_CHARS = 2_000_000


def transport_safe_text(text: str) -> str:
    """Strip terminal codes and unsafe control characters, and bound the size."""
    cleaned = ANSI_ESCAPE_SEQUENCES.sub("", text)
    cleaned = UNSAFE_CONTROL_CHARACTERS.sub(CONTROL_REPLACEMENT, CARRIAGE_RETURNS.sub("\n", cleaned))
    if len(cleaned) > MAX_RESULT_CHARS:
        dropped = len(cleaned) - MAX_RESULT_CHARS
        cleaned = f"{cleaned[:MAX_RESULT_CHARS]}\n[{dropped} more characters not shown]"
    return cleaned


def build_result(**kwargs: Any) -> CallToolResult:
    """Build a CallToolResult; mcp 2.x renamed `isError` to `is_error`."""
    if "isError" in kwargs and "isError" not in CallToolResult.model_fields:
        kwargs["is_error"] = kwargs.pop("isError")
    return CallToolResult(**kwargs)


def text_result(text: str, *, error: bool = False) -> CallToolResult:
    """One text block, cleaned, flagged as an error when it is one."""
    return build_result(content=[TextContent(type="text", text=transport_safe_text(text))], isError=error)


#: Distinct from None, because a flag that is present and set to None means no
#: error, while the attribute being absent means the shape is not one we know.
#: The old code used None for both and so could not tell them apart.
_ABSENT = object()

ERROR_FLAG_NAMES = ("isError", "is_error")


class UnknownResultShape(TypeError):
    """A result carrying no error flag under any name this package knows."""


def result_is_error(result: CallToolResult) -> bool:
    """Read the error flag on either mcp major.

    Raises rather than guessing when it recognises neither name. Returning
    False there would report a failed call as a success, which is how the last
    rename went unnoticed for ten days: 1.x `isError` became 2.x `is_error`,
    and a wrapper that falls back to a default survives only the rename it has
    already been told about. Not knowing is not the same as fine.
    """
    for name in ERROR_FLAG_NAMES:
        flag = getattr(result, name, _ABSENT)
        if flag is not _ABSENT:
            return bool(flag)
    raise UnknownResultShape(
        f"{type(result).__name__} carries no error flag under any known name "
        f"({', '.join(ERROR_FLAG_NAMES)}). The mcp result shape has probably "
        f"changed again; add the new name here rather than assuming success."
    )
