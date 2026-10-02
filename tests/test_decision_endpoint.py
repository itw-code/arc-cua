"""System-2 decision endpoint resolution, persistence, and bridge wiring.

These cover the failure modes that made the Colab backend awkward to reuse across
sessions, each of which is invisible without an explicit assertion:

- A URL pointing at the SGLang port (8000) instead of the gateway port (8001).
  Both are live in the Colab notebook; only 8001 serves /v1/systemone, so the
  wrong port silently 404s on the first escalated decision.
- A pinned URL left over from a previous runtime, whose quick-tunnel hostname no
  longer exists. Trusting the pin would send every decision into a dead tunnel.
- A gateway that answers HTTP 200 while SGLang underneath it has disconnected:
  healthy by status code, useless in practice.
- Precedence between explicit, env, pinned, and loopback candidates.
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from arc_cua.decision.decision_endpoint import (
    DEFAULT_GATEWAY_PORT,
    DecisionEndpointResolver,
    access_headers,
    probe_health,
    to_base_url,
)
from arc_cua.decision.index_bridge import HybridDecisionClient


class _GatewayHandler(BaseHTTPRequestHandler):
    """Minimal stand-in for the Colab gateway (Daemon B, port 8001)."""

    sglang_connected = True
    status = "ok"

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, {
                "status": type(self).status,
                "docling_ready": True,
                "sglang_connected": type(self).sglang_connected,
            })
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        answers = {}
        for name, question in body.get("questions", {}).items():
            options = question.get("options", ["a", "b"])
            answers[name] = {
                "choice": options[0],
                "confidence": 0.99,
                "probabilities": {o: (0.99 if i == 0 else 0.005) for i, o in enumerate(options)},
            }
        self._reply(200, {"answers": answers, "usage": {"prompt_tokens": 12, "completion_tokens": 0}})

    def log_message(self, *args):
        pass


@pytest.fixture
def gateway():
    """Run a real HTTP gateway on an ephemeral port and yield its base URL."""
    _GatewayHandler.status = "ok"
    _GatewayHandler.sglang_connected = True
    server = HTTPServer(("127.0.0.1", 0), _GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


ACCESS_CLIENT_ID = "test-client-id.access"
ACCESS_CLIENT_SECRET = "test-client-secret"


class _AccessGatewayHandler(_GatewayHandler):
    """Gateway behind a Cloudflare Access service-token policy.

    Mirrors the edge: it 403s with a redirect to the Access login host when the
    service-token headers are absent, and it guards /v1/systemone as well as
    /health. That second part is the reason the bridge must send the headers on
    the decision POST, not only on the probe.
    """

    def _authorized(self):
        return (
            self.headers.get("CF-Access-Client-Id") == ACCESS_CLIENT_ID
            and self.headers.get("CF-Access-Client-Secret") == ACCESS_CLIENT_SECRET
        )

    def _deny(self):
        self.send_response(403)
        self.send_header("Content-Type", "text/html")
        self.send_header("Location", "https://ihsanwanda.cloudflareaccess.com/cdn-cgi/access/login/x")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _signature_banned(self):
        """The real edge 1010s Python's default UA before reading any token."""
        if (self.headers.get("User-Agent") or "").startswith("Python-urllib"):
            body = b"error code: 1010"
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        return False

    def do_GET(self):
        if self._signature_banned():
            return
        if not self._authorized():
            self._deny()
            return
        super().do_GET()

    def do_POST(self):
        if self._signature_banned():
            return
        if not self._authorized():
            self._deny()
            return
        super().do_POST()


