"""System-2 Decision Endpoint Discovery and Persistence.

Resolves where the System-2 cortex lives without copy-pasting a fresh URL out of
a Colab cell on every session.

Why this module exists: the Colab notebook (notebooks/sglang_decision_server.ipynb)
exposes the gateway through a Cloudflare quick tunnel, which hands out a *new
random* `*.trycloudflare.com` hostname every time the runtime starts. The old flow
was "run all cells, read the printed URL, `export SGLANG_DECISION_ENDPOINT=...` by
hand". That manual step is the only thing standing between restarting the notebook
and using the bridge, and it is the step that silently breaks.

This resolver keeps a last-known-good endpoint pinned in the user home, validates
it with a real health probe, and re-validates on every construction, so a stale
pinned URL from a dead runtime is detected instead of being trusted.

Cascade, mirroring CDPDiscovery.discover() in cdp_discovery.py:
  1. Explicit argument (constructor or CLI --endpoint).
  2. SGLANG_DECISION_ENDPOINT / SGLANG_BASE_URL environment variables.
  3. Pinned last-known-good endpoint (~/.omp/decision-endpoint.json).
  4. Local loopback gateway probe (http://127.0.0.1:8001).
  5. ConnectionError naming every candidate that was tried.

"Healthy" means the gateway answers GET /health with HTTP 200 *and* reports
sglang_connected true. The gateway can be up while SGLang underneath it has died,
so a 200 alone is not enough; see `probe_health`.

Port contract (this is the bug the resolver exists to stop repeating): the
notebook runs SGLang on 8000 and the gateway that owns /v1/systemone and
/v1/decisions on 8001. Only 8001 serves the bridge API. A URL of
http://127.0.0.1:8000/v1/systemone is wrong and 404s, because SGLang's own
/v1/score and /v1/chat/completions are not the bridge contract.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("arc_cua.decision_endpoint")

# Gateway port in notebooks/sglang_decision_server.ipynb (Daemon B).
DEFAULT_GATEWAY_PORT = 8001
# SGLang port in the same notebook (Daemon A). Exposed for diagnostics only:
# it does not serve /v1/systemone.
DEFAULT_SGLANG_PORT = 8000

# The gateway health contract: sglang_connected must be true, otherwise the
# gateway answers 200 while every bridge call will fail on the SGLang hop.
HEALTH_PATH = "/health"
SYSTEMONE_PATH = "/v1/systemone"
DECISIONS_PATH = "/v1/decisions"

USER_DIR = Path.home() / ".omp"
DEFAULT_PIN_PATH = USER_DIR / "decision-endpoint.json"


def _strip_trailing_slash(url: str) -> str:
    return url.strip().rstrip("/")


def to_base_url(url: str) -> str:
    """Reduce a bridge URL to its origin, tolerating a pasted /v1/systemone path.

    The notebook prints both `{base}/v1/systemone` and a bare base, and both get
    pasted into SGLANG_DECISION_ENDPOINT depending on which line the user read.
    """
    base = _strip_trailing_slash(url)
    for suffix in (SYSTEMONE_PATH, DECISIONS_PATH, HEALTH_PATH):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


# Cloudflare Access service-token credentials. A named tunnel publishes a stable,
# guessable hostname, so the edge must authenticate the caller: otherwise anyone
# who learns arc.<domain> inherits someone else's Colab GPU hours. The edge
# validates these headers and never forwards the request without them.
ACCESS_CLIENT_ID_ENV = "CF_ACCESS_CLIENT_ID"
ACCESS_CLIENT_SECRET_ENV = "CF_ACCESS_CLIENT_SECRET"

# Cloudflare's edge rejects Python's default "Python-urllib/x.y" signature with
# error 1010 before Access even evaluates the token, so every request this
# client makes (probe and decision POST alike) must name itself.
USER_AGENT = "arc-cua"


def _is_signature_block(body: Optional[str]) -> bool:
    """Cloudflare error 1010: the client signature is banned, credentials unread."""
    return "error code: 1010" in (body or "").lower()


def access_headers() -> Dict[str, str]:
    """Build the Cloudflare Access service-token headers, if credentials exist.

    Returns an empty dict when unset, which is the correct behaviour for a quick
    tunnel (random hostname, no edge policy) and for loopback.
    """
    client_id = os.environ.get(ACCESS_CLIENT_ID_ENV, "").strip()
    client_secret = os.environ.get(ACCESS_CLIENT_SECRET_ENV, "").strip()
    if not client_id or not client_secret:
        return {}
    return {
        "CF-Access-Client-Id": client_id,
        "CF-Access-Client-Secret": client_secret,
    }


def _looks_like_access_denial(status: Optional[int], body: Optional[str], exc: urllib.error.HTTPError) -> bool:
    """Detect a Cloudflare Access challenge as distinct from a dead runtime.

    The distinction drives the operator's next action: a 403 means "supply
    credentials", while a timeout or 502 means "start the Colab runtime". Folding
    both into a generic error sends people to reboot a runtime that is alive.
    """
    haystack = (body or "").lower()
    if _is_signature_block(body):
        return False
    if status == 403:
        return True
    if exc is not None:
        location = (exc.headers.get("Location") or "").lower() if exc.headers else ""
        if "cloudflareaccess.com" in location:
            return True
    return any(marker in haystack for marker in (
        "cf-access", "cloudflare access", "access denied", "just a moment",
        "cf-mitigated",
    ))


def probe_health(base_url: str, timeout: float = 3.0, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Probe the gateway health endpoint, optionally through Cloudflare Access.

    Returns {"healthy", "status", "body", "error", "access_denied"}. A gateway
    that reports sglang_connected false is unhealthy even on HTTP 200, because
    the SGLang hop behind it is what actually answers /v1/systemone.

    `access_denied` is reported separately because it is a credentials problem,
    not a runtime problem: the Colab runtime may be alive behind the edge.
    """
    url = f"{_strip_trailing_slash(base_url)}{HEALTH_PATH}"
    request_headers = dict(headers) if headers else access_headers()
    request_headers.setdefault("User-Agent", USER_AGENT)
    request = urllib.request.Request(url, headers=request_headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        denied = _looks_like_access_denial(exc.code, raw, exc)
        if _is_signature_block(raw):
            detail = "Cloudflare blocked this client's signature (error 1010); not a credentials or runtime problem"
        elif denied:
            detail = "Cloudflare Access denied the request"
        else:
            detail = f"HTTP {exc.code}"
        if denied and not access_headers() and not headers:
            detail += " (no CF_ACCESS_CLIENT_ID/SECRET set)"
        return {
            "healthy": False,
            "status": exc.code,
            "body": None,
            "error": detail,
            "access_denied": denied,
        }
    except Exception as exc:  # URLError, timeout, bad host, TLS
        return {"healthy": False, "status": None, "body": None, "error": str(exc), "access_denied": False}

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        # Something answered 200 but it is not our gateway. Cloudflare's
        # interstitial is HTML and lands here rather than raising.
        denied = _looks_like_access_denial(status, raw, None)
        return {
            "healthy": False,
            "status": status,
            "body": None,
            "error": "Cloudflare Access challenge returned HTML" if denied else "health response is not JSON",
            "access_denied": denied,
        }

    if not isinstance(body, dict) or body.get("status") != "ok":
        return {
            "healthy": False,
            "status": status,
            "body": body,
            "error": f"gateway status={body.get('status')!r}",
            "access_denied": False,
        }
    if not body.get("sglang_connected"):
        return {
            "healthy": False,
            "status": status,
            "body": body,
            "error": "gateway up but sglang_connected is false",
            "access_denied": False,
        }
    return {"healthy": True, "status": status, "body": body, "error": None, "access_denied": False}


@dataclasses.dataclass(frozen=True)
class DecisionEndpointSpec:
    """Resolved descriptor for the System-2 decision gateway."""

    base_url: str
    source: str  # "explicit", "env", "pinned", "loopback", or "unverified"
    is_healthy: bool
    health: Dict[str, Any] = dataclasses.field(default_factory=dict)
    pinned_path: Optional[str] = None
    request_headers: Dict[str, str] = dataclasses.field(default_factory=dict)
    description: str = ""

    @property
    def systemone_url(self) -> str:
        return f"{_strip_trailing_slash(self.base_url)}{SYSTEMONE_PATH}"

    @property
    def decisions_url(self) -> str:
        return f"{_strip_trailing_slash(self.base_url)}{DECISIONS_PATH}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_url": self.base_url,
            "systemone_url": self.systemone_url,
            "decisions_url": self.decisions_url,
            "source": self.source,
            "is_healthy": self.is_healthy,
            "pinned_path": self.pinned_path,
            "description": self.description,
            "health": self.health,
        }


