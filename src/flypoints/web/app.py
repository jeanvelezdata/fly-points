"""FastAPI + Jinja2 + HTMX dashboard: a thin layer over ``flypoints.engine.search``.

Jobs run one at a time on a single worker thread (protects the PointsYeah account); the HTTP
handlers only read job state, so the server stays responsive during a long search.
"""
from __future__ import annotations

import json
import logging
import math
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import booking, config, engine, paths
from ..cache import Cache
from ..cli import _other_bank_points
from ..models import Bank, Cabin, OptionFilters, RankedOption, SearchQuery
from ..providers.base import AuthError
from ..views import (
    compare_grid, flatten_csv, mixed_cabin_marker, option_route as _route, option_times, parse_banks, parse_cabins,
    seen_label, segment_lines,
)

HERE = Path(__file__).resolve().parent
MAX_JOBS_KEPT = 50
log = logging.getLogger(__name__)


# --- limits for a shared deployment ---------------------------------------------------------


def _env_int(name: str) -> int | None:
    text = os.environ.get(name, "").strip()
    return int(text) if text else None


@dataclass(frozen=True)
class Limits:
    """Server-side guards for a shared PointsYeah token. None means no limit (local default)."""

    max_searches: int | None = None  # per live search job
    max_sweep_searches: int | None = None  # per sweep job
    daily_searches: int | None = None  # live PointsYeah searches per day, all users together
    auth_message: str | None = None  # shown instead of the provider's AuthError text

    @classmethod
    def from_env(cls) -> "Limits":
        return cls(
            max_searches=_env_int("FLYPOINTS_MAX_SEARCHES"),
            max_sweep_searches=_env_int("FLYPOINTS_MAX_SWEEP_SEARCHES"),
            daily_searches=_env_int("FLYPOINTS_DAILY_SEARCHES"),
            auth_message=os.environ.get("FLYPOINTS_AUTH_MESSAGE", "").strip() or None,
        )


class DailyBudget:
    """Live searches used today, kept in the Cache table so a restart doesn't reset it.
    Only the job worker thread spends it, so there is no check-then-spend race."""

    NAMESPACE = "web_budget"

    def __init__(self, cache: Cache, limit: int):
        self._cache, self.limit = cache, limit

    def used(self, today: date | None = None) -> int:
        return self._cache.get_json(self.NAMESPACE, (today or date.today()).isoformat()) or 0

    def remaining(self, today: date | None = None) -> int:
        return max(self.limit - self.used(today), 0)

    def spend(self, n: int, today: date | None = None) -> None:
        today = today or date.today()
        if n > 0:
            self._cache.put_json(self.NAMESPACE, today.isoformat(), self.used(today) + n, 2 * 86400)
TRUE = {"on", "true", "1", "yes"}
DAYS = [("0", "Mon"), ("1", "Tue"), ("2", "Wed"), ("3", "Thu"), ("4", "Fri"), ("5", "Sat"), ("6", "Sun")]

# --- form parsing ---------------------------------------------------------------------------


def _flag(value: Any) -> bool:
    return str(value).lower() in TRUE


def _tokens(text: str) -> list[str]:
    return [t.strip() for t in str(text or "").replace(";", ",").split(",") if t.strip()]


def _parse_date(text: str, label: str, errors: list[str]) -> date | None:
    try:
        return datetime.strptime(str(text).strip(), "%Y-%m-%d").date()
    except ValueError:
        errors.append(f"{label}: use a date like 2026-12-01.")
        return None


