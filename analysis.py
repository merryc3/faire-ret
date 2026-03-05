import glob
import os

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns


CHARTS_DIR = "charts"


def load_data():
    """Load retailers and orders CSVs using glob."""
    retailers_paths = glob.glob("psa_exercise_retailers*.csv")
    orders_paths = glob.glob("psa_exercise_orders*.csv")

    if not retailers_paths:
        raise FileNotFoundError("No psa_exercise_retailers*.csv files found.")
    if not orders_paths:
        raise FileNotFoundError("No psa_exercise_orders*.csv files found.")

    retailers_path = retailers_paths[0]
    orders_path = orders_paths[0]

    print(f"Using retailers file: {retailers_path}")
    print(f"Using orders file: {orders_path}")

    retailers = pd.read_csv(
        retailers_path,
        parse_dates=[
            "FIRST_APP_SESSION_AT",
            "FIRST_DESKTOP_SESSION_AT",
            "FIRST_MOBILE_WEB_SESSION_AT",
            "FIRST_CONFIRMED_ORDER_PLACED_AT",
        ],
    )
    orders = pd.read_csv(
        orders_path,
        parse_dates=["ORDER_CREATED"],
    )

    return retailers, orders


def initial_exploration(retailers: pd.DataFrame, orders: pd.DataFrame) -> None:
    """Print basic info, dtypes, nulls, and some categorical value_counts."""
    print("\n=== Retailers shape, columns, dtypes ===")
    print(retailers.shape)
    print(retailers.columns)
    print(retailers.dtypes)

    print("\n=== Retailers null counts ===")
    print(retailers.isna().sum())

    print("\n=== Orders shape, columns, dtypes ===")
    print(orders.shape)
    print(orders.columns)
    print(orders.dtypes)

    print("\n=== Orders null counts ===")
    print(orders.isna().sum())

    # Categorical columns in retailers
    cat_cols = retailers.select_dtypes(include=["object", "bool"]).columns
    print("\n=== Retailers categorical value_counts (top 20) ===")
    for col in cat_cols:
        print(f"\nValue counts for retailers[{col}]:")
        print(retailers[col].value_counts(dropna=False).head(20))

    # Confirm ORDER_ID is not unique by showing a sample multi-row order
    print("\n=== Sample multi-row ORDER_ID from orders ===")
    order_counts = orders["ORDER_ID"].value_counts()
    multi_order_ids = order_counts[order_counts > 1]
    if not multi_order_ids.empty:
        sample_id = multi_order_ids.index[0]
        print(f"Sample ORDER_ID with multiple rows: {sample_id}")
        print(orders[orders["ORDER_ID"] == sample_id].head())
    else:
        print("All ORDER_IDs appear to be unique (unexpected for this dataset).")


