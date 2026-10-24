#!/usr/bin/env python3
"""Live GB rail disruption checks for the train-disruption skill.

Two rail data sources, each with a job:

  Darwin (National Rail's live feed, read through a Huxley 2 JSON proxy) is
  authoritative for cancellations, including ones the operator has planned
  hours ahead, but it only sees about four hours forward. The public demo
  proxy needs no key.

  Realtime Trains (data.rtt.io) has the whole day's timetable, but its
  cancellations come from Network Rail's TRUST feed and usually appear only
  close to departure. Needs RTT_API_TOKEN.

Within Darwin's horizon we trust Darwin; beyond it we show RTT rows and label
them "timetable". TfL's open API covers getting across London.

Subcommands:
  board ORIGIN [--to DEST]       every departure for the rest of the day
  train ORIGIN HH:MM [--to DEST] identify a train and show its stops and estimates
  journey --route A,B[,C][+N]... compare whole routes, with changes
  direct-to DEST                 which stations have direct trains to DEST now
  tfl FROM TO                    TfL journey planner (cross-London links)
  london-status                  Tube / Elizabeth line / Overground / DLR status
  crs QUERY                      look up a station's three-letter code

Standard library only. Python 3.10+.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

HUXLEY_URL = os.environ.get("HUXLEY_URL", "https://huxley2.azurewebsites.net").rstrip("/")
DARWIN_ACCESS_TOKEN = os.environ.get("DARWIN_ACCESS_TOKEN")
RTT_URL = "https://data.rtt.io"
TFL_URL = "https://api.tfl.gov.uk"

# Darwin board limits (LDBWS): timeOffset -120..119, timeWindow <= 120, and
# boards with calling points ("expand") return at most 10 services.
DARWIN_MAX_OFFSET = 119
DARWIN_WINDOW = 120
DARWIN_DETAIL_ROWS = 10
DARWIN_BOARD_ROWS = 150
RTT_MAX_WINDOW = timedelta(hours=23, minutes=59)

DEPART_CALL_TYPES = {"ADVERTISED_OPEN", "ADVERTISED_PICK_UP"}
ARRIVE_CALL_TYPES = {"ADVERTISED_OPEN", "ADVERTISED_SET_DOWN"}
BUS_MODES = {"BUS", "SCHEDULED_BUS", "REPLACEMENT_BUS"}


# --------------------------------------------------------------------------
# HTTP


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str) -> None:
        # Drop the query string: it can carry DARWIN_ACCESS_TOKEN.
        detail = f": {body[:300]}" if body.strip() else ""
        super().__init__(f"HTTP {status} from {url.split('?')[0]}{detail}")
        self.status = status
        self.body = body


def get_json(url: str, params: dict[str, str] | None = None,
             headers: dict[str, str] | None = None) -> Any:
    if params:
        url = f"{url}?{urlencode(params)}"
    request = Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "train-disruption-skill/1.0",
        **(headers or {}),
    })
    try:
        with urlopen(request, timeout=30) as response:
            if response.status == 204:
                return None
            return json.load(response)
    except HTTPError as error:
        raise HttpError(error.code, url, error.read().decode(errors="replace")) from None


# --------------------------------------------------------------------------
# Time


def now_london() -> datetime:
    return datetime.now(LONDON).replace(second=0, microsecond=0)


def at_clock(hhmm: str, ref: datetime) -> datetime:
    """Place a bare HH:MM on whichever day puts it nearest `ref`.

    Darwin gives times without dates, and a late-evening board runs past
    midnight, so "00:15" seen at 23:40 means tomorrow.
    """
    hour, minute = (int(part) for part in hhmm.split(":"))
    candidate = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate - ref > timedelta(hours=12):
        candidate -= timedelta(days=1)
    elif ref - candidate > timedelta(hours=12):
        candidate += timedelta(days=1)
    return candidate


def parse_iso(value: str | None) -> datetime | None:
    """RTT times are ISO 8601, usually without an offset (local to the station)."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=LONDON)
    return parsed.astimezone(LONDON)


def rtt_time(value: datetime) -> str:
    return value.astimezone(LONDON).strftime("%Y-%m-%dT%H:%M:%S")


def hm(value: datetime | None) -> str:
    return value.strftime("%H:%M") if value else "--:--"


