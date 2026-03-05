import glob
import os
import warnings

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
import pandas as pd
import seaborn as sns

warnings.filterwarnings("ignore")

os.makedirs("charts", exist_ok=True)

# ─────────────────────────────────────────────
# SECTION 1 – Load & Explore Data
# ─────────────────────────────────────────────
print("=" * 60)
print("SECTION 1: DATA EXPLORATION")
print("=" * 60)

retailer_files = glob.glob("/workspace/psa_exercise_retailers*.csv")
order_files    = glob.glob("/workspace/psa_exercise_orders*.csv")

retailers = pd.read_csv(retailer_files[0], parse_dates=["FIRST_APP_SESSION_AT",
                                                          "FIRST_DESKTOP_SESSION_AT",
                                                          "FIRST_MOBILE_WEB_SESSION_AT",
                                                          "FIRST_CONFIRMED_ORDER_PLACED_AT"])
orders    = pd.read_csv(order_files[0],    parse_dates=["ORDER_CREATED"])

print(f"\nRetailers — shape: {retailers.shape}")
print(retailers.dtypes)
print("\nNull counts:\n", retailers.isnull().sum())

print(f"\nOrders — shape: {orders.shape}")
print(orders.dtypes)
print("\nNull counts:\n", orders.isnull().sum())

print("\n--- Retailer categorical value counts ---")
cat_cols = retailers.select_dtypes(include="object").columns.tolist()
for col in cat_cols:
    print(f"\n{col}:\n{retailers[col].value_counts()}")

print("\n--- Confirm ORDER_ID is NOT unique per row ---")
dupe = orders[orders.duplicated("ORDER_ID", keep=False)].sort_values("ORDER_ID")
sample_order = dupe["ORDER_ID"].iloc[0]
print(f"Sample ORDER_ID with multiple rows: {sample_order}")
print(dupe[dupe["ORDER_ID"] == sample_order])

# ─────────────────────────────────────────────
# SECTION 2 – NDR Calculation & Validation
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("SECTION 2: NDR CALCULATION")
print("=" * 60)

# Merge to get FIRST_CONFIRMED_ORDER_PLACED_AT on orders
orders_m = orders.merge(
    retailers[["RETAILER_ID", "FIRST_CONFIRMED_ORDER_PLACED_AT"]],
    on="RETAILER_ID", how="left"
)

# Compute month offset (0-based from cohort entry month)
orders_m["MONTH_OFFSET"] = (
    (orders_m["ORDER_CREATED"].dt.year  - orders_m["FIRST_CONFIRMED_ORDER_PLACED_AT"].dt.year) * 12 +
    (orders_m["ORDER_CREATED"].dt.month - orders_m["FIRST_CONFIRMED_ORDER_PLACED_AT"].dt.month)
)

# Monthly GMV per retailer
monthly_gmv = (
    orders_m.groupby(["RETAILER_ID", "MONTH_OFFSET"])["TOTAL_GMV"]
    .sum()
    .reset_index()
    .rename(columns={"TOTAL_GMV": "GMV"})
)

# GMV_0 per retailer
gmv0 = monthly_gmv[monthly_gmv["MONTH_OFFSET"] == 0][["RETAILER_ID", "GMV"]].rename(columns={"GMV": "GMV0"})

# Merge and compute NDR per retailer per month
monthly_gmv = monthly_gmv.merge(gmv0, on="RETAILER_ID", how="left")
monthly_gmv["NDR"] = monthly_gmv["GMV"] / monthly_gmv["GMV0"]

# Aggregate cohort NDR: total cohort GMV_t / total cohort GMV_0
# (This is the standard Net Dollar Retention: cohort's total spend in month t
#  relative to cohort's total spend in month 0; retailers inactive in month t contribute 0)
total_gmv0 = gmv0["GMV0"].sum()
month_range = range(0, 18)

ndr_rows = []
for t in month_range:
    cohort_gmv_t = monthly_gmv[monthly_gmv["MONTH_OFFSET"] == t]["GMV"].sum()
    ndr_rows.append({"MONTH": t, "NDR": cohort_gmv_t / total_gmv0})

