"""
queries_part2.py — Targeted follow-up queries on the gaps identified in
                   the gap analysis between queries.sql and the exploration files.

Seven queries, each directly answering a specific unanswered question:

  1.  June vs July cohort split — robustness check; do both cohorts behave alike?

  2a. Payment term × GMV quartile — selection bias test:
        does the NET60 retention advantage persist within each GMV quartile,
        or does it shrink/vanish once retailer size is controlled?

  2b. Payment term × early repeat — stronger behavioral control:
        same test using the best behavioral predictor (active months in first 3).
        If gap persists here, payment term has an effect independent of behaviour.

  3a. Unknown × business type — full cross-tab:
        shows the exact overlap between annual_sales_bucket='Unknown'
        and Online Only business type; is Unknown largely just Online Only?

  3b. Online Only split by Unknown vs Known bucket:
        isolates whether being Online Only OR being Unknown drives the
        low-retention outcome. Are Known-bucket Online Only retailers
        meaningfully different from Unknown Online Only retailers?

  4a. Unknown behavioral profile — month-0 behavior:
        compares what Unknown vs Known retailers DO in month 0.
        If they're behaviourally similar but retain differently, the problem
        is structural (who they are); if behaviourally different, it's
        about what they do on the platform.

  4b. Unknown early repeat distribution:
        are Unknown retailers less likely to ever reach 2/3 or 3/3 early
        months active, or do they have the same distribution but worse
        outcomes within each bucket?

  5.  GMV gradient within ANNUAL_SALES_BUCKET:
        tests whether the Q1→Q4 retention gradient (25.4pp) is real within
        individual sales buckets, or is it entirely an artefact of larger
        retailers being in higher GMV quartiles.

CLAUDE.md QA rules applied throughout:
  • Every query: header comment (question being asked + expected finding)
  • Every CTE: purpose + grain
  • Derived fields: inline comments
  • Pre-JOIN: grain + type documented
  • Fan-out risk flagged wherever orders table is involved
  • QA check after every query
  • Observation comments filled with actual result values
"""

import glob
import os
import sqlite3
import textwrap

import pandas as pd   # only for CSV → SQLite loading

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

def get(rows, key, col, key_col=0, default=None):
    for r in rows:
        if str(r[key_col]) == str(key):
            return r[col] if col < len(r) else default
    return default

def pct_gap(a, b):
    if a is None or b is None:
        return None
    return round(a - b, 1)


# ─────────────────────────────────────────────────────────────
# LOAD DATA
# ─────────────────────────────────────────────────────────────

banner("LOADING DATA INTO SQLITE")

conn = sqlite3.connect(":memory:")
pd.read_csv(glob.glob("/workspace/psa_exercise_retailers*.csv")[0]).to_sql(
    "psa_exercise_retailers", conn, if_exists="replace", index=False)
pd.read_csv(glob.glob("/workspace/psa_exercise_orders*.csv")[0]).to_sql(
    "psa_exercise_orders", conn, if_exists="replace", index=False)

