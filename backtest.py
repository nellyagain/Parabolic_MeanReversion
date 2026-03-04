#!/usr/bin/env python3
"""
Parabolic Snapback & Washout [Daily Screener V1] — Comprehensive Backtester

Replicates the Pine Script V1 logic from the architecture plan against CSV OHLCV data.
Includes volume-dependent gates (climax volume via RVOL, dollar volume filter) and
Run AVWAP trigger modes matching the Pine Script. All detection, state machines,
triggers, risk management, and targets operate identically to the Pine Script.

Trade outcome tracking:
  - SHORT setups: entry at trigger bar close, stop at computed stop price,
    targets at 10 MA and 20 MA. Trade resolves when price hits stop (loss)
    or target (win), or after max_trade_bars (timeout).
  - LONG setups: entry at trigger bar close, stop at computed stop price,
    targets at 10 MA and 20 MA. Same resolution logic.
"""

import csv
import os
import sys
import glob
import math
import argparse
from datetime import datetime, timezone
from dataclasses import dataclass


# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION — matches Pine Script defaults
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Config:
    # Parabolic SHORT — Advance Detection
    # Note: Pine Script defaults are 60/300% for screening thousands of stocks.
    # For backtesting 49 large-cap names, we use calibrated thresholds that
    # capture the same "parabolic overextension" concept at large-cap scale.
    enable_short: bool = True
    largecap_gain_pct: float = 40.0   # Calibrated for large-cap (plan default: 60)
    smallcap_gain_pct: float = 300.0
    cap_cutoff_price: float = 20.0
    use_manual_threshold: bool = False
    manual_gain_pct: float = 80.0
    gain_lookback: int = 20
    min_green_days: int = 3
    min_ext_above_ma: float = 20.0    # Calibrated for large-cap (plan default: 30)
    ext_ma_len: int = 20
    use_ext_bb_filter: bool = False
    bb_dev_mult: float = 3.0

    # Parabolic LONG — Washout Detection (V2: velocity + selling climax)
    enable_long: bool = True
    min_crash_pct: float = 30.0       # Calibrated for large-cap (plan default: 50)
    crash_window: int = 15            # Wider window for large-cap crashes (plan default: 5)
    crash_velocity_min: float = 3.0   # V4: Min crash speed %/bar (sweep optimal: 3.0)
    require_selling_climax: bool = True  # Require capitulation volume during crash
    selling_climax_rvol: float = 3.0  # V4: RVOL threshold for selling climax (sweep optimal: 3.0)
    require_prior_run: bool = True
    prior_run_min_pct: float = 40.0   # Calibrated for large-cap (plan default: 100)
    prior_run_lookback: int = 60      # Wider lookback (plan default: 40)

    # Climax Volume
    use_climax_vol: bool = False      # V4: disabled — kills too many SHORT setups without improving quality
    rvol_threshold: float = 3.0
    rvol_baseline: int = 20
    climax_window_bars: int = 1

    # Liquidity Filters
    min_price: float = 5.0
    min_adr_pct: float = 1.0         # Lowered for stable large-caps (plan default: 2)
    min_avg_dollar_vol: float = 0.0   # V4: disabled — reduces setup count without improving edge
    dollar_vol_len: int = 20

    # Setup Trigger (Daily Proxy)
    short_trigger: str = "Close < Prior Low"
    long_trigger: str = "First Green Day"
    min_bars_after_setup: int = 0

    # Targets
    target_ma_fast: int = 10
    target_ma_slow: int = 20

    # Trend Context
    use_trend_filter: bool = False
    trend_ma_len: int = 50

    # Split Exit — partial at 10MA, runner to 20MA with breakeven stop
    split_exit: bool = True
    split_pct: float = 50.0  # % of position to close at 10MA; rest runs to 20MA

    # Quality Gates
    min_close_strength: float = 0.0
    use_adr_filter: bool = True
    adr_len: int = 20
    max_stop_vs_adr: float = 1.5     # Tightened from 2.5x for loss containment (plan default: 1.0)

    # Risk Management
    short_stop_mode: str = "Run Peak"    # V4: Run Peak best for shorts (ATR too tight for parabolic vol)
    long_stop_mode: str = "ATR Based"    # V4: ATR-based (Washout Low too wide)
    stop_buffer: float = 0.2
    atr_len: int = 14
    atr_mult: float = 1.0           # V4: 1.0x ATR optimal for LONG washout bounce stops

    # Timeouts & Cooldown
    short_setup_timeout: int = 10     # More time for reversal (plan default: 5)
    long_setup_timeout: int = 10      # More time for bounce (plan default: 5)
    cooldown_bars: int = 3

    # Backtest-specific
    max_trade_bars: int = 50  # Max bars to hold a trade before timeout


# ═══════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Bar:
    timestamp: int
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    ohlc4: float = 0.0

    def __post_init__(self):
        self.ohlc4 = (self.open + self.high + self.low + self.close) / 4.0


@dataclass
class Trade:
    ticker: str
    direction: str  # "SHORT" or "LONG"
    entry_bar: int
    entry_date: str
    entry_price: float
    stop_price: float
    target_fast: float  # 10 MA
    target_slow: float  # 20 MA
    exit_bar: int = -1
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""  # "STOP", "TARGET_10MA", "TARGET_10MA_PARTIAL", "TARGET_20MA", "STOP_BREAKEVEN", "TIMEOUT"
    pnl_pct: float = 0.0
    r_multiple: float = 0.0
    bars_held: int = 0
    weight: float = 1.0  # Position weight (1.0 = full, 0.5 = half for split exits)
    is_runner: bool = False  # True for runner leg of split exit
    # Context metrics at entry
    extension_pct: float = 0.0
    rolling_gain_pct: float = 0.0
    green_streak: int = 0
    crash_from_peak: float = 0.0
    risk_pct: float = 0.0


# ═══════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def sma(values: list, length: int) -> float:
    """Simple moving average of last `length` values."""
    if len(values) < length:
        return sum(values) / len(values) if values else 0.0
    return sum(values[-length:]) / length


def atr_calc(bars: list, length: int) -> float:
    """Average True Range."""
    if len(bars) < 2:
        return 0.0
    trs = []
    start = max(1, len(bars) - length)
    for i in range(start, len(bars)):
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close)
        )
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0


def bb_upper(closes: list, length: int, mult: float) -> float:
    """Upper Bollinger Band."""
    if len(closes) < length:
        return float('inf')
    window = closes[-length:]
    mean = sum(window) / length
    variance = sum((x - mean) ** 2 for x in window) / length
    std = math.sqrt(variance)
    return mean + mult * std


def load_csv(filepath: str) -> list:
    """Load OHLCV data into Bar objects. Auto-detects format:
       - Legacy CSV: headers time,open,high,low,close (Unix timestamps)
       - V8 TXT: headers <TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>
    """
    bars = []
    with open(filepath, 'r') as f:
        first_line = f.readline().strip()
        f.seek(0)

        if '<TICKER>' in first_line or '<DATE>' in first_line:
            # V8 format: angle-bracket headers, YYYYMMDD dates
            # Strip angle brackets to get clean column names
            clean_header = first_line.replace('<', '').replace('>', '')
            header_line = f.readline()  # consume original header
            fieldnames = [h.strip() for h in clean_header.split(',')]
            reader = csv.DictReader(f, fieldnames=fieldnames)
            for row in reader:
                date_str = row['DATE'].strip()
                # Parse YYYYMMDD → YYYY-MM-DD
                iso_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                dt = datetime.strptime(date_str, '%Y%m%d').replace(tzinfo=timezone.utc)
                ts = int(dt.timestamp())
                vol = 0.0
                if 'VOL' in row:
                    try:
                        vol = float(row['VOL'])
                    except (ValueError, TypeError):
                        pass
                bars.append(Bar(
                    timestamp=ts,
                    date=iso_date,
                    open=float(row['OPEN']),
                    high=float(row['HIGH']),
                    low=float(row['LOW']),
                    close=float(row['CLOSE']),
                    volume=vol,
                ))
        else:
            # Legacy format: time,open,high,low,close
            reader = csv.DictReader(f)
            for row in reader:
                ts = int(row['time'])
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                vol = 0.0
                for vk in ('Volume', 'volume', 'vol'):
                    if vk in row:
                        try:
                            vol = float(row[vk])
                        except (ValueError, TypeError):
                            pass
                        break
                bars.append(Bar(
                    timestamp=ts,
                    date=dt.strftime('%Y-%m-%d'),
                    open=float(row['open']),
                    high=float(row['high']),
                    low=float(row['low']),
                    close=float(row['close']),
                    volume=vol,
                ))
    return bars


def extract_ticker_from_row(filepath: str) -> str:
    """Extract ticker from first data row of V8 format file (e.g., 'FIZZ.US' → 'FIZZ')."""
    with open(filepath, 'r') as f:
        f.readline()  # skip header
        first_data = f.readline().strip()
        if first_data:
            ticker_field = first_data.split(',')[0].strip()
            # Strip country suffix like '.US'
            if '.' in ticker_field:
                return ticker_field.split('.')[0]
            return ticker_field
    return os.path.splitext(os.path.basename(filepath))[0]


def extract_ticker(filename: str) -> str:
    """Extract ticker symbol from filename.
       Legacy: 'NASDAQ_AAPL, 1D (1).csv' → 'AAPL'
       V8 txt: 'FIZZ.US.txt' → extracted from first data row
    """
    base = os.path.basename(filename)
    # V8 txt format — extract ticker from file content
    if base.lower().endswith('.txt'):
        return extract_ticker_from_row(filename)
    # Legacy CSV format
    parts = base.split(',')[0]
    if '_' in parts:
        return parts.split('_', 1)[1]
    return parts


# ═══════════════════════════════════════════════════════════════════
# CORE ENGINE
# ═══════════════════════════════════════════════════════════════════

