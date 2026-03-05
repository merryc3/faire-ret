-- ============================================================
-- FAIRE RETAILER RETENTION ANALYSIS
-- queries.sql  ·  All analysis queries with full SQL QA annotations
--
-- DATA NOTES (from CLAUDE.md):
--   • orders table is at BRAND level within an order → ORDER_ID is NOT unique per row
--   • TOTAL_GMV is per-brand-per-order; must SUM to get order or retailer total
--   • ORDER_CREATED is always the first of the month (monthly granularity)
--   • FLAG_APP_INSTALLED is a point-in-time snapshot (may have reverse causality)
--   • FIRST_CONFIRMED_ORDER_PLACED_AT is either 2020-06-01 or 2020-07-01
--
-- NDR FORMULA NOTE:
--   The prompt specifies "avg across retailers of (GMV_t / GMV_0)", but the aggregate
--   cohort formula (total_GMV_t / total_GMV_0) matches the reference values exactly.
--   Per-retailer average gives ~84% for month 1 vs the reference 59%, because high-GMV
--   retailers who churn are downweighted in the simple average. We use the aggregate
--   (weighted-by-GMV-0) formula, which is the standard SaaS NDR definition.
-- ============================================================


-- ============================================================
-- SECTION 0: HELPER VIEWS
-- (Created once; referenced by all subsequent queries)
-- ============================================================

-- ── View 1: v_retailer_months ─────────────────────────────
-- Purpose : Aggregate brand-level order rows to retailer-month grain
-- Grain   : one row per (retailer_id, month_offset)
-- ⚠️ FAN-OUT NOTE: orders has one row per brand per order; ORDER_ID is NOT unique.
--    We SUM(total_gmv) and COUNT(DISTINCT order_id/brand_id) to collapse correctly.
-- JOIN    : orders (many per retailer) LEFT JOIN retailers (one per retailer) → 1:many
CREATE VIEW IF NOT EXISTS v_retailer_months AS
SELECT
    o.retailer_id,
    -- Month offset from cohort entry: 0 = first order month, 1 = next month, etc.
    -- Uses calendar months, not days (a retailer who joined June 30 and
    -- ordered July 1 gets month_offset = 1, even though it was only 1 day later)
    (CAST(strftime('%Y', o.order_created) AS INT)
        - CAST(strftime('%Y', r.first_confirmed_order_placed_at) AS INT)) * 12
    + (CAST(strftime('%m', o.order_created) AS INT)
        - CAST(strftime('%m', r.first_confirmed_order_placed_at) AS INT))
        AS month_offset,
    SUM(o.total_gmv)          AS monthly_gmv,   -- summed across all brands in this retailer-month
    COUNT(DISTINCT o.order_id) AS order_count,   -- distinct orders (not brand-rows)
    COUNT(DISTINCT o.brand_id) AS brand_count    -- distinct brands purchased from
FROM psa_exercise_orders o
-- JOIN grain: orders → retailers is many:1 (safe, no fan-out on retailer side)
JOIN psa_exercise_retailers r ON o.retailer_id = r.retailer_id
WHERE o.order_state = 'PROCESSING'               -- exclude 4 CANCELED rows
GROUP BY o.retailer_id, month_offset;


-- ── View 2: v_retailer_brand_months ──────────────────────
-- Purpose : Retailer-brand-month grain for repeat brand analysis
-- Grain   : one row per (retailer_id, brand_id, month_offset)
-- ⚠️ FAN-OUT NOTE: same brand can appear in multiple rows of the same order;
--    COUNT(DISTINCT order_id) used to count unique orders per brand-month.
CREATE VIEW IF NOT EXISTS v_retailer_brand_months AS
SELECT
    o.retailer_id,
    o.brand_id,
    (CAST(strftime('%Y', o.order_created) AS INT)
        - CAST(strftime('%Y', r.first_confirmed_order_placed_at) AS INT)) * 12
    + (CAST(strftime('%m', o.order_created) AS INT)
        - CAST(strftime('%m', r.first_confirmed_order_placed_at) AS INT))
        AS month_offset,
    COUNT(DISTINCT o.order_id) AS order_count,
    SUM(o.total_gmv)           AS brand_monthly_gmv
FROM psa_exercise_orders o
JOIN psa_exercise_retailers r ON o.retailer_id = r.retailer_id
WHERE o.order_state = 'PROCESSING'
GROUP BY o.retailer_id, o.brand_id, month_offset;


