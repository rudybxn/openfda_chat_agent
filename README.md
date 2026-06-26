# openFDA Drug Intelligence Agent

An agent-driven full-stack app over the FDA's public [openFDA](https://open.fda.gov)
API. A user asks a natural-language question about a drug; an LLM agent
(LangGraph) composes calls to a set of atomic tools served by an MCP server that
wraps openFDA, and returns a synthesized, disclaimed answer.

## Architecture

```
Streamlit frontend ──HTTP /chat──▶ Agent backend (FastAPI) ──MCP/HTTP──▶ MCP server (FastMCP) ──HTTPS──▶ openFDA
  thin client                       LangGraph + OpenRouter key             wraps openFDA, holds openFDA key      api.fda.gov
  no secrets                        └──────────── trust boundary: secrets never cross toward the user ──────────┘
```

Three independently deployable services, each in its own folder:

| Folder        | Service                | Responsibility                                              | Secret it holds        |
| ------------- | ---------------------- | ----------------------------------------------------------- | ---------------------- |
| `mcp_server/` | MCP server (FastMCP)   | Wraps openFDA; exposes 4 atomic tools over streamable HTTP  | `OPENFDA_API_KEY`      |
| `agent/`      | Agent backend (FastAPI)| LangGraph ReAct agent + LLM client; serves `/chat`          | `OPENROUTER_API_KEY`   |
| `frontend/`   | Streamlit UI           | Chat interface; calls `/chat` only                          | none                   |

The **security boundary** is the point: each secret exists only in its own
service's environment. The LLM never sees the openFDA key; the frontend has no
secrets at all; the MCP server is never published — only the agent reaches it
over the internal Docker network.

## The tools

Tools are **atomic** — one per openFDA endpoint — and the agent composes them.
That is deliberate: keeping intelligence in the model (not in a fat
`compare_drugs` function) is what makes the system agentic.

| Tool                  | openFDA endpoint          | Input                              | Output                                                        |
| --------------------- | ------------------------- | ---------------------------------- | ------------------------------------------------------------- |
| `resolve_drug`        | `drug/ndc.json`           | `name`                             | `{generic_name, brand_names, found}`                          |
| `get_label`           | `drug/label.json`         | `drug`                             | `{indications, boxed_warning, warnings, dosage, drug_interactions, found}` |
| `count_adverse_events`| `drug/event.json` (FAERS) | `drug`, `limit=10`, `since=YYYYMMDD?` | `[{reaction, count}, ...]` (server-side aggregated, ranked)  |
| `check_recalls`       | `drug/enforcement.json`   | `drug`, `since=YYYYMMDD?`          | `[{reason, classification, status, date, firm}, ...]`         |

Tool input/output contracts and usage guidance live in the docstrings in
[`mcp_server/server.py`](mcp_server/server.py) — written for the model. To add a
fifth tool: add a data function in `mcp_server/fda_client.py` and wrap it with an
`@mcp.tool` docstring in `server.py`. Nothing else changes.

## Why MCP, not a plain LangChain `@tool`?

MCP gives a **reusable, independently deployable wrapper with a hard secret
boundary**, usable by *any* MCP client — not just this agent. The openFDA key
stays behind the MCP service; the agent connects over the network and never
holds it. A plain in-process `@tool` would collapse that boundary and couple the
openFDA logic to this one agent. We use **streamable HTTP** transport (not
stdio): stdio is designed for a subprocess on a single user's machine, which is
the wrong model for a networked web-server context.

## Prerequisites

- **Python 3.11+** and **git**. (Docker is optional — see the bottom of this section.)
- An **OpenRouter API key** — https://openrouter.ai/keys. **Required**; it's the
  only paid credential. The project reaches Claude and other models through
  OpenRouter's OpenAI-compatible gateway. (Swap in a native provider by editing
  `agent/agent.py`.)
- An **openFDA API key** — https://open.fda.gov/apis/authentication/. **Optional**
  and free; openFDA works without one, but a key raises the rate limits.

## Where the API keys go

Each key lives only in the layer that needs it — that *is* the security
boundary, and it holds whether you run locally or in Docker:

| Key                  | Belongs to     | Required? |
| -------------------- | -------------- | --------- |
| `OPENROUTER_API_KEY` | `agent/`       | yes       |
| `OPENFDA_API_KEY`    | `mcp_server/`  | no        |
| _(none)_             | `frontend/`    | —         |

The frontend never holds a secret, and the openFDA key never reaches the LLM.
Locally you set each key in the terminal that runs that layer (below); with
Docker you put both in the root `.env` and Compose routes each to the right
container.

