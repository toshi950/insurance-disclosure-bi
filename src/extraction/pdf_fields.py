"""PDF-side extraction of the fields EDINET XBRL cannot supply (asset
classification, solvency ratio, life/non-life/SSI extension fields, and
reserves where the filing is a PDF), via a grounded LLM read.

Why LLM-first here (unlike the headline BS/PL figures in table_search.py):
these items sit in multi-year, multi-basis (単体/連結), multi-product tables
whose row/column structure differs by company, and deterministic row
matching returned ambiguous candidates in practice (e.g. one label with
three year-columns across two sections). So the model reads the candidate
pages, but nothing it says is trusted on its own:

  grounding check  — the quoted evidence must appear verbatim (whitespace
                     and comma insensitive) in the page text actually sent,
                     and must contain the reported number. A value that is
                     not quotable from the page is discarded, never kept.
  unit conversion  — done in code from the unit string, not by the model.
  basis policy     — individual (単体) figures only; a 連結 figure is
                     rejected, except for the solvency ratio, which is taken
                     on a consolidated basis if that is what is disclosed
                     (2026-10-03 decision).
  bounded cost     — one cheap-model call per (company, field group);
                     escalate to the strong model once if anything in the
                     group failed grounding. Raw model output is cached to
                     data/cache/pdf_llm/ so reruns are free and auditable.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pdfplumber

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from companies import BY_ID, COMPANIES, CompanySpec  # noqa: E402
from extraction.llm_verify import CHEAP_MODEL, STRONG_MODEL, _get_client  # noqa: E402
from schema import (  # noqa: E402
    CompanyRecord,
    ExtractionMethod,
    Industry,
    Provenance,
    SolvencyBasis,
    SourceType,
)

ROOT = Path(__file__).resolve().parent.parent.parent
RAW_DIR = ROOT / "data" / "raw"
CACHE_DIR = ROOT / "data" / "cache"
LLM_CACHE_DIR = CACHE_DIR / "pdf_llm"
MAX_PAGES_PER_GROUP = 3


# ---------------------------------------------------------------------------
# Page cache (text per page; avoids re-parsing 150-300 page PDFs)
# ---------------------------------------------------------------------------

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(text: str) -> str:
    """Some PDFs (e.g. 日本生命) emit stray control characters (BEL) around
    negative-number marks; they break verbatim quoting, so drop them."""
    return _CTRL.sub("", text)


def load_page_texts(company_id: str) -> list[str]:
    return [_clean(t) for t in _load_raw_page_texts(company_id)]


def _load_raw_page_texts(company_id: str) -> list[str]:
    cache = CACHE_DIR / f"{company_id}_pages.json"
    src = RAW_DIR / f"{company_id}.pdf"
    if cache.exists() and cache.stat().st_mtime >= src.stat().st_mtime:
        return [p["text"] for p in json.loads(cache.read_text(encoding="utf-8"))]
    pages = []
    with pdfplumber.open(src) as pdf:
        for pg in pdf.pages:
            try:
                text = pg.extract_text() or ""
            except Exception:
                text = ""
            try:
                tables = pg.extract_tables()
            except Exception:
                tables = []
            pages.append({"text": text, "tables": tables})
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    return [p["text"] for p in pages]


_CID = re.compile(r"\(cid:\d+\)")


def is_garbled(text: str) -> bool:
    """Glyph-id text (e.g. 損保ジャパン p115+): the text layer carries no usable characters."""
    return len(_CID.findall(text)) > 30


def _squash(text: str) -> str:
    return re.sub(r"[\s　]+", "", text)


# ---------------------------------------------------------------------------
# Field groups
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldDef:
    name: str  # schema field name, or a helper name that derive_*() folds into one
    kind: str  # "amount" (-> 百万円) | "percent"
    definition: str
    evidence_any: tuple[str, ...] = ()  # the quoted evidence must contain one of these labels
    evidence_regex: str = ""  # ...and match this pattern, if given (e.g. 基金 but not 基金償却積立金)


@dataclass(frozen=True)
class Group:
    name: str
    industries: tuple[Industry, ...]
    must_all: tuple[str, ...]  # a candidate page must contain all of these
    rank_by: tuple[str, ...]  # more of these on the page = better candidate
    fields: tuple[FieldDef, ...]
    prompt_note: str = ""
    max_pages: int = MAX_PAGES_PER_GROUP
    must_any: tuple[str, ...] = ()  # in addition to must_all, a page must contain at least one of these


ALL = (Industry.LIFE, Industry.NON_LIFE, Industry.SSI)
LIFE = (Industry.LIFE,)
NONLIFE_LIKE = (Industry.NON_LIFE, Industry.SSI)  # 少短は損保型の開示項目

GROUPS: tuple[Group, ...] = (
    Group(
        "reserves",
        ALL,
        must_all=("支払備金", "責任準備金"),
        rank_by=("貸借対照表", "資産の部", "負債の部", "純資産の部", "保険契約準備金"),
        fields=(
            FieldDef("total_assets", "amount", "貸借対照表の「資産の部合計」（総資産。「負債及び純資産の部合計」と同額）"),
            FieldDef("net_assets", "amount", "貸借対照表の「純資産の部合計」（純資産。「負債の部合計」や「株主資本合計」「基金等合計」ではない）"),
            FieldDef("policy_reserve_total", "amount", "貸借対照表の負債の部に計上された「責任準備金」の総額（危険準備金・異常危険準備金等を含む全額。内訳行ではなく合計の1行）"),
            FieldDef("claims_reserve_total", "amount", "貸借対照表の負債の部に計上された「支払備金」の総額（内訳ではなく合計の1行）"),
        ),
        prompt_note="貸借対照表（単体）の最新期末の値。保険契約準備金の内訳の小計や、増減・繰入額の行は対象外。",
    ),
    Group(
        "pl",
        ALL,
        must_all=("経常収益", "経常利益"),
        rank_by=("当期純利益", "当期純剰余", "経常費用", "特別利益", "特別損失", "法人税", "損益計算書"),
        fields=(
            FieldDef("ordinary_income", "amount", "損益計算書の「経常収益」（合計の1行）"),
            FieldDef("ordinary_profit", "amount", "損益計算書の「経常利益」（経常損失の場合は負）"),
            FieldDef("net_income", "amount", "損益計算書の「当期純利益」（相互会社は「当期純剰余」。当期純損失の場合は負。「税引前当期純利益」「契約者配当準備金繰入額」前後の中間段階の利益ではない）"),
        ),
        prompt_note="損益計算書（単体）の最新年度。",
    ),
    Group(
        "loans",
        ALL,
        must_all=("破産更生債権", "危険債権", "正常債権"),
        rank_by=("三月以上延滞", "貸付条件緩和", "要管理債権", "リスク管理債権", "資産の査定"),
        fields=(
            FieldDef("assets_bankrupt_claims", "amount", "破産更生債権及びこれらに準ずる債権の額"),
            FieldDef("assets_doubtful_claims", "amount", "危険債権の額"),
            FieldDef("assets_overdue_3m", "amount", "三月以上延滞債権の額"),
            FieldDef("assets_restructured", "amount", "貸付条件緩和債権の額"),
            FieldDef("assets_normal_claims", "amount", "正常債権の額"),
        ),
        prompt_note="保険業法に基づく債権（資産の自己査定）の区分表。連結と単体が別表で並ぶ場合は単体を使う。「債権の合計」「保険業法上の債権」の最新期末の値。",
    ),
    Group(
        "solvency",
        ALL,
        must_all=("ソルベンシー・マージン比率",),
        rank_by=("総額", "所要", "経済価値", "(Ａ)", "(Ｂ)", "(Ｃ)", "基準"),
        fields=(
            FieldDef("solvency_ratio", "percent", "ソルベンシー・マージン比率（％）。2026年3月期からの経済価値ベース（ESR）の比率が開示されていればそれ（note に ESR と明記）、従来基準の比率のみなら SMR と note に明記。連結と単体が並ぶ場合は連結を優先してbasisに連結と書く（単体のみなら単体）"),
        ),
        prompt_note="最新期末の比率。経済価値ベースと従来基準が併記されている場合は経済価値ベース（ESR）を優先し、note に「ESR」または「SMR」と書く。",
    ),
    Group(
        "solvency_consol",
        ALL,
        must_all=("連結ソルベンシー・マージン比率",),
        rank_by=("総額", "所要", "経済価値", "(Ａ)", "(Ｂ)", "(Ｃ)", "基準", "適格資本", "連結ベース"),
        fields=(
            FieldDef("solvency_ratio_consol", "percent", "**連結**ソルベンシー・マージン比率（％）。連結ベースの比率だけを返す（単体の比率は返さず、連結の比率が無ければ null）。経済価値ベース（ESR）が開示されていればそれ（note に ESR と明記）、従来基準のみなら SMR と明記"),
        ),
        prompt_note="連結ベースの表（「連結ソルベンシー・マージン比率」「連結ベース」）の最新期末の値。basis には必ず「連結」と書く。保険子会社・少額短期保険業者子会社の単体比率は対象外。",
    ),
    Group(
        "life_profit",
        LIFE,
        must_all=("基礎利益",),
        rank_by=("キャピタル損益", "臨時損益", "経常利益", "危険差益", "利差益", "費差益"),
        fields=(
            FieldDef("base_profit", "amount", "基礎利益（単体、最新年度、百万円相当）"),
        ),
    ),
    Group(
        "life_contracts",
        LIFE,
        must_all=("保有契約", "新契約"),
        rank_by=("個人保険", "個人年金保険", "団体保険", "年度末", "増加率", "合計"),
        fields=(
            FieldDef("pif_individual", "amount", "保有契約高の**金額**（件数ではない）：個人保険**全体**の最新年度末（死亡保険・終身保険等の種類別の内訳行ではなく、個人保険の合計行）", evidence_any=("個人保険", "合計", "合 計")),
            FieldDef("pif_annuity", "amount", "保有契約高の**金額**：個人年金保険**全体**の最新年度末（種類別の内訳行ではなく合計行）", evidence_any=("個人年金", "合計", "合 計")),
            FieldDef("new_individual", "amount", "新契約高の**金額**：個人保険**全体**の「新契約」（「新契約＋転換による純増加」や「転換による純増加」の行ではなく、「新契約」の行。種類別の内訳行ではなく合計）の最新年度", evidence_any=("新契約", "個人保険", "合計", "合 計")),
            FieldDef("new_annuity", "amount", "新契約高の**金額**：個人年金保険**全体**の「新契約」（転換を含む行ではなく「新契約」の行。種類別の内訳行ではなく合計）の最新年度", evidence_any=("新契約", "個人年金", "合計", "合 計")),
        ),
        prompt_note="団体保険・団体年金保険は対象外。件数の表ではなく金額（百万円等）の表から取る。個人保険・個人年金保険それぞれの行を別々に返す（合計はこちらで計算する）。",
        max_pages=4,
    ),
    Group(
        "life_annualized",
        LIFE,
        must_all=("年換算保険料",),
        rank_by=("保有契約", "新契約", "個人保険", "個人年金保険", "合計"),
        fields=(
            FieldDef("annualized_premium", "amount", "保有契約の年換算保険料（年度末）：個人保険＋個人年金保険の**合計**行（「合計」と明記された行がある場合のみ。無ければ null）。新契約の年換算保険料ではなく保有契約のもの"),
        ),
    ),
    Group(
        "life_lapse",
        LIFE,
        must_all=("解約",),
        rank_by=("失効率", "解約失効率", "解約・失効", "失効・解約", "個人保険", "転換による"),
        max_pages=6,
        fields=(
            FieldDef("lapse_rate", "percent", "解約失効率（％、個人保険、最新年度）。「解約・失効率」「失効解約率」など名称は異なりうる。転換による減少を含む／除くの区別があれば除く方（解約・失効のみ）。note に区別を書く"),
        ),
    ),
    Group(
        "life_fund",
        LIFE,
        must_all=("基金",),
        must_any=("貸借対照表", "基金等変動計算書"),
        rank_by=("基金償却積立金", "再評価積立金", "純資産の部", "負債の部", "当期末残高", "基金等変動計算書"),
        fields=(
            FieldDef("mutual_company_fund", "amount", "相互会社の「基金」の期末残高。次のどちらかから読む：(a) 貸借対照表の純資産の部の「基金」の行、(b) 基金等変動計算書の**「基金」列**の最新年度「当期末残高」（「基金」列は表の**最初の数値列**。基金が償却済みでその列が空欄の年度は、行の最初の数値が右隣の「基金償却積立金」列の値になっているので、取り違えないこと）。「基金償却積立金」「基金等合計」「基金拠出金」は別科目で対象外。基金の列/行が「－」「―」「–」なら残高なし（0）。基金という科目が表に存在しなければ null",
                     evidence_regex=r"基金(?!償却|等|拠出|の|を)|当期末残高"),
        ),
        prompt_note="株式会社形態の保険会社には基金は無い（null）。変動計算書は年度ごとに別表なので、最新年度の表の当期末残高を使う。",
        max_pages=6,
    ),
    Group(
        "nonlife_core",
        NONLIFE_LIKE,
        must_all=("正味収入保険料",),
        rank_by=("正味支払保険金", "損害率", "事業費率", "コンバインド", "合計"),
        fields=(
            FieldDef("net_premiums_written", "amount", "正味収入保険料（全種目合計、最新年度）"),
            FieldDef("loss_ratio", "percent", "損害率（％、全種目合計。「正味損害率」またはE.I.損害率等の区別があれば note に書く）"),
            FieldDef("expense_ratio", "percent", "事業費率（％、全種目合計。「正味事業費率」等）"),
        ),
        prompt_note="全種目合計の1行を使う（火災・自動車等の種目別の行ではない）。",
        max_pages=5,
    ),
    Group(
        "nonlife_claims",
        NONLIFE_LIKE,
        must_all=("正味支払保険金",),
        rank_by=("元受正味支払保険金", "回収再保険金", "保険引受利益", "合計", "損害率"),
        fields=(
            FieldDef("net_claims_paid", "amount", "正味支払保険金（全種目合計の1行、最新年度）。「元受正味支払保険金」「回収再保険金」ではなく「正味支払保険金」の合計"),
        ),
        prompt_note="全種目合計の1行を使う（種目別の行ではない）。複数年度の列が並ぶ場合は最新年度の列。",
        max_pages=5,
    ),
)

_DASH_IS_ZERO = {"mutual_company_fund", "assets_bankrupt_claims", "assets_doubtful_claims", "assets_overdue_3m", "assets_restructured", "assets_normal_claims"}
_UNIT_TO_MILLION = {"百万円": 1.0, "千円": 0.001, "円": 1e-6, "億円": 100.0, "兆円": 1e6, "万円": 0.01}


def groups_for(spec: CompanySpec) -> list[Group]:
    """Groups worth running for `spec`: skip those whose every target field is regulation-N/A."""
    na = {f for f, _ in spec.not_applicable}
    return [g for g in GROUPS if spec.industry in g.industries
            and not (na and set(_targets(g)) <= na)]


# ---------------------------------------------------------------------------
# Candidate pages
# ---------------------------------------------------------------------------

# A figure-bearing number: thousands-grouped, decimal or percent. Plain 1-3 digit
# integers are excluded on purpose: they are page numbers in tables of contents.
_FIGURE_RE = re.compile(r"\d{1,3}(?:,\d{3})+|\d+\.\d+|[％%]")


def _figure_lines(text: str, keywords: tuple[str, ...]) -> int:
    """Lines that mention one of `keywords` and carry a real figure. Ranking by
    this keeps contents pages / cross-reference indexes (keywords + page
    numbers only) from outranking the table pages."""
    n = 0
    for line in text.split("\n"):
        sq = _squash(line)
        if any(k in sq for k in keywords) and _FIGURE_RE.search(line):
            n += 1
    return n


_SUBSIDIARY_HEADING = re.compile(r"^(?:【\d+】)?(?:\d+\.)?(?:子会社等である保険会社|保険子会社等の単体)")


def own_region_end(texts: list[str]) -> int:
    """Index of the first page (after the front matter) whose heading opens the
    section on subsidiary insurers. Pages from there on describe other
    companies (e.g. 日本生命 p176+ covers its subsidiaries with the same
    layout) and must never be offered as the company's own figures."""
    for i, t in enumerate(texts):
        if i < 10:
            continue
        for line in t.split("\n"):
            if _SUBSIDIARY_HEADING.match(_squash(line)):
                return i
    return len(texts)


