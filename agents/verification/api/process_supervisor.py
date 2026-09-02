#!/usr/bin/env python3
"""Minimal crash-surviving supervisor for one verifier model process tree."""

from __future__ import annotations

import os
import ctypes
import hashlib
import json
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_stop_requested = False
_MODEL_RELEASE_PREFIX = b"RETHLAS_VERIFIER_MODEL_RELEASE_V1\x00"
_PROCESS_GUARD_PATH_ENV = "RETHLAS_INTERNAL_PROCESS_GUARD_PATH"
_PROCESS_GUARD_RUN_ID_ENV = "RETHLAS_INTERNAL_PROCESS_GUARD_RUN_ID"
_DURABLE_OUTPUT_PATH_ENV = "RETHLAS_INTERNAL_DURABLE_OUTPUT_PATH"
_DURABLE_OUTPUT_MAX_BYTES_ENV = "RETHLAS_INTERNAL_DURABLE_OUTPUT_MAX_BYTES"


def _request_stop(_signum: int, _frame: object) -> None:
    global _stop_requested
    _stop_requested = True


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _process_start_identity(pid: int) -> str:
    """Return an in-process PID-reuse fence without spawning ``ps``."""

    if type(pid) is not int or pid <= 1:
        raise RuntimeError("cannot bind verifier process start identity")
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            tail = raw[raw.rindex(")") + 2 :].split()
            if tail[0] == "Z":
                raise RuntimeError("cannot bind verifier process start identity")
            start_ticks = tail[19]
        except (OSError, UnicodeError, ValueError, IndexError) as exc:
            raise RuntimeError("cannot bind verifier process start identity") from exc
        return f"linux:{start_ticks}"
    if sys.platform == "darwin":
        class ProcBsdInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("pbi_rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]

        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            proc_pidinfo = libproc.proc_pidinfo
            proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            proc_pidinfo.restype = ctypes.c_int
            info = ProcBsdInfo()
            copied = proc_pidinfo(
                pid,
                3,
                0,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
        except (AttributeError, OSError) as exc:
            raise RuntimeError("cannot bind verifier process start identity") from exc
        if copied != ctypes.sizeof(info) or info.pbi_pid != pid or info.pbi_status == 5:
            raise RuntimeError("cannot bind verifier process start identity")
        return f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"
    raise RuntimeError("platform lacks a verifier process identity primitive")


def _lifeline_alive(pid: int | None, start_sha256: str | None) -> bool:
    if pid is None and start_sha256 is None:
        return True
    if (
        type(pid) is not int
        or pid <= 1
        or not isinstance(start_sha256, str)
        or len(start_sha256) != 64
    ):
        return False
    try:
        identity = _process_start_identity(pid)
    except RuntimeError:
        return False
    return hashlib.sha256(identity.encode("ascii")).hexdigest() == start_sha256


def _publish_canonical_guard(
    path: Path, payload: dict[str, object]
) -> os.stat_result:
    """Publish one complete, durable, no-replace canonical guard."""

    if not path.is_absolute() or path.parent.is_symlink():
        raise RuntimeError("verifier guard path is not trusted")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _canonical_json(payload) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError("verifier guard write was short")
            view = view[written:]
        os.fsync(descriptor)
        written_metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, path, follow_symlinks=False)
        parent_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                # A recovery reader may have completed this exact same-inode
                # alias cleanup after the final link became durable.
                pass
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    observed_metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(observed_metadata.st_mode)
        or observed_metadata.st_nlink != 1
        or (observed_metadata.st_dev, observed_metadata.st_ino)
        != (written_metadata.st_dev, written_metadata.st_ino)
    ):
        raise RuntimeError("verifier guard identity changed before release")
    return observed_metadata


def _fsync_durable_output(path: Path, *, maximum_bytes: int) -> tuple[int, str]:
    if not path.is_absolute() or path.parent.is_symlink():
        raise RuntimeError("verifier durable output path is not trusted")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > maximum_bytes
        ):
            raise RuntimeError("verifier durable output is not a private regular file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            digest.update(chunk)
        os.fsync(descriptor)
        final_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(final_metadata.st_mode)
            or final_metadata.st_nlink != 1
            or (final_metadata.st_dev, final_metadata.st_ino)
            != (metadata.st_dev, metadata.st_ino)
            or final_metadata.st_size != metadata.st_size
        ):
            raise RuntimeError("verifier durable output changed while syncing")
    finally:
        os.close(descriptor)
    parent_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)
    return metadata.st_size, digest.hexdigest()