ndr_df = pd.DataFrame(ndr_rows)
print("\nNDR by Month (avg across retailers):")
print(ndr_df.to_string(index=False))

ref = {0: 1.00, 1: 0.59, 2: 0.54, 3: 0.56, 5: 0.49, 12: 0.75}
print("\nValidation vs reference:")
for m, v in ref.items():
    calc = ndr_df.loc[ndr_df["MONTH"] == m, "NDR"].values[0]
    print(f"  Month {m:2d}: calc={calc:.2%}  ref={v:.2%}  diff={abs(calc-v):.2%}")

# ─────────────────────────────────────────────
# SECTION 3 – H1: Brand Diversity
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("SECTION 3: H1 – First-Month Brand Diversity")
print("=" * 60)

month0_orders = orders_m[orders_m["MONTH_OFFSET"] == 0]
brand_diversity = (
    month0_orders.groupby("RETAILER_ID")["BRAND_ID"]
    .nunique()
    .reset_index()
    .rename(columns={"BRAND_ID": "UNIQUE_BRANDS_M0"})
)

def bucket_brands(n):
    if n == 1:   return "1"
    if n == 2:   return "2"
    if n <= 5:   return "3-5"
    return "6+"

brand_diversity["BRAND_BUCKET"] = brand_diversity["UNIQUE_BRANDS_M0"].apply(bucket_brands)

# Retention: any order in month 6+ or 12+
has_m6plus  = monthly_gmv[(monthly_gmv["MONTH_OFFSET"] >= 6)  & (monthly_gmv["GMV"] > 0)]["RETAILER_ID"].unique()
has_m12plus = monthly_gmv[(monthly_gmv["MONTH_OFFSET"] >= 12) & (monthly_gmv["GMV"] > 0)]["RETAILER_ID"].unique()

brand_diversity["RET_6MO"]  = brand_diversity["RETAILER_ID"].isin(has_m6plus).astype(int)
brand_diversity["RET_12MO"] = brand_diversity["RETAILER_ID"].isin(has_m12plus).astype(int)

bucket_order = ["1", "2", "3-5", "6+"]
h1_table = (
    brand_diversity.groupby("BRAND_BUCKET")
    .agg(COUNT=("RETAILER_ID", "count"),
         RET_6MO=("RET_6MO", "mean"),
         RET_12MO=("RET_12MO", "mean"))
    .reindex(bucket_order)
    .reset_index()
)
h1_table["RET_6MO"]  = (h1_table["RET_6MO"]  * 100).round(1)
h1_table["RET_12MO"] = (h1_table["RET_12MO"] * 100).round(1)
print(h1_table.to_string(index=False))

fig, ax = plt.subplots(figsize=(8, 5))
x = np.arange(len(bucket_order))
w = 0.35
bars1 = ax.bar(x - w/2, h1_table["RET_6MO"],  w, label="6-Mo Retention %",  color="#4C72B0")
bars2 = ax.bar(x + w/2, h1_table["RET_12MO"], w, label="12-Mo Retention %", color="#DD8452")
ax.set_xticks(x)
ax.set_xticklabels(bucket_order)
ax.set_xlabel("Unique Brands in Month 0")
ax.set_ylabel("Retention Rate (%)")
ax.set_title("H1: Brand Diversity in Month 0 vs Retention")
ax.legend()
ax.set_ylim(0, 100)
for bar in bars1:
    ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1, f"{bar.get_height():.1f}%", ha="center", va="bottom", fontsize=9)
for bar in bars2:
    ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1, f"{bar.get_height():.1f}%", ha="center", va="bottom", fontsize=9)
plt.tight_layout()
plt.savefig("charts/h1_brand_diversity.png", dpi=150)
plt.close()
print("Saved: charts/h1_brand_diversity.png")

# ─────────────────────────────────────────────
# SECTION 4 – H2: Early Repeat Purchasing
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("SECTION 4: H2 – Early Repeat Purchasing")
print("=" * 60)

