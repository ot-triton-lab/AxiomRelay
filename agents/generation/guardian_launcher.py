"""Production launcher for one externally guarded AxiomRelay paid root.

The owner process performs the durable launch prepare with the owner-master
capability.  A freshly detached, single-threaded daemon receives only the
guardian-cycle capability and starts a stable, exec-blocked process group.
The paid control process receives its runner-cycle capability through a
one-shot inherited pipe; the capability is never placed in the launcher's
command line or in the model process environment.

This module intentionally has no release-gate override.  It validates the
source closure against the immutable policy contract before preparing a
launch, and the host independently validates the same canonical manifest.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import select
import signal
import stat
import subprocess
import sys
import time
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LAUNCH_MANIFEST_SCHEMA = "rethlas_guardian_launch_manifest_v1"
LAUNCH_MANIFEST_SCHEMA_DESCRIPTOR = {
    "exact_keys": [
        "adapter_relative_path",
        "adapter_sha256",
        "guardian_control_schema_sha256",
        "guardian_mode",
        "guardian_relative_path",
        "guardian_sha256",
        "handoff_candidate_sha256",
        "launcher_relative_path",
        "launcher_sha256",
        "problem_relative_path",
        "problem_sha256",
        "runner_relative_path",
        "runner_sha256",
        "schema_version",
        "worker_command_sha256",
        "worker_cwd",
        "worker_environment_sha256",
        "worker_executable_sha256",
        "worker_mode",
        "worker_runtime_command_sha256",
    ],
    "manifest_schema_version": LAUNCH_MANIFEST_SCHEMA,
    "schema_version": "rethlas_guardian_launch_manifest_schema_v1",
}
LAUNCH_MANIFEST_KEYS = frozenset(LAUNCH_MANIFEST_SCHEMA_DESCRIPTOR["exact_keys"])
LAUNCH_MANIFEST_SCHEMA_SHA256 = hashlib.sha256(
    json.dumps(
        LAUNCH_MANIFEST_SCHEMA_DESCRIPTOR,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()

LAUNCHER_RELATIVE_PATH = "agents/generation/guardian_launcher.py"
GUARDIAN_RELATIVE_PATH = "agents/generation/guardian.py"
RUNNER_RELATIVE_PATH = "agents/generation/tests/run_hotjoin.sh"
ADAPTER_RELATIVE_PATH = "agents/hotjoin_adapter.py"
GUARDIAN_CONTROL_SCHEMA = "rethlas_guardian_control_v1"
OWNER_TOKEN_ENV = "RETHLAS_REVIEW_CONTROL_TOKEN"
GUARDIAN_TOKEN_ENV = "RETHLAS_GUARDIAN_CYCLE_TOKEN"
RUNNER_TOKEN_ENV = "RETHLAS_RUNNER_CYCLE_TOKEN"
STALE_TOKEN_ENV = "RETHLAS_STALE_RECOVERY_TOKEN"
PRIVILEGED_ENVIRONMENT = frozenset(
    {OWNER_TOKEN_ENV, GUARDIAN_TOKEN_ENV, RUNNER_TOKEN_ENV, STALE_TOKEN_ENV}
)
WORKER_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "CODEX_HOME",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "PYTHONDONTWRITEBYTECODE",
        "RETHLAS_ADVISOR_RECEIPTS_ROOT",
        "RETHLAS_EXPECTED_HOTJOIN_RUN_ID",
        "RETHLAS_EXPECTED_PROBLEM_ID",
        "RETHLAS_EXPECTED_STATEMENT_SHA256",
        "RETHLAS_GENERATION_ROOT",
        "RETHLAS_RECEIPTS_ROOT",
        "RETHLAS_RUN_MODE",
        "RETHLAS_RESOLVED_COST_POLICY_JSON",
        "RETHLAS_RESOLVED_COST_POLICY_SHA256",
        "TMPDIR",
        "USER",
    }
)
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
RUN_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
WATCHDOG_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
GENERATION_CONTROL_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
_MAX_HOST_STDOUT_BYTES = 512 * 1024
_MAX_HOST_STDERR_BYTES = 64 * 1024
_MAX_EVENT_BYTES = 512 * 1024
_REGISTRATION_WINDOW_SECONDS = 25.0
_OWNER_MONITOR_INTERVAL_SECONDS = 0.02
_DAEMON_EVENT_EOF_TIMEOUT_SECONDS = 1.0
_DURABLE_TERMINAL_EXIT_TIMEOUT_SECONDS = 1.0


class LauncherError(RuntimeError):
    """The production launch could not be proven safe."""


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


_MAX_OFFLINE_FAILURE_GROUPS = 256


def _bounded_offline_failure(
    detail_material: list[dict[str, str]],
    observed_groups: list[dict[str, object]],
) -> dict[str, object]:
    """Bind the complete local set while exposing a bounded exact sample."""

    sample = observed_groups[:_MAX_OFFLINE_FAILURE_GROUPS]
    return {
        "schema_version": "rethlas_guardian_offline_failure_v1",
        "code": "offline_cleanup_failure",
        "detail_sha256": canonical_sha256(detail_material),
        "group_count": len(observed_groups),
        "groups": sample,
        "groups_complete": len(sample) == len(observed_groups),
        "groups_sha256": canonical_sha256(observed_groups),
    }


def guardian_cycle_id(*, run_id: str, generation: int, watchdog_id: str) -> str:
    material = {
        "schema_version": "rethlas_guardian_cycle_id_v1",
        "run_id": run_id,
        "generation": generation,
        "watchdog_id": watchdog_id,
    }
    return "cycle_" + canonical_sha256(material)[:32]


def consume_token_fd(descriptor: int, *, label: str) -> str:
    """Consume one exact, non-replayable capability from an inherited FIFO."""

    if type(descriptor) is not int or descriptor <= 2:
        raise LauncherError(f"{label} capability FD must be greater than 2")
    try:
        metadata = os.fstat(descriptor)
    except OSError as error:
        raise LauncherError(f"{label} capability FD is not open") from error
    if not stat.S_ISFIFO(metadata.st_mode):
        raise LauncherError(f"{label} capability FD must be a FIFO")
    try:
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFD)
        fcntl.fcntl(descriptor, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
        raw = bytearray()
        while len(raw) < 64:
            chunk = os.read(descriptor, 64 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        trailing = os.read(descriptor, 1)
    finally:
        os.close(descriptor)
    if (
        len(raw) != 64
        or trailing != b""
        or SHA256_RE.fullmatch(raw.decode("ascii", errors="ignore")) is None
    ):
        for index in range(len(raw)):
            raw[index] = 0
        raise LauncherError(f"{label} capability pipe is not exact 64hex plus EOF")
    token = raw.decode("ascii")
    for index in range(len(raw)):
        raw[index] = 0
    return token


def _fresh_token_pipe(token: str) -> int:
    if SHA256_RE.fullmatch(token) is None:
        raise LauncherError("scoped host capability is malformed")
    read_fd, write_fd = os.pipe()
    try:
        view = memoryview(token.encode("ascii"))
        while view:
            written = os.write(write_fd, view)
            if written <= 0:
                raise LauncherError("capability pipe write was short")
            view = view[written:]
        os.close(write_fd)
        write_fd = -1
        return read_fd
    except BaseException:
        for descriptor in (read_fd, write_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        raise


def _read_all_at(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(131_072, size - offset), offset)
        if not chunk:
            raise LauncherError("pinned source became short")
        chunks.append(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, size):
        raise LauncherError("pinned source grew during attestation")
    return b"".join(chunks)


def _stream_sha256_at(descriptor: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
        if not chunk:
            raise LauncherError("pinned executable became short")
        digest.update(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, size):
        raise LauncherError("pinned executable grew during attestation")
    return digest.hexdigest()


def _read_bounded_fifo(descriptor: int, *, maximum: int, label: str) -> bytes:
    if descriptor <= 2:
        raise LauncherError(f"{label} FD must be greater than 2")
    metadata = os.fstat(descriptor)
    if not stat.S_ISFIFO(metadata.st_mode):
        raise LauncherError(f"{label} FD must be a FIFO")
    flags = fcntl.fcntl(descriptor, fcntl.F_GETFD)
    fcntl.fcntl(descriptor, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise LauncherError(f"{label} exceeded its byte bound")
    finally:
        os.close(descriptor)


def _reject_symlink_components(path: Path) -> None:
    cursor = path
    while True:
        metadata = cursor.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise LauncherError(f"trusted path traverses a symlink: {cursor}")
        if cursor.parent == cursor:
            return
        cursor = cursor.parent


@dataclass(slots=True)
class PinnedSource:
    path: Path
    descriptor: int
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str
    content: bytes

    @classmethod
    def open(
        cls,
        raw_path: Path,
        *,
        expected_sha256: str | None = None,
        allow_root_owner: bool = False,
    ) -> PinnedSource:
        path = Path(os.path.abspath(os.fspath(raw_path)))
        if not path.is_absolute():
            raise LauncherError("trusted source path must be absolute")
        _reject_symlink_components(path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid
                not in ({os.getuid(), 0} if allow_root_owner else {os.getuid()})
                or before.st_nlink < 1
                or (before.st_uid != 0 and before.st_nlink != 1)
                or stat.S_IMODE(before.st_mode) & 0o022
                or before.st_size <= 0
                or before.st_size > _MAX_SOURCE_BYTES
            ):
                raise LauncherError(
                    "trusted source must be one owner-owned, non-writable regular file"
                )
            content = _read_all_at(descriptor, int(before.st_size))
            after = os.fstat(descriptor)
            identity_before = (
                before.st_dev,
                before.st_ino,
                before.st_uid,
                stat.S_IMODE(before.st_mode),
                before.st_nlink,
                before.st_size,
                before.st_mtime_ns,
            )
            identity_after = (
                after.st_dev,
                after.st_ino,
                after.st_uid,
                stat.S_IMODE(after.st_mode),
                after.st_nlink,
                after.st_size,
                after.st_mtime_ns,
            )
            if identity_before != identity_after:
                raise LauncherError("trusted source changed during pinning")
            digest = hashlib.sha256(content).hexdigest()
            if expected_sha256 is not None and digest != expected_sha256:
                raise LauncherError(f"trusted source SHA-256 mismatch: {path}")
            return cls(
                path=path,
                descriptor=descriptor,
                device=int(before.st_dev),
                inode=int(before.st_ino),
                size=int(before.st_size),
                mtime_ns=int(before.st_mtime_ns),
                sha256=digest,
                content=content,
            )
        except BaseException:
            os.close(descriptor)
            raise

    @classmethod
    def adopt(
        cls,
        descriptor: int,
        raw_path: Path,
        *,
        expected_sha256: str,
    ) -> PinnedSource:
        """Adopt the exact FD from the trusted runner's secure entry loader."""

        if type(descriptor) is not int or descriptor <= 2:
            raise LauncherError("secure launcher source FD is invalid")
        if SHA256_RE.fullmatch(expected_sha256) is None:
            raise LauncherError("secure launcher source digest is malformed")
        path = Path(os.path.abspath(os.fspath(raw_path)))
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) & 0o022
                or metadata.st_size <= 0
                or metadata.st_size > _MAX_SOURCE_BYTES
            ):
                raise LauncherError("secure launcher source FD is not trusted")
            content = _read_all_at(descriptor, int(metadata.st_size))
            digest = hashlib.sha256(content).hexdigest()
            if digest != expected_sha256:
                raise LauncherError("secure launcher entry digest mismatch")
            return cls(
                path=path,
                descriptor=descriptor,
                device=int(metadata.st_dev),
                inode=int(metadata.st_ino),
                size=int(metadata.st_size),
                mtime_ns=int(metadata.st_mtime_ns),
                sha256=digest,
                content=content,
            )
        except BaseException:
            os.close(descriptor)
            raise

    def attest_unchanged(self) -> None:
        current = os.fstat(self.descriptor)
        if (
            int(current.st_dev),
            int(current.st_ino),
            int(current.st_size),
            int(current.st_mtime_ns),
        ) != (self.device, self.inode, self.size, self.mtime_ns):
            raise LauncherError(f"pinned source identity changed: {self.path}")
        if (
            hashlib.sha256(_read_all_at(self.descriptor, self.size)).hexdigest()
            != self.sha256
        ):
            raise LauncherError(f"pinned source content changed: {self.path}")

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


@dataclass(slots=True)
class PinnedExecutable:
    """A large executable pinned by FD and streaming digest, without retaining bytes."""

    path: Path
    descriptor: int
    device: int
    inode: int
    owner_uid: int
    mode: int
    links: int
    size: int
    mtime_ns: int
    sha256: str

    @classmethod
    def open(
        cls,
        raw_path: Path,
        *,
        expected_sha256: str | None = None,
    ) -> PinnedExecutable:
        path = Path(os.path.abspath(os.fspath(raw_path)))
        _reject_symlink_components(path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            mode = stat.S_IMODE(before.st_mode)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid not in {os.getuid(), 0}
                or before.st_nlink < 1
                or (before.st_uid != 0 and before.st_nlink != 1)
                or mode & 0o022
                or mode & 0o111 == 0
                or before.st_size <= 0
                or before.st_size > _MAX_EXECUTABLE_BYTES
            ):
                raise LauncherError(
                    "trusted executable must be owner/root-owned, non-writable, "
                    "executable, regular, and at most 512 MiB"
                )
            digest = _stream_sha256_at(descriptor, int(before.st_size))
            after = os.fstat(descriptor)
            identity_before = (
                before.st_dev,
                before.st_ino,
                before.st_uid,
                mode,
                before.st_nlink,
                before.st_size,
                before.st_mtime_ns,
            )
            identity_after = (
                after.st_dev,
                after.st_ino,
                after.st_uid,
                stat.S_IMODE(after.st_mode),
                after.st_nlink,
                after.st_size,
                after.st_mtime_ns,
            )
            if identity_before != identity_after:
                raise LauncherError("trusted executable changed during pinning")
            if expected_sha256 is not None and digest != expected_sha256:
                raise LauncherError(f"trusted executable SHA-256 mismatch: {path}")
            return cls(
                path=path,
                descriptor=descriptor,
                device=int(before.st_dev),
                inode=int(before.st_ino),
                owner_uid=int(before.st_uid),
                mode=mode,
                links=int(before.st_nlink),
                size=int(before.st_size),
                mtime_ns=int(before.st_mtime_ns),
                sha256=digest,
            )
        except BaseException:
            os.close(descriptor)
            raise

    def attest_unchanged(self) -> None:
        current = os.fstat(self.descriptor)
        if (
            int(current.st_dev),
            int(current.st_ino),
            int(current.st_uid),
            stat.S_IMODE(current.st_mode),
            int(current.st_nlink),
            int(current.st_size),
            int(current.st_mtime_ns),
        ) != (
            self.device,
            self.inode,
            self.owner_uid,
            self.mode,
            self.links,
            self.size,
            self.mtime_ns,
        ):
            raise LauncherError(f"pinned executable identity changed: {self.path}")
        if _stream_sha256_at(self.descriptor, self.size) != self.sha256:
            raise LauncherError(f"pinned executable content changed: {self.path}")

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


