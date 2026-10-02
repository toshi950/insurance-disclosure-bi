"""Exception report for items a person should look at before trusting them.

`generate(data)` turns the pipeline output into a self-contained HTML file (page images embedded)
under data/review/. Like the PDFs and extracted data it is local only (data/ is untracked).

An item is flagged when one of these holds (chosen to keep the report short):
  gap                    nothing was extracted and the reason is not "not applicable / not yet disclosed"
  source_mismatch        two sources (EDINET and the company booklet) disagree
  image_read             the value was read from a page image (text layer unusable)
  image_majority         ...and one of the (up to three) reads disagreed
  consolidated_substitute the value is consolidated, not the individual-entity figure
  loose_grounding        the quoted evidence only passed the loose, layout-tolerant check

Decisions already made by a person are kept in data/review/ledger.json and suppress an item until
its value changes:

    python src/review.py                                 # regenerate the report
    python src/review.py confirm <company_id> <field> ok|no_disclosure|needs_fix [note ...]
"""

from __future__ import annotations

import base64
import html
import io
import json
import sys
from datetime import date
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
REVIEW_DIR = ROOT / "data" / "review"
LEDGER = REVIEW_DIR / "ledger.json"
RECORDS = ROOT / "data" / "processed" / "records.json"
RAW_DIR = ROOT / "data" / "raw"
MAX_PAGES_PER_ITEM = 2

PERCENT_FIELDS = {"solvency_ratio", "loss_ratio", "expense_ratio", "combined_ratio", "lapse_rate"}
FLAG_LABELS = {
    "gap": "未取得",
    "source_mismatch": "情報源どうしで値が不一致",
    "image_read": "画像読取（テキスト層から取れないページ）",
    "image_majority": "画像読取：複数回の読取のうち1回が不一致",
    "consolidated_substitute": "連結値を採用（単体ではない）",
    "loose_grounding": "引用の照合が緩い基準で通過（段組・複数行）",
}
DECISIONS = {"ok": "確認済み・値は正しい", "no_disclosure": "確認済み・資料に値が無い", "needs_fix": "要修正"}


def _flat(record: dict) -> dict:
    out = {}
    for part in (record.get("common", {}), record.get("life_ext") or {}, record.get("non_life_ext") or {}, record.get("ssi_ext") or {}):
        out.update({k: v for k, v in part.items() if isinstance(v, (int, float)) and not isinstance(v, bool)})
    return out


def load_ledger() -> dict:
    if LEDGER.exists():
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    return {}


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= max(1e-9, 1e-6 * abs(a))


def collect_items(data: list[dict], ledger: Optional[dict] = None) -> tuple[list[dict], list[dict]]:
    """Returns (items to review, items already confirmed in the ledger and unchanged)."""
    ledger = load_ledger() if ledger is None else ledger
    items, confirmed = [], []
    for entry in data:
        rec = entry["record"]
        cid = rec["company_id"]
        values = _flat(rec)
        prov = rec.get("field_provenance", {})
        for name in entry.get("gaps", []):
            items.append({"company_id": cid, "company_name": rec["company_name"], "field": name, "value": None,
                          "flags": ["gap"], "reason": entry.get("gap_reasons", {}).get(name, ""), "prov": {}})
        for name, value in values.items():
            p = prov.get(name, {})
            flags = [f for f in p.get("flags", []) if f in FLAG_LABELS]
            if flags:
                items.append({"company_id": cid, "company_name": rec["company_name"], "field": name, "value": value,
                              "flags": sorted(set(flags)), "reason": "", "prov": p})
    pending = []
    for it in items:
        led = ledger.get(f"{it['company_id']}:{it['field']}")
        if led and _same(led.get("value"), it["value"]):
            it["decision"] = led
            confirmed.append(it)
        else:
            pending.append(it)
    return pending, confirmed


def _group_for(field: str):
    from extraction import pdf_fields as P

    for g in P.GROUPS:
        if field in P._targets(g):
            return g
    return None


def _pages_for(item: dict) -> list[tuple[str, int, str]]:
    """(doc key, 0-based page index, caption). Flagged values point at their source page; gaps at the
    pages the extractor would have read for that field."""
    from companies import BY_ID
    from extraction import pdf_fields as P

    p = item["prov"]
    if p.get("source_doc") is not None and p.get("source_page") is not None:
        return [(p["source_doc"], p["source_page"], "値の出所ページ")]
    if "gap" not in item["flags"]:
        return []
    spec = BY_ID[item["company_id"]]
    group = _group_for(item["field"])
    docs = [spec.company_id] if spec.source_type.value == "pdf" else [k for k, _ in spec.supplement_docs]
    out = []
    for doc in docs:
        if not (RAW_DIR / f"{doc}.pdf").exists() or group is None:
            continue
        for i in P.candidate_pages(P.load_page_texts(doc), group)[:MAX_PAGES_PER_ITEM]:
            out.append((doc, i, "候補ページ（この項目を探すページ）"))
    return out[:MAX_PAGES_PER_ITEM]


def _render_jpeg_b64(doc: str, page_index: int, scale: float = 1.15) -> Optional[str]:
    import pypdfium2 as pdfium

    path = RAW_DIR / f"{doc}.pdf"
    if not path.exists():
        return None
    img = pdfium.PdfDocument(str(path))[page_index].render(scale=scale).to_pil().convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode()


def _fmt(item: dict) -> str:
    v = item["value"]
    if v is None:
        return "（値なし）"
    return f"{v:,.2f} %" if item["field"] in PERCENT_FIELDS else f"{v:,.2f} 百万円"