_HEADING = re.compile(r"^(?:【\d+】|[（(]\d+[）)])")


def _heading_hits(text: str, keywords: tuple[str, ...]) -> int:
    """Heading-like lines naming a keyword, counted only on pages with few
    headings: a dedicated table page has one or two, a contents page dozens
    (and would otherwise win on keyword count alone)."""
    heads = [_squash(l) for l in text.split("\n") if _HEADING.match(_squash(l))]
    if len(heads) > 4:
        return 0
    return sum(1 for h in heads if any(k in h for k in keywords))


def candidate_pages(texts: list[str], group: Group, limit: Optional[int] = None) -> list[int]:
    limit = limit or group.max_pages
    end = own_region_end(texts)
    scored = []
    for i, t in enumerate(texts[:end]):
        if is_garbled(t):
            continue
        sq = _squash(t)
        if not all(k in sq for k in group.must_all):
            continue
        if group.must_any and not any(k in sq for k in group.must_any):
            continue
        figure_lines = _figure_lines(t, group.must_all + group.rank_by)
        density = len(_FIGURE_RE.findall(t)) // 10  # data-dense pages beat index pages
        kw = sum(sq.count(_squash(k)) for k in group.rank_by)
        heading = _heading_hits(t, group.must_all)
        scored.append(((figure_lines, heading, density, kw), -i, i))
    scored.sort(reverse=True)
    return sorted(i for _, _, i in scored[:limit])


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

