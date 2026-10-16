# OTC history rules 1.0

All market collection remains on otcdesks.cash. No extra network request is
introduced by the indicators: archive existing /api/chart 1_MINUTE mcap bars
and /api/coins snapshots. Existing discovery, 90-candidate limit and request
budgets remain unchanged. Offline/expired candidates are not continuously
polled; ranking changes can reactivate them. Coverage is not platform-wide.

## Storage and causality

- otc_bars: (mint, source minute) primary key; completed, finite, valid OHLCV
  only; first observation is immutable. No fabricated zero-volume bars.
- otc_snapshots: (mint, source timestamp) primary key; holders, liquidity,
  marketCap, received timestamp. Repeated source timestamps are not evidence.
- Both archives retain 30 days, cleaned at most hourly. Current calculations
  read at most 48 hours of bars and 30 minutes of snapshots per candidate.
- Existing candidate K lines are imported once. Past uncollected market data
  cannot be reconstructed. New signal frames store the computed indicators;
  replay reads those frames, not a recomputation using future data.
- Market-cap OHLCV is not price OHLCV. Volume units remain unverified and
  only same-source relative quantities are used.

## Indicators

Every result carries measured values, a rule, and true/false/unknown state.
Unknown includes insufficient, stale, discontinuous and zero-denominator data.
Exact formulas are in trend_history.evaluate and also shown in token details.

- Sustained volume: last two 5m average volumes each >=1.5x preceding 30m
  median minute volume; largest minute <=40% of last 10m volume.
- Stair-step structure: three nonoverlapping 5m highs and lows increase >0.1%.
- Acceleration: three 5m positive returns and average volumes strictly increase.
- Retest: break preceding 30m high by 0.2%, retest within +1%, no subsequent
  low below -2%, later close above both level+0.2% and retest bar high.
- Volume/legs: most recent adjacent >=2-bar up and down legs within 30m;
  up volume >=1.5x >=5 preceding bars, down volume <=70% of up mean,
  down leg closes above the up-leg opening level.
- Multi-window: trailing 5m and 15m each positive and volume >=1.2x their
  preceding equal windows. These overlapping windows are not independent.
- Revival: age >48h, prior 24h continuous bars range <=10%, following 10m
  volume >=3x baseline median and closes above the 24h high.
- Holder acceleration: three snapshots spaced 4-7m apart; later percentage
  growth per minute is positive and exceeds earlier rate.
- Liquidity ratio: same snapshot liquidity / marketCap >=5%; change is shown
  in percentage points against a 4-15m older snapshot. Not executable depth.

## Signal integration

History indicators supplement the legacy basic evidence score, not inflate it.
Full signals require sustained volume AND (structure OR retest OR multi-window),
fresh valid inputs, liquidity >=5000, liquidity change >=-5%, and 0<5m mcap
return<=15%. Holder growth is supporting, not mandatory. Old basic signals
with insufficient sustained history remain previews; failed persistence or
structure becomes watch. Risk/unavailable/inactive gates cannot be upgraded.
Existing later-new-bar confirmation still applies; wording is rule satisfaction,
not guaranteed launch. Rule versions separate old/new events.

## Observed path statistics

Latest 200 OTC events, grouped by rule version. Windows start on the first
full minute at/after signal time and cover 5/15/30 minutes. Require every bar;
otherwise pending or missing. Show median end change, maximum high/low
excursions from first open, close-to-close peak drawdown and positive share.
These are market-cap paths, not execution returns; fees/slippage excluded.
Nonpositive counts are not a calibrated false-positive rate. No claim of
predictive profitability, known true launch time, or measured discovery latency.
