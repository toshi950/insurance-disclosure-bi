"""Human-readable CSV outputs and the human override layer.

  data/output/extracted.csv   what the pipeline extracted (regenerated every run, never edited)
  data/overrides.csv          what a person decided or corrected (persists across runs, edited by hand)
  data/output/final.csv       extracted + overrides merged; this is what the BI layer reads
  data/output/final_wide.csv  the same values as a company-by-field grid for quick reading in Excel

Rows are long format (one row per company x field) so every value carries its unit, entity basis,
period, source document/page, extraction method and review flags. The CSVs hold figures, so they live
under data/ (untracked) like the PDFs.

overrides.csv columns:
  company_id, field
  decision                   ok | no_disclosure | needs_fix | override
  value                      the corrected value (decision = override only)
  expected_extracted_value   the extracted value the person looked at (blank = nothing was extracted).
                             If the extraction later changes, the decision is reported as stale.
  reason, date
"""

from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema import CommonCore, Industry, LifeExtension, NonLifeExtension  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "output"
OVERRIDES = ROOT / "data" / "overrides.csv"
OVERRIDE_COLUMNS = ["company_id", "field", "decision", "value", "expected_extracted_value", "reason", "date"]
DECISIONS = ("ok", "no_disclosure", "needs_fix", "override")
PERCENT_FIELDS = {"solvency_ratio", "loss_ratio", "expense_ratio", "combined_ratio", "lapse_rate"}

STATUS_AUTO = "自動抽出"
STATUS_REVIEW = "要確認"
STATUS_CONFIRMED = "人が確認済み"
STATUS_OVERRIDDEN = "人が修正"
STATUS_GAP = "未取得（要確認）"
STATUS_GAP_CONFIRMED = "資料に値なし（確認済み）"
STATUS_NA = "対象外（制度上該当なし）"
STATUS_ND = "開示前"
STATUS_NEEDS_FIX = "要修正（記録済み）"


def field_catalog(industry: str) -> list[tuple[str, str, str]]:
    """(field, Japanese label, unit) for every numeric field the schema defines for `industry`."""
    models = [CommonCore, {"life": LifeExtension}.get(industry, NonLifeExtension)]
    out = []
    for model in models:
        for name, f in model.model_fields.items():
            if name.endswith(("_basis", "_provenance")) or "Optional[float]" not in str(f.annotation) and "float" not in str(f.annotation):
                continue
            label = (f.description or name).split("（")[0].split("。")[0]
            out.append((name, label, "%" if name in PERCENT_FIELDS else "百万円"))
    return out


# ---------------------------------------------------------------------------
# overrides
# ---------------------------------------------------------------------------

def load_overrides() -> dict[tuple[str, str], dict]:
    if not OVERRIDES.exists():
        return {}
    with OVERRIDES.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return {(r["company_id"], r["field"]): r for r in rows if r.get("company_id") and r.get("field")}  # last row wins


