# Spec: an unrecognised result shape must not read as success

Status: proposed 2026-09-22. Not implemented.
Found by: Low_Rush_8535, in a comment on the r/mcp post about the mcp 2.0.0 fix.

## The defect

`result_is_error` in `src/rigout/results.py` reads the error flag under either
name and falls back to `bool()`:

```python
flag = getattr(result, "isError", None)
if flag is None:
    flag = getattr(result, "is_error", None)
return bool(flag)
```

Hand it a result carrying neither name and both lookups return `None`,
`bool(None)` is `False`, and an error is reported as a success. The wrapper
survives the rename that already happened and fails silently on the next one,
which is the same class of bug the post was about.

`build_result` treats the same unknown shape the other way. It checks
`CallToolResult.model_fields` and would raise if the name were gone. So one
half of the wrapper fails loud and the other fails silent on identical input,
and nothing records which was intended.

No test covers a result with neither field, which is why this survived a change
that was otherwise checked on both majors in isolated venvs.

## Requirement

- **UR-1** When `result_is_error` recognises neither `isError` nor `is_error`
  on a result, it raises rather than returning a value. Not knowing whether a
  call failed is not the same as it having succeeded, and only the raise makes
  the difference visible.
- **UR-2** A result that carries one of the two names keeps today's behaviour
  exactly, including a falsy flag meaning success.
- **UR-3** The failure names the attributes it looked for, so the next rename
  is a one-line read of the traceback rather than an investigation.

## Tasks

1. Distinguish absent from falsy in `result_is_error`, using a sentinel rather
   than `None`, since `None` is also what a present-but-unset flag returns.
2. Raise on absent, with both attribute names in the message.
3. Tests: a result with `isError` true and false; the same for `is_error`; and
   an object carrying neither, which must raise rather than return `False`.
4. Check whether any caller relies on the current fail-open behaviour before
   changing it. The tests in `tests/unit/test_tools.py` only pass real results.

## Not in scope

Guessing the next name. The point is to fail where a human can see it, not to
keep working through a rename nobody has read yet.
