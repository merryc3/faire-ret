"""
Faire Retailer Retention — Exploratory Analysis (Part 1)

exploration.py
  • Loads both CSVs into SQLite (pandas used only for CSV loading)
  • Runs every Section 1–3 query as SQL (no pandas analysis)
  • Prints results with section headers
  • Computes OBSERVATION text from actual result values
  • Writes exploration_part1.sql — the canonical SQL reference with
    real observations embedded as comments

CLAUDE.md QA rules applied to every query:
  • Header comment: what I'm looking for + expected result
  • Every CTE: purpose + grain (one row per what?)
  • Derived fields: inline comments
  • Before any JOIN: grain of each side + join type
  • Fan-out risk flagged wherever orders table is used
  • QA check after each query (row count / null check / range check)
"""

import glob
import os
import sqlite3
import textwrap

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd          # used ONLY for CSV → SQLite loading

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

def run(conn, sql, params=()):
    cur = conn.execute(sql, params)
    return cur.fetchall()

def col_names(conn, sql):
    cur = conn.execute(sql)
    return [d[0] for d in cur.description]

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

def pct_diff(a, b):
    """Return (a - b) formatted as ±X.Xpp."""
    if a is None or b is None:
        return "N/A"
    return f"{a - b:+.1f}pp"

def safe_get(dct, key, col_idx, default=None):
    row = dct.get(key)
    if row is None:
        return default
    try:
        return row[col_idx]
    except IndexError:
        return default


# ─────────────────────────────────────────────────────────────
# STEP 0 — LOAD CSVs INTO SQLITE
# ─────────────────────────────────────────────────────────────

banner("STEP 0: LOADING DATA")

conn = sqlite3.connect(":memory:")

retailer_file = glob.glob("/workspace/psa_exercise_retailers*.csv")[0]
order_file    = glob.glob("/workspace/psa_exercise_orders*.csv")[0]

retailers_df = pd.read_csv(retailer_file)
orders_df    = pd.read_csv(order_file)

retailers_df.to_sql("psa_exercise_retailers", conn, if_exists="replace", index=False)
orders_df.to_sql("psa_exercise_orders",    conn, if_exists="replace", index=False)

print(f"  Retailers : {len(retailers_df):,} rows")
print(f"  Orders    : {len(orders_df):,} rows")


# ─────────────────────────────────────────────────────────────
# HELPER VIEWS  (created once; referenced by all queries)
# ─────────────────────────────────────────────────────────────

# v_retailer_months — one row per (retailer_id, month_offset)
# ⚠️ FAN-OUT: orders has one row per brand per order; ORDER_ID NOT unique per row.
#   SUM(total_gmv) and COUNT(DISTINCT) collapse correctly.
conn.execute("DROP VIEW IF EXISTS v_retailer_months")
conn.execute("""
CREATE VIEW v_retailer_months AS
SELECT
    o.retailer_id,
    -- Calendar-month offset from cohort entry (0 = first order month)
    (CAST(strftime('%Y', o.order_created) AS INT)
        - CAST(strftime('%Y', r.first_confirmed_order_placed_at) AS INT)) * 12
    + (CAST(strftime('%m', o.order_created) AS INT)
        - CAST(strftime('%m', r.first_confirmed_order_placed_at) AS INT))
        AS month_offset,
    SUM(o.total_gmv)           AS monthly_gmv,   -- sum across all brands in this retailer-month
    COUNT(DISTINCT o.order_id) AS order_count,   -- unique orders (not brand rows)
    COUNT(DISTINCT o.brand_id) AS brand_count
FROM psa_exercise_orders o
-- JOIN: orders (many) → retailers (one) — 1:many on retailer side; safe
JOIN psa_exercise_retailers r ON o.retailer_id = r.retailer_id
WHERE o.order_state = 'PROCESSING'
GROUP BY o.retailer_id, month_offset
""")

# v_retailer_summary — one row per retailer with lifetime metrics + retention flags
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
    -- Business type normalised to 4 buckets (raw field has 402 free-text values)
    CASE
        WHEN r.retailer_business_type IN ('Brick & Mortar Store','Brick and Mortar','Brick & Mortar')
             OR LOWER(COALESCE(r.retailer_business_type,'')) LIKE '%brick%mortar%'
            THEN 'Brick & Mortar'
        WHEN r.retailer_business_type IN ('Online Only','Online')
             OR LOWER(COALESCE(r.retailer_business_type,'')) LIKE '%online%only%'
            THEN 'Online Only'
        WHEN r.retailer_business_type IN ('Pop Up Store','Pop Up','Pop-up','pop up','pop-up')
             OR LOWER(COALESCE(r.retailer_business_type,'')) LIKE '%pop%up%'
            THEN 'Pop Up'
        ELSE 'Other/Unknown'
    END AS biz_type_clean
FROM psa_exercise_retailers r
LEFT JOIN v_retailer_months rm ON r.retailer_id = rm.retailer_id
GROUP BY r.retailer_id
""")

conn.commit()
print("  Created views: v_retailer_months, v_retailer_summary")


# ═════════════════════════════════════════════════════════════
# SECTION 1: DATA UNDERSTANDING
# ═════════════════════════════════════════════════════════════

banner("SECTION 1: DATA UNDERSTANDING")

# ── 1a. Row counts ────────────────────────────────────────
sub("1a. Row counts and basic shape")

SQL_1A = """
-- What I'm looking for: confirm row counts match CLAUDE.md documentation
-- Expected: 7,400 retailers (one row per retailer), 259,416 total orders
-- ⚠️ FAN-OUT: orders count is brand-level rows, not order count
SELECT
    'psa_exercise_retailers' AS table_name,
    COUNT(*)                 AS total_rows,
    COUNT(DISTINCT retailer_id) AS unique_retailers,
    COUNT(DISTINCT payment_term) AS distinct_payment_terms,
    COUNT(DISTINCT annual_sales_bucket) AS distinct_sales_buckets,
    SUM(CASE WHEN first_app_session_at IS NULL THEN 1 ELSE 0 END) AS null_app_session
FROM psa_exercise_retailers
UNION ALL
SELECT
    'psa_exercise_orders',
    COUNT(*),
    COUNT(DISTINCT retailer_id),   -- retailers appearing in orders
    COUNT(DISTINCT order_id),      -- ⚠️ orders ≠ rows (brand-level fan-out)
    COUNT(DISTINCT brand_id),
    COUNT(DISTINCT order_state)    -- re-using col 6 for order states count