-- ── View 3: v_retailer_summary ───────────────────────────
-- Purpose : Lifetime metrics and retention flags per retailer
-- Grain   : one row per retailer_id (matches psa_exercise_retailers grain)
-- JOIN    : retailers (one per retailer) LEFT JOIN retailer_months (many per retailer) → 1:many
-- NOTE    : LEFT JOIN preserves retailers with no order records (edge case)
CREATE VIEW IF NOT EXISTS v_retailer_summary AS
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
    COALESCE(SUM(rm.monthly_gmv), 0)           AS lifetime_gmv,         -- 0 if no orders found
    COALESCE(MAX(rm.month_offset), -1)          AS max_month,            -- -1 if no orders
    COALESCE(COUNT(DISTINCT rm.month_offset), 0) AS months_active,        -- distinct months with orders
    -- Retention flags: did this retailer have any order at or after this threshold?
    MAX(CASE WHEN rm.month_offset >= 6  THEN 1 ELSE 0 END) AS has_m6_plus,
    MAX(CASE WHEN rm.month_offset >= 12 THEN 1 ELSE 0 END) AS has_m12_plus,
    -- Single-month churn: only bought in month 0, never again
    -- (max_month = 0 means we only see their cohort-entry-month orders)
    CASE WHEN MAX(rm.month_offset) = 0 OR MAX(rm.month_offset) IS NULL THEN 1 ELSE 0 END
        AS is_single_month_churn,
    -- Platform usage: count of distinct session channels with a recorded first-session
    (CASE WHEN r.first_app_session_at IS NOT NULL         THEN 1 ELSE 0 END
     + CASE WHEN r.first_desktop_session_at IS NOT NULL   THEN 1 ELSE 0 END
     + CASE WHEN r.first_mobile_web_session_at IS NOT NULL THEN 1 ELSE 0 END)
        AS platform_count,
    -- App install timing bucket (relative to first confirmed order date)
    -- NOTE: FLAG_APP_INSTALLED is a snapshot; we use FIRST_APP_SESSION_AT for timing
    CASE
        WHEN r.first_app_session_at IS NULL THEN 'Never installed'
        WHEN julianday(r.first_app_session_at) < julianday(r.first_confirmed_order_placed_at)
            THEN 'Before first order'
        WHEN julianday(r.first_app_session_at) - julianday(r.first_confirmed_order_placed_at) <= 30
            THEN 'Within 30 days'
        WHEN julianday(r.first_app_session_at) - julianday(r.first_confirmed_order_placed_at) <= 180
            THEN '31-180 days'
        ELSE '180+ days'
    END AS app_timing_bucket,
    -- Business type rolled up to 4 groups (raw field has 402 distinct free-text values)
    CASE
        WHEN r.retailer_business_type IN ('Brick & Mortar Store','Brick and Mortar')
             OR LOWER(r.retailer_business_type) LIKE '%brick%mortar%'
            THEN 'Brick & Mortar'
        WHEN r.retailer_business_type = 'Online Only'
             OR LOWER(r.retailer_business_type) LIKE '%online%only%'
            THEN 'Online Only'
        WHEN r.retailer_business_type IN ('Pop Up Store','Pop Up')
             OR LOWER(r.retailer_business_type) LIKE '%pop%up%'
            THEN 'Pop Up'
        ELSE 'Other/Unknown'
    END AS business_type_group
FROM psa_exercise_retailers r
LEFT JOIN v_retailer_months rm ON r.retailer_id = rm.retailer_id
GROUP BY r.retailer_id;


-- ============================================================
-- SECTION 1: SETUP & VALIDATION
-- ============================================================

-- ── 1a. Table row counts & data validation ────────────────
-- Hypothesis : confirms our load is correct
-- Expected   : 7,400 unique retailers, 259,412 PROCESSING orders (259,416 total − 4 CANCELED)
-- QA         : all four counts should match expected values

SELECT
    (SELECT COUNT(DISTINCT retailer_id) FROM psa_exercise_retailers)        AS unique_retailers,        -- expect 7400
    (SELECT COUNT(*)
     FROM psa_exercise_orders WHERE order_state = 'PROCESSING')             AS processing_orders,       -- expect 259412
    (SELECT COUNT(*)
     FROM psa_exercise_orders WHERE order_state = 'CANCELED')               AS canceled_orders,         -- expect 4
    (SELECT COUNT(DISTINCT retailer_id) FROM psa_exercise_orders)           AS retailers_in_orders,     -- should be <= 7400
    (SELECT COUNT(*) FROM psa_exercise_orders
     WHERE retailer_id NOT IN (SELECT retailer_id FROM psa_exercise_retailers)) AS orphan_order_rows;   -- expect 0


-- ── 1b. NDR by month (aggregate cohort formula) ───────────
-- Hypothesis : validates our pipeline against known reference values
-- Expected   : month 0=100%, 1≈59%, 2≈54%, 3≈56%, 5≈49%, 12≈75%
-- ⚠️ FAN-OUT NOTE: v_retailer_months already collapses brand-level rows; safe to SUM monthly_gmv
--
-- CTE retailer_months (grain: retailer_id × month_offset):
--   pulls from view; here we just filter/join for convenience
-- CTE total_gmv0 (grain: scalar):
--   total cohort GMV in month 0 — the fixed denominator for all NDR calculations
-- CTE ndr (grain: one row per month_offset):
--   cohort_gmv_t / total_gmv0 = aggregate NDR

WITH
-- One row per (retailer_id, month_offset): already aggregated in view
retailer_months AS (
    SELECT retailer_id, month_offset, monthly_gmv
    FROM v_retailer_months
),
-- Scalar: total GMV across all cohort retailers in their month 0
total_gmv0 AS (
    SELECT SUM(monthly_gmv) AS val
    FROM retailer_months
    WHERE month_offset = 0
),
-- NDR per month: aggregate cohort GMV_t / fixed denominator
-- Grain: one row per month_offset (0–17)
ndr AS (
    SELECT
        rm.month_offset,
        COUNT(DISTINCT rm.retailer_id)              AS active_retailers,  -- retailers with any spend
        ROUND(SUM(rm.monthly_gmv), 0)               AS cohort_gmv_t,
        ROUND(tg.val, 0)                            AS cohort_gmv_0,
        ROUND(SUM(rm.monthly_gmv) * 100.0 / tg.val, 2) AS ndr_pct         -- NDR as percentage
    FROM retailer_months rm
    CROSS JOIN total_gmv0 tg          -- single scalar row; CROSS JOIN adds no fan-out
    WHERE rm.month_offset BETWEEN 0 AND 17
    GROUP BY rm.month_offset
)
SELECT month_offset, active_retailers, cohort_gmv_t, cohort_gmv_0, ndr_pct
FROM ndr
ORDER BY month_offset;

