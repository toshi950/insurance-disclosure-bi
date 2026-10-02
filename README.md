# Insurance Disclosure BI (working title)

## 課題 (Problem)

🇯🇵 日本の保険業界には、上場企業の有価証券報告書のようなEDINET/XBRL的な統一データ形式が存在しない。生命保険協会・日本損害保険協会・日本少額短期保険協会の会員各社は、保険業法第111条に基づき毎年「ディスクロージャー資料」を公開しているが、各社が独自のPDFレイアウト・用語で作成しており、会社間でソルベンシーマージン比率や責任準備金等の主要指標を横並びで比較すること自体に手作業のコストがかかる。一部の会社（自社が上場している、または公募社債等の継続開示義務を負う個社）は例外的にEDINETへも単体ベースの有価証券報告書を提出しているが、これも会社によって提出有無が分かれ、業界全体を覆う構造化データにはなっていない。

🇬🇧 Japan's insurance industry has no EDINET/XBRL-style standardized disclosure format comparable to what listed companies provide. Member companies of the Life Insurance Association of Japan, the General Insurance Association of Japan, and the Japan Small Amount and Short Term Insurance Association each publish their own annual "disclosure documents" (mandated under Insurance Business Act Article 111), but every company uses its own PDF layout and terminology, making it costly to compare key metrics (solvency margin ratio, policy reserves, etc.) across companies. A subset of companies — those that are themselves listed, or that carry continuous-disclosure obligations from publicly offered bonds and similar instruments — also individually file EDINET securities reports on a non-consolidated basis, but this coverage is inconsistent across the industry and does not amount to industry-wide structured data.

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

### 対象企業（実地検証済み、2026-09-23確定）

| 業界 | データソース | 対象 |
|---|---|---|
| 生命保険 | EDINET（個社・単独提出） | 株式会社かんぽ生命保険 |
| 生命保険 | PDF | 日本生命保険相互会社 |
| 生命保険 | PDF | 明治安田生命保険相互会社 |
| 損害保険 | EDINET（個社・単独提出） | 東京海上日動火災保険株式会社 |
| 損害保険 | PDF | 三井住友海上火災保険株式会社 |
| 損害保険 | PDF | 損害保険ジャパン株式会社 |
| 少額短期保険 | PDF | SBIいきいき少額短期保険株式会社 |
| 少額短期保険 | PDF | さくら少額短期保険株式会社 |

> 📝 少額短期保険業界にはEDINETパターンが存在しない：少額短期保険業者の大半は非上場の小規模事業者で、金融商品取引法上のEDINET提出義務を単体で負っていない（上場グループの子会社であっても、EDINETに出るのは親会社の連結決算であり、少短子会社単体の指標は切り出せない）。これは本プロジェクトが可視化しようとしている開示水準の断層そのものを裏付ける発見として記録する。

> 📝 ソルベンシー規制の断層：2026年3月期より、生保・損保は区別なく経済価値ベースのソルベンシー規制（ESR）に移行した一方、少額短期保険業者は対象法人格に含まれず旧来のソルベンシー・マージン比率（SMR、200%基準）のまま据え置かれている（金融庁公式資料で確認、実データでも裏付け済み——少短のソルベンシー・マージン比率は1,000%を超える水準で開示されており、算出方式・スケールともに生損保とは別物）。「ソルベンシー比率」という一見共通に見える指標ですら、算出方式のタグ付けなしに8社横断で単純比較できないという事実自体が、本プロジェクトの課題設定を裏付けている。

## 現在の状況 (Status)
- [x] データソースの実地検証（EDINET個社データ・各社PDFの取得可否確認、2026-09-23完了）
  - EDINET個社データは表紙の会社名を機械的に検証する必要がある（検索結果の帰属表示が信頼できないケースがあった）
  - PDFの形式は会社ごとに大きく異なる：フォントエンコーディング崩れ（ページ画像化＋LLM読み取りで対応）、決算説明資料（要約）と正式なディスクロージャー誌（本体）の混同、ファイル分割方針の違い、サイトのSPA化による発見しにくさ、等
- [x] 統一スキーマ設計（業界共通コア＋業界別拡張の2層構造）
- [x] 抽出パイプライン実装（EDINET API v2のXBRL＋各社ディスクロージャー資料のPDF。LLMの出力は原文との照合を通った値のみ採用、文字化けページは画像の複数回読取）
- [ ] ダッシュボード実装（Streamlit）

## 検証の方針と人の確認 (Verification and human review)

🇯🇵
- LLMが返した値は、引用が資料本文に存在し数値を含むことをコードで検算し、通らなければ採用しない。単位換算・合算・比率の算出もコード側で行う。
- 本文が文字化けしているページは、ページ画像を複数回読み取り、一致した値のみ採用する。
- 各値に、抽出方法・ページ・引用・単体/連結・期間の出所メモを付ける。抽出方法別の件数は `python src/pipeline.py` の出力で確認できる。
- 人が関与する箇所は2つ：①文字化けページの位置指定（`image_page_hints`）、②目視確認済みの値の上書き（`manual_values`、現在は0件）。いずれも根拠を残す運用で、「全自動」とは主張しない。
- 未開示・廃止の項目は推測で埋めず空欄にする。

🇬🇧
- A value returned by the LLM is accepted only if its quoted evidence is verifiable in the page text and contains the number; unit conversion, sums and ratios are computed in code, not by the model.
- Pages with a garbled text layer are read from page images several times; only values on which the reads agree are kept.
- Every value carries provenance (method, page, quote, entity basis, period). The count of values per extraction path is printed by `python src/pipeline.py`.
- Humans are involved in two places: locating pages in garbled documents (`image_page_hints`) and overriding values they confirmed by eye (`manual_values`, currently empty). Both leave an audit trail; the project does not claim to be fully automatic.
- Items that are undisclosed or discontinued are left blank, never guessed.

## データの扱い (Data handling)

🇯🇵 このリポジトリはコードとスキーマのみを公開し、抽出した数値データ・元のPDF・LLM応答のキャッシュは含めない（`data/` はgit管理外）。各自の環境で、各社が公開する資料を取得して生成する。資料の取得時はrobots.txtを確認する。

🇬🇧 This repository publishes code and schema only. Extracted figures, source PDFs and LLM response caches are not included (`data/` is untracked); they are generated locally from each company's public disclosures, with robots.txt checked at fetch time.
