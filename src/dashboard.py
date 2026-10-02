"""Streamlit dashboard over data/output/final.csv (run `python src/pipeline.py` first).

    streamlit run src/dashboard.py

The dashboard only reads the merged CSV; it never calls the extraction code. Every number carries
its status (auto-extracted / needs review / human-confirmed / human-corrected) and its source
document and page, and values that do not exist (not applicable, not yet disclosed, not found) are
shown as such instead of being blank. Solvency ratios are split by regulatory scale (ESR vs SMR)
because the two are not comparable.
"""

from __future__ import annotations

from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

FINAL = Path(__file__).resolve().parent.parent / "data" / "output" / "final.csv"
INDUSTRY_JA = {"life": "生命保険", "non_life": "損害保険", "ssi": "少額短期保険"}
SCALE_JA = {"ESR_100%基準": "経済価値ベース（ESR、100%基準）", "SMR_200%基準": "従来基準（SMR、200%基準）"}
VALUE_STATUSES = {"自動抽出", "要確認", "人が確認済み", "人が修正", "要修正（記録済み）"}
NO_VALUE_NOTE = {
    "対象外（制度上該当なし）": "対象外",
    "開示前": "開示前",
    "未取得（要確認）": "未取得",
    "資料に値なし（確認済み）": "資料に値なし",
}
# derived metrics are computed here from the extracted values and are labelled as such
DERIVED = {
    "d_profit_margin": ("経常利益率（派生）", "%", "ordinary_profit", "ordinary_income"),
    "d_net_assets_ratio": ("純資産比率（派生）", "%", "net_assets", "total_assets"),
    "d_policy_reserve_ratio": ("責任準備金／総資産（派生）", "%", "policy_reserve_total", "total_assets"),
}

st.set_page_config(page_title="保険ディスクロージャーBI", page_icon="📊", layout="wide")


@st.cache_data
def load() -> pd.DataFrame:
    df = pd.read_csv(FINAL, encoding="utf-8-sig", dtype=str).fillna("")
    df["value"] = pd.to_numeric(df["final_value"], errors="coerce")
    df["industry_ja"] = df["industry"].map(INDUSTRY_JA)
    df["company_short"] = (df["company_name"].str.replace("株式会社", "", regex=False)
                           .str.replace("相互会社", "", regex=False))
    df["warn"] = df["status"].isin(["要確認", "要修正（記録済み）"])  # a human-confirmed value no longer warns
    return df


if not FINAL.exists():
    st.error("data/output/final.csv がありません。先に `python src/pipeline.py` を実行してください。")
    st.stop()

df = load()

# ------------------------------------------------------------------ sidebar
st.sidebar.header("絞り込み")
industries = st.sidebar.multiselect("業態", list(INDUSTRY_JA.values()), default=list(INDUSTRY_JA.values()))
companies_all = df[df["industry_ja"].isin(industries)]["company_short"].drop_duplicates().tolist()
companies = st.sidebar.multiselect("会社", companies_all, default=companies_all)
include_review = st.sidebar.checkbox("要確認の値を含める", value=True,
                                     help="画像読取・連結代用・情報源の不一致などで、人の確認を勧めている値。⚠で表示します。")
unit_amount = st.sidebar.radio("金額の単位", ["百万円", "億円"], horizontal=True)
view = df[df["industry_ja"].isin(industries) & df["company_short"].isin(companies)].copy()
if not include_review:
    view = view[~(view["warn"] & view["value"].notna())]
scale = 0.01 if unit_amount == "億円" else 1.0


def shown(v: float, unit: str) -> float:
    return v if unit == "%" else v * scale


def fmt(v: float, unit: str) -> str:
    if unit == "%":
        return f"{v:,.1f}%"
    x = v * scale
    return f"{x:,.1f}" if abs(x) < 1000 else f"{x:,.0f}"


# ------------------------------------------------------------------ header
st.title("保険ディスクロージャーBI")
st.caption("個社（単体）ベースの統一スキーマで、生保・損保・少額短期保険の主要指標を横並びで見る。"
           "数値は各社の公開資料（EDINET・ディスクロージャー誌）からの自動抽出で、出所と確認状況を併記しています。")