first3_active = (
    monthly_gmv[(monthly_gmv["MONTH_OFFSET"].isin([0, 1, 2])) & (monthly_gmv["GMV"] > 0)]
    .groupby("RETAILER_ID")["MONTH_OFFSET"]
    .nunique()
    .reset_index()
    .rename(columns={"MONTH_OFFSET": "ACTIVE_MONTHS_FIRST3"})
)
# Retailers only in month 0 data: those not in orders at all for m1/m2 get 1
all_ret_df = retailers[["RETAILER_ID"]].copy()
first3_active = all_ret_df.merge(first3_active, on="RETAILER_ID", how="left")
first3_active["ACTIVE_MONTHS_FIRST3"] = first3_active["ACTIVE_MONTHS_FIRST3"].fillna(0).astype(int)
# Clamp: retailers who never even appear in orders_m get 0; those with m0 only = 1
# Make sure month0 retailers who have no orders at all are counted as 0
# (If a retailer is in the retailers file but has no orders, they have 0 active months)

first3_active["RET_12MO"] = first3_active["RETAILER_ID"].isin(has_m12plus).astype(int)

h2_table = (
    first3_active.groupby("ACTIVE_MONTHS_FIRST3")
    .agg(COUNT=("RETAILER_ID", "count"),
         RET_12MO=("RET_12MO", "mean"))
    .reset_index()
)
h2_table["RET_12MO_PCT"] = (h2_table["RET_12MO"] * 100).round(1)
h2_table.columns = ["Active Months (of first 3)", "Count", "RET_12MO", "12-Mo Retention %"]
print(h2_table[["Active Months (of first 3)", "Count", "12-Mo Retention %"]].to_string(index=False))

fig, ax = plt.subplots(figsize=(7, 5))
bars = ax.bar(
    h2_table["Active Months (of first 3)"].astype(str),
    h2_table["12-Mo Retention %"],
    color=["#4C72B0", "#55A868", "#C44E52", "#8172B2"][:len(h2_table)]
)
ax.set_xlabel("Active Months in First 3 Months")
ax.set_ylabel("12-Mo Retention Rate (%)")
ax.set_title("H2: Early Repeat Purchasing vs 12-Mo Retention")
ax.set_ylim(0, 100)
for bar in bars:
    ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1, f"{bar.get_height():.1f}%", ha="center", va="bottom", fontsize=10)
plt.tight_layout()
plt.savefig("charts/h2_early_repeat.png", dpi=150)
plt.close()
print("Saved: charts/h2_early_repeat.png")

# ─────────────────────────────────────────────
# SECTION 5 – H3: App Install & Causality
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("SECTION 5: H3 – App Install & Reverse Causality")
print("=" * 60)

ret_base = retailers[["RETAILER_ID", "FLAG_APP_INSTALLED",
                        "FIRST_APP_SESSION_AT", "FIRST_CONFIRMED_ORDER_PLACED_AT"]].copy()
ret_base["RET_12MO"] = ret_base["RETAILER_ID"].isin(has_m12plus).astype(int)

# Basic retention by app install flag
app_ret = (
    ret_base.groupby("FLAG_APP_INSTALLED")["RET_12MO"]
    .agg(COUNT="count", RET_12MO_PCT=lambda x: (x.mean() * 100).round(1))
    .reset_index()
)
print("\n12-Mo Retention by App Installed flag:")
print(app_ret.to_string(index=False))

# Days from first order to first app session
ret_base["DAYS_TO_APP"] = (
    ret_base["FIRST_APP_SESSION_AT"] - ret_base["FIRST_CONFIRMED_ORDER_PLACED_AT"]
).dt.days

def app_timing_bucket(row):
    if pd.isnull(row["FIRST_APP_SESSION_AT"]):
        return "Never installed"
    d = row["DAYS_TO_APP"]
    if d < 0:
        return "Before first order"
    if d <= 30:
        return "Within 30 days"
    if d <= 180:
        return "31-180 days"
    return "180+ days"

ret_base["APP_TIMING"] = ret_base.apply(app_timing_bucket, axis=1)

timing_counts = ret_base["APP_TIMING"].value_counts()
timing_pct    = (timing_counts / len(ret_base) * 100).round(1)
print("\nApp install timing distribution:")
print(pd.DataFrame({"Count": timing_counts, "Pct %": timing_pct}))