PROMPT = """あなたは日本の保険会社の開示資料から数値を抽出する担当者です。以下は開示資料（{company}）の該当ページの抜粋（プレーンテキスト）です。

{pages}

次の項目を抽出してください。
{field_lines}

共通ルール：
- 対象は**最新年度（最新期末）**の数値。複数年度が並ぶ場合は通常、最新年度が一番右または一番左の列。見出しで年度を確認すること。
- 連結と単体が別表で並ぶ場合は**単体（個別）**を選び、basis に「単体」と書く。連結しか無ければ basis に「連結」、判別できなければ「不明」。
- value_text には原文に印刷されている数字をそのまま（カンマ・△・小数点も含め）書く。換算や計算はしない。
- unit には、その数値の単位として原文に書かれているもの（百万円／千円／円／億円／兆円／万円）を書く。％の項目は「％」。ページ上部の「（単位：…）」等を確認すること。
- evidence には、その数値を含む行の**原文をそのまま**（空白の位置は問わない）引用する。ページに書かれていない文字は一切入れない。
- 該当する値がページに無い、または自信が無い項目は null にする。推測で埋めない。
{note}

以下のJSONのみで回答してください（説明文は不要）：
{{"fields": {{"<項目名>": {{"value_text": "...", "unit": "...", "basis": "単体|連結|不明", "period": "...", "evidence": "...", "note": "..."}} または null}}}}
"""


