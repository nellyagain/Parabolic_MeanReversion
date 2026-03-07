# CLAUDE.md — Parabolic Mean Reversion

## Project Overview

A **mean-reversion trading system** for detecting parabolic stock advances (short setups) and washout crashes (long setups) on daily timeframes. The system is a **candidate-ranking screener**, not an execution engine — it identifies setups for manual intraday execution.

**Three components:**
- **Pine Script V1 indicator** (`Parabolic_Snapback_Washout_V1.pine`) — TradingView screener/overlay for live markets
- **Python backtest engine V1** (`backtest.py`) — validates the V1 strategy against historical OHLC data
- **Python backtest engine V5** (`backtest_v5.py`) — advanced Hybrid Wyckoff/CAN SLIM backtester with 5-phase long-side state machine

## Repository Structure

```
Parabolic_MeanReversion/
├── CLAUDE.md                                          # This file
├── .gitignore                                         # Ignores __pycache__/, *.pyc, data/
├── Parabolic_Snapback_Washout_V1.pine                 # Pine Script v6 indicator (production)
├── Parabolic_MeanReversion_V1_Final_Plan (2).md       # Architecture plan (V1 + V1.5 notes)
│
├── backtest.py                                        # V1 backtest engine (1919 lines)
├── backtest_v5.py                                     # V5 Hybrid Wyckoff/CAN SLIM engine (2422 lines)
│
├── comprehensive_test_suite.py                        # Validation suite (imports backtest.py)
├── generate_synthetic_data.py                         # Synthetic OHLC data generator
├── stat_significance.py                               # Statistical significance analysis (bootstrap, OOS)
│
├── synthetic_data/                                    # Generated synthetic OHLC data (50 tickers)
│
├── backtest_results.txt                               # V1 results — 49 large-cap tickers
├── backtest_results_v2_split_exit.txt                 # V2 split-exit results
├── backtest_results_v3_volume.txt                     # V3 volume-gate results
├── backtest_results_v4.txt                            # V4 comparison (V2 vs V4 configs)
├── sweep_results_v4.txt                               # V4 parameter sweep results
└── test_suite_results.txt                             # Comprehensive test suite output
```

External data directory: `../logs2/`, `./data/`, `./logs2/`, or `/home/user/logs2/` (auto-discovered, or `--data-dir` override) containing OHLC CSV/TXT files.

## Key Concepts

### Strategy Logic (Both Pine & Python)

1. **Parabolic Advance Detection (SHORT side):** Rolling gain over lookback window >= threshold, consecutive green days, extension above MA, optional BB/trend gates
2. **Washout Crash Detection (LONG side):** Price crash >= threshold from recent peak within a velocity window, verified prior parabolic run, selling climax via RVOL
3. **State machines:** IDLE → SETUP_ACTIVE → SETUP_TRIGGERED → IDLE (with timeout, cooldown)
4. **Risk management:** Stop placement (Run Peak / Trigger Bar High / ATR / Structural), ADR-based stop width ceiling, targets at 10 MA and 20 MA

### V5 Long-Side Enhancements (backtest_v5.py only)

V5 replaces the simple "First Green Day" long trigger with a 5-phase Wyckoff/CAN SLIM state machine:
1. **Phase 1:** Crash Detection (velocity + selling climax)
2. **Phase 2:** Automatic Rally (AR) identification
3. **Phase 3:** Base Formation with absorption scoring
4. **Phase 4:** Optional Spring detection (additive to score)
5. **Phase 5:** Breakout trigger with volume + close strength + range expansion

Additional V5 features: dual-channel entry (fast path for deep crashes, Wyckoff base for shallow), RVOL via median baseline, ATR-based dynamic thresholds, regime filter (200 MA), gap exclusion, R-based partial exits, trailing channel stop, per-ticker cooldown, short circuit breaker, short minimum reward filter.

### Important Distinctions

- Labels always say "SETUP" — never "SIGNAL" or "ENTRY"
- V1 is a daily screener; V1.5 (not yet built) handles intraday execution
- "Run AVWAP" (V1, anchored from advance start) is distinct from "Session VWAP" (V1.5, standard intraday)
- Volume gates are **bypassed** in backtest.py when CSV data lacks volume; backtest_v5.py includes RVOL computation

## Development Workflow

### Running the Backtests

