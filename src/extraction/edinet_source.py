"""EDINET API v2 source: fetch XBRL-as-CSV for a filing and map the
non-consolidated (個別) figures onto the unified schema.

Why API v2 instead of the PDF route (see README / handoff notes, 2026-09-27):
the PDF download URL works but yields unstructured text; the API returns
the filing's XBRL already flattened to CSV (`type=5`), so values arrive as
tagged facts and need no table-parsing or LLM verification.

Design rules carried over from the PDF pipeline:
- Individual (非連結) only. Facts are accepted only when the context is
  exactly `*_NonConsolidatedMember` — contexts carrying any extra axis
  member (equity-statement columns etc.) are ignored so e.g. `NetAssets`
  resolves to the balance-sheet total, not a component column.
- Identity is verified from the filing itself (DEI: EDINETCode and
  FilerName) before anything is mapped — the XBRL equivalent of the PDF
  cover-page check.
- A field absent from the filing stays None; nothing is guessed.

The API key is read from `EDINET_API_KEY` (.env, gitignored) and sent as a
header. It is never logged or included in raised error messages.
"""

from __future__ import annotations

import csv
import io
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from companies import BY_ID, CompanySpec  # noqa: E402
from schema import (  # noqa: E402
    CompanyRecord,
    ExtractionMethod,
    Industry,
    Provenance,
    SolvencyBasis,
    SourceType,
)

load_dotenv()

API_BASE = "https://api.edinet-fsa.go.jp/api/v2"
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache"
YEN_PER_MILLION = 1_000_000

_CURRENT_PERIODS = {"当期", "当期末"}


class EdinetError(Exception):
    pass


def _api_key() -> str:
    key = os.environ.get("EDINET_API_KEY")
    if not key:
        raise EdinetError("EDINET_API_KEY not set (expected in .env)")
    return key


