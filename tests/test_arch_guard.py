"""Architecture guard tests: acyclic runtime graph, layering rules, and SSOT checks."""

from __future__ import annotations

from pathlib import Path
import unittest

from scripts import arch_guard


ROOT = Path(__file__).resolve().parents[1]


class ArchitectureGuardTest(unittest.TestCase):
    def test_runtime_import_graph_has_zero_cycles(self):
        graph, _ = arch_guard.build_import_graph(ROOT)
        self.assertGreaterEqual(len(graph), 20)
        self.assertEqual(arch_guard.find_cycles(graph), [])

    def test_runtime_import_graph_has_zero_forbidden_layer_edges(self):
        _, raw_imports = arch_guard.build_import_graph(ROOT)
        self.assertEqual(arch_guard.find_layer_violations(raw_imports), [])

    def test_version_matches_top_level_docs(self):
        self.assertEqual(arch_guard.check_version_consistency(ROOT), [])

    def test_frontend_js_syntax_passes_when_node_present(self):
        errors, _ = arch_guard.check_js_syntax(ROOT)
        self.assertEqual(errors, [])

    def test_no_root_module_directory_collisions(self):
        self.assertEqual(arch_guard.check_namespace_collisions(ROOT), [])

    def test_frontend_esm_imports_resolve_cleanly(self):
        self.assertEqual(arch_guard.check_esm_imports(ROOT), [])

    def test_find_cycles_detects_synthetic_cycle(self):
        cycles = arch_guard.find_cycles({"a": {"b"}, "b": {"c"}, "c": {"a"}})
        self.assertEqual(cycles, [["a", "b", "c", "a"]])


if __name__ == "__main__":
    unittest.main()
