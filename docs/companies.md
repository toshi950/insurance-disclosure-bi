# 対象会社の登録内容を直す・足す / Editing the registered companies

🇯🇵 取り込む会社は `src/companies.py` の `COMPANIES` リスト（8社）で決まります。会社名だけでは追加できず、**取得元の特定と、取得した資料の確認**が必要です。このページは、既存の8社を直す場合と、会社を足す場合の手順です。

## 1社分の登録内容（`CompanySpec`）

| 項目 | 使う場面 | 内容 |
|---|---|---|
| `company_id` | 全社 | 英数字の識別子。`data/` 配下のファイル名や `overrides.csv` で使うので、後から変えない |
| `company_name` | 全社 | 資料の表紙の会社名。取り込んだ資料との照合に使う |
| `industry` | 全社 | `Industry.LIFE`（生保）/ `NON_LIFE`（損保）/ `SSI`（少短） |
| `source_type` | 全社 | `SourceType.EDINET` または `SourceType.PDF` |
| `edinet_code`、`document_id` | EDINET | EDINET の提出者コードと書類ID（例：`S100…`）。年度ごとに変わる |
| `pdf_url`、`site_root` | PDF | ディスクロージャー資料（本体）のURLとサイトのルート。`site_root` は robots.txt の確認に使う |
| `supplement_docs` | EDINET | EDINET で取れない項目を補う、自社サイト上の資料。`(識別子, URL)` の組 |
| `image_page_hints` | 文字化けPDF | 文字化けしたページを画像で読むための、項目グループ名と**0始まり**のページ番号 |
| `not_applicable`、`not_yet_disclosed` | 必要なとき | 制度上存在しない項目、未開示の項目と、その理由。空欄を「取得失敗」と区別するために使う |
| `name_aliases` | 必要なとき | 表紙の略称（会社名の照合で許容） |
| `notes` | 任意 | 確認した経緯のメモ |

## 既存の会社を直す

### 新年度の資料に更新する
1. `pdf_url`（PDFの会社）または `document_id`、`supplement_docs` のURL（EDINETの会社）を新しいものに差し替える。
2. **PDFの会社は `data/raw/{company_id}.pdf` を削除する。** PDFは同名のファイルがあると再取得されないため、消さないと古い資料のまま処理されます（補助資料は `data/raw/{識別子}.pdf`）。
3. ページ位置が年度で変わるため、`image_page_hints` は見直す。
4. `./run.sh` を再実行する。ページ抽出のキャッシュは資料の更新時刻、LLMの回答キャッシュは入力内容で自動的に無効になります。
5. 年度が変わると `overrides.csv` の記録は「古い記録」と警告されることがあるので、要確認レポートで見直す。

### 開示が間に合っていない項目
`not_yet_disclosed` に `(項目名, 理由)` を書くと、空欄のまま「開示前」と表示されます。開示されたら削除して再実行します。

## 会社を足す

1. **資料を特定する。** 上場または継続開示義務のある会社はEDINETの有価証券報告書（単体）、それ以外は協会加盟社のディスクロージャー資料（本体）。**決算説明資料などの要約版では項目が足りません。**
2. **会社名を機械的に確認する。** 検索結果の帰属表示は信頼できないことがあり、EDINET書類は表紙の会社名で確認しています。
3. `COMPANIES` に `CompanySpec` を追加する。既存の同じ業態・取得経路の会社をコピーすると早いです。
4. 実行して `data/review/review.html` を見る。取れなかった項目は、資料に記載がなければ `not_applicable`／`not_yet_disclosed`、文字化けならページを探して `image_page_hints` に書きます。
5. 抽出対象の項目は、会社ではなく業態ごとに `src/schema.py` と `src/extraction/pdf_fields.py` に定義されています。同じ業態の会社なら、項目の定義を触る必要はありません。業態を増やす場合は、スキーマと抽出の定義を足す必要があります。

## 守ること

- 取得は各社が公開している資料に限り、`robots.txt` で禁止されていればコードが取得を止めます。
- 抽出した数値・元のPDF・LLMのキャッシュは `data/` に置かれ、公開リポジトリには含めません。
- 個社（単体）ベースを使い、連結の値は代用扱いとして要確認レポートに出ます。IFRS・US GAAP採用の会社は対象外です。

---

🇬🇧 The companies to ingest are the `COMPANIES` list in `src/companies.py` (8 entries). Adding one needs more than a name: you must identify the source and check the document. The table above lists every `CompanySpec` field.

**Refreshing to a new fiscal year:** replace `pdf_url` / `document_id` / the `supplement_docs` URLs, **delete `data/raw/{company_id}.pdf`** (an existing file is never re-downloaded), review `image_page_hints` (0-based page indices that shift between years), then re-run. The page cache is invalidated by file mtime and the LLM cache by its input hash. Old `overrides.csv` rows may be reported as stale; recheck them in the exception report.

**Adding a company:** locate the full disclosure booklet (EDINET filing for filers, otherwise the association member's disclosure document — not a summary deck), verify the cover name, copy a similar `CompanySpec`, run, and use `data/review/review.html` to resolve gaps with `not_applicable` / `not_yet_disclosed` / `image_page_hints`. Extracted fields are defined per industry (`schema.py`, `extraction/pdf_fields.py`), so a company in an existing industry needs no field definitions; a new industry does.

**Rules:** only publicly disclosed documents are fetched and `robots.txt` is honoured; extracted data, PDFs and LLM caches stay in untracked `data/`; entity-level J-GAAP figures only.