_PINNED_SCRIPT_LOADER = r"""
import hashlib
import os
import sys

descriptor = int(sys.argv[1])
size = int(sys.argv[2])
expected = sys.argv[3]
label = sys.argv[4]
chunks = []
offset = 0
while offset < size:
    chunk = os.pread(descriptor, min(131072, size - offset), offset)
    if not chunk:
        raise SystemExit("pinned adapter became short")
    chunks.append(chunk)
    offset += len(chunk)
if os.pread(descriptor, 1, size):
    raise SystemExit("pinned adapter grew")
source = b"".join(chunks)
if hashlib.sha256(source).hexdigest() != expected:
    raise SystemExit("pinned adapter digest mismatch")
sys.argv = [label, *sys.argv[5:]]
namespace = {
    "__builtins__": __builtins__,
    "__file__": label,
    "__name__": "__main__",
    "__package__": None,
    "__spec__": None,
    "__rethlas_pinned_launcher_fd__": descriptor,
    "__rethlas_pinned_launcher_path__": label,
    "__rethlas_pinned_launcher_sha256__": expected,
}
exec(compile(source, label, "exec", dont_inherit=True), namespace, namespace)
"""
PINNED_SCRIPT_LOADER_SHA256 = hashlib.sha256(
    _PINNED_SCRIPT_LOADER.encode("utf-8")
).hexdigest()