@pytest.fixture
def access_gateway(monkeypatch):
    """Run an Access-guarded gateway and set valid credentials in the env."""
    _GatewayHandler.status = "ok"
    _GatewayHandler.sglang_connected = True
    server = HTTPServer(("127.0.0.1", 0), _AccessGatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", ACCESS_CLIENT_ID)
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", ACCESS_CLIENT_SECRET)
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


class TestURLNormalization:
    """The notebook prints both bare bases and full paths; both get pasted."""

    @pytest.mark.parametrize("raw,expected", [
        ("https://x.trycloudflare.com/v1/systemone", "https://x.trycloudflare.com"),
        ("https://x.trycloudflare.com/v1/decisions", "https://x.trycloudflare.com"),
        ("https://x.trycloudflare.com/health", "https://x.trycloudflare.com"),
        ("https://x.trycloudflare.com/", "https://x.trycloudflare.com"),
        ("https://x.trycloudflare.com", "https://x.trycloudflare.com"),
        ("  https://x.trycloudflare.com/v1/systemone  ", "https://x.trycloudflare.com"),
    ])
    def test_to_base_url(self, raw, expected):
        assert to_base_url(raw) == expected

    def test_bridge_paths_derive_from_base(self, gateway):
        spec = DecisionEndpointResolver(endpoint=gateway).discover()
        assert spec.systemone_url == f"{gateway}/v1/systemone"
        assert spec.decisions_url == f"{gateway}/v1/decisions"


class TestPortContract:
    """Regression: the bridge must target the gateway port, never the SGLang port."""

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch, tmp_path):
        """Clear the pin and env so a developer's real config cannot leak in."""
        monkeypatch.setenv("ARC_DECISION_PIN_PATH", str(tmp_path / "absent.json"))
        for var in ("SGLANG_DECISION_ENDPOINT", "SGLANG_BASE_URL", "SGLANG_GATEWAY_PORT"):
            monkeypatch.delenv(var, raising=False)

    def test_default_is_gateway_port_8001(self):
        assert DEFAULT_GATEWAY_PORT == 8001

    def test_client_defaults_to_8001_not_8000(self):
        client = HybridDecisionClient(prefer_local=False, mock_laya=object())
        assert client.colab_endpoint == "http://127.0.0.1:8001/v1/systemone"
        assert client.colab_decisions_url == "http://127.0.0.1:8001/v1/decisions"
        assert ":8000/" not in client.colab_endpoint

    def test_pasted_sglang_url_is_redirected_to_gateway_port(self, monkeypatch):
        """An operator pasting the SGLang URL must not silently 404 on escalation.

        SGLang on 8000 has no /v1/systemone, so when the only configured
        candidate is on the SGLang port it is remapped to the gateway port.
        """
        monkeypatch.setenv("SGLANG_DECISION_ENDPOINT", "http://127.0.0.1:8000/v1/systemone")
        monkeypatch.setenv("SGLANG_GATEWAY_HOST", "127.0.0.1")
        monkeypatch.setenv("SGLANG_GATEWAY_PORT", str(DEFAULT_GATEWAY_PORT))
        client = HybridDecisionClient(prefer_local=False, mock_laya=object())
        assert client.colab_endpoint == "http://127.0.0.1:8001/v1/systemone"


class TestHealthProbe:
    def test_healthy_gateway(self, gateway):
        result = probe_health(gateway)
        assert result["healthy"] is True
        assert result["body"]["sglang_connected"] is True

    def test_200_with_sglang_down_is_unhealthy(self, gateway):
        _GatewayHandler.sglang_connected = False
        result = probe_health(gateway)
        assert result["status"] == 200
        assert result["healthy"] is False
        assert "sglang_connected" in result["error"]

    def test_200_with_non_ok_status_is_unhealthy(self, gateway):
        _GatewayHandler.status = "degraded"
        result = probe_health(gateway)
        assert result["healthy"] is False

    def test_wrong_service_on_port_is_unhealthy(self, tmp_path):
        result = probe_health("http://127.0.0.1:1", timeout=0.5)
        assert result["healthy"] is False


