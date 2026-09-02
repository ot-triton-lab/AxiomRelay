#!/usr/bin/env python3
"""Fail-closed process guardian for one durable AxiomRelay paid cycle.

This module deliberately knows nothing about the HotJoin wire protocol.  The
owner supplies five small callbacks and persists their effects.  In
particular, a paid command remains behind an exec gate until ``register`` has
returned a durable, request-bound acknowledgement.

The process-group leader is a trusted shim.  It calls ``setsid()``, forks the
paid command, and never execs it.  After the direct command terminates the shim
stays alive until the guardian proves that no residual member remains and
explicitly retires it.
"""

from __future__ import annotations

import dataclasses
import ctypes
import hashlib
import json
import math
import os
import queue
import re
import select
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


REVIEW_CYCLE_SECONDS = 9_000.0
INTERNAL_INTERRUPT_LEAD_SECONDS = 5.0
MAX_PROJECTION_SKEW_SECONDS = 1.0
_RELEASE_TOKEN = b"RETHLAS_GUARDIAN_RELEASE_V1\x00"
_RETIRE_TOKEN = b"RETHLAS_GUARDIAN_RETIRE_V1\x00"
_MAX_SHIM_CONFIG_BYTES = 1_048_576
_SHIM_SOURCE = r"""
import json
import os
import sys

config_fd, gate_fd, retire_fd, event_fd = map(int, sys.argv[1:5])

def report_unhandled(exc_type, error, traceback):
    try:
        message = ("shim_error:%s:%s\n" % (exc_type.__name__, error)).encode(
            "utf-8", errors="replace"
        )[:4096]
        os.write(event_fd, message)
    finally:
        os._exit(126)

sys.excepthook = report_unhandled

def read_all(fd):
    chunks = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)

config = json.loads(read_all(config_fd).decode("utf-8"))
os.close(config_fd)
command = config["command"]
environment = config["environment"]
pass_fds = tuple(config["pass_fds"])
os.write(event_fd, b"shim_ready\n")
release = read_all(gate_fd)
os.close(gate_fd)
if release != b"RETHLAS_GUARDIAN_RELEASE_V1\x00":
    os.write(event_fd, b"pre_release_eof\n")
    os._exit(124)
worker = os.fork()
if worker == 0:
    try:
        os.close(retire_fd)
        os.close(event_fd)
        for descriptor in pass_fds:
            os.set_inheritable(descriptor, True)
        os.execve(command[0], command, environment)
    except BaseException:
        os._exit(127)
for descriptor in pass_fds:
    try:
        os.close(descriptor)
    except OSError:
        pass
waited, status = os.waitpid(worker, 0)
if waited != worker:
    os._exit(125)
returncode = os.waitstatus_to_exitcode(status)
os.write(event_fd, ("worker_rc:%d\n" % returncode).encode("ascii"))
os.close(event_fd)
retirement = read_all(retire_fd)
os.close(retire_fd)
if retirement != b"RETHLAS_GUARDIAN_RETIRE_V1\x00":
    os._exit(125)
os._exit(returncode if 0 <= returncode <= 125 else 125)
"""


class GuardianError(RuntimeError):
    """Base class for a fail-closed guardian error."""


class ClockViolation(GuardianError):
    """An authoritative clock projection became unsafe."""


class IdentityViolation(GuardianError):
    """A process can no longer be proven to be the registered process."""


class GroupStopFailure(IdentityViolation):
    """One group was ambiguous, after every safely bound group was cleaned up."""

    def __init__(self, message: str, receipt: "StopReceipt") -> None:
        super().__init__(message)
        self.receipt = receipt


class HostControlFailure(GuardianError):
    """The durable host control plane failed or equivocated."""


class HostCallbackTimeout(HostControlFailure):
    """A host callback did not return before its safety deadline."""


class ResidualDescendants(GuardianError):
    """The direct paid command returned while descendants remained live."""


class _HardStopDue(GuardianError):
    """Internal control transfer into the zero-grace hard-stop path."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise GuardianError("pipe write was short")
        view = view[written:]


def _close_fds_except(allowed: set[int]) -> None:
    """Close inherited descriptors after fork without trusting the parent env."""

    descriptors: set[int] | None = None
    for directory in ("/dev/fd", "/proc/self/fd"):
        try:
            descriptors = {
                int(name) for name in os.listdir(directory) if name.isdecimal()
            }
            break
        except OSError:
            continue
    if descriptors is None:
        try:
            maximum = int(os.sysconf("SC_OPEN_MAX"))
        except (OSError, ValueError):
            maximum = 65_536
        descriptors = set(range(3, min(maximum, 1_048_576)))
    for descriptor in descriptors - allowed:
        if descriptor <= 2:
            continue
        try:
            os.close(descriptor)
        except OSError:
            pass


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClockViolation(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ClockViolation(f"{name} must be a finite number")
    return result


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Identity sufficient to reject a reused PID or PGID before signalling."""

    pid: int
    uid: int
    pgid: int
    start_marker: str

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 1:
            raise ValueError("pid must identify a non-init process")
        if type(self.uid) is not int or self.uid < 0:
            raise ValueError("uid must be a nonnegative integer")
        if type(self.pgid) is not int or self.pgid <= 1:
            raise ValueError("pgid must identify a non-init process group")
        if not isinstance(self.start_marker, str) or not self.start_marker:
            raise ValueError("start_marker is required")

    def as_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


@dataclass(frozen=True, slots=True)
class PaidGroup:
    """One host-attested paid process group."""

    role: str
    identity: ProcessIdentity

    def __post_init__(self) -> None:
        if self.role not in {"root", "reviewer", "verifier"}:
            raise ValueError("paid group role must be root, reviewer, or verifier")
        if self.identity.pid != self.identity.pgid:
            raise ValueError("a paid group identity must name its stable leader")

    def as_dict(self) -> dict[str, object]:
        return {"role": self.role, "identity": self.identity.as_dict()}


@dataclass(frozen=True, slots=True)
class DeadlineProjection:
    """Host-authenticated projection of the original absolute cycle clock."""

    cycle_started_wall_epoch: float
    cycle_started_monotonic: float
    internal_interrupt_wall_epoch: float
    internal_interrupt_monotonic: float
    hard_stop_wall_epoch: float
    hard_stop_monotonic: float
    projected_wall_epoch: float
    projected_monotonic: float
    boot_identity: str

    def __post_init__(self) -> None:
        start = _finite_number(
            self.cycle_started_wall_epoch, "cycle_started_wall_epoch"
        )
        monotonic_start = _finite_number(
            self.cycle_started_monotonic, "cycle_started_monotonic"
        )
        interrupt_wall = _finite_number(
            self.internal_interrupt_wall_epoch, "internal_interrupt_wall_epoch"
        )
        interrupt_monotonic = _finite_number(
            self.internal_interrupt_monotonic, "internal_interrupt_monotonic"
        )
        stop = _finite_number(self.hard_stop_wall_epoch, "hard_stop_wall_epoch")
        monotonic_stop = _finite_number(self.hard_stop_monotonic, "hard_stop_monotonic")
        projected_wall = _finite_number(
            self.projected_wall_epoch, "projected_wall_epoch"
        )
        projected_monotonic = _finite_number(
            self.projected_monotonic, "projected_monotonic"
        )
        if not isinstance(self.boot_identity, str) or not self.boot_identity:
            raise ClockViolation("boot_identity is required")
        if abs((stop - start) - REVIEW_CYCLE_SECONDS) > 1e-6:
            raise ClockViolation("hard stop is not the original T0 plus 9000 seconds")
        if abs((monotonic_stop - monotonic_start) - REVIEW_CYCLE_SECONDS) > 1e-6:
            raise ClockViolation(
                "monotonic hard stop is not the original T0 plus 9000 seconds"
            )
        if abs(interrupt_wall - (stop - INTERNAL_INTERRUPT_LEAD_SECONDS)) > 1e-6:
            raise ClockViolation(
                "internal interrupt is not the persisted T149:55 wall deadline"
            )
        if (
            abs(
                interrupt_monotonic - (monotonic_stop - INTERNAL_INTERRUPT_LEAD_SECONDS)
            )
            > 1e-6
        ):
            raise ClockViolation(
                "internal interrupt is not the persisted T149:55 monotonic deadline"
            )
        wall_remaining = stop - projected_wall
        monotonic_remaining = monotonic_stop - projected_monotonic
        if abs(wall_remaining - monotonic_remaining) > MAX_PROJECTION_SKEW_SECONDS:
            raise ClockViolation(
                "host wall and monotonic deadline projections disagree"
            )

    def as_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


class GuardianClock:
    """Absolute wall deadline with a same-boot monotonic projection.

    There is intentionally no API that accepts a duration or resets T0.  The
    hard boundary fires when *either* authenticated clock reaches its deadline.
    A backwards sample or wall lag behind monotonic fails closed instead of
    extending the cycle.  A forward wall correction is safe because it can
    only make the earliest-clock boundary arrive sooner.
    """

    def __init__(
        self,
        projection: DeadlineProjection,
        *,
        boot_identity: str,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_rollback_tolerance: float = MAX_PROJECTION_SKEW_SECONDS,
        monotonic_rollback_tolerance: float = 1e-6,
    ) -> None:
        if projection.boot_identity != boot_identity:
            raise ClockViolation("boot identity changed across the deadline projection")
        if (
            not math.isfinite(wall_rollback_tolerance)
            or wall_rollback_tolerance < 0
            or not math.isfinite(monotonic_rollback_tolerance)
            or monotonic_rollback_tolerance < 0
        ):
            raise ValueError("clock rollback tolerances must be finite and nonnegative")
        self.projection = projection
        self.wall_clock = wall_clock
        self.monotonic_clock = monotonic_clock
        self.wall_rollback_tolerance = wall_rollback_tolerance
        self.monotonic_rollback_tolerance = monotonic_rollback_tolerance
        # These are persisted host deadlines, not a new duration derived by
        # this process.  The consistency checks above only reject drift; they
        # never replace or extend either authoritative deadline.
        self.hard_stop_monotonic = projection.hard_stop_monotonic
        self.interrupt_wall_epoch = projection.internal_interrupt_wall_epoch
        self.interrupt_monotonic = projection.internal_interrupt_monotonic
        self._last_wall = projection.projected_wall_epoch
        self._last_monotonic = projection.projected_monotonic

    def _read_sample(self) -> tuple[float, float]:
        wall = _finite_number(self.wall_clock(), "wall clock sample")
        monotonic = _finite_number(self.monotonic_clock(), "monotonic clock sample")
        if wall + self.wall_rollback_tolerance < self._last_wall:
            raise ClockViolation("wall clock rolled backwards")
        if monotonic + self.monotonic_rollback_tolerance < self._last_monotonic:
            raise ClockViolation("monotonic clock rolled backwards")
        return wall, monotonic

    def _commit_sample(
        self, wall: float, monotonic: float, *, check_projection_drift: bool
    ) -> tuple[float, float]:
        elapsed_wall = wall - self.projection.projected_wall_epoch
        elapsed_monotonic = monotonic - self.projection.projected_monotonic
        if (
            check_projection_drift
            and elapsed_wall + MAX_PROJECTION_SKEW_SECONDS < elapsed_monotonic
        ):
            raise ClockViolation("wall clock lagged monotonic deadline projection")
        self._last_wall = max(self._last_wall, wall)
        self._last_monotonic = max(self._last_monotonic, monotonic)
        return wall, monotonic

    def sample(self) -> tuple[float, float]:
        return self._commit_sample(*self._read_sample(), check_projection_drift=True)

    def interrupt_due(self) -> bool:
        wall, monotonic = self._read_sample()
        due = wall >= self.interrupt_wall_epoch or monotonic >= self.interrupt_monotonic
        self._commit_sample(
            wall,
            monotonic,
            # The policy intentionally treats the earliest authenticated clock
            # as authoritative.  A forward wall jump that crosses the durable
            # boundary fires it; it is not delayed by a subsequent drift error.
            check_projection_drift=not due,
        )
        return due

    def hard_stop_due(self) -> bool:
        wall, monotonic = self._read_sample()
        due = (
            wall >= self.projection.hard_stop_wall_epoch
            or monotonic >= self.hard_stop_monotonic
        )
        self._commit_sample(wall, monotonic, check_projection_drift=not due)
        return due

    def seconds_until_next_boundary(self, *, interrupt_sent: bool) -> float:
        wall, monotonic = self.sample()
        if interrupt_sent:
            wall_due = self.projection.hard_stop_wall_epoch
            monotonic_due = self.hard_stop_monotonic
        else:
            wall_due = self.interrupt_wall_epoch
            monotonic_due = self.interrupt_monotonic
        return max(0.0, min(wall_due - wall, monotonic_due - monotonic))

    def seconds_until_hard_stop(self) -> float:
        wall, monotonic = self.sample()
        return max(
            0.0,
            min(
                self.projection.hard_stop_wall_epoch - wall,
                self.hard_stop_monotonic - monotonic,
            ),
        )


