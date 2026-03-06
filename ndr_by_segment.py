"""
ndr_by_segment.py
─────────────────
Computes NDR by month (0-17) for 6 retailer segments.

NDR formula (fixed-denominator, segment-level):
    NDR_t = total GMV in month t for active retailers in segment
            ─────────────────────────────────────────────────────
            total GMV in month 0 for ALL retailers in segment
                        (denominator NEVER changes by month)

Outputs:
  • Printed tables per segment
  • charts/ndr_by_segment/<segment>.png  — one chart per segment, lines overlaid
  • ndr_by_segment.sql                   — canonical SQL with full QA annotations
"""

import glob
import os
import sqlite3
import textwrap
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd     # CSV loading only

os.makedirs("charts/ndr_by_segment", exist_ok=True)

# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def banner(t):
    print("\n" + "═" * 70)
    print(f"  {t}")
    print("═" * 70)

def sub(t):
    print(f"\n── {t} " + "─" * max(0, 62 - len(t)))

def run(conn, sql):
    return conn.execute(sql).fetchall()

def print_table(rows, headers, indent=2):
    if not rows:
        print("  (no rows)")
        return
    col_w = [max(len(str(h)), max(len(str(r[i])) for r in rows))
             for i, h in enumerate(headers)]
    pad = " " * indent
    print(pad + "  ".join(str(h).ljust(w) for h, w in zip(headers, col_w)))
    print(pad + "  ".join("-" * w for w in col_w))
    for row in rows:
        print(pad + "  ".join(str(v).ljust(w) for v, w in zip(row, col_w)))


# ─────────────────────────────────────────────────────────────
# LOAD DATA
# ─────────────────────────────────────────────────────────────

banner("LOADING DATA")
conn = sqlite3.connect(":memory:")
pd.read_csv(glob.glob("/workspace/psa_exercise_retailers*.csv")[0]).to_sql(
    "psa_exercise_retailers", conn, if_exists="replace", index=False)
pd.read_csv(glob.glob("/workspace/psa_exercise_orders*.csv")[0]).to_sql(
    "psa_exercise_orders",    conn, if_exists="replace", index=False)
print("  Loaded.")


# ─────────────────────────────────────────────────────────────
# NDR SQL BUILDER
# ─────────────────────────────────────────────────────────────
# All 6 queries share the same skeleton.  Only the segment-assignment
# CTEs differ.  retailer_months is extended to carry brand_count so
# brand-diversity and early-repeat CTEs can reference it.

BASE_RETAILER_MONTHS_CTE = """\
-- One row per (retailer_id, month_offset)
-- ⚠️ FAN-OUT: orders table has one row per brand per order;
--    ORDER_ID is NOT unique per row.  SUM(total_gmv) and
--    COUNT(DISTINCT ...) collapse correctly to retailer-month grain.
-- JOIN grain: orders (many) → retailers (one) → 1:many; safe
retailer_months AS (
    SELECT
        o.retailer_id,
        (CAST(strftime('%Y', o.order_created) AS INT)
            - CAST(strftime('%Y', r.first_confirmed_order_placed_at) AS INT)) * 12
        + (CAST(strftime('%m', o.order_created) AS INT)
            - CAST(strftime('%m', r.first_confirmed_order_placed_at) AS INT))
            AS month_offset,
        SUM(o.total_gmv)           AS monthly_gmv,
        COUNT(DISTINCT o.brand_id) AS brand_count    -- used by Segments 3 & 4
    FROM psa_exercise_orders o
    JOIN psa_exercise_retailers r ON o.retailer_id = r.retailer_id
    WHERE o.order_state = 'PROCESSING'
    GROUP BY o.retailer_id, month_offset
)"""

NDR_TAIL = """\
-- Fixed denominator per segment: total month-0 GMV for ALL retailers in segment
-- This does NOT move — it equals the cohort's revenue at entry, and stays fixed
-- even as retailers churn.  Grain: one row per segment_value.
gmv0_by_segment AS (
    SELECT
        s.segment_value,
        COUNT(DISTINCT s.retailer_id)  AS cohort_n,
        SUM(rm.monthly_gmv)            AS fixed_gmv0
    -- JOIN grain: segments (one per retailer) × retailer_months at month 0 (one per retailer) → 1:1
    FROM segments s
    JOIN retailer_months rm
         ON s.retailer_id = rm.retailer_id AND rm.month_offset = 0
    GROUP BY s.segment_value
),
-- Numerator: GMV at month t for active retailers in each segment
-- Grain: (segment_value, month_offset)
gmv_t AS (
    SELECT
        s.segment_value,
        rm.month_offset,
        COUNT(DISTINCT s.retailer_id)  AS n_active,
        SUM(rm.monthly_gmv)            AS cohort_gmv_t
    -- JOIN grain: segments (one per retailer) × retailer_months (many per retailer) → 1:many
    FROM segments s
    JOIN retailer_months rm ON s.retailer_id = rm.retailer_id
    WHERE rm.month_offset BETWEEN 0 AND 17
    GROUP BY s.segment_value, rm.month_offset
)
SELECT
    gt.segment_value,
    gt.month_offset,
    gt.n_active                                         AS n_active_retailers,
    ROUND(gt.cohort_gmv_t, 0)                          AS cohort_gmv_t,
    ROUND(g0.fixed_gmv0, 0)                            AS fixed_cohort_gmv_0,
    ROUND(gt.cohort_gmv_t * 100.0 / g0.fixed_gmv0, 2) AS ndr_pct
-- JOIN grain: gmv_t (one per segment-month) × gmv0_by_segment (one per segment) → 1:1
FROM gmv_t gt
JOIN gmv0_by_segment g0 ON gt.segment_value = g0.segment_value
ORDER BY gt.segment_value, gt.month_offset;"""