def run_backtest(ticker: str, bars: list, cfg: Config) -> list:
    """
    Run the full Parabolic Mean Reversion V1 detection and backtest engine.
    Returns a list of Trade objects.
    """
    trades = []
    n = len(bars)
    if n < max(cfg.gain_lookback, cfg.prior_run_lookback, cfg.ext_ma_len, cfg.adr_len, cfg.atr_len) + 5:
        return trades

    # Rolling state
    closes = []
    highs = []
    lows = []
    volumes = []
    dollar_vols = []  # close * volume per bar
    daily_range_pcts = []

    # Short state machine
    short_setup_active = False
    short_setup_bar = -1
    parabolic_peak = 0.0
    parabolic_peak_bar = -1
    advance_start_bar = -1
    advance_start_low = 0.0
    last_short_bar = -999

    # Long state machine
    long_setup_active = False
    long_setup_bar = -1
    washout_low = 0.0
    washout_low_bar = -1
    last_long_bar = -999

    # Climax volume tracking
    last_climax_bar = -999
    last_selling_climax_bar = -999  # For washout detection: high vol + red candle

    # Run AVWAP accumulators
    short_avwap_num = 0.0
    short_avwap_den = 0.0
    short_run_avwap = 0.0
    long_avwap_num = 0.0
    long_avwap_den = 0.0
    long_run_avwap = 0.0
    long_peak_bar_idx = -1  # for AVWAP anchor

    # Green streak
    green_streak = 0

    # Open trades being tracked
    open_trades: list = []

    for i in range(n):
        bar = bars[i]
        closes.append(bar.close)
        highs.append(bar.high)
        lows.append(bar.low)
        volumes.append(bar.volume)
        dollar_vols.append(bar.close * bar.volume)

        # ── Track open trades ──
        new_open_trades = []
        for t in open_trades:
            bars_held = i - t.entry_bar
            if t.exit_bar >= 0:
                new_open_trades.append(t)
                continue

            if t.direction == "SHORT":
                # Determine if both stop and target are hit on same bar
                stop_hit = bar.high >= t.stop_price
                tgt_fast_hit = not t.is_runner and bar.low <= t.target_fast and t.target_fast < t.entry_price
                tgt_slow_hit = bar.low <= t.target_slow and t.target_slow < t.entry_price

                # Intrabar ambiguity resolution: if both stop and target hit,
                # use proximity to open to decide which was hit first
                if stop_hit and (tgt_fast_hit or tgt_slow_hit):
                    dist_to_stop = abs(t.stop_price - bar.open)
                    tgt_price = t.target_fast if tgt_fast_hit else t.target_slow
                    dist_to_tgt = abs(tgt_price - bar.open)
                    if dist_to_tgt <= dist_to_stop:
                        stop_hit = False  # target was closer to open, hit first
                    else:
                        tgt_fast_hit = False
                        tgt_slow_hit = False

                if stop_hit:
                    t.exit_bar = i
                    t.exit_date = bar.date
                    t.exit_price = t.stop_price
                    t.exit_reason = "STOP_BREAKEVEN" if t.is_runner else "STOP"
                    t.pnl_pct = ((t.entry_price - t.exit_price) / t.entry_price) * 100
                    t.bars_held = bars_held
                    if t.risk_pct > 0:
                        t.r_multiple = t.pnl_pct / t.risk_pct
                elif tgt_fast_hit:
                    if cfg.split_exit:
                        t.exit_bar = i
                        t.exit_date = bar.date
                        t.exit_price = t.target_fast
                        t.exit_reason = "TARGET_10MA_PARTIAL"
                        t.pnl_pct = ((t.entry_price - t.exit_price) / t.entry_price) * 100
                        t.bars_held = bars_held
                        t.weight = cfg.split_pct / 100.0
                        if t.risk_pct > 0:
                            t.r_multiple = t.pnl_pct / t.risk_pct
                        runner = Trade(
                            ticker=t.ticker, direction="SHORT",
                            entry_bar=t.entry_bar, entry_date=t.entry_date,
                            entry_price=t.entry_price,
                            stop_price=t.entry_price,
                            target_fast=t.target_fast,
                            target_slow=t.target_slow,
                            extension_pct=t.extension_pct,
                            rolling_gain_pct=t.rolling_gain_pct,
                            green_streak=t.green_streak,
                            risk_pct=t.risk_pct,
                            weight=1.0 - cfg.split_pct / 100.0,
                            is_runner=True,
                        )
                        trades.append(runner)
                        new_open_trades.append(runner)
                    else:
                        t.exit_bar = i
                        t.exit_date = bar.date
                        t.exit_price = t.target_fast
                        t.exit_reason = "TARGET_10MA"
                        t.pnl_pct = ((t.entry_price - t.exit_price) / t.entry_price) * 100
                        t.bars_held = bars_held
                        if t.risk_pct > 0:
                            t.r_multiple = t.pnl_pct / t.risk_pct
                elif tgt_slow_hit:
                    t.exit_bar = i
                    t.exit_date = bar.date
                    t.exit_price = t.target_slow
                    t.exit_reason = "TARGET_20MA"
                    t.pnl_pct = ((t.entry_price - t.exit_price) / t.entry_price) * 100
                    t.bars_held = bars_held
                    if t.risk_pct > 0:
                        t.r_multiple = t.pnl_pct / t.risk_pct
                elif bars_held >= cfg.max_trade_bars:
                    t.exit_bar = i
                    t.exit_date = bar.date
                    t.exit_price = bar.close
                    t.exit_reason = "TIMEOUT"
                    t.pnl_pct = ((t.entry_price - t.exit_price) / t.entry_price) * 100
                    t.bars_held = bars_held
                    if t.risk_pct > 0:
                        t.r_multiple = t.pnl_pct / t.risk_pct
                else:
                    new_open_trades.append(t)
                    continue

            elif t.direction == "LONG":
                # Determine if both stop and target are hit on same bar
                stop_hit = bar.low <= t.stop_price
                tgt_fast_hit = not t.is_runner and bar.high >= t.target_fast and t.target_fast > t.entry_price
                tgt_slow_hit = bar.high >= t.target_slow and t.target_slow > t.entry_price

                # Intrabar ambiguity resolution
                if stop_hit and (tgt_fast_hit or tgt_slow_hit):
                    dist_to_stop = abs(t.stop_price - bar.open)
                    tgt_price = t.target_fast if tgt_fast_hit else t.target_slow
                    dist_to_tgt = abs(tgt_price - bar.open)
                    if dist_to_tgt <= dist_to_stop:
                        stop_hit = False
                    else:
                        tgt_fast_hit = False
                        tgt_slow_hit = False

                if stop_hit:
                    t.exit_bar = i
                    t.exit_date = bar.date
                    t.exit_price = t.stop_price
                    t.exit_reason = "STOP_BREAKEVEN" if t.is_runner else "STOP"
                    t.pnl_pct = ((t.exit_price - t.entry_price) / t.entry_price) * 100
                    t.bars_held = bars_held
                    if t.risk_pct > 0:
                        t.r_multiple = t.pnl_pct / t.risk_pct
                elif tgt_fast_hit:
                    if cfg.split_exit:
                        t.exit_bar = i
                        t.exit_date = bar.date
                        t.exit_price = t.target_fast
                        t.exit_reason = "TARGET_10MA_PARTIAL"
                        t.pnl_pct = ((t.exit_price - t.entry_price) / t.entry_price) * 100
                        t.bars_held = bars_held
                        t.weight = cfg.split_pct / 100.0
                        if t.risk_pct > 0:
                            t.r_multiple = t.pnl_pct / t.risk_pct
                        runner = Trade(
                            ticker=t.ticker, direction="LONG",
                            entry_bar=t.entry_bar, entry_date=t.entry_date,
                            entry_price=t.entry_price,
                            stop_price=t.entry_price,
                            target_fast=t.target_fast,
                            target_slow=t.target_slow,
                            crash_from_peak=t.crash_from_peak,
                            risk_pct=t.risk_pct,
                            weight=1.0 - cfg.split_pct / 100.0,
                            is_runner=True,
                        )
                        trades.append(runner)
                        new_open_trades.append(runner)
                    else:
                        t.exit_bar = i
                        t.exit_date = bar.date
                        t.exit_price = t.target_fast
                        t.exit_reason = "TARGET_10MA"
                        t.pnl_pct = ((t.exit_price - t.entry_price) / t.entry_price) * 100
                        t.bars_held = bars_held
                        if t.risk_pct > 0:
                            t.r_multiple = t.pnl_pct / t.risk_pct
                elif tgt_slow_hit:
                    t.exit_bar = i
                    t.exit_date = bar.date
                    t.exit_price = t.target_slow
                    t.exit_reason = "TARGET_20MA"
                    t.pnl_pct = ((t.exit_price - t.entry_price) / t.entry_price) * 100
                    t.bars_held = bars_held
                    if t.risk_pct > 0:
                        t.r_multiple = t.pnl_pct / t.risk_pct
                elif bars_held >= cfg.max_trade_bars:
                    t.exit_bar = i
                    t.exit_date = bar.date
                    t.exit_price = bar.close
                    t.exit_reason = "TIMEOUT"
                    t.pnl_pct = ((t.exit_price - t.entry_price) / t.entry_price) * 100
                    t.bars_held = bars_held
                    if t.risk_pct > 0:
                        t.r_multiple = t.pnl_pct / t.risk_pct
                else:
                    new_open_trades.append(t)
                    continue
        open_trades = new_open_trades

        # ── Populate daily range pcts BEFORE early-bar guard ──
        drp = ((bar.high / bar.low) - 1) * 100 if bar.low > 0 else 0.0
        daily_range_pcts.append(drp)

        # ── Green streak (must track before skip) ──
        if i > 0 and bar.close > bars[i - 1].close:
            green_streak += 1
        else:
            green_streak = 0

        # ── Early-bar guard ──
        enough_bars_gain = i >= cfg.gain_lookback
        enough_bars_peak = i >= cfg.prior_run_lookback
        min_bars_needed = max(cfg.gain_lookback, cfg.ext_ma_len, cfg.adr_len, cfg.atr_len) + 1

        if i < min_bars_needed:
            continue

        # ═══════════════════════════════════════════════════════════
        # LIQUIDITY GATE
        # ═══════════════════════════════════════════════════════════
        adr_pct = sma(daily_range_pcts, cfg.adr_len)
        avg_dollar_vol = sma(dollar_vols, cfg.dollar_vol_len) / 1e6  # in millions

        liquidity_ok = (bar.close >= cfg.min_price
                        and adr_pct >= cfg.min_adr_pct
                        and avg_dollar_vol >= cfg.min_avg_dollar_vol)

        # ═══════════════════════════════════════════════════════════
        # PARABOLIC ADVANCE DETECTION
        # ═══════════════════════════════════════════════════════════
        # Rolling gain
        window_lows = lows[max(0, i - cfg.gain_lookback + 1):i + 1]
        recent_low = min(window_lows) if window_lows else bar.low
        rolling_gain_pct = ((bar.close - recent_low) / recent_low) * 100 if recent_low > 0 else 0.0

        gain_threshold = cfg.manual_gain_pct if cfg.use_manual_threshold else \
            (cfg.largecap_gain_pct if bar.close >= cfg.cap_cutoff_price else cfg.smallcap_gain_pct)
        is_parabolic_gain = enough_bars_gain and rolling_gain_pct >= gain_threshold

        # Consecutive green days (tracked above before early-bar guard)
        has_green_streak = green_streak >= cfg.min_green_days

        # Extension above MA
        ext_ma = sma(closes, cfg.ext_ma_len)
        extension_pct = ((bar.close - ext_ma) / ext_ma) * 100 if ext_ma > 0 else 0.0
        is_extended = extension_pct >= cfg.min_ext_above_ma

        # Bollinger Band gate (optional)
        bb_ok = True
        if cfg.use_ext_bb_filter:
            bb_up = bb_upper(closes, cfg.ext_ma_len, cfg.bb_dev_mult)
            bb_ok = bar.close > bb_up

        # Trend filter (optional — OFF by default)
        trend_ok = True
        if cfg.use_trend_filter:
            trend_ma = sma(closes, cfg.trend_ma_len)
            trend_ok = bar.close > trend_ma

        # Combined advance detection
        parabolic_advance_detected = (liquidity_ok and is_parabolic_gain and
                                      has_green_streak and is_extended and
                                      bb_ok and trend_ok)

        # ═══════════════════════════════════════════════════════════
        # CLIMAX VOLUME DETECTION
        # ═══════════════════════════════════════════════════════════
        vol_baseline = sma(volumes, cfg.rvol_baseline)
        rvol = bar.volume / vol_baseline if vol_baseline > 0 else 0.0
        is_climax_volume = rvol >= cfg.rvol_threshold

        if is_climax_volume:
            last_climax_bar = i

        # Selling climax: high volume + red candle (for washout detection)
        is_selling_climax_bar = rvol >= cfg.selling_climax_rvol and bar.close < bar.open
        if is_selling_climax_bar:
            last_selling_climax_bar = i

        # Gate: climax must be within N bars of current bar
        climax_aligned = (i - last_climax_bar) <= cfg.climax_window_bars
        climax_vol_ok = climax_aligned if cfg.use_climax_vol else True

        # ═══════════════════════════════════════════════════════════
        # WASHOUT CRASH DETECTION (Long Side) — V2: Velocity + Selling Climax
        # ═══════════════════════════════════════════════════════════
        crash_from_peak = 0.0
        crash_velocity = 0.0
        bars_from_peak = 0
        is_crash_candidate = False
        had_prior_run = True
        peak_bar_idx = -1

        if enough_bars_peak:
            # Find recent peak (use last/most-recent occurrence to match Pine's ta.highestbars)
            peak_window = highs[max(0, i - cfg.prior_run_lookback + 1):i + 1]
            peak_high = max(peak_window)
            # Reverse search: find last occurrence (most recent bar with this high)
            last_idx = len(peak_window) - 1
            for pi in range(last_idx, -1, -1):
                if peak_window[pi] == peak_high:
                    last_idx = pi
                    break
            peak_offset = len(peak_window) - 1 - last_idx
            peak_bar_idx = i - peak_offset

            bars_from_peak = i - peak_bar_idx
            crash_from_peak = ((peak_high - bar.close) / peak_high) * 100 if peak_high > 0 else 0.0

            # V2: Velocity gate — crash must be fast enough (% per bar)
            crash_velocity = crash_from_peak / max(bars_from_peak, 1)
            is_velocity_crash = (crash_from_peak >= cfg.min_crash_pct
                                 and crash_velocity >= cfg.crash_velocity_min
                                 and bars_from_peak <= cfg.crash_window)

            # V2: Selling climax gate — require capitulation volume during crash
            has_selling_climax = True
            if cfg.require_selling_climax:
                # Check if any selling climax bar occurred between peak and now
                has_selling_climax = last_selling_climax_bar >= peak_bar_idx

            is_crash_candidate = is_velocity_crash and has_selling_climax

            # Prior run verification
            if cfg.require_prior_run and is_crash_candidate:
                run_start = max(0, peak_bar_idx - cfg.prior_run_lookback)
                run_end = peak_bar_idx + 1
                if run_start < run_end and run_end <= len(lows):
                    prior_run_low = min(lows[run_start:run_end])
                    prior_run_gain = ((peak_high - prior_run_low) / prior_run_low) * 100 if prior_run_low > 0 else 0.0
                    had_prior_run = prior_run_gain >= cfg.prior_run_min_pct
                else:
                    had_prior_run = False

        washout_detected = liquidity_ok and is_crash_candidate and had_prior_run

        # ═══════════════════════════════════════════════════════════
        # SHORT SETUP STATE MACHINE
        # ═══════════════════════════════════════════════════════════
        short_setup_triggered = False

        # ACTIVATION
        if (cfg.enable_short and parabolic_advance_detected and climax_vol_ok
                and not short_setup_active and (i - last_short_bar > cfg.cooldown_bars)):
            short_setup_active = True
            short_setup_bar = i
            parabolic_peak = bar.high
            parabolic_peak_bar = i
            # AVWAP anchor
            advance_start_low = recent_low
            advance_start_bar = i - cfg.gain_lookback + 1 + (window_lows.index(recent_low) if recent_low in window_lows else 0)
            # Initialize Run AVWAP accumulators
            short_avwap_num = 0.0
            short_avwap_den = 0.0
            short_run_avwap = 0.0

        # PEAK TRACKING
        if short_setup_active and bar.high > parabolic_peak:
            parabolic_peak = bar.high
            parabolic_peak_bar = i

        # Run AVWAP accumulation (short side)
        if short_setup_active:
            src = bar.ohlc4
            short_avwap_num += src * bar.volume
            short_avwap_den += bar.volume
            short_run_avwap = short_avwap_num / short_avwap_den if short_avwap_den > 0 else 0.0

        # TRIGGER
        if short_setup_active and (i - short_setup_bar) >= cfg.min_bars_after_setup:
            short_entry_proxy = False
            if cfg.short_trigger == "First Red Day":
                short_entry_proxy = bar.close < bar.open and i > 0 and bar.close < bars[i - 1].close
            elif cfg.short_trigger == "Close < Prior Low":
                short_entry_proxy = i > 0 and bar.close < bars[i - 1].low
            elif cfg.short_trigger == "Close < Run AVWAP":
                short_entry_proxy = short_run_avwap > 0 and bar.close < short_run_avwap
            elif cfg.short_trigger == "Any Reversal":
                short_entry_proxy = (
                    (i > 0 and bar.close < bars[i - 1].low) or
                    (bar.close < bar.open and i > 0 and bar.close < bars[i - 1].close) or
                    (short_run_avwap > 0 and bar.close < short_run_avwap)
                )
            # Close strength
            bar_rng = max(bar.high - bar.low, 0.0001)
            short_close_str = (bar.high - bar.close) / bar_rng
            short_close_ok = short_close_str >= cfg.min_close_strength

            # Stop price
            if cfg.short_stop_mode == "Run Peak":
                short_stop = parabolic_peak * (1 + cfg.stop_buffer / 100)
            elif cfg.short_stop_mode == "Trigger Bar High":
                short_stop = bar.high * (1 + cfg.stop_buffer / 100)
            else:  # ATR Based
                short_stop = bar.close + (atr_calc(bars[:i + 1], cfg.atr_len) * cfg.atr_mult)

            short_stop_width = abs(short_stop - bar.close) / bar.close * 100 if bar.close > 0 else 0.0
            short_adr_ok = True
            # ADR filter is redundant when ATR-based (ATR mult IS the risk control)
            if cfg.use_adr_filter and adr_pct > 0 and cfg.short_stop_mode != "ATR Based":
                short_adr_ok = short_stop_width <= (adr_pct * cfg.max_stop_vs_adr)

            if short_entry_proxy and short_close_ok and short_adr_ok:
                short_setup_triggered = True

                # Targets
                tf = sma(closes, cfg.target_ma_fast)
                ts = sma(closes, cfg.target_ma_slow)
                risk_pct = ((short_stop - bar.close) / bar.close) * 100 if bar.close > 0 else 0.0

                trade = Trade(
                    ticker=ticker,
                    direction="SHORT",
                    entry_bar=i,
                    entry_date=bar.date,
                    entry_price=bar.close,
                    stop_price=short_stop,
                    target_fast=tf,
                    target_slow=ts,
                    extension_pct=extension_pct,
                    rolling_gain_pct=rolling_gain_pct,
                    green_streak=green_streak,
                    risk_pct=risk_pct,
                )
                trades.append(trade)
                open_trades.append(trade)

                # Reset
                short_setup_active = False
                last_short_bar = i
                short_avwap_num = 0.0
                short_avwap_den = 0.0
                short_run_avwap = 0.0

        # TIMEOUT
        if short_setup_active and (i - short_setup_bar > cfg.short_setup_timeout):
            short_setup_active = False
            short_avwap_num = 0.0
            short_avwap_den = 0.0
            short_run_avwap = 0.0

        # ═══════════════════════════════════════════════════════════
        # LONG SETUP STATE MACHINE
        # ═══════════════════════════════════════════════════════════
        long_setup_triggered = False

        # ACTIVATION
        if (cfg.enable_long and washout_detected and not long_setup_active
                and (i - last_long_bar > cfg.cooldown_bars)):
            long_setup_active = True
            long_setup_bar = i
            washout_low = bar.low
            washout_low_bar = i
            # Initialize long-side Run AVWAP from peak
            long_avwap_num = 0.0
            long_avwap_den = 0.0
            long_run_avwap = 0.0

        # LOW TRACKING
        if long_setup_active and bar.low < washout_low:
            washout_low = bar.low
            washout_low_bar = i

        # Run AVWAP accumulation (long side, anchored from peak)
        if long_setup_active:
            src = bar.ohlc4
            long_avwap_num += src * bar.volume
            long_avwap_den += bar.volume
            long_run_avwap = long_avwap_num / long_avwap_den if long_avwap_den > 0 else 0.0

        # TRIGGER
        if long_setup_active and (i - long_setup_bar) >= cfg.min_bars_after_setup:
            long_entry_proxy = False
            if cfg.long_trigger == "First Green Day":
                long_entry_proxy = bar.close > bar.open and i > 0 and bar.close > bars[i - 1].close
            elif cfg.long_trigger == "Close > Prior High":
                long_entry_proxy = i > 0 and bar.close > bars[i - 1].high
            elif cfg.long_trigger == "Close > Run AVWAP":
                long_entry_proxy = long_run_avwap > 0 and bar.close > long_run_avwap
            elif cfg.long_trigger == "Any Reversal":
                long_entry_proxy = (
                    (i > 0 and bar.close > bars[i - 1].high) or
                    (bar.close > bar.open and i > 0 and bar.close > bars[i - 1].close) or
                    (long_run_avwap > 0 and bar.close > long_run_avwap)
                )

            bar_rng = max(bar.high - bar.low, 0.0001)
            long_close_str = (bar.close - bar.low) / bar_rng
            long_close_ok = long_close_str >= cfg.min_close_strength

            # Stop price
            if cfg.long_stop_mode == "Washout Low":
                long_stop = washout_low * (1 - cfg.stop_buffer / 100)
            else:  # ATR Based
                long_stop = bar.close - (atr_calc(bars[:i + 1], cfg.atr_len) * cfg.atr_mult)

            long_stop_width = abs(bar.close - long_stop) / bar.close * 100 if bar.close > 0 else 0.0
            long_adr_ok = True
            # ADR filter is redundant when ATR-based (ATR mult IS the risk control)
            if cfg.use_adr_filter and adr_pct > 0 and cfg.long_stop_mode != "ATR Based":
                long_adr_ok = long_stop_width <= (adr_pct * cfg.max_stop_vs_adr)

            if long_entry_proxy and long_close_ok and long_adr_ok:
                long_setup_triggered = True

                tf = sma(closes, cfg.target_ma_fast)
                ts = sma(closes, cfg.target_ma_slow)
                risk_pct = ((bar.close - long_stop) / bar.close) * 100 if bar.close > 0 else 0.0

                trade = Trade(
                    ticker=ticker,
                    direction="LONG",
                    entry_bar=i,
                    entry_date=bar.date,
                    entry_price=bar.close,
                    stop_price=long_stop,
                    target_fast=tf,
                    target_slow=ts,
                    crash_from_peak=crash_from_peak,
                    risk_pct=risk_pct,
                )
                trades.append(trade)
                open_trades.append(trade)

                # Reset
                long_setup_active = False
                last_long_bar = i
                long_avwap_num = 0.0
                long_avwap_den = 0.0
                long_run_avwap = 0.0

        # TIMEOUT
        if long_setup_active and (i - long_setup_bar > cfg.long_setup_timeout):
            long_setup_active = False
            long_avwap_num = 0.0
            long_avwap_den = 0.0
            long_run_avwap = 0.0

    # Close any remaining open trades at last bar
    if open_trades:
        last_bar = bars[-1]
        for t in open_trades:
            if t.exit_bar < 0:
                t.exit_bar = n - 1
                t.exit_date = last_bar.date
                t.exit_price = last_bar.close
                t.exit_reason = "OPEN_AT_END"
                t.bars_held = n - 1 - t.entry_bar
                if t.direction == "SHORT":
                    t.pnl_pct = ((t.entry_price - t.exit_price) / t.entry_price) * 100
                else:
                    t.pnl_pct = ((t.exit_price - t.entry_price) / t.entry_price) * 100
                if t.risk_pct > 0:
                    t.r_multiple = t.pnl_pct / t.risk_pct

    return trades