def build_cohort_matrices(
    retailers: pd.DataFrame, orders: pd.DataFrame
):
    """
    Prepare cohort-level monthly GMV matrices and per-retailer metrics.

    Returns:
        orders_with_month: orders joined with retailers and month_index
        gmv_pivot: RETAILER_ID x month_index matrix of total GMV
        active_matrix: RETAILER_ID x month_index matrix of 1/0 activity
        retailers_metrics: retailers with attached GMV/retention features
        ndr_series: NDR by month_index
        max_month: maximum month_index observed
    """
    # Join orders to cohort retailers to ensure alignment
    retailers_core_cols = [
        "RETAILER_ID",
        "FIRST_CONFIRMED_ORDER_PLACED_AT",
        "FLAG_APP_INSTALLED",
        "PAYMENT_TERM",
        "ANNUAL_SALES_BUCKET",
        "RETAILER_BUSINESS_TYPE",
        "RETAILER_BUCKETED_CHANNEL",
        "RETAILER_STORE_TYPE",
    ]
    orders_joined = orders.merge(
        retailers[retailers_core_cols],
        on="RETAILER_ID",
        how="inner",
        suffixes=("", "_RETAILER"),
    )

    # Month buckets
    orders_joined["order_month"] = orders_joined["ORDER_CREATED"].dt.to_period("M")
    orders_joined["first_month"] = (
        orders_joined["FIRST_CONFIRMED_ORDER_PLACED_AT"].dt.to_period("M")
    )
    orders_joined["month_index"] = (
        orders_joined["order_month"] - orders_joined["first_month"]
    ).astype(int)

    # Keep only months >= 0
    orders_joined = orders_joined[orders_joined["month_index"] >= 0].copy()

    # Monthly GMV per retailer
    gmv = (
        orders_joined.groupby(["RETAILER_ID", "month_index"])["TOTAL_GMV"]
        .sum()
        .reset_index()
    )

    gmv_pivot = (
        gmv.pivot(index="RETAILER_ID", columns="month_index", values="TOTAL_GMV")
        .fillna(0.0)
    )
    gmv_pivot = gmv_pivot.sort_index(axis=1)
    max_month = int(gmv_pivot.columns.max())
    all_months = list(range(0, max_month + 1))
    gmv_pivot = gmv_pivot.reindex(columns=all_months, fill_value=0.0)

    gmv0 = gmv_pivot[0]
    valid_gmv0 = gmv0 > 0
    gmv_pivot_valid = gmv_pivot.loc[valid_gmv0]
    gmv0_valid = gmv0.loc[valid_gmv0]

    # NDR by month: average across retailers of (GMV_t / GMV_0)
    ndr_matrix = gmv_pivot_valid.div(gmv0_valid, axis=0)
    ndr_series = ndr_matrix.mean(axis=0)

    # Activity and retention metrics
    active_matrix = (gmv_pivot > 0).astype(int)
    total_gmv = gmv_pivot.sum(axis=1)
    months_active = active_matrix.sum(axis=1)

    if max_month >= 6:
        has_6_plus = (gmv_pivot.loc[:, 6:] > 0).any(axis=1)
    else:
        has_6_plus = pd.Series(False, index=gmv_pivot.index)

    if max_month >= 12:
        has_12_plus = (gmv_pivot.loc[:, 12:] > 0).any(axis=1)
    else:
        has_12_plus = pd.Series(False, index=gmv_pivot.index)

    early_active_count = (gmv_pivot.loc[:, 0:2] > 0).sum(axis=1)

    # First-month brand diversity
    month0 = orders_joined[orders_joined["month_index"] == 0]
    brand_diversity = (
        month0.groupby("RETAILER_ID")["BRAND_ID"].nunique().astype(float)
    )

    metrics = pd.DataFrame(
        {
            "gmv0": gmv0,
            "total_gmv": total_gmv,
            "months_active": months_active,
            "has_6_plus": has_6_plus,
            "has_12_plus": has_12_plus,
            "early_active_count_0_2": early_active_count,
            "brand_diversity": brand_diversity,
        }
    )

    retailers_metrics = (
        retailers.set_index("RETAILER_ID").join(metrics, how="left")
    )

    # App timing fields (days between first order and first app session)
    retailers_metrics["days_to_app"] = (
        retailers_metrics["FIRST_APP_SESSION_AT"]
        - retailers_metrics["FIRST_CONFIRMED_ORDER_PLACED_AT"]
    ).dt.days

    def bucket_app_timing(days):
        if pd.isna(days):
            return "Never installed"
        if days < 0:
            return "Before first order"
        if days <= 30:
            return "Within 30 days"
        if days <= 180:
            return "30-180 days"
        return "180+ days"

    retailers_metrics["app_timing_bucket"] = retailers_metrics["days_to_app"].apply(
        bucket_app_timing
    )

    # Annual sales segment: unknown vs known
    retailers_metrics["ANNUAL_SALES_SEGMENT"] = np.where(
        retailers_metrics["ANNUAL_SALES_BUCKET"] == "Unknown",
        "Unknown",
        "Known",
    )

    return orders_joined, gmv_pivot, active_matrix, retailers_metrics, ndr_series, max_month


def print_ndr(ndr_series: pd.Series, max_month: int) -> None:
    """Print NDR for months 0–17 (or up to max_month if smaller)."""
    print("\n=== NDR by month ===")
    for t in range(0, min(18, max_month + 1)):
        if t in ndr_series.index:
            val = ndr_series.loc[t]
            print(f"Month {t}: {val * 100:.1f}%")
        else:
            print(f"Month {t}: (no data)")


