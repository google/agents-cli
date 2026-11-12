# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Read/write helpers for pending deploy operations in METADATA_FILE.

When a deployment starts (sync or ``--no-wait``), the long-running operation
claim and metadata are persisted as a ``pending_operation`` field inside
``METADATA_FILE`` with an owner token and state lifecycle.

Cross-process concurrency on the same machine/filesystem is protected by an
advisory file lock (``deployment_metadata.json.lock``) around read/claim/write
transitions, ensuring atomic ownership and preventing concurrent CLI mutations
from corrupting or overwriting deployment state.

Note on distributed environments: cross-machine / CI runners that do not share
a local filesystem are explicitly out of reach for local-file state. In such
environments, concurrency control relies on remote Agent Runtime revalidation
and target-side API conflict rejection.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
import socket
import uuid
from collections.abc import Iterator
from typing import Any

import click
import psutil
from filelock import FileLock, Timeout

METADATA_FILE = "deployment_metadata.json"
METADATA_LOCK_FILE = f"{METADATA_FILE}.lock"
DEFAULT_LOCK_TIMEOUT = 15.0

_LOCKS: dict[str, FileLock] = {}


@contextlib.contextmanager
def get_metadata_lock(
    lock_file: str = METADATA_LOCK_FILE,
    timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> Iterator[FileLock]:
    """Yield a process-cached FileLock for the given path.

    Reusing the FileLock instance per absolute path ensures that reentrant
    lock acquisitions within the same thread/process do not self-deadlock,
    while cross-process callers serialize via OS-level file locks.
    """
    abs_path = os.path.abspath(lock_file)
    if abs_path not in _LOCKS:
        _LOCKS[abs_path] = FileLock(abs_path, timeout=timeout)
    lock = _LOCKS[abs_path]
    lock.timeout = timeout
    try:
        with lock:
            yield lock
    except Timeout as e:
        raise click.ClickException(
            f"Timed out waiting for file lock on {lock_file} ({timeout}s). "
            "Another agents-cli deploy process may be running in this directory."
        ) from e


def _atomic_write_json(file_path: str, data: dict[str, Any]) -> None:
    """Atomically write JSON data to file_path via a temporary file."""
    tmp_path = f"{file_path}.tmp.{uuid.uuid4().hex}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, file_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _atomic_write_bytes(file_path: str, data: bytes) -> None:
    """Atomically write raw bytes to file_path via a temporary file."""
    tmp_path = f"{file_path}.tmp.{uuid.uuid4().hex}"
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, file_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _read_metadata() -> dict[str, Any]:
    """Read METADATA_FILE, tolerating a missing or corrupt file.

    A malformed or zero-byte file (left by an interrupted run, a partial
    write, or a manual edit) is treated as empty so a single bad file can't
    permanently block every subsequent deploy. Returns ``{}`` when the file
    is missing or unreadable.
    """
    if not os.path.exists(METADATA_FILE):
        return {}
    try:
        with open(METADATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logging.warning("Ignoring corrupt %s (%s); treating as empty.", METADATA_FILE, e)
        return {}
    if not isinstance(data, dict):
        logging.warning(
            "Ignoring %s with unexpected top-level %s; treating as empty.",
            METADATA_FILE,
            type(data).__name__,
        )
        return {}
    return data


def _read_raw_metadata_bytes() -> bytes | None:
    """Read raw bytes of METADATA_FILE if present, otherwise None."""
    if not os.path.exists(METADATA_FILE):
        return None
    try:
        with open(METADATA_FILE, "rb") as f:
            return f.read()
    except OSError as e:
        logging.warning("Could not read raw %s: %s", METADATA_FILE, e)
        return None


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is currently alive."""
    if pid <= 0:
        return False
    # os.kill(pid, 0) is unreliable on Windows (sends CTRL_C_EVENT), so use psutil.pid_exists.
    return psutil.pid_exists(pid)


STARTING_CLAIM_TTL_SECONDS = 600.0  # 10 minutes


def parse_started_at(value: Any) -> datetime.datetime | None:
    """A stored ``started_at`` timestamp as an aware UTC datetime, or None.

    Tolerates missing or placeholder values (e.g. "unknown"). Naive timestamps
    are treated as UTC.
    """
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def is_starting_claim_stale(
    claim_dict: dict[str, Any],
    ttl_seconds: float = STARTING_CLAIM_TTL_SECONDS,
) -> bool:
    """Return True if a 'starting' deployment claim is stale.

    When host and PID are known and match the local host, process liveness
    is checked directly via the PID. When liveness cannot be checked
    (e.g., across different hosts or missing host/PID), the claim falls
    back to expiring after ttl_seconds.
    """
    if not isinstance(claim_dict, dict) or claim_dict.get("state") != "starting":
        return False

    owner_pid = claim_dict.get("owner_pid")
    owner_host = claim_dict.get("owner_host")
    if (
        isinstance(owner_pid, int)
        and owner_host is not None
        and owner_host == socket.gethostname()
    ):
        return not _is_pid_alive(owner_pid)

    started_at = parse_started_at(claim_dict.get("started_at"))
    if started_at is not None:
        return (
            datetime.datetime.now(tz=datetime.UTC) - started_at
        ).total_seconds() > ttl_seconds

    return False


class OperationClaim:
    """Represents an exclusive claim to perform a deployment mutation."""

    def __init__(
        self,
        *,
        owner_id: str,
        project: str,
        location: str,
        deployment_target: str,
        started_at: str,
        prior_bytes: bytes | None,
        owner_pid: int | None = None,
        owner_host: str | None = None,
    ) -> None:
        self.owner_id = owner_id
        self.owner_pid = owner_pid
        self.owner_host = owner_host
        self.project = project
        self.location = location
        self.deployment_target = deployment_target
        self.started_at = started_at
        self.prior_bytes = prior_bytes
        self.mutation_started = False
        self.completed = False

    def mark_mutation_started(self) -> None:
        """Mark that remote mutation (identity creation or engine create/update) has started."""
        self.mutation_started = True

    def record_operation(self, operation_name: str) -> None:
        """Record the remote operation name once remote submission succeeds."""
        write_operation(
            operation_name=operation_name,
            project=self.project,
            location=self.location,
            deployment_target=self.deployment_target,
            owner_id=self.owner_id,
            state="running",
        )

    def restore_prior_bytes(self) -> None:
        """Restore METADATA_FILE to its exact bytes prior to this claim.

        Used on pre-mutation failure (e.g. local SDK config build failure)
        to cleanly restore prior state byte-for-byte.
        """
        with get_metadata_lock():
            current_op = _read_metadata().get("pending_operation")
            if (
                isinstance(current_op, dict)
                and current_op.get("owner_id") != self.owner_id
            ):
                logging.warning(
                    "Skipping restore of prior metadata: owner mismatch (expected %s, found %s).",
                    self.owner_id,
                    current_op.get("owner_id"),
                )
                return

            if self.prior_bytes is None:
                if os.path.exists(METADATA_FILE):
                    try:
                        os.remove(METADATA_FILE)
                    except OSError as e:
                        logging.warning("Could not remove %s: %s", METADATA_FILE, e)
            else:
                try:
                    _atomic_write_bytes(METADATA_FILE, self.prior_bytes)
                except OSError as e:
                    logging.warning("Could not restore %s: %s", METADATA_FILE, e)

    def release_on_error(self) -> None:
        """Handle failure during deployment.

        If mutation has not started (pre-mutation failure), restore prior bytes
        byte-for-byte for a clean retry. If mutation has started (remote submission
        or identity creation), retain the fail-closed claim for manual reconciliation.
        """
        if self.completed:
            return
        if not self.mutation_started:
            self.restore_prior_bytes()
        else:
            logging.warning(
                "Deployment mutation may have been initiated remotely. "
                "Retaining pending_operation claim (owner=%s) in %s for manual reconciliation.",
                self.owner_id,
                METADATA_FILE,
            )

    def __enter__(self) -> OperationClaim:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        if exc_type is not None:
            self.release_on_error()


def claim_operation(
    project: str,
    location: str,
    deployment_target: str = "agent_runtime",
    owner_id: str | None = None,
) -> OperationClaim:
    """Atomically claim ownership of a pending deploy operation.

    Acquires the file lock, checks whether an active pending operation or claim
    already exists, and refuses if one is found. If a previous claim is in 'starting'
    state and stale (owner PID dead or TTL expired), it is automatically cleared.
    Otherwise writes a new claim with state="starting" and records prior metadata
    bytes for exact rollback on pre-mutation failure.
    """
    with get_metadata_lock():
        data = _read_metadata()
        pending = data.get("pending_operation")
        stale_cleared = False
        if isinstance(pending, dict) and pending:
            if is_starting_claim_stale(pending):
                logging.warning(
                    "Auto-clearing stale 'starting' deployment claim (owner=%s, pid=%s, started at %s).",
                    pending.get("owner_id"),
                    pending.get("owner_pid"),
                    pending.get("started_at"),
                )
                del data["pending_operation"]
                pending = None
                stale_cleared = True
            else:
                owner = pending.get("owner_id", "unknown")
                state = pending.get("state", "unknown")
                op_name = pending.get("operation_name")
                started = pending.get("started_at", "unknown")
                detail = f"operation: {op_name}" if op_name else f"state: {state}"
                raise click.ClickException(
                    f"A deployment operation is already in progress ({detail}, started at {started}, owner: {owner}).\n"
                    "  Wait for the deployment to finish, check status with 'agents-cli deploy --status',\n"
                    "  or resolve the pending operation in deployment_metadata.json if the previous process crashed."
                )

        if stale_cleared:
            clean_data = dict(data)
            clean_data.pop("pending_operation", None)
            prior_bytes = (
                json.dumps(clean_data, indent=2).encode("utf-8") if clean_data else None
            )
        else:
            prior_bytes = _read_raw_metadata_bytes()

        assigned_owner_id = owner_id or uuid.uuid4().hex
        started_at = datetime.datetime.now(tz=datetime.UTC).isoformat()
        current_pid = os.getpid()
        current_host = socket.gethostname()

        claim_record = {
            "owner_id": assigned_owner_id,
            "owner_pid": current_pid,
            "owner_host": current_host,
            "state": "starting",
            "project": project,
            "location": location,
            "deployment_target": deployment_target,
            "started_at": started_at,
        }
        data["pending_operation"] = claim_record
        _atomic_write_json(METADATA_FILE, data)

        return OperationClaim(
            owner_id=assigned_owner_id,
            owner_pid=current_pid,
            owner_host=current_host,
            project=project,
            location=location,
            deployment_target=deployment_target,
            started_at=started_at,
            prior_bytes=prior_bytes,
        )


def write_operation(
    operation_name: str,
    project: str,
    location: str,
    deployment_target: str,
    *,
    owner_id: str | None = None,
    state: str = "running",
) -> None:
    """Persist a pending deploy operation to METADATA_FILE."""
    with get_metadata_lock():
        data = _read_metadata()
        existing_op = data.get("pending_operation")
        if (
            owner_id is not None
            and isinstance(existing_op, dict)
            and existing_op.get("owner_id") is not None
            and existing_op.get("owner_id") != owner_id
        ):
            logging.warning(
                "Not overwriting pending_operation: owner mismatch (expected %s, found %s).",
                owner_id,
                existing_op.get("owner_id"),
            )
            return

        effective_owner = owner_id
        if effective_owner is None and isinstance(existing_op, dict):
            effective_owner = existing_op.get("owner_id")
        if effective_owner is None:
            effective_owner = uuid.uuid4().hex

        started_at = (
            existing_op.get("started_at")
            if isinstance(existing_op, dict) and existing_op.get("started_at")
            else datetime.datetime.now(tz=datetime.UTC).isoformat()
        )

        pending = {
            "owner_id": effective_owner,
            "state": state,
            "operation_name": operation_name,
            "project": project,
            "location": location,
            "deployment_target": deployment_target,
            "started_at": started_at,
        }
        if isinstance(existing_op, dict):
            for key in ("owner_pid", "owner_host"):
                if key in existing_op:
                    pending[key] = existing_op[key]
        data["pending_operation"] = pending
        _atomic_write_json(METADATA_FILE, data)


def read_operation() -> dict[str, Any] | None:
    """Read a pending operation from METADATA_FILE, or None."""
    with get_metadata_lock():
        op = _read_metadata().get("pending_operation")
        if isinstance(op, dict):
            return op
        return None


def clear_operation(owner_id: str | None = None) -> bool:
    """Remove the pending_operation field from METADATA_FILE.

    If owner_id is specified, only removes pending_operation if its owner_id
    matches. A stale owner cannot clear a replacement owner's record.
    Returns True if cleared, False if skipped due to owner mismatch or missing.
    """
    with get_metadata_lock():
        data = _read_metadata()
        if "pending_operation" in data:
            current_op = data["pending_operation"]
            if owner_id is not None and isinstance(current_op, dict):
                current_owner = current_op.get("owner_id")
                if current_owner is not None and current_owner != owner_id:
                    logging.warning(
                        "Not clearing pending_operation: owner mismatch (expected %s, found %s).",
                        owner_id,
                        current_owner,
                    )
                    return False
            del data["pending_operation"]
            _atomic_write_json(METADATA_FILE, data)
            return True
        return False


def complete_operation(
    metadata_update: dict[str, Any],
    owner_id: str | None = None,
) -> None:
    """Atomically merge metadata and remove the owned pending operation claim.

    Merges ``metadata_update`` into existing sibling metadata in METADATA_FILE
    and removes ``pending_operation`` if owned by ``owner_id`` (or if owner_id
    is not provided) in a single atomic file update.
    """
    with get_metadata_lock():
        data = _read_metadata()
        data.update(metadata_update)
        if "pending_operation" in data:
            current_op = data["pending_operation"]
            if (
                owner_id is None
                or not isinstance(current_op, dict)
                or current_op.get("owner_id") is None
                or current_op.get("owner_id") == owner_id
            ):
                del data["pending_operation"]
            else:
                logging.warning(
                    "Retaining pending_operation during completion: owner mismatch (expected %s, found %s).",
                    owner_id,
                    current_op.get("owner_id"),
                )
        _atomic_write_json(METADATA_FILE, data)


def read_remote_agent_runtime_id() -> str | None:
    """Read the remote_agent_runtime_id from METADATA_FILE."""
    value = _read_metadata().get("remote_agent_runtime_id")
    if value is None:
        return None
    # The file is user-editable, so the value may not be a string at all.
    if not isinstance(value, str):
        logging.warning(
            "Ignoring remote_agent_runtime_id in %s: expected a string, got %s.",
            METADATA_FILE,
            type(value).__name__,
        )
        return None
    # Older scaffolds shipped the literal string ``"None"`` as a placeholder, so
    # treat that (and blank values) as "no id recorded" — otherwise a first deploy
    # would try to update an engine literally named ``None`` and fail with a 404
    # instead of creating a new one.
    value = value.strip()
    if value.lower() in ("", "none", "null"):
        return None
    return value