# ═══════════════════════════════════════════════════════════════════
# REPORTING
# ═══════════════════════════════════════════════════════════════════

def print_ticker_summary(ticker: str, trades: list):
    """Print summary for a single ticker (weight-aware for split exits)."""
    if not trades:
        return

    shorts = [t for t in trades if t.direction == "SHORT" and not t.is_runner]
    longs = [t for t in trades if t.direction == "LONG" and not t.is_runner]
    closed = [t for t in trades if t.exit_reason not in ("OPEN_AT_END", "")]

    wins = [t for t in closed if t.pnl_pct > 0]
    losses = [t for t in closed if t.pnl_pct <= 0]
    total_weight = sum(t.weight for t in closed)
    win_weight = sum(t.weight for t in wins)
    win_rate = win_weight / total_weight * 100 if total_weight > 0 else 0

    total_pnl = sum(t.pnl_pct * t.weight for t in closed)
    avg_pnl = total_pnl / total_weight if total_weight > 0 else 0
    win_wt = sum(t.weight for t in wins)
    loss_wt = sum(t.weight for t in losses)
    avg_win = sum(t.pnl_pct * t.weight for t in wins) / win_wt if win_wt > 0 else 0
    avg_loss = sum(t.pnl_pct * t.weight for t in losses) / loss_wt if loss_wt > 0 else 0
    avg_bars = sum(t.bars_held for t in closed) / len(closed) if closed else 0

    # Count original setups (non-runners) for Short/Long display
    print(f"  {ticker:<8} | Total: {len(shorts)+len(longs):>3} | Short: {len(shorts):>3} | Long: {len(longs):>3} | "
          f"Legs: {len(closed):>3} | Win%: {win_rate:>6.1f}% | "
          f"Avg PnL: {avg_pnl:>+7.2f}% | Avg Win: {avg_win:>+7.2f}% | Avg Loss: {avg_loss:>+7.2f}% | "
          f"Avg Bars: {avg_bars:>5.1f}")


