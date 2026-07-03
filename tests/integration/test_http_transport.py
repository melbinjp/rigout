import pytest
from starlette.testclient import TestClient

from rigout.mcp_http_server import create_app


@pytest.mark.integration
class TestHTTPTransport:
    """Integration tests for the Streamable HTTP transport server"""

    @pytest.fixture
    def client(self):
        # Create app without writing the connection file during test setup
        app = create_app(connection_file=None)
        return TestClient(app)

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

    def test_connection_json_endpoint(self, client):
        """Test GET /connection.json returns correct metadata structure"""
        response = client.get("/connection.json")
        assert response.status_code == 200
        data = response.json()
        assert data["mcp_server_type"] == "hardware_access"
        assert data["connection_method"] == "mcp_streamable_http"
        assert "capabilities" in data
        assert "hardware_info" in data

    def test_auth_token_is_advertised_and_required_for_mcp_path(self):
        """Public URL mode should be able to protect the MCP transport with bearer auth."""
        app = create_app(connection_file=None, auth_token="test-token")
        with TestClient(app) as client:
            unauthenticated_connection = client.get("/connection.json")
            assert unauthenticated_connection.status_code == 401

            connection = client.get("/connection.json", headers={"Authorization": "Bearer test-token"}).json()
            assert connection["mcp"]["headers"]["Authorization"] == "Bearer test-token"
            assert connection["security"]["auth"] == "bearer"

            unauthenticated = client.post("/mcp", json={})
            assert unauthenticated.status_code == 401

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
            assert client.get("/connection.json?setup_token=wrong").status_code == 401

            setup_connection = client.get("/connection.json?setup_token=setup-secret")
            assert setup_connection.status_code == 200
            assert setup_connection.json()["mcp"]["url"] == "https://public.example/mcp"
            assert setup_connection.json()["mcp"]["headers"]["Authorization"] == "Bearer test-token"

            bearer_connection = client.get("/connection.json", headers={"Authorization": "Bearer test-token"})
            assert bearer_connection.status_code == 200
            assert bearer_connection.json()["mcp"]["url"] == "https://public.example/mcp"
            assert bearer_connection.json()["mcp"]["headers"]["Authorization"] == "Bearer test-token"