timing_order = ["Before first order", "Within 30 days", "31-180 days", "180+ days", "Never installed"]
h3_table = (
    ret_base.groupby("APP_TIMING")["RET_12MO"]
    .agg(COUNT="count", RET_12MO=lambda x: (x.mean() * 100).round(1))
    .reindex(timing_order)
    .reset_index()
)
print("\n12-Mo Retention by App Timing:")
print(h3_table.to_string(index=False))

fig, axes = plt.subplots(1, 2, figsize=(13, 5))
# Left: timing distribution pie
present_order = [t for t in timing_order if t in timing_counts.index]
axes[0].pie(
    timing_counts[present_order],
    labels=present_order,
    autopct="%1.1f%%",
    startangle=90,
    colors=sns.color_palette("Set2", len(present_order))
)
axes[0].set_title("App Install Timing Distribution")

# Right: 12-mo retention by timing
h3_plot = h3_table.dropna(subset=["RET_12MO"])
bars = axes[1].bar(
    range(len(h3_plot)),
    h3_plot["RET_12MO"],
    color=sns.color_palette("Set2", len(h3_plot))
)
axes[1].set_xticks(range(len(h3_plot)))
axes[1].set_xticklabels(h3_plot["APP_TIMING"], rotation=20, ha="right", fontsize=9)
axes[1].set_ylabel("12-Mo Retention Rate (%)")
axes[1].set_title("12-Mo Retention by App Install Timing")
axes[1].set_ylim(0, 100)
for bar in bars:
    axes[1].text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
                 f"{bar.get_height():.1f}%", ha="center", va="bottom", fontsize=9)
plt.tight_layout()
plt.savefig("charts/h3_app_install.png", dpi=150)
plt.close()
print("Saved: charts/h3_app_install.png")

# ─────────────────────────────────────────────
# SECTION 6 – Payment Terms Analysis
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("SECTION 6: Payment Terms Analysis")
print("=" * 60)

# Lifetime GMV per retailer
lifetime_gmv = (
    orders_m.groupby("RETAILER_ID")["TOTAL_GMV"].sum()
    .reset_index().rename(columns={"TOTAL_GMV": "LIFETIME_GMV"})
)
# Months active
months_active = (
    monthly_gmv[monthly_gmv["GMV"] > 0]
    .groupby("RETAILER_ID")["MONTH_OFFSET"].nunique()
    .reset_index().rename(columns={"MONTH_OFFSET": "MONTHS_ACTIVE"})
)

ret_pay = retailers[["RETAILER_ID", "PAYMENT_TERM", "FLAG_APP_INSTALLED"]].merge(
    lifetime_gmv, on="RETAILER_ID", how="left"
).merge(
    months_active, on="RETAILER_ID", how="left"
)
ret_pay["MONTHS_ACTIVE"]  = ret_pay["MONTHS_ACTIVE"].fillna(0)
ret_pay["LIFETIME_GMV"]   = ret_pay["LIFETIME_GMV"].fillna(0)
ret_pay["RET_6MO"]        = ret_pay["RETAILER_ID"].isin(has_m6plus).astype(int)
ret_pay["RET_12MO"]       = ret_pay["RETAILER_ID"].isin(has_m12plus).astype(int)

# Single-month churn: only active in month 0, never again
has_post_m0 = monthly_gmv[(monthly_gmv["MONTH_OFFSET"] > 0) & (monthly_gmv["GMV"] > 0)]["RETAILER_ID"].unique()
ret_pay["SINGLE_MONTH_CHURN"] = (~ret_pay["RETAILER_ID"].isin(has_post_m0)).astype(int)

