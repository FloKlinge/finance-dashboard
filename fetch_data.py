#!/usr/bin/env python3
"""
Daily financial dashboard data fetcher.

Pulls current values and historical comparisons (1D / 1W / 1M / 52W / YTD)
for a configured set of currencies, rates, equities and commodities, then
renders a static HTML report to output/index.html (plus a raw output/data.csv).

Sources:
- yfinance (Yahoo Finance)         -> currencies, equities, most commodities
- ECB Data Portal SDMX API         -> EUR government bond yield curve (rate
                                       proxies for EUR 1Y/5Y/10Y/30Y) + 3M Euribor
- FRED (St. Louis Fed) CSV export  -> US Treasury constant-maturity yields
- aWATTar API                      -> Austrian day-ahead electricity price
                                       (no auth needed; short history only,
                                       so long lookbacks may show "-")

Run manually with:  python fetch_data.py
"""

import datetime as dt
import io
import os
import sys
import traceback

import pandas as pd
import requests
import yfinance as yf

TODAY = dt.date.today()

# ---------------------------------------------------------------------------
# Instrument configuration
# Each instrument: (display_name, source, identifier)
# source is one of: "yfinance", "ecb", "fred", "awattar"
# ---------------------------------------------------------------------------

INSTRUMENTS = {
    "Currencies": [
        ("EUR/USD", "yfinance", "EURUSD=X"),
        ("EUR/CNY", "yfinance", "EURCNY=X"),
        ("EUR/CHF", "yfinance", "EURCHF=X"),
        ("EUR/RON", "yfinance", "EURRON=X"),
    ],
    "Rates": [
        # NOTE: verify this Euribor series key still resolves - EMMI/ECB
        # occasionally restructure these. See README for how to check.
        ("3M Euribor", "ecb", "FM.B.U2.EUR.RT.MM.EURIBOR3MD_.HSTA"),
        ("EUR 1Y (AAA gov. proxy)", "ecb", "YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_1Y"),
        ("EUR 5Y (AAA gov. proxy)", "ecb", "YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_5Y"),
        ("EUR 10Y (AAA gov. proxy)", "ecb", "YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y"),
        ("EUR 30Y (AAA gov. proxy)", "ecb", "YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_30Y"),
        ("UST 2Y", "fred", "DGS2"),
        ("UST 10Y", "fred", "DGS10"),
        ("UST 30Y", "fred", "DGS30"),
    ],
    "Equities": [
        ("S&P 500", "yfinance", "^GSPC"),
        ("Nasdaq 100", "yfinance", "^NDX"),
        ("Euro Stoxx 50", "yfinance", "^STOXX50E"),
        ("DAX", "yfinance", "^GDAXI"),
        ("ATX", "yfinance", "^ATX"),
    ],
    "Commodities": [
        ("Brent Crude", "yfinance", "BZ=F"),
        ("WTI Crude", "yfinance", "CL=F"),
        # NOTE: verify this ticker on finance.yahoo.com/lookup - Yahoo
        # occasionally renames/delists specific futures contracts.
        ("Dutch TTF Nat Gas", "yfinance", "TTF=F"),
        ("AT Power (day-ahead, EUR/MWh)", "awattar", "AT"),
        ("Gold", "yfinance", "GC=F"),
    ],
}

LOOKBACKS = {
    "1D": 1,
    "1W": 7,
    "1M": 30,
    "52W": 364,
}

# ---------------------------------------------------------------------------
# Source-specific fetchers
# Each returns a pandas Series indexed by (normalized, tz-naive) date.
# ---------------------------------------------------------------------------

def fetch_yfinance(ticker: str) -> pd.Series:
    hist = yf.Ticker(ticker).history(period="2y", interval="1d", auto_adjust=False)
    if hist.empty:
        raise ValueError(f"No data returned for {ticker}")
    s = hist["Close"]
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    return s.dropna()


def fetch_ecb(series_key: str) -> pd.Series:
    parts = series_key.split(".")
    flow = parts[0]
    key = ".".join(parts[1:])  # drop the leading dataflow - it's already in the path
    url = f"https://data-api.ecb.europa.eu/service/data/{flow}/{key}"
    params = {
        "format": "csvdata",
        "startPeriod": (TODAY - dt.timedelta(days=800)).isoformat(),
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df = df[["TIME_PERIOD", "OBS_VALUE"]].dropna()
    df["TIME_PERIOD"] = pd.to_datetime(df["TIME_PERIOD"])
    s = df.set_index("TIME_PERIOD")["OBS_VALUE"]
    return s.sort_index()


def fetch_fred(series_id: str) -> pd.Series:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna()
    return df.set_index("date")["value"].sort_index()


def fetch_awattar(zone: str) -> pd.Series:
    # aWATTar publishes day-ahead spot prices for AT/DE with no auth needed,
    # but only a short rolling history (recent past + next-day prices) -
    # so 1M/52W/YTD comparisons for this instrument will often show "-".
    base = "https://api.awattar.at/v1/marketdata" if zone == "AT" else "https://api.awattar.de/v1/marketdata"
    r = requests.get(base, timeout=30)
    r.raise_for_status()
    data = r.json()["data"]
    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["start_timestamp"], unit="ms").dt.normalize()
    daily = df.groupby("date")["marketprice"].mean()
    return daily.sort_index()


