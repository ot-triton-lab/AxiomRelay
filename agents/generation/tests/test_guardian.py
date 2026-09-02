from __future__ import annotations

import ctypes
import hashlib
import os
import json
import signal
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agents.generation import guardian as guardian_module
from agents.generation.guardian import (
    BlockedProcessGroup,
    ClockViolation,
    DeadlineProjection,
    Guardian,
    GuardianClock,
    GroupStopFailure,
    IdentityViolation,
    MAX_PROJECTION_SKEW_SECONDS,
    PaidGroup,
    PollSnapshot,
    ProcessIdentity,
    RegistrationAck,
    RegistrationRequest,
    ResidualDescendants,
    SystemProcessInspector,
    stop_then_kill_groups,
    wait_for_groups_empty,
)


class MutableClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeInspector:
    def __init__(self, identities: list[ProcessIdentity], boot: str = "boot-1") -> None:
        self.identities = {item.pid: item for item in identities}
        self.boot = boot

    def boot_identity(self) -> str:
        return self.boot

    def identity(self, pid: int) -> ProcessIdentity | None:
        return self.identities.get(pid)

    def group_members(self, pgid: int) -> tuple[ProcessIdentity, ...]:
        return tuple(
            sorted(
                (item for item in self.identities.values() if item.pgid == pgid),
                key=lambda item: item.pid,
            )
        )

    def descendants(self, pid: int) -> tuple[ProcessIdentity, ...]:
        return ()


class FakeSignaler:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def killpg(self, pgid: int, sig: int) -> None:
        self.calls.append((pgid, sig))


class RecordingOSSignaler(FakeSignaler):
    def killpg(self, pgid: int, sig: int) -> None:
        super().killpg(pgid, sig)
        os.killpg(pgid, sig)


def projection(
    *,
    start: float = 100.0,
    projected_wall: float = 1_000.0,
    projected_monotonic: float = 50.0,
    boot: str = "boot-1",
) -> DeadlineProjection:
    return DeadlineProjection(
        cycle_started_wall_epoch=start,
        cycle_started_monotonic=projected_monotonic - (projected_wall - start),
        internal_interrupt_wall_epoch=start + 8_995.0,
        internal_interrupt_monotonic=(
            projected_monotonic - (projected_wall - start) + 8_995.0
        ),
        hard_stop_wall_epoch=start + 9_000.0,
        hard_stop_monotonic=(projected_monotonic - (projected_wall - start) + 9_000.0),
        projected_wall_epoch=projected_wall,
        projected_monotonic=projected_monotonic,
        boot_identity=boot,
    )


def imminent_projection(
    inspector: SystemProcessInspector, seconds: float = 0.2
) -> DeadlineProjection:
    now_wall = time.time()
    now_monotonic = time.monotonic()
    hard_wall = now_wall + seconds
    hard_monotonic = now_monotonic + seconds
    return DeadlineProjection(
        cycle_started_wall_epoch=hard_wall - 9_000.0,
        cycle_started_monotonic=hard_monotonic - 9_000.0,
        internal_interrupt_wall_epoch=hard_wall - 5.0,
        internal_interrupt_monotonic=hard_monotonic - 5.0,
        hard_stop_wall_epoch=hard_wall,
        hard_stop_monotonic=hard_monotonic,
        projected_wall_epoch=now_wall,
        projected_monotonic=now_monotonic,
        boot_identity=inspector.boot_identity(),
    )


def wait_for_worker(group: BlockedProcessGroup, timeout: float = 3.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = group.worker_returncode
        if result is not None:
            return result
        time.sleep(0.01)
    raise AssertionError("worker did not terminate")


def cleanup_group(group: BlockedProcessGroup) -> None:
    group.close_without_release()
    try:
        os.killpg(group.leader_pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    group.reap(timeout=1.0)
    group.close()


def test_absolute_deadline_maps_to_same_boot_monotonic_without_reset() -> None:
    wall = MutableClock(1_000.0)
    monotonic = MutableClock(50.0)
    clock = GuardianClock(
        projection(),
        boot_identity="boot-1",
        wall_clock=wall,
        monotonic_clock=monotonic,
    )

    assert clock.hard_stop_monotonic == 8_150.0
    assert not clock.interrupt_due()
    wall.value = 9_095.0
    monotonic.value = 8_145.0
    assert clock.interrupt_due()
    assert not clock.hard_stop_due()
    wall.value = 9_100.0
    monotonic.value = 8_150.0
    assert clock.hard_stop_due()


def test_deadline_rejects_missing_t0_shape_and_boot_change() -> None:
    with pytest.raises(ClockViolation, match="T0 plus 9000"):
        replace(projection(), hard_stop_wall_epoch=9_099.0)
    with pytest.raises(ClockViolation, match="boot identity"):
        GuardianClock(projection(), boot_identity="rebooted")


def test_host_persisted_monotonic_due_cannot_be_rederived_or_extended() -> None:
    with pytest.raises(ClockViolation, match="projections disagree"):
        replace(projection(), projected_monotonic=47.0)
    with pytest.raises(ClockViolation, match="monotonic hard stop"):
        replace(
            projection(),
            hard_stop_monotonic=8_151.0,
            internal_interrupt_monotonic=8_146.0,
        )


def test_boundary_wait_is_capped_by_host_deadline_not_poll_interval() -> None:
    wall = MutableClock(8_994.999)
    monotonic = MutableClock(100.999)
    projected = DeadlineProjection(
        cycle_started_wall_epoch=0.0,
        cycle_started_monotonic=-8_894.0,
        internal_interrupt_wall_epoch=8_995.0,
        internal_interrupt_monotonic=101.0,
        hard_stop_wall_epoch=9_000.0,
        hard_stop_monotonic=106.0,
        projected_wall_epoch=8_994.0,
        projected_monotonic=100.0,
        boot_identity="boot-1",
    )
    clock = GuardianClock(
        projected,
        boot_identity="boot-1",
        wall_clock=wall,
        monotonic_clock=monotonic,
    )

    assert clock.seconds_until_next_boundary(interrupt_sent=False) == pytest.approx(
        0.001
    )


@pytest.mark.parametrize("which", ["wall", "monotonic"])
def test_clock_rollback_fails_closed(which: str) -> None:
    wall = MutableClock(1_000.0)
    monotonic = MutableClock(50.0)
    clock = GuardianClock(
        projection(),
        boot_identity="boot-1",
        wall_clock=wall,
        monotonic_clock=monotonic,
    )
    clock.sample()
    if which == "wall":
        wall.value -= MAX_PROJECTION_SKEW_SECONDS + 0.01
    else:
        monotonic.value -= 1.0
    with pytest.raises(ClockViolation, match="rolled backwards"):
        clock.sample()


def test_subsecond_wall_adjustment_uses_monotonic_without_extending_deadline() -> (
    None
):
    wall = MutableClock(1_000.0)
    monotonic = MutableClock(50.0)
    clock = GuardianClock(
        projection(),
        boot_identity="boot-1",
        wall_clock=wall,
        monotonic_clock=monotonic,
    )
    clock.sample()
    wall.value -= 0.25
    monotonic.value += 0.25

    assert clock.sample() == (999.75, 50.25)
    assert clock.seconds_until_hard_stop() == pytest.approx(
        clock.hard_stop_monotonic - monotonic.value
    )


def test_runtime_wall_lag_behind_monotonic_fails_closed() -> None:
    wall = MutableClock(1_000.0)
    monotonic = MutableClock(50.0)
    clock = GuardianClock(
        projection(),
        boot_identity="boot-1",
        wall_clock=wall,
        monotonic_clock=monotonic,
    )
    wall.value += 0.5
    monotonic.value += 2.0
    with pytest.raises(ClockViolation, match="lagged monotonic"):
        clock.sample()


def test_forward_wall_correction_is_safe_and_shortens_deadline() -> None:
    wall = MutableClock(1_000.0)
    monotonic = MutableClock(50.0)
    clock = GuardianClock(
        projection(),
        boot_identity="boot-1",
        wall_clock=wall,
        monotonic_clock=monotonic,
    )
    wall.value += 56.0
    monotonic.value += 0.5

    assert clock.sample() == (1_056.0, 50.5)
    assert clock.seconds_until_hard_stop() == pytest.approx(8_044.0)


def test_reused_pid_is_rejected_before_any_signal() -> None:
    expected = ProcessIdentity(101, 501, 101, "start-a")
    inspector = FakeInspector([replace(expected, start_marker="start-b")])
    signaler = FakeSignaler()

    with pytest.raises(IdentityViolation, match="no longer matches"):
        stop_then_kill_groups(
            [PaidGroup("root", expected)], inspector=inspector, signaler=signaler
        )

    assert signaler.calls == []


def test_system_start_marker_is_kernel_precision_not_coarse_ps_time() -> None:
    identity = SystemProcessInspector().identity(os.getpid())
    assert identity is not None
    if sys.platform == "darwin":
        assert identity.start_marker.startswith("darwin-start-us:")
        _prefix, seconds, microseconds = identity.start_marker.split(":")
        assert int(seconds) > 0
        assert 0 <= int(microseconds) < 1_000_000
    elif sys.platform.startswith("linux"):
        assert identity.start_marker.startswith("proc-start-ticks:")


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin uses boot session UUID")
def test_darwin_boot_identity_uses_stable_kernel_session_uuid() -> None:
    inspector = object.__new__(SystemProcessInspector)
    raw_uuid = b"5C199D58-9DD0-487A-BF17-4CFE684DF3FA\x00"
    names: list[bytes] = []

    def sysctlbyname(
        name: bytes,
        output: object,
        size_pointer: object,
        _new_value: object,
        _new_size: int,
    ) -> int:
        names.append(name)
        ctypes.memmove(output, raw_uuid, len(raw_uuid))
        ctypes.cast(
            size_pointer, ctypes.POINTER(ctypes.c_size_t)
        ).contents.value = len(raw_uuid)
        return 0

    inspector._darwin_sysctlbyname = sysctlbyname  # type: ignore[attr-defined]
    expected = hashlib.sha256(
        b"darwin-bootsessionuuid:5c199d58-9dd0-487a-bf17-4cfe684df3fa"
    ).hexdigest()

    assert inspector.boot_identity() == expected
    assert inspector.boot_identity() == expected
    assert names == [b"kern.bootsessionuuid", b"kern.bootsessionuuid"]


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin uses native libproc")
def test_darwin_deadline_inspection_never_spawns_or_waits_for_ps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = SystemProcessInspector()

    def forbidden_subprocess(*args: object, **kwargs: object) -> object:
        raise AssertionError("deadline process inspection must be native")

    monkeypatch.setattr(subprocess, "run", forbidden_subprocess)
    assert inspector.boot_identity()
    identity = inspector.identity(os.getpid())
    assert identity is not None
    assert inspector.group_members(os.getpgrp())
    assert isinstance(inspector.descendants(os.getpid()), tuple)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux uses /proc")
def test_linux_deadline_inspection_never_spawns_or_waits_for_ps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = SystemProcessInspector()

    def forbidden_subprocess(*args: object, **kwargs: object) -> object:
        raise AssertionError("deadline process inspection must be native")

    monkeypatch.setattr(subprocess, "run", forbidden_subprocess)
    assert inspector.boot_identity()
    identity = inspector.identity(os.getpid())
    assert identity is not None
    assert inspector.group_members(os.getpgrp())
    assert isinstance(inspector.descendants(os.getpid()), tuple)


def test_darwin_empty_native_group_treats_eperm_probe_as_pgid_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(guardian_module.sys, "platform", "darwin")

    def denied(_pgid: int, _signal: int) -> None:
        raise PermissionError(1, "operation not permitted")

    monkeypatch.setattr(guardian_module.os, "killpg", denied)
    SystemProcessInspector._confirm_group_absent(4242)


def test_non_darwin_empty_group_keeps_eperm_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(guardian_module.sys, "platform", "linux")

    def denied(_pgid: int, _signal: int) -> None:
        raise PermissionError(1, "operation not permitted")

    monkeypatch.setattr(guardian_module.os, "killpg", denied)
    with pytest.raises(IdentityViolation, match="outside the guardian identity domain"):
        SystemProcessInspector._confirm_group_absent(4242)


def test_darwin_full_pid_buffer_retries_instead_of_accepting_truncation() -> None:
    inspector = object.__new__(SystemProcessInspector)
    calls: list[int] = []

    def enumerate_pids(pointer: object, byte_size: int) -> int:
        capacity = byte_size // ctypes.sizeof(ctypes.c_int)
        calls.append(capacity)
        view = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int))
        if len(calls) == 1:
            for index in range(capacity):
                view[index] = index + 2
            return capacity
        view[0] = 41
        view[1] = 42
        return 2

    assert inspector._darwin_pid_list(  # noqa: SLF001
        enumerate_pids,
        estimated=1,
        operation="test list",
    ) == (41, 42)
    assert len(calls) == 2
    assert calls[1] == calls[0] * 2