def _weighted_avg(trades, attr='pnl_pct'):
    """Weighted average of a trade attribute by position weight."""
    total_wt = sum(t.weight for t in trades)
    if total_wt <= 0:
        return 0.0
    return sum(getattr(t, attr) * t.weight for t in trades) / total_wt


def print_grand_summary(all_trades: list):
    """Print comprehensive cross-ticker summary (weight-aware for split exits)."""
    closed = [t for t in all_trades if t.exit_reason not in ("OPEN_AT_END", "")]
    if not closed:
        print("\n  No closed trades across all tickers.")
        return

    # Original setups (non-runners) for setup counts
    setups = [t for t in all_trades if not t.is_runner]
    n_setups = len(setups)

    shorts = [t for t in closed if t.direction == "SHORT"]
    longs = [t for t in closed if t.direction == "LONG"]

    wins = [t for t in closed if t.pnl_pct > 0]
    losses = [t for t in closed if t.pnl_pct <= 0]

    # Weight-aware metrics
    total_weight = sum(t.weight for t in closed)
    win_weight = sum(t.weight for t in wins)
    loss_weight = sum(t.weight for t in losses)

    total_pnl = sum(t.pnl_pct * t.weight for t in closed)
    avg_pnl = total_pnl / total_weight if total_weight > 0 else 0
    avg_win = _weighted_avg(wins)
    avg_loss = _weighted_avg(losses)
    win_rate = win_weight / total_weight * 100 if total_weight > 0 else 0

    # By direction (weighted)
    short_wins = [t for t in shorts if t.pnl_pct > 0]
    short_losses = [t for t in shorts if t.pnl_pct <= 0]
    long_wins = [t for t in longs if t.pnl_pct > 0]
    long_losses = [t for t in longs if t.pnl_pct <= 0]

    short_wt = sum(t.weight for t in shorts)
    long_wt = sum(t.weight for t in longs)
    short_win_wt = sum(t.weight for t in short_wins)
    long_win_wt = sum(t.weight for t in long_wins)
    short_wr = short_win_wt / short_wt * 100 if short_wt > 0 else 0
    long_wr = long_win_wt / long_wt * 100 if long_wt > 0 else 0
    short_avg = _weighted_avg(shorts)
    long_avg = _weighted_avg(longs)

    # By exit reason
    stops = [t for t in closed if t.exit_reason == "STOP"]
    t10 = [t for t in closed if t.exit_reason == "TARGET_10MA"]
    t10p = [t for t in closed if t.exit_reason == "TARGET_10MA_PARTIAL"]
    t20 = [t for t in closed if t.exit_reason == "TARGET_20MA"]
    sbe = [t for t in closed if t.exit_reason == "STOP_BREAKEVEN"]
    timeouts = [t for t in closed if t.exit_reason == "TIMEOUT"]

    # Profit factor (weighted)
    gross_profit = sum(t.pnl_pct * t.weight for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl_pct * t.weight for t in losses)) if losses else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    # Max drawdown (sequential weighted PnL)
    cumulative = 0.0
    peak_cum = 0.0
    max_dd = 0.0
    for t in sorted(closed, key=lambda x: (x.exit_bar, -x.is_runner)):
        cumulative += t.pnl_pct * t.weight
        if cumulative > peak_cum:
            peak_cum = cumulative
        dd = peak_cum - cumulative
        if dd > max_dd:
            max_dd = dd

    # Average R-multiple (weighted)
    r_trades = [t for t in closed if t.risk_pct > 0]
    r_wt = sum(t.weight for t in r_trades)
    avg_r = sum(t.r_multiple * t.weight for t in r_trades) / r_wt if r_wt > 0 else 0

    # Best and worst trades (by weighted PnL)
    best = max(closed, key=lambda t: t.pnl_pct * t.weight)
    worst = min(closed, key=lambda t: t.pnl_pct * t.weight)

    # Avg holding period
    avg_bars = sum(t.bars_held for t in closed) / len(closed)

    # Tickers with setups
    tickers_with_trades = len(set(t.ticker for t in closed))

    print("\n" + "=" * 100)
    print("                    GRAND BACKTEST SUMMARY — Parabolic Mean Reversion V1")
    print("=" * 100)

    print(f"\n  Original Setups:         {n_setups}")
    print(f"  Total Trade Legs:        {len(closed)}  (includes runner legs from split exits)")
    print(f"  Tickers With Setups:     {tickers_with_trades}")
    print(f"  Open at End (excluded):  {len(all_trades) - len(closed)}")

    print(f"\n  ── Overall Performance (weighted) ──")
    print(f"  Win Rate:                {win_rate:.1f}% ({win_weight:.1f}W / {loss_weight:.1f}L weighted)")
    print(f"  Avg PnL per Trade:       {avg_pnl:+.2f}%")
    print(f"  Avg Winner:              {avg_win:+.2f}%")
    print(f"  Avg Loser:               {avg_loss:+.2f}%")
    print(f"  Profit Factor:           {profit_factor:.2f}")
    print(f"  Avg R-Multiple:          {avg_r:+.2f}R")
    print(f"  Cumulative PnL:          {total_pnl:+.2f}%")
    print(f"  Max Drawdown (seq):      {max_dd:.2f}%")
    print(f"  Avg Holding Period:      {avg_bars:.1f} bars")

    print(f"\n  ── By Direction (weighted) ──")
    short_setups = len([t for t in shorts if not t.is_runner])
    long_setups = len([t for t in longs if not t.is_runner])
    print(f"  SHORT:  {short_setups:>4} setups ({len(shorts)} legs) | Win Rate: {short_wr:>5.1f}% | Avg PnL: {short_avg:>+7.2f}%")
    if shorts:
        print(f"           Avg Win: {_weighted_avg(short_wins):>+7.2f}% | Avg Loss: {_weighted_avg(short_losses):>+7.2f}%")
    print(f"  LONG:   {long_setups:>4} setups ({len(longs)} legs) | Win Rate: {long_wr:>5.1f}% | Avg PnL: {long_avg:>+7.2f}%")
    if longs:
        print(f"           Avg Win: {_weighted_avg(long_wins):>+7.2f}% | Avg Loss: {_weighted_avg(long_losses):>+7.2f}%")

    print(f"\n  ── By Exit Reason ──")
    n = len(closed)
    def _reason_line(label, bucket):
        pct = len(bucket) / n * 100 if n else 0
        avg = _weighted_avg(bucket) if bucket else 0
        wt = sum(t.weight for t in bucket)
        print(f"  {label:<19} {len(bucket):>4}  ({pct:>5.1f}%)  Wt: {wt:>5.1f}  Avg PnL: {avg:>+7.2f}%")
    _reason_line("STOP:", stops)
    _reason_line("TARGET_10MA:", t10)
    _reason_line("TARGET_10MA_PARTIAL:", t10p)
    _reason_line("TARGET_20MA:", t20)
    _reason_line("STOP_BREAKEVEN:", sbe)
    _reason_line("TIMEOUT:", timeouts)

    print(f"\n  ── Extremes ──")
    print(f"  Best Trade:   {best.ticker} {best.direction} {best.entry_date} → {best.exit_date}  PnL: {best.pnl_pct:+.2f}% x{best.weight:.0%}  ({best.exit_reason})")
    print(f"  Worst Trade:  {worst.ticker} {worst.direction} {worst.entry_date} → {worst.exit_date}  PnL: {worst.pnl_pct:+.2f}% x{worst.weight:.0%}  ({worst.exit_reason})")

    # Top 10 trades
    sorted_trades = sorted(closed, key=lambda t: t.pnl_pct * t.weight, reverse=True)
    print(f"\n  ── Top 10 Trades ──")
    print(f"  {'Ticker':<8} {'Dir':<6} {'Entry Date':<12} {'Exit Date':<12} {'Entry':>10} {'Exit':>10} {'PnL%':>8} {'Wt':>4} {'R':>6} {'Exit Reason':<20} {'Bars':>5}")
    print(f"  {'-'*8} {'-'*6} {'-'*12} {'-'*12} {'-'*10} {'-'*10} {'-'*8} {'-'*4} {'-'*6} {'-'*20} {'-'*5}")
    for t in sorted_trades[:10]:
        print(f"  {t.ticker:<8} {t.direction:<6} {t.entry_date:<12} {t.exit_date:<12} "
              f"{t.entry_price:>10.2f} {t.exit_price:>10.2f} {t.pnl_pct:>+7.2f}% {t.weight:>3.0%} {t.r_multiple:>+5.2f}R "
              f"{t.exit_reason:<20} {t.bars_held:>5}")

    # Bottom 10 trades
    print(f"\n  ── Bottom 10 Trades ──")
    print(f"  {'Ticker':<8} {'Dir':<6} {'Entry Date':<12} {'Exit Date':<12} {'Entry':>10} {'Exit':>10} {'PnL%':>8} {'Wt':>4} {'R':>6} {'Exit Reason':<20} {'Bars':>5}")
    print(f"  {'-'*8} {'-'*6} {'-'*12} {'-'*12} {'-'*10} {'-'*10} {'-'*8} {'-'*4} {'-'*6} {'-'*20} {'-'*5}")
    for t in sorted_trades[-10:]:
        print(f"  {t.ticker:<8} {t.direction:<6} {t.entry_date:<12} {t.exit_date:<12} "
              f"{t.entry_price:>10.2f} {t.exit_price:>10.2f} {t.pnl_pct:>+7.2f}% {t.weight:>3.0%} {t.r_multiple:>+5.2f}R "
              f"{t.exit_reason:<20} {t.bars_held:>5}")

    # Per-ticker breakdown (weighted)
    print(f"\n  ── Per-Ticker Breakdown (weighted PnL) ──")
    print(f"  {'Ticker':<8} {'Setups':>7} {'Legs':>6} {'Short':>6} {'Long':>6} {'Win%':>7} {'Avg PnL':>9} {'Total PnL':>10} {'Best':>8} {'Worst':>8}")
    print(f"  {'-'*8} {'-'*7} {'-'*6} {'-'*6} {'-'*6} {'-'*7} {'-'*9} {'-'*10} {'-'*8} {'-'*8}")

    tickers = sorted(set(t.ticker for t in closed))
    for tkr in tickers:
        tkr_trades = [t for t in closed if t.ticker == tkr]
        tkr_setups = [t for t in tkr_trades if not t.is_runner]
        tkr_shorts = [t for t in tkr_setups if t.direction == "SHORT"]
        tkr_longs = [t for t in tkr_setups if t.direction == "LONG"]
        tkr_wins = [t for t in tkr_trades if t.pnl_pct > 0]
        tkr_wt = sum(t.weight for t in tkr_trades)
        tkr_win_wt = sum(t.weight for t in tkr_wins)
        tkr_wr = tkr_win_wt / tkr_wt * 100 if tkr_wt > 0 else 0
        tkr_avg = _weighted_avg(tkr_trades)
        tkr_total = sum(t.pnl_pct * t.weight for t in tkr_trades)
        tkr_best = max(t.pnl_pct * t.weight for t in tkr_trades) if tkr_trades else 0
        tkr_worst = min(t.pnl_pct * t.weight for t in tkr_trades) if tkr_trades else 0
        print(f"  {tkr:<8} {len(tkr_setups):>7} {len(tkr_trades):>6} {len(tkr_shorts):>6} {len(tkr_longs):>6} "
              f"{tkr_wr:>6.1f}% {tkr_avg:>+8.2f}% {tkr_total:>+9.2f}% {tkr_best:>+7.2f}% {tkr_worst:>+7.2f}%")

    # All closed trades log
    print(f"\n  ── Complete Trade Log ({len(closed)} legs from {n_setups} setups) ──")
    print(f"  {'#':>4} {'Ticker':<8} {'Dir':<6} {'Entry Date':<12} {'Exit Date':<12} {'Entry':>10} {'Stop':>10} {'Exit':>10} {'PnL%':>8} {'Wt':>4} {'R':>6} {'Reason':<20} {'Bars':>5} {'Ext%':>7} {'Gain%':>7}")
    print(f"  {'-'*4} {'-'*8} {'-'*6} {'-'*12} {'-'*12} {'-'*10} {'-'*10} {'-'*10} {'-'*8} {'-'*4} {'-'*6} {'-'*20} {'-'*5} {'-'*7} {'-'*7}")
    for idx, t in enumerate(sorted(closed, key=lambda x: (x.ticker, x.entry_bar, x.is_runner)), 1):
        print(f"  {idx:>4} {t.ticker:<8} {t.direction:<6} {t.entry_date:<12} {t.exit_date:<12} "
              f"{t.entry_price:>10.2f} {t.stop_price:>10.2f} {t.exit_price:>10.2f} "
              f"{t.pnl_pct:>+7.2f}% {t.weight:>3.0%} {t.r_multiple:>+5.2f}R {t.exit_reason:<20} {t.bars_held:>5} "
              f"{t.extension_pct:>+6.1f}% {t.rolling_gain_pct:>+6.1f}%")

    print("\n" + "=" * 100)