def minutes_between(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


# --------------------------------------------------------------------------
# Trips


@dataclass
class Trip:
    """One service from `origin` to `dest` (or just departing `origin`)."""

    origin: str
    dest: str | None
    operator: str
    headed_for: str
    mode: str  # "train" or "bus"
    dep_sched: datetime
    dep_est: datetime | None  # None when unknown ("Delayed") or cancelled
    arr_sched: datetime | None
    arr_est: datetime | None
    cancelled: bool
    status: str
    reason: str | None
    platform: str | None
    source: str  # "live" (Darwin) or "timetable" (RTT)

    @property
    def leaves(self) -> datetime:
        return self.dep_est or self.dep_sched

    @property
    def arrives(self) -> datetime | None:
        return self.arr_est or self.arr_sched

    @property
    def usable(self) -> bool:
        return not self.cancelled and (self.dest is None or self.arrives is not None)


REASON_PREFIX = re.compile(
    r"^This (?:service|train) has been (?:cancelled|delayed|terminated|altered)"
    r" (?:because of|by|due to) ", re.IGNORECASE)


def short_reason(text: str | None) -> str | None:
    return REASON_PREFIX.sub("", text).rstrip(".") if text else None


def strip_html(text: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())


# --------------------------------------------------------------------------
# Darwin (via Huxley 2)


def darwin_estimate(sched: datetime, text: str | None) -> tuple[datetime | None, bool]:
    """Turn an LDB estimate ("On time", "12:34", "Delayed", "Cancelled") into
    (estimated time or None, cancelled)."""
    if text == "Cancelled":
        return None, True
    if text in (None, "On time"):
        return sched, False
    if re.fullmatch(r"\d\d:\d\d", text):
        return at_clock(text, sched), False
    return None, False  # "Delayed", "No report": no usable estimate


def darwin_status(est: datetime | None, sched: datetime, raw: str | None, cancelled: bool) -> str:
    if cancelled:
        return "Cancelled"
    if raw == "Delayed":
        return "Delayed (no estimate)"
    if est and est != sched:
        return f"Exp {hm(est)}"
    return "On time"


def parse_darwin_service(service: dict[str, Any], origin: str, dest: str | None,
                         ref: datetime, mode: str) -> Trip | None:
    std = service.get("std")
    if not std:
        return None
    dep_sched = at_clock(std, ref)
    dep_est, dep_cancelled = darwin_estimate(dep_sched, service.get("etd"))
    cancelled = bool(service.get("isCancelled")) or dep_cancelled \
        or bool(service.get("filterLocationCancelled"))

    arr_sched = arr_est = None
    if dest:
        stop = next((point
                     for group in service.get("subsequentCallingPoints") or []
                     for point in group.get("callingPoint") or []
                     if point.get("crs") == dest), None)
        if stop and stop.get("st"):
            arr_sched = at_clock(stop["st"], dep_sched)
            arr_est, arr_cancelled = darwin_estimate(arr_sched, stop.get("at") or stop.get("et"))
            cancelled = cancelled or arr_cancelled or bool(stop.get("isCancelled"))

    headed_for = ", ".join(d["locationName"] for d in service.get("destination") or [])
    now_to = service.get("currentDestinations")
    if now_to:
        headed_for += f" (now only to {', '.join(d['locationName'] for d in now_to)})"

    return Trip(
        origin=origin, dest=dest, operator=service.get("operatorCode") or "?",
        headed_for=headed_for, mode=mode,
        dep_sched=dep_sched, dep_est=None if cancelled else dep_est,
        arr_sched=arr_sched, arr_est=None if cancelled else arr_est,
        cancelled=cancelled,
        status=darwin_status(dep_est, dep_sched, service.get("etd"), cancelled),
        reason=short_reason(service.get("cancelReason") if cancelled else service.get("delayReason")),
        platform=service.get("platform"), source="live",
    )


class DarwinUnavailable(Exception):
    """The Huxley proxy failed. The public demo "regularly does" go down."""


def darwin_board(kind: str, crs: str, other: str | None, rows: int, offset: int,
                 expand: bool) -> dict[str, Any]:
    """kind is "departures" (other = filter destination) or "arrivals" (other = filter origin)."""
    path = f"/{kind}/{quote(crs)}"
    if other:
        path += f"/{'to' if kind == 'departures' else 'from'}/{quote(other)}"
    params = {"timeOffset": str(offset), "timeWindow": str(DARWIN_WINDOW)}
    if expand:
        params["expand"] = "true"
    if DARWIN_ACCESS_TOKEN:
        params["accessToken"] = DARWIN_ACCESS_TOKEN
    try:
        return get_json(f"{HUXLEY_URL}{path}/{rows}", params) or {}
    except (HttpError, URLError, TimeoutError, ValueError) as error:
        raise DarwinUnavailable(f"Darwin live data unavailable ({error})") from None


def nrcc_messages(board: dict[str, Any]) -> list[str]:
    return [strip_html(message.get("value") or "") for message in board.get("nrccMessages") or []]


@dataclass
class DarwinResult:
    trips: list[Trip]
    horizon: datetime | None  # Darwin is complete up to here; None if it saw none of the window
    messages: list[str]
    station_name: str | None


def darwin_departures(origin: str, dest: str | None, start: datetime, end: datetime,
                      expand: bool) -> DarwinResult:
    """Darwin departures covering as much of [start, end] as its horizon allows."""
    now = now_london()
    offset = max(-DARWIN_MAX_OFFSET - 1, minutes_between(now, start))
    rows = DARWIN_DETAIL_ROWS if expand else DARWIN_BOARD_ROWS
    trips: list[Trip] = []
    messages: list[str] = []
    seen: set[str] = set()
    horizon: datetime | None = None
    name: str | None = None

    while offset <= DARWIN_MAX_OFFSET:
        board = darwin_board("departures", origin, dest, rows, offset, expand)
        name = board.get("locationName") or name
        messages += [m for m in nrcc_messages(board) if m not in messages]
        trains = board.get("trainServices") or []
        buses = board.get("busServices") or []
        for service, mode in [(s, "train") for s in trains] + [(s, "bus") for s in buses]:
            key = service.get("serviceID") or ""
            trip = parse_darwin_service(service, origin, dest, now, mode)
            if trip and key not in seen:
                seen.add(key)
                trips.append(trip)
        if len(trains) >= rows:
            # The board was cut off, so it is only complete up to its last train.
            horizon = max(at_clock(s["std"], now) for s in trains if s.get("std"))
            break
        horizon = now + timedelta(minutes=offset + DARWIN_WINDOW)
        if offset == DARWIN_MAX_OFFSET or horizon >= end:
            break
        offset = min(offset + DARWIN_WINDOW, DARWIN_MAX_OFFSET)

    if horizon is not None:
        trips = [t for t in trips if t.dep_sched <= horizon]
    return DarwinResult(trips, horizon, messages, name)


# --------------------------------------------------------------------------
# Realtime Trains


class Rtt:
    def __init__(self, token: str) -> None:
        self._token = token
        self._access: str | None = None

    def _bearer(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _access_token(self) -> str:
        if self._access is None:
            try:
                response = get_json(f"{RTT_URL}/api/get_access_token", headers=self._bearer(self._token))
                self._access = response["token"]
            except HttpError as error:
                if error.status not in (401, 403):
                    raise
                self._access = self._token  # already a long-life access token
        return self._access

    def lineup(self, code: str, start: datetime, end: datetime, *,
               filter_from: str | None = None, filter_to: str | None = None) -> list[dict[str, Any]]:
        params = {"code": code, "timeFrom": rtt_time(start),
                  "timeTo": rtt_time(min(end, start + RTT_MAX_WINDOW))}
        if filter_from:
            params["filterFrom"] = filter_from
        if filter_to:
            params["filterTo"] = filter_to
        data = get_json(f"{RTT_URL}/gb-nr/location", params, self._bearer(self._access_token()))
        return (data or {}).get("services") or []

    def service(self, identity: str, departure_date: str) -> dict[str, Any]:
        data = get_json(f"{RTT_URL}/gb-nr/service", {"uniqueIdentity": f"{identity}:{departure_date}"},
                        self._bearer(self._access_token()))
        return (data or {}).get("service") or {}


def rtt_from_env() -> Rtt | None:
    token = os.environ.get("RTT_API_TOKEN")
    return Rtt(token) if token else None


def rtt_reason(service: dict[str, Any], kind: str) -> str | None:
    for block in service.get("reasons") or []:
        if block.get("type") == kind:
            return block.get("longText") or block.get("shortText")
    return None


def rtt_status(est: datetime | None, sched: datetime, cancelled: bool) -> str:
    if cancelled:
        return "Cancelled"
    if est and est != sched:
        return f"Exp {hm(est)}"
    return "Timetabled"


def parse_rtt_service(service: dict[str, Any], origin: str, dest: str | None,
                      arrival: dict[str, Any] | None) -> Trip | None:
    meta = service.get("scheduleMetadata") or {}
    timing = service.get("temporalData") or {}
    departure = timing.get("departure") or {}
    dep_sched = parse_iso(departure.get("scheduleAdvertised") or departure.get("scheduleInternal"))
    if dep_sched is None or meta.get("inPassengerService") is False \
            or timing.get("scheduledCallType") not in DEPART_CALL_TYPES:
        return None
    dep_est = parse_iso(departure.get("realtimeActual") or departure.get("realtimeForecast"))
    cancelled = bool(departure.get("isCancelled")) \
        or timing.get("displayAs") in ("CANCELLED", "TERMINATES", "DIVERTED")

    arr_sched = arr_est = None
    if arrival:
        arrival_timing = arrival.get("temporalData") or {}
        arriving = arrival_timing.get("arrival") or {}
        arr_sched = parse_iso(arriving.get("scheduleAdvertised") or arriving.get("scheduleInternal"))
        arr_est = parse_iso(arriving.get("realtimeActual") or arriving.get("realtimeForecast"))
        cancelled = cancelled or bool(arriving.get("isCancelled")) \
            or arrival_timing.get("displayAs") in ("CANCELLED", "STARTS", "DIVERTED")

    if cancelled:
        reason = rtt_reason(service, "CANCEL") or (rtt_reason(arrival, "CANCEL") if arrival else None)
    else:
        reason = rtt_reason(service, "DELAY")
    operator = (meta.get("operator") or {}).get("code") or "?"
    platform = ((service.get("locationMetadata") or {}).get("platform") or {})
    return Trip(
        origin=origin, dest=dest, operator=operator,
        headed_for=", ".join(d["location"]["description"] for d in service.get("destination") or []),
        mode="bus" if meta.get("modeType") in BUS_MODES else "train",
        dep_sched=dep_sched, dep_est=None if cancelled else dep_est,
        arr_sched=arr_sched, arr_est=None if cancelled else arr_est,
        cancelled=cancelled, status=rtt_status(dep_est, dep_sched, cancelled),
        reason=reason, platform=platform.get("actual") or platform.get("planned"),
        source="timetable",
    )


def rtt_departures(rtt: Rtt, origin: str, dest: str | None, start: datetime,
                   end: datetime, with_arrivals: bool) -> list[Trip]:
    services = rtt.lineup(origin, start, end, filter_to=dest)
    arrivals: dict[str, dict[str, Any]] = {}
    if dest and with_arrivals:
        # The destination's own line-up carries the arrival times; services
        # join on their unique identity. Allow for the journey time.
        for arrival in rtt.lineup(dest, start, end + timedelta(hours=4), filter_from=origin):
            if (arrival.get("temporalData") or {}).get("scheduledCallType") in ARRIVE_CALL_TYPES:
                arrivals[arrival["scheduleMetadata"]["uniqueIdentity"]] = arrival
    trips = []
    for service in services:
        arrival = arrivals.get(service["scheduleMetadata"]["uniqueIdentity"])
        trip = parse_rtt_service(service, origin, dest, arrival)
        if trip and (arrival is not None or not arrivals):
            trips.append(trip)
    return trips


# --------------------------------------------------------------------------
# Combining sources


@dataclass
class Departures:
    trips: list[Trip]
    live_until: datetime | None
    messages: list[str]
    station_name: str | None
    notes: list[str]


def departures(origin: str, dest: str | None, start: datetime, end: datetime,
               rtt: Rtt | None, with_arrivals: bool) -> Departures:
    """Live Darwin rows as far as Darwin can see, RTT timetable rows after.
    Arrival times cost a 10-row Darwin board and a second RTT query, so only
    fetch them when the caller needs them."""
    notes: list[str] = []
    try:
        live = darwin_departures(origin, dest, start, end, expand=with_arrivals)
    except DarwinUnavailable as error:
        live = DarwinResult([], None, [], None)
        notes.append(f"WARNING: {error}. Timetable rows miss cancellations planned ahead: "
                     "check National Rail before trusting them.")
    trips = [t for t in live.trips if t.dep_sched >= start or (not t.cancelled and t.leaves >= start)]
    rest_from = live.horizon or start
    if rest_from < end:
        if rtt:
            trips += [t for t in rtt_departures(rtt, origin, dest, rest_from, end, with_arrivals)
                      if t.dep_sched <= end and (live.horizon is None or t.dep_sched > live.horizon)]
        else:
            notes.append(f"Nothing shown after {hm(rest_from)}: set RTT_API_TOKEN for the rest of the timetable.")
    trips.sort(key=lambda t: t.dep_sched)
    return Departures(trips, live.horizon, live.messages, live.station_name, notes)


def source_line(result: Departures) -> str:
    if result.live_until is None:
        return "All rows are timetable (RTT): Darwin's live data is unavailable or can't see this far ahead."
    return (f"Live (Darwin) to {hm(result.live_until)}; after that, timetable rows from RTT, "
            "where cancellations usually show only close to departure.")


# --------------------------------------------------------------------------
# Output


def table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    lines += ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in rows]
    return "\n".join(lines)


def trip_row(trip: Trip) -> list[str]:
    return [hm(trip.dep_sched), trip.status, trip.platform or "", trip.operator,
            ("BUS: " if trip.mode == "bus" else "") + trip.headed_for,
            trip.source, trip.reason or ""]


def summarise(trips: list[Trip], live_until: datetime | None) -> list[str]:
    lines = []
    live = [t for t in trips if t.source == "live"]
    later = [t for t in trips if t.source == "timetable"]
    if live:
        cancelled = [t for t in live if t.cancelled]
        delayed = [t for t in live if not t.cancelled and t.dep_est and t.leaves - t.dep_sched >= timedelta(minutes=5)]
        lines.append(f"Live window to {hm(live_until)}: {len(live)} services, {len(cancelled)} cancelled, "
                     f"{len(delayed)} running 5+ min late.")
        if cancelled:
            reasons = Counter(t.reason or "no reason given" for t in cancelled)
            lines.append("Cancellation reasons: " + "; ".join(f"{r} ({n})" for r, n in reasons.most_common()))
            usable = [t for t in live if not t.cancelled]
            lines.append("Next running service: " + (
                f"{hm(usable[0].dep_sched)} to {usable[0].headed_for}" if usable
                else "none in the live window"))
    if later:
        cancelled = sum(t.cancelled for t in later)
        lines.append(f"Timetable{' after that' if live else ''}: {len(later)} services, "
                     f"{cancelled} already marked cancelled in TRUST.")
    if not trips:
        lines.append("No services found in this window.")
    return lines


def print_messages(messages: list[str]) -> None:
    if messages:
        print("\nNational Rail disruption messages for this station:")
        for message in messages:
            print(f"  - {message}")


# --------------------------------------------------------------------------
# Commands


def window(args: argparse.Namespace, default_hours: float | None) -> tuple[datetime, datetime]:
    now = now_london()
    day = date.fromisoformat(args.date) if args.date else now.date()
    start = now
    if args.at:
        hour, minute = (int(part) for part in args.at.split(":"))
        start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=LONDON)
    elif args.date:
        start = datetime(day.year, day.month, day.day, tzinfo=LONDON)
    if args.hours is not None:
        return start, start + timedelta(hours=args.hours)
    if default_hours is not None:
        return start, start + timedelta(hours=default_hours)
    end_of_day = start.replace(hour=23, minute=59)
    return start, max(end_of_day, start + timedelta(hours=1))


