# CLAUDE.md

Context for building the openFDA agentic web app. Read this before generating or editing code in this repo.

## Project overview

A full-stack agentic app over the openFDA drug API. A user asks natural-language drug-safety questions; a LangGraph agent answers by calling tools exposed over MCP, which wrap the openFDA REST API. Three independently deployable layers with a strict secret boundary.

## Architecture

Three layers, one folder each, never collapsed:

1. Frontend (Streamlit). Thin chat client. Calls the agent's `/chat` endpoint over HTTP. Holds no secrets.
2. Agent backend (FastAPI + LangGraph). Holds the LLM provider key. Runs the agent, connects to the MCP server as an MCP client, exposes `POST /chat`.
3. MCP server (FastMCP). Wraps openFDA. Holds the `OPENFDA_API_KEY`. Exposes four tools over streamable HTTP. Never exposed publicly; only the agent reaches it.

Secret boundary (non-negotiable):
- `OPENFDA_API_KEY` exists only in the mcp_server environment.
- The LLM provider key exists only in the agent environment.
- The frontend has no secrets.
- No secret ever enters the model's context or reaches the browser.

## Repo structure

```
openfda-agent/
├── mcp_server/
│   ├── server.py          FastMCP app, @mcp.tool definitions
│   ├── fda_client.py      thin httpx wrapper around api.fda.gov
│   ├── config.py          reads OPENFDA_API_KEY from env
│   ├── requirements.txt
│   └── Dockerfile
├── agent/
│   ├── main.py            FastAPI app, POST /chat
│   ├── agent.py           LangGraph agent + MCP client wiring
│   ├── prompts.py         system prompt and guardrails
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── app.py             Streamlit chat UI, calls /chat only
│   ├── requirements.txt
│   └── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

## Tech stack

- Python 3.11+
- MCP server: fastmcp
- Agent: langgraph, langchain, langchain-mcp-adapters, langchain-openai
- API backend: fastapi, uvicorn
- HTTP client: httpx
- Frontend: streamlit
- Transport between agent and MCP server: streamable HTTP (not stdio; stdio is for local single-user processes and is wrong for a web server)
- LLM: reached through **OpenRouter** (an OpenAI-compatible gateway) so we can switch between models from many providers without code changes. Use `langchain-openai`'s `ChatOpenAI` pointed at the OpenRouter base URL; `create_agent` accepts the model instance. Model is configurable via the `AGENT_MODEL` env var, default `anthropic/claude-sonnet-4.5`. The OpenRouter key (`OPENROUTER_API_KEY`) and base URL are read in exactly one place (`agent/agent.py`); do not hardcode the model slug elsewhere.

## openFDA endpoints

Base URL `https://api.fda.gov/`. One data type per endpoint. Pass the key as `api_key` before other params. Every response carries a `meta.disclaimer` field; surface it through tool output.

- `drug/ndc.json` — NDC directory, brand to generic resolution
- `drug/label.json` — structured product labeling
- `drug/event.json` — FAERS adverse event reports
- `drug/enforcement.json` — recall / enforcement reports

`count=` does server-side aggregation. Use it for adverse-event ranking rather than aggregating client-side. Max `limit` per call is 1000.

## Tools (atomic; the agent composes them)

Keep tools one-to-one with endpoints. Do not build a fat `compare_drugs` or `safety_profile` tool; composition is the agent's job. Write docstrings for the model: when to call, what each field means, and that adverse-event counts are report frequencies, not rates.

1. `resolve_drug(name: str) -> {generic_name, brand_names: list, found: bool}`
   Endpoint: `drug/ndc.json`. Normalizes a brand or misspelled name to a generic. The agent's usual first step.

2. `get_label(drug: str) -> {indications, boxed_warning, warnings, dosage, drug_interactions}`
   Endpoint: `drug/label.json`, search `openfda.generic_name`.

3. `count_adverse_events(drug: str, limit: int = 10, since: str | None = None) -> list[{reaction, count}]`
   Endpoint: `drug/event.json` with `count=patient.reaction.reactionmeddrapt.exact`. Optional `since` filters on `receivedate`.

4. `check_recalls(drug: str, since: str | None = None) -> list[{reason, classification, status, date, firm}]`
   Endpoint: `drug/enforcement.json`.

## Agent setup

```python
import os
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI

# LLM via OpenRouter (OpenAI-compatible). Key lives only in the agent env.
model = ChatOpenAI(
    model=os.environ.get("AGENT_MODEL", "anthropic/claude-sonnet-4.5"),
    base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    api_key=os.environ["OPENROUTER_API_KEY"],
    temperature=0,
)

client = MultiServerMCPClient({
    "openfda": {"url": "http://mcp_server:8000/mcp", "transport": "streamable_http"}
})
tools = await client.get_tools()
agent = create_agent(model, tools, system_prompt=SYSTEM_PROMPT)
```

- Let `langchain-mcp-adapters` convert MCP tools to LangChain tools.
- Tool execution errors return to the model as messages so the agent can self-correct. Do not swallow them.
- `/chat` accepts the user message plus prior turns and streams the response back.

## Guardrails (system prompt)

- Resolve brand names to generics before other lookups.
- State that FAERS data is self-reported and does not establish causation.
- Refuse "should I take this" style medical advice; still answer the underlying factual question.
- Always surface the openFDA `meta.disclaimer`.
- Cite which tool and data backed each claim.

## Conventions and constraints

- Three layers stay in three folders. Never put agent logic inside the Streamlit app.
- No secret in code, logs, or the repo. Ship `.env.example` with empty placeholders.
- Pin dependency versions in each `requirements.txt`.
- Each layer has its own Dockerfile; `docker-compose.yml` runs all three on an internal network with only the agent reachable by the frontend and only the frontend reachable publicly.
- Prefer small, typed tool inputs and outputs. No untyped dicts crossing the MCP boundary.

## Setup commands

Fill these in as the build firms up.

```
# mcp_server
# agent
# frontend
# docker-compose up
```

## Out of scope

- Auth / user accounts
- Non-drug openFDA categories (device, food)
- Live hosting 
