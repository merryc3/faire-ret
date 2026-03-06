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
-- OBSERVATION: Q4 (avg $3,930 month-0 GMV): 71.2% 12-mo retention. Q1 (avg $266): 45.8%. Gap = 25.4pp — strong monotone gradient across quartiles. Single-month churn: Q1 29.5% vs Q4 7.9%. CAUTION: GMV in month 0 could be CAUSED by retailer size, not platform engagement. A large retailer will naturally spend more AND retain better regardless of Faire's actions. Worth investigating: does the gradient hold within each ANNUAL_SALES_BUCKET? If yes, spend depth is predictive beyond retailer size.


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
-- OBSERVATION: 1 brand in month 0 (n=3,703): 50.6% 12-mo retention. 6+ brands (n=945): 72.6%. Gap = 22.0pp. Monotone gradient from 1 → 6+. Single-month churn: 1-brand 26.7% vs 6+ 5.0% — 21.7pp difference at the very first order. Brand diversity in month 0 is more actionable than GMV: Faire can surface brand recommendations and bundles to increase discovery. BUT correlation ≠ causation — retailers who WANT to discover many brands may already be the high-intent cohort.


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
-- OBSERVATION: Retained (max ≥ 12, n=4,250): avg month-0 GMV $1,798, avg 3.34 brands, avg 2.58 orders. Churned (max ≤ 2, n=1,897): avg month-0 GMV $913, avg 1.92 brands, avg 1.55 orders. RATIOS: retained spends 2.0x more in month 0, buys from 1.7x more brands, places 1.7x more orders. The groups are CLEARLY different on day one. However, overlapping ranges (min/max) show no clean decision boundary — you cannot cleanly separate them with a threshold rule. This means month-0 signals are informative for scoring but not deterministic.


