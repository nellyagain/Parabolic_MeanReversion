#!/usr/bin/env python3
"""
Parabolic Mean Reversion V5 — Hybrid Wyckoff/CAN SLIM Backtester

V5 changes (LONG side only, SHORT side unchanged from V4):
  - Replaces "First Green Day" trigger with 5-phase Wyckoff/CAN SLIM state machine:
    Phase 1: Crash Detection (existing velocity + selling climax)
    Phase 2: Automatic Rally (AR) identification
    Phase 3: Base Formation with absorption scoring
    Phase 4: Optional Spring detection (additive to score)
    Phase 5: Breakout trigger with volume + close strength + range expansion
  - RVOL computed using median baseline (not mean)
  - Dynamic thresholds with ATR-based clamps (caps/floors)
  - Regime filter: price vs 200-bar MA
  - Gap exclusion on entry day
  - Exit model: structural stop, time stop, R-based partials, trailing channel
"""

import csv
import os
import sys
import glob
import math
import argparse
import statistics
from datetime import datetime, timezone
from dataclasses import dataclass, field


# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Config:
    # ── SHORT side ──
    enable_short: bool = True
    largecap_gain_pct: float = 40.0
    smallcap_gain_pct: float = 300.0
    cap_cutoff_price: float = 20.0
    use_manual_threshold: bool = False
    manual_gain_pct: float = 80.0
    gain_lookback: int = 20
    min_green_days: int = 3
    min_ext_above_ma: float = 30.0     # V7: was 20.0; raise to only short extreme extensions
    ext_ma_len: int = 20
    use_ext_bb_filter: bool = False
    bb_dev_mult: float = 3.0
    # SHORT exit: no runner — take full profit at 10MA (runner survival collapsed to 0%)
    short_split_exit: bool = False    # V6: disabled (was True in V4/V5)
    short_split_pct: float = 50.0
    short_time_stop: int = 20        # V7: was 15; give SHORT winners more room (TIME_STOP avg +11%)
    # SHORT regime filter: rolling circuit breaker
    short_circuit_breaker: bool = True    # V6: pause shorts if recent performance is bad
    short_circuit_lookback: int = 5       # V7: was 10; faster response to losing streaks
    short_circuit_min_wr: float = 40.0    # V7: was 25%; more aggressive circuit breaker

    # ── LONG side — Crash Detection (kept from V4) ──
    enable_long: bool = True
    min_crash_pct: float = 30.0
    crash_window: int = 15
    crash_velocity_min: float = 3.0
    require_selling_climax: bool = True
    selling_climax_rvol: float = 3.0
    require_prior_run: bool = True
    prior_run_min_pct: float = 40.0
    prior_run_lookback: int = 60

    # ── LONG side — V6 Dual-Channel Entry ──
    # Channel A: Fast path (V4-style) for deep crashes — enter on First Green Day
    long_fast_path: bool = True           # V6: enabled
    fast_path_min_crash: float = 30.0     # V7: was 35%; all crashes ≥30% use fast path FGD entry
    fast_path_trigger: str = "First Green Day"
    # Channel B: Wyckoff base breakout for shallower crashes
    min_base_bars: int = 7
    atr_contraction_pct: float = 80.0
    absorption_threshold: float = 0.2
    # Spring detection (optional, additive)
    spring_atr_mult: float = 1.0
    spring_depth_floor_pct: float = 1.5
    spring_depth_cap_pct: float = 5.0
    spring_max_recovery_bars: int = 3
    spring_score_bonus: float = 0.25
    # Breakout trigger
    breakout_rvol_min: float = 1.2
    breakout_close_strength: float = 0.6
    breakout_range_expansion: float = 1.0
    # Gap exclusion
    gap_exclusion_atr_mult: float = 2.0

    # ── V8: SHORT minimum reward filter ──
    short_min_reward_pct: float = 0.0     # V8: disabled (entry filters reduce sample too much)

    # ── LONG side — Stop Management ──
    long_stop_cap_atr_mult: float = 2.0   # V6: cap structural stop at 2x ATR from entry
    long_max_stop_pct: float = 0.0        # V8: disabled (tighter stops create more stop-outs)
    # ── LONG side — Exit Model ──
    long_exit_mode: str = "v5_hybrid"  # "v5_hybrid" or "v4_legacy"
    long_r1_target: float = 1.5
    long_r2_target: float = 2.5
    long_partial_pct: float = 33.3
    long_trail_channel: int = 10
    long_time_stop: int = 30

    # ── Climax Volume (SHORT side) ──
    use_climax_vol: bool = False
    rvol_threshold: float = 3.0
    rvol_baseline: int = 20
    climax_window_bars: int = 1

    # ── Liquidity Filters ──
    min_price: float = 5.0
    min_adr_pct: float = 1.0
    min_avg_dollar_vol: float = 0.0
    dollar_vol_len: int = 20

    # ── Setup Trigger (SHORT only) ──
    short_trigger: str = "Close < Prior Low"
    min_bars_after_setup: int = 0

    # ── Targets (SHORT side MA exits, LONG side legacy) ──
    target_ma_fast: int = 10
    target_ma_slow: int = 20

    # ── Regime Filter ──
    use_regime_filter: bool = False   # V6: kept disabled (blocks crash-bounce setups below 200MA)
    regime_ma_len: int = 200

    # ── Trend Context ──
    use_trend_filter: bool = False
    trend_ma_len: int = 50

    # ── Split Exit — legacy compat (now controlled by short_split_exit) ──
    split_exit: bool = False       # V6: global off; SHORT uses short_split_exit
    split_pct: float = 50.0

    # ── Quality Gates ──
    min_close_strength: float = 0.0
    use_adr_filter: bool = True
    adr_len: int = 20
    max_stop_vs_adr: float = 1.2      # V7: was 1.5; tighter stops to improve SHORT win/loss ratio

    # ── Risk Management ──
    short_stop_mode: str = "Run Peak"
    long_stop_mode: str = "Structural"  # V6: structural with ATR cap
    stop_buffer: float = 0.2
    atr_len: int = 14
    atr_mult: float = 1.0
    max_loss_pct: float = 0.0            # V8: disabled (conflicts with stop logic, causes worse fills)

    # ── V8: Per-ticker LONG cooldown ──
    long_ticker_cooldown: bool = True     # V8: skip LONG on a ticker after N consecutive stops
    long_ticker_max_consec_stops: int = 3 # V8: number of consecutive LONG stops before cooldown

    # ── Timeouts & Cooldown ──
    short_setup_timeout: int = 10
    long_setup_timeout: int = 10     # V6: fast-path timeout (same as V4)
    long_base_timeout: int = 80      # V6: base-path timeout
    cooldown_bars: int = 3

    # ── Backtest-specific ──
    max_trade_bars: int = 50
    # ── Forward test ──
    forward_test_date: str = "2023-01-01"  # V6: fixed calendar cutoff for all tickers
    # ── V8: Additional data directories ──
    extra_data_dirs: list = None  # V8: list of additional data directories to scan


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
    direction: str
    entry_bar: int
    entry_date: str
    entry_price: float
    stop_price: float
    target_fast: float
    target_slow: float
    exit_bar: int = -1
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_pct: float = 0.0
    r_multiple: float = 0.0
    bars_held: int = 0
    weight: float = 1.0
    is_runner: bool = False
    # Context metrics at entry
    extension_pct: float = 0.0
    rolling_gain_pct: float = 0.0
    green_streak: int = 0
    crash_from_peak: float = 0.0
    risk_pct: float = 0.0
    # V5 LONG context
    base_duration: int = 0
    absorption_score: float = 0.0
    spring_detected: bool = False
    breakout_rvol: float = 0.0
    atr_contraction: float = 0.0
    # V5 exit tracking
    r_unit: float = 0.0          # Dollar value of 1R
    target_r1_price: float = 0.0  # Price at 1.5R
    target_r2_price: float = 0.0  # Price at 2.5R
    trail_stop: float = 0.0      # Current trailing stop
    partial_stage: int = 0       # 0=full, 1=after R1, 2=after R2 (trailing)


# ═══════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def sma(values: list, length: int) -> float:
    if len(values) < length:
        return sum(values) / len(values) if values else 0.0
    return sum(values[-length:]) / length


def median_val(values: list, length: int) -> float:
    if not values:
        return 0.0
    window = values[-length:] if len(values) >= length else values
    return statistics.median(window)


def atr_calc(bars: list, length: int) -> float:
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


def true_range(bar, prev_bar):
    return max(
        bar.high - bar.low,
        abs(bar.high - prev_bar.close),
        abs(bar.low - prev_bar.close)
    )


def bb_upper(closes: list, length: int, mult: float) -> float:
    if len(closes) < length:
        return float('inf')
    window = closes[-length:]
    mean = sum(window) / length
    variance = sum((x - mean) ** 2 for x in window) / length
    std = math.sqrt(variance)
    return mean + mult * std


def clamp(value, floor, cap):
    return max(floor, min(cap, value))


def load_csv(filepath: str) -> list:
    bars = []
    with open(filepath, 'r') as f:
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


