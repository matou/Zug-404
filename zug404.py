"""Collect DB Timetables observations and publish local connection reports.

Only the process running ``check`` or ``collect`` reads DB credentials.
"""

from __future__ import annotations

import argparse
import fcntl
import html
import json
import logging
import re
import sqlite3
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from contextlib import ExitStack, closing
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Europe/Berlin")
BASE_URL = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TRAIN = re.compile(r"^([A-Za-z]+)\s*(\d+)$")
STAMP = re.compile(r"^\d{10}$")
LOG = logging.getLogger("zug404")


@dataclass(frozen=True)
class Leg:
    train: str
    origin: str
    destination: str
    departure: str


@dataclass(frozen=True)
class Connection:
    id: str
    name: str
    weekdays: tuple[int, ...]
    exclude_holidays: str
    legs: tuple[Leg, ...]
    margins: tuple[int, ...]


@dataclass(frozen=True)
class Stop:
    id: str
    category: str
    number: str
    arrival: str | None
    departure: str | None


class ConfigError(ValueError):
    pass


class SourceError(RuntimeError):
    pass


def load_credentials(path: Path | None = None) -> tuple[str, str]:
    path = path or Path(__file__).resolve().with_name("credentials.toml")
    try:
        credentials = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SourceError(f"{path}: create this file from credentials.example.toml") from exc
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise SourceError(f"{path}: could not read valid TOML credentials") from exc
    client_id = credentials.get("DB_CLIENT_ID")
    api_key = credentials.get("DB_API_KEY")
    if not all(isinstance(value, str) and value.strip() for value in (client_id, api_key)):
        raise SourceError(f"{path}: DB_CLIENT_ID and DB_API_KEY must be nonempty strings")
    return client_id, api_key


def parse_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    if not STAMP.fullmatch(value):
        raise ValueError(f"Invalid DB timestamp: {value!r}")
    return datetime.strptime(value, "%y%m%d%H%M").replace(tzinfo=TZ)


def stamp(value: datetime) -> str:
    return value.astimezone(TZ).strftime("%y%m%d%H%M")


def local_datetime(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, clock_time.fromisoformat(hhmm), TZ)


