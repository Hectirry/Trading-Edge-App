# ADR 0015 — Erratum: Polymarket maker rebates ARE active

Date: 2026-04-27
Status: Accepted (erratum)
Scope: Supersedes the maker-rebate fact in [ADR 0014](0014-oracle-lag-v2-maker-first.md)
       and any downstream reasoning that assumed it.

## Background

Source: [docs.polymarket.com/market-makers/maker-rebates.md](https://docs.polymarket.com/market-makers/maker-rebates.md)
and [docs.polymarket.com/trading/fees.md](https://docs.polymarket.com/trading/fees.md),
both verified 2026-04-27 as part of the mm_rebate_v1 Step 1 sanity check
(see `estrategias/en-desarrollo/mm_rebate_v1.md` § Hipótesis erratum).

## What ADR 0014 said

ADR 0014 (oracle_lag_v2 maker-first quoting, since superseded by ceiling
test) characterized the Polymarket maker side as:

> "Capture the **0% maker rebate** (vs 1.5-3.0% taker fee), so per-trade
> economics swing by ~3-5 percentage points in our favour even with no
> improvement in scoring."

> "Maker rebate accounting in the fee model — currently `_dynamic_fee`
> returns a single positive number. Maker side returns 0 (or **negative
> rebate if Polymarket adds one in 2026Q3 per the roadmap**)."

This treats the maker rebate as **zero today** and **possibly negative
later in 2026**.

## What is actually true

The maker rebate program is **active in production** as of the verification
date. The relevant facts:

1. **Maker side**: never charged fees ("Makers are never charged fees" —
   trading/fees.md). Confirmed in line with ADR 0014.

2. **Maker rebate**: a category-specific share of taker fees collected in
   the market is redistributed to makers, distributed **proportionally to
   filled maker volume** in that market. Formula:

       rebate_for_maker = (my_fee_equivalent / total_fee_equivalent_in_market)
                          × (rebate_rate × total_taker_fees_collected)

   where `fee_equivalent = C × feeRate × p × (1 − p)` (the same parabolic
   shape as the taker fee).

3. **Rebate rates by category**:

   | Category                                                          | Rate |
   |-------------------------------------------------------------------|------|
   | Crypto                                                            | 20%  |
   | Sports, Finance, Politics, Economics, Culture, Weather, Tech, Mentions, Other/General | 25% each |
   | Geopolitics                                                       | 0% (fee-free both sides) |

4. **Minimum payout**: 1.00 USDC per accrual period. Daily payout at
   midnight UTC.

5. **Eligibility**: place resting limit orders that get filled (i.e., your
   liquidity is taken). No volume tier — share is proportional only.

6. **`getCurrentRebatedFeesForAMaker`** API endpoint exists at
   `/api-reference/rebates/get-current-rebated-fees-for-a-maker.md` —
   queryable per maker address.

7. The separate **Liquidity Rewards Program** (`/market-makers/liquidity-rewards.md`)
   is **disjoint** and as of the April 2026 pool covers only sports/esports —
   BTC up/down markets are NOT eligible for it. So mm_rebate_v1 economics
   should NOT include liquidity-rewards income, but DO include the
   maker-rebate share above.

## Why ADR 0014 had it wrong

ADR 0014 was authored 2026-04-26. The verification source was internal
("the brief mentions co-lo recommendation if v2 ships and saturates the
available latency") rather than Polymarket's public docs. The phrase
"negative rebate if Polymarket adds one in 2026Q3 per the roadmap" treated
the rebate as a future consideration when in fact the program was already
live. The error did not affect ADR 0014's main conclusion — oracle_lag_v2
was falsified by the ceiling test on 2026-04-27 — but it propagated into
the initial design of mm_rebate_v1 (Step 0 v1, same date).

## Implication for mm_rebate_v1

Step 0 v1 tabletop economics omitted the rebate. The corrected economics
must include the proportional rebate share. For a single 15m BTC market:

- Total taker fees collected per market = Σ (fee_taker(p) × notional) over
  all filled trades.
- Crypto rebate pool per market = 20% × that total.
- Our share = our_filled_maker_volume / total_filled_maker_volume.
- Rebate per market = pool × our_share, paid daily.

For mm_rebate_v1 V1 in below-zona buckets (0.15-0.40), the rebate is a
**marginal-but-positive** adjustment on top of the spread + favorable
adverse selection already captured. Order of magnitude:

- Avg taker fee in bucket 0.15-0.20 ≈ 1.4% of notional.
- 20% rebate × 1.4% = 0.28% of notional contributed to the rebate pool.
- At 5% maker market share (base case): we recover ≈ 0.014% × notional per
  fill, which over the bucket's 162k trades / 5.8 days adds tens of USDC
  to the period total.
- At 1% maker market share (worst case): a fifth of the above.

This is not transformative for the verdict but it does shift the
above-zona buckets (0.60-0.85, currently negative E[I/h]) closer to
break-even by a bps-scale amount.

## Decision

1. ADR 0014 stands as historical record of the oracle_lag_v2 attempt;
   readers should follow the link to this erratum for the corrected fact.
   The ADR header has not been edited (preserving immutability of past
   decisions); only this newer ADR supersedes the erroneous fact.

2. mm_rebate_v1 design (Step 1 helpers) reflects the corrected fact:
   `_fee_model.py` ships `fee_maker(p) = 0` plus a separate
   `rebate_pool_share(my_volume_share, total_taker_fees_pool, category)`
   that returns the maker's expected slice.

3. Step 0 v2 (scheduled for 2026-05-27, routine
   `trig_01UJ26a2L1FB9yi5pNJ4Pg94`) recomputes the verdict including the
   rebate.

## References

- [ADR 0014 — oracle_lag_v2 maker-first quoting (SUPERSEDED)](0014-oracle-lag-v2-maker-first.md)
- docs.polymarket.com/market-makers/maker-rebates.md (verified 2026-04-27)
- docs.polymarket.com/trading/fees.md (verified 2026-04-27)
- `src/trading/strategies/polymarket_btc15m/_fee_model.py` —
  implementation of the corrected fact.
- `estrategias/en-desarrollo/mm_rebate_v1.md` § Hipótesis (erratum block).