def extract_ticker(filename: str) -> str:
    base = os.path.basename(filename)
    parts = base.split(',')[0]
    if '_' in parts:
        return parts.split('_', 1)[1]
    return parts


# ═══════════════════════════════════════════════════════════════════
# ABSORPTION SCORE CALCULATOR
# ═══════════════════════════════════════════════════════════════════

def compute_absorption_score(bars_slice, volumes_slice, base_low, atr_at_crash):
    """
    Compute absorption score (0-1) for a base formation.
    Measures supply exhaustion through:
      - volume_trend: declining volume on tests of the low (0-0.4 weight)
      - range_trend: contracting bar ranges on tests of the low (0-0.3 weight)
      - low_stability: no new ATR-significant lows after initial test (0-0.15 weight)
      - time_in_base: longer base = more cause (0-0.15 weight, log-scaled)
    """
    if len(bars_slice) < 5 or atr_at_crash <= 0:
        return 0.0

    # Define "lower base zone" as within 1 ATR of base_low
    low_zone_threshold = base_low + atr_at_crash

    # Find bars that test the lower zone
    low_zone_bars = []
    for j, bar in enumerate(bars_slice):
        if bar.low <= low_zone_threshold:
            bar_range = bar.high - bar.low
            vol = volumes_slice[j] if j < len(volumes_slice) else 0
            low_zone_bars.append((j, vol, bar_range, bar.low))

    # Volume trend on low-zone tests (negative slope = supply drying up)
    vol_score = 0.0
    if len(low_zone_bars) >= 2:
        vols = [b[1] for b in low_zone_bars]
        # Simple: compare first half avg to second half avg
        mid = len(vols) // 2
        first_half = sum(vols[:mid]) / max(mid, 1)
        second_half = sum(vols[mid:]) / max(len(vols) - mid, 1)
        if first_half > 0:
            decline_ratio = second_half / first_half
            # decline_ratio < 1 means volume is declining (good)
            vol_score = clamp(1.0 - decline_ratio, 0.0, 1.0)

    # Range trend on low-zone tests (contracting ranges = good)
    range_score = 0.0
    if len(low_zone_bars) >= 2:
        ranges = [b[2] for b in low_zone_bars]
        mid = len(ranges) // 2
        first_half = sum(ranges[:mid]) / max(mid, 1)
        second_half = sum(ranges[mid:]) / max(len(ranges) - mid, 1)
        if first_half > 0:
            range_decline = second_half / first_half
            range_score = clamp(1.0 - range_decline, 0.0, 1.0)

    # Low stability: no new ATR-significant lows after first 1/3 of base
    stability_score = 0.0
    if len(bars_slice) >= 6:
        first_third = len(bars_slice) // 3
        initial_low = min(b.low for b in bars_slice[:first_third])
        rest_low = min(b.low for b in bars_slice[first_third:])
        # "Significant" = more than 0.5*ATR below initial low
        if rest_low >= initial_low - 0.5 * atr_at_crash:
            stability_score = 1.0
        elif rest_low >= initial_low - 1.0 * atr_at_crash:
            stability_score = 0.5

    # Time in base (log-scaled, caps at ~60 bars)
    time_score = clamp(math.log(max(len(bars_slice), 1)) / math.log(60), 0.0, 1.0)

    # Weighted combination
    score = (vol_score * 0.4 +
             range_score * 0.3 +
             stability_score * 0.15 +
             time_score * 0.15)

    return clamp(score, 0.0, 1.0)


# ═══════════════════════════════════════════════════════════════════
# CORE ENGINE
# ═══════════════════════════════════════════════════════════════════

