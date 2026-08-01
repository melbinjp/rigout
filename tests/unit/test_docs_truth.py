"""Documentation-truth tests: fail when the docs stop matching the code.

Nothing else in this suite reads a documentation file, so every claim in README.md,
QUICK_REFERENCE.md, SECURITY.md and docs/REQUEST_PATH.md drifts silently when code
moves. These tests make those claims falsifiable.

Design rules, because this runs on every push across four Python versions and three
operating systems and gates merges:

* Truth is always derived from the code at runtime - the advertised tool list, the
  argument parser, the AST of the source files. Nothing about the code is hardcoded
  here, so another agent moving a function does not break these tests.
* Checks that cannot be made reliable are skipped explicitly rather than guessed at.
  Where a claim is located by an English phrase, a rewrite of that phrase skips the
  check instead of failing it; a code change that invalidates the claim still fails.
* Failure messages name the document line, the claim, and where the truth actually
  is, so the person who broke it can fix it without re-deriving this logic.
* Everything is text and path handling only: files are read as UTF-8 with universal
  newlines, and paths are built with pathlib, so Windows CRLF and separators are fine.
"""

import ast
import asyncio
import contextlib
import difflib
import io
import keyword
import os
import re
import shlex
import sys
import tokenize
from functools import cache
from pathlib import Path

import pytest

from rigout import lifecycle, mcp_http_server, mcp_url_launcher, ssh_manager
from rigout.server import handle_list_tools

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src" / "rigout"
README = REPO_ROOT / "README.md"
QUICK_REFERENCE = REPO_ROOT / "QUICK_REFERENCE.md"
SECURITY = REPO_ROOT / "SECURITY.md"
REQUEST_PATH = REPO_ROOT / "docs" / "REQUEST_PATH.md"

pytestmark = pytest.mark.unit

if not (REPO_ROOT / "pyproject.toml").is_file():  # pragma: no cover - not a source checkout
    pytest.skip("documentation truth tests need the source checkout", allow_module_level=True)


def read(path: Path) -> str:
    """Read a repository text file with universal newlines, so CRLF checkouts match."""
    return path.read_text(encoding="utf-8")


def relative(path: Path) -> str:
    """Repository-relative path for a failure message, or the path itself.

    Falls back rather than raising, so a candidate document outside the repository can
    be pointed at this module and checked before it is written. Dry-running a phrasing
    against the real parser is the only way to learn what the parser does with it, and
    that should not require patching this file to do.
    """
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def use_candidate_document(path: str | Path) -> Path:
    """Point the request-path checks at another document and return the previous one.

    For dry-running candidate phrasings before committing to them, which is the only
    way to find out what the parser really does with a form rather than what its
    documentation says it does. Not used by any test.

    Everything in this module is cached, so repointing without clearing would answer
    confidently about the real document while you believed you were testing a
    candidate - the same silent-degradation shape these tests exist to prevent. The
    caches are discovered rather than listed, so a cache added later cannot be
    forgotten here:

        previous = use_candidate_document(r"C:\\tmp\\candidate.md")
        test_request_path_symbol_references_name_symbols_that_exist()
        test_request_path_quoted_anchors_appear_inside_the_symbol_they_name()
        test_request_path_anchors_are_not_left_dangling()
        use_candidate_document(previous)
    """
    global REQUEST_PATH
    previous, REQUEST_PATH = REQUEST_PATH, Path(path)
    for value in list(globals().values()):
        clear = getattr(value, "cache_clear", None)
        if callable(clear):
            clear()
    return previous


# --------------------------------------------------------------------------------------
# Markdown parsing helpers
# --------------------------------------------------------------------------------------

CITATION_RE = re.compile(r"^(?P<path>[\w./_-]*?):(?P<start>\d+)(?:-(?P<end>\d+))?$")
IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
FILE_SUFFIX_RE = re.compile(r"\.(py|json|log|pid|md|toml|ya?ml|txt|sh|ps1|cfg|ini)$")
CAMEL_RE = re.compile(r"[a-z][A-Z]")
LIST_ITEM_RE = re.compile(r"^\s{0,4}(?:[-*+]|\d+\.)\s")
LONG_FLAG_RE = re.compile(r"--[A-Za-z][A-Za-z0-9-]*")

# Dunder methods are defined in every module, so they anchor a citation to nothing.
UNINFORMATIVE_IDENTIFIERS = {
    "__init__",
    "__call__",
    "__main__",
    "__name__",
    "__enter__",
    "__exit__",
    "__str__",
    "__repr__",
}

# A cited line that is blank or nothing but a closing bracket is drift by itself.
TRIVIAL_SOURCE_LINES = {"", ")", "]", "}", "),", "],", "},", "):", '"""', "'''"}

# How far from a cited line a symbol named in the surrounding prose may appear before
# the citation counts as unanchored. This only applies to citations that do not sit
# inside a definition; those are checked against the definition's span instead, which
# moves with the code and needs no tolerance at all.
#
# Calibrated against the last swept state of docs/REQUEST_PATH.md, where citations of
# this kind sat 0 to 8 lines from the symbol they describe, with a handful at 13 to 21
# covered by the per-paragraph rule below. 12 leaves room for that and still fails on
# a 15 line shift; a wider window silently accepts one.
ANCHOR_PROXIMITY_LINES = 12


def code_spans(text: str) -> list[tuple[int, str, bool]]:
    """Every `inline code` span, plus `file.py:line` comments inside fenced blocks.

    Returns (offset, content, inside_fence). Inline spans may wrap across lines, which
    docs/REQUEST_PATH.md does at its 100 column wrap, so the whole document is scanned
    at once with backticks inside fenced blocks masked out.
    """
    spans: list[tuple[int, str, bool]] = []
    masked = list(text)
    offset = 0
    in_fence = False
    for line in text.splitlines(keepends=True):
        is_fence_marker = line.strip().startswith("```")
        if in_fence or is_fence_marker:
            for index in range(offset, offset + len(line)):
                if masked[index] == "`":
                    masked[index] = " "
            if not is_fence_marker:
                for match in re.finditer(r"[\w./_-]*:\d+(?:-\d+)?", line):
                    spans.append((offset + match.start(), match.group(0), True))
        if is_fence_marker:
            in_fence = not in_fence
        offset += len(line)
    for match in re.finditer(r"`([^`]+)`", "".join(masked)):
        spans.append((match.start(1), match.group(1), False))
    spans.sort()
    return spans


def prose_blocks(text: str) -> list[tuple[int, int]]:
    """Offsets of each prose block: blank-line separated, split again at list items."""
    blocks: list[tuple[int, int]] = []
    start: int | None = None
    offset = 0
    in_fence = False
    for line in text.splitlines(keepends=True):
        is_fence_marker = line.strip().startswith("```")
        if is_fence_marker:
            in_fence = not in_fence
        blank = not line.strip()
        if (blank or (not in_fence and not is_fence_marker and LIST_ITEM_RE.match(line))) and start is not None:
            blocks.append((start, offset))
            start = None
        if not blank and start is None:
            start = offset
        offset += len(line)
    if start is not None:
        blocks.append((start, offset))
    return blocks


