"""Third-party mock integration with the public connector/registry contract."""
from pathlib import Path
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from examples.supplier_mock import ExampleSupplierConnector, demo, register_example
from supplier_connectors import ConnectorRegistry, REGISTRY
from supplier_contract import RequestContext, SupplierConnector, UnknownProviderError


class SupplierExampleTest(unittest.TestCase):
    def test_registration_mapping_and_lifecycle_without_network(self):
        with patch("socket.socket", side_effect=AssertionError("mock must not open sockets")):
            result = demo()
        self.assertEqual(result["metadata"]["mode"], "sandbox")
        self.assertFalse(result["metadata"]["networkEnabled"])
        quote = result["quoteDraft"]
        self.assertEqual(quote["mappedOrder"]["quantity"], 500)
        self.assertEqual(quote["mappedOrder"]["finished_size"]["width"], 90)
        self.assertEqual(quote["mappedOrder"]["unit_system"], "mm")
        self.assertEqual(quote["orderRevision"], 4)
        self.assertTrue(quote["mock"])
        self.assertFalse(quote["sent"])
        self.assertEqual(result["retrieved"], quote)
        self.assertEqual(result["cancelled"]["status"], "cancelled")
        self.assertTrue(result["repeatCancel"]["idempotent"])
        self.assertEqual(result["submit"]["status"], "unsupported")

    def test_extension_is_registered_explicitly_and_does_not_change_shengda(self):
        before = REGISTRY.get("shengda").metadata()
        registry = ConnectorRegistry()
        connector = register_example(registry)
        self.assertIsInstance(connector, SupplierConnector)
        self.assertIs(registry.get("example_local_press"), connector)
        with self.assertRaises(ValueError):
            register_example(registry)
        with self.assertRaises(UnknownProviderError):
            registry.get("unregistered")
        self.assertEqual(REGISTRY.get("shengda").metadata(), before)
        self.assertEqual(before["connectionStatus"], "unconfigured")

    def test_order_draft_lookup_is_detached_and_missing_units_block(self):
        connector = ExampleSupplierConnector()
        draft = connector.create_order_draft({"productType": "名片", "size": "9×5.4cm"}, RequestContext(revision=8))
        self.assertEqual(draft["mappedOrder"]["finished_size_text"], "90×54 mm")
        self.assertEqual(connector.get_quote(draft["requestId"])["type"], "order_draft")
        draft["mappedOrder"]["finished_size_text"] = "corrupt caller value"
        draft["source"]["kind"] = "corrupt caller provenance"
        self.assertEqual(connector.get_quote(draft["requestId"])["mappedOrder"]["finished_size_text"], "90×54 mm")
        self.assertEqual(connector.metadata()["source"]["kind"], "mock")
        self.assertEqual(connector.create_quote_draft({"size": "90×54"})["status"], "blocked")
        self.assertEqual(connector.get_quote("absent")["status"], "not_found")

    def test_example_runs_as_documented(self):
        script = Path(__file__).resolve().parents[1] / "examples" / "supplier_mock.py"
        completed = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                             encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["synthetic"])


if __name__ == "__main__":
    unittest.main()