def run_backtest(ticker: str, bars: list, cfg: Config) -> list:
    trades = []
    n = len(bars)
    min_history = max(cfg.gain_lookback, cfg.prior_run_lookback, cfg.ext_ma_len,
                      cfg.adr_len, cfg.atr_len, cfg.regime_ma_len if cfg.use_regime_filter else 0) + 5
    if n < min_history:
        return trades

    # Rolling state
    closes = []
    highs = []
    lows = []
    volumes = []
    dollar_vols = []
    daily_range_pcts = []
    true_ranges = []

    # SHORT state machine
    short_setup_active = False
    short_setup_bar = -1
    parabolic_peak = 0.0
    parabolic_peak_bar = -1
    advance_start_bar = -1
    advance_start_low = 0.0
    last_short_bar = -999
    short_avwap_num = 0.0
    short_avwap_den = 0.0
    short_run_avwap = 0.0
    # V6: SHORT circuit breaker — track recent SHORT trade results
    recent_short_results = []  # list of pnl_pct for last N SHORT trades
    short_circuit_active = False  # True when circuit breaker is tripped
    # V8: Per-ticker LONG consecutive stop tracker
    long_ticker_consec_stops = {}  # ticker -> count of consecutive LONG stops

    # LONG state machine — V6 dual-channel (fast path + base breakout)
    # Fast path state (Channel A)
    long_fast_active = False
    long_fast_bar = -1
    long_fast_washout_low = 0.0
    long_fast_crash_pct = 0.0
    # Phase 1: Crash detected (sets long_crash_detected)
    # Phase 2: AR forming (tracking highest high after crash)
    # Phase 3: Base building (monitoring absorption)
    # Phase 4: Spring detection (optional)
    # Phase 5: Breakout ready → triggered
    LONG_IDLE = 0
    LONG_AR_FORMING = 1
    LONG_BASE_BUILDING = 2
    LONG_BREAKOUT_READY = 3

    long_phase = LONG_IDLE
    long_phase_bar = -1
    long_crash_bar = -1
    long_sc_low = 0.0           # Selling Climax low
    long_ar_high = 0.0          # Automatic Rally high
    long_ar_confirmed = False
    long_ar_pullback_count = 0
    long_base_low = 0.0
    long_base_high = 0.0
    long_pivot = 0.0            # Breakout pivot point
    long_spring_low = 0.0
    long_spring_detected = False
    long_spring_bar = -1
    long_atr_at_crash = 0.0     # ATR when crash detected
    long_crash_bars_slice = []   # Bars during base for absorption calc
    long_crash_vols_slice = []
    last_long_bar = -999

    # Climax tracking
    last_climax_bar = -999
    last_selling_climax_bar = -999

    # Green streak
    green_streak = 0

    # Open trades
    open_trades: list = []

    for i in range(n):
        bar = bars[i]
        closes.append(bar.close)
        highs.append(bar.high)
        lows.append(bar.low)
        volumes.append(bar.volume)
        dollar_vols.append(bar.close * bar.volume)

        # Compute true range
        if i > 0:
            tr = true_range(bar, bars[i - 1])
        else:
            tr = bar.high - bar.low
        true_ranges.append(tr)

        # ── Track open trades ──
        new_open_trades = []
        for t in open_trades:
            bars_held = i - t.entry_bar
            if t.exit_bar >= 0:
                new_open_trades.append(t)
                continue

            if t.direction == "SHORT":
                # V8: Hard max loss cap — force exit if unrealized loss exceeds max_loss_pct
                unrealized_short_pnl = ((t.entry_price - bar.close) / t.entry_price) * 100 if t.entry_price > 0 else 0
                if cfg.max_loss_pct > 0 and unrealized_short_pnl < -cfg.max_loss_pct:
                    t.exit_bar = i
                    t.exit_date = bar.date
                    t.exit_price = bar.close
                    t.exit_reason = "MAX_LOSS"
                    t.pnl_pct = unrealized_short_pnl
                    t.bars_held = bars_held
                    if t.risk_pct > 0:
                        t.r_multiple = t.pnl_pct / t.risk_pct
                    if not t.is_runner:
                        recent_short_results.append(t.pnl_pct)
                    continue

                # V6 SHORT exit: no runner, full exit at 10MA, SHORT-specific time stop
                stop_hit = bar.high >= t.stop_price
                tgt_fast_hit = not t.is_runner and bar.low <= t.target_fast and t.target_fast < t.entry_price
                tgt_slow_hit = t.is_runner and bar.low <= t.target_slow and t.target_slow < t.entry_price

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
                    t.pnl_pct = ((t.entry_price - t.exit_price) / t.entry_price) * 100
                    t.bars_held = bars_held
                    if t.risk_pct > 0:
                        t.r_multiple = t.pnl_pct / t.risk_pct
                    if not t.is_runner:
                        recent_short_results.append(t.pnl_pct)
                elif tgt_fast_hit:
                    # V6: full exit at 10MA (no runner — runner survival collapsed)
                    t.exit_bar = i
                    t.exit_date = bar.date
                    t.exit_price = t.target_fast
                    t.exit_reason = "TARGET_10MA"
                    t.pnl_pct = ((t.entry_price - t.exit_price) / t.entry_price) * 100
                    t.bars_held = bars_held
                    if t.risk_pct > 0:
                        t.r_multiple = t.pnl_pct / t.risk_pct
                    recent_short_results.append(t.pnl_pct)
                elif tgt_slow_hit:
                    # Runner leg hits 20MA
                    t.exit_bar = i
                    t.exit_date = bar.date
                    t.exit_price = t.target_slow
                    t.exit_reason = "TARGET_20MA"
                    t.pnl_pct = ((t.entry_price - t.exit_price) / t.entry_price) * 100
                    t.bars_held = bars_held
                    if t.risk_pct > 0:
                        t.r_multiple = t.pnl_pct / t.risk_pct
                elif bars_held >= cfg.short_time_stop:
                    # V6: SHORT-specific time stop (15 bars, was 50)
                    t.exit_bar = i
                    t.exit_date = bar.date
                    t.exit_price = bar.close
                    t.exit_reason = "TIME_STOP"
                    t.pnl_pct = ((t.entry_price - t.exit_price) / t.entry_price) * 100
                    t.bars_held = bars_held
                    if t.risk_pct > 0:
                        t.r_multiple = t.pnl_pct / t.risk_pct
                    if not t.is_runner:
                        recent_short_results.append(t.pnl_pct)
                else:
                    new_open_trades.append(t)
                    continue

            elif t.direction == "LONG":
                # V8: Hard max loss cap for LONG trades
                unrealized_long_pnl = ((bar.close - t.entry_price) / t.entry_price) * 100 if t.entry_price > 0 else 0
                if cfg.max_loss_pct > 0 and unrealized_long_pnl < -cfg.max_loss_pct and t.partial_stage < 1:
                    t.exit_bar = i
                    t.exit_date = bar.date
                    t.exit_price = bar.close
                    t.exit_reason = "MAX_LOSS"
                    t.pnl_pct = unrealized_long_pnl
                    t.bars_held = bars_held
                    if t.risk_pct > 0:
                        t.r_multiple = t.pnl_pct / t.risk_pct
                    # V8: Track consecutive LONG stops per ticker
                    if not t.is_runner:
                        long_ticker_consec_stops[ticker] = long_ticker_consec_stops.get(ticker, 0) + 1
                    continue

                # V5 LONG exit logic
                if cfg.long_exit_mode == "v5_hybrid" and t.r_unit > 0:
                    # Update trailing stop
                    if t.partial_stage >= 2 and i > t.entry_bar:
                        lookback_start = max(0, i - cfg.long_trail_channel)
                        channel_low = min(bars[j].low for j in range(lookback_start, i))
                        t.trail_stop = max(t.trail_stop, channel_low)

                    stop_price = t.trail_stop if t.partial_stage >= 2 and t.trail_stop > 0 else t.stop_price
                    stop_hit = bar.low <= stop_price

                    # R-based targets
                    r1_hit = t.partial_stage == 0 and t.target_r1_price > 0 and bar.high >= t.target_r1_price
                    r2_hit = t.partial_stage == 1 and t.target_r2_price > 0 and bar.high >= t.target_r2_price

                    # Time stop
                    time_stop = bars_held >= cfg.long_time_stop and t.partial_stage < 2

                    # Intrabar ambiguity
                    if stop_hit and (r1_hit or r2_hit):
                        dist_to_stop = abs(stop_price - bar.open)
                        tgt_p = t.target_r1_price if r1_hit else t.target_r2_price
                        dist_to_tgt = abs(tgt_p - bar.open)
                        if dist_to_tgt <= dist_to_stop:
                            stop_hit = False
                        else:
                            r1_hit = False
                            r2_hit = False

                    if stop_hit:
                        t.exit_bar = i
                        t.exit_date = bar.date
                        t.exit_price = stop_price
                        if t.partial_stage >= 2:
                            t.exit_reason = "TRAIL_STOP"
                        elif t.is_runner:
                            t.exit_reason = "STOP_BREAKEVEN"
                        else:
                            t.exit_reason = "STOP"
                            # V8: Track consecutive LONG stops per ticker
                            long_ticker_consec_stops[ticker] = long_ticker_consec_stops.get(ticker, 0) + 1
                        t.pnl_pct = ((t.exit_price - t.entry_price) / t.entry_price) * 100
                        t.bars_held = bars_held
                        if t.risk_pct > 0:
                            t.r_multiple = t.pnl_pct / t.risk_pct
                    elif r1_hit:
                        # Take 1/3 at 1.5R
                        t.exit_bar = i
                        t.exit_date = bar.date
                        t.exit_price = t.target_r1_price
                        t.exit_reason = "TARGET_R1"
                        t.pnl_pct = ((t.exit_price - t.entry_price) / t.entry_price) * 100
                        t.bars_held = bars_held
                        t.weight = cfg.long_partial_pct / 100.0
                        if t.risk_pct > 0:
                            t.r_multiple = t.pnl_pct / t.risk_pct
                        # V8: Reset consecutive stop counter — trade won
                        long_ticker_consec_stops[ticker] = 0
                        # Create runner for R2
                        runner = Trade(
                            ticker=t.ticker, direction="LONG",
                            entry_bar=t.entry_bar, entry_date=t.entry_date,
                            entry_price=t.entry_price,
                            stop_price=t.entry_price,  # Move to breakeven
                            target_fast=t.target_fast, target_slow=t.target_slow,
                            crash_from_peak=t.crash_from_peak,
                            risk_pct=t.risk_pct,
                            weight=(100.0 - cfg.long_partial_pct) / 100.0,
                            is_runner=True,
                            r_unit=t.r_unit,
                            target_r1_price=t.target_r1_price,
                            target_r2_price=t.target_r2_price,
                            partial_stage=1,
                            base_duration=t.base_duration,
                            absorption_score=t.absorption_score,
                            spring_detected=t.spring_detected,
                            breakout_rvol=t.breakout_rvol,
                        )
                        trades.append(runner)
                        new_open_trades.append(runner)
                    elif r2_hit:
                        # Take another 1/3 at 2.5R, rest trails
                        t.exit_bar = i
                        t.exit_date = bar.date
                        t.exit_price = t.target_r2_price
                        t.exit_reason = "TARGET_R2"
                        t.pnl_pct = ((t.exit_price - t.entry_price) / t.entry_price) * 100
                        t.bars_held = bars_held
                        t.weight = cfg.long_partial_pct / 100.0 / (1.0 - cfg.long_partial_pct / 100.0) if cfg.long_partial_pct < 100 else 0.5
                        if t.risk_pct > 0:
                            t.r_multiple = t.pnl_pct / t.risk_pct
                        # Create trailing runner
                        remaining_weight = 1.0 - cfg.long_partial_pct / 100.0 - t.weight
                        if remaining_weight > 0.01:
                            trail_runner = Trade(
                                ticker=t.ticker, direction="LONG",
                                entry_bar=t.entry_bar, entry_date=t.entry_date,
                                entry_price=t.entry_price,
                                stop_price=t.entry_price,
                                target_fast=t.target_fast, target_slow=t.target_slow,
                                crash_from_peak=t.crash_from_peak,
                                risk_pct=t.risk_pct,
                                weight=remaining_weight,
                                is_runner=True,
                                r_unit=t.r_unit,
                                target_r1_price=t.target_r1_price,
                                target_r2_price=t.target_r2_price,
                                partial_stage=2,
                                trail_stop=bar.low,  # Initialize trailing stop
                                base_duration=t.base_duration,
                                absorption_score=t.absorption_score,
                                spring_detected=t.spring_detected,
                                breakout_rvol=t.breakout_rvol,
                            )
                            trades.append(trail_runner)
                            new_open_trades.append(trail_runner)
                    elif time_stop:
                        t.exit_bar = i
                        t.exit_date = bar.date
                        t.exit_price = bar.close
                        t.exit_reason = "TIME_STOP"
                        t.pnl_pct = ((t.exit_price - t.entry_price) / t.entry_price) * 100
                        t.bars_held = bars_held
                        if t.risk_pct > 0:
                            t.r_multiple = t.pnl_pct / t.risk_pct
                    else:
                        new_open_trades.append(t)
                        continue
                else:
                    # V4 legacy LONG exit
                    stop_hit = bar.low <= t.stop_price
                    tgt_fast_hit = not t.is_runner and bar.high >= t.target_fast and t.target_fast > t.entry_price
                    tgt_slow_hit = bar.high >= t.target_slow and t.target_slow > t.entry_price

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

        # ── Daily range pcts ──
        drp = ((bar.high / bar.low) - 1) * 100 if bar.low > 0 else 0.0
        daily_range_pcts.append(drp)

        # ── Green streak ──
        if i > 0 and bar.close > bars[i - 1].close:
            green_streak += 1
        else:
            green_streak = 0

        # ── Early-bar guard ──
        min_bars_needed = max(cfg.gain_lookback, cfg.ext_ma_len, cfg.adr_len, cfg.atr_len) + 1
        if cfg.use_regime_filter:
            min_bars_needed = max(min_bars_needed, cfg.regime_ma_len + 1)
        if i < min_bars_needed:
            continue

        # ═══════════════════════════════════════════════════════════
        # LIQUIDITY GATE
        # ═══════════════════════════════════════════════════════════
        adr_pct = sma(daily_range_pcts, cfg.adr_len)
        avg_dollar_vol = sma(dollar_vols, cfg.dollar_vol_len) / 1e6

        liquidity_ok = (bar.close >= cfg.min_price
                        and adr_pct >= cfg.min_adr_pct
                        and avg_dollar_vol >= cfg.min_avg_dollar_vol)

        # ═══════════════════════════════════════════════════════════
        # PARABOLIC ADVANCE DETECTION (SHORT side, unchanged)
        # ═══════════════════════════════════════════════════════════
        enough_bars_gain = i >= cfg.gain_lookback
        enough_bars_peak = i >= cfg.prior_run_lookback

        window_lows = lows[max(0, i - cfg.gain_lookback + 1):i + 1]
        recent_low = min(window_lows) if window_lows else bar.low
        rolling_gain_pct = ((bar.close - recent_low) / recent_low) * 100 if recent_low > 0 else 0.0

        gain_threshold = cfg.manual_gain_pct if cfg.use_manual_threshold else \
            (cfg.largecap_gain_pct if bar.close >= cfg.cap_cutoff_price else cfg.smallcap_gain_pct)
        is_parabolic_gain = enough_bars_gain and rolling_gain_pct >= gain_threshold

        has_green_streak = green_streak >= cfg.min_green_days

        ext_ma = sma(closes, cfg.ext_ma_len)
        extension_pct = ((bar.close - ext_ma) / ext_ma) * 100 if ext_ma > 0 else 0.0
        is_extended = extension_pct >= cfg.min_ext_above_ma

        bb_ok = True
        if cfg.use_ext_bb_filter:
            bb_up = bb_upper(closes, cfg.ext_ma_len, cfg.bb_dev_mult)
            bb_ok = bar.close > bb_up

        trend_ok = True
        if cfg.use_trend_filter:
            trend_ma = sma(closes, cfg.trend_ma_len)
            trend_ok = bar.close > trend_ma

        parabolic_advance_detected = (liquidity_ok and is_parabolic_gain and
                                      has_green_streak and is_extended and
                                      bb_ok and trend_ok)

        # ═══════════════════════════════════════════════════════════
        # VOLUME DETECTION (median-based RVOL for V5)
        # ═══════════════════════════════════════════════════════════
        vol_baseline_mean = sma(volumes, cfg.rvol_baseline)
        vol_baseline_median = median_val(volumes, cfg.rvol_baseline)
        rvol_mean = bar.volume / vol_baseline_mean if vol_baseline_mean > 0 else 0.0
        rvol_median = bar.volume / vol_baseline_median if vol_baseline_median > 0 else 0.0

        # Use mean-based for SHORT (V4 compatibility), median-based for LONG breakout
        is_climax_volume = rvol_mean >= cfg.rvol_threshold
        if is_climax_volume:
            last_climax_bar = i

        is_selling_climax_bar = rvol_mean >= cfg.selling_climax_rvol and bar.close < bar.open
        if is_selling_climax_bar:
            last_selling_climax_bar = i

        climax_aligned = (i - last_climax_bar) <= cfg.climax_window_bars
        climax_vol_ok = climax_aligned if cfg.use_climax_vol else True

        # ═══════════════════════════════════════════════════════════
        # WASHOUT CRASH DETECTION (Long Side Phase 1)
        # ═══════════════════════════════════════════════════════════
        crash_from_peak = 0.0
        crash_velocity = 0.0
        bars_from_peak = 0
        is_crash_candidate = False
        had_prior_run = True
        peak_bar_idx = -1

        if enough_bars_peak:
            peak_window = highs[max(0, i - cfg.prior_run_lookback + 1):i + 1]
            peak_high = max(peak_window)
            last_idx = len(peak_window) - 1
            for pi in range(last_idx, -1, -1):
                if peak_window[pi] == peak_high:
                    last_idx = pi
                    break
            peak_offset = len(peak_window) - 1 - last_idx
            peak_bar_idx = i - peak_offset

            bars_from_peak = i - peak_bar_idx
            crash_from_peak = ((peak_high - bar.close) / peak_high) * 100 if peak_high > 0 else 0.0

            crash_velocity = crash_from_peak / max(bars_from_peak, 1)
            is_velocity_crash = (crash_from_peak >= cfg.min_crash_pct
                                 and crash_velocity >= cfg.crash_velocity_min
                                 and bars_from_peak <= cfg.crash_window)

            has_selling_climax = True
            if cfg.require_selling_climax:
                has_selling_climax = last_selling_climax_bar >= peak_bar_idx

            is_crash_candidate = is_velocity_crash and has_selling_climax

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
        # SHORT SETUP STATE MACHINE (unchanged from V4)
        # ═══════════════════════════════════════════════════════════
        short_setup_triggered = False

        if (cfg.enable_short and parabolic_advance_detected and climax_vol_ok
                and not short_setup_active and (i - last_short_bar > cfg.cooldown_bars)):
            short_setup_active = True
            short_setup_bar = i
            parabolic_peak = bar.high
            parabolic_peak_bar = i
            advance_start_low = recent_low
            advance_start_bar = i - cfg.gain_lookback + 1 + (window_lows.index(recent_low) if recent_low in window_lows else 0)
            short_avwap_num = 0.0
            short_avwap_den = 0.0
            short_run_avwap = 0.0

        if short_setup_active and bar.high > parabolic_peak:
            parabolic_peak = bar.high
            parabolic_peak_bar = i

        if short_setup_active:
            src = bar.ohlc4
            short_avwap_num += src * bar.volume
            short_avwap_den += bar.volume
            short_run_avwap = short_avwap_num / short_avwap_den if short_avwap_den > 0 else 0.0

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
            bar_rng = max(bar.high - bar.low, 0.0001)
            short_close_str = (bar.high - bar.close) / bar_rng
            short_close_ok = short_close_str >= cfg.min_close_strength

            if cfg.short_stop_mode == "Run Peak":
                short_stop = parabolic_peak * (1 + cfg.stop_buffer / 100)
            elif cfg.short_stop_mode == "Trigger Bar High":
                short_stop = bar.high * (1 + cfg.stop_buffer / 100)
            else:
                short_stop = bar.close + (atr_calc(bars[:i + 1], cfg.atr_len) * cfg.atr_mult)

            short_stop_width = abs(short_stop - bar.close) / bar.close * 100 if bar.close > 0 else 0.0
            short_adr_ok = True
            if cfg.use_adr_filter and adr_pct > 0 and cfg.short_stop_mode != "ATR Based":
                short_adr_ok = short_stop_width <= (adr_pct * cfg.max_stop_vs_adr)

            # V6: circuit breaker — skip SHORT if trailing performance is bad
            circuit_ok = True
            if cfg.short_circuit_breaker and len(recent_short_results) >= cfg.short_circuit_lookback:
                trailing = recent_short_results[-cfg.short_circuit_lookback:]
                trailing_wr = sum(1 for p in trailing if p > 0) / len(trailing) * 100
                if trailing_wr < cfg.short_circuit_min_wr:
                    circuit_ok = False

            if short_entry_proxy and short_close_ok and short_adr_ok and circuit_ok:
                short_setup_triggered = True
                tf = sma(closes, cfg.target_ma_fast)
                ts_val = sma(closes, cfg.target_ma_slow)
                risk_pct = ((short_stop - bar.close) / bar.close) * 100 if bar.close > 0 else 0.0

                # V8: SHORT minimum reward filter — 10MA target must be >= N% below entry
                short_reward_pct = ((bar.close - tf) / bar.close) * 100 if bar.close > 0 and tf < bar.close else 0.0
                if short_reward_pct < cfg.short_min_reward_pct:
                    short_setup_triggered = False

                if short_setup_triggered:
                    trade = Trade(
                        ticker=ticker, direction="SHORT",
                        entry_bar=i, entry_date=bar.date,
                        entry_price=bar.close, stop_price=short_stop,
                        target_fast=tf, target_slow=ts_val,
                        extension_pct=extension_pct,
                        rolling_gain_pct=rolling_gain_pct,
                        green_streak=green_streak,
                        risk_pct=risk_pct,
                    )
                    trades.append(trade)
                    open_trades.append(trade)

                    short_setup_active = False
                    last_short_bar = i
                    short_avwap_num = 0.0
                    short_avwap_den = 0.0
                    short_run_avwap = 0.0

        if short_setup_active and (i - short_setup_bar > cfg.short_setup_timeout):
            short_setup_active = False
            short_avwap_num = 0.0
            short_avwap_den = 0.0
            short_run_avwap = 0.0

        # ═══════════════════════════════════════════════════════════
        # LONG SETUP — V6 DUAL-CHANNEL
        # Channel A: Fast path for deep crashes (First Green Day)
        # Channel B: Wyckoff base breakout for shallower crashes
        # ═══════════════════════════════════════════════════════════

        # ── CHANNEL A: Fast Path (V4-style) ──
        # Activate on deep crash detection
        if (cfg.enable_long and cfg.long_fast_path and washout_detected
                and crash_from_peak >= cfg.fast_path_min_crash
                and not long_fast_active and long_phase == LONG_IDLE
                and (i - last_long_bar > cfg.cooldown_bars)):
            long_fast_active = True
            long_fast_bar = i
            long_fast_washout_low = bar.low
            long_fast_crash_pct = crash_from_peak

        # Track washout low during fast-path active
        if long_fast_active and bar.low < long_fast_washout_low:
            long_fast_washout_low = bar.low

        # Fast path trigger: First Green Day
        if long_fast_active and (i - long_fast_bar) >= cfg.min_bars_after_setup:
            fast_trigger = False
            if cfg.fast_path_trigger == "First Green Day":
                fast_trigger = bar.close > bar.open and i > 0 and bar.close > bars[i - 1].close

            if fast_trigger:
                # ATR-based stop with cap
                current_atr = atr_calc(bars[:i + 1], cfg.atr_len)
                long_stop = bar.close - (current_atr * cfg.atr_mult)
                # Also consider washout low
                struct_stop = long_fast_washout_low * (1 - cfg.stop_buffer / 100)
                # Use the tighter of: ATR stop, washout low, or 2*ATR cap
                atr_cap = bar.close - (current_atr * cfg.long_stop_cap_atr_mult)
                long_stop = max(long_stop, struct_stop, atr_cap)

                risk_pct = ((bar.close - long_stop) / bar.close) * 100 if bar.close > 0 else 0.0

                # V8: Hard-cap LONG stop at max_loss_pct from entry
                if cfg.long_max_stop_pct > 0 and risk_pct > cfg.long_max_stop_pct and bar.close > 0:
                    long_stop = bar.close * (1 - cfg.long_max_stop_pct / 100.0)
                    risk_pct = cfg.long_max_stop_pct

                # V8: Per-ticker LONG cooldown check
                ticker_cooldown_ok = True
                if cfg.long_ticker_cooldown:
                    consec = long_ticker_consec_stops.get(ticker, 0)
                    if consec >= cfg.long_ticker_max_consec_stops:
                        ticker_cooldown_ok = False

                if not ticker_cooldown_ok:
                    long_fast_active = False
                else:
                    r_unit = bar.close - long_stop

                    target_r1 = bar.close + (r_unit * cfg.long_r1_target) if r_unit > 0 else 0.0
                    target_r2 = bar.close + (r_unit * cfg.long_r2_target) if r_unit > 0 else 0.0
                    tf = sma(closes, cfg.target_ma_fast)
                    ts_val = sma(closes, cfg.target_ma_slow)

                    trade = Trade(
                        ticker=ticker, direction="LONG",
                        entry_bar=i, entry_date=bar.date,
                        entry_price=bar.close, stop_price=long_stop,
                        target_fast=tf, target_slow=ts_val,
                        crash_from_peak=long_fast_crash_pct,
                        risk_pct=risk_pct,
                        base_duration=i - long_fast_bar,
                        r_unit=r_unit,
                        target_r1_price=target_r1,
                        target_r2_price=target_r2,
                    )
                    trades.append(trade)
                    open_trades.append(trade)

                    long_fast_active = False
                    last_long_bar = i

        # Fast path timeout
        if long_fast_active and (i - long_fast_bar > cfg.long_setup_timeout):
            long_fast_active = False

        # ── CHANNEL B: Wyckoff Base Breakout ──
        # Activate on crash detection (only if fast path didn't already take it)
        if (cfg.enable_long and washout_detected and long_phase == LONG_IDLE
                and not long_fast_active
                and (i - last_long_bar > cfg.cooldown_bars)):
            long_phase = LONG_AR_FORMING
            long_phase_bar = i
            long_crash_bar = i
            long_sc_low = bar.low
            long_ar_high = bar.high
            long_ar_confirmed = False
            long_ar_pullback_count = 0
            long_spring_detected = False
            long_spring_low = 0.0
            long_spring_bar = -1
            long_atr_at_crash = atr_calc(bars[:i + 1], cfg.atr_len)
            long_crash_bars_slice = [bar]
            long_crash_vols_slice = [bar.volume]

        # PHASE 2: AR Forming — track highest high, confirm when pullback starts
        if long_phase == LONG_AR_FORMING:
            # Track lowest low (SC low) — only in first few bars
            if bar.low < long_sc_low and (i - long_crash_bar) <= 3:
                long_sc_low = bar.low

            # Track highest high (AR high)
            if bar.high > long_ar_high:
                long_ar_high = bar.high
                long_ar_pullback_count = 0

            # Confirm AR when any pullback starts (close below 95% of AR high, or 1 bar below prior close)
            ar_pullback = bar.close < long_ar_high * 0.97 or (i > 0 and bar.close < bars[i-1].close)
            if ar_pullback:
                long_ar_pullback_count += 1

            # Transition to base building after 1 pullback bar OR after 5 bars regardless
            if (long_ar_pullback_count >= 1 or (i - long_crash_bar) >= 5) and not long_ar_confirmed:
                long_ar_confirmed = True
                long_base_low = long_sc_low
                long_base_high = long_ar_high
                long_pivot = long_ar_high
                long_phase = LONG_BASE_BUILDING
                long_phase_bar = i

            long_crash_bars_slice.append(bar)
            long_crash_vols_slice.append(bar.volume)

            # Timeout during AR forming
            if (i - long_crash_bar) > 20:
                long_phase = LONG_IDLE

        # PHASE 3: Base Building — monitor absorption, detect spring
        if long_phase == LONG_BASE_BUILDING:
            long_crash_bars_slice.append(bar)
            long_crash_vols_slice.append(bar.volume)

            # Update base boundaries
            if bar.low < long_base_low:
                long_base_low = bar.low
            if bar.high > long_base_high:
                long_base_high = bar.high
                long_pivot = long_base_high

            # Spring detection (Phase 4 embedded): price undercuts base low then recovers
            current_atr = atr_calc(bars[:i + 1], cfg.atr_len)
            spring_depth = clamp(
                cfg.spring_atr_mult * current_atr,
                bar.close * cfg.spring_depth_floor_pct / 100,
                bar.close * cfg.spring_depth_cap_pct / 100
            )

            if (not long_spring_detected and bar.low < long_sc_low
                    and bar.low >= long_sc_low - spring_depth):
                # Potential spring — check volume is below median (weak selling)
                if rvol_median < 1.0:
                    long_spring_bar = i
                    long_spring_low = bar.low

            # Confirm spring: recovered above SC low within N bars
            if (long_spring_bar >= 0 and not long_spring_detected
                    and (i - long_spring_bar) <= cfg.spring_max_recovery_bars):
                if bar.close > long_sc_low:
                    long_spring_detected = True
                    long_base_low = long_spring_low  # Update structural low

            # Check if spring attempt failed (didn't recover)
            if (long_spring_bar >= 0 and not long_spring_detected
                    and (i - long_spring_bar) > cfg.spring_max_recovery_bars):
                long_spring_bar = -1  # Reset, allow another attempt

            # Check if enough base time has elapsed for breakout
            bars_in_base = i - long_crash_bar
            if bars_in_base >= cfg.min_base_bars:
                # Compute absorption score
                abs_score = compute_absorption_score(
                    long_crash_bars_slice, long_crash_vols_slice,
                    long_base_low, long_atr_at_crash
                )
                if long_spring_detected:
                    abs_score += cfg.spring_score_bonus

                # ATR contraction check — more lenient for deeper crashes
                atr_now = atr_calc(bars[:i + 1], cfg.atr_len)
                atr_contraction = (atr_now / long_atr_at_crash * 100) if long_atr_at_crash > 0 else 100.0

                # Allow breakout readiness if absorption is sufficient
                # ATR contraction is scored, not gated — deeper bases get better scores anyway
                contraction_ok = atr_contraction <= cfg.atr_contraction_pct
                # If absorption is strong enough, waive the contraction requirement
                if abs_score >= cfg.absorption_threshold and (contraction_ok or abs_score >= cfg.absorption_threshold + 0.15):
                    long_phase = LONG_BREAKOUT_READY
                    long_phase_bar = i

            # Also transition to breakout_ready after min_base_bars regardless
            # if we have any positive absorption score (catch fast setups)
            if bars_in_base >= cfg.min_base_bars and long_phase == LONG_BASE_BUILDING:
                abs_score = compute_absorption_score(
                    long_crash_bars_slice, long_crash_vols_slice,
                    long_base_low, long_atr_at_crash
                )
                if long_spring_detected:
                    abs_score += cfg.spring_score_bonus
                if abs_score > 0:
                    long_phase = LONG_BREAKOUT_READY
                    long_phase_bar = i

            # Timeout
            if bars_in_base > cfg.long_base_timeout:
                long_phase = LONG_IDLE

        # PHASE 5: Breakout Ready — check for trigger
        if long_phase == LONG_BREAKOUT_READY:
            long_crash_bars_slice.append(bar)
            long_crash_vols_slice.append(bar.volume)

            # Update base boundaries
            if bar.low < long_base_low:
                long_base_low = bar.low
            if bar.high > long_base_high:
                long_base_high = bar.high
                long_pivot = long_base_high

            # Recompute metrics each bar
            abs_score = compute_absorption_score(
                long_crash_bars_slice, long_crash_vols_slice,
                long_base_low, long_atr_at_crash
            )
            if long_spring_detected:
                abs_score += cfg.spring_score_bonus

            atr_now = atr_calc(bars[:i + 1], cfg.atr_len)
            atr_contraction = (atr_now / long_atr_at_crash * 100) if long_atr_at_crash > 0 else 100.0

            # Breakout conditions — close above pivot OR close above recent range high
            # Use the lower of pivot and recent 5-bar high as the trigger level
            recent_high = max(highs[max(0,i-5):i]) if i > 5 else long_pivot
            trigger_level = min(long_pivot, recent_high)
            breakout_trigger = bar.close > trigger_level

            # Volume confirmation (median-based RVOL)
            vol_confirmed = rvol_median >= cfg.breakout_rvol_min

            # Close strength
            bar_rng = max(bar.high - bar.low, 0.0001)
            close_str = (bar.close - bar.low) / bar_rng
            close_ok = close_str >= cfg.breakout_close_strength

            # Range expansion
            median_tr = median_val(true_ranges, 10) if len(true_ranges) >= 5 else (sum(true_ranges) / len(true_ranges) if true_ranges else 1.0)
            current_tr = true_ranges[-1] if true_ranges else 0
            range_expanded = current_tr >= median_tr * cfg.breakout_range_expansion

            # Regime filter
            regime_ok = True
            if cfg.use_regime_filter and len(closes) >= cfg.regime_ma_len:
                regime_ma = sma(closes, cfg.regime_ma_len)
                # Price above 200MA OR 200MA slope is positive
                ma_slope_ok = False
                if len(closes) >= cfg.regime_ma_len + 5:
                    ma_now = sma(closes, cfg.regime_ma_len)
                    ma_prev = sma(closes[:-5], cfg.regime_ma_len)
                    ma_slope_ok = ma_now > ma_prev
                regime_ok = bar.close > regime_ma or ma_slope_ok

            # Gap exclusion
            gap_ok = True
            if cfg.gap_exclusion_atr_mult > 0 and i > 0:
                gap_size = abs(bar.open - bars[i - 1].close)
                if gap_size > cfg.gap_exclusion_atr_mult * atr_now:
                    gap_ok = False

            # All conditions met?
            if (breakout_trigger and vol_confirmed and close_ok
                    and range_expanded and regime_ok and gap_ok
                    and abs_score >= cfg.absorption_threshold):

                # Structural stop with ATR cap (V6)
                if long_spring_detected and long_spring_low > 0:
                    long_stop = long_spring_low * (1 - cfg.stop_buffer / 100)
                else:
                    long_stop = long_base_low * (1 - cfg.stop_buffer / 100)
                # V6: cap structural stop at 2x ATR from entry
                atr_cap_stop = bar.close - (atr_now * cfg.long_stop_cap_atr_mult)
                long_stop = max(long_stop, atr_cap_stop)

                risk_pct = ((bar.close - long_stop) / bar.close) * 100 if bar.close > 0 else 0.0

                # V8: Hard-cap LONG stop at max_loss_pct from entry
                if cfg.long_max_stop_pct > 0 and risk_pct > cfg.long_max_stop_pct and bar.close > 0:
                    long_stop = bar.close * (1 - cfg.long_max_stop_pct / 100.0)
                    risk_pct = cfg.long_max_stop_pct

                # V8: Per-ticker LONG cooldown check
                ticker_cooldown_ok = True
                if cfg.long_ticker_cooldown:
                    consec = long_ticker_consec_stops.get(ticker, 0)
                    if consec >= cfg.long_ticker_max_consec_stops:
                        ticker_cooldown_ok = False

                if not ticker_cooldown_ok:
                    long_phase = LONG_IDLE
                else:
                    r_unit = bar.close - long_stop

                    # R-based targets
                    target_r1 = bar.close + (r_unit * cfg.long_r1_target) if r_unit > 0 else 0.0
                    target_r2 = bar.close + (r_unit * cfg.long_r2_target) if r_unit > 0 else 0.0

                    tf = sma(closes, cfg.target_ma_fast)
                    ts_val = sma(closes, cfg.target_ma_slow)
                    bars_in_base = i - long_crash_bar

                    trade = Trade(
                        ticker=ticker, direction="LONG",
                        entry_bar=i, entry_date=bar.date,
                        entry_price=bar.close, stop_price=long_stop,
                        target_fast=tf, target_slow=ts_val,
                        crash_from_peak=crash_from_peak,
                        risk_pct=risk_pct,
                        base_duration=bars_in_base,
                        absorption_score=abs_score,
                        spring_detected=long_spring_detected,
                        breakout_rvol=rvol_median,
                        atr_contraction=atr_contraction,
                        r_unit=r_unit,
                        target_r1_price=target_r1,
                        target_r2_price=target_r2,
                    )
                    trades.append(trade)
                    open_trades.append(trade)

                    # Reset
                    long_phase = LONG_IDLE
                    last_long_bar = i
                    long_crash_bars_slice = []
                    long_crash_vols_slice = []

            # Still check for breakdown (lost base) or timeout
            bars_since_ready = i - long_phase_bar
            if bars_since_ready > 20:
                # Re-check if still valid
                if abs_score < cfg.absorption_threshold or atr_contraction > cfg.atr_contraction_pct:
                    long_phase = LONG_IDLE

            total_bars = i - long_crash_bar
            if total_bars > cfg.long_base_timeout:
                long_phase = LONG_IDLE

    # Close remaining open trades
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

