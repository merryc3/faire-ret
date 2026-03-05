# Faire Retailer Retention Analysis

## Data
Two CSVs in this directory:
- psa_exercise_retailers*.csv: 7,400 retailers who placed first confirmed order in June/July 2020. One row per retailer.
- psa_exercise_orders*.csv: 259,416 rows of orders through Dec 2021. Each row is a BRAND within an order (not one row per order). ORDER_ID repeats across brands in the same order.

## Key data notes
- TOTAL_GMV is per-brand-per-order, NOT per-order total. Sum across rows to get order or retailer GMV.
- ORDER_CREATED is monthly granularity (always first of month), not exact date.
- FLAG_APP_INSTALLED is a point-in-time snapshot. Use FIRST_APP_SESSION_AT to check timing.
- FIRST_CONFIRMED_ORDER_PLACED_AT is either 2020-06-01 or 2020-07-01.

## NDR Formula
NDR_t = GMV_t / GMV_0
Where GMV_t = total spend in month t, GMV_0 = total spend in first month.

## Reference NDR values (to validate against)
Month 0: 100%, Month 1: 59%, Month 2: 54%, Month 3: 56%, Month 5: 49%, Month 12: 75%

## Tools
Use Python with pandas, matplotlib, seaborn. Write all code to analysis.py.
Save all charts to a /charts folder.
```

**3. Open the AI sidebar:**

Press `Cmd+L` (Mac) or `Ctrl+L` (Windows). This opens the chat panel. Make sure the model is set to Claude Sonnet or Claude (check the dropdown at the bottom of the sidebar).

**4. Start prompting. Here's your exact sequence:**

**Prompt 1 — Exploration:**
```
Create analysis.py. Load both CSVs (use glob to find them). 
Print shape, columns, dtypes, null counts. For categorical columns 
in retailers, print value_counts(). For orders, confirm ORDER_ID 
is not unique per row by showing a sample multi-row order.
```

Cursor will write the code into `analysis.py`. Hit **Accept** on the diff, then run it in the terminal (`` Ctrl+` `` to open terminal, then `python3 analysis.py`).

**Prompt 2 — Validate your pipeline:**
```
Add to analysis.py: Calculate NDR by month. For each retailer, 
compute monthly GMV. NDR_t = average across retailers of (GMV_t / GMV_0). 
Print months 0-17. Validate against the reference: month 1 should be ~59%, 
month 5 ~49%, month 12 ~75%. If it doesn't match, debug.
```

**Prompt 3 — First hypothesis:**
```
Add a new section. Hypothesis: first-month brand diversity predicts retention.
For each retailer, count unique BRAND_IDs in month 0. Bucket: 1, 2, 3-5, 6+.
Calculate 6-mo and 12-mo retention (defined as: did the retailer have any 
order in month 6+ or 12+). Print a table and save a bar chart to charts/h1_brand_diversity.png
```

**Prompt 4 — Second hypothesis:**
```
Hypothesis: early repeat purchasing predicts retention. For each retailer, 
count how many of the first 3 months they were active in. 
Show 12-month retention for 1-of-3, 2-of-3, 3-of-3. Table + chart.
```

**Prompt 5 — App causality check:**
```
Hypothesis: app install drives retention, but check for reverse causality.
Show retention by FLAG_APP_INSTALLED. Then use FIRST_APP_SESSION_AT to 
calculate days between first order and first app session. 
What % installed before, within 30 days, 30-180 days, 180+ days after?
Show 12-mo retention for each timing bucket separately.
```

**Prompt 6 — Payment terms:**
```
Show retention metrics (avg lifetime GMV, months active, single-month 
churn rate, 6-mo retention) for NET60 vs PAYMENT_ON_SHIPMENT.
Then cross-tab: 6-month retention for each combination of 
FLAG_APP_INSTALLED x PAYMENT_TERM. Table + chart.
```

**Prompt 7 — Unknown segment:**
```
Deep dive on ANNUAL_SALES_BUCKET = 'Unknown'. Compare to known-bucket 
retailers on: 12-mo retention, avg lifetime GMV, payment term distribution, 
business type distribution, single-month churn. Table format.
```

**Prompt 8 — Generate all charts:**
```
Create a summary visualization: a 2x3 grid of the most important charts 
from our analysis. Save to charts/summary.png