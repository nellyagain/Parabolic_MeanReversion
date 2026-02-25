# Parabolic Mean Reversion — Final Architecture Plan (V1 + V1.5)

---

## Scope & Naming

**V1: "Parabolic Snapback & Washout [Daily Screener V1]"**
- Daily timeframe candidate detection and setup-state tracking
- Screener-first design for scanning large watchlists
- Labels mark **SETUPS**, not signals — execution is manual on intraday charts
- This is a **candidate-ranking scanner**, not a full execution indicator

**V1.5: "Parabolic Execution [Intraday Companion]"** *(separate script, built later)*
- 5-minute chart execution triggers (ORL/ORH breaks, VWAP fail state machine)
- Session VWAP (not anchored run VWAP)
- Converts V1 flagged candidates into actual entries
- Where the documented edge lives

This document covers **V1 only**, with V1.5 architecture notes at the end for continuity.

---

## Design Philosophy

- **Mean-reversion, not trend-continuation.** Every default reflects counter-trend mechanics.
- **Dual-mode.** Short (Snapback) and Long (Washout Bounce) run simultaneously with independent state machines. Not mutually exclusive like V18's structure modes.
- **Honest about what daily can and cannot do.** Daily detection finds candidates. Intraday microstructure confirms entries. V1 handles layer 1; V1.5 handles layer 2.
- **Screener-first.** The primary workflow is: scan thousands of stocks daily → rank by extension/RVOL/crash depth → drill down to 5-min for execution.
- **Risk controls are not optional.** ADR gate, liquidity filters, and position sizing guardrails are core to the edge, not add-on features.

---

## 1. INPUTS

### Group: "Parabolic SHORT — Advance Detection"

| Input | Type | Default | Description |
|---|---|---|---|
| `enableShort` | bool | true | Enable Parabolic Short setups |
| `largecapGainPct` | float | 60.0 | Min % gain threshold (large-cap) |
| `smallcapGainPct` | float | 300.0 | Min % gain threshold (small-cap) |
| `capCutoffPrice` | float | 20.0 | Price proxy: above = large-cap thresholds, below = small-cap |
| `useManualThreshold` | bool | false | Override cap proxy — use single fixed threshold |
| `manualGainPct` | float | 80.0 | Fixed gain threshold when manual mode enabled |
| `gainLookback` | int | 20 | Rolling lookback for gain measurement (bars) |
| `minGreenDays` | int | 3 | Min consecutive green daily closes |
| `minExtAboveMA` | float | 30.0 | Min extension above MA (%) |
| `extMA_Len` | int | 20 | Extension baseline MA length |
| `useExtBBFilter` | bool | false | Also require close > upper Bollinger Band? |
| `bbDevMult` | float | 3.0 | BB standard deviation multiplier |

### Group: "Parabolic LONG — Washout Detection"

| Input | Type | Default | Description |
|---|---|---|---|
| `enableLong` | bool | true | Enable Washout Bounce setups |
| `minCrashPct` | float | 50.0 | Min % decline from peak |
| `crashWindow` | int | 5 | Max bars for crash (velocity gate) |
| `requirePriorRun` | bool | **true** | Require prior parabolic advance before crash |
| `priorRunMinPct` | float | 100.0 | Prior run min % gain to qualify |
| `priorRunLookback` | int | 40 | Lookback from peak to find run start low |

### Group: "Climax Volume"

| Input | Type | Default | Description |
|---|---|---|---|
| `useClimaxVol` | bool | true | Require climax volume for setup activation |
| `rvolThreshold` | float | **3.0** | Min Relative Volume (RVOL) for climax confirmation |
| `rvolBaseline` | int | 20 | RVOL baseline SMA length |
| `climaxWindowBars` | int | **1** | Max bars between climax volume and peak for alignment |
| `useVolumeChurn` | bool | false | Detect volume churn (high vol + narrow range) |
| `churnRangeMax` | float | 50.0 | Churn: max bar range as % of ATR |
| `useVolExpansionScore` | bool | false | Track volume expansion across green streak (ranking, not gate) |

### Group: "Liquidity Filters"

| Input | Type | Default | Description |
|---|---|---|---|
| `minPrice` | float | 5.0 | Minimum stock price |
| `minAvgDollarVol` | float | 20.0 | Min avg daily dollar volume (millions) |
| `dollarVolLen` | int | 20 | Dollar volume SMA length |
| `minADR_Pct` | float | 2.0 | Min ADR % (filters dead/illiquid names) |

### Group: "Setup Trigger (Daily Proxy)"

| Input | Type | Default | Description |
|---|---|---|---|
| `shortTrigger` | string | **"Close < Prior Low"** | Daily proxy for intraday ORL break |
| | | | Options: "First Red Day", "Close < Prior Low", "Close < Run AVWAP", "Any Reversal" |
| `longTrigger` | string | **"First Green Day"** | Daily proxy for intraday ORH break |
| | | | Options: "First Green Day", "Close > Prior High", "Close > Run AVWAP", "Any Reversal" |
| `showRunAVWAP` | bool | true | Show Run AVWAP (context) line |
| `minBarsAfterSetup` | int | **0** | Min bars between setup activation and trigger |

