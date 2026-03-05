#!/usr/bin/env python3
"""
Statistical Significance Analysis for Parabolic Mean Reversion
==============================================================
Two-stage methodology:
  STAGE 1 — Development set (tradable subset: price>$5, avg_vol>100k, bars>500)
    Iterate/confirm edge on this smaller, realistic subset.
  STAGE 2 — Holdout validation (everything NOT in dev set)
    Touched once. If PF holds here, edge is real and not overfit.

Analyses on each stage:
  1. Time-split OOS (70/30 chronological train/test)
  2. Block bootstrap by calendar quarter (PF 95% CI, P(PF>1))
  3. Leave-one-ticker-out & leave-one-year-out sensitivity
  4. Combined verdict

Config under test: "tight + risk15"
  - min_ext_above_ma = 40%
  - max_risk_pct = 15.0%

Uses only Python stdlib.
"""

import sys
import os
import random
import argparse
import glob as globmod
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest import (
    Config, load_csv, extract_ticker, run_backtest, _weighted_avg,
)


# ═══════════════════════════════════════════════════════════════════
# CONFIG UNDER TEST
# ═══════════════════════════════════════════════════════════════════

def tight_risk15_config() -> Config:
    return Config(
        min_ext_above_ma=40.0,
        max_risk_pct=15.0,
    )


# ═══════════════════════════════════════════════════════════════════
# UNIVERSE SPLIT — dev set vs holdout
# ═══════════════════════════════════════════════════════════════════

def split_universe(csv_files, min_price=5.0, min_avg_vol=100_000, min_bars=500):
    """Split files into dev (tradable) and holdout (rest).
    Dev criteria: avg close > min_price, avg volume > min_avg_vol, bars > min_bars.
    """
    dev_files = []
    holdout_files = []
    dev_tickers = []
    holdout_tickers = []

    for f in csv_files:
        ticker = extract_ticker(f)
        bars = load_csv(f)
        if len(bars) < min_bars:
            holdout_files.append(f)
            holdout_tickers.append(ticker)
            continue

        recent = bars[-50:]
        avg_close = sum(b.close for b in recent) / len(recent)
        avg_vol = sum(b.volume for b in recent) / len(recent)

        if avg_close >= min_price and avg_vol >= min_avg_vol:
            dev_files.append(f)
            dev_tickers.append(ticker)
        else:
            holdout_files.append(f)
            holdout_tickers.append(ticker)

    return dev_files, holdout_files, dev_tickers, holdout_tickers


def preload_bars(csv_files):
    """Load bars for all files, return list of (ticker, bars)."""
    result = []
    for f in csv_files:
        ticker = extract_ticker(f)
        bars = load_csv(f)
        result.append((ticker, bars))
    return result


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def run_all_trades(ticker_bars, cfg):
    """Run backtest across all tickers, attach timestamps to trades."""
    all_trades = []
    for ticker, bars in ticker_bars:
        trades = run_backtest(ticker, bars, cfg)
        for t in trades:
            if t.exit_bar >= 0 and t.exit_bar < len(bars):
                t._exit_ts = bars[t.exit_bar].timestamp
                t._exit_date = bars[t.exit_bar].date
            else:
                t._exit_ts = bars[-1].timestamp
                t._exit_date = bars[-1].date
            if t.entry_bar < len(bars):
                t._entry_ts = bars[t.entry_bar].timestamp
        all_trades.extend(trades)
    return all_trades