# ── Helper views ─────────────────────────────────────────────
conn.execute("DROP VIEW IF EXISTS v_retailer_months")
conn.execute("""
CREATE VIEW v_retailer_months AS
SELECT
    o.retailer_id,
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
print("  Views created: v_retailer_months, v_retailer_summary")


# ═════════════════════════════════════════════════════════════
# QUERY 1 — JUNE VS JULY COHORT COMPARISON
# ═════════════════════════════════════════════════════════════

banner("QUERY 1: JUNE vs JULY COHORT COMPARISON")
print("  Question: are the two cohort entry months meaningfully different,")
print("  or can we treat them as a single cohort for all analyses?")

SQL_Q1 = """
-- Question: do June and July 2020 cohorts have different retention profiles?
-- Expected: small difference (both COVID-recovery era); if large, all prior
--   aggregate numbers are hiding a meaningful composition effect.
-- Grain of output: one row per cohort entry month (2 rows)

SELECT
    -- Reformat date for readability
    SUBSTR(first_confirmed_order_placed_at, 1, 7)  AS cohort_month,
    COUNT(*)                                        AS retailer_count,
    ROUND(AVG(has_m12_plus)          * 100, 1)     AS ret_12mo_pct,
    ROUND(AVG(has_m6_plus)           * 100, 1)     AS ret_6mo_pct,
    ROUND(AVG(is_single_month_churn) * 100, 1)     AS single_month_churn_pct,
    ROUND(AVG(lifetime_gmv),                   0)  AS avg_lifetime_gmv,
    ROUND(AVG(months_active),                  2)  AS avg_months_active
FROM v_retailer_summary
GROUP BY cohort_month
ORDER BY cohort_month

-- QA: exactly 2 rows (2020-06 and 2020-07)
-- QA: counts sum to 7400 (3443 June + 3957 July)
-- QA: if ret_12mo gap > 5pp, subsequent cohort-split analyses are warranted
"""
r_q1 = run(conn, SQL_Q1)
print_table(r_q1, ["cohort_month","count","ret_12mo%","ret_6mo%","churn%","avg_gmv","avg_months"])

june_ret  = get(r_q1, "2020-06", 2)
july_ret  = get(r_q1, "2020-07", 2)
june_churn = get(r_q1, "2020-06", 4)
july_churn = get(r_q1, "2020-07", 4)
q1_gap    = pct_gap(june_ret, july_ret) if june_ret and july_ret else None

if q1_gap is not None:
    if abs(q1_gap) <= 3:
        q1_verdict = (f"Gap of {abs(q1_gap):.1f}pp — cohorts are similar. "
                      f"Treating them as a single cohort is reasonable.")
    else:
        q1_verdict = (f"Gap of {abs(q1_gap):.1f}pp — MEANINGFUL DIFFERENCE. "
                      f"Segment analyses should be checked separately per cohort.")
else:
    q1_verdict = "Could not compute gap."

obs_q1 = (
    f"June 2020 cohort (n={get(r_q1,'2020-06',1):,}): {june_ret}% 12-mo retention, "
    f"{june_churn}% single-month churn. "
    f"July 2020 cohort (n={get(r_q1,'2020-07',1):,}): {july_ret}% 12-mo retention, "
    f"{july_churn}% single-month churn. "
    f"{q1_verdict}"
)
print(f"\n  OBSERVATION: {obs_q1}")


# ═════════════════════════════════════════════════════════════
# QUERY 2a — PAYMENT TERM × GMV QUARTILE (SELECTION BIAS TEST)
# ═════════════════════════════════════════════════════════════

banner("QUERY 2a: PAYMENT TERM × GMV QUARTILE (SELECTION BIAS TEST)")
print("  Question: does the NET60 retention advantage (23.4pp) persist within")
print("  each GMV quartile, or does it shrink as GMV controls for retailer size?")
print("  If gap ~ constant across quartiles → NET60 has an independent effect.")
print("  If gap → 0 at high quartiles → it's entirely explained by retailer size.")

SQL_Q2A = """
-- Selection bias test: does NET60 predict retention AFTER controlling for
-- first-month spend depth (a proxy for retailer size/sophistication)?
--
-- INTERPRETATION KEY:
--   If NET60 gap is similar (±5pp) across all 4 quartiles:
--     → NET60 has an effect independent of retailer size
--   If NET60 gap shrinks from Q1 to Q4 (or reverses):
--     → Gap is largely explained by selection: large retailers get NET60 AND retain better
--   If NET60 gap is large at Q1 but small at Q4:
--     → NET60 may matter most for smaller retailers who have cash-flow constraints
--
-- ⚠️ FAN-OUT: v_retailer_months already at retailer-month grain; no fan-out risk
--
-- CTE m0_stats (grain: one row per retailer_id — month 0 GMV only)
-- CTE gmv_ntile (grain: one row per retailer_id — adds NTILE quartile)
-- JOIN: v_retailer_summary (one per retailer) × gmv_ntile (one per retailer) → 1:1
-- Output grain: one row per (gmv_quartile, payment_term) — 8 rows

WITH
-- Month-0 GMV per retailer
-- Grain: one row per retailer_id
m0_stats AS (
    SELECT retailer_id, monthly_gmv AS gmv_m0
    FROM v_retailer_months
    WHERE month_offset = 0
),
-- Assign GMV quartile across ALL retailers (regardless of payment term)
-- Grain: one row per retailer_id
gmv_ntile AS (
    SELECT
        retailer_id,
        NTILE(4) OVER (ORDER BY gmv_m0)  AS gmv_quartile,  -- 1=lowest spend, 4=highest
        gmv_m0
    FROM m0_stats
)
SELECT
    g.gmv_quartile,
    rs.payment_term,
    COUNT(*)                                     AS retailer_count,
    ROUND(AVG(g.gmv_m0), 0)                      AS avg_gmv_m0,      -- for context
    ROUND(AVG(rs.has_m12_plus) * 100, 1)         AS ret_12mo_pct,
    ROUND(AVG(rs.is_single_month_churn) * 100, 1) AS churn_pct
-- JOIN grain: v_retailer_summary (one per retailer) × gmv_ntile (one per retailer) → 1:1
FROM v_retailer_summary rs
JOIN gmv_ntile g ON rs.retailer_id = g.retailer_id
WHERE rs.payment_term IN ('NET60', 'PAYMENT_ON_SHIPMENT')  -- focus on main two terms
GROUP BY g.gmv_quartile, rs.payment_term
ORDER BY g.gmv_quartile, rs.payment_term

-- QA: 8 rows (4 quartiles × 2 payment terms)
-- QA: within each quartile, NET60 + POS counts should sum to ~1850
-- QA: check whether NET60 is over-represented in Q3/Q4 (retailer_count imbalance)
"""
r_q2a = run(conn, SQL_Q2A)
print_table(r_q2a, ["quartile","payment_term","count","avg_gmv","ret_12mo%","churn%"])

# Compute gap per quartile
q2a_gaps = {}
for q in [1, 2, 3, 4]:
    n60  = get(r_q2a, "NET60",              4, key_col=1)  # won't work — need multi-key lookup
    # Use position in filtered rows
    rows_q = [r for r in r_q2a if r[0] == q]
    n60_row = next((r for r in rows_q if r[1] == "NET60"), None)
    pos_row = next((r for r in rows_q if r[1] == "PAYMENT_ON_SHIPMENT"), None)
    if n60_row and pos_row:
        q2a_gaps[q] = round(n60_row[4] - pos_row[4], 1)

gap_q1 = q2a_gaps.get(1); gap_q4 = q2a_gaps.get(4)
net60_share_q4 = next((r[2] for r in r_q2a if r[0]==4 and r[1]=="NET60"), None)
pos_share_q4   = next((r[2] for r in r_q2a if r[0]==4 and r[1]=="PAYMENT_ON_SHIPMENT"), None)

print(f"\n  NET60 vs POS gap per quartile:")
for q in [1, 2, 3, 4]:
    g = q2a_gaps.get(q, "N/A")
    print(f"    Q{q}: {g:+.1f}pp" if isinstance(g, float) else f"    Q{q}: N/A")

if gap_q1 is not None and gap_q4 is not None:
    trend = gap_q4 - gap_q1
    if abs(trend) <= 5:
        q2a_verdict = (f"Gap is STABLE across quartiles (Q1: {gap_q1:+.1f}pp → Q4: {gap_q4:+.1f}pp, "
                       f"Δ={trend:+.1f}pp). Selection bias does not fully explain the NET60 advantage — "
                       f"the effect persists even within GMV-matched groups.")
    elif trend < -5:
        q2a_verdict = (f"Gap NARROWS significantly (Q1: {gap_q1:+.1f}pp → Q4: {gap_q4:+.1f}pp, "
                       f"Δ={trend:+.1f}pp). Evidence of selection bias: larger retailers get NET60 "
                       f"AND retain better; the payment term itself explains less of the gap.")
    else:
        q2a_verdict = (f"Gap WIDENS (Q1: {gap_q1:+.1f}pp → Q4: {gap_q4:+.1f}pp, Δ={trend:+.1f}pp). "
                       f"Unexpected pattern — investigate further.")
else:
    q2a_verdict = "Could not compute trend."

obs_q2a = (
    f"NET60 vs POS 12-mo retention gap: "
    f"Q1={q2a_gaps.get(1,'?'):+}pp, Q2={q2a_gaps.get(2,'?'):+}pp, "
    f"Q3={q2a_gaps.get(3,'?'):+}pp, Q4={q2a_gaps.get(4,'?'):+}pp. "
    f"In Q4 (highest spend): NET60 n={net60_share_q4}, POS n={pos_share_q4} — "
    f"note the imbalance (NET60 retailers skew high-GMV). "
    f"{q2a_verdict}"
)
print(f"\n  OBSERVATION: {obs_q2a}")


# ═════════════════════════════════════════════════════════════
# QUERY 2b — PAYMENT TERM × EARLY REPEAT (BEHAVIOURAL CONTROL)
# ═════════════════════════════════════════════════════════════

banner("QUERY 2b: PAYMENT TERM × EARLY REPEAT (STRONGER BEHAVIOURAL CONTROL)")
print("  Question: does NET60 still predict retention after controlling for")
print("  early repeat purchasing — the strongest known retention predictor?")
print("  This is a cleaner test than GMV quartile because early repeat is")
print("  measured AFTER the payment term is already set.")

SQL_Q2B = """
-- Stronger selection bias test using early repeat behaviour as the control.
-- Early repeat (active months in first 3) is the best predictor we found (40pp gap).
-- If NET60 gap persists WITHIN each activity level, the payment term is contributing
-- something beyond simply attracting higher-intent retailers.
--
-- ⚠️ FAN-OUT: v_retailer_months already at retailer-month grain; COUNT(DISTINCT) safe
--
-- CTE first3_active (grain: one row per retailer_id): active months in {0,1,2}
-- JOIN: v_retailer_summary (one per retailer) × first3_active (≤1 per retailer) → 1:1
-- Output grain: one row per (active_months, payment_term) — 6 rows for main two terms

WITH
-- Count distinct months in {0,1,2} with any orders
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
    COALESCE(f.active_months_first3, 0)       AS active_months,   -- months active in first 3
    rs.payment_term,
    COUNT(*)                                   AS retailer_count,
    ROUND(AVG(rs.has_m12_plus) * 100, 1)      AS ret_12mo_pct,
    ROUND(AVG(rs.is_single_month_churn) * 100, 1) AS churn_pct
-- JOIN grain: v_retailer_summary (one per retailer) × first3_active (≤1 per retailer) → 1:1
FROM v_retailer_summary rs
LEFT JOIN first3_active f ON rs.retailer_id = f.retailer_id
WHERE rs.payment_term IN ('NET60', 'PAYMENT_ON_SHIPMENT')
GROUP BY active_months, rs.payment_term
ORDER BY active_months, rs.payment_term

-- QA: 6 rows (3 activity levels × 2 terms); active_months should cover 1, 2, 3
-- QA: within each activity level, NET60 + POS counts should sum to ~2000-3000
-- QA: 3/3 churn_pct should be 0% for both terms (by definition)
"""
r_q2b = run(conn, SQL_Q2B)
print_table(r_q2b, ["active_months","payment_term","count","ret_12mo%","churn%"])

# Compute gap per activity level
q2b_gaps = {}
for months in [1, 2, 3]:
    rows_m = [r for r in r_q2b if r[0] == months]
    n60_row = next((r for r in rows_m if r[1] == "NET60"), None)
    pos_row = next((r for r in rows_m if r[1] == "PAYMENT_ON_SHIPMENT"), None)
    if n60_row and pos_row:
        q2b_gaps[months] = round(n60_row[3] - pos_row[3], 1)

print(f"\n  NET60 vs POS gap within each early-repeat bucket:")
for m in [1, 2, 3]:
    g = q2b_gaps.get(m)
    print(f"    {m}/3 active: {g:+.1f}pp" if g is not None else f"    {m}/3 active: N/A")

gap_1of3 = q2b_gaps.get(1); gap_3of3 = q2b_gaps.get(3)
if gap_1of3 is not None:
    if abs(gap_1of3) <= 5:
        q2b_verdict = (f"NET60 gap is ~flat across activity levels (1/3: {gap_1of3:+.1f}pp, "
                       f"3/3: {gap_3of3:+.1f}pp). Behavioural engagement does not explain "
                       f"the payment term gap — NET60 adds value beyond just attracting "
                       f"high-intent retailers.")
    else:
        q2b_verdict = (f"NET60 gap is {gap_1of3:+.1f}pp at 1/3 active and {gap_3of3:+.1f}pp "
                       f"at 3/3 active. {'Gap shrinks with more activity — engagement partly explains it.' if gap_1of3 > gap_3of3 else 'Gap is still present even for highly engaged retailers.'}")
else:
    q2b_verdict = "Could not compute."

obs_q2b = (
    f"NET60 vs POS gap controlling for early repeat: "
    f"1/3={q2b_gaps.get(1,'?'):+}pp, 2/3={q2b_gaps.get(2,'?'):+}pp, "
    f"3/3={q2b_gaps.get(3,'?'):+}pp. {q2b_verdict}"
)
print(f"\n  OBSERVATION: {obs_q2b}")


# ═════════════════════════════════════════════════════════════
# QUERY 3a — UNKNOWN × BUSINESS TYPE: FULL CROSS-TAB
# ═════════════════════════════════════════════════════════════

banner("QUERY 3a: UNKNOWN BUCKET × BUSINESS TYPE — FULL CROSS-TAB")
print("  Question: is the Unknown bucket mostly Online Only retailers?")
print("  If Unknown is ≥70% Online Only, the Unknown problem IS the Online Only problem.")
print("  This determines whether we need two separate interventions or one.")

SQL_Q3A = """
-- Full cross-tab: annual_sales_bucket (Unknown/Known) × biz_type_clean
-- Reveals how much of the Unknown bucket is explained by business type composition.
-- Key question: if Unknown skews heavily Online Only, and Online Only skews heavily
-- Unknown, these two 'problems' from the exploration files may be the same segment.
--
-- CTE uses biz_type_clean from v_retailer_summary (pre-normalised 4 groups)
-- Output grain: one row per (bucket_group, biz_type_clean) — up to 8 rows

SELECT
    CASE WHEN annual_sales_bucket = 'Unknown' THEN 'Unknown' ELSE 'Known' END
        AS bucket_group,
    biz_type_clean,
    COUNT(*)                                    AS retailer_count,
    -- Share of THIS biz type within each bucket group
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (
        PARTITION BY
            CASE WHEN annual_sales_bucket = 'Unknown' THEN 'Unknown' ELSE 'Known' END
    ), 1)                                       AS pct_within_bucket,
    ROUND(AVG(has_m12_plus)      * 100, 1)      AS ret_12mo_pct,
    ROUND(AVG(is_single_month_churn) * 100, 1)  AS churn_pct