def test_stop_all_then_kill_all_includes_root_reviewer_and_verifier() -> None:
    identities = [
        ProcessIdentity(101, 501, 101, "root-start"),
        ProcessIdentity(202, 501, 202, "review-start"),
        ProcessIdentity(303, 501, 303, "verify-start"),
    ]
    groups = [
        PaidGroup("root", identities[0]),
        PaidGroup("reviewer", identities[1]),
        PaidGroup("verifier", identities[2]),
    ]
    signaler = FakeSignaler()

    receipt = stop_then_kill_groups(
        groups, inspector=FakeInspector(identities), signaler=signaler
    )

    assert signaler.calls == [
        (101, signal.SIGSTOP),
        (202, signal.SIGSTOP),
        (303, signal.SIGSTOP),
        (101, signal.SIGKILL),
        (202, signal.SIGKILL),
        (303, signal.SIGKILL),
    ]
    assert receipt.stopped_pgids == (101, 202, 303)
    assert receipt.killed_pgids == (101, 202, 303)
    assert receipt.already_empty_pgids == ()


def test_naturally_exited_aux_does_not_block_exact_root_stop_and_kill() -> None:
    root = ProcessIdentity(101, 501, 101, "root-start")
    exited_aux = ProcessIdentity(202, 501, 202, "review-start")
    signaler = FakeSignaler()

    receipt = stop_then_kill_groups(
        [PaidGroup("reviewer", exited_aux), PaidGroup("root", root)],
        inspector=FakeInspector([root]),
        signaler=signaler,
    )

    assert signaler.calls == [
        (101, signal.SIGSTOP),
        (101, signal.SIGKILL),
    ]
    assert receipt.stopped_pgids == (101,)
    assert receipt.killed_pgids == (101,)
    assert receipt.already_empty_pgids == (202,)


def test_aux_disappearing_after_root_stop_cannot_leave_root_frozen() -> None:
    root = ProcessIdentity(101, 501, 101, "root-start")
    auxiliary = ProcessIdentity(202, 501, 202, "review-start")
    inspector = FakeInspector([root, auxiliary])

    class VanishingAuxSignaler(FakeSignaler):
        def killpg(self, pgid: int, sig: int) -> None:
            super().killpg(pgid, sig)
            if (pgid, sig) == (101, signal.SIGSTOP):
                del inspector.identities[auxiliary.pid]

    signaler = VanishingAuxSignaler()
    receipt = stop_then_kill_groups(
        [PaidGroup("reviewer", auxiliary), PaidGroup("root", root)],
        inspector=inspector,
        signaler=signaler,
    )

    assert signaler.calls == [
        (101, signal.SIGSTOP),
        (101, signal.SIGKILL),
    ]
    assert receipt.already_empty_pgids == (202,)


def test_ambiguous_aux_is_not_signalled_but_exact_root_is_killed() -> None:
    root = ProcessIdentity(101, 501, 101, "root-start")
    expected_aux = ProcessIdentity(202, 501, 202, "review-start")
    reused_aux = replace(expected_aux, start_marker="reused")
    signaler = FakeSignaler()

    with pytest.raises(GroupStopFailure) as captured:
        stop_then_kill_groups(
            [PaidGroup("reviewer", expected_aux), PaidGroup("root", root)],
            inspector=FakeInspector([root, reused_aux]),
            signaler=signaler,
        )

    assert signaler.calls == [
        (101, signal.SIGSTOP),
        (101, signal.SIGKILL),
    ]
    assert captured.value.receipt.killed_pgids == (101,)


def test_post_stop_inspector_failure_cannot_prevent_root_sigkill() -> None:
    root = ProcessIdentity(101, 501, 101, "root-start")

    class FlappingInspector(FakeInspector):
        identity_calls = 0

        def identity(self, pid: int) -> ProcessIdentity | None:
            if pid == root.pid:
                self.identity_calls += 1
                if self.identity_calls > 1:
                    return replace(root, start_marker="transient-bad-read")
            return super().identity(pid)

    signaler = FakeSignaler()
    receipt = stop_then_kill_groups(
        [PaidGroup("root", root)],
        inspector=FlappingInspector([root]),
        signaler=signaler,
    )

    assert signaler.calls == [
        (101, signal.SIGSTOP),
        (101, signal.SIGKILL),
    ]
    assert receipt.killed_pgids == (101,)


def test_post_kill_fork_residual_is_rekilled_to_an_empty_fixed_point() -> None:
    root = ProcessIdentity(101, 501, 101, "root-start")
    residual = ProcessIdentity(102, 501, 101, "fork-after-kill")
    inspector = FakeInspector([root])

    class ForkAfterKillSignaler(FakeSignaler):
        kill_count = 0

        def killpg(self, pgid: int, sig: int) -> None:
            super().killpg(pgid, sig)
            if sig == signal.SIGSTOP:
                raise InterruptedError("SIGSTOP reply lost")
            self.kill_count += 1
            if self.kill_count == 1:
                inspector.identities.pop(root.pid)
                inspector.identities[residual.pid] = residual
            else:
                inspector.identities.pop(residual.pid)

    signaler = ForkAfterKillSignaler()
    with pytest.raises(GroupStopFailure) as captured:
        stop_then_kill_groups(
            [PaidGroup("root", root)],
            inspector=inspector,
            signaler=signaler,
            reap=lambda _identity: None,
        )

    assert signaler.calls == [
        (root.pgid, signal.SIGSTOP),
        (root.pgid, signal.SIGKILL),
        (root.pgid, signal.SIGKILL),
    ]
    assert captured.value.receipt.killed_pgids == (root.pgid,)
    assert inspector.group_members(root.pgid) == ()