def _write_child_guard(
    path: Path,
    *,
    parent_pid: int,
    deadline_epoch: float,
    child: Any,
    command: list[str],
) -> dict[str, object]:
    if not path.is_absolute() or path.parent.is_symlink():
        raise RuntimeError("verifier child guard path is not trusted")
    path.parent.mkdir(parents=True, exist_ok=True)
    guard = {
        "schema_version": "rethlas_verifier_child_process_guard_v2",
        "service_pid": parent_pid,
        "wrapper_pid": os.getpid(),
        "wrapper_pgid": os.getpgrp(),
        "child_pid": child.pid,
        "child_pgid": os.getpgid(child.pid),
        "child_start_identity": _process_start_identity(child.pid),
        "deadline_utc": datetime.fromtimestamp(
            deadline_epoch, tz=timezone.utc
        ).isoformat(),
        "command_sha256": hashlib.sha256(_canonical_json(command)).hexdigest(),
        # This is a durable dispatch intent: recovery must assume the release
        # may have crossed the pipe boundary and must never redispatch.
        "state": "release_intent_durable",
        "returncode": None,
        "raw_output_bytes": None,
        "raw_output_sha256": None,
    }
    # The final fence name becomes visible only after every canonical byte is
    # durable.  A crash while writing leaves only an ignorable temporary.
    _publish_canonical_guard(path, guard)
    return guard


def _replace_child_guard(
    path: Path,
    guard: dict[str, object],
    *,
    state: str,
    returncode: int | None = None,
    raw_output_bytes: int | None = None,
    raw_output_sha256: str | None = None,
) -> dict[str, object]:
    updated = {
        **guard,
        "state": state,
        "returncode": returncode,
        "raw_output_bytes": raw_output_bytes,
        "raw_output_sha256": raw_output_sha256,
    }
    encoded = _canonical_json(updated) + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError("verifier child guard update was short")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    parent_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)
    return updated


class _ForkedChild:
    def __init__(self, pid: int, release_fd: int) -> None:
        self.pid = pid
        self.release_fd = release_fd
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        waited, status = os.waitpid(self.pid, os.WNOHANG)
        if waited == self.pid:
            self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(["forked-verifier-model"], timeout)
            time.sleep(0.01)
        assert self.returncode is not None
        return self.returncode


def _spawn_blocked_model(command: list[str]) -> _ForkedChild:
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - assertions observe the parent/supervisor
        try:
            os.close(write_fd)
            os.setsid()
            released = bytearray()
            while True:
                chunk = os.read(read_fd, 65_536)
                if not chunk:
                    break
                released.extend(chunk)
            os.close(read_fd)
            if not bytes(released).startswith(_MODEL_RELEASE_PREFIX):
                os._exit(124)
            prompt = bytes(released)[len(_MODEL_RELEASE_PREFIX) :]
            with tempfile.TemporaryFile(mode="w+b") as prompt_file:
                prompt_file.write(prompt)
                prompt_file.seek(0)
                os.dup2(prompt_file.fileno(), sys.stdin.fileno())
                # No second open/import of this supervisor occurs.  The child
                # executes only the command bytes already held by the pinned
                # trusted wrapper process.
                os.execvpe(command[0], command, os.environ.copy())
        except BaseException:
            os._exit(127)
    os.close(read_fd)
    child = _ForkedChild(pid, write_fd)
    deadline = time.monotonic() + 1.0
    while True:
        try:
            if os.getpgid(pid) == pid:
                return child
        except ProcessLookupError as exc:
            os.close(write_fd)
            raise RuntimeError("blocked verifier child exited before binding") from exc
        if time.monotonic() >= deadline:
            os.close(write_fd)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise RuntimeError("blocked verifier child did not form its process group")
        time.sleep(0.005)