class ProcessInspector(Protocol):
    def boot_identity(self) -> str: ...

    def identity(self, pid: int) -> ProcessIdentity | None: ...

    def group_members(self, pgid: int) -> tuple[ProcessIdentity, ...]: ...

    def descendants(self, pid: int) -> tuple[ProcessIdentity, ...]: ...


class SystemProcessInspector:
    """Read-only POSIX identity inspector used by the external guardian."""

    _DARWIN_ENUMERATION_ATTEMPTS = 5
    _DARWIN_MAX_PID_CAPACITY = 1_048_576

    class _DarwinProcBSDInfo(ctypes.Structure):
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
            ("rfu_1", ctypes.c_uint32),
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

    def __init__(self) -> None:
        self._darwin_proc_pidinfo = None
        self._darwin_proc_listallpids = None
        self._darwin_proc_listpgrppids = None
        self._darwin_sysctlbyname = None
        if sys.platform == "darwin":
            try:
                library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
                proc_pidinfo = library.proc_pidinfo
                proc_pidinfo.argtypes = [
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_uint64,
                    ctypes.c_void_p,
                    ctypes.c_int,
                ]
                proc_pidinfo.restype = ctypes.c_int
                proc_listallpids = library.proc_listallpids
                proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
                proc_listallpids.restype = ctypes.c_int
                proc_listpgrppids = library.proc_listpgrppids
                proc_listpgrppids.argtypes = [
                    ctypes.c_int,
                    ctypes.c_void_p,
                    ctypes.c_int,
                ]
                proc_listpgrppids.restype = ctypes.c_int
                system = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
                sysctlbyname = system.sysctlbyname
                sysctlbyname.argtypes = [
                    ctypes.c_char_p,
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_size_t),
                    ctypes.c_void_p,
                    ctypes.c_size_t,
                ]
                sysctlbyname.restype = ctypes.c_int
            except (AttributeError, OSError) as error:
                raise IdentityViolation(
                    "cannot initialize native Darwin process inspection"
                ) from error
            self._darwin_proc_pidinfo = proc_pidinfo
            self._darwin_proc_listallpids = proc_listallpids
            self._darwin_proc_listpgrppids = proc_listpgrppids
            self._darwin_sysctlbyname = sysctlbyname

    def boot_identity(self) -> str:
        linux_boot = Path("/proc/sys/kernel/random/boot_id")
        if linux_boot.is_file():
            value = linux_boot.read_text(encoding="ascii").strip()
            if value:
                return value
        if sys.platform != "darwin" or self._darwin_sysctlbyname is None:
            raise IdentityViolation("cannot establish host boot identity")
        # ``kern.boottime.tv_usec`` is not a stable boot identifier on Darwin:
        # it can change within one boot after host clock reconciliation.  The
        # kernel's per-session UUID is the native identity intended for this
        # purpose and remains distinct across actual boots.
        buffer = ctypes.create_string_buffer(128)
        size = ctypes.c_size_t(ctypes.sizeof(buffer))
        result = self._darwin_sysctlbyname(
            b"kern.bootsessionuuid",
            ctypes.byref(buffer),
            ctypes.byref(size),
            None,
            0,
        )
        raw = bytes(buffer.raw[: size.value])
        if (
            result != 0
            or size.value != 37
            or not raw.endswith(b"\x00")
            or re.fullmatch(
                rb"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}",
                raw[:-1],
            )
            is None
        ):
            raise IdentityViolation("cannot establish native Darwin boot identity")
        marker = b"darwin-bootsessionuuid:" + raw[:-1].lower()
        return hashlib.sha256(marker).hexdigest()

    def _darwin_info(self, pid: int) -> _DarwinProcBSDInfo | None:
        if self._darwin_proc_pidinfo is None:
            return None
        info = self._DarwinProcBSDInfo()
        size = ctypes.sizeof(info)
        returned = self._darwin_proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)
        if returned != size or info.pbi_pid != pid:
            return None
        return info

    def _darwin_pid_list(
        self,
        enumerator: Callable[[ctypes.c_void_p, int], int],
        *,
        estimated: int,
        operation: str,
    ) -> tuple[int, ...]:
        if estimated <= 0:
            raise IdentityViolation(f"cannot estimate Darwin {operation}")
        capacity = max(16, estimated + 32)
        for _attempt in range(self._DARWIN_ENUMERATION_ATTEMPTS):
            if capacity > self._DARWIN_MAX_PID_CAPACITY:
                break
            buffer = (ctypes.c_int * capacity)()
            count = enumerator(ctypes.byref(buffer), ctypes.sizeof(buffer))
            if count < 0 or count > capacity:
                raise IdentityViolation(
                    f"Darwin {operation} changed unsafely during enumeration"
                )
            # libproc may return exactly the caller's capacity when the buffer
            # was truncated.  Equality is therefore not a complete snapshot.
            if count < capacity:
                return tuple(sorted({pid for pid in buffer[:count] if pid > 1}))
            capacity *= 2
        raise IdentityViolation(f"Darwin {operation} remained truncated")

    def _darwin_pids(self) -> tuple[int, ...]:
        if self._darwin_proc_listallpids is None:
            raise IdentityViolation("native Darwin PID enumeration is unavailable")
        estimated = self._darwin_proc_listallpids(None, 0)
        return self._darwin_pid_list(
            self._darwin_proc_listallpids,
            estimated=estimated,
            operation="process list",
        )

    def _darwin_group_pids(self, pgid: int) -> tuple[int, ...]:
        if self._darwin_proc_listpgrppids is None:
            raise IdentityViolation(
                "native Darwin process-group enumeration is unavailable"
            )

        def enumerate_group(buffer: ctypes.c_void_p, size: int) -> int:
            return self._darwin_proc_listpgrppids(pgid, buffer, size)

        estimated = self._darwin_proc_listpgrppids(pgid, None, 0)
        if estimated == 0:
            return ()
        return self._darwin_pid_list(
            enumerate_group,
            estimated=estimated,
            operation=f"process group {pgid}",
        )

    @staticmethod
    def _linux_stat(pid: int) -> tuple[int, int, str] | None:
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        except (FileNotFoundError, PermissionError, OSError):
            return None
        suffix = raw.rsplit(")", 1)[-1].strip().split()
        if len(suffix) < 20:
            return None
        try:
            return int(suffix[1]), int(suffix[2]), suffix[19]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _linux_uid(pid: int) -> int | None:
        try:
            lines = Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines()
        except (FileNotFoundError, PermissionError, OSError):
            return None
        for line in lines:
            if not line.startswith("Uid:"):
                continue
            fields = line.split()
            try:
                return int(fields[1])
            except (IndexError, ValueError):
                return None
        return None

    @staticmethod
    def _linux_pids() -> tuple[int, ...]:
        try:
            return tuple(
                sorted(
                    int(entry.name)
                    for entry in Path("/proc").iterdir()
                    if entry.name.isdecimal() and int(entry.name) > 1
                )
            )
        except (FileNotFoundError, PermissionError, OSError) as error:
            raise IdentityViolation("cannot enumerate Linux processes") from error

    @staticmethod
    def _confirm_group_absent(pgid: int) -> None:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except PermissionError as error:
            if sys.platform == "darwin":
                # Native libproc enumeration is the membership authority on
                # Darwin. After it returns no members, killpg(0) can still
                # report EPERM when the numeric PGID has been reused by a
                # process in another macOS responsibility/coalition domain.
                # That new unsignalable group is not the previously enumerated
                # paid group. Treat the original identity as absent; any
                # visible same- or foreign-UID member would already have been
                # returned above and would not reach this probe.
                return
            raise IdentityViolation(
                "process group exists but is outside the guardian identity domain"
            ) from error
        except OSError as error:
            raise IdentityViolation(
                "cannot conservatively probe process group"
            ) from error
        raise IdentityViolation(
            "process group exists but enumeration returned no members"
        )

    def _start_marker(self, pid: int) -> str | None:
        linux_stat = self._linux_stat(pid)
        if linux_stat is not None:
            return "proc-start-ticks:" + linux_stat[2]
        if sys.platform == "darwin":
            # ``ps lstart`` has only second precision and cannot exclude PID
            # reuse within that second.  libproc exposes the kernel's
            # microsecond process start time; failure is intentionally fatal.
            info = self._darwin_info(pid)
            if info is None:
                return None
            return f"darwin-start-us:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"
        if not sys.platform.startswith("linux"):
            # Unsupported platforms must not silently weaken identity to a
            # coarse timestamp.
            return None
        return None

    def identity(self, pid: int) -> ProcessIdentity | None:
        if type(pid) is not int or pid <= 1:
            return None
        if sys.platform == "darwin":
            info = self._darwin_info(pid)
            if info is None:
                return None
            return ProcessIdentity(
                pid,
                int(info.pbi_uid),
                int(info.pbi_pgid),
                f"darwin-start-us:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}",
            )
        if not sys.platform.startswith("linux"):
            return None
        before = self._linux_stat(pid)
        uid = self._linux_uid(pid)
        after = self._linux_stat(pid)
        if before is None or uid is None or after != before:
            return None
        _ppid, pgid, start_ticks = before
        try:
            return ProcessIdentity(pid, uid, pgid, f"proc-start-ticks:{start_ticks}")
        except (TypeError, ValueError):
            return None

    def group_members(self, pgid: int) -> tuple[ProcessIdentity, ...]:
        if type(pgid) is not int or pgid <= 1:
            raise IdentityViolation("invalid process group id")
        if sys.platform == "darwin":
            identities = tuple(
                identity
                for pid in self._darwin_group_pids(pgid)
                if (identity := self.identity(pid)) is not None
                and identity.pgid == pgid
            )
        elif sys.platform.startswith("linux"):
            identities = tuple(
                identity
                for pid in self._linux_pids()
                if (identity := self.identity(pid)) is not None
                and identity.pgid == pgid
            )
        else:
            raise IdentityViolation("native process-group enumeration is unavailable")
        ordered = tuple(sorted(identities, key=lambda item: item.pid))
        if not ordered:
            self._confirm_group_absent(pgid)
        return ordered

    def descendants(self, pid: int) -> tuple[ProcessIdentity, ...]:
        """Return the currently observable recursive descendants of ``pid``."""

        if sys.platform == "darwin":
            infos = {
                observed_pid: info
                for observed_pid in self._darwin_pids()
                if (info := self._darwin_info(observed_pid)) is not None
            }
            children: dict[int, list[int]] = {}
            for observed_pid, info in infos.items():
                children.setdefault(int(info.pbi_ppid), []).append(observed_pid)
            pending = list(children.get(pid, ()))
            seen: set[int] = set()
            result: list[ProcessIdentity] = []
            while pending:
                child_pid = pending.pop()
                if child_pid in seen:
                    continue
                seen.add(child_pid)
                pending.extend(children.get(child_pid, ()))
                identity = self.identity(child_pid)
                if identity is not None:
                    result.append(identity)
            return tuple(sorted(result, key=lambda item: item.pid))

        if not sys.platform.startswith("linux"):
            raise IdentityViolation(
                "native process-descendant enumeration is unavailable"
            )
        children: dict[int, list[int]] = {}
        for child_pid in self._linux_pids():
            stat = self._linux_stat(child_pid)
            if stat is None:
                continue
            parent_pid, _pgid, _start_ticks = stat
            children.setdefault(parent_pid, []).append(child_pid)
        pending = list(children.get(pid, ()))
        seen: set[int] = set()
        result: list[ProcessIdentity] = []
        while pending:
            child_pid = pending.pop()
            if child_pid in seen:
                continue
            seen.add(child_pid)
            pending.extend(children.get(child_pid, ()))
            identity = self.identity(child_pid)
            if identity is None:
                # A concurrent exit is harmless only if it has fully vanished.
                continue
            result.append(identity)
        return tuple(sorted(result, key=lambda item: item.pid))


