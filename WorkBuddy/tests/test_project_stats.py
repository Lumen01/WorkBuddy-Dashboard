import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "/example/" + "long-directory/" * 12
PROJECT_A = PREFIX + "team-a/shared"
PROJECT_B = PREFIX + "team-b/shared"
DAY = "2026-11-16"


def event_time(day, time):
    return datetime.fromisoformat(day + "T" + time).astimezone(timezone.utc)


def generation(trace_id, span_id, when, *usage):
    return {"type": "span_end", "span": {
        "traceId": trace_id, "spanId": span_id, "type": "generation",
        "startedAt": when.isoformat() if when else None,
        "toolOutput": json.dumps([
            {"model": model, "usage": tokens} for model, tokens in usage
        ]),
    }}


def tool(trace_id, when, kind="function"):
    return {"type": "span_end", "span": {
        "traceId": trace_id, "name": "Read", "type": kind, "startedAt": when.isoformat(),
    }}


def trace(trace_id, session_id, kind="trace_update"):
    return {"type": kind, "trace": {"traceId": trace_id, "sessionId": session_id}}


class ProjectStatsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "workbuddy.db"
        self.traces = Path(tmp.name) / "traces"
        self.traces.mkdir()
        env = patch.dict(os.environ, WORKBUDDY_DEMO="0", WORKBUDDY_CN_DB=str(self.db_path),
                         WORKBUDDY_CN_TRACES=str(self.traces))
        env.start()
        self.addCleanup(env.stop)

        before = event_time("2026-11-15", "23:59:59")
        first = event_time(DAY, "00:00:00")
        last = event_time(DAY, "23:59:59.999")
        after = event_time("2026-11-17", "00:00:00")
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.executescript("""
                CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, custom_title TEXT,
                    model TEXT, mode TEXT, status TEXT, created_at INTEGER, updated_at INTEGER,
                    last_activity_at INTEGER, is_background_automation INTEGER, cwd TEXT, deleted_at INTEGER);
                CREATE TABLE session_usage(session_id TEXT, used INTEGER, size INTEGER,
                    updated_at INTEGER, credit_json TEXT);
                CREATE TABLE workspaces(path TEXT, last_opened_at INTEGER);
            """)
            for sid, path, created, activity, background, deleted in (
                ("prior", PROJECT_A, before, last, 1, 0),
                ("first", PROJECT_A, first, first, 0, 0),
                ("last", PROJECT_B, last, after, 1, 0),
                ("blank", "", first, first, 0, 0),
                ("relative", "relative/path", last, last, 0, None),
                ("deleted", PROJECT_A, first, first, 0, 1),
                ("later", r"C:\example\shared", after, after, 0, 0),
            ):
                db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                    sid, sid, None, "registered-model", "craft", "completed", int(created.timestamp() * 1000),
                    int(activity.timestamp() * 1000), int(activity.timestamp() * 1000), background, path, deleted,
                ))
            db.executemany("INSERT INTO session_usage VALUES(?,?,?,?,?)", [
                ("prior", 20, 50, 1, '{"registered-model": 4.25}'),
                ("first", 99, 10, 1, '{"old": 99}'),
                ("first", 120, 100, 2, '{"registered-model": 0}'),
                ("last", 0, 0, 1, None),
                ("deleted", 10, 20, 1, '{"registered-model": 125}'),
                ("blank", 0, 50, 1, '{}'),
            ])
            db.executemany("INSERT INTO workspaces VALUES(?,?)",
                           [(PROJECT_A, 0), (PROJECT_B + "/", 1)]
                           + [("/workspaces/%d" % i, i + 2) for i in range(55)])

        meter = ("actual", {"total_tokens": 50, "prompt_tokens": 25, "completion_tokens": 20,
                            "prompt_tokens_details": {"cached_tokens": 5},
                            "completion_tokens_details": {"reasoning_tokens": 3}})
        meter_two = ("actual", {"total_tokens": 72, "prompt_tokens": 50, "completion_tokens": 22,
                                "prompt_tokens_details": {"cached_tokens": 20}})
        meter_three = ("second", {"prompt_tokens": 2, "completion_tokens": 1})
        zero = ("actual", {"total_tokens": 0})
        dup = generation("a", "inside-first", first, meter)
        tool_start = dict(tool("a", first), type="span_start")
        files = {
            "events.jsonl": [
                generation("archive", "archive", event_time("2024-01-01", "00:00:00"), meter_three),
                generation("a", "outside-before", before, meter), dup, dup,
                generation("a2", "inside-last", last, meter_two, meter_three),
                generation("a2", "outside-after", after, meter),
                generation("b", "b", last, meter_three),
                generation("missing", "missing", first, meter_three),
                generation("deleted", "deleted", first, meter_three),
                generation("bad", "bad", first, zero),
                generation("unknown", "unknown", None, meter_three),
                tool_start, tool("a", first), tool("b-tools", last),
                tool("a", first, kind="custom"), tool("bad", first), tool("a", after),
                trace("a2", "prior", "trace_end"), trace("b", "last", "trace_end"),
            ],
            "late-associations.jsonl": [
                trace("a", "prior"), trace("missing", "relative"),
                trace("deleted", "deleted"), trace("bad", "first "),
                trace("b-tools", "last", "trace_start"),
            ],
        }
        for filename, events in files.items():
            (self.traces / filename).write_text(
                "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    def test_project_stats_both_launch_formats(self):
        snapshots = []
        for script in (ROOT / "dashboard.py", ROOT / "workbuddy-skill/scripts/dashboard.py"):
            with self.subTest(script=script):
                spec = importlib.util.spec_from_file_location("dashboard_under_test", script)
                app = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(app)
                all_time = app.compute_stats("cn")
                day = app.compute_stats("cn", DAY, DAY)
                self.check_projects(all_time, day)
                self.assert_partition(all_time)
                self.assert_partition(day)
                self.assert_partition(app.compute_stats("cn", start=DAY))
                self.assert_partition(app.compute_stats("cn", end=DAY))
                empty = app.compute_stats("cn", "2026-10-01", "2026-10-01")
                self.assertEqual(empty["projects"], [])
                self.assertIsNone(empty["project_coverage"]["requests"]["percent"])
                snapshots.append({k: day[k] for k in ("projects", "project_usage", "project_coverage", "conversations")})
        self.assertEqual(snapshots[0], snapshots[1])

    def check_projects(self, all_time, day):
        projects = {p["path"]: p for p in day["projects"]}
        a, b, missing = (projects[p] for p in (PROJECT_A, PROJECT_B, None))
        self.assertEqual(len(day["conversations"]), 4)
        self.assertEqual({p["name"] for p in (a, b)}, {"shared"})
        self.assertTrue(a["workspace_registered"])
        self.assertFalse(b["workspace_registered"])
        self.assertEqual((a["sessions"], a["credits"], a["credit_sessions"]), (1, 0, 1))
        self.assertEqual((a["context_used"], a["context_size"], a["context_percent"]), (120, 100, 120))
        self.assertEqual((b["credits"], b["context_percent"], b["background_sessions"]), (None, None, 1))
        self.assertEqual(b["last_activity"], "2026-11-17 00:00")
        self.assertEqual(missing["sessions"], 2)
        self.assertEqual(a["session_models"], {"registered-model": 1})
        total_projects = {p["path"]: p for p in all_time["projects"]}
        self.assertEqual(total_projects[PROJECT_A]["credits"], 4.25)
        self.assertEqual(total_projects[PROJECT_A]["context_percent"], 93.33)
        self.assertEqual(total_projects[r"C:\example\shared"]["name"], "shared")

        usage = {p["path"]: p for p in day["project_usage"]}
        self.assertEqual((usage[PROJECT_A]["tokens"]["requests"], usage[PROJECT_A]["tokens"]["total"]), (3, 125))
        self.assertEqual((usage[PROJECT_A]["heatmap"][0][0], usage[PROJECT_A]["heatmap"][0][23]), (1, 2))
        self.assertEqual([m["name"] for m in usage[PROJECT_A]["models"]], ["actual", "second"])
        self.assertEqual((usage[PROJECT_B]["tokens"]["requests"], usage[PROJECT_B]["tool_calls"]), (1, 1))
        self.assertEqual((usage[None]["tokens"]["requests"], usage[None]["tokens"]["total"], usage[None]["tool_calls"]),
                         (3, 6, 1))
        self.assertEqual(day["tokens"]["input"], 83)
        self.assertEqual(day["tokens"]["reasoning"], 3)
        self.assertEqual(day["tokens"]["cache_hit_rate"], 30.12)
        self.assertEqual(day["project_coverage"]["requests"], {"attributed": 4, "total": 7, "percent": 57.14})
        self.assertEqual(day["project_coverage"]["tokens"]["attributed"], 128)
        self.assertEqual(day["project_coverage"]["tool_calls"]["attributed"], 2)
        self.assertEqual(all_time["tokens"]["requests"], 11)
        self.assertEqual(sum(row["requests"] for row in all_time["daily"]), 10)
        self.assertEqual(all_time["daily"][-1]["day"], "2026-11-17")

    def assert_partition(self, stats):
        projects = stats["project_usage"]
        for key in ("requests", "total", "input", "output", "cached", "reasoning"):
            self.assertEqual(sum(p["tokens"][key] for p in projects), stats["tokens"][key])
        self.assertEqual(sum(p["tool_calls"] for p in projects), stats["counts"]["tool_calls"])
        for day in range(7):
            for hour in range(24):
                self.assertEqual(sum(p["heatmap"][day][hour] for p in projects), stats["heatmap"][day][hour])


if __name__ == "__main__":
    unittest.main()
