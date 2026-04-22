from trading.engine.fill_model import FillParams, deterministic_rng, settle, simulate_fill
from trading.engine.types import Side


def test_deterministic_rng_stable():
    a = [deterministic_rng("abc").random() for _ in range(3)]
    b = [deterministic_rng("abc").random() for _ in range(3)]
    assert a == b


def test_simulate_fill_returns_price_and_fee():
    res = simulate_fill(
        side=Side.YES_UP,
        pm_yes_ask=0.51,
        pm_no_ask=0.49,
        stake_usd=3.0,
        params=FillParams(fee_k=0.05, slippage_bps=10.0, fill_probability=1.0),
        seed_source="test1",
    )
    assert res.filled is True
    # slippage = 0.51 * 10 / 10000 = 5.1e-4
    assert 0.5105 <= res.entry_price <= 0.5106
    # parabolic fee ~0.05 * p * (1-p) * stake
    assert res.fee > 0


def test_simulate_fill_fails_when_probability_fails():
    # fill_probability=0 forces miss regardless of seed.
    res = simulate_fill(
        side=Side.YES_UP,
        pm_yes_ask=0.51,
        pm_no_ask=0.49,
        stake_usd=3.0,
        params=FillParams(fee_k=0.05, slippage_bps=10.0, fill_probability=0.0),
        seed_source="test2",
    )
    assert res.filled is False


def test_settle_win_pays_positive():
    # entry 0.51, stake 3, win → shares = 5.88, pnl = 5.88 - 3 - fee
    resolution, exit_price, pnl = settle(
        side=Side.YES_UP, entry_price=0.51, stake_usd=3.0, fee=0.0, outcome_went_up=True
    )
    assert resolution == "win"
    assert exit_price == 1.0
    assert pnl > 2.8


def test_settle_loss_returns_minus_stake():
    resolution, exit_price, pnl = settle(
        side=Side.YES_UP, entry_price=0.51, stake_usd=3.0, fee=0.0, outcome_went_up=False
    )
    assert resolution == "loss"
    assert exit_price == 0.0
    assert pnl == -3.0


def test_settle_loss_yes_down_on_up():
    resolution, _, pnl = settle(
        side=Side.YES_DOWN, entry_price=0.49, stake_usd=3.0, fee=0.0, outcome_went_up=True
    )
    assert resolution == "loss"
    assert pnl == -3.0
