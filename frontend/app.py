"""Streamlit chat UI for the openFDA agent.

This is a thin client: it holds no secrets and knows nothing about openFDA, MCP,
or the LLM. It POSTs to the agent's /chat endpoint, reads the NDJSON event
stream, and renders tool calls / results / the final answer live.
"""

import json
import os

import httpx
import streamlit as st

AGENT_URL = os.environ.get("AGENT_URL", "http://localhost:8001")

st.set_page_config(page_title="openFDA Drug Agent", page_icon="💊")
st.title("💊 openFDA Drug Intelligence Agent")
st.caption(
    "Ask about a drug's uses, warnings, adverse-event reports, or recalls. "
    "Powered by the FDA openFDA API. Not medical advice."
)

if "history" not in st.session_state:
    st.session_state.history = []  # [{"role": "user"|"assistant", "content": str}]


def _fmt_args(args: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items())


# Replay prior conversation.
for turn in st.session_state.history:
    with st.chat_message(turn["role"]):
        st.markdown(turn["content"])

prompt = st.chat_input("e.g. What are the serious warnings for Tylenol?")

if prompt:
    with st.chat_message("user"):
        st.markdown(prompt)
    # Send history WITHOUT the current message (the backend appends it).
    prior_history = list(st.session_state.history)
    st.session_state.history.append({"role": "user", "content": prompt})

    with st.chat_message("assistant"):
        # Reserve a slot ABOVE the answer for tool activity. It's filled only if
        # the agent actually calls tools, shows the calls live while the run is
        # in flight, and auto-collapses once the answer is done — so a finished
        # turn shows just the answer, with the tool details tucked into a single
        # collapsed bar rather than a persistent grey block above it.
        status_slot = st.container()
        answer_box = st.empty()
        answer = ""
        status = None
        try:
            with httpx.stream(
                "POST",
                f"{AGENT_URL}/chat",
                json={"message": prompt, "history": prior_history},
                timeout=120.0,
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    etype = event.get("type")
                    if etype in ("tool_call", "tool_result") and status is None:
                        status = status_slot.status("Consulting openFDA…", expanded=True)
                    if etype == "tool_call":
                        status.markdown(
                            f"🔧 **{event['name']}**(`{_fmt_args(event['args'])}`)"
                        )
                    elif etype == "tool_result":
                        status.code(event["content"])
                    elif etype == "final":
                        answer += event["content"]
                        answer_box.markdown(answer)
            if status is not None:
                status.update(label="Tool calls", state="complete", expanded=False)
        except httpx.HTTPError as err:
            answer = f"⚠️ Error contacting the agent backend: {err}"
            answer_box.markdown(answer)
            if status is not None:
                status.update(label="Tool calls (incomplete)", state="error")

    st.session_state.history.append({"role": "assistant", "content": answer})