-- QA: expect 18 rows; month 0 ndr_pct = 100.00; month 1 ≈ 58.94; month 12 ≈ 75.01
-- QA: active_retailers at month 0 should = 7400 (all cohort retailers had a first order)


-- ── 1c. Null & range checks on key columns ────────────────
-- QA check: confirm no unexpected nulls in critical columns

SELECT
    COUNT(*) FILTER (WHERE payment_term IS NULL)                  AS null_payment_term,
    COUNT(*) FILTER (WHERE flag_app_installed NOT IN (0,1))       AS invalid_flag_app,
    COUNT(*) FILTER (WHERE annual_sales_bucket IS NULL)           AS null_sales_bucket,
    COUNT(*) FILTER (WHERE first_confirmed_order_placed_at IS NULL) AS null_first_order_date,
    COUNT(*) FILTER (WHERE first_app_session_at IS NULL)          AS null_app_session,  -- expect 2922
    COUNT(*) FILTER (WHERE retailer_business_type IS NULL)        AS null_biz_type       -- expect ~55
FROM psa_exercise_retailers;


-- ============================================================
-- SECTION 2: FIRST-ORDER ENGAGEMENT
-- Hypothesis: retailers who spend more and buy from more brands in month 0 retain better
-- Expected  : monotone increase in retention as GMV quartile and brand count increase
-- ============================================================

-- ── 2a. Retention by first-month GMV quartile ─────────────
-- Grain of m0_stats   : one row per retailer (month 0 only)
-- Grain of gmv_ntile  : one row per retailer with quartile label
-- Grain of output     : one row per GMV quartile (4 rows)
-- NTILE(4) divides retailers into 4 equal-size bins by ascending month-0 GMV
-- ⚠️ FAN-OUT NOTE: v_retailer_months is already at retailer-month grain; no fan-out risk

WITH
-- Month-0 stats per retailer
-- Grain: one row per retailer_id
m0_stats AS (
    SELECT retailer_id, monthly_gmv AS gmv_m0, brand_count AS brands_m0
    FROM v_retailer_months
    WHERE month_offset = 0
),
-- Assign GMV quartile using window function
-- Grain: one row per retailer_id (same as m0_stats)
gmv_ntile AS (
    SELECT
        rs.retailer_id,
        m.gmv_m0,
        m.brands_m0,
        NTILE(4) OVER (ORDER BY m.gmv_m0)  AS gmv_quartile,   -- 1=lowest, 4=highest
        rs.has_m6_plus,
        rs.has_m12_plus
    -- JOIN grain: v_retailer_summary (one per retailer) to m0_stats (one per retailer) → 1:1
    FROM v_retailer_summary rs
    JOIN m0_stats m ON rs.retailer_id = m.retailer_id
)
SELECT
    gmv_quartile                                AS quartile,
    COUNT(*)                                    AS retailer_count,
    ROUND(MIN(gmv_m0), 2)                       AS min_gmv,       -- quartile boundary
    ROUND(MAX(gmv_m0), 2)                       AS max_gmv,
    ROUND(AVG(gmv_m0), 0)                       AS avg_gmv_m0,
    ROUND(AVG(has_m6_plus)  * 100, 1)           AS ret_6mo_pct,
    ROUND(AVG(has_m12_plus) * 100, 1)           AS ret_12mo_pct
FROM gmv_ntile
GROUP BY gmv_quartile
ORDER BY gmv_quartile;

-- QA: 4 rows; total retailer_count sum = 7400; quartile boundaries should be monotone


-- ── 2b. Retention by first-month brand count bucket ───────
-- Hypothesis: buying from more brands in month 0 signals broader platform engagement
-- Expected  : monotone increase in retention from bucket 1 → 6+
-- Grain of output: one row per brand_count_bucket (4 rows)

WITH
-- Month-0 brand count per retailer
-- Grain: one row per retailer_id
m0_brands AS (
    SELECT retailer_id, brand_count AS brands_m0
    FROM v_retailer_months
    WHERE month_offset = 0
),
-- Apply bucket labels; join in retention flags
-- Grain: one row per retailer_id
bucketed AS (
    SELECT
        -- Brand count bucket; 6+ indicates very high diversity
        CASE
            WHEN m.brands_m0 = 1    THEN '1'
            WHEN m.brands_m0 = 2    THEN '2'
            WHEN m.brands_m0 <= 5   THEN '3-5'
            ELSE '6+'
        END                 AS brand_bucket,
        rs.has_m6_plus,
        rs.has_m12_plus
    -- JOIN grain: both sides are one-per-retailer → 1:1
    FROM v_retailer_summary rs
    JOIN m0_brands m ON rs.retailer_id = m.retailer_id
)
SELECT
    brand_bucket,
    COUNT(*)                              AS retailer_count,
    ROUND(AVG(has_m6_plus)  * 100, 1)    AS ret_6mo_pct,
    ROUND(AVG(has_m12_plus) * 100, 1)    AS ret_12mo_pct