def cmd_board(args: argparse.Namespace) -> None:
    origin, dest = args.origin.upper(), args.to.upper() if args.to else None
    start, end = window(args, None)
    result = departures(origin, dest, start, end, rtt_from_env(), with_arrivals=False)
    title = f"{result.station_name or origin} departures" + (f" calling at {dest}" if dest else "")
    print(f"{title}, {start:%a %d %b} {hm(start)}-{hm(end)}")
    print(source_line(result))
    for note in result.notes:
        print(note)
    print()
    print(table([trip_row(t) for t in result.trips],
                ["Dep", "Status", "Plat", "Op", "Train to", "Source", "Reason"]))
    print()
    for line in summarise(result.trips, result.live_until):
        print(line)
    print_messages(result.messages)


def cmd_train(args: argparse.Namespace) -> None:
    """Find the service leaving ORIGIN nearest HH:MM (scheduled or actual, so
    "the 10:31" and "it left at 10:40" both work) and list its stops."""
    rtt = rtt_from_env()
    if rtt is None:
        sys.exit("train needs RTT_API_TOKEN.")
    origin, dest = args.origin.upper(), args.to.upper() if args.to else None
    day = date.fromisoformat(args.date) if args.date else now_london().date()
    hour, minute = (int(part) for part in args.time.split(":"))
    target = datetime(day.year, day.month, day.day, hour, minute, tzinfo=LONDON)

    def gap(service: dict[str, Any]) -> timedelta:
        departure = (service.get("temporalData") or {}).get("departure") or {}
        times = [parse_iso(departure.get(key)) for key in
                 ("scheduleAdvertised", "realtimeActual", "realtimeForecast")]
        return min((abs(t - target) for t in times if t), default=timedelta.max)

    candidates = [s for s in rtt.lineup(origin, target - timedelta(minutes=45), target + timedelta(minutes=15),
                                        filter_to=dest)
                  if (s.get("temporalData") or {}).get("scheduledCallType") in DEPART_CALL_TYPES]
    if not candidates:
        sys.exit(f"No passenger services from {origin}" + (f" to {dest}" if dest else "") + f" near {args.time}.")
    pick = min(candidates, key=gap)
    meta = pick["scheduleMetadata"]
    detail = rtt.service(meta["identity"], meta["departureDate"])

    def when(timing: dict[str, Any], done: str) -> str:
        sched = parse_iso(timing.get("scheduleAdvertised") or timing.get("scheduleInternal"))
        actual = parse_iso(timing.get("realtimeActual"))
        forecast = parse_iso(timing.get("realtimeForecast"))
        if timing.get("isCancelled"):
            return f"{hm(sched)} CANCELLED"
        if actual:
            return f"{hm(sched)} {done} {hm(actual)}"
        if forecast and forecast != sched:
            return f"{hm(sched)} exp {hm(forecast)}"
        return hm(sched) if sched else ""

    origin_names = ", ".join(o["location"]["description"] for o in pick.get("origin") or [])
    dest_names = ", ".join(d["location"]["description"] for d in pick.get("destination") or [])
    print(f"{(meta.get('operator') or {}).get('name', '?')} {origin_names} to {dest_names}, "
          f"train {meta.get('trainReportingIdentity', '?')}")
    rows = []
    started = False
    for stop in detail.get("locations") or []:
        location = stop.get("location") or {}
        timing = stop.get("temporalData") or {}
        started = started or origin in (location.get("shortCodes") or [])
        if not started or timing.get("displayAs") in (None, "PASS"):
            continue
        platform = (stop.get("locationMetadata") or {}).get("platform") or {}
        rows.append([location.get("description", "?"), when(timing.get("arrival") or {}, "arrived"),
                     when(timing.get("departure") or {}, "left"), platform.get("actual") or platform.get("planned") or "",
                     "" if timing.get("displayAs") == "CALL" else timing.get("displayAs") or ""])
    print(table(rows, ["Stop", "Arrive", "Depart", "Plat", "Note"]))
    reasons = [r.get("longText") or r.get("shortText") for r in detail.get("reasons") or []]
    if reasons:
        print("\nReasons given: " + "; ".join(r for r in reasons if r))