def _weighted_avg(trades, attr='pnl_pct'):
    total_wt = sum(t.weight for t in trades)
    if total_wt <= 0:
        return 0.0
    return sum(getattr(t, attr) * t.weight for t in trades) / total_wt


def print_ticker_summary(ticker: str, trades: list):
    if not trades:
        return
    shorts = [t for t in trades if t.direction == "SHORT" and not t.is_runner]
    longs = [t for t in trades if t.direction == "LONG" and not t.is_runner]
    closed = [t for t in trades if t.exit_reason not in ("OPEN_AT_END", "")]
    if not closed:
        return

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

    print(f"  {ticker:<8} | Total: {len(shorts)+len(longs):>3} | Short: {len(shorts):>3} | Long: {len(longs):>3} | "
          f"Legs: {len(closed):>3} | Win%: {win_rate:>6.1f}% | "
          f"Avg PnL: {avg_pnl:>+7.2f}% | Avg Win: {avg_win:>+7.2f}% | Avg Loss: {avg_loss:>+7.2f}% | "
          f"Avg Bars: {avg_bars:>5.1f}")


def print_grand_summary(all_trades: list, label: str = "V5 HYBRID"):
    closed = [t for t in all_trades if t.exit_reason not in ("OPEN_AT_END", "")]
    if not closed:
        print("\n  No closed trades across all tickers.")
        return

    setups = [t for t in all_trades if not t.is_runner]
    n_setups = len(setups)

    shorts = [t for t in closed if t.direction == "SHORT"]
    longs = [t for t in closed if t.direction == "LONG"]

    wins = [t for t in closed if t.pnl_pct > 0]
    losses = [t for t in closed if t.pnl_pct <= 0]

    total_weight = sum(t.weight for t in closed)
    win_weight = sum(t.weight for t in wins)
    loss_weight = sum(t.weight for t in losses)

    total_pnl = sum(t.pnl_pct * t.weight for t in closed)
    avg_pnl = total_pnl / total_weight if total_weight > 0 else 0
    avg_win = _weighted_avg(wins)
    avg_loss = _weighted_avg(losses)
    win_rate = win_weight / total_weight * 100 if total_weight > 0 else 0

    # By direction
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
    # V5 exit reasons
    r1_exits = [t for t in closed if t.exit_reason == "TARGET_R1"]
    r2_exits = [t for t in closed if t.exit_reason == "TARGET_R2"]
    trail_exits = [t for t in closed if t.exit_reason == "TRAIL_STOP"]
    time_stops = [t for t in closed if t.exit_reason == "TIME_STOP"]
    max_loss_exits = [t for t in closed if t.exit_reason == "MAX_LOSS"]

    # Profit factor
    gross_profit = sum(t.pnl_pct * t.weight for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl_pct * t.weight for t in losses)) if losses else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

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

    # Avg R
    r_trades = [t for t in closed if t.risk_pct > 0]
    r_wt = sum(t.weight for t in r_trades)
    avg_r = sum(t.r_multiple * t.weight for t in r_trades) / r_wt if r_wt > 0 else 0

    best = max(closed, key=lambda t: t.pnl_pct * t.weight)
    worst = min(closed, key=lambda t: t.pnl_pct * t.weight)
    avg_bars_held = sum(t.bars_held for t in closed) / len(closed)
    tickers_with_trades = len(set(t.ticker for t in closed))

    print("\n" + "=" * 100)
    print(f"                    GRAND BACKTEST SUMMARY — {label}")
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
    print(f"  Avg Holding Period:      {avg_bars_held:.1f} bars")

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
    nc = len(closed)
    def _reason_line(label, bucket):
        pct = len(bucket) / nc * 100 if nc else 0
        avg = _weighted_avg(bucket) if bucket else 0
        wt = sum(t.weight for t in bucket)
        print(f"  {label:<19} {len(bucket):>4}  ({pct:>5.1f}%)  Wt: {wt:>5.1f}  Avg PnL: {avg:>+7.2f}%")
    _reason_line("STOP:", stops)
    _reason_line("TARGET_10MA:", t10)
    _reason_line("TARGET_10MA_PARTIAL:", t10p)
    _reason_line("TARGET_20MA:", t20)
    _reason_line("STOP_BREAKEVEN:", sbe)
    _reason_line("TIMEOUT:", timeouts)
    _reason_line("TARGET_R1:", r1_exits)
    _reason_line("TARGET_R2:", r2_exits)
    _reason_line("TRAIL_STOP:", trail_exits)
    _reason_line("TIME_STOP:", time_stops)
    _reason_line("MAX_LOSS:", max_loss_exits)

    print(f"\n  ── Extremes ──")
    print(f"  Best Trade:   {best.ticker} {best.direction} {best.entry_date} → {best.exit_date}  PnL: {best.pnl_pct:+.2f}% x{best.weight:.0%}  ({best.exit_reason})")
    print(f"  Worst Trade:  {worst.ticker} {worst.direction} {worst.entry_date} → {worst.exit_date}  PnL: {worst.pnl_pct:+.2f}% x{worst.weight:.0%}  ({worst.exit_reason})")

    # Top/Bottom 10
    sorted_trades = sorted(closed, key=lambda t: t.pnl_pct * t.weight, reverse=True)
    print(f"\n  ── Top 10 Trades ──")
    print(f"  {'Ticker':<8} {'Dir':<6} {'Entry Date':<12} {'Exit Date':<12} {'Entry':>10} {'Exit':>10} {'PnL%':>8} {'Wt':>4} {'R':>6} {'Exit Reason':<20} {'Bars':>5} {'AbsScr':>6} {'Spring':>6}")
    print(f"  {'-'*8} {'-'*6} {'-'*12} {'-'*12} {'-'*10} {'-'*10} {'-'*8} {'-'*4} {'-'*6} {'-'*20} {'-'*5} {'-'*6} {'-'*6}")
    for t in sorted_trades[:10]:
        spr = "Yes" if t.spring_detected else "No" if t.direction == "LONG" else ""
        print(f"  {t.ticker:<8} {t.direction:<6} {t.entry_date:<12} {t.exit_date:<12} "
              f"{t.entry_price:>10.2f} {t.exit_price:>10.2f} {t.pnl_pct:>+7.2f}% {t.weight:>3.0%} {t.r_multiple:>+5.2f}R "
              f"{t.exit_reason:<20} {t.bars_held:>5} {t.absorption_score:>5.2f} {spr:>6}")

    print(f"\n  ── Bottom 10 Trades ──")
    print(f"  {'Ticker':<8} {'Dir':<6} {'Entry Date':<12} {'Exit Date':<12} {'Entry':>10} {'Exit':>10} {'PnL%':>8} {'Wt':>4} {'R':>6} {'Exit Reason':<20} {'Bars':>5}")
    print(f"  {'-'*8} {'-'*6} {'-'*12} {'-'*12} {'-'*10} {'-'*10} {'-'*8} {'-'*4} {'-'*6} {'-'*20} {'-'*5}")
    for t in sorted_trades[-10:]:
        print(f"  {t.ticker:<8} {t.direction:<6} {t.entry_date:<12} {t.exit_date:<12} "
              f"{t.entry_price:>10.2f} {t.exit_price:>10.2f} {t.pnl_pct:>+7.2f}% {t.weight:>3.0%} {t.r_multiple:>+5.2f}R "
              f"{t.exit_reason:<20} {t.bars_held:>5}")

    # Per-ticker breakdown
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

    # V5 LONG-specific metrics
    long_closed = [t for t in closed if t.direction == "LONG" and not t.is_runner]
    if long_closed:
        print(f"\n  ── V5 LONG Setup Context ──")
        avg_base = sum(t.base_duration for t in long_closed) / len(long_closed)
        avg_abs = sum(t.absorption_score for t in long_closed) / len(long_closed)
        spring_pct = sum(1 for t in long_closed if t.spring_detected) / len(long_closed) * 100
        avg_bvol = sum(t.breakout_rvol for t in long_closed) / len(long_closed)
        print(f"  Avg Base Duration:       {avg_base:.1f} bars")
        print(f"  Avg Absorption Score:    {avg_abs:.3f}")
        print(f"  Spring Detected:         {spring_pct:.1f}%")
        print(f"  Avg Breakout RVOL:       {avg_bvol:.2f}x")

    # Complete trade log
    print(f"\n  ── Complete Trade Log ({len(closed)} legs from {n_setups} setups) ──")
    print(f"  {'#':>4} {'Ticker':<8} {'Dir':<6} {'Entry Date':<12} {'Exit Date':<12} {'Entry':>10} {'Stop':>10} {'Exit':>10} {'PnL%':>8} {'Wt':>4} {'R':>6} {'Reason':<20} {'Bars':>5} {'Base':>5} {'Abs':>5}")
    print(f"  {'-'*4} {'-'*8} {'-'*6} {'-'*12} {'-'*12} {'-'*10} {'-'*10} {'-'*10} {'-'*8} {'-'*4} {'-'*6} {'-'*20} {'-'*5} {'-'*5} {'-'*5}")
    for idx, t in enumerate(sorted(closed, key=lambda x: (x.ticker, x.entry_bar, x.is_runner)), 1):
        print(f"  {idx:>4} {t.ticker:<8} {t.direction:<6} {t.entry_date:<12} {t.exit_date:<12} "
              f"{t.entry_price:>10.2f} {t.stop_price:>10.2f} {t.exit_price:>10.2f} "
              f"{t.pnl_pct:>+7.2f}% {t.weight:>3.0%} {t.r_multiple:>+5.2f}R {t.exit_reason:<20} {t.bars_held:>5} "
              f"{t.base_duration:>5} {t.absorption_score:>5.2f}")

    print("\n" + "=" * 100)