def compute_stats(trades):
    """Key stats from a list of trades."""
    if not trades:
        return None
    closed = [t for t in trades if t.exit_reason not in ("OPEN_AT_END", "")]
    if not closed:
        return None

    setups = [t for t in trades if not t.is_runner]
    wins = [t for t in closed if t.pnl_pct > 0]
    losses = [t for t in closed if t.pnl_pct <= 0]

    total_wt = sum(t.weight for t in closed)
    win_wt = sum(t.weight for t in wins)
    wr = win_wt / total_wt * 100 if total_wt > 0 else 0

    total_pnl = sum(t.pnl_pct * t.weight for t in closed)
    avg_pnl = total_pnl / total_wt if total_wt > 0 else 0

    gp = sum(t.pnl_pct * t.weight for t in wins)
    gl = abs(sum(t.pnl_pct * t.weight for t in losses))
    pf = gp / gl if gl > 0 else float('inf')

    # Max drawdown
    cum = peak = max_dd = 0.0
    for t in sorted(closed, key=lambda x: (x.exit_bar, -x.is_runner)):
        cum += t.pnl_pct * t.weight
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    # Avg R
    r_trades = [t for t in closed if t.risk_pct > 0]
    r_wt = sum(t.weight for t in r_trades)
    avg_r = sum(t.r_multiple * t.weight for t in r_trades) / r_wt if r_wt > 0 else 0

    return {
        'setups': len(setups), 'legs': len(closed),
        'wr': wr, 'avg_pnl': avg_pnl, 'pf': pf,
        'cum_pnl': total_pnl, 'max_dd': max_dd, 'avg_r': avg_r,
        'gross_profit': gp, 'gross_loss': gl,
    }


def print_stats_line(label, stats):
    if stats is None:
        print(f"  {label}: No trades")
        return
    print(f"  {label}: Setups={stats['setups']:>4}  WR={stats['wr']:.1f}%  "
          f"AvgPnL={stats['avg_pnl']:+.2f}%  PF={stats['pf']:.3f}  "
          f"CumPnL={stats['cum_pnl']:+.1f}%  MaxDD={stats['max_dd']:.1f}%  "
          f"AvgR={stats['avg_r']:+.2f}R")


# ═══════════════════════════════════════════════════════════════════
# ANALYSIS 1: TIME-SPLIT OOS (70/30)
# ═══════════════════════════════════════════════════════════════════

def analysis_time_split(all_trades, train_pct=0.70, label=""):
    """Chronological train/test split. Takes pre-computed trades."""
    print(f"\n  ── Time-Split OOS ({train_pct:.0%}/{1-train_pct:.0%}) {label} ──")

    closed = [t for t in all_trades if t.exit_reason not in ("OPEN_AT_END", "")]
    if not closed:
        print("  No closed trades.")
        return None, None

    all_ts = [t._exit_ts for t in closed]
    t_min, t_max = min(all_ts), max(all_ts)
    split_ts = t_min + int((t_max - t_min) * train_pct)

    train = [t for t in all_trades if hasattr(t, '_exit_ts') and t._exit_ts < split_ts]
    test = [t for t in all_trades if hasattr(t, '_exit_ts') and t._exit_ts >= split_ts]

    train_s = compute_stats(train)
    test_s = compute_stats(test)

    split_d = datetime.fromtimestamp(split_ts, tz=timezone.utc).strftime('%Y-%m-%d')
    t_min_d = datetime.fromtimestamp(t_min, tz=timezone.utc).strftime('%Y-%m-%d')
    t_max_d = datetime.fromtimestamp(t_max, tz=timezone.utc).strftime('%Y-%m-%d')

    print(f"  Period: {t_min_d} → {t_max_d}  Split: {split_d}")
    print_stats_line("TRAIN", train_s)
    print_stats_line("TEST ", test_s)

    if test_s:
        verdict = "PASS" if test_s['pf'] > 1.0 else "FAIL"
        print(f"  OOS: {verdict} (Test PF={test_s['pf']:.3f})")

    return train_s, test_s


# ═══════════════════════════════════════════════════════════════════
# ANALYSIS 2: BLOCK BOOTSTRAP (calendar-quarter blocks)
# ═══════════════════════════════════════════════════════════════════