def _build_prompt(spec: CompanySpec, group: Group, texts: list[str], pages: list[int]) -> str:
    page_blocks = "\n\n".join(f"[page {i}]\n{texts[i]}" for i in pages)
    field_lines = "\n".join(f"- {f.name}: {f.definition}" for f in group.fields)
    note = f"- {group.prompt_note}" if group.prompt_note else ""
    return PROMPT.format(company=spec.company_name, pages=page_blocks, field_lines=field_lines, note=note)


def _call(model: str, prompt: str) -> dict:
    resp = _get_client().messages.create(
        model=model, max_tokens=2000, messages=[{"role": "user", "content": prompt}]
    )
    # The strong model may return a ThinkingBlock first; only text blocks carry the answer.
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"_raw": raw, "fields": {}}
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return {"_raw": raw, "fields": {}}
    data["_raw"] = raw
    return data


# ---------------------------------------------------------------------------
# Grounding + conversion
# ---------------------------------------------------------------------------

@dataclass
class FieldResult:
    name: str
    value: Optional[float] = None
    ok: bool = False
    reason: str = ""
    page: Optional[int] = None
    basis: Optional[str] = None
    period: Optional[str] = None
    evidence: Optional[str] = None
    note: Optional[str] = None
    model: Optional[str] = None
    method: ExtractionMethod = ExtractionMethod.LLM_VERIFIED
    doc: Optional[str] = None  # which document (data/raw/{doc}.pdf) `page` refers to
    flags: list[str] = field(default_factory=list)  # review flags, see schema.Provenance.flags


def _digits(s: str) -> str:
    return re.sub(r"[,，\s　△▲－\-]", "", s)


_KANJI_AMOUNT = re.compile(r"^(?:(?P<cho>[\d,]+)兆)?(?:(?P<oku>[\d,]+)億)?(?:(?P<man>[\d,]+)万)?(?P<yen>[\d,]+)?円?$")


def _parse_kanji_million(value_text: str) -> Optional[float]:
    """'2兆3,456億円'（例・架空の値） -> 百万円。単位を含む表記は unit 欄に頼らずここで換算する。"""
    t = re.sub(r"[\s　]", "", value_text)
    if not re.search(r"[兆億万]", t):
        return None
    m = _KANJI_AMOUNT.match(t)
    if not m:
        return None
    g = {k: float(v.replace(",", "")) if v else 0.0 for k, v in m.groupdict().items()}
    return g["cho"] * 1e6 + g["oku"] * 100 + g["man"] * 0.01 + g["yen"] * 1e-6


def _parse_printed(value_text: str) -> Optional[float]:
    t = value_text.strip().replace("，", ",")
    neg = t.startswith(("△", "▲", "-", "−"))
    t = re.sub(r"[△▲\-−,\s　％%]", "", t)
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", t):
        return None
    v = float(t)
    return -v if neg else v


def _line_subsequence_page(evidence: str, texts: list[str], pages: list[int]) -> Optional[int]:
    tokens = [t for t in re.split(r"[\s　]+", evidence.strip()) if t]
    if len(tokens) < 2:
        return None
    for i in pages:
        for line in texts[i].split("\n"):
            pos = 0
            for tok in tokens:
                j = line.find(tok, pos)
                if j < 0:
                    break
                pos = j + len(tok)
            else:
                return i
    return None


def _loose_page(evidence: str, value_text: str, texts: list[str], pages: list[int]) -> Optional[int]:
    """Second-tier grounding for evidence the layout splits (two-column prose,
    wrapped rows): the number must occur on the page as a whole token and at
    least 85% of the evidence's character trigrams must occur on that page
    (a fabricated sentence around a real number scores ~0.7 or less)."""
    core = re.sub(r"[\s　]", "", value_text.replace("，", ",")).rstrip("％%")
    e = _squash(evidence)
    grams = {e[i:i + 3] for i in range(len(e) - 2)}
    if not grams:
        return None
    for i in pages:
        sq = _squash(texts[i])
        if not re.search(r"(?<![\d,.])" + re.escape(core) + r"(?![\d,]|\.\d)", sq):
            continue
        if sum(1 for g in grams if g in sq) / len(grams) >= 0.85:
            return i
    return None


def ground_and_convert(fdef: FieldDef, item, texts: list[str], pages: list[int]) -> FieldResult:
    if isinstance(item, list):
        # The model sometimes lists several candidates. Ambiguity is not resolved by
        # picking one silently: accept only if exactly one candidate survives grounding.
        outs = [ground_and_convert(fdef, it, texts, pages) for it in item if isinstance(it, dict)]
        ok = [o for o in outs if o.ok]
        if len(ok) == 1:
            return ok[0]
        res = FieldResult(fdef.name)
        res.reason = "ambiguous: multiple candidates" if len(ok) > 1 else (outs[0].reason if outs else "not found by model")
        return res
    res = FieldResult(fdef.name)
    if not item or not isinstance(item, dict):
        res.reason = "not found by model"
        return res
    value_text = str(item.get("value_text") or "")
    evidence = str(item.get("evidence") or "")
    unit = str(item.get("unit") or "").strip()
    res.basis, res.period, res.evidence, res.note = (
        item.get("basis"), item.get("period"), evidence, item.get("note"),
    )

    suffixed = re.fullmatch(r"(.+?)(百万円|千円|万円|円)", re.sub(r"[\s　]", "", value_text))
    if suffixed and fdef.kind == "amount" and not re.search(r"[兆億]", value_text):
        value_text, unit = suffixed.group(1), suffixed.group(2)  # e.g. "1,234,567千円" (illustrative, made-up value)
    kanji = _parse_kanji_million(value_text) if fdef.kind == "amount" else None
    printed = _parse_printed(value_text) if kanji is None else None
    if printed is None and kanji is None and fdef.name in _DASH_IS_ZERO and value_text.strip() in ("－", "-", "―", "ー", "−", "–", "—", "─"):
        # 債権区分表の「－」は該当なし（開示された0）。他の項目の「－」は不適用なので0にしない。
        printed = 0.0
    if printed is None and kanji is None:
        res.reason = f"unparseable value_text {value_text!r}"
        return res

    # grounding: the evidence must be quotable from a page that was sent — either
    # verbatim (whitespace-insensitive) or as an ordered subsequence of tokens on a
    # single line (the model often drops the middle of a multi-year row, e.g.
    # "個人保険 1,200,000 △4.3" out of "個人保険 1,300,000 △5.0 1,200,000 △4.3"; the figures
    # here are illustrative, made-up values).
    # Either way the reported number must sit in the quoted text.
    ev_sq = _squash(evidence)
    hit_page = next((i for i in pages if ev_sq and ev_sq in _squash(texts[i])), None)
    if hit_page is None:
        hit_page = _line_subsequence_page(evidence, texts, pages)
    if hit_page is None:
        hit_page = _loose_page(evidence, value_text, texts, pages)
        if hit_page is not None:
            res.note = ((res.note or "") + " [引用は複数行/段組にまたがるため緩い照合（数値＋語句がページに存在）]").strip()
            res.flags.append("loose_grounding")
    if hit_page is None:
        res.reason = "evidence not found verbatim in sent pages"
        return res
    if fdef.evidence_regex and not re.search(fdef.evidence_regex, evidence):
        res.reason = f"evidence does not match {fdef.evidence_regex!r}"
        return res
    if fdef.evidence_any and not any(k in evidence for k in fdef.evidence_any):
        res.reason = f"evidence lacks expected label {fdef.evidence_any}"
        return res
    if kanji is None and printed != 0.0:
        core = re.sub(r"[\s　]", "", value_text.replace("，", ",")).rstrip("％%")  # e.g. "12.3％" vs "12.3%" in the quote (illustrative value)
        # the whole number must be a bounded token: "1" inside "1兆2,345億円" (illustrative value) is not the amount
        bounded = re.search(r"(?<![\d,.])" + re.escape(core) + r"(?![\d,]|\.\d|[兆億万])", evidence)
        if not bounded and re.search(r"\d [\d,]", evidence):
            # digits printed with letter-spacing ("2 6 8 ,7 7 9"): token boundaries are lost, so
            # accept only if the number is the rightmost (= latest-year) figure of the row
            bounded = _squash(evidence).endswith(core)
        if not bounded:
            res.reason = "reported number not contained in evidence as a whole token"
            return res
    res.page = hit_page

    if kanji is not None:
        res.value = kanji
    elif fdef.kind == "percent":
        res.value = printed
    else:
        factor = _UNIT_TO_MILLION.get(unit)
        if factor is None:
            res.reason = f"unknown unit {unit!r}"
            return res
        res.value = printed * factor
    if value_text.strip().startswith(("△", "▲")) and res.value is not None and res.value > 0:
        res.value = -res.value
    res.ok = True
    return res


