"""Risk layer: deterministic, bounded sizing; stops; exposure; circuit breaker."""
import pytest

from app.risk import manager as risk

RISK_CFG = {
    "risk_per_trade": 0.01,
    "atr_mult_stop": 2.5,
    "max_open_positions": 8,
    "max_exposure_per_name": 0.20,
    "max_exposure_per_sector": 0.40,
    "max_total_exposure": 1.0,
    "drawdown_circuit_breaker": 0.25,
}


def _state(equity=100000.0, cash=100000.0, peak=100000.0, **kw):
    return risk.PortfolioState(equity=equity, cash=cash, peak_equity=peak, **kw)


def test_stop_price_and_distance():
    sp = risk.compute_stop_price(entry_price=100.0, atr=4.0, atr_mult=2.5)
    assert sp == pytest.approx(90.0)  # 100 - 2.5*4


def test_trailing_stop_only_ratchets_up():
    s1 = risk.update_trailing_stop(90.0, last_close=110.0, atr=4.0, atr_mult=2.5)
    assert s1 == pytest.approx(100.0)  # 110 - 10
    s2 = risk.update_trailing_stop(s1, last_close=95.0, atr=4.0, atr_mult=2.5)
    assert s2 == pytest.approx(100.0)  # never goes down


def test_fixed_fractional_sizing_known_value():
    # risk_amount = 100000*0.01 = 1000; stop_distance = 2.5*4 = 10 -> qty = 100
    res = risk.size_position(
        entry_price=100.0, atr=4.0, state=_state(), risk_cfg=RISK_CFG,
        ticker="pko", sector="banking",
    )
    # per-name cap = 20% * 100000 = 20000 -> max 200 shares; risk caps at 100
    assert res.qty == 100
    assert res.accepted
    assert res.stop_price == pytest.approx(90.0)


def test_per_name_exposure_cap_binds():
    # tiny ATR -> huge risk-based qty, but per-name cap limits it.
    res = risk.size_position(
        entry_price=100.0, atr=0.1, state=_state(), risk_cfg=RISK_CFG,
        ticker="pko", sector="banking",
    )
    # per-name cap 20000 / 100 = 200 shares max
    assert res.qty == 200


def test_sector_exposure_cap_binds():
    state = _state(exposure_by_sector={"banking": 39000.0})
    res = risk.size_position(
        entry_price=100.0, atr=0.1, state=state, risk_cfg=RISK_CFG,
        ticker="peo", sector="banking",
    )
    # sector cap 40000, used 39000 -> 1000 left -> 10 shares
    assert res.qty == 10


def test_total_exposure_cap_binds():
    state = _state(cash=100000.0, exposure_by_name={"x": 95000.0})
    res = risk.size_position(
        entry_price=100.0, atr=0.1, state=state, risk_cfg=RISK_CFG,
        ticker="pko", sector="banking",
    )
    # total cap 100000, used 95000 -> 5000 left -> 50 shares
    assert res.qty == 50


def test_cash_cap_binds():
    state = _state(equity=100000.0, cash=500.0)
    res = risk.size_position(
        entry_price=100.0, atr=0.1, state=state, risk_cfg=RISK_CFG,
        ticker="pko", sector="banking",
    )
    assert res.qty == 5  # only 500 cash / 100


def test_max_open_positions_rejects():
    state = _state(open_positions=8)
    res = risk.size_position(
        entry_price=100.0, atr=4.0, state=state, risk_cfg=RISK_CFG,
        ticker="pko", sector="banking",
    )
    assert not res.accepted
    assert res.rejected_reason == "max_open_positions"


def test_drawdown_circuit_breaker_rejects():
    state = _state(equity=70000.0, peak=100000.0)  # 30% DD > 25%
    res = risk.size_position(
        entry_price=100.0, atr=4.0, state=state, risk_cfg=RISK_CFG,
        ticker="pko", sector="banking",
    )
    assert not res.accepted
    assert res.rejected_reason == "drawdown_circuit_breaker"


def test_sizing_is_deterministic():
    results = {
        risk.size_position(
            entry_price=100.0, atr=4.0, state=_state(), risk_cfg=RISK_CFG,
            ticker="pko", sector="banking",
        ).qty
        for _ in range(50)
    }
    assert results == {100}


def test_qty_never_negative():
    state = _state(cash=0.0)
    res = risk.size_position(
        entry_price=100.0, atr=4.0, state=state, risk_cfg=RISK_CFG,
        ticker="pko", sector="banking",
    )
    assert res.qty == 0