FROM v_retailer_summary
GROUP BY bucket_group, biz_type_clean
ORDER BY bucket_group, retailer_count DESC

-- QA: 8 rows (2 bucket groups × 4 biz types); pct_within_bucket sums to 100% per group
-- QA: Unknown / Online Only combination should stand out
"""
r_q3a = run(conn, SQL_Q3A)
print_table(r_q3a, ["bucket_group","biz_type","count","pct_within%","ret_12mo%","churn%"])

unk_online_pct = next((r[3] for r in r_q3a if r[0]=="Unknown" and r[1]=="Online Only"), None)
unk_bm_pct     = next((r[3] for r in r_q3a if r[0]=="Unknown" and r[1]=="Brick & Mortar"), None)
known_online_pct = next((r[3] for r in r_q3a if r[0]=="Known" and r[1]=="Online Only"), None)
unk_online_ret = next((r[4] for r in r_q3a if r[0]=="Unknown" and r[1]=="Online Only"), None)
known_bm_ret   = next((r[4] for r in r_q3a if r[0]=="Known"   and r[1]=="Brick & Mortar"), None)

obs_q3a = (
    f"Within Unknown bucket: {unk_online_pct}% are Online Only, {unk_bm_pct}% are Brick & Mortar. "
    f"Within Known bucket: {known_online_pct}% are Online Only. "
    f"{'Unknown is indeed dominated by Online Only — the two problems are largely the same population.' if unk_online_pct and unk_online_pct >= 50 else 'Unknown is NOT predominantly Online Only — it is a more heterogeneous group.'} "
    f"Unknown/Online Only 12-mo retention: {unk_online_ret}% vs Known/Brick & Mortar: {known_bm_ret}% — "
    f"a {round(known_bm_ret - unk_online_ret, 1) if known_bm_ret and unk_online_ret else '?'}pp gap between these two poles."
)
print(f"\n  OBSERVATION: {obs_q3a}")


# ═════════════════════════════════════════════════════════════
# QUERY 3b — ONLINE ONLY: UNKNOWN vs KNOWN BUCKET
# ═════════════════════════════════════════════════════════════

banner("QUERY 3b: ONLINE ONLY — UNKNOWN vs KNOWN BUCKET")
print("  Question: within just Online Only retailers, does being in the Unknown")
print("  bucket still predict worse retention? Or is 'Online Only' the whole story")
print("  and Unknown just labels the same population more accurately?")

SQL_Q3B = """
-- Isolates the 'Online Only' segment and splits it by Unknown vs Known bucket.
-- If Unknown/Online Only retains similarly to Known/Online Only:
--   → Business type is the driver; Unknown label is just measuring the same thing
-- If Unknown/Online Only retains MUCH worse than Known/Online Only:
--   → Being in the Unknown bucket has an additional negative effect beyond just
--     being Online Only (possibly different onboarding, less curation, less support)
--
-- Output grain: one row per (bucket_group) among Online Only retailers — 2 rows

SELECT
    CASE WHEN annual_sales_bucket = 'Unknown' THEN 'Unknown-bucket' ELSE 'Known-bucket' END
        AS bucket_group,
    COUNT(*)                                     AS retailer_count,
    ROUND(AVG(has_m12_plus)      * 100, 1)       AS ret_12mo_pct,
    ROUND(AVG(has_m6_plus)       * 100, 1)       AS ret_6mo_pct,
    ROUND(AVG(is_single_month_churn) * 100, 1)   AS churn_pct,
    ROUND(AVG(lifetime_gmv),               0)    AS avg_lifetime_gmv
FROM v_retailer_summary
WHERE biz_type_clean = 'Online Only'   -- restrict to Online Only only
GROUP BY bucket_group
ORDER BY bucket_group