def append_override(company_id: str, field: str, decision: str, value: Optional[float],
                    expected: Optional[float], reason: str) -> None:
    if decision not in DECISIONS:
        raise SystemExit(f"decision must be one of {DECISIONS}")
    OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
    new = not OVERRIDES.exists()
    with OVERRIDES.open("a", encoding="utf-8-sig" if new else "utf-8", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(OVERRIDE_COLUMNS)
        w.writerow([company_id, field, decision, "" if value is None else value,
                    "" if expected is None else expected, reason, date.today().isoformat()])


def _num(s: Optional[str]) -> Optional[float]:
    if s is None or str(s).strip() == "":
        return None
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def same(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= max(1e-9, 1e-6 * abs(a))


def override_state(ov: Optional[dict], extracted: Optional[float]) -> str:
    """'none' | 'current' | 'stale' for a decision relative to today's extracted value."""
    if not ov:
        return "none"
    return "current" if same(_num(ov.get("expected_extracted_value")), extracted) else "stale"


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------

REVIEW_FLAGS = {"source_mismatch", "image_read", "image_majority", "consolidated_substitute", "loose_grounding"}


def build_rows(data: list[dict]) -> tuple[list[dict], list[str]]:
    overrides = load_overrides()
    rows, warnings = [], []
    for entry in data:
        rec = entry["record"]
        cid = rec["company_id"]
        prov = rec.get("field_provenance", {})
        gaps = set(entry.get("gaps", []))
        parts = {**rec.get("common", {}), **(rec.get("life_ext") or {}), **(rec.get("non_life_ext") or {}), **(rec.get("ssi_ext") or {})}
        for name, label, unit in field_catalog(rec["industry"]):
            p = prov.get(name, {})
            method = p.get("method", "")
            value = parts.get(name)
            value = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
            flags = [f for f in p.get("flags", []) if f in REVIEW_FLAGS]
            ov = overrides.get((cid, name))
            state = override_state(ov, value)
            decision = ov["decision"] if ov else ""
            final, status, override_reason, override_date = value, "", "", ""
            if state == "stale":
                warnings.append(f"{cid}:{name} の人の記録（{decision}）は古い（抽出値が変わった）")
            if ov and decision == "override" and _num(ov.get("value")) is not None:
                final, status = _num(ov["value"]), STATUS_OVERRIDDEN
                override_reason, override_date = ov.get("reason", ""), ov.get("date", "")
            elif method == "not_applicable":
                status = STATUS_NA
            elif method == "not_disclosed" and value is None:
                status = STATUS_ND
            elif value is None:
                status = STATUS_GAP_CONFIRMED if state == "current" and decision == "no_disclosure" else STATUS_GAP
            elif state == "current" and decision == "ok":
                status = STATUS_CONFIRMED
            elif state != "none" and decision == "needs_fix":
                status = STATUS_NEEDS_FIX
            else:
                status = STATUS_REVIEW if flags else STATUS_AUTO
            rows.append({
                "company_id": cid, "company_name": rec["company_name"], "industry": rec["industry"],
                "source_type": rec["source_type"], "field": name, "label": label, "unit": unit,
                "final_value": "" if final is None else final, "status": status,
                "extracted_value": "" if value is None else value,
                "basis": p.get("basis", ""), "period": p.get("period", ""), "source_doc": p.get("source_doc", ""),
                "source_page": "" if p.get("source_page") is None else p["source_page"] + 1,  # PDF viewer page (1-based)
                "method": method, "flags": ";".join(flags),
                "note": p.get("note", "") if value is not None or method in ("not_applicable", "not_disclosed") else entry.get("gap_reasons", {}).get(name, ""),
                "override_reason": override_reason, "override_date": override_date,
            })
    return rows, warnings


FINAL_COLUMNS = ["company_id", "company_name", "industry", "source_type", "field", "label", "unit", "final_value",
                 "status", "extracted_value", "basis", "period", "source_doc", "source_page", "method", "flags",
                 "note", "override_reason", "override_date"]
EXTRACTED_COLUMNS = ["company_id", "company_name", "industry", "source_type", "field", "label", "unit",
                     "extracted_value", "basis", "period", "source_doc", "source_page", "method", "flags", "note"]


def _write(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:  # BOM so Excel opens Japanese text correctly
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_csvs(rows: list[dict]) -> dict[str, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    paths = {"extracted": OUT_DIR / "extracted.csv", "final": OUT_DIR / "final.csv", "wide": OUT_DIR / "final_wide.csv"}
    _write(paths["extracted"], EXTRACTED_COLUMNS, rows)
    _write(paths["final"], FINAL_COLUMNS, rows)
    # wide grid: rows = fields, columns = companies; non-values show the status text
    companies = list(dict.fromkeys(r["company_name"] for r in rows))
    fields = list(dict.fromkeys((r["field"], r["label"], r["unit"]) for r in rows))
    by = {(r["company_name"], r["field"]): r for r in rows}
    grid = []
    for name, label, unit in fields:
        row = {"項目": f"{label}（{unit}）", "field": name}
        for c in companies:
            r = by.get((c, name))
            row[c] = "" if r is None else (r["final_value"] if r["final_value"] != "" else r["status"])
        grid.append(row)
    _write(paths["wide"], ["項目", "field"] + companies, grid)
    return paths