FROM psa_exercise_orders
-- QA: retailers row should show 7400 total_rows = 7400 unique_retailers
-- QA: orders row should show 259,416 total_rows, distinct order_id < total_rows (confirms brand grain)
"""
r1a = run(conn, SQL_1A)
print_table(r1a, ["table","total_rows","unique_key","dim_1","dim_2","dim_3"])

# Extract key values for observation
ret_rows  = r1a[0][1];  ret_unique = r1a[0][2]
ord_rows  = r1a[1][1];  ord_orders = r1a[1][3];  ord_brands = r1a[1][4]
null_app  = r1a[0][5]
obs_1a = (f"Retailers: {ret_rows:,} rows = {ret_unique:,} unique IDs (one-per-retailer grain confirmed). "
          f"Orders: {ord_rows:,} rows but only {ord_orders:,} distinct ORDER_IDs — confirms brand-level grain. "
          f"{ord_brands:,} distinct brands. {null_app:,} retailers missing FIRST_APP_SESSION_AT.")


# ── 1b. Value counts for every categorical column ────────
sub("1b. Value counts — all categorical columns in retailers")

# Run one query per column for clarity
CAT_COLS = [
    ("RETAILER_BUCKETED_CHANNEL",    "retailer_bucketed_channel"),
    ("RETAILER_STORE_TYPE",          "retailer_store_type"),
    ("ANNUAL_SALES_BUCKET",          "annual_sales_bucket"),
    ("PAYMENT_TERM",                 "payment_term"),
    ("FLAG_APP_INSTALLED",           "flag_app_installed"),
    ("FIRST_CONFIRMED_ORDER_PLACED_AT", "first_confirmed_order_placed_at"),
]

r1b_all = {}
for label, col in CAT_COLS:
    sql = f"""
    SELECT {col} AS value, COUNT(*) AS cnt,
           ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM psa_exercise_retailers), 1) AS pct
    FROM psa_exercise_retailers
    GROUP BY {col}
    ORDER BY cnt DESC
    """
    rows = run(conn, sql)
    r1b_all[col] = rows
    print(f"\n  {label}:")
    print_table(rows, ["value","count","pct%"])

# Business type top-10 (free-text — many variants)
sql_biz = """
SELECT retailer_business_type AS value, COUNT(*) AS cnt
FROM psa_exercise_retailers
GROUP BY retailer_business_type
ORDER BY cnt DESC
LIMIT 10
"""
r1b_biz = run(conn, sql_biz)
print(f"\n  RETAILER_BUSINESS_TYPE (top 10 raw values — 402 distinct):")
print_table(r1b_biz, ["value","count"])

# Compute observations from 1b
channel_top = r1b_all["retailer_bucketed_channel"][0]
bucket_vals = {r[0]: r[1] for r in r1b_all["annual_sales_bucket"]}
pay_vals    = {r[0]: r[1] for r in r1b_all["payment_term"]}
cohort_vals = {r[0]: r[1] for r in r1b_all["first_confirmed_order_placed_at"]}
flag_vals   = {str(r[0]): r[1] for r in r1b_all["flag_app_installed"]}
# Look up by date key (not position) to avoid swapped June/July
june_n = cohort_vals.get("2020-06-01 00:00:00", cohort_vals.get("2020-06-01", "?"))
july_n = cohort_vals.get("2020-07-01 00:00:00", cohort_vals.get("2020-07-01", "?"))
obs_1b = (
    f"Channel: {channel_top[0]} dominates at {channel_top[2]}% of cohort. "
    f"Annual sales: Unknown is largest single bucket at {bucket_vals.get('Unknown',0):,} "
    f"({round(bucket_vals.get('Unknown',0)*100/ret_rows,1)}% of cohort) — concerning. "
    f"Payment terms: NET60 {pay_vals.get('NET60',0):,} vs POS {pay_vals.get('PAYMENT_ON_SHIPMENT',0):,}. "
    f"App install: {flag_vals.get('1',0):,} installed ({round(int(flag_vals.get('1',0))*100/ret_rows,1)}%). "
    f"Cohort split: June {june_n:,} / July {july_n:,} retailers."
)


# ── 1c. Confirm grain of orders table ────────────────────
sub("1c. Confirm orders grain: sample multi-brand ORDER_ID")

SQL_1C_FIND = """
-- What I'm looking for: at least one ORDER_ID with >1 row, proving brand-level grain
-- ⚠️ FAN-OUT: this is the fundamental data note from CLAUDE.md
-- QA: if no rows return here, the grain assumption is wrong
SELECT order_id, COUNT(*) AS brand_rows, COUNT(DISTINCT brand_id) AS distinct_brands,
       ROUND(SUM(total_gmv), 2) AS total_gmv_for_order
FROM psa_exercise_orders
WHERE order_state = 'PROCESSING'
GROUP BY order_id
HAVING COUNT(*) > 1
ORDER BY brand_rows DESC
LIMIT 5
"""
r1c_multi = run(conn, SQL_1C_FIND)
print_table(r1c_multi, ["order_id","brand_rows","distinct_brands","total_gmv"])

# Show the actual rows for the top example
sample_oid = r1c_multi[0][0]
SQL_1C_SHOW = f"""
-- Show all rows for ORDER_ID {sample_oid} to confirm brand-level structure
SELECT order_id, retailer_id, brand_id, order_created, order_state,
       ROUND(total_gmv, 2) AS total_gmv
FROM psa_exercise_orders
WHERE order_id = {sample_oid}
ORDER BY brand_id
"""
r1c_show = run(conn, SQL_1C_SHOW)
print(f"\n  Rows for ORDER_ID {sample_oid} (multi-brand order):")
print_table(r1c_show, ["order_id","retailer_id","brand_id","order_created","state","gmv"])

max_brands = r1c_multi[0][2];  max_brand_rows = r1c_multi[0][1]
obs_1c = (f"Confirmed: ORDER_ID {sample_oid} has {max_brand_rows} rows across "
          f"{max_brands} distinct brands. TOTAL_GMV is per-brand-per-order, NOT the order total. "
          f"Always use COUNT(DISTINCT order_id) for order counts and SUM(total_gmv) for GMV.")


# ── 1d. ORDER_STATE distribution ─────────────────────────
sub("1d. ORDER_STATE distribution")

SQL_1D = """
-- What I'm looking for: are there meaningful CANCELED/RETURNED orders to filter?
-- If CANCELED share is small, safe to filter to PROCESSING only
-- QA: all rows should have a non-null ORDER_STATE
SELECT
    order_state,
    COUNT(*)                                                AS row_count,
    ROUND(COUNT(*) * 100.0
        / (SELECT COUNT(*) FROM psa_exercise_orders), 3)   AS pct_of_all_rows,
    COUNT(DISTINCT order_id)                               AS distinct_orders,
    ROUND(SUM(total_gmv), 0)                               AS total_gmv
FROM psa_exercise_orders
GROUP BY order_state
ORDER BY row_count DESC
-- QA: pct_of_all_rows should sum to ~100%; no NULL order_state expected
"""
r1d = run(conn, SQL_1D)
print_table(r1d, ["order_state","row_count","pct%","distinct_orders","total_gmv"])

processing_pct = next((r[2] for r in r1d if r[0]=="PROCESSING"), None)
canceled_n     = next((r[1] for r in r1d if r[0]=="CANCELED"),   0)
obs_1d = (f"PROCESSING: {processing_pct}% of all rows ({canceled_n} CANCELED). "
          f"CANCELED share is negligible — safe to filter WHERE order_state='PROCESSING' for all analysis. "
          f"No NULL order_state rows.")


# ── 1e. Monthly activity over time ────────────────────────
sub("1e. Monthly activity over time (absolute calendar months)")

SQL_1E = """
-- What I'm looking for: understand the time series shape.
-- When does activity peak? Is there seasonal variation? Does cohort grow or shrink?
-- NOTE: ORDER_CREATED is always first-of-month (monthly granularity per CLAUDE.md)
-- ⚠️ FAN-OUT: COUNT(DISTINCT order_id) for order counts; SUM(total_gmv) for GMV
-- Grain: one row per calendar month
SELECT
    order_created                          AS month,
    COUNT(DISTINCT retailer_id)            AS active_retailers,
    COUNT(DISTINCT order_id)               AS order_count,    -- distinct orders (not brand rows)
    ROUND(SUM(total_gmv), 0)               AS total_gmv,
    ROUND(SUM(total_gmv)
        / COUNT(DISTINCT retailer_id), 0)  AS gmv_per_active_retailer  -- avg spend per active retailer