```bash
# V1 backtest — auto-discovers data
python3 backtest.py

# V1 with explicit data directory
python3 backtest.py --data-dir /path/to/csv/data

# V1 analysis modes
python3 backtest.py --sweep                # Parameter sweep
python3 backtest.py --short-stop-sweep     # SHORT stop mode sweep
python3 backtest.py --walk-forward         # Walk-forward OOS validation
python3 backtest.py --risk-sim             # Risk-normalized equity simulation
python3 backtest.py --full-analysis        # All of the above

# V5 backtest
python3 backtest_v5.py --data-dir /path/to/data
```

**CSV format required:** Headers must include `time,open,high,low,close` (and optionally `volume`). The `time` column is a Unix timestamp. Filenames follow patterns like `EXCHANGE_TICKER, 1D (N).csv` or `TICKER.US.txt`.

### Running the Test Suite

```bash
# With synthetic data (included in repo)
python3 comprehensive_test_suite.py --data-dir synthetic_data

# With real data
python3 comprehensive_test_suite.py --data-dir /path/to/real/data
```

The test suite covers: engine correctness, strategy logic, out-of-sample validation, robustness (parameter perturbation, feature ablation), execution realism, portfolio risk, statistical confidence, regime analysis, and forward testing. It imports from `backtest.py`.

### Generating Synthetic Data

```bash
python3 generate_synthetic_data.py
```

Generates OHLC+Volume data with embedded parabolic advances and washout crashes. Output format is V8 TXT (MetaTrader-style). Data is stored in `synthetic_data/`.

### Statistical Significance Analysis

```bash
python3 stat_significance.py --data-dir /path/to/data
```

Two-stage methodology: development set (tradable universe) and holdout validation. Includes time-split OOS, block bootstrap, leave-one-ticker-out, and leave-one-year-out sensitivity. Tests the "tight + risk15" config variant (min_ext_above_ma=40%, max_risk_pct=15%).

### Pine Script Development

The Pine Script (`Parabolic_Snapback_Washout_V1.pine`) uses **Pine Script v6** and is deployed on TradingView. It cannot be run locally. Changes to the Pine Script should maintain parity with the backtest engine logic.

### No CI Pipeline

There are no linters or CI pipelines configured. Validation is done by running the backtest against the dataset and inspecting output, and by running the comprehensive test suite.

## Code Architecture

### backtest.py (V1 — 1919 lines)

| Section | Lines | Purpose |
|---------|-------|---------|
| `Config` dataclass | 32–132 | All strategy parameters with defaults (calibrated for large-cap backtesting) |
| `Bar` / `Trade` dataclasses | 138–180 | Data structures for OHLC bars and trade records |
| Helper functions | ~180–315 | `sma()`, `atr_calc()`, `bb_upper()`, `load_csv()`, `extract_ticker()` |
| `run_backtest()` | 316–1032 | Core engine — detection, state machines, trade tracking, exit logic |
| Reporting & analysis | 1033–1784 | Grand summary, walk-forward, risk sim, sweep, trade logs |
| `main()` | 1785–1919 | CLI entry point with argparse, data discovery, orchestration |

Key V1 additions beyond initial version: split exit (partial at 10MA, runner to 20MA), risk-cap gate (`max_risk_pct`), tradability gates (`min_trade_price`, `min_dollar_vol`), structural long filters (pre-crash uptrend, entry confirmation), parameter sweep and walk-forward modes.

### backtest_v5.py (V5 Hybrid — 2422 lines)

| Section | Lines | Purpose |
|---------|-------|---------|
| `Config` dataclass | 34–159 | Extended config with V5–V8 parameters (dual-channel, Wyckoff, circuit breaker) |
| Data structures | ~160–250 | `Bar`, `Trade`, `WyckoffState` dataclasses |
| Helper functions | ~250–403 | Includes median RVOL, absorption scoring |
| `run_backtest()` | 404–1452 | Extended engine with 5-phase Wyckoff long-side state machine |
| Reporting & analysis | 1453–2279 | Grand summary, sweep, comparison tools |
| `main()` | 2280–2422 | CLI entry with V5-specific options |

### comprehensive_test_suite.py (1405 lines)

Imports `backtest.py` and runs 9 test categories against loaded ticker data. No external dependencies. Tests include: engine correctness (no look-ahead, execution timing), strategy logic audits, out-of-sample 3-way split, robustness (perturbation, ablation), execution realism (spread, gap-through-stop), portfolio risk metrics, statistical bootstrap/Monte Carlo, regime analysis, and forward tests.