def _kill_child_group(child: Any) -> None:
    group_id = child.pid
    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(group_id, signal.SIGKILL)
    except ProcessLookupError:
        return


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    legacy_shape = len(arguments) >= 5 and arguments[3] == "--"
    lifeline_shape = len(arguments) >= 7 and arguments[5] == "--"
    if not legacy_shape and not lifeline_shape:
        print(
            "usage: process_supervisor.py PARENT_PID DEADLINE_EPOCH CHILD_GUARD "
            "[LIFELINE_PID LIFELINE_START_SHA256] -- COMMAND...",
            file=sys.stderr,
        )
        return 2
    try:
        parent_pid = int(arguments[0])
        deadline_epoch = float(arguments[1])
    except ValueError:
        return 2
    child_guard_path = Path(arguments[2])
    lifeline_pid: int | None = None
    lifeline_start_sha256: str | None = None
    if lifeline_shape:
        try:
            lifeline_pid = int(arguments[3])
        except ValueError:
            return 2
        lifeline_start_sha256 = arguments[4]
        command = arguments[6:]
    else:
        command = arguments[4:]
    if parent_pid <= 1 or not command or not child_guard_path.is_absolute():
        return 2

    process_guard_raw = os.environ.pop(_PROCESS_GUARD_PATH_ENV, None)
    process_guard_run_id = os.environ.pop(_PROCESS_GUARD_RUN_ID_ENV, None)
    durable_output_raw = os.environ.pop(_DURABLE_OUTPUT_PATH_ENV, None)
    durable_output_max_raw = os.environ.pop(
        _DURABLE_OUTPUT_MAX_BYTES_ENV, None
    )
    if (process_guard_raw is None) != (process_guard_run_id is None):
        return 2
    if process_guard_raw is not None:
        process_guard_path = Path(process_guard_raw)
        if (
            not process_guard_path.is_absolute()
            or process_guard_path.parent != child_guard_path.parent
            or not process_guard_run_id
        ):
            return 2
        try:
            _publish_canonical_guard(
                process_guard_path,
                {
                    "schema_version": "rethlas_verifier_process_guard_v2",
                    "run_id": process_guard_run_id,
                    "wrapper_pid": os.getpid(),
                    "wrapper_pgid": os.getpgrp(),
                    "wrapper_start_identity": _process_start_identity(os.getpid()),
                    "service_pid": parent_pid,
                    "child_guard_path": str(child_guard_path.resolve()),
                    "deadline_utc": datetime.fromtimestamp(
                        deadline_epoch, tz=timezone.utc
                    ).isoformat(),
                    "command_sha256": hashlib.sha256(
                        _canonical_json(command)
                    ).hexdigest(),
                    "state": "blocked_input_pending",
                },
            )
        except (OSError, RuntimeError):
            return 127
    durable_output_path = (
        None if durable_output_raw is None else Path(durable_output_raw)
    )
    try:
        durable_output_maximum = (
            None
            if durable_output_max_raw is None
            else int(durable_output_max_raw)
        )
    except ValueError:
        return 2
    if (durable_output_path is None) != (durable_output_maximum is None):
        return 2
    if durable_output_path is not None and (
        not durable_output_path.is_absolute()
        or durable_output_path.parent != child_guard_path.parent
        or durable_output_maximum is None
        or not 0 < durable_output_maximum <= 1_000_000_000
    ):
        return 2

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    # The service persists the wrapper pid/pgid/deadline before closing this
    # stdin pipe. If it dies before that release, EOF plus the parent check
    # exits with zero model dispatch.
    prompt = sys.stdin.buffer.read()
    if (
        _stop_requested
        or os.getppid() != parent_pid
        or not _lifeline_alive(lifeline_pid, lifeline_start_sha256)
        or time.time() >= deadline_epoch
    ):
        return 124

    try:
        child = _spawn_blocked_model(command)
    except OSError:
        return 127
    guard: dict[str, object] | None = None
    try:
        guard = _write_child_guard(
            child_guard_path,
            parent_pid=parent_pid,
            deadline_epoch=deadline_epoch,
            child=child,
            command=command,
        )
        release = memoryview(_MODEL_RELEASE_PREFIX + prompt)
        while release:
            written = os.write(child.release_fd, release)
            if written <= 0:
                raise RuntimeError("verifier model release pipe was short")
            release = release[written:]
        os.close(child.release_fd)
        child.release_fd = -1
        guard = _replace_child_guard(
            child_guard_path, guard, state="released"
        )
    except BaseException:
        if child.release_fd >= 0:
            os.close(child.release_fd)
            child.release_fd = -1
        _kill_child_group(child)
        raise
    while True:
        returncode = child.poll()
        if returncode is not None:
            # A successful direct model may still have left tool children.
            _kill_child_group(child)
            if returncode == 0 and durable_output_path is not None:
                try:
                    raw_size, raw_sha256 = _fsync_durable_output(
                        durable_output_path,
                        maximum_bytes=durable_output_maximum,
                    )
                except (OSError, RuntimeError):
                    _replace_child_guard(
                        child_guard_path,
                        guard,
                        state="raw_output_unavailable",
                        returncode=returncode,
                    )
                    return 126
                guard = _replace_child_guard(
                    child_guard_path,
                    guard,
                    state="raw_output_durable",
                    returncode=0,
                    raw_output_bytes=raw_size,
                    raw_output_sha256=raw_sha256,
                )
            _replace_child_guard(
                child_guard_path,
                guard,
                state="completed",
                returncode=returncode,
                raw_output_bytes=guard.get("raw_output_bytes"),
                raw_output_sha256=guard.get("raw_output_sha256"),
            )
            return returncode
        lifeline_lost = not _lifeline_alive(lifeline_pid, lifeline_start_sha256)
        if (
            _stop_requested
            or os.getppid() != parent_pid
            or lifeline_lost
            or time.time() >= deadline_epoch
        ):
            terminal_state = (
                "timed_out"
                if time.time() >= deadline_epoch
                else "caller_lost"
                if lifeline_lost
                else "execution_unknown"
            )
            guard = _replace_child_guard(
                child_guard_path, guard, state=terminal_state
            )
            _kill_child_group(child)
            return 125 if lifeline_lost else 124
        time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(main())
