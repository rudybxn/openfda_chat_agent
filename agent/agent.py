"""LangGraph agent wiring.

The agent connects to the openFDA MCP server over streamable HTTP, lets
langchain-mcp-adapters convert the MCP tools into LangChain tools, and runs a
ReAct-style loop. The LLM is reached through OpenRouter (an OpenAI-compatible
gateway), so we use ChatOpenAI pointed at the OpenRouter base URL rather than
the native Anthropic client. create_agent accepts a model instance, so this is
a drop-in.

Why the adapter matters on camera: langchain-mcp-adapters returns a tool
execution error back to the model as a message instead of crashing the run, so
the agent can self-correct (e.g. recover from a misspelled drug name).
"""

import asyncio
import os

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from prompts import SYSTEM_PROMPT

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://mcp_server:8000/mcp")
OPENROUTER_BASE_URL = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
)
AGENT_MODEL = os.environ.get("AGENT_MODEL", "anthropic/claude-sonnet-4.5")


def build_model() -> ChatOpenAI:
    """Construct the LLM client. The OpenRouter key lives only in this service's
    environment — it never reaches the MCP server or the frontend."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set in the agent environment.")
    # Optional OpenRouter attribution headers (used for their dashboards/rankings).
    headers = {}
    if os.environ.get("OPENROUTER_APP_URL"):
        headers["HTTP-Referer"] = os.environ["OPENROUTER_APP_URL"]
    if os.environ.get("OPENROUTER_APP_TITLE"):
        headers["X-Title"] = os.environ["OPENROUTER_APP_TITLE"]
    return ChatOpenAI(
        model=AGENT_MODEL,
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
        temperature=0,
        default_headers=headers or None,
    )


async def build_agent(retries: int = 10, delay: float = 2.0):
    """Connect to the MCP server (retrying until it's reachable), load its tools,
    and return a compiled ReAct agent."""
    client = MultiServerMCPClient(
        {
            "openfda": {
                "url": MCP_SERVER_URL,
                "transport": "streamable_http",
            }
        }
    )
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            tools = await client.get_tools()
            break
        except Exception as err:  # MCP server may not be up yet at startup
            last_err = err
            await asyncio.sleep(delay)
    else:
        raise RuntimeError(
            f"Could not reach MCP server at {MCP_SERVER_URL}: {last_err}"
        )

    return create_agent(build_model(), tools, system_prompt=SYSTEM_PROMPT)


def _text(content) -> str:
    """LLM message content is usually a string; some providers return a list of
    content parts. Flatten to plain text either way."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content or "")


async def run_agent(agent, message: str, history: list[dict] | None = None):
    """Stream the agent run as a sequence of typed events:
        {"type": "tool_call",   "name": str, "args": dict}
        {"type": "tool_result", "name": str, "content": str}
        {"type": "final",       "content": str}
    """
    messages = []
    for turn in history or []:
        if turn.get("role") == "user":
            messages.append(HumanMessage(turn["content"]))
        elif turn.get("role") == "assistant":
            messages.append(AIMessage(turn["content"]))
    messages.append(HumanMessage(message))

    async for chunk in agent.astream({"messages": messages}, stream_mode="updates"):
        for update in chunk.values():
            for m in update.get("messages", []):
                if isinstance(m, AIMessage):
                    if m.tool_calls:
                        for call in m.tool_calls:
                            yield {
                                "type": "tool_call",
                                "name": call["name"],
                                "args": call.get("args", {}),
                            }
                    elif _text(m.content).strip():
                        yield {"type": "final", "content": _text(m.content)}
                elif isinstance(m, ToolMessage):
                    yield {
                        "type": "tool_result",
                        "name": m.name or "tool",
                        "content": _text(m.content),
                    }
