import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from zug404 import (TZ, ConfigError, Connection, Leg, SourceError, database, eligible,
                    evaluate, load_connection, load_credentials, matching_pair,
                    observe, parse_changes, parse_stations, parse_stops, reconcile_days,
                    render, resolve_plans, stamp,
                    upsert_day)


DAY = date(2026, 10, 1)
CONFIG = Connection("test", "A → C", (0, 1, 2, 3, 4), "de_nationwide",
                    (Leg("RE 89", "A", "B", "08:08"),
                     Leg("ICE 1100", "B", "C", "09:19")), (5,))


class TestConfigAndSource(unittest.TestCase):
    def test_default_connections_are_relative_to_script(self):
        script = Path(__file__).resolve().parents[1] / "zug404.py"
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            result = subprocess.run(
                [sys.executable, str(script), "render", "--database", str(folder / "db.sqlite"),
                 "--output", str(folder / "site")],
                cwd=folder, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("augsburg-berlin.html", (folder / "site" / "index.html").read_text())

    def test_collect_reports_missing_connections(self):
        script = Path(__file__).resolve().parents[1] / "zug404.py"
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            result = subprocess.run(
                [sys.executable, str(script), "collect", "--connections", str(folder / "missing"),
                 "--database", str(folder / "db.sqlite"), "--output", str(folder / "site")],
                cwd=folder, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("no connection files found", result.stderr)

    def test_credentials_are_read_beside_script(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "credentials.toml").write_text('DB_CLIENT_ID = "client"\nDB_API_KEY = "key"\n')
            with patch("zug404.__file__", str(folder / "zug404.py")):
                self.assertEqual(load_credentials(), ("client", "key"))

    def test_missing_or_invalid_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "credentials.toml"
            with self.assertRaisesRegex(SourceError, "create this file"):
                load_credentials(path)
            path.write_text('DB_CLIENT_ID = "client"\nDB_API_KEY = ""\n')
            with self.assertRaisesRegex(SourceError, "nonempty strings"):
                load_credentials(path)
            path.write_text('DB_CLIENT_ID = "client"\nDB_API_KEY = [')
            with self.assertRaisesRegex(SourceError, "valid TOML"):
                load_credentials(path)

    def test_holidays(self):
        self.assertFalse(eligible(CONFIG, date(2026, 10, 3)))
        self.assertFalse(eligible(CONFIG, date(2026, 4, 3)))  # Good Friday
        self.assertFalse(eligible(CONFIG, date(2026, 5, 14)))  # Ascension
        self.assertTrue(eligible(CONFIG, DAY))

    def test_xml_and_exact_trip_matching(self):
        self.assertEqual(parse_stations(b'<stations><station name="A" eva="1"/></stations>', "A"), "1")
        origin = parse_stops(b'<timetable><s id="trip-261001-1"><tl c="RE" n="89"/>'
                             b'<dp pt="2610010808"/></s></timetable>')
        destination = parse_stops(b'<timetable><s id="trip-261001-2"><tl c="RE" n="89"/>'
                                  b'<ar pt="2610010900"/></s></timetable>')
        self.assertEqual(matching_pair(origin, destination, CONFIG.legs[0], DAY),
                         (origin[0], destination[0]))
        with self.assertRaisesRegex(Exception, "found 2"):
            matching_pair(origin * 2, destination, CONFIG.legs[0], DAY)
        changes = parse_changes(b'<timetable><s id="trip-261001-2"><ar ct="2610010904" '
                                b'cs="c"/></s></timetable>')
        self.assertEqual(changes["trip-261001-2"]["ar"]["cs"], "c")

    def test_invalid_transfer_config(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.toml"
            path.write_text('id="test"\nname="Test"\nweekdays=["mon"]\n'
                            '[[legs]]\ntrain="RE 89"\nfrom="A"\nto="B"\n'
                            'scheduled_departure="08:08"\n[[legs]]\ntrain="ICE 1100"\n'
                            'from="C"\nto="D"\nscheduled_departure="09:19"\n')
            with self.assertRaises(ConfigError):
                load_connection(path)


class TestOutcomes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = database(Path(self.temp.name) / "db.sqlite")
        self.plans = [
            ("test", DAY.isoformat(), 0, "1", "2", "a-261001-1", "a-261001-2", "2610010808", "2610010910"),
            ("test", DAY.isoformat(), 1, "2", "3", "b-261001-1", "b-261001-2", "2610010919", "2610011300"),
        ]
        self.db.executemany("INSERT INTO planned VALUES (?,?,?,?,?,?,?,?,?)", self.plans)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def record(self, index, kind, observed, changed=None, status=None):
        self.db.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?)",
                        ("test", DAY.isoformat(), index, kind, observed.isoformat(),
                         changed, status, json.dumps({"ct": changed, "cs": status})))
        self.db.commit()

    def base(self, second_departure="2610010919"):
        self.record(0, "origin", datetime(2026, 10, 1, 8, 8, tzinfo=TZ))
        self.record(0, "destination", datetime(2026, 10, 1, 9, 10, tzinfo=TZ), "2610010914")
        self.record(1, "origin", datetime(2026, 10, 1, 9, 19, tzinfo=TZ), second_departure)
        self.record(1, "destination", datetime(2026, 10, 1, 13, 0, tzinfo=TZ))

    def outcome(self):
        rows = self.db.execute("SELECT * FROM planned ORDER BY leg_index").fetchall()
        return evaluate(self.db, CONFIG, DAY, rows, datetime(2026, 10, 1, 13, 20, tzinfo=TZ))

    def test_transfer_margins_four_five_six_and_reported_delay(self):
        for departure, result in (("2610010918", "failed"), ("2610010919", "worked"),
                                  ("2610010920", "worked")):
            self.db.execute("DELETE FROM observations")
            self.base(departure)
            self.assertEqual(self.outcome()[0], result)
        self.assertEqual(self.outcome()[2], 0)

    def test_cancellation_revocation_and_missing_check(self):
        self.base()
        self.record(1, "origin", datetime(2026, 10, 1, 9, 20, tzinfo=TZ), status="c")
        self.assertEqual(self.outcome()[0], "failed")
        self.record(1, "origin", datetime(2026, 10, 1, 9, 21, tzinfo=TZ), status="p")
        self.assertEqual(self.outcome()[0], "worked")
        self.db.execute("DELETE FROM observations WHERE leg_index=1 AND station_kind='destination'")
        self.assertEqual(self.outcome()[0], "unknown")

    def test_overnight_timestamp_and_idempotent_day(self):
        self.assertEqual(stamp(datetime(2026, 10, 2, 0, 10, tzinfo=TZ)), "2610020010")
        now = datetime(2026, 10, 1, 13, 20, tzinfo=TZ)
        upsert_day(self.db, CONFIG, DAY, "unknown", "waiting", None, now)
        upsert_day(self.db, CONFIG, DAY, "failed", "cancelled", None, now)
        self.assertEqual(self.db.execute("SELECT count(*) FROM days").fetchone()[0], 1)
        render(self.db, [CONFIG], [], Path(self.temp.name) / "site")
        page = (Path(self.temp.name) / "site" / "test.html").read_text()
        self.assertIn("1 eligible days", page)
        self.assertIn("1 samples", page)

    def test_overnight_arrival_delay(self):
        self.db.execute("UPDATE planned SET arrival='2610020010' WHERE leg_index=1")
        self.db.commit()
        self.record(0, "origin", datetime(2026, 10, 1, 8, 8, tzinfo=TZ))
        self.record(0, "destination", datetime(2026, 10, 1, 9, 10, tzinfo=TZ))
        self.record(1, "origin", datetime(2026, 10, 1, 9, 19, tzinfo=TZ))
        self.record(1, "destination", datetime(2026, 10, 2, 0, 15, tzinfo=TZ), "2610020015")
        rows = self.db.execute("SELECT * FROM planned ORDER BY leg_index").fetchall()
        self.assertEqual(evaluate(self.db, CONFIG, DAY, rows,
                                  datetime(2026, 10, 2, 0, 30, tzinfo=TZ)),
                         ("worked", "all reported transfers feasible", 5))

    def test_missed_eligible_day_is_unknown_after_activation(self):
        reconcile_days(self.db, CONFIG, datetime(2026, 10, 1, 7, 0, tzinfo=TZ))
        reconcile_days(self.db, CONFIG, datetime(2026, 10, 5, 12, 0, tzinfo=TZ))
        rows = self.db.execute("SELECT service_date, outcome FROM days ORDER BY service_date").fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows],
                         [("2026-10-01", "unknown"), ("2026-10-02", "unknown"),
                          ("2026-10-05", "unknown")])

    def test_render_shows_complete_trip_and_all_transfer_times(self):
        extended = Connection("test", "A → D", CONFIG.weekdays, CONFIG.exclude_holidays,
                              CONFIG.legs + (Leg("RE 3", "C", "D", "13:15"),), (5, 7))
        self.db.execute("INSERT INTO planned VALUES (?,?,?,?,?,?,?,?,?)",
                        ("test", DAY.isoformat(), 2, "3", "4", "c-261001-1", "c-261001-2",
                         "2610011315", "2610011430"))
        self.base()
        self.record(2, "origin", datetime(2026, 10, 1, 13, 15, tzinfo=TZ))
        self.record(2, "destination", datetime(2026, 10, 1, 14, 30, tzinfo=TZ))
        upsert_day(self.db, extended, DAY, "worked", "all reported transfers feasible", 0,
                   datetime(2026, 10, 1, 15, 0, tzinfo=TZ))
        output = Path(self.temp.name) / "site"
        render(self.db, [extended], [], output)
        for filename in ("index.html", "test.html"):
            page = (output / filename).read_text()
            self.assertEqual(page.count('<ol class="itinerary">'), 1)
            visible_trip = page.split('<ol class="itinerary">', 1)[1].split('</ol>', 1)[0]
            self.assertIn("A departure scheduled 2026-10-01 08:08", visible_trip)
            self.assertIn("D arrival scheduled 2026-10-01 14:30", visible_trip)
            self.assertIn("Transfer at B: scheduled 9 min; minimum 5 min", visible_trip)
            self.assertIn("Transfer at C: scheduled 15 min; minimum 7 min", visible_trip)
            self.assertNotIn("reported", visible_trip)
            self.assertIn("RE 89", visible_trip)
            self.assertIn("ICE 1100", visible_trip)
            self.assertIn("RE 3", visible_trip)
            if filename == "test.html":
                self.assertLess(page.index('<ol class="itinerary">'), page.index('<table>'))