def revalidate_identity(
    expected: ProcessIdentity,
    inspector: ProcessInspector,
    *,
    require_group_leader: bool = True,
) -> ProcessIdentity:
    current = inspector.identity(expected.pid)
    if current is None or current != expected:
        raise IdentityViolation("registered process identity no longer matches")
    if require_group_leader and current.pid != current.pgid:
        raise IdentityViolation("registered process is no longer a group leader")
    return current


class GroupSignaler(Protocol):
    def killpg(self, pgid: int, sig: int) -> None: ...


class OSGroupSignaler:
    def killpg(self, pgid: int, sig: int) -> None:
        os.killpg(pgid, sig)


@dataclass(frozen=True, slots=True)
class StopReceipt:
    stopped_pgids: tuple[int, ...]
    killed_pgids: tuple[int, ...]
    already_empty_pgids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _TerminalCleanupOutcome:
    receipt: StopReceipt
    groups: tuple[PaidGroup, ...]
    proven_empty: bool
    failure_reason: str | None


_POST_KILL_FIXED_POINT_MAX_ROUNDS = 256
_POST_KILL_FIXED_POINT_TIMEOUT_SECONDS = 2.0
_MAX_RESIDUAL_DIAGNOSTIC_IDENTITIES = 8
_MAX_RESIDUAL_DIAGNOSTIC_MARKER_CHARS = 128


def _residual_identity_diagnostic(
    identities: Sequence[ProcessIdentity],
) -> str:
    """Return a deterministic, bounded description of observed identities."""

    records = sorted(
        (
            {
                "pid": item.pid,
                "uid": item.uid,
                "pgid": item.pgid,
                "start_marker": item.start_marker,
            }
            for item in identities
        ),
        key=lambda item: (
            item["pgid"],
            item["pid"],
            item["uid"],
            item["start_marker"],
        ),
    )
    sample = [
        {
            **record,
            "start_marker": str(record["start_marker"])[
                :_MAX_RESIDUAL_DIAGNOSTIC_MARKER_CHARS
            ],
        }
        for record in records[:_MAX_RESIDUAL_DIAGNOSTIC_IDENTITIES]
    ]
    return (
        f"count={len(records)},sha256={_sha256(records)},"
        f"sample={_canonical_json(sample).decode('utf-8')}"
    )


def _registered_group_state(group: PaidGroup, inspector: ProcessInspector) -> str:
    """Return ``live``, ``empty``, or ``ambiguous`` without signalling."""

    current = inspector.identity(group.identity.pid)
    if current is not None:
        if current == group.identity and current.pid == current.pgid:
            # Exact live leader identity is sufficient.  A fallible all-PID
            # membership enumeration must never delay SIGSTOP for this root.
            return "live"
        return "ambiguous"
    members = inspector.group_members(group.identity.pgid)
    if not members:
        return "empty"
    for member in members:
        if (
            member.pgid != group.identity.pgid
            or member.uid != group.identity.uid
            or member.pid == group.identity.pgid
        ):
            return "ambiguous"
    # A process group cannot be numerically reused while these leaderless
    # members keep the original kernel group alive.  The durable leader
    # identity plus same-UID membership therefore remains exact STOP/KILL
    # coverage even after the leader exits.
    return "live"


def _stabilized_registered_group_state(
    group: PaidGroup, inspector: ProcessInspector
) -> str:
    """Classify once; transient uncertainty is retried by the main poll loop."""

    return _registered_group_state(group, inspector)


def _post_kill_bound_group_state(
    group: PaidGroup, inspector: ProcessInspector
) -> str:
    """Return ``live``, ``empty``, or ``ambiguous`` after an exact PG kill.

    Once the original leader has received SIGKILL, it can still fork before
    the pending signal is delivered.  The resulting child keeps the same
    process group alive after the leader is reaped.  That group is still the
    exact bound group while it has no replacement leader and every remaining
    member retains the bound UID.  A different process at the leader PID, or
    a member with a different UID, is a reuse ambiguity and is never signalled.
    """

    current = inspector.identity(group.identity.pid)
    if current == group.identity:
        # The exact original leader is still authoritative evidence that this
        # is the bound process group.  Do not let a fallible all-process
        # enumeration suppress the next immediate SIGKILL.
        return "live"
    if current is not None:
        return "ambiguous"
    members = inspector.group_members(group.identity.pgid)
    if not members:
        return "empty"
    for member in members:
        if (
            member.pgid != group.identity.pgid
            or member.uid != group.identity.uid
            or (member.pid == group.identity.pgid and member != group.identity)
        ):
            return "ambiguous"
    return "live"


def _kill_bound_groups_to_fixed_point(
    groups: Sequence[PaidGroup],
    *,
    inspector: ProcessInspector,
    signaler: GroupSignaler,
    failures: list[str],
) -> None:
    """Repeatedly KILL exact residual PGs until empty or bounded failure.

    There is no TERM/CONT grace.  ``sleep(0)`` only yields so the kernel can
    deliver the already-pending SIGKILL before the next immediate rescan; any
    still-live exact group receives SIGKILL again in that next round.
    """

    pending = {
        group.identity.pgid: group
        for group in groups
    }
    deadline = time.monotonic() + _POST_KILL_FIXED_POINT_TIMEOUT_SECONDS
    for _round in range(_POST_KILL_FIXED_POINT_MAX_ROUNDS):
        if not pending:
            return
        next_pending: dict[int, PaidGroup] = {}
        for pgid, group in sorted(pending.items()):
            try:
                state = _post_kill_bound_group_state(group, inspector)
            except IdentityViolation:
                # Darwin can transiently keep the killed process group
                # addressable while native enumeration exposes no member.
                # Retain exact kill coverage and retry to the fixed-point
                # deadline; persistent uncertainty is reported below.
                next_pending[pgid] = group
                continue
            except BaseException as error:
                failures.append(
                    f"paid group {pgid} post-SIGKILL inspection failed: "
                    f"{type(error).__name__}"
                )
                continue
            if state == "empty":
                continue
            if state == "ambiguous":
                failures.append(
                    f"paid group {pgid} identity became ambiguous after SIGKILL"
                )
                continue
            try:
                signaler.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError as error:
                try:
                    state = _post_kill_bound_group_state(group, inspector)
                except BaseException as inspect_error:
                    failures.append(
                        f"paid group {pgid} ESRCH inspection after SIGKILL failed: "
                        f"{type(inspect_error).__name__}"
                    )
                    continue
                if state != "empty":
                    failures.append(
                        f"paid group {pgid} vanished ambiguously during repeated "
                        f"SIGKILL: {error}"
                    )
                continue
            except BaseException as error:
                failures.append(
                    f"paid group {pgid} repeated SIGKILL failed: "
                    f"{type(error).__name__}"
                )
                continue
            next_pending[pgid] = group
        pending = next_pending
        if not pending:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0)
    for pgid in sorted(pending):
        failures.append(
            f"paid group {pgid} did not reach an empty post-SIGKILL fixed point"
        )


def _require_auxiliary_groups_empty(
    groups: Sequence[PaidGroup],
    *,
    root_pgid: int,
    inspector: ProcessInspector,
) -> None:
    """Reject normal completion unless every registered auxiliary is empty."""

    for group in groups:
        if group.identity.pgid == root_pgid:
            continue
        try:
            auxiliary_state = _registered_group_state(group, inspector)
        except BaseException as inspect_error:
            raise ResidualDescendants(
                "root rc observed while auxiliary group state was unavailable: "
                + _residual_identity_diagnostic((group.identity,))
            ) from inspect_error
        if auxiliary_state != "empty":
            raise ResidualDescendants(
                "root rc observed while auxiliary paid groups were not empty: "
                + _residual_identity_diagnostic((group.identity,))
            )


