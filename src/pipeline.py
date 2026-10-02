"""Run both extraction paths over all 8 target companies and write the unified
records to data/processed/records.json.

EDINET companies -> extraction/edinet_source.py (structured XBRL, individual basis)
PDF companies    -> extraction/pdf_fields.py (grounded LLM read; image double-read
                    where the text layer is garbled)

Cross-field sanity checks run last; a failing check is reported next to the record,
never silently "fixed". Derived values (the combined ratio) are computed here in
code from grounded components, never taken from a model.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from companies import COMPANIES  # noqa: E402
from extraction import edinet_source, pdf_fields  # noqa: E402
from schema import CompanyRecord, ExtractionMethod, Industry, Provenance, SourceType  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "data" / "processed" / "records.json"


def _flat(record: CompanyRecord) -> dict:
    """Every populated field as {name: value}, across common + the active extension."""
    out = {k: v for k, v in record.common.model_dump(exclude_none=True).items() if isinstance(v, (int, float))}
    ext = record.life_ext or record.non_life_ext or record.ssi_ext
    if ext is not None:
        out.update({k: v for k, v in ext.model_dump(exclude_none=True).items() if isinstance(v, (int, float))})
    return out


def apply_manual_values(record: CompanyRecord, spec) -> None:
    for name, value, note in spec.manual_values:
        if pdf_fields._is_empty(record, name):
            target = record.common if hasattr(record.common, name) else record.extension()
            setattr(target, name, value)
            record.field_provenance[name] = Provenance(method=ExtractionMethod.MANUAL_VERIFIED, note=note)


def apply_not_applicable(record: CompanyRecord, spec) -> None:
    for name, note in spec.not_applicable:
        record.field_provenance[name] = Provenance(method=ExtractionMethod.NOT_APPLICABLE, note=note)


def derive_combined_ratio(record: CompanyRecord) -> None:
    ext = record.non_life_ext or record.ssi_ext
    if ext is None:
        return
    loss, expense = getattr(ext, "loss_ratio", None), getattr(ext, "expense_ratio", None)
    if loss is not None and expense is not None and getattr(ext, "combined_ratio", None) is None:
        ext.combined_ratio = round(loss + expense, 2)
        record.field_provenance["combined_ratio"] = Provenance(
            method=ExtractionMethod.DETERMINISTIC, note="損害率＋事業費率（コード側で計算）"
        )


def sanity_checks(record: CompanyRecord) -> list[str]:
    f = _flat(record)
    problems = []
    ta, na = f.get("total_assets"), f.get("net_assets")
    if ta is not None and na is not None and not (0 < na < ta):
        problems.append(f"net_assets {na} not within (0, total_assets {ta})")
    oi, op = f.get("ordinary_income"), f.get("ordinary_profit")
    if oi is not None and op is not None and op > oi:
        problems.append(f"ordinary_profit {op} exceeds ordinary_income {oi}")
    if ta is not None:
        for k in ("policy_reserve_total", "claims_reserve_total"):
            if f.get(k) is not None and f[k] > ta:
                problems.append(f"{k} {f[k]} exceeds total_assets {ta}")
    for k in ("loss_ratio", "expense_ratio", "lapse_rate"):
        if f.get(k) is not None and not (0 <= f[k] <= 150):
            problems.append(f"{k} {f[k]} outside 0-150%")
    sr = f.get("solvency_ratio")
    if sr is not None and not (50 <= sr <= 10000):
        problems.append(f"solvency_ratio {sr} implausible")
    return problems


def method_counts(items: list[dict]) -> dict[str, int]:
    """How many populated numeric fields came from each extraction path (provenance method).
    Published in the README as an honesty measure: it shows how much is machine-extracted vs
    human-confirmed."""
    counts: dict[str, int] = {}
    for item in items:
        rec = CompanyRecord.model_validate(item["record"])
        for name in _flat(rec):
            m = rec.field_provenance.get(name)
            key = m.method.value if m else "derived"
            counts[key] = counts.get(key, 0) + 1
    return counts


def build_all() -> list[dict]:
    results = []
    for spec in COMPANIES:
        if spec.source_type is SourceType.EDINET:
            ex = edinet_source.extract(spec.company_id)
            record = ex.record
            for name in _flat(record):
                record.field_provenance.setdefault(name, Provenance(method=ExtractionMethod.EDINET_XBRL))
            # What EDINET XBRL cannot supply is read from the company's own disclosure booklets.
            supp = pdf_fields.run_supplement(spec.company_id, record)
            gaps = [n for n in ex.missing if pdf_fields._is_empty(record, n)] + [
                n for n, r in supp.items() if not r.ok and pdf_fields._is_empty(record, n) and n not in ex.missing]
        else:
            record, pdf_results = pdf_fields.run_company(spec.company_id)
            gaps = [n for n, r in pdf_results.items() if not r.ok]
        apply_manual_values(record, spec)
        apply_not_applicable(record, spec)
        na = {n for n, _ in spec.not_applicable}
        gaps = [n for n in gaps if pdf_fields._is_empty(record, n) and n not in na]
        derive_combined_ratio(record)
        results.append({
            "record": record.model_dump(mode="json", exclude_none=True),
            "gaps": gaps,
            "sanity_problems": sanity_checks(record),
        })
    return results


if __name__ == "__main__":
    data = build_all()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {OUT.relative_to(OUT.parent.parent.parent)}")
    print("  extraction paths:", method_counts(data))
    for item in data:
        rec = item["record"]
        n = len(_flat(CompanyRecord.model_validate(rec)))
        print(f"  {rec['company_id']:<22} {n:>2} numeric fields | gaps {len(item['gaps']):>2} | sanity: {item['sanity_problems'] or 'ok'}")