def parse_params(raw: Mapping[str, Any]) -> tuple[dict | None, list[str]]:
    """Validate a raw form/saved-search mapping. Returns (normalized params or None, errors)."""
    errors: list[str] = []
    clean: dict[str, Any] = {}
    for field, label in (("origins", "Origins"), ("destinations", "Destinations")):
        tokens = _tokens(raw.get(field, ""))
        if not tokens:
            errors.append(f"{label}: enter at least one airport code or region.")
        for token in tokens:
            try:
                config.expand_airports(token)
            except ValueError:
                errors.append(f"{label}: unknown airport or region {token!r}.")
        clean[field] = ", ".join(tokens)

    d_from = _parse_date(raw.get("date_from", ""), "From date", errors)
    to_text = str(raw.get("date_to", "")).strip() or str(raw.get("date_from", ""))
    d_to = _parse_date(to_text, "To date", errors)
    if d_from and d_to and d_to < d_from:
        errors.append("To date is before the from date.")
    clean["date_from"] = d_from.isoformat() if d_from else ""
    clean["date_to"] = d_to.isoformat() if d_to else ""

    try:
        cabins = parse_cabins(raw.get("cabins") or raw.get("cabin") or [])
        if not cabins:
            raise ValueError
    except ValueError:
        errors.append("Cabin: choose at least one of Y, W, J or F.")
        cabins = [Cabin.J]
    clean["cabins"] = list(dict.fromkeys(c.value for c in cabins))

    try:
        max_searches = int(raw.get("max_searches", 8))
        if max_searches < 1:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("Max searches: enter a whole number of 1 or more.")
        max_searches = 8
    clean["max_searches"] = max_searches

    programs = raw.get("programs") or []
    programs = [programs] if isinstance(programs, str) else list(programs)
    known = config.load_programs()
    errors += [f"Unknown program {code!r}." for code in programs if code not in known]
    clean["programs"] = programs
    for flag in ("include_cash", "include_estimates", "refresh", "direct_only"):
        clean[flag] = _flag(raw.get(flag, False))

    for key, label, cast in (
        ("max_stops", "Max stops", int), ("max_taxes_usd", "Max taxes", float), ("max_points", "Max points", int),
        ("min_seats", "Min seats", int), ("min_cabin_pct", "Min cabin %", int),
    ):
        clean[key] = _number(raw, key, label, cast, errors)
    clean["carriers"] = [c.upper() for c in flatten_csv(raw.get("carriers"))]
    try:
        clean["days_of_week"] = sorted({int(d) for d in raw.get("days_of_week") or []})
        if any(not 0 <= d <= 6 for d in clean["days_of_week"]):
            raise ValueError
    except ValueError:
        errors.append("Days: choose weekdays from the list.")
        clean["days_of_week"] = []
    try:
        clean["banks"] = [b.value for b in parse_banks(raw.get("banks") or [])]
    except ValueError as e:
        errors.append(str(e))
        clean["banks"] = []
    max_age = _number(raw, "max_age_hours", "Stored max age", float, errors)
    clean["max_age_hours"] = engine.STORED_MAX_AGE_H if max_age is None else max_age
    return (None if errors else clean), errors


def parse_sweep_params(raw: Mapping[str, Any]) -> tuple[dict | None, list[str]]:
    """Like ``parse_params`` plus ``fresh_hours``; the sweep only uses the route, dates, cabins and caps."""
    params, errors = parse_params(raw)
    fresh = _number(raw, "fresh_hours", "Fresh hours", float, errors)
    if params is None or errors:
        return None, errors
    params["fresh_hours"] = 24.0 if fresh is None else fresh
    return params, errors


def _number(raw: Mapping[str, Any], key: str, label: str, cast: Callable, errors: list[str]):
    """A blank value is None; otherwise a finite number of 0 or more, or an error is recorded."""
    text = str(raw.get(key) if raw.get(key) is not None else "").strip()
    if not text:
        return None
    try:
        n = cast(text)
        if not math.isfinite(n) or n < 0:
            raise ValueError
        return n
    except ValueError:
        errors.append(f"{label}: enter a number of 0 or more.")
        return None


