import pytest
from starlette.testclient import TestClient

from rigout import __version__
from rigout.http import create_app
from rigout.tools import TOOL_NAMES, Workspace

pytestmark = pytest.mark.integration

HEADERS = {"Accept": "application/json, text/event-stream", "Authorization": "Bearer s3cret"}


@pytest.fixture
def client(tmp_path):
    app = create_app(Workspace(tmp_path), "s3cret", stateless=True, json_response=True)
    with TestClient(app) as test_client:
        yield test_client


def rpc(client, method, params=None):
    response = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers=HEADERS
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_health_is_open(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "name": "rigout", "version": __version__}


def test_mcp_refuses_a_missing_or_wrong_token(client):
    assert client.post("/mcp", json={}).status_code == 401
    wrong = client.post("/mcp", json={}, headers={**HEADERS, "Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    assert wrong.headers["www-authenticate"] == "Bearer"


def test_initialize_names_rigout_and_tells_the_agent_its_workspace(client, tmp_path):
    result = rpc(
        client,
        "initialize",
        {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    )
    assert result["serverInfo"]["name"] == "rigout"
    assert result["serverInfo"]["version"] == __version__
    assert str(tmp_path.resolve()) in result["instructions"]


def test_tools_list_and_a_call(client, tmp_path):
    names = [tool["name"] for tool in rpc(client, "tools/list")["tools"]]
    assert names == list(TOOL_NAMES)

    result = rpc(client, "tools/call", {"name": "write", "arguments": {"path": "hello.txt", "content": "hi"}})
    assert result["isError"] is False
    assert (tmp_path / "hello.txt").read_text() == "hi"
