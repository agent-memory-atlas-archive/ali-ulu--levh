# The memory assistant chat

The dashboard ships an optional chat page at `/assistant` where an
[Opengeni](https://docs.opengeni.ai/) agent answers questions from your own
memories. It is the conversational form of **Ask Your Memory**: the same
recall, a real conversation, and citations you can follow.

It is off until you give the server an Opengeni organization key, and it reads
your store without ever writing to it.

## Turning it on

1. Create a **full-access** organization API key in the Opengeni app under
   *Organization settings → Developer*.
2. Put it in the server environment (`.env` next to the database, or your
   process manager) and restart `levh serve`:

   ```bash
   LEVH_OPENGENI_API_KEY=ogk_...
   ```

3. Open the dashboard and pick **Assistant** in the sidebar.

The bare `OPENGENI_*` aliases are still accepted for compatibility, but the `LEVH_*` names above are canonical. Without a key, `/assistant` renders and `/api/opengeni/*` answers 503 with
`opengeni_not_configured` — nothing is forwarded, and no request leaves the
machine.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `LEVH_OPENGENI_API_KEY` | *(unset)* | Organization key. Turns the assistant on. Server-side only. |
| `LEVH_OPENGENI_API_BASE_URL` | `https://app.opengeni.ai` | API base for a self-hosted Opengeni. |
| `LEVH_OPENGENI_WORKSPACE_ID` | *(resolved)* | Pin the Opengeni workspace instead of resolving it on first request. |
| `LEVH_OPENGENI_INSTALL_ID` | host + database path hash | Pin the installation identity namespace, useful when moving a store between hosts or paths. |

## How it fits the rest of LEVH

The dashboard is a static export served by the FastAPI process, so the browser
has no server route of its own to run in. `server/routes/opengeni.py`
implements Opengeni's documented [proxy
contract](https://docs.opengeni.ai/integrate/proxy-from-any-backend) for this
backend: the browser talks only to `/api/opengeni/*`, and the organization key
never leaves the process. The organization key is **not** a browser value and
must not be put in one.

Two decisions follow from LEVH being local-first, and they are worth knowing
before changing this code:

- **Identity is the local principal, namespaced per install.** There is no
  account layer, so the Opengeni workspace represents this install. LEVH derives
  an external-actor source from a hash of the host name and resolved database
  path, preventing two installs that share one organization key from resolving
  the same workspace and subject by default. Set
  `LEVH_OPENGENI_INSTALL_ID` before moving a store between hosts or paths if
  you want its upstream chat identity to remain stable.
  `LEVH_OPENGENI_WORKSPACE_ID` can still pin a pre-provisioned workspace.
- **The agent reads memory through context, not a tool endpoint.** An MCP tool
  server would require Opengeni to call *back* into this machine over public
  HTTPS, which a server listening on `localhost` cannot offer. Instead the
  proxy recalls the top memories for each message and attaches them to that
  message as model context, so everything is outbound and works behind a
  firewall. The agent is configured with `capabilities: "none"`: it has no
  Opengeni workspace tools, and no LEVH tool to call.

Recall here runs with `reinforce=False`, the same contract `/api/ask`
documents: asking the assistant does not count as using a memory, so it cannot
change what the store considers important.

## What is not forwarded

The proxy forwards the conversation surface and refuses the rest, which is why
the page's client configuration turns those features off rather than showing a
button that can only fail:

- file uploads and attachments
- artifacts, Site previews, and sandbox file reads
- live voice and transcription
- the model picker (the deployment default model is used)
- forensic event reads, `cancel`, and any event type other than
  `user.message`, `user.approvalDecision`, and `user.humanInputResponse`

The assistant cannot change the store at all: there is no write tool, and the
proxy only ever forwards messages to Opengeni. Writing memories on the user's
behalf would need a tool endpoint reachable by Opengeni, which is a deployment
decision rather than a default.

## Read-only by design, and what that protects

A public demo instance (`LEVH_PUBLIC_DEMO=true`) refuses the whole surface: its
store is a fixture every visitor shares and its owner is not paying for
strangers' turns.

When `LEVH_TOKEN` gates `/api/*`, the chat sends the same `X-LEVH-Token`
header as the rest of the dashboard, and the proxy authenticates every request
before it forwards anything. Mutating calls from another site are refused by
`Sec-Fetch-Site`, and the workspace named in the URL must be the one this
server resolved.

## Verifying a change

`tests/test_opengeni_proxy.py` is the boundary suite: it drives the proxy with
a recording upstream (no network, no key) and asserts what reaches Opengeni —
the agent and privacy on session creation, the recalled context on a message,
the subject-scoped session list — together with what never does, such as a
browser-chosen agent, a credential rotation, an unknown route, or another
workspace's id. Run it with:

```bash
uv run --frozen pytest -q tests/test_opengeni_proxy.py
```

A real chat turn is the only test of the integration itself, and it needs a
key: start the server with `LEVH_OPENGENI_API_KEY` set, open `/assistant`, and ask a
question whose answer is in your store.
