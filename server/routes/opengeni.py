"""``/api/opengeni/*`` — the embedded memory assistant's server half.

The contract is the one ``createSessionProxyHandler`` implements in the
JavaScript SDK, written here because LEVH serves its dashboard from FastAPI
(``server/core/opengeni.py`` explains why identity and context work the way
they do). Three rules from that contract are load-bearing and easy to lose in
a port:

- The route *and* method allowlist is exact. An unknown path is a 404, not a
  forwarded request; a known path with the wrong method is not close enough.
- The browser never chooses session configuration, tools, credentials, or the
  workspace. All of it is decided here, and the workspace named in the path
  must be the one this server resolved.
- Errors are forwarded as the API wrote them. The embedded SDK reads those
  codes to decide between "retry" and "ask an administrator".
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from server.core import opengeni
from server.core.tenancy import current_principal
from server.routes.deps import get_engine, public_demo

logger = logging.getLogger("levh.opengeni")

router = APIRouter()

PROXY_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

#: One path segment, mirroring the SDK's ``SEGMENT``: ids only, never an empty
#: or dot segment, and never enough room for an encoded separator to smuggle a
#: path traversal into an upstream URL.
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

#: The contract's body cap. The global middleware allows far more for other
#: endpoints; every message this proxy forwards is text.
_MAX_BODY_BYTES = 1024 * 1024

_CREATE_FIELDS = {"initialMessage", "idempotencyKey", "resources"}
_MODEL_FIELDS = ("model", "reasoningEffort", "latencyMode")
_EVENT_TYPES = {"user.message", "user.approvalDecision", "user.humanInputResponse"}
_QUEUE_OPERATIONS = {"move", "edit", "steer", "delete"}

#: What the assistant is, told once per session. ``capabilities: "none"`` is
#: deliberate: LEVH's data reaches the model as recalled context, so the agent
#: needs no Opengeni workspace tools, connectors, or bundled guides.
_AGENT = {
    "identity": (
        "You are LEVH's memory assistant. LEVH is the user's local-first memory store "
        "for AI agents; it lives on their machine and never leaves it."
    ),
    "instructions": (
        "Answer in the user's language and keep replies short. You are given the "
        "memories LEVH recalled for each message: ground every factual claim in them "
        "and cite them by their bracketed number. Never invent a memory, a date, or a "
        "citation. If the recalled memories do not answer the question, say so plainly "
        "and suggest what the user could capture next. You cannot change the store; do "
        "not offer to save, edit, or delete memories."
    ),
    "capabilities": "none",
}


class _Rejection(Exception):
    """A request this proxy refuses to forward, with the contract's code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _json(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> JSONResponse:
    merged = {"Cache-Control": "no-store"}
    merged.update(headers or {})
    return JSONResponse(body, status_code=status, headers=merged)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return _json({"error": {"code": code, "message": message}}, status)


def _segments(path: str) -> list[str] | None:
    """Decode and validate the segments after ``/api/opengeni/v1``.

    ``None`` means "not this proxy's route", which the caller answers as 404:
    the allowlist below is exact, so an unparsable path is refused before any
    upstream path is built from it.
    """
    raw = path.split("/")
    if not raw or raw[0] != "v1" or len(raw) < 2:
        return None
    decoded: list[str] = []
    for part in raw[1:]:
        value = unquote(part)
        if not _SEGMENT.match(value):
            return None
        decoded.append(value)
    return decoded


async def _read_json(request: Request, *, optional: bool = False) -> dict[str, Any] | None:
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        raise _Rejection(413, "body_too_large", "Request body is too large.")
    if not body:
        if optional:
            return None
        raise _Rejection(400, "invalid_body", "A JSON object body is required.")
    try:
        value = json.loads(body)
    except ValueError as exc:
        raise _Rejection(400, "invalid_json", "A JSON object body is required.") from exc
    if not isinstance(value, dict):
        raise _Rejection(400, "invalid_body", "A JSON object body is required.")
    return value