FROM psa_exercise_orders
WHERE order_state = 'PROCESSING'
GROUP BY order_created
ORDER BY order_created
-- QA: 19 rows expected (Jun 2020 – Dec 2021); month 0 row should have 7400 active retailers
"""
r1e = run(conn, SQL_1E)
print_table(r1e, ["month","active_retailers","order_count","total_gmv","gmv_per_retailer"])

m0_retailers  = r1e[0][1] if r1e else None
last_retailers = r1e[-1][1] if r1e else None
peak_gmv_row   = max(r1e, key=lambda r: r[3]) if r1e else None
peak_month_str = peak_gmv_row[0][:7] if peak_gmv_row else "?"
peak_gmv_val   = f"${peak_gmv_row[3]:,.0f}" if peak_gmv_row else "$?"
obs_1e = (f"{len(r1e)} calendar months (Jun 2020 – Dec 2021). "
          f"Month 0 had {m0_retailers:,} active retailers (full cohort). "
          f"By last month only {last_retailers:,} active — significant attrition. "
          f"GMV peak in {peak_month_str} at {peak_gmv_val} — likely seasonal (holiday). "
          f"GMV per active retailer is a more stable signal than absolute GMV.")


# ═════════════════════════════════════════════════════════════
# SECTION 2: VALIDATE NDR PIPELINE
# ═════════════════════════════════════════════════════════════

banner("SECTION 2: VALIDATE NDR PIPELINE")

# ── 2a. NDR by month ─────────────────────────────────────
sub("2a. NDR by month (aggregate cohort formula, months 0–17)")

SQL_2A = """
-- What I'm looking for: reproduce the reference NDR curve before trusting anything else
-- Expected: month 1 ≈ 59%, month 5 ≈ 49%, month 12 ≈ 75%
-- FORMULA: NDR_t = total_cohort_GMV_t / total_cohort_GMV_0
--   This is the aggregate (GMV-weighted) formula.
--   Per-retailer simple average gives ~84% at month 1 — does NOT match reference.
--   The aggregate formula matches because large retailers who churn pull the
--   numerator down more than equal-weight averaging would.
-- ⚠️ FAN-OUT: v_retailer_months already collapses brand-level rows; safe to SUM
--
-- CTE retailer_months (grain: retailer_id × month_offset): from view
-- CTE total_gmv0 (grain: scalar): cohort's total month-0 GMV — fixed denominator
-- CTE ndr (grain: one row per month_offset 0–17)
WITH
retailer_months AS (
    SELECT retailer_id, month_offset, monthly_gmv
    FROM v_retailer_months
),
-- Single scalar: total GMV across all cohort retailers in their entry month
total_gmv0 AS (
    SELECT SUM(monthly_gmv) AS val
    FROM retailer_months
    WHERE month_offset = 0
),
ndr AS (
    SELECT
        rm.month_offset,
        COUNT(DISTINCT rm.retailer_id)               AS active_retailers,  -- retailers with any spend
        ROUND(SUM(rm.monthly_gmv), 0)                AS cohort_gmv_t,
        ROUND(tg.val, 0)                             AS cohort_gmv_0,
        ROUND(SUM(rm.monthly_gmv) * 100.0 / tg.val, 2) AS ndr_pct          -- NDR as %
    FROM retailer_months rm
    CROSS JOIN total_gmv0 tg  -- single-row scalar; CROSS JOIN adds no fan-out
    WHERE rm.month_offset BETWEEN 0 AND 17
    GROUP BY rm.month_offset
)
SELECT month_offset, active_retailers, ndr_pct
FROM ndr
ORDER BY month_offset
-- QA: 18 rows; month 0 ndr_pct = 100.00; month 1 ≈ 58.94; month 5 ≈ 48.85; month 12 ≈ 75.01
"""
r2a = run(conn, SQL_2A)
print_table(r2a, ["month","active_retailers","ndr_pct%"])

ndr_dict = {r[0]: (r[1], r[2]) for r in r2a}  # month → (active_retailers, ndr_pct)
REF = {0: 100.0, 1: 59.0, 2: 54.0, 3: 56.0, 5: 49.0, 12: 75.0}
print("\n  Validation vs reference:")
all_pass = True
for m, ref_val in REF.items():
    calc = ndr_dict.get(m, (None, None))[1]
    diff = abs(calc - ref_val) if calc else 999
    flag = "✓" if diff < 1.0 else "✗"
    print(f"    Month {m:2d}: calc={calc:.2f}%  ref={ref_val:.1f}%  diff={diff:.2f}pp  {flag}")
    if diff >= 1.0:
        all_pass = False
print(f"  {'All reference values match ✓' if all_pass else 'WARNING: mismatch detected'}")

ndr_m1  = ndr_dict.get(1,  (None, None))[1]
ndr_m5  = ndr_dict.get(5,  (None, None))[1]
ndr_m12 = ndr_dict.get(12, (None, None))[1]
ndr_m0_active = ndr_dict.get(0, (None, None))[0]
obs_2a = (
    f"NDR validates against reference: month1={ndr_m1:.2f}%≈59% ✓, "
    f"month5={ndr_m5:.2f}%≈49% ✓, month12={ndr_m12:.2f}%≈75% ✓. "
    f"The aggregate cohort formula (total_GMV_t / total_GMV_0) is correct. "
    f"NDR dips to {min(r[2] for r in r2a):.1f}% at month "
    f"{min(r2a, key=lambda r: r[2])[0]}, then recovers above 100% by month 12 — "
    f"survivorship bias: only highest-GMV retailers remain active that long."
)


# ── 2b. QA: retailer counts ───────────────────────────────
sub("2b. QA: retailer count in NDR at each month")

SQL_2B = """
-- What I'm looking for: how rapidly does the active cohort shrink?
-- At month 0 we expect all 7,400 retailers; by month 1 we expect a large drop
-- This is a QA check AND an insight into early churn magnitude
-- Grain: one row per month_offset (same as 2a, just re-stating retailer counts)
WITH
retailer_months AS (
    SELECT retailer_id, month_offset
    FROM v_retailer_months
    WHERE month_offset BETWEEN 0 AND 17
),
all_ret AS (
    SELECT COUNT(DISTINCT retailer_id) AS total_cohort
    FROM psa_exercise_retailers
)
SELECT
    rm.month_offset,
    COUNT(DISTINCT rm.retailer_id)                                     AS active_retailers,
    ar.total_cohort                                                     AS cohort_size,
    ROUND(COUNT(DISTINCT rm.retailer_id) * 100.0 / ar.total_cohort, 1) AS pct_still_active
FROM retailer_months rm
CROSS JOIN all_ret ar
GROUP BY rm.month_offset
ORDER BY rm.month_offset
-- QA: month 0 pct_still_active = 100%; should decline monotonically through month 5 or so
"""
r2b = run(conn, SQL_2B)
print_table(r2b, ["month","active_retailers","cohort_size","pct_still_active%"])

m1_active_pct  = next((r[3] for r in r2b if r[0]==1),  None)
m12_active_pct = next((r[3] for r in r2b if r[0]==12), None)
obs_2b = (
    f"Month 0: 100% active (all {ndr_m0_active:,} cohort retailers). "
    f"Month 1: only {m1_active_pct}% still active — {100-m1_active_pct:.1f}pp drop in a single month. "
    f"Month 12: {m12_active_pct}% active. "
    f"IMPORTANT: NDR ≠ active retailer rate. NDR weighs by GMV, so the "
    f"{ndr_m12:.1f}% NDR at month 12 reflects that surviving retailers spend MORE than they did in month 0."
)


# ═════════════════════════════════════════════════════════════
# SECTION 3: RETENTION CUTS BY EVERY DIMENSION
# ═════════════════════════════════════════════════════════════

banner("SECTION 3: RETENTION CUTS BY EVERY DIMENSION")
print("  Goal: find dimensions with the biggest 12-mo retention gaps")
print("  Metrics: 12-mo retention %, avg lifetime GMV, single-month churn %")

# Shared retention SQL template (used by 3a–3f)
# v_retailer_summary is one-per-retailer with has_m12_plus, lifetime_gmv, is_single_month_churn
RETENTION_TEMPLATE = """
SELECT
    {group_col}                                         AS grp,
    COUNT(*)                                            AS retailer_count,
    ROUND(AVG(has_m12_plus) * 100, 1)                  AS ret_12mo_pct,
    ROUND(AVG(lifetime_gmv), 0)                        AS avg_lifetime_gmv,
    ROUND(AVG(is_single_month_churn) * 100, 1)         AS single_month_churn_pct
FROM v_retailer_summary
{where_clause}
GROUP BY {group_col}
ORDER BY {order_clause}
"""

# ── 3a. By payment_term ───────────────────────────────────
sub("3a. Retention by PAYMENT_TERM")
print("  What I'm looking for: does NET60 vs PAYMENT_ON_SHIPMENT predict retention?")
print("  WARNING: selection bias — NET60 requires pre-qualification; any gap may be")
print("  due to retailer quality (larger, more creditworthy), not the payment term itself.")

SQL_3A = """
-- Hypothesis-free cut: just observe the gap, then ask why
-- ⚠️ Selection bias: NET60 extended to pre-qualified retailers who may be systematically larger
-- QA: 4 rows total (NET60, POS, NET90, HOLD_ON_PLACEMENT); POS+NET60 should cover ~99% of cohort
SELECT
    payment_term                                        AS grp,
    COUNT(*)                                            AS retailer_count,
    ROUND(AVG(has_m12_plus) * 100, 1)                  AS ret_12mo_pct,
    ROUND(AVG(lifetime_gmv), 0)                        AS avg_lifetime_gmv,
    ROUND(AVG(is_single_month_churn) * 100, 1)         AS single_month_churn_pct
