# overrides.csv の書き方 / Writing `data/overrides.csv`

🇯🇵 `data/overrides.csv` は、人が目視で確認した結果・修正した値を残すファイルです。抽出結果（`data/output/extracted.csv`）とは別に保存され、再実行しても消えません。`data/` は git 管理外なので、このファイルも手元にだけ置かれます。

**初回実行では作られません。** 判断を最初に記録した時点（下のコマンド、または手書き）で作られます。ファイルがなければ「人の判断なし」として動きます。

## 記録の方法

要確認レポート（`data/review/review.html`）を見て、項目ごとに次のどちらかで記録します。

```bash
# 確認した結果を記録する（ok | no_disclosure | needs_fix）
python src/review.py confirm <company_id> <field> ok "原本の p.12 と一致を確認"

# 値を修正する
python src/review.py override <company_id> <field> <value> "原本の表に合わせて修正"
```

`company_id` は `src/companies.py` に、`field` は `data/output/final.csv` の `field` 列に載っています。記録後に `./run.sh`（または `python src/pipeline.py`）を再実行すると `final.csv` に反映されます。

CSV を直接編集してもかまいません。文字コードは UTF-8、1行目は次のヘッダーです。

```csv
company_id,field,decision,value,expected_extracted_value,reason,date
```

## 列の意味

| 列 | 内容 |
|---|---|
| `company_id` | `companies.py` の `company_id` |
| `field` | 項目名（`final.csv` の `field` 列） |
| `decision` | `ok`：抽出値は原本と一致 / `no_disclosure`：原本に記載がないことを確認 / `needs_fix`：誤りがあるが未修正 / `override`：`value` で置き換える |
| `value` | `decision` が `override` のときだけ使う。単位は `final.csv` の `unit` 列（金額は百万円、比率は%） |
| `expected_extracted_value` | 確認した時点の抽出値。コマンドで記録すると自動で入る。手書きの場合は、確認した抽出値を書く |
| `reason` | 理由・確認箇所のメモ |
| `date` | 記録日（コマンドなら自動） |

## 例（値は説明用の架空のもの）

```csv
company_id,field,decision,value,expected_extracted_value,reason,date
example_co,total_assets,ok,,1000000,原本の貸借対照表と一致,2026-01-01
example_co,policy_reserve_total,override,900000,800000,原本の合計行に合わせて修正,2026-01-01
example_co,solvency_ratio,no_disclosure,,,原本に記載なしを確認,2026-01-01
```

## 動作の決まり

- 同じ `company_id` と `field` の行が複数あれば、**最後の行が有効**です。
- `ok`・`no_disclosure` を記録した項目は、要確認レポートから外れます。ただし、その後に抽出値が変わる（`expected_extracted_value` と食い違う）と「古い記録」として警告が出て、再び確認対象になります。
- `override` の値は `final.csv` で抽出値より優先され、`status` は「人が修正」になります。

---

🇬🇧 `data/overrides.csv` stores what a person verified or corrected. It is kept apart from the extracted output and survives re-runs. `data/` is untracked, so this file stays local.

**It is not created on the first run.** It is created when the first decision is recorded (with the commands above, or by hand). Without the file, the pipeline simply assumes there are no human decisions.

Record a decision with `python src/review.py confirm <company_id> <field> ok|no_disclosure|needs_fix "reason"` or a correction with `python src/review.py override <company_id> <field> <value> "reason"`, then re-run `./run.sh`. Editing the CSV directly also works (UTF-8, header `company_id,field,decision,value,expected_extracted_value,reason,date`). The example rows above use made-up values and a placeholder company.

Rules: the last row for a `(company_id, field)` pair wins; `ok` / `no_disclosure` hide an item from the exception report until its extracted value changes (a mismatch with `expected_extracted_value` is reported as stale); an `override` value replaces the extracted one in `final.csv`, in the unit shown in its `unit` column.