### Group: "Targets"

| Input | Type | Default | Description |
|---|---|---|---|
| `targetMode` | string | "Dual (10 & 20)" | Target MA display mode |
| | | | Options: "10 MA", "20 MA", "Dual (10 & 20)" |
| `targetMA_Fast` | int | 10 | Fast target MA length |
| `targetMA_Slow` | int | 20 | Slow target MA length |
| `showTargetLines` | bool | true | Plot target MA lines on chart |

### Group: "Trend Context"

| Input | Type | Default | Description |
|---|---|---|---|
| `useTrendFilter` | bool | **false** | OFF by default — counter-trend setups |
| `trendMA_Len` | int | 50 | Trend MA length |

### Group: "Quality Gates"

| Input | Type | Default | Description |
|---|---|---|---|
| `minCloseStrength` | float | 0.0 | Min close strength (0–1) |
| `useADRFilter` | bool | **true** | Enforce ADR-based stop width ceiling |
| `adrLen` | int | 20 | ADR calculation length |
| `maxStopVsADR` | float | 1.0 | Max stop width as multiple of ADR |

### Group: "Risk Management"

| Input | Type | Default | Description |
|---|---|---|---|
| `shortStopMode` | string | "Run Peak" | Short stop placement |
| | | | Options: "Run Peak", "Trigger Bar High", "ATR Based" |
| `longStopMode` | string | "Washout Low" | Long stop placement |
| | | | Options: "Washout Low", "ATR Based" |
| `stopBuffer` | float | 0.2 | Stop buffer % |
| `atrLen` | int | 14 | ATR length (for ATR-based stops) |
| `atrMult` | float | 2.0 | ATR multiplier |
| `maxLineAge` | int | **50** | Max stop line age (bars) |

### Group: "Timeouts & Cooldown"

| Input | Type | Default | Description |
|---|---|---|---|
| `shortSetupTimeout` | int | 5 | Bars after climax before setup expires |
| `longSetupTimeout` | int | 5 | Bars after washout low before setup expires |
| `cooldownBars` | int | 3 | Min bars between same-direction setups |

---

## 2. PARABOLIC ADVANCE DETECTION ENGINE

### Liquidity Gate (front-gate — runs first, rejects junk)

```
avgDollarVol = ta.sma(close * volume, dollarVolLen) / 1e6  // in millions
dailyRangePct = ((high / low) - 1) * 100
adrPct = ta.sma(dailyRangePct, adrLen)

liquidityOk = close >= minPrice
              AND avgDollarVol >= minAvgDollarVol
              AND adrPct >= minADR_Pct
```

### Rolling Gain

```
recentLow = ta.lowest(low, gainLookback)
rollingGainPct = ((close - recentLow) / recentLow) * 100

// Adaptive threshold
gainThreshold = useManualThreshold ? manualGainPct :
                (close >= capCutoffPrice ? largecapGainPct : smallcapGainPct)
isParabolicGain = rollingGainPct >= gainThreshold
```

### Consecutive Green Days

```
isGreenDay = close > close[1]
var int greenStreak = 0
greenStreak := isGreenDay ? greenStreak + 1 : 0
hasGreenStreak = greenStreak >= minGreenDays
```

### Extension Above MA

```
extMA = ta.sma(close, extMA_Len)
extensionPct = ((close - extMA) / extMA) * 100
isExtended = extensionPct >= minExtAboveMA
```

### Bollinger Band Gate (optional)

```
[bbMid, bbUpper, bbLower] = ta.bb(close, extMA_Len, bbDevMult)
bbOk = useExtBBFilter ? close > bbUpper : true
```

### Combined Detection

```
parabolicAdvanceDetected = liquidityOk AND isParabolicGain AND hasGreenStreak
                           AND isExtended AND bbOk
```

---

## 3. CLIMAX VOLUME DETECTION

```
volBaseline = ta.sma(volume, rvolBaseline)
rvol = volume / nz(volBaseline, 1)
isClimaxVolume = rvol >= rvolThreshold

// Volume churn: huge volume but tight range = absorption
atrBase = ta.atr(14)
barRange = high - low
rangeVsATR = atrBase > 0 ? (barRange / atrBase) * 100 : 100
isChurning = useVolumeChurn ? (rvol >= rvolThreshold AND rangeVsATR <= churnRangeMax) : false

climaxVolOnBar = isClimaxVolume OR isChurning

// Track last climax bar for alignment window
var int lastClimaxBar = na
if climaxVolOnBar
    lastClimaxBar := bar_index

// Gate: climax must be within N bars of current bar for activation
climaxAligned = not na(lastClimaxBar) AND (bar_index - lastClimaxBar <= climaxWindowBars)

climaxVolOk = useClimaxVol ? climaxAligned : true
```

**Key change from prior revision:** Climax alignment is now explicit via `climaxWindowBars` input and `lastClimaxBar` state tracking. The setup only activates when advance criteria AND climax volume co-occur within the defined window, making behavior deterministic and testable.

### Volume Expansion Score (optional ranking metric)