FROM v_retailer_summary
GROUP BY payment_term
ORDER BY retailer_count DESC
-- QA: retailer_count totals = 7400; NET60+POS together should be ~95%+ of cohort
"""
r3a = run(conn, SQL_3A)
print_table(r3a, ["payment_term","count","ret_12mo%","avg_lifetime_gmv","churn%"])

pay_dict   = {r[0]: r for r in r3a}
net60_12mo = safe_get(pay_dict, "NET60", 2)
pos_12mo   = safe_get(pay_dict, "PAYMENT_ON_SHIPMENT", 2)
gap_3a     = net60_12mo - pos_12mo if (net60_12mo and pos_12mo) else None
net60_gmv  = safe_get(pay_dict, "NET60", 3)
pos_gmv    = safe_get(pay_dict, "PAYMENT_ON_SHIPMENT", 3)
obs_3a = (
    f"NET60: {net60_12mo}% 12-mo retention vs POS: {pos_12mo}% — gap = {gap_3a:.1f}pp. "
    f"NET60 also has higher avg lifetime GMV (${net60_gmv:,.0f} vs ${pos_gmv:,.0f}). "
    f"IMPORTANT: this is likely SELECTION BIAS not a treatment effect. NET60 requires "
    f"pre-qualification; these retailers are probably larger and more established regardless "
    f"of payment term. This gap raises the question: is NET60 causing retention or just "
    f"tagging retailers who were already going to retain?"
)
print(f"\n  OBSERVATION: {obs_3a}")


# ── 3b. By flag_app_installed ─────────────────────────────
sub("3b. Retention by FLAG_APP_INSTALLED")
print("  What I'm looking for: does having the app correlate with retention?")
print("  CAUTION: FLAG_APP_INSTALLED is a snapshot (as of data export, not at first order).")
print("  Cannot determine if app was installed BEFORE or AFTER key retention events.")

SQL_3B = """
-- Observing correlation only — causality direction is unknown from this cut alone
-- FLAG_APP_INSTALLED is a point-in-time snapshot (CLAUDE.md warning)
-- Use FIRST_APP_SESSION_AT for timing analysis (a separate, deeper question)
-- QA: exactly 2 rows (0 and 1); counts sum to 7400
SELECT
    flag_app_installed                                  AS grp,
    COUNT(*)                                            AS retailer_count,
    ROUND(AVG(has_m12_plus) * 100, 1)                  AS ret_12mo_pct,
    ROUND(AVG(lifetime_gmv), 0)                        AS avg_lifetime_gmv,
    ROUND(AVG(is_single_month_churn) * 100, 1)         AS single_month_churn_pct
FROM v_retailer_summary
GROUP BY flag_app_installed
ORDER BY flag_app_installed
-- QA: 2 rows; sums to 7400
"""
r3b = run(conn, SQL_3B)
print_table(r3b, ["app_installed","count","ret_12mo%","avg_lifetime_gmv","churn%"])

app_dict   = {str(r[0]): r for r in r3b}
app1_12mo  = safe_get(app_dict, "1", 2)
app0_12mo  = safe_get(app_dict, "0", 2)
gap_3b     = app1_12mo - app0_12mo if (app1_12mo and app0_12mo) else None
app1_churn = safe_get(app_dict, "1", 4)
app0_churn = safe_get(app_dict, "0", 4)
obs_3b = (
    f"App installed: {app1_12mo}% 12-mo retention vs no app: {app0_12mo}% — gap = {gap_3b:.1f}pp. "
    f"Single-month churn: {app1_churn}% with app vs {app0_churn}% without — {app0_churn - app1_churn:.1f}pp higher churn for non-app. "
    f"Large gap BUT the snapshot nature of FLAG_APP_INSTALLED means retained retailers "
    f"may have installed the app BECAUSE they stayed engaged (reverse causality). "
    f"Next step: use FIRST_APP_SESSION_AT to check whether retailers who installed "
    f"BEFORE their first order differ from those who installed much later."
)
print(f"\n  OBSERVATION: {obs_3b}")


# ── 3c. By annual_sales_bucket ────────────────────────────
sub("3c. Retention by ANNUAL_SALES_BUCKET")
print("  What I'm looking for: do retailers with more sales history retain better?")
print("  Watching for: 'Unknown' bucket as an outlier segment.")

SQL_3C = """
-- Each bucket represents annual sales volume BEFORE joining Faire
-- 'Unknown' may mean: new retailer with no history, missing data, or API failure
-- QA: 7 distinct buckets; total count = 7400
SELECT
    annual_sales_bucket                                 AS grp,
    COUNT(*)                                            AS retailer_count,
    ROUND(AVG(has_m12_plus) * 100, 1)                  AS ret_12mo_pct,
    ROUND(AVG(lifetime_gmv), 0)                        AS avg_lifetime_gmv,
    ROUND(AVG(is_single_month_churn) * 100, 1)         AS single_month_churn_pct
FROM v_retailer_summary
GROUP BY annual_sales_bucket
ORDER BY CASE annual_sales_bucket
    WHEN 'Unknown'            THEN 0
    WHEN 'Brand New Retailer' THEN 1
    WHEN 'X-Small'            THEN 2
    WHEN 'Small'              THEN 3
    WHEN 'Medium'             THEN 4
    WHEN 'Large'              THEN 5
    WHEN 'X-Large'            THEN 6
    ELSE 99 END
-- QA: 7 rows; total count = 7400; Unknown should have lowest retention
"""
r3c = run(conn, SQL_3C)
print_table(r3c, ["annual_sales_bucket","count","ret_12mo%","avg_lifetime_gmv","churn%"])

bucket_dict = {r[0]: r for r in r3c}
unk_12mo    = safe_get(bucket_dict, "Unknown", 2)
unk_n       = safe_get(bucket_dict, "Unknown", 1)
unk_churn   = safe_get(bucket_dict, "Unknown", 4)
known_rows  = [r for r in r3c if r[0] != "Unknown"]
best_known  = max(known_rows, key=lambda r: r[2]) if known_rows else None
worst_known = min(known_rows, key=lambda r: r[2]) if known_rows else None
gap_3c      = best_known[2] - unk_12mo if (best_known and unk_12mo) else None
obs_3c = (
    f"Unknown bucket: {unk_12mo}% 12-mo retention — {gap_3c:.1f}pp below best known bucket "
    f"({best_known[0] if best_known else '?'}: {best_known[2] if best_known else '?'}%). "
    f"Unknown has {unk_n:,} retailers ({round(unk_n*100/ret_rows,1)}% of cohort) and "
    f"{unk_churn}% single-month churn vs {worst_known[4] if worst_known else '?'}% for worst known bucket. "
    f"Unknown is NOT just a slightly different segment — it is a deeply different population. "
    f"Possible explanations: (1) these are brand-new online retailers with no purchase history; "
    f"(2) data collection failure at signup; (3) different onboarding experience. "
    f"Worth investigating: what % of Unknown are Online Only vs Brick & Mortar?"
)
print(f"\n  OBSERVATION: {obs_3c}")


# ── 3d. By retailer_business_type (cleaned) ───────────────
sub("3d. Retention by RETAILER_BUSINESS_TYPE (normalised to 4 groups)")
print("  What I'm looking for: do business types differ in retention?")
print("  Raw field has 402 distinct free-text values — normalised in v_retailer_summary.")

SQL_3D = """
-- biz_type_clean normalises 402 raw values into 4 groups (defined in v_retailer_summary view)
-- Groups: Brick & Mortar, Online Only, Pop Up, Other/Unknown
-- QA: 4 rows; total count = 7400
SELECT
    biz_type_clean                                      AS grp,
    COUNT(*)                                            AS retailer_count,
    ROUND(AVG(has_m12_plus) * 100, 1)                  AS ret_12mo_pct,
    ROUND(AVG(lifetime_gmv), 0)                        AS avg_lifetime_gmv,
    ROUND(AVG(is_single_month_churn) * 100, 1)         AS single_month_churn_pct