### Pine Script Sections

| Section | Purpose |
|---------|---------|
| 1 | Inputs (grouped by feature) |
| 2 | Parabolic Advance Detection Engine |
| 3 | Climax Volume Detection |
| 4 | Washout Crash Detection |
| 5 | Run AVWAP (Context) |
| 6 | Short Setup State Machine |
| 7 | Long Setup State Machine |
| 8 | Risk Management (stops, targets, R-multiples) |
| 9 | Visuals (labels, stop lines, bar coloring, background shading) |
| 10 | Pine Screener Outputs (`SCR:` prefixed `display.none` plots) |
| 11 | Alerts |

## Configuration Defaults (Backtest vs Pine Script)

The Python backtester uses **calibrated thresholds** for its large-cap universe, which differ from the Pine Script defaults designed for scanning thousands of stocks:

| Parameter | Pine Script | Backtest (V1) | Reason |
|-----------|-------------|----------------|--------|
| `largecap_gain_pct` | 60% | 40% | Large-cap stocks have smaller moves |
| `min_ext_above_ma` | 30% | 20% | Same reason |
| `min_crash_pct` | 50% | 30% | Large-cap crashes are less extreme |
| `crash_window` | 5 bars | 15 bars | Crashes unfold slower in large-caps |
| `prior_run_min_pct` | 100% | 40% | Prior runs are smaller |
| `max_stop_vs_adr` | 1.0x | 1.5x | Wider stops for parabolic peaks |
| `setup_timeout` | 5 bars | 10 bars | More time for reversal |
| `min_adr_pct` | 2.0% | 1.0% | Stable large-caps have lower ADR |

V5 (`backtest_v5.py`) introduces further tuning: `max_stop_vs_adr` tightened to 1.2x, `min_ext_above_ma` raised to 30%, short circuit breaker enabled, short minimum reward filter at 3%, long ticker cooldown after 3 consecutive stops.

## Conventions for AI Assistants

### When Modifying Strategy Logic
- Changes to detection, state machine, or risk logic should be reflected in **both** the Pine Script and the relevant Python backtester to maintain parity
- The architecture plan document is the source of truth for intended behavior
- Section 14 of the plan documents defensive coding requirements (AVWAP reset hygiene, early-bar guards, loop index arithmetic) — follow these
- V5's long-side Wyckoff logic is independent from V1/Pine Script — it does not need parity

### When Modifying backtest.py
- The `Config` dataclass is the single source for all tunable parameters
- Trade resolution order matters: check STOP before TARGET before TIMEOUT
- All trades must eventually close (end-of-data uses `OPEN_AT_END`)
- The backtester has no external dependencies beyond Python stdlib
- `comprehensive_test_suite.py` and `stat_significance.py` both import from `backtest.py` — changes to exported symbols (`Config`, `Bar`, `Trade`, `run_backtest`, `load_csv`, `extract_ticker`, `sma`, `atr_calc`, `print_grand_summary`, `_weighted_avg`, `_compute_slice_stats`) may break these modules

### When Modifying backtest_v5.py
- V5 is self-contained (does not import from `backtest.py`)
- The `Config` dataclass has significantly more parameters than V1, organized with version comments (V5–V8)
- SHORT side logic is identical to V4/V1; only LONG side uses the Wyckoff state machine
- Includes dual-channel entry: fast path (deep crashes, First Green Day) and base path (Wyckoff breakout)

### When Modifying the Pine Script
- Uses Pine Script **v6** (`//@version=6`)
- All state variables use `var` for persistence across bars
- Screener outputs are `display=display.none` and prefixed with `SCR:`
- Always clear AVWAP accumulators on both trigger reset and timeout paths (Section 14.1)
- Gate detection logic with early-bar guards (Section 14.2)
- Use "SETUP" language in labels/alerts, never "SIGNAL" or "ENTRY"

### General
- This is a mean-reversion system — defaults reflect counter-trend mechanics
- Short and Long sides are independent dual-mode state machines (not mutually exclusive)
- No volume data in the original backtest CSV files — all volume-dependent gates are bypassed in `backtest.py`
- The `synthetic_data/` directory contains 50 generated tickers with embedded parabolic/washout patterns for testing
- All Python scripts use only the standard library (no pip dependencies)
