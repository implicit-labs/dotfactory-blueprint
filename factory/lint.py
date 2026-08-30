#!/usr/bin/env python3
"""Small dependency-free lint gate for the factory Python boundary."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    code: str
    message: str


def python_files() -> list[Path]:
    paths = [ROOT / "render_workflow.py", ROOT / "lint.py"]
    paths.extend((ROOT / "src").rglob("*.py"))
    paths.extend((ROOT / "tests").rglob("*.py"))
    return sorted(set(paths))


def lint_path(path: Path) -> list[Finding]:
    display = str(path.relative_to(ROOT.parent)) if path.is_relative_to(ROOT.parent) else str(path)
    source = path.read_text(encoding="utf-8")
    findings = []
    if source and not source.endswith("\n"):
        findings.append(Finding(display, len(source.splitlines()), "TXT001", "missing final newline"))
    for number, line in enumerate(source.splitlines(), start=1):
        if "\t" in line:
            findings.append(Finding(display, number, "TXT002", "tab character"))
        if line.endswith((" ", "\t")):
            findings.append(Finding(display, number, "TXT003", "trailing whitespace"))
    try:
        tree = ast.parse(source, filename=str(path), feature_version=9)
        compile(source, str(path), "exec")
    except SyntaxError as error:
        findings.append(Finding(
            display, error.lineno or 1, "PY001", error.msg,
        ))
        return findings
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "*" for alias in node.names
        ):
            findings.append(Finding(display, node.lineno, "PY002", "wildcard import"))
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            findings.append(Finding(display, node.lineno, "PY003", "bare except"))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
            "eval", "exec", "breakpoint",
        }:
            findings.append(Finding(
                display, node.lineno, "PY004", f"forbidden call: {node.func.id}",
            ))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defaults = list(node.args.defaults) + [
                item for item in node.args.kw_defaults if item is not None
            ]
            for default in defaults:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    findings.append(Finding(
                        display, default.lineno, "PY005", "mutable function default",
                    ))
    return findings


def lint_paths(paths: Iterable[Path]) -> list[Finding]:
    findings = []
    for path in paths:
        findings.extend(lint_path(path))
    return sorted(findings)


def main() -> int:
    paths = python_files()
    findings = lint_paths(paths)
    for finding in findings:
        print(
            f"{finding.path}:{finding.line}: {finding.code} {finding.message}",
            file=sys.stderr,
        )
    if findings:
        return 1
    print(f"PASS factory lint ({len(paths)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