def fetch_csv_zip(document_id: str, cache_dir: Path = CACHE_DIR, force: bool = False) -> Path:
    """Download the XBRL-to-CSV zip (`type=5`) for `document_id`, cached."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / f"{document_id}_csv.zip"
    if dest.exists() and not force:
        return dest

    try:
        resp = requests.get(
            f"{API_BASE}/documents/{document_id}",
            params={"type": 5},
            headers={"Ocp-Apim-Subscription-Key": _api_key()},
            timeout=120,
        )
    except requests.RequestException as e:
        # Do not chain: the original exception can embed request details.
        raise EdinetError(f"{document_id}: request failed ({type(e).__name__})") from None
    if resp.status_code != 200:
        raise EdinetError(f"{document_id}: HTTP {resp.status_code}")
    if not resp.content.startswith(b"PK"):
        # API v2 returns JSON error bodies with HTTP 200 in some cases.
        raise EdinetError(f"{document_id}: response is not a zip (document missing or key rejected)")

    dest.write_bytes(resp.content)
    return dest


@dataclass(frozen=True)
class Fact:
    element: str  # local name without prefix, e.g. "NetAssets"
    label: str
    period: str  # 当期 / 当期末 / 前期 ...
    scope: str  # 個別 / 連結 / その他
    context: str
    value: str


def load_facts(zip_path: Path) -> list[Fact]:
    """Parse the main securities-report CSV (jpcrp030000-asr) of the zip."""
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if "jpcrp030000" in n and n.endswith(".csv")]
        if not names:
            raise EdinetError(f"{zip_path.name}: no jpcrp030000 CSV in archive")
        text = z.read(names[0]).decode("utf-16")
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    next(reader)  # header
    facts = []
    for r in reader:
        if len(r) < 9:
            continue
        facts.append(
            Fact(
                element=r[0].split(":", 1)[-1],
                label=r[1],
                period=r[3],
                scope=r[4],
                context=r[2],
                value=r[8],
            )
        )
    return facts


@dataclass
class FilingIdentity:
    edinet_code: Optional[str]
    filer_name: Optional[str]
    fiscal_year_end: Optional[date]
    accounting_standard: Optional[str]


def read_identity(facts: list[Fact]) -> FilingIdentity:
    dei = {f.element: f.value for f in facts if f.element.endswith("DEI")}
    fy_end = dei.get("CurrentFiscalYearEndDateDEI")
    return FilingIdentity(
        edinet_code=dei.get("EDINETCodeDEI"),
        filer_name=dei.get("FilerNameInJapaneseDEI"),
        fiscal_year_end=date.fromisoformat(fy_end) if fy_end and fy_end[0].isdigit() else None,
        accounting_standard=dei.get("AccountingStandardsDEI"),
    )


def verify_identity(identity: FilingIdentity, spec: CompanySpec) -> bool:
    """Both the EDINET code and the filer name must agree with the registry
    (the filer name is compared loosely, as in pdf_source._normalize)."""
    if identity.edinet_code != spec.edinet_code:
        return False
    from extraction.pdf_source import _normalize  # noqa: E402

    return _normalize(spec.company_name) == _normalize(identity.filer_name or "")


def _is_plain_nonconsolidated(f: Fact) -> bool:
    """True only for the filing's headline individual context, i.e.
    `CurrentYear{Instant,Duration}_NonConsolidatedMember` with no further
    axis members appended."""
    return f.scope == "個別" and f.period in _CURRENT_PERIODS and f.context.endswith(
        ("CurrentYearInstant_NonConsolidatedMember", "CurrentYearDuration_NonConsolidatedMember")
    ) and f.context.count("Member") == 1


def _is_plain_nonconsolidated_kpi(f: Fact) -> bool:
    """経営指標等（`*SummaryOfBusinessResults*`）は scope が「個別」にならない
    ことがあるため、文脈IDのみで個別・当期を判定する。"""
    return f.period in _CURRENT_PERIODS and f.context.endswith(
        ("CurrentYearInstant_NonConsolidatedMember", "CurrentYearDuration_NonConsolidatedMember")
    ) and f.context.count("Member") == 1


def _to_million_yen(raw: str) -> Optional[float]:
    raw = raw.strip().replace(",", "")
    try:
        return float(raw) / YEN_PER_MILLION
    except ValueError:
        return None  # "－" etc.: the filing shows no figure


# ---------------------------------------------------------------------------
# Text-block parsing (notes that EDINET exposes only as flattened text)
# ---------------------------------------------------------------------------
# 注記・経営者分析の多くはXBRLの数値タグではなくテキストブロックで提出され、表は
# 「ラベル＋数値」が空白なしで連結された平文になる。そのため、(1) ラベル直後の数値
# トークンのみを読み、(2) 合計が貸借対照表本体（構造化タグ）と一致するかを必ず
# 検算し、(3) 一致しなければ値を捨てて None のままにする（推測で埋めない）。

_NUM = re.compile(r"(△?\d{1,3}(?:,\d{3})*|－)")


def _numbers_after(text: str, label: str, count: int) -> Optional[list[Optional[float]]]:
    """First `count` numeric tokens following `label` (`－` -> 0.0, `△` -> negative).
    Returns None if the label is absent or too few tokens follow."""
    i = text.find(label)
    if i < 0:
        return None
    tokens = _NUM.findall(text[i + len(label):])[:count]
    if len(tokens) < count:
        return None
    out: list[Optional[float]] = []
    for t in tokens:
        if t == "－":
            out.append(0.0)
        else:
            neg = t.startswith("△")
            v = float(t.lstrip("△").replace(",", ""))
            out.append(-v if neg else v)
    return out


def _text_block(facts: list[Fact], element_prefix: str) -> Optional[str]:
    """Individual-context (non-consolidated) text block whose element name
    starts with `element_prefix`."""
    for f in facts:
        if f.element.startswith(element_prefix) and f.context.endswith(
            ("CurrentYearInstant_NonConsolidatedMember", "CurrentYearDuration_NonConsolidatedMember")
        ):
            return f.value
    return None


_RECEIVABLE_LABELS = (
    "破産更生債権及びこれらに準ずる債権額",
    "危険債権額",
    "三月以上延滞債権額",
    "貸付条件緩和債権額",
)


def parse_asset_classification(text: str) -> Optional[dict[str, float]]:
    """保険業法に基づく債権区分（百万円）。「該当するものはありません」は4区分とも0の明示開示。
    表形式の場合は合計との一致を検算。正常債権はXBRL注記に出ないので扱わない。"""
    if "該当するものはありません" in text:
        return {"bankrupt": 0.0, "doubtful": 0.0, "overdue_3m": 0.0, "restructured": 0.0}
    vals = []
    for label in _RECEIVABLE_LABELS:
        n = _numbers_after(text, label, 2)
        if n is None:
            return None
        vals.append(n[1])
    total = _numbers_after(text, "合計", 2)
    if total is None or abs(sum(vals) - total[1]) > 1:
        return None
    return dict(zip(("bankrupt", "doubtful", "overdue_3m", "restructured"), vals))


def parse_consolidated_solvency(text: str) -> Optional[float]:
    """経営者分析の「連結ソルベンシー・マージン比率は181％」。個別の比率はXBRLに存在しない。"""
    m = re.search(r"連結ソルベンシー・マージン比率は(\d+(?:\.\d+)?)％", text)
    return float(m.group(1)) if m else None


@dataclass
class EdinetExtraction:
    record: CompanyRecord
    identity: FilingIdentity
    identity_verified: bool
    missing: list[str] = field(default_factory=list)


# schema field -> candidate XBRL element names, first match wins.
COMMON_MAP: dict[str, tuple[str, ...]] = {
    "total_assets": ("Assets",),
    "net_assets": ("NetAssets",),
    "ordinary_income": ("OperatingIncomeINS", "OrdinaryRevenuesINS"),
    "ordinary_profit": ("OrdinaryIncome",),
    "net_income": ("ProfitLoss",),
    "claims_reserve_total": ("OutstandingClaimsLiabilitiesINS",),
}
NON_LIFE_MAP: dict[str, tuple[str, ...]] = {
    "net_premiums_written": ("NetPremiumsWrittenOIINS",),
    "net_claims_paid": ("NetLossPaidOEINS",),
}


def extract(company_id: str, zip_path: Optional[Path] = None) -> EdinetExtraction:
    spec = BY_ID[company_id]
    if spec.source_type is not SourceType.EDINET or not spec.document_id:
        raise EdinetError(f"{company_id} is not an EDINET company")

    facts = load_facts(zip_path or fetch_csv_zip(spec.document_id))
    identity = read_identity(facts)
    verified = verify_identity(identity, spec)

    record = CompanyRecord(
        company_id=spec.company_id,
        company_name=spec.company_name,
        industry=spec.industry,
        source_type=SourceType.EDINET,
        fiscal_year_end=identity.fiscal_year_end,
        document_id=spec.document_id,
        verified_cover_page=verified,
    )
    if not verified:
        # Same stance as the PDF path: unverified identity -> map nothing.
        return EdinetExtraction(record, identity, False, missing=["<identity mismatch>"])

    plain = {}
    for f in facts:
        if _is_plain_nonconsolidated(f):
            plain.setdefault(f.element, f)

    missing: list[str] = []

    def pick(candidates: tuple[str, ...]) -> Optional[float]:
        for name in candidates:
            fact = plain.get(name)
            if fact is not None:
                return _to_million_yen(fact.value)
        return None

    for field_name, candidates in COMMON_MAP.items():
        value = pick(candidates)
        if value is None:
            missing.append(field_name)
        else:
            setattr(record.common, field_name, value)

    common = record.common
    kpi = {f.element: f for f in facts if _is_plain_nonconsolidated_kpi(f)}

    def kpi_value(name: str) -> Optional[float]:
        f = kpi.get(name)
        if f is None:
            return None
        try:
            return float(f.value.replace(",", ""))
        except ValueError:
            return None  # "－"

    # --- 責任準備金・支払備金：貸借対照表の総額のみ（内訳は取らない。schema.py参照）---
    common.policy_reserve_total = pick(("PolicyReserveLiabilitiesINS",))
    if common.policy_reserve_total is None:
        missing.append("policy_reserve_total")
    common.claims_reserve_provenance = Provenance(
        method=ExtractionMethod.EDINET_XBRL,
        note="OutstandingClaimsLiabilitiesINS（貸借対照表の支払備金・総額）",
    )

    # --- 資産自己査定（保険業法に基づく債権区分）。正常債権は注記に出ない ---
    loans_text = _text_block(facts, "NotesRegardingLoansBasedOnInsuranceBusinessAct")
    loans = parse_asset_classification(loans_text) if loans_text else None
    if loans:
        common.assets_bankrupt_claims = loans["bankrupt"]
        common.assets_doubtful_claims = loans["doubtful"]
        common.assets_substandard_claims = loans["overdue_3m"] + loans["restructured"]
    else:
        missing += ["assets_bankrupt_claims", "assets_doubtful_claims", "assets_substandard_claims"]
    missing.append("assets_normal_claims")

    # --- ソルベンシー比率：連結ベースで取る方針（2026-10-03決定）。個別の比率はXBRLに無く、
    # 経営者分析に本文記載があれば採用、無ければ None（PDF側で補完）---
    md_a = next((f.value for f in facts if f.element.startswith("ManagementAnalysis")), None)
    ratio = parse_consolidated_solvency(md_a) if md_a else None
    if ratio is not None:
        common.solvency_ratio = ratio
        common.solvency_basis = SolvencyBasis.ESR_100
        common.solvency_provenance = Provenance(
            method=ExtractionMethod.EDINET_XBRL,
            note="経営者分析の連結ソルベンシー・マージン比率（経済価値ベース、監査未済の暫定値）。"
            "ソルベンシー比率は連結ベースで取る方針",
        )
    else:
        missing.append("solvency_ratio")
        common.solvency_provenance = Provenance(
            method=ExtractionMethod.NOT_ATTEMPTED,
            note="EDINET XBRLの本文に比率の数値記載なし（連結・個別とも）。PDF側で補完する",
        )

    # --- 業界別拡張 ---
    if spec.industry is Industry.NON_LIFE:
        ext = record.extension()
        for field_name, candidates in NON_LIFE_MAP.items():
            value = pick(candidates)
            if value is None:
                missing.append(field_name)
            else:
                setattr(ext, field_name, value)
        loss, expense = kpi_value("NetLossRatioSummaryOfBusinessResultsINS"), kpi_value(
            "NetOperatingExpenseRatioSummaryOfBusinessResultsINS"
        )
        if loss is not None:
            ext.loss_ratio = round(loss * 100, 2)
        else:
            missing.append("loss_ratio")
        if expense is not None:
            ext.expense_ratio = round(expense * 100, 2)
        else:
            missing.append("expense_ratio")
        if loss is not None and expense is not None:
            ext.combined_ratio = round((loss + expense) * 100, 2)
        else:
            missing.append("combined_ratio")
    elif spec.industry is Industry.LIFE:
        ext = record.extension()
        base = kpi_value("CorePofitSummaryOfBusinessResults")  # 基礎利益（タクソノミ上の綴りは CorePofit）
        if base is not None:
            ext.base_profit = base / YEN_PER_MILLION
        else:
            missing.append("base_profit")
        # 保有契約高・新契約高・年換算保険料・解約失効率：個別XBRLのタグに無い（PDF側で補完）
        missing += ["policies_in_force", "new_policies", "annualized_premium", "lapse_rate"]

    return EdinetExtraction(record, identity, True, missing)


if __name__ == "__main__":
    for cid in ("kampo", "tokio_marine_nichido"):
        try:
            res = extract(cid)
        except EdinetError as e:
            print(f"{cid}: FAILED - {e}")
            continue
        i = res.identity
        print(f"{cid}: identity_verified={res.identity_verified} "
              f"({i.filer_name}, {i.edinet_code}, FY end {i.fiscal_year_end}, {i.accounting_standard})")
        print("  common :", res.record.common.model_dump(exclude_none=True, mode="json"))
        ext = res.record.life_ext or res.record.non_life_ext
        if ext:
            print("  ext    :", ext.model_dump(exclude_none=True, mode="json"))
        print("  missing:", res.missing)