def form_to_raw(form) -> dict:
    keys = (
        "origins", "destinations", "date_from", "date_to", "max_searches", "max_stops", "carriers", "max_taxes_usd",
        "max_points", "min_seats", "min_cabin_pct", "max_age_hours", "fresh_hours",
    )
    raw: dict[str, Any] = {k: form.get(k, "") for k in keys}
    raw["cabins"] = form.getlist("cabins") or form.getlist("cabin")
    for key in ("programs", "days_of_week", "banks"):
        raw[key] = form.getlist(key)
    for flag in ("include_cash", "include_estimates", "refresh", "direct_only"):
        raw[flag] = flag in form
    return raw


def build_query(params: dict) -> SearchQuery:
    return SearchQuery(
        origins=_tokens(params["origins"]),
        destinations=_tokens(params["destinations"]),
        date_from=date.fromisoformat(params["date_from"]),
        date_to=date.fromisoformat(params["date_to"]),
        cabins=[Cabin(c) for c in params["cabins"]],
        max_searches=params["max_searches"],
        programs=params["programs"] or None,
        direct_only=params["direct_only"],
        max_stops=params["max_stops"],
        carriers=params["carriers"] or None,
        days_of_week=params["days_of_week"] or None,
        max_taxes_usd=params["max_taxes_usd"],
        max_points=params["max_points"],
        min_seats=params["min_seats"],
        banks=[Bank(b) for b in params["banks"]] or None,
        min_cabin_pct=params["min_cabin_pct"],
    )


# --- result rows: sorting and filtering -----------------------------------------------------

SORT_COLUMNS = ("date", "depart", "route", "stops", "program", "cabin", "miles", "taxes", "points", "bank", "cash", "cpp", "seats")
COLUMNS = [
    ("date", "Date"), ("depart", "Depart"), (None, "Arrive"), ("route", "Route"), (None, "Flights"), ("stops", "Stops"), ("program", "Program"), ("cabin", "Cabin"), ("miles", "Miles"),
    ("taxes", "Taxes"), ("points", "Best bank pts"), ("bank", "Best bank"), (None, "Other bank"),
    ("cash", "Cash"), ("cpp", "cpp"), ("seats", "Seats"), (None, "Seen"), (None, "Flags"), (None, "Note"), (None, "Book"),
]
NUMERIC = {"stops", "miles", "taxes", "points", "cash", "cpp", "seats"}


def safe_url(url: str | None) -> str | None:
    """Only http(s) links are rendered, whatever a provider or data file contains."""
    return url if url and url.startswith(("http://", "https://")) else None


def make_row(o: RankedOption, idx: int) -> dict:
    """``idx`` is the option's position in the job's full result list (used for the booking panel)."""
    a = o.offer
    marker, detail = mixed_cabin_marker(a)
    depart, arrive = option_times(o) or (None, None)
    return {
        "idx": idx, "date": a.date.isoformat(), "depart": depart, "arrive": arrive, "route": _route(o),
        "flights": " · ".join(s.flight_number for s in a.segments),  # operating flights, e.g. "AV259 · IB424"
        "stops": len(a.segments) - 1 if a.segments else None,  # None: no routing (chart estimates)
        "program": a.program_code, "cabin": a.cabin.value, "mix": marker, "mix_detail": detail,
        "miles": a.miles, "taxes": a.taxes_usd, "points": o.best_bank_points,
        "bank": o.best_bank.label if o.best_bank else None, "other": _other_bank_points(o),
        "cash": o.cash_fare.price_usd if o.cash_fare else None, "cpp": o.cpp, "seats": a.seats,
        "seen": seen_label(a), "estimated": a.estimated, "note": o.note or "", "url": safe_url(a.booking_url),
    }