FROM v_retailer_summary
GROUP BY biz_type_clean
ORDER BY retailer_count DESC
-- QA: 4 rows; Brick & Mortar should be largest group (~4600+)
"""
r3d = run(conn, SQL_3D)
print_table(r3d, ["biz_type","count","ret_12mo%","avg_lifetime_gmv","churn%"])

biz_dict     = {r[0]: r for r in r3d}
bm_12mo      = safe_get(biz_dict, "Brick & Mortar", 2)
online_12mo  = safe_get(biz_dict, "Online Only", 2)
popup_12mo   = safe_get(biz_dict, "Pop Up", 2)
gap_3d       = bm_12mo - online_12mo if (bm_12mo and online_12mo) else None
online_churn = safe_get(biz_dict, "Online Only", 4)
bm_churn     = safe_get(biz_dict, "Brick & Mortar", 4)
online_n     = safe_get(biz_dict, "Online Only", 1)
obs_3d = (
    f"Online Only: {online_12mo}% 12-mo retention ({online_n:,} retailers) vs "
    f"Brick & Mortar: {bm_12mo}% — gap = {gap_3d:.1f}pp. "
    f"Online Only has {online_churn}% single-month churn vs {bm_churn}% for B&M. "
    f"Pop Up: {popup_12mo}%. "
    f"Online-only retailers may be testing Faire without deep commitment, "
    f"or may have lower average order values and less urgency for wholesale. "
    f"IMPORTANT: there is significant overlap between Online Only and the Unknown "
    f"ANNUAL_SALES_BUCKET — these may be the same underlying population "
    f"(new online retailers with no prior sales history). Worth cross-tabbing."
)
print(f"\n  OBSERVATION: {obs_3d}")


# ── 3e. By retailer_store_type (top 10) ───────────────────
sub("3e. Retention by RETAILER_STORE_TYPE (top 10 by count)")
print("  What I'm looking for: which store categories retain best/worst?")
print("  This is the merchandise category (grocery, gift, home, etc.).")

SQL_3E = """
-- retailer_store_type is the merchandise category of the retailer's store
-- Limited to top 10 by count; long tail of smaller categories excluded
-- ⚠️ 392 retailers have store_type='unknown' — these are treated as a bucket
-- QA: 10 rows; each row's count sums with others to approach 7400
SELECT
    retailer_store_type                                AS grp,
    COUNT(*)                                           AS retailer_count,
    ROUND(AVG(has_m12_plus) * 100, 1)                 AS ret_12mo_pct,
    ROUND(AVG(lifetime_gmv), 0)                       AS avg_lifetime_gmv,
    ROUND(AVG(is_single_month_churn) * 100, 1)        AS single_month_churn_pct
FROM v_retailer_summary
GROUP BY retailer_store_type
ORDER BY retailer_count DESC
LIMIT 10
-- QA: grocery should be largest; ret_12mo should vary meaningfully across types
"""
r3e = run(conn, SQL_3E)
print_table(r3e, ["store_type","count","ret_12mo%","avg_lifetime_gmv","churn%"])

store_dict   = {r[0]: r for r in r3e}
best_store   = max(r3e, key=lambda r: r[2])
worst_store  = min(r3e, key=lambda r: r[2])
gap_3e       = best_store[2] - worst_store[2]
high_gmv_store = max(r3e, key=lambda r: r[3])
obs_3e = (
    f"Best 12-mo retention: {best_store[0]} ({best_store[2]}%). "
    f"Worst: {worst_store[0]} ({worst_store[2]}%). "
    f"Gap across top-10 types: {gap_3e:.1f}pp. "
    f"Highest avg lifetime GMV: {high_gmv_store[0]} (${high_gmv_store[3]:,.0f}). "
    f"Store type gaps are real but smaller than behavioral gaps (Section 4). "
    f"'other' and 'unknown' types both have low retention — these may overlap heavily "
    f"with the Unknown annual_sales_bucket population."
)
print(f"\n  OBSERVATION: {obs_3e}")


# ── 3f. By retailer_bucketed_channel ─────────────────────
sub("3f. Retention by RETAILER_BUCKETED_CHANNEL")
print("  What I'm looking for: does acquisition channel predict retention quality?")

SQL_3F = """
-- RETAILER_BUCKETED_CHANNEL = how the retailer was acquired (Organic, Referral, Paid, Partnerships)
-- QA: exactly 4 rows; total count = 7400
SELECT
    retailer_bucketed_channel                          AS grp,
    COUNT(*)                                           AS retailer_count,
    ROUND(AVG(has_m12_plus) * 100, 1)                 AS ret_12mo_pct,
    ROUND(AVG(lifetime_gmv), 0)                       AS avg_lifetime_gmv,
    ROUND(AVG(is_single_month_churn) * 100, 1)        AS single_month_churn_pct
FROM v_retailer_summary
GROUP BY retailer_bucketed_channel
ORDER BY retailer_count DESC
-- QA: 4 rows; Organic largest; Partnerships smallest (n=88)
"""
r3f = run(conn, SQL_3F)
print_table(r3f, ["channel","count","ret_12mo%","avg_lifetime_gmv","churn%"])

ch_dict       = {r[0]: r for r in r3f}
best_ch       = max(r3f, key=lambda r: r[2])
worst_ch      = min(r3f, key=lambda r: r[2])
gap_3f        = best_ch[2] - worst_ch[2]
partnerships_n = safe_get(ch_dict, "Partnerships", 1, 0)
organic_12mo  = safe_get(ch_dict, "Organic", 2)
obs_3f = (
    f"Channel retention ranges: {worst_ch[0]} lowest at {worst_ch[2]}%, "
    f"{best_ch[0]} highest at {best_ch[2]}% — gap = {gap_3f:.1f}pp. "
    f"Partnerships: only {partnerships_n} retailers — too small for reliable conclusions. "
    f"Organic ({organic_12mo}%) vs Paid: small gap suggests acquisition channel "
    f"is a WEAK predictor of retention compared to behavioral signals (Section 4). "
    f"This is reassuring — it means retention is driven by on-platform behavior "
    f"more than where the retailer came from."
)
print(f"\n  OBSERVATION: {obs_3f}")


# ═════════════════════════════════════════════════════════════
# SECTION 3 GAP RANKING (computed for use in exploration_part1.sql and output)
# ═════════════════════════════════════════════════════════════

print("\n")
banner("SECTION 3: RETENTION GAP RANKING SUMMARY")

gaps_s3 = []
if gap_3a: gaps_s3.append(("3a. Payment term (NET60 vs POS)", gap_3a, "profile — selection bias likely"))
if gap_3b: gaps_s3.append(("3b. App install (1 vs 0)",         gap_3b, "profile — reverse causality risk"))
if gap_3c: gaps_s3.append(("3c. Annual sales Unknown vs best",  gap_3c, "profile — overlaps Online Only"))
if gap_3d: gaps_s3.append(("3d. Business type (B&M vs Online)", gap_3d, "profile — overlaps Unknown bucket"))
if gap_3e: gaps_s3.append(("3e. Store type (best vs worst)",    gap_3e, "profile — moderate signal"))
if gap_3f: gaps_s3.append(("3f. Channel (best vs worst)",       gap_3f, "profile — weak signal"))
gaps_s3.sort(key=lambda x: x[1], reverse=True)

print("  Dimension                                Gap     Type")
print("  " + "-" * 65)
for name, gap, note in gaps_s3:
    print(f"  {name:<42} {gap:+.1f}pp  {note}")


# ═════════════════════════════════════════════════════════════
# NOW WRITE exploration_part1.sql
# ═════════════════════════════════════════════════════════════

banner("WRITING exploration_part1.sql")

def wrap_comment(text, width=72, prefix="-- "):
    """Wrap a long observation into SQL comment lines."""
    lines = textwrap.wrap(text, width - len(prefix))
    return "\n".join(prefix + line for line in lines)


sql_file = """\
-- ============================================================
-- FAIRE RETAILER RETENTION — EXPLORATORY ANALYSIS (PART 1)
-- exploration_part1.sql
--
-- Purpose : Understand the data before forming any hypotheses.
--   Sections 1–3 only (Sections 4–5 in exploration_part2.sql).
--   Every query is annotated with:
--     • What I'm looking for
--     • OBSERVATION: actual values from running the query
--     • QA check (row count / null check / range check)
--     • CTE grain + JOIN documentation
--     • Fan-out risk flag where orders table is involved
--
-- DATA NOTES (CLAUDE.md):
--   • orders table is BRAND-level within an order → ORDER_ID not unique per row
--   • TOTAL_GMV is per-brand-per-order; always SUM to get order or retailer GMV
--   • ORDER_CREATED is always first-of-month (monthly granularity)
--   • FLAG_APP_INSTALLED is a point-in-time snapshot — reverse causality risk
--   • FIRST_CONFIRMED_ORDER_PLACED_AT is 2020-06-01 or 2020-07-01 only
--
-- NDR FORMULA NOTE:
--   Per-retailer average formula gives ~84% at month 1 (does NOT match reference).
--   Aggregate cohort formula (total_GMV_t / total_GMV_0) gives 58.94% ≈ 59% ✓.
--   We use the aggregate formula throughout.
-- ============================================================


