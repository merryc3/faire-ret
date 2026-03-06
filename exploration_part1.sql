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

-- OBSERVATION: Retailers: 7,400 rows = 7,400 unique IDs (one-per-retailer grain confirmed). Orders: 259,416 rows but only 209,551 distinct ORDER_IDs — confirms brand-level grain. 13,852 distinct brands. 2,922 retailers missing FIRST_APP_SESSION_AT.


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

-- OBSERVATION: Channel: Organic dominates at 57.4% of cohort. Annual sales: Unknown is largest single bucket at 2,285 (30.9% of cohort) — concerning. Payment terms: NET60 3,486 vs POS 3,899. App install: 4,475 installed (60.5%). Cohort split: June 3,443 / July 3,957 retailers.


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
-- Replace 1079681 with any ORDER_ID from Step 1 result
SELECT order_id, retailer_id, brand_id, order_created,
       order_state, ROUND(total_gmv, 2) AS total_gmv
FROM psa_exercise_orders
WHERE order_id = 1079681
ORDER BY brand_id;

-- OBSERVATION: Confirmed: ORDER_ID 1079681 has 63 rows across 41 distinct brands. TOTAL_GMV is per-brand-per-order, NOT the order total. Always use COUNT(DISTINCT order_id) for order counts and SUM(total_gmv) for GMV.


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

-- OBSERVATION: PROCESSING: 99.998% of all rows (4 CANCELED). CANCELED share is negligible — safe to filter WHERE order_state='PROCESSING' for all analysis. No NULL order_state rows.


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

-- QA: 19 rows expected (Jun 2020 – Dec 2021)
-- QA: first row active_retailers = 7400 (full cohort in month 0)
-- OBSERVATION: 19 calendar months (Jun 2020 – Dec 2021). Month 0 had 3,443 active retailers (full cohort). By last month only 2,057 active — significant attrition. GMV peak in 2021-10 at $10,076,648 — likely seasonal (holiday). GMV per active retailer is a more stable signal than absolute GMV.


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
-- OBSERVATION: NDR validates against reference: month1=58.94%≈59% ✓, month5=48.85%≈49% ✓, month12=75.01%≈75% ✓. The aggregate cohort formula (total_GMV_t / total_GMV_0) is correct. NDR dips to 48.9% at month 5, then recovers above 100% by month 12 — survivorship bias: only highest-GMV retailers remain active that long.


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
-- OBSERVATION: Month 0: 100% active (all 7,400 cohort retailers). Month 1: only 45.2% still active — 54.8pp drop in a single month. Month 12: 33.7% active. IMPORTANT: NDR ≠ active retailer rate. NDR weighs by GMV, so the 75.0% NDR at month 12 reflects that surviving retailers spend MORE than they did in month 0.


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
-- OBSERVATION: NET60: 69.8% 12-mo retention vs POS: 46.4% — gap = 23.4pp. NET60 also has higher avg lifetime GMV ($23,356 vs $12,893). IMPORTANT: this is likely SELECTION BIAS not a treatment effect. NET60 requires pre-qualification; these retailers are probably larger and more established regardless of payment term. This gap raises the question: is NET60 causing retention or just tagging retailers who were already going to retain?


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
-- OBSERVATION: App installed: 65.9% 12-mo retention vs no app: 44.4% — gap = 21.5pp. Single-month churn: 11.2% with app vs 30.1% without — 18.9pp higher churn for non-app. Large gap BUT the snapshot nature of FLAG_APP_INSTALLED means retained retailers may have installed the app BECAUSE they stayed engaged (reverse causality). Next step: use FIRST_APP_SESSION_AT to check whether retailers who installed BEFORE their first order differ from those who installed much later.


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
-- OBSERVATION: Unknown bucket: 37.8% 12-mo retention — 31.8pp below best known bucket (Medium: 69.6%). Unknown has 2,285 retailers (30.9% of cohort) and 27.7% single-month churn vs 0.0% for worst known bucket. Unknown is NOT just a slightly different segment — it is a deeply different population. Possible explanations: (1) these are brand-new online retailers with no purchase history; (2) data collection failure at signup; (3) different onboarding experience. Worth investigating: what % of Unknown are Online Only vs Brick & Mortar?


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
-- OBSERVATION: Online Only: 39.1% 12-mo retention (1,859 retailers) vs Brick & Mortar: 66.5% — gap = 27.4pp. Online Only has 24.2% single-month churn vs 15.4% for B&M. Pop Up: 54.9%. Online-only retailers may be testing Faire without deep commitment, or may have lower average order values and less urgency for wholesale. IMPORTANT: there is significant overlap between Online Only and the Unknown ANNUAL_SALES_BUCKET — these may be the same underlying population (new online retailers with no prior sales history). Worth cross-tabbing.


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
-- OBSERVATION: Best 12-mo retention: gift (64.8%). Worst: other (43.7%). Gap across top-10 types: 21.1pp. Highest avg lifetime GMV: mercantile_general ($30,245). Store type gaps are real but smaller than behavioral gaps (Section 4). 'other' and 'unknown' types both have low retention — these may overlap heavily with the Unknown annual_sales_bucket population.


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
-- OBSERVATION: Channel retention ranges: Partnerships lowest at 35.2%, Organic highest at 59.4% — gap = 24.2pp. Partnerships: only 88 retailers — too small for reliable conclusions. Organic (59.4%) vs Paid: small gap suggests acquisition channel is a WEAK predictor of retention compared to behavioral signals (Section 4). This is reassuring — it means retention is driven by on-platform behavior more than where the retailer came from.


-- ============================================================
-- SECTION 3 SUMMARY: RETENTION GAPS BY DIMENSION (RANKED)
-- ============================================================
--
-- Dimension                                Gap     Notes
-- ─────────────────────────────────────────────────────────
--   3c. Annual sales Unknown vs best           +31.8pp  (profile — overlaps Online Only)
--   3d. Business type (B&M vs Online)          +27.4pp  (profile — overlaps Unknown bucket)
--   3f. Channel (best vs worst)                +24.2pp  (profile — weak signal)
--   3a. Payment term (NET60 vs POS)            +23.4pp  (profile — selection bias likely)
--   3b. App install (1 vs 0)                   +21.5pp  (profile — reverse causality risk)
--   3e. Store type (best vs worst)             +21.1pp  (profile — moderate signal)
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