def analysis_block_bootstrap(all_trades, n_iter=5000, block_months=3,
                              seed=42, label=""):
    """Block bootstrap resampling by calendar quarter. Takes pre-computed trades."""
    print(f"\n  ── Block Bootstrap (n={n_iter}, block={block_months}mo) {label} ──")

    closed = [t for t in all_trades if t.exit_reason not in ("OPEN_AT_END", "")]
    if not closed:
        print("  No closed trades.")
        return None

    # Group into quarterly blocks
    def block_key(t):
        y = int(t._exit_date[:4])
        m = int(t._exit_date[5:7])
        return f"{y}-Q{(m-1)//block_months}"

    blocks = defaultdict(list)
    for t in closed:
        blocks[block_key(t)].append(t)
    keys = sorted(blocks.keys())
    n_blocks = len(keys)

    full_s = compute_stats(all_trades)
    print(f"  Full-sample: PF={full_s['pf']:.3f}  WR={full_s['wr']:.1f}%  "
          f"AvgPnL={full_s['avg_pnl']:+.2f}%")
    print(f"  Blocks: {n_blocks} ({keys[0]} → {keys[-1]}), "
          f"trades/block: {min(len(blocks[k]) for k in keys)}-{max(len(blocks[k]) for k in keys)}")

    rng = random.Random(seed)
    pfs, avg_pnls = [], []

    for _ in range(n_iter):
        sampled = []
        for _ in range(n_blocks):
            sampled.extend(blocks[rng.choice(keys)])
        if not sampled:
            continue

        wins = [t for t in sampled if t.pnl_pct > 0]
        losses = [t for t in sampled if t.pnl_pct <= 0]
        gp = sum(t.pnl_pct * t.weight for t in wins)
        gl = abs(sum(t.pnl_pct * t.weight for t in losses))
        pf = gp / gl if gl > 0 else 10.0
        pfs.append(min(pf, 10.0))  # cap outliers

        tw = sum(t.weight for t in sampled)
        avg_pnls.append(sum(t.pnl_pct * t.weight for t in sampled) / tw if tw > 0 else 0)

    pfs.sort()
    avg_pnls.sort()

    def pctl(lst, p):
        return lst[max(0, min(int(len(lst) * p), len(lst) - 1))]

    ci_lo, ci_hi = pctl(pfs, 0.025), pctl(pfs, 0.975)
    pf_med = pctl(pfs, 0.5)
    prob_gt1 = sum(1 for x in pfs if x > 1.0) / len(pfs)
    ap_lo, ap_hi = pctl(avg_pnls, 0.025), pctl(avg_pnls, 0.975)
    prob_ap_gt0 = sum(1 for x in avg_pnls if x > 0) / len(avg_pnls)

    print(f"  PF:     median={pf_med:.3f}  95% CI=[{ci_lo:.3f}, {ci_hi:.3f}]  P(PF>1)={prob_gt1:.1%}")
    print(f"  AvgPnL: 95% CI=[{ap_lo:+.2f}%, {ap_hi:+.2f}%]  P(>0)={prob_ap_gt0:.1%}")

    if ci_lo >= 1.0:
        print(f"  Bootstrap: STRONG (lower CI >= 1.0)")
    elif prob_gt1 >= 0.90:
        print(f"  Bootstrap: GOOD (P(PF>1) >= 90%)")
    elif prob_gt1 >= 0.70:
        print(f"  Bootstrap: MARGINAL")
    else:
        print(f"  Bootstrap: WEAK")

    return {'pf_ci_low': ci_lo, 'pf_ci_high': ci_hi, 'pf_median': pf_med,
            'prob_pf_gt_1': prob_gt1, 'avg_pnl_ci_low': ap_lo, 'avg_pnl_ci_high': ap_hi}


# ═══════════════════════════════════════════════════════════════════
# ANALYSIS 3: LEAVE-ONE-OUT SENSITIVITY
# ═══════════════════════════════════════════════════════════════════

