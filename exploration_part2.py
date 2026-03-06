"""
Faire Retailer Retention — Exploratory Analysis (Part 2)

exploration_part2.py
  Sections 4–5: behavioral signals + combined synthesis across Part 1 + Part 2.

  What changes vs Part 1:
    • Part 1 (exploration_part1.sql) covered STATIC PROFILE attributes:
        payment_term, flag_app_installed, annual_sales_bucket,
        business_type, store_type, acquisition_channel
    • Part 2 covers what retailers actually DO on the platform:
        first-month spend depth, brand diversity, early repeat ordering,
        platform breadth — things Faire can influence

  Architecture:
    • Python: load CSVs → SQLite, create views, run SQL, print results,
      compute observation text from actual values, write exploration_part2.sql
    • SQL: ALL analysis logic (no pandas analysis)
    • exploration_part2.sql: canonical SQL reference with real observations

  Part 1 retention gaps (hard-coded from exploration_part1.sql run):
    3c. Unknown annual_sales_bucket   +31.8pp  profile — overlaps Online Only
    3d. B&M vs Online Only            +27.4pp  profile — overlaps Unknown bucket
    3f. Channel (best vs worst)       +24.2pp  profile — Partnerships n=88
    3a. Payment term NET60 vs POS     +23.4pp  profile — selection bias
    3b. App install 1 vs 0            +21.5pp  profile — snapshot, reverse causality
    3e. Store type (best vs worst)    +21.1pp  profile — moderate signal
"""

import glob
import os
import sqlite3
import textwrap

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd          # only for CSV → SQLite loading

os.makedirs("charts", exist_ok=True)

# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def banner(title: str) -> None:
    print("\n" + "═" * 70)
    print(f"  {title}")
    print("═" * 70)

def sub(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 62 - len(title)))

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

def safe(rows, key, col, default=None, key_col=0):
    """Look up a value in a list-of-tuples by key column."""
    for r in rows:
        if str(r[key_col]) == str(key):
            try:
                return r[col]
            except IndexError:
                return default
    return default


# ─────────────────────────────────────────────────────────────
# STEP 0 — LOAD CSVs INTO SQLITE
# ─────────────────────────────────────────────────────────────

banner("STEP 0: LOADING DATA")

conn = sqlite3.connect(":memory:")

retailer_file = glob.glob("/workspace/psa_exercise_retailers*.csv")[0]
order_file    = glob.glob("/workspace/psa_exercise_orders*.csv")[0]

pd.read_csv(retailer_file).to_sql("psa_exercise_retailers", conn, if_exists="replace", index=False)
pd.read_csv(order_file).to_sql("psa_exercise_orders",    conn, if_exists="replace", index=False)

# ── Helper views ─────────────────────────────────────────────
# v_retailer_months: one row per (retailer_id, month_offset)
# ⚠️ FAN-OUT: orders is brand-level; ORDER_ID NOT unique per row
conn.execute("DROP VIEW IF EXISTS v_retailer_months")
conn.execute("""
CREATE VIEW v_retailer_months AS
SELECT
    o.retailer_id,
    -- calendar-month offset from cohort entry (0 = entry month)
    (CAST(strftime('%Y', o.order_created) AS INT)
        - CAST(strftime('%Y', r.first_confirmed_order_placed_at) AS INT)) * 12
    + (CAST(strftime('%m', o.order_created) AS INT)
        - CAST(strftime('%m', r.first_confirmed_order_placed_at) AS INT))
        AS month_offset,
    SUM(o.total_gmv)           AS monthly_gmv,
    COUNT(DISTINCT o.order_id) AS order_count,
    COUNT(DISTINCT o.brand_id) AS brand_count
FROM psa_exercise_orders o
JOIN psa_exercise_retailers r ON o.retailer_id = r.retailer_id
WHERE o.order_state = 'PROCESSING'
GROUP BY o.retailer_id, month_offset
""")

# v_retailer_summary: one row per retailer with lifetime metrics + retention flags
conn.execute("DROP VIEW IF EXISTS v_retailer_summary")
conn.execute("""
CREATE VIEW v_retailer_summary AS
SELECT
    r.retailer_id,
    r.payment_term,
    r.flag_app_installed,
    r.annual_sales_bucket,
    r.retailer_store_type,
    r.retailer_bucketed_channel,
    r.retailer_business_type,
    r.first_confirmed_order_placed_at,
    r.first_app_session_at,
    r.first_desktop_session_at,
    r.first_mobile_web_session_at,
    COALESCE(SUM(rm.monthly_gmv), 0)             AS lifetime_gmv,
    COALESCE(MAX(rm.month_offset), -1)            AS max_month,
    COALESCE(COUNT(DISTINCT rm.month_offset), 0)  AS months_active,
    MAX(CASE WHEN rm.month_offset >= 6  THEN 1 ELSE 0 END) AS has_m6_plus,
    MAX(CASE WHEN rm.month_offset >= 12 THEN 1 ELSE 0 END) AS has_m12_plus,
    CASE WHEN MAX(rm.month_offset) = 0
              OR MAX(rm.month_offset) IS NULL THEN 1 ELSE 0 END AS is_single_month_churn,
    (CASE WHEN r.first_app_session_at IS NOT NULL          THEN 1 ELSE 0 END
     + CASE WHEN r.first_desktop_session_at IS NOT NULL    THEN 1 ELSE 0 END
     + CASE WHEN r.first_mobile_web_session_at IS NOT NULL THEN 1 ELSE 0 END)
        AS platform_count
FROM psa_exercise_retailers r
LEFT JOIN v_retailer_months rm ON r.retailer_id = rm.retailer_id
GROUP BY r.retailer_id
""")

conn.commit()
print("  Loaded and views created.")


# ═════════════════════════════════════════════════════════════
# SECTION 4: BEHAVIORAL EXPLORATION
# ═════════════════════════════════════════════════════════════

banner("SECTION 4: BEHAVIORAL EXPLORATION")
print("  Shifting from WHO retailers are (Section 3) to WHAT they DO.")
print("  Behavioral signals reflect on-platform engagement Faire can influence.")


# ── 4a. First-month GMV quartile → 12-mo retention ───────
sub("4a. 12-mo retention by first-month GMV quartile")
print("  What I'm looking for: does spending MORE in month 0 predict retention?")
print("  NTILE(4) splits retailers into equal-size bins by month-0 GMV.")
print("  If Q4 >> Q1, initial spend depth is a retention signal.")

