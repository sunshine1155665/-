"""Which sessions/sources are test data and must not feed production recall.

The detail layer has always refused to archive these sessions, but older
memories (night-test runs, verify/devcheck sessions) and test Bilibili rows
still sit in the real DB and competed with real memories.  The 2026-10-26
decision is read-path isolation: keep the rows (no destructive cleanup without
owner approval) but never surface them to the prompt.  The owner's night-shift
activity is read from ``conversation_turns`` by ``_recent_activity_evidence``,
so excluding ``night-*`` memory rows does not lose that block.
"""

from __future__ import annotations

TEST_SESSION_MARKERS = (
    "verify-",
    "devcheck",
    "test-",
    "live-tts25",
    "smoke-",
    "night-",
    "demo-",
)

# Fixture BVIDs used by tests/manual probes.  Real BVIDs are never these.
TEST_BVIDS = frozenset({"BV_TEST", "BV_DEMO"})


def is_test_session(session_id: object) -> bool:
    text = str(session_id or "")
    return bool(text) and any(marker in text for marker in TEST_SESSION_MARKERS)


def is_test_bvid(source_id: object) -> bool:
    return str(source_id or "") in TEST_BVIDS