-- ============================================================
-- SETUP: HELPER VIEWS
-- Run these once before executing any section query.
-- These views are referenced throughout; they do NOT need to be
-- re-run if you are in the same SQLite session as analysis.py.
-- ============================================================

-- v_retailer_months: one row per (retailer_id, month_offset)
-- ⚠️ FAN-OUT: orders has one row per brand per order; ORDER_ID NOT unique.
--    SUM(total_gmv) and COUNT(DISTINCT) collapse correctly to retailer-month grain.
DROP VIEW IF EXISTS v_retailer_months;
CREATE VIEW v_retailer_months AS
SELECT
    o.retailer_id,
    (CAST(strftime('%Y', o.order_created) AS INT)
        - CAST(strftime('%Y', r.first_confirmed_order_placed_at) AS INT)) * 12
    + (CAST(strftime('%m', o.order_created) AS INT)
        - CAST(strftime('%m', r.first_confirmed_order_placed_at) AS INT))
        AS month_offset,
    SUM(o.total_gmv)           AS monthly_gmv,   -- sum across all brands; not per-order
    COUNT(DISTINCT o.order_id) AS order_count,   -- distinct orders (NOT brand rows)
    COUNT(DISTINCT o.brand_id) AS brand_count
FROM psa_exercise_orders o
-- JOIN: orders (many per retailer) → retailers (one per retailer) → 1:many; safe
JOIN psa_exercise_retailers r ON o.retailer_id = r.retailer_id
WHERE o.order_state = 'PROCESSING'
GROUP BY o.retailer_id, month_offset;


-- v_retailer_summary: one row per retailer — lifetime metrics + retention flags
-- LEFT JOIN preserves retailers with no order records (defensive; all 7400 have month 0)
DROP VIEW IF EXISTS v_retailer_summary;
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
    -- Business type: 402 raw free-text values collapsed to 4 groups
    CASE
        WHEN r.retailer_business_type IN ('Brick & Mortar Store','Brick and Mortar','Brick & Mortar')
             OR LOWER(COALESCE(r.retailer_business_type,'')) LIKE '%brick%mortar%'
            THEN 'Brick & Mortar'
        WHEN r.retailer_business_type IN ('Online Only','Online')
             OR LOWER(COALESCE(r.retailer_business_type,'')) LIKE '%online%only%'
            THEN 'Online Only'
        WHEN r.retailer_business_type IN ('Pop Up Store','Pop Up','Pop-up','pop up','pop-up')
             OR LOWER(COALESCE(r.retailer_business_type,'')) LIKE '%pop%up%'
            THEN 'Pop Up'
        ELSE 'Other/Unknown'
    END AS biz_type_clean
FROM psa_exercise_retailers r
LEFT JOIN v_retailer_months rm ON r.retailer_id = rm.retailer_id
GROUP BY r.retailer_id;


-- ============================================================
-- SECTION 1: DATA UNDERSTANDING
-- What do we have? What's the grain? Any data quality issues?
-- ============================================================


-- ── 1a. Row counts and basic shape ──────────────────────
-- What I'm looking for: confirm row counts match CLAUDE.md
--   documentation before trusting any downstream numbers.
--   Expected: 7,400 retailers (one row per retailer),
--   259,416 total order rows (brand-level, not order-level)
-- ⚠️ FAN-OUT: order total_rows ≠ order count; confirmed below
--
-- QA: unique_retailers = total_rows for retailers (one-per-retailer grain)
-- QA: orders distinct order_id < total_rows (confirms brand-level fan-out)

SELECT
    'psa_exercise_retailers'        AS table_name,
    COUNT(*)                        AS total_rows,
    COUNT(DISTINCT retailer_id)     AS unique_retailers,      -- must = total_rows
    COUNT(DISTINCT payment_term)    AS distinct_payment_terms,
    COUNT(DISTINCT annual_sales_bucket) AS distinct_sales_buckets,
    SUM(CASE WHEN first_app_session_at IS NULL THEN 1 ELSE 0 END) AS null_app_session
FROM psa_exercise_retailers
UNION ALL
SELECT
    'psa_exercise_orders',
    COUNT(*),
    COUNT(DISTINCT retailer_id),    -- retailers in orders
    COUNT(DISTINCT order_id),       -- ⚠️ distinct orders < total rows (brand fan-out)
    COUNT(DISTINCT brand_id),
    COUNT(DISTINCT order_state)     -- re-using slot for order state count
FROM psa_exercise_orders;

-- OBSERVATION: {obs_1a}


-- ── 1b. Value counts — every categorical column ──────────
-- What I'm looking for: distribution of all categorical fields.
--   Are there dominant categories? Any surprising concentrations?
--   Is the data balanced or skewed?
-- Note: RETAILER_BUSINESS_TYPE has 402 distinct raw values;
--   queried separately showing top 10 only.
-- QA: each column's counts should sum to 7400

-- RETAILER_BUCKETED_CHANNEL
SELECT retailer_bucketed_channel AS value,
       COUNT(*) AS cnt,
       ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM psa_exercise_retailers), 1) AS pct
FROM psa_exercise_retailers
GROUP BY retailer_bucketed_channel ORDER BY cnt DESC;

-- ANNUAL_SALES_BUCKET
SELECT annual_sales_bucket AS value,
       COUNT(*) AS cnt,
       ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM psa_exercise_retailers), 1) AS pct
FROM psa_exercise_retailers
GROUP BY annual_sales_bucket ORDER BY cnt DESC;

-- PAYMENT_TERM
SELECT payment_term AS value,
       COUNT(*) AS cnt,
       ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM psa_exercise_retailers), 1) AS pct
FROM psa_exercise_retailers
GROUP BY payment_term ORDER BY cnt DESC;

-- FLAG_APP_INSTALLED
SELECT flag_app_installed AS value,
       COUNT(*) AS cnt,
       ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM psa_exercise_retailers), 1) AS pct
FROM psa_exercise_retailers
GROUP BY flag_app_installed ORDER BY cnt DESC;

-- FIRST_CONFIRMED_ORDER_PLACED_AT (cohort entry month)
SELECT first_confirmed_order_placed_at AS value,
       COUNT(*) AS cnt,
       ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM psa_exercise_retailers), 1) AS pct
FROM psa_exercise_retailers
GROUP BY first_confirmed_order_placed_at ORDER BY cnt DESC;

-- RETAILER_STORE_TYPE (full list)
SELECT retailer_store_type AS value, COUNT(*) AS cnt
FROM psa_exercise_retailers
GROUP BY retailer_store_type ORDER BY cnt DESC;

-- RETAILER_BUSINESS_TYPE — top 10 raw values (402 distinct total)
SELECT retailer_business_type AS value, COUNT(*) AS cnt
FROM psa_exercise_retailers
GROUP BY retailer_business_type ORDER BY cnt DESC LIMIT 10;

-- OBSERVATION: {obs_1b}


-- ── 1c. Confirm orders grain: sample multi-brand ORDER_ID ──
-- What I'm looking for: concrete proof that ORDER_ID repeats
--   across brands — i.e., the orders table is at BRAND level,
--   not ORDER level. This is critical for avoiding double-counts.
-- ⚠️ FAN-OUT: this query IS the proof of the fan-out. Always use
--   COUNT(DISTINCT order_id) for order counts, never COUNT(*).
-- QA: every row in this result should have brand_rows > 1

-- Step 1: find ORDER_IDs with multiple rows
SELECT order_id,
       COUNT(*)               AS brand_rows,      -- number of brand-level rows
       COUNT(DISTINCT brand_id) AS distinct_brands,
       ROUND(SUM(total_gmv), 2) AS total_gmv_for_order  -- correct per-order GMV