def stop_then_kill_groups(
    groups: Sequence[PaidGroup],
    *,
    inspector: ProcessInspector,
    signaler: GroupSignaler | None = None,
    reap: Callable[[ProcessIdentity], None] | None = None,
    discover_after_stop: Callable[[], Sequence[PaidGroup]] | None = None,
) -> StopReceipt:
    """STOP/KILL every exactly bound live group and prove natural exits.

    An ambiguous registration is never signalled, but it also cannot prevent
    the exact root or any other exactly bound paid group from being stopped and
    killed.  The raised :class:`GroupStopFailure` carries the partial receipt so
    recovery can prove that already-stopped exact groups were not abandoned.
    There is deliberately no TERM/CONT or sleep between STOP and KILL.
    Supplying ``reap`` opts into the production closure contract: after reaping
    each original leader, the function repeatedly KILLs and rescans its exact
    bound PGID to an empty fixed point.  Callers without a reaper retain the
    legacy single-shot signal helper contract and must prove emptiness later.
    """

    signaler = OSGroupSignaler() if signaler is None else signaler
    failures: list[str] = []
    canonical: dict[int, PaidGroup] = {}
    conflicted_pgids: set[int] = set()
    for group in groups:
        if group.identity.pgid in conflicted_pgids:
            continue
        previous = canonical.get(group.identity.pgid)
        if previous is not None and previous != group:
            canonical.pop(group.identity.pgid, None)
            conflicted_pgids.add(group.identity.pgid)
            failures.append(f"conflicting registration for PGID {group.identity.pgid}")
            continue
        canonical[group.identity.pgid] = group
    stopped: list[int] = []
    # A STOP syscall that reports an error is response-unknown: the kernel may
    # already have applied the signal.  Keep every such exact group in the
    # immediate KILL set unless a post-error inspection proves it empty.  This
    # prevents an interrupted/transport-wrapped signal call from stranding a
    # paid group in the stopped state.
    kill_groups: list[PaidGroup] = []
    already_empty: set[int] = set()
    pending = tuple(
        sorted(
            canonical.values(),
            key=lambda item: (item.role != "root", item.identity.pgid),
        )
    )
    discovery_rounds = 0
    while True:
        for group in pending:
            try:
                state = _registered_group_state(group, inspector)
            except BaseException as exc:
                failures.append(
                    f"paid group {group.identity.pgid} inspection before SIGSTOP failed: "
                    f"{type(exc).__name__}"
                )
                continue
            if state == "empty":
                already_empty.add(group.identity.pgid)
                continue
            if state == "ambiguous":
                failures.append(
                    f"paid group {group.identity.pgid} identity no longer matches before SIGSTOP"
                )
                continue
            try:
                signaler.killpg(group.identity.pgid, signal.SIGSTOP)
            except ProcessLookupError as exc:
                try:
                    state = _registered_group_state(group, inspector)
                except BaseException as inspect_error:
                    failures.append(
                        f"paid group {group.identity.pgid} ESRCH inspection before "
                        f"SIGSTOP failed: {type(inspect_error).__name__}"
                    )
                    kill_groups.append(group)
                    continue
                if state == "empty":
                    already_empty.add(group.identity.pgid)
                else:
                    failures.append(
                        f"paid group {group.identity.pgid} vanished ambiguously before "
                        f"SIGSTOP: {exc}"
                    )
                    kill_groups.append(group)
                continue
            except BaseException as exc:
                failures.append(
                    f"paid group {group.identity.pgid} SIGSTOP failed: "
                    f"{type(exc).__name__}"
                )
                kill_groups.append(group)
                continue
            stopped.append(group.identity.pgid)
            kill_groups.append(group)

        if discover_after_stop is None:
            break
        discovery_rounds += 1
        if discovery_rounds > 16:
            failures.append("paid process-group discovery did not reach a fixed point")
            break
        try:
            discovered = tuple(discover_after_stop())
        except BaseException as exc:
            failures.append(
                f"paid process-group discovery after SIGSTOP failed: {type(exc).__name__}"
            )
            break
        new_groups: list[PaidGroup] = []
        for group in discovered:
            pgid = group.identity.pgid
            if pgid in conflicted_pgids:
                continue
            previous = canonical.get(pgid)
            if previous is not None:
                if previous != group:
                    canonical.pop(pgid, None)
                    conflicted_pgids.add(pgid)
                    failures.append(
                        f"conflicting discovered registration for PGID {pgid}"
                    )
                continue
            canonical[pgid] = group
            new_groups.append(group)
        if not new_groups:
            break
        pending = tuple(
            sorted(
                new_groups,
                key=lambda item: (item.role != "root", item.identity.pgid),
            )
        )
    killed: list[int] = []
    for group in kill_groups:
        # Once SIGSTOP succeeded, or returned response-unknown for the exact
        # live identity, there is no deliberate scheduling window in this
        # function in which that leader can legitimately be replaced.  A
        # second fallible inspector query must not strand a possibly stopped
        # root.  Signal the bound PGID immediately; only an ESRCH result is
        # reclassified as proven-empty versus ambiguous.
        try:
            signaler.killpg(group.identity.pgid, signal.SIGKILL)
        except ProcessLookupError as exc:
            try:
                state = _registered_group_state(group, inspector)
            except BaseException as inspect_error:
                failures.append(
                    f"paid group {group.identity.pgid} ESRCH inspection before SIGKILL "
                    f"failed: {type(inspect_error).__name__}"
                )
                continue
            if state == "empty":
                already_empty.add(group.identity.pgid)
            else:
                failures.append(
                    f"paid group {group.identity.pgid} became ambiguous before SIGKILL: {exc}"
                )
            continue
        except OSError as exc:
            failures.append(
                f"paid group {group.identity.pgid} SIGKILL failed: {type(exc).__name__}"
            )
            continue
        killed.append(group.identity.pgid)
    if reap is not None:
        killed_set = set(killed)
        for group in kill_groups:
            if group.identity.pgid not in killed_set:
                continue
            try:
                reap(group.identity)
            except BaseException as exc:
                failures.append(
                    f"paid group {group.identity.pgid} reap failed: {type(exc).__name__}"
                )
        _kill_bound_groups_to_fixed_point(
            tuple(
                group
                for group in kill_groups
                if group.identity.pgid in killed_set
            ),
            inspector=inspector,
            signaler=signaler,
            failures=failures,
        )
    receipt = StopReceipt(
        tuple(sorted(stopped)),
        tuple(sorted(killed)),
        tuple(sorted(already_empty)),
    )
    if failures:
        raise GroupStopFailure("; ".join(failures), receipt)
    return receipt


def wait_for_groups_empty(
    groups: Sequence[PaidGroup],
    *,
    inspector: ProcessInspector,
    timeout: float = 2.0,
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> None:
    """Boundedly prove all killed identities and process groups are empty."""

    deadline = monotonic_clock() + timeout
    while True:
        remaining: list[int] = []
        for group in groups:
            current = inspector.identity(group.identity.pid)
            members = inspector.group_members(group.identity.pgid)
            if current is not None or members:
                remaining.append(group.identity.pgid)
        if not remaining:
            return
        if monotonic_clock() >= deadline:
            raise IdentityViolation(
                "paid groups did not become empty after SIGKILL: "
                + ",".join(map(str, sorted(set(remaining))))
            )
        time.sleep(0.005)


@dataclass(frozen=True, slots=True)
class RegistrationRequest:
    run_id: str
    generation_control_instance_id: str
    watchdog_id: str
    root_group: PaidGroup
    owner_uid: int
    policy_digest: str
    boot_identity: str
    command_sha256: str
    lifeline_attached: bool

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "generation_control_instance_id",
            "watchdog_id",
            "policy_digest",
            "boot_identity",
            "command_sha256",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} is required")
        if self.owner_uid != self.root_group.identity.uid:
            raise ValueError("root process UID does not match owner UID")
        if not self.lifeline_attached:
            raise ValueError("guardian registration requires an inherited lifeline")

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "generation_control_instance_id": self.generation_control_instance_id,
            "watchdog_id": self.watchdog_id,
            "root_group": self.root_group.as_dict(),
            "owner_uid": self.owner_uid,
            "policy_digest": self.policy_digest,
            "boot_identity": self.boot_identity,
            "command_sha256": self.command_sha256,
            "lifeline_attached": self.lifeline_attached,
        }

    @property
    def request_sha256(self) -> str:
        return _sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class RegistrationAck:
    registration_id: str
    request_sha256: str
    durable: bool
    release_authorized: bool
    projection: DeadlineProjection

    def __post_init__(self) -> None:
        if not isinstance(self.registration_id, str) or not self.registration_id:
            raise ValueError("registration_id is required")
        if not _is_sha256(self.request_sha256):
            raise ValueError("request_sha256 must be a lowercase SHA-256 digest")
        if type(self.durable) is not bool or type(self.release_authorized) is not bool:
            raise ValueError("registration booleans must be exact booleans")


@dataclass(frozen=True, slots=True)
class PollSnapshot:
    sequence: int
    registration_id: str
    request_sha256: str
    boot_identity: str
    paid_groups: tuple[PaidGroup, ...]

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("poll sequence must be a nonnegative integer")
        if not isinstance(self.registration_id, str) or not self.registration_id:
            raise ValueError("registration_id is required")
        if not _is_sha256(self.request_sha256):
            raise ValueError("request_sha256 must be a lowercase SHA-256 digest")
        if not isinstance(self.boot_identity, str) or not self.boot_identity:
            raise ValueError("boot_identity is required")
        pgids = [item.identity.pgid for item in self.paid_groups]
        if len(pgids) != len(set(pgids)):
            raise ValueError("poll paid_groups must not contain duplicate PGIDs")

    @property
    def snapshot_sha256(self) -> str:
        return _sha256(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "registration_id": self.registration_id,
            "request_sha256": self.request_sha256,
            "boot_identity": self.boot_identity,
            "paid_groups": [item.as_dict() for item in self.paid_groups],
        }


@dataclass(frozen=True, slots=True)
class FinalizeReport:
    registration_id: str
    request_sha256: str
    state: str
    reason: str
    forced: bool
    direct_returncode: int | None
    stopped_pgids: tuple[int, ...]
    killed_pgids: tuple[int, ...]
    already_empty_pgids: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.state not in {"completed", "watchdog_forced", "execution_unknown"}:
            raise ValueError("invalid guardian final state")
        if not self.reason:
            raise ValueError("guardian final reason is required")
        if self.state == "watchdog_forced" and not self.forced:
            raise ValueError("watchdog_forced report must be forced")
        outcomes = (
            self.stopped_pgids,
            self.killed_pgids,
            self.already_empty_pgids,
        )
        if any(
            type(pgid) is not int or pgid <= 1
            for collection in outcomes
            for pgid in collection
        ):
            raise ValueError("guardian process-group receipts must contain PGIDs")
        if any(tuple(sorted(set(collection))) != collection for collection in outcomes):
            raise ValueError(
                "guardian process-group receipts must be sorted and unique"
            )
        if set(self.killed_pgids) & set(self.already_empty_pgids):
            raise ValueError("killed and already-empty PGIDs must be disjoint")

    def as_dict(self) -> dict[str, object]:
        return {
            "registration_id": self.registration_id,
            "request_sha256": self.request_sha256,
            "state": self.state,
            "reason": self.reason,
            "forced": self.forced,
            "direct_returncode": self.direct_returncode,
            "stopped_pgids": list(self.stopped_pgids),
            "killed_pgids": list(self.killed_pgids),
            "already_empty_pgids": list(self.already_empty_pgids),
        }