pay_table = (
    ret_pay.groupby("PAYMENT_TERM")
    .agg(COUNT=("RETAILER_ID", "count"),
         AVG_LIFETIME_GMV=("LIFETIME_GMV", "mean"),
         AVG_MONTHS_ACTIVE=("MONTHS_ACTIVE", "mean"),
         SINGLE_MONTH_CHURN_RATE=("SINGLE_MONTH_CHURN", "mean"),
         RET_6MO=("RET_6MO", "mean"))
    .reset_index()
)
pay_table["AVG_LIFETIME_GMV"]        = pay_table["AVG_LIFETIME_GMV"].round(0)
pay_table["AVG_MONTHS_ACTIVE"]       = pay_table["AVG_MONTHS_ACTIVE"].round(2)
pay_table["SINGLE_MONTH_CHURN_RATE"] = (pay_table["SINGLE_MONTH_CHURN_RATE"] * 100).round(1)
pay_table["RET_6MO"]                 = (pay_table["RET_6MO"] * 100).round(1)
print("\nRetention Metrics by Payment Term:")
print(pay_table.to_string(index=False))

# Cross-tab: 6-mo retention by FLAG_APP_INSTALLED x PAYMENT_TERM
cross = ret_pay.pivot_table(
    values="RET_6MO", index="FLAG_APP_INSTALLED",
    columns="PAYMENT_TERM", aggfunc="mean"
) * 100
cross = cross.round(1)
print("\n6-Mo Retention % — App Installed x Payment Term:")
print(cross)

fig, axes = plt.subplots(1, 2, figsize=(13, 5))
# Left: bar chart – 6-mo retention by payment term
pay_terms = pay_table["PAYMENT_TERM"].tolist()
bars = axes[0].bar(pay_terms, pay_table["RET_6MO"], color=["#4C72B0", "#DD8452"])
axes[0].set_ylabel("6-Mo Retention Rate (%)")
axes[0].set_title("6-Mo Retention by Payment Term")
axes[0].set_ylim(0, 100)
for bar in bars:
    axes[0].text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
                 f"{bar.get_height():.1f}%", ha="center", va="bottom", fontsize=11)

# Right: heatmap cross-tab
sns.heatmap(cross, annot=True, fmt=".1f", cmap="YlGnBu", ax=axes[1], vmin=0, vmax=100,
            linewidths=0.5, cbar_kws={"label": "6-Mo Retention %"})
axes[1].set_title("6-Mo Retention %: App Installed x Payment Term")
axes[1].set_xlabel("Payment Term")
axes[1].set_ylabel("App Installed")
plt.tight_layout()
plt.savefig("charts/h4_payment_terms.png", dpi=150)
plt.close()
print("Saved: charts/h4_payment_terms.png")

# ─────────────────────────────────────────────
# SECTION 7 – Unknown Segment Deep Dive
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("SECTION 7: Unknown ANNUAL_SALES_BUCKET Deep Dive")
print("=" * 60)

ret_full = retailers.merge(lifetime_gmv, on="RETAILER_ID", how="left")\
                    .merge(months_active, on="RETAILER_ID", how="left")
ret_full["MONTHS_ACTIVE"]  = ret_full["MONTHS_ACTIVE"].fillna(0)
ret_full["LIFETIME_GMV"]   = ret_full["LIFETIME_GMV"].fillna(0)
ret_full["RET_12MO"]       = ret_full["RETAILER_ID"].isin(has_m12plus).astype(int)
ret_full["RET_6MO"]        = ret_full["RETAILER_ID"].isin(has_m6plus).astype(int)
ret_full["SINGLE_MONTH_CHURN"] = (~ret_full["RETAILER_ID"].isin(has_post_m0)).astype(int)
ret_full["SEGMENT"] = ret_full["ANNUAL_SALES_BUCKET"].apply(
    lambda x: "Unknown" if x == "Unknown" else "Known"
)

seg_table = (
    ret_full.groupby("SEGMENT")
    .agg(COUNT=("RETAILER_ID", "count"),
         RET_12MO=("RET_12MO", "mean"),
         AVG_LIFETIME_GMV=("LIFETIME_GMV", "mean"),
         SINGLE_MONTH_CHURN=("SINGLE_MONTH_CHURN", "mean"),
         RET_6MO=("RET_6MO", "mean"))
    .reset_index()
)
seg_table["RET_12MO"]        = (seg_table["RET_12MO"] * 100).round(1)
seg_table["AVG_LIFETIME_GMV"] = seg_table["AVG_LIFETIME_GMV"].round(0)
seg_table["SINGLE_MONTH_CHURN"] = (seg_table["SINGLE_MONTH_CHURN"] * 100).round(1)
seg_table["RET_6MO"]         = (seg_table["RET_6MO"] * 100).round(1)
print("\nUnknown vs Known Segments – Core Metrics:")
print(seg_table.to_string(index=False))