c1, c2, c3, c4 = st.columns(4)
n_values = int(df["value"].notna().sum())
c1.metric("対象会社", df["company_id"].nunique())
c2.metric("値が入っている項目", n_values)
c3.metric("要確認（⚠）", int((df["warn"] & df["value"].notna()).sum()))
c4.metric("値なし", int(df["value"].isna().sum()), help="対象外・開示前・未取得の合計")

tab_chart, tab_table, tab_source, tab_notes = st.tabs(["指標の比較", "比較表", "出所と確認状況", "読み方・注意点"])

# ------------------------------------------------------------------ chart
with tab_chart:
    base_fields = (view[["field", "label", "unit"]].drop_duplicates().set_index("field").to_dict("index"))
    options = {f"{v['label']}（{v['unit'] if v['unit'] == '%' else unit_amount}）": k for k, v in base_fields.items()}
    for k, (label, _unit, _a, _b) in DERIVED.items():
        options[label] = k
    default = next((k for k in options if k.startswith("ソルベンシー")), list(options)[0])
    choice = st.selectbox("指標", list(options), index=list(options).index(default))
    key = options[choice]

    if key in DERIVED:
        label, unit, num, den = DERIVED[key]
        wide = view[view["field"].isin([num, den])].pivot_table(index="company_short", columns="field", values="value")
        info = view.drop_duplicates("company_short").set_index("company_short")
        if num in wide and den in wide:
            wide = wide.dropna(subset=[num, den])
            wide["value"] = wide[num] / wide[den] * 100
        else:
            wide = pd.DataFrame()
        chart_df = wide.reset_index()[["company_short", "value"]] if not wide.empty else pd.DataFrame(columns=["company_short", "value"])
        chart_df["industry_ja"] = chart_df["company_short"].map(info["industry_ja"]) if not chart_df.empty else []
        chart_df["scale_tag"], chart_df["warn"], chart_df["status"] = "", False, "派生（計算値）"
        chart_df["label"], chart_df["unit"] = label, "%"
        missing = pd.DataFrame(columns=["company_short", "status"])
        st.caption("派生指標は、抽出した値から本ダッシュボード内で計算したものです（原資料の値ではありません）。")
    else:
        sub = view[view["field"] == key]
        chart_df = sub[sub["value"].notna()].copy()
        missing = sub[sub["value"].isna()][["company_short", "status"]]
        unit = sub["unit"].iloc[0] if not sub.empty else ""

    if chart_df.empty:
        st.info("表示できる値がありません。")
    else:
        chart_df["display"] = [shown(v, u) for v, u in zip(chart_df["value"], chart_df["unit"])]
        chart_df["text"] = [fmt(v, u) + (" ⚠" if w else "") for v, u, w in zip(chart_df["value"], chart_df["unit"], chart_df["warn"])]
        groups = list(chart_df.groupby("scale_tag")) if key == "solvency_ratio" else [("", chart_df)]
        for tag, part in groups:
            if tag:
                st.subheader(SCALE_JA.get(tag, tag))
                st.caption("ソルベンシー比率は、算出方式（ESRとSMR）が異なると単純比較できません。方式ごとに分けて表示しています。")
            part = part.assign(axis_label=part["company_short"] + "　" + part["text"])  # value in the label: readable in any theme
            bars = alt.Chart(part).mark_bar().encode(
                x=alt.X("display:Q", title=choice),
                y=alt.Y("axis_label:N", sort=alt.EncodingSortField("display", order="descending"), title=None,
                        axis=alt.Axis(labelLimit=420, labelOverlap=False)),
                color=alt.Color("industry_ja:N", title="業態"),
                tooltip=[alt.Tooltip("company_short:N", title="会社"), alt.Tooltip("text:N", title="値"),
                         alt.Tooltip("status:N", title="状態")],
            )
            st.altair_chart(bars.properties(height=max(140, 44 * len(part))), use_container_width=True)
    if not missing.empty:
        st.markdown("**値なし：** " + "、".join(f"{r.company_short}（{NO_VALUE_NOTE.get(r.status, r.status)}）" for r in missing.itertuples()))
    if not chart_df.empty and (chart_df["unit"] != "%").any() and chart_df["industry_ja"].nunique() > 1:
        st.caption("金額は会社規模が大きく異なる（少額短期保険は大手と桁が違う）ため、会社間の比較には比率の指標が向いています。")

