"""The Opengeni session proxy: what it forwards, and what it refuses.

The proxy is the only place the organization key is used, and the only place a
browser can reach Opengeni at all, so these tests are about the boundary rather
than about chat: an unknown route never reaches upstream, session
configuration never comes from the browser, the workspace in the path must be
the one this server resolved, and LEVH's own recall is what the model is given.

No network: the upstream calls are replaced with a recorder, which is also what
lets a test assert on the *request* the proxy would have sent.
"""

import os
import sys
import tempfile
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["EMBEDDER_MODE"] = "hash"

from server.core import opengeni
from server.core.memory_engine import MemoryEngine

WORKSPACE = "ws-1"
SUBJECT = "sub-1"


class Upstream:
    """Records what the proxy would send, and answers with canned payloads."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: dict[str, tuple[int, Any]] = {}
        self.default: tuple[int, Any] = (200, {"ok": True})

    def answer(self, path: str, payload: Any, status: int = 200) -> None:
        self.responses[path] = (status, payload)

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        external_id: str | None,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, Any, dict[str, str]]:
        self.calls.append(
            {"method": method, "path": path, "external_id": external_id, "params": params, "body": body}
        )
        status, payload = self.responses.get(path, self.default)
        return status, payload, {}

    def call(self, path: str) -> dict[str, Any]:
        return next(call for call in self.calls if call["path"] == path)


@pytest_asyncio.fixture
async def api(monkeypatch):
    """A throwaway store, a configured key, and a recording upstream."""
    import server.api as api_mod

    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    if api_mod._engine is not None:
        await api_mod._engine.shutdown()
    engine = MemoryEngine(db_path=db_path, embedder_mode="hash", short_term_max=50)
    await engine.initialize()
    api_mod._engine = engine
    api_mod._initialized = True

    upstream = Upstream()
    monkeypatch.setenv("OPENGENI_API_KEY", "ogk_test")
    monkeypatch.setenv("LEVH_OPENGENI_INSTALL_ID", "test-install")
    monkeypatch.delenv("LEVH_PUBLIC_DEMO", raising=False)
    monkeypatch.delenv("LEVH_OPENGENI_WORKSPACE_ID", raising=False)
    monkeypatch.setattr(opengeni, "request_json", upstream.request_json)

    async def workspace_id(_: str) -> str:
        return WORKSPACE

    async def subject_id(_: str) -> str:
        return SUBJECT

    monkeypatch.setattr(opengeni, "workspace_id", workspace_id)
    monkeypatch.setattr(opengeni, "subject_id", subject_id)

    async with AsyncClient(
        transport=ASGITransport(app=api_mod.app), base_url="http://test"
    ) as client:
        yield client, upstream, engine

    await engine.shutdown()
    if os.path.exists(db_path):
        os.unlink(db_path)


# ── The boundary ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_route_is_not_forwarded(api):
    client, upstream, _ = api
    response = await client.get("/api/opengeni/v1/admin/everything")
    assert response.status_code == 404
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_path_segments_are_validated_before_any_url_is_built(api):
    client, upstream, _ = api
    # ``..`` would otherwise be a path traversal into the upstream URL.
    response = await client.get("/api/opengeni/v1/workspaces/../sessions")
    assert response.status_code == 404
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_without_a_key_the_proxy_refuses_rather_than_calling(api, monkeypatch):
    client, upstream, _ = api
    monkeypatch.delenv("OPENGENI_API_KEY")
    response = await client.get(f"/api/opengeni/v1/workspaces/{WORKSPACE}")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "opengeni_not_configured"
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_public_demo_has_no_assistant(api, monkeypatch):
    client, upstream, _ = api
    monkeypatch.setenv("LEVH_PUBLIC_DEMO", "true")
    response = await client.get(f"/api/opengeni/v1/workspaces/{WORKSPACE}")
    assert response.status_code == 404
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_another_workspace_in_the_path_is_refused(api):
    client, upstream, _ = api
    response = await client.get("/api/opengeni/v1/workspaces/someone-else/sessions")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "workspace_not_allowed"
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_a_cross_site_mutation_is_refused(api):
    client, upstream, _ = api
    response = await client.post(
        f"/api/opengeni/v1/workspaces/{WORKSPACE}/sessions",
        json={"initialMessage": "hi"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_a_method_the_contract_does_not_use_is_refused(api):
    client, upstream, _ = api
    response = await client.request(
        "PUT", f"/api/opengeni/v1/workspaces/{WORKSPACE}/sessions/abc/control", json={}
    )
    assert response.status_code == 404
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_upstream_errors_are_forwarded_unchanged(api):
    client, upstream, _ = api
    upstream.answer(
        f"/v1/workspaces/{WORKSPACE}/usage/me",
        {"error": {"code": "allowance_exhausted", "message": "out of budget"}},
        status=429,
    )
    response = await client.get(f"/api/opengeni/v1/workspaces/{WORKSPACE}/usage/me?period=month")
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "allowance_exhausted"


# ── config/client ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_config_reports_this_host_not_the_upstream_deployment(api):
    client, upstream, _ = api
    upstream.answer(
        "/v1/config/client",
        {
            "models": [],
            "artifacts": {"editableLiveUrl": "wss://someone-else"},
            "voiceInput": {"available": True, "resumable": True},
            "fileUploads": {"enabled": True},
        },
    )
    response = await client.get("/api/opengeni/v1/config/client")
    body = response.json()
    assert response.status_code == 200
    assert upstream.call("/v1/config/client")["params"]["workspaceId"] == WORKSPACE
    assert body["apiContractRevision"] == opengeni.API_CONTRACT_REVISION
    assert body["workspaceId"] == WORKSPACE
    # Surfaces this proxy does not forward must not be advertised.
    assert body["artifacts"] is False
    assert body["sandboxFiles"] is False
    assert body["modelSelection"] is False
    assert body["voiceInput"]["available"] is False
    # The chunked-upload half of voice input is not forwarded either.
    assert "resumable" not in body["voiceInput"]
    assert body["fileUploads"]["enabled"] is False


# ── Sessions ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_allowlisted_reads_forward_to_upstream(api):
    """Every allowed read must reach upstream, not resolve to an un-awaited value."""
    client, upstream, _ = api
    session = f"/v1/workspaces/{WORKSPACE}/sessions/abc"
    upstream.answer(session, {"id": "abc"})
    upstream.answer(f"{session}/queue", {"items": []})
    reads = (
        f"/api/opengeni/v1/workspaces/{WORKSPACE}",
        f"/api/opengeni/v1/workspaces/{WORKSPACE}/model-catalog",
        f"/api/opengeni/v1/workspaces/{WORKSPACE}/usage/me?period=month",
        f"/api/opengeni/v1/workspaces/{WORKSPACE}/sessions/abc",
        f"/api/opengeni/v1/workspaces/{WORKSPACE}/sessions/abc/queue",
    )
    for read in reads:
        assert (await client.get(read)).status_code == 200, read
    for path in (
        f"/v1/workspaces/{WORKSPACE}",
        f"/v1/workspaces/{WORKSPACE}/model-catalog",
        f"/v1/workspaces/{WORKSPACE}/usage/me",
        session,
        f"{session}/queue",
    ):
        assert upstream.call(path)["external_id"] == "local"


@pytest.mark.asyncio
async def test_session_create_sets_agent_privacy_and_memory(api):
    client, upstream, engine = api
    await engine.store(
        content="Atlas uses PostgreSQL in production",
        importance=0.9,
        memory_type="episodic",
    )
    response = await client.post(
        f"/api/opengeni/v1/workspaces/{WORKSPACE}/sessions",
        json={"initialMessage": "Which database does Atlas use?", "idempotencyKey": "k1"},
    )
    assert response.status_code == 200
    body = upstream.call(f"/v1/workspaces/{WORKSPACE}/sessions")["body"]
    assert body["initialMessage"] == "Which database does Atlas use?"
    assert body["idempotencyKey"] == "k1"
    # Private chats over a personal store, decided by the server.
    assert body["visibility"] == "private"
    assert body["agentAccess"] == "session"
    assert body["agent"]["capabilities"] == "none"
    # LEVH's own recall is what the model is given, as context not prompt.
    assert "PostgreSQL" in body["modelContext"]
    assert body["modelContext"].startswith("The user's LEVH memory store")


@pytest.mark.asyncio
async def test_session_create_refuses_browser_chosen_configuration(api):
    client, upstream, _ = api
    response = await client.post(
        f"/api/opengeni/v1/workspaces/{WORKSPACE}/sessions",
        json={"initialMessage": "hi", "agent": {"capabilities": "all"}},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "create_field_not_allowed"
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_session_create_requires_a_message(api):
    client, upstream, _ = api
    response = await client.post(f"/api/opengeni/v1/workspaces/{WORKSPACE}/sessions", json={})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "initial_message_required"


@pytest.mark.asyncio
async def test_session_list_is_scoped_to_this_install(api):
    client, upstream, _ = api
    upstream.answer(
        f"/v1/workspaces/{WORKSPACE}/sessions",
        {"sessions": [], "pinned": [{"id": "someone-elses"}], "nextPage": None},
    )
    response = await client.get(
        f"/api/opengeni/v1/workspaces/{WORKSPACE}/sessions?view=page&createdBySubjectId=other"
    )
    assert response.status_code == 200
    params = upstream.call(f"/v1/workspaces/{WORKSPACE}/sessions")["params"]
    # The creator filter is the server's, not the browser's.
    assert params["createdBySubjectId"] == SUBJECT
    assert params["createdByKind"] == "subject"
    assert response.json()["pinned"] == []


@pytest.mark.asyncio
async def test_session_list_requires_the_paged_view(api):
    client, upstream, _ = api
    response = await client.get(f"/api/opengeni/v1/workspaces/{WORKSPACE}/sessions")
    assert response.status_code == 400
    assert upstream.calls == []


# ── Messages and events ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_message_carries_recalled_memory_as_context(api):
    client, upstream, engine = api
    await engine.store(
        content="The deploy branch is prod, not main",
        importance=0.9,
        memory_type="episodic",
    )
    session = f"/v1/workspaces/{WORKSPACE}/sessions/abc"
    response = await client.post(
        f"/api/opengeni{session}/events",
        json={"type": "user.message", "payload": {"text": "which branch do we deploy?"}},
    )
    assert response.status_code == 200
    payload = upstream.call(f"{session}/events")["body"]["payload"]
    assert "prod" in payload["modelContext"]


@pytest.mark.asyncio
async def test_the_browser_cannot_rotate_mcp_credentials(api):
    client, upstream, _ = api
    session = f"/v1/workspaces/{WORKSPACE}/sessions/abc"
    response = await client.post(
        f"/api/opengeni{session}/events",
        json={
            "type": "user.message",
            "payload": {"text": "hi", "mcpCredentialUpdates": [{"id": "x", "headers": {}}]},
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "credential_update_not_allowed"
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_only_the_three_contract_event_types_are_accepted(api):
    client, upstream, _ = api
    session = f"/v1/workspaces/{WORKSPACE}/sessions/abc"
    response = await client.post(
        f"/api/opengeni{session}/events", json={"type": "session.cancel", "payload": {}}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "event_not_allowed"
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_forensic_event_reads_are_refused(api):
    client, upstream, _ = api
    response = await client.get(
        f"/api/opengeni/v1/workspaces/{WORKSPACE}/sessions/abc/events?mode=forensic"
    )
    assert response.status_code == 403
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_control_is_pause_and_resume_only(api):
    client, upstream, _ = api
    session = f"/api/opengeni/v1/workspaces/{WORKSPACE}/sessions/abc"
    response = await client.post(f"{session}/control", json={"action": "cancel"})
    assert response.status_code == 403
    assert upstream.calls == []
    response = await client.post(f"{session}/control", json={"action": "pause"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_only_the_title_may_be_patched(api):
    client, upstream, _ = api
    session = f"/api/opengeni/v1/workspaces/{WORKSPACE}/sessions/abc"
    response = await client.patch(f"{session}", json={"title": "x", "visibility": "workspace"})
    assert response.status_code == 400
    assert upstream.calls == []


# ── The actor header ───────────────────────────────────────────────


def test_actor_header_encodes_the_same_way_the_sdk_does():
    encoded = opengeni.actor_header("a/../b", source="levh")
    # Percent-encoded JSON: the slash and the dots survive as data rather than
    # as path, which is what keeps the header one value.
    assert encoded.startswith("%7B%22mode%22%3A%22external%22")
    assert "/" not in encoded
    assert opengeni.actor_header("local") == opengeni.actor_header("local", source="levh")



# ── Core proxy helpers (diff-coverage guards) ──────────────────────


def test_core_proxy_helpers_cover_config_headers_decode_and_reset(monkeypatch):
    import httpx

    monkeypatch.setenv("LEVH_OPENGENI_API_KEY", "ogk_prefixed")
    monkeypatch.setenv("LEVH_OPENGENI_API_BASE_URL", "https://self-hosted.example/")

    assert opengeni.api_base_url() == "https://self-hosted.example"
    service_headers = opengeni.upstream_headers(None)
    assert service_headers["Authorization"] == "Bearer ogk_prefixed"
    assert "x-opengeni-external-actor" not in service_headers

    user_headers = opengeni.upstream_headers("local")
    assert user_headers["x-opengeni-external-actor"] == opengeni.actor_header("local")

    assert opengeni._decode(httpx.Response(204, content=b"")) is None
    assert opengeni._decode(httpx.Response(200, content=b"not-json")) is None

    error = opengeni.UpstreamError(429, {"error": "budget"})
    assert error.status == 429
    assert error.payload == {"error": "budget"}
    assert "429" in str(error)

    opengeni._workspace_cache["default"] = "ws-stale"
    opengeni._subject_cache["local"] = "sub-stale"
    opengeni.reset_caches()
    assert opengeni._workspace_cache == {}
    assert opengeni._subject_cache == {}


@pytest.mark.asyncio
async def test_request_json_uses_configured_base_headers_and_decodes(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 201
        content = b'{"ok": true}'
        headers = {"x-test": "yes"}

        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def request(self, method, url, *, params, json, headers):
            captured.update(
                method=method,
                url=url,
                params=params,
                json=json,
                headers=headers,
            )
            return FakeResponse()

    monkeypatch.setenv("LEVH_OPENGENI_API_KEY", "ogk_test")
    monkeypatch.setenv("LEVH_OPENGENI_API_BASE_URL", "https://proxy.example/")
    monkeypatch.setattr(opengeni.httpx, "AsyncClient", FakeClient)

    status, payload, headers = await opengeni.request_json(
        "POST",
        "/v1/example",
        external_id="local",
        params={"a": "b"},
        body={"hello": "world"},
        timeout=9.0,
    )

    assert status == 201
    assert payload == {"ok": True}
    assert headers["x-test"] == "yes"
    assert captured["timeout"] == 9.0
    assert captured["url"] == "https://proxy.example/v1/example"
    assert captured["headers"]["Authorization"] == "Bearer ogk_test"
    assert "x-opengeni-external-actor" in captured["headers"]


@pytest.mark.asyncio
async def test_memory_context_empty_and_budget_paths():
    class Result:
        def __init__(self, memories):
            self.memories = memories

    class Engine:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []

        async def recall(self, text, *, top_k, reinforce):
            self.calls.append((text, top_k, reinforce))
            return Result(self.responses.pop(0))

    empty = Engine([[]])
    assert await opengeni.memory_context(empty, "question") is None
    assert empty.calls == [("question", 6, False)]
    assert await opengeni.memory_context(empty, "   ") is None

    class Memory:
        def __init__(self, content, created_at="", tags=None):
            self.content = content
            self.created_at = created_at
            self.tags = tags or []

    long_second = "x" * (opengeni.CONTEXT_MAX_CHARS + 50)
    engine = Engine(
        [[
            Memory("first fact", "2026-10-05T12:00:00+00:00", ["peer", "prod"]),
            Memory(long_second),
        ]]
    )
    context = await opengeni.memory_context(engine, "what changed?")
    assert "[1] 2026-10-05 (peer, prod)" in context
    assert "first fact" in context
    assert long_second not in context



@pytest.mark.asyncio
async def test_browser_model_context_cannot_override_recalled_memory(api):
    client, upstream, engine = api
    await engine.store(
        content="Atlas production database is PostgreSQL",
        importance=0.9,
        memory_type="episodic",
    )
    session = f"/v1/workspaces/{WORKSPACE}/sessions/abc"

    response = await client.post(
        f"/api/opengeni{session}/events",
        json={
            "type": "user.message",
            "payload": {
                "text": "Which database does Atlas use?",
                "modelContext": "FORGED BROWSER CONTEXT",
            },
        },
    )

    assert response.status_code == 200
    context = upstream.call(f"{session}/events")["body"]["payload"]["modelContext"]
    assert "PostgreSQL" in context
    assert "FORGED BROWSER CONTEXT" not in context


@pytest.fixture
def identity_upstream(monkeypatch):
    """Exercise identity resolution itself, with no network or shared cache state."""
    upstream = Upstream()
    monkeypatch.setattr(opengeni, "_workspace_cache", {})
    monkeypatch.setattr(opengeni, "_subject_cache", {})
    monkeypatch.delenv("LEVH_OPENGENI_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("OPENGENI_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("STACKMEMORY_OPENGENI_WORKSPACE_ID", raising=False)
    monkeypatch.setattr(opengeni, "request_json", upstream.request_json)
    return upstream


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["workspace", "subject"])
async def test_identity_resolution_caches_per_external_identity(identity_upstream, kind):
    upstream = identity_upstream
    if kind == "workspace":
        resolve = opengeni.workspace_id
        path = "/v1/workspaces/external"
        payload = {"workspace": {"id": WORKSPACE}}
        expected = WORKSPACE
    else:
        resolve = opengeni.subject_id
        path = "/v1/access/me"
        payload = {"subjectId": SUBJECT}
        expected = SUBJECT
    upstream.answer(path, payload)

    assert await resolve("local") == expected
    assert await resolve("local") == expected
    assert len(upstream.calls) == 1
    call = upstream.calls[0]
    if kind == "workspace":
        assert call["method"] == "PUT"
        assert call["external_id"] is None
        assert call["body"] == {
            "externalSource": opengeni.identity_source(),
            "externalId": "local",
            "name": "LEVH",
        }
    else:
        assert call["method"] == "GET"
        assert call["external_id"] == "local"

    assert await resolve("another") == expected
    assert len(upstream.calls) == 2
    opengeni.reset_caches()
    assert await resolve("local") == expected
    assert len(upstream.calls) == 3


@pytest.mark.asyncio
async def test_workspace_override_skips_provisioning(identity_upstream, monkeypatch):
    monkeypatch.setenv("LEVH_OPENGENI_WORKSPACE_ID", "  ws-provisioned  ")
    assert await opengeni.workspace_id("local") == "ws-provisioned"
    assert identity_upstream.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["workspace", "subject"])
@pytest.mark.parametrize("status,payload", [(429, {"error": {"code": "budget"}}), (200, {}), (200, None)])
async def test_failed_identity_resolution_is_not_cached(identity_upstream, kind, status, payload):
    upstream = identity_upstream
    if kind == "workspace":
        resolve = opengeni.workspace_id
        path = "/v1/workspaces/external"
        valid = {"workspace": {"id": WORKSPACE}}
        expected = WORKSPACE
        error_code = "invalid_workspace_response"
    else:
        resolve = opengeni.subject_id
        path = "/v1/access/me"
        valid = {"subjectId": SUBJECT}
        expected = SUBJECT
        error_code = "invalid_access_response"
    upstream.answer(path, payload, status=status)
    with pytest.raises(opengeni.UpstreamError) as exc:
        await resolve("local")
    assert exc.value.status == (status if status >= 400 else 502)
    if status >= 400:
        assert exc.value.payload == payload
    else:
        assert exc.value.payload["error"]["code"] == error_code

    upstream.answer(path, valid)
    assert await resolve("local") == expected
    assert len(upstream.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,payload,expected_status,expected_payload",
    [
        (429, {"error": {"code": "budget"}}, 429, {"error": {"code": "budget"}}),
        (302, None, 502, None),
    ],
)
async def test_identity_errors_are_reported_to_browser(
    api, monkeypatch, status, payload, expected_status, expected_payload,
):
    client, upstream, _ = api

    async def fail(_):
        raise opengeni.UpstreamError(status, payload)

    monkeypatch.setattr(opengeni, "workspace_id", fail)
    response = await client.get("/api/opengeni/v1/config/client")
    assert response.status_code == expected_status
    assert response.headers["cache-control"] == "no-store"
    if expected_payload is not None:
        assert response.json() == expected_payload
    else:
        assert response.json()["error"]["code"] == "upstream_error"
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_proxy_rejects_malformed_paths_before_upstream(api):
    client, upstream, _ = api

    for path in (
        "/api/opengeni/not-v1",
        "/api/opengeni/v1/workspaces/bad%20segment",
    ):
        response = await client.get(path)
        assert response.status_code == 404, path

    assert upstream.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{",
        b"[]",
    ],
)
async def test_proxy_rejects_invalid_json_object_bodies(api, body):
    client, upstream, _ = api
    response = await client.post(
        f"/api/opengeni/v1/workspaces/{WORKSPACE}/sessions",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] in {"invalid_body", "invalid_json"}
    assert upstream.calls == []


def test_identity_source_is_install_scoped(monkeypatch):
    monkeypatch.setenv("LEVH_OPENGENI_INSTALL_ID", "install-a")
    source_a = opengeni.identity_source()
    monkeypatch.setenv("LEVH_OPENGENI_INSTALL_ID", "install-b")
    source_b = opengeni.identity_source()

    assert source_a.startswith("levh-")
    assert source_b.startswith("levh-")
    assert source_a != source_b
    assert "install-a" not in source_a
    assert opengeni.actor_header("local") != opengeni.actor_header("local", source=source_a)


def test_default_identity_source_uses_host_and_database(monkeypatch):
    class Config:
        database_path = "/stores/one.db"

    for name in ("LEVH_OPENGENI_INSTALL_ID", "OPENGENI_INSTALL_ID", "STACKMEMORY_OPENGENI_INSTALL_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(opengeni.socket, "gethostname", lambda: "host-a")
    monkeypatch.setattr(opengeni, "resolve_runtime_config", lambda: Config())

    source_a = opengeni.identity_source()
    assert source_a.startswith("levh-")
    assert "host-a" not in source_a
    assert "/stores/one.db" not in source_a

    monkeypatch.setattr(opengeni.socket, "gethostname", lambda: "host-b")
    assert opengeni.identity_source() != source_a
