---
name: tea-strategy-removal
description: >
  Step-by-step protocol for safely removing a strategy from
  Trading-Edge-App. Covers the deactivation flow (when running in paper),
  the file-deletion order, dispatch unhooking, doc updates, and the
  post-removal audit. Codifies lessons from the 2026-04-26 cleanup of
  last_90s_forecaster_v1/v2 + contest_ensemble_v1 + contest_avengers_v1
  where dangling references survived the first pass (skill imports, ADR
  status flag, INDICE rows, helper docstrings). Invoke when user says
  "elimina la estrategia X", "descarta Y", "limpia Z", or asks how to
  retire a strategy.
---

Removing a strategy touches **at least 9 places**. Missing any of them
leaves dangling references that surface as ImportErrors on the next
engine restart, or as stale claims in docs/skills/INDICE that mislead
future sessions. This protocol is exhaustive on purpose.

## Hard rules (non-negotiable)

1. **Never delete the strategy `.md`.** Move it to
   `estrategias/descartadas/<name>.md`. The `.md` is institutional
   learning — `estrategias/README.md` makes this explicit ("Nunca
   borrar").
2. **Append a final Historial entry** to the `.md` before moving:
   ISO date + the metric/event that falsified the hypothesis + 1-line
   motivo.
3. **Mark associated ADRs as Superseded**, do not delete them.
   ADRs are the design record; they outlive the code.
4. **Never silently drop helpers shared with surviving strategies.**
   Run grep before deleting any `_*.py` — `_v2_features.py` and
   `_lgb_runner.py` look like dead v2 leftovers but are reused by v3.

## Pre-flight (ask the user before starting)

1. Confirm the target name (`<family>/<name>`) and motivo en 1 línea.
2. Is it currently `enabled = true` in
   `config/environments/staging.toml` (i.e. running in paper)?
3. Does it have a dedicated ADR? (`grep -l "<name>" Docs/decisions/`)
4. Does it have dedicated dashboards / CLIs / scripts beyond the four
   canonical files? (e.g. `contest_ensemble_v1` had
   `cli/contest_ab_weekly.py` + `dashboards/contest_ab.json`)

If any answer is unclear, ask before deleting.

## Phase A — Deactivate (only if running in paper)

Skip this phase if the strategy was never in `staging.toml`.

1. Edit `config/environments/staging.toml`: set
   `[strategies.<name>] enabled = false` (do not remove the block yet
   — keep it there as the deactivation marker for the deploy step).
2. `make deploy-staging` — rebuilds engine, picks up the disabled flag.
3. `make check-staging` — confirm engine is up and heartbeat fresh.
4. `make logs-engine` — verify the strategy is no longer in the
   "loaded strategies" startup line. No `ImportError` should appear.
5. **Wait for the user to confirm** the strategy is gone from paper
   before deleting code. Removing the `.py` while staging still has
   `enabled=true` will crash the engine on next restart.

## Phase B — Move the `.md` and append final Historial

```bash
# 1. Append final Historial entry FIRST (while still in current state dir)
#    Sections: ### YYYY-MM-DD — Descartada
#    Body: motivo (1-3 líneas) + métrica que falsificó + commit hash si aplica

# 2. Move (preserves git history):
git mv estrategias/<estado>/<name>.md estrategias/descartadas/<name>.md
```

Where `<estado>` is `activas/` or `en-desarrollo/` (whichever it lived in).

## Phase C — Delete code, config, tests, results

In this order:

```bash
# Strategy code
rm src/trading/strategies/<family>/<name>.py

# All TOMLs matching the strategy (incl. evaluation profiles like _phase1, _phase15, _v2)
rm config/strategies/<prefix>_<name>*.toml

# Tests (golden, integration, unit — all of them)
rm tests/unit/strategies/test_<name>*.py

# Results (entire dir — these are VPS output, no institutional value)
rm -r estrategias/resultados/<name>/

# Optional, only if the strategy had dedicated artifacts beyond the four canonical files:
rm src/trading/cli/<name_specific>.py            # e.g. contest_ab_weekly.py
rm infra/grafana/dashboards/<name_specific>.json # e.g. contest_ab.json
rm scripts/<name_specific>.py                    # e.g. grid_search_v1_divisor.py
```

## Phase D — Unhook the dispatch (ADR 0008: dispatch is manual)

Two files have an explicit `if/elif` tree on strategy name. Both must
be edited:

1. `src/trading/cli/backtest.py` — find `_load_strategy`, delete the
   `elif strategy_name == "<family>/<name>":` branch and its imports.
2. `src/trading/cli/paper_engine.py` — same pattern under
   `_build_strategy` (or `_load_strategy`, depending on the version).
3. `config/environments/staging.toml` — now remove the
   `[strategies.<name>]` block entirely (Phase A only set
   `enabled=false`; this completes the cleanup).
4. Same for `config/environments/dev.toml` and
   `config/environments/production.toml` if present.

## Phase E — Hunt dangling references

Run these greps. Each should be **empty** afterwards (except for the
moved `.md` in `descartadas/`, which is allowed):

```bash
# Python imports + module references
grep -rn "<name>" src/ tests/ scripts/ --include="*.py" \
  | grep -v "__pycache__" \
  | grep -v "estrategias/descartadas/"

# Configs, docs, dashboards
grep -rn "<name>" Docs/ config/ infra/ \
  --include="*.md" --include="*.toml" --include="*.json" --include="*.yml" --include="*.yaml" \
  | grep -v "estrategias/descartadas/"

# Skills (this is where the 2026-04-26 cleanup leaked — skills are easy to miss)
grep -rn "<name>" .claude/skills/ --include="*.md"

# Top-level docs
grep -n "<name>" CLAUDE.md README.md Makefile 2>/dev/null
```

For each hit, decide:
- **Imports/dispatch in src/**: must remove (else ImportError).
- **Doc body / ADR Context section**: usually keep as historical record;
  consider adding "(retired YYYY-MM-DD)" note inline.
- **Skills examples**: must update — they get loaded into future
  Claude sessions as if current.
- **Helper docstrings (`_*.py`)**: keep if the helper survives and the
  docstring is descriptive; the reference is documentation, not code.

## Phase F — Update docs and ADRs

1. **`estrategias/INDICE.md`** — move the row from Activas/En-desarrollo
   to Descartadas. Format:
   ```
   | <name> | <family> | <motivo en 1-2 palabras> | <1-line summary + key metric that killed it> |
   ```
2. **`estrategias/BITACORA.md`** — append entry:
   ```
   ### YYYY-MM-DD — <name> descartada
   <2-4 lines: what failed, what we learned, what's the next bet>
   ```
3. **`Docs/decisions/<NNNN>-*.md`** if a dedicated ADR exists:
   - Change the front-matter / first line: `Status: Accepted` →
     `Status: SUPERSEDED YYYY-MM-DD by <reason>`.
   - Add a top block above Context:
     ```
     > **SUPERSEDED YYYY-MM-DD** — <2-3 lines: why this decision was
     > reversed; reference the ADR that replaces it if any>.
     ```
   - Do **not** edit the original Context/Decision/Consequences sections.
     The ADR's value is the historical record.
4. **`Docs/runbook.md`** — remove operational lines (kill switches,
   on-call notes) referring to the strategy.
5. **`CLAUDE.md`** — only if the strategy was named explicitly there.

## Phase G — Sanity checks (must pass before committing)

```bash
# Syntax
python -m py_compile src/trading/cli/backtest.py src/trading/cli/paper_engine.py
python -m py_compile $(find src/trading -name "*.py")

# Tests
pytest -q tests/unit/strategies/

# Lint
ruff check . && ruff format --check .

# Final dangling-reference sweep (should return 0 hits outside descartadas/)
grep -rn "<name>" . \
  --include="*.py" --include="*.toml" --include="*.md" --include="*.json" \
  --exclude-dir=.git --exclude-dir=__pycache__ \
  --exclude-dir=.claude/worktrees --exclude-dir=node_modules \
  | grep -v "estrategias/descartadas/" \
  | grep -v "estrategias/BITACORA.md" \
  | grep -v "Docs/decisions/"
```

If the last command returns hits, go back to Phase E.

## Phase H — Commit

Single commit, no amends. Suggested message:

```
chore(strategies): remove <family>/<name> — <motivo en 1 línea>

- .md moved to descartadas/ (preserved per estrategias/README.md)
- Code, TOML(s), tests, results deleted
- Dispatch unhooked from cli/backtest.py + cli/paper_engine.py
- staging.toml entry removed
- ADR <NNNN> marked SUPERSEDED (if applicable)
- INDICE + BITACORA updated

Reason: <2-3 lines on what killed it — metric + sample window>
```

## Phase I — Post-deploy verification (only if Phase A applied)

After the commit lands on main and CI passes:

```bash
make deploy-staging
make check-staging
make logs-engine | head -100
```

Confirm:
- Engine is up.
- Loaded strategies line does not include `<name>`.
- No `ImportError` / `KeyError` / `ModuleNotFoundError` on startup.
- Heartbeat from `tea-telegram-bot` is fresh.

## Common mistakes (from the 2026-04-26 cleanup)

- **Deleting the `.md` instead of moving it.** Violation of
  `estrategias/README.md` ("Nunca borrar"). The `.md` of a discarded
  strategy is the next session's prior art.
- **Forgetting `paper_engine.py` dispatch.** The backtest dispatch is
  obvious; the paper one lives in a sibling file and gets missed.
- **Forgetting `staging.toml`.** Engine restart fails with
  `ImportError` because it tries to load a strategy whose `.py` is gone.
- **Forgetting skills under `.claude/skills/*.md`.** Real example from
  2026-04-26: `tea-strategy-template/SKILL.md` kept showing
  `from trading.strategies.polymarket_btc5m.last_90s_forecaster_v2 import LGBRunner`
  weeks after v2 was deleted. Skills are loaded into future sessions
  verbatim — stale examples mislead Claude in subsequent runs.
- **Forgetting ADR Superseded marker.** The ADR continues to be read
  as authoritative.
- **Forgetting evaluation profile TOMLs** (`_phase1.toml`, `_phase15.toml`,
  `_v2.toml`). They share the strategy name but are separate files
  invoked manually with `--params`.
- **Deleting helpers shared with surviving strategies.**
  `_lgb_runner.py`, `_microstructure_provider.py`, `_v2_features.py`,
  `_oracle_lag_cesta.py`, `_shared_providers.py`, `_macro_provider.py`,
  `_bb_ofi_features.py` — grep before deleting.
- **Removing the strategy from `staging.toml` and pushing in the same
  commit as the code deletion.** If the deploy fires before the commit
  lands, the engine reloads with `enabled=true` pointing at deleted
  code. Always set `enabled=false` and deploy first (Phase A), then
  delete (Phases B–F) in a separate commit.

## Files affected — checklist

For a strategy named `<family>/<name>` with prefix `<prefix>`:

```
[ ] estrategias/<estado>/<name>.md                  → mv to descartadas/
[ ] src/trading/strategies/<family>/<name>.py       → rm
[ ] config/strategies/<prefix>_<name>*.toml         → rm (all profiles)
[ ] tests/unit/strategies/test_<name>*.py           → rm
[ ] estrategias/resultados/<name>/                  → rm -r
[ ] src/trading/cli/backtest.py                     → edit (dispatch)
[ ] src/trading/cli/paper_engine.py                 → edit (dispatch)
[ ] config/environments/staging.toml                → edit (remove block)
[ ] config/environments/{dev,production}.toml       → edit if present
[ ] estrategias/INDICE.md                           → move row
[ ] estrategias/BITACORA.md                         → append entry
[ ] Docs/decisions/<NNNN>-*.md                      → mark SUPERSEDED
[ ] Docs/runbook.md                                 → edit if present
[ ] .claude/skills/*/SKILL.md                       → edit if examples reference it
[ ] infra/grafana/dashboards/<name>*.json           → rm if dedicated
[ ] src/trading/cli/<name_specific>.py              → rm if dedicated
[ ] scripts/<name_specific>.py                      → rm if dedicated
```