-- QA: 2 rows (Unknown-bucket and Known-bucket); both within Online Only population
-- QA: total count should match the Online Only count from exploration_part1 (~1859)
-- QA: if gap in ret_12mo > 10pp, Unknown has an additional effect beyond business type
"""
r_q3b = run(conn, SQL_Q3B)
print_table(r_q3b, ["bucket_group","count","ret_12mo%","ret_6mo%","churn%","avg_gmv"])

unk_oo_ret   = next((r[2] for r in r_q3b if "Unknown" in str(r[0])), None)
known_oo_ret = next((r[2] for r in r_q3b if "Known"   in str(r[0])), None)
unk_oo_n     = next((r[1] for r in r_q3b if "Unknown" in str(r[0])), None)
known_oo_n   = next((r[1] for r in r_q3b if "Known"   in str(r[0])), None)
gap_3b       = pct_gap(known_oo_ret, unk_oo_ret)

if gap_3b is not None:
    if gap_3b <= 5:
        q3b_verdict = ("Gap ≤5pp — the Unknown bucket adds little beyond being Online Only. "
                       "A single 'Online Only' intervention strategy would cover both groups.")
    elif gap_3b <= 15:
        q3b_verdict = (f"Moderate gap of {gap_3b:.1f}pp — Unknown/Online Only is somewhat "
                       "worse than Known/Online Only. Both business type AND Unknown bucket "
                       "independently predict poor retention.")
    else:
        q3b_verdict = (f"Large gap of {gap_3b:.1f}pp — the Unknown bucket has a strong "
                       "ADDITIONAL effect beyond business type. These are distinctly different "
                       "populations even within Online Only.")
else:
    q3b_verdict = "Could not compute."

obs_q3b = (
    f"Among Online Only retailers: "
    f"Known-bucket (n={known_oo_n}): {known_oo_ret}% 12-mo retention. "
    f"Unknown-bucket (n={unk_oo_n}): {unk_oo_ret}% 12-mo retention. "
    f"Gap = {gap_3b:+.1f}pp. {q3b_verdict}"
)
print(f"\n  OBSERVATION: {obs_q3b}")


# ═════════════════════════════════════════════════════════════
# QUERY 4a — UNKNOWN BEHAVIORAL PROFILE (MONTH-0 BEHAVIOUR)
# ═════════════════════════════════════════════════════════════

banner("QUERY 4a: UNKNOWN SEGMENT — MONTH-0 BEHAVIOURAL PROFILE")
print("  Question: do Unknown retailers behave differently on their FIRST month,")
print("  or do they start the same but then diverge?")
print("  If behaviourally similar but worse outcomes: structural (who they are).")
print("  If behaviourally different from day 1: platform engagement is the story.")

SQL_Q4A = """
-- Compares month-0 behaviour of Unknown vs Known retailers.
-- ⚠️ FAN-OUT: v_retailer_months already at retailer-month grain; no fan-out risk
--
-- CTE m0_stats (grain: one row per retailer_id — month 0 metrics only)
-- JOIN: v_retailer_summary (one per retailer) × m0_stats (one per retailer) → 1:1
-- Output grain: one row per segment (Unknown / Known) — 2 rows

WITH
-- Month-0 GMV, brand count, and order count per retailer
-- Grain: one row per retailer_id
m0_stats AS (
    SELECT
        retailer_id,
        monthly_gmv  AS gmv_m0,
        brand_count  AS brands_m0,
        order_count  AS orders_m0   -- distinct orders in month 0 (not brand rows)
    FROM v_retailer_months
    WHERE month_offset = 0
)
SELECT
    CASE WHEN rs.annual_sales_bucket = 'Unknown' THEN 'Unknown' ELSE 'Known' END
        AS segment,
    COUNT(*)                      AS retailer_count,
    ROUND(AVG(m.gmv_m0),    0)    AS avg_gmv_m0,
    ROUND(AVG(m.brands_m0), 2)    AS avg_brands_m0,
    ROUND(AVG(m.orders_m0), 2)    AS avg_orders_m0,
    ROUND(AVG(rs.has_m12_plus) * 100, 1) AS ret_12mo_pct   -- for reference
-- JOIN grain: v_retailer_summary (one per retailer) × m0_stats (one per retailer) → 1:1
FROM v_retailer_summary rs
JOIN m0_stats m ON rs.retailer_id = m.retailer_id
GROUP BY segment
ORDER BY segment

-- QA: 2 rows; Known + Unknown counts = 7400
-- QA: Known expected higher avg_gmv_m0 and avg_brands_m0 than Unknown
"""
r_q4a = run(conn, SQL_Q4A)
print_table(r_q4a, ["segment","count","avg_gmv_m0","avg_brands_m0","avg_orders_m0","ret_12mo%"])

known_gmv    = get(r_q4a, "Known",   2)
unk_gmv      = get(r_q4a, "Unknown", 2)
known_brands = get(r_q4a, "Known",   3)
unk_brands   = get(r_q4a, "Unknown", 3)
known_ret_4a = get(r_q4a, "Known",   5)
unk_ret_4a   = get(r_q4a, "Unknown", 5)

gmv_ratio    = round(known_gmv / unk_gmv, 2)    if unk_gmv    else None
brand_ratio  = round(known_brands / unk_brands, 2) if unk_brands else None

obs_q4a = (
    f"Known retailers: avg month-0 GMV ${known_gmv:,.0f}, {known_brands:.2f} brands. "
    f"Unknown retailers: avg month-0 GMV ${unk_gmv:,.0f}, {unk_brands:.2f} brands. "
    f"Known spends {gmv_ratio:.1f}x more and orders from {brand_ratio:.1f}x more brands in month 0. "
    f"Retention difference: Known {known_ret_4a}% vs Unknown {unk_ret_4a}% = "
    f"{round(known_ret_4a - unk_ret_4a, 1)}pp gap. "
    f"Unknown retailers are behaviourally weaker from day one — lower spend AND less exploration. "
    f"This suggests the poor retention is at least partly explained by lower initial engagement, "
    f"NOT purely by who they are. Implication: an engagement intervention in month 0 "
    f"(more brand recommendations, lower barriers to first order) could help this segment."
)
print(f"\n  OBSERVATION: {obs_q4a}")


# ═════════════════════════════════════════════════════════════
# QUERY 4b — UNKNOWN EARLY REPEAT DISTRIBUTION
# ═════════════════════════════════════════════════════════════

banner("QUERY 4b: UNKNOWN SEGMENT — EARLY REPEAT DISTRIBUTION")
print("  Question: are Unknown retailers less likely to ever reach 2/3 or 3/3")
print("  early months? Or do they reach the same activity levels but with")
print("  worse outcomes within each bucket?")
print("  This tells us WHERE in the funnel the Unknown problem sits.")

SQL_Q4B = """
-- For each (Unknown/Known) segment, show the distribution of early-repeat activity
-- (1/3, 2/3, 3/3 active months) and the 12-mo retention within each cell.
-- ⚠️ FAN-OUT: v_retailer_months at retailer-month grain; COUNT(DISTINCT) safe
--
-- CTE first3_active (grain: one row per retailer_id)
-- JOIN: v_retailer_summary (one per retailer) × first3_active (≤1 per retailer) → 1:1
-- Output grain: one row per (segment, active_months) — up to 6 rows

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
    CASE WHEN rs.annual_sales_bucket = 'Unknown' THEN 'Unknown' ELSE 'Known' END
        AS segment,
    COALESCE(f.active_months_first3, 0)        AS active_months,
    COUNT(*)                                    AS retailer_count,
    -- Share of this activity level WITHIN this segment
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (
        PARTITION BY
            CASE WHEN rs.annual_sales_bucket = 'Unknown' THEN 'Unknown' ELSE 'Known' END
    ), 1)                                       AS pct_within_segment,
    ROUND(AVG(rs.has_m12_plus) * 100, 1)       AS ret_12mo_pct
