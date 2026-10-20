"""Propose updates to data/bonuses.yaml from public transfer-bonus tracker pages.

Nothing is written unless ``apply`` is called. Only Chase and Capital One bonuses to airline
programs listed in programs.yaml are considered.

Sources (both server-rendered HTML, no JS needed):
- awardtravelfinder: one card per bonus under "Current Transfer Bonuses", with start and end dates.
- pointme: a table "Active credit card transfer bonuses" with an end date only (no start). It also
  keeps already-expired rows in that table, so expired rows are skipped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import httpx
import yaml
from bs4 import BeautifulSoup

from . import config
from .models import Bank
from .paths import DATA_DIR

BONUSES_PATH = DATA_DIR / "bonuses.yaml"

SOURCES = {
    "awardtravelfinder": "https://awardtravelfinder.com/transfer-bonuses",
    "pointme": "https://point.me/insights/current-transfer-bonuses",
}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)

BANK_NAMES = {"chase ultimate rewards": Bank.chase, "capital one miles": Bank.capital_one}

# normalized tracker name (lower-case, alphanumerics only) -> programs.yaml code
_PROGRAM_NAMES = {
    "Air Canada Aeroplan": "AC", "Aeroplan": "AC",
    "Avianca LifeMiles": "AV", "LifeMiles": "AV",
    "Air France KLM Flying Blue": "KL", "Air France-KLM Flying Blue": "KL", "Flying Blue": "KL",
    "British Airways Avios": "BA", "British Airways Club": "BA", "The British Airways Club": "BA",
    "British Airways Executive Club": "BA",
    "Iberia Avios": "IB", "Iberia Plus": "IB",
    "Aer Lingus AerClub": "EI", "Aer Lingus Avios": "EI",
    "Virgin Atlantic Flying Club": "VS", "Virgin Atlantic": "VS",
    "Singapore KrisFlyer": "SQ", "Singapore Airlines KrisFlyer": "SQ", "KrisFlyer": "SQ",
    "Southwest Rapid Rewards": "WN",
    "United MileagePlus": "UA", "MileagePlus": "UA",
    "Aeromexico Rewards": "AM", "AeroMexico ClubPremier": "AM", "Aeromexico Club Premier": "AM",
    "Cathay Asia Miles": "CX", "Cathay Pacific Asia Miles": "CX", "Asia Miles": "CX",
    "Etihad Guest": "EY",
    "Finnair Plus": "AY",
    "Qantas Frequent Flyer": "QF", "Qantas": "QF",
    "Qatar Airways Privilege Club": "QR", "Qatar Privilege Club Avios": "QR", "Qatar Privilege Club": "QR",
    "TAP Miles&Go": "TP", "TAP Air Portugal Miles&Go": "TP",
    "Turkish Miles&Smiles": "TK", "Turkish Airlines Miles&Smiles": "TK",
    "Emirates Skywards": "EK",
    "EVA Air Infinity MileageLands": "BR", "EVA Air": "BR",
    "JAL Mileage Bank": "JL", "JAL (Japan Airlines) Mileage Bank": "JL", "Japan Airlines Mileage Bank": "JL",
}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


PROGRAM_CODES = {_norm(k): v for k, v in _PROGRAM_NAMES.items()}


def program_code(name: str) -> str | None:
    return PROGRAM_CODES.get(_norm(name))


def parse_percent(text: str) -> float | None:
    """"+30%" / "30%" -> 0.3. "Up to 100%" and anything without a plain percentage -> None."""
    if re.search(r"up\s*to", text, re.I):
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    return round(float(m.group(1)) / 100, 4) if m else None


def _date(text: str) -> date:
    text = text.replace(",", " ")
    for fmt in ("%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(" ".join(text.split()), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"bad date {text!r}")


@dataclass
class Row:
    """One bonus as listed on a tracker page, before mapping to our models."""

    source: str
    bank: str
    destination: str
    percent: str
    start: date | None
    end: date | None


_VALID = re.compile(
    r"Valid:\s*([A-Za-z]{3,9} \d{1,2})(?:,? (\d{4}))?\s*-\s*([A-Za-z]{3,9} \d{1,2}),? (\d{4})"
)


def parse_awardtravelfinder(html: str, url: str = SOURCES["awardtravelfinder"]) -> list[Row] | None:
    """Cards with an h4 "Bank → Program", "+N%" and "Valid: ..."
    (active) or "Expired: ..." (skipped). None if no cards were found (layout changed)."""
    soup = BeautifulSoup(html, "html.parser")
    rows, seen = [], 0
    for h4 in soup.find_all("h4"):
        if "→" not in h4.get_text():
            continue
        card = h4.parent  # smallest ancestor that also holds the percentage and validity lines
        while card.parent is not None and not re.search(r"Valid:|Expired:", card.get_text()):
            card = card.parent
        seen += 1
        text = " ".join(card.get_text(" ", strip=True).split())
        m = _VALID.search(text)
        if not m:  # expired card
            continue
        bank, _, dest = (s.strip() for s in h4.get_text(" ", strip=True).partition("→"))
        end = _date(f"{m.group(3)} {m.group(4)}")
        start = _date(f"{m.group(1)} {m.group(2) or end.year}")
        if start > end:  # "Dec 15 - Jan 10, 2027"
            start = start.replace(year=start.year - 1)
        pct = re.search(r"\+\s*\d+(?:\.\d+)?%", text)
        rows.append(Row(url, bank, dest, pct.group(0) if pct else "", start, end))
    return rows if seen else None


def parse_pointme(html: str, url: str = SOURCES["pointme"]) -> list[Row] | None:
    """Table with header Source Program | Destination Partner | Bonus | Effective Ratio | Expires.
    None if that table is missing."""
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        head = [c.get_text(" ", strip=True).lower() for c in trs[0].find_all(["th", "td"])] if trs else []
        if head[:3] != ["source program", "destination partner", "bonus"] or "expires" not in head:
            continue
        i_end = head.index("expires")
        rows = []
        for tr in trs[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if len(cells) <= i_end:
                continue
            try:
                end = _date(cells[i_end])
            except ValueError:
                end = None
            rows.append(Row(url, cells[0], cells[1], cells[2], None, end))
        return rows
    return None


PARSERS = {"awardtravelfinder": parse_awardtravelfinder, "pointme": parse_pointme}


def _to_bonus(row: Row, programs: dict, today: date, warnings: list[str]) -> config.Bonus | None:
    bank = BANK_NAMES.get(row.bank.lower())
    if bank is None:
        return None  # other issuers are not our business
    if row.end is not None and row.end < today:
        return None  # tracker still lists it, but it is over
    label = f"{bank.label} -> {row.destination}"
    code = program_code(row.destination)
    if code is None or code not in programs:
        warnings.append(f"skipped {label}: unknown or non-airline program ({row.source})")
        return None
    if bank not in programs[code].banks:
        warnings.append(f"skipped {label}: {bank.label} does not transfer to {code} in programs.yaml")
        return None
    pct = parse_percent(row.percent)
    if pct is None:
        warnings.append(f"skipped {label}: could not read bonus {row.percent!r} ({row.source})")
        return None
    if row.end is None:
        warnings.append(f"skipped {label} {row.percent}: no end date ({row.source})")
        return None
    return config.Bonus(
        bank=bank, program=code, bonus=pct, start=min(row.start or today, row.end), end=row.end,
        source=row.source, verified=today,
    )


def _merge(bonuses: list[config.Bonus], warnings: list[str]) -> list[config.Bonus]:
    """One entry per (bank, program). Sources that disagree on percentage or end date are
    dropped entirely with a warning (conservative); agreeing ones keep the row with a real start."""
    groups: dict[tuple[Bank, str], list[config.Bonus]] = {}
    for b in bonuses:
        groups.setdefault((b.bank, b.program), []).append(b)
    out = []
    for (bank, code), items in groups.items():
        if len({(b.bonus, b.end) for b in items}) > 1:
            found = "; ".join(f"{b.bonus:.0%} until {b.end} ({b.source})" for b in items)
            warnings.append(f"conflict for {bank.label} -> {code}, not proposed: {found}")
            continue
        # a real start date sorts earlier than the synthesized "today" start
        out.append(min(items, key=lambda b: b.start))
    return out


def collect(
    sources: list[str] | None = None,
    client: httpx.Client | None = None,
    today: date | None = None,
) -> tuple[list[config.Bonus], list[str], int]:
    """Like ``fetch_proposals`` plus the number of sources that fetched and parsed successfully."""
    today = today or date.today()
    names = sources or list(SOURCES)
    own = client is None
    client = client or httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True
    )
    programs = config.load_programs()
    warnings: list[str] = []
    found: list[config.Bonus] = []
    parsed = 0
    try:
        for name in names:
            url = SOURCES[name]
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                warnings.append(f"{name}: fetch failed ({e})")
                continue
            rows = PARSERS[name](resp.text, url)
            if rows is None:
                warnings.append(f"{name}: no bonus entries found, page layout may have changed")
                continue
            parsed += 1
            found += [b for r in rows if (b := _to_bonus(r, programs, today, warnings))]
    finally:
        if own:
            client.close()
    return _merge(found, warnings), warnings, parsed


def fetch_proposals(
    sources: list[str] | None = None,
    client: httpx.Client | None = None,
    today: date | None = None,
) -> tuple[list[config.Bonus], list[str]]:
    """(proposed bonuses, warnings). ``sources`` are keys of SOURCES; default is all of them."""
    proposed, warnings, _ = collect(sources, client, today)
    return proposed, warnings


@dataclass
class Diff:
    added: list[config.Bonus] = field(default_factory=list)
    removed: list[config.Bonus] = field(default_factory=list)
    changed: list[tuple[config.Bonus, config.Bonus]] = field(default_factory=list)  # (old, new)

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed)


def diff(
    current: list[config.Bonus],
    proposed: list[config.Bonus],
    today: date,
    sources_parsed: bool = True,
) -> Diff:
    """Compare every row in the file with the proposals, matched by (bank, program).

    Rows already expired are proposed for removal. Live rows that no source lists any more are
    proposed for removal only when ``sources_parsed`` (at least one source parsed fine).
    Rows that have not started yet are left alone. A changed row keeps its original start date.
    """
    out = Diff()
    prop = {(b.bank, b.program): b for b in proposed}
    live = {}
    for b in current:
        if b.end < today:
            out.removed.append(b)
        else:
            live[(b.bank, b.program)] = b
    for key, new in prop.items():
        old = live.get(key)
        if old is None:
            out.added.append(new)
        elif (old.bonus, old.end) != (new.bonus, new.end):
            out.changed.append((old, new.model_copy(update={"start": min(old.start, new.start)})))
    if sources_parsed:
        out.removed += [b for key, b in live.items() if key not in prop and b.start <= today]
    return out


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def read_all(path: Path | None = None) -> list[config.Bonus]:
    """Every row in the bonuses file, expired or not (``config.load_bonuses`` filters to active)."""
    path = path or BONUSES_PATH
    rows = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [config.Bonus(**r) for r in rows]


def apply(d: Diff, path: Path | None = None) -> None:
    """Write the diff into ``path``, keeping its leading comment header; rows sorted by bank, program."""
    path = path or BONUSES_PATH
    text = path.read_text(encoding="utf-8")
    header = []
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            break
        header.append(line)
    gone = {_key(b) for b in d.removed} | {_key(old) for old, _ in d.changed}
    rows = [b for b in read_all(path) if _key(b) not in gone] + d.added + [new for _, new in d.changed]
    rows.sort(key=lambda b: (b.bank.value, b.program, b.end))
    body = [
        {k: v for k, v in {**b.model_dump(), "bank": b.bank.value}.items() if v is not None} for b in rows
    ]
    dumped = yaml.dump(body, Dumper=_NoAliasDumper, sort_keys=False) if body else "[]\n"
    path.write_text("\n".join(header) + "\n" + dumped, encoding="utf-8")


def _key(b: config.Bonus) -> tuple:
    return (b.bank, b.program, b.start, b.end, b.bonus)