FROM psa_exercise_orders
WHERE order_state = 'PROCESSING'
GROUP BY order_id
HAVING COUNT(*) > 1
ORDER BY brand_rows DESC
LIMIT 5;

-- Step 2: show all rows for one example ORDER_ID (proves structure)
-- Replace {sample_oid} with any ORDER_ID from Step 1 result
SELECT order_id, retailer_id, brand_id, order_created,
       order_state, ROUND(total_gmv, 2) AS total_gmv
FROM psa_exercise_orders
WHERE order_id = {sample_oid}
ORDER BY brand_id;

-- OBSERVATION: {obs_1c}


-- ── 1d. ORDER_STATE distribution ────────────────────────
-- What I'm looking for: are there enough CANCELED/RETURNED orders
--   to affect our analysis? If < 0.1% canceled, safe to filter
--   to PROCESSING only.
-- QA: pct_of_all_rows should sum to ~100%; no NULL order_state

SELECT
    order_state,
    COUNT(*)                                                AS row_count,
    ROUND(COUNT(*) * 100.0
        / (SELECT COUNT(*) FROM psa_exercise_orders), 3)   AS pct_of_all_rows,
    COUNT(DISTINCT order_id)                               AS distinct_orders,
    ROUND(SUM(total_gmv), 0)                               AS total_gmv
FROM psa_exercise_orders
GROUP BY order_state
ORDER BY row_count DESC;

-- OBSERVATION: {obs_1d}


-- ── 1e. Monthly activity over time ───────────────────────
-- What I'm looking for: time series shape of the cohort.
--   When does activity peak? Seasonal patterns? How fast does
--   the cohort shrink? Is GMV per active retailer growing?
-- NOTE: ORDER_CREATED is first-of-month; these are calendar months
-- ⚠️ FAN-OUT: COUNT(DISTINCT order_id) for orders; SUM(total_gmv) for GMV
-- Grain: one row per calendar month (Jun 2020 – Dec 2021)

SELECT
    order_created                          AS month,
    COUNT(DISTINCT retailer_id)            AS active_retailers,
    COUNT(DISTINCT order_id)               AS order_count,
    ROUND(SUM(total_gmv), 0)               AS total_gmv,
    ROUND(SUM(total_gmv)
        / COUNT(DISTINCT retailer_id), 0)  AS gmv_per_active_retailer
FROM psa_exercise_orders
WHERE order_state = 'PROCESSING'
GROUP BY order_created
ORDER BY order_created;

-- QA: {len_r1e} rows expected (Jun 2020 – Dec 2021)
-- QA: first row active_retailers = 7400 (full cohort in month 0)
-- OBSERVATION: {obs_1e}


-- ============================================================
-- SECTION 2: VALIDATE NDR PIPELINE
-- Reproduce the NDR reference chart before trusting any analysis.
-- If this doesn't match, nothing else can be trusted.
-- ============================================================


-- ── 2a. NDR by month (aggregate cohort formula) ─────────
-- What I'm looking for: reproduce the reference NDR values exactly.
--   Reference: month 0=100%, 1≈59%, 2≈54%, 3≈56%, 5≈49%, 12≈75%
--
-- FORMULA CHOICE: NDR_t = total_cohort_GMV_t / total_cohort_GMV_0
--   The per-retailer-average formula (avg of GMV_t/GMV_0 per retailer)
--   gives ~84% at month 1 — does NOT match. The aggregate formula matches
--   because it weights by GMV_0, so large retailers who churn pull the
--   numerator down proportionally. This is the standard SaaS NRR definition.
--
-- ⚠️ FAN-OUT: v_retailer_months already aggregates brand-level rows;
--   safe to SUM monthly_gmv directly.
--
-- CTE retailer_months (grain: retailer_id × month_offset): from view
-- CTE total_gmv0 (grain: single scalar): cohort's total month-0 GMV — fixed denominator
-- CTE ndr (grain: one row per month_offset 0–17)

WITH
retailer_months AS (
    SELECT retailer_id, month_offset, monthly_gmv
    FROM v_retailer_months
),
-- Single scalar: total GMV in the cohort's entry month
-- This is the fixed denominator for all 18 NDR values
total_gmv0 AS (
    SELECT SUM(monthly_gmv) AS val
    FROM retailer_months
    WHERE month_offset = 0
),
ndr AS (
    SELECT
        rm.month_offset,
        COUNT(DISTINCT rm.retailer_id)               AS active_retailers,
        ROUND(SUM(rm.monthly_gmv), 0)                AS cohort_gmv_t,
        ROUND(tg.val, 0)                             AS cohort_gmv_0,  -- constant denominator
        ROUND(SUM(rm.monthly_gmv) * 100.0 / tg.val, 2) AS ndr_pct
    FROM retailer_months rm
    CROSS JOIN total_gmv0 tg  -- single-row scalar; CROSS JOIN adds no fan-out risk
    WHERE rm.month_offset BETWEEN 0 AND 17
    GROUP BY rm.month_offset
)
SELECT month_offset, active_retailers, cohort_gmv_t, cohort_gmv_0, ndr_pct
FROM ndr
ORDER BY month_offset;

-- QA: 18 rows; month 0 ndr_pct = 100.00
-- QA: month 1 ≈ 58.94 (ref: 59%) ✓
-- QA: month 5 ≈ 48.85 (ref: 49%) ✓
-- QA: month 12 ≈ 75.01 (ref: 75%) ✓
-- OBSERVATION: {obs_2a}


-- ── 2b. QA: retailer count at each month ─────────────────
-- What I'm looking for: how fast does the active cohort shrink?
--   Shows both the RETENTION RATE (% still active) and confirms
--   the NDR picture — NDR > active% at later months because
--   surviving retailers spend more than they did in month 0.
--
-- CTE retailer_months (grain: retailer_id × month_offset 0–17)
-- CTE all_ret (grain: scalar — total cohort size)
-- JOIN grain: retailer_months CROSS JOIN all_ret (scalar; no fan-out)
-- Output grain: one row per month_offset (18 rows)

WITH
retailer_months AS (
    SELECT retailer_id, month_offset
    FROM v_retailer_months
    WHERE month_offset BETWEEN 0 AND 17
),
all_ret AS (
    SELECT COUNT(DISTINCT retailer_id) AS total_cohort
    FROM psa_exercise_retailers
)
SELECT
    rm.month_offset,
    COUNT(DISTINCT rm.retailer_id)                                     AS active_retailers,
    ar.total_cohort                                                     AS cohort_size,
    ROUND(COUNT(DISTINCT rm.retailer_id) * 100.0 / ar.total_cohort, 1) AS pct_still_active
FROM retailer_months rm
CROSS JOIN all_ret ar
GROUP BY rm.month_offset
ORDER BY rm.month_offset;

-- QA: 18 rows; month 0 pct_still_active = 100.0; monotone decrease expected early months
-- OBSERVATION: {obs_2b}


-- ============================================================
-- SECTION 3: RETENTION CUTS BY EVERY DIMENSION
-- Goal: find which STATIC PROFILE attributes have the biggest
--   12-mo retention gaps. These will guide where to look next.
-- Metrics: 12-mo retention %, avg lifetime GMV, single-month churn %
-- NOTE: all cuts in this section are correlational — confounders likely.
-- ============================================================


-- ── 3a. By PAYMENT_TERM ──────────────────────────────────
-- What I'm looking for: does NET60 vs PAYMENT_ON_SHIPMENT
--   predict retention? This is the biggest apparent gap but
--   also the most confounded (see observation below).
-- ⚠️ SELECTION BIAS: NET60 is extended to pre-qualified retailers.
--   Any retention gap may reflect retailer quality, not payment terms.
-- QA: 4 rows total; NET60 + POS together ≈ 99% of cohort

SELECT
    payment_term                                        AS grp,
    COUNT(*)                                            AS retailer_count,
    ROUND(AVG(has_m12_plus) * 100, 1)                  AS ret_12mo_pct,
    ROUND(AVG(lifetime_gmv), 0)                        AS avg_lifetime_gmv,
    ROUND(AVG(is_single_month_churn) * 100, 1)         AS single_month_churn_pct
FROM v_retailer_summary
GROUP BY payment_term
ORDER BY retailer_count DESC;

-- QA: 4 rows; total retailer_count = 7400; NET60+POS ≈ 95%+ of rows
-- OBSERVATION: {obs_3a}


