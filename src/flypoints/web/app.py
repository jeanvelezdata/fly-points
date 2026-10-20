"""FastAPI + Jinja2 + HTMX dashboard: a thin layer over ``flypoints.engine.search``.

Jobs run one at a time on a single worker thread (protects the PointsYeah account); the HTTP
handlers only read job state, so the server stays responsive during a long search.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config, engine, paths
from ..cli import _other_bank_points, _route
from ..models import Cabin, RankedOption, SearchQuery
from ..providers.base import AuthError

HERE = Path(__file__).resolve().parent
MAX_JOBS_KEPT = 50
TRUE = {"on", "true", "1", "yes"}

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
        cabin = Cabin(str(raw.get("cabin", "J")).upper())
    except ValueError:
        errors.append("Cabin: choose Y, W, J or F.")
        cabin = Cabin.J
    clean["cabin"] = cabin.value

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
    for flag in ("include_cash", "include_estimates", "refresh"):
        clean[flag] = _flag(raw.get(flag, False))
    return (None if errors else clean), errors


def form_to_raw(form) -> dict:
    keys = ("origins", "destinations", "date_from", "date_to", "cabin", "max_searches")
    raw: dict[str, Any] = {k: form.get(k, "") for k in keys}
    raw["programs"] = form.getlist("programs")
    for flag in ("include_cash", "include_estimates", "refresh"):
        raw[flag] = flag in form
    return raw


def build_query(params: dict) -> SearchQuery:
    return SearchQuery(
        origins=_tokens(params["origins"]),
        destinations=_tokens(params["destinations"]),
        date_from=date.fromisoformat(params["date_from"]),
        date_to=date.fromisoformat(params["date_to"]),
        cabin=Cabin(params["cabin"]),
        max_searches=params["max_searches"],
        programs=params["programs"] or None,
    )


# --- result rows: sorting and filtering -----------------------------------------------------

SORT_COLUMNS = ("date", "route", "stops", "program", "cabin", "miles", "taxes", "points", "bank", "cash", "cpp", "seats")
COLUMNS = [
    ("date", "Date"), ("route", "Route"), ("stops", "Stops"), ("program", "Program"), ("cabin", "Cabin"), ("miles", "Miles"),
    ("taxes", "Taxes"), ("points", "Best bank pts"), ("bank", "Best bank"), (None, "Other bank"),
    ("cash", "Cash"), ("cpp", "cpp"), ("seats", "Seats"), (None, "Flags"), (None, "Note"), (None, "Book"),
]
NUMERIC = {"stops", "miles", "taxes", "points", "cash", "cpp", "seats"}


def make_row(o: RankedOption) -> dict:
    a = o.offer
    url = a.booking_url if a.booking_url and a.booking_url.startswith(("http://", "https://")) else None
    return {
        "date": a.date.isoformat(), "route": _route(o),
        "stops": len(a.segments) - 1 if a.segments else None,  # None: no routing (chart estimates)
        "program": a.program_code, "cabin": a.cabin.value,
        "miles": a.miles, "taxes": a.taxes_usd, "points": o.best_bank_points,
        "bank": o.best_bank.label if o.best_bank else None, "other": _other_bank_points(o),
        "cash": o.cash_fare.price_usd if o.cash_fare else None, "cpp": o.cpp, "seats": a.seats,
        "estimated": a.estimated, "note": o.note or "", "url": url,
    }


def _num(text: Any) -> float | None:
    try:
        return float(str(text).replace(",", ""))
    except ValueError:
        return None


def select_rows(rows: list[dict], q: Mapping[str, str]) -> list[dict]:
    """Apply the filter form values, then the sort, from query params."""
    max_points, max_taxes, min_seats, max_stops = (
        _num(q.get(k, "")) for k in ("max_points", "max_taxes", "min_seats", "max_stops")
    )
    out = [
        r for r in rows
        if (not q.get("program") or r["program"] == q["program"])
        and (max_points is None or (r["points"] is not None and r["points"] <= max_points))
        and (max_taxes is None or r["taxes"] <= max_taxes)
        and (not _flag(q.get("hide_est", "")) or not r["estimated"])
        and (min_seats is None or r["seats"] is None or r["seats"] >= min_seats)
        and (max_stops is None or (r["stops"] is not None and r["stops"] <= max_stops))
    ]
    column = q.get("sort", "")
    if column in SORT_COLUMNS:
        known = [r for r in out if r[column] is not None]
        missing = [r for r in out if r[column] is None]
        out = sorted(known, key=lambda r: r[column], reverse=q.get("dir") == "desc") + missing
    return out


# --- jobs -----------------------------------------------------------------------------------


class Job:
    def __init__(self, params: dict):
        self.id = uuid.uuid4().hex[:10]
        self.params = params
        self.status = "queued"  # queued | running | done | error
        self.done = 0
        self.total = 0
        self.message = ""
        self.result: engine.SearchResult | None = None
        self.rows: list[dict] = []
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
        return f"{p['origins']} to {p['destinations']}, {p['date_from']}..{p['date_to']}, {p['cabin']}"


class JobRunner:
    """FIFO queue drained by one daemon thread, so only one live search runs at a time."""

    def __init__(self, search_fn, cache):
        self._search_fn, self._cache = search_fn, cache
        self._jobs: dict[str, Job] = {}
        self._queue: queue.Queue[Job] = queue.Queue()
        self._lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True, name="flypoints-jobs").start()

    def submit(self, params: dict) -> Job:
        job = Job(params)
        with self._lock:
            self._jobs[job.id] = job
            for old in [j.id for j in self._jobs.values() if not j.active][:-MAX_JOBS_KEPT]:
                del self._jobs[old]
        self._queue.put(job)
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
        try:
            result = self._search_fn(
                build_query(p), cache=self._cache, progress=on_progress, refresh=p["refresh"],
                include_estimates=p["include_estimates"], include_cash=p["include_cash"],
            )
            job.result, job.rows = result, [make_row(o) for o in result.options]
            job.status = "done"
        except AuthError as e:
            job.error, job.status = str(e), "error"
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


def create_app(cache=None, search_fn=engine.search, saved_path: Path | str | None = None) -> FastAPI:
    app = FastAPI(title="fly-points")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    runner = JobRunner(search_fn, cache)
    saved = SavedSearches(Path(saved_path) if saved_path else paths.STATE_DIR / "saved_searches.json")
    app.state.runner, app.state.saved = runner, saved

    def render(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def results_context(job: Job, q: Mapping[str, str]) -> dict:
        return {
            "job": job, "rows": select_rows(job.rows, q), "total_rows": len(job.rows), "q": dict(q),
            "columns": COLUMNS, "numeric": NUMERIC, "result_programs": sorted({r["program"] for r in job.rows}),
        }

    def job_view(request: Request, job: Job):
        status = job.status  # read once: the worker thread may finish mid-render
        ctx = results_context(job, {}) if status == "done" else {"job": job}
        return render(request, "_job.html", status_snapshot=status, **ctx)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return render(
            request, "index.html",
            regions={k: ", ".join(v) for k, v in config.load_regions().items()},
            programs=list(config.load_programs().values()), saved=saved.list(),
        )

    @app.post("/jobs", response_class=HTMLResponse)
    async def start_job(request: Request):
        params, errors = parse_params(form_to_raw(await request.form()))
        if errors:
            return render(request, "_errors.html", status=422, errors=errors)
        return job_view(request, runner.submit(params))

    @app.get("/jobs/{job_id}/progress", response_class=HTMLResponse)
    def job_progress(request: Request, job_id: str):
        job = runner.get(job_id)
        if job is None:
            return render(request, "_errors.html", status=404, errors=["Unknown job (the server may have restarted)."])
        return job_view(request, job)

    @app.get("/jobs/{job_id}/results", response_class=HTMLResponse)
    def job_results(request: Request, job_id: str):
        job = runner.get(job_id)
        if job is None or job.status != "done":
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

    if reload:
        # Restart on .py changes under src/flypoints; templates/CSS are re-read per request anyway.
        # Reload needs an import string, and restarts drop in-memory jobs.
        uvicorn.run("flypoints.web.app:create_app", factory=True, host=host, port=port, log_level="info",
                    reload=True, reload_dirs=[str(HERE.parent)])
    else:
        uvicorn.run(create_app(), host=host, port=port, log_level="info")
