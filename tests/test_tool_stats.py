"""Gateway tool-call telemetry (v1.3.0): coverage for recording, resilience,
aggregation boundaries and the bounded memory ring. Synthetic data only."""
from pathlib import Path
import tempfile
import unittest

from agent import Agent, Memory
from storage_contract import TOOL_CALL_LOG_LIMIT
from storage_memory import InMemoryOrderStore
from storage_sqlite import SQLiteOrderStore


class ExplodingStatsStore(InMemoryOrderStore):
    """Telemetry backend failure simulator: every stats write raises."""

    def record_tool_call(self, entry):
        raise RuntimeError("synthetic stats backend down")


class NoTelemetryStore(InMemoryOrderStore):
    """Store built before v1.3.0: the optional capability is absent."""

    record_tool_call = None
    load_tool_stats = None


def fresh_agent(store, session_id="stats-check"):
    return Agent(Memory(store=store), session_id)


class GatewayRecordingTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryOrderStore()

    def test_rejected_and_blocked_calls_leave_gateway_records(self):
        agent = fresh_agent(self.store)
        rejected = agent.call_tool("disallowed_tool")
        self.assertIn("不在白名单中", rejected["messages"][0])
        blocked = agent.call_tool("request_supplier_quote", {})
        self.assertEqual(blocked["toolResult"]["status"], "blocked")
        rows = list(self.store._tool_calls)
        self.assertEqual([row["status"] for row in rows], ["rejected", "blocked"])
        self.assertEqual(rows[0]["tool_name"], "disallowed_tool")
        self.assertIn("白名单", rows[0]["reason"])
        self.assertEqual(rows[1]["tool_name"], "request_supplier_quote")
        self.assertEqual(rows[1]["reason"], "order_not_ready")
        self.assertTrue(all(isinstance(row["duration_ms"], int) and row["duration_ms"] >= 0 for row in rows))
        self.assertTrue(all(row["session_id"] == "stats-check" for row in rows))
        self.assertTrue(all(row["run_id"] for row in rows))
        stats = self.store.load_tool_stats(30)
        self.assertEqual(stats["totals"]["calls"], 2)
        self.assertEqual(stats["totals"]["rejected"], 1)
        self.assertEqual(stats["totals"]["blocked"], 1)
        self.assertEqual(stats["blockedReasons"], [{"reason": "order_not_ready", "count": 1}])

    def test_run_trace_terminal_events_carry_duration_ms(self):
        agent = fresh_agent(self.store)
        rejected = agent.call_tool("disallowed_tool")
        blocked = agent.call_tool("request_supplier_quote", {})
        for response, status in ((rejected, "rejected"), (blocked, "blocked")):
            terminal = [event for event in response["runTrace"]
                        if event.get("step") == "tool" and event.get("status") == status]
            self.assertEqual(len(terminal), 1, status)
            self.assertIsInstance(terminal[0].get("durationMs"), int)
            self.assertGreaterEqual(terminal[0]["durationMs"], 0)

    def test_successful_calls_record_ok_status(self):
        agent = fresh_agent(self.store)
        # validate_order takes no public arguments beyond the stripped legacy
        # envelope; a bare payload on a draft order still terminates as ok.
        agent.call_tool("validate_order", {})
        row = self.store._tool_calls[-1]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["tool_name"], "validate_order")
        self.assertEqual(row["reason"], "")

    def test_item_index_is_captured_from_the_call_payload(self):
        agent = fresh_agent(self.store)
        agent.call_tool("estimate_price", {"itemIndex": 0})
        row = self.store._tool_calls[-1]
        self.assertEqual(row["tool_name"], "estimate_price")
        self.assertEqual(row["item_index"], 0)

    def test_sqlite_backend_persists_gateway_records(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteOrderStore(Path(directory) / "stats.sqlite3")
            agent = fresh_agent(store)
            agent.call_tool("request_supplier_quote", {})
            agent.call_tool("nope")
            reopened = SQLiteOrderStore(Path(directory) / "stats.sqlite3")
            stats = reopened.load_tool_stats(30)
            self.assertEqual(stats["totals"]["calls"], 2)
            self.assertEqual(stats["totals"]["blocked"], 1)
            self.assertEqual(stats["totals"]["rejected"], 1)


class ResilienceTest(unittest.TestCase):
    def test_stats_backend_failure_never_breaks_chat(self):
        store = ExplodingStatsStore()
        agent = fresh_agent(store, "resilient")
        try:
            response = agent.chat("你好")
        except Exception as error:  # pragma: no cover - failure path
            self.fail(f"chat must survive telemetry failure, got {error!r}")
        self.assertTrue(response["messages"])
        self.assertEqual(store.load_order("resilient")["state"]["messages"][0]["role"], "user")

    def test_stats_backend_failure_never_breaks_tool_gateway(self):
        store = ExplodingStatsStore()
        agent = fresh_agent(store)
        response = agent.call_tool("request_supplier_quote", {})
        self.assertEqual(response["toolResult"]["status"], "blocked")

    def test_store_without_telemetry_capability_is_skipped(self):
        agent = fresh_agent(NoTelemetryStore())
        response = agent.call_tool("request_supplier_quote", {})
        self.assertEqual(response["toolResult"]["status"], "blocked")
        self.assertTrue(response["messages"])


class AggregationBoundaryTest(unittest.TestCase):
    def test_p95_nearest_rank_and_average_boundaries(self):
        # rank = ceil(0.95 * n) over the sorted sample; each scenario starts
        # from a fresh store so the samples are exact.
        scenarios = [
            ([100], 100.0, 100.0),            # single sample: p95 is that sample
            ([100, 200], 200.0, 150.0),       # rank ceil(1.9) = 2 -> max of the pair
            ([100] * 18 + [500], 500.0, 121.05),   # rank ceil(18.05) = 19 -> the outlier
            ([100] * 19 + [500], 100.0, 120.0),    # rank ceil(19) = 19 -> outlier above p95
        ]
        with tempfile.TemporaryDirectory() as directory:
            for durations, expected_p95, expected_avg in scenarios:
                stores = [InMemoryOrderStore(),
                          SQLiteOrderStore(Path(directory) / f"bounds-{len(durations)}.sqlite3")]
                for store in stores:
                    with self.subTest(mode=store.metadata()["mode"], samples=len(durations)):
                        for duration in durations:
                            store.record_tool_call({"tool_name": "t", "status": "ok", "duration_ms": duration})
                        stats = store.load_tool_stats(30)
                        self.assertEqual(stats["totals"]["calls"], len(durations))
                        self.assertEqual(stats["totals"]["p95Ms"], expected_p95)
                        self.assertEqual(stats["totals"]["avgMs"], expected_avg)

    def test_empty_window_reports_zeroed_structure(self):
        stats = InMemoryOrderStore().load_tool_stats(7)
        self.assertEqual(stats["windowDays"], 7)
        self.assertEqual(stats["scope"], "local-only")
        self.assertEqual(stats["totals"], {"ok": 0, "failed": 0, "blocked": 0, "rejected": 0,
                                           "calls": 0, "successRate": 0, "avgMs": None, "p95Ms": None})
        self.assertEqual(stats["tools"], [])
        self.assertEqual(stats["blockedReasons"], [])

    def test_memory_ring_is_bounded_and_keeps_newest_entries(self):
        store = InMemoryOrderStore()
        for index in range(TOOL_CALL_LOG_LIMIT + 5):
            store.record_tool_call({"tool_name": f"tool-{index}", "status": "ok", "duration_ms": index})
        stats = store.load_tool_stats(30)
        self.assertEqual(stats["totals"]["calls"], TOOL_CALL_LOG_LIMIT)
        self.assertEqual(len(store._tool_calls), TOOL_CALL_LOG_LIMIT)
        names = {tool["tool"] for tool in stats["tools"]}
        self.assertNotIn("tool-0", names)
        self.assertIn(f"tool-{TOOL_CALL_LOG_LIMIT + 4}", names)

    def test_recorded_entries_are_detached_copies(self):
        store = InMemoryOrderStore()
        entry = {"tool_name": "estimate_price", "status": "ok", "reason": "", "duration_ms": 5,
                 "session_id": "s", "run_id": "r", "item_index": 1}
        store.record_tool_call(entry)
        entry["status"] = "tampered outside"
        self.assertEqual(store._tool_calls[0]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
