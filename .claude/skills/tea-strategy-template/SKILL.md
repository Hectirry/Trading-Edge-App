---
name: tea-strategy-template
description: >
  Skeleton for adding a new polymarket_btc5m strategy to Trading-Edge-App.
  Lists every file that must be touched (strategy .py, TOML, dispatch in
  backtest.py + paper_engine.py, doc under estrategias/en-desarrollo/),
  the canonical TOML section layout, and the shadow→active promotion
  flow. Mirrors the exact pattern of last_90s_forecaster_v3 (the latest
  reference). Invoke when user says "create a new strategy", "stub
  strategy X", or asks how to register a strategy in the engine.
---

A new strategy needs **four files plus two dispatch edits**. Anything
less and it won't load; anything more is premature.

## Files to create

```
src/trading/strategies/polymarket_btc5m/<name>.py
config/strategies/pbt5m_<name>.toml
estrategias/en-desarrollo/<name>.md
tests/unit/strategies/polymarket_btc5m/test_<name>.py
```

## Files to edit (dispatch)

```
src/trading/cli/backtest.py        # _load_strategy switch
src/trading/cli/paper_engine.py    # _build_strategy switch
config/environments/staging.toml   # [strategies.<name>] enabled = true
```

## Strategy .py skeleton

Inherit from `StrategyBase`. The minimum surface:

```python
from trading.engine.types import StrategyBase, TickContext, Decision

class MyStrategy(StrategyBase):
    name = "my_strategy_v1"

    def __init__(self, params, sizing, runner=None):
        self.params = params
        self.sizing = sizing
        self.runner = runner

    async def on_start(self) -> None:
        ...

    def should_enter(self, ctx: TickContext) -> Decision:
        # gate by t_in_window first; this is THE most common bug
        if not (self.params.entry_window_start_s
                <= ctx.t_in_window
                <= self.params.entry_window_end_s):
            return Decision.skip("outside_entry_window")
        ...
```

If the strategy uses an LGBM model, **reuse `LGBRunner` from v2** rather
than instantiate your own. v3 imports it directly:

```python
from trading.strategies.polymarket_btc5m.last_90s_forecaster_v2 import LGBRunner
```

## TOML skeleton

The order of sections matters for readability and matches every
existing strategy. Copy from `pbt5m_last_90s_forecaster_v3.toml`:

```toml
name = "<name>"
description = "<one-liner>"

[params]
entry_window_start_s = 205
entry_window_end_s = 215
edge_threshold = 0.02
spread_max_bps = 300.0
adx_threshold = 20.0
consecutive_min = 2
# Promotion gates (sample-size-aware in train_last90s).
promotion_auc_min = 0.55
promotion_brier_max = 0.245
promotion_ece_max = 0.05

[sizing]
stake_usd = 5.0
kelly_fraction = 0.25
kelly_min_trades = 20
kelly_max_stake_usd = 15.0

[backtest]
earliest_entry_t_s = 205
latest_entry_t_s = 215
window_seconds = 300
decision_interval_s = 1

[fill_model]
slippage_bps = 10.0
fill_probability = 1.0
apply_fee_in_backtest = true
fee_k = 0.05

[risk]
bypass_in_backtest = true
cooldown_seconds = 30
max_position_size_usd = 15.0
daily_loss_limit_usd = 50.0
daily_trade_limit = 999999
min_pm_depth_usd = 15.0
skip_if_spread_bps = 500
loss_pause_threshold_usd = 5.0
loss_pause_window_minutes = 30
loss_pause_duration_minutes = 30

[paper]
capital_usd = 1000.0
daily_loss_alert_pct = 0.03
daily_loss_pause_pct = 0.05
shadow = true   # always start shadow
```

## Dispatch — `backtest.py` and `paper_engine.py`

Both files have a `_load_strategy` / `_build_strategy` function with an
explicit if/elif tree on the strategy name. Add an `elif` branch
matching your name. There is no auto-discovery. Don't try to add one.

## Doc — `estrategias/en-desarrollo/<name>.md`

Sections that must exist (validated against
`last_90s_forecaster_v3.md`):

- `# <name>` + `Estado: en-desarrollo` + family + `Creada:` + `Autor:`
- `## Hipótesis` — exact lift threshold needed (e.g. "+3 pp AUC vs
  baseline X over the same subset")
- `## Variables clave` — horizon, window, sizing, toggles
- `## Falsificación` — concrete kill criteria
- `## Datos requeridos` — every table touched + retention
- `## Implementación` — every file + dispatch
- `## Plan de validación` — 3 phases: subset → walk-forward → shadow
- `## Resultados` — populate after first measurement
- `## Veredicto` — initial: empty
- `## Historial` — chronological log of measurements

## INDICE.md

Add a row under `## En desarrollo`:

```
| <name> | polymarket_btc5m | — | — | <one-line summary> |
```

When the strategy is promoted (`is_active=true`), move the row to the
`## Activas` table. When falsified, move to `## Descartadas` with
`motivo` and the doc to `estrategias/descartadas/`.

## Shadow → active

Strategies always start `shadow=true` in TOML. The promotion gate
lives in `.claude/skills/tea-promotion-gate/SKILL.md` — invoke that
when ready to flip `is_active=true`. Never flip without running it.

## Don't

- Don't add the strategy to `staging.toml` with `enabled=true` until
  the TOML + dispatch are landed and a backtest CLI run produces a
  non-error baseline.
- Don't introduce a new feature module under
  `src/trading/engine/features/` for one strategy. If the feature is
  reusable, fine; otherwise compose existing functions.
- Don't add new deps to `pyproject.toml` unless the strategy requires
  a model framework not already pinned (LightGBM, XGBoost are pinned;
  PyTorch is not — adding it is a separate ADR).