# ═══════════════════════════════════════════════════════════════════
# WALK-FORWARD VALIDATION
# ═══════════════════════════════════════════════════════════════════

def _compute_slice_stats(trades):
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

    r_trades = [t for t in closed if t.risk_pct > 0]
    r_wt = sum(t.weight for t in r_trades)
    avg_r = sum(t.r_multiple * t.weight for t in r_trades) / r_wt if r_wt > 0 else 0

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
    longs_list = [t for t in setups if t.direction == "LONG"]

    return {
        'setups': len(setups), 'shorts': len(shorts), 'longs': len(longs_list),
        'legs': len(closed),
        'wr': wr, 'avg_pnl': avg_pnl, 'pf': pf,
        'cum_pnl': total_pnl, 'max_dd': max_dd,
        'avg_r': avg_r, 'expectancy_r': avg_r,
    }


def run_walk_forward(csv_files: list, n_slices: int = 4, cfg: Config = None):
    if cfg is None:
        cfg = Config()

    ticker_bars = []
    for csv_file in csv_files:
        ticker = extract_ticker(csv_file)
        bars_data = load_csv(csv_file)
        ticker_bars.append((ticker, bars_data))

    all_timestamps = []
    for _, bars_data in ticker_bars:
        all_timestamps.extend(b.timestamp for b in bars_data)
    t_min = min(all_timestamps)
    t_max = max(all_timestamps)
    slice_size = (t_max - t_min) / n_slices
    slice_boundaries = [(t_min + int(j * slice_size), t_min + int((j + 1) * slice_size))
                        for j in range(n_slices)]

    print(f"\n  ═══ WALK-FORWARD VALIDATION ({n_slices} time slices) ═══")
    print(f"  Config: long_exit={cfg.long_exit_mode}, long_stop={cfg.long_stop_mode}, "
          f"min_base={cfg.min_base_bars}, abs_thresh={cfg.absorption_threshold}")

    from datetime import datetime as dt_cls
    from datetime import timezone as tz
    for j, (t_start, t_end) in enumerate(slice_boundaries):
        d_start = dt_cls.fromtimestamp(t_start, tz=tz.utc).strftime('%Y-%m-%d')
        d_end = dt_cls.fromtimestamp(t_end, tz=tz.utc).strftime('%Y-%m-%d')
        print(f"  Slice {j+1}: {d_start} → {d_end}")

    all_trades = []
    for ticker, bars_data in ticker_bars:
        t_trades = run_backtest(ticker, bars_data, cfg)
        for t in t_trades:
            if t.exit_bar >= 0 and t.exit_bar < len(bars_data):
                t._exit_ts = bars_data[t.exit_bar].timestamp
            else:
                t._exit_ts = bars_data[-1].timestamp
        all_trades.extend(t_trades)

    slice_results = []
    print(f"\n  {'Slice':>5} {'Period':<25} {'Setups':>6} {'S':>4} {'L':>4} {'WR%':>6} "
          f"{'AvgPnL':>8} {'PF':>6} {'CumPnL':>9} {'MaxDD':>7} {'AvgR':>6}")

    for j, (t_start, t_end) in enumerate(slice_boundaries):
        d_start = dt_cls.fromtimestamp(t_start, tz=tz.utc).strftime('%Y-%m-%d')
        d_end = dt_cls.fromtimestamp(t_end, tz=tz.utc).strftime('%Y-%m-%d')

        slice_trades = [t for t in all_trades if t_start <= t._exit_ts < t_end]
        stats = _compute_slice_stats(slice_trades)
        if stats is None:
            print(f"  {j+1:>5} {d_start} → {d_end:<14} {'(no trades)':>6}")
            slice_results.append(None)
            continue

        slice_results.append(stats)
        print(f"  {j+1:>5} {d_start} → {d_end:<14} {stats['setups']:>6} {stats['shorts']:>4} {stats['longs']:>4} "
              f"{stats['wr']:>5.1f}% {stats['avg_pnl']:>+7.2f}% {stats['pf']:>5.2f} "
              f"{stats['cum_pnl']:>+8.1f}% {stats['max_dd']:>6.1f}% {stats['avg_r']:>+5.2f}R")

    full_stats = _compute_slice_stats(all_trades)
    if full_stats:
        print(f"  {'FULL':>5} {'(all periods)':<25} {full_stats['setups']:>6} {full_stats['shorts']:>4} {full_stats['longs']:>4} "
              f"{full_stats['wr']:>5.1f}% {full_stats['avg_pnl']:>+7.2f}% {full_stats['pf']:>5.2f} "
              f"{full_stats['cum_pnl']:>+8.1f}% {full_stats['max_dd']:>6.1f}% {full_stats['avg_r']:>+5.2f}R")

    valid_slices = [s for s in slice_results if s is not None]
    if valid_slices:
        pfs = [s['pf'] for s in valid_slices]
        avg_pnls = [s['avg_pnl'] for s in valid_slices]
        profitable_slices = sum(1 for s in valid_slices if s['pf'] > 1.0)
        pf_above_1_1 = sum(1 for s in valid_slices if s['pf'] > 1.1)

        print(f"\n  ── Stability Assessment ──")
        print(f"  Profitable slices (PF>1.0):  {profitable_slices}/{len(valid_slices)}")
        print(f"  Robust slices (PF>1.1):      {pf_above_1_1}/{len(valid_slices)}")
        print(f"  PF range:                    {min(pfs):.2f} – {max(pfs):.2f}")
        print(f"  Avg PnL range:               {min(avg_pnls):+.2f}% – {max(avg_pnls):+.2f}%")

        if profitable_slices == len(valid_slices) and pf_above_1_1 >= len(valid_slices) // 2:
            print(f"  VERDICT: PASS — edge is stable across time periods")
        elif profitable_slices >= len(valid_slices) * 0.5:
            print(f"  VERDICT: MARGINAL — edge present in some periods but not consistent")
        else:
            print(f"  VERDICT: FAIL — no stable edge across time periods")

    return slice_results


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Run V5 Hybrid Wyckoff/CAN SLIM backtest")
    parser.add_argument("--data-dir", help="Directory containing OHLC CSV files")
    parser.add_argument("--extra-data", nargs="*", help="V8: Additional data directories to scan")
    parser.add_argument("--walk-forward", action="store_true", help="Run walk-forward validation")
    parser.add_argument("--forward-test", action="store_true",
                        help="Split data 70/30 for in-sample/out-of-sample comparison")
    parser.add_argument("--long-only", action="store_true", help="Run LONG side only")
    parser.add_argument("--compare", action="store_true", help="Run both V4 and V5 for comparison")

    args = parser.parse_args()

    candidates = []
    if args.data_dir:
        candidates.append(args.data_dir)
    candidates.extend([
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs2"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs2"),
        "/home/user/logs2",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "30-tix-chart-data"),
        "/home/user/30-tix-chart-data",
    ])

    data_dir = next((d for d in candidates if os.path.isdir(d)), None)
    if not data_dir:
        print("ERROR: Data directory not found.")
        sys.exit(1)

    # V8: Collect extra data directories
    extra_dirs = args.extra_data or []

    csv_files = sorted(glob.glob(os.path.join(data_dir, "*.csv")))
    # V8: Merge CSVs from extra data directories (dedup by ticker)
    seen_tickers = set(extract_ticker(f) for f in csv_files)
    for edir in extra_dirs:
        if os.path.isdir(edir):
            for f in sorted(glob.glob(os.path.join(edir, "*.csv"))):
                tkr = extract_ticker(f)
                if tkr not in seen_tickers:
                    csv_files.append(f)
                    seen_tickers.add(tkr)
    csv_files = sorted(csv_files, key=lambda f: extract_ticker(f))
    if not csv_files:
        print(f"ERROR: No CSV files found in {data_dir}")
        sys.exit(1)

    cfg = Config()
    if args.long_only:
        cfg.enable_short = False

    if args.forward_test:
        # V6: fixed calendar date cutoff for all tickers (not per-ticker 70/30)
        cutoff_date = cfg.forward_test_date
        print("=" * 100)
        print(f"  V6 HYBRID — FORWARD TEST (IS: before {cutoff_date} / OOS: {cutoff_date}+)")
        print("=" * 100)
        print(f"\n  Data directory: {data_dir}")
        print(f"  Tickers found:  {len(csv_files)}")
        print(f"  Cutoff date:    {cutoff_date}")

        is_trades = []
        oos_trades = []

        for csv_file in csv_files:
            ticker = extract_ticker(csv_file)
            bars_data = load_csv(csv_file)
            if len(bars_data) < 200:
                continue

            all_t = run_backtest(ticker, bars_data, cfg)
            for t in all_t:
                if t.entry_date < cutoff_date:
                    is_trades.append(t)
                else:
                    oos_trades.append(t)

        print(f"\n  ═══ IN-SAMPLE (before {cutoff_date}) ═══")
        print_grand_summary(is_trades, label=f"V6 HYBRID — IS (before {cutoff_date})")

        print(f"\n  ═══ OUT-OF-SAMPLE ({cutoff_date}+) ═══")
        print_grand_summary(oos_trades, label=f"V6 HYBRID — OOS ({cutoff_date}+)")
        return

    if args.walk_forward:
        run_walk_forward(csv_files, n_slices=4, cfg=cfg)
        return

    # Default: full backtest
    print("=" * 100)
    print("  Parabolic Mean Reversion V5 — Hybrid Wyckoff/CAN SLIM Backtester")
    print("=" * 100)
    print(f"\n  Data directory: {data_dir}")
    print(f"  Tickers found:  {len(csv_files)}")
    print(f"\n  V6 Configuration:")
    print(f"    ── SHORT ──")
    print(f"    Short Enabled:         {cfg.enable_short}")
    print(f"    Short Split Exit:      {cfg.short_split_exit} (V6: runner killed)")
    print(f"    Short Time Stop:       {cfg.short_time_stop} bars (V6: was 50)")
    print(f"    Circuit Breaker:       {cfg.short_circuit_breaker} (trailing {cfg.short_circuit_lookback} trades, min WR {cfg.short_circuit_min_wr}%)")
    print(f"    Min Reward:            {cfg.short_min_reward_pct}% (V8: 10MA must be >= N% below entry)")
    print(f"    ── LONG ──")
    print(f"    Long Enabled:          {cfg.enable_long}")
    print(f"    Long Fast Path:        {cfg.long_fast_path} (>= {cfg.fast_path_min_crash}% crash → close > prior HIGH)")
    print(f"    Long Base Path:        min {cfg.min_base_bars} bars, abs >= {cfg.absorption_threshold}")
    print(f"    Stop Cap:              {cfg.long_stop_cap_atr_mult}x ATR / max {cfg.long_max_stop_pct}%")
    print(f"    Ticker Cooldown:       {cfg.long_ticker_cooldown} (skip after {cfg.long_ticker_max_consec_stops} consec stops)")
    print(f"    Long Exit Mode:        {cfg.long_exit_mode}")
    print(f"    R1/R2 Targets:         {cfg.long_r1_target}R / {cfg.long_r2_target}R")
    print(f"    Long Time Stop:        {cfg.long_time_stop} bars")
    print(f"    Trail Channel:         {cfg.long_trail_channel} bars")
    print(f"    Max Loss Cap:          {cfg.max_loss_pct}% (V8: force exit if unrealized loss exceeds)")
    print(f"    Forward Test Date:     {cfg.forward_test_date}")

    print(f"\n  ── Per-Ticker Results ──")
    print(f"  {'Ticker':<8} | {'Total':>6} | {'Short':>6} | {'Long':>6} | {'Legs':>7} | {'Win%':>7} | "
          f"{'Avg PnL':>9} | {'Avg Win':>9} | {'Avg Loss':>9} | {'Avg Bars':>9}")
    print(f"  {'-' * 100}")

    all_trades = []
    for csv_file in csv_files:
        ticker = extract_ticker(csv_file)
        bars_data = load_csv(csv_file)
        t_trades = run_backtest(ticker, bars_data, cfg)
        all_trades.extend(t_trades)
        print_ticker_summary(ticker, t_trades)

    print_grand_summary(all_trades, label="V5 HYBRID")


if __name__ == "__main__":
    main()