class TestCascadePrecedence:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch, tmp_path):
        for var in ("SGLANG_DECISION_ENDPOINT", "SGLANG_BASE_URL",
                    "SGLANG_GATEWAY_HOST", "SGLANG_GATEWAY_PORT"):
            monkeypatch.delenv(var, raising=False)
        self.pin = tmp_path / "endpoint.json"

    def test_explicit_beats_env(self, gateway, monkeypatch):
        monkeypatch.setenv("SGLANG_DECISION_ENDPOINT", "http://127.0.0.1:1")
        spec = DecisionEndpointResolver(endpoint=gateway, pin_path=self.pin).discover()
        assert spec.base_url == gateway
        assert spec.source == "explicit"

    def test_env_beats_pinned(self, gateway, monkeypatch):
        self.pin.write_text(json.dumps({"base_url": "http://127.0.0.1:1"}), encoding="utf-8")
        monkeypatch.setenv("SGLANG_DECISION_ENDPOINT", gateway)
        spec = DecisionEndpointResolver(pin_path=self.pin).discover()
        assert spec.base_url == gateway
        assert spec.source == "env:SGLANG_DECISION_ENDPOINT"

    def test_pinned_used_when_nothing_else_set(self, gateway):
        self.pin.write_text(json.dumps({"base_url": gateway}), encoding="utf-8")
        spec = DecisionEndpointResolver(pin_path=self.pin).discover()
        assert spec.base_url == gateway
        assert spec.source == "pinned"
        assert spec.is_healthy is True

    def test_base_url_env_is_honoured(self, gateway, monkeypatch):
        monkeypatch.setenv("SGLANG_BASE_URL", gateway)
        spec = DecisionEndpointResolver(pin_path=self.pin).discover()
        assert spec.base_url == gateway
        assert spec.source == "env:SGLANG_BASE_URL"

    def test_stale_pin_is_rejected_not_trusted(self, gateway):
        """A dead pinned URL must not be returned as a usable endpoint.

        The quick tunnel reissues its hostname each runtime, so a pin left from
        yesterday points at a domain that no longer resolves.
        """
        self.pin.write_text(json.dumps({"base_url": "http://127.0.0.1:1"}), encoding="utf-8")
        spec = DecisionEndpointResolver(pin_path=self.pin, timeout=0.5).discover()
        assert spec.is_healthy is False
        assert spec.source == "unverified"

    def test_corrupt_pin_falls_through_to_loopback(self, gateway, monkeypatch):
        self.pin.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("SGLANG_DECISION_ENDPOINT", gateway)
        spec = DecisionEndpointResolver(pin_path=self.pin).discover()
        assert spec.base_url == gateway

    def test_env_wins_over_stale_pin(self, gateway, monkeypatch):
        """The Colab restart case: dead pin on disk, fresh runtime URL in env."""
        self.pin.write_text(json.dumps({"base_url": "http://127.0.0.1:1"}), encoding="utf-8")
        monkeypatch.setenv("SGLANG_DECISION_ENDPOINT", gateway)
        spec = DecisionEndpointResolver(pin_path=self.pin, timeout=0.5).discover()
        assert spec.base_url == gateway
        assert spec.is_healthy is True


class TestPinPersistence:
    def test_pin_then_discover_survives_restart(self, gateway, tmp_path):
        pin = tmp_path / "endpoint.json"
        resolver = DecisionEndpointResolver(pin_path=pin)
        resolver.pin(gateway)

        payload = json.loads(pin.read_text(encoding="utf-8"))
        assert payload["base_url"] == gateway
        assert payload["systemone_url"] == f"{gateway}/v1/systemone"
        assert payload["health"]["healthy"] is True
        assert payload["pinned_at"]

        # A brand new resolver, as in a fresh process or next day.
        revived = DecisionEndpointResolver(pin_path=pin).discover()
        assert revived.base_url == gateway
        assert revived.source == "pinned"
        assert revived.is_healthy is True

    def test_pin_refuses_unhealthy_endpoint(self, tmp_path):
        pin = tmp_path / "endpoint.json"
        resolver = DecisionEndpointResolver(pin_path=pin, timeout=0.5)
        with pytest.raises(ConnectionError):
            resolver.pin("http://127.0.0.1:1")
        assert not pin.exists()

    def test_pin_refuses_gateway_with_sglang_down(self, gateway, tmp_path):
        pin = tmp_path / "endpoint.json"
        _GatewayHandler.sglang_connected = False
        with pytest.raises(ConnectionError):
            DecisionEndpointResolver(pin_path=pin).pin(gateway)
        assert not pin.exists()

    def test_pin_accepts_full_path_url(self, gateway, tmp_path):
        pin = tmp_path / "endpoint.json"
        DecisionEndpointResolver(pin_path=pin).pin(f"{gateway}/v1/systemone")
        assert json.loads(pin.read_text(encoding="utf-8"))["base_url"] == gateway

    def test_clear_pin(self, gateway, tmp_path):
        pin = tmp_path / "endpoint.json"
        resolver = DecisionEndpointResolver(pin_path=pin)
        resolver.pin(gateway)
        assert resolver.clear_pin() is True
        assert resolver.clear_pin() is False
        assert not pin.exists()