## Run it locally (recommended)

No Docker needed. You run the three layers in three terminals; they talk over
HTTP on localhost, exactly as the containers do over the Docker network.

**1. Clone and install once.** A single shared virtualenv at the repo root is
fine — install all three layers' requirements into it:

```bash
git clone <repo-url> && cd openfda-agent
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r mcp_server/requirements.txt \
            -r agent/requirements.txt \
            -r frontend/requirements.txt
```

**2. Start each layer in its own terminal.** Activate the venv in each
(`source .venv/bin/activate`), then set that layer's key and run it:

```bash
# Terminal 1 — MCP server (openFDA wrapper) → MCP endpoint at http://localhost:8000/mcp/
cd mcp_server
export OPENFDA_API_KEY=...           # optional; omit to run keyless (lower rate limit)
python server.py

# Terminal 2 — Agent backend (LangGraph + LLM) → serves /chat on :8001
cd agent
export OPENROUTER_API_KEY=...                        # required
export AGENT_MODEL=anthropic/claude-sonnet-4.5       # optional; any OpenRouter slug
export MCP_SERVER_URL=http://localhost:8000/mcp/     # point the agent at the local MCP server
uvicorn main:app --port 8001

# Terminal 3 — Streamlit frontend → http://localhost:8501
cd frontend
export AGENT_URL=http://localhost:8001
streamlit run app.py
```

Then open **http://localhost:8501**. Start them in order (MCP → agent →
frontend); the agent retries the MCP connection at startup, so a few seconds'
lag between terminals is fine.

> Prefer not to `export`? Prefix a single command instead, e.g.
> `OPENROUTER_API_KEY=... uvicorn main:app --port 8001`. Either way the key
> lives only in that shell session — nothing is written to disk or committed.

## Run it with Docker (optional)

If you'd rather not manage three terminals, Docker Compose runs all three layers
with one command and enforces the same secret boundary (each key is injected
into only its own container). It's heavier than the local path above — fine to
skip.

```bash
cp .env.example .env        # fill in OPENROUTER_API_KEY (and optionally OPENFDA_API_KEY)
docker compose up --build   # UI at http://localhost:8501
```

The keys live in the root `.env`; Compose passes each to only the service that
needs it, and the layers start in dependency order via healthchecks (MCP →
agent → frontend).

## Test each layer

Each layer can be checked on its own, so you can confirm a clone works before
wiring everything together:

```bash
# MCP server — unit tests (mock the network: fast, free, no keys, no LLM)
cd mcp_server && pip install -r requirements-dev.txt && pytest

# MCP server — live smoke test against the real openFDA API (no LLM needed)
cd mcp_server && python smoke_test.py Tylenol

# Agent backend — once it's running, hit /chat directly (see "API" below)
curl -N localhost:8001/chat -H 'content-type: application/json' \
  -d '{"message": "What are the boxed warnings for Tylenol?"}'

# Frontend — just open http://localhost:8501 in a browser
```

## API

`POST /chat` on the agent backend. Body: `{"message": str, "history": [{"role","content"}]}`.
Returns an NDJSON stream of events: `tool_call`, `tool_result`, and `final`.

```bash
curl -N localhost:8001/chat -H 'content-type: application/json' \
  -d '{"message": "What are the boxed warnings for Tylenol?"}'
```

## Things a new maintainer should know

- **Adverse-event counts are report frequencies, not rates** — FAERS data is
  voluntary, unverified, and does not establish causation. The system prompt
  ([`agent/prompts.py`](agent/prompts.py)) enforces this framing, plus the
  resolve-first workflow, the medical-advice refusal-with-redirect, and the
  openFDA disclaimer.
- **Self-correction is a feature:** `langchain-mcp-adapters` returns tool errors
  to the model as messages rather than crashing, so a misspelled drug name is
  recoverable mid-run.
- **Deployment:** locally this is one `docker compose up`. In the cloud each
  layer is its own container — agent and MCP server as serverless containers /
  small services (e.g. Fargate tasks or Lambda container images behind API
  Gateway; both layers are stateless and scale horizontally), with the MCP
  server on **private ingress** so only the agent reaches it. Secrets come from
  a managed secrets store, not `.env` files. The frontend sits on a public
  container host.

## Disclaimer

Information is sourced from openFDA (FDA drug labeling and the FAERS adverse
event database) and is **not medical advice**. Consult a healthcare professional
for medical decisions.