def _browser_payload(value: Any) -> dict[str, Any]:
    """Browser-supplied message fields, minus anything it may not set.

    MCP credentials are the server's to rotate. A browser that sends
    ``mcpCredentialUpdates`` is either stale or hostile, and both deserve a
    refusal rather than a silent drop.
    """
    payload = dict(value) if isinstance(value, dict) else {}
    if "mcpCredentialUpdates" in payload:
        raise _Rejection(403, "credential_update_not_allowed", "MCP credentials are server-owned.")
    return payload


def _file_resources(value: Any) -> list[dict[str, Any]]:
    """Attachments: file references only, with nothing else riding along."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise _Rejection(403, "resource_not_allowed", "Only file attachments may be added from the browser.")
    resources: list[dict[str, Any]] = []
    for resource in value:
        file = resource if isinstance(resource, dict) else {}
        file_id = file.get("fileId")
        if file.get("kind") != "file" or not isinstance(file_id, str) or not file_id:
            raise _Rejection(403, "resource_not_allowed", "Only file attachments may be added from the browser.")
        entry: dict[str, Any] = {"kind": "file", "fileId": file_id}
        if isinstance(file.get("mountPath"), str):
            entry["mountPath"] = file["mountPath"]
        resources.append(entry)
    return resources


def _sanitize_message(value: Any) -> dict[str, Any]:
    """A browser message/steer/draft body with the server-owned fields removed."""
    message = _browser_payload(value)
    if message.get("resources") is not None:
        message["resources"] = _file_resources(message["resources"])
    # The browser does not pick the model (see ``config/client``), and the SDK
    # is told so; a value that arrives anyway is dropped rather than trusted.
    for field in _MODEL_FIELDS:
        message.pop(field, None)
    # modelContext is server-owned too: LEVH recall supplies it immediately
    # before forwarding. Accepting a browser value here would let untrusted UI
    # text masquerade as recalled memory context.
    message.pop("modelContext", None)
    return message


async def _stream(
    path: str,
    params: dict[str, str],
    external_id: str,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    """Re-emit an upstream SSE stream, unbuffered, until either side closes."""
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0))
    headers = opengeni.upstream_headers(external_id, accept="text/event-stream")
    headers.update(extra_headers or {})
    try:
        upstream = client.build_request(
            "GET", f"{opengeni.api_base_url()}{path}", params=params or None, headers=headers
        )
        response = await client.send(upstream, stream=True)
        if response.status_code >= 400:
            payload = await response.aread()
            await response.aclose()
            await client.aclose()
            return Response(
                content=payload,
                status_code=response.status_code,
                media_type=response.headers.get("content-type", "application/json"),
                headers={"Cache-Control": "no-store"},
            )

        async def iterator() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                # Runs on client disconnect too: the upstream socket must not
                # outlive the browser that asked for it.
                await response.aclose()
                await client.aclose()

        return StreamingResponse(
            iterator(),
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "text/event-stream"),
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("opengeni stream failed: %s", exc)
        return _error(502, "upstream_unavailable", "Opengeni is temporarily unavailable — retry.")


async def opengeni_proxy(request: Request, path: str, engine=Depends(get_engine)) -> Response:
    """Forward one browser call to Opengeni as the local principal."""
    if public_demo():
        # A public demo install has no owner to pay for the turns, and its
        # store is a fixture every visitor shares.
        return _error(404, "route_not_allowed", "Not found.")
    if not opengeni.enabled():
        return _error(
            503,
            "opengeni_not_configured",
            "Set OPENGENI_API_KEY on the LEVH server to enable the assistant chat.",
        )
    if request.method != "GET" and request.headers.get("sec-fetch-site") == "cross-site":
        # The same default the SDK ships with: a browser sends this header on
        # the cross-site case, and the dashboard is same-origin, so refusing it
        # costs nothing and closes the CSRF hole a cookie-less token leaves.
        return _error(403, "mutation_denied", "Request denied.")
    try:
        return await _dispatch(request, path, engine)
    except _Rejection as rejection:
        return _error(rejection.status, rejection.code, rejection.message)
    except opengeni.UpstreamError as failure:
        status = failure.status if 400 <= failure.status < 600 else 502
        if isinstance(failure.payload, dict):
            return _json(failure.payload, status)
        return _error(status, "upstream_error", "The Opengeni API refused this request.")
    except httpx.HTTPError as exc:
        logger.warning("opengeni request failed: %s", exc)
        return _error(502, "upstream_unavailable", "Opengeni is temporarily unavailable — retry.")


# One route per method, not one route with five methods. FastAPI derives a
# route's ``operationId`` from the *first* method in its list, so a single
# multi-method route publishes the same ``operationId`` five times — a schema a
# generated client cannot consume, and the reason this loop exists rather than
# a ``@router.api_route(..., methods=[...])`` decorator.
for _method in PROXY_METHODS:
    router.add_api_route(
        "/api/opengeni/{path:path}",
        opengeni_proxy,
        methods=[_method],
        name=f"opengeni_proxy_{_method.lower()}",
    )


async def _dispatch(request: Request, path: str, engine: Any) -> Response:
    segments = _segments(path)
    if segments is None:
        return _error(404, "route_not_allowed", "Not found.")

    principal = current_principal()
    user = principal.id
    tenant = principal.workspace_id
    workspace_id = await opengeni.workspace_id(tenant)
    query = dict(request.query_params)

    root, rest = segments[0], segments[1:]

    if root == "config":
        if rest == ["client"] and request.method == "GET":
            return await _client_config(user, workspace_id, query)
        return _error(404, "route_not_allowed", "Not found.")

    if root != "workspaces" or not rest:
        return _error(404, "route_not_allowed", "Not found.")
    path_workspace, area, tail = rest[0], (rest[1] if len(rest) > 1 else None), rest[2:]
    if path_workspace != workspace_id:
        return _error(403, "workspace_not_allowed", "This workspace is not available.")

    base = f"/v1/workspaces/{workspace_id}"

    if area is None and request.method == "GET":
        return await _forward("GET", base, user, params=query)
    if area == "model-catalog" and not tail and request.method == "GET":
        return await _forward("GET", f"{base}/model-catalog", user, params=query)
    if area == "usage" and tail == ["me"] and request.method == "GET":
        if any(key != "period" for key in query):
            return _error(400, "invalid_usage_query", "Only period is accepted for own usage.")
        return await _forward("GET", f"{base}/usage/me", user, params=query)
    if area == "live-events" and tail == ["stream"] and request.method == "GET":
        return await _stream(f"{base}/live-events/stream", query, user)
    if area == "inference-control" and not tail and request.method == "POST":
        body = await _read_json(request)
        if (body or {}).get("action") != "resume":
            return _error(403, "control_not_allowed", "Only resume is available.")
        return await _post(f"{base}/inference-control", body, user)
    if area != "sessions":
        return _error(404, "route_not_allowed", "Not found.")

    if not tail:
        if request.method == "GET":
            return await _list_sessions(user, base, query)
        if request.method == "POST":
            return await _create_session(request, engine, user, base)
        return _error(404, "route_not_allowed", "Not found.")

    session_id, op = tail[0], tail[1:]
    session = f"{base}/sessions/{session_id}"
    body = await _read_json(request, optional=request.method == "GET")
    route = f"{request.method} {'/'.join(op)}"

    if route == "GET ":
        return await _forward("GET", session, user, params=query)
    if route == "PATCH ":
        title = (body or {}).get("title")
        if list((body or {}).keys()) != ["title"] or not isinstance(title, str):
            raise _Rejection(400, "invalid_body", "Only { title } may be updated.")
        return await _post(session, {"title": title}, user, method="PATCH")
    if route == "PUT archive":
        archived = (body or {}).get("archived")
        if not isinstance(archived, bool):
            raise _Rejection(400, "invalid_body", "archived is required.")
        payload: dict[str, Any] = {"archived": archived}
        expected_version = (body or {}).get("expectedVersion")
        if isinstance(expected_version, int) and not isinstance(expected_version, bool):
            payload["expectedVersion"] = expected_version
        return await _post(f"{session}/archive", payload, user, method="PUT")
    if route == "DELETE ":
        return _error(404, "route_not_allowed", "Not found.")
    if route == "GET events":
        return await _events(user, f"{session}/events", query)
    if route == "GET events/stream":
        # Prove this user may read the session *before* the stream starts, so an
        # authorization failure is an HTTP status rather than a silent stream.
        status, _, _ = await opengeni.request_json("GET", session, external_id=user)
        if status >= 400:
            return await _forward("GET", session, user)
        last_event_id = request.headers.get("last-event-id")
        extra = {"Last-Event-ID": last_event_id} if last_event_id else None
        return await _stream(f"{session}/events/stream", query, user, extra)
    if route == "POST events":
        event = body or {}
        if event.get("type") not in _EVENT_TYPES:
            return _error(403, "event_not_allowed", "This session event type is not available.")
        payload = dict(event.get("payload") or {})
        if event["type"] == "user.message":
            payload = _sanitize_message(payload)
            context = await opengeni.memory_context(engine, str(payload.get("text") or ""))
            if context:
                payload["modelContext"] = _join_context(context, payload.get("modelContext"))
        else:
            payload = _browser_payload(payload)
        return await _post(f"{session}/events", {**event, "payload": payload}, user)
    if route == "POST steer":
        message = _sanitize_message(body)
        context = await opengeni.memory_context(engine, str(message.get("text") or ""))
        if context:
            message["modelContext"] = _join_context(context, message.get("modelContext"))
        return await _post(f"{session}/steer", message, user)
    if route == "GET queue":
        return await _forward("GET", f"{session}/queue", user, params=query)
    if route == "GET composer-draft":
        return await _forward("GET", f"{session}/composer-draft", user, params=query)
    if route == "PUT composer-draft":
        return await _post(f"{session}/composer-draft", _sanitize_message(body), user, method="PUT")
    if route == "POST composer-draft/submit":
        message = _sanitize_message(body)
        context = await opengeni.memory_context(engine, str(message.get("text") or ""))
        if context:
            message["modelContext"] = _join_context(context, message.get("modelContext"))
        return await _post(f"{session}/composer-draft/submit", message, user)
    if route == "POST control":
        if (body or {}).get("action") not in {"pause", "resume"}:
            return _error(403, "control_not_allowed", "Only pause and resume are available.")
        return await _post(f"{session}/control", body, user)
    if route == "GET human-input-requests":
        return await _forward("GET", f"{session}/human-input-requests", user, params=query)
    if len(op) == 2 and op[0] == "human-input-requests" and request.method == "GET":
        return await _forward("GET", f"{session}/human-input-requests/{op[1]}", user, params=query)
    if len(op) == 3 and op[0] == "queue" and op[2] in _QUEUE_OPERATIONS and request.method == "POST":
        return await _post(f"{session}/queue/{op[1]}/{op[2]}", body, user)
    return _error(404, "route_not_allowed", "Not found.")


def _join_context(*parts: Any) -> str:
    """Join context blocks the way the SDK's ``joinContext`` does."""
    return "\n\n".join(str(part).strip() for part in parts if isinstance(part, str) and part.strip())


