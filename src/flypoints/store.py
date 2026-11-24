"""Queryable SQLite store of live award offers and of which searches have been run.

``offers`` holds one row per distinct offer (upserted); ``searches`` records every completed
live search, which is what tells "searched, nothing found" apart from "never searched".
Re-recording a window replaces what was stored for it, so space that disappeared is removed.
Row ``fetched_at`` is the search time (epoch seconds), not the offer's own timestamp.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import paths
from .models import AwardOffer, Cabin

RETENTION_S = 30 * 24 * 3600


def _codes(cabins: list[Cabin]) -> list[str]:
    return sorted({Cabin(c).value for c in cabins})


def _ts(when: datetime | None) -> float:
    return time.time() if when is None else when.timestamp()


class OfferStore:
    def __init__(self, path: Path | str | None = None):
        if path is None:
            paths.ensure_state_dir()
            path = paths.STATE_DIR / "offers.sqlite"
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS offers ("
                "provider TEXT NOT NULL, program_code TEXT NOT NULL, origin TEXT NOT NULL, "
                "destination TEXT NOT NULL, date TEXT NOT NULL, cabin TEXT NOT NULL, "
                "flight_numbers TEXT NOT NULL, miles INTEGER NOT NULL, taxes_usd REAL NOT NULL, "
                "seats INTEGER, fetched_at REAL NOT NULL, offer_json TEXT NOT NULL, "
                "PRIMARY KEY (provider, program_code, origin, destination, date, cabin, flight_numbers))"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS searches ("
                "provider TEXT NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL, "
                "date_from TEXT NOT NULL, date_to TEXT NOT NULL, cabins TEXT NOT NULL, "
                "searched_at REAL NOT NULL)"
            )
            self._db.execute("CREATE INDEX IF NOT EXISTS searches_route ON searches (origin, destination)")

    def record_search(
        self,
        provider: str,
        origin: str,
        destination: str,
        date_from: date,
        date_to: date,
        cabins: list[Cabin],
        offers: list[AwardOffer],
        searched_at: datetime | None = None,
    ) -> None:
        """Record a completed live search and replace the offers stored for that provider, route,
        dates and cabins with ``offers`` (one transaction). Offers outside the route, window or
        cabins are ignored; among duplicates of one key the cheapest is kept."""
        codes = _codes(cabins)
        ts = _ts(searched_at)
        best: dict[tuple, AwardOffer] = {}
        for o in offers:
            if (
                o.origin == origin
                and o.destination == destination
                and date_from <= o.date <= date_to
                and o.cabin.value in codes
            ):
                key = (o.program_code, o.date, o.cabin, tuple(s.flight_number for s in o.segments))
                old = best.get(key)
                if old is None or (o.miles, o.taxes_usd) < (old.miles, old.taxes_usd):
                    best[key] = o
        marks = ",".join("?" * len(codes))
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM offers WHERE provider = ? AND origin = ? AND destination = ? "
                f"AND date BETWEEN ? AND ? AND cabin IN ({marks})",
                (provider, origin, destination, date_from.isoformat(), date_to.isoformat(), *codes),
            )
            for (program, day, cabin, flights), o in best.items():
                self._db.execute(
                    "INSERT INTO offers VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT (provider, program_code, origin, destination, date, cabin, flight_numbers) "
                    "DO UPDATE SET "
                    "miles=excluded.miles, taxes_usd=excluded.taxes_usd, seats=excluded.seats, "
                    "fetched_at=excluded.fetched_at, offer_json=excluded.offer_json "
                    "WHERE excluded.fetched_at >= offers.fetched_at",
                    (provider, program, origin, destination, day.isoformat(), cabin.value, ",".join(flights),
                     o.miles, o.taxes_usd, o.seats, ts, o.model_dump_json()),
                )
            self._db.execute(
                "INSERT INTO searches VALUES (?,?,?,?,?,?,?)",
                (provider, origin, destination, date_from.isoformat(), date_to.isoformat(), ",".join(codes), ts),
            )

    def query(
        self,
        origins: list[str],
        destinations: list[str],
        date_from: date,
        date_to: date,
        cabins: list[Cabin],
        max_age_s: float | None = None,
        now: datetime | None = None,
        provider: str | None = None,
    ) -> list[AwardOffer]:
        """Stored offers for the airports, inclusive dates and cabins, no older than ``max_age_s``
        (optionally from one provider only)."""
        codes = _codes(cabins)
        cutoff = _ts(now) - max_age_s if max_age_s is not None else 0.0
        o_marks, d_marks, c_marks = (",".join("?" * len(x)) for x in (origins, destinations, codes))
        with self._lock:
            rows = self._db.execute(
                "SELECT offer_json FROM offers WHERE "
                f"origin IN ({o_marks}) AND destination IN ({d_marks}) AND cabin IN ({c_marks}) "
                "AND date BETWEEN ? AND ? AND fetched_at >= ? AND (? IS NULL OR provider = ?) "
                "ORDER BY date, miles",
                (*origins, *destinations, *codes, date_from.isoformat(), date_to.isoformat(), cutoff,
                 provider, provider),
            ).fetchall()
        return [AwardOffer.model_validate_json(r[0]) for r in rows]

    def coverage(
        self,
        origin: str,
        destination: str,
        date_from: date,
        date_to: date,
        cabins: list[Cabin],
        max_age_s: float | None = None,
        now: datetime | None = None,
        provider: str | None = None,
    ) -> set[date]:
        """Dates in the window covered, for every requested cabin, by a search no older than
        ``max_age_s`` (optionally by one provider). Includes "searched, found nothing"."""
        wanted = _codes(cabins)
        cutoff = _ts(now) - max_age_s if max_age_s is not None else 0.0
        with self._lock:
            rows = self._db.execute(
                "SELECT date_from, date_to, cabins FROM searches WHERE origin = ? AND destination = ? "
                "AND date_to >= ? AND date_from <= ? AND searched_at >= ? AND (? IS NULL OR provider = ?)",
                (origin, destination, date_from.isoformat(), date_to.isoformat(), cutoff, provider, provider),
            ).fetchall()
        spans = [(date.fromisoformat(a), date.fromisoformat(b), set(c.split(","))) for a, b, c in rows]
        out, day = set(), date_from
        while day <= date_to:
            if all(any(a <= day <= b and cabin in cs for a, b, cs in spans) for cabin in wanted):
                out.add(day)
            day += timedelta(days=1)
        return out

    def prune(self, today: date, now: datetime | None = None) -> int:
        """Delete offers and searches for departure dates before ``today`` and rows older than 30
        days; returns how many rows were removed."""
        cutoff = _ts(now) - RETENTION_S
        with self._lock, self._db:
            n = self._db.execute(
                "DELETE FROM offers WHERE date < ? OR fetched_at < ?", (today.isoformat(), cutoff)
            ).rowcount
            n += self._db.execute(
                "DELETE FROM searches WHERE date_to < ? OR searched_at < ?", (today.isoformat(), cutoff)
            ).rowcount
        return n

    def close(self) -> None:
        with self._lock:
            self._db.close()
