# Tests

Run all current tests:

```bash
python -m pytest -q
```

Run only unit tests:

```bash
python -m pytest tests/unit -q
```

Run HTTP transport integration tests:

```bash
python -m pytest tests/integration/test_http_transport.py -q
```

Skipped integration tests require external SSH/tunnel resources and are intentionally not part of the default local readiness signal.
