"""Cross-process serialization for real campaign regression commands."""

from __future__ import annotations

import hashlib
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


@contextmanager
def campaign_operation_lock(
    home: Path,
    campaign_id: str,
    *,
    timeout_seconds: float = 60.0,
) -> Iterator[None]:
    """Allow only one mutating regression process per campaign at a time."""

    if timeout_seconds < 0:
        raise ValueError("campaign operation lock timeout must be nonnegative")
    normalized_campaign_id = str(campaign_id or "").strip()
    if not normalized_campaign_id:
        raise ValueError("campaign operation lock requires a campaign id")
    resolved_home = home.expanduser().resolve()
    lock_dir = resolved_home / ".regression-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    campaign_token = hashlib.sha256(
        normalized_campaign_id.encode("utf-8")
    ).hexdigest()[:24]
    lock_path = lock_dir / f"campaign-operation-{campaign_token}.lock"
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        deadline = time.monotonic() + timeout_seconds
        while True:
            lock_file.seek(0)
            try:
                if os.name == "nt":
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "another regression command is operating on campaign "
                        f"{normalized_campaign_id}; retry after it completes"
                    ) from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            lock_file.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