# ═══════════════════════════════════════════════════════════════════
# PARAMETER SWEEP
# ═══════════════════════════════════════════════════════════════════

def run_sweep(csv_files: list):
    """Grid search across ATR mult, crash velocity, selling climax RVOL."""
    from dataclasses import replace
    import itertools

    base_cfg = Config()

    # Parameter grids
    atr_mults = [1.0, 1.5, 2.0, 2.5, 3.0]
    crash_velocities = [0.0, 3.0, 5.0, 7.0, 10.0]  # 0 = disabled (original behavior)
    selling_climax_rvols = [0.0, 1.5, 2.0, 2.5, 3.0]  # 0 = disabled
    directions = ["BOTH", "SHORT", "LONG"]

    # Preload all bar data
    ticker_bars = []
    for csv_file in csv_files:
        ticker = extract_ticker(csv_file)
        bars = load_csv(csv_file)
        ticker_bars.append((ticker, bars))

    results = []
    total_combos = len(atr_mults) * len(crash_velocities) * len(selling_climax_rvols) * len(directions)
    combo_num = 0

    print(f"\n  Running parameter sweep: {total_combos} combinations...")
    print(f"  ATR mults: {atr_mults}")
    print(f"  Crash velocities: {crash_velocities}")
    print(f"  Selling climax RVOLs: {selling_climax_rvols}")
    print(f"  Directions: {directions}\n")

    header = (f"  {'#':>4} {'Dir':<6} {'ATR_M':>5} {'Vel':>5} {'SC_RV':>5} "
              f"{'Setups':>6} {'S':>4} {'L':>4} {'WR%':>6} {'AvgPnL':>8} "
              f"{'PF':>6} {'CumPnL':>9} {'AvgWin':>8} {'AvgLoss':>8} {'MaxDD':>7}")
    print(header)
    print(f"  {'-' * len(header)}")

    for atr_m, vel, sc_rv, direction in itertools.product(atr_mults, crash_velocities, selling_climax_rvols, directions):
        combo_num += 1
        cfg = replace(base_cfg,
                      atr_mult=atr_m,
                      crash_velocity_min=vel if vel > 0 else 0.0,
                      require_selling_climax=sc_rv > 0,
                      selling_climax_rvol=sc_rv if sc_rv > 0 else 2.0,
                      enable_short=direction in ("BOTH", "SHORT"),
                      enable_long=direction in ("BOTH", "LONG"),
                      )

        all_trades = []
        for ticker, bars in ticker_bars:
            trades = run_backtest(ticker, bars, cfg)
            all_trades.extend(trades)

        closed = [t for t in all_trades if t.exit_reason not in ("OPEN_AT_END", "")]
        if not closed:
            continue

        setups = [t for t in all_trades if not t.is_runner]
        shorts_n = len([t for t in setups if t.direction == "SHORT"])
        longs_n = len([t for t in setups if t.direction == "LONG"])

        wins = [t for t in closed if t.pnl_pct > 0]
        losses = [t for t in closed if t.pnl_pct <= 0]
        total_weight = sum(t.weight for t in closed)
        win_weight = sum(t.weight for t in wins)
        wr = win_weight / total_weight * 100 if total_weight > 0 else 0
        total_pnl = sum(t.pnl_pct * t.weight for t in closed)
        avg_pnl = total_pnl / total_weight if total_weight > 0 else 0
        gross_profit = sum(t.pnl_pct * t.weight for t in wins)
        gross_loss = abs(sum(t.pnl_pct * t.weight for t in losses))
        pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        avg_win = _weighted_avg(wins) if wins else 0
        avg_loss = _weighted_avg(losses) if losses else 0

        # Max drawdown
        cumulative = 0.0
        peak_cum = 0.0
        max_dd = 0.0
        for t in sorted(closed, key=lambda x: (x.exit_bar, -x.is_runner)):
            cumulative += t.pnl_pct * t.weight
            if cumulative > peak_cum:
                peak_cum = cumulative
            dd = peak_cum - cumulative
            if dd > max_dd:
                max_dd = dd

        vel_str = f"{vel:.0f}" if vel > 0 else "off"
        sc_str = f"{sc_rv:.1f}" if sc_rv > 0 else "off"

        print(f"  {combo_num:>4} {direction:<6} {atr_m:>5.1f} {vel_str:>5} {sc_str:>5} "
              f"{len(setups):>6} {shorts_n:>4} {longs_n:>4} {wr:>5.1f}% {avg_pnl:>+7.2f}% "
              f"{pf:>5.2f} {total_pnl:>+8.1f}% {avg_win:>+7.2f}% {avg_loss:>+7.2f}% {max_dd:>6.1f}%")

        results.append({
            'direction': direction, 'atr_mult': atr_m,
            'crash_velocity': vel, 'selling_climax_rvol': sc_rv,
            'setups': len(setups), 'shorts': shorts_n, 'longs': longs_n,
            'wr': wr, 'avg_pnl': avg_pnl, 'pf': pf,
            'cum_pnl': total_pnl, 'avg_win': avg_win, 'avg_loss': avg_loss,
            'max_dd': max_dd,
        })

    # Print top 20 by profit factor (min 10 setups)
    viable = [r for r in results if r['setups'] >= 10]
    top_pf = sorted(viable, key=lambda r: r['pf'], reverse=True)[:20]

    print(f"\n  ═══ TOP 20 BY PROFIT FACTOR (min 10 setups) ═══")
    print(f"  {'#':>3} {'Dir':<6} {'ATR_M':>5} {'Vel':>5} {'SC_RV':>5} "
          f"{'Setups':>6} {'S':>4} {'L':>4} {'WR%':>6} {'AvgPnL':>8} "
          f"{'PF':>6} {'CumPnL':>9} {'MaxDD':>7}")
    for idx, r in enumerate(top_pf, 1):
        vel_str = f"{r['crash_velocity']:.0f}" if r['crash_velocity'] > 0 else "off"
        sc_str = f"{r['selling_climax_rvol']:.1f}" if r['selling_climax_rvol'] > 0 else "off"
        print(f"  {idx:>3} {r['direction']:<6} {r['atr_mult']:>5.1f} {vel_str:>5} {sc_str:>5} "
              f"{r['setups']:>6} {r['shorts']:>4} {r['longs']:>4} {r['wr']:>5.1f}% {r['avg_pnl']:>+7.2f}% "
              f"{r['pf']:>5.2f} {r['cum_pnl']:>+8.1f}% {r['max_dd']:>6.1f}%")

    # Top 20 by avg PnL
    top_avg = sorted(viable, key=lambda r: r['avg_pnl'], reverse=True)[:20]
    print(f"\n  ═══ TOP 20 BY AVG PNL (min 10 setups) ═══")
    print(f"  {'#':>3} {'Dir':<6} {'ATR_M':>5} {'Vel':>5} {'SC_RV':>5} "
          f"{'Setups':>6} {'S':>4} {'L':>4} {'WR%':>6} {'AvgPnL':>8} "
          f"{'PF':>6} {'CumPnL':>9} {'MaxDD':>7}")
    for idx, r in enumerate(top_avg, 1):
        vel_str = f"{r['crash_velocity']:.0f}" if r['crash_velocity'] > 0 else "off"
        sc_str = f"{r['selling_climax_rvol']:.1f}" if r['selling_climax_rvol'] > 0 else "off"
        print(f"  {idx:>3} {r['direction']:<6} {r['atr_mult']:>5.1f} {vel_str:>5} {sc_str:>5} "
              f"{r['setups']:>6} {r['shorts']:>4} {r['longs']:>4} {r['wr']:>5.1f}% {r['avg_pnl']:>+7.2f}% "
              f"{r['pf']:>5.2f} {r['cum_pnl']:>+8.1f}% {r['max_dd']:>6.1f}%")

    return results


