"""Opengeni session proxy — LEVH's half of the embedded memory assistant.

The dashboard is a static Next.js export served by this FastAPI process, so
the proxy that ``createSessionProxyRoute`` implements for Next.js has no
Next.js process to live in. This module is that same proxy written for this
backend (docs.opengeni.ai/integrate/proxy-from-any-backend): the organization
key stays here, and the browser only ever talks to ``/api/opengeni/*``.

Two things follow from LEVH being local-first, and both are deliberate:

- **Identity is the local principal.** There is no account layer, so the
  Opengeni workspace is the install (the ``default`` tenancy boundary today),
  registered in a per-install Opengeni external-actor namespace so it cannot
  collide with another LEVH installation that happens to share the same
  organization key.
- **The agent reads LEVH through per-message context, not a tool endpoint.**
  Product tools would require Opengeni to call *back* into the user's machine
  over public HTTPS — impossible for a server that listens on localhost, and
  not a defensible default. Recall injection is outbound-only, so the
  assistant works on the same machine and behind the same firewall as the
  store.

Nothing here writes to the store: the assistant answers from memory. That is
the contract ``/api/ask`` already documents ("read-only — asking does not
reinforce memories"), kept by passing ``reinforce=False`` to recall below.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
from typing import Any
from urllib.parse import quote

import httpx

from server.core.env import get_env
from server.core.runtime_config import resolve_runtime_config

logger = logging.getLogger("levh.opengeni")

#: The API contract revision this proxy implements.
#:
#: The embedded SDK compares the revision it was built against with the one
#: the proxy reports, and reloads the page when they differ. Forwarding the
#: *upstream* deployment's revision would therefore turn every Opengeni release
#: into an apparent host-side breaking change, so the proxy owns this value and
#: it moves only when this module is updated to match.
API_CONTRACT_REVISION = "2026-09-plugins-and-skills-v1"

DEFAULT_API_BASE_URL = "https://app.opengeni.ai"

#: Prefix for Opengeni external identity namespaces. A per-install suffix is
#: added by :func:`identity_source` so two LEVH installs that share one
#: organization key do not share chats by accident.
IDENTITY_SOURCE_PREFIX = "levh"

_WORKSPACE_NAME = "LEVH"

#: How long the injected memory block may get. Recall returns whole memories
#: and the model's context is finite, so one long memory must not be able to
#: crowd out the conversation it was attached to.
CONTEXT_MAX_CHARS = 4200

_TIMEOUT_SECONDS = 30.0

_workspace_cache: dict[str, str] = {}
_workspace_lock = asyncio.Lock()
_subject_cache: dict[str, str] = {}
_subject_lock = asyncio.Lock()


class UpstreamError(RuntimeError):
    """An upstream call that the proxy cannot forward as-is."""

    def __init__(self, status: int, payload: Any) -> None:
        super().__init__(f"opengeni upstream returned {status}")
        self.status = status
        self.payload = payload


def api_base_url() -> str:
    """Where the Opengeni API lives; self-hosted deployments override it."""
    return (get_env("OPENGENI_API_BASE_URL", "") or DEFAULT_API_BASE_URL).rstrip("/")


def api_key() -> str:
    return (get_env("OPENGENI_API_KEY", "") or "").strip()


def enabled() -> bool:
    """Whether this install has an organization key to serve the assistant with.

    Read per request, not frozen at import: an install that exports the key
    after the server started (or a test that sets it) must be able to turn the
    assistant on without a restart.
    """
    return bool(api_key())


def installation_id() -> str:
    """Stable-enough namespace input that keeps independent installs isolated.

    Operators that move a store between hosts or paths can pin the namespace
    with ``LEVH_OPENGENI_INSTALL_ID``. Otherwise the hash is derived from the
    host name and resolved database path, which avoids exposing either value to
    Opengeni while making separate local installs distinct by default.
    """
    explicit = (get_env("OPENGENI_INSTALL_ID", "") or "").strip()
    if explicit:
        seed = explicit
    else:
        seed = f"{socket.gethostname()}\0{resolve_runtime_config().database_path}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def identity_source() -> str:
    """Opengeni external-identity namespace for this LEVH installation."""
    return f"{IDENTITY_SOURCE_PREFIX}-{installation_id()}"


def actor_header(external_id: str, source: str | None = None) -> str:
    """The ``x-opengeni-external-actor`` value for one host-authenticated user.

    The API reads this to run the request as that user, never as the
    organization key's own service authority. The encoding is the SDK's —
    percent-encoded JSON — which is what keeps an id containing a slash or a
    quote from splitting the header value.
    """
    payload = json.dumps(
        {
            "mode": "external",
            "identity": {
                "externalId": external_id,
                "source": source or identity_source(),
            },
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    # ``safe`` mirrors encodeURIComponent: the JavaScript SDK produces this
    # header with it, and an id must encode identically from either side.
    return quote(payload, safe="-_.!~*'()")


def upstream_headers(
    external_id: str | None,
    *,
    source: str | None = None,
    accept: str = "application/json",
) -> dict[str, str]:
    """Headers for one upstream call.

    ``external_id=None`` is the *service* scope: the organization key acting as
    itself, which is what workspace provisioning needs and nothing else does.
    """
    headers = {
        "Authorization": f"Bearer {api_key()}",
        "Accept": accept,
        "x-opengeni-api-contract": API_CONTRACT_REVISION,
    }
    if external_id is not None:
        headers["x-opengeni-external-actor"] = actor_header(external_id, source)
    return headers


def _decode(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


async def request_json(
    method: str,
    path: str,
    *,
    external_id: str | None,
    params: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = _TIMEOUT_SECONDS,
) -> tuple[int, Any, dict[str, str]]:
    """One upstream call; returns ``(status, decoded_body, response_headers)``.

    Statuses are returned rather than raised: the route layer forwards the
    API's own status and error body, which is exactly what the embedded SDK
    reads to decide between "retry", "ask an admin", and "the user typed
    something invalid".
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(
            method,
            f"{api_base_url()}{path}",
            params=params or None,
            json=body,
            headers=upstream_headers(external_id),
        )
    return response.status_code, _decode(response), dict(response.headers)


