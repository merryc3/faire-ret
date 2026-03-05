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
Load CSVs into SQLite and run all analysis as SQL queries.
Use Python only for loading data into SQLite, executing queries, and generating charts.
Save all SQL queries to queries.sql with full comments.
Save all charts to a /charts folder.
```