def analyze_brand_diversity(retailers_metrics: pd.DataFrame):
    """Hypothesis 1: first-month brand diversity predicts retention."""
    df = retailers_metrics.copy()
    df = df[df["gmv0"] > 0].copy()
    df["brand_diversity"] = df["brand_diversity"].fillna(0).astype(int)

    def bucket_brand_diversity(n: int) -> str:
        if n <= 1:
            return "1"
        if n == 2:
            return "2"
        if 3 <= n <= 5:
            return "3-5"
        return "6+"

    df["brand_bucket"] = df["brand_diversity"].apply(bucket_brand_diversity)

    summary = (
        df.groupby("brand_bucket")
        .agg(
            retailers_count=("brand_bucket", "size"),
            retention_6m=("has_6_plus", "mean"),
            retention_12m=("has_12_plus", "mean"),
        )
        .reset_index()
    )
    summary["retention_6m_pct"] = summary["retention_6m"] * 100
    summary["retention_12m_pct"] = summary["retention_12m"] * 100

    print("\n=== Hypothesis 1: Brand diversity vs retention ===")
    print(summary)

    # Bar chart: 6- and 12-month retention by brand diversity bucket
    plot_df = summary.melt(
        id_vars="brand_bucket",
        value_vars=["retention_6m_pct", "retention_12m_pct"],
        var_name="metric",
        value_name="retention_pct",
    )
    metric_map = {
        "retention_6m_pct": "6-month retention",
        "retention_12m_pct": "12-month retention",
    }
    plot_df["metric"] = plot_df["metric"].map(metric_map)

    plt.figure(figsize=(8, 5))
    sns.barplot(
        data=plot_df,
        x="brand_bucket",
        y="retention_pct",
        hue="metric",
    )
    plt.xlabel("Unique brands in month 0")
    plt.ylabel("Retention (%)")
    plt.title("Retention by first-month brand diversity")
    plt.tight_layout()
    plt.savefig(os.path.join(CHARTS_DIR, "h1_brand_diversity.png"))
    plt.close()

    return summary


def analyze_early_repeat(retailers_metrics: pd.DataFrame):
    """Hypothesis 2: early repeat purchasing predicts retention."""
    df = retailers_metrics.copy()
    df = df[df["gmv0"] > 0].copy()

    # Clip at 3+ so we get 0–3 active months in first 3
    df["early_active_group"] = df["early_active_count_0_2"].fillna(0).astype(int)
    df["early_active_group"] = df["early_active_group"].clip(upper=3)

    # Focus on 1-of-3, 2-of-3, 3-of-3 as requested
    df_interest = df[df["early_active_group"].isin([1, 2, 3])].copy()

    summary = (
        df_interest.groupby("early_active_group")
        .agg(
            retailers_count=("early_active_group", "size"),
            retention_12m=("has_12_plus", "mean"),
        )
        .reset_index()
    )
    summary["retention_12m_pct"] = summary["retention_12m"] * 100

    print("\n=== Hypothesis 2: Early repeat purchasing vs 12-month retention ===")
    print(summary)

    plt.figure(figsize=(7, 5))
    sns.barplot(
        data=summary,
        x="early_active_group",
        y="retention_12m_pct",
        color="#4c72b0",
    )
    plt.xlabel("Active months in first 3 (of months 0–2)")
    plt.ylabel("12-month retention (%)")
    plt.title("12-month retention vs early repeat activity")
    plt.tight_layout()
    plt.savefig(os.path.join(CHARTS_DIR, "h2_early_repeat.png"))
    plt.close()

    return summary


def analyze_app_effects(retailers_metrics: pd.DataFrame):
    """App install vs retention and timing buckets."""
    df = retailers_metrics.copy()
    df = df[df["gmv0"] > 0].copy()

    print("\n=== App install flag vs 12-month retention ===")
    app_flag_summary = (
        df.groupby("FLAG_APP_INSTALLED")["has_12_plus"].agg(["mean", "count"])
    )
    app_flag_summary["retention_12m_pct"] = app_flag_summary["mean"] * 100
    print(app_flag_summary)

    # Timing buckets among those who installed
    installed = df[~df["FIRST_APP_SESSION_AT"].isna()].copy()
    timing_pct = (
        installed["app_timing_bucket"].value_counts(normalize=True) * 100
    ).rename("pct_of_installed")

    print(
        "\n=== App install timing among installed (share by timing bucket) ==="
    )
    print(timing_pct)

    # 12-month retention by timing bucket (including Never installed)
    timing_retention = (
        df.groupby("app_timing_bucket")["has_12_plus"]
        .agg(["mean", "count"])
        .reset_index()
    )
    timing_retention["retention_12m_pct"] = timing_retention["mean"] * 100

    print("\n=== 12-month retention by app timing bucket ===")
    print(timing_retention)

    return app_flag_summary, timing_pct, timing_retention