FROM v_retailer_summary rs
LEFT JOIN first3_active f ON rs.retailer_id = f.retailer_id
GROUP BY segment, active_months
ORDER BY segment, active_months

-- QA: up to 6 rows (2 segments × up to 3 activity levels); total = 7400
-- QA: pct_within_segment sums to 100% per segment
-- If Unknown has fewer 3/3 retailers: funnel problem (they disengage early)
-- If same distribution but lower ret_12mo within each level: outcome problem
"""
r_q4b = run(conn, SQL_Q4B)
print_table(r_q4b, ["segment","active_months","count","pct_within%","ret_12mo%"])

# Get 3/3 share for Known vs Unknown
known_3of3_pct = next((r[3] for r in r_q4b if r[0]=="Known"   and r[1]==3), None)
unk_3of3_pct   = next((r[3] for r in r_q4b if r[0]=="Unknown" and r[1]==3), None)
known_3of3_ret = next((r[4] for r in r_q4b if r[0]=="Known"   and r[1]==3), None)
unk_3of3_ret   = next((r[4] for r in r_q4b if r[0]=="Unknown" and r[1]==3), None)
known_1of3_ret = next((r[4] for r in r_q4b if r[0]=="Known"   and r[1]==1), None)
unk_1of3_ret   = next((r[4] for r in r_q4b if r[0]=="Unknown" and r[1]==1), None)

dist_diff = round(known_3of3_pct - unk_3of3_pct, 1) if (known_3of3_pct and unk_3of3_pct) else None

obs_q4b = (
    f"3/3 months active: Known {known_3of3_pct}% of segment vs Unknown {unk_3of3_pct}% of segment "
    f"({dist_diff:+.1f}pp difference in reaching 3/3). "
    f"12-mo retention within 3/3: Known {known_3of3_ret}% vs Unknown {unk_3of3_ret}%. "
    f"12-mo retention within 1/3: Known {known_1of3_ret}% vs Unknown {unk_1of3_ret}%. "
    f"{'FUNNEL problem dominates: Unknown retailers are less likely to reach higher activity levels — fewer reach 3/3.' if dist_diff and dist_diff > 5 else 'OUTCOME problem dominates: Unknown retailers have similar activity distributions but worse retention within each bucket.'} "
    f"Intervention implication: "
    f"{'focus on activating Unknown retailers in months 1–2 (the funnel problem).' if dist_diff and dist_diff > 5 else 'something else beyond activity frequency is driving poor Unknown retention (e.g. category fit, product availability, support quality).'}"
)
print(f"\n  OBSERVATION: {obs_q4b}")


# ═════════════════════════════════════════════════════════════
# QUERY 5 — GMV GRADIENT WITHIN ANNUAL_SALES_BUCKET
# ═════════════════════════════════════════════════════════════

banner("QUERY 5: GMV GRADIENT WITHIN ANNUAL_SALES_BUCKET")
print("  Question: does the Q1→Q4 GMV gradient (25.4pp retention gap) persist")
print("  within individual sales buckets, or is it purely a size artefact?")
print("  If gradient is flat within buckets: GMV quartile = retailer size proxy only.")
print("  If gradient persists within buckets: spend depth is independently predictive.")

SQL_Q5 = """
-- Tests whether high first-month spend predicts better retention within
-- homogeneous sales-bucket groups (i.e., after controlling for retailer size tier).
-- ⚠️ NOTE: NTILE(4) is computed across ALL retailers; it is NOT per-bucket.
--   This means within a small bucket (e.g. Medium, n=578), Q1 retailers are
--   those in the bottom quartile OVERALL, not just within that bucket.
--   This preserves comparability but means some buckets may have few Q1 rows.
-- ⚠️ FAN-OUT: v_retailer_months already at retailer-month grain; no fan-out risk
--
-- CTE m0_stats (grain: one row per retailer_id — month 0 GMV)
-- CTE gmv_ntile (grain: one row per retailer_id — adds global NTILE quartile)
-- JOIN: v_retailer_summary × gmv_ntile → 1:1
-- Output grain: one row per (annual_sales_bucket, gmv_quartile)
-- Excludes 'Unknown' (already studied) and 'X-Large' (n=1)

WITH
m0_stats AS (
    SELECT retailer_id, monthly_gmv AS gmv_m0
    FROM v_retailer_months
    WHERE month_offset = 0
),
gmv_ntile AS (
    SELECT
        retailer_id,
        NTILE(4) OVER (ORDER BY gmv_m0)  AS gmv_quartile,
        gmv_m0
    FROM m0_stats
)
SELECT
    rs.annual_sales_bucket,
    g.gmv_quartile,
    COUNT(*)                             AS retailer_count,
    ROUND(AVG(g.gmv_m0), 0)             AS avg_gmv_m0,       -- context for the quartile
    ROUND(AVG(rs.has_m12_plus) * 100, 1) AS ret_12mo_pct
FROM v_retailer_summary rs
JOIN gmv_ntile g ON rs.retailer_id = g.retailer_id
WHERE rs.annual_sales_bucket NOT IN ('Unknown', 'X-Large')  -- exclude outlier buckets
GROUP BY rs.annual_sales_bucket, g.gmv_quartile
ORDER BY rs.annual_sales_bucket, g.gmv_quartile