```
// Count consecutive days where volume increased during the green streak
// Soft ranking metric — NOT a gate
var int volExpansionCount = 0
if isGreenDay and volume > volume[1]
    volExpansionCount := volExpansionCount + 1
else if not isGreenDay
    volExpansionCount := 0

// Score: how many of the green streak days had expanding volume
volExpansionScore = useVolExpansionScore ? volExpansionCount : 0
```

---

## 4. WASHOUT CRASH DETECTION (for Long side)

### Peak and Crash Measurement

```
// Find recent peak using native function (no loop — screener-efficient)
peakOffset = ta.highestbars(high, priorRunLookback)  // returns negative offset
peakBarIdx = bar_index + peakOffset
recentPeakHigh = high[math.abs(peakOffset)]

// Measure crash
barsFromPeak = bar_index - peakBarIdx
crashFromPeak = recentPeakHigh > 0 ? ((recentPeakHigh - close) / recentPeakHigh) * 100 : 0.0

isCrashCandidate = crashFromPeak >= minCrashPct AND barsFromPeak <= crashWindow
```

### Prior Parabolic Run Verification (tightened window)

```
// Measure prior run using a FIXED window behind the peak only
// Do NOT expand beyond priorRunLookback — avoids pulling in stale lows
hadPriorRun = true
if requirePriorRun and not na(peakBarIdx)
    barsBack = bar_index - peakBarIdx
    // Look back from the peak, constrained to priorRunLookback
    // Find lowest low in the window [peakBar - priorRunLookback ... peakBar]
    float priorRunLow = recentPeakHigh
    for i = barsBack to math.min(barsBack + priorRunLookback, bar_index) - 1
        if low[i] < priorRunLow
            priorRunLow := low[i]
    priorRunGain = priorRunLow > 0 ? ((recentPeakHigh - priorRunLow) / priorRunLow) * 100 : 0
    hadPriorRun := priorRunGain >= priorRunMinPct
```

**Key change from prior revision:** The prior-run window is now strictly bounded to `priorRunLookback` bars before the peak. It no longer dynamically expands, preventing contamination from unrelated older lows in different market regimes.

### Selling Climax Signature (contextual, for screener ranking — not a hard gate)

```
crashVolSpike = rvol >= rvolThreshold AND close < open
lowerWick = math.min(open, close) - low
barRng = math.max(high - low, syminfo.mintick)
hammerRatio = (lowerWick / barRng) * 100
isSellingClimax = crashVolSpike OR hammerRatio > 50
```

### Combined Detection

```
washoutDetected = liquidityOk AND isCrashCandidate AND hadPriorRun
```

---

## 5. RUN AVWAP (CONTEXT)

### Naming Distinction

| Concept | Definition | Script |
|---|---|---|
| **Run AVWAP** | Anchored from start of parabolic advance (short) or from peak/crash origin (long). Contextual mean reference. | V1 |
| **Session VWAP** | Standard intraday VWAP recalculated each session. Used for execution triggers (VWAP fail / reclaim). | V1.5 |

These are different concepts serving different purposes. V1's Run AVWAP is useful for:
- Visual reference showing institutional cost basis of the move
- Confluence with daily trigger options
- Screener metric (% distance from Run AVWAP)

It is **not** a substitute for the intraday VWAP fail/reclaim logic.

### Anchor Logic

