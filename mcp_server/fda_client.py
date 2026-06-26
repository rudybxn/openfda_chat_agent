"""Thin async wrapper around the openFDA REST API (https://api.fda.gov).

This module owns ALL knowledge of openFDA: which endpoint each tool hits, how
search queries are built, and how raw responses are shaped into the small,
clean dicts the agent consumes. The MCP tool layer (server.py) only adds
docstrings and exposes these functions over MCP.

openFDA query notes:
- Phrase searches are double-quoted (`field:"value"`) so multi-word names work.
- A 404 from openFDA means "no matching records," not a transport error; we
  translate it to an empty result rather than raising.
- Endpoint field references:
    drug/ndc.json          brand_name, generic_name
    drug/label.json        openfda.generic_name + the label text fields
    drug/event.json        patient.drug.openfda.generic_name (FAERS reports)
    drug/enforcement.json  openfda.generic_name (recalls)
"""

from typing import Any

import httpx

from config import (
    MAX_FIELD_CHARS,
    OPENFDA_API_KEY,
    OPENFDA_BASE_URL,
    OPENFDA_DISCLAIMER,
)


def _clean(term: str) -> str:
    """Strip quotes so a user value can't break out of the quoted phrase."""
    return term.strip().replace('"', "")


def _truncate(value: Any) -> Any:
    """openFDA label fields are arrays of long strings. Take the first element
    and cap its length so the agent context stays manageable."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS].rstrip() + " …[truncated]"
    return value


def _disclaimer(data: dict | None) -> str:
    """openFDA includes an identical disclaimer in `meta.disclaimer` on every
    response. Surface the LIVE value when present; fall back to the canonical
    text when a call returned no records (a 404 has no `meta` block) so every
    tool result always carries a disclaimer for the agent to relay."""
    if data:
        disclaimer = (data.get("meta") or {}).get("disclaimer")
        if disclaimer:
            return disclaimer
    return OPENFDA_DISCLAIMER


async def _get(path: str, params: dict) -> dict | None:
    """GET an openFDA endpoint. Returns parsed JSON, or None when openFDA
    reports no matching records (HTTP 404)."""
    params = dict(params)
    if OPENFDA_API_KEY:
        params["api_key"] = OPENFDA_API_KEY
    async with httpx.AsyncClient(base_url=OPENFDA_BASE_URL, timeout=20.0) as client:
        resp = await client.get(path, params=params)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


async def _count(path: str, search: str, field: str, limit: int = 1) -> list[dict]:
    """Server-side aggregation: count distinct values of `field` for records
    matching `search`, ranked by frequency. Returns [{term, count}, ...]."""
    data = await _get(
        path, {"search": search, "count": f"{field}.exact", "limit": limit}
    )
    if not data or not data.get("results"):
        return []
    return data["results"]


async def resolve_drug(name: str) -> dict:
    """Resolve a brand or generic name to its generic name via drug/ndc.json.

    Rather than taking an arbitrary first NDC record (which may be a combination
    product), we ask openFDA which generic name is MOST COMMON for the search —
    so "Tylenol" resolves to "ACETAMINOPHEN", not a multi-ingredient cold-and-flu
    product that happens to sort first.
    """
    term = _clean(name)
    for field in ("brand_name", "generic_name"):
        # Call _get directly (not _count) so we can also read meta.disclaimer.
        data = await _get(
            "/drug/ndc.json",
            {"search": f'{field}:"{term}"', "count": "generic_name.exact", "limit": 1},
        )
        results = data.get("results") if data else None
        if results:
            generic = results[0]["term"]
            brand_rows = await _count(
                "/drug/ndc.json", f'generic_name:"{generic}"', "brand_name", limit=10
            )
            return {
                "generic_name": generic,
                "brand_names": [r["term"] for r in brand_rows],
                "found": True,
                "disclaimer": _disclaimer(data),
            }
    return {
        "generic_name": None,
        "brand_names": [],
        "found": False,
        "disclaimer": OPENFDA_DISCLAIMER,
    }


async def get_label(drug: str) -> dict:
    """Fetch the structured product label from drug/label.json."""
    term = _clean(drug)
    data = await _get(
        "/drug/label.json",
        {"search": f'openfda.generic_name:"{term}"', "limit": 1},
    )
    if not data or not data.get("results"):
        return {"found": False, "disclaimer": _disclaimer(data)}
    r = data["results"][0]
    return {
        "found": True,
        "indications": _truncate(r.get("indications_and_usage")),
        "boxed_warning": _truncate(r.get("boxed_warning")),
        "warnings": _truncate(r.get("warnings") or r.get("warnings_and_cautions")),
        "dosage": _truncate(r.get("dosage_and_administration")),
        "drug_interactions": _truncate(r.get("drug_interactions")),
        "disclaimer": _disclaimer(data),
    }


async def count_adverse_events(
    drug: str, limit: int = 10, since: str | None = None
) -> list[dict]:
    """Server-side count of FAERS adverse-event reaction terms from
    drug/event.json. openFDA does the aggregation via the `count` parameter."""
    term = _clean(drug)
    search = f'patient.drug.openfda.generic_name:"{term}"'
    if since:
        search += f" AND receivedate:[{_clean(since)} TO 30001231]"
    data = await _get(
        "/drug/event.json",
        {
            "search": search,
            "count": "patient.reaction.reactionmeddrapt.exact",
            "limit": limit,
        },
    )
    if not data or not data.get("results"):
        return []
    return [{"reaction": r["term"], "count": r["count"]} for r in data["results"]]


async def check_recalls(drug: str, since: str | None = None) -> list[dict]:
    """Fetch enforcement (recall) records from drug/enforcement.json."""
    term = _clean(drug)
    search = f'openfda.generic_name:"{term}"'
    if since:
        search += f" AND report_date:[{_clean(since)} TO 30001231]"
    data = await _get("/drug/enforcement.json", {"search": search, "limit": 10})
    if not data or not data.get("results"):
        return []
    return [
        {
            "reason": r.get("reason_for_recall"),
            "classification": r.get("classification"),
            "status": r.get("status"),
            "date": r.get("recall_initiation_date"),
            "firm": r.get("recalling_firm"),
        }
        for r in data["results"]
    ]
