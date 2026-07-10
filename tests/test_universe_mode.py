"""A2: full-market universe mode — the anti-survivorship escape hatch.

mode=config trades only the hand-picked universe.yaml list (a demo subset
chosen with today's knowledge — survivorship-biased by construction);
mode=full_market trades every ingested non-index instrument, deriving
delisting dates from the last printed bar for archive-discovered rows so
dead tickers stop being perpetual entry candidates.
"""
import pandas as pd

from app.backtest import engine
from app.ingestion import stooq

from tests.conftest import make_stooq_csv, synthetic_series


def _ingest(conn, ticker, rows, **inst):
    iid = stooq.upsert_instrument(conn, {"ticker": ticker, "name": ticker, **inst},
                                  is_index=inst.get("is_index", False))
    stooq.store_bars(conn, iid, stooq.parse_csv(make_stooq_csv(rows)))
    return iid


def _universe():
    return {"benchmark": {"ticker": "wig20tr", "is_index": True}, "indices": [],
            "instruments": [{"ticker": "aaa", "sector": "tech"}]}


def _seed(conn):
    n = 300
    _ingest(conn, "wig20tr", synthetic_series(n=n, base=2000, drift=0.0),
            is_index=True)
    # hand-listed, alive
    _ingest(conn, "aaa", synthetic_series(n=n, base=100, drift=0.0008),
            sector="tech", listed_from="2018-01-01")
    # archive-style rows (ticker=isin, no listing metadata):
    # one dead (bars stop ~150 sessions early), one still trading
    dead_rows = synthetic_series(n=n, base=50, drift=0.0)[: n - 150]
    _ingest(conn, "plcompany0001", dead_rows)
    _ingest(conn, "plcompany0002", synthetic_series(n=n, base=30, drift=0.0))
    conn.commit()


def test_config_mode_trades_only_the_hand_list(conn):
    _seed(conn)
    instruments, _ = engine.load_instruments(conn, _universe(), "wig20tr")
    assert {i.ticker for i in instruments} == {"aaa"}


def test_full_market_mode_reaches_archive_instruments(conn):
    _seed(conn)
    instruments, _ = engine.load_instruments(conn, _universe(), "wig20tr",
                                             mode="full_market")
    assert {i.ticker for i in instruments} == \
        {"aaa", "plcompany0001", "plcompany0002"}


def test_full_market_derives_delisting_from_last_bar(conn):
    _seed(conn)
    instruments, _ = engine.load_instruments(conn, _universe(), "wig20tr",
                                             mode="full_market")
    by_ticker = {i.ticker: i for i in instruments}
    dead = by_ticker["plcompany0001"]
    alive = by_ticker["plcompany0002"]
    # dead ticker: delisted on its last printed bar, no longer alive after
    assert dead.delisted_on == dead.prices.index[-1].date().isoformat()
    after = dead.prices.index[-1] + pd.Timedelta(days=5)
    assert not engine._alive(dead, after)
    # still-trading archive ticker keeps trading (no derived delisting)
    assert alive.delisted_on is None
    # hand-listed metadata is never overridden
    assert by_ticker["aaa"].delisted_on is None
    assert by_ticker["aaa"].listed_from == "2018-01-01"


def test_delist_gap_boundary(conn):
    """Pin both sides of FULL_MARKET_DELIST_GAP_DAYS: a short pause is a
    suspension (stale mark, still tradable), a long one is a delisting."""
    n = 300
    _ingest(conn, "wig20tr", synthetic_series(n=n, base=2000, drift=0.0),
            is_index=True)
    _ingest(conn, "aaa", synthetic_series(n=n, base=100, drift=0.0),
            sector="tech", listed_from="2018-01-01")
    full = synthetic_series(n=n, base=40, drift=0.0)
    _ingest(conn, "plnear0000001", full[:-5])    # ~7 calendar days of silence
    _ingest(conn, "plfar00000001", full[:-20])   # ~28 calendar days of silence
    conn.commit()
    instruments, _ = engine.load_instruments(conn, _universe(), "wig20tr",
                                             mode="full_market")
    by_ticker = {i.ticker: i for i in instruments}
    market_last = max(i.prices.index[-1] for i in instruments)
    gap = pd.Timedelta(days=engine.FULL_MARKET_DELIST_GAP_DAYS)
    # fixture sanity: the two names genuinely sit on opposite sides of the cut
    assert market_last - by_ticker["plnear0000001"].prices.index[-1] <= gap
    assert market_last - by_ticker["plfar00000001"].prices.index[-1] > gap
    assert by_ticker["plnear0000001"].delisted_on is None       # suspension
    assert by_ticker["plfar00000001"].delisted_on == \
        by_ticker["plfar00000001"].prices.index[-1].date().isoformat()


def test_dead_archive_ticker_is_written_off_in_marking(conn):
    """The derived delisting date feeds the A4 write-off convention."""
    _seed(conn)
    instruments, _ = engine.load_instruments(conn, _universe(), "wig20tr",
                                             mode="full_market")
    dead = {i.ticker: i for i in instruments}["plcompany0001"]
    after = dead.prices.index[-1] + pd.Timedelta(days=30)
    assert engine._mark_price(dead, after) is None
