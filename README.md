# Insurance Disclosure BI (working title)

## 課題 (Problem)

🇯🇵 日本の保険業界には、上場企業の有価証券報告書のようなEDINET/XBRL的な統一データ形式が存在しない。生命保険協会・日本損害保険協会・日本少額短期保険協会の会員各社は、保険業法第111条に基づき毎年「ディスクロージャー資料」を公開しているが、各社が独自のPDFレイアウト・用語で作成しており、会社間でソルベンシーマージン比率や責任準備金等の主要指標を横並びで比較すること自体に手作業のコストがかかる。一部の会社（特定子会社基準を満たす個社）は例外的にEDINETへも単体ベースの有価証券報告書を提出しているが、これも会社によって提出有無が分かれ、業界全体を覆う構造化データにはなっていない。

🇬🇧 Japan's insurance industry has no EDINET/XBRL-style standardized disclosure format comparable to what listed companies provide. Member companies of the Life Insurance Association of Japan, the General Insurance Association of Japan, and the Japan Small Amount and Short Term Insurance Association each publish their own annual "disclosure documents" (mandated under Insurance Business Act Article 111), but every company uses its own PDF layout and terminology, making it costly to compare key metrics (solvency margin ratio, policy reserves, etc.) across companies. A subset of companies that meet the "specified subsidiary" threshold also individually file EDINET securities reports on a non-consolidated basis, but this coverage is inconsistent across the industry and does not amount to industry-wide structured data.

## アプローチ (Approach)

🇯🇵 個社（保険引受主体そのもの。持株会社の連結決算ではない）を単位とした統一スキーマを設計し、構造化データ（EDINET個社提出分）と非定型PDF（各社ディスクロージャー資料）の両方から同じスキーマへ正規化する。抽出は決定的処理（テキスト・表抽出）を一次手段とし、LLMは低信頼度時の検証・補完にのみ発動するハイブリッド構成とする（リトライ上限付き、無条件の自律ループは作らない）。

🇬🇧 The unit of analysis is the individual underwriting entity itself, not a holding company's consolidated results. A unified schema is designed to normalize both structured data (EDINET filings from individually-filing subsidiaries) and unstructured PDFs (each company's disclosure documents) into the same shape. Extraction uses deterministic processing (text/table extraction) as the primary method, with an LLM invoked only for verification/completion when confidence is low — bounded by a retry limit, never an unconditional autonomous loop.

## 対象範囲 (Scope)

🇯🇵 意図的な限定：
- **会計基準はJ-GAAPのみ**。IFRS/US GAAP採用企業は対象外とする（保険負債の会計処理の前提自体が異なり、単純な指標比較が成立しないため。IFRS/USGAAPは国際的な比較可能性インフラが既に一定程度存在し、このプロジェクトが埋めようとしているギャップの優先度が相対的に低い）
- **連結決算は使わない**。個社（単体）ベースのデータのみを対象とする。持株会社の連結財務諸表は、保険引受以外の事業や複数の保険会社の数値が混在し、かつ大手グループはIFRS任意適用が進んでいるため、比較可能性の観点で不適格と判断した
- **対象社数は生保・損保・少額短期保険の3業界×原則3パターン**（後述）に限定し、各協会加盟社全社への網羅的対応はスコープ外とする（対応社数の拡張は本プロジェクト完了後の発展的課題として扱う）

🇬🇧 Deliberate boundaries:
- **J-GAAP only.** Companies reporting under IFRS/US GAAP are excluded — insurance liability accounting differs fundamentally between standards, so metrics wouldn't be comparable, and international standards already have reasonably mature comparability infrastructure elsewhere.
- **Non-consolidated (entity-level) data only.** Holding-company consolidated statements are excluded, since they blend multiple business lines/subsidiaries and major groups have increasingly adopted IFRS at the consolidated level.
- **A bounded set of companies** across three industry segments (see below), not exhaustive coverage of every association member. Expanding coverage is treated as a future/parallel enhancement, not a blocker for this project's completion.

### 対象企業（初期スコープ）

| 業界 | データソース | 対象 |
|---|---|---|
| 生命保険 | EDINET（個社・特定子会社として単独提出） | 第一生命保険株式会社 |
| 生命保険 | PDF | 日本生命保険相互会社 |
| 生命保険 | PDF | 明治安田生命保険相互会社 |
| 損害保険 | EDINET（個社・特定子会社として単独提出） | 東京海上日動火災保険株式会社 |
| 損害保険 | PDF | 三井住友海上火災保険株式会社 |
| 損害保険 | PDF | あいおいニッセイ同和損保 または 損害保険ジャパン |
| 少額短期保険 | PDF | au少額短期保険 |
| 少額短期保険 | PDF | GMO少額短期保険 |

> 📝 少額短期保険業界にはEDINETパターンが存在しない：少額短期保険業者の大半は非上場の小規模事業者で、金融商品取引法上のEDINET提出義務を単体で負っていない（上場グループの子会社であっても、EDINETに出るのは親会社の連結決算であり、少短子会社単体の指標は切り出せない）。これは本プロジェクトが可視化しようとしている開示水準の断層そのものを裏付ける発見として記録する。

## 現在の状況 (Status)
- [ ] データソースの実地検証（EDINET個社データ・各社PDFの取得可否確認）
- [ ] 統一スキーマ設計
- [ ] 抽出パイプライン実装（決定的処理＋LLM検証ループ）
- [ ] ダッシュボード実装（Streamlit）