class TestAdapterIntegration(unittest.TestCase):
    def test_plan_resolution_and_change_capture(self):
        class API:
            def __init__(self):
                self.calls = []

            def get(self, *parts):
                self.calls.append(parts)
                if parts[0] == "station":
                    eva = {"A": "1", "B": "2", "C": "3"}[parts[1]]
                    return f'<stations><station name="{parts[1]}" eva="{eva}"/></stations>'.encode()
                if parts[0] == "fchg":
                    return (b'<timetable><s id="a-261001-2"><ar ct="2610010914"/>'
                            b'</s></timetable>' if parts[1] == "2" else b'<timetable/>')
                eva, day, hour = parts[1:]
                stops = {
                    ("1", "261001", "08"): '<s id="a-261001-1"><tl c="RE" n="89"/><dp pt="2610010808"/></s>',
                    ("2", "261001", "09"): '<s id="a-261001-2"><tl c="RE" n="89"/><ar pt="2610010910"/></s><s id="b-261001-1"><tl c="ICE" n="1100"/><dp pt="2610010919"/></s>',
                    ("3", "261001", "13"): '<s id="b-261001-2"><tl c="ICE" n="1100"/><ar pt="2610011300"/></s>',
                }
                return f'<timetable>{stops.get((eva, day, hour), "")}</timetable>'.encode()

        with tempfile.TemporaryDirectory() as temp:
            db = database(Path(temp) / "db.sqlite")
            api = API()
            plans = resolve_plans(db, api, CONFIG, DAY)
            self.assertEqual(len(plans), 2)
            self.assertEqual([row["destination_id"] for row in plans],
                             ["a-261001-2", "b-261001-2"])
            first_count = len(api.calls)
            resolve_plans(db, api, CONFIG, DAY)
            self.assertEqual(len(api.calls), first_count)
            observe(db, api, CONFIG, DAY, plans, datetime.now(TZ))
            self.assertEqual(db.execute("SELECT count(*) FROM observations").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT changed_time FROM observations WHERE leg_index=0 "
                                        "AND station_kind='destination'").fetchone()[0], "2610010914")
            self.assertEqual([call for call in api.calls if call[0] == "fchg"],
                             [("fchg", "1"), ("fchg", "2"), ("fchg", "3")])
            db.close()


if __name__ == "__main__":
    unittest.main()