def extract_group(spec: CompanySpec, group: Group, texts: Optional[list[str]] = None, doc_key: Optional[str] = None,
                  use_cache: bool = True) -> dict[str, FieldResult]:
    doc = doc_key or spec.company_id
    texts = texts if texts is not None else load_page_texts(doc)
    pages = candidate_pages(texts, group)
    if not pages:
        return {f.name: FieldResult(f.name, reason="no candidate page (must_all keywords absent)") for f in group.fields}

    cache_file = LLM_CACHE_DIR / f"{doc}__{group.name}.json"
    prompt = _build_prompt(spec, group, texts, pages)
    cached = json.loads(cache_file.read_text(encoding="utf-8")) if use_cache and cache_file.exists() else None
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    if cached and cached.get("prompt_hash") == prompt_hash:  # prompt covers pages, field defs and wording
        runs = cached["runs"]
    else:
        runs = []
        data = _call(CHEAP_MODEL, prompt)
        runs.append({"model": CHEAP_MODEL, "data": data})
        results = {f.name: ground_and_convert(f, (data.get("fields") or {}).get(f.name), texts, pages) for f in group.fields}
        if not all(r.ok or r.reason == "not found by model" for r in results.values()):
            # one bounded escalation, only when something failed grounding
            data2 = _call(STRONG_MODEL, prompt)
            runs.append({"model": STRONG_MODEL, "data": data2})
        LLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"pages": pages, "prompt_hash": prompt_hash, "runs": runs}, ensure_ascii=False, indent=1), encoding="utf-8")

    final: dict[str, FieldResult] = {}
    for f in group.fields:
        best: Optional[FieldResult] = None
        for run in runs:  # cheap first; a later (strong) run only fills what the cheap run could not ground
            r = ground_and_convert(f, (run["data"].get("fields") or {}).get(f.name), texts, pages)
            r.model = run["model"]
            r.doc = doc
            if r.ok:
                best = r
                break
            best = best or r
        best.doc = doc
        final[f.name] = best
    return final


# ---------------------------------------------------------------------------
# Image fallback (garbled text layer)
# ---------------------------------------------------------------------------
# ページの場所は companies.py の image_page_hints（手動で特定）を使う。モデルに見出しを読ませての
# 自動探索は誤読で別会社・子会社のページを拾った（異常危険準備金で実際に発生）ため廃止した。
# 損保ジャパンの「業績データ」章（p115〜）は文字が glyph-id（"(cid:1234)"）で出力され、
# テキスト層から何も読めない。ここだけはページ画像をモデルに読ませる。画像には
# 「原文との文字列照合」ができないため、代わりに **二重読取**（安価モデルと上位モデルが
# 独立に読み、換算後の値が完全一致した項目のみ採用）を検証手段とする。不一致は None。

IMAGE_PROMPT = """あなたは日本の保険会社の開示資料から数値を抽出する担当者です。添付は開示資料（{company}）の該当ページの画像です（{n}枚）。

次の項目を抽出してください。
{field_lines}

共通ルール：
- 対象は**最新年度（最新期末）**の数値。複数年度の列が並ぶ場合は見出しの年度で確認する（網掛けされた右端の列が最新であることが多い）。
- 連結と単体が別表の場合は**単体**を選び、basis に「単体」と書く。連結しか無ければ「連結」、判別できなければ「不明」。
- value_text には画像に印刷されている数字をそのまま（カンマ・△・小数点を含め）書く。換算や計算はしない。「－」は「－」と書く。
- unit には、その数値の単位として表に書かれているもの（百万円／千円／円／億円／兆円／万円）を書く。％の項目は「％」。表の右上の「（単位：…）」を必ず確認する。
- evidence には、その数値がある行の見出し（科目名）と数値を書き写す。
- 該当する値が無い、または読み取りに自信が無い項目は null にする。推測で埋めない。
{note}

以下のJSONのみで回答してください（説明文は不要）：
{{"fields": {{"<項目名>": {{"value_text": "...", "unit": "...", "basis": "単体|連結|不明", "period": "...", "evidence": "...", "note": "..."}} または null}}}}
"""


def _render_png_b64(company_id: str, page_index: int, scale: float) -> str:
    import base64
    import io

    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(RAW_DIR / f"{company_id}.pdf"))
    buf = io.BytesIO()
    pdf[page_index].render(scale=scale).to_pil().save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _image_call(model: str, prompt: str, images_b64: list[str], max_tokens: int = 2000) -> dict:
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b}}
        for b in images_b64
    ] + [{"type": "text", "text": prompt}]
    resp = _get_client().messages.create(
        model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": content}]
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"_raw": raw, "fields": {}}
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return {"_raw": raw, "fields": {}}
    data["_raw"] = raw
    return data