class GuardianCallbacks(Protocol):
    def register(self, request: RegistrationRequest) -> RegistrationAck: ...

    def poll(
        self,
        registration_id: str,
        discovered_groups: Sequence[PaidGroup] = (),
    ) -> PollSnapshot: ...

    def internal_interrupt(self, registration_id: str, request_sha256: str) -> None: ...

    def lifeline_lost(self, registration_id: str, request_sha256: str) -> None: ...

    def finalize(self, report: FinalizeReport) -> None: ...


class BlockedProcessGroup:
    """A setsid leader whose paid worker cannot exec before release."""

    def __init__(
        self,
        leader_pid: int,
        gate_fd: int,
        retire_fd: int,
        event_fd: int,
        command_sha256: str,
    ) -> None:
        self.leader_pid = leader_pid
        self.gate_fd = gate_fd
        self.retire_fd = retire_fd
        self.event_fd = event_fd
        self.command_sha256 = command_sha256
        self.released = False
        self._events = bytearray()
        self._worker_returncode: int | None = None
        self._leader_returncode: int | None = None
        self._shim_ready = False
        self._shim_error: str | None = None
        os.set_blocking(event_fd, False)

    @classmethod
    def spawn(
        cls,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        pass_fds: Sequence[int] = (),
        inspector: ProcessInspector | None = None,
        startup_timeout: float = 2.0,
    ) -> "BlockedProcessGroup":
        if not command or any(
            not isinstance(item, str) or not item or "\0" in item for item in command
        ):
            raise ValueError("command must be a nonempty string sequence")
        if not os.path.isabs(command[0]):
            raise ValueError("paid worker command must use an absolute executable path")
        worker_environment: dict[str, str] = {}
        for key, value in dict(env or {}).items():
            if (
                not isinstance(key, str)
                or not key
                or "=" in key
                or "\0" in key
                or not isinstance(value, str)
                or "\0" in value
            ):
                raise ValueError(
                    "paid worker environment must be explicit NUL-free strings"
                )
            worker_environment[key] = value
        normalized_pass_fds: list[int] = []
        for descriptor in pass_fds:
            if type(descriptor) is not int or descriptor < 0:
                raise ValueError("paid worker pass_fds must be open descriptors")
            if descriptor in normalized_pass_fds:
                raise ValueError("paid worker pass_fds must be unique")
            try:
                os.fstat(descriptor)
            except OSError as error:
                raise ValueError(
                    "paid worker pass_fds must be open descriptors"
                ) from error
            normalized_pass_fds.append(descriptor)
        if threading.active_count() != 1:
            raise GuardianError(
                "blocked process group must be forked by a single-threaded guardian"
            )
        inspector = SystemProcessInspector() if inspector is None else inspector
        command_tuple = tuple(command)
        command_sha256 = _sha256(list(command_tuple))
        gate_read, gate_write = os.pipe()
        retire_read, retire_write = os.pipe()
        event_read, event_write = os.pipe()
        config_read, config_write = os.pipe()
        config = _canonical_json(
            {
                "command": list(command_tuple),
                "environment": worker_environment,
                "pass_fds": normalized_pass_fds,
            }
        )
        if len(config) > _MAX_SHIM_CONFIG_BYTES:
            for descriptor in (
                gate_read,
                gate_write,
                retire_read,
                retire_write,
                event_read,
                event_write,
                config_read,
                config_write,
            ):
                os.close(descriptor)
            raise ValueError("paid worker configuration is too large")
        pid = os.fork()
        if pid == 0:  # pragma: no cover - behavior is observed from the guardian
            try:
                os.close(gate_write)
                os.close(retire_write)
                os.close(event_read)
                os.close(config_write)
                os.setsid()
                allowed = {
                    0,
                    1,
                    2,
                    gate_read,
                    retire_read,
                    event_write,
                    config_read,
                    *normalized_pass_fds,
                }
                _close_fds_except(allowed)
                for descriptor in allowed - {0, 1, 2}:
                    os.set_inheritable(descriptor, True)
                # A fork-only shim would retain the daemon's original kernel
                # environment even after os.environ.clear().  Re-exec an
                # embedded, already-attested shim with an empty environment so
                # neither /proc nor platform process inspection can recover the
                # owner or guardian capability.  No source path is reopened.
                os.environ.clear()
                os.execve(
                    sys.executable,
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-c",
                        _SHIM_SOURCE,
                        str(config_read),
                        str(gate_read),
                        str(retire_read),
                        str(event_write),
                    ],
                    {},
                )
            except BaseException as error:
                try:
                    os.write(
                        event_write,
                        (f"shim_error:{type(error).__name__}:{error}\n").encode(
                            "utf-8", errors="replace"
                        )[:4096],
                    )
                except BaseException:
                    pass
                os._exit(126)
        os.close(gate_read)
        os.close(retire_read)
        os.close(event_write)
        os.close(config_read)
        # Ownership transfers to the gate shim at fork.  The guardian daemon
        # must not retain the runner-control capability or keep its one-shot
        # pipe artificially live for the duration of the cycle.
        for descriptor in normalized_pass_fds:
            os.close(descriptor)
        try:
            _write_all(config_write, config)
            os.close(config_write)
        except BaseException:
            for descriptor in (config_write, gate_write, retire_write, event_read):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
            raise
        group = cls(pid, gate_write, retire_write, event_read, command_sha256)
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            identity = inspector.identity(pid)
            group._read_events()
            if (
                group._shim_ready
                and identity is not None
                and identity.pid == identity.pgid
            ):
                try:
                    if os.getsid(pid) == pid:
                        return group
                except ProcessLookupError:
                    break
            waited, _status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                break
            time.sleep(0.005)
        group.close_without_release()
        group.reap(timeout=0.5)
        detail = f": {group._shim_error}" if group._shim_error else ""
        raise GuardianError(
            "blocked child did not establish a stable session leader" + detail
        )

    def release(self) -> None:
        if self.released or self.gate_fd < 0:
            raise GuardianError("blocked process group release is not replayable")
        view = memoryview(_RELEASE_TOKEN)
        while view:
            written = os.write(self.gate_fd, view)
            if written <= 0:
                raise GuardianError("release gate write was short")
            view = view[written:]
        os.close(self.gate_fd)
        self.gate_fd = -1
        self.released = True

    def close_without_release(self) -> None:
        if self.gate_fd >= 0:
            os.close(self.gate_fd)
            self.gate_fd = -1

    def _read_events(self) -> None:
        if self.event_fd < 0:
            return
        while True:
            try:
                chunk = os.read(self.event_fd, 65_536)
            except BlockingIOError:
                break
            if not chunk:
                os.close(self.event_fd)
                self.event_fd = -1
                break
            self._events.extend(chunk)
        while b"\n" in self._events:
            raw, _, remaining = self._events.partition(b"\n")
            self._events = bytearray(remaining)
            if raw.startswith(b"worker_rc:"):
                self._worker_returncode = int(raw.split(b":", 1)[1])
            elif raw == b"shim_ready":
                self._shim_ready = True
            elif raw.startswith(b"shim_error:"):
                self._shim_error = raw.split(b":", 1)[1].decode(
                    "utf-8", errors="replace"
                )

    @property
    def worker_returncode(self) -> int | None:
        self._read_events()
        return self._worker_returncode

    def leader_returncode(self) -> int | None:
        if self._leader_returncode is not None:
            return self._leader_returncode
        try:
            waited, status = os.waitpid(self.leader_pid, os.WNOHANG)
        except ChildProcessError:
            raise IdentityViolation("stable leader was reaped outside the guardian")
        if waited == 0:
            return None
        self._leader_returncode = os.waitstatus_to_exitcode(status)
        return self._leader_returncode

    def retire_after_empty(self, inspector: ProcessInspector) -> int:
        returncode = self.worker_returncode
        if returncode is None:
            raise GuardianError("direct worker has not terminated")
        identity = inspector.identity(self.leader_pid)
        if identity is None or identity.pid != identity.pgid:
            raise IdentityViolation("stable leader exited before retirement")
        members = inspector.group_members(identity.pgid)
        residual = tuple(item for item in members if item.pid != self.leader_pid)
        if residual:
            raise ResidualDescendants(
                "direct rc observed with residual descendants: "
                + _residual_identity_diagnostic(residual)
            )
        view = memoryview(_RETIRE_TOKEN)
        while view:
            written = os.write(self.retire_fd, view)
            if written <= 0:
                raise GuardianError("retirement gate write was short")
            view = view[written:]
        os.close(self.retire_fd)
        self.retire_fd = -1
        return returncode

    def reap(self, timeout: float = 2.0) -> int | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            returncode = self.leader_returncode()
            if returncode is not None:
                return returncode
            time.sleep(0.005)
        return None

    def close(self) -> None:
        self.close_without_release()
        for name in ("retire_fd", "event_fd"):
            descriptor = getattr(self, name)
            if descriptor >= 0:
                os.close(descriptor)
                setattr(self, name, -1)


def _lifeline_eof(descriptor: int) -> bool:
    readable, _, _ = select.select([descriptor], [], [], 0)
    if not readable:
        return False
    try:
        return os.read(descriptor, 1) == b""
    except BlockingIOError:
        return False


