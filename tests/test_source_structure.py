import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "ai_sessions"
EXCLUDED_SUBPACKAGES = {"harnesses"}
GENERIC_MODULES = tuple(
    sorted(
        path.relative_to(ROOT).as_posix()
        for path in SOURCE_ROOT.rglob("*.py")
        if not EXCLUDED_SUBPACKAGES.intersection(path.relative_to(SOURCE_ROOT).parts)
    )
)
COMPATIBILITY_FUNCTIONS = {
    ("src/ai_sessions/app.py", "load_codex_sessions"),
    ("src/ai_sessions/app.py", "load_claude_sessions"),
    ("src/ai_sessions/app.py", "custom_mode_notice"),
    ("src/ai_sessions/config.py", "LaunchConfig.load"),
    ("src/ai_sessions/config.py", "LaunchConfig._profile"),
}
COMPATIBILITY_MODULE_CONSTANTS = {"src/ai_sessions/config.py"}
PROVIDERS = {"claude", "codex"}


def function_owner(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    owner = node
    names: list[str] = []
    found_function = False
    while owner in parents:
        owner = parents[owner]
        if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(owner.name)
            found_function = found_function or isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            )
    return ".".join(reversed(names)) if found_function else "<module>"


def find_violations(source: str, relative: str) -> list[str]:
    tree = ast.parse(source, filename=relative)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    violations: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in PROVIDERS
        ):
            continue
        owner = function_owner(node, parents)
        if (relative, owner) in COMPATIBILITY_FUNCTIONS:
            continue
        if owner == "<module>" and relative in COMPATIBILITY_MODULE_CONSTANTS:
            continue
        violations.append(f"{relative}:{node.lineno} {owner} names {node.value!r}")
    return violations


class SourceStructureTests(unittest.TestCase):
    def test_detector_catches_direct_and_indirect_provider_branches(self) -> None:
        violating = """
PROVIDERS = ("claude", "codex")
HANDLERS = {"claude": object()}
def route(tool):
    return HANDLERS["codex"] if tool in PROVIDERS else None
"""
        found = find_violations(violating, "synthetic.py")
        self.assertEqual(len(found), 4)

    def test_detector_honors_only_declared_compatibility_scopes(self) -> None:
        allowed_function = 'class LaunchConfig:\n    def load(self):\n        return "claude"\n'
        self.assertEqual(find_violations(allowed_function, "src/ai_sessions/config.py"), [])
        allowed_module = 'DEFAULT = "codex"\n'
        self.assertEqual(find_violations(allowed_module, "src/ai_sessions/config.py"), [])
        self.assertNotEqual(find_violations(allowed_function, "synthetic.py"), [])

    def test_compatibility_allowlist_names_existing_functions(self) -> None:
        unused: list[tuple[str, str]] = []
        for relative, name in COMPATIBILITY_FUNCTIONS:
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=relative)
            parents: dict[ast.AST, ast.AST] = {}
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[child] = parent
            if not any(
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in PROVIDERS
                and function_owner(node, parents) == name
                for node in ast.walk(tree)
            ):
                unused.append((relative, name))
        self.assertEqual(unused, [])
        self.assertTrue(COMPATIBILITY_MODULE_CONSTANTS)
        self.assertTrue(
            all((ROOT / relative).is_file() for relative in COMPATIBILITY_MODULE_CONSTANTS)
        )
        unused_modules: list[str] = []
        for relative in COMPATIBILITY_MODULE_CONSTANTS:
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=relative)
            parents: dict[ast.AST, ast.AST] = {}
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[child] = parent
            if not any(
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in PROVIDERS
                and function_owner(node, parents) == "<module>"
                for node in ast.walk(tree)
            ):
                unused_modules.append(relative)
        self.assertEqual(unused_modules, [])
        self.assertTrue(EXCLUDED_SUBPACKAGES)
        self.assertTrue(all((SOURCE_ROOT / name).is_dir() for name in EXCLUDED_SUBPACKAGES))
        self.assertIn("src/ai_sessions/platforms/windows.py", GENERIC_MODULES)

    def test_generic_modules_do_not_name_builtin_providers_outside_compatibility(self) -> None:
        violations: list[str] = []
        for relative in GENERIC_MODULES:
            violations.extend(
                find_violations((ROOT / relative).read_text(encoding="utf-8"), relative)
            )
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
