"""Reference data: point-in-time index membership + corporate actions.

Loads the YAML fixtures (config/index_membership.yaml, config/corporate_actions.yaml)
into their tables and derives the back-adjusted price series (adjusted=1 rows)
from raw bars + actions. Raw and adjusted series are NEVER mixed: the backtest
keeps running on raw prices; the adjusted series is materialized alongside so
it can be verified and, by a separate explicit decision, consumed later.

All of this is deterministic code. No LLM anywhere near it.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

from app.ingestion.stooq import Bar, store_bars

ACTION_TYPES = ("dividend", "split", "rights_issue")


@dataclass
class RefdataReport:
    membership_rows: int = 0
    action_rows: int = 0
    adjusted_instruments: int = 0
    adjusted_bars: int = 0
    failures: list[str] | None = None

    def __post_init__(self):
        if self.failures is None:
            self.failures = []

    @property
    def ok(self) -> bool:
        return not self.failures


def _iso(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _instrument_id(conn, ticker: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM instruments WHERE ticker = ?", (ticker.lower(),)
    ).fetchone()
    return int(row["id"]) if row else None


def load_index_membership(conn, membership_cfg: dict, report: RefdataReport) -> None:
    """Load index membership ranges from the YAML fixture.

    The fixture is AUTHORITATIVE per index: existing rows for each index named
    in the YAML are replaced wholesale, so correcting a date or deleting an
    entry in the YAML actually removes the stale row (an upsert on a PK that
    contains the corrected column would silently keep both versions).
    """
    for index_name, entries in (membership_cfg.get("indices") or {}).items():
        conn.execute("DELETE FROM index_membership WHERE index_name = ?",
                     (index_name.lower(),))
        for entry in entries or []:
            ticker = entry["ticker"]
            inst_id = _instrument_id(conn, ticker)
            if inst_id is None:
                report.failures.append(
                    f"index_membership: ticker '{ticker}' not in instruments (run ingest first)"
                )
                continue
            conn.execute(
                """
                INSERT INTO index_membership (index_name, instrument_id, date_from, date_to, source)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(index_name, instrument_id, date_from) DO UPDATE SET
                    date_to=excluded.date_to, source=excluded.source
                """,
                (
                    index_name.lower(),
                    inst_id,
                    _iso(entry["date_from"]),
                    _iso(entry.get("date_to")),
                    entry.get("source"),
                ),
            )
            report.membership_rows += 1
    conn.commit()


def load_corporate_actions(conn, actions_cfg: dict, report: RefdataReport) -> None:
    """Load corporate actions from the YAML fixture.

    The fixture is the SINGLE source of record for this table: existing rows
    are replaced wholesale so a corrected ex-date/type or a deleted entry does
    not leave a stale row that would shield stops on the wrong day, credit a
    dividend twice, or corrupt the derived adjusted series.
    """
    conn.execute("DELETE FROM corporate_actions")
    for entry in actions_cfg.get("actions") or []:
        ticker = entry["ticker"]
        action_type = entry["action_type"]
        if action_type not in ACTION_TYPES:
            report.failures.append(
                f"corporate_actions: unknown action_type '{action_type}' for '{ticker}'"
            )
            continue
        value = float(entry["value_or_ratio"])
        if value <= 0:
            report.failures.append(
                f"corporate_actions: non-positive value_or_ratio for '{ticker}' {action_type}"
            )
            continue
        inst_id = _instrument_id(conn, ticker)
        if inst_id is None:
            report.failures.append(
                f"corporate_actions: ticker '{ticker}' not in instruments (run ingest first)"
            )
            continue
        conn.execute(
            """
            INSERT INTO corporate_actions (instrument_id, action_type, ex_date, value_or_ratio, source)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(instrument_id, action_type, ex_date) DO UPDATE SET
                value_or_ratio=excluded.value_or_ratio, source=excluded.source
            """,
            (inst_id, action_type, _iso(entry["ex_date"]), value, entry.get("source")),
        )
        report.action_rows += 1
    conn.commit()


def load_actions_for_instrument(conn, instrument_id: int) -> list[dict]:
    """Corporate actions for one instrument, sorted by ex_date ascending."""
    rows = conn.execute(
        "SELECT action_type, ex_date, value_or_ratio FROM corporate_actions"
        " WHERE instrument_id = ? ORDER BY ex_date",
        (instrument_id,),
    ).fetchall()
    return [
        {"action_type": r["action_type"], "ex_date": r["ex_date"],
         "value_or_ratio": float(r["value_or_ratio"])}
        for r in rows
    ]


def price_factor(action: dict, close_before_ex: float | None) -> float:
    """Multiplicative factor the price gaps by on the ex-date (post/pre ratio).

    split r          -> 1/r      (price divides by the split ratio)
    dividend D       -> (C - D)/C where C = last cum-dividend close
    rights_issue f   -> f        (theoretical ex-rights price factor)
    """
    kind = action["action_type"]
    value = float(action["value_or_ratio"])
    if kind == "split":
        return 1.0 / value
    if kind == "rights_issue":
        return value
    # dividend
    if not close_before_ex or close_before_ex <= 0:
        return 1.0  # cannot derive a factor without the cum close; no-op
    return max(0.0, (close_before_ex - value) / close_before_ex)


def back_adjust(bars: list[Bar], actions: list[dict]) -> list[Bar]:
    """Back-adjust raw bars so the LAST bar equals the raw series.

    Every bar strictly BEFORE an ex-date is multiplied by that action's price
    factor (cumulatively across actions). Volume is scaled inversely for splits
    only (share count changes); dividends/rights leave volume untouched.
    Deterministic; input order does not matter.
    """
    if not bars:
        return []
    ordered = sorted(bars, key=lambda b: b.date)
    close_by_date = {b.date: b.close for b in ordered}
    dates = [b.date for b in ordered]

    def _close_before(ex_date: str) -> float | None:
        prior = [d for d in dates if d < ex_date]
        return close_by_date[prior[-1]] if prior else None

    adjusted: list[Bar] = []
    for bar in ordered:
        pf = 1.0
        vf = 1.0
        for action in actions:
            if bar.date < action["ex_date"]:
                f = price_factor(action, _close_before(action["ex_date"]))
                pf *= f
                if action["action_type"] == "split":
                    vf *= float(action["value_or_ratio"])
        def _scaled(v, factor):
            return None if v is None else v * factor

        adjusted.append(Bar(
            date=bar.date, as_of_date=bar.as_of_date,
            open=_scaled(bar.open, pf), high=_scaled(bar.high, pf),
            low=_scaled(bar.low, pf), close=_scaled(bar.close, pf),
            volume=_scaled(bar.volume, vf),
        ))
    return adjusted


def derive_adjusted_series(conn, instrument_id: int) -> int:
    """Materialize adjusted=1 rows for one instrument from raw bars + actions.

    Returns the number of bars written (0 when the instrument has no actions —
    an adjusted series identical to raw would only invite accidental mixing).
    """
    actions = load_actions_for_instrument(conn, instrument_id)
    if not actions:
        return 0
    rows = conn.execute(
        "SELECT date, as_of_date, open, high, low, close, volume, source FROM prices"
        " WHERE instrument_id = ? AND adjusted = 0 ORDER BY date",
        (instrument_id,),
    ).fetchall()
    bars = [
        Bar(date=r["date"], as_of_date=r["as_of_date"], open=r["open"], high=r["high"],
            low=r["low"], close=r["close"], volume=r["volume"])
        for r in rows
    ]
    if not bars:
        return 0
    # Each adjusted row inherits the provenance of the raw row it derives from
    # (a real series may legitimately mix 'gpw' and 'stooq' segments). Paired
    # by DATE, not by position, so a back_adjust that ever drops or reorders
    # bars cannot silently shift labels between rows (an unknown date fails
    # loudly with KeyError instead).
    source_by_date = {r["date"]: r["source"] for r in rows}
    n = 0
    for source, group in itertools.groupby(
            back_adjust(bars, actions), key=lambda b: source_by_date[b.date]):
        n += store_bars(conn, instrument_id, list(group),
                        adjusted=True, source=source)
    conn.commit()
    return n


def load_refdata(conn, membership_cfg: dict, actions_cfg: dict) -> RefdataReport:
    """Load both fixtures and rebuild adjusted series for affected instruments.

    The adjusted series is fully rebuilt: stale adjusted rows from actions
    that were since corrected or removed are wiped first (adjusted=1 rows are
    only ever produced here, so the wipe cannot destroy source data).
    """
    report = RefdataReport()
    load_index_membership(conn, membership_cfg, report)
    load_corporate_actions(conn, actions_cfg, report)
    conn.execute("DELETE FROM prices WHERE adjusted = 1")
    conn.commit()
    ids = [
        int(r["instrument_id"]) for r in conn.execute(
            "SELECT DISTINCT instrument_id FROM corporate_actions"
        ).fetchall()
    ]
    for inst_id in ids:
        n = derive_adjusted_series(conn, inst_id)
        if n:
            report.adjusted_instruments += 1
            report.adjusted_bars += n
    return report