def capture_descendant_process_groups(
    root_groups: Sequence[PaidGroup],
    *,
    registered_groups: Mapping[int, PaidGroup],
    candidate_groups: dict[int, PaidGroup],
    inspector: ProcessInspector,
    owner_uid: int,
    deadline_check: Callable[[], None] | None = None,
) -> None:
    """Stage exact same-UID descendant process groups without signalling.

    ``candidate_groups`` is deliberately updated with every exact group before
    an ambiguity is raised.  Failure cleanup can therefore STOP/KILL the safe
    subset while preserving an execution-unknown outcome for the ambiguity.
    The host must still durably attest candidates before normal execution may
    rely on them.
    """

    if type(owner_uid) is not int or owner_uid < 0:
        raise ValueError("descendant capture owner UID is invalid")
    ambiguities: list[int] = []
    for root_group in root_groups:
        if deadline_check is not None:
            deadline_check()
        if root_group.identity.uid != owner_uid:
            raise IdentityViolation("descendant capture root has the wrong UID")
        observed_root = inspector.identity(root_group.identity.pid)
        if observed_root is None:
            try:
                members = inspector.group_members(root_group.identity.pgid)
            except IdentityViolation:
                # Keep the already-bound root in durable/local coverage while
                # Darwin resolves a just-exited process-group visibility
                # window.  Poll retirement or terminal cleanup must still
                # obtain a reliable empty/live classification later.
                continue
            if not members:
                continue
            if any(
                member.uid != owner_uid
                or member.pgid != root_group.identity.pgid
                or member.pid == root_group.identity.pgid
                for member in members
            ):
                ambiguities.append(root_group.identity.pgid)
                continue
            scan_seeds = members
        elif observed_root != root_group.identity:
            ambiguities.append(root_group.identity.pgid)
            continue
        else:
            scan_seeds = (root_group.identity,)
        for seed in scan_seeds:
            observed_seed = inspector.identity(seed.pid)
            if observed_seed is None:
                continue
            if observed_seed != seed:
                ambiguities.append(seed.pgid)
                continue
            for descendant in inspector.descendants(seed.pid):
                if deadline_check is not None:
                    deadline_check()
                if descendant.pgid in registered_groups:
                    continue
                existing_candidate = candidate_groups.get(descendant.pgid)
                if existing_candidate is not None:
                    # One process-group snapshot can contain both the leader
                    # and its members.  Once the exact leader has been staged,
                    # later members of that same group must not be asked to
                    # reconstruct a leader which may already have exited.
                    try:
                        candidate_state = _stabilized_registered_group_state(
                            existing_candidate, inspector
                        )
                    except IdentityViolation:
                        # Keep the exact staged identity for the next main-loop
                        # poll. Sleeping per group here can consume the entire
                        # hard-stop lead window.
                        continue
                    if candidate_state == "ambiguous":
                        ambiguities.append(descendant.pgid)
                    continue
                if descendant.uid != owner_uid:
                    ambiguities.append(descendant.pgid)
                    continue
                observed_descendant = inspector.identity(descendant.pid)
                if observed_descendant is None:
                    # Codex creates short-lived helper process groups while a
                    # sub-agent is materializing.  If the exact descendant
                    # snapshot identified the group leader, retain that bound
                    # identity while the group is empty or has only same-UID
                    # leaderless members.  POSIX cannot reuse the numeric PGID
                    # while those members keep the original group alive.  A
                    # non-leader cannot reconstruct a vanished leader identity.
                    if descendant.pid != descendant.pgid:
                        ambiguities.append(descendant.pgid)
                        continue
                    leader = descendant
                elif observed_descendant != descendant:
                    ambiguities.append(descendant.pgid)
                    continue
                else:
                    leader = (
                        descendant
                        if descendant.pid == descendant.pgid
                        else inspector.identity(descendant.pgid)
                    )
                if (
                    leader is None
                    or leader.pid != leader.pgid
                    or leader.uid != owner_uid
                ):
                    ambiguities.append(descendant.pgid)
                    continue
                staged = PaidGroup("root", leader)
                try:
                    leader_state = _stabilized_registered_group_state(
                        staged, inspector
                    )
                except IdentityViolation:
                    # The descendant snapshot already supplied the exact
                    # same-UID leader. Stage it for the next durable poll.
                    candidate_groups[leader.pgid] = staged
                    continue
                if leader_state == "ambiguous":
                    ambiguities.append(leader.pgid)
                    continue
                existing = candidate_groups.get(leader.pgid)
                if existing is not None and existing.identity != leader:
                    ambiguities.append(leader.pgid)
                    continue
                candidate_groups[leader.pgid] = staged
            observed_seed_after = inspector.identity(seed.pid)
            if observed_seed_after is not None and observed_seed_after != seed:
                ambiguities.append(seed.pgid)
    if ambiguities:
        raise IdentityViolation(
            "paid descendants escaped into ambiguous process groups: "
            + ",".join(map(str, sorted(set(ambiguities))))
        )


