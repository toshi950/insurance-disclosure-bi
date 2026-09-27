"""Fetch + cover-page verification for both EDINET-PDF and company-PDF
sources. Both source types converge on the same shape once downloaded: a
local PDF file whose claimed identity (company name, fiscal year) must be
confirmed from the document itself before anything is extracted from it.

Rationale for the cover-page check (see handoff notes, 2026-09-23 実地検証):
WebSearch's company-name attribution for EDINET document IDs was wrong three
times in a row during manual verification (holding company vs. operating
subsidiary mix-ups, and one outright unrelated company). Any URL — whether
guessed from a search result or hand-entered in `companies.py` — is treated
as an unverified claim until the fetched PDF's own cover page confirms it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pdfplumber
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from companies import CompanySpec  # noqa: E402
from robots_check import USER_AGENT, is_fetch_allowed  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "raw"


class FetchError(Exception):
    pass


class RobotsDisallowed(FetchError):
    pass


def fetch_pdf(spec: CompanySpec, cache_dir: Path = CACHE_DIR, force: bool = False) -> Path:
    """Download `spec`'s PDF (EDINET or company disclosure) to the cache,
    honoring robots.txt. Returns the local path. Does not re-download if
    already cached, unless `force=True`.
    """
    if spec.pdf_url is None:
        raise FetchError(f"{spec.company_id}: no pdf_url set in companies.py")

    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / f"{spec.company_id}.pdf"
    if dest.exists() and not force:
        return dest

    if not is_fetch_allowed(spec.pdf_url):
        raise RobotsDisallowed(
            f"{spec.company_id}: robots.txt disallows fetching {spec.pdf_url} "
            "(or the check failed closed — see robots_check.py docstring)"
        )

    resp = requests.get(
        spec.pdf_url, headers={"User-Agent": USER_AGENT}, timeout=60
    )
    resp.raise_for_status()
    if "pdf" not in resp.headers.get("content-type", "").lower():
        raise FetchError(
            f"{spec.company_id}: response content-type "
            f"{resp.headers.get('content-type')!r} does not look like a PDF"
        )

    dest.write_bytes(resp.content)
    return dest


def _normalize(name: str) -> str:
    """Loosely normalize a company name for matching: strip legal-entity
    suffixes/prefixes and whitespace so trivial formatting differences
    (full-width space, "株式会社" placement) don't cause false negatives.
    """
    stripped = re.sub(r"(株式会社|相互会社|有限責任|合同会社)", "", name)
    stripped = re.sub(r"\s+", "", stripped)
    return stripped


def verify_cover_page(pdf_path: Path, expected_company_name: str, scan_pages: int = 5) -> dict:
    """Confirm the document is actually about `expected_company_name`.

    Two evidence sources, in order:
    1. Page text (first `scan_pages` pages) — works for main-volume booklets
       that print a title page with the full legal name.
    2. PDF metadata (`/Author`, `/Title`) — needed for supplementary
       sub-volumes that skip a named cover and start straight into tables
       (confirmed case: 明治安田生命's "業績に関する諸資料" volume, whose
       page 1 is already the balance sheet with no company name in the
       visible text, but whose /Author field correctly identifies it).

    A metadata-only match is recorded as such (`matched_via: "metadata"`)
    rather than silently treated identically to a page-text match, since
    it's a weaker signal (metadata can in principle be wrong or stale in a
    way visible body text is not) — callers that want to be strict can
    check `matched_via` and require `"page_text"`.
    """
    target = _normalize(expected_company_name)
    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)
        for i in range(min(scan_pages, n)):
            text = pdf.pages[i].extract_text() or ""
            if target in _normalize(text):
                return {
                    "verified": True,
                    "matched_via": "page_text",
                    "matched_page": i,
                    "total_pages": n,
                }

        metadata = pdf.metadata or {}
        meta_text = " ".join(str(v) for v in metadata.values())
        if target in _normalize(meta_text):
            return {
                "verified": True,
                "matched_via": "metadata",
                "matched_page": None,
                "total_pages": n,
                "metadata": metadata,
            }

    return {
        "verified": False,
        "matched_via": None,
        "matched_page": None,
        "total_pages": n,
    }


if __name__ == "__main__":
    # Smoke test against the 6 already-downloaded PDF-source companies plus
    # a re-fetch check for the 2 EDINET ones (skipped: EDINET's robots.txt
    # question is still open, see conversation — do not fetch those here).
    from companies import COMPANIES, SourceType

    for spec in COMPANIES:
        if spec.source_type is not SourceType.PDF:
            continue
        try:
            path = fetch_pdf(spec)
        except FetchError as e:
            print(f"{spec.company_id}: FETCH FAILED - {e}")
            continue
        result = verify_cover_page(path, spec.company_name)
        print(f"{spec.company_id}: {result}")
