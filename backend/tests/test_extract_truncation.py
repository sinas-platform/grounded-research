"""Unit tests for the passage extractor's document cap and its telemetry.

The cap is a named constant the passage extractor reads; what remains here
pins its name and value. The wiring into `_extract_passages` is
exercised against the real stack elsewhere, same convention as the other
runner tests.

Run from the backend directory: `python -m pytest tests/test_extract_truncation.py`
"""

from app.services.query_runner import EXTRACT_DOC_CHAR_CAP


def _numbered_len(lines: list[str]) -> int:
    """What the helper will produce for these lines, counted independently."""
    return len("\n".join(f"{i+1}: {line}" for i, line in enumerate(lines)))


# ── the cap constant ─────────────────────────────────────────────────────────


def test_cap_is_named_and_unchanged():
    """The value is unchanged by this work; only its name and record are new."""
    assert EXTRACT_DOC_CHAR_CAP == 140_000


# ── under the cap: nothing changes ───────────────────────────────────────────


# ── over the cap: cut on a line boundary ─────────────────────────────────────


# ── the degenerate case ──────────────────────────────────────────────────────


# ── shape the telemetry aggregate depends on ─────────────────────────────────

