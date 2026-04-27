# ADR 0014 — `oracle_lag_v2` maker-first quoting (scaffolding)

Date: 2026-04-26
Status: SUPERSEDED 2026-04-27 (falsified by ceiling test, see top block)
Scope: Phase 3.11 — follow-up to ADR 0013 / oracle_lag_v1

> **SUPERSEDED 2026-04-27 — falsified by ceiling test.** Ceiling A/B
> backtest over 2026-04-18 → 2026-04-26 (8 days, 2118 markets,
> `polybot-agent.db`) under ideal-maker assumption (fee=0, slippage=0,
> fill=100 %) returned: v2 avg PnL/trade **$0.68** vs v1 **$11.96**.
> Falsification Gate #1 of this ADR (`v2 ≥ v1 + 1.5 ¢`) **fails by an
> order of magnitude** — even with the absolute upper bound of the
> hypothesis, v2 captures ~5.7 % of v1's per-trade edge. Root cause:
> the Φ(δ/σ√τ) signal is **not invariant in time within a market**.
> Widening the entry window from [285,297]s to [60,297]s lets v2 quote
> earlier but the residual at t=60s carries ~3× more uncertainty than
> at t=285s, and that diluted-signal hit dwarfs the fee saving. Corollary:
> the theoretical taker fee captured by v1 was <0.5 % of v1's total PnL,
> not the +30-50 % projected here — this ADR overestimated the weight
> of the taker fee by ~100×. **Permanent learning** (preserved in
> BITACORA): an execution-policy change (taker→maker) cannot be
> evaluated as "same predictive edge, different fee" — the policy
> conditions the entry window, and the entry window conditions signal
> quality. Code retired (oracle_lag_v2.py, TOMLs, tests,
> avellaneda_stoikov.py + test, dispatch entries). v1 remains the
> canonical implementation of the BS-digital residual on the multi-CEX
> cesta.

> **2026-04-26 update — gate bypass declared by operator** *(now
> historical, see SUPERSEDED block above).* The original
> "≥ 2 weeks of real paper-shadow operation of v1" precondition (see
> § Decision below) was bypassed by explicit operator decision: v1 and
> v2 ship together, v2 in `[paper] shadow = true`. Rationale per
> operator: "the impact of waiting two weeks for nothing is greater
> than letting v2 observe in shadow now". v2 still defaulted to
> shadow because the cancel+place adapter on top of
> `SimulatedExecutionClient` (see Sketch § 1) was not yet wired —
> shadow=false would have required that work plus the § Falsification
> gate. The ceiling test (above) made the wiring effort moot.

## Context

`oracle_lag_v1` (ADR 0013) demonstrated taker edge in backtest +
paper_ticks shadow: Sharpe/trade 0.515 on canonical paper_ticks data
with permutation p-value 0.0. The brief that motivated the family
explicitly states that taker pure edge is structurally compressing in
2026 due to dynamic fees (1.56-3.15 % at p=0.5):

> "*las firmas que sobreviven … operan ya en modo maker-first con
>  micro-price quoting, no sniping bruto*"

`oracle_lag_v1` is therefore the gateway — it proves the underlying
scoring is sound — but the long-term strategy is `oracle_lag_v2` with
maker-first quoting that captures the taker fee instead of paying it.

## Decision

Defer implementation. Open this ADR as a placeholder so the design
intent is recorded. Implement only after `oracle_lag_v1` survives
≥ 2 weeks of real paper-shadow operation (post-ADR-0011 promotion
gate, operator-driven flip of `[paper] shadow = false`).

## Sketch

`oracle_lag_v2` shares the BS-digital scoring core with v1; the
difference is **execution policy**:

- **v1 taker**: at `t ∈ [285, 297]` if `EV_net > θ`, fire FAK at the
  current ask. Pay dynamic fee.
- **v2 maker**: from `t ≈ 60 s` onward, post **GTC limit orders** at
  the maker side of the spread on the favoured outcome. Cancel +
  re-quote on book moves. Capture the 0 % maker rebate (vs 1.5-3.0 %
  taker fee), so per-trade economics swing by ~3-5 percentage points
  in our favour even with no improvement in scoring.

Required infrastructure:

1. **Activate `paper/limit_book_sim.py`** — it exists but isn't wired
   into the execution path. Currently `SimulatedExecutionClient` only
   simulates FAK fills; we need a queue-position simulator for resting
   limit orders.
2. **Avellaneda-Stoikov spread + inventory-risk model** — optimal
   distance from the mid as a function of σ, time-to-close, current
   inventory. Reference: Avellaneda & Stoikov 2008 "*High-frequency
   trading in a limit order book*", Quantitative Finance.
3. **Maker rebate accounting** in the fee model — currently
   `_dynamic_fee` returns a single positive number. Maker side returns
   0 (or negative rebate if Polymarket adds one in 2026Q3 per the
   roadmap).
4. **Cancel/re-quote logic** with rate-limit budget (Polymarket allows
   3,000 cancels / 10 s; v2 must stay well under that).
5. **A new TOML section** `[execution]` selecting `mode = "maker"` vs
   `mode = "taker"` (v1 default).

Estimated effort: ~1 week dedicated.

## Falsification

`oracle_lag_v2` is worthwhile only if it materially beats v1's PnL/share
NET of fees. Concrete gate, evaluated on the same paper_ticks window:

- Realized PnL/share v2 ≥ realized PnL/share v1 + 1.5 ¢
  (i.e. captures at least half of the avoided taker fee).
- Maker fill rate ≥ 40 % at the chosen `[execution].limit_offset_bps`.
  Below that, the model is competitive enough on the open book that
  most quotes never fill.

If those don't hold, v2 is no better than v1 and adds operational
complexity. Move it to `descartadas/` and stick with v1.

## Out-of-scope

- Cross-platform Polymarket↔Kalshi arbitrage (separate strategy
  family, separate ADR).
- News-driven LLM-swarm signals (PolySwarm-style, separate research
  thread, separate ADR).
- Co-location AWS eu-west-2 — the brief mentions ≤ 2 ms RTT from
  Dublin VPS to `clob.polymarket.com` is achievable. Co-lo upgrade is
  an infra decision orthogonal to the strategy file. Document the
  co-lo recommendation if v2 ships and saturates the available latency.

## References

- ADR 0013 — `oracle_lag_v1` 7-sprint roadmap (parent ADR).
- Brief (user-supplied 2026-04-26) on Polymarket BTC 5 m bot ecology.
- Avellaneda & Stoikov 2008 — market making with inventory risk.
- Polymarket CLOB rate limits — 3,500 orders / 3,000 cancels per 10 s
  window per the 2026 docs.