def parse_route(spec: str) -> tuple[list[str], int]:
    """"KGX,CBG+15" -> (["KGX", "CBG"], 15): stations in order, and minutes
    needed to reach the first one."""
    lead = 0
    if "+" in spec:
        spec, lead_text = spec.rsplit("+", 1)
        lead = int(lead_text)
    stations = [s.strip().upper() for s in spec.split(",") if s.strip()]
    if len(stations) < 2:
        raise argparse.ArgumentTypeError(f"route needs at least two stations: {spec!r}")
    return stations, lead


def build_itineraries(legs: list[list[Trip]], earliest: datetime,
                      change: timedelta) -> list[list[Trip]]:
    """Chain each usable first-leg trip to the first connection that can be
    made, then drop journeys that arrive no earlier than one leaving later."""
    journeys = []
    for first in legs[0]:
        if not first.usable or first.leaves < earliest:
            continue
        chain = [first]
        for options in legs[1:]:
            arrived = chain[-1].arrives
            if arrived is None:
                break
            onward = next((t for t in options if t.usable and t.leaves >= arrived + change), None)
            if onward is None:
                break
            chain.append(onward)
        if len(chain) == len(legs):
            journeys.append(chain)
    journeys.sort(key=lambda chain: chain[0].leaves)
    kept: list[list[Trip]] = []
    best: datetime | None = None
    for chain in reversed(journeys):
        arrival = chain[-1].arrives
        if arrival is not None and (best is None or arrival < best):
            kept.append(chain)
            best = arrival
    return list(reversed(kept))