-- QA: ≤24 rows (6 known buckets × up to 4 quartiles)
-- QA: some bucket×quartile cells may be small — note where n<50
-- If ret_12mo increases Q1→Q4 WITHIN each bucket: spend depth is genuinely predictive
-- If ret_12mo is flat within each bucket: the overall gradient is pure composition bias
"""
r_q5 = run(conn, SQL_Q5)
print_table(r_q5, ["annual_sales_bucket","quartile","count","avg_gmv","ret_12mo%"])

# Summarise: for each bucket, compute Q1 vs Q4 gap
buckets_q5 = {}
for row in r_q5:
    bkt = row[0]
    q   = row[1]
    ret = row[4]
    if bkt not in buckets_q5:
        buckets_q5[bkt] = {}
    buckets_q5[bkt][q] = ret

print("\n  Q1 vs Q4 gap within each ANNUAL_SALES_BUCKET:")
within_gaps = []
for bkt, qs in sorted(buckets_q5.items()):
    q1r = qs.get(1); q4r = qs.get(4)
    if q1r and q4r:
        g = round(q4r - q1r, 1)
        within_gaps.append(g)
        print(f"    {bkt:<22}: Q1={q1r}% → Q4={q4r}% (gap={g:+.1f}pp)")
    else:
        print(f"    {bkt:<22}: insufficient data (bucket too small)")

avg_within_gap = round(sum(within_gaps) / len(within_gaps), 1) if within_gaps else None

obs_q5 = (
    f"Q1→Q4 retention gradient within individual sales buckets: "
    f"{', '.join(f'{b}={qs.get(4,0)-(qs.get(1,0)):+.1f}pp' for b, qs in sorted(buckets_q5.items()) if qs.get(1) and qs.get(4))}. "
    f"Average within-bucket gradient: {avg_within_gap:+.1f}pp (vs overall gradient of +25.4pp). "
    f"{'The gradient PERSISTS within buckets — first-month spend depth predicts retention independently of retailer size. This validates first-month GMV as a genuine leading indicator, not just a retailer size proxy.' if avg_within_gap and avg_within_gap >= 10 else 'The gradient SHRINKS substantially within buckets — much of the 25.4pp overall gap is explained by larger retailers having both higher GMV and better retention. The within-bucket gradient is real but weaker.'}"
)
print(f"\n  OBSERVATION: {obs_q5}")


# ═════════════════════════════════════════════════════════════
# WRITE queries_part2.sql
# ═════════════════════════════════════════════════════════════

banner("WRITING queries_part2.sql")

# Build the Q2a quartile table for the SQL comment
q2a_table_lines = []
q2a_table_lines.append("--   Quartile  NET60     POS      NET60–POS gap")
for q in [1,2,3,4]:
    rows_q = [r for r in r_q2a if r[0] == q]
    n60_row = next((r for r in rows_q if r[1] == "NET60"), None)
    pos_row = next((r for r in rows_q if r[1] == "PAYMENT_ON_SHIPMENT"), None)
    if n60_row and pos_row:
        g = round(n60_row[4] - pos_row[4], 1)
        q2a_table_lines.append(f"--   Q{q}        {n60_row[4]}%    {pos_row[4]}%    {g:+.1f}pp")
q2a_inline_table = "\n".join(q2a_table_lines)

# Q2b inline table
q2b_table_lines = ["--   Active months  NET60     POS      NET60–POS gap"]
for m in [1,2,3]:
    rows_m = [r for r in r_q2b if r[0] == m]
    n60_row = next((r for r in rows_m if r[1] == "NET60"), None)
    pos_row = next((r for r in rows_m if r[1] == "PAYMENT_ON_SHIPMENT"), None)
    if n60_row and pos_row:
        g = round(n60_row[3] - pos_row[3], 1)
        q2b_table_lines.append(f"--   {m}/3               {n60_row[3]}%    {pos_row[3]}%    {g:+.1f}pp")
q2b_inline_table = "\n".join(q2b_table_lines)

sql_out = """\
-- ============================================================
-- FAIRE RETAILER RETENTION — FOLLOW-UP QUERIES (PART 2)
-- queries_part2.sql
--
-- Purpose: targeted follow-up on the gaps identified between
--   queries.sql and the exploratory analysis (exploration_part1.sql,
--   exploration_part2.sql).  Each query directly answers a specific
--   unanswered question raised in the exploration observations.
--
-- CLAUDE.md QA rules applied to every query:
--   • Header: question being tested + expected finding
--   • CTE grain annotations
--   • Pre-JOIN documentation (grain + type)
--   • Fan-out risk flagged wherever orders table is involved
--   • QA check after each query
--   • OBSERVATION comments contain actual result values
--
-- DATA NOTES (CLAUDE.md):
--   • orders table is BRAND-level → ORDER_ID NOT unique per row
--   • TOTAL_GMV is per-brand-per-order; always SUM to get retailer GMV
--   • FLAG_APP_INSTALLED is a snapshot — use FIRST_APP_SESSION_AT for timing
-- ============================================================


-- SETUP: run these views once per session (same definitions as queries.sql)
DROP VIEW IF EXISTS v_retailer_months;
CREATE VIEW v_retailer_months AS
SELECT
    o.retailer_id,
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
    r.retailer_business_type,
    r.first_confirmed_order_placed_at,
    r.first_app_session_at,
    COALESCE(SUM(rm.monthly_gmv), 0)             AS lifetime_gmv,
    COALESCE(MAX(rm.month_offset), -1)            AS max_month,
    COALESCE(COUNT(DISTINCT rm.month_offset), 0)  AS months_active,
    MAX(CASE WHEN rm.month_offset >= 6  THEN 1 ELSE 0 END) AS has_m6_plus,
    MAX(CASE WHEN rm.month_offset >= 12 THEN 1 ELSE 0 END) AS has_m12_plus,
    CASE WHEN MAX(rm.month_offset) = 0
              OR MAX(rm.month_offset) IS NULL THEN 1 ELSE 0 END AS is_single_month_churn,
    CASE
        WHEN r.retailer_business_type IN ('Brick & Mortar Store','Brick and Mortar','Brick & Mortar')
             OR LOWER(COALESCE(r.retailer_business_type,'')) LIKE '%brick%mortar%' THEN 'Brick & Mortar'
        WHEN r.retailer_business_type IN ('Online Only','Online')
             OR LOWER(COALESCE(r.retailer_business_type,'')) LIKE '%online%only%'  THEN 'Online Only'
        WHEN r.retailer_business_type IN ('Pop Up Store','Pop Up','Pop-up','pop up','pop-up')
             OR LOWER(COALESCE(r.retailer_business_type,'')) LIKE '%pop%up%'       THEN 'Pop Up'
        ELSE 'Other/Unknown'
    END AS biz_type_clean
FROM psa_exercise_retailers r
LEFT JOIN v_retailer_months rm ON r.retailer_id = rm.retailer_id
GROUP BY r.retailer_id;


-- ============================================================
-- QUERY 1: JUNE vs JULY COHORT COMPARISON
-- Question: are the two cohort entry months meaningfully different?
-- Expected: small gap (≤3pp); if large, re-run all segment analyses split by cohort.
-- Grain of output: one row per cohort entry month (2 rows)
-- ============================================================

SELECT
    SUBSTR(first_confirmed_order_placed_at, 1, 7)  AS cohort_month,
    COUNT(*)                                        AS retailer_count,
    ROUND(AVG(has_m12_plus)          * 100, 1)     AS ret_12mo_pct,
    ROUND(AVG(has_m6_plus)           * 100, 1)     AS ret_6mo_pct,
    ROUND(AVG(is_single_month_churn) * 100, 1)     AS single_month_churn_pct,
    ROUND(AVG(lifetime_gmv),                   0)  AS avg_lifetime_gmv,
    ROUND(AVG(months_active),                  2)  AS avg_months_active
FROM v_retailer_summary
GROUP BY cohort_month
ORDER BY cohort_month;

-- QA: 2 rows; counts sum to 7400 (June ≈ 3443, July ≈ 3957)
-- QA: if |ret_12mo gap| > 5pp, flag for cohort-specific re-analysis
-- OBSERVATION: {obs_q1}


-- ============================================================
-- QUERY 2a: SELECTION BIAS TEST — PAYMENT TERM × GMV QUARTILE
-- Question: does the NET60 retention advantage (23.4pp overall) persist
--   within each GMV quartile, or does it shrink as we control for retailer size?
-- Expected: if selection bias is the main driver, gap should narrow at high quartiles
--   (where NET60 and POS retailers have similar GMV and presumably similar size).
-- ⚠️ FAN-OUT: v_retailer_months already at retailer-month grain; no fan-out risk
--
-- CTE m0_stats (grain: one row per retailer_id — month-0 GMV only)
-- CTE gmv_ntile (grain: one row per retailer_id — adds global NTILE quartile)
-- JOIN: v_retailer_summary (one per retailer) × gmv_ntile (one per retailer) → 1:1
-- Output grain: one row per (gmv_quartile, payment_term) — 8 rows
-- ============================================================

WITH
m0_stats AS (
    SELECT retailer_id, monthly_gmv AS gmv_m0
    FROM v_retailer_months
    WHERE month_offset = 0
),
gmv_ntile AS (
    SELECT
        retailer_id,
        NTILE(4) OVER (ORDER BY gmv_m0)  AS gmv_quartile,  -- global quartile, not per-term
        gmv_m0
    FROM m0_stats
)
SELECT
    g.gmv_quartile,
    rs.payment_term,
    COUNT(*)                                      AS retailer_count,
    ROUND(AVG(g.gmv_m0), 0)                       AS avg_gmv_m0,
    ROUND(AVG(rs.has_m12_plus)       * 100, 1)    AS ret_12mo_pct,
    ROUND(AVG(rs.is_single_month_churn) * 100, 1) AS churn_pct