def easter(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def nationwide_holiday(day: date) -> bool:
    fixed = {(1, 1), (5, 1), (10, 3), (12, 25), (12, 26)}
    return (day.month, day.day) in fixed or day in {
        easter(day.year) + timedelta(days=offset) for offset in (-2, 1, 39, 50)
    }


def eligible(config: Connection, day: date) -> bool:
    return day.weekday() in config.weekdays and not (
        config.exclude_holidays == "de_nationwide" and nationwide_holiday(day)
    )


def load_connection(path: Path) -> Connection:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        identity = raw["id"]
        if not isinstance(identity, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", identity):
            raise ConfigError("id must contain lowercase letters, digits, or hyphens")
        name = raw["name"]
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("name must be nonempty")
        days = raw["weekdays"]
        if not isinstance(days, list) or not days or len(set(days)) != len(days):
            raise ConfigError("weekdays must be a nonempty list without duplicates")
        weekdays = tuple(WEEKDAYS.index(day) for day in days)
        holidays = raw.get("exclude_holidays", "none")
        if holidays not in ("none", "de_nationwide"):
            raise ConfigError("exclude_holidays must be none or de_nationwide")
        legs = []
        for item in raw["legs"]:
            match = TRAIN.fullmatch(item["train"].strip())
            if not match:
                raise ConfigError(f"invalid train {item['train']!r}")
            departure = item["scheduled_departure"]
            clock_time.fromisoformat(departure)
            if not re.fullmatch(r"\d\d:\d\d", departure):
                raise ConfigError("scheduled_departure must be HH:MM")
            origin, destination = item["from"].strip(), item["to"].strip()
            if not origin or not destination or origin == destination:
                raise ConfigError("each leg needs distinct named stations")
            legs.append(Leg(f"{match[1].upper()} {match[2]}", origin, destination, departure))
        if len(legs) < 2:
            raise ConfigError("at least two legs are required")
        transfers = [leg.destination for leg in legs[:-1]]
        for previous, following in zip(legs, legs[1:]):
            if previous.destination != following.origin:
                raise ConfigError("adjacent legs must meet at the same station")
        margins = {station: 5 for station in transfers}
        for item in raw.get("transfers", []):
            station, minutes = item["station"], item["min_minutes"]
            if station not in margins or not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 0:
                raise ConfigError("transfer override has unknown station or invalid minutes")
            margins[station] = minutes
        return Connection(identity, name, weekdays, holidays, tuple(legs), tuple(margins[s] for s in transfers))
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, SyntaxError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def load_configs(folder: Path) -> tuple[list[Connection], list[str]]:
    configs, errors = [], []
    seen = set()
    for path in sorted(folder.glob("*.toml")):
        try:
            config = load_connection(path)
            if config.id in seen:
                raise ConfigError(f"{path}: duplicate id {config.id}")
            seen.add(config.id)
            configs.append(config)
        except ConfigError as exc:
            errors.append(str(exc))
    return configs, errors


def parse_stations(xml: bytes, name: str) -> str:
    root = ET.fromstring(xml)
    matches = [s.get("eva") for s in root.iter("station") if s.get("name") == name]
    if len(matches) != 1 or not matches[0]:
        raise SourceError(f"station {name!r} did not resolve uniquely")
    return matches[0]


def parse_stops(xml: bytes) -> list[Stop]:
    root = ET.fromstring(xml)
    result = []
    for s in root.iter("s"):
        label = s.find("tl")
        if label is None or not s.get("id"):
            continue
        ar, dp = s.find("ar"), s.find("dp")
        result.append(Stop(s.attrib["id"], (label.get("c") or "").upper(),
                           label.get("n") or "", ar.get("pt") if ar is not None else None,
                           dp.get("pt") if dp is not None else None))
    return result


def parse_changes(xml: bytes) -> dict[str, dict[str, dict[str, str]]]:
    root = ET.fromstring(xml)
    result = {}
    for s in root.iter("s"):
        if not s.get("id"):
            continue
        result[s.attrib["id"]] = {
            kind: dict(event.attrib) if event is not None else {}
            for kind in ("ar", "dp")
            if (event := s.find(kind)) is not None
        }
    return result


class Timetables:
    def __init__(self, client_id: str, api_key: str):
        if not client_id or not api_key:
            raise SourceError("DB client ID and API key are required")
        self.headers = {"DB-Client-ID": client_id, "DB-Api-Key": api_key}
        self.last_call = 0.0

    def get(self, *parts: str) -> bytes:
        # One request per second leaves room below the free plan's 60/minute limit.
        delay = 1.05 - (time.monotonic() - self.last_call)
        if delay > 0:
            time.sleep(delay)
        path = "/".join(urllib.parse.quote(part, safe="") for part in parts)
        request = urllib.request.Request(f"{BASE_URL}/{path}", headers=self.headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise SourceError(f"DB Timetables {path}: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise SourceError(f"DB Timetables {path}: {exc.reason}") from exc
        finally:
            self.last_call = time.monotonic()


SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
  connection_id TEXT NOT NULL, service_date TEXT NOT NULL,
  config_json TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT 'unknown',
  reason TEXT NOT NULL DEFAULT 'awaiting observations',
  arrival_delay_minutes INTEGER, updated_at TEXT NOT NULL,
  PRIMARY KEY (connection_id, service_date)
);
CREATE TABLE IF NOT EXISTS planned (
  connection_id TEXT NOT NULL, service_date TEXT NOT NULL, leg_index INTEGER NOT NULL,
  origin_eva TEXT NOT NULL, destination_eva TEXT NOT NULL,
  origin_id TEXT NOT NULL, destination_id TEXT NOT NULL,
  departure TEXT NOT NULL, arrival TEXT NOT NULL,
  PRIMARY KEY (connection_id, service_date, leg_index)
);
CREATE TABLE IF NOT EXISTS observations (
  connection_id TEXT NOT NULL, service_date TEXT NOT NULL, leg_index INTEGER NOT NULL,
  station_kind TEXT NOT NULL, observed_at TEXT NOT NULL,
  changed_time TEXT, status TEXT, raw_json TEXT NOT NULL,
  PRIMARY KEY (connection_id, service_date, leg_index, station_kind, observed_at)
);
CREATE TABLE IF NOT EXISTS stations (name TEXT PRIMARY KEY, eva TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan_slices (
  eva TEXT NOT NULL, service_date TEXT NOT NULL, hour INTEGER NOT NULL,
  response BLOB NOT NULL, PRIMARY KEY (eva, service_date, hour)
);
CREATE TABLE IF NOT EXISTS activation (
  connection_id TEXT PRIMARY KEY, service_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_attempts (
  connection_id TEXT NOT NULL, service_date TEXT NOT NULL,
  attempted_at TEXT NOT NULL, PRIMARY KEY (connection_id, service_date)
);
"""


def database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def trip_key(stop_id: str) -> str:
    # The final component is the stop index; preceding components identify the dated trip.
    parts = stop_id.rsplit("-", 1)
    return parts[0] if len(parts) == 2 else stop_id


def matching_pair(origins: list[Stop], destinations: list[Stop], leg: Leg,
                  day: date) -> tuple[Stop, Stop]:
    category, number = leg.train.split()
    nominal = local_datetime(day, leg.departure)
    candidates = []
    for origin in origins:
        if origin.category != category or origin.number != number or not origin.departure:
            continue
        departure = parse_stamp(origin.departure)
        if departure is None or abs((departure - nominal).total_seconds()) > 60 * 60:
            continue
        for destination in destinations:
            if (destination.category, destination.number) != (category, number):
                continue
            if trip_key(origin.id) != trip_key(destination.id) or not destination.arrival:
                continue
            arrival = parse_stamp(destination.arrival)
            if arrival and departure < arrival <= departure + timedelta(hours=24):
                candidates.append((origin, destination))
    if len(candidates) != 1:
        raise SourceError(f"{leg.train} {leg.origin} → {leg.destination}: "
                          f"expected one planned trip, found {len(candidates)}")
    return candidates[0]


def station_eva(db: sqlite3.Connection, api: Timetables, name: str) -> str:
    row = db.execute("SELECT eva FROM stations WHERE name=?", (name,)).fetchone()
    if row:
        return row["eva"]
    eva = parse_stations(api.get("station", name), name)
    db.execute("INSERT INTO stations VALUES (?, ?)", (name, eva))
    db.commit()
    return eva


def plans_at(db: sqlite3.Connection, api: Timetables, eva: str,
             hours: set[tuple[date, int]]) -> list[Stop]:
    # A plan slice may contain overlapping stops; the stop ID removes duplicates.
    found = {}
    for part_day, hour in sorted(hours):
        row = db.execute("SELECT response FROM plan_slices WHERE eva=? AND service_date=? AND hour=?",
                         (eva, part_day.isoformat(), hour)).fetchone()
        if row:
            xml = row["response"]
        else:
            try:
                xml = api.get("plan", eva, part_day.strftime("%y%m%d"), f"{hour:02d}")
            except SourceError as exc:
                if "HTTP 404" in str(exc):
                    continue
                raise
        stops = parse_stops(xml)
        if not row and stops:
            db.execute("INSERT OR REPLACE INTO plan_slices VALUES (?, ?, ?, ?)",
                       (eva, part_day.isoformat(), hour, xml))
            db.commit()
        for stop in stops:
            found[stop.id] = stop
    return list(found.values())


def resolve_plans(db: sqlite3.Connection, api: Timetables, config: Connection,
                  day: date) -> list[sqlite3.Row]:
    existing = db.execute("SELECT * FROM planned WHERE connection_id=? AND service_date=? ORDER BY leg_index",
                          (config.id, day.isoformat())).fetchall()
    if len(existing) == len(config.legs):
        return existing
    for index, leg in enumerate(config.legs):
        origin_eva = station_eva(db, api, leg.origin)
        destination_eva = station_eva(db, api, leg.destination)
        nominal = local_datetime(day, leg.departure)
        origin_hours = {(point.date(), point.hour) for point in
                        (nominal - timedelta(hours=1), nominal, nominal + timedelta(hours=1))}
        # Search one day forward from the nominal departure, including overnight trains.
        destination_hours = {(point.date(), point.hour) for point in
                             (nominal + timedelta(hours=offset) for offset in range(25))}
        origins = plans_at(db, api, origin_eva, origin_hours)
        destinations = plans_at(db, api, destination_eva, destination_hours)
        start, end = matching_pair(origins, destinations, leg, day)
        db.execute("INSERT OR REPLACE INTO planned VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (config.id, day.isoformat(), index, origin_eva, destination_eva,
                    start.id, end.id, start.departure, end.arrival))
        db.commit()
    plans = db.execute("SELECT * FROM planned WHERE connection_id=? AND service_date=? ORDER BY leg_index",
                       (config.id, day.isoformat())).fetchall()
    for before, after in zip(plans, plans[1:]):
        if parse_stamp(before["arrival"]) >= parse_stamp(after["departure"]):
            raise SourceError(f"{config.id}: planned transfer is impossible; review configuration")
    return plans


def observe(db: sqlite3.Connection, api: Timetables, config: Connection,
            day: date, plans: list[sqlite3.Row], now: datetime) -> None:
    by_eva = {}
    for plan in plans:
        for eva in (plan["origin_eva"], plan["destination_eva"]):
            by_eva[eva] = None
    for eva in by_eva:
        by_eva[eva] = parse_changes(api.get("fchg", eva))
        observed_at = datetime.now(TZ).isoformat()
        for plan in plans:
            for station_kind, key, event in (
                ("origin", "origin_id", "dp"), ("destination", "destination_id", "ar")
            ):
                if plan[f"{station_kind}_eva"] != eva:
                    continue
                change = by_eva[eva].get(plan[key], {}).get(event, {})
                db.execute("INSERT OR REPLACE INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (config.id, day.isoformat(), plan["leg_index"], station_kind,
                            observed_at, change.get("ct"), change.get("cs"),
                            json.dumps(change, sort_keys=True)))
        db.commit()


def event_report(db: sqlite3.Connection, config_id: str, day: str, index: int,
                 kind: str, planned: str) -> tuple[datetime | None, str, bool]:
    rows = db.execute("""SELECT * FROM observations WHERE connection_id=? AND service_date=?
                         AND leg_index=? AND station_kind=? ORDER BY observed_at""",
                      (config_id, day, index, kind)).fetchall()
    planned_at = parse_stamp(planned)
    # A revoked cancellation has status p. Absence in a later full feed alone is not revocation.
    statuses = [row for row in rows if row["status"]]
    if statuses and statuses[-1]["status"] == "c":
        return None, "cancelled", True
    # A 'p' status revokes an earlier cancellation; disregard earlier estimates.
    if statuses and statuses[-1]["status"] == "p":
        rows = [row for row in rows if row["observed_at"] >= statuses[-1]["observed_at"]]
    changed = [row["changed_time"] for row in rows if row["changed_time"]]
    if changed:
        return parse_stamp(changed[-1]), "reported time", False
    # A full-feed check close to the event permits only a 'no reported change' inference.
    timely = any(-timedelta(minutes=2) <=
                 datetime.fromisoformat(row["observed_at"]) - planned_at <= timedelta(minutes=10)
                 for row in rows)
    if timely:
        return planned_at, "no reported change", False
    return None, "no timely observation", False


def evaluate(db: sqlite3.Connection, config: Connection, day: date,
             plans: list[sqlite3.Row], now: datetime) -> tuple[str, str, int | None]:
    events = []
    for index, plan in enumerate(plans):
        departure = event_report(db, config.id, day.isoformat(), index, "origin", plan["departure"])
        arrival = event_report(db, config.id, day.isoformat(), index, "destination", plan["arrival"])
        events.append((departure, arrival))
    if any(event[2] for leg in events for event in leg):
        return "failed", "reported cancellation", None
    for index, (earlier, later) in enumerate(zip(events, events[1:])):
        arrival, departure = earlier[1][0], later[0][0]
        if arrival and departure and departure - arrival < timedelta(minutes=config.margins[index]):
            return "failed", f"transfer at {config.legs[index].destination} below {config.margins[index]} minutes", None
    end = parse_stamp(plans[-1]["arrival"])
    if now < end + timedelta(minutes=10):
        return "unknown", "journey still being observed", None
    if any(event[0] is None for leg in events for event in leg):
        missing = [f"{config.legs[i].train} {kind}: {event[1]}"
                   for i, leg in enumerate(events)
                   for kind, event in zip(("departure", "arrival"), leg) if event[0] is None]
        return "unknown", "; ".join(missing), None
    delay = round((events[-1][1][0] - end).total_seconds() / 60)
    return "worked", "all reported transfers feasible", delay


def upsert_day(db: sqlite3.Connection, config: Connection, day: date,
               outcome: str, reason: str, delay: int | None, now: datetime) -> None:
    db.execute("""INSERT INTO days VALUES (?, ?, ?, ?, ?, ?, ?)
                  ON CONFLICT(connection_id, service_date) DO UPDATE SET
                  config_json=excluded.config_json, outcome=excluded.outcome,
                  reason=excluded.reason, arrival_delay_minutes=excluded.arrival_delay_minutes,
                  updated_at=excluded.updated_at""",
               (config.id, day.isoformat(), json.dumps(asdict(config), ensure_ascii=False),
                outcome, reason, delay, now.isoformat()))
    db.commit()


def collect(db: sqlite3.Connection, api: Timetables, config: Connection,
            day: date, now: datetime) -> None:
    if not eligible(config, day):
        return
    first = local_datetime(day, config.legs[0].departure)
    if now < first - timedelta(minutes=30) or now > first + timedelta(hours=18):
        return
    prior = db.execute("SELECT outcome FROM days WHERE connection_id=? AND service_date=?",
                       (config.id, day.isoformat())).fetchone()
    if prior is None:
        upsert_day(db, config, day, "unknown", "awaiting timetable", None, now)
    elif prior["outcome"] in ("worked", "failed"):
        last = db.execute("""SELECT arrival FROM planned WHERE connection_id=? AND service_date=?
                             ORDER BY leg_index DESC LIMIT 1""", (config.id, day.isoformat())).fetchone()
        if last and now >= parse_stamp(last["arrival"]) + timedelta(minutes=10):
            return
    try:
        planned_count = db.execute("SELECT count(*) FROM planned WHERE connection_id=? AND service_date=?",
                                   (config.id, day.isoformat())).fetchone()[0]
        if planned_count < len(config.legs):
            attempt = db.execute("SELECT attempted_at FROM plan_attempts WHERE connection_id=? AND service_date=?",
                                 (config.id, day.isoformat())).fetchone()
            if attempt and now - datetime.fromisoformat(attempt["attempted_at"]) < timedelta(minutes=10):
                return
            db.execute("INSERT OR REPLACE INTO plan_attempts VALUES (?, ?, ?)",
                       (config.id, day.isoformat(), now.isoformat()))
            db.commit()
        plans = resolve_plans(db, api, config, day)
        observe(db, api, config, day, plans, now)
        outcome, reason, delay = evaluate(db, config, day, plans, now)
        upsert_day(db, config, day, outcome, reason, delay, now)
    except (SourceError, ET.ParseError, ValueError) as exc:
        LOG.error("%s %s: %s", config.id, day, exc)
        if prior is None or prior["outcome"] == "unknown":
            upsert_day(db, config, day, "unknown", str(exc), None, now)


def reconcile_days(db: sqlite3.Connection, config: Connection, now: datetime) -> None:
    row = db.execute("SELECT service_date FROM activation WHERE connection_id=?", (config.id,)).fetchone()
    if row is None:
        db.execute("INSERT INTO activation VALUES (?, ?)", (config.id, now.date().isoformat()))
        db.commit()
        first = now.date()
    else:
        first = date.fromisoformat(row["service_date"])
    existing = {row[0] for row in db.execute("SELECT service_date FROM days WHERE connection_id=?",
                                             (config.id,))}
    day = first
    while day <= now.date():
        if eligible(config, day) and day.isoformat() not in existing:
            end = local_datetime(day, config.legs[0].departure) + timedelta(hours=18)
            reason = "collector missed the observation window" if now > end else "awaiting observations"
            upsert_day(db, config, day, "unknown", reason, None, now)
        day += timedelta(days=1)


def render(db: sqlite3.Connection, configs: list[Connection], errors: list[str], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    names = {config.id: config.name for config in configs}
    ids = sorted(set(names) | {row[0] for row in db.execute("SELECT DISTINCT connection_id FROM days")})
    cards = []
    for identity in ids:
        rows = db.execute("SELECT * FROM days WHERE connection_id=? ORDER BY service_date DESC", (identity,)).fetchall()
        name = names[identity] if identity in names else json.loads(rows[0]["config_json"])["name"]
        counts = {state: sum(row["outcome"] == state for row in rows) for state in ("worked", "failed", "unknown")}
        samples = counts["worked"] + counts["failed"]
        rate = f"{counts['worked'] / samples:.1%}" if samples else "—"
        cards.append(f'<article><h2><a href="{identity}.html">{html.escape(name)}</a></h2>'
                     f'<p>{len(rows)} eligible days · {counts["worked"]} worked · {counts["failed"]} failed · '
                     f'{counts["unknown"]} unknown · {samples} samples · {rate} success</p></article>')
        table = []
        for row in rows:
            plans = db.execute("SELECT * FROM planned WHERE connection_id=? AND service_date=? ORDER BY leg_index",
                               (identity, row["service_date"])).fetchall()
            config_data = json.loads(row["config_json"])
            details = []
            for plan in plans:
                i = plan["leg_index"]
                leg = config_data["legs"][i]
                dep = event_report(db, identity, row["service_date"], i, "origin", plan["departure"])
                arr = event_report(db, identity, row["service_date"], i, "destination", plan["arrival"])
                def label(event: tuple[datetime | None, str, bool], planned: str,
                          kind: str) -> str:
                    reported = event[0].strftime("%Y-%m-%d %H:%M %Z") if event[0] else "—"
                    scheduled = parse_stamp(planned).strftime("%Y-%m-%d %H:%M %Z")
                    observation = db.execute("""SELECT observed_at, status FROM observations
                                                WHERE connection_id=? AND service_date=?
                                                AND leg_index=? AND station_kind=?
                                                ORDER BY observed_at DESC LIMIT 1""",
                                             (identity, row["service_date"], i, kind)).fetchone()
                    seen = (f'; last checked {html.escape(observation["observed_at"])}; '
                            f'source status {html.escape(observation["status"] or "none")}'
                            if observation else "; never checked")
                    return (f"planned {scheduled}; reported {reported} "
                            f"({html.escape(event[1])}){seen}")
                details.append(f'<li>{html.escape(leg["train"])}: {html.escape(leg["origin"])} departure '
                               f'{label(dep, plan["departure"], "origin")}; '
                               f'{html.escape(leg["destination"])} arrival '
                               f'{label(arr, plan["arrival"], "destination")}</li>')
            for i, (earlier, later) in enumerate(zip(plans, plans[1:])):
                arrival = event_report(db, identity, row["service_date"], i, "destination", earlier["arrival"])[0]
                departure = event_report(db, identity, row["service_date"], i + 1, "origin", later["departure"])[0]
                minutes = round((departure - arrival).total_seconds() / 60) if arrival and departure else None
                details.append(f'<li>Transfer at {html.escape(config_data["legs"][i]["destination"])}: '
                               f'{minutes if minutes is not None else "unknown"} minutes; minimum '
                               f'{config_data["margins"][i]} minutes</li>')
            detail = "<ul>" + "".join(details) + "</ul>" if details else "Timetable not resolved."
            delay = f'{row["arrival_delay_minutes"]:+d} min' if row["arrival_delay_minutes"] is not None else "—"
            table.append(f'<tr><td>{row["service_date"]}</td><td>{row["outcome"]}</td><td>{delay}</td>'
                         f'<td><details><summary>{html.escape(row["reason"])}</summary>{detail}</details></td></tr>')
        body = (f'<p><a href="index.html">All connections</a></p><h1>{html.escape(name)}</h1>'
                f'<p>{len(rows)} eligible days · {samples} samples · {rate} success. '
                'Outcomes and final arrival delay use reported times, which may be estimates.</p>'
                '<table><thead><tr><th>Date</th><th>Outcome</th><th>Reported arrival delay</th>'
                '<th>Reason and train details</th></tr></thead><tbody>' + "".join(table) + '</tbody></table>')
        (output / f"{identity}.html").write_text(page(name, body), encoding="utf-8")
    issues = "".join(f"<li>{html.escape(error)}</li>" for error in errors)
    issue_block = f"<h2>Configuration issues</h2><ul>{issues}</ul>" if errors else ""
    (output / "index.html").write_text(page("Zug-404", "<h1>Zug-404</h1>" +
                                           ("".join(cards) or "<p>No observations yet.</p>") + issue_block),
                                        encoding="utf-8")


def page(title: str, body: str) -> str:
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width,initial-scale=1"><title>' + html.escape(title) +
            '</title><style>body{font:1rem/1.5 system-ui;max-width:75rem;margin:2rem auto;padding:0 1rem}'
            'a{color:#005a9c}article{border:1px solid #ccc;padding:0 1rem;margin:1rem 0}'
            'table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #ccc;'
            'padding:.65rem;text-align:left;vertical-align:top}details{max-width:60rem}'
            'summary{cursor:pointer}ul{padding-left:1.4rem}</style><body>' + body + '</body></html>')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "collect", "render"))
    parser.add_argument("--connections", type=Path, default=Path("connections"))
    parser.add_argument("--database", type=Path, default=Path("data/zug404.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("site"))
    parser.add_argument("--date", type=date.fromisoformat, help="service date for check, YYYY-MM-DD")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    configs, errors = load_configs(args.connections)
    for error in errors:
        LOG.error("%s", error)
    with ExitStack() as stack:
        if args.command in ("check", "collect"):
            lock_path = args.database.with_suffix(".lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock = stack.enter_context(lock_path.open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                LOG.info("another timetable run is active; skipping")
                return 0
        db = stack.enter_context(closing(database(args.database)))
        if args.command in ("check", "collect"):
            try:
                api = Timetables(*load_credentials())
            except SourceError as exc:
                LOG.error("%s", exc)
                return 2
        if args.command == "check":
            check_day = args.date or datetime.now(TZ).date()
            for config in configs:
                if not eligible(config, check_day):
                    LOG.info("%s is not eligible on %s", config.id, check_day)
                    continue
                try:
                    plans = resolve_plans(db, api, config, check_day)
                    for index, plan in enumerate(plans):
                        LOG.info("%s %s: %s departs %s and reaches %s at %s; stop IDs %s / %s",
                                 config.id, check_day, config.legs[index].train,
                                 parse_stamp(plan["departure"]).isoformat(),
                                 config.legs[index].destination,
                                 parse_stamp(plan["arrival"]).isoformat(),
                                 plan["origin_id"], plan["destination_id"])
                    for index, (before, after) in enumerate(zip(plans, plans[1:])):
                        minutes = round((parse_stamp(after["departure"]) -
                                         parse_stamp(before["arrival"])).total_seconds() / 60)
                        LOG.info("%s transfer at %s: planned %d minutes; minimum %d",
                                 config.id, config.legs[index].destination, minutes,
                                 config.margins[index])
                except (SourceError, ET.ParseError, ValueError) as exc:
                    LOG.error("%s %s: %s", config.id, check_day, exc)
                    errors.append(str(exc))
        if args.command == "collect":
            now = datetime.now(TZ)
            for config in configs:
                reconcile_days(db, config, now)
            for day in (now.date() - timedelta(days=1), now.date()):
                for config in configs:
                    collect(db, api, config, day, now)
        if args.command != "check":
            render(db, configs, errors, args.output)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
