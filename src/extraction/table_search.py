"""Deterministic (non-LLM) extraction of labeled numeric values from
disclosure PDFs, via pdfplumber's table detection rather than raw
`extract_text()`.

Why tables and not plain text: Japanese financial statements are laid out
in two side-by-side blocks per page (資産の部 / 負債の部, or 当期/前期 side
by side), and `extract_text()` reading order can interleave those columns
in ways that put a label far from its value. `extract_tables()` respects
the detected grid, so a label and its numbers stay on the same row.

This module implements the "決定的処理を一次抽出手段とする" half of the
project's hybrid design (see handoff notes). It is deliberately narrow:
one label -> the most recent fiscal year's numeric value, with the
evidence (page, row) attached so a human (or the LLM verification layer,
not yet implemented) can audit it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pdfplumber

NUMBER_RE = re.compile(r"-?△?[0-9][0-9,]*(?:\.[0-9]+)?")

# A substring match like "総資産" inside "従業員1人当たり総資産" is numerically
# plausible but a different metric, not a formatting variant. These are the
# modifier fragments observed in practice; a substring match is rejected
# outright if the surrounding text (label minus the matched substring)
# contains any of them, rather than trying to numerically detect the
# semantic error after the fact.
DISQUALIFYING_MODIFIERS = [
    "1人当たり", "1件当たり", "従業員",  # per-employee / per-something ratios
    "その他",  # "other X" sub-line, not the X total itself
    "税引前", "税引後",  # pre/post-tax variants of a profit line
    "の減少", "の増加", "の増減", "繰入額", "戻入額",  # flow/movement lines, not the balance itself
    "比率", "割合", "構成比",  # ratio/percentage columns misidentified as the label
]


@dataclass
class FoundValue:
    label_matched: str
    value: float
    page_index: int
    row_text: list  # the raw row cells, for auditing
    all_numbers_in_row: list  # every number found, in row order

    @property
    def plausible(self) -> bool:
        """Cheap sanity check for BS/PL headline yen figures: a genuine
        total for these companies' scale is never a 1-3 digit number.
        Values below this are near-certainly a stray percentage/ratio/count
        column picked up by mistake (observed repeatedly: "100" from a
        composition-ratio column, small counts from an adjacent index).
        Not a claim of correctness — just "not an obvious miss" — real
        verification for anything under this line should go through the
        LLM fallback (see llm_verify.py), not be trusted as-is.
        """
        return abs(self.value) >= 1000


def _parse_number(token: str) -> Optional[float]:
    """Parse a Japanese financial-table number token.

    Handles: thousands commas, a leading △ (triangle) meaning negative
    (standard Japanese accounting convention for negative amounts,
    distinct from a plain hyphen), and plain "-" as "not applicable"
    (returns None, not 0 — a missing value must not be silently
    conflated with an actual zero).
    """
    token = token.strip()
    if token in ("", "-", "−", "―", "*", "－"):
        return None
    negative = token.startswith("△") or token.startswith("▲")
    cleaned = token.lstrip("△▲").replace(",", "").replace("　", "")
    if not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", cleaned):
        return None
    value = float(cleaned)
    return -value if negative else value


def find_labeled_row(
    pdf_path: Path,
    label: str,
    page_range: Optional[range] = None,
    exact: bool = False,
) -> Optional[FoundValue]:
    """Search every table on every page (or just `page_range`) for a row
    whose first non-empty cell contains `label`. Returns the FIRST match
    (callers needing every match should use `find_all_labeled_rows`).

    `exact=False` (default) does substring matching so e.g. searching for
    "支払備金" also matches a row literally labeled "支払備金合計" — useful,
    but means a caller after a specific sub-line item should pass a more
    specific label or post-filter `row_text`.
    """
    for match in find_all_labeled_rows(pdf_path, label, page_range, exact):
        return match
    return None


def _normalize_label_text(text: str) -> str:
    return (text or "").replace("\n", "").replace(" ", "").replace("　", "")


def _extract_numbers(cell_line: str) -> list[float]:
    numbers = []
    for m in re.finditer(r"[△▲]?-?[0-9][0-9,]*(?:\.[0-9]+)?", cell_line):
        v = _parse_number(m.group())
        if v is not None:
            numbers.append(v)
    return numbers


def _sub_rows(row: list) -> list[tuple[str, list]]:
    """Expand a pdfplumber table row into (label_line, [value_cells...])
    pairs, handling the common Japanese-disclosure layout where many line
    items share one physical row with no horizontal rule between them —
    the label cell and each value cell instead each contain N stacked
    lines separated by '\\n' (verified structure: 三井住友海上 f01.pdf p.16).

    Alignment strategy per value cell, in order of preference:
    - same line count as the label cell -> positional 1:1 zip
    - exactly one line -> a single total, usually belonging to the FIRST
      label line (the row's own header/subtotal item; sub-items' totals
      live in the multi-line cell instead). Paired with label_lines[0].
    - line count == label count - 1 -> sub-items only, no cell for the
      header line itself; zip against label_lines[1:].
    - anything else -> skip this cell for positional pairing (still
      reachable via the whole-cell fallback below).
    """
    label_cell = row[0] or ""
    label_lines = [l for l in label_cell.split("\n")] or [label_cell]
    n_labels = len(label_lines)

    # label_line -> accumulated list of raw cell-line strings (one per
    # value column, so a downstream caller can pick e.g. the last for
    # "most recent fiscal year").
    acc: dict[int, list[str]] = {idx: [] for idx in range(n_labels)}

    for cell in row[1:]:
        if not cell:
            continue
        value_lines = cell.split("\n")
        if len(value_lines) == n_labels:
            for idx, vline in enumerate(value_lines):
                acc[idx].append(vline)
        elif len(value_lines) == 1:
            acc[0].append(value_lines[0])
        elif len(value_lines) == n_labels - 1:
            for idx, vline in enumerate(value_lines, start=1):
                acc[idx].append(vline)
        # else: ambiguous alignment for this cell, skip it rather than
        # guess a pairing that could silently attach the wrong number.

    return [(label_lines[idx], acc[idx]) for idx in range(n_labels)]


def find_all_labeled_rows(
    pdf_path: Path,
    label: str,
    page_range: Optional[range] = None,
    exact: bool = False,
) -> list[FoundValue]:
    results: list[FoundValue] = []
    label_norm = label.replace(" ", "")
    with pdfplumber.open(pdf_path) as pdf:
        indices = page_range if page_range is not None else range(len(pdf.pages))
        for i in indices:
            if i >= len(pdf.pages):
                break
            page = pdf.pages[i]
            try:
                tables = page.extract_tables()
            except Exception:
                continue
            for table in tables:
                for row in table:
                    if not row:
                        continue
                    for label_line, value_cell_lines in _sub_rows(row):
                        first_cell = _normalize_label_text(label_line)
                        row_matches = (
                            first_cell == label_norm if exact else label_norm in first_cell
                        )
                        if not row_matches:
                            continue
                        numbers = []
                        for vline in value_cell_lines:
                            numbers.extend(_extract_numbers(vline))
                        if numbers:
                            results.append(
                                FoundValue(
                                    label_matched=first_cell,
                                    value=numbers[-1],  # rightmost column = most recent FY
                                    page_index=i,
                                    row_text=[label_line, *value_cell_lines],
                                    all_numbers_in_row=numbers,
                                )
                            )
    return results


def find_many_labeled_rows(
    pdf_path: Path, labels: list[str], page_range: Optional[range] = None
) -> dict[str, Optional[FoundValue]]:
    """Single-pass version of `find_labeled_row` for multiple labels at
    once. Table extraction is the expensive step (per page); doing it once
    per document instead of once per label matters a lot on 150-300 page
    documents.

    Matching is exact-first, substring-fallback (per label independently):
    a plain substring search on "当期純利益" also matches "税引前当期純利益"
    and "経常利益" also matches "経常利益の減少額" — both real cases seen in
    ms_sompo/sompo_japan/sbi_ikiiki_ssi. Scanning the whole page range once
    for exact matches before falling back to substring for any label still
    unmatched avoids that without giving up substring matching entirely
    (still needed for e.g. "総資産" vs "総資産額").
    """
    labels_norm = {label: label.replace(" ", "") for label in labels}
    # Four tiers, best to worst: exact label match with a plausible value,
    # substring match with a plausible value, exact match with an
    # implausible value (kept only as a fallback), substring+implausible.
    tiers: dict[str, dict[str, Optional[FoundValue]]] = {
        "exact_ok": {label: None for label in labels},
        "sub_ok": {label: None for label in labels},
        "exact_bad": {label: None for label in labels},
        "sub_bad": {label: None for label in labels},
    }

    def _done(label: str) -> bool:
        return tiers["exact_ok"][label] is not None

    with pdfplumber.open(pdf_path) as pdf:
        indices = page_range if page_range is not None else range(len(pdf.pages))
        for i in indices:
            if i >= len(pdf.pages):
                break
            if all(_done(label) for label in labels):
                break
            page = pdf.pages[i]
            try:
                tables = page.extract_tables()
            except Exception:
                continue
            for table in tables:
                for row in table:
                    if not row:
                        continue
                    for label_line, value_cell_lines in _sub_rows(row):
                        first_cell = _normalize_label_text(label_line)
                        for label, label_norm in labels_norm.items():
                            if _done(label):
                                continue
                            is_exact = first_cell == label_norm
                            is_sub = label_norm in first_cell
                            if not (is_exact or is_sub):
                                continue
                            if is_sub and not is_exact:
                                surrounding = first_cell.replace(label_norm, "")
                                if any(m in surrounding for m in DISQUALIFYING_MODIFIERS):
                                    continue
                            numbers = []
                            for vline in value_cell_lines:
                                numbers.extend(_extract_numbers(vline))
                            if not numbers:
                                continue
                            found = FoundValue(
                                label_matched=first_cell,
                                value=numbers[-1],
                                page_index=i,
                                row_text=[label_line, *value_cell_lines],
                                all_numbers_in_row=numbers,
                            )
                            tier = ("exact" if is_exact else "sub") + (
                                "_ok" if found.plausible else "_bad"
                            )
                            if tiers[tier][label] is None:
                                tiers[tier][label] = found

    result: dict[str, Optional[FoundValue]] = {}
    for label in labels:
        result[label] = (
            tiers["exact_ok"][label]
            or tiers["sub_ok"][label]
            or tiers["exact_bad"][label]
            or tiers["sub_bad"][label]
        )
    return result


if __name__ == "__main__":
    import sys
    import time

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    targets = ["総資産", "純資産", "経常収益", "経常利益", "当期純利益"]
    cache_dir = Path(__file__).resolve().parent.parent.parent / "data" / "raw"

    for company_id in [
        "nippon_life",
        "meiji_yasuda",
        "ms_sompo",
        "sompo_japan",
        "sbi_ikiiki_ssi",
        "sakura_ssi",
    ]:
        path = cache_dir / f"{company_id}.pdf"
        t0 = time.time()
        # Headline BS/PL figures are always near the front of these
        # documents in practice (see per-company page numbers already
        # confirmed during manual inspection); cap the scan so a 292-page
        # file (sompo_japan) doesn't force a full-document table pass here.
        hits = find_many_labeled_rows(path, targets, page_range=range(0, 40))
        elapsed = time.time() - t0
        print(f"=== {company_id} ({elapsed:.1f}s) ===")
        for label, found in hits.items():
            if found:
                print(f"  {label}: {found.value:,.0f} (page {found.page_index}, matched='{found.label_matched}')")
            else:
                print(f"  {label}: NOT FOUND in first 40 pages")