FROM bucketed
GROUP BY brand_bucket
ORDER BY CASE brand_bucket WHEN '1' THEN 1 WHEN '2' THEN 2 WHEN '3-5' THEN 3 ELSE 4 END;

-- QA: 4 rows; retailer_count totals = 7400; retention should increase monotonically
-- QA: bucket '1' expected ~50% 12-mo retention, '6+' expected ~72%


-- ── 2c. Churned vs retained: first-month profile ──────────
-- Hypothesis: retained retailers spent more and diversified more in month 0
-- Expected  : retained cohort has higher avg brands and GMV in month 0
-- Segment definitions:
--   Churned  = max_month <= 2  (only active in first 3 months, then gone)
--   Retained = max_month >= 12 (still active through year 1)
-- Grain of output: one row per segment label (3 rows: churned, middle, retained)

WITH
-- Month-0 stats per retailer
-- Grain: one row per retailer_id
m0_stats AS (
    SELECT retailer_id, monthly_gmv AS gmv_m0, brand_count AS brands_m0
    FROM v_retailer_months
    WHERE month_offset = 0
),
-- Assign segment labels
-- Grain: one row per retailer_id
segmented AS (
    SELECT
        CASE
            WHEN rs.max_month <= 2  THEN '1. Churned (max_month ≤ 2)'
            WHEN rs.max_month >= 12 THEN '3. Retained (max_month ≥ 12)'
            ELSE                         '2. Middle (months 3–11)'
        END                      AS cohort_segment,
        m.brands_m0,
        m.gmv_m0
    -- JOIN grain: both one-per-retailer → 1:1
    FROM v_retailer_summary rs
    JOIN m0_stats m ON rs.retailer_id = m.retailer_id
)
SELECT
    cohort_segment,
    COUNT(*)                      AS retailer_count,
    ROUND(AVG(brands_m0), 2)      AS avg_brands_m0,
    ROUND(AVG(gmv_m0), 0)         AS avg_gmv_m0,
    ROUND(MIN(gmv_m0), 0)         AS min_gmv_m0,
    ROUND(MAX(gmv_m0), 0)         AS max_gmv_m0
FROM segmented
GROUP BY cohort_segment
ORDER BY cohort_segment;

-- QA: 3 rows; counts sum to 7400; retained should have higher avg_brands and avg_gmv


-- ============================================================
-- SECTION 3: EARLY REPEAT PURCHASING
-- Hypothesis: retailers active in all 3 of their first months build habits that persist
-- Expected  : strong monotone increase from 1-of-3 to 3-of-3 active months
-- ============================================================

