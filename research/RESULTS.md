# Research results behind the signal tool

Every rule here was **pre-registered** before it was run: the rule, the data window and the pass/fail bar were written down
first. Results are reported whether they pass or fail. Dates are 2026. Not financial advice.

---

## 1. 0DTE opening-range breakout on the stock in play (`python -m signals orb`)

**Source.** Zarattini, Barbon & Aziz (2024), *A Profitable Day Trading Strategy for the U.S. Equity Market* (SSRN 4729284).
They rank liquid stocks by relative volume in the first five minutes and trade a breakout of the first 5-minute candle
with a stop of 10% of the daily range (ATR14). We kept their stock rules and asked whether buying the **same-day
at-the-money option** instead of the stock is worth it.

**Data.**
- Option prices: real 1-minute option bars (Alpaca, from 2024-02).
- Stock prices: consolidated 1-minute stock bars.
- Universe: the 250 most-traded US stocks.
- Ambiguous entry minutes (where the stop and the entry both trade inside the same minute) were resolved with tick data.
- Costs: 2.5% of the premium per side (stress test: 5%).

**Rule kept ("plan b").**
- Skip the day if VIX closed above 20.
- Take the top relative-volume name whose at-the-money option costs ≤ $2.00.
- Stop on a 1-minute close beyond 10% of ATR14.
- Otherwise hold to 15:55.

| Sample | Trades | Mean per trade | 95% CI | At 5% cost |
|---|---|---|---|---|
| Build sample, 2024-09 → 2026-10 (rules chosen here) | 393 | **+8.3%** | +0.9 to +17.1 | positive |
| **Untouched holdout, 2024-02 → 2024-09** | 144 | **+2.3%** | −6.5 to +12.0 | −1.8% |

Typical shape: about 1 trade in 4 wins. The average winner is about +$70 per contract and the average loser about −$14.
A few big days carry the average.

**What was tested and dropped** (all pre-registered):
- QQQ-divergence filter: failed the permutation test.
- Low-GEX filter: wrong sign.
- Earnings-only filter: worse.
- Debit spreads instead of calls: −24%.
- Intraday exits (time stops, break-even, trailing, VWAP, take-profit at 2×): none beat holding to 15:55.
- Pairing with 45-day credit spreads: lowered the Sharpe ratio.

**Honest read.**
- The edge is real in the build sample.
- It is small and uncertain out of sample.
- It is roughly break-even if fills are poor.

That is why the tool paper-trades it first. It records whether a limit at the mid would have filled and what the bid/ask
were, so live fills can be compared with the backtest.

Paper log so far: 2026-10-23, SLB 50 call bought at 0.82 mid (filled in 16 s), exited 15:55 at 3.08 mid: **+275%**
(+$226 per contract).

---

## 2. SPY "buy the dip above the 200-day" (`python -m signals spy`)

**Source.** Connors & Alvarez (2008), *Short Term Trading Strategies That Work*.
- Buy SPY at the close when it is above its 200-day average and its 2-day RSI is below 10.
- Sell at the first close above the 5-day average.

Reproduce with free data: `python -m research.spy_rsi2_backtest` (uses the same code as the live signal).

| SPY, 1994 → 2026-10, 0.02% cost per side | |
|---|---|
| Trades | 263 (about 8 a year) |
| Winners | 76% |
| Mean per trade | **+0.63%** (average hold 3.4 days) |
| 1994-2009 / 2010-2026 | +0.84% / +0.46% |
| Random entries above the 200-day (same holds), 95th percentile | +0.27%: the rule beats it |
| Shares only, compounded | +5.1%/yr, max drawdown −11%, invested 11% of days |
| SPY buy and hold | +10.8%/yr, max drawdown −55% |

**Options version** (a ~45-day at-the-money SPY call bought at the signal close and sold at the exit close), real Alpaca
option prices 2024-03 → 2026-10:
- 24 trades, 71% winners.
- **+8.9% per trade**, +$3,347 total on one contract each.
- Worst trade −$874; a contract costs about $1,500.
- Option daily closes are last-trade prints, so treat individual numbers as approximate.

### Tested and rejected: selling put spreads above the 200-day

The other well-known "200-day" idea is to sell SPY bull put spreads only while SPY is above its 200-day average.
- Spread: about 45 days, 20-delta, 1% wide.
- Exit: at 50% profit or 21 days to expiry.

Priced back to 1994 with a VIX-based model:

| Cost assumption | Mean, % of the money at risk |
|---|---|
| Before costs | +0.7% per trade |
| After realistic costs | −1% to −5% |

The 200-day filter did not beat the same trade without it. Real 2024-26 prices looked better (+$23 per spread, 59 trades),
but that was one bull-market window. **It failed the pre-registered test and is not in the tool.**

---

## What would change these conclusions

- **Breakout:** about 30 paper trades whose fills and returns look like the build sample → go live small. Fills that are
  systematically worse than mid, or skipped signals that do better than filled ones → stop.
- **RSI(2):** it is a 30-year, widely published rule with a small edge per trade. The main risk is a crash while holding.
  A −20% gap would cost about the whole premium on the options version.