@dataclass
class RouteOutcome:
    stations: list[str]
    lead: int
    legs: list[Departures]
    journeys: list[list[Trip]]

    @property
    def earliest_arrival(self) -> datetime | None:
        return self.journeys[0][-1].arrives if self.journeys else None


def evaluate_route(spec: str, start: datetime, end: datetime, change: timedelta,
                   rtt: Rtt | None) -> RouteOutcome:
    stations, lead = parse_route(spec)
    earliest = start + timedelta(minutes=lead)
    legs = []
    for index, (a, b) in enumerate(zip(stations, stations[1:])):
        # Later legs need to reach past the window for late connections.
        leg_end = end if index == 0 else end + timedelta(hours=3)
        legs.append(departures(a, b, earliest, leg_end, rtt, with_arrivals=True))
    journeys = build_itineraries([leg.trips for leg in legs], earliest, change)
    return RouteOutcome(stations, lead, legs, journeys)


def describe_trip(trip: Trip) -> str:
    return (f"{trip.origin} {hm(trip.leaves)} > {trip.dest} {hm(trip.arrives)}  "
            f"{trip.operator} to {trip.headed_for}"
            + (f", plat {trip.platform}" if trip.platform else "")
            + (f", {trip.status}" if trip.status not in ("On time", "Timetabled") else ""))