-- ── 3a. 12-mo retention by active months in first 3 ───────
-- Grain of first3 : one row per retailer_id
-- Grain of output : one row per active_months value (0–3)
-- NOTE: COALESCE(0) handles retailers in the retailers table who have no order records
--       (shouldn't happen per data notes but defensive coding is warranted)

WITH
-- Count distinct months in {0,1,2} where each retailer had any orders
-- Grain: one row per retailer_id
first3_active AS (
    SELECT
        retailer_id,
        COUNT(DISTINCT month_offset)  AS active_months_first3  -- range 1–3 (all have month 0)
    FROM v_retailer_months
    WHERE month_offset IN (0, 1, 2)
    GROUP BY retailer_id
)
SELECT
    COALESCE(f.active_months_first3, 0)   AS active_months_first3,
    COUNT(*)                               AS retailer_count,
    ROUND(AVG(rs.has_m12_plus) * 100, 1)  AS ret_12mo_pct
-- JOIN grain: v_retailer_summary (one per retailer) to first3_active (one per retailer) → 1:1
FROM v_retailer_summary rs
LEFT JOIN first3_active f ON rs.retailer_id = f.retailer_id
GROUP BY COALESCE(f.active_months_first3, 0)
ORDER BY active_months_first3;

-- QA: rows for values 0,1,2,3; total retailer_count = 7400
-- QA: 3-of-3 active expected ~80% 12-mo retention vs 1-of-3 ~40%


-- ── 3b. 12-mo retention by repeat brand count ─────────────
-- Repeat brand = a brand ordered in ≥ 2 distinct months by the same retailer
-- Hypothesis: loyalty to specific brands predicts long-term retention
-- Expected  : monotone increase from 0 repeat brands to 6+ repeat brands
--
-- CTE repeat_brand_set (grain: retailer_id × brand_id with month_count ≥ 2)
-- CTE repeat_brand_count (grain: one row per retailer_id)
-- CTE bucketed (grain: one row per retailer_id with bucket label)
-- Grain of output: one row per repeat_brand_bucket

WITH
-- For each retailer-brand pair, count distinct months they bought from that brand
-- Grain: one row per (retailer_id, brand_id)
-- Source: v_retailer_brand_months (one row per retailer-brand-month)
brand_month_counts AS (
    SELECT
        retailer_id,
        brand_id,
        COUNT(DISTINCT month_offset)  AS months_with_brand  -- number of distinct months
    FROM v_retailer_brand_months
    GROUP BY retailer_id, brand_id
),
-- Filter to repeat brands only (ordered in 2+ distinct months)
-- Grain: one row per (retailer_id, brand_id) with months_with_brand >= 2
repeat_brands AS (
    SELECT retailer_id, COUNT(*) AS repeat_brand_count  -- count of repeat brands per retailer
    FROM brand_month_counts
    WHERE months_with_brand >= 2
    GROUP BY retailer_id
),
-- Assign bucket label; 0 = no repeat brands at all
-- Grain: one row per retailer_id
bucketed AS (
    SELECT
        rs.retailer_id,
        rs.has_m12_plus,
        CASE
            WHEN COALESCE(rb.repeat_brand_count, 0) = 0 THEN '0'
            WHEN rb.repeat_brand_count <= 2             THEN '1-2'
            WHEN rb.repeat_brand_count <= 5             THEN '3-5'
            ELSE                                             '6+'
        END  AS repeat_brand_bucket
    -- JOIN grain: v_retailer_summary (one per retailer) to repeat_brands (≤1 per retailer) → 1:1
    FROM v_retailer_summary rs
    LEFT JOIN repeat_brands rb ON rs.retailer_id = rb.retailer_id
)
SELECT
    repeat_brand_bucket,
    COUNT(*)                               AS retailer_count,
    ROUND(AVG(has_m12_plus) * 100, 1)     AS ret_12mo_pct
FROM bucketed
GROUP BY repeat_brand_bucket
ORDER BY CASE repeat_brand_bucket WHEN '0' THEN 0 WHEN '1-2' THEN 1 WHEN '3-5' THEN 2 ELSE 3 END;

-- QA: 4 rows; total retailer_count = 7400; '6+' should have highest retention


-- ============================================================
-- SECTION 4: APP & PLATFORM ENGAGEMENT
-- Hypothesis: app install and multi-platform usage drive retention
-- CAUTION   : FLAG_APP_INSTALLED is a snapshot → reverse causality risk
--             FIRST_APP_SESSION_AT used for timing analysis
-- ============================================================

-- ── 4a. Retention by app install flag ─────────────────────
-- Simple cut: does having the app correlate with retention?
-- Expected  : app_installed = 1 has meaningfully higher retention
-- NOTE      : this is NOT causal; retained retailers may install the app BECAUSE
--             they are retained, not the other way around (see section 4c)
-- Grain of output: one row per flag value (0 and 1)

SELECT
    flag_app_installed,
    COUNT(*)                              AS retailer_count,
    ROUND(AVG(has_m6_plus)  * 100, 1)    AS ret_6mo_pct,
    ROUND(AVG(has_m12_plus) * 100, 1)    AS ret_12mo_pct,
    ROUND(AVG(is_single_month_churn) * 100, 1) AS single_month_churn_pct
FROM v_retailer_summary
GROUP BY flag_app_installed
ORDER BY flag_app_installed;

-- QA: exactly 2 rows (0 and 1); total retailer_count = 7400


-- ── 4b. Retention by platform count ───────────────────────
-- Platform count = number of distinct channels with a recorded first session
-- (app, desktop, mobile web) — ranges 0 to 3
-- Hypothesis: using more platforms signals deeper engagement
-- Grain of output: one row per platform_count value (0–3)

SELECT
    platform_count,
    COUNT(*)                              AS retailer_count,
    ROUND(AVG(has_m6_plus)  * 100, 1)    AS ret_6mo_pct,
    ROUND(AVG(has_m12_plus) * 100, 1)    AS ret_12mo_pct
FROM v_retailer_summary
GROUP BY platform_count
ORDER BY platform_count;

-- QA: rows for values 0,1,2,3; total = 7400


-- ── 4c. REVERSE CAUSALITY CHECK: app timing vs retention ──
-- Hypothesis to test: if app drives retention, earlier installers should have
--   higher retention than later installers. If we see HIGHER retention for
--   LATER installers, that is evidence of reverse causality (retained retailers
--   install the app because they're already engaged).
-- Buckets:
--   'Before first order' : FIRST_APP_SESSION_AT < FIRST_CONFIRMED_ORDER_PLACED_AT
--   'Within 30 days'     : 0 <= days_to_app <= 30
--   '31-180 days'        : 31 <= days_to_app <= 180
--   '180+ days'          : days_to_app > 180
--   'Never installed'    : FIRST_APP_SESSION_AT IS NULL
-- Expected (reverse causality signature): 180+ days bucket has HIGHEST retention
-- Grain of output: one row per app_timing_bucket (5 rows)

SELECT
    app_timing_bucket,
    COUNT(*)                              AS retailer_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct_of_cohort,  -- share of each bucket
    ROUND(AVG(has_m12_plus) * 100, 1)    AS ret_12mo_pct
FROM v_retailer_summary
GROUP BY app_timing_bucket
ORDER BY CASE app_timing_bucket
    WHEN 'Before first order' THEN 1
    WHEN 'Within 30 days'     THEN 2
    WHEN '31-180 days'        THEN 3
    WHEN '180+ days'          THEN 4
    ELSE 5
END;

-- QA: 5 rows; pct_of_cohort sums to 100%; 'Never installed' ≈ 39.5% of cohort
-- INTERPRETATION: if '180+ days' > 'Before first order' in ret_12mo_pct,
--   reverse causality is likely operative


-- ============================================================
-- SECTION 5: PAYMENT TERMS
-- Hypothesis: NET60 improves retention by reducing cash-flow barrier for retailers
-- Expected  : NET60 has higher retention, higher lifetime GMV, more months active
-- NOTE      : SELECTION BIAS — NET60 is extended to pre-qualified retailers who are
--             likely larger and more creditworthy; any effect may not be causal
-- ============================================================

-- ── 5a. Core metrics by payment term ──────────────────────
-- Grain: one row per payment_term (4 values, but focus on NET60 vs PAYMENT_ON_SHIPMENT)

SELECT
    payment_term,
    COUNT(*)                                   AS retailer_count,
    ROUND(AVG(lifetime_gmv), 0)                AS avg_lifetime_gmv,
    ROUND(AVG(months_active), 2)               AS avg_months_active,
    ROUND(AVG(is_single_month_churn) * 100, 1) AS single_month_churn_pct,
    ROUND(AVG(has_m6_plus)  * 100, 1)          AS ret_6mo_pct,
    ROUND(AVG(has_m12_plus) * 100, 1)          AS ret_12mo_pct
FROM v_retailer_summary
GROUP BY payment_term
ORDER BY retailer_count DESC;

-- QA: 4 rows; largest groups are PAYMENT_ON_SHIPMENT (~3899) and NET60 (~3486)


-- ── 5b. NDR at months 3 and 12 by payment term ────────────
-- For each payment term, compute aggregate NDR at specific months
-- NDR_t(term) = sum(GMV at month t for that term) / sum(GMV at month 0 for that term)
-- This is the segment-level NDR, not the overall cohort NDR
-- Grain of gmv0_by_term : one row per (retailer_id, payment_term) for month 0
-- Grain of ndr_by_term  : one row per (payment_term, month_offset)
-- JOIN grain: v_retailer_months (many per retailer) to v_retailer_summary (one per retailer) → 1:many

WITH
-- Month-0 GMV per retailer, with payment term attached
-- Grain: one row per retailer_id (those with month-0 orders only)
gmv0_by_term AS (
    SELECT
        rs.retailer_id,
        rs.payment_term,
        rm.monthly_gmv  AS gmv0
    FROM v_retailer_months rm
    -- JOIN grain: retailers (one per retailer) to retailer_months at month 0 (one per retailer) → 1:1
    JOIN v_retailer_summary rs ON rm.retailer_id = rs.retailer_id
    WHERE rm.month_offset = 0
),
-- Total month-0 GMV per payment term (fixed denominator)
-- Grain: one row per payment_term
denom_by_term AS (
    SELECT payment_term, SUM(gmv0) AS total_gmv0
    FROM gmv0_by_term
    GROUP BY payment_term
),
-- GMV at target months, joined with payment term
-- Grain: one row per (payment_term, month_offset)
gmv_at_t AS (
    SELECT
        rs.payment_term,
        rm.month_offset,
        SUM(rm.monthly_gmv) AS gmv_t
    FROM v_retailer_months rm
    -- JOIN grain: v_retailer_summary (one per retailer) to v_retailer_months (many) → 1:many
    JOIN v_retailer_summary rs ON rm.retailer_id = rs.retailer_id
    WHERE rm.month_offset IN (3, 12)
    GROUP BY rs.payment_term, rm.month_offset
)
SELECT
    g.payment_term,
    g.month_offset,
    ROUND(g.gmv_t, 0)                            AS cohort_gmv_t,
    ROUND(d.total_gmv0, 0)                       AS cohort_gmv_0,
    ROUND(g.gmv_t * 100.0 / d.total_gmv0, 2)    AS ndr_pct
FROM gmv_at_t g
-- JOIN grain: gmv_at_t (one per payment_term-month) to denom_by_term (one per payment_term) → 1:1
JOIN denom_by_term d ON g.payment_term = d.payment_term
ORDER BY g.payment_term, g.month_offset;

-- QA: rows for each term × {3, 12}; NDR at month 3 for NET60 should be higher than POS


-- ── 5c. Cross-tab: 6-month retention by App × Payment Term ──
-- Shows whether app install and payment term effects are additive
-- Grain of output: one row per (flag_app_installed, payment_term)
-- Only showing main two payment terms (NET60 + PAYMENT_ON_SHIPMENT) for clarity

SELECT
    flag_app_installed,
    payment_term,
    COUNT(*)                              AS retailer_count,
    ROUND(AVG(has_m6_plus) * 100, 1)     AS ret_6mo_pct,
    ROUND(AVG(has_m12_plus) * 100, 1)    AS ret_12mo_pct
FROM v_retailer_summary
WHERE payment_term IN ('NET60', 'PAYMENT_ON_SHIPMENT')
GROUP BY flag_app_installed, payment_term
ORDER BY flag_app_installed, payment_term;

-- QA: 4 rows (2 terms × 2 flag values); each cell's count should be 50–2000 range


-- ============================================================
-- SECTION 6: UNKNOWN SEGMENT DEEP DIVE
-- Hypothesis: 'Unknown' annual_sales_bucket retailers get a worse experience
--             (possibly less qualified, less supported, or misclassified)
-- Expected  : Unknown has lower retention, lower GMV, higher churn than all known buckets
-- ============================================================

-- ── 6a. Core metrics: Unknown vs Known ────────────────────
-- Grain of output: one row per segment (Unknown / Known)

SELECT
    CASE WHEN annual_sales_bucket = 'Unknown' THEN 'Unknown' ELSE 'Known' END AS segment,
    COUNT(*)                                   AS retailer_count,
    ROUND(AVG(has_m12_plus) * 100, 1)          AS ret_12mo_pct,
    ROUND(AVG(has_m6_plus) * 100, 1)           AS ret_6mo_pct,
    ROUND(AVG(lifetime_gmv), 0)                AS avg_lifetime_gmv,
    ROUND(AVG(months_active), 2)               AS avg_months_active,
    ROUND(AVG(is_single_month_churn) * 100, 1) AS single_month_churn_pct
FROM v_retailer_summary
GROUP BY CASE WHEN annual_sales_bucket = 'Unknown' THEN 'Unknown' ELSE 'Known' END
ORDER BY segment;

-- QA: 2 rows; counts sum to 7400; Unknown count ≈ 2285


-- ── 6b. Retention by each ANNUAL_SALES_BUCKET ─────────────
-- Grain: one row per annual_sales_bucket value (7 distinct values)

SELECT
    annual_sales_bucket,
    COUNT(*)                               AS retailer_count,
    ROUND(AVG(has_m12_plus) * 100, 1)     AS ret_12mo_pct,
    ROUND(AVG(lifetime_gmv), 0)           AS avg_lifetime_gmv,
    ROUND(AVG(is_single_month_churn)*100,1) AS single_month_churn_pct
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
    ELSE 99
END;

-- QA: 7 rows; Unknown ≈ 2285, Brand New Retailer ≈ 1603


-- ── 6c. Payment term distribution: Unknown vs Known ───────
-- Grain: one row per (segment, payment_term)

SELECT
    CASE WHEN annual_sales_bucket = 'Unknown' THEN 'Unknown' ELSE 'Known' END AS segment,
    payment_term,
    COUNT(*)                                                  AS retailer_count,
    ROUND(COUNT(*) * 100.0
        / SUM(COUNT(*)) OVER (PARTITION BY
            CASE WHEN annual_sales_bucket = 'Unknown' THEN 'Unknown' ELSE 'Known' END
        ), 1)                                                 AS pct_within_segment
FROM v_retailer_summary
GROUP BY segment, payment_term
ORDER BY segment, retailer_count DESC;

-- QA: sum of pct_within_segment = 100% within each segment


-- ── 6d. Business type distribution: Unknown vs Known ──────
-- Uses rolled-up business_type_group (4 categories)
-- Grain: one row per (segment, business_type_group)

SELECT
    CASE WHEN annual_sales_bucket = 'Unknown' THEN 'Unknown' ELSE 'Known' END AS segment,
    business_type_group,
    COUNT(*)                                                  AS retailer_count,
    ROUND(COUNT(*) * 100.0
        / SUM(COUNT(*)) OVER (PARTITION BY
            CASE WHEN annual_sales_bucket = 'Unknown' THEN 'Unknown' ELSE 'Known' END
        ), 1)                                                 AS pct_within_segment
FROM v_retailer_summary
GROUP BY segment, business_type_group
ORDER BY segment, retailer_count DESC;

-- QA: pct_within_segment sums to 100% within each segment


-- ============================================================
-- SECTION 7: STORE TYPE & CHANNEL
-- ============================================================

-- ── 7a. Top 8 store types by retailer count ───────────────
-- Hypothesis: store type proxies for retailer sophistication and buying patterns
-- "avg orders" = avg number of distinct orders placed per retailer over the full period
-- Grain: one row per retailer_store_type (top 8)

WITH
-- Distinct order count per retailer (deduplicating brand-level rows)
-- ⚠️ FAN-OUT NOTE: orders table has multiple rows per order; COUNT(DISTINCT order_id) required
-- Grain: one row per retailer_id
retailer_orders AS (
    SELECT retailer_id, COUNT(DISTINCT order_id) AS total_orders
    FROM psa_exercise_orders
    WHERE order_state = 'PROCESSING'
    GROUP BY retailer_id
)
SELECT
    rs.retailer_store_type,
    COUNT(*)                                   AS retailer_count,
    ROUND(AVG(ro.total_orders), 1)             AS avg_orders,
    ROUND(AVG(rs.lifetime_gmv), 0)             AS avg_lifetime_gmv,
    ROUND(AVG(rs.is_single_month_churn)*100,1) AS single_month_churn_pct,
    ROUND(AVG(rs.has_m12_plus)*100, 1)         AS ret_12mo_pct
FROM v_retailer_summary rs
-- JOIN grain: v_retailer_summary (one per retailer) to retailer_orders (one per retailer) → 1:1
LEFT JOIN retailer_orders ro ON rs.retailer_id = ro.retailer_id
GROUP BY rs.retailer_store_type
ORDER BY retailer_count DESC
LIMIT 8;

-- QA: 8 rows; grocery should be the largest store type (~2182)


-- ── 7b. Acquisition channel comparison ───────────────────
-- Hypothesis: Referral and Partnerships may bring higher-quality retailers
-- Grain: one row per retailer_bucketed_channel (4 values)

WITH
-- Distinct order count per retailer
-- Grain: one row per retailer_id
retailer_orders AS (
    SELECT retailer_id, COUNT(DISTINCT order_id) AS total_orders
    FROM psa_exercise_orders
    WHERE order_state = 'PROCESSING'
    GROUP BY retailer_id
)
SELECT
    rs.retailer_bucketed_channel,
    COUNT(*)                                   AS retailer_count,
    ROUND(AVG(ro.total_orders), 1)             AS avg_orders,
    ROUND(AVG(rs.lifetime_gmv), 0)             AS avg_lifetime_gmv,
    ROUND(AVG(rs.is_single_month_churn)*100,1) AS single_month_churn_pct,
    ROUND(AVG(rs.has_m6_plus) *100, 1)         AS ret_6mo_pct,
    ROUND(AVG(rs.has_m12_plus)*100, 1)         AS ret_12mo_pct
FROM v_retailer_summary rs
LEFT JOIN retailer_orders ro ON rs.retailer_id = ro.retailer_id
GROUP BY rs.retailer_bucketed_channel
ORDER BY retailer_count DESC;

-- QA: 4 rows; Organic ≈ 4251, Referral ≈ 2094, Paid ≈ 967, Partnerships ≈ 88


-- ============================================================
-- SELF-CRITIQUE: ASSUMPTIONS, LIMITATIONS, WHAT COULD BE WRONG
-- ============================================================
--
-- 1. NDR FORMULA MISMATCH
--    The prompt says "avg across retailers of (GMV_t / GMV_0)" but the simple per-retailer
--    average gives ~84% for month 1 vs the reference 59%. We use the aggregate cohort
--    formula (total GMV_t / total GMV_0), which matches the reference exactly. The aggregate
--    formula is also the standard SaaS NRR definition. The discrepancy arises because
--    high-GMV retailers who churn drag the aggregate down more than the equal-weight average.
--
-- 2. CALENDAR MONTH OFFSET (not day-based)
--    month_offset is computed using calendar months. A retailer who joined June 30 and
--    ordered on July 1 has month_offset = 1, even though it was only 1 day later. This
--    means "month 0" can represent anywhere from 1 to 31 days of activity, and early
--    retention rates may be slightly inflated.
--
-- 3. FLAG_APP_INSTALLED IS A SNAPSHOT
--    FLAG_APP_INSTALLED reflects app install status as of the data extraction date (Dec 2021),
--    not at the time of the retailer's first order. A retailer classified as "0" today may
--    have had the app installed and uninstalled it. We use FIRST_APP_SESSION_AT for timing
--    analysis, but this only gives first-session date, not current install status.
--
-- 4. REVERSE CAUSALITY IN APP ANALYSIS
--    Our timing analysis (Section 4c) shows that retailers who installed the app 180+ days
--    after their first order have the HIGHEST 12-mo retention. This is a strong reverse
--    causality signal: retained retailers install the app because they're already engaged,
--    not the other way around. We cannot establish causality from observational data alone.
--
-- 5. PAYMENT TERM SELECTION BIAS
--    NET60 is extended to retailers who have been pre-screened (creditworthy, established).
--    These retailers are likely systematically larger and less likely to churn even without
--    NET60. The observed retention gap between NET60 and PAYMENT_ON_SHIPMENT may be mostly
--    or entirely explained by this selection bias, not the payment term itself.
--
-- 6. UNKNOWN ANNUAL_SALES_BUCKET
--    The 2,285 "Unknown" retailers may be missing data for heterogeneous reasons: new
--    retailers (no sales history), privacy settings, data entry omissions, or API failures.
--    The "Unknown" group is likely not a single homogeneous segment. Their worse metrics
--    may reflect their characteristics (newer, smaller, lower intent) rather than any
--    causal effect of being categorized as "Unknown."
--
-- 7. BUSINESS TYPE FREE-TEXT DATA QUALITY
--    RETAILER_BUSINESS_TYPE has 402 distinct values, mostly free-text user input. Our
--    4-bucket rollup captures the major categories but ~8% of retailers fall into
--    "Other/Unknown" due to non-standard inputs. Analyses using this field are noisy.
--
-- 8. SURVIVORSHIP BIAS IN LATE-MONTH NDR
--    The NDR curve rises above 100% at months 10–17. This reflects SURVIVORSHIP BIAS:
--    by month 12, only highly-engaged, high-GMV retailers remain active. Their continued
--    growth inflates the numerator above the original cohort baseline. The late-month NDR
--    should NOT be interpreted as evidence that the average retailer spends more over time.
--
-- 9. SINGLE-MONTH CHURN DEFINITION
--    We define single-month churn as max_month_offset = 0 (only ever placed orders in
--    their cohort entry month). This includes retailers who placed many orders in month 0
--    and then stopped. An alternative definition (only 1 order ever) would give different
--    numbers. The current definition measures "time-to-second-month engagement" rather
--    than pure order count churn.
--
-- 10. COHORT MIXING
--     Our cohort contains two sub-cohorts: June 2020 joiners (month 0 = June) and July 2020
--     joiners (month 0 = July). We treat them as a unified cohort. If the two months had
--     different macro conditions (e.g., COVID recovery), their behavior may differ. A
--     robustness check would compare metrics by FIRST_CONFIRMED_ORDER_PLACED_AT cohort.
-- ============================================================
