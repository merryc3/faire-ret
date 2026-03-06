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
),
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
-- NDR by Payment Term: NET60 vs PAYMENT_ON_SHIPMENT
WITH
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
),
-- Segment assignment: one row per retailer
-- Grain: (retailer_id, segment_value)
-- Excludes NET90 (n=8) and HOLD_ON_PLACEMENT (n=7) — too small for reliable NDR curves
segments AS (
    SELECT retailer_id, payment_term AS segment_value
    FROM psa_exercise_retailers
    WHERE payment_term IN ('NET60', 'PAYMENT_ON_SHIPMENT')
    -- QA: should cover ~6985 of 7400 retailers
),
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
ORDER BY gt.segment_value, gt.month_offset;

-- QA: 2 segment values (NET60, PAYMENT_ON_SHIPMENT)
-- QA: month-0 NDR = 100.00% for both
-- QA: fixed_cohort_gmv_0 constant within each segment across all months


-- ============================================================
-- SEGMENT 2: ANNUAL SALES BUCKET — Unknown vs Known
-- Question: does Unknown NDR ever recover, or is the revenue
--   gap permanent? If Unknown never crosses even 50% NDR by
--   month 17, the revenue loss is structural, not recoverable.
-- ============================================================
-- NDR by Annual Sales Bucket: Unknown vs Known
WITH
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
),
-- Segment assignment: Unknown bucket vs all other buckets grouped as 'Known'
-- Grain: (retailer_id, segment_value)
segments AS (
    SELECT retailer_id,
           CASE WHEN annual_sales_bucket = 'Unknown' THEN 'Unknown' ELSE 'Known' END
               AS segment_value
    FROM psa_exercise_retailers
    -- QA: 2285 Unknown, 5115 Known
),
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
ORDER BY gt.segment_value, gt.month_offset;

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
-- NDR by Early Repeat Activity: 1/3, 2/3, 3/3 active in months 0-2
WITH
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
),
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
),
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
ORDER BY gt.segment_value, gt.month_offset;

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
-- NDR by First-Month Brand Count: 1, 2, 3-5, 6+ brands
WITH
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
),
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
),
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
ORDER BY gt.segment_value, gt.month_offset;

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
-- NDR by First-Month GMV Quartile: Q1 ($0-$427) to Q4 ($1,689-$69,125)
WITH
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
),
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
),
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
ORDER BY gt.segment_value, gt.month_offset;

-- QA: 4 segment values (Q1-Q4); 1850 retailers each; month-0 NDR = 100%
-- QA: Q4 fixed_gmv_0 >> Q1 fixed_gmv_0 (high-GMV retailers dominate denominator)


-- ============================================================
-- SEGMENT 6: BUSINESS TYPE — Brick & Mortar vs Online Only vs Pop Up
-- Question: does Online Only NDR track similarly to the Unknown bucket?
--   If yes, these are the same underlying population and a single
--   intervention could address both.
--   Other/Unknown (~421 retailers) excluded for clarity.
-- ============================================================
-- NDR by Business Type: Brick & Mortar vs Online Only vs Pop Up
WITH
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
),
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
),
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
ORDER BY gt.segment_value, gt.month_offset;

-- QA: 3 segment values; month-0 NDR = 100% for all
-- QA: Brick & Mortar fixed_gmv_0 >> Online Only (larger spenders)


-- ============================================================
-- OBSERVATIONS: FINDINGS ACROSS ALL SEGMENTS
-- ============================================================
--
-- ── Which segments cross 100% NDR (true expansion) ──────────
-- ── Payment Term
--   NET60                 : month-12 NDR=87.9%  crosses 100% at month 15
--   PAYMENT_ON_SHIPMENT   : month-12 NDR=60.4%  never crosses 100%
--
-- ── Annual Bucket
--   Known                 : month-12 NDR=87.6%  never crosses 100%
--   Unknown               : month-12 NDR=35.3%  never crosses 100%
--
-- ── Early Repeat
--   1-of-3                : month-12 NDR=38.1%  never crosses 100%
--   2-of-3                : month-12 NDR=58.2%  never crosses 100%
--   3-of-3                : month-12 NDR=107.9%  crosses 100% at month 1
--
-- ── Brand Count
--   1 brand               : month-12 NDR=104.0%  crosses 100% at month 12
--   2 brands              : month-12 NDR=82.5%  crosses 100% at month 13
--   3-5 brands            : month-12 NDR=72.2%  never crosses 100%
--   6+ brands             : month-12 NDR=59.3%  never crosses 100%
--
-- ── GMV Quartile
--   Q1                    : month-12 NDR=134.1%  crosses 100% at month 1
--   Q2                    : month-12 NDR=97.2%  crosses 100% at month 13
--   Q3                    : month-12 NDR=88.7%  crosses 100% at month 15
--   Q4                    : month-12 NDR=63.6%  never crosses 100%
--
-- ── Business Type
--   Brick & Mortar        : month-12 NDR=88.5%  never crosses 100%
--   Online Only           : month-12 NDR=32.1%  never crosses 100%
--   Pop Up                : month-12 NDR=73.0%  never crosses 100%
--
--
-- ── Headcount vs revenue divergence ─────────────────────────
--
-- The most important divergence is in the EARLY REPEAT segment:
--   • 3/3 retailers: headcount retention at month 12 = 59.1%,
--     NDR at month 12 = {ndr_m12_3of3:.1f}%
--     → NDR >> headcount. Surviving 3/3 retailers are spending
--       SIGNIFICANTLY more than the entire 3/3 cohort did in month 0.
--       This is genuine expansion, not just survival.
--
--   • 1/3 retailers: headcount retention = 18.0%,
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