async def _forward(
    method: str,
    path: str,
    user: str,
    *,
    params: dict[str, str] | None = None,
) -> JSONResponse:
    status, payload, _ = await opengeni.request_json(method, path, external_id=user, params=params)
    return _json(payload, status)


async def _post(
    path: str,
    body: Any,
    user: str,
    *,
    method: str = "POST",
) -> JSONResponse:
    status, payload, _ = await opengeni.request_json(method, path, external_id=user, body=body)
    return _json(payload, status)


async def _events(user: str, path: str, query: dict[str, str]) -> Response:
    """Event history: forward paging metadata, never forensic reads."""
    if query.get("mode") == "forensic":
        raise _Rejection(403, "forensic_events_not_allowed", "Forensic event reads are not proxied.")
    status, payload, headers = await opengeni.request_json("GET", path, external_id=user, params=query)
    forwarded = {
        name: value
        for name, value in headers.items()
        if name.lower().startswith("x-opengeni-") and name.lower() != "x-opengeni-api-contract"
    }
    return _json(payload, status, forwarded)


async def _list_sessions(user: str, base: str, query: dict[str, str]) -> JSONResponse:
    """The caller's own chats, filtered server-side in Opengeni.

    ``createdBySubjectId`` is added here rather than accepted from the browser:
    it is the one filter that keeps this list to the local principal's chats,
    and a query the browser could widen would not be a filter at all.
    """
    if query.get("view") != "page":
        raise _Rejection(400, "page_view_required", "List sessions with listSessionPage.")
    params = dict(query)
    params["createdByKind"] = "subject"
    params["createdBySubjectId"] = await opengeni.subject_id(user)
    status, payload, _ = await opengeni.request_json("GET", f"{base}/sessions", external_id=user, params=params)
    if isinstance(payload, dict):
        # Pins are a separate personal projection this proxy does not manage.
        payload = {**payload, "pinned": []}
    return _json(payload, status)


