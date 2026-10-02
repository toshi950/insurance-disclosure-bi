"""Registry of the 8 target companies (see README "対象企業" table).

For EDINET companies we already have verified document IDs (2026-09-23 実地
検証). For PDF companies the disclosure URL is not yet locked down here —
`pdf_url` is left as None with a TODO until each is located and the file is
confirmed to be the full disclosure booklet (not a summary/決算説明資料,
per the 明治安田生命 lesson in the handoff notes).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from schema import Industry, SourceType


@dataclass(frozen=True)
class CompanySpec:
    company_id: str
    company_name: str
    industry: Industry
    source_type: SourceType
    # EDINET path
    edinet_code: Optional[str] = None
    document_id: Optional[str] = None  # e.g. "S100YD29"
    # PDF path
    pdf_url: Optional[str] = None
    site_root: Optional[str] = None  # used for robots.txt lookups
    notes: str = ""
    # Garbled-text-layer PDFs only: (group name, 0-based PDF page indices) located by
    # hand (2026-10-03). Page discovery by model-read headings proved unreliable, so
    # image extraction reads only these pages. See extraction/pdf_fields.py.
    image_page_hints: tuple[tuple[str, tuple[int, ...]], ...] = ()
    # EDINET companies: the company's own disclosure booklets (統合報告書／ディスクロージャー誌) on its
    # website, used only for fields EDINET XBRL cannot supply. (doc_key, url); the file is cached as
    # data/raw/{doc_key}.pdf. Added 2026-10-03 (user's intent: read each company's own disclosure
    # materials, not the EDINET-rendered PDF).
    supplement_docs: tuple[tuple[str, str], ...] = ()
    # (field, value in 百万円/%, note) — values a person confirmed against the source by eye; applied
    # after extraction only where the field is still empty.
    manual_values: tuple[tuple[str, float, str], ...] = ()
    name_aliases: tuple[str, ...] = ()  # short forms used on booklet covers (identity check accepts them)


COMPANIES: list[CompanySpec] = [
    CompanySpec(
        company_id="kampo",
        company_name="株式会社かんぽ生命保険",
        industry=Industry.LIFE,
        source_type=SourceType.EDINET,
        edinet_code="E31755",
        document_id="S100YD29",
        name_aliases=("かんぽ生命",),
        supplement_docs=(
            ("kampo_material", "https://www.jp-life.japanpost.jp/ir/disclosure/assets/pdf/2026/disc26_material_a4.pdf"),
            ("kampo_main", "https://www.jp-life.japanpost.jp/ir/disclosure/assets/pdf/2026/disc26_all_a4.pdf"),
            ("kampo_solvency", "https://www.jp-life.japanpost.jp/ir/disclosure/assets/pdf/2026/disc26_ability_situation.pdf"),
        ),
        notes="第20期、2026-06-18提出。持株会社を介さず直接上場。PDF直リンク検証済み"
        "（disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/{doc}.pdf）",
    ),
    CompanySpec(
        company_id="tokio_marine_nichido",
        company_name="東京海上日動火災保険株式会社",
        industry=Industry.NON_LIFE,
        source_type=SourceType.EDINET,
        edinet_code="E03823",
        document_id="S100YLTM",
        supplement_docs=(
            ("tokio_perf", "https://www.tokiomarine-nichido.co.jp/company/pdf/TMNF_2026_d_05.pdf"),
        ),
        notes="第83期、2026-06-26提出。非上場の完全子会社だが公募社債等の継続開示義務により単独提出。"
        "PDF直リンク検証済み",
    ),
    CompanySpec(
        company_id="nippon_life",
        company_name="日本生命保険相互会社",
        industry=Industry.LIFE,
        source_type=SourceType.PDF,
        pdf_url="https://www.nissay.co.jp/assets/kaisha/annai/gyoseki/2026/disc2026_02.pdf",
        site_root="https://www.nissay.co.jp",
        notes="「統合報告書2026」の【資料編】(5.13MB)。本編は disc2026.pdf。"
        "基礎利益・保有契約高・新契約高・責任準備金等を確認済み。「基金」等の相互会社特有科目あり",
    ),
    CompanySpec(
        company_id="meiji_yasuda",
        company_name="明治安田生命保険相互会社",
        industry=Industry.LIFE,
        source_type=SourceType.PDF,
        pdf_url="https://www.meijiyasuda.co.jp/profile/corporate_info/disclosure/data/status-2026/pdf/status_2026_01.pdf",
        site_root="https://www.meijiyasuda.co.jp",
        notes="【別冊】「業績に関する諸資料」(2026年版)。検索で最初に見つかりがちな"
        "決算説明資料（PowerPoint由来の要約デック、支払備金・解約失効率等が欠落）とは別物、"
        "こちらが正式な数値本体。「資料編」(status_2026_07.pdf)にも重複データがある可能性あり",
    ),
    CompanySpec(
        company_id="ms_sompo",
        company_name="三井住友海上火災保険株式会社",
        industry=Industry.NON_LIFE,
        source_type=SourceType.PDF,
        pdf_url="https://www.ms-ins.com/company/aboutus/disclosure/data/f01.pdf",
        site_root="https://www.ms-ins.com",
        notes="「業績データ」セクション(f01.pdf)。実地検証済み（種目別×準備金種類の2軸クロス表を含む"
        "詳細開示、異常危険準備金も種目別内訳まで取得可能）。他セクションはa01〜g01に分割されている",
    ),
    CompanySpec(
        company_id="sompo_japan",
        company_name="損害保険ジャパン株式会社",
        industry=Industry.NON_LIFE,
        source_type=SourceType.PDF,
        pdf_url="https://www.sompo-japan.co.jp/-/media/SJNK/files/company/disclosure/2026/sj_disc2026.pdf",
        site_root="https://www.sompo-japan.co.jp",
        notes="「損保ジャパンの現状2026」。⚠️一部テキストがフォントエンコーディング崩れ。"
        "業績データ章（0始まりp115〜）は文字がglyph-ID化しテキスト抽出不可（2026-10-03確認）。"
        "該当ページを画像で二重読取（pdf_fields.py参照）。1ファイルに統合、292ページ。"
        "貸借対照表=p130、損益計算書=p135、保険業法に基づく債権=p147。単体ソルベンシー比率は2026年10月末開示予定（p37）で未開示",
        image_page_hints=(("reserves", (130,)), ("pl", (135,)), ("loans", (147,))),
    ),
    CompanySpec(
        company_id="sbi_ikiiki_ssi",
        company_name="SBIいきいき少額短期保険株式会社",
        industry=Industry.SSI,
        source_type=SourceType.PDF,
        pdf_url="https://www.i-sedai.com/pdf/disclosure2025.pdf",
        site_root="https://www.i-sedai.com",
        notes="2025年度版。実地検証済み。ソルベンシー・マージン比率が1,000%を超える水準であることを確認"
        "（生損保のESRとは別基準・別スケール）。ドメインはi-sedai.com"
        "（別会社の類似名「SBI日本少額短期保険」n-ssi.co.jpと混同しないこと）",
    ),
    CompanySpec(
        company_id="sakura_ssi",
        company_name="さくら少額短期保険株式会社",
        industry=Industry.SSI,
        source_type=SourceType.PDF,
        pdf_url="https://www.sakura-ssi.co.jp/wp/wp-content/uploads/2025/07/disclosure_2025.pdf",
        site_root="https://www.sakura-ssi.co.jp",
        notes="2025年度版。実地検証済み。31ページの小規模開示",
    ),
]

BY_ID: dict[str, CompanySpec] = {c.company_id: c for c in COMPANIES}


def edinet_pdf_url(spec: CompanySpec) -> str:
    if spec.source_type is not SourceType.EDINET or not spec.document_id:
        raise ValueError(f"{spec.company_id} is not an EDINET company with a known document_id")
    return f"https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/{spec.document_id}.pdf"