def make_grid(options: list[RankedOption], idx_of: Mapping[int, int]) -> list[dict]:
    """Compare-view rows; ``idx_of`` maps id(option) to its position in the job's result list."""
    grid = []
    for row in compare_grid(options):
        cells = {}
        for cabin, c in row.cells.items():
            marker, detail = mixed_cabin_marker(c.option.offer)
            cells[cabin.value] = {
                "idx": idx_of[id(c.option)], "points": c.points, "bank": c.bank, "miles": c.miles, "seats": c.seats,
                "estimated": c.estimated, "mix": marker, "mix_detail": detail, "seen": seen_label(c.option.offer),
            }
        grid.append({"program": row.program_code, "route": row.route, "date": row.date.isoformat(), "cells": cells})
    return grid


def _num(text: Any) -> float | None:
    try:
        return float(str(text).replace(",", ""))
    except ValueError:
        return None


def _multi(q: Mapping[str, Any], key: str) -> list[str]:
    """All values of a repeated query param (a plain mapping gives its single value)."""
    if hasattr(q, "getlist"):
        values = q.getlist(key)
    else:
        v = q.get(key, [])
        values = [v] if isinstance(v, str) else list(v)
    return [v for v in values if v != ""]


def option_filters(q: Mapping[str, str]) -> OptionFilters:
    """The engine filters set by the results filter form (blank or invalid values are ignored)."""
    max_points, max_taxes, min_seats, max_stops, min_cabin_pct = (
        n if n is not None and math.isfinite(n) else None
        for n in (_num(q.get(k, "")) for k in ("max_points", "max_taxes", "min_seats", "max_stops", "min_cabin_pct"))
    )
    days = sorted({int(d) for d in _multi(q, "days") if d.isdigit() and int(d) <= 6})
    return OptionFilters(
        max_points=None if max_points is None else int(max_points),
        max_taxes_usd=max_taxes,
        min_seats=None if min_seats is None else int(min_seats),
        max_stops=None if max_stops is None else int(max_stops),
        carriers=flatten_csv(q.get("carriers", "")) or None,
        days_of_week=days or None,
        min_cabin_pct=None if min_cabin_pct is None else int(min_cabin_pct),
    )


def select_rows(rows: list[dict], q: Mapping[str, str]) -> list[dict]:
    """Apply the program / hide-estimates filters, then the sort, from query params."""
    out = [
        r for r in rows
        if (not q.get("program") or r["program"] == q["program"])
        and (not _flag(q.get("hide_est", "")) or not r["estimated"])
    ]
    column = q.get("sort", "")
    if column in SORT_COLUMNS:
        known = [r for r in out if r[column] is not None]
        missing = [r for r in out if r[column] is None]
        out = sorted(known, key=lambda r: r[column], reverse=q.get("dir") == "desc") + missing
    return out


# --- jobs -----------------------------------------------------------------------------------


class Job:
    def __init__(self, params: dict, kind: str = "search"):
        self.id = uuid.uuid4().hex[:10]
        self.params = params
        self.kind = kind  # search | sweep
        self.status = "queued"  # queued | running | done | error
        self.done = 0
        self.total = 0
        self.message = ""
        self.result: engine.SearchResult | None = None
        self.rows: list[dict] = []
        self.stats: engine.SweepStats | None = None  # sweeps only
        self.stop_event = threading.Event()  # sweeps only
        self.error: str | None = None
        self.started: float | None = None
        self.finished: float | None = None

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")

    @property
    def percent(self) -> int:
        return int(100 * self.done / self.total) if self.total else 0

    @property
    def elapsed(self) -> int:
        if self.started is None:
            return 0
        return int((self.finished or time.time()) - self.started)

    @property
    def title(self) -> str:
        p = self.params
        text = f"{p['origins']} to {p['destinations']}, {p['date_from']}..{p['date_to']}, {','.join(p['cabins'])}"
        if self.kind == "sweep":
            return f"Sweep {text}"
        return f"{text} (stored)" if p.get("stored") else text