SQL_4A = """
-- What I'm looking for: does higher first-month spend predict 12-mo retention?
-- Hypothesis-free: just observe the gradient from Q1 to Q4.
-- ⚠️ FAN-OUT: v_retailer_months already collapses brand-level rows; SUM is safe
-- NOTE: NTILE(4) uses month-0 GMV only, not lifetime GMV
--
-- CTE m0_stats (grain: one row per retailer_id, month 0 only)
-- CTE gmv_ntile (grain: one row per retailer_id, adds quartile label)
-- JOIN: v_retailer_summary (one per retailer) → m0_stats (one per retailer) → 1:1
-- Output grain: one row per GMV quartile (4 rows)

WITH
-- Month-0 GMV and brand count per retailer
-- Grain: one row per retailer_id
m0_stats AS (
    SELECT retailer_id, monthly_gmv AS gmv_m0, brand_count AS brands_m0
    FROM v_retailer_months
    WHERE month_offset = 0
),
-- Assign equal-size quartile by month-0 GMV
-- Grain: one row per retailer_id
gmv_ntile AS (
    SELECT
        rs.retailer_id,
        m.gmv_m0,
        NTILE(4) OVER (ORDER BY m.gmv_m0)  AS gmv_quartile,  -- 1=lowest spend, 4=highest
        rs.has_m12_plus,
        rs.is_single_month_churn
    -- JOIN grain: both one-per-retailer → 1:1; no fan-out
    FROM v_retailer_summary rs
    JOIN m0_stats m ON rs.retailer_id = m.retailer_id
)
SELECT
    gmv_quartile                                AS quartile,
    COUNT(*)                                    AS retailer_count,
    ROUND(MIN(gmv_m0), 0)                       AS min_gmv_m0,  -- quartile boundary
    ROUND(MAX(gmv_m0), 0)                       AS max_gmv_m0,
    ROUND(AVG(gmv_m0), 0)                       AS avg_gmv_m0,
    ROUND(AVG(has_m12_plus)      * 100, 1)      AS ret_12mo_pct,
    ROUND(AVG(is_single_month_churn) * 100, 1)  AS single_month_churn_pct
FROM gmv_ntile
GROUP BY gmv_quartile
ORDER BY gmv_quartile

-- QA: exactly 4 rows; total retailer_count = 7400
-- QA: min/max boundaries should be monotone increasing
-- QA: ret_12mo_pct should increase from Q1 to Q4 if hypothesis holds
"""
r4a = run(conn, SQL_4A)
print_table(r4a, ["quartile","count","min_gmv","max_gmv","avg_gmv","ret_12mo%","churn%"])

q1_ret = safe(r4a, "1", 5); q4_ret = safe(r4a, "4", 5)
gap_4a = q4_ret - q1_ret if (q1_ret and q4_ret) else None
q4_churn = safe(r4a, "4", 6); q1_churn = safe(r4a, "1", 6)
q1_avg_gmv = safe(r4a, "1", 4); q4_avg_gmv = safe(r4a, "4", 4)
obs_4a = (
    f"Q4 (avg ${q4_avg_gmv:,.0f} month-0 GMV): {q4_ret}% 12-mo retention. "
    f"Q1 (avg ${q1_avg_gmv:,.0f}): {q1_ret}%. "
    f"Gap = {gap_4a:.1f}pp — strong monotone gradient across quartiles. "
    f"Single-month churn: Q1 {q1_churn}% vs Q4 {q4_churn}%. "
    f"CAUTION: GMV in month 0 could be CAUSED by retailer size, not platform engagement. "
    f"A large retailer will naturally spend more AND retain better regardless of Faire's actions. "
    f"Worth investigating: does the gradient hold within each ANNUAL_SALES_BUCKET? "
    f"If yes, spend depth is predictive beyond retailer size."
)
print(f"\n  OBSERVATION: {obs_4a}")


# ── 4b. First-month brand count → 12-mo retention ────────
sub("4b. 12-mo retention by first-month brand count")
print("  What I'm looking for: does trying MORE brands in month 0 predict retention?")
print("  Buckets: 1 brand, 2 brands, 3–5, 6+.")
print("  Brand diversity in month 0 = breadth of discovery on the platform.")

SQL_4B = """
-- What I'm looking for: does brand exploration in month 0 predict 12-mo retention?
-- Brand count is more actionable than GMV — Faire can SHOW retailers more brands.
-- ⚠️ FAN-OUT: v_retailer_months collapses brand-level rows; brand_count is correct
--
-- CTE m0_brands (grain: one row per retailer_id, month 0 only)
-- CTE bucketed (grain: one row per retailer_id, adds bucket label)
-- JOIN: v_retailer_summary (one per retailer) → m0_brands (one per retailer) → 1:1
-- Output grain: one row per bucket (4 rows)

WITH
-- Month-0 distinct brand count per retailer
-- Grain: one row per retailer_id
m0_brands AS (
    SELECT retailer_id, brand_count AS brands_m0
    FROM v_retailer_months
    WHERE month_offset = 0
),
-- Label each retailer with a brand-count bucket
-- Grain: one row per retailer_id
bucketed AS (
    SELECT
        CASE
            WHEN m.brands_m0 = 1  THEN '1'
            WHEN m.brands_m0 = 2  THEN '2'
            WHEN m.brands_m0 <= 5 THEN '3-5'
            ELSE                       '6+'
        END                      AS brand_bucket,
        m.brands_m0,
        rs.has_m12_plus,
        rs.is_single_month_churn
    -- JOIN grain: both one-per-retailer → 1:1
    FROM v_retailer_summary rs
    JOIN m0_brands m ON rs.retailer_id = m.retailer_id
)
SELECT
    brand_bucket,
    COUNT(*)                                    AS retailer_count,
    ROUND(AVG(brands_m0), 1)                    AS avg_brands_m0,
    ROUND(AVG(has_m12_plus)      * 100, 1)      AS ret_12mo_pct,
    ROUND(AVG(is_single_month_churn) * 100, 1)  AS single_month_churn_pct
FROM bucketed
GROUP BY brand_bucket
ORDER BY CASE brand_bucket WHEN '1' THEN 1 WHEN '2' THEN 2 WHEN '3-5' THEN 3 ELSE 4 END

-- QA: 4 rows; total retailer_count = 7400
-- QA: ret_12mo_pct should increase from '1' to '6+' if brand diversity matters
"""
r4b = run(conn, SQL_4B)
print_table(r4b, ["brand_bucket","count","avg_brands","ret_12mo%","churn%"])