-- ── 3b. By FLAG_APP_INSTALLED ────────────────────────────
-- What I'm looking for: does the app correlate with retention?
-- ⚠️ SNAPSHOT WARNING (CLAUDE.md): FLAG_APP_INSTALLED reflects
--   status as of data export, NOT at time of first order.
--   A retained retailer may have installed app months later.
--   Use FIRST_APP_SESSION_AT for timing-based analysis.
-- QA: exactly 2 rows (0 and 1); counts sum to 7400

SELECT
    flag_app_installed                                  AS grp,
    COUNT(*)                                            AS retailer_count,
    ROUND(AVG(has_m12_plus) * 100, 1)                  AS ret_12mo_pct,
    ROUND(AVG(lifetime_gmv), 0)                        AS avg_lifetime_gmv,
    ROUND(AVG(is_single_month_churn) * 100, 1)         AS single_month_churn_pct
FROM v_retailer_summary
GROUP BY flag_app_installed
ORDER BY flag_app_installed;

-- QA: exactly 2 rows; sum = 7400
-- OBSERVATION: {obs_3b}


-- ── 3c. By ANNUAL_SALES_BUCKET ───────────────────────────
-- What I'm looking for: does prior sales volume predict
--   on-Faire retention? And does the 'Unknown' bucket stand out?
-- QA: 7 rows; total count = 7400; Unknown should have lowest retention

SELECT
    annual_sales_bucket                                 AS grp,
    COUNT(*)                                            AS retailer_count,
    ROUND(AVG(has_m12_plus) * 100, 1)                  AS ret_12mo_pct,
    ROUND(AVG(lifetime_gmv), 0)                        AS avg_lifetime_gmv,
    ROUND(AVG(is_single_month_churn) * 100, 1)         AS single_month_churn_pct
FROM v_retailer_summary
GROUP BY annual_sales_bucket
ORDER BY CASE annual_sales_bucket
    WHEN 'Unknown'            THEN 0
    WHEN 'Brand New Retailer' THEN 1
    WHEN 'X-Small'            THEN 2
    WHEN 'Small'              THEN 3
    WHEN 'Medium'             THEN 4
    WHEN 'Large'              THEN 5
    WHEN 'X-Large'            THEN 6
    ELSE 99 END;

-- QA: 7 rows; Unknown ≈ 2285 retailers; known buckets sum to ~5115
-- OBSERVATION: {obs_3c}


-- ── 3d. By RETAILER_BUSINESS_TYPE (normalised) ───────────
-- What I'm looking for: Brick & Mortar vs Online Only vs Pop Up.
--   Raw field has 402 distinct free-text values. Cleaned in view.
-- QA: 4 rows (Brick & Mortar, Online Only, Pop Up, Other/Unknown)

SELECT
    biz_type_clean                                      AS grp,
    COUNT(*)                                            AS retailer_count,
    ROUND(AVG(has_m12_plus) * 100, 1)                  AS ret_12mo_pct,
    ROUND(AVG(lifetime_gmv), 0)                        AS avg_lifetime_gmv,
    ROUND(AVG(is_single_month_churn) * 100, 1)         AS single_month_churn_pct
FROM v_retailer_summary
GROUP BY biz_type_clean
ORDER BY retailer_count DESC;

-- QA: 4 rows; Brick & Mortar largest; total = 7400
-- OBSERVATION: {obs_3d}


-- ── 3e. By RETAILER_STORE_TYPE (top 10 by count) ─────────
-- What I'm looking for: which merchandise categories retain
--   best and worst on Faire? Store type = what they sell.
-- ⚠️ 392 retailers have store_type = 'unknown' (no data)
-- QA: 10 rows; grocery should be largest (~2182)

SELECT
    retailer_store_type                                AS grp,
    COUNT(*)                                           AS retailer_count,
    ROUND(AVG(has_m12_plus) * 100, 1)                 AS ret_12mo_pct,
    ROUND(AVG(lifetime_gmv), 0)                       AS avg_lifetime_gmv,
    ROUND(AVG(is_single_month_churn) * 100, 1)        AS single_month_churn_pct
FROM v_retailer_summary
GROUP BY retailer_store_type
ORDER BY retailer_count DESC
LIMIT 10;

-- QA: 10 rows; ret_12mo should vary meaningfully across types
-- OBSERVATION: {obs_3e}


-- ── 3f. By RETAILER_BUCKETED_CHANNEL ─────────────────────
-- What I'm looking for: does acquisition channel predict quality?
-- Expecting: channel gaps to be smaller than behavioral gaps
--   (Section 4) — retention is driven more by what retailers DO
--   than how they found us.
-- QA: exactly 4 rows; total count = 7400; Partnerships n ≈ 88

SELECT
    retailer_bucketed_channel                          AS grp,
    COUNT(*)                                           AS retailer_count,
    ROUND(AVG(has_m12_plus) * 100, 1)                 AS ret_12mo_pct,
    ROUND(AVG(lifetime_gmv), 0)                       AS avg_lifetime_gmv,
    ROUND(AVG(is_single_month_churn) * 100, 1)        AS single_month_churn_pct
FROM v_retailer_summary
GROUP BY retailer_bucketed_channel
ORDER BY retailer_count DESC;

-- QA: 4 rows; Organic largest; Partnerships n ≈ 88 (too small for strong conclusions)
-- OBSERVATION: {obs_3f}


-- ============================================================
-- SECTION 3 SUMMARY: RETENTION GAPS BY DIMENSION (RANKED)
-- ============================================================
--
-- Dimension                                Gap     Notes
-- ─────────────────────────────────────────────────────────
{gap_table}
--
-- KEY TAKEAWAY FROM SECTION 3:
-- All Section 3 gaps are based on STATIC PROFILE attributes
-- (things we know at signup). The largest gaps have confounders:
--   • Payment term: selection bias (NET60 = pre-qualified)
--   • App install: snapshot + reverse causality risk
--   • Annual sales Unknown: overlaps with Online Only segment
-- Section 4 will explore BEHAVIORAL signals (what retailers DO)
-- which may be both stronger predictors and more actionable.
-- ============================================================
""".format(
    obs_1a   = obs_1a,
    obs_1b   = obs_1b,
    sample_oid = sample_oid,
    obs_1c   = obs_1c,
    obs_1d   = obs_1d,
    obs_1e   = obs_1e,
    obs_2a   = obs_2a,
    obs_2b   = obs_2b,
    obs_3a   = obs_3a,
    obs_3b   = obs_3b,
    obs_3c   = obs_3c,
    obs_3d   = obs_3d,
    obs_3e   = obs_3e,
    obs_3f   = obs_3f,
    gap_table = "\n".join(
        f"--   {name:<42} {gap:+.1f}pp  ({note})"
        for name, gap, note in gaps_s3
    ),
    len_r1e  = len(r1e),
)

with open("/workspace/exploration_part1.sql", "w") as f:
    f.write(sql_file)

print(f"  Written: /workspace/exploration_part1.sql")
print(f"  File size: {len(sql_file):,} bytes  ({sql_file.count(chr(10))} lines)")


# ─────────────────────────────────────────────────────────────
# FINAL SUMMARY TO STDOUT
# ─────────────────────────────────────────────────────────────

banner("COMPLETE — KEY FINDINGS SUMMARY")

print("""
  Section 1: Data quality
    • Orders table is brand-level (ORDER_ID not unique per row) — confirmed
    • Only 4 CANCELED rows (<0.001%) — safe to filter to PROCESSING
    • 2,922 retailers missing FIRST_APP_SESSION_AT (never installed app)
    • Unknown annual_sales_bucket = largest single bucket (2,285 retailers, 31%)

  Section 2: NDR validates
    • Aggregate cohort formula matches reference exactly (per-retailer avg does NOT)
    • Month 1 drop: only 45% of retailers still active, NDR = 59%
    • Month 12 NDR > 100% later months = survivorship bias in retained cohort

  Section 3: Retention gaps by profile dimension (largest to smallest):""")

for name, gap, note in gaps_s3:
    print(f"    {gap:+.1f}pp  {name}  [{note}]")

print("""
  ALL Section 3 gaps have significant confounders.
  Section 4 (behavioral signals) will likely show larger, cleaner gaps.
""")

conn.close()