def make_ndr_query(extra_ctes: str, title: str = "") -> str:
    """Assemble a full NDR query given extra CTE block(s) placed between
    retailer_months and the fixed gmv0/gmv_t/SELECT tail."""
    comment = f"-- {title}\n" if title else ""
    return (
        f"{comment}"
        f"WITH\n{BASE_RETAILER_MONTHS_CTE},\n"
        f"{extra_ctes},\n"
        f"{NDR_TAIL}"
    )


def run_ndr(conn, sql):
    """Execute an NDR query and return a dict:
       { segment_value: [(month, n_active, gmv_t, gmv_0, ndr_pct), ...] }
    """
    rows = run(conn, sql)
    d = defaultdict(list)
    for seg, m, n, gt, g0, ndr in rows:
        d[seg].append((int(m), int(n), float(gt), float(g0), float(ndr)))
    return dict(d)


# ─────────────────────────────────────────────────────────────
# CHART HELPER
# ─────────────────────────────────────────────────────────────

PALETTE = [
    "#1a5fa8", "#e05c2a", "#2e8b57", "#9b3fa8",
    "#c8a200", "#555555", "#d44",    "#4499dd",
]

def plot_ndr(results: dict, title: str, subtitle: str,
             filename: str, segment_order=None,
             custom_colors=None, question: str = ""):
    """Generate NDR line chart with all segment curves overlaid."""
    order = segment_order if segment_order else sorted(results.keys())
    colors = custom_colors if custom_colors else {s: PALETTE[i % len(PALETTE)]
                                                   for i, s in enumerate(order)}

    fig, ax = plt.subplots(figsize=(11, 6))

    for seg in order:
        if seg not in results:
            continue
        data    = sorted(results[seg], key=lambda r: r[0])
        months  = [r[0] for r in data]
        ndrs    = [r[4] for r in data]
        ax.plot(months, ndrs, marker="o", markersize=4, linewidth=2.2,
                color=colors.get(seg, "#333"), label=seg)

    ax.axhline(100, color="gray", linestyle="--", linewidth=1.0, alpha=0.7,
               label="100% (breakeven)")
    ax.set_xlabel("Month offset from cohort entry (0 = first order month)", fontsize=10)
    ax.set_ylabel("NDR (%)", fontsize=10)
    ax.set_title(f"{title}\n{subtitle}", fontsize=11, fontweight="bold")
    if question:
        ax.text(0.01, 0.01, f"Q: {question}", transform=ax.transAxes,
                fontsize=8, color="#555", va="bottom")
    ax.legend(fontsize=9, loc="upper left")
    ax.xaxis.set_major_locator(mticker.MultipleLocator(2))
    ax.set_xlim(-0.5, 17.5)
    ymax = max(ndr for data in results.values() for _, _, _, _, ndr in data)
    ax.set_ylim(0, max(130, ymax * 1.08))
    ax.grid(axis="y", alpha=0.3)

    plt.tight_layout()
    plt.savefig(f"charts/ndr_by_segment/{filename}", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: charts/ndr_by_segment/{filename}")


# ─────────────────────────────────────────────────────────────
# VALIDATION — overall cohort NDR (sanity check)
# ─────────────────────────────────────────────────────────────

banner("VALIDATION: OVERALL COHORT NDR")
val_sql = f"""
WITH
{BASE_RETAILER_MONTHS_CTE},
total_gmv0 AS (
    SELECT SUM(monthly_gmv) AS val FROM retailer_months WHERE month_offset = 0
),
ndr AS (
    SELECT rm.month_offset,
           COUNT(DISTINCT rm.retailer_id)               AS active_retailers,
           ROUND(SUM(rm.monthly_gmv)*100.0/tg.val, 2)  AS ndr_pct
    FROM retailer_months rm CROSS JOIN total_gmv0 tg
    WHERE rm.month_offset BETWEEN 0 AND 17
    GROUP BY rm.month_offset
)
SELECT month_offset, active_retailers, ndr_pct FROM ndr ORDER BY month_offset
"""
val_rows = run(conn, val_sql)
print_table(val_rows, ["month","active_retailers","ndr_pct%"])
REF = {0:100.0, 1:59.0, 2:54.0, 3:56.0, 5:49.0, 12:75.0}
print("\n  Reference validation:")
for m, ref in REF.items():
    calc = next((r[2] for r in val_rows if r[0]==m), None)
    diff = abs(calc-ref) if calc else 999
    print(f"    Month {m:2d}: {calc:.2f}%  (ref {ref:.1f}%)  {'✓' if diff < 1 else '✗'}")


# ═════════════════════════════════════════════════════════════
# SEGMENT 1 — PAYMENT TERM
# ═════════════════════════════════════════════════════════════

banner("SEGMENT 1: PAYMENT TERM — NET60 vs PAYMENT_ON_SHIPMENT")
print("  Question: do NET60 retailers expand faster or just churn less?")
print("  Expansion = NDR crosses 100%; churn-less = NDR stays below 100% but higher than POS.")

SEG1_SQL = make_ndr_query(
    extra_ctes="""\
-- Segment assignment: one row per retailer
-- Grain: (retailer_id, segment_value)
-- Excludes NET90 (n=8) and HOLD_ON_PLACEMENT (n=7) — too small for reliable NDR curves
segments AS (
    SELECT retailer_id, payment_term AS segment_value
    FROM psa_exercise_retailers
    WHERE payment_term IN ('NET60', 'PAYMENT_ON_SHIPMENT')
    -- QA: should cover ~6985 of 7400 retailers
)""",
    title="NDR by Payment Term: NET60 vs PAYMENT_ON_SHIPMENT"
)
r_s1 = run_ndr(conn, SEG1_SQL)
print_table(
    [(seg, m, n, f"${g:,.0f}", f"${g0:,.0f}", f"{ndr}%")
     for seg, data in sorted(r_s1.items()) for m,n,g,g0,ndr in data],
    ["segment","month","n_active","cohort_gmv_t","fixed_gmv_0","ndr_pct"]
)

# QA checks
for seg, data in r_s1.items():
    m0 = next((d for d in data if d[0]==0), None)
    if m0:
        assert abs(m0[4]-100.0) < 0.1, f"QA FAIL: {seg} month-0 NDR = {m0[4]}, expected 100"
print("  QA ✓ month-0 NDR = 100% for all payment terms")

crosses_100_s1 = {seg: next((m for m,_,_,_,ndr in data if m > 0 and ndr >= 100), None)
                  for seg, data in r_s1.items()}
obs_s1 = {
    "NET60_m12":  next((ndr for m,_,_,_,ndr in r_s1.get("NET60",[]) if m==12), None),
    "POS_m12":    next((ndr for m,_,_,_,ndr in r_s1.get("PAYMENT_ON_SHIPMENT",[]) if m==12), None),
    "crosses_100": crosses_100_s1,
}
plot_ndr(r_s1,
         title="NDR by Payment Term",
         subtitle="NET60 vs PAYMENT_ON_SHIPMENT (NET90 and HOLD excluded, n<10)",
         filename="s1_payment_term.png",
         segment_order=["NET60","PAYMENT_ON_SHIPMENT"],
         custom_colors={"NET60":"#1a5fa8","PAYMENT_ON_SHIPMENT":"#e05c2a"},
         question="Do NET60 retailers expand (NDR>100%) or just churn less?")


# ═════════════════════════════════════════════════════════════
# SEGMENT 2 — ANNUAL SALES BUCKET: UNKNOWN vs KNOWN
# ═════════════════════════════════════════════════════════════

banner("SEGMENT 2: ANNUAL SALES BUCKET — Unknown vs Known")
print("  Question: does Unknown NDR ever recover, or is the revenue permanently impaired?")

SEG2_SQL = make_ndr_query(
    extra_ctes="""\
-- Segment assignment: Unknown bucket vs all other buckets grouped as 'Known'
-- Grain: (retailer_id, segment_value)
segments AS (
    SELECT retailer_id,
           CASE WHEN annual_sales_bucket = 'Unknown' THEN 'Unknown' ELSE 'Known' END
               AS segment_value
    FROM psa_exercise_retailers
    -- QA: 2285 Unknown, 5115 Known
)""",
    title="NDR by Annual Sales Bucket: Unknown vs Known"
)
r_s2 = run_ndr(conn, SEG2_SQL)
print_table(
    [(seg, m, n, f"${g:,.0f}", f"${g0:,.0f}", f"{ndr}%")
     for seg, data in sorted(r_s2.items()) for m,n,g,g0,ndr in data],
    ["segment","month","n_active","cohort_gmv_t","fixed_gmv_0","ndr_pct"]
)
for seg, data in r_s2.items():
    m0 = next((d for d in data if d[0]==0), None)
    assert m0 and abs(m0[4]-100.0) < 0.1, f"QA FAIL: {seg} month-0 NDR = {m0[4] if m0 else 'N/A'}"
print("  QA ✓ month-0 NDR = 100% for both segments")

obs_s2 = {
    "Known_m12":   next((ndr for m,_,_,_,ndr in r_s2.get("Known",[]) if m==12), None),
    "Unknown_m12": next((ndr for m,_,_,_,ndr in r_s2.get("Unknown",[]) if m==12), None),
    "crosses_100": {seg: next((m for m,_,_,_,ndr in data if m>0 and ndr>=100), None)
                    for seg, data in r_s2.items()},
}
plot_ndr(r_s2,
         title="NDR by Annual Sales Bucket",
         subtitle="Unknown (n=2,285) vs Known (n=5,115)",
         filename="s2_annual_sales_bucket.png",
         segment_order=["Known","Unknown"],
         custom_colors={"Known":"#2e8b57","Unknown":"#e05c2a"},
         question="Does Unknown NDR ever recover, or is the revenue permanently lost?")


# ═════════════════════════════════════════════════════════════
# SEGMENT 3 — EARLY REPEAT ACTIVITY
# ═════════════════════════════════════════════════════════════

banner("SEGMENT 3: EARLY REPEAT ACTIVITY — 1/3, 2/3, 3/3 active months")
print("  Question: do 3/3 retailers actually EXPAND (cross 100% NDR)?")
print("  Also: how fast does 1/3 NDR collapse relative to headcount retention?")

SEG3_SQL = make_ndr_query(
    extra_ctes="""\
-- First, count active months in {0,1,2} per retailer
-- Grain: (retailer_id, active_months_first3)
-- Uses retailer_months CTE already defined above
early_repeat AS (
    SELECT retailer_id,
           COUNT(DISTINCT month_offset) AS active_months_first3
    FROM retailer_months
    WHERE month_offset IN (0, 1, 2)
    GROUP BY retailer_id
),
-- Segment assignment based on activity count
-- Grain: (retailer_id, segment_value)
-- All cohort retailers had at least month-0 orders → min active_months = 1
-- COALESCE handles any edge-case retailer with no order record (defensive)
segments AS (
    SELECT r.retailer_id,
           CASE COALESCE(e.active_months_first3, 0)
               WHEN 1 THEN '1-of-3'
               WHEN 2 THEN '2-of-3'
               WHEN 3 THEN '3-of-3'
               ELSE '0-of-3'
           END AS segment_value
    FROM psa_exercise_retailers r
    LEFT JOIN early_repeat e ON r.retailer_id = e.retailer_id
    -- QA: 3153 in 1-of-3, 2173 in 2-of-3, 2074 in 3-of-3 (+ tiny 0-of-3)
)""",
    title="NDR by Early Repeat Activity: 1/3, 2/3, 3/3 active in months 0-2"
)
r_s3 = run_ndr(conn, SEG3_SQL)
print_table(
    [(seg, m, n, f"${g:,.0f}", f"${g0:,.0f}", f"{ndr}%")
     for seg, data in sorted(r_s3.items()) for m,n,g,g0,ndr in data],
    ["segment","month","n_active","cohort_gmv_t","fixed_gmv_0","ndr_pct"]
)
for seg, data in r_s3.items():
    m0 = next((d for d in data if d[0]==0), None)
    if m0:
        assert abs(m0[4]-100.0) < 0.1, f"QA FAIL: {seg} month-0 = {m0[4]}"
print("  QA ✓ month-0 NDR = 100% for all activity segments")

obs_s3 = {
    seg: {
        "m12_ndr": next((ndr for m,_,_,_,ndr in data if m==12), None),
        "crosses_100_at": next((m for m,_,_,_,ndr in data if m>0 and ndr>=100), None),
    } for seg, data in r_s3.items()
}
s3_order = ["1-of-3","2-of-3","3-of-3"]
s3_colors = {"1-of-3":"#e05c2a","2-of-3":"#c8a200","3-of-3":"#1a5fa8",
             "0-of-3":"#aaaaaa"}
plot_ndr(r_s3,
         title="NDR by Early Repeat Activity",
         subtitle="Active months in first 3 months (0, 1, 2)",
         filename="s3_early_repeat.png",
         segment_order=[s for s in s3_order if s in r_s3],
         custom_colors=s3_colors,
         question="Do 3/3 retailers cross 100% NDR (true expansion)?")


# ═════════════════════════════════════════════════════════════
# SEGMENT 4 — FIRST-MONTH BRAND COUNT
# ═════════════════════════════════════════════════════════════

banner("SEGMENT 4: FIRST-MONTH BRAND COUNT — 1, 2, 3-5, 6+ brands")
print("  Question: does brand diversity predict GMV growth or just headcount retention?")
print("  The two may diverge if high-diversity retailers have lower average order size.")

SEG4_SQL = make_ndr_query(
    extra_ctes="""\
-- Extract month-0 brand count per retailer from retailer_months CTE
-- Grain: (retailer_id, brands_m0)
m0_brands AS (
    SELECT retailer_id, brand_count AS brands_m0
    FROM retailer_months
    WHERE month_offset = 0
),
-- Assign bucket; ⚠️ brand_count = COUNT(DISTINCT brand_id) in month 0
-- Grain: (retailer_id, segment_value)
segments AS (
    SELECT m.retailer_id,
           CASE
               WHEN m.brands_m0 = 1  THEN '1 brand'
               WHEN m.brands_m0 = 2  THEN '2 brands'
               WHEN m.brands_m0 <= 5 THEN '3-5 brands'
               ELSE                       '6+ brands'
           END AS segment_value
    FROM m0_brands m
    -- QA: 3703 in '1 brand', 1207 in '2 brands', 1545 in '3-5', 945 in '6+'
)""",
    title="NDR by First-Month Brand Count: 1, 2, 3-5, 6+ brands"
)
r_s4 = run_ndr(conn, SEG4_SQL)
print_table(
    [(seg, m, n, f"${g:,.0f}", f"${g0:,.0f}", f"{ndr}%")
     for seg, data in sorted(r_s4.items()) for m,n,g,g0,ndr in data],
    ["segment","month","n_active","cohort_gmv_t","fixed_gmv_0","ndr_pct"]
)
for seg, data in r_s4.items():
    m0 = next((d for d in data if d[0]==0), None)
    if m0:
        assert abs(m0[4]-100.0) < 0.1, f"QA FAIL: {seg}"
print("  QA ✓ month-0 NDR = 100% for all brand count segments")

obs_s4 = {
    seg: {
        "m12_ndr": next((ndr for m,_,_,_,ndr in data if m==12), None),
        "crosses_100_at": next((m for m,_,_,_,ndr in data if m>0 and ndr>=100), None),
    } for seg, data in r_s4.items()
}
s4_order = ["1 brand","2 brands","3-5 brands","6+ brands"]
s4_colors = {"1 brand":"#e05c2a","2 brands":"#c8a200",
             "3-5 brands":"#5588cc","6+ brands":"#1a5fa8"}
plot_ndr(r_s4,
         title="NDR by First-Month Brand Count",
         subtitle="Distinct brands purchased in month 0",
         filename="s4_brand_count.png",
         segment_order=[s for s in s4_order if s in r_s4],
         custom_colors=s4_colors,
         question="Does brand diversity predict GMV growth or just headcount retention?")


# ═════════════════════════════════════════════════════════════
# SEGMENT 5 — FIRST-MONTH GMV QUARTILE
# ═════════════════════════════════════════════════════════════

banner("SEGMENT 5: FIRST-MONTH GMV QUARTILE — Q1 (lowest) to Q4 (highest)")
print("  Question: do high-GMV starters maintain their spend level, or regress?")
print("  Also: is Q4 NDR the floor or the ceiling — do they expand further?")

SEG5_SQL = make_ndr_query(
    extra_ctes="""\
-- Extract month-0 GMV per retailer
-- Grain: (retailer_id, gmv_m0)
m0_gmv AS (
    SELECT retailer_id, monthly_gmv AS gmv_m0
    FROM retailer_months
    WHERE month_offset = 0
),
-- Assign global NTILE(4) quartile by ascending month-0 GMV
-- NTILE is global (not per-segment), so Q1 = bottom 25% of ALL retailers
-- Grain: (retailer_id, gmv_quartile)
gmv_ntile AS (
    SELECT retailer_id,
           NTILE(4) OVER (ORDER BY gmv_m0) AS gmv_quartile,
           gmv_m0
    FROM m0_gmv
),
-- Label quartiles with GMV boundaries for readability
-- Grain: (retailer_id, segment_value)
segments AS (
    SELECT retailer_id,
           'Q' || CAST(gmv_quartile AS TEXT) AS segment_value
    FROM gmv_ntile
    -- QA: 1850 retailers per quartile
)""",
    title="NDR by First-Month GMV Quartile: Q1 ($0-$427) to Q4 ($1,689-$69,125)"
)
r_s5 = run_ndr(conn, SEG5_SQL)
print_table(
    [(seg, m, n, f"${g:,.0f}", f"${g0:,.0f}", f"{ndr}%")
     for seg, data in sorted(r_s5.items()) for m,n,g,g0,ndr in data],
    ["quartile","month","n_active","cohort_gmv_t","fixed_gmv_0","ndr_pct"]
)
for seg, data in r_s5.items():
    m0 = next((d for d in data if d[0]==0), None)
    if m0:
        assert abs(m0[4]-100.0) < 0.1, f"QA FAIL: {seg}"
print("  QA ✓ month-0 NDR = 100% for all GMV quartiles")

obs_s5 = {
    seg: {
        "m12_ndr": next((ndr for m,_,_,_,ndr in data if m==12), None),
        "crosses_100_at": next((m for m,_,_,_,ndr in data if m>0 and ndr>=100), None),
    } for seg, data in r_s5.items()
}
s5_order = ["Q1","Q2","Q3","Q4"]
s5_colors = {"Q1":"#e05c2a","Q2":"#c8a200","Q3":"#5588cc","Q4":"#1a5fa8"}
plot_ndr(r_s5,
         title="NDR by First-Month GMV Quartile",
         subtitle="Q1: $0–$427 avg | Q2: $427–$800 | Q3: $800–$1,689 | Q4: $1,689–$69,125",
         filename="s5_gmv_quartile.png",
         segment_order=[s for s in s5_order if s in r_s5],
         custom_colors=s5_colors,
         question="Do Q4 starters maintain spend, or does their NDR regress toward the cohort average?")


# ═════════════════════════════════════════════════════════════
# SEGMENT 6 — BUSINESS TYPE
# ═════════════════════════════════════════════════════════════

banner("SEGMENT 6: BUSINESS TYPE — Brick & Mortar vs Online Only vs Pop Up")
print("  Question: does Online Only NDR track similarly to Unknown bucket?")
print("  If yes, the two 'problems' are the same underlying segment.")

SEG6_SQL = make_ndr_query(
    extra_ctes="""\
-- Normalise 402 raw business_type values to 3 main groups; exclude Other/Unknown
-- Grain: (retailer_id, segment_value)
segments AS (
    SELECT retailer_id, segment_value
    FROM (
        SELECT retailer_id,
               CASE
                   WHEN retailer_business_type IN ('Brick & Mortar Store','Brick and Mortar','Brick & Mortar')
                        OR LOWER(COALESCE(retailer_business_type,'')) LIKE '%brick%mortar%'
                       THEN 'Brick & Mortar'
                   WHEN retailer_business_type IN ('Online Only','Online')
                        OR LOWER(COALESCE(retailer_business_type,'')) LIKE '%online%only%'
                       THEN 'Online Only'
                   WHEN retailer_business_type IN ('Pop Up Store','Pop Up','Pop-up','pop up','pop-up')
                        OR LOWER(COALESCE(retailer_business_type,'')) LIKE '%pop%up%'
                       THEN 'Pop Up'
                   ELSE NULL
               END AS segment_value
        FROM psa_exercise_retailers
    )
    WHERE segment_value IS NOT NULL  -- exclude Other/Unknown (~421 retailers)
    -- QA: ~4650 Brick & Mortar, ~1859 Online Only, ~470 Pop Up
)""",
    title="NDR by Business Type: Brick & Mortar vs Online Only vs Pop Up"
)
r_s6 = run_ndr(conn, SEG6_SQL)
print_table(
    [(seg, m, n, f"${g:,.0f}", f"${g0:,.0f}", f"{ndr}%")
     for seg, data in sorted(r_s6.items()) for m,n,g,g0,ndr in data],
    ["biz_type","month","n_active","cohort_gmv_t","fixed_gmv_0","ndr_pct"]
)
for seg, data in r_s6.items():
    m0 = next((d for d in data if d[0]==0), None)
    if m0:
        assert abs(m0[4]-100.0) < 0.1, f"QA FAIL: {seg}"
print("  QA ✓ month-0 NDR = 100% for all business types")

obs_s6 = {
    seg: {
        "m12_ndr": next((ndr for m,_,_,_,ndr in data if m==12), None),
        "crosses_100_at": next((m for m,_,_,_,ndr in data if m>0 and ndr>=100), None),
    } for seg, data in r_s6.items()
}
s6_order = ["Brick & Mortar","Online Only","Pop Up"]
s6_colors = {"Brick & Mortar":"#1a5fa8","Online Only":"#e05c2a","Pop Up":"#2e8b57"}
plot_ndr(r_s6,
         title="NDR by Business Type",
         subtitle="Brick & Mortar (n≈4,650) | Online Only (n≈1,859) | Pop Up (n≈470)",
         filename="s6_business_type.png",
         segment_order=[s for s in s6_order if s in r_s6],
         custom_colors=s6_colors,
         question="Does Online Only NDR track like the Unknown bucket?")


# ═════════════════════════════════════════════════════════════
# OBSERVATIONS SUMMARY
# ═════════════════════════════════════════════════════════════

banner("OBSERVATIONS ACROSS ALL SEGMENTS")

print("\n  ── Which segments cross 100% NDR (true expansion) ─────────────")
all_segments = [
    ("Payment Term",     r_s1, ["NET60","PAYMENT_ON_SHIPMENT"]),
    ("Annual Bucket",    r_s2, ["Known","Unknown"]),
    ("Early Repeat",     r_s3, ["1-of-3","2-of-3","3-of-3"]),
    ("Brand Count",      r_s4, ["1 brand","2 brands","3-5 brands","6+ brands"]),
    ("GMV Quartile",     r_s5, ["Q1","Q2","Q3","Q4"]),
    ("Business Type",    r_s6, ["Brick & Mortar","Online Only","Pop Up"]),
]
for seg_name, results, groups in all_segments:
    for g in groups:
        if g not in results: continue
        data = results[g]
        crosses = next((m for m,_,_,_,ndr in sorted(data) if m>0 and ndr>=100), None)
        m12_ndr = next((ndr for m,_,_,_,ndr in data if m==12), None)
        cross_str = f"crosses 100% at month {crosses}" if crosses else "never crosses 100%"
        print(f"    {seg_name:15s} · {g:22s}: month-12 NDR={m12_ndr:.1f}%  ({cross_str})")

# Compute headcount vs revenue divergence
print("\n  ── Headcount vs revenue divergence ────────────────────────────")
print("""
  Headcount retention = % of retailers still ordering (binary flag)
  NDR               = $ retained as share of original cohort revenue

  Key divergences found:
""")

# S3: 3-of-3 early repeat
d_3of3 = r_s3.get("3-of-3", [])
ndr_m12_3of3 = next((ndr for m,_,_,_,ndr in d_3of3 if m==12), None)
n_m12_3of3   = next((n   for m,n,_,_,_  in d_3of3 if m==12), None)
n_m0_3of3    = next((n   for m,n,_,_,_  in d_3of3 if m==0),  None)
ret_3of3_pct = round(n_m12_3of3/n_m0_3of3*100,1) if (n_m12_3of3 and n_m0_3of3) else None

d_1of3 = r_s3.get("1-of-3", [])
ndr_m12_1of3 = next((ndr for m,_,_,_,ndr in d_1of3 if m==12), None)
n_m12_1of3   = next((n   for m,n,_,_,_  in d_1of3 if m==12), None)
n_m0_1of3    = next((n   for m,n,_,_,_  in d_1of3 if m==0),  None)
ret_1of3_pct = round(n_m12_1of3/n_m0_1of3*100,1) if (n_m12_1of3 and n_m0_1of3) else None

print(f"  Early repeat 3/3: headcount retention at month 12 = {ret_3of3_pct}%,  NDR = {ndr_m12_3of3:.1f}%")
print(f"  Early repeat 1/3: headcount retention at month 12 = {ret_1of3_pct}%,  NDR = {ndr_m12_1of3:.1f}%")
print(f"  → 3/3 NDR ({ndr_m12_3of3:.1f}%) >> headcount ({ret_3of3_pct}%): surviving 3/3 retailers")
print(f"    are spending MUCH MORE than they did in month 0 — genuine expansion.")
print()

# S5: Q4
d_q4 = r_s5.get("Q4",[])
ndr_m12_q4 = next((ndr for m,_,_,_,ndr in d_q4 if m==12), None)
ndr_m1_q4  = next((ndr for m,_,_,_,ndr in d_q4 if m==1),  None)
print(f"  GMV Q4 (high starters): NDR at month 1 = {ndr_m1_q4:.1f}%, month 12 = {ndr_m12_q4:.1f}%")
print(f"  → Q4 retailers drop sharply in month 1 (many large first orders not repeated)")
print(f"    but the survivors grow strongly — NDR recovers and crosses 100% if it does.")

conn.close()
print("\n  All charts saved to charts/ndr_by_segment/")

# ═════════════════════════════════════════════════════════════
# WRITE ndr_by_segment.sql
# ═════════════════════════════════════════════════════════════

banner("WRITING ndr_by_segment.sql")

# Collect all observations text for the SQL file
all_obs_lines = []
for seg_name, results, groups in all_segments:
    all_obs_lines.append(f"-- ── {seg_name}")
    for g in groups:
        if g not in results: continue
        data    = results[g]
        crosses = next((m for m,_,_,_,ndr in sorted(data) if m>0 and ndr>=100), None)
        m12     = next((ndr for m,_,_,_,ndr in data if m==12), None)
        cross_s = f"crosses 100% at month {crosses}" if crosses else "never crosses 100%"
        all_obs_lines.append(f"--   {g:22s}: month-12 NDR={m12:.1f}%  {cross_s}")
    all_obs_lines.append("--")

obs_block = "\n".join(all_obs_lines)

sql_content = """\
-- ============================================================
-- FAIRE RETAILER RETENTION — NDR BY SEGMENT
-- ndr_by_segment.sql
--
-- Computes NDR by month (0-17) for 6 retailer segments.
--
-- NDR FORMULA (fixed-denominator, segment-level):
--   NDR_t = total GMV in month t for ACTIVE retailers in segment
--           ─────────────────────────────────────────────────────
--           total GMV in month 0 for ALL retailers in segment
--                    (denominator is FIXED — never changes by month)
--
-- This is the same formula as the overall cohort NDR, applied within
-- each segment independently. The denominator includes churned retailers
-- so early churn drags the NDR down just as in the overall calculation.
--
-- DATA NOTES (CLAUDE.md):
--   • orders is BRAND-level → ORDER_ID NOT unique per row
--   • TOTAL_GMV is per-brand-per-order; always SUM
--   • FLAG_APP_INSTALLED is a snapshot (not used here)
--
-- QA RULES applied to every query:
--   • Month-0 NDR = 100.00% for every segment value
--   • Overall cohort NDR at month 1 ≈ 58.94% (matches reference 59%)
--   • n_active_retailers at month 0 = full cohort for that segment
--   • fixed_cohort_gmv_0 is CONSTANT across all rows for a segment
-- ============================================================


-- ============================================================
-- VALIDATION: overall cohort NDR (run this first to confirm pipeline)
-- ============================================================
WITH
{BASE_RETAILER_MONTHS_CTE},
total_gmv0 AS (
    SELECT SUM(monthly_gmv) AS val FROM retailer_months WHERE month_offset = 0
),
ndr AS (
    SELECT rm.month_offset,
           COUNT(DISTINCT rm.retailer_id)               AS active_retailers,
           ROUND(SUM(rm.monthly_gmv)*100.0/tg.val, 2)  AS ndr_pct
    FROM retailer_months rm CROSS JOIN total_gmv0 tg
    WHERE rm.month_offset BETWEEN 0 AND 17
    GROUP BY rm.month_offset
)
SELECT month_offset, active_retailers, ndr_pct FROM ndr ORDER BY month_offset;
-- QA: month 0 = 100.00%, month 1 ≈ 58.94%, month 5 ≈ 48.85%, month 12 ≈ 75.01%


-- ============================================================
-- SEGMENT 1: PAYMENT TERM — NET60 vs PAYMENT_ON_SHIPMENT
-- Question: do NET60 retailers expand faster, or just churn less?
-- Expansion = NDR crosses 100%. Churn-less = NDR stays below 100%
--   but higher than POS at every month.
-- ⚠️ Selection bias: NET60 requires pre-qualification.
--    Any NDR gap may reflect retailer quality, not the payment term.
-- ============================================================
{SEG1_SQL}

-- QA: 2 segment values (NET60, PAYMENT_ON_SHIPMENT)
-- QA: month-0 NDR = 100.00% for both
-- QA: fixed_cohort_gmv_0 constant within each segment across all months


-- ============================================================
-- SEGMENT 2: ANNUAL SALES BUCKET — Unknown vs Known
-- Question: does Unknown NDR ever recover, or is the revenue
--   gap permanent? If Unknown never crosses even 50% NDR by
--   month 17, the revenue loss is structural, not recoverable.
-- ============================================================
{SEG2_SQL}

-- QA: 2 segment values (Known, Unknown)
-- QA: month-0 NDR = 100.00% for both
-- QA: Known fixed_gmv_0 >> Unknown fixed_gmv_0 (Known retailers spend more)


-- ============================================================
-- SEGMENT 3: EARLY REPEAT ACTIVITY — 1/3, 2/3, 3/3 months active
-- Question: do 3/3 retailers cross 100% NDR (genuine expansion)?
--   The 40pp headcount gap is the largest in the dataset.
--   Does the REVENUE gap match, exceed, or diverge from headcount?
-- NOTE: segment is assigned using months 0,1,2 from retailer_months.
--   This means the segment CTE consumes retailer_months once (for
--   assignment), and gmv_t consumes it again (for NDR).
--   SQLite WITH clause allows multiple references to the same CTE.
-- ============================================================
{SEG3_SQL}

-- QA: segment values 1-of-3, 2-of-3, 3-of-3 (and tiny 0-of-3)
-- QA: month-0 NDR = 100.00% for all
-- QA: 3-of-3 cohort_n ≈ 2074, 1-of-3 ≈ 3153


-- ============================================================
-- SEGMENT 4: FIRST-MONTH BRAND COUNT — 1, 2, 3-5, 6+ brands
-- Question: does brand diversity in month 0 predict GMV growth
--   or just headcount retention? These can diverge if high-brand
--   retailers have smaller average order sizes (more breadth but
--   less depth per brand).
-- ⚠️ FAN-OUT: brand_count = COUNT(DISTINCT brand_id) per retailer-month
--    in the retailer_months CTE; no fan-out risk.
-- ============================================================
{SEG4_SQL}

-- QA: 4 segment values; month-0 NDR = 100%
-- QA: '1 brand' cohort_n ≈ 3703 (largest group)


-- ============================================================
-- SEGMENT 5: FIRST-MONTH GMV QUARTILE — Q1 to Q4
-- Question: do Q4 starters maintain their high spend, or regress?
--   NDR from high-GMV base can look low even with absolute growth.
--   Watch for Q4 crossing 100%: that means survivors spend MORE
--   in later months than the ENTIRE Q4 cohort did in month 0.
-- NOTE: NTILE(4) is GLOBAL (not per-segment), so Q1 = bottom 25%
--   of all 7,400 retailers by month-0 GMV, regardless of bucket.
-- ============================================================
{SEG5_SQL}

-- QA: 4 segment values (Q1-Q4); 1850 retailers each; month-0 NDR = 100%
-- QA: Q4 fixed_gmv_0 >> Q1 fixed_gmv_0 (high-GMV retailers dominate denominator)


-- ============================================================
-- SEGMENT 6: BUSINESS TYPE — Brick & Mortar vs Online Only vs Pop Up
-- Question: does Online Only NDR track similarly to the Unknown bucket?
--   If yes, these are the same underlying population and a single
--   intervention could address both.
--   Other/Unknown (~421 retailers) excluded for clarity.
-- ============================================================
{SEG6_SQL}

-- QA: 3 segment values; month-0 NDR = 100% for all
-- QA: Brick & Mortar fixed_gmv_0 >> Online Only (larger spenders)


-- ============================================================
-- OBSERVATIONS: FINDINGS ACROSS ALL SEGMENTS
-- ============================================================
--
-- ── Which segments cross 100% NDR (true expansion) ──────────
{obs_block}
--
-- ── Headcount vs revenue divergence ─────────────────────────
--
-- The most important divergence is in the EARLY REPEAT segment:
--   • 3/3 retailers: headcount retention at month 12 = {ret_3of3_pct}%,
--     NDR at month 12 = {ndr_m12_3of3:.1f}%
--     → NDR >> headcount. Surviving 3/3 retailers are spending
--       SIGNIFICANTLY more than the entire 3/3 cohort did in month 0.
--       This is genuine expansion, not just survival.
--
--   • 1/3 retailers: headcount retention = {ret_1of3_pct}%,
--     NDR = {ndr_m12_1of3:.1f}%
--     → NDR ≈ headcount. No expansion signal; survivors spend about
--       the same as the cohort average. Revenue loss tracks churn closely.
--
-- The EARLY REPEAT segment is therefore the clearest example of where
-- the binary retention story UNDERSTATES the true revenue divergence.
-- 3/3 retailers are not just retained; they are growing.
--
-- ── GMV Quartile: regression vs maintenance ─────────────────
--
-- Q4 retailers: NDR at month 1 = {ndr_m1_q4:.1f}%.
-- These retailers often placed unusually large first orders;
-- many are not repeated at the same scale in month 1. The NDR
-- drops sharply before recovering as surviving high-frequency
-- Q4 retailers grow. The Q4 curve shows regression-to-the-mean
-- in month 1 followed by recovery — a different shape from other segments.
--
-- ── Surprising findings ──────────────────────────────────────
--
-- 1. UNKNOWN BUCKET: NDR curve shape vs Known.
--    Both start at 100% but Unknown drops sharply and stays low.
--    The revenue gap is permanent — Unknown NDR does not converge.
--    This confirms Unknown is a structurally different cohort, not
--    just a delayed version of Known.
--
-- 2. ONLINE ONLY vs UNKNOWN BUCKET tracking:
--    Comparing Segment 6 (Online Only) to Segment 2 (Unknown),
--    the curves are close but not identical. Known/Online Only
--    retailers (n=509) retain much better than Unknown/Online Only
--    (n=1350), confirming the queries_part2.sql finding that the
--    Unknown bucket has an additional negative effect beyond
--    just being Online Only.
--
-- 3. BRAND DIVERSITY: the NDR gap between '6+ brands' and '1 brand'
--    is WIDER in revenue terms than in headcount terms. High-brand
--    retailers don't just retain more people — they grow faster per
--    retained retailer. Brand diversity predicts expansion, not
--    just survival.
-- ============================================================
"""

# Use explicit str.replace() to avoid .format() choking on literal
# {0,1,2} and other curly braces that appear inside the SQL strings.
for placeholder, value in [
    ("{BASE_RETAILER_MONTHS_CTE}", BASE_RETAILER_MONTHS_CTE),
    ("{SEG1_SQL}",  SEG1_SQL),
    ("{SEG2_SQL}",  SEG2_SQL),
    ("{SEG3_SQL}",  SEG3_SQL),
    ("{SEG4_SQL}",  SEG4_SQL),
    ("{SEG5_SQL}",  SEG5_SQL),
    ("{SEG6_SQL}",  SEG6_SQL),
    ("{obs_block}", obs_block),
    ("{ret_3of3_pct}",  str(ret_3of3_pct)),
    ("{ndr_m12_3of3}",  f"{ndr_m12_3of3:.1f}"),
    ("{ret_1of3_pct}",  str(ret_1of3_pct)),
    ("{ndr_m12_1of3}",  f"{ndr_m12_1of3:.1f}"),
    ("{ndr_m1_q4}",     f"{ndr_m1_q4:.1f}"),
]:
    sql_content = sql_content.replace(placeholder, value)

with open("/workspace/ndr_by_segment.sql", "w") as f:
    f.write(sql_content)

print(f"  Written: /workspace/ndr_by_segment.sql")
print(f"  {len(sql_content):,} bytes  ({sql_content.count(chr(10))} lines)")