# ═══════════════════════════════════════════════════════════════════
# SHORT STOP MODE SWEEP
# ═══════════════════════════════════════════════════════════════════

def run_short_stop_sweep(csv_files: list):
    """Sweep SHORT stop modes (Run Peak, Trigger Bar High, ATR Based) with multipliers."""
    from dataclasses import replace

    base_cfg = Config()

    # Preload all bar data
    ticker_bars = []
    for csv_file in csv_files:
        ticker = extract_ticker(csv_file)
        bars = load_csv(csv_file)
        ticker_bars.append((ticker, bars))

    stop_modes = ["Run Peak", "Trigger Bar High", "ATR Based"]
    atr_mults = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]  # only relevant for ATR Based
    results = []

    print(f"\n  ═══ SHORT STOP MODE SWEEP ═══")
    print(f"  LONG side: frozen at ATR 2.5x, vel 3.0, SC 3.0 (best config)")
    print(f"  Testing SHORT stop modes: {stop_modes}")
    print(f"  ATR multipliers (for ATR Based): {atr_mults}\n")

    header = (f"  {'#':>3} {'StopMode':<18} {'ATR_M':>5} "
              f"{'Setups':>6} {'S':>4} {'L':>4} {'WR%':>6} {'AvgPnL':>8} "
              f"{'PF':>6} {'CumPnL':>9} {'AvgWin':>8} {'AvgLoss':>8} {'MaxDD':>7} "
              f"{'S_WR%':>6} {'S_AvgPnL':>9} {'S_PF':>6}")
    print(header)

    combo_num = 0
    for stop_mode in stop_modes:
        mults = atr_mults if stop_mode == "ATR Based" else [0.0]
        for atr_m in mults:
            combo_num += 1
            # Freeze LONG at best config; only vary SHORT stop
            cfg = replace(base_cfg,
                          short_stop_mode=stop_mode,
                          atr_mult=atr_m if stop_mode == "ATR Based" else 2.5,
                          # LONG frozen at best V4 config
                          long_stop_mode="ATR Based",
                          crash_velocity_min=3.0,
                          require_selling_climax=True,
                          selling_climax_rvol=3.0,
                          )
            # For non-ATR short modes, LONG still needs its own ATR mult
            if stop_mode != "ATR Based":
                cfg = replace(cfg, atr_mult=2.5)

            all_trades = []
            for ticker, bars in ticker_bars:
                trades = run_backtest(ticker, bars, cfg)
                all_trades.extend(trades)

            closed = [t for t in all_trades if t.exit_reason not in ("OPEN_AT_END", "")]
            if not closed:
                continue

            setups = [t for t in all_trades if not t.is_runner]
            shorts_n = len([t for t in setups if t.direction == "SHORT"])
            longs_n = len([t for t in setups if t.direction == "LONG"])

            wins = [t for t in closed if t.pnl_pct > 0]
            losses = [t for t in closed if t.pnl_pct <= 0]
            total_weight = sum(t.weight for t in closed)
            win_weight = sum(t.weight for t in wins)
            wr = win_weight / total_weight * 100 if total_weight > 0 else 0
            total_pnl = sum(t.pnl_pct * t.weight for t in closed)
            avg_pnl = total_pnl / total_weight if total_weight > 0 else 0
            gross_profit = sum(t.pnl_pct * t.weight for t in wins)
            gross_loss = abs(sum(t.pnl_pct * t.weight for t in losses))
            pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')
            avg_win = _weighted_avg(wins) if wins else 0
            avg_loss = _weighted_avg(losses) if losses else 0

            # Max drawdown
            cumulative = 0.0
            peak_cum = 0.0
            max_dd = 0.0
            for t in sorted(closed, key=lambda x: (x.exit_bar, -x.is_runner)):
                cumulative += t.pnl_pct * t.weight
                if cumulative > peak_cum:
                    peak_cum = cumulative
                dd = peak_cum - cumulative
                if dd > max_dd:
                    max_dd = dd

            # SHORT-only metrics
            s_closed = [t for t in closed if t.direction == "SHORT"]
            s_wins = [t for t in s_closed if t.pnl_pct > 0]
            s_losses = [t for t in s_closed if t.pnl_pct <= 0]
            s_wt = sum(t.weight for t in s_closed)
            s_win_wt = sum(t.weight for t in s_wins)
            s_wr = s_win_wt / s_wt * 100 if s_wt > 0 else 0
            s_avg = _weighted_avg(s_closed) if s_closed else 0
            s_gp = sum(t.pnl_pct * t.weight for t in s_wins)
            s_gl = abs(sum(t.pnl_pct * t.weight for t in s_losses))
            s_pf = s_gp / s_gl if s_gl > 0 else float('inf')

            atr_str = f"{atr_m:.1f}" if stop_mode == "ATR Based" else "n/a"
            print(f"  {combo_num:>3} {stop_mode:<18} {atr_str:>5} "
                  f"{len(setups):>6} {shorts_n:>4} {longs_n:>4} {wr:>5.1f}% {avg_pnl:>+7.2f}% "
                  f"{pf:>5.2f} {total_pnl:>+8.1f}% {avg_win:>+7.2f}% {avg_loss:>+7.2f}% {max_dd:>6.1f}% "
                  f"{s_wr:>5.1f}% {s_avg:>+8.2f}% {s_pf:>5.2f}")

            results.append({
                'stop_mode': stop_mode, 'atr_mult': atr_m,
                'setups': len(setups), 'shorts': shorts_n, 'longs': longs_n,
                'wr': wr, 'avg_pnl': avg_pnl, 'pf': pf,
                'cum_pnl': total_pnl, 'max_dd': max_dd,
                's_wr': s_wr, 's_avg_pnl': s_avg, 's_pf': s_pf,
            })

    # Best SHORT-only PF
    print(f"\n  ═══ RANKED BY SHORT PROFIT FACTOR ═══")
    ranked = sorted(results, key=lambda r: r['s_pf'], reverse=True)
    for idx, r in enumerate(ranked, 1):
        atr_str = f"{r['atr_mult']:.1f}" if r['stop_mode'] == "ATR Based" else "n/a"
        print(f"  {idx:>3} {r['stop_mode']:<18} ATR={atr_str:>4} | "
              f"S_PF: {r['s_pf']:>5.2f} | S_WR: {r['s_wr']:>5.1f}% | S_AvgPnL: {r['s_avg_pnl']:>+7.2f}% | "
              f"Combined PF: {r['pf']:>5.2f} | CumPnL: {r['cum_pnl']:>+8.1f}%")

    return results