# Payment term distribution
pay_dist = (
    ret_full.groupby(["SEGMENT", "PAYMENT_TERM"])["RETAILER_ID"]
    .count().unstack(fill_value=0)
)
pay_dist_pct = pay_dist.div(pay_dist.sum(axis=1), axis=0) * 100
print("\nPayment Term Distribution (%):")
print(pay_dist_pct.round(1))

# Business type distribution
biz_dist = (
    ret_full.groupby(["SEGMENT", "RETAILER_BUSINESS_TYPE"])["RETAILER_ID"]
    .count().unstack(fill_value=0)
)
biz_dist_pct = biz_dist.div(biz_dist.sum(axis=1), axis=0) * 100
print("\nBusiness Type Distribution (%):")
print(biz_dist_pct.round(1))

# Within Unknown: 12-mo retention by ANNUAL_SALES_BUCKET breakdown
unk_detail = (
    ret_full[ret_full["ANNUAL_SALES_BUCKET"] != "Unknown"]
    .groupby("ANNUAL_SALES_BUCKET")
    .agg(COUNT=("RETAILER_ID", "count"),
         RET_12MO=("RET_12MO", "mean"))
    .reset_index()
)
unk_detail["RET_12MO"] = (unk_detail["RET_12MO"] * 100).round(1)
print("\nKnown ANNUAL_SALES_BUCKET – 12-Mo Retention:")
print(unk_detail.to_string(index=False))

fig, axes = plt.subplots(1, 2, figsize=(13, 5))
# Left: 12-mo retention comparison
segments = seg_table["SEGMENT"].tolist()
ret_vals = seg_table["RET_12MO"].tolist()
bars = axes[0].bar(segments, ret_vals, color=["#4C72B0", "#C44E52"])
axes[0].set_ylabel("12-Mo Retention Rate (%)")
axes[0].set_title("12-Mo Retention: Known vs Unknown Segment")
axes[0].set_ylim(0, 100)
for bar in bars:
    axes[0].text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
                 f"{bar.get_height():.1f}%", ha="center", va="bottom", fontsize=11)

# Right: avg lifetime GMV
gmv_vals = seg_table["AVG_LIFETIME_GMV"].tolist()
bars2 = axes[1].bar(segments, gmv_vals, color=["#4C72B0", "#C44E52"])
axes[1].set_ylabel("Avg Lifetime GMV ($)")
axes[1].set_title("Avg Lifetime GMV: Known vs Unknown Segment")
for bar in bars2:
    axes[1].text(bar.get_x() + bar.get_width()/2, bar.get_height() + bar.get_height()*0.02,
                 f"${bar.get_height():,.0f}", ha="center", va="bottom", fontsize=11)
plt.tight_layout()
plt.savefig("charts/h5_unknown_segment.png", dpi=150)
plt.close()
print("Saved: charts/h5_unknown_segment.png")

# ─────────────────────────────────────────────
# SECTION 8 – Summary Visualization (2x3 grid)
# ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("SECTION 8: Summary Visualization")
print("=" * 60)

fig = plt.figure(figsize=(18, 11))
gs  = gridspec.GridSpec(2, 3, figure=fig, hspace=0.45, wspace=0.35)

# ── Panel 1: NDR curve (top-left) ──────────────────────────
ax1 = fig.add_subplot(gs[0, 0])
ax1.plot(ndr_df["MONTH"], ndr_df["NDR"] * 100, marker="o", color="#4C72B0", linewidth=2)
ref_months = list(ref.keys())
ref_vals   = [ref[m] * 100 for m in ref_months]
ax1.scatter(ref_months, ref_vals, color="red", zorder=5, label="Reference", s=60)
ax1.set_xlabel("Month (0 = cohort entry)")
ax1.set_ylabel("NDR (%)")
ax1.set_title("NDR Curve (Months 0–17)")
ax1.legend(fontsize=8)
ax1.set_ylim(0, 120)
ax1.axhline(100, color="gray", linestyle="--", linewidth=0.8)