def line_number_at(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def is_symbol_like(token: str) -> bool:
    """Code identifiers carry an underscore or internal capital; English words do not.

    Without this, prose words such as `--public-url` or `rigout status` are read as
    symbol names, match some unrelated line in the cited file, and produce noise.
    """
    return len(token) >= 4 and ("_" in token or bool(CAMEL_RE.search(token)))


def span_identifiers(span: str) -> set[str]:
    """Candidate code identifiers named by one inline code span."""
    normalised = " ".join(span.split())
    if CITATION_RE.match(normalised) or FILE_SUFFIX_RE.search(normalised):
        return set()
    return {
        token
        for token in IDENTIFIER_RE.findall(normalised)
        if is_symbol_like(token)
        and token not in UNINFORMATIVE_IDENTIFIERS
        and token not in module_names()
        and not keyword.iskeyword(token)
    }


@cache
def module_names() -> frozenset[str]:
    """Module stems in the package.

    ``rigout.mcp_url_launcher:main`` names a module, not a symbol, and a module's own
    name turns up in its docstrings and log strings, which would anchor a citation to
    a line that has nothing to do with it.
    """
    return frozenset(path.stem for path in SRC_DIR.rglob("*.py"))


# --------------------------------------------------------------------------------------
# Source-of-truth helpers, all derived from the code at runtime
# --------------------------------------------------------------------------------------


@cache
def advertised_tools() -> tuple:
    """The tool list the MCP server actually serves, from handle_list_tools()."""
    return tuple(asyncio.run(handle_list_tools()))


@cache
def advertised_tool_names() -> frozenset[str]:
    return frozenset(tool.name for tool in advertised_tools())


@cache
def advertised_schema_words() -> frozenset[str]:
    """Every property name and enum value in the advertised input schemas.

    Documentation mentions these in the same breath as tool names (`use_sudo`,
    `bypass_security`), so they have to be recognised as not-a-tool-name.
    """
    words: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    words.update(str(name) for name in value)
                if key == "enum" and isinstance(value, list):
                    words.update(str(item) for item in value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for tool in advertised_tools():
        walk(tool.inputSchema)
    return frozenset(words)


@cache
def advertised_argument_names() -> frozenset[str]:
    """Just the property names of the advertised schemas, without the enum values.

    `bypass_security` and `use_sudo` are real, documented parts of the public surface
    that exist in server.py as schema text rather than as Python symbols, the same as
    a tool name. Enum values are left out: they are ordinary words like `list` and
    `auto`, too common to let stand for a symbol.
    """
    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    names.update(str(name) for name in value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for tool in advertised_tools():
        walk(tool.inputSchema)
    return frozenset(names)


class ModuleFacts:
    """What one source file says about itself, all of it derived at runtime.

    ``definitions`` maps a name to the line spans of its definitions: functions and
    classes including their decorators, module level assignments, and attribute
    assignments such as ``self._max_requests_per_minute``. ``occurrences`` maps a name
    to every line where it appears as a real identifier token - not inside a string or
    a comment, so a name that only survives in a docstring cannot anchor anything.
    ``imports`` holds the names this module imports, which is how a documented
    ``subprocess.run`` is told apart from a documented Rigout symbol.
    """

    def __init__(self, source: str):
        self.lines: tuple[str, ...] = tuple(source.splitlines())
        self.definitions: dict[str, list[tuple[int, int]]] = {}
        self.occurrences: dict[str, set[int]] = {}
        self.imports: set[str] = set()
        self.literals: set[str] = set()

        def record(name: str, start: int, end: int | None) -> None:
            self.definitions.setdefault(name, []).append((start, end or start))

        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                start = min([node.lineno] + [decorator.lineno for decorator in node.decorator_list])
                record(node.name, start, node.end_lineno)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        record(target.id, node.lineno, node.end_lineno)
                    elif isinstance(target, ast.Attribute):
                        record(target.attr, node.lineno, node.end_lineno)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                record(node.target.id, node.lineno, node.end_lineno)
            elif isinstance(node, ast.Import | ast.ImportFrom):
                for alias in node.names:
                    self.imports.add((alias.asname or alias.name).split(".")[0])
                if isinstance(node, ast.ImportFrom) and node.module:
                    self.imports.add(node.module.split(".")[0])
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.literals.add(node.value)

        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
        except (tokenize.TokenError, IndentationError, SyntaxError):  # pragma: no cover - unparsable source
            tokens = []
        for token in tokens:
            if token.type == tokenize.NAME:
                self.occurrences.setdefault(token.string, set()).add(token.start[0])

    def knows(self, name: str) -> bool:
        return name in self.definitions or name in self.occurrences

    def mentions_between(self, name: str, first: int, last: int) -> bool:
        return any(first <= line <= last for line in self.occurrences.get(name, ()))


@cache
def source_index() -> dict[str, ModuleFacts]:
    """ModuleFacts for every module in the package, keyed by path relative to src."""
    return {path.relative_to(SRC_DIR).as_posix(): ModuleFacts(read(path)) for path in sorted(SRC_DIR.rglob("*.py"))}


@cache
def package_symbols() -> frozenset[str]:
    """Every name the package itself defines, in any module.

    Anchoring only on these keeps third party names out of it. ``exec_command`` and
    ``to_thread`` are Paramiko's and asyncio's; where they are called says nothing
    reliable about where the prose that mentions them points, and treating them as
    evidence produced false failures against a freshly checked document.
    """
    return frozenset(name for facts in source_index().values() for name in facts.definitions)


@cache
def project_scripts() -> dict[str, str]:
    """The [project.scripts] table from pyproject.toml.

    Parsed with a small regex rather than tomllib because CI runs Python 3.10, where
    tomllib does not exist.
    """
    text = read(REPO_ROOT / "pyproject.toml")
    section = re.search(r"^\[project\.scripts\]\s*$(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    if not section:
        return {}
    return {
        match.group(1): match.group(2)
        for match in re.finditer(r"^([\w.-]+)\s*=\s*\"([^\"]+)\"", section.group(1), re.MULTILINE)
    }


@cache
def parser_option_strings() -> frozenset[str]:
    """Every long option any argument parser in the package defines.

    Read from the AST of every ``add_argument`` call in the package, so a flag added,
    renamed or removed anywhere - the launcher's start and management parsers, the
    lifecycle subcommands, the HTTP server's own parser - is picked up without
    repeating the list here.
    """
    options: set[str] = set()
    for path in sorted(SRC_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(read(path))):
            if not isinstance(node, ast.Call) or getattr(node.func, "attr", None) != "add_argument":
                continue
            for argument in node.args:
                if isinstance(argument, ast.Constant) and str(argument.value).startswith("--"):
                    options.add(str(argument.value))
    return frozenset(options)


@cache
def wrapper_option_strings() -> frozenset[str]:
    """Long options mentioned by the shell wrappers, which README documents separately."""
    options: set[str] = set()
    for name in ("rigout.sh", "rigout.ps1"):
        path = REPO_ROOT / name
        if path.is_file():
            options.update(LONG_FLAG_RE.findall(read(path)))
    return frozenset(options)


# --------------------------------------------------------------------------------------
# 1. Citations in docs/REQUEST_PATH.md
# --------------------------------------------------------------------------------------


class Citation:
    """One `file.py:line` claim, with the position in the document that made it."""

    def __init__(self, doc: Path, text: str, offset: int, path: str, start: int, end: int, in_fence: bool):
        self.doc = doc
        self.offset = offset
        self.doc_line = line_number_at(text, offset)
        self.path = path
        self.start = start
        self.end = end
        self.in_fence = in_fence

    @property
    def where(self) -> str:
        return f"{relative(self.doc)}:{self.doc_line}"

    @property
    def claim(self) -> str:
        span = f"{self.start}-{self.end}" if self.end != self.start else str(self.start)
        return f"`{self.path}:{span}`"


@cache
def citations() -> tuple[Citation, ...]:
    """Every citation in docs/REQUEST_PATH.md, including bare `:123` continuations.

    A citation without a path (``ssh_manager.py:681, :1072``, and the `# :285` comments
    inside fenced examples) inherits the last path named before it.
    """
    text = read(REQUEST_PATH)
    found: list[Citation] = []
    last_path: str | None = None
    for offset, content, in_fence in code_spans(text):
        match = CITATION_RE.match(" ".join(content.split()))
        if not match:
            continue
        path = match.group("path") or last_path
        if match.group("path"):
            last_path = match.group("path")
        if not path:
            continue
        start = int(match.group("start"))
        found.append(Citation(REQUEST_PATH, text, offset, path, start, int(match.group("end") or start), in_fence))
    return tuple(found)


def resolve_citation(path: str) -> tuple[str, Path | None]:
    """Resolve a cited path to (kind, path) where kind is package, repo or external."""
    parts = path.split("/")
    in_package = SRC_DIR.joinpath(*parts)
    if in_package.is_file():
        return "package", in_package
    in_repo = REPO_ROOT.joinpath(*parts)
    if in_repo.is_file():
        return "repo", in_repo
    top_level = parts[0]
    module = sys.modules.get(top_level)
    if module is None:
        try:
            module = __import__(top_level)
        except Exception:
            module = None
    origin = getattr(module, "__file__", None) if module is not None else None
    if origin:
        external = Path(origin).resolve().parent.parent.joinpath(*parts)
        if external.is_file():
            return "external", external
    return "unresolved", None


def test_request_path_citations_resolve_to_a_real_file_and_line():
    """Every `file.py:line` names a file that exists and a line that file has.

    This says nothing about how many citations there are. The document is converting
    to symbol references, so an empty result here is legitimate; coverage is guarded
    by test_request_path_keeps_its_verifiable_references instead.
    """
    failures = []
    for citation in citations():
        kind, resolved = resolve_citation(citation.path)
        if kind == "unresolved":
            failures.append(
                f"{citation.where} cites {citation.claim} but {citation.path} is not in src/rigout/, "
                f"not in the repository root, and not inside an importable package"
            )
            continue
        if kind == "external":
            # Third party line numbers move with the dependency, which pyproject.toml
            # does not pin, so only the file itself is verified.
            continue
        line_count = len(read(resolved).splitlines())
        if citation.end > line_count:
            failures.append(f"{citation.where} cites {citation.claim} but {citation.path} has only {line_count} lines")
        elif citation.end < citation.start:
            failures.append(f"{citation.where} cites {citation.claim}, an inverted line range")
    assert not failures, "stale citations in the request path document:\n" + "\n".join(failures)


@cache
def block_keys() -> tuple[tuple[int, int], ...]:
    """Paragraph boundaries of the request path document, cached for grouping."""
    return tuple(prose_blocks(read(REQUEST_PATH)))


def block_key(offset: int) -> int:
    """Which paragraph of the document an offset falls in, or -1 if none."""
    for position, (start, end) in enumerate(block_keys()):
        if start <= offset < end:
            return position
    return -1


@cache
def citation_contexts() -> dict[int, tuple[object, frozenset[str]]]:
    """For each citation, its paragraph group and the code symbols that paragraph names.

    A symbol counts only if the package really defines it somewhere and the cited file
    really contains it, so prose about another module, and third party names such as
    ``exec_command``, cannot anchor anything. Keyed by the citation's document offset.
    """
    text = read(REQUEST_PATH)
    spans = code_spans(text)
    index = source_index()
    contexts: dict[int, tuple[object, frozenset[str]]] = {}

    for citation in citations():
        facts = index.get(citation.path)
        if citation.in_fence:
            line_start = text.rfind("\n", 0, citation.offset) + 1
            line_end = text.find("\n", citation.offset)
            context = [text[line_start:line_end]]
            group_key: object = ("fence", citation.doc_line)
        else:
            group_key = block_key(citation.offset)
            start, end = block_keys()[group_key] if group_key >= 0 else (citation.offset, citation.offset)
            context = [span for offset, span, fenced in spans if start <= offset < end and not fenced]

        named: set[str] = set()
        for span in context:
            named |= span_identifiers(span)
        relevant = {name for name in named & package_symbols() if facts is not None and facts.knows(name)}
        contexts[citation.offset] = (group_key, frozenset(relevant))
    return contexts


# --------------------------------------------------------------------------------------
# 1b. Symbol references in docs/REQUEST_PATH.md
# --------------------------------------------------------------------------------------
#
# A line number is the only part of a citation that rots. A symbol survives every
# refactor that does not rename it, and this file can confirm it from the AST.
#
# DO NOT DELETE THE NUMBERED CITATION SUPPORT once the document has been converted.
# It looks like dead transition scaffolding and is not. Some claims are about code
# that no symbol encloses - module level logging setup is the standing example - and
# for those a line number is the only mechanism that reaches the statement at all. A
# checker that cannot express a claim is worse than one that expresses it in a form
# needing occasional maintenance. Both forms are permanent.
#
# The recognised forms, all of which must be inside backticks:
#
#   `auto_failover` in `ssh_manager.py`          symbol then file
#   `auto_failover` (`ssh_manager.py`)           symbol then file in parentheses
#   `ssh_manager.py`'s `auto_failover`           file then symbol
#   `execute_in_session` in `ssh_manager.py`, at `self._check_rate_limit(`
#                                                symbol, file, and a quoted fragment
#                                                that must appear inside that symbol
#
# Rules for whoever writes these, because they are invisible at the point of writing:
#
# * The symbol and the file have to be ADJACENT: only whitespace, brackets, and at
#   most three connecting words may sit between them. That is what stops a paragraph
#   mentioning four files and six symbols from being read as twenty-four claims. A
#   full stop between them ends the pairing, and so does a table pipe, which is a cell
#   boundary rather than a connector. To put a reference in a table, keep the symbol
#   and the file in the SAME cell.
# * Either ORDER works, and an anchor attaches in either order: the gap before the
#   anchor is measured from the end of the whole symbol-and-file pair, not from the
#   file alone. It did not always, and the asymmetry was silent - the reference still
#   parsed, still verified its symbol, and still counted, while the anchor quietly did
#   not attach. test_request_path_anchors_are_not_left_dangling now makes any anchor
#   that fails to attach a failure rather than a silence.
# * A bare symbol reference is only true at the file that DEFINES the symbol.
#   `sanitize_command_output` in `security_validator.py` verifies;
#   `sanitize_command_output` in `ssh_manager.py`, which only calls it, does not. So
#   every claim about where something is CALLED needs the anchor form. For a document
#   about a request path that makes the anchor form the common case, not the upgrade.
# * A reference may STRADDLE THE LINE WRAP. The document wraps at 100 columns, so a
#   symbol, its file and its anchor routinely land on different lines; a single newline
#   and its indentation are just whitespace here. A BLANK line is not: it ends the
#   paragraph and the pairing with it.
# * A COMMA ALONE does not join a symbol to a file, because a list of two references
#   would then have the first file stealing the second symbol. Writing one anyway is
#   reported by test_request_path_has_no_comma_joined_references rather than silently
#   dropped, so the reference cannot vanish without a word.

SYMBOL_SPAN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
PY_FILE_SPAN_RE = re.compile(r"^[\w./_-]+\.py$")
CONNECTOR_WORDS = {"in", "of", "from", "inside", "at", "the", "defined", "declared", "lives", "is", "s"}
ANCHOR_CONNECTOR_WORDS = {"at", "around", "on"}
MAX_CONNECTOR_LENGTH = 30
MAX_ANCHOR_INTRO_LENGTH = 40

# The floor below which the document is no longer meaningfully checked. The last
# fully numbered revision carried 207 verifiable references; the symbol-first rewrite
# is a change of form, not of content, so a large drop means the parser stopped
# recognising the form rather than that the document got shorter. If a rewrite really
# does shrink it, lower this deliberately in the same commit and say so.
MINIMUM_VERIFIED_REFERENCES = 150


class SymbolReference:
    """One "`symbol` in `file.py`" claim, optionally with a quoted anchor fragment."""

    def __init__(
        self,
        text: str,
        offset: int,
        path: str,
        symbol: str,
        anchor: str | None,
        file_offset: int = -1,
        symbol_offset: int = -1,
    ):
        self.offset = offset
        self.doc_line = line_number_at(text, offset)
        self.path = path
        self.symbol = symbol
        self.anchor = anchor
        # Which spans were consumed by this pair, so a span already in a claim is not
        # also reported as an unjoined one.
        self.file_offset = file_offset
        self.symbol_offset = symbol_offset

    @property
    def where(self) -> str:
        return f"{relative(REQUEST_PATH)}:{self.doc_line}"

    @property
    def claim(self) -> str:
        """The claim in normalised form, which is not necessarily how it was written.

        All the accepted shapes - parenthesised, possessive, and pairs straddling the
        100-column wrap - are reported as "`symbol` in `file.py`", so a failure message
        will not tell you which form the document used. That is deliberate, because the
        message already carries the exact document line, which is the authoritative
        pointer; reproducing the written text would put a line break in the middle of a
        failure and say nothing the line number does not.
        """
        anchor = f", at `{self.anchor}`" if self.anchor else ""
        return f"`{self.symbol}` in `{self.path}`{anchor}"


def is_symbol_span(span: str) -> bool:
    """True for a bare `identifier` or `dotted.identifier`, not a filename or keyword.

    ``connection.json`` and ``tools/command.py`` read as dotted identifiers and are
    not; ``finally`` is a keyword the document quotes as prose, and is not either.
    """
    return bool(
        SYMBOL_SPAN_RE.match(span)
        and not FILE_SUFFIX_RE.search(span)
        and not any(keyword.iskeyword(part) for part in span.split("."))
    )


def _joins_a_pair(between: str) -> bool:
    """True when the text between two spans actually joins them into one claim.

    A comma alone does not. Two references side by side in a list -
    "`stdio_main` in `server.py`, `create_app` in `mcp_http_server.py`" - put a bare
    ", " between the first file and the SECOND reference's symbol, which is a shorter
    gap than the " in " joining the first pair. Without this the file would pair with
    the neighbouring claim's symbol and report a confident, wrong mismatch. So a join
    needs a connecting word, or the bracket of the parenthesised form.
    """
    return bool(re.search(r"[A-Za-z]", between) or "(" in between)


def _connector_is_adjacent(between: str, allowed: set[str]) -> bool:
    """True when only punctuation and a connecting word or two separate two spans."""
    if len(between) > MAX_CONNECTOR_LENGTH or "." in between or "|" in between or "\n\n" in between:
        return False
    words = re.findall(r"[A-Za-z]+", between.lower())
    return len(words) <= 3 and all(word in allowed for word in words)


@cache
def symbol_references() -> tuple[SymbolReference, ...]:
    """Every "`symbol` in `file.py`" claim in docs/REQUEST_PATH.md.

    The symbol may come before or after the file, and the anchor gap is measured from
    the end of whichever of the two comes last, so neither ordering is privileged.
    """
    return tuple(reference for reference, _ in _parsed_symbol_references())


@cache
def _parsed_symbol_references() -> tuple[tuple[SymbolReference, str | None], ...]:
    """Symbol references paired with the text of an anchor that would not attach."""
    text = read(REQUEST_PATH)
    spans = [(offset, " ".join(span.split()), fenced) for offset, span, fenced in code_spans(text)]
    found: list[tuple[SymbolReference, str | None]] = []

    for position, (offset, span, fenced) in enumerate(spans):
        if fenced or not PY_FILE_SPAN_RE.match(span):
            continue
        before = spans[position - 1] if position else None
        after = spans[position + 1] if position + 1 < len(spans) else None
        candidates = []
        if before and is_symbol_span(before[1]):
            gap = text[before[0] + len(before[1]) : offset]
            if _joins_a_pair(gap) and _connector_is_adjacent(gap, CONNECTOR_WORDS):
                candidates.append((len(gap), before[1], before[0], offset + len(span)))
        if after and is_symbol_span(after[1]):
            gap = text[offset + len(span) : after[0]]
            if _joins_a_pair(gap) and _connector_is_adjacent(gap, CONNECTOR_WORDS):
                candidates.append((len(gap) + 1, after[1], after[0], after[0] + len(after[1])))
        if not candidates:
            continue
        _, symbol, symbol_offset, pair_end = min(candidates)

        anchor: str | None = None
        dangling: str | None = None
        for anchor_offset, anchor_span, anchor_fenced in spans[position + 1 :][:3]:
            if anchor_offset < pair_end or anchor_fenced:
                continue  # part of the pair itself, or a fenced example
            if is_symbol_span(anchor_span) or PY_FILE_SPAN_RE.match(anchor_span):
                break
            if CITATION_RE.match(anchor_span):
                break  # a line citation, never an anchor fragment
            gap = text[pair_end:anchor_offset]
            words = re.findall(r"[A-Za-z]+", gap.lower())
            # An anchor is introduced, "..., at `fragment`". Prose that merely happens
            # to contain "at" or "on" somewhere in a long gap is not an attempt at one,
            # and must not be reported as a near miss. Nor is prose that ends in one by
            # coincidence a sentence later: "). The command is split at `&&`" ends with
            # "at" and means nothing of the kind, so a sentence boundary in the gap
            # rules it out. A real anchor intro never crosses one.
            introduced = words and words[-1] in ANCHOR_CONNECTOR_WORDS
            if not (introduced and "." not in gap and len(gap) <= MAX_ANCHOR_INTRO_LENGTH):
                break
            if _connector_is_adjacent(gap, CONNECTOR_WORDS | ANCHOR_CONNECTOR_WORDS):
                anchor = anchor_span
            else:
                dangling = anchor_span
            break
        found.append(
            (
                SymbolReference(text, min(offset, symbol_offset), span, symbol, anchor, offset, symbol_offset),
                dangling,
            )
        )
    return tuple(found)


@cache
def unjoined_pairs() -> tuple[tuple[int, str, str], ...]:
    """A symbol and a file sitting side by side with only a comma between them.

    A comma does not join a pair - it cannot, or a list of two references would have
    the first file stealing the second symbol. But a writer converting a table cell
    like "`execute_command`, `ssh_manager.py`" has plainly written a claim, and the
    parser's silence about it would be the reference vanishing without a word. Neither
    span may already belong to a real pair, which is what keeps a legitimate list -
    "`stdio_main` in `server.py`, `create_app` in `mcp_http_server.py`" - out of it.

    Returns (offset, symbol, path).
    """
    text = read(REQUEST_PATH)
    spans = [(offset, " ".join(span.split()), fenced) for offset, span, fenced in code_spans(text)]
    claimed = set()
    for reference, _ in _parsed_symbol_references():
        claimed.update({reference.file_offset, reference.symbol_offset})

    found = []
    for position, (offset, span, fenced) in enumerate(spans):
        if fenced or offset in claimed or not PY_FILE_SPAN_RE.match(span):
            continue
        neighbours = [
            spans[position - 1] if position else None,
            spans[position + 1] if position + 1 < len(spans) else None,
        ]
        for neighbour in neighbours:
            if neighbour is None:
                continue
            other_offset, other_span, other_fenced = neighbour
            if other_fenced or other_offset in claimed or not is_symbol_span(other_span):
                continue
            # Gaps run between span CONTENTS, so they carry the two spans' backticks -
            # the same convention the pairing above uses.
            gap = (
                text[other_offset + len(other_span) : offset]
                if other_offset < offset
                else text[offset + len(span) : other_offset]
            )
            if re.fullmatch(r"[\s,`]*", gap):
                found.append((min(offset, other_offset), other_span, span))
                break
    return tuple(found)


def test_request_path_has_no_comma_joined_references():
    """A symbol and a file joined by nothing but a comma are not a claim, and say so.

    This is the one place where refusing to pair could lose a reference in silence, so
    it is reported instead. The fix is one word.
    """
    text = read(REQUEST_PATH)
    failures = [
        f"{relative(REQUEST_PATH)}:{line_number_at(text, offset)} puts `{symbol}` and `{path}` side by side with "
        f"only a comma between them. A comma does not join them into a claim, so nothing about it is checked. "
        f"Write `{symbol}` in `{path}`."
        for offset, symbol, path in unjoined_pairs()
    ]
    assert not failures, "references a comma failed to join:\n" + "\n".join(failures)


def test_request_path_anchors_are_not_left_dangling():
    """A fragment written as an anchor must actually attach to its symbol.

    The pairing rules are invisible at the point of writing, so an anchor that does
    not attach would otherwise degrade in silence: the reference still parses, still
    verifies its symbol, and still counts toward the floor, while the fragment that
    was the whole point of the claim goes unchecked. That is worse than a wholesale
    parse failure, which at least shows up as a number. So it is a failure here.
    """
    failures = []
    for reference, dangling in _parsed_symbol_references():
        if dangling is None:
            continue
        failures.append(
            f"{reference.where} writes `{dangling}` as an anchor on `{reference.symbol}` in "
            f"`{reference.path}`, but too much text sits between them for it to attach, so it "
            f"would not be checked. Put the anchor directly after the pair - "
            f"`{reference.symbol}` in `{reference.path}`, at `{dangling}` - or reword so it does "
            f"not read as an anchor."
        )
    assert not failures, "anchors that would not be verified:\n" + "\n".join(failures)


def _resolve_symbol(facts: ModuleFacts, symbol: str) -> tuple[bool, str]:
    """Check one documented symbol against a module. Returns (ok, explanation).

    A bare `symbol` - the form the whole conversion is about - must be defined in the
    file, imported by it, or be an advertised MCP tool name or tool argument declared
    there as schema text, which is the only way either exists in Python. Nothing
    looser: letting
    any string literal count was tried and is wrong, because renaming ``auto_failover``
    in ssh_manager.py leaves the string ``"auto_failover"`` behind as a dict key and the
    stale claim passes.

    A dotted `a.b` is a path into something rather than a symbol of this file -
    ``server.run`` reaches into the SDK, ``mcp.headers.Authorization`` names a key in
    generated JSON - so the last part need only be used there, as a token or as a
    whole string literal. Weaker, and deliberately so; the strict rule above is the one
    that carries the document.
    """
    parts = symbol.split(".")
    name = parts[-1]
    if name not in facts.definitions:
        dotted_use = len(parts) > 1 and (name in facts.occurrences or name in facts.literals)
        advertised = advertised_tool_names() | advertised_argument_names()
        declared_tool = name in advertised and name in facts.literals
        if not (name in facts.imports or declared_tool or dotted_use):
            close = difflib.get_close_matches(name, facts.definitions, n=3, cutoff=0.7)
            suggestion = f"; did you mean {', '.join(close)}?" if close else ""
            return False, f"does not define, import or mention {name}{suggestion}"
        return True, ""
    if len(parts) > 1 and parts[-2] in facts.definitions:
        owners = facts.definitions[parts[-2]]
        inside = any(
            owner_start <= start and end <= owner_end
            for start, end in facts.definitions[name]
            for owner_start, owner_end in owners
        )
        if not inside:
            return False, f"defines {name}, but not inside {parts[-2]}"
    return True, ""


def test_request_path_symbol_references_name_symbols_that_exist():
    """Every "`symbol` in `file.py`" claim is a symbol that file really defines.

    This is the check that replaces line numbers. It reads the definition out of the
    module's AST at runtime, so it survives every refactor that does not rename the
    symbol, and it fails the moment one is renamed or removed.
    """
    index = source_index()
    failures = []
    for reference in symbol_references():
        kind, resolved = resolve_citation(reference.path)
        if kind == "unresolved":
            failures.append(f"{reference.where} says {reference.claim} but {reference.path} does not exist")
            continue
        facts = index.get(reference.path)
        if facts is None:
            # A file outside the package, or a non-module file: existence is all we can say.
            continue
        ok, explanation = _resolve_symbol(facts, reference.symbol)
        if not ok:
            failures.append(f"{reference.where} says {reference.claim} but {reference.path} {explanation}")
    assert not failures, "symbol references that no longer resolve:\n" + "\n".join(failures)


def test_request_path_quoted_anchors_appear_inside_the_symbol_they_name():
    """A quoted fragment attached to a symbol reference must be in that symbol's body.

    This is what an interior-of-function line number becomes. It pins the claim to
    code rather than to a position, so it stays true while the function moves and
    fails when the line it describes is changed or deleted.

    THE RESIDUAL, measured rather than assumed. This proves ATTACHMENT, not INTENT.
    An anchor that occurs more than once inside the symbol passes while pinning
    nothing: `sanitize_command_output(` appears on both the local and the SSH branch of
    execute_in_session, so a sentence about one branch anchored on it would pass while
    describing the other. Nothing here can distinguish them, and making repetition a
    failure would reject legitimate anchors, so choosing a distinctive fragment is the
    author's job. Count occurrences before writing one:

        import test_docs_truth as t
        facts = t.source_index()["ssh_manager.py"]
        for start, end in facts.definitions["execute_in_session"]:
            print([start + i for i, line in enumerate(facts.lines[start - 1 : end]) if FRAGMENT in line])

    One hit means the anchor is distinctive. More than one means it will attach to
    whichever comes first and prove nothing about which the prose meant.
    """
    index = source_index()
    failures = []
    for reference in symbol_references():
        if not reference.anchor:
            continue
        facts = index.get(reference.path)
        if facts is None:
            continue
        spans = facts.definitions.get(reference.symbol.split(".")[-1])
        if not spans:
            continue  # reported by the symbol test
        wanted = " ".join(reference.anchor.split())
        bodies = [" ".join(" ".join(facts.lines[start - 1 : end]).split()) for start, end in spans]
        if any(wanted in body for body in bodies):
            continue
        whole = " ".join(" ".join(facts.lines).split())
        where = "elsewhere in the file" if wanted in whole else "nowhere in the file"
        failures.append(
            f"{reference.where} says {reference.claim}, but that text is {where}. "
            f"{reference.symbol} spans {'; '.join(f':{start}-{end}' for start, end in spans[:3])}"
        )
    assert not failures, "quoted anchors that no longer match the code:\n" + "\n".join(failures)


def test_request_path_file_mentions_resolve():
    """Every `file.py` named in the document is a file that exists.

    Bare mentions - the handler table, "`ssh_manager.py` does not import
    `config_manager`" - carry no symbol to check, but a renamed or deleted module
    still has to show up somewhere.
    """
    text = read(REQUEST_PATH)
    failures = []
    for offset, span, fenced in code_spans(text):
        normalised = " ".join(span.split())
        if fenced or not PY_FILE_SPAN_RE.match(normalised):
            continue
        if resolve_citation(normalised)[0] == "unresolved":
            failures.append(
                f"{relative(REQUEST_PATH)}:{line_number_at(text, offset)} names `{normalised}`, which is not in "
                f"src/rigout/, not in the repository root, and not inside an importable package"
            )
    assert not failures, "\n".join(sorted(set(failures)))


def test_request_path_keeps_its_verifiable_references():
    """The document must keep enough machine-checkable references to be worth having.

    Both forms count. This exists for the transition: a citation converted into a
    phrasing the parser above does not recognise becomes invisible rather than wrong,
    every citation test still passes, and the suite goes green while checking less.
    A floor turns that silence into a failure.

    Only references this file can actually check are counted. One that points outside
    the package is real but unverifiable here, and counting it would let the floor be
    satisfied by references that verify nothing, which is the false comfort the floor
    exists to prevent.
    """
    index = source_index()
    numbered = sum(1 for citation in citations() if resolve_citation(citation.path)[0] in {"package", "repo"})
    symbolic = sum(1 for reference in symbol_references() if reference.path in index)
    total = numbered + symbolic
    assert total >= MINIMUM_VERIFIED_REFERENCES, (
        f"{relative(REQUEST_PATH)} now yields only {total} checkable references "
        f"({numbered} line citations, {symbolic} symbol references), below the floor of "
        f"{MINIMUM_VERIFIED_REFERENCES}. Either the document lost its citations, or it was rewritten into a "
        f"form this parser does not recognise - see the recognised forms at the top of section 1b. "
        f"If the document genuinely got shorter, lower MINIMUM_VERIFIED_REFERENCES in the same change."
    )


def _symbol_locations(names: frozenset[str], facts: ModuleFacts) -> str:
    reports = []
    for name in sorted(names):
        spans = facts.definitions.get(name)
        if spans:
            reports.append(", ".join(f"{name} is defined at :{start}-{end}" for start, end in spans[:3]))
        else:
            hits = sorted(facts.occurrences.get(name, ()))[:5]
            reports.append(f"{name} appears at {', '.join(f':{hit}' for hit in hits)}")
    return "; ".join(reports)


def _where_it_went(citation: Citation) -> str:
    """The part of a failure message that says where the code actually is now."""
    facts = source_index().get(citation.path)
    names = citation_contexts().get(citation.offset, (None, frozenset()))[1]
    if facts is not None and names:
        return f"; {_symbol_locations(names, facts)}"
    if facts is None:
        return ""
    for distance in range(1, 8):
        for candidate in (citation.start - distance, citation.start + distance):
            if 1 <= candidate <= len(facts.lines):
                body = facts.lines[candidate - 1].strip()
                if body not in TRIVIAL_SOURCE_LINES and not body.startswith("#"):
                    return f"; the nearest code is line {candidate}: {body!r}"
    return ""


def test_request_path_citations_point_at_real_code():
    """A cited line is never blank or a lone closing bracket.

    This is the check that catches a file that merely shrank or shifted upward: the
    line number still resolves, but there is nothing at it.
    """
    failures = []
    for citation in citations():
        kind, resolved = resolve_citation(citation.path)
        if kind not in {"package", "repo"} or resolved is None:
            continue
        lines = read(resolved).splitlines()
        if citation.end > len(lines):
            continue  # reported by the range test
        body = lines[citation.start - 1].strip()
        if body in TRIVIAL_SOURCE_LINES:
            failures.append(
                f"{citation.where} cites {citation.claim} but that line is {body!r}{_where_it_went(citation)}"
            )
    assert not failures, "citations pointing at nothing:\n" + "\n".join(failures)


def test_request_path_citations_match_the_code_they_describe():
    """A citation must land on the code its own paragraph is talking about.

    For each citation, the symbols named in the surrounding paragraph (or, inside a
    fenced example, on the same line) are looked up in the cited file. The citation
    then has to sit inside one of those symbols' definitions, or within a few lines of
    a mention of one, both read from the file at runtime so they move with the code.

    Citations are judged per paragraph and per file rather than one at a time: a
    paragraph often cites both a definition and a call site further down, and only the
    first can be pinned to a definition span. A refactor that moves code invalidates
    the whole group, which is the drift this is for. A paragraph whose prose names no
    symbol of the cited file is left to the two tests above.

    A symbol reference in the paragraph anchors the group too. Under conversion it is
    usually the definition citation that becomes a symbol reference, and the call site
    that keeps its line number; without this the leftover line number would be judged
    alone and fail for having lost its partner, which is a false failure caused by
    nothing but the conversion.
    """
    index = source_index()
    groups: dict[tuple[object, str], list[tuple[Citation, bool, frozenset[str]]]] = {}
    for reference in symbol_references():
        facts = index.get(reference.path)
        if facts is None or reference.symbol.split(".")[-1] not in facts.definitions:
            continue
        groups.setdefault((block_key(reference.offset), reference.path), [])
    anchored_by_symbol = set(groups)

    for citation in citations():
        facts = index.get(citation.path)
        if facts is None:
            continue
        if citation.end > len(facts.lines) or facts.lines[citation.start - 1].strip() in TRIVIAL_SOURCE_LINES:
            continue  # reported by the tests above
        group_key, relevant = citation_contexts()[citation.offset]
        if not relevant:
            continue

        anchored = any(
            any(start - 1 <= citation.start <= end for start, end in facts.definitions.get(name, ()))
            for name in relevant
        )
        if not anchored:
            anchored = any(
                facts.mentions_between(
                    name, citation.start - ANCHOR_PROXIMITY_LINES, citation.end + ANCHOR_PROXIMITY_LINES
                )
                for name in relevant
            )
        groups.setdefault((group_key, citation.path), []).append((citation, anchored, relevant))

    failures = []
    for key, items in groups.items():
        if key in anchored_by_symbol or any(anchored for _, anchored, _ in items):
            continue
        for citation, _, relevant in items:
            failures.append(
                f"{citation.where} cites {citation.claim}, but nothing within "
                f"{ANCHOR_PROXIMITY_LINES} lines of it relates to that text. In {citation.path}, "
                f"{_symbol_locations(relevant, index[citation.path])}"
            )
    assert not failures, "citations that no longer describe the code at the line they name:\n" + "\n".join(
        sorted(set(failures))
    )


# --------------------------------------------------------------------------------------
# 2. Tool list agreement
# --------------------------------------------------------------------------------------


def bullet_prefixes(markdown: str) -> list[str]:
    """The text of each list item up to its first colon, where tool names are named."""
    prefixes = []
    for line in markdown.splitlines():
        if LIST_ITEM_RE.match(line):
            body = LIST_ITEM_RE.sub("", line, count=1)
            prefixes.append(body.split(":", 1)[0])
    return prefixes


def readme_sections() -> dict[str, str]:
    sections: dict[str, str] = {}
    heading = "(top)"
    buffer: list[str] = []
    for line in read(README).splitlines():
        if line.startswith("## "):
            sections[heading] = "\n".join(buffer)
            heading = line[3:].strip()
            buffer = []
        else:
            buffer.append(line)
    sections[heading] = "\n".join(buffer)
    return sections


def test_readme_documents_every_advertised_tool():
    """Every tool the server serves is named somewhere in README.md."""
    text = read(README)
    missing = sorted(name for name in advertised_tool_names() if f"`{name}`" not in text)
    assert not missing, (
        "handle_list_tools() advertises tools that README.md never mentions: "
        + ", ".join(missing)
        + " (add them under the MCP tools section)"
    )


def test_readme_does_not_document_tools_that_do_not_exist():
    """The README tool list names no tool the server does not serve."""
    scored = {
        heading: {name for prefix in bullet_prefixes(body) for name in advertised_tool_names() if f"`{name}`" in prefix}
        for heading, body in readme_sections().items()
    }
    heading, hits = max(scored.items(), key=lambda item: len(item[1]))
    assert len(hits) > len(advertised_tool_names()) / 2, (
        "could not find the tool list in README.md: no section names more than half of the advertised tools "
        "in its bullet points. Either the list moved out of bullet form or the tools were renamed."
    )
    claimed: set[str] = set()
    for prefix in bullet_prefixes(readme_sections()[heading]):
        claimed.update(re.findall(r"`([a-z][a-z0-9_]*)`", prefix))
    unknown = sorted(claimed - advertised_tool_names() - advertised_schema_words())
    assert not unknown, (
        f"README.md section '{heading}' documents tools that handle_list_tools() does not advertise: "
        + ", ".join(unknown)
        + ". Advertised: "
        + ", ".join(sorted(advertised_tool_names()))
    )


@cache
def dispatched_tool_names() -> frozenset[str] | None:
    """Tool names the if/elif chain in server.py actually handles.

    The chain and the advertised list are two hand-maintained copies of the same names.
    Located by finding the function that returns "Unknown tool" and reading the string
    comparisons against its first argument; a rewrite into a registry returns None and
    the check is skipped rather than failing wrongly.
    """
    tree = ast.parse(read(SRC_DIR / "server.py"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        source = ast.dump(node)
        if "Unknown tool" not in source or not node.args.args:
            continue
        argument = node.args.args[0].arg
        names = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Compare) and isinstance(child.left, ast.Name) and child.left.id == argument:
                for operator, comparator in zip(child.ops, child.comparators, strict=False):
                    if isinstance(operator, ast.Eq) and isinstance(getattr(comparator, "value", None), str):
                        names.add(comparator.value)
        if names:
            return frozenset(names)
    return None


def test_dispatch_chain_handles_exactly_the_advertised_tools():
    """An advertised tool with no dispatch branch answers `Unknown tool` at runtime."""
    dispatched = dispatched_tool_names()
    if dispatched is None:
        pytest.skip("server.py no longer dispatches with string comparisons; this check needs rewriting")
    advertised = advertised_tool_names()
    assert not (advertised - dispatched), (
        "handle_list_tools() advertises tools that _handle_call_tool_result does not dispatch, so calling them "
        "returns 'Unknown tool': " + ", ".join(sorted(advertised - dispatched))
    )
    assert not (dispatched - advertised), (
        "server.py dispatches tools that handle_list_tools() does not advertise, so no client can call them: "
        + ", ".join(sorted(dispatched - advertised))
    )


def test_documented_tool_counts_match_the_advertised_list():
    """Counts stated in prose ("15 Tool objects", "15-branch chain") stay true."""
    number_words = {
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "sixteen": 16,
        "seventeen": 17,
        "eighteen": 18,
        "nineteen": 19,
        "twenty": 20,
    }
    patterns = (
        r"(\w+)[- ]branch if/elif chain",
        r"list of (\w+) `Tool` objects",
        r"same (\w+) names",
    )
    expected = len(advertised_tool_names())
    failures = []
    checked = 0
    for doc in (README, QUICK_REFERENCE, REQUEST_PATH):
        text = read(doc)
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                token = match.group(1).lower()
                value = number_words.get(token, int(token) if token.isdigit() else None)
                if value is None:
                    continue
                checked += 1
                if value != expected:
                    failures.append(
                        f"{relative(doc)}:{line_number_at(text, match.start())} says {match.group(0)!r} "
                        f"but handle_list_tools() advertises {expected} tools"
                    )
    if not checked:
        pytest.skip("no tool-count claims found in the documentation")
    assert not failures, "\n".join(failures)


# --------------------------------------------------------------------------------------
# 3. CLI surface agreement
# --------------------------------------------------------------------------------------

ENV_ASSIGNMENT_RE = re.compile(r"^[A-Z_][A-Z0-9_]*=")


def documented_command_lines() -> list[tuple[Path, int, str]]:
    """Command lines shown in fenced blocks or backticks that invoke the launcher."""
    found: list[tuple[Path, int, str]] = []
    for doc in (README, QUICK_REFERENCE, REQUEST_PATH, SECURITY):
        text = read(doc)
        in_fence = False
        for number, line in enumerate(text.splitlines(), start=1):
            if line.strip().startswith("```"):
                in_fence = not in_fence
            elif in_fence and line.strip():
                found.append((doc, number, line.strip()))
        for span_offset, span, fenced in code_spans(text):
            if not fenced and re.match(r"^(rigout|python -m rigout\.mcp_url_launcher)\b", span.strip()):
                found.append((doc, line_number_at(text, span_offset), " ".join(span.split())))
    return found


# Where a documented command line stops being rigout's and becomes the shell's.
# `rigout url | xclip` and `rigout url > url.txt` document real usage, and the tokens
# after the operator belong to another program; feeding them to rigout's parser would
# report a documentation error that is not one. Everything before the operator is still
# checked, which is the part this test exists to check.
SHELL_OPERATORS = frozenset({"|", "||", "&&", "&", ";", ">", ">>", "<", "<<", "2>", "2>&1"})


def launcher_arguments(command_line: str) -> list[str] | None:
    """Argument list for a documented command line, or None if it is not the launcher."""
    try:
        tokens = shlex.split(command_line, posix=True)
    except ValueError:
        return None
    while tokens and ENV_ASSIGNMENT_RE.match(tokens[0]):
        tokens = tokens[1:]
    for index, token in enumerate(tokens):
        if token in SHELL_OPERATORS or token.startswith("#"):
            tokens = tokens[:index]
            break
    if not tokens:
        return None
    if tokens[0] == "rigout":
        return tokens[1:]
    if tokens[:3] == ["python", "-m", "rigout.mcp_url_launcher"]:
        return tokens[3:]
    return None


def test_documented_command_lines_parse():
    """Every `rigout ...` invocation in the docs is accepted by the real parser.

    Truth comes from calling mcp_url_launcher.parse_args, so a renamed subcommand or a
    removed flag fails here instead of failing a user.
    """
    checked = 0
    failures = []
    for doc, number, command_line in documented_command_lines():
        arguments = launcher_arguments(command_line)
        if arguments is None:
            continue
        checked += 1
        try:
            mcp_url_launcher.parse_args(arguments)
        except SystemExit:
            failures.append(f"{relative(doc)}:{number} documents `{command_line}` but the launcher's parser rejects it")
        except Exception as error:  # noqa: BLE001 - reported, not swallowed
            failures.append(f"{relative(doc)}:{number} documents `{command_line}` but parsing raised {error!r}")
    assert checked, "no documented rigout command lines were found; the extraction or the docs changed"
    assert not failures, "documented commands the CLI does not accept:\n" + "\n".join(failures)


def rigout_flag_span(span: str) -> bool:
    """True when a code span's flags belong to rigout rather than another program.

    ``pip install --upgrade rigout`` documents a pip flag; ``rigout --tunnel none`` and
    a bare ``--no-auth`` document rigout's own.
    """
    tokens = " ".join(span.split()).split()
    while tokens and ENV_ASSIGNMENT_RE.match(tokens[0]):
        tokens = tokens[1:]
    if not tokens or tokens[0].startswith("-"):
        return True
    if tokens[0] in {"rigout", "rigout-stdio"}:
        return True
    return tokens[0] == "python" and any(token.startswith("rigout") for token in tokens[1:3])


def test_documented_flags_exist_in_the_cli():
    """Every `--flag` shown in the docs exists in a parser or in a shell wrapper."""
    known = parser_option_strings() | wrapper_option_strings()
    assert known, "no long options could be read from the argument parsers"
    failures = []
    for doc in (README, QUICK_REFERENCE, REQUEST_PATH, SECURITY):
        text = read(doc)
        for offset, span, fenced in code_spans(text):
            if fenced or not rigout_flag_span(span):
                continue
            for flag in LONG_FLAG_RE.findall(span):
                if flag not in known:
                    failures.append(
                        f"{relative(doc)}:{line_number_at(text, offset)} documents `{flag}`, which no rigout "
                        f"parser and neither shell wrapper defines"
                    )
    assert not failures, "documented flags that do not exist:\n" + "\n".join(sorted(set(failures)))


@cache
def lifecycle_subcommands() -> frozenset[str]:
    """Subcommands parse_args really recognises.

    Every string in any set, list or tuple literal in the launcher is a candidate, and
    each candidate is then put through parse_args itself and kept only if the parser
    answers with that command. The confirmation step is what makes the wide net safe:
    an unrelated literal such as the ``{"text", "json"}`` output choices is offered to
    the parser, rejected, and dropped.

    The net is wide because a narrow one broke. It first read set literals inside
    parse_args only, and when the commands moved out to a module-level
    LIFECYCLE_COMMANDS constant the derivation silently returned nothing and this
    check skipped itself out of existence - the same silent under-verification these
    tests exist to catch, in the tests themselves.
    """
    candidates: set[str] = set()
    for node in ast.walk(ast.parse(read(SRC_DIR / "mcp_url_launcher.py"))):
        if isinstance(node, ast.Set | ast.List | ast.Tuple):
            candidates.update(item.value for item in node.elts if isinstance(getattr(item, "value", None), str))
    confirmed = set()
    noise = io.StringIO()
    for name in candidates:
        if not name.isidentifier():
            continue
        # argparse prints usage to stderr for every candidate it rejects, which is
        # most of them; that is the mechanism working, not something to read.
        with contextlib.redirect_stderr(noise), contextlib.redirect_stdout(noise):
            try:
                parsed = mcp_url_launcher.parse_args([name])
            except (SystemExit, Exception):
                continue
        if getattr(parsed, "command", None) == name:
            confirmed.add(name)
    return frozenset(confirmed)


def test_lifecycle_subcommands_are_documented():
    """A subcommand the CLI accepts but no document mentions is undiscoverable."""
    subcommands = lifecycle_subcommands()
    if not subcommands:
        pytest.skip("could not read the lifecycle subcommand set from parse_args")
    documented = read(README) + read(QUICK_REFERENCE)
    missing = sorted(name for name in subcommands if f"rigout {name}" not in documented)
    assert not missing, (
        "mcp_url_launcher.parse_args accepts subcommands that README.md and QUICK_REFERENCE.md never show: "
        + ", ".join(missing)
    )


def test_documented_console_scripts_match_pyproject():
    """`rigout` and `rigout-stdio` claims agree with [project.scripts] and import."""
    scripts = project_scripts()
    assert scripts, "no [project.scripts] entries could be read from pyproject.toml"
    for name, target in scripts.items():
        module_name, _, attribute = target.partition(":")
        module = __import__(module_name, fromlist=[attribute])
        assert callable(getattr(module, attribute, None)), (
            f"pyproject.toml maps the `{name}` console script to {target}, which is not callable"
        )
    documented = read(README) + read(QUICK_REFERENCE) + read(REQUEST_PATH)
    missing = sorted(name for name in scripts if f"`{name}`" not in documented and f"{name}\n" not in documented)
    assert not missing, "console scripts no document mentions: " + ", ".join(missing)
    failures = []
    for doc in (README, QUICK_REFERENCE, REQUEST_PATH):
        text = read(doc)
        for offset, span, fenced in code_spans(text):
            if fenced or ":" not in span:
                continue
            claim = span.strip()
            if re.fullmatch(r"rigout\.[\w.]+:\w+", claim) and claim not in scripts.values():
                failures.append(
                    f"{relative(doc)}:{line_number_at(text, offset)} names the entry point `{claim}`, "
                    f"but pyproject.toml declares {sorted(scripts.values())}"
                )
    assert not failures, "\n".join(failures)


# --------------------------------------------------------------------------------------
# 4. Other claims that drift
# --------------------------------------------------------------------------------------


def test_project_layout_paths_exist():
    """Paths drawn in the README project layout resolve in the repository."""
    text = read(README)
    sections = readme_sections()
    layout = next((body for heading, body in sections.items() if "layout" in heading.lower()), None)
    if layout is None:
        pytest.skip("README.md has no project layout section")
    prefix = ""
    failures = []
    checked = 0
    for line in layout.splitlines():
        if not line.strip() or line.strip().startswith("```"):
            continue
        token = line.split()[0]
        if "/" not in token and not token.endswith(".py"):
            continue
        indented = line.startswith((" ", "\t"))
        candidate = f"{prefix}{token}" if indented and prefix else token
        if not indented:
            prefix = token if token.endswith("/") else ""
        checked += 1
        if not (REPO_ROOT / candidate.rstrip("/")).exists():
            failures.append(
                f"{relative(README)}:{line_number_at(text, text.find(line))} draws `{candidate}` "
                f"in the project layout, but it does not exist"
            )
    if not checked:
        pytest.skip("no paths found in the README project layout")
    assert not failures, "\n".join(failures)


def test_documented_python_requirement_matches_pyproject():
    """ "Requires Python X.Y or newer" agrees with requires-python."""
    declared = re.search(r"^requires-python\s*=\s*\"([^\"]+)\"", read(REPO_ROOT / "pyproject.toml"), re.MULTILINE)
    assert declared, "pyproject.toml has no requires-python"
    minimum = re.search(r"(\d+\.\d+)", declared.group(1))
    assert minimum, f"could not read a minimum version from requires-python = {declared.group(1)!r}"
    failures = []
    checked = 0
    for doc in (README, QUICK_REFERENCE):
        text = read(doc)
        for match in re.finditer(r"[Pp]ython (\d+\.\d+)( or newer| or later|\+)", text):
            checked += 1
            if match.group(1) != minimum.group(1):
                failures.append(
                    f"{relative(doc)}:{line_number_at(text, match.start())} says {match.group(0)!r} but "
                    f"pyproject.toml requires-python is {declared.group(1)!r}"
                )
    if not checked:
        pytest.skip("no Python version requirement stated in the documentation")
    assert not failures, "\n".join(failures)


def test_managed_state_filenames_are_documented(tmp_path):
    """Every file RuntimePaths creates is named in README.md and QUICK_REFERENCE.md."""
    paths = lifecycle.RuntimePaths.resolve(tmp_path)
    names = {
        value.name
        for field, value in vars(paths).items()
        if isinstance(value, Path) and value != paths.root and not field.startswith("_")
    }
    assert names, "RuntimePaths exposes no managed files; this check needs rewriting"
    readme_text = read(README)
    quick_text = read(QUICK_REFERENCE)
    missing = sorted(name for name in names if name not in readme_text or name not in quick_text)
    assert not missing, (
        "RuntimePaths manages files that README.md or QUICK_REFERENCE.md does not mention: " + ", ".join(missing)
    )


def test_status_json_keys_are_documented(tmp_path):
    """Every key `status --output json` emits is named in README.md.

    That key set is the automation contract: it is stable across states, so a caller
    reads a field without checking first and gets null rather than a KeyError. A key
    shipped without documentation is a promise nobody made deliberately. Deriving the
    truth from `runtime_status` rather than a hardcoded list means adding a field to
    the payload fails this test until someone writes it down.
    """
    emitted = set(lifecycle.runtime_status(lifecycle.RuntimePaths.resolve(tmp_path)))
    assert emitted, "runtime_status produced no keys; this check needs rewriting"

    documented = set(re.findall(r"`([a-z_]+)`", read(README)))
    missing = sorted(key for key in emitted if key not in documented)

    assert not missing, (
        "status --output json emits keys README.md does not document: "
        + ", ".join(missing)
        + ". Add them to the key table, or stop emitting them."
    )


def test_documented_default_state_directory_matches_the_code(monkeypatch):
    """The per-platform state directory in README.md is the one the code resolves."""
    monkeypatch.delenv("RIGOUT_STATE_DIR", raising=False)
    labels = {"win32": "windows", "darwin": "macos"}
    label = labels.get(sys.platform, "linux")
    text = read(README)
    documented: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*[-*+]\s*(\w+):\s*(.+)$", line)
        if match and match.group(1).lower() == label:
            documented.extend(re.findall(r"`([^`]+)`", match.group(2)))
    if not documented:
        pytest.skip(f"README.md documents no default state directory for {label}")
    actual = lifecycle.default_state_dir()
    expanded = []
    for candidate in documented:
        text_path = candidate.replace("%LOCALAPPDATA%", "$LOCALAPPDATA")
        resolved = Path(os.path.expandvars(text_path)).expanduser()
        expanded.append(resolved)
        if "$" not in str(resolved) and resolved.resolve() == actual:
            return
    pytest.fail(
        f"README.md documents the {label} state directory as {documented}, resolving to "
        f"{[str(path) for path in expanded]}, but lifecycle.default_state_dir() returns {actual}"
    )


def test_documented_rate_limit_matches_the_code():
    """ "60 requests per minute" tracks the constant the request path actually uses."""
    default = getattr(ssh_manager, "DEFAULT_MAX_REQUESTS_PER_MINUTE", None)
    if not isinstance(default, int):
        pytest.skip("ssh_manager exposes no DEFAULT_MAX_REQUESTS_PER_MINUTE constant to compare against")
    failures = []
    checked = 0
    for doc in (README, QUICK_REFERENCE, SECURITY, REQUEST_PATH):
        text = read(doc)
        for match in re.finditer(r"(\d+)((?: \w+){0,3}) per minute", text):
            checked += 1
            if int(match.group(1)) != default:
                failures.append(
                    f"{relative(doc)}:{line_number_at(text, match.start())} says {match.group(0)!r} but "
                    f"ssh_manager.DEFAULT_MAX_REQUESTS_PER_MINUTE is {default}"
                )
    if not checked:
        pytest.skip("no per-minute rate limit claim found in the documentation")
    assert not failures, "\n".join(failures)


def test_documented_setup_token_lifetime_matches_the_code():
    """ "expires after 15 minutes" tracks DEFAULT_SETUP_TOKEN_TTL_SECONDS."""
    ttl = getattr(mcp_http_server, "DEFAULT_SETUP_TOKEN_TTL_SECONDS", None)
    if not isinstance(ttl, int | float):
        pytest.skip("mcp_http_server exposes no DEFAULT_SETUP_TOKEN_TTL_SECONDS to compare against")
    expected = ttl / 60
    patterns = (
        r"[Ss]etup token[^.]{0,120}?(\d+)\s+minute",
        r"(\d+)\s+minutes? after server startup",
        r"TTL is (\d+)\s+minute",
    )
    failures = []
    checked = 0
    for doc in (README, QUICK_REFERENCE, SECURITY, REQUEST_PATH):
        text = read(doc)
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                checked += 1
                if float(match.group(1)) != expected:
                    failures.append(
                        f"{relative(doc)}:{line_number_at(text, match.start())} claims a "
                        f"{match.group(1)} minute setup token lifetime but "
                        f"DEFAULT_SETUP_TOKEN_TTL_SECONDS is {ttl} ({expected:g} minutes)"
                    )
    if not checked:
        pytest.skip("no setup token lifetime claim found in the documentation")
    assert not failures, "\n".join(sorted(set(failures)))


def test_documented_activity_line_bounds_match_the_tool_schema():
    """ "1-200 recent activity lines" is the advertised schema, not a wish."""
    schema = next(
        (tool.inputSchema for tool in advertised_tools() if tool.name == "get_server_activity"),
        None,
    )
    if not schema:
        pytest.skip("get_server_activity is not advertised")
    lines_schema = schema.get("properties", {}).get("lines", {})
    minimum, maximum = lines_schema.get("minimum"), lines_schema.get("maximum")
    if minimum is None or maximum is None:
        pytest.skip("the get_server_activity schema declares no bounds for `lines`")
    failures = []
    checked = 0
    for doc in (README, QUICK_REFERENCE):
        text = read(doc)
        for match in re.finditer(r"(\d+)-(\d+) (?:recent )?activity lines", text):
            checked += 1
            if (int(match.group(1)), int(match.group(2))) != (minimum, maximum):
                failures.append(
                    f"{relative(doc)}:{line_number_at(text, match.start())} says {match.group(0)!r} but the "
                    f"advertised schema allows {minimum}-{maximum}"
                )
    if not checked:
        pytest.skip("no activity line bounds claim found in the documentation")
    assert not failures, "\n".join(failures)
