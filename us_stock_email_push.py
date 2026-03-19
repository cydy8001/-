#!/usr/bin/env python3
"""Daily US stock email push: market cap >= threshold (default: $40B)."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import smtplib
import ssl
import sys
import time
from statistics import mean
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

TRADINGVIEW_SCAN_URL = "https://scanner.tradingview.com/america/scan"
FINVIZ_URL = "https://finviz.com/screener.ashx?v=111&f=geo_usa,cap_largeover,ind_stocksonly,exch_{exchange}&o=-marketcap&r={start}"
STOOQ_DAILY_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"


def load_config() -> dict:
    load_dotenv()

    sender = os.getenv("EMAIL_SENDER")
    app_password = os.getenv("EMAIL_APP_PASSWORD")
    receiver_raw = os.getenv("EMAIL_RECEIVER", "cydy8001@gmail.com")
    receivers = [x.strip() for x in receiver_raw.split(",") if x.strip()]
    if not receivers:
        receivers = ["cydy8001@gmail.com"]
    market_cap_billion = float(os.getenv("MARKET_CAP_MIN_BILLION", "40"))

    missing = []
    if not sender:
        missing.append("EMAIL_SENDER")
    if not app_password:
        missing.append("EMAIL_APP_PASSWORD")

    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    return {
        "sender": sender,
        "app_password": app_password,
        "receivers": receivers,
        "market_cap_min": int(market_cap_billion * 1_000_000_000),
        "market_cap_billion": market_cap_billion,
    }


def parse_market_cap_to_int(raw: str) -> int:
    value = raw.strip().upper().replace(" ", "")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMBT])", value)
    if not match:
        raise ValueError(f"Unsupported market cap format: {raw}")

    number = float(match.group(1))
    unit = match.group(2)
    unit_scale = {
        "K": 1_000,
        "M": 1_000_000,
        "B": 1_000_000_000,
        "T": 1_000_000_000_000,
    }
    return int(number * unit_scale[unit])


def parse_price(raw: str) -> float | None:
    value = raw.replace(",", "").strip()
    try:
        return float(value)
    except ValueError:
        return None


def fetch_finviz_page(session: requests.Session, exchange: str, start: int) -> List[dict]:
    url = FINVIZ_URL.format(exchange=exchange, start=start)

    for attempt in range(3):
        try:
            response = session.get(url, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            table = soup.select_one("#screener-table table")
            if table is None:
                return []

            rows = table.find_all("tr", class_="styled-row")
            page_stocks: List[dict] = []
            for row in rows:
                cols = row.find_all("td")
                if len(cols) < 11:
                    continue

                symbol = cols[1].get_text(strip=True)
                name = cols[2].get_text(strip=True)
                exchange_name = "NASDAQ" if exchange == "nasd" else "NYSE"
                price = parse_price(cols[8].get_text(strip=True))
                market_cap_raw = cols[6].get_text(strip=True)

                try:
                    market_cap = parse_market_cap_to_int(market_cap_raw)
                except ValueError:
                    continue

                page_stocks.append(
                    {
                        "symbol": symbol,
                        "name": name,
                            "exchange": exchange_name,
                        "price": price,
                        "currency": "USD",
                        "market_cap": market_cap,
                    }
                )
            return page_stocks
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))

    return []


def fetch_finviz_stocks_for_exchange(session: requests.Session, exchange: str) -> List[dict]:
    all_stocks: List[dict] = []
    start = 1

    while True:
        page = fetch_finviz_page(session, exchange=exchange, start=start)
        if not page:
            break

        all_stocks.extend(page)
        if len(page) < 20:
            break

        start += 20
        time.sleep(0.3)

    # Remove potential duplicates if site layout changes and repeats rows.
    dedup: Dict[str, dict] = {}
    for stock in all_stocks:
        dedup[stock["symbol"]] = stock

    return list(dedup.values())


def fetch_finviz_stocks(session: requests.Session) -> List[dict]:
    all_stocks = []
    for exch in ("nasd", "nyse"):
        all_stocks.extend(fetch_finviz_stocks_for_exchange(session, exchange=exch))

    dedup: Dict[str, dict] = {}
    for stock in all_stocks:
        dedup[stock["symbol"]] = stock
    return list(dedup.values())


def fetch_tradingview_page(
    session: requests.Session,
    market_cap_min: int,
    start: int,
    end: int,
) -> dict:
    payload = {
        "filter": [
            {"left": "exchange", "operation": "in_range", "right": ["NASDAQ", "NYSE"]},
            {"left": "market_cap_basic", "operation": "egreater", "right": market_cap_min},
            {"left": "type", "operation": "in_range", "right": ["stock"]},
            {"left": "subtype", "operation": "in_range", "right": ["common"]},
        ],
        "options": {"lang": "en"},
        "columns": [
            "name",
            "description",
            "exchange",
            "type",
            "subtype",
            "market_cap_basic",
            "average_volume_10d_calc",
            "average_volume_30d_calc",
            "high",
            "price_52_week_high",
            "close",
        ],
        "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
        "range": [start, end],
    }

    for attempt in range(3):
        try:
            response = session.post(TRADINGVIEW_SCAN_URL, json=payload, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))

    return {"totalCount": 0, "data": []}


def fetch_tradingview_candidates(session: requests.Session, market_cap_min: int) -> List[dict]:
    page_size = 200
    first = fetch_tradingview_page(
        session=session,
        market_cap_min=market_cap_min,
        start=0,
        end=page_size - 1,
    )

    total_count = int(first.get("totalCount") or 0)
    rows = list(first.get("data") or [])

    for start in range(page_size, total_count, page_size):
        page = fetch_tradingview_page(
            session=session,
            market_cap_min=market_cap_min,
            start=start,
            end=start + page_size - 1,
        )
        rows.extend(page.get("data") or [])
        time.sleep(0.1)

    results: List[dict] = []
    for row in rows:
        d = row.get("d", [])
        if len(d) < 11:
            continue

        symbol = d[0]
        name = d[1] or symbol
        exchange = d[2]
        sec_type = d[3]
        subtype = d[4]
        market_cap = d[5]
        avg_vol_10 = d[6]
        avg_vol_30 = d[7]
        high_1d = d[8]
        high_52w = d[9]
        close_price = d[10]

        numeric_fields = [market_cap, avg_vol_10, avg_vol_30, high_1d, high_52w, close_price]
        if any(not isinstance(v, (int, float)) for v in numeric_fields):
            continue

        results.append(
            {
                "symbol": symbol,
                "name": name,
                "exchange": exchange,
                "type": sec_type,
                "subtype": subtype,
                "market_cap": int(market_cap),
                "avg_vol_10": float(avg_vol_10),
                "avg_vol_30": float(avg_vol_30),
                "high_1d": float(high_1d),
                "high_52w": float(high_52w),
                "close": float(close_price),
                "currency": "USD",
            }
        )

    return results


def fetch_stooq_history(session: requests.Session, symbol: str) -> List[dict]:
    candidates = [
        f"{symbol.lower()}.us",
        f"{symbol.lower().replace('-', '.')}.us",
    ]

    for stooq_symbol in candidates:
        url = STOOQ_DAILY_URL.format(symbol=stooq_symbol)
        try:
            response = session.get(url, timeout=20)
            response.raise_for_status()
        except requests.RequestException:
            continue

        lines = [line for line in response.text.splitlines() if line.strip()]
        if len(lines) <= 1 or lines[0].startswith("No data"):
            continue

        rows = []
        for row in lines[1:]:
            parts = row.split(",")
            if len(parts) != 6:
                continue

            date, open_, high, low, close, volume = parts
            try:
                rows.append(
                    {
                        "date": date,
                        "open": float(open_),
                        "high": float(high),
                        "low": float(low),
                        "close": float(close),
                        "volume": float(volume),
                    }
                )
            except ValueError:
                continue

        if rows:
            rows.sort(key=lambda x: x["date"])
            return rows

    return []


def calculate_metrics_from_history(history: List[dict]) -> Optional[dict]:
    if len(history) < 60:
        return None

    recent = history[-252:] if len(history) >= 252 else history
    latest = recent[-1]
    volumes = [x["volume"] for x in recent if x["volume"] > 0]
    if len(volumes) < 30:
        return None

    avg_vol_10 = mean(volumes[-10:])
    avg_vol_30 = mean(volumes[-30:])
    if avg_vol_30 <= 0:
        return None

    high_52w = max(x["high"] for x in recent)
    high_1d = latest["high"]
    if high_52w <= 0:
        return None

    vol_ratio = avg_vol_10 / avg_vol_30
    distance_to_52w_high = (high_52w - high_1d) / high_52w

    return {
        "avg_vol_10": avg_vol_10,
        "avg_vol_30": avg_vol_30,
        "vol_ratio": vol_ratio,
        "high_1d": high_1d,
        "high_52w": high_52w,
        "distance_to_52w_high": distance_to_52w_high,
        "close": latest["close"],
    }


def format_market_cap(value: int) -> str:
    return f"${value / 1_000_000_000:,.2f}B"


def apply_strategy_filters(quotes: List[dict], market_cap_min: int) -> List[dict]:
    selected: List[dict] = []

    for q in quotes:
        # Rule 1: Market Cap >= 40B
        market_cap = q.get("market_cap")
        if not isinstance(market_cap, (int, float)):
            continue
        if market_cap < market_cap_min:
            continue

        # Rule 3: Exchange = NASDAQ or NYSE
        exchange = q.get("exchange", "")
        if exchange not in ("NASDAQ", "NYSE"):
            continue

        # Rule 4: Common stock
        if q.get("type") != "stock" or q.get("subtype") != "common":
            continue

        avg_vol_10 = q.get("avg_vol_10")
        avg_vol_30 = q.get("avg_vol_30")
        high_1d = q.get("high_1d")
        high_52w = q.get("high_52w")
        close_price = q.get("close")
        if not all(isinstance(v, (int, float)) for v in [avg_vol_10, avg_vol_30, high_1d, high_52w, close_price]):
            continue
        if avg_vol_30 <= 0 or high_52w <= 0:
            continue

        vol_ratio = avg_vol_10 / avg_vol_30
        distance_to_52w_high = (high_52w - high_1d) / high_52w

        # Rule 2: Average Volume 10D >= 130% of Average Volume 30D
        if vol_ratio < 1.3:
            continue

        # Rule 5: 1-day high is >=10% below 52-week high
        if distance_to_52w_high < 0.10:
            continue

        symbol = q.get("symbol")
        name = q.get("name") or "-"
        price = close_price
        currency = q.get("currency", "USD")

        selected.append(
            {
                "symbol": symbol,
                "name": name,
                "exchange": exchange,
                "price": price,
                "currency": currency,
                "market_cap": int(market_cap),
                "avg_vol_10": avg_vol_10,
                "avg_vol_30": avg_vol_30,
                "vol_ratio": vol_ratio,
                "high_1d": high_1d,
                "high_52w": high_52w,
                "distance_to_52w_high": distance_to_52w_high,
            }
        )

    selected.sort(key=lambda x: x["market_cap"], reverse=True)
    return selected


def build_email_body(stocks: List[dict], market_cap_billion: float) -> tuple[str, str]:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rows_text = []
    rows_html = []
    for i, s in enumerate(stocks, start=1):
        price = "-" if s["price"] is None else f"{s['price']:.2f} {s['currency']}"
        cap = format_market_cap(s["market_cap"])
        vol_ratio = f"{s['vol_ratio'] * 100:.1f}%"
        dist_52w = f"{s['distance_to_52w_high'] * 100:.1f}%"
        rows_text.append(
            f"{i:>3}. {s['symbol']:<8} | {cap:>12} | VOL10/30 {vol_ratio:>8} | 52W Gap {dist_52w:>7} | {s['name']}"
        )
        rows_html.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td>{s['symbol']}</td>"
            f"<td>{s['name']}</td>"
            f"<td>{s['exchange']}</td>"
            f"<td>{price}</td>"
            f"<td>{cap}</td>"
            f"<td>{s['avg_vol_10']:,.0f}</td>"
            f"<td>{s['avg_vol_30']:,.0f}</td>"
            f"<td>{vol_ratio}</td>"
            f"<td>{s['high_1d']:.2f}</td>"
            f"<td>{s['high_52w']:.2f}</td>"
            f"<td>{dist_52w}</td>"
            "</tr>"
        )

    text_body = (
        f"US Stocks with Market Cap >= ${market_cap_billion:.0f}B\n"
        f"Generated at: {now}\n"
        f"Count: {len(stocks)}\n\n"
        + "\n".join(rows_text)
    )

    html_body = f"""
    <html>
      <body>
        <h2>US Stocks with Market Cap &ge; ${market_cap_billion:.0f}B</h2>
                <p><strong>Strategy Filters:</strong></p>
                <ol>
                    <li>Market Cap &ge; ${market_cap_billion:.0f}B</li>
                    <li>Average Volume 10D &ge; 130% of Average Volume 30D</li>
                    <li>Exchange = NASDAQ or NYSE</li>
                    <li>Common Stock only</li>
                    <li>1-day High is at least 10% below 52-week High</li>
                </ol>
        <p><strong>Generated at:</strong> {now}<br><strong>Count:</strong> {len(stocks)}</p>
        <table border="1" cellspacing="0" cellpadding="6" style="border-collapse: collapse; font-family: Arial, sans-serif; font-size: 13px;">
          <thead>
            <tr>
              <th>#</th>
              <th>Symbol</th>
              <th>Name</th>
              <th>Exchange</th>
              <th>Price</th>
              <th>Market Cap</th>
                            <th>Avg Vol 10D</th>
                            <th>Avg Vol 30D</th>
                            <th>Vol 10D/30D</th>
                            <th>High 1D</th>
                            <th>High 52W</th>
                            <th>Gap to 52W High</th>
            </tr>
          </thead>
          <tbody>
            {''.join(rows_html)}
          </tbody>
        </table>
      </body>
    </html>
    """

    return text_body, html_body


def send_email(sender: str, app_password: str, receivers: List[str], subject: str, text_body: str, html_body: str) -> None:
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(receivers)

    message.attach(MIMEText(text_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(sender, app_password)
        server.sendmail(sender, receivers, message.as_string())


def main() -> int:
    parser = argparse.ArgumentParser(description="Email US stocks above market-cap threshold.")
    parser.add_argument("--dry-run", action="store_true", help="Do not send email; print sample output only.")
    parser.add_argument(
        "--save-json",
        default="",
        help="Optional output path to save filtered results as JSON.",
    )
    args = parser.parse_args()

    try:
        cfg = load_config()
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; stock-email-bot/1.0)",
            "Accept": "application/json,text/plain,*/*",
        }
    )

    quotes = fetch_tradingview_candidates(session, cfg["market_cap_min"])
    stocks = apply_strategy_filters(quotes, cfg["market_cap_min"])

    if args.save_json:
        with open(args.save_json, "w", encoding="utf-8") as f:
            json.dump(stocks, f, ensure_ascii=False, indent=2)

    subject = f"[Daily] US Stocks >= ${cfg['market_cap_billion']:.0f}B ({len(stocks)} found)"
    text_body, html_body = build_email_body(stocks, cfg["market_cap_billion"])

    if args.dry_run:
        print(subject)
        print(text_body[:2000])
        return 0

    send_email(
        sender=cfg["sender"],
        app_password=cfg["app_password"],
        receivers=cfg["receivers"],
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )
    print(f"[OK] Email sent to {', '.join(cfg['receivers'])}, count={len(stocks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