# ═══════════════════════════════════════════════════════════════════
# WALK-FORWARD VALIDATION (Time-Split OOS)
# ═══════════════════════════════════════════════════════════════════

def _compute_slice_stats(trades):
    """Compute stats for a time slice of trades."""
    closed = [t for t in trades if t.exit_reason not in ("OPEN_AT_END", "")]
    if not closed:
        return None

    setups = [t for t in trades if not t.is_runner]
    wins = [t for t in closed if t.pnl_pct > 0]
    losses = [t for t in closed if t.pnl_pct <= 0]
    total_weight = sum(t.weight for t in closed)
    win_weight = sum(t.weight for t in wins)
    wr = win_weight / total_weight * 100 if total_weight > 0 else 0
    total_pnl = sum(t.pnl_pct * t.weight for t in closed)
    avg_pnl = total_pnl / total_weight if total_weight > 0 else 0
    gross_profit = sum(t.pnl_pct * t.weight for t in wins)
    gross_loss = abs(sum(t.pnl_pct * t.weight for t in losses))
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    # R-based metrics
    r_trades = [t for t in closed if t.risk_pct > 0]
    r_wt = sum(t.weight for t in r_trades)
    avg_r = sum(t.r_multiple * t.weight for t in r_trades) / r_wt if r_wt > 0 else 0
    expectancy_r = avg_r  # per-trade expectancy in R

    # Max drawdown in R terms
    cum_r = 0.0
    peak_r = 0.0
    max_dd_r = 0.0
    for t in sorted(r_trades, key=lambda x: (x.exit_bar, -x.is_runner)):
        cum_r += t.r_multiple * t.weight
        if cum_r > peak_r:
            peak_r = cum_r
        dd = peak_r - cum_r
        if dd > max_dd_r:
            max_dd_r = dd

    # Max drawdown in %
    cumulative = 0.0
    peak_cum = 0.0
    max_dd = 0.0
    for t in sorted(closed, key=lambda x: (x.exit_bar, -x.is_runner)):
        cumulative += t.pnl_pct * t.weight
        if cumulative > peak_cum:
            peak_cum = cumulative
        dd = peak_cum - cumulative
        if dd > max_dd:
            max_dd = dd

    shorts = [t for t in setups if t.direction == "SHORT"]
    longs = [t for t in setups if t.direction == "LONG"]

    return {
        'setups': len(setups), 'shorts': len(shorts), 'longs': len(longs),
        'legs': len(closed),
        'wr': wr, 'avg_pnl': avg_pnl, 'pf': pf,
        'cum_pnl': total_pnl, 'max_dd': max_dd,
        'avg_r': avg_r, 'expectancy_r': expectancy_r, 'max_dd_r': max_dd_r,
    }


def run_walk_forward(csv_files: list, n_slices: int = 4, cfg: 'Config' = None):
    """Time-split walk-forward validation. Splits each ticker's bars into N
    chronological slices and evaluates each independently."""
    from dataclasses import replace

    if cfg is None:
        cfg = Config()  # Use base Config defaults (V4 optimal)

    # Preload all bar data
    ticker_bars = []
    for csv_file in csv_files:
        ticker = extract_ticker(csv_file)
        bars = load_csv(csv_file)
        ticker_bars.append((ticker, bars))

    # Determine global time range
    all_timestamps = []
    for _, bars in ticker_bars:
        all_timestamps.extend(b.timestamp for b in bars)
    t_min = min(all_timestamps)
    t_max = max(all_timestamps)
    slice_size = (t_max - t_min) / n_slices
    slice_boundaries = [(t_min + int(i * slice_size), t_min + int((i + 1) * slice_size))
                        for i in range(n_slices)]

    print(f"\n  ═══ WALK-FORWARD VALIDATION ({n_slices} time slices) ═══")
    print(f"  Config: short_stop={cfg.short_stop_mode}, long_stop={cfg.long_stop_mode}, "
          f"atr_mult={cfg.atr_mult}, vel={cfg.crash_velocity_min}, sc_rvol={cfg.selling_climax_rvol}")

    from datetime import datetime, timezone as tz
    for i, (t_start, t_end) in enumerate(slice_boundaries):
        d_start = datetime.fromtimestamp(t_start, tz=tz.utc).strftime('%Y-%m-%d')
        d_end = datetime.fromtimestamp(t_end, tz=tz.utc).strftime('%Y-%m-%d')
        print(f"  Slice {i+1}: {d_start} → {d_end}")

    # Run backtest on full data, then split trades by exit timestamp into slices
    all_trades = []
    for ticker, bars in ticker_bars:
        trades = run_backtest(ticker, bars, cfg)
        # Attach exit timestamp for slicing
        for t in trades:
            if t.exit_bar >= 0 and t.exit_bar < len(bars):
                t._exit_ts = bars[t.exit_bar].timestamp
            else:
                t._exit_ts = bars[-1].timestamp
        all_trades.extend(trades)

    slice_results = []
    print(f"\n  {'Slice':>5} {'Period':<25} {'Setups':>6} {'S':>4} {'L':>4} {'WR%':>6} "
          f"{'AvgPnL':>8} {'PF':>6} {'CumPnL':>9} {'MaxDD':>7} "
          f"{'AvgR':>6} {'ExpR':>6} {'MaxDD_R':>7}")

    for i, (t_start, t_end) in enumerate(slice_boundaries):
        d_start = datetime.fromtimestamp(t_start, tz=tz.utc).strftime('%Y-%m-%d')
        d_end = datetime.fromtimestamp(t_end, tz=tz.utc).strftime('%Y-%m-%d')

        slice_trades = [t for t in all_trades if t_start <= t._exit_ts < t_end]
        stats = _compute_slice_stats(slice_trades)
        if stats is None:
            print(f"  {i+1:>5} {d_start} → {d_end:<14} {'(no trades)':>6}")
            slice_results.append(None)
            continue

        slice_results.append(stats)
        print(f"  {i+1:>5} {d_start} → {d_end:<14} {stats['setups']:>6} {stats['shorts']:>4} {stats['longs']:>4} "
              f"{stats['wr']:>5.1f}% {stats['avg_pnl']:>+7.2f}% {stats['pf']:>5.2f} "
              f"{stats['cum_pnl']:>+8.1f}% {stats['max_dd']:>6.1f}% "
              f"{stats['avg_r']:>+5.2f}R {stats['expectancy_r']:>+5.2f}R {stats['max_dd_r']:>6.2f}R")

    # Aggregate (full period)
    full_stats = _compute_slice_stats(all_trades)
    if full_stats:
        print(f"  {'FULL':>5} {'(all periods)':<25} {full_stats['setups']:>6} {full_stats['shorts']:>4} {full_stats['longs']:>4} "
              f"{full_stats['wr']:>5.1f}% {full_stats['avg_pnl']:>+7.2f}% {full_stats['pf']:>5.2f} "
              f"{full_stats['cum_pnl']:>+8.1f}% {full_stats['max_dd']:>6.1f}% "
              f"{full_stats['avg_r']:>+5.2f}R {full_stats['expectancy_r']:>+5.2f}R {full_stats['max_dd_r']:>6.2f}R")

    # Stability check
    valid_slices = [s for s in slice_results if s is not None]
    if valid_slices:
        pfs = [s['pf'] for s in valid_slices]
        avg_pnls = [s['avg_pnl'] for s in valid_slices]
        exp_rs = [s['expectancy_r'] for s in valid_slices]
        profitable_slices = sum(1 for s in valid_slices if s['pf'] > 1.0)
        pf_above_1_1 = sum(1 for s in valid_slices if s['pf'] > 1.1)

        print(f"\n  ── Stability Assessment ──")
        print(f"  Profitable slices (PF>1.0):  {profitable_slices}/{len(valid_slices)}")
        print(f"  Robust slices (PF>1.1):      {pf_above_1_1}/{len(valid_slices)}")
        print(f"  PF range:                    {min(pfs):.2f} – {max(pfs):.2f}")
        print(f"  Avg PnL range:               {min(avg_pnls):+.2f}% – {max(avg_pnls):+.2f}%")
        print(f"  Expectancy(R) range:         {min(exp_rs):+.2f}R – {max(exp_rs):+.2f}R")

        # Verdict
        if profitable_slices == len(valid_slices) and pf_above_1_1 >= len(valid_slices) // 2:
            print(f"  VERDICT: PASS — edge is stable across time periods")
        elif profitable_slices >= len(valid_slices) * 0.5:
            print(f"  VERDICT: MARGINAL — edge present in some periods but not consistent")
        else:
            print(f"  VERDICT: FAIL — no stable edge across time periods")

    return slice_results


# ═══════════════════════════════════════════════════════════════════
# RISK-NORMALIZED EQUITY CURVE
# ═══════════════════════════════════════════════════════════════════

