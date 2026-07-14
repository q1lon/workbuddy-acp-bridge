"""测试 _PersistentSessionStore 的文件锁，重点覆盖 POSIX (macOS) 路径。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from workbuddy_acp_bridge.server import _PersistentSessionStore


def test_lock_acquire_and_release(tmp_path: Path) -> None:
    """验证 acquire_lock 返回文件对象，release_lock 释放后另一个进程可以获取锁。"""
    store = _PersistentSessionStore(path=tmp_path / "state.json")
    lock1 = store.acquire_lock()
    assert lock1 is not None
    assert not lock1.closed
    store.release_lock(lock1)
    # 释放后重新获取不应阻塞
    lock2 = store.acquire_lock()
    assert lock2 is not None
    store.release_lock(lock2)


def test_lock_exclusion(tmp_path: Path) -> None:
    """验证两个进程不能同时持有锁。"""
    store = _PersistentSessionStore(path=tmp_path / "state.json")

    src_dir = str(Path(__file__).resolve().parents[1] / "src")

    # 子进程获取锁后应阻止父进程同时获取
    state_path = str(tmp_path / "state.json")
    script = f"""import sys
sys.path.insert(0, {src_dir!r})
import time
from pathlib import Path
from workbuddy_acp_bridge.server import _PersistentSessionStore
store = _PersistentSessionStore(path=Path({state_path!r}))
lock = store.acquire_lock()
print("child: locked", flush=True)
time.sleep(0.5)
store.release_lock(lock)
"""

    # 同步点：等待子进程打印 "child: locked" 后再让父进程尝试加锁
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    assert line.strip() == b"child: locked", f"子进程未正常加锁: {line}"

    # 父进程尝试获取锁 — 应等待子进程释放
    start = __import__("time").time()
    lock = store.acquire_lock()
    elapsed = __import__("time").time() - start
    store.release_lock(lock)
    proc.wait(timeout=5)

    # 至少阻塞了 0.3 秒（考虑调度误差），说明确实等待了子进程
    assert elapsed >= 0.3, f"elapsed={elapsed:.2f}s, 锁未正确互斥"


def test_lock_file_created(tmp_path: Path) -> None:
    """验证锁文件在 acquire_lock 时被创建。"""
    store = _PersistentSessionStore(path=tmp_path / "state.json")
    assert not store.lock_path.exists()
    lock = store.acquire_lock()
    assert store.lock_path.exists()
    store.release_lock(lock)


def test_posix_lock_roundtrip(tmp_path: Path) -> None:
    """在 POSIX 系统上验证 acquire/release 路径正常。"""
    if os.name == "nt":
        pytest.skip("POSIX 专属测试：Windows 使用 msvcrt 路径")
    store = _PersistentSessionStore(path=tmp_path / "state.json")
    lock = store.acquire_lock()
    store.release_lock(lock)
    # 锁文件在 release 后保留，供后续进程使用
    assert store.lock_path.exists()