import json

import pytest

from rigout import cli

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGOUT_STATE_DIR", str(tmp_path))
    return tmp_path


def test_token_is_kept_across_starts_until_reset():
    first = cli.load_token()
    assert cli.load_token() == first
    replaced = cli.load_token(reset=True)
    assert replaced != first
    assert cli.load_token() == replaced


def test_client_settings():
    claude = cli.client_config("claude", "https://h.example/mcp", "t0k")
    assert claude == 'claude mcp add --transport http rigout https://h.example/mcp --header "Authorization: Bearer t0k"'

    cursor = json.loads(cli.client_config("cursor", "https://h.example/mcp", "t0k"))
    assert cursor["mcpServers"]["rigout"] == {
        "url": "https://h.example/mcp",
        "headers": {"Authorization": "Bearer t0k"},
    }

    vscode = json.loads(cli.client_config("vscode", "https://h.example/mcp", "t0k"))
    assert vscode["servers"]["rigout"]["type"] == "http"


def test_bare_rigout_means_serve(monkeypatch):
    seen = {}

    def fake_serve(args, public_base=None):
        seen["args"] = args
        return 0

    monkeypatch.setattr(cli, "serve", fake_serve)
    assert cli.main(["--workspace", ".", "--port", "9999"]) == 0
    assert seen["args"].command == "serve"
    assert seen["args"].port == 9999


def test_url_prints_the_last_started_url(capsys):
    cli.save_url("https://h.example/mcp")
    token = cli.load_token()
    assert cli.main(["url", "--client", "raw"]) == 0
    out = capsys.readouterr().out
    assert "https://h.example/mcp" in out
    assert token in out


def test_url_before_any_start_says_what_to_do(capsys):
    assert cli.main(["url"]) == 1
    assert "Start rigout first" in capsys.readouterr().err