-- ── 4d. Early ordering frequency → 12-mo retention ───────
-- What I'm looking for: does ordering in months 0 AND 1 AND 2 predict year-1 retention?
-- This tests HABIT FORMATION: retailers who order every month in their first 3 months
--   have built a regular Faire habit vs those who only ordered once.
-- This is the most actionable signal: Faire can nudge retailers in months 1 and 2.
-- ⚠️ FAN-OUT: v_retailer_months is at retailer-month grain; COUNT(DISTINCT) correct
--
-- CTE first3_active (grain: one row per retailer_id):
--   counts distinct months in {0,1,2} where retailer had at least one order
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
-- OBSERVATION: 1-of-3 months active (n=3,153): 40.1% 12-mo retention. 2-of-3 (n=2,173): 60.7%. 3-of-3 (n=2,074): 80.4%. Gap 3/3 vs 1/3 = 40.3pp — THE LARGEST SINGLE GAP in this analysis. Single-month churn: 1/3 43.8% vs 3/3 0.0%. Each additional active month in the first 3 adds ~20pp of 12-mo retention. This is the most actionable finding: if Faire can convert a retailer who orders in month 0 only into one who also orders in month 1, retention roughly doubles from 40.1% to 60.7%. CRITICAL caveat: causality is unclear. Do repeat orders CAUSE retention, or do both reflect underlying retailer intent that Faire cannot change? An experiment (e.g., discount offer to 1/3 retailers to trigger month-1 order) would help distinguish these.


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
-- OBSERVATION: 1 platform (n=884): 31.7% 12-mo retention. 2 platforms (n=2,426): 48.6%. 3 platforms (n=4,068): 68.6%. Gap 3-platforms vs 1-platform = 36.9pp. Single-month churn: 1-platform 45.0% vs 3-platform 9.6%. STRONG REVERSE CAUSALITY WARNING: platform_count is a LIFETIME snapshot. Retailers who have been active for 12+ months naturally accumulate more platform sessions than those who churned after month 0. The platform count is partly a MEASURE of retention, not a predictor of it. To test causality, we would need to check whether platform_count AS OF MONTH 1 predicts retention from month 2 onwards — which requires event-level session data timestamped more precisely than FIRST_*_SESSION_AT provides.


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
--    1.  +40.3pp  behavioral    4d. Early repeat (3/3 vs 1/3 months active)
--    2.  +36.9pp  behavioral    4e. Platform breadth (3 vs 1 platforms)
--    3.  +31.8pp  profile       3c. Unknown annual_sales_bucket vs best known
--    4.  +27.4pp  profile       3d. Business type: B&M vs Online Only
--    5.  +25.4pp  behavioral    4a. First-month GMV (Q4 vs Q1)
--    6.  +24.2pp  profile       3f. Channel: Organic vs Partnerships
--    7.  +23.4pp  profile       3a. Payment term: NET60 vs POS
--    8.  +22.0pp  behavioral    4b. Brand diversity (6+ vs 1 brand)
--    9.  +21.5pp  profile       3b. App install: flag=1 vs 0
--   10.  +21.1pp  profile       3e. Store type: gift vs other
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
--    1. 4d. Early repeat (3/3 vs 1/3 months active): behavioral —
--strongest & most actionable
--    2. 4e. Platform breadth (3 vs 1 platforms): behavioral — strong
--reverse causality risk
--    3. 3c. Unknown annual_sales_bucket vs best known: Overlaps heavily
--with Online Only business type
--    4. 3d. Business type: B&M vs Online Only: Likely same population
--as Unknown bucket
--    5. 4a. First-month GMV (Q4 vs Q1): behavioral — confounded with
--retailer size
--    6. 3f. Channel: Organic vs Partnerships: Partnerships n=88; too
--small for conclusions
--    7. 3a. Payment term: NET60 vs POS: Selection bias: NET60 = pre-
--qualified retailers
--    8. 4b. Brand diversity (6+ vs 1 brand): behavioral — most
--actionable (Faire can show more brands)
--    9. 3b. App install: flag=1 vs 0: Snapshot field; strong reverse
--causality risk
--   10. 3e. Store type: gift vs other: Moderate signal; overlaps with
--Unknown segment
--
-- ─────────────────────────────────────────────────────────
-- 5.3  FOLLOW-UP QUESTIONS WORTH INVESTIGATING
-- ─────────────────────────────────────────────────────────
-- Listed in order of (gap size × actionability × clarity of causal story)
--
-- ── EARLY REPEAT ORDERING (4d — BIGGEST gap: 40pp)
--   What causes a 40.1% retention rate for 1/3 retailers? Is month-1
--   inactivity
--   driven by stockpiling (ordered enough in month 0), discovery failure
--   (didn't find the right brands), or intent (they only ever wanted one
--   order)?
--   Can a targeted nudge (e.g. brand recommendation email in week 3)
--   increase
--   month-1 activation? What is the marginal cost vs retention value?
--   Experiment idea: randomly assign 1/3 retailers to a reminder
--   campaign
--   and measure month-1 reactivation + 12-mo retention lift.
--
-- ── PLATFORM BREADTH (4e — 37pp gap, but reverse causality risk)
--   Platform count is a LIFETIME snapshot — retained retailers naturally
--   accumulate more sessions. The 37pp gap may largely be
--   measuring retention (outcome) rather than predicting it (input).
--   Better question: what % of retailers first used mobile BEFORE month
--   1?
--   Did mobile-first vs desktop-first retailers have different month-1
--   reactivation rates? This requires timestamped session data.
--
-- ── UNKNOWN ANNUAL_SALES_BUCKET (3c — 31.8pp below best known)
--   Unknown has ? retailers below the Known
--   cohort on every metric. Key question: are they structurally
--   different
--   (new online retailers, no history) or just missing data?
--   Cross-tab Unknown × business_type_clean: if Unknown is >70% Online
--   Only,
--   the 'Unknown' problem IS the 'Online Only' problem.
--   Also: do Unknown retailers get a different onboarding experience?
--   Check if they have different FIRST_APP_SESSION_AT timing.
--
-- ── BRAND DIVERSITY IN MONTH 0 (4b — 22pp gap)
--   1-brand retailers (n=3,703) have 26.7% single-month churn vs
--   5.0% for 6+ brand retailers. Can Faire's onboarding surface more
--   brand recommendations during the first visit to reduce 1-brand
--   orders?
--   Is low brand count caused by store specialization (e.g. a grocery
--   store
--   only buys from food brands) or by limited discovery?
--   Check: does brand diversity in month 0 predict repeat purchasing in
--   month 1,
--   controlling for GMV? That would distinguish discovery from intent.
--
-- ── PAYMENT TERM (3a — 23.4pp gap, selection bias)
--   NET60 and PAYMENT_ON_SHIPMENT retailers differ on BOTH retention AND
--   size.
--   To isolate the payment-term effect, cross-tab: within each GMV
--   quartile
--   (from 4a), do NET60 retailers still retain better than POS
--   retailers?
--   If the gap disappears within quartiles, it's pure selection bias.
--   If it persists, the payment term itself may matter (cash flow
--   relief).
--
-- ── APP INSTALL (3b — 21.5pp gap, reverse causality)
--   The FIRST_APP_SESSION_AT timing analysis (run in analysis.py) showed
--   that
--   retailers who installed the app 180+ days AFTER their first order
--   had
--   HIGHER 12-mo retention than those who installed before their first
--   order.
--   This is the clearest reverse causality signature in the data.
--   The app install flag is NOT a reliable leading indicator of
--   retention.
--   Better metric: app install BEFORE month 1 vs after month 1.
--

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
