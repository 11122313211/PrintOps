#!/usr/bin/env python3
"""Architecture & SSOT guard for PrintOps (standard library only).

Checks four structural invariants without importing or executing runtime code:
1. Zero circular imports across runtime Python modules (root and ``printops/``);
2. Forbidden reverse cross-layer edges remain absent (including function-local imports);
3. ``VERSION`` stays aligned with ``README.md``, ``README.en.md`` and ``docs/RELEASE_CHECKLIST.md``;
4. Frontend JavaScript files pass ``node --check`` when ``node`` is installed.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

# (source_suffix_or_name, forbidden_target_prefixes)
FORBIDDEN_EDGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("product_knowledge", ("pricing_engine", "printops.pricing")),
    ("printops.domain.knowledge", ("pricing_engine", "printops.pricing")),
    ("storage_sqlite", ("order_model", "printops.domain.order_model")),
    ("printops.storage.sqlite", ("order_model", "printops.domain.order_model")),
    ("order_contract", ("tools", "printops.tools")),
    ("printops.domain.order_contract", ("tools", "printops.tools")),
    ("supplier_adapters", ("supplier_connectors", "printops.supplier.connectors")),
    ("printops.supplier.adapters", ("supplier_connectors", "printops.supplier.connectors")),
)


def discover_runtime_modules(root: Path = ROOT) -> dict[str, Path]:
    """Collect runtime Python modules at repository root and under ``printops/``."""
    modules: dict[str, Path] = {}
    for path in sorted(root.glob("*.py")):
        modules[path.stem] = path
    pkg_root = root / "printops"
    if pkg_root.is_dir():
        for path in sorted(pkg_root.rglob("*.py")):
            rel = path.relative_to(root).with_suffix("")
            parts = list(rel.parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            mod_name = ".".join(parts)
            modules[mod_name] = path
    return modules


def _resolve_relative(module_name: str, is_package: bool, level: int, target: str | None) -> str:
    parts = module_name.split(".")
    base = parts if is_package else parts[:-1]
    drop = max(level - 1, 0)
    prefix = base[: len(base) - drop] if drop <= len(base) else []
    if target:
        prefix.extend(target.split("."))
    return ".".join(p for p in prefix if p)


def build_import_graph(root: Path = ROOT) -> tuple[dict[str, set[str]], dict[str, list[tuple[int, str]]]]:
    """Parse ASTs and return (internal_graph, raw_imports_by_module)."""
    modules = discover_runtime_modules(root)
    known = set(modules)
    graph: dict[str, set[str]] = {mod: set() for mod in modules}
    raw_imports: dict[str, list[tuple[int, str]]] = defaultdict(list)

    for mod, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        is_pkg = path.name == "__init__.py"
        for node in ast.walk(tree):
            candidates: list[tuple[int, str]] = []
            if isinstance(node, ast.Import):
                for alias in node.names:
                    candidates.append((node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    resolved = _resolve_relative(mod, is_pkg, node.level, node.module)
                    if resolved:
                        candidates.append((node.lineno, resolved))
                    for alias in node.names:
                        if alias.name != "*":
                            sub = f"{resolved}.{alias.name}" if resolved else alias.name
                            candidates.append((node.lineno, sub))
                elif node.module:
                    candidates.append((node.lineno, node.module))
                    for alias in node.names:
                        if alias.name != "*":
                            candidates.append((node.lineno, f"{node.module}.{alias.name}"))
            for lineno, target in candidates:
                raw_imports[mod].append((lineno, target))
                if target in known and target != mod:
                    graph[mod].add(target)
                elif "." in target:
                    # Match nearest enclosing known module when importing a symbol from a module.
                    parts = target.split(".")
                    for idx in range(len(parts) - 1, 0, -1):
                        prefix = ".".join(parts[:idx])
                        if prefix in known and prefix != mod:
                            graph[mod].add(prefix)
                            break
    return graph, raw_imports


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Find directed import cycles deterministically."""
    color = {node: 0 for node in graph}
    path: list[str] = []
    cycles: list[list[str]] = []

    def dfs(u: str) -> None:
        color[u] = 1
        path.append(u)
        for v in sorted(graph.get(u, ())):
            if color.get(v) == 1:
                idx = path.index(v)
                cycles.append(path[idx:] + [v])
            elif color.get(v) == 0:
                dfs(v)
        path.pop()
        color[u] = 2

    for node in sorted(graph):
        if color[node] == 0:
            dfs(node)
    return cycles


def find_layer_violations(raw_imports: dict[str, list[tuple[int, str]]]) -> list[str]:
    """Detect forbidden cross-layer import edges."""
    violations: list[str] = []
    for mod, forbidden_targets in FORBIDDEN_EDGES:
        for lineno, target in raw_imports.get(mod, []):
            for prefix in forbidden_targets:
                if target == prefix or target.startswith(prefix + "."):
                    violations.append(f"{mod} (L{lineno}) -> {target}")
                    break
    return sorted(set(violations))