def _convert_only(fdef: FieldDef, item) -> Optional[tuple[float, dict]]:
    """Value (百万円 / %) from a model item without any text grounding (image path)."""
    if isinstance(item, list):
        items = [it for it in item if isinstance(it, dict)]
        conv = [c for c in (_convert_only(fdef, it) for it in items) if c]
        return conv[0] if len(conv) == 1 else None
    if not isinstance(item, dict):
        return None
    vt = str(item.get("value_text") or "")
    unit = str(item.get("unit") or "").strip()
    suffixed = re.fullmatch(r"(.+?)(百万円|千円|万円|円)", re.sub(r"[\s　]", "", vt))
    if suffixed and fdef.kind == "amount" and not re.search(r"[兆億]", vt):
        vt, unit = suffixed.group(1), suffixed.group(2)
    kanji = _parse_kanji_million(vt) if fdef.kind == "amount" else None
    printed = _parse_printed(vt) if kanji is None else None
    if printed is None and kanji is None and fdef.name in _DASH_IS_ZERO and vt.strip() in ("－", "-", "―", "ー", "−", "–", "—", "─"):
        printed = 0.0
    if kanji is not None:
        value = kanji
    elif printed is None:
        return None
    elif fdef.kind == "percent":
        value = printed
    else:
        factor = _UNIT_TO_MILLION.get(unit)
        if factor is None:
            return None
        value = printed * factor
    if vt.strip().startswith(("△", "▲")) and value > 0:
        value = -value
    return value, item


def extract_group_images(spec: CompanySpec, group: Group, fields: list[FieldDef], texts: list[str]) -> dict[str, FieldResult]:
    out = {f.name: FieldResult(f.name, reason="image fallback: no matching page") for f in fields}
    pages = list(dict(spec.image_page_hints).get(group.name, ()))
    if not pages:
        return {f.name: FieldResult(f.name, reason="image fallback: no page hint in companies.py") for f in fields}
    cache_file = LLM_CACHE_DIR / f"{spec.company_id}__{group.name}__img.json"
    field_lines = "\n".join(f"- {f.name}: {f.definition}" for f in fields)
    note = f"- {group.prompt_note}" if group.prompt_note else ""
    prompt = IMAGE_PROMPT.format(company=spec.company_name, n=len(pages), field_lines=field_lines, note=note)
    key = hashlib.sha256((prompt + str(pages)).encode()).hexdigest()[:16]
    cached = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else None
    if cached and cached.get("key") == key:
        reads = cached["reads"]
    else:
        imgs = [_render_png_b64(spec.company_id, i, 1.6) for i in pages]
        reads = [_image_call(m, prompt, imgs) for m in (CHEAP_MODEL, STRONG_MODEL)]
        # Third read, only if the two disagree on some field: strong model at higher resolution.
        # A value is accepted when at least two of the (up to three) reads agree.
        disagree = [f for f in fields if (
            (a := _convert_only(f, (reads[0].get("fields") or {}).get(f.name))) is None
            or (b := _convert_only(f, (reads[1].get("fields") or {}).get(f.name))) is None
            or abs(a[0] - b[0]) > 1e-9)]
        if disagree:
            lines3 = "\n".join(f"- {f.name}: {f.definition}" for f in disagree)
            prompt3 = IMAGE_PROMPT.format(company=spec.company_name, n=len(pages), field_lines=lines3, note=note)
            imgs3 = [_render_png_b64(spec.company_id, i, 2.2) for i in pages]
            reads.append(_image_call(STRONG_MODEL, prompt3, imgs3))
        LLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"key": key, "pages": pages, "reads": reads}, ensure_ascii=False, indent=1), encoding="utf-8")
    for f in fields:
        conv = [_convert_only(f, (rd.get("fields") or {}).get(f.name)) for rd in reads]
        conv = [c for c in conv if c is not None]
        r = FieldResult(f.name, page=pages[0])
        agreed = None
        for i, c in enumerate(conv):
            if sum(1 for d in conv if abs(d[0] - c[0]) <= 1e-9) >= 2:
                agreed = c
                break
        if agreed is None:
            r.reason = f"image fallback: no two reads agree ({[c[0] for c in conv]})"
        else:
            it = agreed[1]
            r.value, r.ok = agreed[0], True
            r.basis, r.period, r.evidence = it.get("basis"), it.get("period"), it.get("evidence")
            n_agree = sum(1 for d in conv if abs(d[0] - agreed[0]) <= 1e-9)
            r.note = f"{it.get('note') or ''} [画像読取・{len(reads)}回中{n_agree}回一致、対象ページ {pages}]".strip()
            r.model = f"{CHEAP_MODEL}+{STRONG_MODEL}"
            r.method = ExtractionMethod.LLM_IMAGE_FALLBACK
            r.doc = spec.company_id
            r.flags.append("image_read")
            if n_agree < len(reads):
                r.flags.append("image_majority")
        out[f.name] = r
    return out


# ---------------------------------------------------------------------------
# Record assembly
# ---------------------------------------------------------------------------

def _accept(fdef_name: str, r: FieldResult) -> tuple[bool, str]:
    if not r.ok:
        return False, r.reason
    if fdef_name == "solvency_ratio":
        return True, ""  # consolidated allowed (2026-10-03 decision)
    if r.basis == "連結":
        return False, "consolidated figure rejected (individual-basis policy)"
    return True, ""


def _modal_year(results: dict[str, FieldResult]) -> Optional[int]:
    """The fiscal year most results of one document refer to (used when a value's own period has no year)."""
    years = [y for y in (_fiscal_year(r.period) for r in results.values() if r.ok) if y is not None]
    return max(set(years), key=years.count) if years else None


def _solvency_basis(r: FieldResult, spec: CompanySpec, ref_year: Optional[int] = None) -> Optional[SolvencyBasis]:
    """Which regulatory scale the ratio is on, decided by regulation, not by the model's note:
    small-amount short-term insurers stay on the old SMR (200%) basis; life and non-life insurers moved
    to the economic-value-based ESR (100%) basis for fiscal years ending March 2026 and later, even
    though their booklets still call the figure "ソルベンシー・マージン比率"."""
    if spec.industry is Industry.SSI:
        return SolvencyBasis.SMR_200
    year = _fiscal_year(r.period) or ref_year
    if year is not None and year >= 2025:
        return SolvencyBasis.ESR_100
    note = (r.note or "")
    if "ESR" in note.upper() or "経済価値" in note:
        return SolvencyBasis.ESR_100
    if "SMR" in note.upper():
        return SolvencyBasis.SMR_200
    return None


