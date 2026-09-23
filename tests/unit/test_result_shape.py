"""An unrecognised result shape must not read as success.

`result_is_error` fell through two getattr calls to `bool(None)`, so a result
carrying neither `isError` nor `is_error` was reported as a success. Raised by
a reader of the mcp 2.0.0 write-up: can a missing error field accidentally look
like success. It could. specs/unknown-result-shape-fails-open.md.
"""

import pytest

from rigout.results import UnknownResultShapeError, result_is_error, text_result

pytestmark = pytest.mark.unit


class NeitherName:
    """A result from some future major that renamed the flag again."""

    def __init__(self):
        self.content = []


class OnlyCamel:
    def __init__(self, flag):
        self.isError = flag


class OnlySnake:
    def __init__(self, flag):
        self.is_error = flag


def test_camel_case_flag_is_read():
    assert result_is_error(OnlyCamel(True))
    assert not result_is_error(OnlyCamel(False))


def test_snake_case_flag_is_read():
    assert result_is_error(OnlySnake(True))
    assert not result_is_error(OnlySnake(False))


def test_a_result_this_package_builds_still_works():
    assert result_is_error(text_result("broke", error=True))
    assert not result_is_error(text_result("fine"))


def test_neither_name_raises_rather_than_reporting_success():
    with pytest.raises(UnknownResultShapeError):
        result_is_error(NeitherName())


def test_the_failure_names_both_attributes_it_looked_for():
    with pytest.raises(UnknownResultShapeError) as caught:
        result_is_error(NeitherName())
    message = str(caught.value)
    assert "isError" in message and "is_error" in message


def test_an_explicit_none_flag_is_not_treated_as_absent():
    """A present flag set to None means no error, and must not raise: only the
    attribute being missing entirely is the unknown shape."""
    assert not result_is_error(OnlyCamel(None))


def test_unknown_shape_error_shows_the_payload():
    """Asked on r/mcp: an unknown shape should fail and print what arrived."""
    with pytest.raises(UnknownResultShapeError, match="Received: .*content"):
        result_is_error(NeitherName())