def check_version_consistency(root: Path = ROOT) -> list[str]:
    """Verify VERSION matches top-level documentation declarations."""
    errors: list[str] = []
    version_path = root / "VERSION"
    if not version_path.is_file():
        return ["缺少 VERSION 文件"]
    version = version_path.read_text(encoding="utf-8").strip()
    checks = (
        ("README.md", f"version-{version}-"),
        ("README.en.md", f"version-{version}-"),
        ("docs/RELEASE_CHECKLIST.md", f"`{version}`"),
    )
    for rel_path, expected in checks:
        doc_path = root / rel_path
        if not doc_path.is_file():
            errors.append(f"缺少文档文件：{rel_path}")
            continue
        head = "\n".join(doc_path.read_text(encoding="utf-8").splitlines()[:40])
        if expected not in head:
            errors.append(f"{rel_path} 顶部未与 VERSION ({version}) 对齐（缺少 {expected}）")
    return errors


def check_js_syntax(root: Path = ROOT) -> tuple[list[str], int]:
    """Run ``node --check`` on frontend JS files when Node.js is available."""
    node = shutil.which("node")
    if not node:
        return [], 0
    js_files = sorted(root.glob("*.js"))
    web_dir = root / "web"
    if web_dir.is_dir():
        js_files.extend(sorted(web_dir.rglob("*.js")))
    errors: list[str] = []
    for path in js_files:
        proc = subprocess.run(
            [node, "--check", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()[:3]
            errors.append(f"{path.relative_to(root)}: {' | '.join(detail)}")
    return errors, len(js_files)


def check_esm_imports(root: Path = ROOT) -> list[str]:
    """Verify relative ES module import paths and named exports under ``web/js/``."""
    web_js = root / "web" / "js"
    if not web_js.is_dir():
        return []
    exports_map: dict[Path, set[str]] = {}
    for path in sorted(web_js.rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        exports_map[path.resolve()] = set(
            re.findall(r"^export\s+(?:async\s+)?(?:function|const|let|var)\s+([A-Za-z0-9_$]+)", text, re.M)
        )
    errors: list[str] = []
    for path in sorted(web_js.rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'import\s*\{([^}]+)\}\s*from\s*"([^"]+)"', text):
            names = [item.strip().split(" as ")[0].strip() for item in match.group(1).split(",") if item.strip()]
            target = (path.parent / match.group(2)).resolve()
            if not target.is_file():
                errors.append(f"前端 ES 模块导入目标不存在：{path.relative_to(root)} -> {match.group(2)}")
                continue
            target_exports = exports_map.get(target, set())
            for name in names:
                if name not in target_exports:
                    errors.append(
                        f"前端 ES 模块未导出符号：{path.relative_to(root)} 导入 {name} <- {match.group(2)}"
                    )
    return errors


def check_css_braces(root: Path = ROOT) -> list[str]:
    """Verify every CSS file under ``web/`` has balanced braces."""
    web_dir = root / "web"
    if not web_dir.is_dir():
        return []
    errors: list[str] = []
    for path in sorted(web_dir.rglob("*.css")):
        text = path.read_text(encoding="utf-8")
        # Strip comments and string literals before counting braces
        cleaned = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        cleaned = re.sub(r'"(?:\\.|[^"\\])*"', '""', cleaned)
        cleaned = re.sub(r"'(?:\\.|[^'\\])*'", "''", cleaned)
        opens, closes = cleaned.count("{"), cleaned.count("}")
        if opens != closes:
            errors.append(f"CSS 花括号不平衡：{path.relative_to(root)} (open={opens}, close={closes})")
    return errors


def check_namespace_collisions(root: Path = ROOT) -> list[str]:
    """Ensure root modules never collide with same-named directories."""
    collisions: list[str] = []
    for py_file in sorted(root.glob("*.py")):
        sibling_dir = root / py_file.stem
        if sibling_dir.is_dir():
            collisions.append(f"根目录存在同名文件与目录冲突：{py_file.name} vs {sibling_dir.name}/")
    return collisions


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass

    graph, raw_imports = build_import_graph(ROOT)
    cycles = find_cycles(graph)
    violations = find_layer_violations(raw_imports)
    version_errors = check_version_consistency(ROOT)
    collision_errors = check_namespace_collisions(ROOT)
    esm_errors = check_esm_imports(ROOT)
    css_errors = check_css_braces(ROOT)
    js_errors, js_count = check_js_syntax(ROOT)

    problems: list[str] = []
    for cycle in cycles:
        problems.append("发现循环依赖：" + " -> ".join(cycle))
    for violation in violations:
        problems.append("发现跨层反向依赖：" + violation)
    problems.extend(version_errors)
    problems.extend(collision_errors)
    problems.extend(esm_errors)
    problems.extend(css_errors)
    for err in js_errors:
        problems.append("前端 JS 语法错误：" + err)

    if problems:
        print("架构与一致性守卫失败：")
        for item in problems:
            print(f"- {item}")
        return 1

    print(
        f"架构与一致性守卫通过：{len(graph)} 个 Python 运行时模块（0 循环 / 0 跨层违规），"
        f"版本文档一致，前端 JS 语法检查 {js_count} 个文件。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