def test_post_kill_exact_leader_does_not_need_membership_scan() -> None:
    root = ProcessIdentity(101, 501, 101, "root-start")

    class MembershipUnavailableWhileLeaderLives(FakeInspector):
        def group_members(self, pgid: int) -> tuple[ProcessIdentity, ...]:
            if root.pid in self.identities:
                raise IdentityViolation("enumeration failed")
            return super().group_members(pgid)

    inspector = MembershipUnavailableWhileLeaderLives([root])

    class RemoveLeaderOnRepeatedKill(FakeSignaler):
        kill_count = 0

        def killpg(self, pgid: int, sig: int) -> None:
            super().killpg(pgid, sig)
            if sig == signal.SIGKILL:
                self.kill_count += 1
                if self.kill_count == 2:
                    inspector.identities.pop(root.pid)

    signaler = RemoveLeaderOnRepeatedKill()
    receipt = stop_then_kill_groups(
        [PaidGroup("root", root)],
        inspector=inspector,
        signaler=signaler,
        reap=lambda _identity: None,
    )

    assert signaler.calls == [
        (root.pgid, signal.SIGSTOP),
        (root.pgid, signal.SIGKILL),
        (root.pgid, signal.SIGKILL),
    ]
    assert receipt.killed_pgids == (root.pgid,)
    assert inspector.group_members(root.pgid) == ()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_bound_leaderless_same_uid_residual_group_is_stopped_killed_and_empty(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "leaderless.ready"
    exit_leader = tmp_path / "leader.exit"
    source = f"""
import os
import time
from pathlib import Path
ready = Path({str(ready)!r})
exit_leader = Path({str(exit_leader)!r})
child = os.fork()
if child == 0:
    time.sleep(30)
    os._exit(0)
ready.write_text(str(child))
while not exit_leader.exists():
    time.sleep(0.005)
os._exit(0)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", source],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    inspector = SystemProcessInspector()
    signaler = RecordingOSSignaler()
    try:
        deadline = time.monotonic() + 2.0
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        identity = inspector.identity(process.pid)
        assert identity is not None and identity.pid == identity.pgid
        residual_pid = int(ready.read_text(encoding="ascii"))
        exit_leader.touch()
        assert process.wait(timeout=1.0) == 0
        assert inspector.identity(identity.pid) is None
        members = inspector.group_members(identity.pgid)
        assert any(item.pid == residual_pid for item in members)

        receipt = stop_then_kill_groups(
            (PaidGroup("reviewer", identity),),
            inspector=inspector,
            signaler=signaler,
            reap=lambda _identity: None,
        )

        assert (identity.pgid, signal.SIGSTOP) in signaler.calls
        assert (identity.pgid, signal.SIGKILL) in signaler.calls
        assert receipt.killed_pgids == (identity.pgid,)
        wait_for_groups_empty(
            (PaidGroup("reviewer", identity),), inspector=inspector
        )
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1.0)


def test_leaderless_group_with_foreign_uid_member_is_never_signalled() -> None:
    expected = ProcessIdentity(202, 501, 202, "bound-leader")
    foreign = ProcessIdentity(203, 777, 202, "foreign-member")
    signaler = FakeSignaler()

    with pytest.raises(GroupStopFailure, match="no longer matches"):
        stop_then_kill_groups(
            (PaidGroup("reviewer", expected),),
            inspector=FakeInspector([foreign]),
            signaler=signaler,
        )

    assert signaler.calls == []


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_response_unknown_after_real_sigstop_still_kills_and_reaps_group() -> None:
    inspector = SystemProcessInspector()
    group = BlockedProcessGroup.spawn(
        [sys.executable, "-c", "import time; time.sleep(30)"], inspector=inspector
    )
    group.release()
    identity = inspector.identity(group.leader_pid)
    assert identity is not None

    class StopThenLoseReply(RecordingOSSignaler):
        def killpg(self, pgid: int, sig: int) -> None:
            super().killpg(pgid, sig)
            if sig == signal.SIGSTOP:
                raise InterruptedError("SIGSTOP reply lost")

    signaler = StopThenLoseReply()
    try:
        with pytest.raises(GroupStopFailure) as captured:
            stop_then_kill_groups(
                [PaidGroup("root", identity)],
                inspector=inspector,
                signaler=signaler,
                reap=lambda _identity: group.reap(),
            )
        assert signaler.calls[0] == (identity.pgid, signal.SIGSTOP)
        assert len(signaler.calls) >= 2
        assert all(
            call == (identity.pgid, signal.SIGKILL)
            for call in signaler.calls[1:]
        )
        assert captured.value.receipt.stopped_pgids == ()
        assert captured.value.receipt.killed_pgids == (identity.pgid,)
        wait_for_groups_empty([PaidGroup("root", identity)], inspector=inspector)
    finally:
        cleanup_group(group)


def test_exact_root_does_not_need_fallible_membership_scan_before_stop() -> None:
    root = ProcessIdentity(101, 501, 101, "root-start")

    class MembershipUnavailable(FakeInspector):
        def group_members(self, pgid: int) -> tuple[ProcessIdentity, ...]:
            raise IdentityViolation("enumeration failed")

    signaler = FakeSignaler()
    receipt = stop_then_kill_groups(
        [PaidGroup("root", root)],
        inspector=MembershipUnavailable([root]),
        signaler=signaler,
    )
    assert signaler.calls == [
        (root.pgid, signal.SIGSTOP),
        (root.pgid, signal.SIGKILL),
    ]
    assert receipt.killed_pgids == (root.pgid,)


def test_one_aux_inspection_failure_does_not_block_other_exact_groups() -> None:
    root = ProcessIdentity(101, 501, 101, "root-start")
    unavailable_aux = ProcessIdentity(202, 501, 202, "missing-reviewer")
    verifier = ProcessIdentity(303, 501, 303, "verifier-start")

    class PartiallyUnavailable(FakeInspector):
        def identity(self, pid: int) -> ProcessIdentity | None:
            if pid == unavailable_aux.pid:
                return None
            return super().identity(pid)

        def group_members(self, pgid: int) -> tuple[ProcessIdentity, ...]:
            if pgid == unavailable_aux.pgid:
                raise IdentityViolation("enumeration failed")
            return super().group_members(pgid)

    signaler = FakeSignaler()
    with pytest.raises(GroupStopFailure) as captured:
        stop_then_kill_groups(
            [
                PaidGroup("reviewer", unavailable_aux),
                PaidGroup("root", root),
                PaidGroup("verifier", verifier),
            ],
            inspector=PartiallyUnavailable([root, verifier]),
            signaler=signaler,
        )
    assert signaler.calls == [
        (root.pgid, signal.SIGSTOP),
        (verifier.pgid, signal.SIGSTOP),
        (root.pgid, signal.SIGKILL),
        (verifier.pgid, signal.SIGKILL),
    ]
    assert captured.value.receipt.killed_pgids == (root.pgid, verifier.pgid)


def test_root_completion_rejects_absent_aux_leader_with_residual_group_member() -> None:
    root = ProcessIdentity(101, 501, 101, "root-start")
    auxiliary = ProcessIdentity(202, 501, 202, "review-start")
    residual = ProcessIdentity(203, 501, 202, "review-child")
    inspector = FakeInspector([root, residual])

    with pytest.raises(ResidualDescendants, match="were not empty"):
        guardian_module._require_auxiliary_groups_empty(  # noqa: SLF001
            [PaidGroup("root", root), PaidGroup("reviewer", auxiliary)],
            root_pgid=root.pgid,
            inspector=inspector,
        )


def test_residual_identity_diagnostic_is_sorted_bounded_and_content_bound() -> None:
    identities = tuple(
        ProcessIdentity(
            300 - index,
            501,
            200 + (index % 2),
            f"marker-{index}-" + ("x" * 256),
        )
        for index in range(12)
    )

    forward = guardian_module._residual_identity_diagnostic(identities)  # noqa: SLF001
    reverse = guardian_module._residual_identity_diagnostic(  # noqa: SLF001
        tuple(reversed(identities))
    )

    assert forward == reverse
    assert forward.startswith("count=12,sha256=")
    assert forward.count('"pid":') == 8
    assert "marker-11-" in forward
    assert "marker-3-" not in forward
    assert len(forward) < 2_500


def test_discovery_happens_after_root_stop_and_before_any_kill() -> None:
    root = ProcessIdentity(101, 501, 101, "root-start")
    escaped = ProcessIdentity(202, 501, 202, "escaped-start")
    signaler = FakeSignaler()
    discovery_calls = 0

    def discover() -> tuple[PaidGroup, ...]:
        nonlocal discovery_calls
        discovery_calls += 1
        if discovery_calls == 1:
            assert signaler.calls == [(root.pgid, signal.SIGSTOP)]
            return (PaidGroup("reviewer", escaped),)
        assert signaler.calls == [
            (root.pgid, signal.SIGSTOP),
            (escaped.pgid, signal.SIGSTOP),
        ]
        return ()

    receipt = stop_then_kill_groups(
        [PaidGroup("root", root)],
        inspector=FakeInspector([root, escaped]),
        signaler=signaler,
        discover_after_stop=discover,
    )
    assert signaler.calls == [
        (root.pgid, signal.SIGSTOP),
        (escaped.pgid, signal.SIGSTOP),
        (root.pgid, signal.SIGKILL),
        (escaped.pgid, signal.SIGKILL),
    ]
    assert receipt.killed_pgids == (root.pgid, escaped.pgid)


def test_capture_continues_after_ambiguous_descendant_and_keeps_later_exact_group() -> (
    None
):
    uid = os.getuid()
    root = ProcessIdentity(101, uid, 101, "root")
    ambiguous = ProcessIdentity(250, uid, 260, "ambiguous-member")
    exact = ProcessIdentity(303, uid, 303, "exact-escape")

    class EscapeInspector(FakeInspector):
        def descendants(self, pid: int) -> tuple[ProcessIdentity, ...]:
            assert pid == root.pid
            return (ambiguous, exact)

    inspector = EscapeInspector([root, exact])
    candidates: dict[int, PaidGroup] = {}
    guardian = Guardian(object(), inspector=inspector)  # type: ignore[arg-type]
    with pytest.raises(IdentityViolation, match="ambiguous"):
        guardian._capture_escaped_groups(
            root_group=PaidGroup("root", root),
            registered_groups={root.pgid: PaidGroup("root", root)},
            candidate_groups=candidates,
        )

    assert candidates == {exact.pgid: PaidGroup("root", exact)}


def test_descendant_capture_stages_just_exited_empty_group_leader() -> None:
    uid = os.getuid()
    root = ProcessIdentity(101, uid, 101, "root")
    exited = ProcessIdentity(202, uid, 202, "short-lived-leader")

    class JustExitedLeaderInspector(FakeInspector):
        def descendants(self, pid: int) -> tuple[ProcessIdentity, ...]:
            assert pid == root.pid
            return (exited,)

    candidates: dict[int, PaidGroup] = {}
    guardian_module.capture_descendant_process_groups(
        (PaidGroup("root", root),),
        registered_groups={root.pgid: PaidGroup("root", root)},
        candidate_groups=candidates,
        inspector=JustExitedLeaderInspector([root]),
        owner_uid=uid,
    )

    assert candidates == {exited.pgid: PaidGroup("root", exited)}


def test_descendant_capture_stages_just_exited_leader_with_same_uid_members() -> (
    None
):
    uid = os.getuid()
    root = ProcessIdentity(101, uid, 101, "root")
    exited = ProcessIdentity(202, uid, 202, "short-lived-leader")
    residual = ProcessIdentity(203, uid, 202, "residual-member")

    class LeaderlessGroupInspector(FakeInspector):
        def descendants(self, pid: int) -> tuple[ProcessIdentity, ...]:
            assert pid == root.pid
            # The native descendant snapshot saw both processes before the
            # leader exited.  Subsequent exact identity reads see only the
            # same-UID residual member of that still-live process group.
            return (exited, residual)

    candidates: dict[int, PaidGroup] = {}
    guardian_module.capture_descendant_process_groups(
        (PaidGroup("root", root),),
        registered_groups={root.pgid: PaidGroup("root", root)},
        candidate_groups=candidates,
        inspector=LeaderlessGroupInspector([root, residual]),
        owner_uid=uid,
    )

    assert candidates == {exited.pgid: PaidGroup("root", exited)}


def test_descendant_capture_stages_transient_group_without_per_group_sleep() -> None:
    uid = os.getuid()
    root = ProcessIdentity(101, uid, 101, "root")
    exited = ProcessIdentity(202, uid, 202, "short-lived-leader")

    class TransientGroupInspector(FakeInspector):
        group_reads = 0

        def descendants(self, pid: int) -> tuple[ProcessIdentity, ...]:
            assert pid == root.pid
            return (exited,)

        def group_members(self, pgid: int) -> tuple[ProcessIdentity, ...]:
            if pgid == exited.pgid:
                self.group_reads += 1
                if self.group_reads == 1:
                    raise IdentityViolation("transient Darwin group visibility")
            return super().group_members(pgid)

    inspector = TransientGroupInspector([root])
    candidates: dict[int, PaidGroup] = {}
    guardian_module.capture_descendant_process_groups(
        (PaidGroup("root", root),),
        registered_groups={root.pgid: PaidGroup("root", root)},
        candidate_groups=candidates,
        inspector=inspector,
        owner_uid=uid,
    )

    assert inspector.group_reads == 1
    assert candidates == {exited.pgid: PaidGroup("root", exited)}


def test_descendant_capture_rechecks_deadline_across_many_groups() -> None:
    uid = os.getuid()
    root = ProcessIdentity(101, uid, 101, "root")
    descendants = tuple(
        ProcessIdentity(1_000 + index, uid, 1_000 + index, f"child-{index}")
        for index in range(256)
    )

    class ManyGroupsInspector(FakeInspector):
        def descendants(self, pid: int) -> tuple[ProcessIdentity, ...]:
            return descendants if pid == root.pid else ()

    monotonic = MutableClock(0.0)
    checks = 0

    def deadline_check() -> None:
        nonlocal checks
        checks += 1
        monotonic.value += 1.0
        if monotonic.value >= 5.0:
            raise guardian_module._HardStopDue("test hard stop")  # noqa: SLF001

    with pytest.raises(guardian_module._HardStopDue, match="test hard stop"):  # noqa: SLF001
        guardian_module.capture_descendant_process_groups(
            (PaidGroup("root", root),),
            registered_groups={root.pgid: PaidGroup("root", root)},
            candidate_groups={},
            inspector=ManyGroupsInspector([root, *descendants]),
            owner_uid=uid,
            deadline_check=deadline_check,
        )

    assert checks == 5


def test_descendant_capture_preserves_exact_subset_but_rejects_root_pid_swap() -> None:
    uid = os.getuid()
    root = ProcessIdentity(101, uid, 101, "root")
    reused_root = replace(root, start_marker="reused-root")
    escaped = ProcessIdentity(202, uid, 202, "escaped")

    class RootSwapInspector(FakeInspector):
        root_reads = 0

        def identity(self, pid: int) -> ProcessIdentity | None:
            if pid == root.pid:
                self.root_reads += 1
                return root if self.root_reads <= 2 else reused_root
            return super().identity(pid)

        def descendants(self, pid: int) -> tuple[ProcessIdentity, ...]:
            assert pid == root.pid
            return (escaped,)

    candidates: dict[int, PaidGroup] = {}
    with pytest.raises(IdentityViolation, match=str(root.pgid)):
        guardian_module.capture_descendant_process_groups(
            (PaidGroup("root", root),),
            registered_groups={root.pgid: PaidGroup("root", root)},
            candidate_groups=candidates,
            inspector=RootSwapInspector([root, escaped]),
            owner_uid=uid,
        )

    assert candidates == {escaped.pgid: PaidGroup("root", escaped)}


def test_conflicting_duplicate_pgid_fails_before_signal() -> None:
    first = ProcessIdentity(101, 501, 101, "start-a")
    conflicting = ProcessIdentity(101, 501, 101, "start-b")
    signaler = FakeSignaler()
    with pytest.raises(IdentityViolation, match="conflicting"):
        stop_then_kill_groups(
            [PaidGroup("root", first), PaidGroup("verifier", conflicting)],
            inspector=FakeInspector([first]),
            signaler=signaler,
        )
    assert signaler.calls == []


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_gate_eof_before_release_executes_zero_paid_command(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    group = BlockedProcessGroup.spawn(
        [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ]
    )
    try:
        group.close_without_release()
        assert group.reap() == 124
        assert not marker.exists()
    finally:
        group.close()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_stable_session_leader_never_execs_paid_command(tmp_path: Path) -> None:
    worker_pid = tmp_path / "worker.pid"
    inspector = SystemProcessInspector()
    group = BlockedProcessGroup.spawn(
        [
            sys.executable,
            "-c",
            (
                "import os; from pathlib import Path; "
                f"Path({str(worker_pid)!r}).write_text(str(os.getpid()))"
            ),
        ],
        inspector=inspector,
    )
    try:
        leader_before = inspector.identity(group.leader_pid)
        assert leader_before is not None
        assert os.getsid(group.leader_pid) == group.leader_pid
        group.release()
        assert wait_for_worker(group) == 0
        assert int(worker_pid.read_text()) != group.leader_pid
        assert inspector.identity(group.leader_pid) == leader_before
        assert group.retire_after_empty(inspector) == 0
        assert group.reap() == 0
    finally:
        group.close()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_paid_worker_environment_is_exact_and_never_inherits_guardian_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "worker-env.json"
    secret_names = (
        "RETHLAS_REVIEW_CONTROL_TOKEN",
        "RETHLAS_GUARDIAN_CONTROL_TOKEN",
        "CODEX_HOME",
    )
    for index, name in enumerate(secret_names):
        monkeypatch.setenv(name, f"guardian-secret-{index}")
    group = BlockedProcessGroup.spawn(
        [
            sys.executable,
            "-c",
            (
                "import json,os; from pathlib import Path; "
                f"Path({str(output)!r}).write_text(json.dumps(dict(os.environ), "
                "sort_keys=True))"
            ),
        ],
        env={"RETHLAS_INNER_ALLOWED": "yes"},
    )
    inspector = SystemProcessInspector()
    try:
        group.release()
        assert wait_for_worker(group) == 0
        observed = json.loads(output.read_text(encoding="utf-8"))
        assert observed["RETHLAS_INNER_ALLOWED"] == "yes"
        assert all(name not in observed for name in secret_names)
        assert group.retire_after_empty(inspector) == 0
        assert group.reap() == 0
    finally:
        group.close()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_reexeced_shim_itself_has_no_guardian_secret_or_unapproved_high_fd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_values = (
        "owner-master-secret-sentinel",
        "guardian-cycle-secret-sentinel",
        "runner-cycle-secret-sentinel",
    )
    for name, value in zip(
        (
            "RETHLAS_REVIEW_CONTROL_TOKEN",
            "RETHLAS_GUARDIAN_CYCLE_TOKEN",
            "RETHLAS_RUNNER_CYCLE_TOKEN",
        ),
        secret_values,
        strict=True,
    ):
        monkeypatch.setenv(name, value)
    unrelated_read, unrelated_write = os.pipe()
    high_fd = 200
    os.dup2(unrelated_read, high_fd, inheritable=True)
    os.close(unrelated_read)
    group = BlockedProcessGroup.spawn([sys.executable, "-c", "pass"])
    try:
        process_view = subprocess.run(
            ["ps", "eww", "-p", str(group.leader_pid), "-o", "command="],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert all(value not in process_view for value in secret_values)
        proc_fd_root = Path("/proc") / str(group.leader_pid) / "fd"
        if proc_fd_root.is_dir():
            assert not os.path.lexists(proc_fd_root / str(high_fd))
        else:
            lsof = shutil.which("lsof")
            if lsof is None:
                pytest.skip("requires /proc process FDs or lsof")
            descriptor_view = subprocess.run(
                [lsof, "-a", "-p", str(group.leader_pid), "-d", str(high_fd), "-Fn"],
                check=False,
                capture_output=True,
                text=True,
            )
            assert f"f{high_fd}\n" not in descriptor_view.stdout
        group.close_without_release()
        assert group.reap() == 124
    finally:
        os.close(high_fd)
        os.close(unrelated_write)
        group.close()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_explicit_one_shot_fd_survives_sanitized_shim_and_reaches_worker(
    tmp_path: Path,
) -> None:
    token_read, token_write = os.pipe()
    token_fd = 201
    os.dup2(token_read, token_fd, inheritable=False)
    os.close(token_read)
    os.write(token_write, b"runner-cycle-token")
    os.close(token_write)
    output = tmp_path / "token.txt"
    group = BlockedProcessGroup.spawn(
        [
            sys.executable,
            "-c",
            (
                "import os; from pathlib import Path; "
                f"Path({str(output)!r}).write_bytes(os.read({token_fd}, 4096)); "
                f"os.close({token_fd})"
            ),
        ],
        env={
            "RETHLAS_RUNNER_CYCLE_TOKEN_FD": str(token_fd),
            "RETHLAS_RUNNER_CYCLE_TOKEN_SHA256": "a" * 64,
        },
        pass_fds=(token_fd,),
    )
    with pytest.raises(OSError):
        os.fstat(token_fd)
    inspector = SystemProcessInspector()
    try:
        group.release()
        assert wait_for_worker(group) == 0
        assert output.read_bytes() == b"runner-cycle-token"
        assert group.retire_after_empty(inspector) == 0
        assert group.reap() == 0
    finally:
        group.close()


@pytest.mark.parametrize(
    ("command", "environment"),
    [
        (("python", "-c", "pass"), {}),
        ((sys.executable, "bad\0argument"), {}),
        ((sys.executable, "-c", "pass"), {"BAD=KEY": "value"}),
        ((sys.executable, "-c", "pass"), {"GOOD_KEY": "bad\0value"}),
    ],
)
def test_paid_worker_rejects_unpinned_command_or_unsafe_environment(
    command: tuple[str, ...], environment: dict[str, str]
) -> None:
    with pytest.raises(ValueError):
        BlockedProcessGroup.spawn(command, env=environment)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_rc_zero_with_residual_descendant_fails_closed(tmp_path: Path) -> None:
    inspector = SystemProcessInspector()
    group = BlockedProcessGroup.spawn(
        [
            sys.executable,
            "-c",
            (
                "import subprocess,sys; "
                "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])"
            ),
        ],
        inspector=inspector,
    )
    try:
        group.release()
        assert wait_for_worker(group) == 0
        with pytest.raises(ResidualDescendants, match="residual"):
            group.retire_after_empty(inspector)
    finally:
        cleanup_group(group)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_stable_leader_early_exit_stops_kills_same_uid_bound_residual_group() -> None:
    inspector = SystemProcessInspector()
    now_wall = time.time()

    class KillLeaderCallbacks(RecordingCallbacks):
        def poll(self, registration_id: str) -> PollSnapshot:
            assert self.request is not None
            identity = self.request.root_group.identity
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                members = inspector.group_members(identity.pgid)
                if any(item.pid != identity.pid for item in members):
                    break
                time.sleep(0.005)
            else:
                raise AssertionError("paid worker did not enter the root group")
            os.kill(identity.pid, signal.SIGKILL)
            return super().poll(registration_id)

    callbacks = KillLeaderCallbacks(
        projection(
            start=now_wall,
            projected_wall=now_wall,
            projected_monotonic=time.monotonic(),
            boot=inspector.boot_identity(),
        )
    )
    signaler = RecordingOSSignaler()
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            signaler=signaler,
            poll_interval=0,
        ).run(
            [sys.executable, "-c", "import time; time.sleep(0.2)"],
            **guardian_kwargs(read_fd),
        )
        assert report.state == "execution_unknown"
        assert callbacks.request is not None
        root_group = callbacks.request.root_group
        assert (root_group.identity.pgid, signal.SIGSTOP) in signaler.calls
        assert (root_group.identity.pgid, signal.SIGKILL) in signaler.calls
        assert root_group.identity.pgid in report.killed_pgids
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                if (
                    inspector.identity(root_group.identity.pgid) is None
                    and not inspector.group_members(root_group.identity.pgid)
                ):
                    break
            except IdentityViolation:
                pass
            time.sleep(0.005)
        else:
            raise AssertionError("same-UID bound residual group remained live")
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_ordinary_setsid_escape_is_detected_stopped_and_killed(tmp_path: Path) -> None:
    inspector = SystemProcessInspector()
    now_wall = time.time()
    callbacks = RecordingCallbacks(
        projection(
            start=now_wall,
            projected_wall=now_wall,
            projected_monotonic=time.monotonic(),
            boot=inspector.boot_identity(),
        )
    )
    escaped_pid_path = tmp_path / "escaped.pid"
    command = (
        "import os,time; from pathlib import Path; child=os.fork(); "
        "(os.setsid(), Path(%r).write_text(str(os.getpid())), time.sleep(30)) "
        "if child == 0 else time.sleep(30)"
    ) % str(escaped_pid_path)
    signaler = RecordingOSSignaler()
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            signaler=signaler,
            poll_interval=0.01,
        ).run([sys.executable, "-c", command], **guardian_kwargs(read_fd))
        assert report.state == "execution_unknown"
        assert "setsid descendant was not attested" in report.reason
        deadline = time.monotonic() + 1.0
        while not escaped_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        escaped_pid = int(escaped_pid_path.read_text())
        assert (escaped_pid, signal.SIGSTOP) in signaler.calls
        assert (escaped_pid, signal.SIGKILL) in signaler.calls
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_hung_host_poll_cannot_hide_a_new_setsid_escape_past_t150(
    tmp_path: Path,
) -> None:
    inspector = SystemProcessInspector()
    escaped_pid_path = tmp_path / "hung-poll-escape.pid"
    release_poll = threading.Event()

    class HungPollCallbacks(RecordingCallbacks):
        def poll(self, registration_id: str) -> PollSnapshot:
            release_poll.wait(10)
            return super().poll(registration_id)

    callbacks = HungPollCallbacks(imminent_projection(inspector, seconds=0.4))
    command = (
        "import os,time; from pathlib import Path; time.sleep(0.05); child=os.fork(); "
        "(os.setsid(), Path(%r).write_text(str(os.getpid())), time.sleep(30)) "
        "if child == 0 else time.sleep(30)"
    ) % str(escaped_pid_path)
    signaler = RecordingOSSignaler()
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            signaler=signaler,
            host_callback_timeout=5.0,
            poll_interval=0.01,
        ).run([sys.executable, "-c", command], **guardian_kwargs(read_fd))
        deadline = time.monotonic() + 1.0
        while not escaped_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        escaped_pid = int(escaped_pid_path.read_text(encoding="ascii"))
        assert (escaped_pid, signal.SIGSTOP) in signaler.calls
        assert (escaped_pid, signal.SIGKILL) in signaler.calls
        assert report.state in {"execution_unknown", "watchdog_forced"}
        empty_deadline = time.monotonic() + 1.0
        while (
            inspector.identity(escaped_pid) is not None
            and time.monotonic() < empty_deadline
        ):
            time.sleep(0.01)
        assert inspector.identity(escaped_pid) is None
    finally:
        release_poll.set()
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
@pytest.mark.parametrize("clock_event", ["wall_crosses_t150", "wall_rolls_back"])
def test_hung_callback_is_preempted_by_authoritative_clock_change(
    clock_event: str,
) -> None:
    inspector = SystemProcessInspector()
    wall = MutableClock(8_994.0)
    monotonic = MutableClock(100.0)
    projected = DeadlineProjection(
        0.0,
        -8_894.0,
        8_995.0,
        101.0,
        9_000.0,
        106.0,
        8_994.0,
        100.0,
        inspector.boot_identity(),
    )
    release_poll = threading.Event()

    class ClockChangingCallbacks(RecordingCallbacks):
        def poll(self, registration_id: str) -> PollSnapshot:
            if clock_event == "wall_crosses_t150":
                wall.value = projected.hard_stop_wall_epoch
            else:
                wall.value = (
                    projected.projected_wall_epoch
                    - MAX_PROJECTION_SKEW_SECONDS
                    - 0.01
                )
            release_poll.wait(10)
            return super().poll(registration_id)

    callbacks = ClockChangingCallbacks(projected)
    read_fd, write_fd = os.pipe()
    started = time.monotonic()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            wall_clock=wall,
            monotonic_clock=monotonic,
            host_callback_timeout=5.0,
            poll_interval=0.01,
        ).run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **guardian_kwargs(read_fd),
        )
        assert time.monotonic() - started < 1.0
        if clock_event == "wall_crosses_t150":
            assert report.state == "watchdog_forced"
            assert report.reason == "absolute_hard_stop"
        else:
            assert report.state == "execution_unknown"
            assert "rolled backwards" in report.reason
        assert report.killed_pgids
    finally:
        release_poll.set()
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_ambiguous_escape_at_t150_can_never_be_reported_watchdog_complete() -> None:
    delegate = SystemProcessInspector()
    phantom = ProcessIdentity(999_998, os.getuid(), 999_997, "phantom")

    class AmbiguousEscapeInspector:
        def boot_identity(self) -> str:
            return delegate.boot_identity()

        def identity(self, pid: int) -> ProcessIdentity | None:
            if pid in {phantom.pid, phantom.pgid}:
                return None
            return delegate.identity(pid)

        def group_members(self, pgid: int) -> tuple[ProcessIdentity, ...]:
            if pgid == phantom.pgid:
                return ()
            return delegate.group_members(pgid)

        def descendants(self, pid: int) -> tuple[ProcessIdentity, ...]:
            return (*delegate.descendants(pid), phantom)

    inspector = AmbiguousEscapeInspector()
    wall = MutableClock(8_994.0)
    monotonic = MutableClock(100.0)
    projected = DeadlineProjection(
        0.0,
        -8_894.0,
        8_995.0,
        101.0,
        9_000.0,
        106.0,
        8_994.0,
        100.0,
        inspector.boot_identity(),
    )
    release_poll = threading.Event()

    class T150DuringPoll(RecordingCallbacks):
        def poll(self, registration_id: str) -> PollSnapshot:
            wall.value = projected.hard_stop_wall_epoch
            release_poll.wait(10)
            return super().poll(registration_id)

    callbacks = T150DuringPoll(projected)
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            wall_clock=wall,
            monotonic_clock=monotonic,
            host_callback_timeout=5.0,
        ).run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **guardian_kwargs(read_fd),
        )
        assert report.state == "execution_unknown"
        assert "ambiguous process groups" in report.reason
        assert report.killed_pgids
    finally:
        release_poll.set()
        os.close(read_fd)
        os.close(write_fd)


class RecordingCallbacks:
    def __init__(
        self,
        projection_value: DeadlineProjection,
        *,
        wall: MutableClock | None = None,
        monotonic: MutableClock | None = None,
        fail_poll: bool = False,
        equivocate: bool = False,
    ) -> None:
        self.projection = projection_value
        self.wall = wall
        self.monotonic = monotonic
        self.fail_poll = fail_poll
        self.equivocate = equivocate
        self.request: RegistrationRequest | None = None
        self.finalized = []
        self.interrupts: list[tuple[str, str]] = []
        self.lifeline_losses: list[tuple[str, str]] = []
        self.poll_count = 0

    def register(self, request: RegistrationRequest) -> RegistrationAck:
        self.request = request
        return RegistrationAck(
            "reg-1", request.request_sha256, True, True, self.projection
        )

    def poll(self, registration_id: str) -> PollSnapshot:
        if self.fail_poll:
            raise RuntimeError("host unavailable")
        assert self.request is not None
        self.poll_count += 1
        if self.wall is not None:
            if self.poll_count == 1:
                self.wall.value = self.projection.hard_stop_wall_epoch - 5
                if self.monotonic is not None:
                    self.monotonic.value = self.projection.hard_stop_monotonic - 5
            elif self.poll_count >= 2:
                self.wall.value = self.projection.hard_stop_wall_epoch
                if self.monotonic is not None:
                    self.monotonic.value = self.projection.hard_stop_monotonic
        identity = self.request.root_group.identity
        snapshot = PollSnapshot(
            0 if self.equivocate else self.poll_count,
            registration_id,
            self.request.request_sha256,
            self.request.boot_identity,
            (PaidGroup("root", identity),),
        )
        if self.equivocate and self.poll_count > 1:
            return replace(snapshot, paid_groups=())
        return snapshot

    def internal_interrupt(self, registration_id: str, request_sha256: str) -> None:
        self.interrupts.append((registration_id, request_sha256))

    def lifeline_lost(self, registration_id: str, request_sha256: str) -> None:
        self.lifeline_losses.append((registration_id, request_sha256))

    def finalize(self, report) -> None:  # noqa: ANN001
        self.finalized.append(report)


def guardian_kwargs(lifeline_fd: int) -> dict[str, object]:
    return {
        "run_id": "run-1",
        "generation_control_instance_id": "instance-1",
        "watchdog_id": "watchdog-1",
        "policy_digest": "a" * 64,
        "lifeline_fd": lifeline_fd,
    }


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_monitor_only_guardian_never_time_interrupts_long_running_root() -> None:
    inspector = SystemProcessInspector()
    callbacks = RecordingCallbacks(imminent_projection(inspector, seconds=0.08))
    read_fd, write_fd = os.pipe()
    started = time.monotonic()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            poll_interval=0.01,
        ).run(
            [sys.executable, "-c", "import time; time.sleep(0.20)"],
            monitor_only=True,
            **guardian_kwargs(read_fd),
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert time.monotonic() - started >= 0.18
    assert report.state == "completed"
    assert report.reason == "paid_group_empty"
    assert callbacks.interrupts == []


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_monitor_only_lifeline_loss_persists_after_legacy_t150_elapsed() -> None:
    inspector = SystemProcessInspector()
    callbacks = RecordingCallbacks(imminent_projection(inspector, seconds=0.05))
    read_fd, write_fd = os.pipe()
    dropper_pid = os.fork()
    if dropper_pid == 0:  # pragma: no cover - child timing helper
        os.close(read_fd)
        time.sleep(0.15)
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            poll_interval=0.01,
            host_callback_timeout=0.5,
        ).run(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            monitor_only=True,
            **guardian_kwargs(read_fd),
        )
    finally:
        os.close(read_fd)
        os.waitpid(dropper_pid, 0)
    assert callbacks.lifeline_losses
    assert callbacks.interrupts == []
    assert report.state == "execution_unknown"
    assert "lifeline" in report.reason


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_registration_callback_failure_keeps_exec_gate_closed(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"

    class RejectingCallbacks:
        request: RegistrationRequest | None = None

        def register(self, request: RegistrationRequest) -> RegistrationAck:
            self.request = request
            raise RuntimeError("durable store unavailable")

        def poll(self, registration_id: str) -> PollSnapshot:
            raise AssertionError("must not poll")

        def internal_interrupt(self, registration_id: str, request_sha256: str) -> None:
            raise AssertionError("must not interrupt")

        def lifeline_lost(self, registration_id: str, request_sha256: str) -> None:
            raise AssertionError("must not report an unreleased lifeline")

        def finalize(self, report) -> None:  # noqa: ANN001
            raise AssertionError("unregistered attempt has no host marker")

    read_fd, write_fd = os.pipe()
    callbacks = RejectingCallbacks()
    try:
        report = Guardian(callbacks).run(
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()",
            ],
            **guardian_kwargs(read_fd),
        )
        assert report.state == "execution_unknown"
        assert "durable store unavailable" in report.reason
        assert not marker.exists()
        assert callbacks.request is not None
        with pytest.raises(ChildProcessError):
            os.waitpid(callbacks.request.root_group.identity.pid, os.WNOHANG)
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_hung_registration_is_bounded_and_executes_zero_paid_command(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "must-not-exist"
    release_hung_callback = threading.Event()

    class HungRegister:
        def register(self, request: RegistrationRequest) -> RegistrationAck:
            release_hung_callback.wait(10)
            raise AssertionError("unreachable")

        def poll(self, registration_id: str) -> PollSnapshot:
            raise AssertionError("must not poll")

        def internal_interrupt(self, registration_id: str, request_sha256: str) -> None:
            raise AssertionError("must not interrupt")

        def lifeline_lost(self, registration_id: str, request_sha256: str) -> None:
            raise AssertionError("must not report")

        def finalize(self, report) -> None:  # noqa: ANN001
            raise AssertionError("must not finalize")

    read_fd, write_fd = os.pipe()
    started = time.monotonic()
    try:
        report = Guardian(HungRegister(), registration_timeout=0.1).run(
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()",
            ],
            **guardian_kwargs(read_fd),
        )
        assert time.monotonic() - started < 1.0
        assert report.state == "execution_unknown"
        assert "register did not return" in report.reason
        assert not marker.exists()
    finally:
        release_hung_callback.set()
        time.sleep(0.01)
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_elapsed_hard_stop_before_release_reaps_without_exec(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    inspector = SystemProcessInspector()
    now_wall = time.time()
    now_monotonic = time.monotonic()
    callbacks = RecordingCallbacks(
        projection(
            start=now_wall - 9_000.0,
            projected_wall=now_wall,
            projected_monotonic=now_monotonic,
            boot=inspector.boot_identity(),
        )
    )
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(callbacks, inspector=inspector).run(
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()",
            ],
            **guardian_kwargs(read_fd),
        )
        assert report.state == "watchdog_forced"
        assert report.reason == "hard_stop_due_before_release"
        assert callbacks.request is not None
        assert report.already_empty_pgids == (
            callbacks.request.root_group.identity.pgid,
        )
        assert not marker.exists()
        with pytest.raises(ChildProcessError):
            os.waitpid(callbacks.request.root_group.identity.pid, os.WNOHANG)
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_normal_rc_zero_requires_empty_group_and_durable_finalize() -> None:
    now_wall = time.time()
    now_mono = time.monotonic()
    callbacks = RecordingCallbacks(
        projection(
            start=now_wall - 1,
            projected_wall=now_wall,
            projected_monotonic=now_mono,
            boot=SystemProcessInspector().boot_identity(),
        )
    )
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(callbacks).run(
            [sys.executable, "-c", "pass"], **guardian_kwargs(read_fd)
        )
        assert report.state == "completed", report.reason
        assert report.direct_returncode == 0
        assert report.reason == "paid_group_empty"
        assert callbacks.finalized == [report]
        assert callbacks.request is not None
        assert report.already_empty_pgids == (
            callbacks.request.root_group.identity.pgid,
        )
        assert set(report.as_dict()) == {
            "registration_id",
            "request_sha256",
            "state",
            "reason",
            "forced",
            "direct_returncode",
            "stopped_pgids",
            "killed_pgids",
            "already_empty_pgids",
        }
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_known_worker_rc_residual_uses_one_exact_terminal_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = os.getuid()
    root = ProcessIdentity(101, uid, 101, "root-start")
    residual = ProcessIdentity(102, uid, 101, "helper-start")
    inspector = FakeInspector([root, residual])
    now_wall = time.time()
    callbacks = RecordingCallbacks(
        projection(
            start=now_wall,
            projected_wall=now_wall,
            projected_monotonic=time.monotonic(),
            boot=inspector.boot_identity(),
        )
    )

    class FakeBlockedChild:
        leader_pid = root.pid
        command_sha256 = "b" * 64
        released = False

        @classmethod
        def spawn(cls, *_args, **_kwargs):  # noqa: ANN002, ANN003
            return cls()

        def release(self) -> None:
            self.released = True

        @property
        def worker_returncode(self) -> int:
            return 0

        def retire_after_empty(self, _inspector) -> int:  # noqa: ANN001
            raise ResidualDescendants(
                "direct rc observed with residual descendants: "
                + guardian_module._residual_identity_diagnostic((residual,))  # noqa: SLF001
            )

        def reap(self, timeout: float = 2.0) -> int:  # noqa: ARG002
            return 0

        def leader_returncode(self) -> int | None:
            return None

        def close_without_release(self) -> None:
            return None

        def close(self) -> None:
            return None

    class RemovingSignaler(FakeSignaler):
        def killpg(self, pgid: int, sig: int) -> None:
            super().killpg(pgid, sig)
            if sig == signal.SIGKILL:
                for pid in tuple(inspector.identities):
                    if inspector.identities[pid].pgid == pgid:
                        del inspector.identities[pid]

    monkeypatch.setattr(guardian_module, "BlockedProcessGroup", FakeBlockedChild)
    signaler = RemovingSignaler()
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            signaler=signaler,
            poll_interval=0,
        ).run([sys.executable, "-c", "pass"], **guardian_kwargs(read_fd))
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert report.state == "completed"
    assert report.reason == "paid_worker_returned_group_cleanup"
    assert report.direct_returncode == 0
    assert report.forced is False
    assert report.stopped_pgids == (root.pgid,)
    assert report.killed_pgids == (root.pgid,)
    assert report.already_empty_pgids == ()
    assert signaler.calls == [
        (root.pgid, signal.SIGSTOP),
        (root.pgid, signal.SIGKILL),
    ]
    assert callbacks.finalized == [report]


def test_worker_rc_terminal_cleanup_cannot_launder_unattested_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = os.getuid()
    root = ProcessIdentity(101, uid, 101, "root-start")
    candidate = ProcessIdentity(202, uid, 202, "late-setsid-start")

    class CandidateInspector(FakeInspector):
        def descendants(self, pid: int) -> tuple[ProcessIdentity, ...]:
            return (candidate,) if pid == root.pid else ()

    inspector = CandidateInspector([root, candidate])
    now_wall = time.time()
    callbacks = RecordingCallbacks(
        projection(
            start=now_wall,
            projected_wall=now_wall,
            projected_monotonic=time.monotonic(),
            boot=inspector.boot_identity(),
        )
    )

    class FakeBlockedChild:
        leader_pid = root.pid
        command_sha256 = "b" * 64

        @classmethod
        def spawn(cls, *_args, **_kwargs):  # noqa: ANN002, ANN003
            return cls()

        def release(self) -> None:
            return None

        @property
        def worker_returncode(self) -> int:
            return 0

        def retire_after_empty(self, _inspector) -> int:  # noqa: ANN001
            raise AssertionError("an unattested candidate must preclude retirement")

        def reap(self, timeout: float = 2.0) -> int:  # noqa: ARG002
            return 0

        def leader_returncode(self) -> int | None:
            return None

        def close_without_release(self) -> None:
            return None

        def close(self) -> None:
            return None

    class RemovingSignaler(FakeSignaler):
        def killpg(self, pgid: int, sig: int) -> None:
            super().killpg(pgid, sig)
            if sig == signal.SIGKILL:
                for pid in tuple(inspector.identities):
                    if inspector.identities[pid].pgid == pgid:
                        del inspector.identities[pid]

    monkeypatch.setattr(guardian_module, "BlockedProcessGroup", FakeBlockedChild)
    signaler = RemovingSignaler()
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            signaler=signaler,
            poll_interval=0,
        ).run([sys.executable, "-c", "pass"], **guardian_kwargs(read_fd))
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert report.state == "execution_unknown"
    assert "unattested paid candidates" in report.reason
    assert "terminal cleanup captured an unattested paid group" in report.reason
    assert report.stopped_pgids == (root.pgid, candidate.pgid)
    assert report.killed_pgids == (root.pgid, candidate.pgid)
    assert signaler.calls == [
        (root.pgid, signal.SIGSTOP),
        (candidate.pgid, signal.SIGSTOP),
        (root.pgid, signal.SIGKILL),
        (candidate.pgid, signal.SIGKILL),
    ]
    assert inspector.identities == {}


def test_durable_worker_return_waits_for_post_poll_candidate_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = os.getuid()
    root = ProcessIdentity(101, uid, 101, "root-start")
    candidate = ProcessIdentity(202, uid, 202, "teardown-helper-start")

    class PostPollCandidateInspector(FakeInspector):
        def __init__(self) -> None:
            super().__init__([root, candidate])
            self.root_descendant_scans = 0

        def descendants(self, pid: int) -> tuple[ProcessIdentity, ...]:
            if pid != root.pid:
                return ()
            self.root_descendant_scans += 1
            if self.root_descendant_scans == 1:
                return ()
            return (candidate,) if candidate.pid in self.identities else ()

    inspector = PostPollCandidateInspector()
    now_wall = time.time()

    class AttestingCallbacks(RecordingCallbacks):
        def poll(
            self,
            registration_id: str,
            discovered_groups: tuple[PaidGroup, ...] = (),
        ) -> PollSnapshot:
            assert self.request is not None
            self.poll_count += 1
            paid_groups = [self.request.root_group]
            if discovered_groups:
                assert self.poll_count >= 2
                paid_groups.extend(discovered_groups)
                for group in discovered_groups:
                    inspector.identities.pop(group.identity.pid, None)
            return PollSnapshot(
                self.poll_count,
                registration_id,
                self.request.request_sha256,
                self.request.boot_identity,
                tuple(paid_groups),
            )

    callbacks = AttestingCallbacks(
        projection(
            start=now_wall,
            projected_wall=now_wall,
            projected_monotonic=time.monotonic(),
            boot=inspector.boot_identity(),
        )
    )

    class FakeBlockedChild:
        leader_pid = root.pid
        command_sha256 = "b" * 64

        @classmethod
        def spawn(cls, *_args, **_kwargs):  # noqa: ANN002, ANN003
            return cls()

        def release(self) -> None:
            return None

        @property
        def worker_returncode(self) -> int:
            return 0

        def retire_after_empty(self, _inspector) -> int:  # noqa: ANN001
            inspector.identities.pop(root.pid, None)
            return 0

        def reap(self, timeout: float = 2.0) -> int:  # noqa: ARG002
            return 0

        def leader_returncode(self) -> int | None:
            return None

        def close_without_release(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(guardian_module, "BlockedProcessGroup", FakeBlockedChild)
    signaler = FakeSignaler()
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            signaler=signaler,
            poll_interval=0,
            durably_attest_discovered_groups=True,
        ).run([sys.executable, "-c", "pass"], **guardian_kwargs(read_fd))
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert callbacks.poll_count >= 2
    assert report.state == "completed", report.reason
    assert report.reason == "paid_group_empty"
    assert report.already_empty_pgids == (root.pgid, candidate.pgid)
    assert signaler.calls == []
    assert callbacks.finalized == [report]


def test_worker_rc_cleanup_crossing_t150_is_watchdog_and_not_resignalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = os.getuid()
    root = ProcessIdentity(101, uid, 101, "root-start")
    residual = ProcessIdentity(102, uid, 101, "helper-start")
    inspector = FakeInspector([root, residual])
    wall = MutableClock(1_000.0)
    monotonic = MutableClock(50.0)
    projected = projection(
        start=1_000.0,
        projected_wall=wall.value,
        projected_monotonic=monotonic.value,
        boot=inspector.boot_identity(),
    )
    callbacks = RecordingCallbacks(projected)

    class FakeBlockedChild:
        leader_pid = root.pid
        command_sha256 = "b" * 64

        @classmethod
        def spawn(cls, *_args, **_kwargs):  # noqa: ANN002, ANN003
            return cls()

        def release(self) -> None:
            return None

        @property
        def worker_returncode(self) -> int:
            return 0

        def retire_after_empty(self, _inspector) -> int:  # noqa: ANN001
            raise ResidualDescendants("known helper remained")

        def reap(self, timeout: float = 2.0) -> int:  # noqa: ARG002
            return 0

        def leader_returncode(self) -> int | None:
            return None

        def close_without_release(self) -> None:
            return None

        def close(self) -> None:
            return None

    class DeadlineSignaler(FakeSignaler):
        def killpg(self, pgid: int, sig: int) -> None:
            super().killpg(pgid, sig)
            if sig == signal.SIGKILL:
                inspector.identities.clear()
                wall.value = projected.hard_stop_wall_epoch
                monotonic.value = projected.hard_stop_monotonic

    monkeypatch.setattr(guardian_module, "BlockedProcessGroup", FakeBlockedChild)
    signaler = DeadlineSignaler()
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            signaler=signaler,
            wall_clock=wall,
            monotonic_clock=monotonic,
            poll_interval=0,
        ).run([sys.executable, "-c", "pass"], **guardian_kwargs(read_fd))
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert report.state == "watchdog_forced"
    assert report.reason == "absolute_hard_stop"
    assert report.direct_returncode == 0
    assert signaler.calls == [
        (root.pgid, signal.SIGSTOP),
        (root.pgid, signal.SIGKILL),
    ]
    assert callbacks.finalized == [report]


def test_cleanup_finalize_response_unknown_never_repeats_stop_or_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = os.getuid()
    root = ProcessIdentity(101, uid, 101, "root-start")
    residual = ProcessIdentity(102, uid, 101, "helper-start")
    inspector = FakeInspector([root, residual])
    now_wall = time.time()

    class LostFinalizeCallbacks(RecordingCallbacks):
        def finalize(self, report) -> None:  # noqa: ANN001
            self.finalized.append(report)
            raise ConnectionError("finalize response lost")

    callbacks = LostFinalizeCallbacks(
        projection(
            start=now_wall,
            projected_wall=now_wall,
            projected_monotonic=time.monotonic(),
            boot=inspector.boot_identity(),
        )
    )

    class FakeBlockedChild:
        leader_pid = root.pid
        command_sha256 = "b" * 64

        @classmethod
        def spawn(cls, *_args, **_kwargs):  # noqa: ANN002, ANN003
            return cls()

        def release(self) -> None:
            return None

        @property
        def worker_returncode(self) -> int:
            return 0

        def retire_after_empty(self, _inspector) -> int:  # noqa: ANN001
            raise ResidualDescendants("known helper remained")

        def reap(self, timeout: float = 2.0) -> int:  # noqa: ARG002
            return 0

        def leader_returncode(self) -> int | None:
            return None

        def close_without_release(self) -> None:
            return None

        def close(self) -> None:
            return None

    class RemovingSignaler(FakeSignaler):
        def killpg(self, pgid: int, sig: int) -> None:
            super().killpg(pgid, sig)
            if sig == signal.SIGKILL:
                inspector.identities.clear()

    monkeypatch.setattr(guardian_module, "BlockedProcessGroup", FakeBlockedChild)
    signaler = RemovingSignaler()
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            signaler=signaler,
            poll_interval=0,
        ).run([sys.executable, "-c", "pass"], **guardian_kwargs(read_fd))
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert report.state == "execution_unknown"
    assert report.reason == "finalize_response_unknown:HostControlFailure"
    assert signaler.calls == [
        (root.pgid, signal.SIGSTOP),
        (root.pgid, signal.SIGKILL),
    ]
    assert len(callbacks.finalized) == 2
    assert callbacks.finalized[0] == callbacks.finalized[1]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_real_worker_rc_zero_lingering_helper_is_frozen_killed_and_completed(
    tmp_path: Path,
) -> None:
    inspector = SystemProcessInspector()
    now_wall = time.time()
    callbacks = RecordingCallbacks(
        projection(
            start=now_wall,
            projected_wall=now_wall,
            projected_monotonic=time.monotonic(),
            boot=inspector.boot_identity(),
        )
    )
    helper_path = tmp_path / "lingering-helper.pid"
    source = (
        "import subprocess,sys; from pathlib import Path; "
        "helper=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
        f"Path({str(helper_path)!r}).write_text(str(helper.pid))"
    )
    signaler = RecordingOSSignaler()
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            signaler=signaler,
            poll_interval=0.005,
        ).run([sys.executable, "-c", source], **guardian_kwargs(read_fd))
        helper_pid = int(helper_path.read_text(encoding="ascii"))
        assert report.state == "completed", report.reason
        assert report.reason == "paid_worker_returned_group_cleanup"
        assert report.direct_returncode == 0
        assert report.stopped_pgids == report.killed_pgids
        assert callbacks.request is not None
        root_pgid = callbacks.request.root_group.identity.pgid
        assert report.killed_pgids == (root_pgid,)
        assert (root_pgid, signal.SIGSTOP) in signaler.calls
        assert (root_pgid, signal.SIGKILL) in signaler.calls
        assert inspector.identity(helper_pid) is None
        assert inspector.group_members(root_pgid) == ()
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_known_nonzero_worker_rc_is_still_an_exact_completed_terminal() -> None:
    now_wall = time.time()
    now_mono = time.monotonic()
    callbacks = RecordingCallbacks(
        projection(
            start=now_wall - 1,
            projected_wall=now_wall,
            projected_monotonic=now_mono,
            boot=SystemProcessInspector().boot_identity(),
        )
    )
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(callbacks).run(
            [sys.executable, "-c", "raise SystemExit(70)"],
            **guardian_kwargs(read_fd),
        )
        assert report.state == "completed"
        assert report.direct_returncode == 70
        assert report.reason == "paid_group_empty"
        assert report.forced is False
        assert callbacks.finalized == [report]
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_lost_finalize_reply_replays_exact_same_marker() -> None:
    inspector = SystemProcessInspector()
    now_wall = time.time()

    class LostReplyCallbacks(RecordingCallbacks):
        def finalize(self, report) -> None:  # noqa: ANN001
            self.finalized.append(report)
            if len(self.finalized) == 1:
                raise ConnectionError("reply lost after durable commit")

    callbacks = LostReplyCallbacks(
        projection(
            start=now_wall,
            projected_wall=now_wall,
            projected_monotonic=time.monotonic(),
            boot=inspector.boot_identity(),
        )
    )
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(callbacks, inspector=inspector).run(
            [sys.executable, "-c", "pass"], **guardian_kwargs(read_fd)
        )
        assert report.state == "completed"
        assert callbacks.finalized == [report, report]
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_identical_poll_sequence_replay_is_accepted_once() -> None:
    inspector = SystemProcessInspector()
    now_wall = time.time()

    class DuplicateCallbacks(RecordingCallbacks):
        def poll(self, registration_id: str) -> PollSnapshot:
            assert self.request is not None
            self.poll_count += 1
            sequence = 1 if self.poll_count <= 2 else self.poll_count - 1
            return PollSnapshot(
                sequence,
                registration_id,
                self.request.request_sha256,
                self.request.boot_identity,
                (self.request.root_group,),
            )

    callbacks = DuplicateCallbacks(
        projection(
            start=now_wall,
            projected_wall=now_wall,
            projected_monotonic=time.monotonic(),
            boot=inspector.boot_identity(),
        )
    )
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(callbacks, inspector=inspector, poll_interval=0.01).run(
            [sys.executable, "-c", "import time; time.sleep(0.08)"],
            **guardian_kwargs(read_fd),
        )
        assert callbacks.poll_count >= 2
        assert report.state == "completed"
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_unattested_candidate_expires_on_next_completed_poll_even_same_sequence(
    tmp_path: Path,
) -> None:
    inspector = SystemProcessInspector()
    now_wall = time.time()
    escaped_pid_path = tmp_path / "same-sequence-escape.pid"

    class SameSequenceCallbacks(RecordingCallbacks):
        def poll(self, registration_id: str) -> PollSnapshot:
            assert self.request is not None
            self.poll_count += 1
            return PollSnapshot(
                1,
                registration_id,
                self.request.request_sha256,
                self.request.boot_identity,
                (self.request.root_group,),
            )

    callbacks = SameSequenceCallbacks(
        projection(
            start=now_wall,
            projected_wall=now_wall,
            projected_monotonic=time.monotonic(),
            boot=inspector.boot_identity(),
        )
    )
    command = (
        "import os,time; from pathlib import Path; child=os.fork(); "
        "(os.setsid(), Path(%r).write_text(str(os.getpid())), time.sleep(30)) "
        "if child == 0 else time.sleep(30)"
    ) % str(escaped_pid_path)
    signaler = RecordingOSSignaler()
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            signaler=signaler,
            poll_interval=0.01,
        ).run([sys.executable, "-c", command], **guardian_kwargs(read_fd))
        assert callbacks.poll_count >= 2
        escaped_pid = int(escaped_pid_path.read_text(encoding="ascii"))
        assert (escaped_pid, signal.SIGKILL) in signaler.calls
        assert report.state == "execution_unknown"
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_opaque_candidate_is_promoted_only_after_durable_snapshot_echo_and_exits_empty(
    tmp_path: Path,
) -> None:
    inspector = SystemProcessInspector()
    now_wall = time.time()
    escaped_pid_path = tmp_path / "durably-attested-escape.pid"

    class DurableDiscoveryCallbacks(RecordingCallbacks):
        def __init__(self) -> None:
            super().__init__(
                projection(
                    start=now_wall,
                    projected_wall=now_wall,
                    projected_monotonic=time.monotonic(),
                    boot=inspector.boot_identity(),
                )
            )
            self.discovered_submissions: list[tuple[PaidGroup, ...]] = []
            self.persisted: dict[int, PaidGroup] = {}
            self.echoed_polls = 0

        def poll(
            self,
            registration_id: str,
            discovered_groups: tuple[PaidGroup, ...] = (),
        ) -> PollSnapshot:
            assert self.request is not None
            self.poll_count += 1
            self.discovered_submissions.append(discovered_groups)
            for group in discovered_groups:
                self.persisted[group.identity.pgid] = group
            live: list[PaidGroup] = []
            for pgid, group in tuple(self.persisted.items()):
                current = inspector.identity(group.identity.pid)
                try:
                    members = inspector.group_members(pgid)
                except IdentityViolation:
                    # Darwin can briefly keep an exited process group
                    # addressable while native enumeration exposes no
                    # identity.  The production host conservatively keeps
                    # its durable row live across that observation.
                    live.append(group)
                    continue
                if current == group.identity:
                    live.append(group)
                elif current is None and not members:
                    del self.persisted[pgid]
                else:
                    live.append(group)
            if live:
                self.echoed_polls += 1
            return PollSnapshot(
                self.poll_count,
                registration_id,
                self.request.request_sha256,
                self.request.boot_identity,
                (self.request.root_group, *live),
            )

    callbacks = DurableDiscoveryCallbacks()
    command = (
        "import os,time; from pathlib import Path; child=os.fork(); "
        "(os.setsid(), Path(%r).write_text(str(os.getpid())), time.sleep(0.18)) "
        "if child == 0 else (time.sleep(0.04), os.waitpid(child, 0), time.sleep(0.08))"
    ) % str(escaped_pid_path)
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            poll_interval=0.005,
            durably_attest_discovered_groups=True,
        ).run([sys.executable, "-c", command], **guardian_kwargs(read_fd))
        escaped_pgid = int(escaped_pid_path.read_text(encoding="ascii"))
        submitted = [
            group
            for groups in callbacks.discovered_submissions
            for group in groups
            if group.identity.pgid == escaped_pgid
        ]
        assert submitted
        assert callbacks.echoed_polls >= 2
        assert report.state == "completed", report.reason
        assert escaped_pgid in report.already_empty_pgids
        assert inspector.identity(escaped_pgid) is None
        assert inspector.group_members(escaped_pgid) == ()
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_locally_expired_candidate_is_submitted_before_terminal_receipt() -> None:
    delegate = SystemProcessInspector()
    uid = os.getuid()
    synthetic = ProcessIdentity(999_991, uid, 999_991, "unsubmitted-short-lived")

    class OnePostPollCandidateInspector:
        root_pid: int | None = None
        root_descendant_calls = 0

        def boot_identity(self) -> str:
            return delegate.boot_identity()

        def identity(self, pid: int) -> ProcessIdentity | None:
            return delegate.identity(pid)

        def group_members(self, pgid: int) -> tuple[ProcessIdentity, ...]:
            return delegate.group_members(pgid)

        def descendants(self, pid: int) -> tuple[ProcessIdentity, ...]:
            if self.root_pid is None:
                self.root_pid = pid
            if pid == self.root_pid:
                self.root_descendant_calls += 1
                if self.root_descendant_calls in {2, 3}:
                    return (synthetic,)
            return delegate.descendants(pid)

    inspector = OnePostPollCandidateInspector()
    now_wall = time.time()

    class RootOnlyCallbacks(RecordingCallbacks):
        def __init__(self) -> None:
            super().__init__(
                projection(
                    start=now_wall,
                    projected_wall=now_wall,
                    projected_monotonic=time.monotonic(),
                    boot=inspector.boot_identity(),
                )
            )
            self.discovered_submissions: list[tuple[PaidGroup, ...]] = []

        def poll(
            self,
            registration_id: str,
            discovered_groups: tuple[PaidGroup, ...] = (),
        ) -> PollSnapshot:
            assert self.request is not None
            self.poll_count += 1
            self.discovered_submissions.append(discovered_groups)
            return PollSnapshot(
                self.poll_count,
                registration_id,
                self.request.request_sha256,
                self.request.boot_identity,
                (self.request.root_group,),
            )

    callbacks = RootOnlyCallbacks()
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            poll_interval=0.005,
            durably_attest_discovered_groups=True,
        ).run(
            [sys.executable, "-c", "import time; time.sleep(0.08)"],
            **guardian_kwargs(read_fd),
        )

        assert inspector.root_descendant_calls >= 3
        assert any(
            group.identity.pgid == synthetic.pgid
            for groups in callbacks.discovered_submissions
            for group in groups
        )
        assert report.state == "completed", report.reason
        assert synthetic.pgid in report.already_empty_pgids
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_durable_discovered_group_exit_after_commit_before_snapshot_is_proven_empty(
    tmp_path: Path,
) -> None:
    delegate = SystemProcessInspector()

    class OneTransientEmptyInspection:
        target_pgid: int | None = None
        raised = False

        def boot_identity(self) -> str:
            return delegate.boot_identity()

        def identity(self, pid: int) -> ProcessIdentity | None:
            return delegate.identity(pid)

        def descendants(self, pid: int) -> tuple[ProcessIdentity, ...]:
            return delegate.descendants(pid)

        def group_members(self, pgid: int) -> tuple[ProcessIdentity, ...]:
            if (
                pgid == self.target_pgid
                and not self.raised
                and delegate.identity(pgid) is None
            ):
                self.raised = True
                raise IdentityViolation("transient Darwin group visibility")
            return delegate.group_members(pgid)

    inspector = OneTransientEmptyInspection()
    now_wall = time.time()
    escaped_pid_path = tmp_path / "commit-return-race.pid"

    class CommitThenDelayCallbacks(RecordingCallbacks):
        committed: PaidGroup | None = None

        def poll(
            self,
            registration_id: str,
            discovered_groups: tuple[PaidGroup, ...] = (),
        ) -> PollSnapshot:
            assert self.request is not None
            self.poll_count += 1
            if discovered_groups:
                assert len(discovered_groups) == 1
                self.committed = discovered_groups[0]
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    identity = delegate.identity(self.committed.identity.pid)
                    try:
                        members = delegate.group_members(
                            self.committed.identity.pgid
                        )
                    except IdentityViolation:
                        time.sleep(0.005)
                        continue
                    if identity is None and not members:
                        break
                    time.sleep(0.005)
                else:
                    raise AssertionError("committed candidate did not exit")
                inspector.target_pgid = self.committed.identity.pgid
                groups = (self.request.root_group, self.committed)
            else:
                groups = (self.request.root_group,)
            return PollSnapshot(
                self.poll_count,
                registration_id,
                self.request.request_sha256,
                self.request.boot_identity,
                groups,
            )

    callbacks = CommitThenDelayCallbacks(
        projection(
            start=now_wall,
            projected_wall=now_wall,
            projected_monotonic=time.monotonic(),
            boot=inspector.boot_identity(),
        )
    )
    command = (
        "import os,time; from pathlib import Path; child=os.fork(); "
        "(os.setsid(), Path(%r).write_text(str(os.getpid())), time.sleep(0.15)) "
        "if child == 0 else (os.waitpid(child, 0), time.sleep(0.12))"
    ) % str(escaped_pid_path)
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            poll_interval=0.005,
            durably_attest_discovered_groups=True,
        ).run([sys.executable, "-c", command], **guardian_kwargs(read_fd))
        escaped_pgid = int(escaped_pid_path.read_text(encoding="ascii"))
        assert callbacks.committed is not None
        assert inspector.raised
        assert callbacks.committed.identity.pgid == escaped_pgid
        assert report.state == "completed", report.reason
        assert escaped_pgid in report.already_empty_pgids
        assert inspector.identity(escaped_pgid) is None
        assert inspector.group_members(escaped_pgid) == ()
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_opaque_host_rejection_never_leaves_discovered_setsid_group_live(
    tmp_path: Path,
) -> None:
    inspector = SystemProcessInspector()
    now_wall = time.time()
    escaped_pid_path = tmp_path / "rejected-escape.pid"

    class OmittingDiscoveryCallbacks(RecordingCallbacks):
        def poll(
            self,
            registration_id: str,
            discovered_groups: tuple[PaidGroup, ...] = (),
        ) -> PollSnapshot:
            assert self.request is not None
            self.poll_count += 1
            return PollSnapshot(
                self.poll_count,
                registration_id,
                self.request.request_sha256,
                self.request.boot_identity,
                (self.request.root_group,),
            )

    callbacks = OmittingDiscoveryCallbacks(
        projection(
            start=now_wall,
            projected_wall=now_wall,
            projected_monotonic=time.monotonic(),
            boot=inspector.boot_identity(),
        )
    )
    command = (
        "import os,time; from pathlib import Path; child=os.fork(); "
        "(os.setsid(), Path(%r).write_text(str(os.getpid())), time.sleep(30)) "
        "if child == 0 else time.sleep(30)"
    ) % str(escaped_pid_path)
    signaler = RecordingOSSignaler()
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            signaler=signaler,
            poll_interval=0.005,
            durably_attest_discovered_groups=True,
        ).run([sys.executable, "-c", command], **guardian_kwargs(read_fd))
        escaped_pgid = int(escaped_pid_path.read_text(encoding="ascii"))
        assert report.state == "execution_unknown"
        assert "omitted a discovered paid group" in report.reason
        assert escaped_pgid in report.killed_pgids
        assert inspector.identity(escaped_pgid) is None
        assert inspector.group_members(escaped_pgid) == ()
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_durable_reparented_setsid_leader_second_generation_is_attested_and_empty(
    tmp_path: Path,
) -> None:
    inspector = SystemProcessInspector()
    now_wall = time.time()
    first_path = tmp_path / "first.pid"
    second_path = tmp_path / "second.pid"
    broker_exit = tmp_path / "broker.exit"
    spawn_second = tmp_path / "spawn.second"
    finish = tmp_path / "finish"
    worker_source = f"""
import os
import time
from pathlib import Path
first = Path({str(first_path)!r})
second = Path({str(second_path)!r})
broker_exit = Path({str(broker_exit)!r})
spawn_second = Path({str(spawn_second)!r})
finish = Path({str(finish)!r})
broker = os.fork()
if broker == 0:
    candidate = os.fork()
    if candidate == 0:
        os.setsid()
        first.write_text(str(os.getpid()))
        while not spawn_second.exists():
            time.sleep(0.005)
        nested = os.fork()
        if nested == 0:
            os.setsid()
            second.write_text(str(os.getpid()))
            while not finish.exists():
                time.sleep(0.005)
            os._exit(0)
        while not finish.exists():
            time.sleep(0.005)
        os.waitpid(nested, 0)
        os._exit(0)
    while not broker_exit.exists():
        time.sleep(0.005)
    os._exit(0)
os.waitpid(broker, 0)
while not finish.exists():
    time.sleep(0.005)
time.sleep(0.2)
"""

    class ReparentingHostCallbacks(RecordingCallbacks):
        persisted: dict[int, PaidGroup]
        submitted_pgids: list[int]
        reparented = False

        def __init__(self) -> None:
            super().__init__(
                projection(
                    start=now_wall,
                    projected_wall=now_wall,
                    projected_monotonic=time.monotonic(),
                    boot=inspector.boot_identity(),
                )
            )
            self.persisted = {}
            self.submitted_pgids = []

        def poll(
            self,
            registration_id: str,
            discovered_groups: tuple[PaidGroup, ...] = (),
        ) -> PollSnapshot:
            assert self.request is not None
            self.poll_count += 1
            for group in discovered_groups:
                self.persisted[group.identity.pgid] = group
                self.submitted_pgids.append(group.identity.pgid)
            if discovered_groups and not self.reparented:
                first_pgid = discovered_groups[0].identity.pgid
                broker_exit.touch()
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    descendants = inspector.descendants(
                        self.request.root_group.identity.pid
                    )
                    if all(item.pid != first_pgid for item in descendants):
                        break
                    time.sleep(0.005)
                else:
                    raise AssertionError("first candidate was not reparented")
                spawn_second.touch()
                while not second_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                if not second_path.exists():
                    raise AssertionError("second setsid candidate did not start")
                self.reparented = True
            elif discovered_groups and self.reparented:
                finish.touch()
            live: list[PaidGroup] = []
            for pgid, group in tuple(self.persisted.items()):
                current = inspector.identity(group.identity.pid)
                try:
                    members = inspector.group_members(pgid)
                except IdentityViolation:
                    live.append(group)
                    continue
                if current == group.identity or members:
                    live.append(group)
                else:
                    del self.persisted[pgid]
            return PollSnapshot(
                self.poll_count,
                registration_id,
                self.request.request_sha256,
                self.request.boot_identity,
                (self.request.root_group, *live),
            )

    callbacks = ReparentingHostCallbacks()
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            poll_interval=0.005,
            durably_attest_discovered_groups=True,
        ).run(
            [sys.executable, "-c", worker_source],
            **guardian_kwargs(read_fd),
        )
        first_pgid = int(first_path.read_text(encoding="ascii"))
        second_pgid = int(second_path.read_text(encoding="ascii"))
        assert callbacks.submitted_pgids.count(first_pgid) == 1
        assert callbacks.submitted_pgids.count(second_pgid) == 1
        assert report.state == "completed", report.reason
        assert {first_pgid, second_pgid} <= set(report.already_empty_pgids)
        for pgid in (first_pgid, second_pgid):
            assert inspector.identity(pgid) is None
            assert inspector.group_members(pgid) == ()
    finally:
        finish.touch()
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_candidate_created_during_poll_can_be_attested_on_next_poll(
    tmp_path: Path,
) -> None:
    inspector = SystemProcessInspector()
    now_wall = time.time()
    auxiliary_pid_path = tmp_path / "attested-aux.pid"

    class AttestingCallbacks(RecordingCallbacks):
        def poll(self, registration_id: str) -> PollSnapshot:
            assert self.request is not None
            self.poll_count += 1
            deadline = time.monotonic() + 1.0
            while not auxiliary_pid_path.exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            paid_groups = [self.request.root_group]
            if self.poll_count >= 2 and auxiliary_pid_path.exists():
                auxiliary_pid = int(auxiliary_pid_path.read_text(encoding="ascii"))
                identity = inspector.identity(auxiliary_pid)
                if identity is not None:
                    paid_groups.append(PaidGroup("reviewer", identity))
            return PollSnapshot(
                self.poll_count,
                registration_id,
                self.request.request_sha256,
                self.request.boot_identity,
                tuple(paid_groups),
            )

    callbacks = AttestingCallbacks(
        projection(
            start=now_wall,
            projected_wall=now_wall,
            projected_monotonic=time.monotonic(),
            boot=inspector.boot_identity(),
        )
    )
    command = (
        "import os,time; from pathlib import Path; child=os.fork(); "
        "(os.setsid(), Path(%r).write_text(str(os.getpid())), time.sleep(0.15)) "
        "if child == 0 else (time.sleep(0.2), os.waitpid(child, 0), time.sleep(0.2))"
    ) % str(auxiliary_pid_path)
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            poll_interval=0.01,
        ).run([sys.executable, "-c", command], **guardian_kwargs(read_fd))
        assert callbacks.poll_count >= 2
        assert report.state == "completed", report.reason
        assert report.direct_returncode == 0
        assert callbacks.request is not None
        auxiliary_pgid = int(auxiliary_pid_path.read_text(encoding="ascii"))
        assert report.already_empty_pgids == tuple(
            sorted((callbacks.request.root_group.identity.pgid, auxiliary_pgid))
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_t_minus_five_interrupt_then_t150_stop_kill_without_grace() -> None:
    inspector = SystemProcessInspector()
    boot = inspector.boot_identity()
    wall = MutableClock(8_994.0)
    monotonic = MutableClock(100.0)
    projected = DeadlineProjection(
        cycle_started_wall_epoch=0.0,
        cycle_started_monotonic=-8_894.0,
        internal_interrupt_wall_epoch=8_995.0,
        internal_interrupt_monotonic=101.0,
        hard_stop_wall_epoch=9_000.0,
        hard_stop_monotonic=106.0,
        projected_wall_epoch=8_994.0,
        projected_monotonic=100.0,
        boot_identity=boot,
    )
    callbacks = RecordingCallbacks(projected, wall=wall, monotonic=monotonic)
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            wall_clock=wall,
            monotonic_clock=monotonic,
            poll_interval=0,
        ).run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **guardian_kwargs(read_fd),
        )
        assert len(callbacks.interrupts) == 1
        assert report.state == "watchdog_forced"
        assert report.reason == "absolute_hard_stop"
        assert report.stopped_pgids == report.killed_pgids
        assert len(report.killed_pgids) == 1
        assert callbacks.finalized == [report]
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_t150_stops_kills_reaps_and_proves_root_reviewer_verifier_empty() -> None:
    inspector = SystemProcessInspector()
    auxiliaries = [
        BlockedProcessGroup.spawn(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            inspector=inspector,
        )
        for _ in range(2)
    ]
    for auxiliary in auxiliaries:
        auxiliary.release()
    auxiliary_identities = tuple(
        inspector.identity(item.leader_pid) for item in auxiliaries
    )
    assert all(item is not None for item in auxiliary_identities)
    reviewer_identity, verifier_identity = auxiliary_identities
    assert reviewer_identity is not None and verifier_identity is not None
    auxiliary_groups = (
        PaidGroup("reviewer", reviewer_identity),
        PaidGroup("verifier", verifier_identity),
    )
    wall = MutableClock(8_994.0)
    monotonic = MutableClock(100.0)
    projected = DeadlineProjection(
        0.0,
        -8_894.0,
        8_995.0,
        101.0,
        9_000.0,
        106.0,
        8_994.0,
        100.0,
        inspector.boot_identity(),
    )

    class AuxiliaryCallbacks(RecordingCallbacks):
        def poll(self, registration_id: str) -> PollSnapshot:
            base = super().poll(registration_id)
            return replace(
                base,
                paid_groups=(base.paid_groups[0], *auxiliary_groups),
            )

    callbacks = AuxiliaryCallbacks(projected, wall=wall, monotonic=monotonic)
    reapers = {item.leader_pid: item for item in auxiliaries}
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            wall_clock=wall,
            monotonic_clock=monotonic,
            poll_interval=0,
            reap_group=lambda identity: reapers[identity.pid].reap(),
        ).run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **guardian_kwargs(read_fd),
        )
        assert report.state == "watchdog_forced"
        assert len(report.stopped_pgids) == 3
        assert report.stopped_pgids == report.killed_pgids
        assert all(inspector.identity(pgid) is None for pgid in report.killed_pgids)
    finally:
        os.close(read_fd)
        os.close(write_fd)
        for auxiliary in auxiliaries:
            cleanup_group(auxiliary)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_t150_aux_exit_race_is_proven_empty_while_root_is_still_killed() -> None:
    inspector = SystemProcessInspector()
    auxiliary = BlockedProcessGroup.spawn(
        [sys.executable, "-c", "import time; time.sleep(30)"], inspector=inspector
    )
    auxiliary.release()
    auxiliary_identity = inspector.identity(auxiliary.leader_pid)
    assert auxiliary_identity is not None
    auxiliary_group = PaidGroup("reviewer", auxiliary_identity)
    wall = MutableClock(8_994.0)
    monotonic = MutableClock(100.0)
    projected = DeadlineProjection(
        0.0,
        -8_894.0,
        8_995.0,
        101.0,
        9_000.0,
        106.0,
        8_994.0,
        100.0,
        inspector.boot_identity(),
    )

    class AuxiliaryCallbacks(RecordingCallbacks):
        def poll(self, registration_id: str) -> PollSnapshot:
            base = super().poll(registration_id)
            return replace(base, paid_groups=(base.paid_groups[0], auxiliary_group))

    class ExitAuxAfterRootStop(RecordingOSSignaler):
        def killpg(self, pgid: int, sig: int) -> None:
            super().killpg(pgid, sig)
            if sig == signal.SIGSTOP and pgid != auxiliary_identity.pgid:
                os.killpg(auxiliary_identity.pgid, signal.SIGKILL)
                auxiliary.reap(timeout=1.0)

    callbacks = AuxiliaryCallbacks(projected, wall=wall, monotonic=monotonic)
    signaler = ExitAuxAfterRootStop()
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            signaler=signaler,
            wall_clock=wall,
            monotonic_clock=monotonic,
            poll_interval=0,
        ).run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **guardian_kwargs(read_fd),
        )
        assert report.state == "watchdog_forced"
        assert report.already_empty_pgids == (auxiliary_identity.pgid,)
        assert len(report.killed_pgids) == 1
        assert report.killed_pgids[0] != auxiliary_identity.pgid
    finally:
        os.close(read_fd)
        os.close(write_fd)
        cleanup_group(auxiliary)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_post_release_lifeline_eof_is_reported_then_original_t150_is_enforced() -> None:
    inspector = SystemProcessInspector()
    wall = MutableClock(8_994.0)
    monotonic = MutableClock(100.0)
    callbacks = RecordingCallbacks(
        DeadlineProjection(
            cycle_started_wall_epoch=0.0,
            cycle_started_monotonic=-8_894.0,
            internal_interrupt_wall_epoch=8_995.0,
            internal_interrupt_monotonic=101.0,
            hard_stop_wall_epoch=9_000.0,
            hard_stop_monotonic=106.0,
            projected_wall_epoch=8_994.0,
            projected_monotonic=100.0,
            boot_identity=inspector.boot_identity(),
        ),
        wall=wall,
        monotonic=monotonic,
    )
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            wall_clock=wall,
            monotonic_clock=monotonic,
            poll_interval=0,
        ).run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **guardian_kwargs(read_fd),
        )
        assert len(callbacks.lifeline_losses) == 1
        assert report.state == "watchdog_forced"
        assert report.reason == "absolute_hard_stop"
        assert report.forced
        assert report.killed_pgids
        assert callbacks.finalized == [report]
    finally:
        os.close(read_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_detached_guardian_survives_wrapper_sigkill_and_enforces_original_t150(
    tmp_path: Path,
) -> None:
    ready_read, ready_write = os.pipe()
    report_path = tmp_path / "guardian.report"
    lifeline_path = tmp_path / "lifeline.lost"
    identity_path = tmp_path / "groups.txt"
    wrapper_pid = os.fork()
    if wrapper_pid == 0:  # pragma: no cover - parent assertions observe it
        try:
            os.close(ready_read)
            lifeline_read, lifeline_write = os.pipe()
            guardian_pid = os.fork()
            if guardian_pid == 0:
                try:
                    os.close(ready_write)
                    os.close(lifeline_write)
                    os.setsid()
                    inspector = SystemProcessInspector()

                    class DetachedCallbacks:
                        request: RegistrationRequest | None = None
                        sequence = 0

                        def register(
                            self, request: RegistrationRequest
                        ) -> RegistrationAck:
                            self.request = request
                            identity_path.write_text(
                                f"{os.getpgrp()} {request.root_group.identity.pgid}",
                                encoding="ascii",
                            )
                            return RegistrationAck(
                                "reg-detached",
                                request.request_sha256,
                                True,
                                True,
                                imminent_projection(inspector, seconds=0.35),
                            )

                        def poll(self, registration_id: str) -> PollSnapshot:
                            assert self.request is not None
                            self.sequence += 1
                            return PollSnapshot(
                                self.sequence,
                                registration_id,
                                self.request.request_sha256,
                                self.request.boot_identity,
                                (self.request.root_group,),
                            )

                        def internal_interrupt(
                            self, registration_id: str, request_sha256: str
                        ) -> None:
                            return None

                        def lifeline_lost(
                            self, registration_id: str, request_sha256: str
                        ) -> None:
                            lifeline_path.touch()

                        def finalize(self, report) -> None:  # noqa: ANN001
                            report_path.write_text(
                                f"{report.state}|{report.reason}|"
                                f"{','.join(map(str, report.killed_pgids))}",
                                encoding="ascii",
                            )

                    report = Guardian(
                        DetachedCallbacks(), inspector=inspector, poll_interval=0.01
                    ).run(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        **guardian_kwargs(lifeline_read),
                    )
                    os.close(lifeline_read)
                    os._exit(0 if report.state == "watchdog_forced" else 2)
                except BaseException:
                    os._exit(3)
            os.close(lifeline_read)
            os.write(ready_write, f"{guardian_pid}\n".encode("ascii"))
            os.close(ready_write)
            while True:
                time.sleep(10)
        except BaseException:
            os._exit(4)
    os.close(ready_write)
    guardian_pid = int(os.read(ready_read, 64).strip())
    os.close(ready_read)
    try:
        os.kill(wrapper_pid, signal.SIGKILL)
        os.waitpid(wrapper_pid, 0)
        deadline = time.monotonic() + 3.0
        while not report_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert lifeline_path.exists()
        assert report_path.read_text(encoding="ascii").startswith(
            "watchdog_forced|absolute_hard_stop|"
        )
        guardian_pgid, paid_pgid = map(
            int, identity_path.read_text(encoding="ascii").split()
        )
        assert guardian_pgid != paid_pgid
        assert SystemProcessInspector().identity(paid_pgid) is None
    finally:
        try:
            os.kill(guardian_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
@pytest.mark.parametrize("mode", ["poll_failure", "duplicate_equivocation"])
def test_host_poll_failure_or_duplicate_equivocation_fails_closed(mode: str) -> None:
    inspector = SystemProcessInspector()
    now_wall = time.time()
    callbacks = RecordingCallbacks(
        projection(
            start=now_wall,
            projected_wall=now_wall,
            projected_monotonic=time.monotonic(),
            boot=inspector.boot_identity(),
        ),
        fail_poll=mode == "poll_failure",
        equivocate=mode == "duplicate_equivocation",
    )
    read_fd, write_fd = os.pipe()
    try:
        report = Guardian(callbacks, inspector=inspector, poll_interval=0).run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **guardian_kwargs(read_fd),
        )
        assert report.state == "execution_unknown"
        expected = "host unavailable" if mode == "poll_failure" else "equivocated"
        assert expected in report.reason
        assert report.forced
        assert report.killed_pgids
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
@pytest.mark.parametrize("hung_operation", ["poll", "internal_interrupt"])
def test_hung_host_callback_cannot_cross_original_t150(hung_operation: str) -> None:
    inspector = SystemProcessInspector()
    release_hung_callback = threading.Event()

    class HungCallbacks(RecordingCallbacks):
        @staticmethod
        def _hang() -> None:
            release_hung_callback.wait(10)

        def poll(self, registration_id: str) -> PollSnapshot:
            if hung_operation == "poll":
                self._hang()
            return super().poll(registration_id)

        def internal_interrupt(self, registration_id: str, request_sha256: str) -> None:
            if hung_operation == "internal_interrupt":
                self._hang()
            super().internal_interrupt(registration_id, request_sha256)

    callbacks = HungCallbacks(imminent_projection(inspector))
    read_fd, write_fd = os.pipe()
    started = time.monotonic()
    try:
        report = Guardian(
            callbacks,
            inspector=inspector,
            host_callback_timeout=10.0,
            poll_interval=0.01,
        ).run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **guardian_kwargs(read_fd),
        )
        elapsed = time.monotonic() - started
        assert elapsed < 1.0
        assert report.state == "watchdog_forced"
        assert report.reason == "absolute_hard_stop"
        assert report.killed_pgids
    finally:
        release_hung_callback.set()
        time.sleep(0.01)
        os.close(read_fd)
        os.close(write_fd)


def test_poll_snapshot_digest_makes_identical_replay_deterministic() -> None:
    identity = ProcessIdentity(101, 501, 101, "start")
    first = PollSnapshot(7, "reg-1", "f" * 64, "boot-1", (PaidGroup("root", identity),))
    replay = replace(first)
    changed = replace(first, paid_groups=())

    assert first.snapshot_sha256 == replay.snapshot_sha256
    assert first.snapshot_sha256 != changed.snapshot_sha256