class TestBridgeIntegration:
    """The client must actually reach System-2 through the resolved endpoint."""


    @pytest.fixture(autouse=True)
    def _isolate_pin(self, monkeypatch, tmp_path):
        """Never inherit the operator's real ~/.omp pin into a bridge test.

        Without this, a pin written by an earlier session silently outranks the
        loopback port the test set, and the suite's outcome would depend on
        whatever the developer last ran.
        """
        monkeypatch.setenv("ARC_DECISION_PIN_PATH", str(tmp_path / "absent.json"))

    def test_escalation_uses_resolved_endpoint(self, gateway, monkeypatch, tmp_path):
        monkeypatch.setenv("SGLANG_DECISION_ENDPOINT", gateway)
        client = HybridDecisionClient(prefer_local=False, mock_laya=object())
        result = client._decide_choice("page 1 text", "field", "Patient name", ["Alice", "Bob"])
        assert result["tier"].value == "remote_sglang"
        assert result["choice"] == "Alice"
        assert result["confidence"] > 0.9

    def test_dead_endpoint_degrades_to_heuristic(self, monkeypatch):
        """A cold Colab must not raise: the bridge falls through to the rule tier."""
        monkeypatch.setenv("SGLANG_DECISION_ENDPOINT", "http://127.0.0.1:1")
        client = HybridDecisionClient(prefer_local=False, mock_laya=object())
        result = client._decide_choice("page", "field", "Patient name", ["Alice", "Bob"])
        assert result["tier"].value == "heuristic"
        assert result["choice"] == "Alice"
        assert result["confidence"] == 0.50

    def test_client_constructor_survives_no_endpoint(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SGLANG_DECISION_ENDPOINT", raising=False)
        monkeypatch.delenv("SGLANG_BASE_URL", raising=False)
        monkeypatch.setenv("SGLANG_GATEWAY_PORT", "1")
        client = HybridDecisionClient(prefer_local=False, mock_laya=object())
        assert client.colab_endpoint.startswith("http://127.0.0.1:1/")


class TestCloudflareAccess:
    """A named tunnel publishes a guessable hostname, so the edge must authenticate.

    The critical property is that an Access denial is reported as a *credentials*
    problem, not a dead-runtime problem: the operator's remedy is completely
    different, and a runtime that is serving happily behind the edge must not be
    reported as down.
    """

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ARC_DECISION_PIN_PATH", str(tmp_path / "absent.json"))
        monkeypatch.delenv("SGLANG_DECISION_ENDPOINT", raising=False)
        monkeypatch.delenv("SGLANG_BASE_URL", raising=False)
        monkeypatch.setenv("SGLANG_GATEWAY_PORT", "1")

    def test_valid_credentials_pass_the_probe(self, access_gateway):
        result = probe_health(access_gateway)
        assert result["healthy"] is True
        assert result["access_denied"] is False

    def test_missing_credentials_report_access_denial(self, access_gateway, monkeypatch):
        monkeypatch.delenv("CF_ACCESS_CLIENT_ID", raising=False)
        monkeypatch.delenv("CF_ACCESS_CLIENT_SECRET", raising=False)
        result = probe_health(access_gateway)
        assert result["healthy"] is False
        assert result["status"] == 403
        assert result["access_denied"] is True
        assert "CF_ACCESS_CLIENT_ID" in result["error"]

    def test_wrong_credentials_report_access_denial(self, access_gateway, monkeypatch):
        monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "wrong")
        monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "wrong")
        result = probe_health(access_gateway)
        assert result["access_denied"] is True
        assert result["healthy"] is False

    def test_access_denied_survives_resolution(self, access_gateway, monkeypatch):
        """The verdict must reach the caller, not be flattened into 'unhealthy'."""
        monkeypatch.delenv("CF_ACCESS_CLIENT_ID", raising=False)
        monkeypatch.delenv("CF_ACCESS_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("SGLANG_DECISION_ENDPOINT", access_gateway)
        spec = DecisionEndpointResolver().discover()
        assert spec.is_healthy is False
        assert spec.health["access_denied"] is True
        assert "Access" in spec.health["error"]

    def test_dead_host_is_not_reported_as_access_denial(self, monkeypatch):
        """A refused connection must stay a runtime problem, or operators chase
        credentials that are already correct."""
        monkeypatch.setenv("SGLANG_DECISION_ENDPOINT", "http://127.0.0.1:1")
        spec = DecisionEndpointResolver(timeout=0.5).discover()
        assert spec.is_healthy is False
        assert spec.health["access_denied"] is False

    def test_bridge_sends_credentials_on_decision_post(self, access_gateway, monkeypatch):
        """A probe that authenticates but a POST that does not is a silent failure."""
        monkeypatch.setenv("SGLANG_DECISION_ENDPOINT", access_gateway)
        client = HybridDecisionClient(prefer_local=False, mock_laya=object())
        assert "CF-Access-Client-Id" in client.decision_request_headers
        assert "CF-Access-Client-Secret" in client.decision_request_headers
        result = client._decide_choice("letter", "field", "Patient name", ["Alice", "Bob"])
        assert result["tier"].value == "remote_sglang"
        assert result["choice"] == "Alice"

    def test_bridge_without_credentials_degrades(self, access_gateway, monkeypatch):
        monkeypatch.delenv("CF_ACCESS_CLIENT_ID", raising=False)
        monkeypatch.delenv("CF_ACCESS_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("SGLANG_DECISION_ENDPOINT", access_gateway)
        client = HybridDecisionClient(prefer_local=False, mock_laya=object())
        result = client._decide_choice("letter", "field", "Patient name", ["Alice", "Bob"])
        assert result["tier"].value == "heuristic"

    def test_pin_through_access_gateway(self, access_gateway, tmp_path):
        pin = tmp_path / "endpoint.json"
        DecisionEndpointResolver(pin_path=pin).pin(access_gateway)
        payload = json.loads(pin.read_text(encoding="utf-8"))
        assert payload["base_url"] == access_gateway
        assert payload["health"]["healthy"] is True

    def test_pin_refuses_access_denied_endpoint(self, access_gateway, tmp_path, monkeypatch):
        monkeypatch.delenv("CF_ACCESS_CLIENT_ID", raising=False)
        monkeypatch.delenv("CF_ACCESS_CLIENT_SECRET", raising=False)
        pin = tmp_path / "endpoint.json"
        with pytest.raises(ConnectionError):
            DecisionEndpointResolver(pin_path=pin).pin(access_gateway)
        assert not pin.exists()

    def test_signature_block_is_not_reported_as_access_denial(self, access_gateway):
        """Error 1010 fires before Access reads the token; calling it a denial
        sends the operator to rotate credentials that are already correct."""
        headers = {
            "CF-Access-Client-Id": ACCESS_CLIENT_ID,
            "CF-Access-Client-Secret": ACCESS_CLIENT_SECRET,
            "User-Agent": "Python-urllib/3.12",
        }
        result = probe_health(access_gateway, headers=headers)
        assert result["healthy"] is False
        assert result["access_denied"] is False
        assert "1010" in result["error"]