# ------------------------------------------------------------------ table
with tab_table:
    st.caption("1行＝指標、1列＝会社。値が無いセルには理由（対象外・開示前・未取得）を表示します。⚠は要確認。")
    rows = []
    for (field, label, unit), g in view.groupby(["field", "label", "unit"], sort=False):
        row = {"指標": f"{label}（{unit if unit == '%' else unit_amount}）"}
        for r in g.itertuples():
            if pd.notna(r.value):
                row[r.company_short] = fmt(r.value, unit) + (" ⚠" if r.warn else "")
            else:
                row[r.company_short] = NO_VALUE_NOTE.get(r.status, r.status)
        rows.append(row)
    table = pd.DataFrame(rows)
    st.dataframe(table, use_container_width=True, hide_index=True)
    st.download_button("表示中の比較表をCSVでダウンロード", table.to_csv(index=False, encoding="utf-8-sig"),
                       file_name="comparison.csv", mime="text/csv")

# ------------------------------------------------------------------ source
with tab_source:
    st.caption("各値の出所（資料・ページ）と確認状況です。ページはPDFビューアの1始まりの番号です。")
    statuses = st.multiselect("ステータス", sorted(df["status"].unique()), default=sorted(df["status"].unique()))
    src = view[view["status"].isin(statuses)][
        ["company_short", "label", "value", "unit", "status", "basis", "period", "source_doc", "source_page", "method", "flags", "note"]
    ].rename(columns={"company_short": "会社", "label": "項目", "value": "値", "unit": "単位", "status": "状態", "basis": "単体/連結",
                      "period": "期間", "source_doc": "資料", "source_page": "ページ", "method": "抽出方法", "flags": "フラグ", "note": "メモ"})
    st.dataframe(src, use_container_width=True, hide_index=True)
    st.markdown("要確認の項目は、`python src/pipeline.py` が生成する **要確認レポート**（`data/review/review.html`、ページ画像つき）で目視確認し、"
                "修正は `data/overrides.csv` に1行足して再実行します。")

# ------------------------------------------------------------------ notes
with tab_notes:
    st.markdown("""
**単位と基準**
- 金額は百万円（サイドバーで億円に切替）。比率は%。単体（個社）ベースが原則で、ソルベンシー比率だけは連結を優先しています（行ごとの「単体/連結」で確認できます）。
- 全社とも2026年3月期（2025年度）の資料です。

**ソルベンシー比率は方式が2つあります**
- 生保・損保は経済価値ベース（ESR、100%基準）、少額短期保険は従来基準（SMR、200%基準）のままです。数値の水準も意味も違うため、同じ軸では比べません。
- 損保3社（東京海上日動・三井住友海上・損保ジャパン）は、2026年3月期の比率がまだ開示されていません（経過措置で2026年10月末までに開示予定）。

**値がない理由の区別**
- **対象外**：制度上存在しない項目（例：少額短期保険の債権区分は、運用が預金・国債等に限られるため該当なし。株式会社に基金はない）。
- **開示前**：開示予定だがまだ出ていない。
- **未取得**：取得できていない（要確認レポートで原因を確認します）。

**あえて集めていない項目**
- 実質純資産額（規制上の根拠が2026年3月31日施行の命令改正で廃止）、EV/EEV（付録的開示で会社ごとに概念が異なる）、異常危険準備金（責任準備金に含めて扱う）。

**派生指標**
- 経常利益率・純資産比率・責任準備金比率は、抽出した値からこの画面で計算した参考値です。
""")
