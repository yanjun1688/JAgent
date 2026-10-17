"""Dead-code gate: private symbols defined but never referenced.

Implements the method documented in
``JAgent-docs/reviews/review_20260908_dead_code_sweep.md`` §1:

1. AST-extract single-underscore ``_name`` definitions (functions, classes,
   module/class-level constants) from the given roots.
2. Count repo-wide word occurrences of each name across all ``.py`` files
   (comments and docstrings included, matching the review's criterion).
3. A symbol whose *only* occurrence is its own definition is a candidate.

Deliberate limits (the review is explicit that this must NOT be mechanical):

* Decorated defs are skipped: framework/validator callbacks (pydantic
  ``@model_validator`` etc.) are invoked dynamically and naive counting flags
  them as dead. See review §4.
* Private *methods* are only reported when truly single-occurrence; dynamic
  dispatch on an attribute name that appears elsewhere still suppresses them.
* A candidate is a *lead for human review*, never an auto-delete order. To keep
  a reviewed-and-intended symbol, add its name to ``.deadcode-allow``.
* General-purpose ``vulture`` is intentionally not used: on this codebase it
  drowns pydantic fields / FastAPI routes / middleware in false positives.

Exit code is non-zero when unallowed candidates exist (CI gate).
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOW_FILE = REPO_ROOT / ".deadcode-allow"
SKIP_DIRS = {".venv", ".git", ".mypy_cache", "__pycache__", "node_modules", "data"}


def _iter_py_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            for p in root.rglob("*.py"):
                if not any(part in SKIP_DIRS for part in p.parts):
                    files.append(p)
    return sorted(set(files))


def _collect_candidates(files: list[Path]) -> list[tuple[str, Path, int]]:
    """Return ``(name, file, lineno)`` for private definitions in ``files``."""
    out: list[tuple[str, Path, int]] = []
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        except SyntaxError:
            continue
        for node in tree.body:
            out.extend(_candidates_in(node, f))
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    out.extend(_candidates_in(child, f))
    return [c for c in out if not c[0].startswith("__") and c[0].startswith("_")]


def _candidates_in(node: ast.stmt, f: Path) -> list[tuple[str, Path, int]]:
    name: str | None = None
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.decorator_list:  # dynamic dispatch (pydantic/FastAPI/...)
            return []
        name = node.name
    elif isinstance(node, ast.ClassDef):
        name = node.name
    elif isinstance(node, ast.Assign):
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        name = node.target.id
    if not name or not name.startswith("_") or name.startswith("__"):
        return []
    return [(name, f, node.lineno)]


def _count_occurrences(name: str, files: list[Path], cache: dict[Path, str]) -> int:
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    total = 0
    for f in files:
        if f not in cache:
            cache[f] = f.read_text(encoding="utf-8", errors="ignore")
        total += len(pattern.findall(cache[f]))
    return total


def _load_allow() -> set[str]:
    if not ALLOW_FILE.exists():
        return set()
    names: set[str] = set()
    for line in ALLOW_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(line)
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect defined-but-unreferenced private symbols.")
    parser.add_argument("roots", nargs="*", default=["harness"], help="files/dirs to scan (default: harness)")
    args = parser.parse_args(argv)

    roots = [Path(r) if Path(r).is_absolute() else REPO_ROOT / r for r in args.roots]
    scan_files = _iter_py_files(roots)
    all_text_files = _iter_py_files([REPO_ROOT])
    allow = _load_allow()

    cache: dict[Path, str] = {}
    candidates: list[tuple[str, Path, int]] = []
    for name, f, lineno in _collect_candidates(scan_files):
        if name in allow:
            continue
        if _count_occurrences(name, all_text_files, cache) <= 1:
            candidates.append((name, f, lineno))

    if not candidates:
        print(f"[OK] No dead private symbols across {len(scan_files)} file(s).")
        return 0

    print(f"[DEAD CODE] {len(candidates)} private symbol(s) defined but never referenced:")
    for name, f, lineno in candidates:
        print(f"  {f.relative_to(REPO_ROOT)}:{lineno}: {name}")
    print("\nReview each: delete it, or add the name to .deadcode-allow with a reason.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