def prefer_consolidated_solvency(results: dict[str, FieldResult]) -> None:
    """ソルベンシー比率は連結ベースで取る方針（2026-10-03決定）。連結の比率が根拠付きで取れていれば
    それを solvency_ratio とし、無ければ（単体のみ開示等）group "solvency" の結果をそのまま使う。"""
    c = results.pop("solvency_ratio_consol", None)
    if c is not None and c.ok and c.basis == "連結":
        c.name = "solvency_ratio"
        c.flags.append("consolidated_substitute")
        results["solvency_ratio"] = c


def derive_substandard(results: dict[str, FieldResult]) -> None:
    """要管理債権 = 三月以上延滞債権 + 貸付条件緩和債権（保険業法上の定義）。両方が
    根拠付きで取れた場合のみ、コード側で合算する（モデルに計算させない）。"""
    a, b = results.get("assets_overdue_3m"), results.get("assets_restructured")
    if a and b and a.ok and b.ok:
        r = FieldResult("assets_substandard_claims", value=a.value + b.value, ok=True,
                        page=a.page, basis=a.basis if a.basis == b.basis else "不明",
                        period=a.period, model=a.model, doc=a.doc,
                        flags=sorted(set(a.flags) | set(b.flags)), method=a.method,
                        evidence=f"{a.evidence} / {b.evidence}",
                        note="三月以上延滞債権＋貸付条件緩和債権の合算（コード側で計算）")
    else:
        r = FieldResult("assets_substandard_claims", reason="overdue_3m/restructured not both grounded")
    results["assets_substandard_claims"] = r
    for k in ("assets_overdue_3m", "assets_restructured"):
        results.pop(k, None)


def derive_life_totals(results: dict[str, FieldResult]) -> None:
    """保有契約高・新契約高 = 個人保険 + 個人年金保険（団体除く）。コード側で合算。"""
    for target, a_key, b_key in (
        ("policies_in_force", "pif_individual", "pif_annuity"),
        ("new_policies", "new_individual", "new_annuity"),
    ):
        a, b = results.pop(a_key, None), results.pop(b_key, None)
        if a and b and a.ok and b.ok:
            results[target] = FieldResult(
                target, value=a.value + b.value, ok=True, page=a.page,
                basis=a.basis if a.basis == b.basis else "不明", period=a.period, model=a.model,
                doc=a.doc, flags=sorted(set(a.flags) | set(b.flags)), method=a.method,
                evidence=f"{a.evidence} / {b.evidence}",
                note="個人保険＋個人年金保険の合計（団体除く、コード側で計算）",
            )
        else:
            results[target] = FieldResult(target, reason=f"{a_key}/{b_key} not both grounded")


def _fiscal_year(period: Optional[str]) -> Optional[int]:
    """Normalise '2025年度末' / '2026年3月31日現在' / '2026年3月期' to the fiscal-year start year."""
    if not period:
        return None
    m = re.search(r"(\d{4})年度", period)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{4})年(\d{1,2})月", period)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return y - 1 if mo <= 3 else y
    return None


def enforce_latest_period(results: dict[str, FieldResult]) -> None:
    """Reject values that belong to an earlier fiscal year than the document's
    own latest (modal) one. Seen in practice: 明治安田's (now dropped) 実質純資産額 table printed
    a 2024年度末 figure while the 2025年度末 column reads 廃止; the earlier
    year's number must not be reported as current."""
    years = [y for y in (_fiscal_year(r.period) for r in results.values() if r.ok) if y is not None]
    if not years:
        return
    ref = max(set(years), key=years.count)
    for r in results.values():
        y = _fiscal_year(r.period) if r.ok else None
        if y is not None and y < ref:
            r.ok = False
            r.reason = f"stale period {r.period!r} (document's latest is FY{ref})"


def apply_to_record(record: CompanyRecord, spec: CompanySpec, results: dict[str, FieldResult]) -> list[str]:
    """Write accepted results into `record`, never overwriting a value that
    is already present (EDINET/earlier source wins). Returns the names that
    stayed empty, with the reason recorded in field_provenance."""
    empty: list[str] = []
    for name, r in results.items():
        ok, why = _accept(name, r)
        if not ok:
            record.field_provenance[name] = Provenance(method=ExtractionMethod.NOT_ATTEMPTED, note=f"PDF抽出不採用: {why}")
            empty.append(name)
            continue
        target = record.common if hasattr(record.common, name) else None
        if target is None:
            ext_name = name
            target = record.extension()
            if spec.industry is Industry.SSI:
                setattr(target, ext_name, r.value)  # SSIExtension allows extras
                prov_ok = True
            elif hasattr(target, ext_name):
                if getattr(target, ext_name) is not None:
                    continue
                setattr(target, ext_name, r.value)
            else:
                empty.append(name)
                continue
        else:
            if getattr(target, name) is not None:
                continue
            setattr(target, name, r.value)
        if name == "solvency_ratio":
            record.common.solvency_basis = _solvency_basis(r, spec, _modal_year(results))
        record.field_provenance[name] = Provenance(
            method=r.method,
            source_page=r.page,
            evidence=r.evidence,
            basis=r.basis,
            period=r.period,
            source_doc=r.doc,
            flags=list(r.flags),
            note=(r.note or "") if r.method is ExtractionMethod.LLM_IMAGE_FALLBACK
            else f"{r.note or ''} [model={r.model}; 引用はページ本文と照合済み]".strip(),
        )
    return empty


def run_company(company_id: str, record: Optional[CompanyRecord] = None) -> tuple[CompanyRecord, dict[str, FieldResult]]:
    spec = BY_ID[company_id]
    if spec.source_type is not SourceType.PDF:
        raise ValueError(f"{company_id} is not a PDF-source company")
    record = record or CompanyRecord(
        company_id=spec.company_id, company_name=spec.company_name,
        industry=spec.industry, source_type=SourceType.PDF,
    )
    texts = load_page_texts(company_id)
    all_results: dict[str, FieldResult] = {}
    hinted = dict(spec.image_page_hints)
    for g in groups_for(spec):
        res = extract_group(spec, g, texts)
        if g.name in hinted:
            # A hand-located exact table beats whatever the text layer offered (for 損保ジャパン the
            # readable front pages only give 億円-rounded highlights), so image reads take precedence.
            for name, r in extract_group_images(spec, g, list(g.fields), texts).items():
                res[name] = r if r.ok else FieldResult(
                    name, reason=f"hinted page read failed ({r.reason}); text-layer value rejected as imprecise")
        all_results.update(res)
    if "assets_overdue_3m" in all_results:
        derive_substandard(all_results)
    if "pif_individual" in all_results:
        derive_life_totals(all_results)
    prefer_consolidated_solvency(all_results)
    enforce_latest_period(all_results)
    apply_to_record(record, spec, all_results)
    return record, all_results