def cmd_journey(args: argparse.Namespace) -> None:
    start, end = window(args, 4)
    rtt = rtt_from_env()
    if rtt is None:
        print("RTT_API_TOKEN not set: only Darwin's live window (about 4 hours) is visible.\n")
    outcomes = [evaluate_route(spec, start, end, timedelta(minutes=args.change), rtt) for spec in args.route]

    print(f"Routes leaving from {hm(start)} {start:%a %d %b}, changes of at least {args.change} min")
    notes = dict.fromkeys(note for o in outcomes for leg in o.legs for note in leg.notes)
    for note in notes:
        print(note)
    reachable = [o for o in outcomes if o.earliest_arrival is not None]
    if reachable:
        best = min(reachable, key=lambda o: o.earliest_arrival or end)
        first = best.journeys[0]
        print(f"Earliest arrival: {' > '.join(best.stations)}, leave {hm(first[0].leaves)}, "
              f"arrive {hm(first[-1].arrives)}")
    else:
        print("No route has a usable journey in this window.")

    messages: dict[str, list[str]] = {}
    for outcome in outcomes:
        stations = outcome.stations
        print(f"\n== {' > '.join(stations)}"
              + (f" (allowing {outcome.lead} min to reach {stations[0]})" if outcome.lead else ""))
        for index, leg in enumerate(outcome.legs):
            live = [t for t in leg.trips if t.source == "live"]
            print(f"   {stations[index]}>{stations[index + 1]}: {len(leg.trips)} departures, "
                  f"{sum(t.cancelled for t in leg.trips)} cancelled"
                  + (f" ({sum(t.cancelled for t in live)} of {len(live)} in the live window "
                     f"to {hm(leg.live_until)})" if live else ""))
            if leg.messages:
                messages[leg.station_name or stations[index]] = leg.messages
        if not outcome.journeys:
            print("   No usable journeys.")
            reasons = Counter(t.reason for t in outcome.legs[0].trips if t.cancelled and t.reason)
            if reasons:
                print(f"   Main reason: {reasons.most_common(1)[0][0]}")
            continue
        for chain in outcome.journeys[:args.limit]:
            arrives = chain[-1].arrives or chain[-1].leaves
            flag = " [timetable only]" if any(t.source == "timetable" for t in chain) else ""
            print(f"   leave {hm(chain[0].leaves)} arrive {hm(arrives)} "
                  f"({minutes_between(chain[0].leaves, arrives)} min){flag}")
            if len(chain) > 1 or args.verbose:
                for trip in chain:
                    print(f"      {describe_trip(trip)}")

    for station, station_messages in messages.items():
        print(f"\nNational Rail messages at {station}:")
        for message in station_messages:
            print(f"  - {message}")