async def _create_session(request: Request, engine: Any, user: str, base: str) -> JSONResponse:
    """Start a chat: browser message in, server-chosen agent and privacy out."""
    body = await _read_json(request) or {}
    unknown = [
        key
        for key in body
        if key not in _CREATE_FIELDS and key not in _MODEL_FIELDS
    ]
    if unknown:
        raise _Rejection(
            400,
            "create_field_not_allowed",
            f'The server chooses session configuration; "{unknown[0]}" cannot be set from the browser.',
        )
    initial_message = body.get("initialMessage")
    if not isinstance(initial_message, str) or not initial_message.strip():
        raise _Rejection(400, "initial_message_required", "initialMessage is required.")
    idempotency_key = body.get("idempotencyKey")
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key):
        raise _Rejection(400, "invalid_idempotency_key", "idempotencyKey must be a non-empty string.")

    payload: dict[str, Any] = {
        "initialMessage": initial_message,
        "agent": _AGENT,
        # Private chats about a personal, local store: the session sees its own
        # memory only, exactly as the SDK's "private" default does.
        "visibility": "private",
        "agentAccess": "session",
        "memoryScope": "user",
    }
    if idempotency_key:
        payload["idempotencyKey"] = idempotency_key
    resources = _file_resources(body.get("resources"))
    if resources:
        payload["resources"] = resources
    context = await opengeni.memory_context(engine, initial_message)
    if context:
        payload["modelContext"] = context
    return await _post(f"{base}/sessions", payload, user)


