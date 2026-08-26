# Daily Market Dashboard

Zero-cost daily report of currencies, rates, equities and commodities,
published as a static page via GitHub Pages.

## Setup (one-time)

1. Create a new **public** GitHub repository (Pages' free tier requires
   public, unless you have GitHub Pro/Team) and push these files to it,
   e.g.:

   ```bash
   git init
   git add .
   git commit -m "Initial dashboard setup"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo-name>.git
   git push -u origin main
   ```

2. In the repo, go to **Settings -> Pages** and set:
   - Source: **Deploy from a branch**
   - Branch: **gh-pages** / `(root)`
   (This branch doesn't exist yet — it gets created automatically the
   first time the workflow runs. Come back to this setting after step 3.)

3. Go to the **Actions** tab, open "Daily Market Dashboard", and click
   **Run workflow** to trigger it manually the first time. Check the run
   for any red ❌ steps — a failed instrument fetch is logged as a
   warning but won't fail the whole run; check the logs to see which
   series, if any, didn't resolve (the 3M Euribor and TTF gas tickers are
   flagged in the script as worth double-checking).

4. Once the run succeeds, go back to **Settings -> Pages** — it should
   now offer the `gh-pages` branch. Select it and save. Your dashboard
   will be live at:

   `https://<you>.github.io/<repo-name>/`

From then on, it re-runs automatically every weekday at 06:00 UTC.

## Data sources

| Category     | Source                                   | Notes |
|--------------|-------------------------------------------|-------|
| Currencies   | Yahoo Finance (`yfinance`)                | — |
| EUR rates    | ECB Data Portal (yield-curve / FM API)    | AAA euro-area government bond yields used as a swap-rate proxy for 1Y/5Y/10Y/30Y; 3M Euribor pulled from ECB's FM dataset |
| UST rates    | FRED (constant-maturity Treasury yields)  | — |
| Equities     | Yahoo Finance                             | — |
| Commodities  | Yahoo Finance                             | Brent, WTI, Dutch TTF gas, Gold |
| AT Power     | aWATTar API (day-ahead auction price)     | Short history only — 1M/52W/YTD changes will often show "-" since aWATTar doesn't retain long-term data |

## Extending later

To add macro indicators (CPI, GDP, etc.), add a new category to the
`INSTRUMENTS` dict in `fetch_data.py`. FRED and Eurostat are good free
sources for those.

## Verifying instrument symbols

- Yahoo Finance tickers: https://finance.yahoo.com/lookup
- ECB series keys: https://data.ecb.europa.eu (search a series, the key
  is shown on its detail page)