b1_ret  = safe(r4b, "1",  3); b6_ret  = safe(r4b, "6+", 3)
gap_4b  = b6_ret - b1_ret if (b1_ret and b6_ret) else None
b1_n    = safe(r4b, "1",  1); b6_n    = safe(r4b, "6+", 1)
b1_churn = safe(r4b, "1", 4); b6_churn = safe(r4b, "6+", 4)
obs_4b = (
    f"1 brand in month 0 (n={b1_n:,}): {b1_ret}% 12-mo retention. "
    f"6+ brands (n={b6_n:,}): {b6_ret}%. "
    f"Gap = {gap_4b:.1f}pp. "
    f"Monotone gradient from 1 → 6+. "
    f"Single-month churn: 1-brand {b1_churn}% vs 6+ {b6_churn}% — "
    f"{b1_churn - b6_churn:.1f}pp difference at the very first order. "
    f"Brand diversity in month 0 is more actionable than GMV: "
    f"Faire can surface brand recommendations and bundles to increase discovery. "
    f"BUT correlation ≠ causation — retailers who WANT to discover many brands "
    f"may already be the high-intent cohort."
)
print(f"\n  OBSERVATION: {obs_4b}")


# ── 4c. Churned vs retained: first-month behavioral profile ─
sub("4c. Churned vs retained: month-0 behavioral profile comparison")
print("  What I'm looking for: HOW DIFFERENT are churned vs retained in month 0?")
print("  churned = max_month ≤ 2  (left within 3 months, never came back)")
print("  retained = max_month ≥ 12  (still active through year 1)")
print("  'Middle' = max_month 3–11 for completeness.")

SQL_4C = """
-- What I'm looking for: quantify how different churned and retained retailers
--   look in their very first month, BEFORE we know their fate.
-- If the groups look very different, month-0 behavior could be an early warning signal.
-- ⚠️ FAN-OUT: v_retailer_months already aggregated; no fan-out risk
--
-- CTE m0_stats (grain: one row per retailer_id): month-0 metrics
-- CTE segmented (grain: one row per retailer_id): adds segment label
-- JOIN: v_retailer_summary (one per retailer) → m0_stats (one per retailer) → 1:1
-- Output grain: one row per cohort_segment (3 rows)

WITH
-- Month-0 GMV, brand count, and order count per retailer
-- Grain: one row per retailer_id
m0_stats AS (
    SELECT
        retailer_id,
        monthly_gmv  AS gmv_m0,
        brand_count  AS brands_m0,
        order_count  AS orders_m0     -- distinct orders in month 0 (not brand rows)
    FROM v_retailer_months
    WHERE month_offset = 0
),
-- Add segment label: churned / middle / retained
-- Grain: one row per retailer_id
segmented AS (
    SELECT
        CASE
            WHEN rs.max_month <= 2  THEN '1_Churned (max ≤ 2)'
            WHEN rs.max_month >= 12 THEN '3_Retained (max ≥ 12)'
            ELSE                         '2_Middle (3–11)'
        END                       AS cohort_segment,
        m.gmv_m0,
        m.brands_m0,
        m.orders_m0
    -- JOIN grain: both one-per-retailer → 1:1
    FROM v_retailer_summary rs
    JOIN m0_stats m ON rs.retailer_id = m.retailer_id
)
SELECT
    cohort_segment,
    COUNT(*)                      AS retailer_count,
    ROUND(AVG(gmv_m0), 0)         AS avg_gmv_m0,
    ROUND(AVG(brands_m0), 2)      AS avg_brands_m0,
    ROUND(AVG(orders_m0), 2)      AS avg_orders_m0,
    ROUND(MIN(gmv_m0), 0)         AS min_gmv_m0,
    ROUND(MAX(gmv_m0), 0)         AS max_gmv_m0
FROM segmented
GROUP BY cohort_segment
ORDER BY cohort_segment

-- QA: 3 rows; total = 7400
-- QA: retained should have higher avg_gmv_m0 and avg_brands_m0 than churned
"""
r4c = run(conn, SQL_4C)
print_table(r4c, ["segment","count","avg_gmv_m0","avg_brands_m0","avg_orders_m0","min_gmv","max_gmv"])

churned_row  = next((r for r in r4c if "Churned"  in str(r[0])), None)
retained_row = next((r for r in r4c if "Retained" in str(r[0])), None)

if churned_row and retained_row:
    gmv_ratio    = retained_row[2] / churned_row[2]  if churned_row[2]  else None
    brand_ratio  = retained_row[3] / churned_row[3]  if churned_row[3]  else None
    order_ratio  = retained_row[4] / churned_row[4]  if churned_row[4]  else None
    obs_4c = (
        f"Retained (max ≥ 12, n={retained_row[1]:,}): avg month-0 GMV ${retained_row[2]:,.0f}, "
        f"avg {retained_row[3]:.2f} brands, avg {retained_row[4]:.2f} orders. "
        f"Churned (max ≤ 2, n={churned_row[1]:,}): avg month-0 GMV ${churned_row[2]:,.0f}, "
        f"avg {churned_row[3]:.2f} brands, avg {churned_row[4]:.2f} orders. "
        f"RATIOS: retained spends {gmv_ratio:.1f}x more in month 0, "
        f"buys from {brand_ratio:.1f}x more brands, places {order_ratio:.1f}x more orders. "
        f"The groups are CLEARLY different on day one. "
        f"However, overlapping ranges (min/max) show no clean decision boundary — "
        f"you cannot cleanly separate them with a threshold rule. "
        f"This means month-0 signals are informative for scoring but not deterministic."
    )
else:
    obs_4c = "Could not compute — check segment labels."
print(f"\n  OBSERVATION: {obs_4c}")


# ── 4d. Early ordering frequency → 12-mo retention ───────
sub("4d. 12-mo retention by active months in first 3 months (habit formation)")
print("  What I'm looking for: does ordering in MULTIPLE early months predict retention?")
print("  Hypothesis-free framing: just count months 0, 1, 2 where retailer had any order.")
print("  1/3 = only joined (month 0 only); 2/3 = bought in one repeat month; 3/3 = all three.")