def _strict_json(raw: bytes, *, label: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise LauncherError(f"duplicate JSON key in {label}: {key}")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise LauncherError(f"non-finite JSON value in {label}: {value}")

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LauncherError(f"invalid {label}: {error}") from error


class _GuardianEventStream:
    """Incrementally validate the daemon's exact newline-delimited event stream."""

    def __init__(self, *, target_command_sha256: str, worker_mode: str) -> None:
        self.target_command_sha256 = target_command_sha256
        self.worker_mode = worker_mode
        self.buffer = bytearray()
        self.registered: dict[str, Any] | None = None
        self.release_marker: dict[str, Any] | None = None
        self.final_report: dict[str, Any] | None = None
        self.daemon_error: dict[str, Any] | None = None
        self.eof = False
        self.failure: LauncherError | None = None
        self._terminal_event: str | None = None

    def _fail(self, error: LauncherError | str) -> None:
        if self.failure is None:
            self.failure = (
                error if isinstance(error, LauncherError) else LauncherError(error)
            )

    def _consume(self, raw: bytes) -> None:
        try:
            event = _strict_json(raw, label="guardian event")
            if not isinstance(event, dict):
                raise LauncherError("guardian event is not an object")
            event_kind = event.get("event")
            if event_kind == "registered":
                if (
                    self.registered is not None
                    or self.release_marker is not None
                    or self._terminal_event is not None
                ):
                    raise LauncherError("guardian registration event replayed")
                self.registered = event
            elif event_kind == "worker_released":
                if (
                    self.registered is None
                    or self.release_marker is not None
                    or self._terminal_event is not None
                ):
                    raise LauncherError(
                        "worker release was not strictly after registration"
                    )
                if (
                    event.get("command_sha256") != self.target_command_sha256
                    or event.get("mode") != self.worker_mode
                    or type(event.get("pid")) is not int
                    or type(event.get("pgid")) is not int
                    or event["pid"] <= 1
                    or event["pgid"] <= 1
                ):
                    raise LauncherError("worker release marker is malformed")
                self.release_marker = event
            elif event_kind == "final":
                if self.registered is None:
                    raise LauncherError("guardian final event preceded registration")
                if self._terminal_event is not None:
                    raise LauncherError("guardian terminal event replayed")
                report = event.get("report")
                if not isinstance(report, dict):
                    raise LauncherError("guardian final event is malformed")
                self.final_report = report
                self._terminal_event = event_kind
            elif event_kind == "daemon_error":
                if self._terminal_event is not None:
                    raise LauncherError("guardian terminal event replayed")
                self.daemon_error = event
                self._terminal_event = event_kind
            else:
                raise LauncherError("guardian emitted an unknown event")
        except LauncherError as error:
            self._fail(error)

    def drain_available(self, descriptor: int) -> None:
        """Drain every currently available byte and distinguish EAGAIN from EOF."""

        if self.eof:
            return
        while True:
            try:
                chunk = os.read(descriptor, 65_536)
            except BlockingIOError:
                return
            if not chunk:
                self.eof = True
                if self.buffer and self.failure is None:
                    self._fail(
                        "guardian event stream ended with a partial frame"
                    )
                self.buffer.clear()
                return
            if self.failure is not None:
                continue
            self.buffer.extend(chunk)
            while b"\n" in self.buffer:
                raw, _, remainder = self.buffer.partition(b"\n")
                self.buffer = bytearray(remainder)
                if len(raw) + 1 > _MAX_EVENT_BYTES:
                    self._fail("guardian event frame exceeded its byte bound")
                    self.buffer.clear()
                    break
                self._consume(raw)
                if self.failure is not None:
                    self.buffer.clear()
                    break
            if len(self.buffer) > _MAX_EVENT_BYTES:
                self._fail("guardian event frame exceeded its byte bound")
                self.buffer.clear()

    def require_clean_eof(self) -> None:
        if self.failure is not None:
            raise self.failure
        if not self.eof:
            raise LauncherError("guardian event stream did not reach EOF")


def _drain_guardian_events_after_exit(
    descriptor: int,
    events: _GuardianEventStream,
) -> None:
    """Boundedly drain bytes queued before process exit through the true pipe EOF."""

    deadline = time.monotonic() + _DAEMON_EVENT_EOF_TIMEOUT_SECONDS
    while not events.eof:
        events.drain_available(descriptor)
        if events.eof:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            events._fail("guardian event stream did not reach EOF after daemon exit")
            return
        readable, _, _ = select.select(
            [descriptor],
            [],
            [],
            min(_OWNER_MONITOR_INTERVAL_SECONDS, remaining),
        )
        if not readable:
            continue


class PinnedAdapterClient:
    def __init__(self, adapter: PinnedSource, database_path: Path) -> None:
        self.adapter = adapter
        self.database_path = Path(os.path.abspath(os.fspath(database_path)))

    def invoke(
        self,
        command: str,
        payload: Mapping[str, object] | None,
        *,
        token: str | None,
        token_domain: str | None = None,
        extra_fds: Sequence[int] = (),
    ) -> dict[str, Any]:
        self.adapter.attest_unchanged()
        inherited_fds = [self.adapter.descriptor]
        for descriptor in extra_fds:
            if (
                type(descriptor) is not int
                or descriptor <= 2
                or descriptor in inherited_fds
            ):
                raise LauncherError("host extra FD closure is invalid")
            os.fstat(descriptor)
            inherited_fds.append(descriptor)
        token_fd = -1
        argv = [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _PINNED_SCRIPT_LOADER,
            str(self.adapter.descriptor),
            str(self.adapter.size),
            self.adapter.sha256,
            str(self.adapter.path),
        ]
        if token is not None:
            if token_domain not in {"owner", "guardian", "runner"}:
                raise LauncherError("scoped host capability domain is invalid")
            token_fd = _fresh_token_pipe(token)
            if token_fd in inherited_fds:
                raise LauncherError("host token FD aliases a source FD")
            inherited_fds.append(token_fd)
            argv.extend(
                [
                    "--control-token-fd",
                    str(token_fd),
                    "--control-token-domain",
                    token_domain,
                ]
            )
        elif token_domain is not None:
            raise LauncherError("host capability domain lacks a token pipe")
        argv.extend(["--db", str(self.database_path), command])
        environment = _pinned_adapter_environment()
        input_bytes = None
        if payload is not None:
            envelope = {
                "schema_version": GUARDIAN_CONTROL_SCHEMA,
                "command": command.replace("-", "_"),
                "payload": dict(payload),
            }
            input_bytes = canonical_json(envelope)
        try:
            completed = subprocess.run(
                argv,
                input=input_bytes,
                capture_output=True,
                check=False,
                env=environment,
                pass_fds=tuple(inherited_fds),
            )
        finally:
            if token_fd >= 0:
                os.close(token_fd)
        if (
            len(completed.stdout) > _MAX_HOST_STDOUT_BYTES
            or len(completed.stderr) > _MAX_HOST_STDERR_BYTES
        ):
            raise LauncherError(f"host {command} exceeded its output bound")
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace")[:4096]
            raise LauncherError(
                f"host {command} failed rc={completed.returncode}: {detail}"
            )
        value = _strict_json(completed.stdout, label=f"host {command} response")
        if not isinstance(value, dict):
            raise LauncherError(f"host {command} response is not an object")
        return value

    def policy_contract(self) -> dict[str, Any]:
        return self.invoke("policy-contract", None, token=None)


def _pinned_adapter_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Carry the owner-resolved cost manifest across the isolated host hop."""

    source_environment = os.environ if source is None else source
    environment = {
        "LANG": source_environment.get("LANG", "C.UTF-8"),
        "LC_ALL": source_environment.get("LC_ALL", "C.UTF-8"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    manifest = source_environment.get("RETHLAS_RESOLVED_COST_POLICY_JSON")
    digest = source_environment.get("RETHLAS_RESOLVED_COST_POLICY_SHA256")
    if (manifest is None) != (digest is None):
        raise LauncherError("resolved cost policy environment is incomplete")
    for key, value in (
        ("RETHLAS_RESOLVED_COST_POLICY_JSON", manifest),
        ("RETHLAS_RESOLVED_COST_POLICY_SHA256", digest),
    ):
        if value is None:
            continue
        if "\0" in value:
            raise LauncherError("resolved cost policy environment is invalid")
        environment[key] = value
    return environment


def _load_guardian(source: PinnedSource) -> types.ModuleType:
    module_name = "rethlas_pinned_production_guardian"
    module = types.ModuleType(module_name)
    module.__file__ = str(source.path)
    module.__package__ = None
    module.__spec__ = None
    sys.modules[module_name] = module
    try:
        exec(
            compile(source.content, str(source.path), "exec", dont_inherit=True),
            module.__dict__,
            module.__dict__,
        )
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def build_launch_manifest(
    *,
    launcher_sha256: str,
    guardian_sha256: str,
    runner_sha256: str,
    adapter_sha256: str,
    guardian_control_schema_sha256: str,
    worker_command_sha256: str,
    worker_runtime_command_sha256: str,
    worker_executable_sha256: str,
    worker_environment_sha256: str,
    guardian_mode: str,
    worker_mode: str,
    worker_cwd: str,
    problem_relative_path: str,
    problem_sha256: str,
    handoff_candidate_sha256: str | None,
) -> dict[str, object]:
    if guardian_mode not in {"hard_stop", "monitor_only"}:
        raise LauncherError("launcher manifest guardian mode is invalid")
    manifest = {
        "schema_version": LAUNCH_MANIFEST_SCHEMA,
        "launcher_relative_path": LAUNCHER_RELATIVE_PATH,
        "launcher_sha256": launcher_sha256,
        "guardian_relative_path": GUARDIAN_RELATIVE_PATH,
        "guardian_sha256": guardian_sha256,
        "runner_relative_path": RUNNER_RELATIVE_PATH,
        "runner_sha256": runner_sha256,
        "adapter_relative_path": ADAPTER_RELATIVE_PATH,
        "adapter_sha256": adapter_sha256,
        "guardian_control_schema_sha256": guardian_control_schema_sha256,
        "guardian_mode": guardian_mode,
        "worker_command_sha256": worker_command_sha256,
        "worker_runtime_command_sha256": worker_runtime_command_sha256,
        "worker_executable_sha256": worker_executable_sha256,
        "worker_environment_sha256": worker_environment_sha256,
        "worker_mode": worker_mode,
        "worker_cwd": worker_cwd,
        "problem_relative_path": problem_relative_path,
        "problem_sha256": problem_sha256,
        "handoff_candidate_sha256": handoff_candidate_sha256,
    }
    if set(manifest) != LAUNCH_MANIFEST_KEYS:
        raise AssertionError("launcher manifest implementation drifted from its schema")
    if any(
        SHA256_RE.fullmatch(str(value)) is None
        for key, value in manifest.items()
        if key.endswith("_sha256")
        and not (key == "handoff_candidate_sha256" and value is None)
    ):
        raise LauncherError("launcher manifest contains a malformed digest")
    return manifest


def _validate_policy_and_manifest(
    contract: Mapping[str, object],
    *,
    expected_contract_sha256: str,
    manifest: Mapping[str, object],
) -> str:
    if (
        set(contract)
        != {
            "schema_version",
            "review_cadence_policy",
            "continuous_supervisor_policy",
            "context_guard_policy",
            "contract_sha256",
        }
        or contract.get("schema_version") != "rethlas-policy-contract-v1"
    ):
        raise LauncherError("policy contract top-level shape is invalid")
    claimed = contract.get("contract_sha256")
    if claimed != expected_contract_sha256 or SHA256_RE.fullmatch(str(claimed)) is None:
        raise LauncherError("policy contract digest differs from the runner pin")
    material = dict(contract)
    material.pop("contract_sha256")
    if canonical_sha256(material) != claimed:
        raise LauncherError("policy contract digest is not canonical")
    policy = contract.get(
        "continuous_supervisor_policy"
        if manifest.get("guardian_mode") == "monitor_only"
        else "review_cadence_policy"
    )
    if not isinstance(policy, dict):
        raise LauncherError("review cadence policy is absent")
    policy_sha256 = policy.get("policy_sha256")
    policy_material = dict(policy)
    policy_material.pop("policy_sha256", None)
    if (
        SHA256_RE.fullmatch(str(policy_sha256)) is None
        or canonical_sha256(policy_material) != policy_sha256
    ):
        raise LauncherError("review cadence policy digest is invalid")
    if policy.get("guardian_enforcement_ready") is not True:
        raise LauncherError("guardian enforcement is not released")
    if policy.get("clock") not in {
        "earliest_durable_wall_and_same_boot_monotonic",
        "global_rolling_wall_and_same_boot_monotonic",
    }:
        raise LauncherError("guardian clock policy is not an approved dual clock")
    if manifest.get("guardian_mode") != policy.get("guardian_mode", "hard_stop"):
        raise LauncherError("guardian mode differs from its policy")
    expected = {
        "approved_guardian_launcher_sha256": manifest["launcher_sha256"],
        "approved_guardian_sha256": manifest["guardian_sha256"],
        "approved_guardian_runner_sha256": manifest["runner_sha256"],
        "guardian_control_schema_sha256": manifest["guardian_control_schema_sha256"],
        "guardian_launch_manifest_schema_sha256": (LAUNCH_MANIFEST_SCHEMA_SHA256),
    }
    for key, value in expected.items():
        if policy.get(key) != value:
            raise LauncherError(f"review policy source/schema pin mismatch: {key}")
    return str(policy_sha256)


_WORKER_RELEASE_BOOTSTRAP = r"""
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time

mode = sys.argv[1]
descriptor = int(sys.argv[2])
event_fd = int(sys.argv[3])
target_sha256 = sys.argv[4]
target = sys.argv[5:]
if (
    mode not in {"runner_control", "opaque_guarded_command"}
    or descriptor <= 2
    or event_fd <= 2
    or not stat.S_ISFIFO(os.fstat(descriptor).st_mode)
    or re.fullmatch(r"[0-9a-f]{64}", target_sha256) is None
):
    os._exit(126)
flags = fcntl.fcntl(descriptor, fcntl.F_GETFD)
fcntl.fcntl(descriptor, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
if mode == "opaque_guarded_command":
    raw = bytearray()
    while len(raw) < 64:
        chunk = os.read(descriptor, 64 - len(raw))
        if not chunk:
            break
        raw.extend(chunk)
    trailing = os.read(descriptor, 1)
    os.close(descriptor)
    if len(raw) != 64 or trailing != b"" or re.fullmatch(b"[0-9a-f]{64}", raw) is None:
        os._exit(126)
    for index in range(len(raw)):
        raw[index] = 0
environment = dict(os.environ)
for name in (
    "RETHLAS_REVIEW_CONTROL_TOKEN",
    "RETHLAS_GUARDIAN_CYCLE_TOKEN",
    "RETHLAS_RUNNER_CYCLE_TOKEN",
    "RETHLAS_STALE_RECOVERY_TOKEN",
):
    environment.pop(name, None)
if not target or not os.path.isabs(target[0]):
    os._exit(126)
marker = {
    "command_sha256": target_sha256,
    "event": "worker_released",
    "mode": mode,
    "monotonic": time.monotonic(),
    "pgid": os.getpgrp(),
    "pid": os.getpid(),
    "wall_epoch": time.time(),
}
encoded = (json.dumps(
    marker,
    allow_nan=False,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
) + "\n").encode("utf-8")
view = memoryview(encoded)
while view:
    written = os.write(event_fd, view)
    if written <= 0:
        os._exit(126)
    view = view[written:]
os.close(event_fd)
if mode == "runner_control":
    target.extend(["--runner-token-fd", str(descriptor)])
os.execve(target[0], target, environment)
"""


def _identity(guardian: types.ModuleType, value: Mapping[str, object]) -> object:
    if set(value) != {"pid", "uid", "pgid", "start_marker"}:
        raise LauncherError("host process identity shape is invalid")
    return guardian.ProcessIdentity(
        pid=value["pid"],
        uid=value["uid"],
        pgid=value["pgid"],
        start_marker=value["start_marker"],
    )


def _paid_group(guardian: types.ModuleType, value: Mapping[str, object]) -> object:
    if set(value) != {"role", "identity"} or not isinstance(
        value.get("identity"), dict
    ):
        raise LauncherError("host paid-group shape is invalid")
    return guardian.PaidGroup(value["role"], _identity(guardian, value["identity"]))


def _emit_event(descriptor: int, value: Mapping[str, object]) -> None:
    raw = canonical_json(dict(value)) + b"\n"
    if len(raw) > _MAX_EVENT_BYTES:
        raise LauncherError("guardian event exceeded its byte bound")
    view = memoryview(raw)
    while view:
        try:
            written = os.write(descriptor, view)
        except BrokenPipeError:
            return
        if written <= 0:
            raise LauncherError("guardian event pipe write was short")
        view = view[written:]


class ProductionGuardianCallbacks:
    def __init__(
        self,
        guardian: types.ModuleType,
        host: PinnedAdapterClient,
        *,
        launch_intent_sha256: str,
        guardian_token: str,
        event_fd: int,
    ) -> None:
        self.guardian = guardian
        self.host = host
        self.launch_intent_sha256 = launch_intent_sha256
        self.guardian_token = guardian_token
        self.event_fd = event_fd
        self.inspector = guardian.SystemProcessInspector()
        self.request_sha256: str | None = None
        self.registration_id: str | None = None
        self.previous_snapshot_sha256: str | None = None

    def _host(self, command: str, payload: Mapping[str, object]) -> dict[str, Any]:
        return self.host.invoke(
            command,
            payload,
            token=self.guardian_token,
            token_domain="guardian",
        )

    def register(self, request: object) -> object:
        daemon = self.inspector.identity(os.getpid())
        if daemon is None or daemon.pid != daemon.pgid or os.getsid(0) != os.getpid():
            raise LauncherError("guardian daemon is not its stable session leader")
        result = self._host(
            "guardian-register",
            {
                "daemon_identity": daemon.as_dict(),
                "launch_intent_sha256": self.launch_intent_sha256,
                "request": request.as_dict(),
            },
        )
        value = result.get("registration_ack")
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "registration_id",
                "request_sha256",
                "durable",
                "release_authorized",
                "projection",
            }
            or not isinstance(value.get("projection"), dict)
        ):
            raise LauncherError("host registration acknowledgement is malformed")
        projection = self.guardian.DeadlineProjection(**value["projection"])
        acknowledgement = self.guardian.RegistrationAck(
            registration_id=value["registration_id"],
            request_sha256=value["request_sha256"],
            durable=value["durable"],
            release_authorized=value["release_authorized"],
            projection=projection,
        )
        self.request_sha256 = request.request_sha256
        self.registration_id = acknowledgement.registration_id
        _emit_event(
            self.event_fd,
            {
                "event": "registered",
                "registration_ack": {
                    "registration_id": acknowledgement.registration_id,
                    "request_sha256": acknowledgement.request_sha256,
                    "projection": projection.as_dict(),
                },
            },
        )
        return acknowledgement

    def poll(
        self,
        registration_id: str,
        discovered_groups: Sequence[object] = (),
    ) -> object:
        if self.request_sha256 is None:
            raise LauncherError("guardian poll preceded registration")
        discovered = [item.as_dict() for item in discovered_groups]
        if discovered != sorted(
            discovered, key=lambda item: item["identity"]["pgid"]
        ) or len({item["identity"]["pgid"] for item in discovered}) != len(
            discovered
        ):
            raise LauncherError("guardian discovered groups are not sorted and unique")
        poll_request = {
            "schema_version": "rethlas_guardian_poll_request_v1",
            "registration_id": registration_id,
            "request_sha256": self.request_sha256,
            "discovered_groups": discovered,
            "expected_previous_snapshot_sha256": self.previous_snapshot_sha256,
        }
        poll_request_sha256 = canonical_sha256(poll_request)
        result = self._host(
            "guardian-poll",
            {
                "registration_id": registration_id,
                "request_sha256": self.request_sha256,
                "discovered_groups": discovered,
                "expected_previous_snapshot_sha256": (
                    self.previous_snapshot_sha256
                ),
            },
        )
        value = result.get("snapshot")
        if (
            set(result)
            != {
                "schema_version",
                "snapshot",
                "snapshot_sha256",
                "poll_request_sha256",
            }
            or result.get("schema_version")
            != "rethlas_guardian_poll_result_v1"
            or result.get("poll_request_sha256") != poll_request_sha256
            or not isinstance(value, dict)
            or set(value)
            != {
                "sequence",
                "registration_id",
                "request_sha256",
                "boot_identity",
                "paid_groups",
            }
            or not isinstance(value.get("paid_groups"), list)
            or result.get("snapshot_sha256") != canonical_sha256(value)
        ):
            raise LauncherError("host poll snapshot is malformed")
        snapshot = self.guardian.PollSnapshot(
            sequence=value["sequence"],
            registration_id=value["registration_id"],
            request_sha256=value["request_sha256"],
            boot_identity=value["boot_identity"],
            paid_groups=tuple(
                _paid_group(self.guardian, item) for item in value["paid_groups"]
            ),
        )
        if snapshot.snapshot_sha256 != result["snapshot_sha256"]:
            raise LauncherError("host poll snapshot object changed during validation")
        self.previous_snapshot_sha256 = snapshot.snapshot_sha256
        return snapshot

    def internal_interrupt(self, registration_id: str, request_sha256: str) -> None:
        self._host(
            "guardian-internal-interrupt",
            {
                "registration_id": registration_id,
                "request_sha256": request_sha256,
            },
        )

    def lifeline_lost(self, registration_id: str, request_sha256: str) -> None:
        self._host(
            "guardian-lifeline-lost",
            {
                "registration_id": registration_id,
                "request_sha256": request_sha256,
            },
        )

    def finalize(self, report: object) -> None:
        value = report.as_dict()
        self._host(
            "guardian-finalize",
            {"report": value, "report_sha256": canonical_sha256(value)},
        )


@dataclass(frozen=True, slots=True)
class LaunchConfiguration:
    database_path: Path
    run_id: str
    generation_control_instance_id: str
    watchdog_id: str
    admission_mode: str
    expected_cycle_id: str
    expected_generation: int
    expected_clock_sha256: str | None
    capability_revision: int
    policy_contract_sha256: str
    policy_digest: str
    worker_cwd: Path
    worker_mode: str
    problem_relative_path: str
    worker_command: tuple[str, ...]
    guardian_mode: str = "hard_stop"


def _validate_configuration(configuration: LaunchConfiguration) -> None:
    if RUN_ID_RE.fullmatch(configuration.run_id) is None:
        raise LauncherError("run id is malformed")
    if WATCHDOG_ID_RE.fullmatch(configuration.watchdog_id) is None:
        raise LauncherError("watchdog id is malformed")
    if (
        GENERATION_CONTROL_ID_RE.fullmatch(configuration.generation_control_instance_id)
        is None
    ):
        raise LauncherError("generation-control instance id is malformed")
    if configuration.admission_mode not in {
        "initial_new_cycle",
        "next_new_cycle",
        "same_cycle_resume",
    }:
        raise LauncherError("guardian admission mode is invalid")
    if configuration.worker_mode not in {"runner_control", "opaque_guarded_command"}:
        raise LauncherError("guardian worker mode is invalid")
    if configuration.guardian_mode not in {"hard_stop", "monitor_only"}:
        raise LauncherError("guardian deadline mode is invalid")
    problem_relative = Path(configuration.problem_relative_path)
    if (
        problem_relative.is_absolute()
        or not configuration.problem_relative_path.startswith("data/")
        or problem_relative.suffix != ".md"
        or ".." in problem_relative.parts
    ):
        raise LauncherError(
            "guardian problem path must be a data-relative markdown file"
        )
    if (
        type(configuration.expected_generation) is not int
        or configuration.expected_generation < 1
        or type(configuration.capability_revision) is not int
        or configuration.capability_revision < 1
    ):
        raise LauncherError("guardian generation/capability revision is invalid")
    if configuration.admission_mode == "same_cycle_resume":
        if re.fullmatch(r"cycle_[0-9a-f]{32}", configuration.expected_cycle_id) is None:
            raise LauncherError("same-cycle resume requires an existing cycle id")
        if SHA256_RE.fullmatch(str(configuration.expected_clock_sha256)) is None:
            raise LauncherError("same-cycle resume requires the exact clock digest")
    else:
        expected_cycle_id = guardian_cycle_id(
            run_id=configuration.run_id,
            generation=configuration.expected_generation,
            watchdog_id=configuration.watchdog_id,
        )
        if configuration.expected_cycle_id != expected_cycle_id:
            raise LauncherError("new-cycle guardian cycle id is not canonical")
        if configuration.expected_clock_sha256 is not None:
            raise LauncherError("new-cycle launch cannot inherit a clock digest")
    if (
        SHA256_RE.fullmatch(configuration.policy_contract_sha256) is None
        or SHA256_RE.fullmatch(configuration.policy_digest) is None
    ):
        raise LauncherError("policy contract/review digest is malformed")
    if (
        not configuration.worker_command
        or not os.path.isabs(configuration.worker_command[0])
        or any(not item or "\0" in item for item in configuration.worker_command)
    ):
        raise LauncherError("worker command must be an absolute, nonempty argv")
    if (
        not configuration.worker_cwd.is_absolute()
        or not configuration.worker_cwd.is_dir()
    ):
        raise LauncherError("worker cwd must be an existing absolute directory")


def _worker_environment() -> dict[str, str]:
    result: dict[str, str] = {}
    for key in sorted(WORKER_ENVIRONMENT_ALLOWLIST):
        value = os.environ.get(key)
        if value is None:
            continue
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise LauncherError("worker environment contains an invalid entry")
        result[key] = value
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    return result


def _pinned_runner_control_command(
    configuration: LaunchConfiguration,
    adapter_source: PinnedSource,
    worker_adapter_fd: int,
) -> tuple[str, ...]:
    _runner_control_operation(configuration, adapter_path=adapter_source.path)
    requested = configuration.worker_command
    if worker_adapter_fd <= 2:
        raise LauncherError("runner-control adapter FD is invalid")
    metadata = os.fstat(worker_adapter_fd)
    if (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    ) != (
        adapter_source.device,
        adapter_source.inode,
        adapter_source.size,
        adapter_source.mtime_ns,
    ):
        raise LauncherError("runner-control adapter FD is not the pinned source")
    return (
        requested[0],
        "-I",
        "-B",
        "-c",
        _PINNED_SCRIPT_LOADER,
        str(worker_adapter_fd),
        str(adapter_source.size),
        adapter_source.sha256,
        str(adapter_source.path),
        *requested[2:],
    )


def _runner_control_operation(
    configuration: LaunchConfiguration,
    *,
    adapter_path: Path,
) -> str:
    requested = configuration.worker_command
    if not requested:
        raise LauncherError("runner-control target command is empty")
    adapter_prefix = (
        requested[0],
        str(adapter_path),
        "--db",
        str(configuration.database_path),
    )
    is_generator = (
        len(requested) > len(adapter_prefix)
        and requested[: len(adapter_prefix)] == adapter_prefix
        and requested[len(adapter_prefix)] == "run-generator"
    )
    is_guarded_review = bool(
        len(requested) == len(adapter_prefix) + 5
        and requested[: len(adapter_prefix)] == adapter_prefix
        and requested[len(adapter_prefix) : -1]
        == (
            "guarded-review-drive",
            "--run-id",
            configuration.run_id,
            "--boundary-id",
        )
        and re.fullmatch(r"reviewbound_[0-9a-f]{32}", requested[-1]) is not None
    )
    if (
        not (is_generator or is_guarded_review)
        or "--runner-token-fd" in requested
    ):
        raise LauncherError(
            "runner-control target must be an exact pinned adapter run-generator "
            "or guarded-review-drive command without a runner token option"
        )
    return "run-generator" if is_generator else "guarded-review-drive"


def _durably_attest_discovered_groups(
    configuration: LaunchConfiguration,
    host: PinnedAdapterClient,
    *,
    worker_adapter_fd: int | None,
    runner_token_fd: int,
    blocked_command: Sequence[str],
) -> bool:
    if configuration.worker_mode == "opaque_guarded_command":
        return True
    if configuration.worker_mode != "runner_control" or worker_adapter_fd is None:
        raise LauncherError("runner-control daemon lacks its pinned adapter FD")
    exact_target = _pinned_runner_control_command(
        configuration,
        host.adapter,
        worker_adapter_fd,
    )
    expected_blocked_command = (
        *exact_target,
        "--runner-token-fd",
        str(runner_token_fd),
    )
    if tuple(blocked_command) != expected_blocked_command:
        raise LauncherError(
            "runner-control daemon command differs from its pinned launch closure"
        )
    operation = _runner_control_operation(
        configuration,
        adapter_path=host.adapter.path,
    )
    return operation == "run-generator"


def _daemon_main(
    guardian: types.ModuleType,
    host: PinnedAdapterClient,
    *,
    configuration: LaunchConfiguration,
    launch_intent_sha256: str,
    guardian_token: str,
    runner_token_fd: int,
    worker_adapter_fd: int | None,
    release_event_fd: int | None,
    lifeline_fd: int,
    event_fd: int,
    blocked_command: Sequence[str],
    worker_environment: Mapping[str, str],
) -> int:
    try:
        durably_attest_discovered_groups = _durably_attest_discovered_groups(
            configuration,
            host,
            worker_adapter_fd=worker_adapter_fd,
            runner_token_fd=runner_token_fd,
            blocked_command=blocked_command,
        )
        if os.getsid(0) != os.getpid():
            os.setsid()
        os.chdir(configuration.worker_cwd)
        host_environment = _pinned_adapter_environment(worker_environment)
        os.environ.clear()
        os.environ.update(host_environment)
        callbacks = ProductionGuardianCallbacks(
            guardian,
            host,
            launch_intent_sha256=launch_intent_sha256,
            guardian_token=guardian_token,
            event_fd=event_fd,
        )
        report = guardian.Guardian(
            callbacks,
            inspector=callbacks.inspector,
            poll_interval=0.05,
            durably_attest_discovered_groups=durably_attest_discovered_groups,
        ).run(
            blocked_command,
            run_id=configuration.run_id,
            generation_control_instance_id=(
                configuration.generation_control_instance_id
            ),
            watchdog_id=configuration.watchdog_id,
            policy_digest=configuration.policy_digest,
            lifeline_fd=lifeline_fd,
            monitor_only=configuration.guardian_mode == "monitor_only",
            env=worker_environment,
            pass_fds=(
                (
                    runner_token_fd,
                    *((worker_adapter_fd,) if worker_adapter_fd is not None else ()),
                    *((release_event_fd,) if release_event_fd is not None else ()),
                )
            ),
        )
        value = report.as_dict()
        _emit_event(event_fd, {"event": "final", "report": value})
        return (
            0
            if (value["state"] == "completed" and value["direct_returncode"] == 0)
            else 70
        )
    except BaseException as error:
        _emit_event(
            event_fd,
            {
                "event": "daemon_error",
                "error_type": type(error).__name__,
                "error": str(error)[:4096],
            },
        )
        return 70
    finally:
        for descriptor in (
            runner_token_fd,
            -1 if worker_adapter_fd is None else worker_adapter_fd,
            -1 if release_event_fd is None else release_event_fd,
            lifeline_fd,
            event_fd,
        ):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _internal_daemon_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config-fd", type=int, required=True)
    parser.add_argument("--guardian-token-fd", type=int, required=True)
    parser.add_argument("--runner-token-fd", type=int, required=True)
    parser.add_argument("--worker-adapter-fd", type=int, required=True)
    parser.add_argument("--lifeline-fd", type=int, required=True)
    parser.add_argument("--event-fd", type=int, required=True)
    parser.add_argument("--release-event-fd", type=int, required=True)
    parser.add_argument("--guardian-source-fd", type=int, required=True)
    parser.add_argument("--guardian-source-path", required=True)
    parser.add_argument("--guardian-source-sha256", required=True)
    parser.add_argument("--adapter-source-fd", type=int, required=True)
    parser.add_argument("--adapter-source-path", required=True)
    parser.add_argument("--adapter-source-sha256", required=True)
    args = parser.parse_args(argv)
    if any(name in os.environ for name in PRIVILEGED_ENVIRONMENT):
        raise LauncherError("detached guardian inherited a privileged environment key")
    raw = _read_bounded_fifo(
        args.config_fd,
        maximum=2 * 1024 * 1024,
        label="guardian daemon configuration",
    )
    value = _strict_json(raw, label="guardian daemon configuration")
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "configuration",
            "launch_intent_sha256",
            "blocked_command",
            "worker_environment",
        }
        or value.get("schema_version") != "rethlas_guardian_daemon_config_v1"
    ):
        raise LauncherError("guardian daemon configuration shape is invalid")
    configuration_value = value["configuration"]
    if not isinstance(configuration_value, dict) or set(configuration_value) != {
        "database_path",
        "run_id",
        "generation_control_instance_id",
        "watchdog_id",
        "admission_mode",
        "expected_cycle_id",
        "expected_generation",
        "expected_clock_sha256",
        "capability_revision",
            "policy_contract_sha256",
            "policy_digest",
            "guardian_mode",
            "worker_cwd",
        "worker_mode",
        "problem_relative_path",
        "worker_command",
    }:
        raise LauncherError("guardian daemon launch configuration is malformed")
    blocked_command = value["blocked_command"]
    worker_environment = value["worker_environment"]
    if (
        not isinstance(blocked_command, list)
        or not all(isinstance(item, str) and item for item in blocked_command)
        or not isinstance(worker_environment, dict)
        or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in worker_environment.items()
        )
    ):
        raise LauncherError("guardian daemon worker closure is malformed")
    configuration = LaunchConfiguration(
        database_path=Path(str(configuration_value["database_path"])),
        run_id=str(configuration_value["run_id"]),
        generation_control_instance_id=str(
            configuration_value["generation_control_instance_id"]
        ),
        watchdog_id=str(configuration_value["watchdog_id"]),
        admission_mode=str(configuration_value["admission_mode"]),
        expected_cycle_id=str(configuration_value["expected_cycle_id"]),
        expected_generation=configuration_value["expected_generation"],
        expected_clock_sha256=configuration_value["expected_clock_sha256"],
        capability_revision=configuration_value["capability_revision"],
        policy_contract_sha256=str(configuration_value["policy_contract_sha256"]),
        policy_digest=str(configuration_value["policy_digest"]),
        guardian_mode=str(configuration_value["guardian_mode"]),
        worker_cwd=Path(str(configuration_value["worker_cwd"])),
        worker_mode=str(configuration_value["worker_mode"]),
        problem_relative_path=str(configuration_value["problem_relative_path"]),
        worker_command=tuple(configuration_value["worker_command"]),
    )
    _validate_configuration(configuration)
    launcher_fd = globals().pop("__rethlas_pinned_launcher_fd__", None)
    launcher_path = globals().pop("__rethlas_pinned_launcher_path__", None)
    launcher_sha256 = globals().pop("__rethlas_pinned_launcher_sha256__", None)
    if (
        type(launcher_fd) is not int
        or not isinstance(launcher_path, str)
        or not isinstance(launcher_sha256, str)
    ):
        raise LauncherError("guardian daemon lacks its pinned launcher entry")
    sources = [
        PinnedSource.adopt(
            launcher_fd,
            Path(launcher_path),
            expected_sha256=launcher_sha256,
        ),
        PinnedSource.adopt(
            args.guardian_source_fd,
            Path(args.guardian_source_path),
            expected_sha256=args.guardian_source_sha256,
        ),
        PinnedSource.adopt(
            args.adapter_source_fd,
            Path(args.adapter_source_path),
            expected_sha256=args.adapter_source_sha256,
        ),
    ]
    try:
        guardian_token = consume_token_fd(args.guardian_token_fd, label="guardian")
        guardian = _load_guardian(sources[1])
        host = PinnedAdapterClient(sources[2], configuration.database_path)
        return _daemon_main(
            guardian,
            host,
            configuration=configuration,
            launch_intent_sha256=str(value["launch_intent_sha256"]),
            guardian_token=guardian_token,
            runner_token_fd=args.runner_token_fd,
            worker_adapter_fd=(
                args.worker_adapter_fd if args.worker_adapter_fd >= 0 else None
            ),
            release_event_fd=(
                args.release_event_fd if args.release_event_fd >= 0 else None
            ),
            lifeline_fd=args.lifeline_fd,
            event_fd=args.event_fd,
            blocked_command=tuple(blocked_command),
            worker_environment=worker_environment,
        )
    finally:
        for source in sources:
            source.close()


def _status(
    host: PinnedAdapterClient,
    configuration: LaunchConfiguration,
    owner_token: str,
) -> dict[str, Any]:
    return host.invoke(
        "guardian-status",
        {"run_id": configuration.run_id, "watchdog_id": configuration.watchdog_id},
        token=owner_token,
        token_domain="owner",
    )


def _status_has_durable_terminal_report(status: Mapping[str, object]) -> bool:
    """Treat the content-addressed terminal row as authoritative over state lag."""

    terminal_report = status.get("terminal_report")
    return (
        isinstance(terminal_report, dict)
        or isinstance(status.get("offline_finalize"), dict)
    )


def _status_has_durable_registration(status: Mapping[str, object]) -> bool:
    registration_id = status.get("registration_id")
    if registration_id is None:
        return False
    if not isinstance(registration_id, str) or not registration_id:
        raise LauncherError("guardian status registration id is malformed")
    return True


def _event_registration_binding(
    events: _GuardianEventStream,
) -> tuple[str, str] | None:
    registered = events.registered
    if registered is None:
        return None
    acknowledgement = registered.get("registration_ack")
    if (
        not isinstance(acknowledgement, dict)
        or set(acknowledgement)
        != {"registration_id", "request_sha256", "projection"}
    ):
        raise LauncherError("guardian registration event acknowledgement is malformed")
    registration_id = acknowledgement.get("registration_id")
    request_sha256 = acknowledgement.get("request_sha256")
    projection = acknowledgement.get("projection")
    projection_keys = {
        "cycle_started_wall_epoch",
        "cycle_started_monotonic",
        "internal_interrupt_wall_epoch",
        "internal_interrupt_monotonic",
        "hard_stop_wall_epoch",
        "hard_stop_monotonic",
        "projected_wall_epoch",
        "projected_monotonic",
        "boot_identity",
    }
    if (
        not isinstance(registration_id, str)
        or not registration_id
        or SHA256_RE.fullmatch(str(request_sha256)) is None
        or not isinstance(projection, dict)
        or set(projection) != projection_keys
        or not isinstance(projection.get("boot_identity"), str)
        or not projection["boot_identity"]
        or any(
            type(projection.get(key)) not in {int, float}
            or not math.isfinite(float(projection[key]))
            for key in projection_keys - {"boot_identity"}
        )
    ):
        raise LauncherError("guardian registration event binding is malformed")
    return registration_id, str(request_sha256)


def _validate_registration_status_binding(
    events: _GuardianEventStream,
    status: Mapping[str, object],
    *,
    required: bool,
) -> None:
    binding = _event_registration_binding(events)
    if binding is None:
        if required:
            raise LauncherError("terminal guardian run lacks its registration event")
        return
    if (
        status.get("registration_id") != binding[0]
        or status.get("request_sha256") != binding[1]
    ):
        raise LauncherError("guardian event registration differs from durable status")


def _wait_and_drain_guardian_after_cleanup(
    daemon_process: subprocess.Popen[bytes],
    descriptor: int,
    events: _GuardianEventStream,
) -> int:
    """After offline cleanup kills/reaps the daemon, drain its final pipe bytes."""

    try:
        child_status = daemon_process.wait(
            timeout=_DAEMON_EVENT_EOF_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as error:
        # Preserve any queued evidence before reporting that cleanup did not
        # finish the exact child within the bounded observation window.
        _drain_guardian_events_after_exit(descriptor, events)
        raise LauncherError(
            "guardian daemon did not terminate after offline cleanup"
        ) from error
    _drain_guardian_events_after_exit(descriptor, events)
    events.require_clean_eof()
    return child_status


def _validate_offline_event_stream(
    events: _GuardianEventStream,
    status: Mapping[str, object],
    offline: Mapping[str, object],
) -> None:
    events.require_clean_eof()
    _validate_registration_status_binding(events, status, required=False)
    durable_registration = status.get("registration_id")
    if (
        not isinstance(durable_registration, str)
        or not durable_registration
        or offline.get("registration_id") != durable_registration
    ):
        raise LauncherError("offline terminal receipt differs from durable registration")
    if events.daemon_error is not None and events.registered is not None:
        # A post-registration daemon error is valid evidence explaining why
        # offline cleanup was needed; the cleanup receipt stays authoritative.
        return


def _finalize_offline_event_stream(
    daemon_process: subprocess.Popen[bytes],
    descriptor: int,
    events: _GuardianEventStream,
    status: Mapping[str, object],
    offline: Mapping[str, object],
    *,
    wait_for_daemon: bool,
) -> int | None:
    """Validate pipe evidence only after the durable offline cleanup completes."""

    child_status = (
        _wait_and_drain_guardian_after_cleanup(daemon_process, descriptor, events)
        if wait_for_daemon
        else daemon_process.returncode
    )
    _validate_offline_event_stream(events, status, offline)
    return child_status


def _validated_offline_finalize_status(
    status: Mapping[str, object],
) -> dict[str, object] | None:
    value = status.get("offline_finalize")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise LauncherError("durable offline finalize status is malformed")
    expected_keys = {
        "schema_version",
        "operation_id",
        "manifest_sha256",
        "registration_id",
        "report_sha256",
        "state",
        "capture_sealed",
        "coverage_complete",
        "all_empty_verified",
        "terminal_sequence",
        "receipt_sha256",
    }
    seed = dict(value)
    receipt_sha256 = seed.pop("receipt_sha256", None)
    if (
        set(value) != expected_keys
        or value.get("schema_version")
        != "rethlas_guardian_offline_finalize_v1"
        or value.get("state") not in {"watchdog_forced", "execution_unknown"}
        or type(value.get("capture_sealed")) is not bool
        or type(value.get("coverage_complete")) is not bool
        or type(value.get("all_empty_verified")) is not bool
        or value.get("state") == "watchdog_forced"
        and not (
            value.get("capture_sealed") is True
            and value.get("coverage_complete") is True
            and value.get("all_empty_verified") is True
        )
        or SHA256_RE.fullmatch(str(value.get("manifest_sha256"))) is None
        or SHA256_RE.fullmatch(str(value.get("report_sha256"))) is None
        or type(value.get("terminal_sequence")) is not int
        or value["terminal_sequence"] < 0
        or canonical_sha256(seed) != receipt_sha256
    ):
        raise LauncherError("durable offline finalize receipt is invalid")
    return value


def _terminal_reports_reconcile(
    daemon_report: Mapping[str, object], durable_report: Mapping[str, object]
) -> bool:
    if dict(daemon_report) == dict(durable_report):
        return True
    # A finalize response can be lost after the host durably committed the
    # original report.  Guardian conservatively emits execution_unknown, but
    # the subsequent owner status read is authoritative when every immutable
    # report field still matches.
    if (
        daemon_report.get("state") != "execution_unknown"
        or not str(daemon_report.get("reason", "")).startswith(
            "finalize_response_unknown:"
        )
    ):
        return False
    ignored = {"state", "reason"}
    return {
        key: value for key, value in daemon_report.items() if key not in ignored
    } == {
        key: value for key, value in durable_report.items() if key not in ignored
    }


def _validate_terminal_event_stream(
    events: _GuardianEventStream,
    status: Mapping[str, object],
    terminal_report: Mapping[str, object],
    *,
    worker_mode: str,
    final_event_required: bool = True,
) -> None:
    events.require_clean_eof()
    if events.daemon_error is not None:
        raise LauncherError(
            "guardian daemon reported an error after terminalization: "
            + str(events.daemon_error.get("error_type"))
        )
    _validate_registration_status_binding(events, status, required=True)
    binding = _event_registration_binding(events)
    assert binding is not None
    if (
        terminal_report.get("registration_id") != binding[0]
        or terminal_report.get("request_sha256") != binding[1]
    ):
        raise LauncherError("terminal guardian report differs from registration event")
    if worker_mode == "opaque_guarded_command" and events.release_marker is None:
        raise LauncherError("terminal guardian run lacks its post-release marker")
    if final_event_required and events.final_report is None:
        raise LauncherError("terminal guardian run lacks its final event")
    if events.final_report is not None and not _terminal_reports_reconcile(
        events.final_report, terminal_report
    ):
        raise LauncherError("daemon final event differs from durable host status")


def _offline_stop(
    guardian: types.ModuleType,
    host: PinnedAdapterClient,
    configuration: LaunchConfiguration,
    *,
    owner_token: str,
    status: Mapping[str, object],
    daemon_pid: int,
) -> dict[str, Any]:
    cycle_id = status.get("cycle_id")
    clock_sha256 = status.get("clock_sha256")
    if not isinstance(cycle_id, str) or SHA256_RE.fullmatch(str(clock_sha256)) is None:
        raise LauncherError("offline stop lacks an exact active cycle clock")
    operation_id = (
        "offline_"
        + canonical_sha256(
            {
                "schema_version": "rethlas_guardian_offline_operation_id_v1",
                "run_id": configuration.run_id,
                "watchdog_id": configuration.watchdog_id,
                "cycle_id": cycle_id,
                "clock_sha256": clock_sha256,
            }
        )[:48]
    )
    manifest = host.invoke(
        "guardian-offline-stop",
        {
            "run_id": configuration.run_id,
            "cycle_id": cycle_id,
            "expected_clock_sha256": clock_sha256,
            "operation_id": operation_id,
        },
        token=owner_token,
        token_domain="owner",
    )
    expected_keys = {
        "schema_version",
        "operation_id",
        "run_id",
        "cycle_id",
        "registration_id",
        "request_sha256",
        "expected_clock_sha256",
        "state",
        "hard_stop_wall_epoch",
        "hard_stop_monotonic",
        "boot_identity",
        "observed_boot_identity",
        "daemon_identity",
        "groups",
        "proven_empty_groups",
        "capture_round",
        "capture_sealed",
        "previous_cleanup_manifest_sha256",
        "manifest_sha256",
    }

    def validate_cleanup_manifest(
        value: Mapping[str, object],
        *,
        expected_previous: str | None,
        expected_round: int,
    ) -> tuple[list[object], list[object], str]:
        if (
            set(value) != expected_keys
            or value.get("schema_version")
            != "rethlas_guardian_offline_stop_v1"
            or value.get("operation_id") != operation_id
            or value.get("run_id") != configuration.run_id
            or value.get("cycle_id") != cycle_id
            or value.get("expected_clock_sha256") != clock_sha256
            or value.get("state")
            not in {
                "already_empty",
                "stop_required",
                "reboot_proven_terminal",
            }
            or type(value.get("capture_round")) is not int
            or value.get("capture_round") != expected_round
            or type(value.get("capture_sealed")) is not bool
            or value.get("previous_cleanup_manifest_sha256")
            != expected_previous
            or not isinstance(value.get("groups"), list)
            or not isinstance(value.get("proven_empty_groups"), list)
            or not isinstance(value.get("daemon_identity"), dict)
            or not isinstance(value.get("observed_boot_identity"), str)
            or not value.get("observed_boot_identity")
            or value.get("state") != "stop_required"
            and value.get("capture_sealed") is not True
        ):
            raise LauncherError("offline stop manifest is malformed")
        seed = dict(value)
        claimed = seed.pop("manifest_sha256")
        if (
            SHA256_RE.fullmatch(str(claimed)) is None
            or canonical_sha256(seed) != claimed
        ):
            raise LauncherError("offline stop manifest digest is invalid")
        live = [_paid_group(guardian, item) for item in value["groups"]]
        empty = [
            _paid_group(guardian, item) for item in value["proven_empty_groups"]
        ]
        live_pgids = [item.identity.pgid for item in live]
        empty_pgids = [item.identity.pgid for item in empty]
        if (
            live_pgids != sorted(set(live_pgids))
            or empty_pgids != sorted(set(empty_pgids))
            or set(live_pgids) & set(empty_pgids)
        ):
            raise LauncherError("offline stop manifest groups are not exact")
        return live, empty, str(claimed)

    initial_groups, initial_empty, initial_manifest_sha256 = (
        validate_cleanup_manifest(
            manifest,
            expected_previous=None,
            expected_round=0,
        )
    )
    if manifest["state"] == "stop_required" and manifest["capture_sealed"]:
        raise LauncherError("initial offline stop manifest is unexpectedly sealed")
    head_status = host.invoke(
        "guardian-offline-capture-status",
        {"operation_id": operation_id},
        token=owner_token,
        token_domain="owner",
    )
    head_keys = {
        "schema_version",
        "operation_id",
        "state",
        "capture_round",
        "cleanup_manifest",
        "cleanup_manifest_sha256",
        "receipt_sha256",
    }
    head_seed = dict(head_status)
    head_receipt_sha256 = head_seed.pop("receipt_sha256", None)
    head_manifest = head_status.get("cleanup_manifest")
    if (
        set(head_status) != head_keys
        or head_status.get("schema_version")
        != "rethlas_guardian_offline_capture_status_v1"
        or head_status.get("operation_id") != operation_id
        or type(head_status.get("capture_round")) is not int
        or not isinstance(head_manifest, dict)
        or canonical_sha256(head_seed) != head_receipt_sha256
    ):
        raise LauncherError("offline capture status receipt is malformed")
    head_round = int(head_status["capture_round"])
    head_previous = head_manifest.get("previous_cleanup_manifest_sha256")
    if (
        head_round == 0
        and head_previous is not None
        or head_round > 0
        and SHA256_RE.fullmatch(str(head_previous)) is None
    ):
        raise LauncherError("offline capture status chain head is malformed")
    groups, proven_empty_groups, claimed_manifest_sha256 = (
        validate_cleanup_manifest(
            head_manifest,
            expected_previous=head_previous,
            expected_round=head_round,
        )
    )
    if (
        head_status.get("cleanup_manifest_sha256")
        != claimed_manifest_sha256
        or head_status.get("capture_round") != head_manifest.get("capture_round")
    ):
        raise LauncherError("offline capture status differs from its manifest")
    immutable_keys = expected_keys - {
        "groups",
        "proven_empty_groups",
        "capture_round",
        "capture_sealed",
        "previous_cleanup_manifest_sha256",
        "manifest_sha256",
    }
    if any(head_manifest[key] != manifest[key] for key in immutable_keys):
        raise LauncherError("offline capture chain changed its initial binding")
    manifest = head_manifest
    daemon_identity = _identity(guardian, manifest["daemon_identity"])
    if daemon_identity.pid != daemon_pid or daemon_identity.pgid != daemon_pid:
        raise LauncherError("offline stop daemon identity differs from the child")
    # The paid root(s) must be frozen before the supervising daemon.  Marking
    # the daemon auxiliary preserves stop_then_kill_groups' root-first order.
    daemon_group = guardian.PaidGroup("verifier", daemon_identity)
    all_groups = (*groups, daemon_group)
    if len({item.identity.pgid for item in all_groups}) != len(all_groups):
        raise LauncherError("offline stop manifest contains duplicate PGIDs")
    inspector = guardian.SystemProcessInspector()
    manifest_frozen = {
        key: manifest[key]
        for key in expected_keys
        if key
        not in {
            "groups",
            "proven_empty_groups",
            "capture_round",
            "capture_sealed",
            "previous_cleanup_manifest_sha256",
            "manifest_sha256",
        }
    }
    host_proven_empty: dict[int, object] = {
        item.identity.pgid: item for item in proven_empty_groups
    }
    failure_details: set[tuple[str, str]] = set()
    emergency_groups: dict[int, object] = {}
    reconcile_latest_head: Any = None

    def record_failure(
        stage: str, error: BaseException | None = None, *, kind: str | None = None
    ) -> None:
        if kind is None:
            if isinstance(error, guardian.GroupStopFailure):
                kind = "group_stop_failure"
            elif isinstance(error, guardian.IdentityViolation):
                kind = "identity_violation"
            elif isinstance(error, LauncherError):
                kind = "host_or_receipt_failure"
            elif isinstance(error, OSError):
                kind = "process_control_failure"
            else:
                kind = "unexpected_failure"
        failure_details.add((stage, kind))

    def merge_receipts(*values: object) -> object:
        stopped: set[int] = set()
        killed: set[int] = set()
        already_empty: set[int] = set()
        for value in values:
            stopped.update(value.stopped_pgids)
            killed.update(value.killed_pgids)
            already_empty.update(value.already_empty_pgids)
        # A later idempotent cleanup can observe a group empty after an earlier
        # SIGKILL.  Preserve the action receipt and keep the two coverage sets
        # disjoint as required by the durable host protocol.
        already_empty.difference_update(killed)
        return guardian.StopReceipt(
            tuple(sorted(stopped)),
            tuple(sorted(killed)),
            tuple(sorted(already_empty)),
        )

    if manifest["state"] == "reboot_proven_terminal":
        if (
            manifest["observed_boot_identity"] == manifest["boot_identity"]
            or manifest["capture_sealed"] is not True
            or groups
        ):
            raise LauncherError("reboot cleanup manifest is inconsistent")
        record_failure(
            "boot_identity", kind="boot_identity_changed"
        )
        receipt = guardian.StopReceipt(
            (),
            (),
            tuple(sorted({*host_proven_empty, daemon_identity.pgid})),
        )
    elif manifest["state"] == "already_empty":
        receipt = guardian.StopReceipt(
            (),
            (),
            tuple(
                sorted(
                    {
                        *(item.identity.pgid for item in all_groups),
                        *host_proven_empty,
                    }
                )
            ),
        )
    else:
        known_groups = {item.identity.pgid: item for item in all_groups}

        def validate_capture_receipt(
            captured: Mapping[str, object],
            *,
            submitted: list[object],
            capture_request_sha256: str,
            previous_manifest_sha256: str,
            previous_round: int,
        ) -> tuple[list[object], list[object], dict[str, object], list[object], list[object], str]:
            capture_keys = {
                "schema_version",
                "operation_id",
                "capture_request_sha256",
                "previous_cleanup_manifest_sha256",
                "accepted_groups",
                "already_empty_groups",
                "cleanup_manifest",
                "cleanup_manifest_sha256",
                "receipt_sha256",
            }
            receipt_seed = dict(captured)
            receipt_sha256 = receipt_seed.pop("receipt_sha256", None)
            if (
                set(captured) != capture_keys
                or captured.get("schema_version")
                != "rethlas_guardian_offline_capture_v1"
                or captured.get("operation_id") != operation_id
                or captured.get("capture_request_sha256")
                != capture_request_sha256
                or captured.get("previous_cleanup_manifest_sha256")
                != previous_manifest_sha256
                or not isinstance(captured.get("accepted_groups"), list)
                or not isinstance(captured.get("already_empty_groups"), list)
                or not isinstance(captured.get("cleanup_manifest"), dict)
                or canonical_sha256(receipt_seed) != receipt_sha256
            ):
                raise LauncherError("offline capture receipt is malformed")
            accepted = [
                _paid_group(guardian, item) for item in captured["accepted_groups"]
            ]
            already_empty = [
                _paid_group(guardian, item)
                for item in captured["already_empty_groups"]
            ]
            submitted_by_pgid = {
                item.identity.pgid: item for item in submitted
            }
            acknowledged = (*accepted, *already_empty)
            if (
                len({item.identity.pgid for item in acknowledged})
                != len(acknowledged)
                or {item.identity.pgid: item for item in acknowledged}
                != submitted_by_pgid
            ):
                raise LauncherError(
                    "offline capture did not acknowledge the exact candidates"
                )
            next_manifest = captured["cleanup_manifest"]
            next_groups, next_empty, next_manifest_sha256 = (
                validate_cleanup_manifest(
                    next_manifest,
                    expected_previous=previous_manifest_sha256,
                    expected_round=previous_round + 1,
                )
            )
            if (
                captured.get("cleanup_manifest_sha256")
                != next_manifest_sha256
                or next_manifest.get("capture_sealed") is not (not submitted)
                or any(
                    next_manifest[key] != value
                    for key, value in manifest_frozen.items()
                )
            ):
                raise LauncherError("offline capture superseded the frozen manifest")
            next_live_by_pgid = {
                item.identity.pgid: item for item in next_groups
            }
            next_empty_by_pgid = {
                item.identity.pgid: item for item in next_empty
            }
            if any(
                next_live_by_pgid.get(item.identity.pgid) != item
                for item in accepted
            ) or any(
                next_empty_by_pgid.get(item.identity.pgid) != item
                for item in already_empty
            ):
                raise LauncherError(
                    "offline capture manifest omitted an acknowledged candidate"
                )
            return (
                accepted,
                already_empty,
                next_manifest,
                next_groups,
                next_empty,
                next_manifest_sha256,
            )

        def read_capture_head() -> tuple[
            dict[str, object], list[object], list[object], str
        ]:
            value = host.invoke(
                "guardian-offline-capture-status",
                {"operation_id": operation_id},
                token=owner_token,
                token_domain="owner",
            )
            status_seed = dict(value)
            status_receipt_sha256 = status_seed.pop("receipt_sha256", None)
            head = value.get("cleanup_manifest")
            if (
                set(value) != head_keys
                or value.get("schema_version")
                != "rethlas_guardian_offline_capture_status_v1"
                or value.get("operation_id") != operation_id
                or type(value.get("capture_round")) is not int
                or not isinstance(head, dict)
                or canonical_sha256(status_seed) != status_receipt_sha256
            ):
                raise LauncherError(
                    "offline capture recovery status receipt is malformed"
                )
            recovered_round = int(value["capture_round"])
            recovered_previous = head.get(
                "previous_cleanup_manifest_sha256"
            )
            if (
                recovered_round == 0
                and recovered_previous is not None
                or recovered_round > 0
                and SHA256_RE.fullmatch(str(recovered_previous)) is None
            ):
                raise LauncherError(
                    "offline capture recovery chain head is malformed"
                )
            recovered_groups, recovered_empty, recovered_sha256 = (
                validate_cleanup_manifest(
                    head,
                    expected_previous=recovered_previous,
                    expected_round=recovered_round,
                )
            )
            if (
                value.get("cleanup_manifest_sha256") != recovered_sha256
                or any(
                    head[key] != frozen_value
                    for key, frozen_value in manifest_frozen.items()
                )
            ):
                raise LauncherError(
                    "offline capture recovery changed its frozen binding"
                )
            return head, recovered_groups, recovered_empty, recovered_sha256

        def adopt_capture_head(
            head: dict[str, object],
            recovered_groups: list[object],
            recovered_empty: list[object],
            recovered_sha256: str,
        ) -> None:
            nonlocal manifest, claimed_manifest_sha256, groups
            manifest = head
            claimed_manifest_sha256 = recovered_sha256
            groups = recovered_groups
            host_proven_empty.clear()
            host_proven_empty.update(
                {item.identity.pgid: item for item in recovered_empty}
            )
            for item in recovered_groups:
                known_groups[item.identity.pgid] = item

        def discover_after_stop() -> tuple[object, ...]:
            nonlocal manifest, claimed_manifest_sha256, groups
            candidates: dict[int, object] = {}
            try:
                guardian.capture_descendant_process_groups(
                    tuple(known_groups.values()),
                    registered_groups=known_groups,
                    candidate_groups=candidates,
                    inspector=inspector,
                    owner_uid=os.getuid(),
                )
            except guardian.IdentityViolation as error:
                # Exact candidates are retained and killed even when a
                # different descendant is ambiguous; finalization then stays
                # fail-closed instead of silently omitting either condition.
                record_failure("descendant_capture", error)
            submitted: list[object] = []
            for pgid, candidate in sorted(candidates.items()):
                existing = known_groups.get(pgid)
                if existing is not None:
                    if existing != candidate:
                        record_failure(
                            "descendant_capture", kind="identity_conflict"
                        )
                    continue
                submitted.append(candidate)
            submitted_values = [item.as_dict() for item in submitted]
            capture_request = {
                "schema_version": (
                    "rethlas_guardian_offline_capture_request_v1"
                ),
                "operation_id": operation_id,
                "previous_cleanup_manifest_sha256": claimed_manifest_sha256,
                "discovered_groups": submitted_values,
            }
            capture_request_sha256 = canonical_sha256(capture_request)
            previous_manifest_sha256 = claimed_manifest_sha256
            previous_round = int(manifest["capture_round"])
            try:
                capture_error: BaseException | None = None
                for _attempt in range(2):
                    try:
                        captured = host.invoke(
                            "guardian-offline-capture",
                            {
                                "operation_id": operation_id,
                                "previous_cleanup_manifest_sha256": (
                                    previous_manifest_sha256
                                ),
                                "discovered_groups": submitted_values,
                            },
                            token=owner_token,
                            token_domain="owner",
                        )
                        (
                            accepted,
                            already_empty,
                            next_manifest,
                            next_groups,
                            next_empty,
                            next_manifest_sha256,
                        ) = validate_capture_receipt(
                            captured,
                            submitted=submitted,
                            capture_request_sha256=capture_request_sha256,
                            previous_manifest_sha256=previous_manifest_sha256,
                            previous_round=previous_round,
                        )
                        break
                    except BaseException as error:
                        capture_error = error
                else:
                    assert capture_error is not None
                    raise capture_error
            except BaseException as error:
                # Response-unknown host capture is never authority to continue
                # normally.  First recover the durable head: the host may have
                # committed the exact request before losing both replies.
                try:
                    recovered = read_capture_head()
                    recovered_head, recovered_live, recovered_empty, recovered_sha = (
                        recovered
                    )
                    recovered_live_by_pgid = {
                        item.identity.pgid: item for item in recovered_live
                    }
                    recovered_empty_by_pgid = {
                        item.identity.pgid: item for item in recovered_empty
                    }
                    if (
                        bool(submitted)
                        and all(
                            recovered_live_by_pgid.get(item.identity.pgid) == item
                            or recovered_empty_by_pgid.get(item.identity.pgid) == item
                            for item in submitted
                        )
                        or not submitted
                        and recovered_head.get("capture_sealed") is True
                    ):
                        adopt_capture_head(
                            recovered_head,
                            recovered_live,
                            recovered_empty,
                            recovered_sha,
                        )
                        return tuple(
                            recovered_live_by_pgid[item.identity.pgid]
                            for item in submitted
                            if item.identity.pgid in recovered_live_by_pgid
                        )
                except BaseException as recovery_error:
                    record_failure("capture_status", recovery_error)
                # The candidate was not durably recovered.  Retain each exact
                # local identity as emergency kill coverage and force a
                # content-addressed execution_unknown finalization.
                record_failure("host_capture", error)
                for item in submitted:
                    known_groups[item.identity.pgid] = item
                    emergency_groups[item.identity.pgid] = item
                return tuple(submitted)
            adopt_capture_head(
                next_manifest,
                next_groups,
                next_empty,
                next_manifest_sha256,
            )
            return tuple(accepted)

        receipts: list[object] = []
        cleanup_needs_retry = False
        try:
            receipts.append(
                guardian.stop_then_kill_groups(
                    all_groups,
                    inspector=inspector,
                    signaler=guardian.OSGroupSignaler(),
                    reap=lambda identity: (
                        os.waitpid(daemon_pid, 0)
                        if identity.pid == daemon_pid
                        else None
                    ),
                    discover_after_stop=(
                        None
                        if manifest["capture_sealed"]
                        else discover_after_stop
                    ),
                )
            )
        except guardian.GroupStopFailure as error:
            receipts.append(error.receipt)
            record_failure("stop_then_kill", error)
            cleanup_needs_retry = True
        except BaseException as error:
            record_failure("stop_then_kill", error)
            cleanup_needs_retry = True

        # A partial/response-unknown cleanup is followed by one idempotent
        # exact retry without further discovery.  The first pass already froze
        # all safely captured descendants; retrying only that known set avoids
        # inventing authority while preventing a possibly stopped group from
        # being stranded.
        if cleanup_needs_retry:
            try:
                receipts.append(
                    guardian.stop_then_kill_groups(
                        tuple(known_groups.values()),
                        inspector=inspector,
                        signaler=guardian.OSGroupSignaler(),
                        reap=lambda identity: (
                            os.waitpid(daemon_pid, 0)
                            if identity.pid == daemon_pid
                            else None
                        ),
                    )
                )
            except guardian.GroupStopFailure as error:
                receipts.append(error.receipt)
                record_failure("cleanup_retry", error)
            except BaseException as error:
                record_failure("cleanup_retry", error)

        try:
            guardian.wait_for_groups_empty(
                tuple(known_groups.values()),
                inspector=inspector,
            )
        except BaseException as error:
            record_failure("empty_proof", error)
            # One last zero-grace exact pass handles a transient residual that
            # appeared after the first SIGKILL.  Its failure is evidence for
            # execution_unknown, never a reason to skip durable finalization.
            try:
                receipts.append(
                    guardian.stop_then_kill_groups(
                        tuple(known_groups.values()),
                        inspector=inspector,
                        signaler=guardian.OSGroupSignaler(),
                        reap=lambda identity: (
                            os.waitpid(daemon_pid, 0)
                            if identity.pid == daemon_pid
                            else None
                        ),
                    )
                )
                guardian.wait_for_groups_empty(
                    tuple(known_groups.values()),
                    inspector=inspector,
                )
            except guardian.GroupStopFailure as retry_error:
                receipts.append(retry_error.receipt)
                record_failure("empty_retry", retry_error)
            except BaseException as retry_error:
                record_failure("empty_retry", retry_error)

        receipt = merge_receipts(*receipts)
        receipt = merge_receipts(
            receipt,
            guardian.StopReceipt(
                (),
                (),
                tuple(sorted(host_proven_empty)),
            ),
        )

        if manifest.get("capture_sealed") is not True and not failure_details:
            record_failure("capture_seal", kind="unsealed_cleanup_head")

        def reconcile_latest_cleanup_head() -> bool:
            """Adopt and clean a concurrently advanced durable chain head."""

            nonlocal receipt
            previous_head_sha256 = claimed_manifest_sha256
            prior_known_pgids = set(known_groups)
            recovered_head = read_capture_head()
            if recovered_head[3] == previous_head_sha256:
                return False
            if int(recovered_head[0]["capture_round"]) < int(
                manifest["capture_round"]
            ):
                raise LauncherError("offline capture head moved backwards")
            adopt_capture_head(*recovered_head)
            late_live = tuple(
                item
                for item in recovered_head[1]
                if item.identity.pgid not in prior_known_pgids
            )
            late_cleanup_needs_retry = False
            try:
                late_receipt = guardian.stop_then_kill_groups(
                    late_live,
                    inspector=inspector,
                    signaler=guardian.OSGroupSignaler(),
                    reap=lambda _identity: None,
                    # Even an advanced head with no new live groups needs one
                    # exact empty capture to become sealed.  Calling discovery
                    # with an empty pending set performs that fixed-point CAS.
                    discover_after_stop=(
                        None
                        if manifest["capture_sealed"]
                        else discover_after_stop
                    ),
                )
                receipt = merge_receipts(receipt, late_receipt)
            except guardian.GroupStopFailure as late_error:
                receipt = merge_receipts(receipt, late_error.receipt)
                record_failure("late_head_cleanup", late_error)
                late_cleanup_needs_retry = True
            except BaseException as late_error:
                record_failure("late_head_cleanup", late_error)
                late_cleanup_needs_retry = True
            try:
                guardian.wait_for_groups_empty(
                    tuple(known_groups.values()),
                    inspector=inspector,
                )
            except BaseException as late_error:
                record_failure("late_head_empty_proof", late_error)
                late_cleanup_needs_retry = True
            if late_cleanup_needs_retry:
                try:
                    retry_receipt = guardian.stop_then_kill_groups(
                        tuple(known_groups.values()),
                        inspector=inspector,
                        signaler=guardian.OSGroupSignaler(),
                        reap=lambda _identity: None,
                    )
                    receipt = merge_receipts(receipt, retry_receipt)
                    guardian.wait_for_groups_empty(
                        tuple(known_groups.values()),
                        inspector=inspector,
                    )
                except guardian.GroupStopFailure as retry_error:
                    receipt = merge_receipts(
                        receipt, retry_error.receipt
                    )
                    record_failure("late_head_cleanup_retry", retry_error)
                except BaseException as retry_error:
                    record_failure("late_head_cleanup_retry", retry_error)
            receipt = merge_receipts(
                receipt,
                guardian.StopReceipt(
                    (), (), tuple(sorted(host_proven_empty))
                ),
            )
            return True

        reconcile_latest_head = reconcile_latest_cleanup_head

        # A capture response can remain unknown while the host actually moved
        # the durable chain head.  Refresh once more after local cleanup so an
        # exact host-attested identity is not duplicated as a failure claim.
        if emergency_groups:
            try:
                reconcile_latest_cleanup_head()
            except BaseException as error:
                record_failure("capture_status", error)
        receipt = merge_receipts(
            receipt,
            guardian.StopReceipt(
                (), (), tuple(sorted(host_proven_empty))
            ),
        )
    def build_finalize_payload() -> tuple[dict[str, object], object | None]:
        durable_groups_by_pgid = {
            item.identity.pgid: item
            for item in [*groups, *host_proven_empty.values()]
        }
        observed_failure_group_values: list[dict[str, object]] = []
        for pgid, item in sorted(emergency_groups.items()):
            durable = durable_groups_by_pgid.get(pgid)
            if durable == item:
                continue
            observed_failure_group_values.append(item.as_dict())
        failure: dict[str, object] | None = None
        failure_sha256: str | None = None
        report_receipt = receipt
        if failure_details:
            detail_material = [
                {"stage": stage, "error_type": error_type}
                for stage, error_type in sorted(failure_details)
            ]
            failure = _bounded_offline_failure(
                detail_material, observed_failure_group_values
            )
            failure_group_values = failure["groups"]
            assert isinstance(failure_group_values, list)
            failure_sha256 = canonical_sha256(failure)
            # The host can bind action receipts only to its durable manifest
            # and the bounded failure sample.  Every locally observed group is
            # still killed and checked above, while truncation deliberately
            # forces durable coverage_complete=false without claiming an
            # unrepresented PGID.
            reportable_pgids = set(durable_groups_by_pgid) | {
                daemon_identity.pgid
            } | {
                int(item["identity"]["pgid"])
                for item in failure_group_values
            }
            report_receipt = guardian.StopReceipt(
                tuple(
                    pgid
                    for pgid in receipt.stopped_pgids
                    if pgid in reportable_pgids
                ),
                tuple(
                    pgid
                    for pgid in receipt.killed_pgids
                    if pgid in reportable_pgids
                ),
                tuple(
                    pgid
                    for pgid in receipt.already_empty_pgids
                    if pgid in reportable_pgids
                ),
            )
        covered = sorted(
            set(report_receipt.killed_pgids)
            | set(report_receipt.already_empty_pgids)
        )
        empty_proof = {
            "schema_version": "rethlas_guardian_empty_proof_v1",
            "manifest_sha256": claimed_manifest_sha256,
            "empty_pgids": covered,
            "failure": failure,
            "failure_sha256": failure_sha256,
        }
        return (
            {
                "operation_id": operation_id,
                "manifest_sha256": claimed_manifest_sha256,
                "stopped_pgids": list(report_receipt.stopped_pgids),
                "killed_pgids": list(report_receipt.killed_pgids),
                "already_empty_pgids": list(
                    report_receipt.already_empty_pgids
                ),
                "empty_proof_sha256": canonical_sha256(empty_proof),
                "failure": failure,
                "failure_sha256": failure_sha256,
            },
            failure,
        )

    finalize_error: BaseException | None = None
    for _head_attempt in range(16):
        finalize_payload, failure = build_finalize_payload()
        for _attempt in range(2):
            try:
                result = host.invoke(
                    "guardian-offline-finalize",
                    finalize_payload,
                    token=owner_token,
                    token_domain="owner",
                )
                validated = _validated_offline_finalize_status(
                    {"offline_finalize": result}
                )
                if (
                    validated is None
                    or validated.get("operation_id") != operation_id
                    or validated.get("manifest_sha256")
                    != claimed_manifest_sha256
                    or failure is not None
                    and validated.get("state") != "execution_unknown"
                ):
                    raise LauncherError(
                        "offline finalize response changed its durable binding"
                    )
                return dict(validated)
            except BaseException as error:
                finalize_error = error
        # An exact finalize may commit before its response is lost.  A
        # terminal owner status read is authoritative and avoids restarting
        # cleanup or replacing the one-winner receipt.
        try:
            durable = _validated_offline_finalize_status(
                _status(host, configuration, owner_token)
            )
            if durable is not None:
                if durable.get("operation_id") != operation_id:
                    raise LauncherError(
                        "durable offline terminal belongs to another operation"
                    )
                return dict(durable)
        except BaseException as status_error:
            finalize_error = status_error
            break
        if reconcile_latest_head is None:
            break
        try:
            if not reconcile_latest_head():
                break
        except BaseException as reconcile_error:
            finalize_error = reconcile_error
            record_failure("capture_status", reconcile_error)
            break
    assert finalize_error is not None
    raise LauncherError(
        "offline finalize response remained unknown after exact replay"
    ) from finalize_error


def launch(
    configuration: LaunchConfiguration,
    *,
    owner_token: str,
    launcher_source: PinnedSource,
    guardian_source: PinnedSource,
    runner_source: PinnedSource,
    adapter_source: PinnedSource,
    worker_executable_source: PinnedExecutable,
    problem_source: PinnedSource,
    handoff_candidate_source: PinnedSource | None,
) -> dict[str, Any]:
    """Launch and synchronously monitor one durable Guardian root."""

    _validate_configuration(configuration)
    expected_problem_path = Path(
        os.path.abspath(
            os.fspath(configuration.worker_cwd / configuration.problem_relative_path)
        )
    )
    if problem_source.path != expected_problem_path:
        raise LauncherError("pinned problem path differs from worker cwd binding")
    if worker_executable_source.path != Path(
        os.path.abspath(configuration.worker_command[0])
    ):
        raise LauncherError("pinned worker executable differs from target argv")
    if SHA256_RE.fullmatch(owner_token) is None:
        raise LauncherError("owner capability from FIFO is malformed")
    if any(name in os.environ for name in PRIVILEGED_ENVIRONMENT):
        raise LauncherError("owner launcher inherited a privileged environment key")
    host = PinnedAdapterClient(adapter_source, configuration.database_path)
    guardian = _load_guardian(guardian_source)
    runner_read, runner_write = os.pipe()
    lifeline_read, lifeline_write = os.pipe()
    event_read, event_write = os.pipe()
    worker_adapter_fd = (
        os.dup(adapter_source.descriptor)
        if configuration.worker_mode == "runner_control"
        else -1
    )
    release_event_write = (
        os.dup(event_write)
        if configuration.worker_mode == "opaque_guarded_command"
        else -1
    )
    config_read = config_write = -1
    guardian_read = guardian_write = -1
    guardian_token = secrets.token_hex(32)
    runner_token = secrets.token_hex(32)
    try:
        worker_environment = _worker_environment()
        target_command = (
            _pinned_runner_control_command(
                configuration, adapter_source, worker_adapter_fd
            )
            if configuration.worker_mode == "runner_control"
            else configuration.worker_command
        )
        target_command_sha256 = canonical_sha256(list(target_command))
        if configuration.worker_mode == "runner_control":
            blocked_command = (
                *target_command,
                "--runner-token-fd",
                str(runner_read),
            )
        else:
            blocked_command = (
                sys.executable,
                "-I",
                "-B",
                "-c",
                _WORKER_RELEASE_BOOTSTRAP,
                configuration.worker_mode,
                str(runner_read),
                str(release_event_write),
                target_command_sha256,
                *target_command,
            )
        command_sha256 = canonical_sha256(list(blocked_command))
        manifest = build_launch_manifest(
            launcher_sha256=launcher_source.sha256,
            guardian_sha256=guardian_source.sha256,
            runner_sha256=runner_source.sha256,
            adapter_sha256=adapter_source.sha256,
            guardian_control_schema_sha256=(
                str(
                    host.policy_contract()["review_cadence_policy"][
                        "guardian_control_schema_sha256"
                    ]
                )
            ),
            worker_command_sha256=target_command_sha256,
            worker_runtime_command_sha256=command_sha256,
            worker_executable_sha256=worker_executable_source.sha256,
            worker_environment_sha256=canonical_sha256(worker_environment),
            guardian_mode=configuration.guardian_mode,
            worker_mode=configuration.worker_mode,
            worker_cwd=str(configuration.worker_cwd),
            problem_relative_path=configuration.problem_relative_path,
            problem_sha256=problem_source.sha256,
            handoff_candidate_sha256=(
                handoff_candidate_source.sha256
                if handoff_candidate_source is not None
                else None
            ),
        )
        contract = host.policy_contract()
        policy_digest = _validate_policy_and_manifest(
            contract,
            expected_contract_sha256=configuration.policy_contract_sha256,
            manifest=manifest,
        )
        if policy_digest != configuration.policy_digest:
            raise LauncherError("launch policy digest differs from its admission pin")
        inspector = guardian.SystemProcessInspector()
        boot_identity = inspector.boot_identity()
        wall_now = time.time()
        monotonic_now = time.monotonic()
        prepare_payload = {
            "admission_mode": configuration.admission_mode,
            "boot_identity": boot_identity,
            "capability_revision": configuration.capability_revision,
            "command_sha256": command_sha256,
            "expected_clock_sha256": configuration.expected_clock_sha256,
            "expected_cycle_id": configuration.expected_cycle_id,
            "expected_generation": configuration.expected_generation,
            "generation_control_instance_id": (
                configuration.generation_control_instance_id
            ),
            "guardian_sha256": guardian_source.sha256,
            "guardian_token_sha256": hashlib.sha256(
                guardian_token.encode("ascii")
            ).hexdigest(),
            "launch_manifest": manifest,
            "launch_manifest_sha256": canonical_sha256(manifest),
            "policy_digest": configuration.policy_digest,
            "registration_not_after_monotonic": (
                monotonic_now + _REGISTRATION_WINDOW_SECONDS
            ),
            "registration_not_after_wall_epoch": (
                wall_now + _REGISTRATION_WINDOW_SECONDS
            ),
            "run_id": configuration.run_id,
            "runner_token_sha256": hashlib.sha256(
                runner_token.encode("ascii")
            ).hexdigest(),
            "watchdog_id": configuration.watchdog_id,
            "worker_command": list(target_command),
            "worker_runtime_command": list(blocked_command),
            "worker_environment": worker_environment,
        }
        prepared = host.invoke(
            "guardian-prepare",
            prepare_payload,
            token=owner_token,
            token_domain="owner",
            extra_fds=((worker_adapter_fd,) if worker_adapter_fd >= 0 else ()),
        )
        launch_intent_sha256 = prepared.get("launch_intent_sha256")
        if SHA256_RE.fullmatch(str(launch_intent_sha256)) is None:
            raise LauncherError("guardian prepare returned no launch intent digest")
        for source in (
            launcher_source,
            guardian_source,
            runner_source,
            adapter_source,
            worker_executable_source,
            problem_source,
        ):
            source.attest_unchanged()
        if handoff_candidate_source is not None:
            handoff_candidate_source.attest_unchanged()
        os.write(runner_write, runner_token.encode("ascii"))
        os.close(runner_write)
        runner_write = -1
        config_read, config_write = os.pipe()
        guardian_read, guardian_write = os.pipe()
        daemon_configuration = {
            "schema_version": "rethlas_guardian_daemon_config_v1",
            "configuration": {
                "database_path": str(configuration.database_path),
                "run_id": configuration.run_id,
                "generation_control_instance_id": (
                    configuration.generation_control_instance_id
                ),
                "watchdog_id": configuration.watchdog_id,
                "admission_mode": configuration.admission_mode,
                "expected_cycle_id": configuration.expected_cycle_id,
                "expected_generation": configuration.expected_generation,
                "expected_clock_sha256": configuration.expected_clock_sha256,
                "capability_revision": configuration.capability_revision,
                "policy_contract_sha256": configuration.policy_contract_sha256,
                "policy_digest": configuration.policy_digest,
                "guardian_mode": configuration.guardian_mode,
                "worker_cwd": str(configuration.worker_cwd),
                "worker_mode": configuration.worker_mode,
                "problem_relative_path": configuration.problem_relative_path,
                "worker_command": list(configuration.worker_command),
            },
            "launch_intent_sha256": launch_intent_sha256,
            "blocked_command": list(blocked_command),
            "worker_environment": worker_environment,
        }
        daemon_argv = [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _PINNED_SCRIPT_LOADER,
            str(launcher_source.descriptor),
            str(launcher_source.size),
            launcher_source.sha256,
            str(launcher_source.path),
            "--internal-daemon",
            "--config-fd",
            str(config_read),
            "--guardian-token-fd",
            str(guardian_read),
            "--runner-token-fd",
            str(runner_read),
            "--worker-adapter-fd",
            str(worker_adapter_fd),
            "--lifeline-fd",
            str(lifeline_read),
            "--event-fd",
            str(event_write),
            "--release-event-fd",
            str(release_event_write),
            "--guardian-source-fd",
            str(guardian_source.descriptor),
            "--guardian-source-path",
            str(guardian_source.path),
            "--guardian-source-sha256",
            guardian_source.sha256,
            "--adapter-source-fd",
            str(adapter_source.descriptor),
            "--adapter-source-path",
            str(adapter_source.path),
            "--adapter-source-sha256",
            adapter_source.sha256,
        ]
        daemon_process = subprocess.Popen(
            daemon_argv,
            stdin=subprocess.DEVNULL,
            env={
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            pass_fds=(
                launcher_source.descriptor,
                guardian_source.descriptor,
                adapter_source.descriptor,
                config_read,
                guardian_read,
                runner_read,
                *((worker_adapter_fd,) if worker_adapter_fd >= 0 else ()),
                lifeline_read,
                event_write,
                *((release_event_write,) if release_event_write >= 0 else ()),
            ),
            start_new_session=True,
        )
        daemon_pid = daemon_process.pid
        os.close(config_read)
        config_read = -1
        os.close(guardian_read)
        guardian_read = -1
        os.close(runner_read)
        runner_read = -1
        if worker_adapter_fd >= 0:
            os.close(worker_adapter_fd)
            worker_adapter_fd = -1
        if release_event_write >= 0:
            os.close(release_event_write)
            release_event_write = -1
        os.close(lifeline_read)
        lifeline_read = -1
        os.close(event_write)
        event_write = -1
        config_bytes = canonical_json(daemon_configuration)
        view = memoryview(config_bytes)
        while view:
            written = os.write(config_write, view)
            if written <= 0:
                raise LauncherError("guardian configuration pipe write was short")
            view = view[written:]
        os.close(config_write)
        config_write = -1
        os.write(guardian_write, guardian_token.encode("ascii"))
        os.close(guardian_write)
        guardian_write = -1
        guardian_token = ""
        runner_token = ""
        os.set_blocking(event_read, False)
        events = _GuardianEventStream(
            target_command_sha256=target_command_sha256,
            worker_mode=configuration.worker_mode,
        )
        child_status: int | None = None
        durable_terminal_seen_at: float | None = None
        while True:
            readable = []
            if not events.eof:
                readable, _, _ = select.select(
                    [event_read], [], [], _OWNER_MONITOR_INTERVAL_SECONDS
                )
            if readable:
                events.drain_available(event_read)
            returncode = daemon_process.poll()
            if returncode is not None:
                child_status = returncode
                # poll() only reaps the daemon.  Bytes written between the
                # preceding readiness check and this observation remain queued
                # in the pipe and must be consumed through its real EOF.
                _drain_guardian_events_after_exit(event_read, events)
                break
            if events.eof:
                try:
                    child_status = daemon_process.wait(
                        timeout=_DAEMON_EVENT_EOF_TIMEOUT_SECONDS
                    )
                except subprocess.TimeoutExpired as error:
                    raise LauncherError(
                        "guardian event stream reached EOF before daemon exit"
                    ) from error
                break
            if events.failure is not None:
                raise events.failure
            if events.registered is not None:
                projection = events.registered["registration_ack"]["projection"]
                hard_due = bool(
                    time.time() >= float(projection["hard_stop_wall_epoch"])
                    or (
                        inspector.boot_identity() == projection["boot_identity"]
                        and time.monotonic() >= float(projection["hard_stop_monotonic"])
                    )
                )
                if hard_due:
                    status = _status(host, configuration, owner_token)
                    if _status_has_durable_terminal_report(status):
                        if durable_terminal_seen_at is None:
                            durable_terminal_seen_at = time.monotonic()
                        elif (
                            time.monotonic() - durable_terminal_seen_at
                            >= _DURABLE_TERMINAL_EXIT_TIMEOUT_SECONDS
                        ):
                            offline_terminal = _validated_offline_finalize_status(
                                status
                            )
                            # The durable terminal winner has already revoked
                            # Guardian authority.  Stop the exact daemon group
                            # if it remains stuck before emitting/closing its
                            # terminal pipe.
                            try:
                                os.killpg(daemon_pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            child_status = _wait_and_drain_guardian_after_cleanup(
                                daemon_process,
                                event_read,
                                events,
                            )
                            if offline_terminal is not None:
                                _validate_offline_event_stream(
                                    events, status, offline_terminal
                                )
                                return {
                                    "schema_version": (
                                        "rethlas_guardian_launcher_result_v1"
                                    ),
                                    "run_id": configuration.run_id,
                                    "watchdog_id": configuration.watchdog_id,
                                    "launch_manifest_sha256": canonical_sha256(
                                        manifest
                                    ),
                                    "registration": events.registered,
                                    "release_marker": events.release_marker,
                                    "release_marker_sha256": (
                                        canonical_sha256(events.release_marker)
                                        if events.release_marker is not None
                                        else None
                                    ),
                                    "report": None,
                                    "offline_finalize": offline_terminal,
                                    "daemon_wait_status": child_status,
                                    "state": str(offline_terminal["state"]),
                                }
                            terminal_report = status.get("terminal_report")
                            if not isinstance(terminal_report, dict):
                                raise LauncherError(
                                    "durable terminal status omitted its report"
                                )
                            _validate_terminal_event_stream(
                                events,
                                status,
                                terminal_report,
                                worker_mode=configuration.worker_mode,
                                final_event_required=False,
                            )
                            return {
                                "schema_version": (
                                    "rethlas_guardian_launcher_result_v1"
                                ),
                                "run_id": configuration.run_id,
                                "watchdog_id": configuration.watchdog_id,
                                "launch_manifest_sha256": canonical_sha256(manifest),
                                "registration": events.registered,
                                "release_marker": events.release_marker,
                                "release_marker_sha256": (
                                    canonical_sha256(events.release_marker)
                                    if events.release_marker is not None
                                    else None
                                ),
                                "report": terminal_report,
                                "offline_finalize": None,
                                "daemon_wait_status": child_status,
                                "state": str(terminal_report.get("state")),
                            }
                    else:
                        durable_terminal_seen_at = None
                        offline = _offline_stop(
                            guardian,
                            host,
                            configuration,
                            owner_token=owner_token,
                            status=status,
                            daemon_pid=daemon_pid,
                        )
                        offline = _validated_offline_finalize_status(
                            {"offline_finalize": offline}
                        )
                        if offline is None:
                            raise LauncherError(
                                "offline stop returned no durable receipt"
                            )
                        child_status = _finalize_offline_event_stream(
                            daemon_process,
                            event_read,
                            events,
                            status,
                            offline,
                            wait_for_daemon=True,
                        )
                        return {
                            "schema_version": "rethlas_guardian_launcher_result_v1",
                            "run_id": configuration.run_id,
                            "watchdog_id": configuration.watchdog_id,
                            "launch_manifest_sha256": canonical_sha256(manifest),
                            "registration": events.registered,
                            "release_marker": events.release_marker,
                            "release_marker_sha256": (
                                canonical_sha256(events.release_marker)
                                if events.release_marker is not None
                                else None
                            ),
                            "report": None,
                            "offline_finalize": offline,
                            "daemon_wait_status": child_status,
                            "state": str(
                                offline.get("state", "execution_unknown")
                            ),
                        }
        status = _status(host, configuration, owner_token)
        if not _status_has_durable_terminal_report(status):
            durable_registration = _status_has_durable_registration(status)
            if not durable_registration and events.registered is None:
                events.require_clean_eof()
                raise LauncherError(
                    "guardian daemon exited before durable registration; zero release proven"
                )
            offline = _offline_stop(
                guardian,
                host,
                configuration,
                owner_token=owner_token,
                status=status,
                daemon_pid=daemon_pid,
            )
            offline = _validated_offline_finalize_status(
                {"offline_finalize": offline}
            )
            if offline is None:
                raise LauncherError("offline stop returned no durable receipt")
            _finalize_offline_event_stream(
                daemon_process,
                event_read,
                events,
                status,
                offline,
                wait_for_daemon=False,
            )
            return {
                "schema_version": "rethlas_guardian_launcher_result_v1",
                "run_id": configuration.run_id,
                "watchdog_id": configuration.watchdog_id,
                "launch_manifest_sha256": canonical_sha256(manifest),
                "registration": events.registered,
                "release_marker": events.release_marker,
                "release_marker_sha256": (
                    canonical_sha256(events.release_marker)
                    if events.release_marker is not None
                    else None
                ),
                "report": None,
                "offline_finalize": offline,
                "state": str(offline.get("state", "execution_unknown")),
            }
        offline_terminal = _validated_offline_finalize_status(status)
        if offline_terminal is not None:
            _validate_offline_event_stream(events, status, offline_terminal)
            return {
                "schema_version": "rethlas_guardian_launcher_result_v1",
                "run_id": configuration.run_id,
                "watchdog_id": configuration.watchdog_id,
                "launch_manifest_sha256": canonical_sha256(manifest),
                "registration": events.registered,
                "release_marker": events.release_marker,
                "release_marker_sha256": (
                    canonical_sha256(events.release_marker)
                    if events.release_marker is not None
                    else None
                ),
                "report": None,
                "offline_finalize": offline_terminal,
                "daemon_wait_status": child_status,
                "state": str(offline_terminal["state"]),
            }
        terminal_report = status.get("terminal_report")
        if not isinstance(terminal_report, dict):
            raise LauncherError("terminal guardian status omitted its report")
        _validate_terminal_event_stream(
            events,
            status,
            terminal_report,
            worker_mode=configuration.worker_mode,
        )
        return {
            "schema_version": "rethlas_guardian_launcher_result_v1",
            "run_id": configuration.run_id,
            "watchdog_id": configuration.watchdog_id,
            "launch_manifest_sha256": canonical_sha256(manifest),
            "registration": events.registered,
            "release_marker": events.release_marker,
            "release_marker_sha256": (
                canonical_sha256(events.release_marker)
                if events.release_marker is not None
                else None
            ),
            "report": terminal_report,
            "offline_finalize": None,
            "daemon_wait_status": child_status,
            "state": str(terminal_report.get("state")),
        }
    finally:
        for descriptor in (
            runner_read,
            runner_write,
            worker_adapter_fd,
            lifeline_read,
            lifeline_write,
            event_read,
            event_write,
            release_event_write,
            config_read,
            config_write,
            guardian_read,
            guardian_write,
        ):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-token-fd", type=int, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--adapter-path", type=Path, required=True)
    parser.add_argument("--adapter-sha256", required=True)
    parser.add_argument("--guardian-path", type=Path, required=True)
    parser.add_argument("--runner-path", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--generation-control-instance-id", required=True)
    parser.add_argument("--watchdog-id", required=True)
    parser.add_argument(
        "--admission-mode",
        choices=("initial_new_cycle", "next_new_cycle", "same_cycle_resume"),
        required=True,
    )
    parser.add_argument("--expected-cycle-id", required=True)
    parser.add_argument("--expected-generation", type=int, required=True)
    parser.add_argument("--expected-clock-sha256")
    parser.add_argument("--capability-revision", type=int, required=True)
    parser.add_argument("--policy-contract-sha256", required=True)
    parser.add_argument("--policy-digest", required=True)
    parser.add_argument(
        "--guardian-mode",
        choices=("hard_stop", "monitor_only"),
        default="hard_stop",
    )
    parser.add_argument("--worker-cwd", type=Path, required=True)
    parser.add_argument("--problem-path", type=Path, required=True)
    parser.add_argument("--problem-relative-path", required=True)
    parser.add_argument("--handoff-candidate-path", type=Path)
    parser.add_argument(
        "--worker-mode",
        choices=("runner_control", "opaque_guarded_command"),
        default="runner_control",
    )
    parser.add_argument("worker_command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--internal-daemon":
        try:
            return _internal_daemon_main(arguments[1:])
        except (LauncherError, OSError, ValueError, KeyError) as error:
            print(f"guardian daemon failed closed: {error}", file=sys.stderr)
            return 70
    args = _parser().parse_args(arguments)
    worker_command = list(args.worker_command)
    if worker_command and worker_command[0] == "--":
        worker_command.pop(0)
    sources: list[PinnedSource | PinnedExecutable] = []
    try:
        owner_token = consume_token_fd(args.owner_token_fd, label="owner")
        if SHA256_RE.fullmatch(args.adapter_sha256) is None:
            raise LauncherError("adapter SHA-256 argument is malformed")
        entry_fd = globals().pop("__rethlas_pinned_launcher_fd__", None)
        entry_path = globals().pop("__rethlas_pinned_launcher_path__", None)
        entry_sha256 = globals().pop("__rethlas_pinned_launcher_sha256__", None)
        if (
            type(entry_fd) is not int
            or not isinstance(entry_path, str)
            or not isinstance(entry_sha256, str)
        ):
            raise LauncherError(
                "launcher must execute from the trusted runner's pinned-FD loader"
            )
        launcher_source = PinnedSource.adopt(
            entry_fd, Path(entry_path), expected_sha256=entry_sha256
        )
        guardian_source = PinnedSource.open(args.guardian_path)
        runner_source = PinnedSource.open(args.runner_path)
        adapter_source = PinnedSource.open(
            args.adapter_path, expected_sha256=args.adapter_sha256
        )
        if not worker_command:
            raise LauncherError("worker command is required")
        worker_executable_source = PinnedExecutable.open(Path(worker_command[0]))
        problem_source = PinnedSource.open(args.problem_path)
        handoff_candidate_source = (
            PinnedSource.open(args.handoff_candidate_path)
            if args.handoff_candidate_path is not None
            else None
        )
        sources.extend(
            [
                launcher_source,
                guardian_source,
                runner_source,
                adapter_source,
                worker_executable_source,
                problem_source,
            ]
        )
        if handoff_candidate_source is not None:
            sources.append(handoff_candidate_source)
        configuration = LaunchConfiguration(
            database_path=Path(os.path.abspath(os.fspath(args.db))),
            run_id=args.run_id,
            generation_control_instance_id=args.generation_control_instance_id,
            watchdog_id=args.watchdog_id,
            admission_mode=args.admission_mode,
            expected_cycle_id=args.expected_cycle_id,
            expected_generation=args.expected_generation,
            expected_clock_sha256=args.expected_clock_sha256,
            capability_revision=args.capability_revision,
            policy_contract_sha256=args.policy_contract_sha256,
            policy_digest=args.policy_digest,
            guardian_mode=args.guardian_mode,
            worker_cwd=Path(os.path.abspath(os.fspath(args.worker_cwd))),
            worker_mode=args.worker_mode,
            problem_relative_path=args.problem_relative_path,
            worker_command=tuple(worker_command),
        )
        result = launch(
            configuration,
            owner_token=owner_token,
            launcher_source=launcher_source,
            guardian_source=guardian_source,
            runner_source=runner_source,
            adapter_source=adapter_source,
            worker_executable_source=worker_executable_source,
            problem_source=problem_source,
            handoff_candidate_source=handoff_candidate_source,
        )
        sys.stdout.buffer.write(canonical_json(result) + b"\n")
        report = result.get("report")
        return (
            0
            if (
                result.get("state") == "completed"
                and isinstance(report, dict)
                and report.get("direct_returncode") == 0
            )
            else 70
        )
    except (LauncherError, OSError, ValueError, KeyError) as error:
        print(f"guardian launcher failed closed: {error}", file=sys.stderr)
        return 70
    finally:
        for source in sources:
            source.close()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "GUARDIAN_RELATIVE_PATH",
    "LAUNCHER_RELATIVE_PATH",
    "LAUNCH_MANIFEST_KEYS",
    "LAUNCH_MANIFEST_SCHEMA",
    "LAUNCH_MANIFEST_SCHEMA_DESCRIPTOR",
    "LAUNCH_MANIFEST_SCHEMA_SHA256",
    "RUNNER_RELATIVE_PATH",
    "LaunchConfiguration",
    "LauncherError",
    "PinnedSource",
    "build_launch_manifest",
    "canonical_json",
    "canonical_sha256",
    "consume_token_fd",
    "guardian_cycle_id",
    "launch",
    "main",
]