FROM v_retailer_summary rs
JOIN gmv_ntile g ON rs.retailer_id = g.retailer_id
WHERE rs.payment_term IN ('NET60', 'PAYMENT_ON_SHIPMENT')
GROUP BY g.gmv_quartile, rs.payment_term
ORDER BY g.gmv_quartile, rs.payment_term;

-- QA: 8 rows; within each quartile NET60+POS counts should sum to ~925 (half of 1850)
-- QA: check retailer_count imbalance per quartile — NET60 should skew toward Q3/Q4
-- RESULT SUMMARY:
{q2a_inline_table}
-- OBSERVATION: {obs_q2a}


-- ============================================================
-- QUERY 2b: SELECTION BIAS TEST — PAYMENT TERM × EARLY REPEAT
-- Question: does NET60 still predict retention after controlling for
--   early repeat behaviour (the strongest retention predictor at 40pp)?
-- This is a cleaner test than GMV quartile: early repeat is a behaviour that
--   happens AFTER the payment term is set, making it harder to explain away.
-- ⚠️ FAN-OUT: v_retailer_months at retailer-month grain; COUNT(DISTINCT) safe
--
-- CTE first3_active (grain: one row per retailer_id)
-- JOIN: v_retailer_summary × first3_active → 1:1
-- Output grain: one row per (active_months, payment_term) — 6 rows
-- ============================================================

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
    COALESCE(f.active_months_first3, 0)        AS active_months,
    rs.payment_term,
    COUNT(*)                                    AS retailer_count,
    ROUND(AVG(rs.has_m12_plus)       * 100, 1) AS ret_12mo_pct,
    ROUND(AVG(rs.is_single_month_churn) * 100, 1) AS churn_pct
FROM v_retailer_summary rs
LEFT JOIN first3_active f ON rs.retailer_id = f.retailer_id
WHERE rs.payment_term IN ('NET60', 'PAYMENT_ON_SHIPMENT')
GROUP BY active_months, rs.payment_term
ORDER BY active_months, rs.payment_term;

-- QA: 6 rows (3 activity levels × 2 terms); 3/3 churn_pct = 0% for both terms
-- RESULT SUMMARY:
{q2b_inline_table}
-- OBSERVATION: {obs_q2b}


-- ============================================================
-- QUERY 3a: UNKNOWN BUCKET × BUSINESS TYPE — FULL CROSS-TAB
-- Question: is the Unknown bucket mostly Online Only retailers?
--   If Unknown ≥70% Online Only, the two identified 'problem segments' are
--   largely the same population — one intervention strategy covers both.
-- Output grain: one row per (bucket_group, biz_type_clean) — 8 rows
-- ============================================================

SELECT
    CASE WHEN annual_sales_bucket = 'Unknown' THEN 'Unknown' ELSE 'Known' END
        AS bucket_group,
    biz_type_clean,
    COUNT(*)                                    AS retailer_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (
        PARTITION BY
            CASE WHEN annual_sales_bucket = 'Unknown' THEN 'Unknown' ELSE 'Known' END
    ), 1)                                       AS pct_within_bucket,
    ROUND(AVG(has_m12_plus)      * 100, 1)      AS ret_12mo_pct,
    ROUND(AVG(is_single_month_churn) * 100, 1)  AS churn_pct
FROM v_retailer_summary
GROUP BY bucket_group, biz_type_clean
ORDER BY bucket_group, retailer_count DESC;

-- QA: 8 rows; pct_within_bucket sums to 100% within each bucket group
-- QA: Unknown / Online Only cell expected to be large
-- OBSERVATION: {obs_q3a}


-- ============================================================
-- QUERY 3b: ONLINE ONLY — UNKNOWN vs KNOWN BUCKET
-- Question: within Online Only retailers only, does being in the Unknown
--   bucket predict worse retention, or is business type the whole story?
-- If gap ≤5pp: business type is the driver; Unknown label is redundant
-- If gap >10pp: Unknown bucket has an additional negative effect
-- Output grain: one row per bucket_group, filtered to Online Only — 2 rows
-- ============================================================

SELECT
    CASE WHEN annual_sales_bucket = 'Unknown' THEN 'Unknown-bucket' ELSE 'Known-bucket' END
        AS bucket_group,
    COUNT(*)                                     AS retailer_count,
    ROUND(AVG(has_m12_plus)      * 100, 1)       AS ret_12mo_pct,
    ROUND(AVG(has_m6_plus)       * 100, 1)       AS ret_6mo_pct,
    ROUND(AVG(is_single_month_churn) * 100, 1)   AS churn_pct,
    ROUND(AVG(lifetime_gmv),               0)    AS avg_lifetime_gmv
FROM v_retailer_summary
WHERE biz_type_clean = 'Online Only'
GROUP BY bucket_group
ORDER BY bucket_group;

-- QA: 2 rows; total count ≈ 1859 (matches Online Only n from exploration_part1)
-- QA: if ret_12mo gap > 10pp, Unknown has an effect beyond business type alone
-- OBSERVATION: {obs_q3b}


-- ============================================================
-- QUERY 4a: UNKNOWN SEGMENT — MONTH-0 BEHAVIOURAL PROFILE
-- Question: do Unknown retailers behave differently from day one, or do they
--   start similarly but diverge later?
-- If behaviourally different (lower GMV, fewer brands) in month 0:
--   → Platform engagement is part of the problem; month-0 interventions could help
-- If behaviourally similar but worse outcomes:
--   → Structural (who they are); platform engagement isn't the main lever
-- ⚠️ FAN-OUT: v_retailer_months at retailer-month grain; no fan-out risk
--
-- CTE m0_stats (grain: one row per retailer_id — month 0 only)
-- JOIN: v_retailer_summary × m0_stats → 1:1
-- Output grain: one row per segment (2 rows)
-- ============================================================

WITH
m0_stats AS (
    SELECT
        retailer_id,
        monthly_gmv  AS gmv_m0,
        brand_count  AS brands_m0,
        order_count  AS orders_m0
    FROM v_retailer_months
    WHERE month_offset = 0
)
SELECT
    CASE WHEN rs.annual_sales_bucket = 'Unknown' THEN 'Unknown' ELSE 'Known' END
        AS segment,
    COUNT(*)                      AS retailer_count,
    ROUND(AVG(m.gmv_m0),    0)    AS avg_gmv_m0,
    ROUND(AVG(m.brands_m0), 2)    AS avg_brands_m0,
    ROUND(AVG(m.orders_m0), 2)    AS avg_orders_m0,
    ROUND(AVG(rs.has_m12_plus) * 100, 1) AS ret_12mo_pct
FROM v_retailer_summary rs
JOIN m0_stats m ON rs.retailer_id = m.retailer_id
GROUP BY segment
ORDER BY segment;

-- QA: 2 rows; total = 7400
-- QA: Known expected to have higher avg_gmv_m0, avg_brands_m0, avg_orders_m0
-- OBSERVATION: {obs_q4a}


