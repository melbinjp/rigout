"""A tool result must be safe to put on the wire, whatever produced it.

These cover a defect found against a live server: reading a binary file returned its
bytes decoded as text, and the control characters among them corrupted the event
stream carrying the response. A bare carriage return ends a Server-Sent Event, so the
JSON message was truncated mid-string and the client waited for a reply that could
never arrive. It reproduced at every size from 500 KB up, with no error and no
timeout - the session simply stopped.

Three separate guarantees, because one is not enough:
  - no tool result carries characters that can break the transport (the chokepoint),
  - a read of a binary file does not try to return it as text (the cause), and
  - no single result is unbounded (the backstop).
"""

import pytest
from mcp.types import CallToolResult, TextContent

from rigout.tools import file_ops
from rigout.tools._results import (
    MAX_RESULT_CHARS,
    error_result,
    transport_safe_result,
    transport_safe_text,
)
from rigout.tools.command import MAX_COMMAND_OUTPUT_CHARS, bounded_command_output


@pytest.mark.unit
@pytest.mark.parametrize("char", ["\x00", "\r", "\x1b", "\x07", "\x0b", "\x0c", "\x7f", "\x01"])
def test_control_characters_never_survive_into_a_result(char):
    """A bare CR alone is enough to truncate an SSE event and hang the client."""
    cleaned = transport_safe_text(f"before{char}after")

    assert char not in cleaned
    assert "before" in cleaned and "after" in cleaned


@pytest.mark.unit
@pytest.mark.parametrize("char", ["\n", "\t"])
def test_ordinary_whitespace_is_left_alone(char):
    """Stripping newlines or tabs would mangle every command's output."""
    assert transport_safe_text(f"a{char}b") == f"a{char}b"


@pytest.mark.unit
def test_unicode_is_not_damaged():
    text = "hello 世界 éàü \U0001f600 مرحبا"
    assert transport_safe_text(text) == text


@pytest.mark.unit
def test_a_result_is_bounded_and_says_so():
    oversized = "x" * (MAX_RESULT_CHARS + 5000)

    cleaned = transport_safe_text(oversized)

    assert len(cleaned) < len(oversized)
    assert "truncated" in cleaned
    assert "5000 more characters" in cleaned


@pytest.mark.unit
def test_text_within_the_bound_is_returned_unchanged():
    text = "y" * (MAX_RESULT_CHARS - 1)
    assert transport_safe_text(text) == text


@pytest.mark.unit
def test_every_text_block_of_a_result_is_cleaned():
    result = CallToolResult(
        content=[
            TextContent(type="text", text="first\x00block"),
            TextContent(type="text", text="second\rblock"),
        ]
    )

    cleaned = transport_safe_result(result)

    assert all("\x00" not in block.text and "\r" not in block.text for block in cleaned.content)


@pytest.mark.unit
def test_error_results_are_cleaned_too():
    """An error carrying raw bytes breaks the stream exactly as a success does."""
    cleaned = transport_safe_result(error_result("failed on \x00\x01 input"))

    assert cleaned.isError is True
    assert "\x00" not in cleaned.content[0].text


@pytest.mark.unit
def test_dispatch_applies_transport_safety_to_every_tool():
    """Applied once at dispatch, so a tool added later inherits it by default.

    Asserted against the real dispatcher rather than a handler, because the point is
    that no handler has to remember.
    """
    import asyncio
    from unittest.mock import AsyncMock, patch

    from rigout import server

    dirty = CallToolResult(content=[TextContent(type="text", text="out\x00put\rhere")])
    with patch.object(server, "_dispatch_tool", AsyncMock(return_value=dirty)):
        result = asyncio.run(server._handle_call_tool_result("execute_command", {"command": "x"}))

    assert "\x00" not in result.content[0].text
    assert "\r" not in result.content[0].text


@pytest.mark.unit
def test_binary_files_are_recognized_by_a_nul_byte():
    assert file_ops.looks_binary(b"\x89PNG\r\n\x1a\n\x00\x00\x00") is True
    assert file_ops.looks_binary(b"#!/bin/sh\necho hello\n") is False
    assert file_ops.looks_binary(b"") is False


@pytest.mark.unit
def test_reading_a_binary_file_explains_itself_instead_of_returning_bytes(tmp_path):
    """The message has to name a way forward, or the caller just tries again."""
    target = tmp_path / "image.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 40)

    text = file_ops._read_bounded(target)

    assert "binary file" in text
    assert "bulk_file_transfer" in text
    assert "base64" in text
    # The bytes themselves must not be in there under any encoding.
    assert "\x00" not in text


@pytest.mark.unit
def test_reading_a_text_file_is_unaffected(tmp_path):
    target = tmp_path / "notes.txt"
    # Written as bytes so this asserts about file content rather than about what the
    # platform's text mode does to a newline on the way in; Windows would store CRLF.
    target.write_bytes(b"line one\nline two\n")

    assert file_ops._read_bounded(target) == "line one\nline two\n"


@pytest.mark.unit
def test_a_remote_binary_read_is_refused_the_same_way():
    """A remote read arrives already decoded, so the test is for a NUL character."""
    refused = file_ops._truncate_remote("ELF\x00\x01\x02 garbage", "/usr/bin/ls")

    assert "binary file" in refused
    assert "/usr/bin/ls" in refused


@pytest.mark.unit
def test_remote_text_reads_are_not_mistaken_for_binary():
    assert file_ops._truncate_remote("plain text", "/etc/hostname") == "plain text"


@pytest.mark.unit
def test_command_output_is_bounded_and_states_the_truncation():
    """A live server returned five million characters from one call before this."""
    bounded = bounded_command_output("z" * (MAX_COMMAND_OUTPUT_CHARS + 1234))

    assert len(bounded) < MAX_COMMAND_OUTPUT_CHARS + 200
    assert "1234 more characters" in bounded


@pytest.mark.unit
def test_ordinary_command_output_is_returned_whole():
    assert bounded_command_output("build succeeded\n") == "build succeeded\n"


@pytest.mark.unit
def test_ansi_colour_codes_are_removed_rather_than_mangled():
    """Colouring output is what ordinary tools do; the caller wants the words.

    Replacing the ESC character alone would turn `\x1b[31mFAILED\x1b[0m` into
    `?[31mFAILED?[0m`, which is noisier than what arrived.
    """
    assert transport_safe_text("\x1b[31mFAILED\x1b[0m") == "FAILED"
    assert transport_safe_text("\x1b[1;32m PASS \x1b[0m rest") == " PASS  rest"


@pytest.mark.unit
def test_cursor_movement_and_progress_redraws_are_removed():
    """pip, npm and cargo redraw progress bars with cursor escapes and CRs."""
    cleaned = transport_safe_text("Downloading\x1b[2K\x1b[1G 45%\r Downloading 90%\n")

    assert "\x1b" not in cleaned
    assert "\r" not in cleaned
    assert "45%" in cleaned and "90%" in cleaned


@pytest.mark.unit
def test_window_title_and_hyperlink_sequences_are_removed():
    assert transport_safe_text("\x1b]0;my title\x07done") == "done"
    # ESC backslash is the String Terminator, the other way an OSC sequence can end.
    assert transport_safe_text("\x1b]8;;http://example.com\x1b\\link") == "link"


@pytest.mark.unit
def test_text_that_merely_mentions_an_escape_is_untouched():
    """A literal backslash-x-1-b in source or docs is text, not an escape."""
    text = r"use \x1b[31m for red"
    assert transport_safe_text(text) == text
