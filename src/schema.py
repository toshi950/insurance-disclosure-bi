"""Unified schema for insurance disclosure data.

Two-layer design (see README "アプローチ" section):
- Common core: fields every company should attempt to report, regardless of
  industry segment (life / non-life / small-amount-short-term, "SSI").
- Industry extensions: fields that only make sense for one segment, kept
  separate so cross-industry comparison never forces an artificial mapping
  (e.g. 基礎利益 has no non-life equivalent; コンバインドレシオ has no life
  equivalent).

Every numeric field is Optional: a company simply not disclosing an item is
expected and must be representable, not treated as an extraction failure.
Every field that could plausibly be extracted with low confidence carries
a companion `*_confidence` note when relevant (kept lightweight: see
`Provenance` below rather than one confidence field per value).
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Industry(str, Enum):
    LIFE = "life"  # 生命保険
    NON_LIFE = "non_life"  # 損害保険
    SSI = "ssi"  # 少額短期保険 (Small-amount, Short-term Insurance)


class SourceType(str, Enum):
    EDINET = "edinet"  # 個社・単独提出の有価証券報告書 (non-consolidated)
    PDF = "pdf"  # 各社ディスクロージャー資料 (unstructured PDF)


class ExtractionMethod(str, Enum):
    """How a given value was obtained. Kept per-value so the dashboard can
    flag anything that went through the LLM fallback path.
    """

    DETERMINISTIC = "deterministic"  # pdfplumber text/table extraction
    EDINET_XBRL = "edinet_xbrl"  # EDINET API v2 の構造化XBRL（個別・非連結）から直接取得
    LLM_VERIFIED = "llm_verified"  # deterministic result, LLM confirmed
    LLM_IMAGE_FALLBACK = "llm_image_fallback"  # page image + LLM read (see
    # ループ設計原則: only used when deterministic extraction fails, e.g.
    # font-encoding corruption as seen with 損保ジャパン)
    NOT_DISCLOSED = "not_disclosed"  # confirmed absent from the source
    NOT_ATTEMPTED = "not_attempted"  # extraction not yet run for this field


class SolvencyBasis(str, Enum):
    """The tagging that makes the headline "solvency ratio" comparable (or
    explicitly *not* comparable) across companies. See README's ソルベンシー
    規制の断層 note: this distinction is itself part of the project's thesis,
    not a detail to normalize away.
    """

    ESR_100 = "ESR_100%基準"  # 経済価値ベースのソルベンシー比率 (2026年3月期〜、生保・損保)
    SMR_200 = "SMR_200%基準"  # 旧来のソルベンシー・マージン比率 (少短)


class ReserveMethodBasis(str, Enum):
    """Which axis a company's 支払備金 breakdown was disclosed on. Recorded
    because the *actual* disclosed axis (再保険控除前後 × 地震/自賠責特掲, per
    the Tokio Marine実データ) turned out to differ from the originally
    assumed 普通備金/IBNR axis — both are represented so extraction doesn't
    force data into the wrong shape.
    """

    NORMAL_IBNR = "normal_ibnr"  # 普通備金 / IBNR
    NET_OF_REINSURANCE = "net_of_reinsurance"  # 出再控除前後の差引 × 地震・自賠責特掲
    SINGLE_FIGURE = "single_figure"  # 内訳非開示、単一集計値のみ (生保はこれが標準)


class Provenance(BaseModel):
    """Where a value came from and how much to trust it. Attached to
    individual fields that are worth auditing (the headline/ambiguous ones),
    rather than every single field, to keep the schema readable.
    """

    method: ExtractionMethod = ExtractionMethod.NOT_ATTEMPTED
    source_page: Optional[int] = None
    note: Optional[str] = None


class CommonCore(BaseModel):
    """Fields every company should attempt to report."""

    total_assets: Optional[float] = Field(None, description="総資産（百万円）")
    net_assets: Optional[float] = Field(None, description="純資産（百万円）")
    ordinary_income: Optional[float] = Field(None, description="経常収益（百万円）")
    ordinary_profit: Optional[float] = Field(None, description="経常利益（百万円）")
    net_income: Optional[float] = Field(None, description="当期純利益（百万円）")

    # --- ソルベンシー比率: 値と算出方式は必ずセットで扱う ---
    solvency_ratio: Optional[float] = Field(
        None, description="ソルベンシー関連比率（%）。basisと必ずセットで解釈する"
    )
    solvency_basis: Optional[SolvencyBasis] = None
    solvency_provenance: Optional[Provenance] = None

    # --- 責任準備金 (2026-09-23: 生損保共通と判明、共通コアへ格上げ済み) ---
    policy_reserve_ordinary: Optional[float] = Field(
        None, description="普通責任準備金（百万円）。これ以上の内訳分解はしない"
    )
    policy_reserve_contingency: Optional[float] = Field(
        None,
        description="危険準備金（百万円）。生保拡張のcontingency_reserve_breakdownで内訳を扱う",
    )

    # --- 支払備金 ---
    claims_reserve_total: Optional[float] = Field(
        None, description="支払備金・単一集計値（百万円）。必須項目"
    )
    claims_reserve_basis: Optional[ReserveMethodBasis] = None
    claims_reserve_normal: Optional[float] = Field(
        None, description="普通備金（百万円）。任意サブフィールド、損保・少短で開示があれば"
    )
    claims_reserve_ibnr: Optional[float] = Field(
        None, description="IBNR・既発生未報告備金（百万円）。任意サブフィールド"
    )
    claims_reserve_provenance: Optional[Provenance] = None

    # --- 資産自己査定5区分 (保険業法に基づく債権区分) ---
    assets_bankrupt_claims: Optional[float] = Field(None, description="破産更生債権等（百万円）")
    assets_doubtful_claims: Optional[float] = Field(None, description="危険債権（百万円）")
    assets_substandard_claims: Optional[float] = Field(
        None, description="要管理債権：三月以上延滞債権＋貸付条件緩和債権（百万円）"
    )
    assets_normal_claims: Optional[float] = Field(None, description="正常債権（百万円）")


class LifeExtension(BaseModel):
    """生保拡張。Industry.LIFE の企業にのみ適用。"""

    base_profit: Optional[float] = Field(
        None, description="基礎利益（百万円）＝経常利益－（キャピタル損益＋臨時損益）"
    )
    policies_in_force: Optional[float] = Field(None, description="保有契約高（商品区分別、百万円）")
    new_policies: Optional[float] = Field(None, description="新契約高（商品区分別、百万円）")
    annualized_premium: Optional[float] = Field(None, description="年換算保険料（百万円）")
    lapse_rate: Optional[float] = Field(None, description="解約失効率（%）")
    embedded_value: Optional[float] = Field(
        None,
        description="実質純資産額 or EEV（百万円）。任意開示のため個社EDINETには基本出てこない",
    )
    contingency_reserve_breakdown: Optional[dict[str, float]] = Field(
        None,
        description="危険準備金の内訳（施行規則第69条：保険リスク対応/予定利率リスク対応 等）",
    )
    mutual_company_fund: Optional[float] = Field(
        None, description="基金（百万円）。相互会社特有科目、株式会社形態の場合はNone"
    )


class NonLifeExtension(BaseModel):
    """損保拡張。Industry.NON_LIFE の企業にのみ適用。"""

    net_premiums_written: Optional[float] = Field(None, description="正味収入保険料（百万円）")
    net_claims_paid: Optional[float] = Field(None, description="正味支払保険金（百万円）")
    loss_ratio: Optional[float] = Field(None, description="損害率（%）")
    expense_ratio: Optional[float] = Field(None, description="事業費率（%）")
    combined_ratio: Optional[float] = Field(
        None, description="コンバインドレシオ（%）＝損害率＋事業費率"
    )
    catastrophe_reserve: Optional[float] = Field(
        None,
        description="異常危険準備金（百万円）。施行規則第70条、損保に固有の科目"
        "（生保は同機能を危険準備金でカバーしており科目名自体が存在しない）",
    )
    catastrophe_reserve_by_line: Optional[dict[str, float]] = Field(
        None, description="異常危険準備金の種目別内訳（開示があれば、例：三井住友海上）"
    )


class SSIExtension(BaseModel):
    """少短拡張。Industry.SSI の企業にのみ適用。

    生保協会「虎の巻」・損保協会「かんたんガイド」いずれにも少短固有の指標
    定義がないため、実データ確認時に個別把握したフィールドのみ持つ。現時点
    ではプレースホルダー（未使用）。
    """

    model_config = {"extra": "allow"}


class CompanyRecord(BaseModel):
    """One company's full extracted record for one fiscal year."""

    company_id: str = Field(..., description="companies.py のキーと一致させる")
    company_name: str
    industry: Industry
    source_type: SourceType
    fiscal_year_end: Optional[date] = None
    document_id: Optional[str] = Field(None, description="EDINET書類ID、またはPDFのソース識別子")
    verified_cover_page: bool = Field(
        False,
        description="PDF表紙の【会社名】【事業年度】【提出日】を機械的に検証済みか。"
        "WebSearchの帰属表示は信頼しないという実地検証の教訓に基づく必須チェック",
    )

    common: CommonCore = Field(default_factory=CommonCore)
    life_ext: Optional[LifeExtension] = None
    non_life_ext: Optional[NonLifeExtension] = None
    ssi_ext: Optional[SSIExtension] = None

    def extension(self):
        """Return whichever industry extension applies, constructing an
        empty one on first access."""
        if self.industry is Industry.LIFE:
            if self.life_ext is None:
                self.life_ext = LifeExtension()
            return self.life_ext
        if self.industry is Industry.NON_LIFE:
            if self.non_life_ext is None:
                self.non_life_ext = NonLifeExtension()
            return self.non_life_ext
        if self.industry is Industry.SSI:
            if self.ssi_ext is None:
                self.ssi_ext = SSIExtension()
            return self.ssi_ext
        raise ValueError(f"Unknown industry: {self.industry}")
