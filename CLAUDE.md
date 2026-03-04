# CLAUDE.md — Parabolic Mean Reversion

## Project Overview

A **mean-reversion trading system** for detecting parabolic stock advances (short setups) and washout crashes (long setups) on daily timeframes. The system is a **candidate-ranking screener**, not an execution engine — it identifies setups for manual intraday execution.

**Two components:**
- **Pine Script V1 indicator** (`Parabolic_Snapback_Washout_V1.pine`) — TradingView screener/overlay for live markets
- **Python backtest engine** (`backtest.py`) — validates the strategy against historical OHLC CSV data

## Repository Structure

```
Parabolic_MeanReversion/
├── Parabolic_Snapback_Washout_V1.pine   # Pine Script v6 indicator (production)
├── backtest.py                           # Python backtest engine (CLI tool)
├── backtest_results.txt                  # Sample results from 49 large-cap tickers
├── Parabolic_MeanReversion_V1_Final_Plan (2).md  # Architecture plan (V1 + V1.5 notes)
└── CLAUDE.md                             # This file
```

External data directory: `../logs2/` (or `--data-dir` override) containing OHLC CSV files with columns `time,open,high,low,close`.

## Key Concepts

### Strategy Logic (Both Pine & Python)

1. **Parabolic Advance Detection (SHORT side):** Rolling gain over lookback window >= threshold, consecutive green days, extension above MA, optional BB/trend gates
2. **Washout Crash Detection (LONG side):** Price crash >= threshold from recent peak within a velocity window, verified prior parabolic run
3. **State machines:** IDLE → SETUP_ACTIVE → SETUP_TRIGGERED → IDLE (with timeout, cooldown)
4. **Risk management:** Stop placement (Run Peak / Trigger Bar High / ATR / Washout Low), ADR-based stop width ceiling, targets at 10 MA and 20 MA

### Important Distinctions

- Labels always say "SETUP" — never "SIGNAL" or "ENTRY"
- V1 is a daily screener; V1.5 (not yet built) handles intraday execution
- "Run AVWAP" (V1, anchored from advance start) is distinct from "Session VWAP" (V1.5, standard intraday)
- Volume gates are **bypassed** in the Python backtester because CSV data lacks volume

## Development Workflow

### Running the Backtest

```bash
# Auto-discovers data in ../logs2/, ./logs2/, or /home/user/logs2/
python3 backtest.py

# Explicit data directory
python3 backtest.py --data-dir /path/to/csv/data
```

**CSV format required:** Headers must be `time,open,high,low,close`. The `time` column is a Unix timestamp. Filenames follow the pattern `EXCHANGE_TICKER, 1D (N).csv` (e.g., `NASDAQ_AAPL, 1D (1).csv`).

### No Test Suite or Linter

There are no automated tests, linters, or CI pipelines configured. Validation is done by running the backtest against the CSV dataset and inspecting the output.

### Pine Script Development

The Pine Script (`Parabolic_Snapback_Washout_V1.pine`) uses **Pine Script v6** and is deployed on TradingView. It cannot be run locally. Changes to the Pine Script should maintain parity with the backtest engine logic.

## Code Architecture

### backtest.py

| Section | Lines | Purpose |
|---------|-------|---------|
| `Config` dataclass | 33–101 | All strategy parameters with defaults (calibrated for large-cap backtesting) |
| `Bar` / `Trade` dataclasses | 108–144 | Data structures for OHLC bars and trade records |
| Helper functions | 151–211 | `sma()`, `atr_calc()`, `bb_upper()`, `load_csv()`, `extract_ticker()` |
| `run_backtest()` | 218–632 | Core engine — detection, state machines, trade tracking, exit logic |
| Reporting functions | 639–821 | Per-ticker summaries, grand summary, trade logs |
| `main()` | 828–907 | CLI entry point with argparse, data discovery, orchestration |

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

The Python backtester uses **calibrated thresholds** for its 49-ticker large-cap universe, which differ from the Pine Script defaults designed for scanning thousands of stocks:

| Parameter | Pine Script | Backtest | Reason |
|-----------|-------------|----------|--------|
| `largecap_gain_pct` | 60% | 40% | Large-cap stocks have smaller moves |
| `min_ext_above_ma` | 30% | 20% | Same reason |
| `min_crash_pct` | 50% | 30% | Large-cap crashes are less extreme |
| `crash_window` | 5 bars | 15 bars | Crashes unfold slower in large-caps |
| `prior_run_min_pct` | 100% | 40% | Prior runs are smaller |
| `max_stop_vs_adr` | 1.0x | 2.5x | Wider stops for parabolic peaks |
| `setup_timeout` | 5 bars | 10 bars | More time for reversal |
| `min_adr_pct` | 2.0% | 1.0% | Stable large-caps have lower ADR |

## Conventions for AI Assistants

### When Modifying Strategy Logic
- Changes to detection, state machine, or risk logic should be reflected in **both** the Pine Script and the Python backtester to maintain parity
- The architecture plan document is the source of truth for intended behavior
- Section 14 of the plan documents defensive coding requirements (AVWAP reset hygiene, early-bar guards, loop index arithmetic) — follow these

### When Modifying backtest.py
- The `Config` dataclass is the single source for all tunable parameters
- Trade resolution order matters: check STOP before TARGET before TIMEOUT
- All trades must eventually close (end-of-data uses `OPEN_AT_END`)
- The backtester has no external dependencies beyond Python stdlib

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
- No volume data in the backtest CSV files — all volume-dependent gates are bypassed in `backtest.py`
- The backtest ran on 49 large-cap tickers producing 33 closed trades with a 60.6% win rate and 0.98 profit factor