def _section(item: dict) -> str:
    p = item["prov"]
    chips = "".join(f'<span class="chip">{html.escape(FLAG_LABELS[f])}</span>' for f in item["flags"])
    rows = [f"<tr><th>値</th><td>{html.escape(_fmt(item))}</td></tr>"]
    if item["reason"]:
        rows.append(f"<tr><th>未取得の理由（機械判定）</th><td>{html.escape(item['reason'])}</td></tr>")
    for label, key in (("単体／連結", "basis"), ("期間", "period"), ("抽出方法", "method"), ("引用", "evidence"), ("メモ", "note")):
        if p.get(key):
            rows.append(f"<tr><th>{label}</th><td>{html.escape(str(p[key]))}</td></tr>")
    figs = []
    for doc, idx, cap in _pages_for(item):
        b64 = _render_jpeg_b64(doc, idx)
        if b64:
            figs.append(f'<figure><figcaption><b>{html.escape(doc)}.pdf　PDF {idx + 1} ページ目</b>（0始まり {idx}）— {html.escape(cap)}</figcaption>'
                        f'<img src="data:image/jpeg;base64,{b64}" loading="lazy"></figure>')
    cmd = f"python src/review.py confirm {item['company_id']} {item['field']} ok|no_disclosure|needs_fix \"メモ\""
    return (f'<section><h2>{html.escape(item["company_name"])}　／　<code>{html.escape(item["field"])}</code></h2>{chips}'
            f'<table class="info">{"".join(rows)}</table>{"".join(figs)}'
            f'<p class="verdict">目視の結果を台帳に記録：<code>{html.escape(cmd)}</code></p></section>')


def generate(data: list[dict]) -> tuple[Path, dict]:
    pending, confirmed = collect_items(data)
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for it in pending:
        for f in it["flags"]:
            counts[f] = counts.get(f, 0) + 1
    summary = "".join(
        f'<tr><td>{html.escape(i["company_name"])}</td><td><code>{html.escape(i["field"])}</code></td>'
        f'<td>{html.escape("、".join(FLAG_LABELS[f] for f in i["flags"]))}</td></tr>' for i in pending)
    done = "".join(
        f'<tr><td>{html.escape(i["company_name"])}</td><td><code>{html.escape(i["field"])}</code></td>'
        f'<td>{html.escape(DECISIONS.get(i["decision"]["decision"], i["decision"]["decision"]))}（{html.escape(i["decision"].get("date", ""))}）'
        f' {html.escape(i["decision"].get("note", ""))}</td></tr>' for i in confirmed)
    doc = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>要確認項目レポート</title>
<style>
body{{font-family:-apple-system,"Hiragino Sans",sans-serif;max-width:980px;margin:24px auto;padding:0 16px;color:#222;line-height:1.6}}
h1{{font-size:1.4rem}} h2{{font-size:1.05rem;border-bottom:2px solid #3b6;padding-bottom:4px;margin-top:36px}}
table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #ccc;padding:6px 8px;vertical-align:top;font-size:.9rem;text-align:left}}
th{{background:#f3f3f3;width:12em}} figure{{margin:14px 0;border:1px solid #ddd;padding:8px;background:#fafafa}}
img{{max-width:100%;height:auto;border:1px solid #bbb}} figcaption{{font-size:.85rem;margin-bottom:6px}}
.chip{{display:inline-block;background:#fff3cd;border:1px solid #e0b000;border-radius:10px;padding:1px 10px;margin:2px 6px 8px 0;font-size:.8rem}}
.verdict{{background:#fffbe6;border:1px dashed #cb0;padding:8px;font-size:.85rem}} code{{font-size:.8rem}}
</style></head><body>
<h1>要確認項目レポート（{date.today().isoformat()}生成）</h1>
<p>機械的に判定した「人が見たほうがよい項目」です（{len(pending)}件）。値の正しさを保証するものではなく、<b>目視の確認先</b>を示します。確認した結果は台帳に記録すると、値が変わるまで次回から出なくなります。このファイルはページ画像を含むため、公開リポジトリには置きません（<code>data/</code>はgit管理外）。</p>
<h2>一覧</h2><table><tr><th>会社</th><th>項目</th><th>理由</th></tr>{summary or '<tr><td colspan="3">要確認項目はありません</td></tr>'}</table>
{"".join(_section(i) for i in pending)}
<h2>確認済み（台帳、値が変わるまで非表示）</h2><table><tr><th>会社</th><th>項目</th><th>判定</th></tr>{done or '<tr><td colspan="3">なし</td></tr>'}</table>
</body></html>"""
    path = REVIEW_DIR / "review.html"
    path.write_text(doc, encoding="utf-8")
    return path, counts


def confirm(company_id: str, field: str, decision: str, note: str) -> None:
    if decision not in DECISIONS:
        raise SystemExit(f"decision must be one of {sorted(DECISIONS)}")
    data = json.loads(RECORDS.read_text(encoding="utf-8"))
    entry = next((e for e in data if e["record"]["company_id"] == company_id), None)
    if entry is None:
        raise SystemExit(f"unknown company_id {company_id!r}")
    value = _flat(entry["record"]).get(field)
    ledger = load_ledger()
    ledger[f"{company_id}:{field}"] = {"decision": decision, "value": value, "note": note, "date": date.today().isoformat()}
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"recorded {company_id}:{field} -> {decision}")


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "confirm":
        confirm(sys.argv[2], sys.argv[3], sys.argv[4], " ".join(sys.argv[5:]))
    data = json.loads(RECORDS.read_text(encoding="utf-8"))
    path, counts = generate(data)
    print(f"review report: {path.name} {counts}")
