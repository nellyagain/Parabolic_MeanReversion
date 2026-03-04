#!/usr/bin/env python3
"""
Synthetic OHLC Data Generator for Parabolic Mean Reversion Backtesting.

Generates realistic daily OHLC data with embedded patterns:
- Parabolic advances (for SHORT setup detection)
- Washout crashes (for LONG setup detection)
- Normal trending / mean-reverting regimes
- Realistic volume, gaps, and volatility clustering

Output format: V8 TXT with <TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>
"""

import os
import math
import random
from datetime import datetime, timedelta

SEED = 42


def generate_ticker_data(ticker: str, n_bars: int = 1000,
                         base_price: float = 100.0,
                         inject_parabolic: bool = True,
                         inject_washout: bool = True,
                         avg_volume: float = 5_000_000,
                         seed: int = None) -> list:
    """Generate realistic OHLC+Volume data for one ticker.

    Returns list of dicts with keys: date, open, high, low, close, volume.
    """
    if seed is not None:
        rng = random.Random(seed)
    else:
        rng = random.Random()

    bars = []
    price = base_price
    vol_base = avg_volume

    # Volatility state (GARCH-like clustering)
    vol_pct = 0.02  # daily vol

    start_date = datetime(2018, 1, 2)

    # Plan injection points for parabolic advances and washouts
    parabolic_zones = []
    washout_zones = []

    if inject_parabolic:
        # 2-4 parabolic advance zones
        n_para = rng.randint(2, 4)
        for _ in range(n_para):
            start = rng.randint(100, n_bars - 200)
            length = rng.randint(15, 40)
            parabolic_zones.append((start, start + length))

    if inject_washout:
        # 1-3 washout crash zones (after parabolic runs)
        n_wash = rng.randint(1, 3)
        for _ in range(n_wash):
            start = rng.randint(200, n_bars - 100)
            length = rng.randint(5, 15)
            washout_zones.append((start, start + length))

    def in_zone(i, zones):
        for s, e in zones:
            if s <= i <= e:
                return True, (e - s), (i - s)
        return False, 0, 0

    for i in range(n_bars):
        date = start_date + timedelta(days=i)
        # Skip weekends
        while date.weekday() >= 5:
            date += timedelta(days=1)

        # Volatility clustering (mean-reverting)
        vol_pct = max(0.005, min(0.08, vol_pct + rng.gauss(0, 0.002)))

        # Base return
        drift = 0.0002  # slight upward drift
        ret = rng.gauss(drift, vol_pct)

        # Check if in parabolic zone
        is_para, para_len, para_pos = in_zone(i, parabolic_zones)
        if is_para:
            # Accelerating gains
            progress = para_pos / max(para_len, 1)
            ret = abs(rng.gauss(0.015 + 0.02 * progress, 0.005))
            vol_pct = max(vol_pct, 0.03)

        # Check if in washout zone
        is_wash, wash_len, wash_pos = in_zone(i, washout_zones)
        if is_wash:
            # Sharp decline
            progress = wash_pos / max(wash_len, 1)
            ret = -abs(rng.gauss(0.03 + 0.04 * progress, 0.01))
            vol_pct = max(vol_pct, 0.04)

        # Compute OHLC
        new_price = price * (1 + ret)
        new_price = max(new_price, 1.0)  # floor

        # Open with possible gap
        gap = rng.gauss(0, vol_pct * 0.3)
        open_price = price * (1 + gap)
        open_price = max(open_price, 1.0)

        close_price = new_price

        # High/low
        intraday_range = abs(close_price - open_price) + abs(rng.gauss(0, vol_pct * price * 0.5))
        if close_price >= open_price:
            high = max(open_price, close_price) + abs(rng.gauss(0, intraday_range * 0.3))
            low = min(open_price, close_price) - abs(rng.gauss(0, intraday_range * 0.2))
        else:
            high = max(open_price, close_price) + abs(rng.gauss(0, intraday_range * 0.2))
            low = min(open_price, close_price) - abs(rng.gauss(0, intraday_range * 0.3))

        high = max(high, max(open_price, close_price))
        low = min(low, min(open_price, close_price))
        low = max(low, 0.5)

        # Volume with regime sensitivity
        vol_mult = 1.0
        if is_para:
            vol_mult = 1.5 + progress * 2.0  # volume increases during parabolic
        elif is_wash:
            vol_mult = 2.0 + progress * 3.0  # climax volume during washout

        volume = max(100, int(vol_base * vol_mult * (0.5 + rng.random())))

        bars.append({
            'date': date,
            'open': round(open_price, 2),
            'high': round(high, 2),
            'low': round(low, 2),
            'close': round(close_price, 2),
            'volume': volume,
        })

        price = close_price

    return bars


