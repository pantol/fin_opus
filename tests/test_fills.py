"""Fill model: spread, commission, slippage, volume cap (deterministic)."""
import pytest

from app.backtest import fills

COSTS = {
    "commission_bps": 38.0,
    "commission_min": 3.0,
    "spread_bps": 20.0,
    "slippage_bps": 10.0,
    "max_volume_participation": 0.10,
}


def test_buy_pays_more_than_reference():
    f = fills.simulate_fill(side="BUY", requested_qty=10, reference_price=100.0,
                            bar_volume=1_000_000, costs=COSTS)
    # half spread = 100 * 0.0020/2 = 0.10 ; slippage = 100 * 0.0010 = 0.10
    assert f.price == pytest.approx(100.20)
    assert f.qty == 10


def test_sell_receives_less_than_reference():
    f = fills.simulate_fill(side="SELL", requested_qty=10, reference_price=100.0,
                            bar_volume=1_000_000, costs=COSTS)
    assert f.price == pytest.approx(99.80)


def test_commission_minimum_applies_for_small_trade():
    f = fills.simulate_fill(side="BUY", requested_qty=1, reference_price=10.0,
                            bar_volume=1_000_000, costs=COSTS)
    # notional ~10.02 * 0.0038 = 0.038 -> below min -> 3.0
    assert f.fee == pytest.approx(3.0)


def test_commission_bps_applies_for_large_trade():
    f = fills.simulate_fill(side="BUY", requested_qty=1000, reference_price=100.0,
                            bar_volume=10_000_000, costs=COSTS)
    notional = f.price * f.qty
    assert f.fee == pytest.approx(notional * 0.0038)
    assert f.fee > 3.0


def test_volume_cap_limits_quantity():
    # bar volume 1000, participation 10% -> max 100 shares
    f = fills.simulate_fill(side="BUY", requested_qty=500, reference_price=100.0,
                            bar_volume=1000, costs=COSTS)
    assert f.qty == 100
    assert f.capped is True
    assert f.requested_qty == 500


def test_zero_volume_yields_no_fill():
    f = fills.simulate_fill(side="BUY", requested_qty=10, reference_price=100.0,
                            bar_volume=0, costs=COSTS)
    assert f.qty == 0


def test_slippage_cost_accumulates_with_qty():
    f = fills.simulate_fill(side="BUY", requested_qty=10, reference_price=100.0,
                            bar_volume=1_000_000, costs=COSTS)
    # (half_spread 0.10 + slippage 0.10) * 10 = 2.0
    assert f.slippage == pytest.approx(2.0)


def test_fill_is_deterministic():
    prices = [
        fills.simulate_fill(side="BUY", requested_qty=10, reference_price=100.0,
                            bar_volume=1_000_000, costs=COSTS).price
        for _ in range(20)
    ]
    assert len(set(prices)) == 1
    assert prices[0] == pytest.approx(100.20)