**For SHORT setups:** Anchored from the **exact lowest bar** within the gain lookback window (the run's starting point).

```
// Use ta.lowestbars for precise anchor
lowestBarOffset = ta.lowestbars(low, gainLookback)  // returns negative offset
advanceStartBar = bar_index + lowestBarOffset
advanceStartLow = low[math.abs(lowestBarOffset)]
```

**For LONG setups:** Anchored from the **recent peak** (the crash origin). This gives the institutional cost basis of the decline phase — the relevant mean for a washout bounce.

```
// Peak already tracked in Section 4
longAVWAP_AnchorBar = peakBarIdx
longAVWAP_AnchorPrice = recentPeakHigh
```

**Key change from prior revision:** AVWAP anchor for shorts uses `ta.lowestbars` for exact bar identification instead of the approximate `bar_index - gainLookback`. Long-side AVWAP is explicitly anchored from the peak (Option A from review), not the advance start.

### Accumulation

```
// Short-side Run AVWAP
var float shortAvwapNum = 0.0
var float shortAvwapDen = 0.0
var float shortRunAVWAP = na

if shortSetupActive and bar_index >= advanceStartBar
    src = ohlc4
    shortAvwapNum += src * volume
    shortAvwapDen += volume
    shortRunAVWAP := shortAvwapDen > 0 ? shortAvwapNum / shortAvwapDen : na

// Long-side Run AVWAP (anchored from peak)
var float longAvwapNum = 0.0
var float longAvwapDen = 0.0
var float longRunAVWAP = na

if longSetupActive and not na(longAVWAP_AnchorBar) and bar_index >= longAVWAP_AnchorBar
    src = ohlc4
    longAvwapNum += src * volume
    longAvwapDen += volume
    longRunAVWAP := longAvwapDen > 0 ? longAvwapNum / longAvwapDen : na

// Unified reference for plotting / triggers
runAVWAP = shortSetupActive ? shortRunAVWAP :
           longSetupActive  ? longRunAVWAP  : na
```

---

## 6. SHORT SETUP STATE MACHINE

```
State: IDLE → SETUP_ACTIVE → SETUP_TRIGGERED → IDLE

var bool  shortSetupActive  = false
var int   shortSetupBar     = na
var float parabolicPeak     = na
var int   parabolicPeakBar  = na
var float advanceStartLow   = na
var int   advanceStartBar   = na
var int   lastShortBar      = na   // cooldown tracking

// ── ACTIVATION ──
if enableShort AND parabolicAdvanceDetected AND climaxVolOk
   AND not shortSetupActive
   AND (na(lastShortBar) OR bar_index - lastShortBar > cooldownBars)

    shortSetupActive := true
    shortSetupBar    := bar_index
    parabolicPeak    := high
    parabolicPeakBar := bar_index

    // Precise AVWAP anchor using ta.lowestbars
    lowestOffset     := ta.lowestbars(low, gainLookback)
    advanceStartBar  := bar_index + lowestOffset
    advanceStartLow  := low[math.abs(lowestOffset)]

    // Initialize AVWAP accumulation
    shortAvwapNum    := 0.0
    shortAvwapDen    := 0.0

// ── PEAK TRACKING ──
if shortSetupActive AND high > parabolicPeak
    parabolicPeak    := high
    parabolicPeakBar := bar_index

// ── TRIGGER ──
// Enforce minimum bars between activation and trigger
setupMature = (bar_index - shortSetupBar) >= minBarsAfterSetup

shortEntryProxy = switch shortTrigger
    "First Red Day"      => close < open AND close < close[1]
    "Close < Prior Low"  => close < low[1]
    "Close < Run AVWAP"  => not na(shortRunAVWAP) AND close < shortRunAVWAP
    "Any Reversal"       => (close < low[1]) OR
                            (close < open AND close < close[1]) OR
                            (not na(shortRunAVWAP) AND close < shortRunAVWAP)

// Close strength for shorts: close near low = strong red candle
barRng = math.max(high - low, syminfo.mintick)
shortCloseStr = (high - close) / barRng
shortCloseOk  = shortCloseStr >= minCloseStrength

// ADR gate
shortStop = ... // per Section 8
shortStopWidth = math.abs(shortStop - close) / close * 100
adrOk = useADRFilter ? shortStopWidth <= (adrPct * maxStopVsADR) : true

shortSetupTriggered = shortSetupActive AND setupMature AND shortEntryProxy
                      AND shortCloseOk AND adrOk

// ── RESET ──
if shortSetupTriggered
    shortSetupActive := false
    lastShortBar     := bar_index

// ── TIMEOUT ──
if shortSetupActive AND (bar_index - shortSetupBar > shortSetupTimeout)
    shortSetupActive := false
```

---

## 7. LONG SETUP STATE MACHINE

```
State: IDLE → WASHOUT_ACTIVE → SETUP_TRIGGERED → IDLE

var bool  longSetupActive  = false
var int   longSetupBar     = na
var float washoutLow       = na
var int   washoutLowBar    = na
var int   lastLongBar      = na

// ── ACTIVATION ──
if enableLong AND washoutDetected AND not longSetupActive
   AND (na(lastLongBar) OR bar_index - lastLongBar > cooldownBars)

    longSetupActive := true
    longSetupBar    := bar_index
    washoutLow      := low
    washoutLowBar   := bar_index

    // Initialize long-side AVWAP from peak
    longAvwapNum    := 0.0
    longAvwapDen    := 0.0

// ── LOW TRACKING ──
if longSetupActive AND low < washoutLow
    washoutLow    := low
    washoutLowBar := bar_index

// ── TRIGGER ──
longSetupMature = (bar_index - longSetupBar) >= minBarsAfterSetup

longEntryProxy = switch longTrigger
    "First Green Day"    => close > open AND close > close[1]
    "Close > Prior High" => close > high[1]
    "Close > Run AVWAP"  => not na(longRunAVWAP) AND close > longRunAVWAP
    "Any Reversal"       => (close > high[1]) OR
                            (close > open AND close > close[1])

longCloseStr = (close - low) / barRng
longCloseOk  = longCloseStr >= minCloseStrength

longStop = ... // per Section 8
longStopWidth = math.abs(close - longStop) / close * 100
longAdrOk = useADRFilter ? longStopWidth <= (adrPct * maxStopVsADR) : true

longSetupTriggered = longSetupActive AND longSetupMature AND longEntryProxy
                     AND longCloseOk AND longAdrOk

// ── RESET ──
if longSetupTriggered
    longSetupActive := false
    lastLongBar     := bar_index

// ── TIMEOUT ──
if longSetupActive AND (bar_index - longSetupBar > longSetupTimeout)
    longSetupActive := false
```

---

## 8. RISK MANAGEMENT

### ADR Calculation (corrected formula)

```
dailyRangePct = ((high / low) - 1) * 100
adrPct = ta.sma(dailyRangePct, adrLen)
```

### Short Stop

```
shortStopPrice = switch shortStopMode
    "Run Peak"         => parabolicPeak * (1 + stopBuffer / 100)
    "Trigger Bar High" => high * (1 + stopBuffer / 100)
    "ATR Based"        => close + (ta.atr(atrLen) * atrMult)

shortEntry = close
shortRiskPct = ((shortStopPrice - shortEntry) / shortEntry) * 100
```

**Stop mode notes:**
- **Run Peak:** Absolute high of the entire parabolic phase. Widest stop, safest.
- **Trigger Bar High:** High of the specific bar that triggered the setup. Tighter, closer to HOD concept.
- **ATR Based:** Volatility-adaptive. Useful when the peak is very distant.

"AVWAP Level" stop removed from V1. The documented VWAP stop is intraday session VWAP, not run-anchored AVWAP. Belongs in V1.5.

### Long Stop

```
longStopPrice = switch longStopMode
    "Washout Low" => washoutLow * (1 - stopBuffer / 100)
    "ATR Based"   => close - (ta.atr(atrLen) * atrMult)

longEntry = close
longRiskPct = ((longEntry - longStopPrice) / longEntry) * 100
```

### Target Calculation

```
targetFast = ta.sma(close, targetMA_Fast)
targetSlow = ta.sma(close, targetMA_Slow)

// Short targets (price is above MAs — reversion target is below)
shortRewardPctFast = ((shortEntry - targetFast) / shortEntry) * 100
shortRewardPctSlow = ((shortEntry - targetSlow) / shortEntry) * 100

// Long targets (price is below MAs — reversion target is above)
longRewardPctFast = ((targetFast - longEntry) / longEntry) * 100
longRewardPctSlow = ((targetSlow - longEntry) / longEntry) * 100

// R-multiples
shortR_Fast = shortRiskPct > 0 ? shortRewardPctFast / shortRiskPct : 0
shortR_Slow = shortRiskPct > 0 ? shortRewardPctSlow / shortRiskPct : 0
longR_Fast  = longRiskPct > 0 ? longRewardPctFast / longRiskPct : 0
longR_Slow  = longRiskPct > 0 ? longRewardPctSlow / longRiskPct : 0
```

---

## 9. VISUALS

### Bar Coloring

```
barcolor(shortSetupTriggered ? color.red : longSetupTriggered ? color.blue : na)
```

### Setup Labels

```
if shortSetupTriggered
    label text:
    "⬇ SHORT SETUP
     Entry Zone: {close}
     Stop: {shortStopPrice} ({shortStopMode})
     Target₁: {targetFast} (10 MA)
     Target₂: {targetSlow} (20 MA)
     Risk: {shortRiskPct}%
     R (10MA): {shortR_Fast}x
     R (20MA): {shortR_Slow}x
     RVOL: {rvol}x
     Ext: {extensionPct}% > MA{extMA_Len}
     Green Streak: {greenStreak}d"

    label.style_label_down, color = color.red

if longSetupTriggered
    label text:
    "⬆ WASHOUT SETUP
     Entry Zone: {close}
     Stop: {longStopPrice} ({longStopMode})
     Target₁: {targetFast} (10 MA)
     Target₂: {targetSlow} (20 MA)
     Risk: {longRiskPct}%
     R (10MA): {longR_Fast}x
     R (20MA): {longR_Slow}x
     Crash: {crashFromPeak}% in {barsFromPeak}d
     RVOL: {rvol}x"

    label.style_label_up, color = color.blue
```

### Stop Lines (V18 style)

```
// Short: stop line ABOVE entry (red horizontal extending right)
// Long: stop line BELOW entry (green horizontal extending right)
// Lines extend rightward each bar
// Turn dashed + change color if breached (close crosses stop)
// Auto-delete after maxLineAge bars (50 default)
// Array management identical to V18 pattern
```

### Target MA Lines

```
plot(showTargetLines and (targetMode == "10 MA" or targetMode == "Dual (10 & 20)") ?
     targetFast : na, "Target 10 MA", color.orange, linewidth=1)

plot(showTargetLines and (targetMode == "20 MA" or targetMode == "Dual (10 & 20)") ?
     targetSlow : na, "Target 20 MA", color.yellow, linewidth=1)
```

### Run AVWAP (Context)

```
avwapColor = close > nz(runAVWAP, close) ? color.new(color.teal, 30) : color.new(color.red, 30)
plot(showRunAVWAP and (shortSetupActive or longSetupActive) ?
     runAVWAP : na, "Run AVWAP (Context)", avwapColor, linewidth=2)
```

### Background Shading (subtle — setup forming state)

```
bgcolor(shortSetupActive and not shortSetupTriggered ? color.new(color.red, 93) : na)
bgcolor(longSetupActive and not longSetupTriggered ? color.new(color.green, 93) : na)
```

### Climax Marker

```
if parabolicAdvanceDetected and climaxVolOk
    label: "🔥" (tiny, above bar) — marks the climax detection day
```

---

## 10. PINE SCREENER OUTPUTS

All `display=display.none` for screener-only use.

### Setup State

```
plot(shortSetupTriggered ? 1 : 0,            title="SCR: Short Setup Triggered",     display=display.none)
plot(longSetupTriggered ? 1 : 0,             title="SCR: Long Setup Triggered",      display=display.none)
plot(shortSetupActive ? 1 : 0,               title="SCR: Short Setup Active",        display=display.none)
plot(longSetupActive ? 1 : 0,                title="SCR: Long Setup Active",         display=display.none)
plot(parabolicAdvanceDetected ? 1 : 0,       title="SCR: Parabolic Advance Detected",display=display.none)
plot(washoutDetected ? 1 : 0,                title="SCR: Washout Detected",          display=display.none)
```

### Ranking Metrics

```
plot(extensionPct,                            title="SCR: Extension Above MA %",      display=display.none)
plot(greenStreak,                             title="SCR: Consecutive Green Days",    display=display.none)
plot(rvol,                                    title="SCR: Relative Volume (RVOL)",    display=display.none)
plot(rollingGainPct,                          title="SCR: Rolling Gain %",            display=display.none)
plot(crashFromPeak,                           title="SCR: Crash From Peak %",         display=display.none)
plot(isSellingClimax ? 1 : 0,                 title="SCR: Selling Climax Detected",   display=display.none)
plot(hammerRatio,                              title="SCR: Hammer Ratio %",             display=display.none)
plot(volExpansionScore,                        title="SCR: Streak Vol Expansion Score", display=display.none)
```

### Liquidity

```
plot(avgDollarVol,                            title="SCR: Avg Dollar Vol (M)",        display=display.none)
```

### Setup Age (for prioritizing fresh setups)

```
shortSetupAge = shortSetupActive ? bar_index - shortSetupBar : 0
longSetupAge  = longSetupActive  ? bar_index - longSetupBar  : 0
plot(shortSetupAge,                           title="SCR: Short Setup Age",           display=display.none)
plot(longSetupAge,                            title="SCR: Long Setup Age",            display=display.none)
```

### Risk Metrics

```
plot(shortSetupTriggered ? shortRiskPct : 0,  title="SCR: Short Risk %",             display=display.none)
plot(longSetupTriggered ? longRiskPct : 0,    title="SCR: Long Risk %",              display=display.none)
plot(shortSetupTriggered ? shortR_Fast : 0,   title="SCR: Short R (10MA)",           display=display.none)
plot(shortSetupTriggered ? shortR_Slow : 0,   title="SCR: Short R (20MA)",           display=display.none)
plot(longSetupTriggered ? longR_Fast : 0,     title="SCR: Long R (10MA)",            display=display.none)
plot(longSetupTriggered ? longR_Slow : 0,     title="SCR: Long R (20MA)",            display=display.none)
plot(adrPct,                                  title="SCR: ADR %",                    display=display.none)
```

### Distance / Proximity Metrics

```
pctFromRunAVWAP = not na(runAVWAP) and runAVWAP > 0 ?
                  ((close - runAVWAP) / runAVWAP) * 100 : 0
plot(pctFromRunAVWAP,                         title="SCR: % from Run AVWAP",         display=display.none)

pctFromTarget10 = targetFast > 0 ?
                  ((close - targetFast) / targetFast) * 100 : 0
pctFromTarget20 = targetSlow > 0 ?
                  ((close - targetSlow) / targetSlow) * 100 : 0
plot(pctFromTarget10,                         title="SCR: % from 10 MA",             display=display.none)
plot(pctFromTarget20,                         title="SCR: % from 20 MA",             display=display.none)

// Stop width for external position sizing
plot(shortSetupTriggered ? shortRiskPct :
     longSetupTriggered ? longRiskPct : 0,    title="SCR: Stop Width %",             display=display.none)
```

### Approaching Setup Zones (early warning)

```
approachingParabolic = rollingGainPct >= (gainThreshold * 0.8)
                       AND rollingGainPct < gainThreshold
                       AND greenStreak >= minGreenDays - 1
plot(approachingParabolic ? 1 : 0,            title="SCR: Approaching Parabolic",    display=display.none)

extensionHeating = extensionPct >= (minExtAboveMA * 0.7)
                   AND extensionPct < minExtAboveMA
plot(extensionHeating ? 1 : 0,                title="SCR: Extension Heating Up",     display=display.none)
```

### Screener Workflow Reference

| Screener Query | What It Finds |
|---|---|
| `SCR: Short Setup Active = 1` | Confirmed parabolic advance, awaiting reversal day |
| `SCR: Short Setup Triggered = 1` | Reversal proxy fired today — drill down to 5-min |
| `SCR: Extension Above MA % > 50` + `SCR: RVOL > 2` | Ranked list of most stretched stocks with volume |
| `SCR: Consecutive Green Days >= 5` | Multi-day momentum streaks |
| `SCR: Parabolic Advance Detected = 1` | Meets all advance criteria today |
| `SCR: Washout Detected = 1` | 50%+ crash from recent peak in velocity window |
| `SCR: Long Setup Triggered = 1` | Washout bounce proxy fired — drill down to 5-min |
| `SCR: Short R (20MA) > 3` | High R-multiple short setups |
| `SCR: Approaching Parabolic = 1` | Early warning — almost meets parabolic criteria |
| `SCR: Short Setup Age` ≤ 2 | Fresh short setups (prioritize over stale) |
| `SCR: Avg Dollar Vol (M) > 50` | High-liquidity candidates only |
| `SCR: Hammer Ratio % > 50` | Strong washout exhaustion (long-side quality ranking) |
| `SCR: Streak Vol Expansion Score >= 3` | Short candidates with accelerating volume conviction |

---

## 11. ALERTS

```
alertcondition(shortSetupTriggered,         "Short Setup Triggered — Review Intraday")
alertcondition(longSetupTriggered,          "Washout Setup Triggered — Review Intraday")
alertcondition(parabolicAdvanceDetected AND climaxVolOk,
                                            "Parabolic Climax Detected — Watch for Reversal")
alertcondition(washoutDetected,             "Washout Crash Detected — Watch for Bounce")
alertcondition(shortSetupActive,            "Short Setup Active — Awaiting Trigger")
alertcondition(longSetupActive,             "Long Setup Active — Awaiting Trigger")
alertcondition(approachingParabolic,        "Approaching Parabolic Threshold")
```

---

## 12. STATE MACHINE DIAGRAMS

### Parabolic Short

```
                    ┌──────────────────────┐
                    │        IDLE          │
                    └──────────┬───────────┘
                               │
                    Advance Criteria Met
                    + Climax Aligned (within window)
                    + Liquidity OK
                    + Cooldown Cleared
                               │
                    ┌──────────▼───────────┐
                    │    SETUP ACTIVE       │
                    │                       │
                    │  • Track peak high    │
                    │  • Accumulate Run     │
                    │    AVWAP (from exact  │
                    │    advance start bar) │
                    │  • Subtle red bg      │
                    │  • 🔥 climax marker   │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                 │
         [Timeout]    [minBarsAfterSetup met  [Peak Updates]
         (5 bars)      + Daily Trigger Fires     (loop)
              │          + Quality Gates OK]
              ▼                │
            IDLE               ▼
                        SETUP TRIGGERED
                        • Label: "⬇ SHORT SETUP"
                        • Stop line plotted
                        • State → IDLE
                        • Cooldown starts
```

### Washout Long

```
                    ┌──────────────────────┐
                    │        IDLE          │
                    └──────────┬───────────┘
                               │
                    Crash ≥ minCrashPct
                    within crashWindow bars
                    + Prior Run OK (required)
                    + Liquidity OK
                    + Cooldown Cleared
                               │
                    ┌──────────▼───────────┐
                    │   WASHOUT ACTIVE      │
                    │                       │
                    │  • Track washout low  │
                    │  • Accumulate Run     │
                    │    AVWAP (from peak)  │
                    │  • Subtle green bg    │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                 │
         [Timeout]    [minBarsAfterSetup met  [Low Updates]
         (5 bars)      + Daily Trigger Fires     (loop)
              │          + Quality Gates OK]
              ▼                │
            IDLE               ▼
                        SETUP TRIGGERED
                        • Label: "⬆ WASHOUT SETUP"
                        • Stop line plotted
                        • State → IDLE
                        • Cooldown starts
```

---

## 13. V1.5 INTRADAY COMPANION — Architecture Notes

Deferred to a separate build phase. Documenting here for continuity.

### Purpose
Converts V1 daily candidates into precise intraday entries with the actual edge described in the research.

### Scope
- Runs on 5-minute (or 1-minute) charts
- Assumes the trader has already identified a candidate via V1 daily screener

### Core Components

**Session VWAP** — standard `ta.vwap`, NOT the run-anchored AVWAP from V1.

**Opening Range Detection:**
```
// Track OR high/low for first N minutes (configurable: 5, 15, 30)
// Freeze values after OR period ends
var float ORH = na
var float ORL = na
var bool  orComplete = false
```

**VWAP Fail State Machine (Short):**
```
State 1: Price breaks below Session VWAP (initial crack)
State 2: Price bounces back to/above VWAP on declining volume (retracement)
State 3: Red candle closes below VWAP → SHORT ENTRY
Stop: HOD or VWAP reclaim level
```

**ORH Break (Long):**
```
Trigger: Close > ORH with volume confirmation
Stop: LOD (absolute low of session)
```

**ORL Break (Short):**
```
Trigger: Close < ORL with volume confirmation
Stop: HOD (absolute high of session)
```

### Integration with V1
- V1 daily screener identifies candidates and outputs `SCR: Short Setup Active` / `SCR: Long Setup Active`
- Trader loads V1.5 on the 5-min chart of flagged candidates
- V1.5 handles precise timing, V1 handles candidate selection
- No data dependency between scripts — they are independent

---

## 14. IMPLEMENTATION CAUTIONS

Defensive coding requirements to address during implementation. These are not architectural changes — they are code-quality guardrails that prevent subtle bugs.

### 14.1 AVWAP Accumulator Reset — Prevent Stale Value Leak

`shortRunAVWAP` and `longRunAVWAP` are `var` floats. If a setup times out or triggers, the last computed AVWAP value persists in memory and can bleed into plots or trigger logic on subsequent bars.

**Requirement:** On ALL reset paths (timeout AND trigger reset), explicitly set:
```
shortRunAVWAP := na
shortAvwapNum := 0.0
shortAvwapDen := 0.0
```
and:
```
longRunAVWAP := na
longAvwapNum := 0.0
longAvwapDen := 0.0
```

This applies to:
- Section 6: short setup timeout block AND short trigger reset block
- Section 7: long setup timeout block AND long trigger reset block

Do not rely on the `shortSetupActive` / `longSetupActive` flag alone to guard downstream usage — clear the values at source.

### 14.2 Early-Bar Guard for `ta.highestbars` / `ta.lowestbars`

Both `ta.highestbars(high, priorRunLookback)` and `ta.lowestbars(low, gainLookback)` return unreliable values when `bar_index` is less than the lookback length (insufficient history on early chart bars).

**Requirement:** Gate all detection sections that use these functions with:
```
enoughBarsForGain = bar_index >= gainLookback
enoughBarsForPeak = bar_index >= priorRunLookback
```

Apply:
- Section 2 (Advance Detection): wrap `isParabolicGain` and AVWAP anchor logic with `enoughBarsForGain`
- Section 4 (Washout Detection): wrap `peakOffset` / `recentPeakHigh` / crash measurement with `enoughBarsForPeak`

Without this, the indicator will produce garbage detections on the first N bars of any chart load.

### 14.3 Prior-Run Loop Index Direction — Off-by-One Risk

The bounded loop in Section 4 (prior-run low detection) iterates from `barsBack` into deeper history using `low[i]`. This is the most likely location for an off-by-one bug because:

- `barsBack` is the offset from current bar to peak (`bar_index - peakBarIdx`)
- The loop then walks further back from the peak by up to `priorRunLookback` additional bars
- Pine's `low[i]` indexes bars ago from current bar, not from any anchor point

**Requirement:** During implementation:
- Add a comment block above the loop explaining the index arithmetic and direction
- Verify that `i = barsBack` corresponds to the peak bar and `i = barsBack + priorRunLookback - 1` corresponds to the earliest bar in the run window
- Ensure the upper bound does not exceed `bar_index` (cannot index before bar 0)
- Test with a known example (e.g., LAZR 2020) to confirm the measured prior-run gain matches visual chart inspection

### 14.4 Label and Alert Language Consistency

All user-facing text must reinforce that V1 is a screener/setup detector, not an execution engine.

**Requirement:**
- Every label must use "SETUP" (never "SIGNAL" or "ENTRY")
- Every alert title must include "Review Intraday" or "Watch for" language
- No alert message should imply automatic execution (e.g., avoid "Buy" / "Sell" / "Enter")

Cross-check during implementation:
- Section 9 labels: "⬇ SHORT SETUP" / "⬆ WASHOUT SETUP" ✓
- Section 11 alerts: all include qualifying language ✓
- Ensure no slip during coding where convenience shortcuts introduce "signal" terminology

---

## 15. CHANGE LOG

### Changes from Original Plan → Rev 1

| Item | Original | Rev 1 |
|---|---|---|
| System identity | Execution indicator | Candidate-ranking screener (V1) + execution companion (V1.5) |
| Labels | "SIGNAL" | "SETUP" |
| RVOL default | 2.5 | 3.0 |
| Short trigger default | "First Red Day" | "Close < Prior Low" |
| `requirePriorRun` default | toggle (ambiguous) | true |
| AVWAP naming | "Anchored VWAP" | "Run AVWAP (Context)" |
| Short stop modes | HOD, Parabolic Peak, ATR, AVWAP | Run Peak, Trigger Bar High, ATR |
| ADR formula | `sma(H-L, N) / close * 100` | `sma((H/L - 1) * 100, N)` |
| Liquidity filters | absent | Added: minPrice, minAvgDollarVol |
| Target MAs | single MA | Dual 10 & 20 MA |
| Position sizing | proposed as label field | Screener output only (SCR: Stop Width %) |
| Weekly MTF | proposed as optional | Deferred |
| V1.5 intraday | not planned | Documented architecture |

### Changes from Rev 1 → Final (this document)

| Item | Rev 1 | Final |
|---|---|---|
| AVWAP anchor (short) | `bar_index - gainLookback` (approximate) | `ta.lowestbars` for exact bar |
| AVWAP anchor (long) | ambiguous / inherited from short | Explicitly anchored from peak |
| Climax alignment | implicit in state behavior | Explicit `climaxWindowBars` input + `lastClimaxBar` tracking |
| Prior-run window | `priorRunLookback + barsBack` (expandable) | Fixed window `[peakBar - priorRunLookback ... peakBar]` |
| Min bars after setup | not present | `minBarsAfterSetup` input (default 0) |
| Min ADR filter | not present | `minADR_Pct` input (default 2%) in liquidity group |
| SCR: Avg Dollar Vol | not present | Added to screener outputs |
| SCR: Setup Age | not present | Added short/long setup age to screener outputs |
| Long Run AVWAP | same as short (advance start) | Anchored from peak (crash origin) |
| Peak tracking (Section 4) | `for` loop scanning highs | `ta.highestbars()` native function (screener-efficient) |
| SCR: Hammer Ratio % | not present | Added continuous ranking metric for washout quality |
| Vol Expansion Score | not present | Optional ranking score (count of expanding vol days in streak) |
| Prior-run low loop | — | Kept as bounded loop for V1 (optimize later if needed) |
| Implementation cautions | not documented | Added Section 14: AVWAP reset hygiene, early-bar guards, loop index caution, label language consistency |
