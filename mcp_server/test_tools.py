"""Unit tests for the openFDA tool layer.

These mock the network (no real openFDA calls), so they're fast and free and
verify two things that matter when adding or changing a tool:
  1. query building   — the right endpoint + search params are sent
  2. response shaping  — raw openFDA JSON is reduced to the documented contract

Run from this directory:   pytest
(or from the repo root:     pytest mcp_server)
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import fda_client  # noqa: E402


# --- helpers ---------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


def patch_get(monkeypatch, responder):
    """Replace fda_client._get with a recording fake. `responder(path, params)`
    returns the canned JSON (or None for a 404 / no-results)."""
    calls = []

    async def _fake_get(path, params):
        calls.append({"path": path, "params": params})
        return responder(path, params)

    monkeypatch.setattr(fda_client, "_get", _fake_get)
    return calls


# --- resolve_drug ----------------------------------------------------------


def test_resolve_drug_brand_hit(monkeypatch):
    """A brand resolves to its MOST COMMON generic (count-based), and brands are
    pulled back for that generic."""

    def responder(path, params):
        search, count = params["search"], params["count"]
        # Step 1: most common generic for the brand.
        if search == 'brand_name:"Tylenol"' and count == "generic_name.exact":
            return {"results": [{"term": "ACETAMINOPHEN", "count": 500},
                                 {"term": "ACETAMINOPHEN, CAFFEINE", "count": 12}]}
        # Step 2: top brands for that generic.
        if search == 'generic_name:"ACETAMINOPHEN"' and count == "brand_name.exact":
            return {"results": [{"term": "TYLENOL", "count": 300},
                                 {"term": "TYLENOL PM", "count": 90}]}
        return None

    calls = patch_get(monkeypatch, responder)
    result = run(fda_client.resolve_drug("Tylenol"))

    assert result == {
        "generic_name": "ACETAMINOPHEN",
        "brand_names": ["TYLENOL", "TYLENOL PM"],
        "found": True,
        # No `meta` in the canned response → falls back to the canonical text.
        "disclaimer": fda_client.OPENFDA_DISCLAIMER,
    }
    # First lookup: count generic_name for the brand, on the NDC endpoint.
    assert calls[0]["path"] == "/drug/ndc.json"
    assert calls[0]["params"]["search"] == 'brand_name:"Tylenol"'
    assert calls[0]["params"]["count"] == "generic_name.exact"


def test_resolve_drug_falls_back_to_generic(monkeypatch):
    def responder(path, params):
        search, count = params["search"], params["count"]
        if search.startswith("brand_name:"):
            return None  # not a brand
        if search == 'generic_name:"ibuprofen"' and count == "generic_name.exact":
            return {"results": [{"term": "IBUPROFEN", "count": 400}]}
        if search == 'generic_name:"IBUPROFEN"' and count == "brand_name.exact":
            return {"results": [{"term": "ADVIL", "count": 200},
                                 {"term": "MOTRIN", "count": 150}]}
        return None

    calls = patch_get(monkeypatch, responder)
    result = run(fda_client.resolve_drug("ibuprofen"))

    assert result["found"] is True
    assert result["generic_name"] == "IBUPROFEN"
    assert result["brand_names"] == ["ADVIL", "MOTRIN"]
    # Tried brand_name first, then generic_name, then brands-for-generic.
    assert [c["params"]["search"] for c in calls] == [
        'brand_name:"ibuprofen"',
        'generic_name:"ibuprofen"',
        'generic_name:"IBUPROFEN"',
    ]


def test_resolve_drug_not_found(monkeypatch):
    patch_get(monkeypatch, lambda path, params: None)
    result = run(fda_client.resolve_drug("notadrug"))
    assert result == {
        "generic_name": None,
        "brand_names": [],
        "found": False,
        "disclaimer": fda_client.OPENFDA_DISCLAIMER,
    }


def test_resolve_drug_strips_injected_quotes(monkeypatch):
    calls = patch_get(monkeypatch, lambda path, params: None)
    run(fda_client.resolve_drug('foo" OR x'))
    # The user's quote is stripped so it can't break out of the phrase.
    assert calls[0]["params"]["search"] == 'brand_name:"foo OR x"'


# --- get_label -------------------------------------------------------------


def test_get_label_shapes_and_truncates(monkeypatch):
    monkeypatch.setattr(fda_client, "MAX_FIELD_CHARS", 20)
    long_text = "x" * 100

    def responder(path, params):
        return {"results": [{
            "indications_and_usage": [long_text],
            "boxed_warning": ["Serious risk."],
            "warnings_and_cautions": ["Be careful."],  # fallback field
            "dosage_and_administration": ["Take one."],
            "drug_interactions": ["Avoid Y."],
        }]}

    calls = patch_get(monkeypatch, responder)
    result = run(fda_client.get_label("acetaminophen"))

    assert calls[0]["path"] == "/drug/label.json"
    assert calls[0]["params"]["search"] == 'openfda.generic_name:"acetaminophen"'
    assert result["found"] is True
    assert result["boxed_warning"] == "Serious risk."
    # warnings falls back to warnings_and_cautions when `warnings` is absent.
    assert result["warnings"] == "Be careful."
    # Long field: first list element, truncated with a marker.
    assert result["indications"].endswith("…[truncated]")
    assert result["indications"].startswith("x" * 20)


def test_get_label_surfaces_live_disclaimer(monkeypatch):
    """When openFDA returns a `meta.disclaimer`, the tool relays that live value
    rather than the canonical fallback."""
    live = "LIVE openFDA disclaimer text."

    def responder(path, params):
        return {"meta": {"disclaimer": live}, "results": [{"boxed_warning": ["x"]}]}

    patch_get(monkeypatch, responder)
    result = run(fda_client.get_label("acetaminophen"))
    assert result["disclaimer"] == live


def test_disclaimer_falls_back_when_meta_absent():
    """No data (a 404 → None) yields the canonical disclaimer, never an empty
    string, so every tool result carries one."""
    assert fda_client._disclaimer(None) == fda_client.OPENFDA_DISCLAIMER
    assert fda_client._disclaimer({"results": []}) == fda_client.OPENFDA_DISCLAIMER


def test_get_label_not_found(monkeypatch):
    patch_get(monkeypatch, lambda path, params: None)
    assert run(fda_client.get_label("notadrug")) == {
        "found": False,
        "disclaimer": fda_client.OPENFDA_DISCLAIMER,
    }


# --- count_adverse_events --------------------------------------------------


def test_count_adverse_events_shape_and_query(monkeypatch):
    def responder(path, params):
        return {"results": [
            {"term": "NAUSEA", "count": 1200},
            {"term": "HEADACHE", "count": 800},
        ]}

    calls = patch_get(monkeypatch, responder)
    result = run(fda_client.count_adverse_events("acetaminophen", limit=2))

    assert calls[0]["path"] == "/drug/event.json"
    assert calls[0]["params"]["count"] == "patient.reaction.reactionmeddrapt.exact"
    assert calls[0]["params"]["limit"] == 2
    assert result == [
        {"reaction": "NAUSEA", "count": 1200},
        {"reaction": "HEADACHE", "count": 800},
    ]


def test_count_adverse_events_since_adds_date_range(monkeypatch):
    calls = patch_get(monkeypatch, lambda path, params: {"results": []})
    run(fda_client.count_adverse_events("acetaminophen", since="20230101"))
    assert "receivedate:[20230101 TO 30001231]" in calls[0]["params"]["search"]


def test_count_adverse_events_empty(monkeypatch):
    patch_get(monkeypatch, lambda path, params: None)
    assert run(fda_client.count_adverse_events("notadrug")) == []


# --- check_recalls ---------------------------------------------------------


def test_check_recalls_shape_and_query(monkeypatch):
    def responder(path, params):
        return {"results": [{
            "reason_for_recall": "Contamination",
            "classification": "Class I",
            "status": "Ongoing",
            "recall_initiation_date": "20240115",
            "recalling_firm": "Acme Pharma",
        }]}

    calls = patch_get(monkeypatch, responder)
    result = run(fda_client.check_recalls("acetaminophen"))

    assert calls[0]["path"] == "/drug/enforcement.json"
    assert result == [{
        "reason": "Contamination",
        "classification": "Class I",
        "status": "Ongoing",
        "date": "20240115",
        "firm": "Acme Pharma",
    }]


def test_check_recalls_since_adds_date_range(monkeypatch):
    calls = patch_get(monkeypatch, lambda path, params: {"results": []})
    run(fda_client.check_recalls("acetaminophen", since="20230101"))
    assert "report_date:[20230101 TO 30001231]" in calls[0]["params"]["search"]


def test_check_recalls_empty(monkeypatch):
    patch_get(monkeypatch, lambda path, params: None)
    assert run(fda_client.check_recalls("notadrug")) == []


# --- _get transport behavior ----------------------------------------------


def test_get_translates_404_to_none(monkeypatch):
    """A 404 from openFDA means 'no matching records' — not an error."""

    class FakeResponse:
        status_code = 404

        def raise_for_status(self):  # should not be called for 404
            raise AssertionError("raise_for_status called on a 404")

        def json(self):
            raise AssertionError("json() called on a 404")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, path, params=None):
            return FakeResponse()

    monkeypatch.setattr(fda_client.httpx, "AsyncClient", FakeClient)
    assert run(fda_client._get("/drug/label.json", {"search": "x"})) is None


def test_get_returns_json_on_200(monkeypatch):
    payload = {"results": [{"generic_name": "x"}]}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, path, params=None):
            return FakeResponse()

    monkeypatch.setattr(fda_client.httpx, "AsyncClient", FakeClient)
    assert run(fda_client._get("/drug/ndc.json", {"search": "x"})) == payload