FETCHERS = {
    "yfinance": fetch_yfinance,
    "ecb": fetch_ecb,
    "fred": fetch_fred,
    "awattar": fetch_awattar,
}

# ---------------------------------------------------------------------------
# Change calculation
# ---------------------------------------------------------------------------

def nearest_on_or_before(series: pd.Series, target_date: pd.Timestamp):
    eligible = series[series.index <= target_date]
    if eligible.empty:
        return None, None
    d = eligible.index[-1]
    return d, eligible.iloc[-1]


def compute_row(name: str, series: pd.Series) -> dict:
    series = series.dropna().sort_index()
    if series.empty:
        return {"Instrument": name, "Error": "no data"}

    latest_date = series.index[-1]
    latest_val = float(series.iloc[-1])
    row = {
        "Instrument": name,
        "Timestamp": latest_date.strftime("%Y-%m-%d"),
        "Price": round(latest_val, 4),
    }

    for label, days in LOOKBACKS.items():
        target = latest_date - pd.Timedelta(days=days)
        _, past_val = nearest_on_or_before(series, target)
        if past_val is None or past_val == 0:
            row[f"{label} %"] = None
            row[f"{label} abs"] = None
        else:
            row[f"{label} abs"] = round(latest_val - past_val, 4)
            row[f"{label} %"] = round((latest_val / past_val - 1) * 100, 2)

    # YTD: last available close of the previous year
    jan1 = pd.Timestamp(latest_date.year, 1, 1)
    _, ytd_base = nearest_on_or_before(series, jan1 - pd.Timedelta(days=1))
    if ytd_base is None or ytd_base == 0:
        row["YTD %"] = None
        row["YTD abs"] = None
    else:
        row["YTD abs"] = round(latest_val - ytd_base, 4)
        row["YTD %"] = round((latest_val / ytd_base - 1) * 100, 2)

    return row

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_report() -> dict:
    sections = {}
    for category, items in INSTRUMENTS.items():
        rows = []
        for name, source, ident in items:
            try:
                series = FETCHERS[source](ident)
                rows.append(compute_row(name, series))
            except Exception as exc:  # noqa: BLE001 - keep the report alive
                print(f"[WARN] {name} ({source}:{ident}) failed: {exc}", file=sys.stderr)
                traceback.print_exc()
                rows.append({"Instrument": name, "Error": str(exc)})
        sections[category] = pd.DataFrame(rows)
    return sections


COLS_ORDER = [
    "Instrument", "Timestamp", "Price",
    "1D %", "1D abs", "1W %", "1W abs",
    "1M %", "1M abs", "52W %", "52W abs", "YTD %", "YTD abs",
]


def _fmt_cell(col: str, val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "<td>-</td>"
    if col.endswith("%"):
        cls = "pos" if val >= 0 else "neg"
        return f"<td class='{cls}'>{val:+.2f}%</td>"
    if col.endswith("abs"):
        cls = "pos" if val >= 0 else "neg"
        return f"<td class='{cls}'>{val:+.4f}</td>"
    return f"<td>{val}</td>"


def render_html(sections: dict, out_path: str = "output/index.html") -> None:
    html_parts = [
        "<html><head><meta charset='utf-8'>",
        "<title>Daily Market Dashboard</title>",
        "<style>",
        "body{font-family:Arial,sans-serif;margin:2em;background:#fafafa;}",
        "h2{margin-top:2em;}",
        "table{border-collapse:collapse;width:100%;margin-bottom:1em;background:#fff;}",
        "th,td{border:1px solid #ddd;padding:6px 10px;text-align:right;font-size:0.9em;}",
        "th{background:#333;color:#fff;}",
        "td:first-child,th:first-child{text-align:left;}",
        ".pos{color:#0a7d28;} .neg{color:#c0392b;}",
        "</style></head><body>",
        "<h1>Daily Market Dashboard</h1>",
        f"<p>Generated: {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} UTC</p>",
    ]

    for category, df in sections.items():
        html_parts.append(f"<h2>{category}</h2>")
        if df.empty:
            html_parts.append("<p>No data.</p>")
            continue

        for c in COLS_ORDER:
            if c not in df.columns:
                df[c] = None

        header_html = "<tr>" + "".join(f"<th>{c}</th>" for c in COLS_ORDER) + "</tr>"
        rows_html = []
        for _, r in df.iterrows():
            if isinstance(r.get("Error"), str):
                cells = [f"<td>{r['Instrument']}</td>", f"<td colspan='{len(COLS_ORDER) - 1}'>Error: {r['Error']}</td>"]
            else:
                cells = [f"<td>{r['Instrument']}</td>"]
                for c in COLS_ORDER[1:]:
                    cells.append(_fmt_cell(c, r.get(c)))
            rows_html.append("<tr>" + "".join(cells) + "</tr>")

        html_parts.append(f"<table>{header_html}{''.join(rows_html)}</table>")

    html_parts.append("</body></html>")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(html_parts))

    all_rows = []
    for category, df in sections.items():
        d = df.copy()
        d.insert(0, "Category", category)
        all_rows.append(d)
    pd.concat(all_rows, ignore_index=True).to_csv("output/data.csv", index=False)


if __name__ == "__main__":
    report_sections = build_report()
    render_html(report_sections)
    print("Report written to output/index.html")