class Guardian:
    """Callback-driven guardian loop; no HotJoin response keys are assumed."""

    def __init__(
        self,
        callbacks: GuardianCallbacks,
        *,
        inspector: ProcessInspector | None = None,
        signaler: GroupSignaler | None = None,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        poll_interval: float = 0.05,
        reap_group: Callable[[ProcessIdentity], None] | None = None,
        host_callback_timeout: float = 2.0,
        registration_timeout: float = 5.0,
        finalize_timeout: float = 5.0,
        durably_attest_discovered_groups: bool = False,
    ) -> None:
        self.callbacks = callbacks
        self.inspector = SystemProcessInspector() if inspector is None else inspector
        self.signaler = OSGroupSignaler() if signaler is None else signaler
        self.wall_clock = wall_clock
        self.monotonic_clock = monotonic_clock
        self.poll_interval = poll_interval
        self.reap_group = reap_group
        self.host_callback_timeout = _finite_number(
            host_callback_timeout, "host_callback_timeout"
        )
        self.registration_timeout = _finite_number(
            registration_timeout, "registration_timeout"
        )
        self.finalize_timeout = _finite_number(finalize_timeout, "finalize_timeout")
        if type(durably_attest_discovered_groups) is not bool:
            raise ValueError("durably_attest_discovered_groups must be an exact boolean")
        self.durably_attest_discovered_groups = durably_attest_discovered_groups
        if (
            min(
                self.host_callback_timeout,
                self.registration_timeout,
                self.finalize_timeout,
            )
            <= 0
        ):
            raise ValueError("guardian callback timeouts must be positive")

    @staticmethod
    def _validate_poll(
        snapshot: PollSnapshot,
        ack: RegistrationAck,
        request: RegistrationRequest,
    ) -> None:
        if (
            snapshot.registration_id != ack.registration_id
            or snapshot.request_sha256 != request.request_sha256
            or snapshot.boot_identity != request.boot_identity
        ):
            raise HostControlFailure("host poll is not bound to this registration")

    @staticmethod
    def _call_once_bounded(
        callback: Callable[[], object],
        *,
        timeout: float,
        operation: str,
        monitor: Callable[[], None] | None = None,
    ) -> object:
        if timeout <= 0:
            raise HostCallbackTimeout(f"{operation} reached its safety deadline")
        outcomes: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                outcomes.put((True, callback()), block=False)
            except BaseException as error:
                outcomes.put((False, error), block=False)

        thread = threading.Thread(
            target=invoke,
            name=f"rethlas-guardian-{operation}",
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HostCallbackTimeout(
                    f"{operation} did not return before its safety deadline"
                )
            try:
                succeeded, value = outcomes.get(timeout=min(0.02, remaining))
                break
            except queue.Empty:
                if monitor is not None:
                    monitor()
        if succeeded:
            return value
        assert isinstance(value, BaseException)
        raise value

    @classmethod
    def _call_with_one_exact_replay(
        cls,
        callback: Callable[[], object],
        *,
        timeout: float,
        operation: str,
        monitor: Callable[[], None] | None = None,
    ) -> object:
        """Retry one response-unknown callback with byte-for-byte same inputs."""

        first_error: BaseException | None = None
        deadline = time.monotonic() + timeout
        for _attempt in range(2):
            try:
                return cls._call_once_bounded(
                    callback,
                    timeout=max(0.0, deadline - time.monotonic()),
                    operation=operation,
                    monitor=monitor,
                )
            except (IdentityViolation, ClockViolation, _HardStopDue):
                raise
            except BaseException as error:
                if first_error is None:
                    first_error = error
        assert first_error is not None
        raise HostControlFailure(
            f"durable host callback failed twice: {first_error}"
        ) from first_error

    def _capture_escaped_groups(
        self,
        *,
        root_group: PaidGroup,
        registered_groups: dict[int, PaidGroup],
        candidate_groups: dict[int, PaidGroup],
        deadline_check: Callable[[], None] | None = None,
    ) -> None:
        """Stage exact same-UID setsid descendants for host attestation or kill."""

        # A durably known setsid leader may be reparented away from the
        # original root and later fork another setsid group.  Scan the dynamic
        # exact union to a fixed point so both normal polling and failure
        # cleanup retain coverage of that second generation.
        for _round in range(16):
            if deadline_check is not None:
                deadline_check()
            roots_by_pgid = {
                root_group.identity.pgid: root_group,
                **registered_groups,
                **candidate_groups,
            }
            before = set(candidate_groups)
            capture_descendant_process_groups(
                tuple(
                    roots_by_pgid[pgid] for pgid in sorted(roots_by_pgid)
                ),
                registered_groups=registered_groups,
                candidate_groups=candidate_groups,
                inspector=self.inspector,
                owner_uid=os.getuid(),
                deadline_check=deadline_check,
            )
            if set(candidate_groups) == before:
                return
        raise IdentityViolation(
            "paid descendant capture did not reach a fixed point"
        )

    def run(
        self,
        command: Sequence[str],
        *,
        run_id: str,
        generation_control_instance_id: str,
        watchdog_id: str,
        policy_digest: str,
        lifeline_fd: int,
        monitor_only: bool = False,
        env: Mapping[str, str] | None = None,
        pass_fds: Sequence[int] = (),
    ) -> FinalizeReport:
        boot_identity = self.inspector.boot_identity()
        child = BlockedProcessGroup.spawn(
            command,
            env=env,
            pass_fds=pass_fds,
            inspector=self.inspector,
        )
        registration_id = "unregistered"
        request_sha256 = "unregistered"
        registered_groups: dict[int, PaidGroup] = {}
        candidate_groups: dict[int, PaidGroup] = {}
        candidate_first_missing_poll: dict[int, int] = {}
        proven_empty_pgids: set[int] = set()
        stop_receipt = StopReceipt((), (), ())
        released = False
        direct_returncode: int | None = None
        terminal_report: FinalizeReport | None = None
        terminal_cleanup: _TerminalCleanupOutcome | None = None
        terminal_cleanup_started = False
        clock: GuardianClock | None = None
        try:
            identity = self.inspector.identity(child.leader_pid)
            if identity is None or identity.pid != identity.pgid:
                raise IdentityViolation("cannot bind stable root group identity")
            root_group = PaidGroup("root", identity)
            request = RegistrationRequest(
                run_id=run_id,
                generation_control_instance_id=generation_control_instance_id,
                watchdog_id=watchdog_id,
                root_group=root_group,
                owner_uid=os.getuid(),
                policy_digest=policy_digest,
                boot_identity=boot_identity,
                command_sha256=child.command_sha256,
                lifeline_attached=lifeline_fd >= 0,
            )
            request_sha256 = request.request_sha256
            ack = self._call_with_one_exact_replay(
                lambda: self.callbacks.register(request),
                timeout=self.registration_timeout,
                operation="register",
            )
            if not isinstance(ack, RegistrationAck):
                raise HostControlFailure(
                    "registration returned a malformed acknowledgement"
                )
            registration_id = ack.registration_id
            if (
                not ack.durable
                or not ack.release_authorized
                or ack.request_sha256 != request_sha256
                or ack.projection.boot_identity != boot_identity
            ):
                raise HostControlFailure(
                    "registration did not durably authorize release"
                )
            clock = GuardianClock(
                ack.projection,
                boot_identity=boot_identity,
                wall_clock=self.wall_clock,
                monotonic_clock=self.monotonic_clock,
            )
            registered_groups[identity.pgid] = root_group

            def check_capture_deadline() -> None:
                if not monitor_only and clock.hard_stop_due():
                    raise _HardStopDue(
                        "absolute hard stop during descendant capture"
                    )

            def monitor_paid_descendants() -> None:
                check_capture_deadline()
                self._capture_escaped_groups(
                    root_group=root_group,
                    registered_groups=registered_groups,
                    candidate_groups=candidate_groups,
                    deadline_check=check_capture_deadline,
                )

            def clean_released_topology() -> _TerminalCleanupOutcome:
                """Immediately freeze, kill, and prove the exact paid topology empty."""

                nonlocal terminal_cleanup, terminal_cleanup_started
                if terminal_cleanup is not None:
                    return terminal_cleanup
                if terminal_cleanup_started:
                    # This path is synchronous.  Re-entry would mean the first
                    # terminal signal outcome was lost, so it must not emit a
                    # second STOP/KILL sequence or claim a clean completion.
                    return _TerminalCleanupOutcome(
                        StopReceipt((), (), ()),
                        tuple(registered_groups.values()),
                        False,
                        "terminal cleanup was re-entered after an unknown response",
                    )
                terminal_cleanup_started = True
                failures: list[str] = []
                durable_pgids = set(registered_groups)

                def discover_stopped_descendants() -> tuple[PaidGroup, ...]:
                    try:
                        self._capture_escaped_groups(
                            root_group=root_group,
                            registered_groups=registered_groups,
                            candidate_groups=candidate_groups,
                        )
                    except IdentityViolation as capture_error:
                        # Capture retains every exact group found before an
                        # ambiguity.  They still join the frozen kill set, but
                        # the ambiguity prevents a completed terminal claim.
                        failures.append(str(capture_error))
                    discovered: list[PaidGroup] = []
                    for pgid, candidate in candidate_groups.items():
                        if pgid in proven_empty_pgids:
                            failures.append(
                                "terminal cleanup observed reuse of a historically "
                                "empty PGID: "
                                + _residual_identity_diagnostic((candidate.identity,))
                            )
                        existing = registered_groups.get(pgid)
                        if (
                            existing is not None
                            and existing.identity != candidate.identity
                        ):
                            failures.append(
                                "candidate conflicted with a registered process group"
                            )
                            continue
                        if existing is None:
                            registered_groups[pgid] = candidate
                            discovered.append(candidate)
                            failures.append(
                                "terminal cleanup captured an unattested paid group: "
                                + _residual_identity_diagnostic((candidate.identity,))
                            )
                    return tuple(discovered)

                stop_failure: GroupStopFailure | None = None
                try:
                    receipt = stop_then_kill_groups(
                        tuple(registered_groups.values()),
                        inspector=self.inspector,
                        signaler=self.signaler,
                        reap=lambda item: (
                            child.reap()
                            if item.pid == child.leader_pid
                            else (
                                self.reap_group(item)
                                if self.reap_group is not None
                                else None
                            )
                        ),
                        # The exact root and every known auxiliary are frozen
                        # before this callback.  Each newly found setsid group
                        # is STOPped before discovery repeats to a fixed point.
                        discover_after_stop=discover_stopped_descendants,
                    )
                except GroupStopFailure as stop_error:
                    stop_failure = stop_error
                    receipt = stop_error.receipt
                    failures.append(str(stop_error))
                except BaseException as stop_error:
                    receipt = StopReceipt((), (), ())
                    failures.append(f"{type(stop_error).__name__}:{stop_error}")
                groups = tuple(
                    sorted(
                        registered_groups.values(),
                        key=lambda item: item.identity.pgid,
                    )
                )
                empty = False
                try:
                    wait_for_groups_empty(groups, inspector=self.inspector)
                    final_pgids = set(registered_groups)
                    empty = (
                        stop_failure is None
                        and not failures
                        and not candidate_groups
                        and final_pgids == durable_pgids
                        and set(receipt.stopped_pgids)
                        <= (
                            set(receipt.killed_pgids)
                            | set(receipt.already_empty_pgids)
                        )
                        and durable_pgids
                        == (
                            set(receipt.killed_pgids)
                            | set(receipt.already_empty_pgids)
                        )
                    )
                except BaseException as empty_error:
                    failures.append(f"{type(empty_error).__name__}:{empty_error}")
                terminal_cleanup = _TerminalCleanupOutcome(
                    receipt,
                    groups,
                    empty,
                    ";".join(dict.fromkeys(failures)) if failures else None,
                )
                return terminal_cleanup

            if not monitor_only and clock.hard_stop_due():
                child.close_without_release()
                if child.reap(timeout=1.0) is None:
                    raise IdentityViolation(
                        "blocked leader did not exit at an elapsed T150"
                    )
                terminal_report = FinalizeReport(
                    registration_id,
                    request_sha256,
                    "watchdog_forced",
                    "hard_stop_due_before_release",
                    True,
                    None,
                    (),
                    (),
                    (identity.pgid,),
                )
                self._call_with_one_exact_replay(
                    lambda: self.callbacks.finalize(terminal_report),
                    timeout=self.finalize_timeout,
                    operation="finalize",
                )
                return terminal_report
            child.release()
            released = True
            last_sequence = -1
            last_poll_digest: str | None = None
            completed_poll_ordinal = 0
            interrupt_sent = False
            lifeline_reported = False
            while True:
                if not monitor_only and clock.hard_stop_due():
                    raise _HardStopDue("absolute hard stop")
                if not monitor_only and not interrupt_sent and clock.interrupt_due():
                    self._call_with_one_exact_replay(
                        lambda: self.callbacks.internal_interrupt(
                            registration_id, request_sha256
                        ),
                        timeout=(
                            self.host_callback_timeout
                            if monitor_only
                            else min(
                                self.host_callback_timeout,
                                clock.seconds_until_hard_stop(),
                            )
                        ),
                        operation="internal_interrupt",
                        monitor=monitor_paid_descendants,
                    )
                    interrupt_sent = True
                    # The callback may have consumed the entire five-second
                    # window.  Re-enter at the hard-stop check before polling.
                    continue
                if not lifeline_reported and _lifeline_eof(lifeline_fd):
                    # Persist wrapper death before changing process state. The
                    # bounded mode then continues independently to the same
                    # T150 boundary; monitor-only treats lifeline loss as a
                    # safety emergency and cleans the protected topology.
                    self._call_with_one_exact_replay(
                        lambda: self.callbacks.lifeline_lost(
                            registration_id, request_sha256
                        ),
                        timeout=(
                            self.host_callback_timeout
                            if monitor_only
                            else min(
                                self.host_callback_timeout,
                                clock.seconds_until_hard_stop(),
                            )
                        ),
                        operation="lifeline_lost",
                        monitor=monitor_paid_descendants,
                    )
                    lifeline_reported = True
                    if monitor_only:
                        raise HostControlFailure(
                            "continuous supervisor lifeline was lost"
                        )
                    continue
                submitted_candidates: tuple[PaidGroup, ...] = ()
                if self.durably_attest_discovered_groups:
                    # Opaque workers cannot use the runner-scoped paid-group
                    # prepare/release RPCs.  Send every exact locally staged
                    # setsid group to the guardian-scoped host transaction;
                    # it must durably validate and echo each still-live group
                    # in this same snapshot before local-only tracking ends.
                    monitor_paid_descendants()
                    submitted_candidates = tuple(
                        sorted(
                            candidate_groups.values(),
                            key=lambda item: item.identity.pgid,
                        )
                    )
                if self.durably_attest_discovered_groups:
                    def poll_callback() -> object:
                        return self.callbacks.poll(
                            registration_id, submitted_candidates
                        )
                else:
                    def poll_callback() -> object:
                        return self.callbacks.poll(registration_id)
                snapshot = self._call_once_bounded(
                    poll_callback,
                    timeout=(
                        self.host_callback_timeout
                        if monitor_only
                        else min(
                            self.host_callback_timeout,
                            clock.seconds_until_next_boundary(
                                interrupt_sent=interrupt_sent
                            ),
                        )
                    ),
                    operation="poll",
                    monitor=monitor_paid_descendants,
                )
                if not isinstance(snapshot, PollSnapshot):
                    raise HostControlFailure("host poll returned a malformed snapshot")
                self._validate_poll(snapshot, ack, request)
                submitted_candidate_pgids = {
                    item.identity.pgid for item in submitted_candidates
                }
                digest = snapshot.snapshot_sha256
                if snapshot.sequence < last_sequence:
                    raise HostControlFailure("host poll sequence rolled backwards")
                if snapshot.sequence == last_sequence:
                    if digest != last_poll_digest:
                        raise HostControlFailure(
                            "duplicate host poll sequence equivocated"
                        )
                else:
                    last_sequence = snapshot.sequence
                    last_poll_digest = digest
                completed_poll_ordinal += 1
                for group in snapshot.paid_groups:
                    existing = registered_groups.get(group.identity.pgid)
                    if existing is not None and existing != group:
                        raise HostControlFailure("host reused a registered PGID")
                    if group.identity.uid != os.getuid():
                        raise IdentityViolation(
                            "paid auxiliary group has the wrong UID"
                        )
                    candidate = candidate_groups.get(group.identity.pgid)
                    if candidate is not None and candidate.identity != group.identity:
                        raise HostControlFailure(
                            "host attested a different identity for a staged paid group"
                        )
                    current = self.inspector.identity(group.identity.pid)
                    if current != group.identity:
                        if current is not None:
                            raise IdentityViolation(
                                "host snapshot paid-group PID was reused"
                            )
                        try:
                            members = self.inspector.group_members(
                                group.identity.pgid
                            )
                        except IdentityViolation:
                            # Darwin can briefly expose no enumerable members
                            # while killpg(0) still reports the just-exited
                            # group.  The host has already durably attested the
                            # exact identity, so retain it as registered kill
                            # coverage and resolve it on the next poll.  Root
                            # completion still requires a later empty proof.
                            candidate_groups.pop(group.identity.pgid, None)
                            candidate_first_missing_poll.pop(
                                group.identity.pgid, None
                            )
                            registered_groups[group.identity.pgid] = group
                            continue
                        except BaseException as inspect_error:
                            raise IdentityViolation(
                                "host snapshot paid-group empty proof was unavailable"
                            ) from inspect_error
                        if members:
                            if (
                                group.identity.pgid
                                == root_group.identity.pgid
                                or any(
                                    member.pgid != group.identity.pgid
                                    or member.uid != group.identity.uid
                                    or member.pid == group.identity.pgid
                                    for member in members
                                )
                            ):
                                raise IdentityViolation(
                                    "host snapshot paid-group leader vanished with ambiguous members"
                                )
                            raise ResidualDescendants(
                                "host snapshot paid-group leader exited with residual members"
                            )
                        if group.identity.pgid == root_group.identity.pgid:
                            raise IdentityViolation(
                                "stable root exited before snapshot validation"
                            )
                        # The host committed an exact identity, but that group
                        # naturally became empty before the snapshot reached
                        # this daemon.  Preserve its content-bound empty proof
                        # instead of converting a safe exit race into failure.
                        candidate_groups.pop(group.identity.pgid, None)
                        candidate_first_missing_poll.pop(
                            group.identity.pgid, None
                        )
                        registered_groups.pop(group.identity.pgid, None)
                        proven_empty_pgids.add(group.identity.pgid)
                        continue
                    candidate_groups.pop(group.identity.pgid, None)
                    candidate_first_missing_poll.pop(group.identity.pgid, None)
                    registered_groups[group.identity.pgid] = group
                # Omitting a still-live previously registered group is not a
                # terminal receipt and cannot silently remove kill coverage.
                polled_pgids = {item.identity.pgid for item in snapshot.paid_groups}
                retired_pgids: list[int] = []
                for pgid, group in registered_groups.items():
                    if pgid == root_group.identity.pgid or pgid in polled_pgids:
                        continue
                    current = self.inspector.identity(group.identity.pid)
                    if current == group.identity:
                        raise HostControlFailure("host poll omitted a live paid group")
                    if current is not None:
                        raise IdentityViolation("omitted paid group PID was reused")
                    try:
                        members = self.inspector.group_members(pgid)
                    except IdentityViolation:
                        # A just-exited group may still exist as a kernel
                        # object while native enumeration exposes no live
                        # leader.  Keep its durable registration and retry;
                        # never turn that transient state into an empty proof.
                        continue
                    if members:
                        raise IdentityViolation("omitted paid group PGID was reused")
                    retired_pgids.append(pgid)
                for pgid in retired_pgids:
                    del registered_groups[pgid]
                    proven_empty_pgids.add(pgid)
                monitor_paid_descendants()
                retired_candidates: list[int] = []
                for pgid, group in candidate_groups.items():
                    try:
                        state = _stabilized_registered_group_state(
                            group, self.inspector
                        )
                    except IdentityViolation:
                        if self.durably_attest_discovered_groups:
                            # A group discovered after the completed host poll
                            # can exit inside Darwin's enumeration window.  It
                            # remains staged for the next durable poll, which
                            # must either attest the exact identity or record
                            # its already-empty disposition.  Do not signal or
                            # locally launder it into terminal coverage.
                            continue
                        raise
                    if state == "empty":
                        if (
                            self.durably_attest_discovered_groups
                            and pgid not in submitted_candidate_pgids
                        ):
                            # Keep a locally vanished exact candidate until a
                            # successful host poll can durably record its
                            # already-empty disposition.
                            continue
                        retired_candidates.append(pgid)
                        # Only a candidate included in this successfully
                        # completed host poll has durable terminal coverage.
                        # A helper discovered by the post-poll scan can exit
                        # before the next submission; retire it locally without
                        # laundering that unknown PGID into the final receipt.
                        if pgid in submitted_candidate_pgids:
                            proven_empty_pgids.add(pgid)
                        continue
                    if state == "ambiguous":
                        raise IdentityViolation(
                            "unattested paid candidate identity became ambiguous"
                        )
                    if self.durably_attest_discovered_groups:
                        if any(
                            item.identity.pgid == pgid
                            for item in submitted_candidates
                        ):
                            raise HostControlFailure(
                                "durable host poll omitted a discovered paid group"
                            )
                    else:
                        first_missing = candidate_first_missing_poll.get(pgid)
                        if first_missing is None:
                            candidate_first_missing_poll[pgid] = completed_poll_ordinal
                        elif completed_poll_ordinal > first_missing:
                            raise IdentityViolation(
                                "exact setsid descendant was not attested by the next host poll"
                            )
                for pgid in retired_candidates:
                    candidate_groups.pop(pgid, None)
                    candidate_first_missing_poll.pop(pgid, None)
                direct_returncode = child.worker_returncode
                if direct_returncode is not None:
                    if not monitor_only and clock.hard_stop_due():
                        raise _HardStopDue(
                            "absolute hard stop before direct-return empty proof"
                        )
                    cleanup_trigger: ResidualDescendants | None = None
                    try:
                        if candidate_groups:
                            raise ResidualDescendants(
                                "root rc observed with unattested paid candidates: "
                                + _residual_identity_diagnostic(
                                    tuple(
                                        item.identity
                                        for item in candidate_groups.values()
                                    )
                                )
                            )
                        _require_auxiliary_groups_empty(
                            tuple(registered_groups.values()),
                            root_pgid=root_group.identity.pgid,
                            inspector=self.inspector,
                        )
                        child.retire_after_empty(self.inspector)
                    except ResidualDescendants as residual_error:
                        cleanup_trigger = residual_error
                    if (
                        cleanup_trigger is not None
                        and self.durably_attest_discovered_groups
                    ):
                        # A normal worker return can race with Codex teardown
                        # helpers created after the preceding host poll.  Keep
                        # the root shim and Guardian alive so the next poll can
                        # durably attest or retire every exact group.  No next
                        # paid turn is authorized before finalization, and the
                        # original absolute hard stop still bounds this drain.
                        remaining = (
                            self.poll_interval
                            if monitor_only
                            else clock.seconds_until_next_boundary(
                                interrupt_sent=interrupt_sent
                            )
                        )
                        time.sleep(min(self.poll_interval, remaining))
                        continue
                    if cleanup_trigger is None:
                        if child.reap() is None:
                            raise IdentityViolation(
                                "stable leader did not reap after retirement"
                            )
                        if not monitor_only and clock.hard_stop_due():
                            raise _HardStopDue(
                                "absolute hard stop before natural empty proof"
                            )
                        terminal_report = FinalizeReport(
                            registration_id,
                            request_sha256,
                            "completed",
                            "paid_group_empty",
                            False,
                            direct_returncode,
                            (),
                            (),
                            tuple(
                                sorted(proven_empty_pgids | set(registered_groups))
                            ),
                        )
                    else:
                        cleanup = clean_released_topology()
                        if not cleanup.proven_empty:
                            raise ResidualDescendants(
                                str(cleanup_trigger)
                                + ";terminal cleanup failed:"
                                + str(cleanup.failure_reason or "empty proof unavailable")
                            )
                        if not monitor_only and clock.hard_stop_due():
                            raise _HardStopDue(
                                "absolute hard stop before terminal cleanup empty proof"
                            )
                        killed = set(cleanup.receipt.killed_pgids)
                        terminal_report = FinalizeReport(
                            registration_id,
                            request_sha256,
                            "completed",
                            "paid_worker_returned_group_cleanup",
                            False,
                            direct_returncode,
                            cleanup.receipt.stopped_pgids,
                            cleanup.receipt.killed_pgids,
                            tuple(
                                sorted(
                                    (
                                        proven_empty_pgids
                                        | set(cleanup.receipt.already_empty_pgids)
                                    )
                                    - killed
                                )
                            ),
                        )
                    self._call_with_one_exact_replay(
                        lambda: self.callbacks.finalize(terminal_report),
                        timeout=self.finalize_timeout,
                        operation="finalize",
                    )
                    return terminal_report
                if child.leader_returncode() is not None:
                    raise IdentityViolation(
                        "stable leader exited before the paid worker"
                    )
                remaining = (
                    self.poll_interval
                    if monitor_only
                    else clock.seconds_until_next_boundary(
                        interrupt_sent=interrupt_sent
                    )
                )
                time.sleep(min(self.poll_interval, remaining))
        except BaseException as error:
            child.close_without_release()
            if not released:
                child.reap(timeout=1.0)
            if terminal_report is not None:
                # The group was already proven terminal.  Never replace a
                # response-unknown finalize with a different marker; recovery
                # replays the exact same content-bound report.
                return dataclasses.replace(
                    terminal_report,
                    state="execution_unknown",
                    reason=f"finalize_response_unknown:{type(error).__name__}",
                )
            groups_proven_empty = False
            cleanup_failure_reason: str | None = None
            if released and (registered_groups or candidate_groups):
                cleanup = clean_released_topology()
                killed = set(cleanup.receipt.killed_pgids)
                stop_receipt = StopReceipt(
                    cleanup.receipt.stopped_pgids,
                    cleanup.receipt.killed_pgids,
                    tuple(
                        sorted(
                            (
                                set(cleanup.receipt.already_empty_pgids)
                                | proven_empty_pgids
                            )
                            - killed
                        )
                    ),
                )
                groups_proven_empty = cleanup.proven_empty
                cleanup_failure_reason = cleanup.failure_reason
            crossed_hard_stop = False
            if clock is not None and not monitor_only:
                try:
                    crossed_hard_stop = clock.hard_stop_due()
                except ClockViolation:
                    crossed_hard_stop = False
            hard_stop_completed = (
                released
                and bool(registered_groups)
                and groups_proven_empty
                and cleanup_failure_reason is None
                and (
                    set(stop_receipt.killed_pgids)
                    | set(stop_receipt.already_empty_pgids)
                )
                == (set(registered_groups) | proven_empty_pgids)
                and (isinstance(error, _HardStopDue) or crossed_hard_stop)
            )
            report = FinalizeReport(
                registration_id,
                request_sha256,
                "watchdog_forced" if hard_stop_completed else "execution_unknown",
                (
                    "absolute_hard_stop"
                    if hard_stop_completed
                    else (
                        f"{type(error).__name__}:{error}"
                        + (
                            f";cleanup:{cleanup_failure_reason}"
                            if cleanup_failure_reason
                            else ""
                        )
                    )
                ),
                released,
                direct_returncode,
                stop_receipt.stopped_pgids,
                stop_receipt.killed_pgids,
                stop_receipt.already_empty_pgids,
            )
            if registration_id != "unregistered":
                try:
                    self._call_with_one_exact_replay(
                        lambda: self.callbacks.finalize(report),
                        timeout=self.finalize_timeout,
                        operation="finalize",
                    )
                except BaseException:
                    pass
            return report
        finally:
            if not released:
                # Closing the sole write end makes the shim observe EOF and
                # guarantees zero exec.  Reap it so a rejected registration or
                # clock projection cannot leave a zombie leader behind.
                child.close_without_release()
                child.reap(timeout=1.0)
            else:
                # Also reap an externally killed/early-exit stable leader.
                child.reap(timeout=0.05)
            child.close()


__all__ = [
    "BlockedProcessGroup",
    "ClockViolation",
    "DeadlineProjection",
    "FinalizeReport",
    "Guardian",
    "GuardianCallbacks",
    "GuardianClock",
    "GuardianError",
    "GroupStopFailure",
    "HostControlFailure",
    "IdentityViolation",
    "OSGroupSignaler",
    "PaidGroup",
    "PollSnapshot",
    "ProcessIdentity",
    "ProcessInspector",
    "RegistrationAck",
    "RegistrationRequest",
    "ResidualDescendants",
    "StopReceipt",
    "SystemProcessInspector",
    "capture_descendant_process_groups",
    "revalidate_identity",
    "stop_then_kill_groups",
    "wait_for_groups_empty",
]