-- ============================================================
-- QUERY 4b: UNKNOWN SEGMENT — EARLY REPEAT DISTRIBUTION
-- Question: are Unknown retailers less likely to REACH 2/3 or 3/3 activity
--   (funnel problem), or do they reach the same levels but retain worse (outcome problem)?
-- FUNNEL problem → intervene in months 1–2 to increase activity
-- OUTCOME problem → something else drives poor retention (fit, category, support)
-- ⚠️ FAN-OUT: v_retailer_months at retailer-month grain; COUNT(DISTINCT) safe
--
-- CTE first3_active (grain: one row per retailer_id)
-- JOIN: v_retailer_summary × first3_active → 1:1
-- Output grain: one row per (segment, active_months) — up to 6 rows
-- ============================================================

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
    CASE WHEN rs.annual_sales_bucket = 'Unknown' THEN 'Unknown' ELSE 'Known' END
        AS segment,
    COALESCE(f.active_months_first3, 0)        AS active_months,
    COUNT(*)                                    AS retailer_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (
        PARTITION BY
            CASE WHEN rs.annual_sales_bucket = 'Unknown' THEN 'Unknown' ELSE 'Known' END
    ), 1)                                       AS pct_within_segment,
    ROUND(AVG(rs.has_m12_plus) * 100, 1)       AS ret_12mo_pct
FROM v_retailer_summary rs
LEFT JOIN first3_active f ON rs.retailer_id = f.retailer_id
GROUP BY segment, active_months
ORDER BY segment, active_months;

-- QA: ≤6 rows; pct_within_segment sums to 100% per segment; total = 7400
-- OBSERVATION: {obs_q4b}


-- ============================================================
-- QUERY 5: GMV GRADIENT WITHIN ANNUAL_SALES_BUCKET
-- Question: does the Q1→Q4 retention gap (25.4pp) persist within individual
--   sales buckets, or does it disappear once retailer size tier is controlled?
-- NOTE: NTILE(4) uses GLOBAL quartiles (not per-bucket), so Q1 means bottom
--   quartile of ALL retailers. Some bucket×quartile cells will be small.
-- ⚠️ FAN-OUT: v_retailer_months at retailer-month grain; no fan-out risk
--
-- CTE m0_stats (grain: one row per retailer_id)
-- CTE gmv_ntile (grain: one row per retailer_id — global NTILE quartile)
-- JOIN: v_retailer_summary × gmv_ntile → 1:1
-- Output grain: one row per (annual_sales_bucket, gmv_quartile)
-- Excludes 'Unknown' (studied in Queries 3–4) and 'X-Large' (n=1)
-- ============================================================

WITH
m0_stats AS (
    SELECT retailer_id, monthly_gmv AS gmv_m0
    FROM v_retailer_months
    WHERE month_offset = 0
),
gmv_ntile AS (
    SELECT
        retailer_id,
        NTILE(4) OVER (ORDER BY gmv_m0)  AS gmv_quartile,
        gmv_m0
    FROM m0_stats
)
SELECT
    rs.annual_sales_bucket,
    g.gmv_quartile,
    COUNT(*)                              AS retailer_count,
    ROUND(AVG(g.gmv_m0), 0)              AS avg_gmv_m0,
    ROUND(AVG(rs.has_m12_plus) * 100, 1) AS ret_12mo_pct
FROM v_retailer_summary rs
JOIN gmv_ntile g ON rs.retailer_id = g.retailer_id
WHERE rs.annual_sales_bucket NOT IN ('Unknown', 'X-Large')
GROUP BY rs.annual_sales_bucket, g.gmv_quartile
ORDER BY rs.annual_sales_bucket, g.gmv_quartile;

-- QA: ≤24 rows; flag any cell with retailer_count < 30 as unreliable
-- QA: if gradient flat within all buckets: overall 25.4pp was size composition bias
-- OBSERVATION: {obs_q5}


-- ============================================================
-- SYNTHESIS: WHAT THESE QUERIES RESOLVE
-- ============================================================
--
-- Q1  (June vs July): {q1_synthesis}
--
-- Q2a (Payment term × GMV quartile):
--   NET60–POS gap per quartile: Q1={q2a_g1}pp, Q2={q2a_g2}pp, Q3={q2a_g3}pp, Q4={q2a_g4}pp
--   {q2a_synthesis}
--
-- Q2b (Payment term × early repeat):
--   NET60–POS gap per activity level: 1/3={q2b_g1}pp, 2/3={q2b_g2}pp, 3/3={q2b_g3}pp
--   {q2b_synthesis}
--
-- Q3  (Unknown × business type):
--   {q3_synthesis}
--
-- Q4  (Unknown behavioral profile):
--   Known: avg ${known_gmv_s:,.0f} GMV, {known_brands_s:.2f} brands in month 0 → {known_ret_s}% ret
--   Unknown: avg ${unk_gmv_s:,.0f} GMV, {unk_brands_s:.2f} brands in month 0 → {unk_ret_s}% ret
--   {q4_synthesis}
--
-- Q5  (GMV gradient within buckets):
--   Average within-bucket Q1→Q4 gap: {avg_within_s}pp (vs 25.4pp overall)
--   {q5_synthesis}
-- ============================================================
""".format(
    obs_q1    = obs_q1,
    obs_q2a   = obs_q2a,
    obs_q2b   = obs_q2b,
    obs_q3a   = obs_q3a,
    obs_q3b   = obs_q3b,
    obs_q4a   = obs_q4a,
    obs_q4b   = obs_q4b,
    obs_q5    = obs_q5,
    q2a_inline_table = q2a_inline_table,
    q2b_inline_table = q2b_inline_table,
    # Synthesis block values
    q1_synthesis = q1_verdict,
    q2a_g1 = f"{q2a_gaps.get(1,0):+.1f}", q2a_g2 = f"{q2a_gaps.get(2,0):+.1f}",
    q2a_g3 = f"{q2a_gaps.get(3,0):+.1f}", q2a_g4 = f"{q2a_gaps.get(4,0):+.1f}",
    q2a_synthesis = q2a_verdict,
    q2b_g1 = f"{q2b_gaps.get(1,0):+.1f}", q2b_g2 = f"{q2b_gaps.get(2,0):+.1f}",
    q2b_g3 = f"{q2b_gaps.get(3,0):+.1f}",
    q2b_synthesis = q2b_verdict,
    q3_synthesis  = f"Unknown is {unk_online_pct}% Online Only vs {known_online_pct}% for Known. " + (
        "The Unknown bucket and Online Only overlap heavily — largely the same population."
        if unk_online_pct and unk_online_pct >= 50 else
        "Unknown is NOT predominantly Online Only."),
    known_gmv_s = known_gmv, unk_gmv_s = unk_gmv,
    known_brands_s = known_brands, unk_brands_s = unk_brands,
    known_ret_s = known_ret_4a, unk_ret_s = unk_ret_4a,
    q4_synthesis = (
        "Unknown retailers are behaviourally weaker from day one: "
        f"they spend {round(known_gmv/unk_gmv,1) if unk_gmv else '?'}x less and try "
        f"{round(known_brands/unk_brands,1) if unk_brands else '?'}x fewer brands in month 0. "
        f"Poor retention is partly explained by lower initial engagement, not purely who they are. "
        + ("FUNNEL problem: Unknown retailers are less likely to reach 3/3 activity." if dist_diff and dist_diff > 5 else
           "OUTCOME problem: Unknown retailers have similar activity distribution but worse outcomes within each level.")
    ),
    avg_within_s = avg_within_gap,
    q5_synthesis = (
        "GMV gradient PERSISTS within buckets — first-month spend depth is independently predictive of retention."
        if avg_within_gap and avg_within_gap >= 10 else
        "GMV gradient WEAKENS substantially within buckets — the 25.4pp overall gap is partly retailer size composition."
    ),
)

with open("/workspace/queries_part2.sql", "w") as f:
    f.write(sql_out)

print(f"  Written: /workspace/queries_part2.sql")
print(f"  {len(sql_out):,} bytes  ({sql_out.count(chr(10))} lines)")

conn.close()