def cmd_direct_to(args: argparse.Namespace) -> None:
    """Which stations have direct trains to DEST in the next couple of hours,
    and how many of those trains are cancelled. Expanded boards return 10
    services at a time, so step forward through a few pages."""
    dest = args.dest.upper()
    now = now_london()
    offset = 0
    seen: set[str] = set()
    calls: dict[str, dict[str, Any]] = defaultdict(lambda: {"name": "", "trains": 0, "cancelled": 0, "ops": set()})
    first_arrival: datetime | None = None
    last_arrival: datetime | None = None
    name = dest
    messages: list[str] = []
    for _ in range(args.pages):
        board = darwin_board("arrivals", dest, None, DARWIN_DETAIL_ROWS, offset, expand=True)
        name = board.get("locationName") or name
        messages += [m for m in nrcc_messages(board) if m not in messages]
        services = board.get("trainServices") or []
        for service in services:
            if service.get("serviceID") in seen or not service.get("sta"):
                continue
            seen.add(service.get("serviceID") or "")
            arrival = at_clock(service["sta"], now)
            first_arrival = first_arrival or arrival
            last_arrival = arrival
            arrival_cancelled = bool(service.get("isCancelled")) or service.get("eta") == "Cancelled"
            for group in service.get("previousCallingPoints") or []:
                for point in group.get("callingPoint") or []:
                    stats = calls[point.get("crs") or "?"]
                    stats["name"] = point.get("locationName") or ""
                    stats["trains"] += 1
                    stats["ops"].add(service.get("operatorCode") or "?")
                    if arrival_cancelled or point.get("isCancelled") or point.get("et") == "Cancelled":
                        stats["cancelled"] += 1
        if len(services) < DARWIN_DETAIL_ROWS or last_arrival is None:
            break
        next_offset = minutes_between(now, last_arrival) + 1
        if next_offset <= offset or next_offset > DARWIN_MAX_OFFSET:
            break
        offset = next_offset

    print(f"Stations with direct trains to {name} ({dest}), arrivals {hm(first_arrival)}-{hm(last_arrival)}, "
          f"{len(seen)} trains")
    ranked = sorted(calls.items(), key=lambda item: (-item[1]["trains"], item[1]["name"]))
    if args.match:
        ranked = [item for item in ranked if args.match.lower() in item[1]["name"].lower()]
    print(table([[f"{s['name']} ({crs})", str(s["trains"]), str(s["cancelled"]), ",".join(sorted(s["ops"]))]
                 for crs, s in ranked[:args.top]],
                ["From", "Trains", "Cancelled", "Operators"]))
    print_messages(messages)


def tfl_journey(origin: str, dest: str, params: dict[str, str]) -> dict[str, Any]:
    """TfL answers an ambiguous place with HTTP 300 and a list of candidates;
    take the best-scoring candidate for each end and ask once more."""
    for _ in range(2):
        try:
            return get_json(f"{TFL_URL}/Journey/JourneyResults/{quote(origin)}/to/{quote(dest)}", params)
        except HttpError as error:
            if error.status != 300:
                raise
            body = json.loads(error.body)
            for key in ("fromLocationDisambiguation", "toLocationDisambiguation"):
                options = (body.get(key) or {}).get("disambiguationOptions") or []
                if not options:
                    continue
                best = max(options, key=lambda option: option.get("matchQuality", 0))
                place = best.get("place") or {}
                print(f"(Taking {place.get('commonName', best['parameterValue'])} for "
                      f"{origin if key.startswith('from') else dest})")
                if key.startswith("from"):
                    origin = best["parameterValue"]
                else:
                    dest = best["parameterValue"]
    raise SystemExit("TfL could not resolve those places; try a postcode, lat,lon or station code.")


