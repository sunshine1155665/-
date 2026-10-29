"""Tests for the single-owner file lock.

The lock exists because Windows ``SO_REUSEADDR`` can put two listeners on one
port.  These tests pin the two properties the rest of the stack relies on:
the same process cannot take a lock twice, and a second *process* is refused
until the first one releases it.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fatfish.process_lock import LockBusy, acquire, held, lock_path


ROOT = Path(__file__).resolve().parents[1]


class ProcessLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "run" / "thing.lock"

    def test_lock_path_uses_the_run_directory(self) -> None:
        self.assertEqual(
            lock_path(Path("data"), "tts"),
            Path("data") / "run" / "tts.lock",
        )
        self.assertEqual(
            lock_path(Path("data"), "tts.lock"),
            Path("data") / "run" / "tts.lock",
        )

    def test_second_acquire_in_one_process_is_refused(self) -> None:
        first = acquire(self.path)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(acquire(self.path))
            with self.assertRaises(LockBusy):
                with held(self.path):
                    self.fail("held() should not enter while the lock is busy")
        finally:
            first.release()

    def test_release_allows_a_new_owner(self) -> None:
        first = acquire(self.path)
        assert first is not None
        first.release()
        second = acquire(self.path)
        self.assertIsNotNone(second)
        assert second is not None
        second.release()

    def test_different_paths_do_not_conflict(self) -> None:
        one = acquire(self.path)
        two = acquire(self.path.with_name("other.lock"))
        try:
            self.assertIsNotNone(one)
            self.assertIsNotNone(two)
        finally:
            if one is not None:
                one.release()
            if two is not None:
                two.release()

    def test_a_second_process_is_refused(self) -> None:
        lock = acquire(self.path)
        self.assertIsNotNone(lock)
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from fatfish.process_lock import acquire; "
            "print('busy' if acquire(sys.argv[2]) is None else 'ok')"
        )
        try:
            result = subprocess.run(
                [sys.executable, "-c", code, str(ROOT), str(self.path)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        finally:
            assert lock is not None
            lock.release()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "busy")

    def test_timeout_returns_none_for_a_held_lock(self) -> None:
        lock = acquire(self.path)
        assert lock is not None
        try:
            self.assertIsNone(acquire(self.path, timeout=0.15))
        finally:
            lock.release()


if __name__ == "__main__":
    unittest.main()