# ---------------------------------------------------------------------------
# Supplement documents for EDINET companies
# ---------------------------------------------------------------------------

# group -> the schema fields it can fill (helper fields fold into these)
_GROUP_TARGETS = {
    "solvency_consol": ("solvency_ratio",),
    "loans": ("assets_bankrupt_claims", "assets_doubtful_claims", "assets_substandard_claims", "assets_normal_claims"),
    "life_contracts": ("policies_in_force", "new_policies"),
}


def _targets(group: Group) -> tuple[str, ...]:
    return _GROUP_TARGETS.get(group.name, tuple(f.name for f in group.fields))


def _is_empty(record: CompanyRecord, name: str) -> bool:
    if hasattr(record.common, name):
        return getattr(record.common, name) is None
    ext = record.life_ext or record.non_life_ext or record.ssi_ext
    return ext is None or getattr(ext, name, None) is None


def fetch_supplement(doc_key: str, url: str, company_name: str) -> Optional[Path]:
    """Download one of the company's own disclosure PDFs (robots.txt honoured) and confirm it is
    really that company's document from its own text/metadata before anything is read from it."""
    import requests

    from robots_check import USER_AGENT, is_fetch_allowed

    dest = RAW_DIR / f"{doc_key}.pdf"
    if not dest.exists():
        if not is_fetch_allowed(url):
            print(f"  [skip] robots.txt disallows {url}")
            return None
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=180)
        resp.raise_for_status()
        if not resp.content.startswith(b"%PDF"):
            print(f"  [skip] {url} is not a PDF")
            return None
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
    from extraction.pdf_source import _normalize

    spec = next((c for c in COMPANIES if c.company_name == company_name), None)
    targets = [_normalize(company_name)] + [_normalize(a) for a in (spec.name_aliases if spec else ())]
    texts = load_page_texts(doc_key)
    meta = ""
    with pdfplumber.open(dest) as pdf:
        meta = " ".join(str(v) for v in (pdf.metadata or {}).values())
    if not any(t in _normalize(x) for t in targets for x in [*texts[:15], meta]):
        print(f"  [skip] {doc_key}: company name not found in first pages/metadata (identity unverified)")
        return None
    return dest


def run_supplement(company_id: str, record: CompanyRecord) -> dict[str, FieldResult]:
    """Fill gaps in an EDINET-sourced `record` from the company's own disclosure booklets.
    Values already present (EDINET) are never overwritten, except the solvency ratio, where the
    booklet's official figure replaces EDINET's provisional MD&A figure."""
    spec = BY_ID[company_id]
    all_results: dict[str, FieldResult] = {}
    for doc_key, url in spec.supplement_docs:
        if fetch_supplement(doc_key, url, spec.company_name) is None:
            continue
        texts = load_page_texts(doc_key)
        results: dict[str, FieldResult] = {}
        for g in groups_for(spec):
            if g.name == "life_fund":
                continue  # 基金は相互会社特有。株式会社（かんぽ生命）には該当しない
            wanted = [n for n in _targets(g)
                      if (_is_empty(record, n) or n == "solvency_ratio") and not (all_results.get(n) and all_results[n].ok)]
            if wanted:
                results.update(extract_group(spec, g, texts, doc_key=doc_key))
        if "assets_overdue_3m" in results:
            derive_substandard(results)
        if "pif_individual" in results:
            derive_life_totals(results)
        prefer_consolidated_solvency(results)
        enforce_latest_period(results)
        for name, r in results.items():
            if r.ok and not (all_results.get(name) and all_results[name].ok):
                r.note = f"{r.note or ''} [補完元: {doc_key}.pdf]".strip()
                all_results[name] = r
            else:
                all_results.setdefault(name, r)
    def current(name: str) -> Optional[float]:
        if hasattr(record.common, name):
            return getattr(record.common, name)
        ext = record.life_ext or record.non_life_ext or record.ssi_ext
        return getattr(ext, name, None) if ext is not None else None

    def differs(a: float, b: float) -> bool:
        return abs(a - b) > max(0.05, 0.001 * abs(a))  # ratios: 0.05pt; amounts: 0.1%

    # 1) cross-source check: EDINET already holds a value and the booklet gives another one
    for n, r in all_results.items():
        cur = current(n)
        if r.ok and cur is not None and n != "solvency_ratio" and differs(cur, r.value):
            prov = record.field_provenance.setdefault(n, Provenance(method=ExtractionMethod.EDINET_XBRL))
            prov.flags.append("source_mismatch")
            prov.note = f"{prov.note or ''} [自社資料{r.doc}.pdfの値は {r.value:,.2f}（EDINET値を採用）]".strip()
    # 2) fill the gaps
    apply_to_record(record, spec, {n: r for n, r in all_results.items() if r.ok and _is_empty(record, n)})
    # 3) the booklet's official solvency ratio supersedes EDINET's provisional MD&A figure
    for n, r in all_results.items():
        if n == "solvency_ratio" and r.ok:
            before = current(n)
            flags = list(r.flags)
            note = f"{r.note or ''} [model={r.model}; 引用はページ本文と照合済み]".strip()
            if before is not None and differs(before, r.value):
                flags.append("source_mismatch")
                note += f" [置換前のEDINET値（経営者分析の暫定値）: {before:,.1f}]"
            record.common.solvency_ratio = r.value
            record.common.solvency_basis = _solvency_basis(r, spec, _modal_year(all_results))
            record.field_provenance[n] = Provenance(
                method=r.method, source_page=r.page, evidence=r.evidence, basis=r.basis, period=r.period,
                source_doc=r.doc, flags=flags, note=note)
    return all_results

if __name__ == "__main__":
    ids = sys.argv[1:] or ["nippon_life", "meiji_yasuda", "ms_sompo", "sompo_japan", "sbi_ikiiki_ssi", "sakura_ssi"]
    for cid in ids:
        record, results = run_company(cid)
        print(f"=== {cid} ===")
        for name, r in results.items():
            mark = "OK " if r.ok else "-- "
            val = f"{r.value:,.2f}" if r.value is not None else "None"
            print(f"  {mark}{name}: {val} (p{r.page}, {r.basis}, {r.period}, {r.model}) {'' if r.ok else r.reason}")
