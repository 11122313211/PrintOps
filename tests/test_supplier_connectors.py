"""Supplier extension contract and failure-safety tests, all without a network."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from supplier_adapters import DEFAULT_FIELD_MAP, PLATFORMS, SupplierAdapter, get_adapter, list_platforms
from supplier_contract import HumanConfirmation, RequestContext, TransportPolicy, UnknownProviderError, order_fingerprint
from supplier_connectors import (ConnectorRegistry, ExportConnector, GuardedTransportConnector,
                                 MemoryRequestJournal, MockConnector, REGISTRY, SQLiteRequestJournal)
from tools import match_supplier_capability, prepare_handoff, request_supplier_quote

ORDER = {"productType": "名片", "quantity": "500 张", "quantityValue": 500, "size": "9×5.4cm",
         "paper": "300g 白卡", "printing": "双面四色", "deadline": "下周", "platform": "generic"}


class FakeTransport:
    def __init__(self, replies=None):
        self.replies = list(replies or [{"status": "submitted", "requestId": "external-1"}])
        self.calls = []

    def execute(self, operation, payload, *, timeout, idempotency_key, cancel_event):
        self.calls.append((operation, deepcopy(payload), timeout, idempotency_key))
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def transport_connector(transport, **kwargs):
    metadata = deepcopy(PLATFORMS["generic"])
    metadata.update(providerId="test_provider", name="测试供应商", mode=kwargs.pop("mode", "sandbox"),
                    connectionStatus="connected", networkEnabled=True,
                    capabilities=["quote_draft", "order_draft", "get_quote", "submit_order", "cancel"],
                    credentialsConfigured=True, healthVerified=True, contractVerified=True)
    adapter = SupplierAdapter("test_provider", "测试供应商", field_map={"*": DEFAULT_FIELD_MAP})
    return GuardedTransportConnector(adapter, metadata, transport, **kwargs)


def confirmation(order=ORDER, revision=2, **changes):
    now = datetime.now(timezone.utc)
    value = HumanConfirmation("test_provider", revision, order_fingerprint(order), "authenticated-human",
                              now.isoformat(), (now + timedelta(minutes=5)).isoformat())
    return replace(value, **changes)


class SupplierRegistryTest(unittest.TestCase):
    def test_bundled_connectors_never_open_network_and_unsupported_is_explicit(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
            for metadata in REGISTRY.list_metadata():
                connector = REGISTRY.get(metadata["providerId"])
                self.assertFalse(metadata["networkEnabled"])
                self.assertFalse(metadata["verified"])
                self.assertFalse(connector.create_quote_draft(ORDER)["sent"])
                self.assertFalse(connector.create_order_draft(ORDER)["sent"])
                self.assertEqual(connector.submit_order(ORDER)["status"], "unsupported")
                self.assertEqual(connector.health()["status"], "offline_ready")
        shengda = REGISTRY.get("shengda").metadata()
        self.assertEqual(shengda["mode"], "export_only")
        self.assertEqual(shengda["connectionStatus"], "unconfigured")
        self.assertEqual(shengda["supplierProfile"]["categories"], [])
        self.assertIsNone(shengda["supplierProfile"]["leadTime"])
        self.assertIsNone(shengda["updatedAt"])
        self.assertIsNone(shengda["expiresAt"])

    def test_unknown_provider_does_not_fall_back(self):
        for lookup in (get_adapter, REGISTRY.get):
            with self.assertRaises(UnknownProviderError):
                lookup("unknown")
        self.assertEqual(request_supplier_quote(ORDER, "unknown")["reason"], "unknown_provider")
        self.assertEqual(match_supplier_capability(ORDER, "unknown")["reason"], "unknown_provider")
        self.assertEqual(prepare_handoff({**ORDER, "platform": "unknown"})["reason"], "unknown_provider")

    def test_any_trusted_provider_can_be_registered_without_core_changes(self):
        registry = ConnectorRegistry()
        connector = transport_connector(FakeTransport())
        registry.register(connector)
        self.assertIs(registry.get("test_provider"), connector)
        self.assertEqual(registry.get("test_provider").create_quote_draft(ORDER)["mode"], "sandbox")
        with self.assertRaises(ValueError):
            registry.register(connector)
        with self.assertRaises(TypeError):
            registry.register("https://attacker.example/connector.py")
        self.assertIn("shengda", [value["id"] for value in list_platforms()])

    def test_registered_provider_is_reachable_through_http_and_mcp_boundaries(self):
        from agent import Agent, Memory
        import server
        from mcp_server import PrintOpsMCP, MCP_PROTOCOL_VERSION
        from supplier_adapters import ADAPTERS
        metadata = deepcopy(PLATFORMS["generic"])
        metadata.update(providerId="custom_contract_test", name="自定义测试连接器", displayName="自定义测试连接器")
        connector = ExportConnector(SupplierAdapter("custom_contract_test", "自定义测试连接器",
                                                     field_map={"*": DEFAULT_FIELD_MAP}), metadata)
        with patch.dict(REGISTRY._connectors), patch.dict(PLATFORMS), patch.dict(ADAPTERS), tempfile.TemporaryDirectory() as tmp:
            REGISTRY.register(connector)
            memory = Memory(Path(tmp) / "memory.sqlite3")
            agent = Agent(memory, "registry-session")
            response = server.dispatch_post("/api/tools/call", {
                "toolName": "match_supplier_capability", "payload": {"platformId": "custom_contract_test"}}, agent)
            self.assertEqual(response["toolResult"]["platformId"], "custom_contract_test")
            self.assertEqual(response["toolResult"]["status"], "review")
            self.assertIn("custom_contract_test", [p["id"] for p in server.dispatch_get("/api/platforms")["platforms"]])
            bridge = PrintOpsMCP(Path(tmp) / "memory.sqlite3", bound_session_id=agent.id, capabilities="L0,L1")
            bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": MCP_PROTOCOL_VERSION}})
            result = bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "match_supplier_capability", "arguments": {"sessionId": agent.id, "platformId": "custom_contract_test"}}})
            payload = result["result"]["structuredContent"]
            self.assertEqual(payload["toolResult"]["platformId"], "custom_contract_test")

    def test_unitless_dimensions_block_export_and_submit(self):
        ambiguous = {**ORDER, "size": "90×54"}
        self.assertEqual(REGISTRY.get("generic").create_quote_draft(ambiguous)["reason"], "invalid_measurement")
        self.assertEqual(prepare_handoff(ambiguous)["status"], "blocked")
        transport = FakeTransport()
        result = transport_connector(transport).submit_order(ambiguous, RequestContext(2, "invalid-unit"))
        self.assertEqual(result["reason"], "invalid_measurement")
        self.assertEqual(transport.calls, [])

    def test_unverified_profile_neither_claims_supported_nor_unsupported(self):
        result = match_supplier_capability({**ORDER, "paper": "PVC板"}, "shengda")
        self.assertEqual(result["status"], "review")
        self.assertFalse(result["verified"])
        self.assertEqual(result["supported"], [])
        self.assertEqual(result["unsupported"], [])
        self.assertTrue(result["needsReview"])
        self.assertFalse(request_supplier_quote({**ORDER, "paper": "PVC板"}, "shengda")["sent"])

    def test_expired_verified_profile_does_not_claim_current_capability(self):
        connector = REGISTRY.get("generic")
        metadata = connector.metadata()
        metadata.update(verified=True, expiresAt="2000-01-01T00:00:00+00:00",
                        supplierProfile={"categories": ["名片"], "papers": ["白卡"], "maxSize": "1200×900 mm"})
        with patch.object(connector, "metadata", return_value=metadata):
            result = match_supplier_capability(ORDER)
        self.assertEqual(result["status"], "review")
        self.assertFalse(result["verified"])
        self.assertEqual(result["supported"], [])

    def test_verified_explicit_incompatibility_still_blocks_handoff(self):
        connector = REGISTRY.get("generic")
        metadata = connector.metadata()
        metadata.update(verified=True, supplierProfile={"categories": ["画册"], "papers": ["铜版纸"],
                                                        "maxSize": "1200×900 mm"})
        with patch.object(connector, "metadata", return_value=metadata):
            handoff = prepare_handoff(ORDER)
        self.assertEqual(handoff["status"], "blocked")
        self.assertEqual(handoff["reason"], "supplier_unsupported")
        self.assertFalse(handoff["mappedOrder"])

    def test_mapping_normalizes_nested_and_structured_dimensions_without_losing_semantics(self):
        order = {**ORDER, "productType": "包装盒",
                 "productSpecs": {"boxSize": "60×40×20cm", "boxSizeInner": "59×39×19cm", "bleed": "0.3cm"},
                 "dimensions": {"packageInnerSize": "59×39×19cm", "expandedSize": "80×60cm"},
                 "measurements": {"packageOuterSize": {"width": 60, "height": 40, "depth": 20, "unit": "cm"}}}
        result = get_adapter("shengda").map_order(order)
        self.assertEqual(result["boxDimension"], "600×400×200 mm")
        self.assertEqual(result["productSpecs"]["bleed"], "3 mm")
        self.assertEqual(result["dimensions"]["packageInnerSize"], "590×390×190 mm")
        self.assertEqual(result["measurements"]["packageInnerSize"]["semantic"], "package_inner")
        self.assertEqual(result["measurements"]["packageOuterSize"]["width"], 600)
        self.assertEqual(result["measurements"]["packageOuterSize"]["unit"], "mm")
        self.assertEqual(order["productSpecs"]["boxSize"], "60×40×20cm")

    def test_mock_quote_status_and_cancel_are_explicitly_mock(self):
        mock = MockConnector()
        draft = mock.create_quote_draft(ORDER)
        self.assertTrue(mock.get_quote(draft["requestId"])["mock"])
        self.assertEqual(mock.cancel(draft["requestId"])["status"], "cancelled")
        self.assertTrue(mock.cancel(draft["requestId"])["idempotent"])
        self.assertEqual(mock.get_quote("missing")["status"], "not_found")


class SupplierTransportTest(unittest.TestCase):
    def test_read_retries_are_bounded_and_share_deadline(self):
        transport = FakeTransport([TimeoutError(), {"status": "received", "requestId": "quote-1"}])
        journal = MemoryRequestJournal()
        connector = transport_connector(transport, journal=journal,
                                        policy=TransportPolicy(timeout_seconds=.2, deadline_seconds=.5, max_attempts=2))
        result = connector.get_quote("quote-1")
        self.assertEqual(result["status"], "received")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(all(call[2] <= .2 for call in transport.calls))
        self.assertEqual([e["event"] for e in journal.events], ["started", "finished"])

    def test_read_retry_exhaustion_does_not_continue_forever(self):
        transport = FakeTransport([TimeoutError(), TimeoutError(), TimeoutError()])
        connector = transport_connector(transport, policy=TransportPolicy(max_attempts=2))
        self.assertEqual(connector.get_quote("quote-1")["status"], "timeout")
        self.assertEqual(len(transport.calls), 2)

    def test_submit_timeout_is_unknown_not_retried_even_after_restart(self):
        transport = FakeTransport([TimeoutError()])
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "supplier.sqlite3")
            connector = transport_connector(transport, journal=SQLiteRequestJournal(path))
            context = RequestContext(revision=2, idempotency_key="stable-1")
            result = connector.submit_order(ORDER, context)
            self.assertEqual(result["status"], "unknown")
            self.assertTrue(result["requiresReconciliation"])
            restored = transport_connector(transport, journal=SQLiteRequestJournal(path))
            repeated = restored.submit_order(ORDER, context)
            self.assertEqual(repeated["status"], "unknown")
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(len(transport.calls), 1)

    def test_same_key_for_different_order_or_revision_is_blocked(self):
        transport = FakeTransport()
        connector = transport_connector(transport)
        context = RequestContext(revision=2, idempotency_key="stable-1")
        connector.submit_order(ORDER, context)
        self.assertEqual(connector.submit_order({**ORDER, "quantity": "600 张"}, context)["reason"], "idempotency_conflict")
        self.assertEqual(connector.submit_order(ORDER, replace(context, revision=3))["reason"], "idempotency_conflict")
        self.assertEqual(len(transport.calls), 1)

    def test_live_requires_enablement_verified_setup_durable_storage_and_bound_confirmation(self):
        transport = FakeTransport()
        context = RequestContext(revision=2, idempotency_key="stable-1", confirmation=confirmation())
        disabled = transport_connector(transport, mode="live")
        self.assertEqual(disabled.submit_order(ORDER, context)["reason"], "live_disabled")
        transient = transport_connector(transport, mode="live", live_enabled=True)
        self.assertEqual(transient.submit_order(ORDER, context)["reason"], "durable_journal_required")
        with tempfile.TemporaryDirectory() as tmp:
            connector = transport_connector(transport, mode="live", live_enabled=True,
                                            journal=SQLiteRequestJournal(str(Path(tmp) / "supplier.sqlite3")))
            for invalid in (None, confirmation(revision=1), confirmation({**ORDER, "quantity": "1 张"}),
                            confirmation(provider_id="other"), confirmation(expires_at="2000-01-01T00:00:00+00:00")):
                result = connector.submit_order(ORDER, replace(context, confirmation=invalid))
                self.assertEqual(result["reason"], "human_confirmation_required")
            self.assertEqual(transport.calls, [])
            self.assertEqual(connector.submit_order(ORDER, context)["status"], "submitted")
            self.assertEqual(transport.calls[0][1]["order"]["size"], "90×54 mm")

    def test_cancellation_targets_known_submission_and_is_idempotent(self):
        transport = FakeTransport([{"status": "submitted", "requestId": "external-1"},
                                   {"status": "cancelled", "requestId": "external-1"}])
        connector = transport_connector(transport)
        connector.submit_order(ORDER, RequestContext(2, "submit-1"))
        context = RequestContext(2, "cancel-1")
        self.assertEqual(connector.cancel("external-1", context)["status"], "cancelled")
        self.assertTrue(connector.cancel("external-1", context)["idempotent"])
        self.assertEqual(connector.cancel("missing", context)["reason"], "unknown_request")
        self.assertEqual(connector.cancel("external-1", replace(context, revision=3))["reason"], "stale_revision")
        self.assertEqual(len(transport.calls), 2)

    def test_transport_ignoring_timeout_is_still_bounded(self):
        release = Event()
        class HangingTransport:
            def execute(self, *args, **kwargs):
                release.wait(1)
                return {"status": "submitted", "requestId": "late"}
        connector = transport_connector(HangingTransport(), policy=TransportPolicy(.03, .05, 2, 1))
        started = time.monotonic()
        result = connector.submit_order(ORDER, RequestContext(2, "slow"))
        elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "unknown")
        self.assertLess(elapsed, .3)
        self.assertEqual(connector.submit_order(ORDER, RequestContext(2, "second"))["status"], "busy")
        release.set()

    def test_cancel_wait_returns_unknown_and_never_claims_remote_rollback(self):
        started, release = Event(), Event()
        class HangingTransport:
            def execute(self, *args, **kwargs):
                started.set()
                release.wait(1)
                return {"status": "submitted"}
        connector = transport_connector(HangingTransport(), policy=TransportPolicy(.5, 1, 1, 1))
        outcomes = []
        worker = Thread(target=lambda: outcomes.append(connector.submit_order(ORDER, RequestContext(2, "cancel-me"))))
        worker.start()
        self.assertTrue(started.wait(.2))
        self.assertTrue(connector.cancel_wait("cancel-me"))
        worker.join(.3)
        release.set()
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes[0]["status"], "unknown")
        self.assertEqual(outcomes[0]["reason"], "wait_cancelled")

    def test_audit_failure_blocks_dispatch(self):
        class BrokenJournal(MemoryRequestJournal):
            def append_audit(self, event):
                raise OSError("disk full")
        transport = FakeTransport()
        connector = transport_connector(transport, journal=BrokenJournal())
        self.assertEqual(connector.submit_order(ORDER, RequestContext(2, "stable"))["reason"], "audit_unavailable")
        self.assertEqual(transport.calls, [])

    def test_unfinished_durable_claim_is_never_resent(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = SQLiteRequestJournal(str(Path(tmp) / "supplier.sqlite3"))
            journal.claim("test_provider:submit_order:crashed", order_fingerprint(ORDER),
                          {"operation": "submit_order", "revision": 2, "requestId": None})
            transport = FakeTransport()
            result = transport_connector(transport, journal=journal).submit_order(ORDER, RequestContext(2, "crashed"))
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