class DecisionEndpointResolver:
    """Discover the System-2 decision gateway across restarts and runtimes.

    The pinned cache is a cache, not a source of truth: every candidate is
    health-probed, and a pinned entry that no longer answers is reported as
    stale rather than returned. `pin()` re-records the current healthy endpoint
    so the next session starts from a known-good base.
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        timeout: float = 3.0,
        pin_path: Optional[Path] = None,
        verify: bool = True,
        headers: Optional[Dict[str, str]] = None,
    ):
        """Initialize the resolver.

        Args:
            endpoint: Explicit gateway URL; wins over every other source.
            timeout: Per-probe timeout in seconds.
            pin_path: Override for the pinned-cache file. Defaults to
                $ARC_DECISION_PIN_PATH, then ~/.omp/decision-endpoint.json. The env
                var exists so tests (and sandboxed runs) can be isolated from the
                operator's real pinned endpoint instead of inheriting it.
            verify: When False, skip health probes and trust the first candidate.
                Used by offline tests; production paths keep verification on.
            headers: Extra request headers, e.g. Cloudflare Access service-token
                credentials. Defaults to whatever $CF_ACCESS_CLIENT_ID/SECRET hold.
        """
        self.timeout = timeout
        self.verify = verify
        self.pin_path = Path(pin_path or os.environ.get("ARC_DECISION_PIN_PATH") or DEFAULT_PIN_PATH)
        self.headers = dict(headers) if headers else access_headers()
        self._explicit = endpoint

    # -- candidate collection -------------------------------------------------

    def _env_candidates(self) -> List[tuple]:
        out = []
        for var in ("SGLANG_DECISION_ENDPOINT", "SGLANG_BASE_URL"):
            val = os.environ.get(var)
            if val and val.strip():
                out.append((val.strip(), f"env:{var}"))
        return out

    def _pinned_candidate(self) -> Optional[tuple]:
        if not self.pin_path.exists():
            return None
        try:
            data = json.loads(self.pin_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Pinned decision endpoint unreadable: %s", exc)
            return None
        base = data.get("base_url")
        if not base or not isinstance(base, str):
            return None
        return (base, "pinned")

    def _loopback_candidate(self) -> tuple:
        host = os.environ.get("SGLANG_GATEWAY_HOST", "127.0.0.1")
        port = os.environ.get("SGLANG_GATEWAY_PORT", str(DEFAULT_GATEWAY_PORT))
        return (f"http://{host}:{port}", "loopback")

    def _remap_sglang_port(self, base: str) -> str:
        """Rewrite a loopback URL on the SGLang port to the gateway port.

        The notebook prints both URLs side by side, and only the gateway one owns
        /v1/systemone. Someone pasting the SGLang line should still get a working
        endpoint rather than a silent 404 on the first escalated decision, so the
        8000 -> 8001 swap is applied for loopback addresses only. A tunnel URL is
        left alone: its port is part of the hostname Cloudflare issued and
        remapping it would invent an address that does not exist.
        """
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(base)
        if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            return base
        if parsed.port != DEFAULT_SGLANG_PORT:
            return base
        logger.info(
            "Rewriting SGLang port %d to gateway port %d for %s: only the gateway serves /v1/systemone.",
            DEFAULT_SGLANG_PORT, DEFAULT_GATEWAY_PORT, base,
        )
        return urlunparse(parsed._replace(netloc=f"{parsed.hostname}:{DEFAULT_GATEWAY_PORT}"))

    # -- public API -----------------------------------------------------------

    def discover(self) -> DecisionEndpointSpec:
        """Resolve the decision gateway, health-checking each candidate in order.

        Raises:
            ConnectionError: if no candidate is healthy. The message names every
                source that was tried, so a stale pin and a cold Colab are
                distinguishable at a glance.
        """
        candidates: List[tuple] = []
        if self._explicit and self._explicit.strip():
            candidates.append((self._explicit.strip(), "explicit"))
        candidates.extend(self._env_candidates())
        pinned = self._pinned_candidate()
        if pinned:
            candidates.append(pinned)
        candidates.append(self._loopback_candidate())

        tried: List[str] = []
        seen: set = set()
        first_url: Optional[str] = None
        last_health: Dict[str, Any] = {}
        for raw_url, source in candidates:
            base = self._remap_sglang_port(to_base_url(raw_url))
            if base in seen:
                continue
            seen.add(base)
            tried.append(f"{base} ({source})")
            if first_url is None:
                first_url = base
            if not self.verify:
                return DecisionEndpointSpec(
                    base_url=base,
                    source=source,
                    is_healthy=False,
                    request_headers=dict(self.headers),
                    description=f"Resolved via {source} (health not verified)",
                )
            health = probe_health(base, self.timeout, self.headers)
            if health["healthy"]:
                return DecisionEndpointSpec(
                    base_url=base,
                    source=source,
                    is_healthy=True,
                    health=health,
                    pinned_path=str(self.pin_path) if self.pin_path.exists() else None,
                    request_headers=dict(self.headers),
                    description=f"Healthy gateway via {source}",
                )
            # A denial outranks later failures: the loopback probe runs last and
            # always fails, which would otherwise mask "Access said no" and send
            # the operator to restart a runtime that is already up.
            if health.get("access_denied") or not last_health.get("access_denied"):
                last_health = health
            logger.info("Decision endpoint candidate rejected: %s -- %s", base, health.get("error"))

        # Nothing healthy. Return the best-known address unverified so callers
        # get a real URL to complain about, rather than a bare exception string.
        # The last probe's verdict is carried over: an Access denial must stay an
        # Access denial here, or the operator is told to restart a runtime that
        # is already serving requests behind the edge.
        if first_url:
            denied = bool(last_health.get("access_denied"))
            if denied:
                detail = "Cloudflare Access denied the request (missing or wrong credentials)"
            else:
                detail = "no candidate passed the health probe"
            return DecisionEndpointSpec(
                base_url=first_url,
                source="unverified",
                is_healthy=False,
                health={"healthy": False, "error": detail, "access_denied": denied},
                request_headers=dict(self.headers),
                description="No gateway passed the health probe; resolved address is unverified",
            )
        raise ConnectionError(
            "No System-2 decision endpoint could be resolved. Tried: "
            + ("; ".join(tried) if tried else "(no candidates)")
            + ". Start the Colab notebook (Runtime > Run all), then run "
            "`arc-cua doctor --pin` to record its URL."
        )

    def request_headers(self) -> Dict[str, str]:
        """Headers the bridge must send on decision POSTs, not just on the probe.

        A Cloudflare Access policy guards the whole hostname, so /v1/systemone
        needs the same service-token headers as /health. Probing without them and
        posting with them (or the reverse) yields a gateway that looks healthy in
        `doctor` and then 403s on every real decision.
        """
        return dict(self.headers)

    def pin(self, base_url: Optional[str] = None) -> DecisionEndpointSpec:
        """Record a healthy endpoint as last-known-good for the next session.

        Args:
            base_url: Endpoint to pin. Defaults to resolving one.

        Raises:
            ConnectionError: if the endpoint to pin is not healthy. Pinning a dead
                URL is worse than pinning nothing: it would survive restart and
                fail silently at the first bridge call.
        """
        if base_url:
            spec = DecisionEndpointSpec(
                base_url=to_base_url(base_url),
                source="explicit",
                is_healthy=False,
                description="Explicit endpoint (health pending)",
            )
            health = probe_health(spec.base_url, self.timeout, self.headers)
            if not health["healthy"]:
                raise ConnectionError(
                    f"Refusing to pin {spec.base_url}: health check failed ({health.get('error')}). "
                    "Is the Colab runtime still up?"
                )
            spec = DecisionEndpointSpec(
                base_url=spec.base_url,
                source="explicit",
                is_healthy=True,
                health=health,
                description="Pinned explicit endpoint",
            )
        else:
            spec = self.discover()
            if not spec.is_healthy:
                raise ConnectionError(
                    f"Refusing to pin {spec.base_url}: it did not pass the health probe. "
                    "Start the Colab notebook, or pass --endpoint."
                )

        self.pin_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "base_url": spec.base_url,
            "systemone_url": spec.systemone_url,
            "decisions_url": spec.decisions_url,
            "pinned_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "health": spec.health,
        }
        self.pin_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("Pinned decision endpoint %s -> %s", spec.base_url, self.pin_path)
        return dataclasses.replace(spec, pinned_path=str(self.pin_path))

    def clear_pin(self) -> bool:
        """Delete the pinned cache. Returns True if a file was removed."""
        if self.pin_path.exists():
            self.pin_path.unlink()
            return True
        return False
