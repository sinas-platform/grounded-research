"""No name may be used that is never bound.

`_gate_answer` wrote its findings into a module-level `_gate_seqs` dict. A
later commit deleted the declaration and left the writes, so every gate
verdict raised NameError inside a wide `try` and came back out of the except
as "unparseable — treated as pass". The answer gate stopped gating, three
hours of runs published unreviewed, and nothing in the suite noticed.

Ruff's F rules catch this in milliseconds and were already selected in
pyproject — they were simply never run. This pins the fatal subset: a name
used but never bound (F821), a definition silently shadowed (F811), and an
import that resolves to nothing (F822). The style rules stay out of it on
purpose, so this test is about correctness and cannot be waved through.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FATAL = "F821,F811,F822"
APP = Path(__file__).resolve().parents[1] / "app"


def test_no_undefined_or_shadowed_names() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(APP),
         "--select", FATAL, "--no-cache", "--output-format", "concise"],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return
    if "No module named ruff" in proc.stderr:
        import pytest

        pytest.skip("ruff not installed in this environment")
    raise AssertionError(
        "names used but never bound (this is what broke the answer gate):\n"
        + proc.stdout.strip()
    )