def analysis_leave_one_out(all_trades, label=""):
    """Leave-one-ticker-out and leave-one-year-out. Takes pre-computed trades."""
    print(f"\n  ── Leave-One-Out Sensitivity {label} ──")

    closed = [t for t in all_trades if t.exit_reason not in ("OPEN_AT_END", "")]
    if not closed:
        print("  No closed trades.")
        return None

    full_s = compute_stats(all_trades)
    print(f"  Full: PF={full_s['pf']:.3f}  Setups={full_s['setups']}")

    # Leave-one-ticker-out
    tickers = sorted(set(t.ticker for t in closed))
    print(f"\n  Leave-one-TICKER-out ({len(tickers)} tickers):")
    print(f"  {'Removed':<10} {'Setups':>6} {'WR%':>6} {'PF':>6} {'dPF':>7}")

    ticker_pfs = []
    for tkr in tickers:
        remaining = [t for t in all_trades if t.ticker != tkr]
        s = compute_stats(remaining)
        if s is None:
            continue
        dpf = s['pf'] - full_s['pf']
        print(f"  {tkr:<10} {s['setups']:>6} {s['wr']:>5.1f}% {s['pf']:>5.2f} {dpf:>+6.3f}")
        ticker_pfs.append((tkr, s['pf'], dpf))

    if ticker_pfs:
        pfs = [x[1] for x in ticker_pfs]
        below1 = [x[0] for x in ticker_pfs if x[1] < 1.0]
        worst = min(ticker_pfs, key=lambda x: x[1])
        print(f"  PF range: {min(pfs):.3f}–{max(pfs):.3f}")
        if below1:
            print(f"  WARNING: PF < 1.0 when removing: {', '.join(below1)}")
        else:
            print(f"  PASS: No single ticker removal drops PF < 1.0")

    # Leave-one-year-out
    years = sorted(set(t._exit_date[:4] for t in closed if hasattr(t, '_exit_date')))
    print(f"\n  Leave-one-YEAR-out ({len(years)} years):")
    print(f"  {'Year':>6} {'Setups':>6} {'WR%':>6} {'PF':>6} {'dPF':>7}")

    year_pfs = []
    for yr in years:
        remaining = [t for t in all_trades
                     if not (hasattr(t, '_exit_date') and t._exit_date[:4] == yr)]
        s = compute_stats(remaining)
        if s is None:
            continue
        dpf = s['pf'] - full_s['pf']
        print(f"  {yr:>6} {s['setups']:>6} {s['wr']:>5.1f}% {s['pf']:>5.2f} {dpf:>+6.3f}")
        year_pfs.append((yr, s['pf'], dpf))

    if year_pfs:
        pfs = [x[1] for x in year_pfs]
        below1 = [x[0] for x in year_pfs if x[1] < 1.0]
        print(f"  PF range: {min(pfs):.3f}–{max(pfs):.3f}")
        if below1:
            print(f"  WARNING: PF < 1.0 when removing: {', '.join(below1)}")
        else:
            print(f"  PASS: No single year removal drops PF < 1.0")

    # Per-year breakdown
    print(f"\n  Per-year performance:")
    print(f"  {'Year':>6} {'Setups':>6} {'WR%':>6} {'AvgPnL':>8} {'PF':>6} {'CumPnL':>9}")
    for yr in years:
        yr_trades = [t for t in all_trades
                     if hasattr(t, '_exit_date') and t._exit_date[:4] == yr]
        s = compute_stats(yr_trades)
        if s:
            print(f"  {yr:>6} {s['setups']:>6} {s['wr']:>5.1f}% "
                  f"{s['avg_pnl']:>+7.2f}% {s['pf']:>5.2f} {s['cum_pnl']:>+8.1f}%")

    return {
        'ticker_pfs': ticker_pfs,
        'year_pfs': year_pfs,
    }


# ═══════════════════════════════════════════════════════════════════
# COMBINED VERDICT
# ═══════════════════════════════════════════════════════════════════

