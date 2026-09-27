"""robots.txt check, run per-URL at fetch time (not just once at design time).

**Finding (2026-09-27, discovered while wiring up the 6 PDF sources):**
`urllib.robotparser`'s built-in `.read()` uses `urllib.request`'s default
User-Agent string ("Python-urllib/3.x"). At least two of our target sites
(nissay.co.jp, meijiyasuda.co.jp) return HTTP 403 specifically for that
default string via their WAF, while responding normally (200 with real rules,
or a genuine 404 meaning "no robots.txt") to any other client — verified by
comparing curl with/without an explicit `-A "Python-urllib/3.12"` override.
robotparser treats any 401/403 on robots.txt itself as "disallow everything,"
so using its default `.read()` silently produces false "blocked" verdicts
that have nothing to do with the site's actual policy.

The fix is to identify ourselves honestly with our own descriptive
User-Agent (never spoof a browser) when fetching robots.txt, and feed the
body to `RobotFileParser.parse()` ourselves so we control exactly how each
HTTP status is interpreted.
"""

from __future__ import annotations

from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

USER_AGENT = "insurance-disclosure-bi/0.1 (personal research project; github.com/toshi950/insurance-disclosure-bi)"


def is_fetch_allowed(url: str, user_agent: str = USER_AGENT) -> bool:
    """Return True if robots.txt (if any) permits fetching `url`.

    Status handling (mirrors the convention `urllib.robotparser` itself
    documents, applied here with an honest identifying UA instead of its
    default one):
    - 200: parse the body and evaluate normally.
    - 404 (or any other 4xx that isn't 401/403): no robots.txt found →
      fail open (allowed). This matched observed behavior for a genuine
      "no robots.txt" site (meijiyasuda.co.jp) during verification.
    - 401/403: treat as an explicit "disallow everything" signal — but
      only once confirmed it isn't itself a User-Agent-based block (this
      function already sends an honest, non-default UA, so a 401/403 here
      is much more likely to be a real signal than robotparser's default
      behavior would produce).
    - Any other failure (timeout, connection error, 5xx): fail closed
      (not allowed) — don't guess; investigate instead of silently
      proceeding on a transient issue.
    """
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    try:
        resp = requests.get(
            robots_url, headers={"User-Agent": user_agent}, timeout=10
        )
    except requests.RequestException:
        return False

    if resp.status_code == 200:
        parser = RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser.can_fetch(user_agent, url)

    if resp.status_code in (401, 403):
        return False

    if 400 <= resp.status_code < 500:
        # No robots.txt on this host (404 and similar) -> no restriction.
        return True

    # 5xx or anything unexpected: don't guess.
    return False