def compute_single_month_churn(
    active_matrix: pd.DataFrame,
    retailer_mask: pd.Series,
    max_month: int,
) -> float:
    """Compute average single-month churn rate for a segment."""
    idx = active_matrix.index[retailer_mask]
    if idx.empty or max_month < 1:
        return float("nan")

    active_sub = active_matrix.loc[idx]
    monthly_rates = []

    for m in range(0, max_month):
        current_active = active_sub[m] == 1
        denom = int(current_active.sum())
        if denom == 0:
            continue
        next_active = active_sub[m + 1] == 1
        churned = current_active & (~next_active)
        rate = churned.sum() / denom
        monthly_rates.append(rate)

    if not monthly_rates:
        return float("nan")
    return float(np.mean(monthly_rates))


def analyze_payment_terms(
    retailers_metrics: pd.DataFrame,
    active_matrix: pd.DataFrame,
    max_month: int,
):
    """Payment term analysis and app x term cross-tab."""
    df = retailers_metrics.copy()
    df = df[df["gmv0"] > 0].copy()

    groups = []
    for term, sub in df.groupby("PAYMENT_TERM"):
        mask = df.index.isin(sub.index)
        churn = compute_single_month_churn(active_matrix, mask, max_month)
        groups.append(
            {
                "PAYMENT_TERM": term,
                "retailers_count": len(sub),
                "avg_lifetime_gmv": sub["total_gmv"].mean(),
                "avg_months_active": sub["months_active"].mean(),
                "single_month_churn": churn,
                "retention_6m": sub["has_6_plus"].mean(),
            }
        )

    summary = pd.DataFrame(groups)
    summary["retention_6m_pct"] = summary["retention_6m"] * 100
    summary["single_month_churn_pct"] = summary["single_month_churn"] * 100

    print("\n=== Payment terms: retention and churn metrics ===")
    print(summary)

    # Cross-tab: 6-month retention by PAYMENT_TERM x FLAG_APP_INSTALLED
    crosstab = (
        df.groupby(["PAYMENT_TERM", "FLAG_APP_INSTALLED"])["has_6_plus"]
        .mean()
        .reset_index()
    )
    crosstab["retention_6m_pct"] = crosstab["has_6_plus"] * 100
    print(
        "\n=== 6-month retention by PAYMENT_TERM x FLAG_APP_INSTALLED ==="
    )
    print(crosstab)

    # Chart: grouped bar for cross-tab
    plt.figure(figsize=(8, 5))
    sns.barplot(
        data=crosstab,
        x="PAYMENT_TERM",
        y="retention_6m_pct",
        hue="FLAG_APP_INSTALLED",
    )
    plt.ylabel("6-month retention (%)")
    plt.title("6-month retention by payment term and app install")
    plt.tight_layout()
    plt.savefig(os.path.join(CHARTS_DIR, "payment_term_app_crosstab.png"))
    plt.close()

    return summary, crosstab


def analyze_unknown_segment(
    retailers_metrics: pd.DataFrame,
    active_matrix: pd.DataFrame,
    max_month: int,
):
    """Deep dive on ANNUAL_SALES_BUCKET = 'Unknown' vs known."""
    df = retailers_metrics.copy()
    df = df[df["gmv0"] > 0].copy()

    segments = []
    for seg, sub in df.groupby("ANNUAL_SALES_SEGMENT"):
        mask = df.index.isin(sub.index)
        churn = compute_single_month_churn(active_matrix, mask, max_month)
        segments.append(
            {
                "ANNUAL_SALES_SEGMENT": seg,
                "retailers_count": len(sub),
                "avg_lifetime_gmv": sub["total_gmv"].mean(),
                "avg_months_active": sub["months_active"].mean(),
                "retention_12m": sub["has_12_plus"].mean(),
                "single_month_churn": churn,
            }
        )

    summary = pd.DataFrame(segments)
    summary["retention_12m_pct"] = summary["retention_12m"] * 100
    summary["single_month_churn_pct"] = summary["single_month_churn"] * 100

    print("\n=== Unknown vs known annual sales segments ===")
    print(summary)

    # Payment term distribution
    payment_dist = (
        df.groupby("ANNUAL_SALES_SEGMENT")["PAYMENT_TERM"]
        .value_counts(normalize=True)
        .rename("share")
        .reset_index()
    )
    payment_dist["share_pct"] = payment_dist["share"] * 100

    print("\n=== Payment term distribution by annual sales segment ===")
    print(payment_dist)

    # Business type distribution
    business_dist = (
        df.groupby("ANNUAL_SALES_SEGMENT")["RETAILER_BUSINESS_TYPE"]
        .value_counts(normalize=True)
        .rename("share")
        .reset_index()
    )
    business_dist["share_pct"] = business_dist["share"] * 100

    print("\n=== Business type distribution by annual sales segment ===")
    print(business_dist)

    return summary, payment_dist, business_dist