async def _client_config(user: str, workspace_id: str, query: dict[str, str]) -> JSONResponse:
    """The one call every embedded page makes first, rewritten for this host.

    Three kinds of fact are corrected here: the contract revision the page
    should speak, the workspace it belongs to, and which optional surfaces this
    proxy actually serves. Claiming a surface it does not forward — uploads,
    artifacts, live voice, a model picker — puts a button in front of the user
    that can only fail.
    """
    params = dict(query)
    params["workspaceId"] = workspace_id
    status, payload, _ = await opengeni.request_json("GET", "/v1/config/client", external_id=user, params=params)
    if status >= 400 or not isinstance(payload, dict):
        return _json(payload, status)
    config = dict(payload)
    # Upstream proxy capabilities never authorize this host's routes.
    config.pop("artifacts", None)
    voice = config.get("voiceInput")
    if isinstance(voice, dict):
        # `resumable` is the chunked-upload half of voice input, which this
        # proxy does not forward; dropping it keeps the browser from offering
        # a long recording that could only fail mid-upload.
        config["voiceInput"] = {
            key: value for key, value in voice.items() if key != "resumable"
        } | {"available": False}
    uploads = config.get("fileUploads")
    if isinstance(uploads, dict):
        config["fileUploads"] = {**uploads, "enabled": False}
    config.update(
        {
            "apiContractRevision": opengeni.API_CONTRACT_REVISION,
            "workspaceId": workspace_id,
            "sandboxFiles": False,
            "artifacts": False,
            "sessionCreation": True,
            "archive": True,
            "realtimeVoice": False,
            "modelSelection": False,
        }
    )
    return _json(config, 200)