def cmd_tfl(args: argparse.Namespace) -> None:
    params = {"timeIs": "Departing"}
    if args.at:
        params["time"] = args.at.replace(":", "")
    if args.date:
        params["date"] = args.date.replace("-", "")
    if args.modes:
        params["mode"] = args.modes
    result = tfl_journey(args.origin, args.dest, params)
    disruptions: list[str] = []
    for journey in (result.get("journeys") or [])[:args.limit]:
        start = parse_iso(journey.get("startDateTime"))
        arrive = parse_iso(journey.get("arrivalDateTime"))
        legs = journey.get("legs") or []
        summary = " > ".join((leg.get("instruction") or {}).get("summary", "") for leg in legs)
        flag = " [DISRUPTED]" if any(leg.get("isDisrupted") for leg in legs) else ""
        print(f"{hm(start)} > {hm(arrive)} ({journey.get('duration')} min){flag}: {summary}")
        for leg in legs:
            for disruption in leg.get("disruptions") or []:
                text = strip_html(disruption.get("description") or "")
                if text and text not in disruptions:
                    disruptions.append(text)
    if disruptions:
        print("\nTfL disruption notes on these legs:")
        for text in disruptions:
            print(f"  - {text}")


def cmd_london_status(args: argparse.Namespace) -> None:
    lines = get_json(f"{TFL_URL}/Line/Mode/{args.modes}/Status")
    problems = []
    for line in lines:
        for status in line.get("lineStatuses") or []:
            if status.get("statusSeverityDescription") != "Good Service":
                problems.append(f"{line['name']}: {status['statusSeverityDescription']}"
                                + (f". {strip_html(status['reason'])}" if status.get("reason") else ""))
    print("\n".join(problems) if problems else f"Good service on all {args.modes} lines.")


def cmd_crs(args: argparse.Namespace) -> None:
    params = {"accessToken": DARWIN_ACCESS_TOKEN} if DARWIN_ACCESS_TOKEN else None
    for station in get_json(f"{HUXLEY_URL}/crs/{quote(args.query)}", params) or []:
        print(f"{station['crsCode']}  {station['stationName']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    def add_window(command: argparse.ArgumentParser) -> None:
        command.add_argument("--at", help="start time HH:MM (default now)")
        command.add_argument("--date", help="YYYY-MM-DD (default today)")
        command.add_argument("--hours", type=float, help="length of the window")

    board = commands.add_parser("board", help="departures for the rest of the day")
    board.add_argument("origin", help="station code, e.g. LST")
    board.add_argument("--to", help="only services calling at this station")
    add_window(board)
    board.set_defaults(handler=cmd_board)

    train = commands.add_parser("train", help="identify a train and show its stops (needs RTT_API_TOKEN)")
    train.add_argument("origin", help="station it left from, e.g. STP")
    train.add_argument("time", help="HH:MM, scheduled or actual departure")
    train.add_argument("--to", help="a station it calls at later, to pick the right train")
    train.add_argument("--date", help="YYYY-MM-DD (default today)")
    train.set_defaults(handler=cmd_train)

    journey = commands.add_parser("journey", help="compare routes, with changes")
    journey.add_argument("--route", action="append", required=True,
                         help="comma-separated station codes, optional +N minutes to reach the first, "
                              "e.g. LST,CBG or KGX,CBG+15 or LST,ELY,KLN")
    journey.add_argument("--change", type=int, default=5, help="minimum minutes to change trains")
    journey.add_argument("--limit", type=int, default=4, help="journeys to list per route")
    journey.add_argument("--verbose", action="store_true", help="show the train for single-leg journeys too")
    add_window(journey)
    journey.set_defaults(handler=cmd_journey)

    direct = commands.add_parser("direct-to", help="stations with direct trains to DEST right now")
    direct.add_argument("dest")
    direct.add_argument("--pages", type=int, default=3, help="pages of 10 arrivals to read")
    direct.add_argument("--top", type=int, default=30)
    direct.add_argument("--match", help="only stations whose name contains this, e.g. London")
    direct.set_defaults(handler=cmd_direct_to)

    tfl = commands.add_parser("tfl", help="TfL journey planner")
    tfl.add_argument("origin", help="place name, postcode, lat,lon or TfL stop id")
    tfl.add_argument("dest")
    tfl.add_argument("--at", help="depart at HH:MM")
    tfl.add_argument("--date", help="YYYY-MM-DD")
    tfl.add_argument("--modes", help="e.g. tube,elizabeth-line,overground,national-rail,walking")
    tfl.add_argument("--limit", type=int, default=3)
    tfl.set_defaults(handler=cmd_tfl)

    status = commands.add_parser("london-status", help="TfL line status")
    status.add_argument("--modes", default="tube,elizabeth-line,overground,dlr")
    status.set_defaults(handler=cmd_london_status)

    crs = commands.add_parser("crs", help="station code lookup")
    crs.add_argument("query")
    crs.set_defaults(handler=cmd_crs)

    args = parser.parse_args()
    try:
        args.handler(args)
    except (HttpError, DarwinUnavailable) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
