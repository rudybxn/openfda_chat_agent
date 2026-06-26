"""FastAPI backend exposing the agent over a single /chat endpoint.

/chat streams newline-delimited JSON (NDJSON) events so the frontend can show
tool calls and results live — which is what makes the agentic behavior visible.
The frontend never talks to the MCP server or the LLM directly; it only calls
/chat. No secrets live in the frontend.
"""

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent import build_agent, run_agent


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build the agent once at startup (connects to MCP, loads tools).
    app.state.agent = await build_agent()
    yield


app = FastAPI(title="openFDA Agent", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    async def event_stream():
        async for event in run_agent(app.state.agent, req.message, req.history):
            yield json.dumps(event) + "\n"

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")