class JobRunner:
    """FIFO queue drained by one daemon thread, so only one live search or sweep runs at a time.
    Stored searches read only the local store, so they run immediately in the caller's thread."""

    def __init__(
        self, search_fn, cache, stored_fn=engine.search_stored, sweep_fn=engine.sweep,
        budget: DailyBudget | None = None, auth_message: str | None = None,
    ):
        self._search_fn, self._cache = search_fn, cache
        self._stored_fn, self._sweep_fn = stored_fn, sweep_fn
        self._budget, self._auth_message = budget, auth_message
        self._jobs: dict[str, Job] = {}
        self._queue: queue.Queue[Job] = queue.Queue()
        self._lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True, name="flypoints-jobs").start()

    def _register(self, job: Job) -> Job:
        with self._lock:
            self._jobs[job.id] = job
            for old in [j.id for j in self._jobs.values() if not j.active][:-MAX_JOBS_KEPT]:
                del self._jobs[old]
        return job

    def submit(self, params: dict, kind: str = "search") -> Job:
        job = self._register(Job(params, kind))
        self._queue.put(job)
        return job

    def submit_stored(self, params: dict) -> Job:
        job = self._register(Job({**params, "stored": True}))
        self._run(job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _worker(self) -> None:
        while True:
            self._run(self._queue.get())

    def _run(self, job: Job) -> None:
        def on_progress(done: int, total: int, message: str) -> None:
            job.done, job.total, job.message = done, total, message

        job.started, job.status = time.time(), "running"
        p = job.params
        live = not p.get("stored")
        remaining = self._budget.remaining() if self._budget and live else None
        if remaining:  # spend at most what is left of today's budget
            p = job.params = {**p, "max_searches": min(p["max_searches"], remaining)}
        if job.kind == "sweep" and remaining == 0:
            job.error = f"Today's live-search budget ({self._budget.limit}) is used up; try again tomorrow."
            job.status, job.finished = "error", time.time()
            return
        try:
            if job.kind == "sweep":
                job.stats = self._sweep_fn(
                    _tokens(p["origins"]), _tokens(p["destinations"]), date.fromisoformat(p["date_from"]),
                    date.fromisoformat(p["date_to"]), [Cabin(c) for c in p["cabins"]], p["max_searches"],
                    p["fresh_hours"], progress=on_progress, stop_event=job.stop_event,
                )
                if self._budget:
                    self._budget.spend(job.stats.run)
            else:
                if p.get("stored") or remaining == 0:
                    result = self._stored_fn(
                        build_query(p), cache=self._cache, max_age_hours=p["max_age_hours"],
                        include_estimates=p["include_estimates"],
                    )
                    if remaining == 0:
                        job.params = {**p, "stored": True}
                        result.warnings.insert(0, f"Today's live-search budget ({self._budget.limit}) is used up, "
                                                  "so these are stored results only.")
                else:
                    result = self._search_fn(
                        build_query(p), cache=self._cache, progress=on_progress, refresh=p["refresh"],
                        include_estimates=p["include_estimates"], include_cash=p["include_cash"],
                    )
                    if self._budget:
                        self._budget.spend(result.live_searches_run)
                job.result, job.rows = result, [make_row(o, i) for i, o in enumerate(result.options)]
            job.status = "done"
        except AuthError as e:
            if self._auth_message:
                log.warning("award search login failed: %s", e)
            job.error, job.status = self._auth_message or str(e), "error"
        except Exception as e:  # keep the worker alive whatever a search throws
            job.error, job.status = f"{type(e).__name__}: {e}", "error"
        job.finished = time.time()


# --- saved searches -------------------------------------------------------------------------


class SavedSearches:
    def __init__(self, path: Path):
        self.path, self._lock = path, threading.Lock()

    def _read(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return []

    def _write(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def list(self) -> list[dict]:
        with self._lock:
            return self._read()

    def add(self, name: str, params: dict) -> None:
        with self._lock:
            self._write(self._read() + [{"id": uuid.uuid4().hex[:8], "name": name, "params": params}])

    def get(self, item_id: str) -> dict | None:
        return next((i for i in self.list() if i["id"] == item_id), None)

    def delete(self, item_id: str) -> None:
        with self._lock:
            self._write([i for i in self._read() if i["id"] != item_id])


# --- app ------------------------------------------------------------------------------------


def create_app(
    cache=None, search_fn=engine.search, saved_path: Path | str | None = None,
    stored_fn=engine.search_stored, sweep_fn=engine.sweep, limits: Limits | None = None,
) -> FastAPI:
    limits = limits or Limits.from_env()
    app = FastAPI(title="fly-points")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    budget = None
    if limits.daily_searches is not None:
        budget = DailyBudget(cache or Cache(), limits.daily_searches)
    runner = JobRunner(search_fn, cache, stored_fn, sweep_fn, budget, limits.auth_message)
    saved = SavedSearches(Path(saved_path) if saved_path else paths.STATE_DIR / "saved_searches.json")
    app.state.runner, app.state.saved = runner, saved

    def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def cap_errors(params: dict, kind: str = "search") -> list[str]:
        cap = limits.max_sweep_searches if kind == "sweep" else limits.max_searches
        if cap is not None and params["max_searches"] > cap:
            return [f"Max searches: this server allows at most {cap} per {kind}."]
        return []

    def results_context(job: Job, q: Mapping[str, str]) -> dict:
        idx_of = {id(o): i for i, o in enumerate(job.result.options)}
        options = engine.filter_options(job.result.options, option_filters(q))
        rows = select_rows([make_row(o, idx_of[id(o)]) for o in options], q)
        view = "compare" if q.get("view") == "compare" else "list"
        ctx = {
            "job": job, "rows": rows, "total_rows": len(job.rows), "q": dict(q), "view": view,
            "columns": COLUMNS, "numeric": NUMERIC, "result_programs": sorted({r["program"] for r in job.rows}),
            "days": DAYS, "sel_days": _multi(q, "days"),
        }
        if view == "compare":
            kept = {r["idx"] for r in rows}  # the program / hide-estimates filters apply to the grid too
            ctx["grid"] = make_grid([o for o in options if idx_of[id(o)] in kept], idx_of)
            ctx["cabins"] = [c.value for c in Cabin]
        return ctx

    def job_view(request: Request, job: Job):
        status = job.status  # read once: the worker thread may finish mid-render
        ctx = results_context(job, {}) if status == "done" and job.kind == "search" else {"job": job}
        return render(request, "_sweep.html" if job.kind == "sweep" else "_job.html", status_snapshot=status, **ctx)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return render(
            request, "index.html",
            regions={k: ", ".join(v) for k, v in config.load_regions().items()},
            programs=list(config.load_programs().values()), saved=saved.list(), days=DAYS, cabins=list(Cabin),
            banks=list(Bank), stored_age=engine.STORED_MAX_AGE_H, limits=limits,
            not_live=[p.code for p in config.load_programs().values() if not p.live],
        )

    @app.post("/jobs", response_class=HTMLResponse)
    async def start_job(request: Request):
        params, errors = parse_params(form_to_raw(await request.form()))
        errors = errors or cap_errors(params)
        if errors:
            return render(request, "_errors.html", status=422, errors=errors)
        return job_view(request, runner.submit(params))

    @app.post("/jobs/stored", response_class=HTMLResponse)
    async def start_stored_job(request: Request):
        params, errors = parse_params(form_to_raw(await request.form()))
        if errors:
            return render(request, "_errors.html", status=422, errors=errors)
        return job_view(request, runner.submit_stored(params))

    @app.post("/sweeps", response_class=HTMLResponse)
    async def start_sweep(request: Request):
        params, errors = parse_sweep_params(form_to_raw(await request.form()))
        errors = errors or cap_errors(params, "sweep")
        if errors:
            return render(request, "_errors.html", status=422, errors=errors)
        return job_view(request, runner.submit(params, "sweep"))

    @app.get("/sweeps/{job_id}/progress", response_class=HTMLResponse)
    def sweep_progress(request: Request, job_id: str):
        job = runner.get(job_id)
        if job is None or job.kind != "sweep":
            return render(request, "_errors.html", status=404, errors=["Unknown sweep (the server may have restarted)."])
        return job_view(request, job)

    @app.post("/sweeps/{job_id}/stop", response_class=HTMLResponse)
    def stop_sweep(request: Request, job_id: str):
        job = runner.get(job_id)
        if job is None or job.kind != "sweep":
            return render(request, "_errors.html", status=404, errors=["Unknown sweep (the server may have restarted)."])
        job.stop_event.set()
        return job_view(request, job)

    @app.get("/jobs/{job_id}/book/{index}", response_class=HTMLResponse)
    def job_booking(request: Request, job_id: str, index: int):
        job = runner.get(job_id)
        if job is None or job.status != "done" or job.result is None or not 0 <= index < len(job.result.options):
            return render(request, "_errors.html", status=404, errors=["No such result to book."])
        option = job.result.options[index]
        plan = booking.build_booking_plan(option)
        steps = [{"kind": s.kind, "text": s.text, "url": safe_url(s.url)} for s in plan.steps]
        return render(request, "_booking.html", plan=plan, steps=steps, row=make_row(option, index),
                      flights=segment_lines(option))

    @app.get("/jobs/{job_id}/progress", response_class=HTMLResponse)
    def job_progress(request: Request, job_id: str):
        job = runner.get(job_id)
        if job is None:
            return render(request, "_errors.html", status=404, errors=["Unknown job (the server may have restarted)."])
        return job_view(request, job)

    @app.get("/jobs/{job_id}/results", response_class=HTMLResponse)
    def job_results(request: Request, job_id: str):
        job = runner.get(job_id)
        if job is None or job.status != "done" or job.result is None:
            return render(request, "_errors.html", status=404, errors=["No finished results for this job."])
        return render(request, "_results.html", **results_context(job, request.query_params))

    @app.post("/saved", response_class=HTMLResponse)
    async def save_search(request: Request):
        form = await request.form()
        params, errors = parse_params(form_to_raw(form))
        name = str(form.get("name", "")).strip()
        if not name:
            errors.append("Saved search name: enter a name.")
        if errors:
            resp = render(request, "_errors.html", status=422, errors=errors)
            resp.headers["HX-Retarget"], resp.headers["HX-Reswap"] = "#form-errors", "innerHTML"
            return resp
        saved.add(name, params)
        return render(request, "_saved.html", saved=saved.list())

    @app.post("/saved/{item_id}/run", response_class=HTMLResponse)
    def run_saved(request: Request, item_id: str):
        item = saved.get(item_id)
        if item is None:
            return render(request, "_errors.html", status=404, errors=["Saved search not found."])
        params, errors = parse_params(item["params"])
        errors = errors or cap_errors(params)
        if errors:
            return render(request, "_errors.html", status=422, errors=errors)
        return job_view(request, runner.submit(params))

    @app.delete("/saved/{item_id}", response_class=HTMLResponse)
    def delete_saved(request: Request, item_id: str):
        saved.delete(item_id)
        return render(request, "_saved.html", saved=saved.list())

    return app


def serve(host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    import uvicorn

    cache = Cache()
    log.info("cache: purged %d expired entries", cache.purge_expired())
    cache.close()

    if reload:
        # Restart on .py changes under src/flypoints; templates/CSS are re-read per request anyway.
        # Reload needs an import string, and restarts drop in-memory jobs.
        uvicorn.run("flypoints.web.app:create_app", factory=True, host=host, port=port, log_level="info",
                    reload=True, reload_dirs=[str(HERE.parent)])
    else:
        uvicorn.run(create_app(), host=host, port=port, log_level="info")
