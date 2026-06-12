"""Tests for the shared cross-process lock helper in pipeline._batch_common.

This is the single mechanism that replaces the two divergent pid guards (the
batch-eval lock and the UI local-run pid file). Contract:
- atomic acquire (no empty-file TOCTOU window a racer can exploit),
- stores pid + timestamp,
- a holder refreshes the timestamp (heartbeat) so a long LIVE run never looks
  stale and is never stolen,
- a lock is reclaimable only if its holder is dead OR its timestamp is older
  than max_age (the pid-reuse safety valve),
- release only deletes a lock we still own.
"""

import os
import subprocess
import sys
import time

import pytest

from pipeline import _batch_common as bc


def _spawn_live():
    """A real, alive child process whose pid we can borrow as a 'foreign holder'."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class TestProcessLock:
    def test_acquire_writes_pid_and_timestamp(self, tmp_path):
        lock = tmp_path / "x.lock"
        assert bc.acquire_process_lock(lock, max_age=1800) is True
        pid, ts = bc.read_process_lock(lock)
        assert pid == os.getpid()
        assert ts > 0

    def test_refuses_live_foreign_holder(self, tmp_path):
        lock = tmp_path / "x.lock"
        child = _spawn_live()
        try:
            lock.write_text(f"{child.pid} {time.time()}", encoding="utf-8")
            assert bc.acquire_process_lock(lock, max_age=1800) is False
            assert lock.exists()                 # must not steal a live holder's lock
        finally:
            child.kill()

    def test_reclaims_dead_holder(self, tmp_path):
        lock = tmp_path / "x.lock"
        lock.write_text(f"{_dead_pid()} {time.time()}", encoding="utf-8")
        assert bc.acquire_process_lock(lock, max_age=1800) is True
        assert bc.read_process_lock(lock)[0] == os.getpid()

    def test_stale_live_holder_reclaimed(self, tmp_path):
        # pid-reuse valve: a live pid whose timestamp is older than max_age (the
        # original holder died and the OS recycled its pid, OR it stopped
        # heartbeating) is reclaimable — otherwise the lock wedges forever.
        lock = tmp_path / "x.lock"
        child = _spawn_live()
        try:
            lock.write_text(f"{child.pid} {time.time() - 10000}", encoding="utf-8")
            assert bc.acquire_process_lock(lock, max_age=1800) is True
        finally:
            child.kill()

    def test_empty_file_not_immediately_takeable(self, tmp_path):
        # #5 (TOCTOU): a racer that sees a just-created EMPTY lock (the window
        # between O_EXCL create and the pid write) must NOT treat it as free.
        lock = tmp_path / "x.lock"
        lock.write_text("", encoding="utf-8")    # fresh mtime, no pid yet
        assert bc.acquire_process_lock(lock, max_age=1800) is False

    def test_refresh_updates_timestamp(self, tmp_path):
        lock = tmp_path / "x.lock"
        bc.acquire_process_lock(lock, max_age=1800)
        _, ts0 = bc.read_process_lock(lock)
        time.sleep(0.02)
        bc.refresh_process_lock(lock)
        _, ts1 = bc.read_process_lock(lock)
        assert ts1 > ts0

    def test_release_only_if_owner(self, tmp_path):
        lock = tmp_path / "x.lock"
        bc.acquire_process_lock(lock, max_age=1800)
        lock.write_text(f"{os.getpid() + 1} {time.time()}", encoding="utf-8")  # someone else
        bc.release_process_lock(lock)
        assert lock.exists()                     # not ours → must not delete

    def test_release_deletes_when_owner(self, tmp_path):
        lock = tmp_path / "x.lock"
        bc.acquire_process_lock(lock, max_age=1800)
        bc.release_process_lock(lock)
        assert not lock.exists()

    def test_process_lock_active(self, tmp_path):
        # The non-acquiring liveness probe used by is_running()-style checks.
        lock = tmp_path / "x.lock"
        assert bc.process_lock_active(lock, max_age=1800) is False   # absent
        child = _spawn_live()
        try:
            lock.write_text(f"{child.pid} {time.time()}", encoding="utf-8")
            assert bc.process_lock_active(lock, max_age=1800) is True
            lock.write_text(f"{child.pid} {time.time() - 10000}", encoding="utf-8")
            assert bc.process_lock_active(lock, max_age=1800) is False  # stale → inactive
        finally:
            child.kill()
