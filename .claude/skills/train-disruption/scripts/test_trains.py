"""Tests for trains.py. Run: python3 test_trains.py (from this folder).

Fixtures are trimmed copies of real Huxley (Darwin) and RTT responses from
24 October 2026, when overhead-wire damage cancelled Liverpool Street to
Cambridge trains.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from typing import Any
from unittest import mock

import trains
from trains import LONDON, Trip

NOW = datetime(2026, 10, 24, 10, 18, tzinfo=LONDON)


def at(hhmm: str, day: int = 24) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(2026, 10, day, hour, minute, tzinfo=LONDON)


def darwin_service(std: str, etd: str, cbg_st: str = "11:45", cbg_et: str = "On time",
                   **extra: Any) -> dict[str, Any]:
    return {
        "std": std, "etd": etd, "operatorCode": "GN", "platform": "10",
        "serviceID": f"svc-{std}",
        "destination": [{"locationName": "Kings Lynn", "crs": "KLN"}],
        "currentDestinations": None, "isCancelled": etd == "Cancelled",
        "filterLocationCancelled": False, "cancelReason": None, "delayReason": None,
        "subsequentCallingPoints": [{"callingPoint": [
            {"locationName": "Cambridge", "crs": "CBG", "st": cbg_st, "et": cbg_et,
             "at": None, "isCancelled": False},
        ]}],
        **extra,
    }


def rtt_service(uid: str, dep: str, call_type: str = "ADVERTISED_PICK_UP",
                cancelled: bool = False) -> dict[str, Any]:
    return {
        "temporalData": {
            "departure": {"scheduleAdvertised": f"2026-10-24T{dep}:00", "isCancelled": cancelled},
            "scheduledCallType": call_type,
            "displayAs": "CANCELLED" if cancelled else "CALL",
        },
        "locationMetadata": {"platform": {"planned": "2"}},
        "scheduleMetadata": {"uniqueIdentity": uid, "operator": {"code": "LE"},
                             "modeType": "TRAIN", "inPassengerService": True},
        "destination": [{"location": {"description": "Cambridge North"}}],
    }


def rtt_arrival(uid: str, arr: str) -> dict[str, Any]:
    return {
        "temporalData": {"arrival": {"scheduleAdvertised": f"2026-10-24T{arr}:00"},
                         "scheduledCallType": "ADVERTISED_OPEN", "displayAs": "CALL"},
        "scheduleMetadata": {"uniqueIdentity": uid},
    }


def trip(origin: str, dest: str, dep: str, arr: str, cancelled: bool = False) -> Trip:
    return Trip(origin=origin, dest=dest, operator="XX", headed_for=dest, mode="train",
                dep_sched=at(dep), dep_est=None if cancelled else at(dep),
                arr_sched=at(arr), arr_est=None if cancelled else at(arr),
                cancelled=cancelled, status="Cancelled" if cancelled else "On time",
                reason=None, platform=None, source="live")


class ClockTests(unittest.TestCase):
    def test_bare_times_land_on_the_nearest_day(self) -> None:
        late = datetime(2026, 10, 24, 23, 40, tzinfo=LONDON)
        self.assertEqual(trains.at_clock("00:15", late), at("00:15", day=25))
        early = datetime(2026, 10, 25, 0, 10, tzinfo=LONDON)
        self.assertEqual(trains.at_clock("23:50", early), at("23:50", day=24))

    def test_rtt_times_without_offset_are_london_local(self) -> None:
        self.assertEqual(trains.parse_iso("2026-10-24T11:05:00"), at("11:05"))


class DarwinParsingTests(unittest.TestCase):
    def test_train_now_starting_further_down_the_line_is_cancelled_here(self) -> None:
        service = darwin_service(
            "10:54", "Cancelled", cancelReason="This service has been cancelled because of a fault on a train",
            currentOrigins=[{"locationName": "Cambridge", "crs": "CBG"}])
        parsed = trains.parse_darwin_service(service, "KGX", "CBG", NOW, "train")
        assert parsed is not None
        self.assertTrue(parsed.cancelled)
        self.assertFalse(parsed.usable)
        self.assertEqual(parsed.reason, "a fault on a train")

    def test_delayed_train_carries_estimates_through_to_the_destination(self) -> None:
        parsed = trains.parse_darwin_service(darwin_service("10:31", "10:40", "11:39", "11:46"),
                                             "STP", "CBG", NOW, "train")
        assert parsed is not None
        self.assertEqual((parsed.leaves, parsed.arrives), (at("10:40"), at("11:46")))
        self.assertEqual(parsed.status, "Exp 10:40")

    def test_delayed_without_estimate_falls_back_to_the_timetable(self) -> None:
        parsed = trains.parse_darwin_service(darwin_service("10:31", "Delayed"), "STP", "CBG", NOW, "train")
        assert parsed is not None
        self.assertEqual(parsed.leaves, at("10:31"))
        self.assertEqual(parsed.status, "Delayed (no estimate)")


class RttParsingTests(unittest.TestCase):
    def test_arrival_joins_on_unique_identity(self) -> None:
        rtt = mock.Mock()
        rtt.lineup.side_effect = [
            [rtt_service("a", "12:53"), rtt_service("b", "13:00", call_type="ADVERTISED_SET_DOWN")],
            [rtt_arrival("a", "14:07")],
        ]
        found = trains.rtt_departures(rtt, "LST", "CBG", at("12:00"), at("14:00"), with_arrivals=True)
        self.assertEqual([(t.dep_sched, t.arrives, t.source) for t in found],
                         [(at("12:53"), at("14:07"), "timetable")])


class ItineraryTests(unittest.TestCase):
    def test_skips_cancellations_respects_change_time_and_drops_slower_options(self) -> None:
        first_leg = [
            trip("LST", "SRA", "10:00", "10:07", cancelled=True),
            trip("LST", "SRA", "10:05", "10:12"),
            trip("LST", "SRA", "10:10", "10:17"),
        ]
        second_leg = [
            trip("SRA", "CBG", "10:15", "11:15"),  # only 3 minutes after the 10:12 arrives
            trip("SRA", "CBG", "10:25", "11:20"),
        ]
        journeys = trains.build_itineraries([first_leg, second_leg], at("09:55"), timedelta(minutes=5))
        # 10:05 and 10:10 both make the 10:25; the later departure wins.
        self.assertEqual([[t.dep_sched for t in chain] for chain in journeys], [[at("10:10"), at("10:25")]])


class DeparturesTests(unittest.TestCase):
    def test_timetable_rows_fill_in_only_after_a_truncated_live_board(self) -> None:
        board = {"trainServices": [darwin_service(f"{10 + i // 2}:{'54' if i % 2 else '24'}", "On time")
                                   for i in range(trains.DARWIN_DETAIL_ROWS)],
                 "locationName": "London Kings Cross", "nrccMessages": [{"value": "<p>Tree on line</p>"}]}
        rtt = mock.Mock()
        rtt.lineup.side_effect = [
            [rtt_service("early", "14:24"), rtt_service("late", "15:24")],
            [rtt_arrival("early", "15:10"), rtt_arrival("late", "16:10")],
        ]
        with mock.patch.object(trains, "now_london", return_value=NOW), \
                mock.patch.object(trains, "darwin_board", return_value=board):
            result = trains.departures("KGX", "CBG", NOW, at("18:00"), rtt, with_arrivals=True)
        self.assertEqual(result.live_until, at("14:54"))  # the tenth live row
        self.assertEqual([t.source for t in result.trips], ["live"] * 10 + ["timetable"])
        self.assertEqual(result.trips[-1].dep_sched, at("15:24"))
        self.assertEqual(result.messages, ["Tree on line"])

    def test_falls_back_to_timetable_with_a_warning_when_darwin_is_down(self) -> None:
        rtt = mock.Mock()
        rtt.lineup.return_value = [rtt_service("a", "12:53")]
        with mock.patch.object(trains, "now_london", return_value=NOW), \
                mock.patch.object(trains, "darwin_board", side_effect=trains.DarwinUnavailable("down")):
            result = trains.departures("LST", "CBG", NOW, at("18:00"), rtt, with_arrivals=False)
        self.assertIsNone(result.live_until)
        self.assertEqual([t.source for t in result.trips], ["timetable"])
        self.assertTrue(result.notes[0].startswith("WARNING: down."))


if __name__ == "__main__":
    unittest.main()