# ── Panel 2: H1 Brand Diversity (top-middle) ───────────────
ax2 = fig.add_subplot(gs[0, 1])
x   = np.arange(len(bucket_order))
w   = 0.35
ax2.bar(x - w/2, h1_table["RET_6MO"],  w, label="6-Mo",  color="#4C72B0")
ax2.bar(x + w/2, h1_table["RET_12MO"], w, label="12-Mo", color="#DD8452")
ax2.set_xticks(x)
ax2.set_xticklabels(bucket_order)
ax2.set_xlabel("Brands in Month 0")
ax2.set_ylabel("Retention (%)")
ax2.set_title("H1: Brand Diversity → Retention")
ax2.legend(fontsize=8)
ax2.set_ylim(0, 100)

# ── Panel 3: H2 Early Repeat (top-right) ───────────────────
ax3 = fig.add_subplot(gs[0, 2])
h2_plot = h2_table[h2_table["Active Months (of first 3)"] > 0].copy()
ax3.bar(h2_plot["Active Months (of first 3)"].astype(str),
        h2_plot["12-Mo Retention %"],
        color=["#55A868", "#4C72B0", "#C44E52"])
ax3.set_xlabel("Active Months in First 3")
ax3.set_ylabel("12-Mo Retention (%)")
ax3.set_title("H2: Early Repeat → 12-Mo Retention")
ax3.set_ylim(0, 100)

# ── Panel 4: H3 App Install Timing (bottom-left) ───────────
ax4 = fig.add_subplot(gs[1, 0])
h3_clean = h3_table.dropna(subset=["RET_12MO"])
colors4  = sns.color_palette("Set2", len(h3_clean))
ax4.bar(range(len(h3_clean)), h3_clean["RET_12MO"], color=colors4)
ax4.set_xticks(range(len(h3_clean)))
ax4.set_xticklabels(h3_clean["APP_TIMING"], rotation=15, ha="right", fontsize=7)
ax4.set_ylabel("12-Mo Retention (%)")
ax4.set_title("H3: App Install Timing → Retention")
ax4.set_ylim(0, 100)

# ── Panel 5: Payment Term (bottom-middle) ──────────────────
ax5 = fig.add_subplot(gs[1, 1])
sns.heatmap(cross, annot=True, fmt=".1f", cmap="YlGnBu", ax=ax5,
            vmin=0, vmax=100, linewidths=0.5,
            cbar_kws={"label": "6-Mo Ret %", "shrink": 0.8})
ax5.set_title("H4: App x Payment Term → 6-Mo Ret %")
ax5.set_xlabel("Payment Term")
ax5.set_ylabel("App Installed")

# ── Panel 6: Unknown Segment (bottom-right) ────────────────
ax6 = fig.add_subplot(gs[1, 2])
metrics = ["RET_6MO", "RET_12MO", "SINGLE_MONTH_CHURN"]
metric_labels = ["6-Mo Ret%", "12-Mo Ret%", "Churn %"]
x6 = np.arange(len(metrics))
w6 = 0.35
known_vals   = seg_table[seg_table["SEGMENT"] == "Known"][metrics].values[0]
unknown_vals = seg_table[seg_table["SEGMENT"] == "Unknown"][metrics].values[0]
ax6.bar(x6 - w6/2, known_vals,   w6, label="Known",   color="#4C72B0")
ax6.bar(x6 + w6/2, unknown_vals, w6, label="Unknown", color="#C44E52")
ax6.set_xticks(x6)
ax6.set_xticklabels(metric_labels)
ax6.set_ylabel("Rate (%)")
ax6.set_title("H5: Unknown vs Known Segment")
ax6.legend(fontsize=8)
ax6.set_ylim(0, 100)

fig.suptitle("Faire Retailer Retention Analysis — Summary Dashboard", fontsize=14, fontweight="bold", y=1.01)
plt.savefig("charts/summary.png", dpi=150, bbox_inches="tight")
plt.close()
print("Saved: charts/summary.png")

print("\n" + "=" * 60)
print("ALL SECTIONS COMPLETE")
print("=" * 60)
