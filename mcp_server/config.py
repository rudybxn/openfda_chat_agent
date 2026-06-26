"""Configuration for the openFDA MCP server.

The openFDA API key is read from the environment and lives ONLY in this
service. It is never passed to the agent or the frontend. openFDA works
without a key, but an unauthenticated client is rate-limited to 240
requests/minute and 1,000/day; a free key raises this to 240/min and
120,000/day. See https://open.fda.gov/apis/authentication/.
"""

import os

OPENFDA_API_KEY = os.environ.get("OPENFDA_API_KEY", "")
OPENFDA_BASE_URL = os.environ.get("OPENFDA_BASE_URL", "https://api.fda.gov")

# Long free-text label fields are truncated to this many characters before
# being returned to the agent, so a single tool call can't blow the LLM
# context window.
MAX_FIELD_CHARS = int(os.environ.get("MAX_FIELD_CHARS", "1500"))

# openFDA returns an identical disclaimer in `meta.disclaimer` on every
# response. We surface the LIVE value per request (see fda_client._disclaimer),
# but keep the canonical text as a fallback so a tool can always return a
# disclaimer even when a call yields no records (a 404 has no `meta` block).
OPENFDA_DISCLAIMER = os.environ.get(
    "OPENFDA_DISCLAIMER",
    "Do not rely on openFDA to make decisions regarding medical care. While we "
    "make every effort to ensure that data is accurate, you should assume all "
    "results are unvalidated. We may limit or otherwise restrict your access to "
    "the API in line with our Terms of Service.",
)
