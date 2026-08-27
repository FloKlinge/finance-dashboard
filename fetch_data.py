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
  "Rates":[
        ("3M Euribor", "bundesbank", "BBIG1.D.D0.EUR.MMKT.EURIBOR.M03.BID._Z"),
        ("EUR 2Y (DE Bund)", "bundesbank", "BBSSY.D.REN.EUR.A610.000000WT0202.A"),
        ("EUR 5Y (DE Bund)", "bundesbank", "BBSSY.D.REN.EUR.A620.000000WT0505.A"),
        ("EUR 10Y (DE Bund)", "bundesbank", "BBSSY.D.REN.EUR.A630.000000WT1010.A"),
        ("EUR 30Y (DE Bund)", "bundesbank", "BBSSY.D.REN.EUR.A640.000000WT3030.A"),
        ("UST 2Y", "treasury", "2 Yr"),
        ("UST 10Y", "treasury", "10 Yr"),
        ("UST 30Y", "treasury", "30 Yr"),
    ],
    "Equities": [
        ("S&P 500", "yfinance", "^GSPC"),
        ("Nasdaq Composite", "yfinance", "^IXIC"),
        ("Euro Stoxx 50", "yfinance", "^STOXX50E"),
        ("DAX", "yfinance", "^GDAXI"),
        ("ATX", "yfinance", "^ATX"),
    ],
    "Commodities": [
        ("Brent Crude", "yfinance", "BZ=F"),
        ("WTI Crude", "yfinance", "CL=F"),
        ("Dutch TTF Nat Gas", "yfinance", "TTF=F"),
        ("AT Power (day-ahead, EUR/MWh)", "energycharts", "AT"),
        ("DE Power (day-ahead, EUR/MWh)", "energycharts", "DE-LU"),
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
    # Explicit start/end (rather than period="2y") avoids a known yfinance/
    # Yahoo quirk where period-based ranges can lag the most recent close
    # by a day compared to explicit dates.
    start = (TODAY - dt.timedelta(days=800)).isoformat()
    end = (TODAY + dt.timedelta(days=1)).isoformat()
    hist = yf.Ticker(ticker).history(start=start, end=end, interval="1d", auto_adjust=False)
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

def fetch_treasury_yield(tenor_col: str) -> pd.Series:
    # U.S. Treasury's own daily par yield curve export - published same-day
    # (typically by late afternoon US time), unlike FRED's mirror which can
    # lag by a day or more. Pull current + previous year for enough history.
    frames = []
    for yr in sorted({TODAY.year, TODAY.year - 1}):
        url = (
            f"https://home.treasury.gov/resource-center/data-chart-center/"
            f"interest-rates/daily-treasury-rates.csv/{yr}/all"
        )
        params = {
            "type": "daily_treasury_yield_curve",
            "field_tdr_date_value": yr,
            "page": "",
            "_format": "csv",
        }
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        frames.append(pd.read_csv(io.StringIO(r.text)))
    df = pd.concat(frames, ignore_index=True)

    date_col = next((c for c in df.columns if "date" in c.lower()), None)
    value_col = next((c for c in df.columns if c.strip().lower() == tenor_col.lower()), None)
    if date_col is None or value_col is None:
        raise ValueError(
            f"Unrecognized Treasury CSV shape for tenor '{tenor_col}'. "
            f"Columns found: {list(df.columns)}"
        )

    df[date_col] = pd.to_datetime(df[date_col], format="%m/%d/%Y")
    s = pd.to_numeric(df[value_col], errors="coerce")
    s.index = df[date_col]
    return s.dropna().sort_index()
  

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


def fetch_energycharts(zone: str) -> pd.Series:
    # Fraunhofer ISE's Energy-Charts API - no auth needed, real historical
    # depth via explicit start/end dates (unlike aWATTar's few-day window).
    url = "https://api.energy-charts.info/price"
    params = {
        "bzn": zone,
        "start": (TODAY - dt.timedelta(days=800)).isoformat(),
        "end": TODAY.isoformat(),
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()

    # Detect the relevant keys flexibly rather than assuming exact names.
    ts_key = next((k for k in data if "unix" in k.lower() or "time" in k.lower()), None)
    price_key = next((k for k in data if "price" in k.lower()), None)
    if ts_key is None or price_key is None:
        raise ValueError(
            f"Unrecognized Energy-Charts response shape for zone {zone}. "
            f"Keys found: {list(data.keys())}"
        )

    ts = pd.to_datetime(data[ts_key], unit="s")
    prices = pd.to_numeric(pd.Series(data[price_key], index=ts), errors="coerce").dropna()
    daily = prices.groupby(prices.index.normalize()).mean()
    return daily.sort_index()


FETCHERS = {
    "yfinance": fetch_yfinance,
    "ecb": fetch_ecb,
    "bundesbank": fetch_bundesbank,
    "fred": fetch_fred,
    "treasury": fetch_treasury_yield,
    "energycharts": fetch_energycharts,
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


# How to render the "abs" (absolute change) columns, per category.
# "bps"   -> value is in percentage points; display as basis points (x100), 1 decimal
# "1dec"  -> display the raw value with 1 decimal
# "4dec"  -> display the raw value with 4 decimals (default, e.g. FX rates)
ABS_FORMAT_BY_CATEGORY = {
    "Rates": "bps",
    "Equities": "1dec",
    "Commodities": "1dec",
}


def _fmt_cell(col: str, val, abs_format: str = "4dec") -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "<td>-</td>"
    if col.endswith("%"):
        cls = "pos" if val >= 0 else "neg"
        return f"<td class='{cls}'>{val:+.2f}%</td>"
    if col.endswith("abs"):
        cls = "pos" if val >= 0 else "neg"
        if abs_format == "bps":
            return f"<td class='{cls}'>{val * 100:+.1f}bps</td>"
        if abs_format == "1dec":
            return f"<td class='{cls}'>{val:+.1f}</td>"
        return f"<td class='{cls}'>{val:+.4f}</td>"
    return f"<td>{val}</td>"


def render_html(sections: dict, out_path: str = "output/index.html") -> None:
    html_parts = [
        "<html><head><meta charset='utf-8'>",
        "<title>Daily Market Dashboard</title>",
        "<style>",
        "body{font-family:Arial,sans-serif;margin:2em;background:#fafafa;}",
        "h2{margin-top:2em;}",
        "table{border-collapse:collapse;width:100%;margin-bottom:1em;background:#fff;table-layout:fixed;}",
        "th,td{border:1px solid #ddd;padding:6px 10px;text-align:right;font-size:0.9em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}",
        "th:nth-child(1),td:nth-child(1){width:17%;}",
        "th:nth-child(2),td:nth-child(2){width:9%;}",
        "th:nth-child(3),td:nth-child(3){width:8%;}",
        "th:nth-child(4),td:nth-child(4),th:nth-child(5),td:nth-child(5),"
        "th:nth-child(6),td:nth-child(6),th:nth-child(7),td:nth-child(7),"
        "th:nth-child(8),td:nth-child(8),th:nth-child(9),td:nth-child(9),"
        "th:nth-child(10),td:nth-child(10),th:nth-child(11),td:nth-child(11),"
        "th:nth-child(12),td:nth-child(12),th:nth-child(13),td:nth-child(13){width:6.6%;}",
        "th{background:#333;color:#fff;}",
        "td:first-child,th:first-child{text-align:left;}",
        ".pos{color:#0a7d28;} .neg{color:#c0392b;}",
        # Thicker divider at the start of each timeframe group (1D/1W/1M/52W/YTD)
        "th:nth-child(4),td:nth-child(4),th:nth-child(6),td:nth-child(6),"
        "th:nth-child(8),td:nth-child(8),th:nth-child(10),td:nth-child(10),"
        "th:nth-child(12),td:nth-child(12){border-left:2px solid #999;}",
        # Subtle alternating band per timeframe group, so % and abs visually pair up
        "td:nth-child(4),td:nth-child(5),td:nth-child(8),td:nth-child(9),"
        "td:nth-child(12),td:nth-child(13){background-color:#f2f2f2;}",
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

        abs_format = ABS_FORMAT_BY_CATEGORY.get(category, "4dec")
        header_labels = [
            (c + " (bps)") if (c.endswith("abs") and abs_format == "bps") else c
            for c in COLS_ORDER
        ]
        header_html = "<tr>" + "".join(f"<th>{c}</th>" for c in header_labels) + "</tr>"
        rows_html = []
        for _, r in df.iterrows():
            if isinstance(r.get("Error"), str):
                cells = [f"<td>{r['Instrument']}</td>", f"<td colspan='{len(COLS_ORDER) - 1}'>Error: {r['Error']}</td>"]
            else:
                cells = [f"<td>{r['Instrument']}</td>"]
                for c in COLS_ORDER[1:]:
                    cells.append(_fmt_cell(c, r.get(c), abs_format))
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
