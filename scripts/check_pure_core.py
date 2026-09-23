"""Fail if the pure core (lifecycle.py, providers/) imports frappe.

Standard library only, so it runs in CI without a bench. Usage: python scripts/check_pure_core.py
"""

import ast
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "local_payments"
PURE_TARGETS = [APP / "lifecycle.py", APP / "providers"]
FORBIDDEN = "frappe"


def iter_files():
	for target in PURE_TARGETS:
		if target.is_file():
			yield target
		elif target.is_dir():
			yield from sorted(target.rglob("*.py"))


def forbidden_imports(path: Path):
	tree = ast.parse(path.read_text(), filename=str(path))
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			names = [alias.name for alias in node.names]
		elif isinstance(node, ast.ImportFrom) and node.level == 0:
			names = [node.module or ""]
		else:
			continue
		for name in names:
			if name == FORBIDDEN or name.startswith(FORBIDDEN + "."):
				yield node.lineno, name


def main() -> int:
	failures = [(path, line, name) for path in iter_files() for line, name in forbidden_imports(path)]
	for path, line, name in failures:
		print(f"{path.relative_to(APP.parent)}:{line}: forbidden import of {name}")
	return 1 if failures else 0


if __name__ == "__main__":
	sys.exit(main())
