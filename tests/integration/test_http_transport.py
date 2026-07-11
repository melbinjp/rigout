import json
import logging
from urllib.parse import parse_qs

import pytest
from starlette.testclient import TestClient

from rigout import __version__, mcp_http_server
from rigout.mcp_http_server import (
    DEFAULT_SETUP_TOKEN_TTL_SECONDS,
    RedactSetupTokenQueryMiddleware,
    create_app,
)


@pytest.mark.integration
class TestHTTPTransport:
    """Integration tests for the Streamable HTTP transport server"""

    @pytest.fixture
    def client(self):
        # Create app without writing the connection file during test setup
        app = create_app(connection_file=None)
        with TestClient(app) as client:
            yield client

    def test_root_endpoint(self, client):
        """Test GET / returns basic welcome info"""
        response = client.get("/")
        assert response.status_code == 200
        assert "AI Agent Hardware Access" in response.text
        assert "mcp" in response.text.lower()

    def test_health_endpoint(self, client):
        """Test GET /health returns OK status"""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["server"] == "enhanced-hardware-server"
        assert data["version"] == __version__

    def test_initialize_advertises_rigout_package_version(self, client):
        """HTTP initialization must not advertise the MCP SDK version."""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "version-test", "version": "1"},
                },
            },
            headers={"Accept": "application/json, text/event-stream"},
        )

        assert response.status_code == 200
        data_line = next(line for line in response.text.splitlines() if line.startswith("data: "))
        payload = json.loads(data_line.removeprefix("data: "))
        assert payload["result"]["serverInfo"]["version"] == __version__

    def test_connection_json_endpoint(self, client):
        """Test GET /connection.json returns correct metadata structure"""
        response = client.get("/connection.json")
        assert response.status_code == 200
        data = response.json()
        assert data["mcp_server_type"] == "hardware_access"
        assert data["connection_method"] == "mcp_streamable_http"
        assert data["server"] == {"name": "rigout", "version": __version__}
        assert "capabilities" in data
        assert "server_activity" in data["capabilities"]
        assert data["security"]["activity_access"]["tool"] == "get_server_activity"
        assert "hardware_info" in data

    def test_auth_token_is_advertised_and_required_for_mcp_path(self):
        """Public URL mode should be able to protect the MCP transport with bearer auth."""
        app = create_app(connection_file=None, auth_token="test-token")
        with TestClient(app) as client:
            unauthenticated_connection = client.get("/connection.json")
            assert unauthenticated_connection.status_code == 401
            assert unauthenticated_connection.headers["www-authenticate"] == "Bearer"
            assert unauthenticated_connection.headers["cache-control"] == "no-store"
            assert unauthenticated_connection.headers["pragma"] == "no-cache"

            connection_response = client.get("/connection.json", headers={"Authorization": "Bearer test-token"})
            assert connection_response.headers["cache-control"] == "no-store"
            assert connection_response.headers["pragma"] == "no-cache"
            connection = connection_response.json()
            assert connection["mcp"]["headers"]["Authorization"] == "Bearer test-token"
            assert connection["security"]["auth"] == "bearer"

            unauthenticated = client.post("/mcp", json={})
            assert unauthenticated.status_code == 401
            assert unauthenticated.headers["www-authenticate"] == "Bearer"
            assert unauthenticated.headers["cache-control"] == "no-store"
            assert unauthenticated.headers["pragma"] == "no-cache"

            authenticated = client.post("/mcp", json={}, headers={"Authorization": "Bearer test-token"})
            assert authenticated.status_code in {400, 406}

    def test_setup_token_can_fetch_public_connection(self):
        app = create_app(
            connection_file=None,
            public_url="https://public.example/mcp",
            setup_token="setup-secret",
            auth_token="test-token",
        )

        with TestClient(app) as client:
            assert client.get("/connection.json").status_code == 401
            wrong_setup_token = client.get("/connection.json?setup_token=wrong")
            assert wrong_setup_token.status_code == 401
            assert wrong_setup_token.headers["www-authenticate"] == "Bearer"
            assert wrong_setup_token.headers["cache-control"] == "no-store"
            assert wrong_setup_token.headers["pragma"] == "no-cache"

            setup_connection = client.get("/connection.json?setup_token=setup-secret")
            assert setup_connection.status_code == 200
            assert setup_connection.headers["cache-control"] == "no-store"
            assert setup_connection.headers["pragma"] == "no-cache"
            assert setup_connection.json()["mcp"]["url"] == "https://public.example/mcp"
            assert setup_connection.json()["mcp"]["headers"]["Authorization"] == "Bearer test-token"

            header_setup_connection = client.get("/connection.json", headers={"X-Setup-Token": "setup-secret"})
            assert header_setup_connection.status_code == 200
            assert header_setup_connection.json()["mcp"]["url"] == "https://public.example/mcp"

            bearer_connection = client.get("/connection.json", headers={"Authorization": "Bearer test-token"})
            assert bearer_connection.status_code == 200
            assert bearer_connection.json()["mcp"]["url"] == "https://public.example/mcp"
            assert bearer_connection.json()["mcp"]["headers"]["Authorization"] == "Bearer test-token"

    def test_setup_token_expires_but_bearer_auth_remains_valid(self, monkeypatch):
        now = [1_000.0]
        monkeypatch.setattr(mcp_http_server.time, "monotonic", lambda: now[0])
        app = create_app(
            connection_file=None,
            setup_token="setup-secret",
            auth_token="test-token",
        )

        with TestClient(app) as client:
            assert client.get("/connection.json?setup_token=setup-secret").status_code == 200

            now[0] += DEFAULT_SETUP_TOKEN_TTL_SECONDS
            expired_query = client.get("/connection.json?setup_token=setup-secret")
            expired_header = client.get("/connection.json", headers={"X-Setup-Token": "setup-secret"})
            assert expired_query.status_code == 401
            assert expired_header.status_code == 401

            bearer_connection = client.get("/connection.json", headers={"Authorization": "Bearer test-token"})
            assert bearer_connection.status_code == 200

    @pytest.mark.asyncio
    async def test_setup_token_is_redacted_before_access_logging(self):
        observed = {}
        scope = {
            "type": "http",
            "query_string": b"keep=visible&setup%5Ftoken=first-secret&setup_token=second-secret",
        }

        async def inner_app(inner_scope, _receive, send):
            observed["application"] = inner_scope["query_string"]
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            return {"type": "http.disconnect"}

        async def access_log_send(message):
            if message["type"] == "http.response.start":
                observed["access_log"] = scope["query_string"]

        middleware = RedactSetupTokenQueryMiddleware(inner_app)
        await middleware(scope, receive, access_log_send)

        assert b"first-secret" in observed["application"]
        assert b"second-secret" in observed["application"]
        assert b"secret" not in observed["access_log"]
        redacted_query = parse_qs(observed["access_log"].decode("ascii"))
        assert redacted_query["keep"] == ["visible"]
        assert redacted_query["setup_token"] == ["REDACTED", "REDACTED"]

    def test_malformed_request_has_concise_log_and_jsonrpc_error(self, caplog):
        headers = {"Accept": "application/json, text/event-stream"}
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1"},
            },
        }
        app = create_app(connection_file=None)

        with TestClient(app) as client:
            initialize_response = client.post("/mcp", json=initialize, headers=headers)
            session_headers = {
                **headers,
                "Mcp-Session-Id": initialize_response.headers["mcp-session-id"],
            }
            client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=session_headers,
            )

            caplog.clear()
            with caplog.at_level(logging.WARNING):
                logging.getLogger("mcp.shared.session").warning("Failed to validate request: named-logger-secret")
                malformed_response = client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "get_hardware_info",
                            "arguments": ["sensitive-marker"],
                        },
                    },
                    headers=session_headers,
                )

        assert malformed_response.status_code == 200
        assert '"code":-32602' in malformed_response.text
        assert '"message":"Invalid request parameters"' in malformed_response.text
        warning_text = "\n".join(record.getMessage() for record in caplog.records)
        assert "Rejected malformed MCP request: invalid request parameters" in warning_text
        assert "validation errors" not in warning_text
        assert "sensitive-marker" not in warning_text
        assert "named-logger-secret" not in warning_text