def write_v8_txt(ticker: str, bars: list, output_dir: str):
    """Write bars in V8 TXT format."""
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, f"{ticker}.US.txt")

    with open(filepath, 'w') as f:
        f.write("<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>\n")
        for bar in bars:
            date_str = bar['date'].strftime('%Y%m%d')
            f.write(f"{ticker}.US,D,{date_str},000000,"
                    f"{bar['open']:.2f},{bar['high']:.2f},{bar['low']:.2f},{bar['close']:.2f},"
                    f"{bar['volume']},0\n")

    return filepath


def write_legacy_csv(ticker: str, bars: list, output_dir: str, exchange: str = "NASDAQ"):
    """Write bars in legacy CSV format (time,open,high,low,close)."""
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, f"{exchange}_{ticker}, 1D (1).csv")

    with open(filepath, 'w') as f:
        f.write("time,open,high,low,close\n")
        for bar in bars:
            ts = int(bar['date'].timestamp())
            f.write(f"{ts},{bar['open']:.2f},{bar['high']:.2f},{bar['low']:.2f},{bar['close']:.2f}\n")

    return filepath


# Ticker universe with different characteristics
LARGE_CAP_TICKERS = [
    ("AAPL", 180, 8e6), ("MSFT", 350, 7e6), ("GOOGL", 140, 5e6),
    ("AMZN", 170, 6e6), ("NVDA", 500, 10e6), ("META", 350, 8e6),
    ("TSLA", 250, 15e6), ("AMD", 120, 12e6), ("NFLX", 450, 4e6),
    ("CRM", 250, 3e6), ("AVGO", 800, 2e6), ("COST", 550, 1.5e6),
    ("ADBE", 500, 2e6), ("ORCL", 110, 5e6), ("QCOM", 150, 4e6),
    ("INTC", 35, 15e6), ("CSCO", 50, 8e6), ("TXN", 170, 3e6),
    ("MU", 85, 8e6), ("AMAT", 160, 4e6),
]

MID_CAP_TICKERS = [
    ("ROKU", 70, 3e6), ("SNAP", 12, 10e6), ("PINS", 30, 5e6),
    ("COIN", 180, 6e6), ("HOOD", 15, 8e6), ("MARA", 20, 12e6),
    ("RIOT", 12, 10e6), ("PLTR", 20, 15e6), ("SOFI", 8, 12e6),
    ("DKNG", 35, 5e6), ("DASH", 100, 3e6), ("ABNB", 140, 4e6),
    ("RBLX", 40, 6e6), ("UPST", 30, 4e6), ("AFRM", 35, 5e6),
]

SMALL_CAP_TICKERS = [
    ("BBBY", 5, 20e6), ("AMC", 8, 25e6), ("GME", 15, 15e6),
    ("CLOV", 3, 8e6), ("WISH", 2, 6e6), ("IRNT", 4, 5e6),
    ("SPCE", 5, 8e6), ("LCID", 6, 10e6), ("RIVN", 15, 6e6),
    ("MVIS", 3, 7e6), ("WKHS", 4, 5e6), ("GOEV", 2, 4e6),
    ("SKLZ", 3, 5e6), ("CLNE", 4, 3e6), ("OPEN", 5, 6e6),
]


def generate_full_universe(output_dir: str, seed: int = SEED):
    """Generate synthetic data for 50 tickers across market cap segments."""
    rng = random.Random(seed)
    files = []

    all_tickers = (
        [(t, p, v, "large") for t, p, v in LARGE_CAP_TICKERS] +
        [(t, p, v, "mid") for t, p, v in MID_CAP_TICKERS] +
        [(t, p, v, "small") for t, p, v in SMALL_CAP_TICKERS]
    )

    for i, (ticker, base_price, avg_vol, cap_type) in enumerate(all_tickers):
        n_bars = rng.randint(800, 1500)
        inject_para = rng.random() < 0.7  # 70% chance of parabolic pattern
        inject_wash = rng.random() < 0.6  # 60% chance of washout

        # Small caps more likely to have extreme patterns
        if cap_type == "small":
            inject_para = True
            inject_wash = True

        bars = generate_ticker_data(
            ticker, n_bars=n_bars, base_price=base_price,
            inject_parabolic=inject_para, inject_washout=inject_wash,
            avg_volume=avg_vol, seed=seed + i
        )

        filepath = write_v8_txt(ticker, bars, output_dir)
        files.append(filepath)
        print(f"  Generated {ticker:<6} | {n_bars:>4} bars | price ~${base_price:>6.0f} | "
              f"parabolic={inject_para} | washout={inject_wash} | {cap_type}")

    return files


if __name__ == "__main__":
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "synthetic_data")
    print("Generating synthetic OHLC data...")
    files = generate_full_universe(output_dir)
    print(f"\nGenerated {len(files)} ticker files in {output_dir}")