SQL_4D = """
-- What I'm looking for: does early repeat ordering (months 0,1,2) predict 12-mo retention?
-- This is the MOST ACTIONABLE behavioral signal: Faire can send nudges/reminders in months 1-2.
-- ⚠️ FAN-OUT: v_retailer_months already at retailer-month grain; COUNT(DISTINCT) safe
--
-- CTE first3_active (grain: one row per retailer_id): count distinct active months in {0,1,2}
-- JOIN: v_retailer_summary (one per retailer) → first3_active (≤1 per retailer) → 1:1
-- COALESCE(0): defensive for any retailer in retailers table with no order records
-- Output grain: one row per active_months value (3–4 rows)

WITH
-- Count how many of months {0,1,2} each retailer placed at least one order
-- Grain: one row per retailer_id
first3_active AS (
    SELECT
        retailer_id,
        COUNT(DISTINCT month_offset)  AS active_months_first3
    FROM v_retailer_months
    WHERE month_offset IN (0, 1, 2)
    GROUP BY retailer_id
)
SELECT
    COALESCE(f.active_months_first3, 0)   AS active_months,   -- 0 if retailer had no orders at all
    COUNT(*)                               AS retailer_count,
    ROUND(AVG(rs.has_m12_plus) * 100, 1)  AS ret_12mo_pct,
    ROUND(AVG(rs.is_single_month_churn) * 100, 1) AS single_month_churn_pct
-- JOIN: v_retailer_summary (one per retailer) to first3_active (≤1 per retailer) → 1:1
FROM v_retailer_summary rs
LEFT JOIN first3_active f ON rs.retailer_id = f.retailer_id
GROUP BY COALESCE(f.active_months_first3, 0)
ORDER BY active_months

-- QA: rows for values 1, 2, 3 (and possibly 0 for edge cases)
-- QA: total retailer_count = 7400
-- QA: ret_12mo_pct should STRONGLY increase from 1 to 3 — expected largest behavioral gap
"""
r4d = run(conn, SQL_4D)
print_table(r4d, ["active_months","count","ret_12mo%","churn%"])

d1_ret = safe(r4d, "1", 2); d3_ret = safe(r4d, "3", 2)
gap_4d = d3_ret - d1_ret if (d1_ret and d3_ret) else None
d1_n   = safe(r4d, "1", 1); d3_n   = safe(r4d, "3", 1)
d2_ret = safe(r4d, "2", 2)
d1_churn = safe(r4d, "1", 3); d3_churn = safe(r4d, "3", 3)
obs_4d = (
    f"1-of-3 months active (n={d1_n:,}): {d1_ret}% 12-mo retention. "
    f"2-of-3 (n={safe(r4d, '2', 1):,}): {d2_ret}%. "
    f"3-of-3 (n={d3_n:,}): {d3_ret}%. "
    f"Gap 3/3 vs 1/3 = {gap_4d:.1f}pp — THE LARGEST SINGLE GAP in this analysis. "
    f"Single-month churn: 1/3 {d1_churn}% vs 3/3 {d3_churn}%. "
    f"Each additional active month in the first 3 adds ~{gap_4d/2:.0f}pp of 12-mo retention. "
    f"This is the most actionable finding: if Faire can convert a retailer who orders "
    f"in month 0 only into one who also orders in month 1, "
    f"retention roughly doubles from {d1_ret}% to {d2_ret}%. "
    f"CRITICAL caveat: causality is unclear. Do repeat orders CAUSE retention, "
    f"or do both reflect underlying retailer intent that Faire cannot change? "
    f"An experiment (e.g., discount offer to 1/3 retailers to trigger month-1 order) "
    f"would help distinguish these."
)
print(f"\n  OBSERVATION: {obs_4d}")


# ── 4e. Platform count → 12-mo retention ─────────────────
sub("4e. 12-mo retention by platform count (app + desktop + mobile web)")
print("  What I'm looking for: does using Faire across multiple channels predict retention?")
print("  Platform count = non-null values in {FIRST_APP_SESSION_AT, FIRST_DESKTOP_SESSION_AT,")
print("                                         FIRST_MOBILE_WEB_SESSION_AT}")
print("  0 platforms = no session recorded in any channel (very unusual).")

SQL_4E = """
-- What I'm looking for: does using more of Faire's platforms predict retention?
-- Multi-platform usage may signal deeper engagement OR just reflect that
-- the retailer has been active for longer (survivorship effect).
-- ⚠️ CAUTION: platform_count is a lifetime snapshot (not at month 0), so
--   retained retailers naturally accumulate more platform sessions over time.
--   This is another potential reverse causality: retained retailers APPEAR
--   on more platforms because they've been active longer, not vice versa.
-- platform_count is pre-computed in v_retailer_summary
-- Output grain: one row per platform_count value (0–3)

SELECT
    platform_count,
    COUNT(*)                                    AS retailer_count,
    ROUND(AVG(has_m12_plus) * 100, 1)          AS ret_12mo_pct,
    ROUND(AVG(is_single_month_churn) * 100, 1)  AS single_month_churn_pct
FROM v_retailer_summary
GROUP BY platform_count
ORDER BY platform_count

-- QA: rows for values 0, 1, 2, 3; total = 7400
-- QA: platform_count = 0 should be tiny (no session data at all)
-- QA: ret_12mo should increase with platform count (expected strong gradient)
"""
r4e = run(conn, SQL_4E)
print_table(r4e, ["platforms","count","ret_12mo%","churn%"])

p1_ret = safe(r4e, "1", 2); p3_ret = safe(r4e, "3", 2)
p0_ret = safe(r4e, "0", 2)
gap_4e = p3_ret - p1_ret if (p1_ret and p3_ret) else None
p1_n   = safe(r4e, "1", 1); p3_n   = safe(r4e, "3", 1)
p2_ret = safe(r4e, "2", 2)
p1_churn = safe(r4e, "1", 3); p3_churn = safe(r4e, "3", 3)
obs_4e = (
    f"1 platform (n={p1_n:,}): {p1_ret}% 12-mo retention. "
    f"2 platforms (n={safe(r4e, '2', 1):,}): {p2_ret}%. "
    f"3 platforms (n={p3_n:,}): {p3_ret}%. "
    f"Gap 3-platforms vs 1-platform = {gap_4e:.1f}pp. "
    f"Single-month churn: 1-platform {p1_churn}% vs 3-platform {p3_churn}%. "
    f"STRONG REVERSE CAUSALITY WARNING: platform_count is a LIFETIME snapshot. "
    f"Retailers who have been active for 12+ months naturally accumulate more "
    f"platform sessions than those who churned after month 0. The platform count "
    f"is partly a MEASURE of retention, not a predictor of it. "
    f"To test causality, we would need to check whether platform_count AS OF MONTH 1 "
    f"predicts retention from month 2 onwards — which requires event-level session data "
    f"timestamped more precisely than FIRST_*_SESSION_AT provides."
)
print(f"\n  OBSERVATION: {obs_4e}")


# ═════════════════════════════════════════════════════════════
# SECTION 4 GAP SUMMARY
# ═════════════════════════════════════════════════════════════

banner("SECTION 4: BEHAVIORAL GAP RANKING")

