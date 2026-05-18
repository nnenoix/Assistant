"""Analytics primitives — ABC classification (80/15/5 rule) and a full
multi-metric analyzer.

Ported from D:\\UniversalAnalytics\\core\\analyzer.py (Tkinter desktop app
by the user). Algorithm preserved exactly: sort by metric desc, cumsum,
A = top 80%, B = next 15%, C = remaining 5%. The full analyzer adds:
  - Per-SKU grouping with sums of revenue/qty/profit
  - 3-metric ABC (revenue, quantity, profit) + composite code 'AAA'/'AAC'/...
  - Weighted score (40% revenue + 40% profit + 20% quantity)
  - Smart-advice strings ("ЛИДЕР", "Скрытый алмаз", etc.)
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def abc_split(rows: list[dict], metric: str) -> list[dict]:
    """Classify each row as A/B/C by `metric`. Rows are sorted within the
    function (no mutation). Returns the same rows with a new key `abc` set.

    Rule:
        Sort by `metric` desc. Cumulative share of total.
        share ≤ 0.80 → A; ≤ 0.95 → B; else → C. Zero/negative → C.
    """
    if not rows:
        return []
    df = pd.DataFrame(rows)
    if metric not in df.columns:
        for r in rows:
            r.setdefault("abc", "?")
        return rows
    cats = _calc_abc(df, metric)
    df["abc"] = cats
    return df.to_dict("records")


def _calc_abc(df: pd.DataFrame, target_col: str) -> pd.Series:
    """Mirror of Analyzer._calc_abc_generic. Returns a Series of A/B/C
    indexed exactly like the input df."""
    if target_col not in df.columns:
        return pd.Series(["?"] * len(df), index=df.index)
    # Coerce to numeric
    col = pd.to_numeric(df[target_col], errors="coerce").fillna(0)
    temp = df.assign(_metric=col).sort_values(by="_metric", ascending=False).copy()
    pos = temp.loc[temp["_metric"] > 0, "_metric"]
    total = pos.sum()
    if total <= 0:
        return pd.Series(["C"] * len(df), index=df.index)
    temp["_cumsum"] = temp["_metric"].cumsum()
    temp["_share"] = temp["_cumsum"] / total
    cond = [
        temp["_metric"] <= 0,
        temp["_share"] <= 0.80,
        temp["_share"] <= 0.95,
    ]
    res = np.select(cond, ["C", "A", "B"], default="C")
    return pd.Series(res, index=temp.index)


def _smart_advice(code: str) -> str:
    if "?" in code:
        return "Нет данных"
    if code == "AAA":
        return "🔥 Лидер — держать сток"
    if code == "CCC":
        return "Кандидат на вывод из ассортимента"
    if code[0] == "A" and code[2] == "C":
        return "Работа в ноль — проверить цену/себестоимость"
    if code[0] == "C" and code[2] == "A":
        return "💎 Скрытый алмаз — раскрутить в рекламе"
    return "Базовый товар"


def abc_analysis(
    rows: list[dict],
    sku_col: str = "sku",
    revenue_col: str = "revenue",
    qty_col: str = "qty",
    profit_col: str | None = "profit",
    costs: list[dict] | None = None,
) -> dict:
    """Full ABC analysis on a list of row dicts (e.g. from sheets, excel,
    or parsed bank statements).

    Required columns: sku_col, revenue_col, qty_col. profit_col optional.
    `costs` is an optional [{sku, cost}, ...] list — if provided, final_profit
    = revenue - cost × qty is computed.

    Returns:
        {
            total_skus, total_revenue, total_qty, total_profit,
            categories: {AAA: n, AAB: n, ..., CCC: n},
            top_a: [{sku, name?, revenue, qty, profit, margin_pct, abc, advice}, ...],
            rows: [...full sorted result...]
        }
    """
    if not rows:
        return {"error": "empty input"}
    df = pd.DataFrame(rows).copy()
    # Normalize column names
    for c in (sku_col, revenue_col, qty_col):
        if c not in df.columns:
            return {"error": f"missing column '{c}'. Got: {list(df.columns)}"}

    df[sku_col] = df[sku_col].fillna("Не указано").astype(str)
    agg = {revenue_col: "sum", qty_col: "sum"}
    if profit_col and profit_col in df.columns:
        agg[profit_col] = "sum"
    name_col = "name" if "name" in df.columns else None
    if name_col:
        agg[name_col] = "first"
    grouped = df.groupby(sku_col, as_index=False).agg(agg)

    # Costs merge
    if costs:
        costs_df = pd.DataFrame(costs)
        if "sku" in costs_df.columns and "cost" in costs_df.columns:
            costs_df["sku"] = costs_df["sku"].astype(str).str.strip()
            grouped[sku_col] = grouped[sku_col].astype(str).str.strip()
            grouped = pd.merge(grouped, costs_df[["sku", "cost"]],
                              left_on=sku_col, right_on="sku", how="left")
            grouped["cost"] = grouped["cost"].fillna(0)
            grouped["final_profit"] = grouped[revenue_col] - (grouped["cost"] * grouped[qty_col])
        else:
            grouped["cost"] = 0
            grouped["final_profit"] = grouped.get(profit_col, 0)
    elif profit_col and profit_col in grouped.columns:
        grouped["final_profit"] = grouped[profit_col]
        grouped["cost"] = 0
    else:
        grouped["final_profit"] = 0
        grouped["cost"] = 0

    grouped["margin_pct"] = np.where(
        grouped[revenue_col] != 0,
        grouped["final_profit"] / grouped[revenue_col] * 100,
        0,
    )

    # 3 metrics
    grouped["abc_rev"] = _calc_abc(grouped, revenue_col)
    grouped["abc_qty"] = _calc_abc(grouped, qty_col)
    grouped["abc_prof"] = _calc_abc(grouped, "final_profit")
    grouped["abc_code"] = grouped["abc_rev"] + grouped["abc_qty"] + grouped["abc_prof"]
    grouped["advice"] = grouped["abc_code"].apply(_smart_advice)

    # Composite score (40/40/20)
    max_rev = grouped[revenue_col].max() or 1
    max_qty = grouped[qty_col].max() or 1
    max_prof = grouped["final_profit"].max() or 1
    grouped["score"] = (
        grouped[revenue_col] / max_rev * 0.4
        + grouped["final_profit"] / max_prof * 0.4
        + grouped[qty_col] / max_qty * 0.2
    )
    grouped = grouped.sort_values(by="score", ascending=False)

    # Round
    for col in [revenue_col, qty_col, "final_profit", "cost", "margin_pct", "score"]:
        if col in grouped.columns:
            grouped[col] = pd.to_numeric(grouped[col], errors="coerce").round(2)

    # Top A summary
    top_a = grouped[grouped["abc_rev"] == "A"].head(15).to_dict("records")
    categories = grouped["abc_code"].value_counts().to_dict()

    return {
        "total_skus": int(len(grouped)),
        "total_revenue": float(grouped[revenue_col].sum()),
        "total_qty": float(grouped[qty_col].sum()),
        "total_profit": float(grouped["final_profit"].sum()),
        "avg_margin_pct": float(grouped["margin_pct"].mean()),
        "categories": {k: int(v) for k, v in categories.items()},
        "abc_rev_counts": {
            "A": int((grouped["abc_rev"] == "A").sum()),
            "B": int((grouped["abc_rev"] == "B").sum()),
            "C": int((grouped["abc_rev"] == "C").sum()),
        },
        "top_a": top_a,
        "rows": grouped.to_dict("records"),
    }