def verdict(oos, bootstrap, loo, stage_name):
    """Synthesize all tests into pass/fail."""
    print(f"\n  ── VERDICT: {stage_name} ──")
    checks = []

    if oos:
        _, test_s = oos
        if test_s:
            checks.append(("OOS PF > 1.0", test_s['pf'] > 1.0, f"PF={test_s['pf']:.3f}"))

    if bootstrap:
        ci = bootstrap['pf_ci_low']
        p = bootstrap['prob_pf_gt_1']
        checks.append(("Bootstrap CI low >= 0.90", ci >= 0.90,
                       f"CI=[{ci:.3f}, {bootstrap['pf_ci_high']:.3f}]"))
        checks.append(("P(PF>1) >= 80%", p >= 0.80, f"{p:.1%}"))

    if loo:
        for kind, data in [("ticker", loo.get('ticker_pfs', [])),
                           ("year", loo.get('year_pfs', []))]:
            if data:
                pfs = [x[1] for x in data]
                checks.append((f"No {kind} drops PF < 1.0",
                               all(pf >= 1.0 for pf in pfs),
                               f"min={min(pfs):.3f}"))

    passed = sum(1 for _, p, _ in checks if p)
    for name, p, detail in checks:
        print(f"  {'PASS' if p else 'FAIL':>4}  {name:<35} {detail}")
    print(f"\n  Score: {passed}/{len(checks)}")

    if passed == len(checks):
        print(f"  ==> STRONG: Robust edge confirmed on {stage_name}")
    elif passed >= len(checks) * 0.7:
        print(f"  ==> MODERATE: Edge likely real, proceed with caution")
    elif passed >= len(checks) * 0.5:
        print(f"  ==> WEAK: Edge fragile")
    else:
        print(f"  ==> FAIL: Insufficient evidence")

    return passed, len(checks)


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Two-stage stat significance: dev set → holdout validation")
    parser.add_argument("--data-dir", help="Data directory override")
    parser.add_argument("--bootstrap-n", type=int, default=5000,
                        help="Bootstrap iterations (default: 5000)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--train-pct", type=float, default=0.70)
    parser.add_argument("--min-price", type=float, default=5.0,
                        help="Min avg close for dev set (default: $5)")
    parser.add_argument("--min-vol", type=float, default=100_000,
                        help="Min avg volume for dev set (default: 100k)")
    parser.add_argument("--min-bars", type=int, default=500,
                        help="Min bar count for dev set (default: 500)")
    parser.add_argument("--dev-only", action="store_true",
                        help="Run only Stage 1 (dev set) — skip holdout")
    parser.add_argument("--holdout-only", action="store_true",
                        help="Run only Stage 2 (holdout) — skip dev set analysis")
    args = parser.parse_args()

    # Data discovery
    candidates = []
    if args.data_dir:
        candidates.append(args.data_dir)
    candidates.extend([
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs2"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs2"),
        "/home/user/logs2",
    ])
    data_dir = next((d for d in candidates if os.path.isdir(d)), None)
    if not data_dir:
        print("ERROR: No data directory found.")
        sys.exit(1)

    csv_files = sorted(
        globmod.glob(os.path.join(data_dir, "**", "*.csv"), recursive=True) +
        globmod.glob(os.path.join(data_dir, "**", "*.txt"), recursive=True)
    )
    if not csv_files:
        print(f"ERROR: No data files in {data_dir}")
        sys.exit(1)

    print("=" * 90)
    print("  STATISTICAL SIGNIFICANCE — Two-Stage Validation")
    print("  Config: tight+risk15 (ext=40%, risk_cap=15%)")
    print("=" * 90)
    print(f"\n  Data: {data_dir} ({len(csv_files)} files)")
    print(f"  Dev filter: price >= ${args.min_price}, vol >= {args.min_vol:,.0f}, bars >= {args.min_bars}")

    # Split universe
    print(f"\n  Splitting universe...")
    dev_files, holdout_files, dev_tickers, holdout_tickers = split_universe(
        csv_files, min_price=args.min_price, min_avg_vol=args.min_vol, min_bars=args.min_bars)

    print(f"  Dev set:     {len(dev_files):>5} tickers (tradable subset)")
    print(f"  Holdout set: {len(holdout_files):>5} tickers (untouched validation)")

    cfg = tight_risk15_config()

    # ═══════════════════════════════════════════════════════════════
    # STAGE 1: DEV SET
    # ═══════════════════════════════════════════════════════════════
    if not args.holdout_only:
        print("\n" + "=" * 90)
        print("  STAGE 1: DEVELOPMENT SET (tradable subset)")
        print("=" * 90)

        print(f"\n  Loading dev bars...")
        dev_bars = preload_bars(dev_files)
        total_bars = sum(len(b) for _, b in dev_bars)
        print(f"  Loaded {len(dev_bars)} tickers, {total_bars:,} bars")

        # Baseline comparison
        print(f"\n  ── Baseline Comparison ──")
        default_trades = run_all_trades(dev_bars, Config())
        s_default = compute_stats(default_trades)
        print_stats_line("Default (ext=20, no cap)", s_default)

        print(f"  Computing tight+risk15 trades...")
        dev_trades = run_all_trades(dev_bars, cfg)
        s_tight = compute_stats(dev_trades)
        print_stats_line("Tight+Risk15 (ext=40, cap=15%)", s_tight)

        # Run all analyses on pre-computed dev trades (no re-running backtest)
        oos_dev = analysis_time_split(dev_trades, args.train_pct, label="[DEV]")
        boot_dev = analysis_block_bootstrap(dev_trades, args.bootstrap_n, seed=args.seed,
                                            label="[DEV]")
        loo_dev = analysis_leave_one_out(dev_trades, label="[DEV]")
        dev_passed, dev_total = verdict(oos_dev, boot_dev, loo_dev, "STAGE 1 — DEV SET")

        if args.dev_only:
            print("\n  (--dev-only: skipping holdout)")
            print("\n" + "=" * 90)
            return

    # ═══════════════════════════════════════════════════════════════
    # STAGE 2: HOLDOUT (touched ONCE)
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 90)
    print("  STAGE 2: HOLDOUT VALIDATION (untouched set)")
    print("=" * 90)

    print(f"\n  Loading holdout bars...")
    holdout_bars = preload_bars(holdout_files)
    total_bars = sum(len(b) for _, b in holdout_bars)
    print(f"  Loaded {len(holdout_bars)} tickers, {total_bars:,} bars")

    print(f"  Computing holdout trades...")
    holdout_trades = run_all_trades(holdout_bars, cfg)
    holdout_stats = compute_stats(holdout_trades)
    print_stats_line("Holdout full", holdout_stats)

    oos_ho = analysis_time_split(holdout_trades, args.train_pct, label="[HOLDOUT]")
    boot_ho = analysis_block_bootstrap(holdout_trades, args.bootstrap_n, seed=args.seed,
                                       label="[HOLDOUT]")
    loo_ho = analysis_leave_one_out(holdout_trades, label="[HOLDOUT]")
    ho_passed, ho_total = verdict(oos_ho, boot_ho, loo_ho, "STAGE 2 — HOLDOUT")

    # ═══════════════════════════════════════════════════════════════
    # FINAL
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 90)
    print("  FINAL COMBINED ASSESSMENT")
    print("=" * 90)
    if not args.holdout_only:
        print(f"  Stage 1 (Dev):     {dev_passed}/{dev_total} checks")
    print(f"  Stage 2 (Holdout): {ho_passed}/{ho_total} checks")

    if not args.holdout_only and dev_passed == dev_total and ho_passed >= ho_total * 0.7:
        print(f"\n  ==> CONFIRMED: Edge holds on both dev and holdout sets.")
        print(f"      Safe to commit tight+risk15 as default with conservative sizing.")
    elif not args.holdout_only and dev_passed >= dev_total * 0.7 and ho_passed >= ho_total * 0.5:
        print(f"\n  ==> PROBABLE: Edge likely real but some holdout weakness.")
        print(f"      Consider tighter position sizing or additional filters.")
    else:
        print(f"\n  ==> INCONCLUSIVE or FAIL: Do not deploy without further investigation.")

    print("\n" + "=" * 90)


if __name__ == "__main__":
    main()