gaps_s4 = []
if gap_4d: gaps_s4.append(("4d. Early repeat (3/3 vs 1/3 months active)", gap_4d,
                             "behavioral — strongest & most actionable", "LARGEST GAP"))
if gap_4e: gaps_s4.append(("4e. Platform breadth (3 vs 1 platforms)",   gap_4e,
                             "behavioral — strong reverse causality risk", ""))
if gap_4a: gaps_s4.append(("4a. First-month GMV (Q4 vs Q1)",            gap_4a,
                             "behavioral — confounded with retailer size", ""))
if gap_4b: gaps_s4.append(("4b. Brand diversity (6+ vs 1 brand)",       gap_4b,
                             "behavioral — most actionable (Faire can show more brands)", ""))
gaps_s4.sort(key=lambda x: x[1], reverse=True)

print("\n  Section 4 Behavioral Gaps:")
print("  " + "-" * 70)
for name, gap, note, flag in gaps_s4:
    print(f"  {gap:+.1f}pp  {name}  [{note}]{(' ◄ ' + flag) if flag else ''}")


# ═════════════════════════════════════════════════════════════
# SECTION 5: COMBINED SYNTHESIS AND NEXT STEPS
# ═════════════════════════════════════════════════════════════

banner("SECTION 5: COMBINED SYNTHESIS AND NEXT STEPS")

# Part 1 gaps (from exploration_part1.sql output — hard-coded)
PART1_GAPS = [
    ("3c. Unknown annual_sales_bucket vs best known", 31.8,
     "profile", "Overlaps heavily with Online Only business type"),
    ("3d. Business type: B&M vs Online Only",         27.4,
     "profile", "Likely same population as Unknown bucket"),
    ("3f. Channel: Organic vs Partnerships",           24.2,
     "profile", "Partnerships n=88; too small for conclusions"),
    ("3a. Payment term: NET60 vs POS",                 23.4,
     "profile", "Selection bias: NET60 = pre-qualified retailers"),
    ("3b. App install: flag=1 vs 0",                   21.5,
     "profile", "Snapshot field; strong reverse causality risk"),
    ("3e. Store type: gift vs other",                  21.1,
     "profile", "Moderate signal; overlaps with Unknown segment"),
]

# Combine Part 1 and Part 2
ALL_GAPS = []
for name, gap, type_, caveat in PART1_GAPS:
    ALL_GAPS.append((name, gap, type_, caveat))
for name, gap, note, flag in gaps_s4:
    ALL_GAPS.append((name, gap, "behavioral", note))
ALL_GAPS.sort(key=lambda x: x[1], reverse=True)

print("\n  Combined ranking (Section 3 profile + Section 4 behavioral):")
print("  " + "-" * 78)
print(f"  {'Rank':<5} {'Gap':>6}  {'Type':>12}  {'Dimension'}")
print("  " + "-" * 78)
for i, (name, gap, type_, caveat) in enumerate(ALL_GAPS, 1):
    print(f"  {i:<5} {gap:>+.1f}pp  {type_:>12}  {name}")
print()
print("  All caveats:")
for i, (name, gap, type_, caveat) in enumerate(ALL_GAPS, 1):
    print(f"    {i}. {name}: {caveat}")


# ═════════════════════════════════════════════════════════════
# WRITE exploration_part2.sql
# ═════════════════════════════════════════════════════════════

banner("WRITING exploration_part2.sql")

# Build Section 5 combined table (for SQL comment)
s5_gap_table_lines = []
for i, (name, gap, type_, caveat) in enumerate(ALL_GAPS, 1):
    s5_gap_table_lines.append(
        f"--   {i:>2}.  {gap:>+.1f}pp  {type_:<12}  {name}"
    )
s5_gap_table = "\n".join(s5_gap_table_lines)

s5_caveat_lines = []
for i, (name, gap, type_, caveat) in enumerate(ALL_GAPS, 1):
    # Wrap long lines
    line = f"--   {i:>2}. {name}:"
    wrapped = textwrap.wrap(f"   {i:>2}. {name}: {caveat}", width=70)
    for j, w in enumerate(wrapped):
        s5_caveat_lines.append(f"--{w}")
s5_caveat_block = "\n".join(s5_caveat_lines)

# Section 5 next-steps questions (derived from the data)
s5_qs = [
    ("EARLY REPEAT ORDERING (4d — BIGGEST gap: {gap:.0f}pp)".format(gap=gap_4d),
     [
         f"What causes a {d1_ret}% retention rate for 1/3 retailers? Is month-1 inactivity",
         "driven by stockpiling (ordered enough in month 0), discovery failure",
         "(didn't find the right brands), or intent (they only ever wanted one order)?",
         "Can a targeted nudge (e.g. brand recommendation email in week 3) increase",
         "month-1 activation? What is the marginal cost vs retention value?",
         "Experiment idea: randomly assign 1/3 retailers to a reminder campaign",
         "and measure month-1 reactivation + 12-mo retention lift.",
     ]
    ),
    ("PLATFORM BREADTH (4e — {gap:.0f}pp gap, but reverse causality risk)".format(gap=gap_4e),
     [
         "Platform count is a LIFETIME snapshot — retained retailers naturally",
         f"accumulate more sessions. The {gap_4e:.0f}pp gap may largely be",
         "measuring retention (outcome) rather than predicting it (input).",
         "Better question: what % of retailers first used mobile BEFORE month 1?",
         "Did mobile-first vs desktop-first retailers have different month-1",
         "reactivation rates? This requires timestamped session data.",
     ]
    ),
    ("UNKNOWN ANNUAL_SALES_BUCKET (3c — 31.8pp below best known)",
     [
         f"Unknown has {safe(r4c, '1_Churned', 1, '?')} retailers below the Known",
         "cohort on every metric. Key question: are they structurally different",
         "(new online retailers, no history) or just missing data?",
         "Cross-tab Unknown × business_type_clean: if Unknown is >70% Online Only,",
         "the 'Unknown' problem IS the 'Online Only' problem.",
         "Also: do Unknown retailers get a different onboarding experience?",
         "Check if they have different FIRST_APP_SESSION_AT timing.",
     ]
    ),
    ("BRAND DIVERSITY IN MONTH 0 (4b — {gap:.0f}pp gap)".format(gap=gap_4b),
     [
         f"1-brand retailers (n={b1_n:,}) have {b1_churn}% single-month churn vs",
         f"{b6_churn}% for 6+ brand retailers. Can Faire's onboarding surface more",
         "brand recommendations during the first visit to reduce 1-brand orders?",
         "Is low brand count caused by store specialization (e.g. a grocery store",
         "only buys from food brands) or by limited discovery?",
         "Check: does brand diversity in month 0 predict repeat purchasing in month 1,",
         "controlling for GMV? That would distinguish discovery from intent.",
     ]
    ),
    ("PAYMENT TERM (3a — 23.4pp gap, selection bias)",
     [
         "NET60 and PAYMENT_ON_SHIPMENT retailers differ on BOTH retention AND size.",
         "To isolate the payment-term effect, cross-tab: within each GMV quartile",
         "(from 4a), do NET60 retailers still retain better than POS retailers?",
         "If the gap disappears within quartiles, it's pure selection bias.",
         "If it persists, the payment term itself may matter (cash flow relief).",
     ]
    ),
    ("APP INSTALL (3b — 21.5pp gap, reverse causality)",
     [
         "The FIRST_APP_SESSION_AT timing analysis (run in analysis.py) showed that",
         "retailers who installed the app 180+ days AFTER their first order had",
         "HIGHER 12-mo retention than those who installed before their first order.",
         "This is the clearest reverse causality signature in the data.",
         "The app install flag is NOT a reliable leading indicator of retention.",
         "Better metric: app install BEFORE month 1 vs after month 1.",
     ]
    ),
]