def create_summary_visualization(
    ndr_series: pd.Series,
    brand_summary: pd.DataFrame,
    early_summary: pd.DataFrame,
    app_timing_retention: pd.DataFrame,
    payment_term_summary: pd.DataFrame,
    unknown_summary: pd.DataFrame,
):
    """Create 2x3 grid summary of key charts."""
    sns.set(style="whitegrid")

    fig, axes = plt.subplots(2, 3, figsize=(18, 10))

    # 1. NDR over time
    ax = axes[0, 0]
    ndr_plot = ndr_series.copy() * 100
    ax.plot(ndr_plot.index, ndr_plot.values, marker="o")
    ax.set_xlabel("Month since first order")
    ax.set_ylabel("NDR (%)")
    ax.set_title("Net Dollar Retention by month")

    # 2. Brand diversity vs 12m retention
    ax = axes[0, 1]
    brand_plot = brand_summary.copy()
    sns.barplot(
        data=brand_plot,
        x="brand_bucket",
        y="retention_12m_pct",
        ax=ax,
        color="#4c72b0",
    )
    ax.set_xlabel("Unique brands in month 0")
    ax.set_ylabel("12-month retention (%)")
    ax.set_title("Brand diversity vs 12m retention")

    # 3. Early repeat vs 12m retention
    ax = axes[0, 2]
    early_plot = early_summary.copy()
    sns.barplot(
        data=early_plot,
        x="early_active_group",
        y="retention_12m_pct",
        ax=ax,
        color="#55a868",
    )
    ax.set_xlabel("Active months in first 3")
    ax.set_ylabel("12-month retention (%)")
    ax.set_title("Early repeat vs 12m retention")

    # 4. App timing vs 12m retention
    ax = axes[1, 0]
    timing_plot = app_timing_retention.copy()
    sns.barplot(
        data=timing_plot,
        x="app_timing_bucket",
        y="retention_12m_pct",
        ax=ax,
        color="#c44e52",
    )
    ax.set_xlabel("App timing bucket")
    ax.set_ylabel("12-month retention (%)")
    ax.set_title("App timing vs 12m retention")
    ax.tick_params(axis="x", rotation=30)

    # 5. Payment term vs 6m retention
    ax = axes[1, 1]
    pay_plot = payment_term_summary.copy()
    sns.barplot(
        data=pay_plot,
        x="PAYMENT_TERM",
        y="retention_6m_pct",
        ax=ax,
        color="#8172b3",
    )
    ax.set_xlabel("Payment term")
    ax.set_ylabel("6-month retention (%)")
    ax.set_title("Payment term vs 6m retention")

    # 6. Unknown vs known 12m retention
    ax = axes[1, 2]
    unknown_plot = unknown_summary.copy()
    sns.barplot(
        data=unknown_plot,
        x="ANNUAL_SALES_SEGMENT",
        y="retention_12m_pct",
        ax=ax,
        color="#ccb974",
    )
    ax.set_xlabel("Annual sales segment")
    ax.set_ylabel("12-month retention (%)")
    ax.set_title("Unknown vs known 12m retention")

    plt.tight_layout()
    plt.savefig(os.path.join(CHARTS_DIR, "summary.png"))
    plt.close()


def main():
    os.makedirs(CHARTS_DIR, exist_ok=True)
    sns.set(style="whitegrid")

    retailers, orders = load_data()
    initial_exploration(retailers, orders)

    (
        orders_with_month,
        gmv_pivot,
        active_matrix,
        retailers_metrics,
        ndr_series,
        max_month,
    ) = build_cohort_matrices(retailers, orders)

    print_ndr(ndr_series, max_month)

    brand_summary = analyze_brand_diversity(retailers_metrics)
    early_summary = analyze_early_repeat(retailers_metrics)
    app_flag_summary, app_timing_pct, app_timing_retention = analyze_app_effects(
        retailers_metrics
    )
    payment_term_summary, payment_crosstab = analyze_payment_terms(
        retailers_metrics, active_matrix, max_month
    )
    unknown_summary, payment_dist, business_dist = analyze_unknown_segment(
        retailers_metrics, active_matrix, max_month
    )

    create_summary_visualization(
        ndr_series,
        brand_summary,
        early_summary,
        app_timing_retention,
        payment_term_summary,
        unknown_summary,
    )


if __name__ == "__main__":
    main()