def run_risk_normalized(csv_files: list, cfg: 'Config' = None,
                        alloc_pct: float = 5.0,
                        max_concurrent: int = 5,
                        max_dd_breaker_pct: float = 25.0,
                        starting_capital: float = 100000.0):
    """Simulate with equal-dollar position sizing (fixed % of equity per trade),
    concurrent exposure cap, and drawdown circuit breaker.
    Uses % PnL sizing (not R-based) because the strategy's edge is from win rate."""
    if cfg is None:
        cfg = Config(
            short_stop_mode="Run Peak",
            long_stop_mode="ATR Based",
            atr_mult=2.5,
            crash_velocity_min=3.0,
            require_selling_climax=True,
            selling_climax_rvol=3.0,
        )

    # Collect all trades with proper timestamps
    all_trades = []
    for csv_file in csv_files:
        ticker = extract_ticker(csv_file)
        bars = load_csv(csv_file)
        trades = run_backtest(ticker, bars, cfg)
        for t in trades:
            if t.entry_bar < len(bars):
                t._entry_ts = bars[t.entry_bar].timestamp
            if t.exit_bar >= 0 and t.exit_bar < len(bars):
                t._exit_ts = bars[t.exit_bar].timestamp
            else:
                t._exit_ts = bars[-1].timestamp
        all_trades.extend(trades)

    # Sort by exit timestamp (realize PnL in order)
    closed = [t for t in all_trades if t.exit_reason not in ("OPEN_AT_END", "")]
    closed.sort(key=lambda t: (t._exit_ts, -t.is_runner))

    print(f"\n  ═══ RISK-NORMALIZED SIMULATION (Equal-Dollar Sizing) ═══")
    print(f"  Starting Capital:    ${starting_capital:,.0f}")
    print(f"  Allocation Per Trade:{alloc_pct}% of equity")
    print(f"  Max Concurrent:      {max_concurrent} positions")
    print(f"  DD Circuit Breaker:  {max_dd_breaker_pct}% of peak equity")
    print(f"  Total trade legs:    {len(closed)}")

    equity = starting_capital
    peak_equity = starting_capital
    trade_results = []
    trades_taken = 0
    trades_skipped_breaker = 0

    for t in closed:
        # Check circuit breaker
        dd_pct = ((peak_equity - equity) / peak_equity) * 100 if peak_equity > 0 else 0
        if dd_pct >= max_dd_breaker_pct:
            trades_skipped_breaker += 1
            continue

        # Equal-dollar sizing: allocate fixed % of current equity
        position_value = equity * (alloc_pct / 100)
        # PnL in dollars = position_value * (pnl_pct / 100) * weight
        dollar_pnl = position_value * (t.pnl_pct / 100) * t.weight
        equity += dollar_pnl
        if equity > peak_equity:
            peak_equity = equity

        trade_results.append({
            'ticker': t.ticker, 'direction': t.direction,
            'pnl_pct': t.pnl_pct, 'weight': t.weight,
            'r_mult': t.r_multiple,
            'equity_after': equity, 'dollar_pnl': dollar_pnl,
            'exit_date': t.exit_date,
        })
        trades_taken += 1

    # Final stats
    total_return = ((equity - starting_capital) / starting_capital) * 100
    min_equity = min(r['equity_after'] for r in trade_results) if trade_results else starting_capital
    max_dd_dollar = peak_equity - min_equity
    max_dd_pct_final = (max_dd_dollar / peak_equity) * 100 if peak_equity > 0 else 0

    print(f"\n  ── Results ──")
    print(f"  Trades Taken:        {trades_taken}")
    print(f"  Skipped (breaker):   {trades_skipped_breaker}")
    print(f"  Final Equity:        ${equity:,.0f}")
    print(f"  Total Return:        {total_return:+.2f}%")
    print(f"  Peak Equity:         ${peak_equity:,.0f}")
    print(f"  Max Drawdown:        ${max_dd_dollar:,.0f} ({max_dd_pct_final:.1f}%)")
    if trades_taken > 0:
        print(f"  Avg $ PnL/Trade:     ${sum(r['dollar_pnl'] for r in trade_results) / trades_taken:+,.0f}")

    # Equity curve milestones
    if trade_results:
        print(f"\n  ── Equity Curve (every 20th trade) ──")
        print(f"  {'#':>4} {'Date':<12} {'Ticker':<8} {'Dir':<6} {'PnL%':>8} {'Wt':>4} {'$PnL':>10} {'Equity':>12} {'DD%':>6}")
        for idx, r in enumerate(trade_results):
            if idx % 20 == 0 or idx == len(trade_results) - 1:
                cur_dd = ((peak_equity - r['equity_after']) / peak_equity) * 100 if peak_equity > 0 else 0
                # Recalculate running peak for display
                running_peak = max(starting_capital, max(tr['equity_after'] for tr in trade_results[:idx+1]))
                cur_dd = ((running_peak - r['equity_after']) / running_peak) * 100 if running_peak > 0 else 0
                print(f"  {idx+1:>4} {r['exit_date']:<12} {r['ticker']:<8} {r['direction']:<6} "
                      f"{r['pnl_pct']:>+7.2f}% {r['weight']:>3.0%} "
                      f"${r['dollar_pnl']:>+9,.0f} ${r['equity_after']:>11,.0f} {cur_dd:>5.1f}%")

    # Also run without breaker for comparison
    print(f"\n  ── Without Circuit Breaker ──")
    equity_nb = starting_capital
    peak_nb = starting_capital
    min_nb = starting_capital
    for t in closed:
        position_value = equity_nb * (alloc_pct / 100)
        dollar_pnl = position_value * (t.pnl_pct / 100) * t.weight
        equity_nb += dollar_pnl
        if equity_nb > peak_nb:
            peak_nb = equity_nb
        if equity_nb < min_nb:
            min_nb = equity_nb

    total_return_nb = ((equity_nb - starting_capital) / starting_capital) * 100
    max_dd_nb = ((peak_nb - min_nb) / peak_nb) * 100 if peak_nb > 0 else 0
    print(f"  Final Equity:        ${equity_nb:,.0f}")
    print(f"  Total Return:        {total_return_nb:+.2f}%")
    print(f"  Peak Equity:         ${peak_nb:,.0f}")
    print(f"  Max Drawdown:        {max_dd_nb:.1f}%")

    return trade_results


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Run Parabolic Mean Reversion backtest on OHLC CSVs")
    parser.add_argument(
        "--data-dir",
        help="Directory containing OHLC CSV files (defaults to auto-discovery)",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Run parameter sweep instead of single backtest",
    )
    parser.add_argument(
        "--short-stop-sweep",
        action="store_true",
        help="Sweep SHORT stop modes (Run Peak, Trigger Bar High, ATR Based)",
    )
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help="Run walk-forward (time-split OOS) validation",
    )
    parser.add_argument(
        "--risk-sim",
        action="store_true",
        help="Run risk-normalized equity simulation",
    )
    parser.add_argument(
        "--full-analysis",
        action="store_true",
        help="Run all analyses: short stop sweep, walk-forward, risk sim",
    )
    args = parser.parse_args()

    candidates = []
    if args.data_dir:
        candidates.append(args.data_dir)
    candidates.extend([
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs2"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs2"),
        "/home/user/logs2",
    ])

    data_dir = next((d for d in candidates if os.path.isdir(d)), None)
    if not data_dir:
        print("ERROR: Data directory not found. Checked:")
        for d in candidates:
            print(f"  - {d}")
        sys.exit(1)

    csv_files = sorted(
        glob.glob(os.path.join(data_dir, "*.csv")) +
        glob.glob(os.path.join(data_dir, "*.txt"))
    )
    if not csv_files:
        print(f"ERROR: No data files (*.csv, *.txt) found in {data_dir}")
        sys.exit(1)

    if args.sweep:
        run_sweep(csv_files)
        return

    if args.short_stop_sweep or args.full_analysis:
        run_short_stop_sweep(csv_files)
        if not args.full_analysis:
            return

    if args.walk_forward or args.full_analysis:
        run_walk_forward(csv_files, n_slices=4)
        if not args.full_analysis:
            return

    if args.risk_sim or args.full_analysis:
        run_risk_normalized(csv_files)
        if not args.full_analysis:
            return

    if args.full_analysis:
        return

    cfg = Config()

    print("=" * 100)
    print("  Parabolic Snapback & Washout [Daily Screener V1] — Comprehensive Backtest")
    print("=" * 100)
    print(f"\n  Data directory: {data_dir}")
    print(f"  Tickers found:  {len(csv_files)}")
    print(f"\n  Configuration:")
    print(f"    Short Enabled:       {cfg.enable_short}")
    print(f"    Long Enabled:        {cfg.enable_long}")
    print(f"    Large-Cap Gain:      {cfg.largecap_gain_pct}%  (price >= ${cfg.cap_cutoff_price})")
    print(f"    Small-Cap Gain:      {cfg.smallcap_gain_pct}%  (price < ${cfg.cap_cutoff_price})")
    print(f"    Gain Lookback:       {cfg.gain_lookback} bars")
    print(f"    Min Green Days:      {cfg.min_green_days}")
    print(f"    Min Ext Above MA:    {cfg.min_ext_above_ma}%")
    print(f"    Min Crash %:         {cfg.min_crash_pct}%")
    print(f"    Crash Window:        {cfg.crash_window} bars")
    print(f"    Crash Velocity Min:  {cfg.crash_velocity_min}%/bar")
    print(f"    Selling Climax Gate: {cfg.require_selling_climax} (RVOL >= {cfg.selling_climax_rvol}x)")
    print(f"    Require Prior Run:   {cfg.require_prior_run} (min {cfg.prior_run_min_pct}%)")
    print(f"    Short Trigger:       {cfg.short_trigger}")
    print(f"    Long Trigger:        {cfg.long_trigger}")
    print(f"    Short Stop Mode:     {cfg.short_stop_mode}")
    print(f"    Long Stop Mode:      {cfg.long_stop_mode}")
    print(f"    Stop Buffer:         {cfg.stop_buffer}%")
    print(f"    Setup Timeout:       {cfg.short_setup_timeout} / {cfg.long_setup_timeout} bars")
    print(f"    Cooldown:            {cfg.cooldown_bars} bars")
    print(f"    Max Trade Duration:  {cfg.max_trade_bars} bars")
    print(f"    Split Exit:          {cfg.split_exit} ({cfg.split_pct}% at 10MA, {100-cfg.split_pct}% runner to 20MA)")
    print(f"    Climax Volume:       {cfg.use_climax_vol} (RVOL >= {cfg.rvol_threshold}x, window {cfg.climax_window_bars} bars)")
    print(f"    Dollar Vol Filter:   >= ${cfg.min_avg_dollar_vol}M avg")
    print(f"    ADR Filter:          {cfg.use_adr_filter} (max {cfg.max_stop_vs_adr}x ADR)")
    print(f"    Min Price:           ${cfg.min_price}")
    print(f"    Min ADR:             {cfg.min_adr_pct}%")

    print(f"\n  ── Per-Ticker Results ──")
    print(f"  {'Ticker':<8} | {'Total':>6} | {'Short':>6} | {'Long':>6} | {'Legs':>7} | {'Win%':>7} | "
          f"{'Avg PnL':>9} | {'Avg Win':>9} | {'Avg Loss':>9} | {'Avg Bars':>9}")
    print(f"  {'-' * 100}")

    all_trades = []
    for csv_file in csv_files:
        ticker = extract_ticker(csv_file)
        bars = load_csv(csv_file)

        trades = run_backtest(ticker, bars, cfg)
        all_trades.extend(trades)

        print_ticker_summary(ticker, trades)

    print_grand_summary(all_trades)


if __name__ == "__main__":
    main()