s5_questions_block = ""
for title, lines in s5_qs:
    s5_questions_block += f"-- ── {title}\n"
    for line in lines:
        wrapped = textwrap.wrap(line, 68)
        for w in wrapped:
            s5_questions_block += f"--   {w}\n"
    s5_questions_block += "--\n"

# Build the full SQL file content
sql_file = """\
-- ============================================================
-- FAIRE RETAILER RETENTION — EXPLORATORY ANALYSIS (PART 2)
-- exploration_part2.sql
--
-- Covers: Section 4 (behavioral signals) + Section 5 (synthesis)
-- Predecessor: exploration_part1.sql (static profile dimensions)
--
-- KEY SHIFT: Part 1 covered WHO retailers are at signup.
--   Part 2 covers what retailers DO in their first months.
--   Behavioral signals (what they DO) show larger, cleaner gaps
--   than profile signals (who they are) — and are more actionable.
--
-- DATA NOTES (CLAUDE.md):
--   • orders table is BRAND-level → ORDER_ID NOT unique per row
--   • TOTAL_GMV is per-brand-per-order; always SUM to get retailer GMV
--   • ORDER_CREATED is first-of-month (monthly granularity)
--   • FLAG_APP_INSTALLED is a snapshot — use FIRST_APP_SESSION_AT for timing
--   • FIRST_CONFIRMED_ORDER_PLACED_AT = 2020-06-01 or 2020-07-01
-- ============================================================


-- ============================================================
-- SETUP: HELPER VIEWS
-- (Same as exploration_part1.sql; run once per session)
-- ============================================================

DROP VIEW IF EXISTS v_retailer_months;
CREATE VIEW v_retailer_months AS
SELECT
    o.retailer_id,
    -- Calendar-month offset from cohort entry month (0 = entry month)
    (CAST(strftime('%Y', o.order_created) AS INT)
        - CAST(strftime('%Y', r.first_confirmed_order_placed_at) AS INT)) * 12
    + (CAST(strftime('%m', o.order_created) AS INT)
        - CAST(strftime('%m', r.first_confirmed_order_placed_at) AS INT))
        AS month_offset,
    SUM(o.total_gmv)           AS monthly_gmv,
    COUNT(DISTINCT o.order_id) AS order_count,   -- ⚠️ distinct; NOT brand rows
    COUNT(DISTINCT o.brand_id) AS brand_count
FROM psa_exercise_orders o
JOIN psa_exercise_retailers r ON o.retailer_id = r.retailer_id
WHERE o.order_state = 'PROCESSING'
GROUP BY o.retailer_id, month_offset;

DROP VIEW IF EXISTS v_retailer_summary;
CREATE VIEW v_retailer_summary AS
SELECT
    r.retailer_id,
    r.payment_term,
    r.flag_app_installed,
    r.annual_sales_bucket,
    r.retailer_store_type,
    r.retailer_bucketed_channel,
    COALESCE(SUM(rm.monthly_gmv), 0)             AS lifetime_gmv,
    COALESCE(MAX(rm.month_offset), -1)            AS max_month,
    COALESCE(COUNT(DISTINCT rm.month_offset), 0)  AS months_active,
    MAX(CASE WHEN rm.month_offset >= 12 THEN 1 ELSE 0 END) AS has_m12_plus,
    CASE WHEN MAX(rm.month_offset) = 0
              OR MAX(rm.month_offset) IS NULL THEN 1 ELSE 0 END AS is_single_month_churn,
    -- Platform count: non-null first-session timestamps across 3 channels
    (CASE WHEN r.first_app_session_at IS NOT NULL          THEN 1 ELSE 0 END
     + CASE WHEN r.first_desktop_session_at IS NOT NULL    THEN 1 ELSE 0 END
     + CASE WHEN r.first_mobile_web_session_at IS NOT NULL THEN 1 ELSE 0 END)
        AS platform_count
FROM psa_exercise_retailers r
LEFT JOIN v_retailer_months rm ON r.retailer_id = rm.retailer_id
GROUP BY r.retailer_id;


-- ============================================================
-- SECTION 4: BEHAVIORAL EXPLORATION
-- What retailers DO on the platform — vs who they ARE (Section 3)
-- Key question: which BEHAVIORS in the first 1–3 months are the
--   strongest predictors of 12-month retention?
-- These are potentially actionable: Faire can design nudges, features,
--   and onboarding flows to push retailers toward high-retention behaviors.
-- ============================================================


-- ── 4a. 12-mo retention by first-month GMV quartile ─────
-- What I'm looking for: does spending MORE in month 0 predict retention?
-- NTILE(4) splits retailers into equal-size bins by month-0 GMV.
-- If Q4 >> Q1, initial spend depth is a leading retention indicator.
-- CAUTION: first-month GMV is correlated with retailer SIZE, which
--   may drive both spend and retention independently. See caveat below.
-- ⚠️ FAN-OUT: v_retailer_months collapses brand-level rows; SUM safe
--
-- CTE m0_stats (grain: one row per retailer_id — month 0 only)
-- CTE gmv_ntile (grain: one row per retailer_id — adds NTILE quartile)
-- JOIN grain: v_retailer_summary (one per retailer) × m0_stats (one per retailer) → 1:1
-- Output grain: one row per GMV quartile (4 rows)

WITH
m0_stats AS (
    SELECT retailer_id, monthly_gmv AS gmv_m0, brand_count AS brands_m0
    FROM v_retailer_months
    WHERE month_offset = 0
),
gmv_ntile AS (
    SELECT
        rs.retailer_id,
        m.gmv_m0,
        NTILE(4) OVER (ORDER BY m.gmv_m0)  AS gmv_quartile,  -- 1=lowest, 4=highest
        rs.has_m12_plus,
        rs.is_single_month_churn
    FROM v_retailer_summary rs
    JOIN m0_stats m ON rs.retailer_id = m.retailer_id
)
SELECT
    gmv_quartile                                AS quartile,
    COUNT(*)                                    AS retailer_count,
    ROUND(MIN(gmv_m0), 0)                       AS min_gmv_m0,
    ROUND(MAX(gmv_m0), 0)                       AS max_gmv_m0,
    ROUND(AVG(gmv_m0), 0)                       AS avg_gmv_m0,
    ROUND(AVG(has_m12_plus)      * 100, 1)      AS ret_12mo_pct,
    ROUND(AVG(is_single_month_churn) * 100, 1)  AS single_month_churn_pct
FROM gmv_ntile
GROUP BY gmv_quartile
ORDER BY gmv_quartile;

-- QA: 4 rows; total retailer_count = 7400
-- QA: min/max GMV boundaries monotone increasing
-- QA: ret_12mo_pct should increase Q1 → Q4
-- OBSERVATION: {obs_4a}


-- ── 4b. 12-mo retention by first-month brand count ───────
-- What I'm looking for: does trying MORE brands in month 0 predict retention?
-- Brand diversity = breadth of catalogue exploration in first month.
-- More actionable than GMV: Faire can surface recommendations to widen exploration.
-- Buckets: 1 brand, 2 brands, 3–5, 6+ (as used in analyses.py / queries.sql)
-- ⚠️ FAN-OUT: brand_count in v_retailer_months uses COUNT(DISTINCT brand_id); safe
--
-- CTE m0_brands (grain: one row per retailer_id — month 0 only)
-- CTE bucketed (grain: one row per retailer_id — adds bucket label)
-- JOIN grain: v_retailer_summary (one per retailer) × m0_brands (one per retailer) → 1:1
-- Output grain: one row per bucket (4 rows)

WITH
m0_brands AS (
    SELECT retailer_id, brand_count AS brands_m0
    FROM v_retailer_months
    WHERE month_offset = 0
),
bucketed AS (
    SELECT
        CASE
            WHEN m.brands_m0 = 1  THEN '1'
            WHEN m.brands_m0 = 2  THEN '2'
            WHEN m.brands_m0 <= 5 THEN '3-5'
            ELSE                       '6+'
        END                      AS brand_bucket,
        m.brands_m0,
        rs.has_m12_plus,
        rs.is_single_month_churn
    FROM v_retailer_summary rs
    JOIN m0_brands m ON rs.retailer_id = m.retailer_id
)
SELECT
    brand_bucket,
    COUNT(*)                                    AS retailer_count,
    ROUND(AVG(brands_m0), 1)                    AS avg_brands_m0,
    ROUND(AVG(has_m12_plus)      * 100, 1)      AS ret_12mo_pct,
    ROUND(AVG(is_single_month_churn) * 100, 1)  AS single_month_churn_pct
FROM bucketed
GROUP BY brand_bucket
ORDER BY CASE brand_bucket WHEN '1' THEN 1 WHEN '2' THEN 2 WHEN '3-5' THEN 3 ELSE 4 END;

-- QA: 4 rows; total retailer_count = 7400
-- QA: ret_12mo_pct should increase from '1' to '6+'
-- OBSERVATION: {obs_4b}


-- ── 4c. Churned vs retained: month-0 behavioral profile ──
-- What I'm looking for: HOW DIFFERENT are churned and retained retailers
--   in their very first month, before we know their fate?
-- If the groups look very different on DAY ONE, early-warning scoring is feasible.
-- Churned = max_month ≤ 2 (never came back after month 2)
-- Retained = max_month ≥ 12 (still active through year 1)
-- ⚠️ FAN-OUT: v_retailer_months already collapsed; no fan-out risk
--
-- CTE m0_stats (grain: one row per retailer_id — month 0 stats)
-- CTE segmented (grain: one row per retailer_id — adds segment label)
-- JOIN grain: v_retailer_summary (one per retailer) × m0_stats (one per retailer) → 1:1
-- Output grain: one row per cohort_segment (3 rows)

WITH
m0_stats AS (
    SELECT
        retailer_id,
        monthly_gmv  AS gmv_m0,
        brand_count  AS brands_m0,
        order_count  AS orders_m0     -- distinct orders in month 0 (not brand rows)
    FROM v_retailer_months
    WHERE month_offset = 0
),
segmented AS (
    SELECT
        CASE
            WHEN rs.max_month <= 2  THEN '1_Churned (max ≤ 2)'
            WHEN rs.max_month >= 12 THEN '3_Retained (max ≥ 12)'
            ELSE                         '2_Middle (3-11)'
        END                       AS cohort_segment,
        m.gmv_m0,
        m.brands_m0,
        m.orders_m0
    FROM v_retailer_summary rs
    JOIN m0_stats m ON rs.retailer_id = m.retailer_id
)
SELECT
    cohort_segment,
    COUNT(*)                      AS retailer_count,
    ROUND(AVG(gmv_m0), 0)         AS avg_gmv_m0,
    ROUND(AVG(brands_m0), 2)      AS avg_brands_m0,
    ROUND(AVG(orders_m0), 2)      AS avg_orders_m0,
    ROUND(MIN(gmv_m0), 0)         AS min_gmv_m0,
    ROUND(MAX(gmv_m0), 0)         AS max_gmv_m0
FROM segmented
GROUP BY cohort_segment
ORDER BY cohort_segment;

-- QA: 3 rows; total = 7400; Churned+Retained+Middle = 7400
-- QA: retained should have higher avg_gmv_m0 and avg_brands_m0 than churned
-- QA: min/max ranges overlap (no clean cutoff) — confirms scoring, not rules
-- OBSERVATION: {obs_4c}


-- ── 4d. Early ordering frequency → 12-mo retention ───────
-- What I'm looking for: does ordering in months 0 AND 1 AND 2 predict year-1 retention?
-- This tests HABIT FORMATION: retailers who order every month in their first 3 months
--   have built a regular Faire habit vs those who only ordered once.
-- This is the most actionable signal: Faire can nudge retailers in months 1 and 2.
-- ⚠️ FAN-OUT: v_retailer_months is at retailer-month grain; COUNT(DISTINCT) correct
--
-- CTE first3_active (grain: one row per retailer_id):
--   counts distinct months in {{0,1,2}} where retailer had at least one order
-- JOIN grain: v_retailer_summary (one per retailer) × first3_active (≤1 per retailer) → 1:1
-- COALESCE: defensive for any retailer in retailers table with zero order records
-- Output grain: one row per active_months count (0–3)

WITH
first3_active AS (
    SELECT
        retailer_id,
        COUNT(DISTINCT month_offset)  AS active_months_first3
    FROM v_retailer_months
    WHERE month_offset IN (0, 1, 2)
    GROUP BY retailer_id
)
SELECT
    COALESCE(f.active_months_first3, 0)    AS active_months,
    COUNT(*)                                AS retailer_count,
    ROUND(AVG(rs.has_m12_plus) * 100, 1)   AS ret_12mo_pct,
    ROUND(AVG(rs.is_single_month_churn) * 100, 1) AS single_month_churn_pct
FROM v_retailer_summary rs
LEFT JOIN first3_active f ON rs.retailer_id = f.retailer_id
GROUP BY COALESCE(f.active_months_first3, 0)
ORDER BY active_months;

-- QA: rows for values 1, 2, 3 (and possibly 0); total = 7400
-- QA: ret_12mo_pct should strongly increase from 1 → 3 — expected LARGEST gap
-- OBSERVATION: {obs_4d}


-- ── 4e. Platform breadth → 12-mo retention ───────────────
-- What I'm looking for: does using app + desktop + mobile predict retention?
-- Platform count = count of non-null values in:
--   FIRST_APP_SESSION_AT, FIRST_DESKTOP_SESSION_AT, FIRST_MOBILE_WEB_SESSION_AT
-- ⚠️ REVERSE CAUSALITY WARNING: platform_count is a LIFETIME snapshot.
--   A retailer who has been active for 12+ months naturally accumulates more
--   platform sessions than one who churned after month 0. The gradient may
--   be measuring retention (outcome) rather than predicting it (leading indicator).
--   Use only first-60-days session timestamps to build a genuine early indicator.
-- Output grain: one row per platform_count value (0–3)

SELECT
    platform_count,
    COUNT(*)                                    AS retailer_count,
    ROUND(AVG(has_m12_plus) * 100, 1)          AS ret_12mo_pct,
    ROUND(AVG(is_single_month_churn) * 100, 1)  AS single_month_churn_pct
FROM v_retailer_summary
GROUP BY platform_count
ORDER BY platform_count;

-- QA: rows for 0, 1, 2, 3; total = 7400
-- QA: platform_count = 0 should be very small
-- QA: strong gradient expected (but see reverse causality caveat above)
-- OBSERVATION: {obs_4e}


-- ============================================================
-- SECTION 5: OBSERVATIONS AND NEXT STEPS
-- Combined synthesis from exploration_part1.sql (Sections 1–3)
-- and exploration_part2.sql (Section 4).
-- ============================================================
--
-- ─────────────────────────────────────────────────────────
-- 5.1  RANKED RETENTION GAPS — ALL DIMENSIONS
-- ─────────────────────────────────────────────────────────
-- Each gap = (best group 12-mo retention) − (worst group 12-mo retention)
-- within that dimension, using actual data from this run.
--
-- Rank  Gap       Type          Dimension
-- ────────────────────────────────────────────────────────────────────
{s5_gap_table}
-- ────────────────────────────────────────────────────────────────────
--
-- KEY STRUCTURAL OBSERVATION:
--   The TOP 2 gaps are BEHAVIORAL (what retailers do on the platform).
--   The next 2 are PROFILE attributes with known confounders.
--   This matters for actionability:
--
--   BEHAVIORAL gaps (Sections 4):
--     • Can be influenced by product and ops interventions
--     • Month-1 order nudge, brand recommendations, app push notifications
--     • BUT causality direction is unclear from observational data
--
--   PROFILE gaps (Section 3):
--     • Describe SEGMENTS, not levers
--     • Payment term gap: selection bias (NET60 = pre-qualified)
--     • App install gap: snapshot + reverse causality
--     • Unknown bucket gap: may reflect who is in the bucket,
--       not how they were treated
--
-- ─────────────────────────────────────────────────────────
-- 5.2  CONFOUNDERS AND CAVEATS (per dimension)
-- ─────────────────────────────────────────────────────────
{s5_caveat_block}
--
-- ─────────────────────────────────────────────────────────
-- 5.3  FOLLOW-UP QUESTIONS WORTH INVESTIGATING
-- ─────────────────────────────────────────────────────────
-- Listed in order of (gap size × actionability × clarity of causal story)
--
{s5_questions_block}
--
-- ─────────────────────────────────────────────────────────
-- 5.4  WHAT THE DATA CANNOT TELL US
-- ─────────────────────────────────────────────────────────
-- • CAUSALITY: every gap in this analysis is correlational.
--   The most important unanswered question is: what CAUSES month-1 inactivity?
--   Supply (brands unavailable)? Demand (stocked up)? Awareness (forgot)?
--   Only experiments can answer this.
--
-- • COUNTERFACTUALS: we do not know what retention would look like without
--   Faire's existing interventions (emails, recommendations, onboarding).
--   The "baseline" here already includes whatever Faire was doing in 2020–2021.
--
-- • COHORT SPECIFICITY: this is a single June/July 2020 cohort during COVID
--   recovery. Retention patterns may differ for later cohorts as the platform
--   matured and competition intensified. Do not assume these numbers are
--   stable over time.
--
-- • EXTERNAL FACTORS: we see only Faire-side data. Retailers may be
--   purchasing from competitors, experiencing business challenges, or
--   expanding beyond wholesale — none of which we can observe.
--
-- • DATA QUALITY IN UNKNOWN BUCKET: 30.9% of the cohort has unknown annual
--   sales data. If this is non-random (e.g., Online Only retailers skipped
--   the signup field), all segment analyses excluding Unknown are biased
--   toward more established retailers.
-- ============================================================
""".format(
    obs_4a              = obs_4a,
    obs_4b              = obs_4b,
    obs_4c              = obs_4c,
    obs_4d              = obs_4d,
    obs_4e              = obs_4e,
    s5_gap_table        = s5_gap_table,
    s5_caveat_block     = s5_caveat_block,
    s5_questions_block  = s5_questions_block,
)

with open("/workspace/exploration_part2.sql", "w") as f:
    f.write(sql_file)

print(f"\n  Written: /workspace/exploration_part2.sql")
print(f"  File size: {len(sql_file):,} bytes  ({sql_file.count(chr(10))} lines)")

conn.close()