async def workspace_id(external_id: str) -> str:
    """Resolve — and on first use register — this install's Opengeni workspace.

    ``externalSource`` + ``externalId`` is Opengeni's idempotency key for the
    mapping, so a restart re-resolves the same workspace instead of creating a
    second one, and a second one is what would silently split a user's chat
    history in two. ``LEVH_OPENGENI_WORKSPACE_ID`` short-circuits the lookup for
    an install provisioned deliberately, or one that must point at a workspace
    the key can already reach.

    Called with the *service* scope: workspace provisioning is the key's own
    action, and every user-scoped call that follows needs the workspace that
    this one created.
    """
    override = (get_env("LEVH_OPENGENI_WORKSPACE_ID", "") or "").strip()
    if override:
        return override
    cached = _workspace_cache.get(external_id)
    if cached:
        return cached
    async with _workspace_lock:
        cached = _workspace_cache.get(external_id)
        if cached:
            return cached
        status, payload, _ = await request_json(
            # PUT, not POST: `ensureWorkspace` in the JavaScript SDK is the
            # authority here, and the docs page that says POST is wrong — the
            # API routes a POST at that path to `/v1/workspaces/{workspaceId}`
            # and answers 405.
            "PUT",
            "/v1/workspaces/external",
            external_id=None,
            body={
                "externalSource": identity_source(),
                "externalId": external_id,
                "name": _WORKSPACE_NAME,
            },
        )
        if status >= 400:
            raise UpstreamError(status, payload)
        workspace = payload.get("workspace") if isinstance(payload, dict) else None
        resolved = str((workspace or {}).get("id") or "")
        if not resolved:
            raise UpstreamError(
                502,
                {"error": {"code": "invalid_workspace_response", "message": "Opengeni returned no workspace id."}},
            )
        _workspace_cache[external_id] = resolved
        return resolved


async def subject_id(external_id: str) -> str:
    """This user's Opengeni subject id, used to scope their session list.

    Looked up once and cached: it never changes, and the list filter is the one
    place the proxy needs the canonical id rather than the external one it was
    handed.
    """
    cached = _subject_cache.get(external_id)
    if cached:
        return cached
    async with _subject_lock:
        cached = _subject_cache.get(external_id)
        if cached:
            return cached
        status, payload, _ = await request_json("GET", "/v1/access/me", external_id=external_id)
        if status >= 400:
            raise UpstreamError(status, payload)
        subject = str((payload or {}).get("subjectId") or "") if isinstance(payload, dict) else ""
        if not subject:
            raise UpstreamError(
                502,
                {"error": {"code": "invalid_access_response", "message": "Opengeni returned no subject id."}},
            )
        _subject_cache[external_id] = subject
        return subject


def reset_caches() -> None:
    """Forget resolved workspaces and subjects (tests, and a key change)."""
    _workspace_cache.clear()
    _subject_cache.clear()


async def memory_context(engine: Any, question: str, *, limit: int = 6) -> str | None:
    """LEVH's own recall, rendered as the context the agent answers from.

    ``reinforce=False`` for the reason ``/api/ask`` documents: asking the
    assistant must not change what the store considers important. An empty
    result returns ``None`` rather than an empty block, so a first-run store
    produces no stray context on the message.
    """
    text = (question or "").strip()
    if not text:
        return None
    result = await engine.recall(text, top_k=limit, reinforce=False)
    memories = list(getattr(result, "memories", None) or [])
    if not memories:
        return None
    lines = [
        "The user's LEVH memory store was searched for this message.",
        "Answer from the memories below and cite them by their bracketed number.",
        "If they do not cover the question, say what is missing instead of guessing.",
        "",
    ]
    used = 0
    for index, memory in enumerate(memories, start=1):
        created = str(getattr(memory, "created_at", "") or "")[:10]
        tags = ", ".join(str(tag) for tag in (getattr(memory, "tags", None) or []))
        label = f"[{index}]"
        if created:
            label += f" {created}"
        if tags:
            label += f" ({tags})"
        body = " ".join(str(getattr(memory, "content", "") or "").split())
        block = f"{label}\n{body}\n"
        # Always keep the first (best-ranked) memory, however long it is; stop
        # before the cap only once at least one memory made it in.
        if used + len(block) > CONTEXT_MAX_CHARS and index > 1:
            break
        lines.append(block)
        used += len(block)
    return "\n".join(lines).strip() or None
