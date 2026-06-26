"""openFDA MCP server.

Exposes four atomic tools over MCP (streamable HTTP transport). Each tool maps
one-to-one to an openFDA endpoint and stays thin — the agent composes them to
answer a question, which is what makes the system read as "agentic."

The docstrings below are written FOR THE MODEL: they state exactly when to use
each tool, what each field means, and the critical caveat that adverse-event
counts are report frequencies, not rates. A vague docstring is the main reason
an agent picks the wrong tool.

Transport: streamable HTTP (not stdio). stdio is designed for a subprocess on a
single user's machine; in a networked web-server context where a separate agent
service connects over the internal network, HTTP is the correct choice.
"""

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

import fda_client

mcp = FastMCP("openfda")


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Liveness probe for Docker / orchestrators. Plain HTTP (not MCP), so a
    healthcheck can hit it without speaking the MCP protocol."""
    return JSONResponse({"status": "ok"})


@mcp.tool
async def resolve_drug(name: str) -> dict:
    """Resolve a drug name (brand OR generic, possibly misspelled) to its
    canonical generic name. This is almost always the FIRST tool to call:
    every other tool searches by generic name, so resolve first and pass the
    returned `generic_name` to them.

    Args:
        name: A brand name ("Tylenol", "Advil"), a generic name
            ("acetaminophen"), or the user's best guess at spelling.

    Returns:
        {
          "generic_name": str | None,   # canonical generic, e.g. "acetaminophen"
          "brand_names": list[str],     # known brand names for that generic
          "found": bool,                # False if no match — try a different spelling
          "disclaimer": str             # openFDA's data disclaimer — relay it to the user
        }
    """
    return await fda_client.resolve_drug(name)


@mcp.tool
async def get_label(drug: str) -> dict:
    """Get the FDA structured product label: what a drug is approved for and
    its serious risks. Use this for "what is X for", "what are the warnings",
    "what's the dosage", or "does X interact with anything" questions.

    Pass a GENERIC name (call resolve_drug first if you only have a brand name).
    Long text fields are truncated for brevity.

    Args:
        drug: Generic drug name.

    Returns:
        {
          "found": bool,
          "indications": str | None,        # approved uses
          "boxed_warning": str | None,      # the most serious (black-box) warning, if any
          "warnings": str | None,
          "dosage": str | None,
          "drug_interactions": str | None,
          "disclaimer": str                 # openFDA's data disclaimer — relay it to the user
        }
    """
    return await fda_client.get_label(drug)


@mcp.tool
async def count_adverse_events(
    drug: str, limit: int = 10, since: str | None = None
) -> list[dict]:
    """Return the most frequently reported adverse-event reactions for a drug,
    aggregated server-side from the FAERS database, ranked by report count.

    IMPORTANT: these counts are FREQUENCIES OF REPORTS, not incidence rates.
    They are voluntarily submitted, not verified, and do NOT establish that the
    drug caused the reaction. Always frame results this way to the user.

    Pass a GENERIC name (call resolve_drug first if needed).

    Args:
        drug: Generic drug name.
        limit: How many top reactions to return (default 10).
        since: Optional lower bound on report date, format YYYYMMDD
            (e.g. "20230101"). Omit for all-time.

    Returns:
        [ {"reaction": str, "count": int}, ... ]   # ranked, highest count first
        An empty list means no reports were found.
    """
    return await fda_client.count_adverse_events(drug, limit=limit, since=since)


@mcp.tool
async def check_recalls(drug: str, since: str | None = None) -> list[dict]:
    """Look up FDA enforcement actions (recalls) for a drug.

    Pass a GENERIC name (call resolve_drug first if needed).

    Args:
        drug: Generic drug name.
        since: Optional lower bound on report date, format YYYYMMDD. Omit for all.

    Returns:
        [
          {
            "reason": str,            # reason for the recall
            "classification": str,    # "Class I" (most serious) .. "Class III"
            "status": str,            # e.g. "Ongoing", "Terminated"
            "date": str,              # recall initiation date (YYYYMMDD)
            "firm": str               # recalling firm
          }, ...
        ]
        An empty list means no recalls were found.
    """
    return await fda_client.check_recalls(drug, since=since)


if __name__ == "__main__":
    # Streamable HTTP transport. FastMCP serves the MCP endpoint at /mcp/.
    mcp.run(transport="http", host="0.0.0.0", port=8000)
