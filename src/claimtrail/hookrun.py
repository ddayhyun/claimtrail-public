"""Stop 훅 한 번의 실행을 조립한다.

shell 은 stdin 을 넘기고 종료 코드를 되돌려 주는 얇은 래퍼로 남고, 판단은
전부 여기서 한다. 그래야 pytest 로 검증할 수 있다.

여기서 지키는 것
    1. 못 잰 것을 잰 것처럼 만들지 않는다. digest 가 없으면 이유를 남긴다.
    2. 알림을 멈추는 것과 판정을 바꾸는 것은 다른 일이다.
       종료 0 은 '이번엔 막지 않는다'이지 '통과했다'가 아니다.
    3. 상태를 확정하지 못했으면 최신 증빙도 공개하지 않는다.
    4. 배경 작업이 도는 중이면 검증하지 않고 연기한다. 돌리고 강등하지 않는다.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .cli import execute
from .detect import detect_all
from .hookcontext import execution_context
from .hooklock import LockUnavailable, RunLock, backend_name
from .hookscan import Fingerprint, fingerprint, parse_watch, resolve_root
from .hookstate import (
    RUNNING,
    STATE_INVALID,
    STATE_UNREADABLE,
    Finalized,
    HookState,
    SkipDecision,
    apply_notification,
    archive_run,
    begin_run,
    build_state,
    can_skip,
    deferred,
    degrade,
    failure_signature,
    finalize,
    invalidate_latest,
    load_state,
    load_state_result,
    new_run_id,
    normalize_reason,
    publish_latest,
    restore_latest,
    run_paths,
    save_state,
    save_state_cas,
    should_notify,
    state_dir_for,
    validate_evidence,
)
from .report import build_json, build_markdown, overall_verdict
from .runners.base import PASS, RUN_TIMEOUT, UNVERIFIED

# 훅 전체가 쓸 수 있는 시간. Stop 훅 설정의 timeout 보다 짧아야 한다.
DEFAULT_BUDGET_SEC = 240
# 검증에 최소한 남겨 둘 시간. busy wait 이 이만큼은 남기고 멈춘다.
MIN_RUN_RESERVE_SEC = 90
# busy wait 상한. 관측된 최장 검증(57초)보다 길게 잡되 예산을 넘지 않는다.
BUSY_WAIT_CAP_SEC = 90
BUSY_POLL_SEC = 0.5

LOG_FILE = "hook.log"

# 로그에 남기는 단계. 순서대로 적는다. total 은 훅 내부 시간이라
# hook_internal_total 로 부른다 -- 래퍼와 Python 기동은 밖이다.
TIMED_STAGES = (
    "lock",
    "fp_before",
    "detect",
    "context",
    "execute",
    "evidence",
    "fp_after",
    "archive",
    "state_publish",
)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _int_env(env: dict[str, str], key: str, default: int) -> int:
    try:
        return max(1, int(env[key]))
    except (KeyError, ValueError):
        return default


@dataclass
class Outcome:
    """한 번의 훅 실행이 남긴 것. 테스트가 읽는 창구이기도 하다."""

    exit_code: int
    action: str
    reason_code: str = ""
    detail: str = ""
    notified: bool = False
    published: bool = False
    run_id: str = ""
    # 단계별 벽시계(초). 로그의 t= 항목과 같은 값이다. 테스트가 정확히 읽는다.
    timings: dict[str, float] = field(default_factory=dict)


class _Runner:
    """한 번의 실행. 상태를 들고 다녀야 해서 클래스로 둔다."""

    def __init__(
        self,
        stdin_text: str,
        env: dict[str, str] | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        from .hookstate import parse_stop_input

        self.env = dict(os.environ if env is None else env)
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.stop = parse_stop_input(stdin_text)
        self.budget = _int_env(self.env, "CLAIMTRAIL_HOOK_BUDGET", DEFAULT_BUDGET_SEC)
        self.reserve = _int_env(self.env, "CLAIMTRAIL_MIN_RUN_RESERVE", MIN_RUN_RESERVE_SEC)
        self.busy_cap = _int_env(self.env, "CLAIMTRAIL_BUSY_WAIT_CAP", BUSY_WAIT_CAP_SEC)
        self.deadline = self.clock() + self.budget
        self.state_dir: Path | None = None
        self.log_lines: list[str] = []
        # 이번 실행이 본 환경 지문. 상태에 함께 남긴다.
        self.context_digest = ""
        # 단계별 벽시계. canary 에서 검증기 밖의 시간이 어디로 갔는지 알 수 없었다.
        self.t0 = self.clock()
        self.timings: dict[str, float] = {}

    # --- 시간 -----------------------------------------------------------

    def _timed(self, stage: str, fn, *args, **kwargs):
        """fn 을 돌리고 걸린 시간을 stage 에 더한다. 같은 stage 는 누적한다."""
        started = self.clock()
        try:
            return fn(*args, **kwargs)
        finally:
            self.timings[stage] = round(
                self.timings.get(stage, 0.0) + (self.clock() - started), 3
            )

    def _timing_note(self) -> str:
        """로그 한 줄에 붙일 시간 요약. total 과 other 를 여기서 확정한다."""
        total = round(self.clock() - self.t0, 3)
        known = sum(self.timings.get(k, 0.0) for k in TIMED_STAGES)
        self.timings["hook_internal_total"] = total
        self.timings["other"] = round(total - known, 3)
        parts = [f"hook_internal_total:{total:.3f}"]
        parts += [f"{k}:{self.timings.get(k, 0.0):.3f}" for k in TIMED_STAGES]
        parts.append(f"other:{self.timings['other']:.3f}")
        return "t=" + " ".join(parts)

    # --- 로그 -----------------------------------------------------------

    def log(self, action: str, code: str, detail: str = "") -> str:
        session = (self.stop.session_id or "-")[:8]
        line = "\t".join(
            (_now(), session, action, code, f"{self.budget}", detail.replace("\t", " "))
        )
        self.log_lines.append(line)
        if self.state_dir is not None:
            try:
                with open(self.state_dir / LOG_FILE, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass  # 로그를 못 써도 판정은 남긴다
        return line

    # --- 종료 코드 -------------------------------------------------------

    def fail_exit(self, notified: bool) -> int:
        """실패·미검증의 종료 코드.

        최초 호출에서 훅이 제 일을 못 한 것은 '검증하지 못했다'이므로 알린다.
        재호출에서 같은 실패를 또 알리면 무한 루프가 된다.
        """
        if not self.stop.stop_hook_active:
            return 2
        return 2 if notified else 0

    def unrecorded_exit(self) -> int:
        """알림 이력을 남길 수 없는 오류의 종료 코드.

        signature 를 저장하지 못하므로 '같은 실패인가'로 루프를 끊을 수 없다.
        그래서 최초 호출에서만 알리고 재호출에서는 끊는다. 종료 0 이어도
        통과가 아니다 -- 기록할 수 있는 것은 로그뿐이다.
        """
        return 0 if self.stop.stop_hook_active else 2

    # 예전 이름. 훅 내부 오류도 같은 규칙을 쓴다.
    emergency_exit = unrecorded_exit

    # --- 상태 기록 -------------------------------------------------------

    def record(
        self,
        run_id: str,
        before: Fingerprint,
        after: Fingerprint,
        final: Finalized,
        policy_hash: str,
        archived: Path | None,
        base: HookState | None,
        notify_previous: HookState | None,
        cas: bool,
    ) -> tuple[bool, bool]:
        """판정을 확정하고 알림 여부를 정한다. (저장 성공, 알림함) 을 돌려준다.

        base 와 notify_previous 를 나눈다. base 는 이번 실행이 남긴 running
        상태여서 started_at·lock_backend 를 들고 있고, notify_previous 는
        직전 실행의 알림 이력이다. 하나로 쓰면 둘 중 하나가 사라진다.

        알림 결정은 저장 전에 끝낸다 -- 저장이 한 번에 끝나야 늦게 깨어난
        실행이 끼어들 틈이 줄어든다.
        """
        state = build_state(
            run_id,
            before,
            after,
            final,
            policy_hash,
            archived,
            base,
            execution_context=self.context_digest,
            session_id=self.stop.session_id,
        )
        notified = False

        if not final.is_deferred and final.verdict != PASS:
            signature = failure_signature(
                state.input_fingerprint,
                final.verdict,
                final.reason_code,
                normalize_reason(final.before_reason),
                normalize_reason(final.after_reason),
                final.evidence_code,
            )
            decision = should_notify(
                notify_previous, signature, self.stop.session_id, self.stop.stop_hook_active
            )
            state = apply_notification(state, decision, signature, self.stop.session_id)
            notified = decision.notify

        assert self.state_dir is not None
        try:
            saved = (
                save_state_cas(self.state_dir, state, run_id)
                if cas
                else bool(save_state(self.state_dir, state))
            )
        except OSError:
            return False, notified
        return saved, notified

    # --- 잠금 -------------------------------------------------------------

    def acquire(self, lock: RunLock, run_id: str) -> bool:
        """예산 안에서 기다렸다 잡는다.

        즉시 포기하면 다른 실행이 남긴 실패를 이 세션이 듣지 못한다.
        다만 기다리다 정작 검증할 시간이 없어지면 안 되므로, 검증에 남겨야
        할 몫(reserve)을 빼고 남는 만큼만 기다린다.
        """
        if lock.acquire(run_id, self.stop.session_id):
            return True
        wait = min(float(self.busy_cap), self.deadline - self.clock() - self.reserve)
        if wait <= 0:
            return False
        until = self.clock() + wait
        while self.clock() < until:
            self.sleep(BUSY_POLL_SEC)
            if lock.acquire(run_id, self.stop.session_id):
                return True
        return False

    # --- 증빙 -------------------------------------------------------------

    def _invalidate(self, run_id: str) -> None:
        """최신본을 무효화한다. 디스크 문제로 실패하면 기록하고 계속 간다.

        무효화는 실행 중에 죽었을 때 옛 PASS 가 현재처럼 보이는 것을 막는
        방어선이다. 그 파일을 쓸 수 없는 디스크라면 뒤의 공개도 같은 이유로
        실패해 publish_failed 로 드러난다. 여기서 훅을 죽이면 그 사실조차
        남지 않는다.
        """
        assert self.state_dir is not None
        try:
            invalidate_latest(self.state_dir, run_id)
        except OSError as exc:
            self.log("invalidatefail", "invalidate_failed", f"{run_id}: {exc}")

    def crosscheck(self, path: Path, root: Path, verdict: str) -> tuple[str, str]:
        """기록한 JSON 을 다시 읽어 이번 실행과 대조한다.

        메모리의 값끼리 비교하면 동어반복이다. 디스크에 실제로 남은 것을
        확인해야 직렬화나 기록이 어긋난 경우를 잡는다.
        """
        check = validate_evidence(path)
        if not check.valid:
            return check.reason_code, check.detail
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return "invalid_json", str(exc)
        for key, expected in (
            ("tool", "claimtrail"),
            ("version", __version__),
            ("target", str(root)),
        ):
            if data.get(key) != expected:
                return f"{key}_mismatch", f"디스크={data.get(key)!r}, 이번 실행={expected!r}"
        if check.verdict != verdict:
            return (
                "evidence_verdict_mismatch",
                f"디스크={check.verdict!r}, 이번 실행={verdict!r}",
            )
        return "", ""

    # --- 본체 -------------------------------------------------------------

    def _input_problem(self) -> str:
        """입력이 쓸 만한지. 문제가 있으면 그 이유를 돌려준다."""
        if not self.stop.valid:
            return "JSON 으로 읽지 못했다"
        missing = [
            name
            for name in ("hook_event_name", "session_id", "cwd")
            if not getattr(self.stop, name)
        ]
        return f"필수 필드가 비어 있다: {', '.join(missing)}" if missing else ""

    def _context_note(self) -> str:
        """페이로드에서 읽은 맥락. 키 이름만 남기고 값은 남기지 않는다."""
        parts = []
        if self.stop.session_crons_active:
            parts.append("session_crons=1")
        if self.stop.unknown_keys:
            parts.append("unknown=" + ",".join(self.stop.unknown_keys))
        return " ".join(parts)

    def run(self) -> Outcome:
        out = self._run_inner()
        out.timings = dict(self.timings)
        return out

    def _run_inner(self) -> Outcome:  # noqa: PLR0911 - 각 분기가 서로 다른 사건이다
        problem = self._input_problem()
        if problem:
            self.log("badinput", "invalid_hook_input", problem)
            return Outcome(
                self.unrecorded_exit(), "badinput", "invalid_hook_input", problem
            )
        if self.stop.hook_event_name != "Stop":
            return Outcome(0, "skip", "not_stop_event")

        root_res = resolve_root(Path(self.stop.cwd), self.env)
        try:
            policy = parse_watch(self.env.get("CLAIMTRAIL_WATCH"))
        except ValueError as exc:
            self.log("badconfig", "bad_watch_config", str(exc))
            return Outcome(
                self.unrecorded_exit(), "badconfig", "bad_watch_config", str(exc)
            )

        base = self.env.get("CLAIMTRAIL_STATE_DIR")
        self.state_dir = state_dir_for(root_res.root, Path(base) if base else None)
        from .hookstate import ensure_state_dir

        ensure_state_dir(self.state_dir)

        if not root_res.scope_known:
            self.log("noscope", "no_scope", root_res.reason)
            return Outcome(
                self.unrecorded_exit(), "noscope", "no_scope", root_res.reason
            )

        if not backend_name():
            self.log("nolock", "lock_backend_unavailable", "fcntl 도 msvcrt 도 없다")
            return Outcome(
                self.unrecorded_exit(), "nolock", "lock_backend_unavailable"
            )

        run_id = new_run_id()
        lock = RunLock(self.state_dir)
        try:
            got = self._timed("lock", self.acquire, lock, run_id)
        except LockUnavailable as exc:
            self.log("nolock", "lock_backend_unavailable", str(exc))
            return Outcome(
                self.unrecorded_exit(), "nolock", "lock_backend_unavailable"
            )
        if not got:
            self.log("busy", "lock_busy_timeout", "다른 실행이 잠금을 보유 중이다")
            return Outcome(self.unrecorded_exit(), "busy", "lock_busy_timeout")

        try:
            return self._locked(root_res.root, policy, run_id)
        finally:
            lock.release()

    def _locked(self, root: Path, policy, run_id: str) -> Outcome:
        assert self.state_dir is not None
        policy_hash = policy.policy_hash()
        note = self._context_note()

        loaded = load_state_result(self.state_dir)
        if loaded.status == STATE_UNREADABLE:
            self.log("statebad", "state_unreadable", loaded.detail)
            return Outcome(
                self.unrecorded_exit(), "statebad", "state_unreadable", loaded.detail
            )
        previous = loaded.state
        if loaded.status == STATE_INVALID:
            self.log("statebad", "state_corrupt", loaded.detail)

        # 결론 없이 끝난 실행이 있었다는 사실 자체가 표본이다. 어느 실행이
        # 중단됐는지 남기고, 무슨 파일이 있든 반드시 다시 검증한다.
        interrupted = previous is not None and previous.phase == RUNNING
        if interrupted:
            assert previous is not None
            self.log(
                "interrupted",
                "interrupted_run",
                f"이전 실행 {previous.run_id} 이 결론 없이 끝났다",
            )

        before = self._timed("fp_before", fingerprint, root, policy)

        # 배경 작업이 돌면 검증하지 않고 연기한다. 돌리고 나서 강등하는 것과
        # 다르다 -- 지금 잰 것은 곧 달라질 상태다. 알림 예산도 쓰지 않는다.
        if self.stop.background_active:
            return self._defer(run_id, before, policy_hash, previous, note)

        # 탐지는 한 번. 환경 지문은 탐지된 검증기 기준이고, execute 도 같은
        # 탐지 결과를 받는다. 두 번 돌리면 그 사이에 답이 달라질 수 있다.
        detections = self._timed("detect", detect_all, root)
        context = self._timed("context", execution_context, root, detections)
        if not context.ok:
            # 캐시만 포기한다. 검증은 한다.
            self.log("context", "context_unavailable", context.reason)
        self.context_digest = context.digest or ""
        if before.ok and not before.cacheable:
            self.log("nocache", "uncacheable_input", ", ".join(before.uncacheable))

        if interrupted:
            decision = SkipDecision(False, "이전 실행이 중단됐다")
        elif self.env.get("CLAIMTRAIL_CACHE", "1") == "0":
            decision = SkipDecision(False, "cache_disabled")
            self.log("nocache", "cache_disabled", "CLAIMTRAIL_CACHE=0")
        else:
            decision = can_skip(
                previous,
                before,
                policy_hash,
                self.state_dir,
                execution_context=self.context_digest,
                session_id=self.stop.session_id,
            )
        if decision.skip:
            assert previous is not None
            # 디스크 문제로 복원에 실패한 것은 훅의 고장이 아니다. 예외로 새면
            # hook_internal_error 가 되어 원인이 흐려진다.
            try:
                restored = restore_latest(self.state_dir, previous.run_id)
            except OSError as exc:
                restored = None
                self.log("restorefail", "restore_failed", f"{previous.run_id}: {exc}")
            if restored is not None:
                self.log("skip", "cached_pass", f"{decision.reason} | {self._timing_note()}")
                return Outcome(0, "skip", "cached_pass", run_id=previous.run_id)
            # 건너뛸 근거를 다시 세우지 못했다. 성공으로 끝내지 않는다.
            self.log("restorefail", "restore_failed", previous.run_id)

        self._invalidate(run_id)
        begin_run(
            self.state_dir,
            run_id,
            before,
            policy_hash,
            backend_name(),
            note,
            execution_context=self.context_digest,
        )
        base = load_state(self.state_dir)

        detections, results = self._timed(
            "execute",
            execute,
            root,
            self._runner_timeout(),
            self.deadline,
            detections=detections,
        )
        verdict = overall_verdict(results)
        # note 를 파싱하지 않는다. 사람이 읽는 문구를 판단 근거로 쓰면
        # 문구를 다듬는 순간 판단이 깨진다. 러너가 남긴 원인 코드만 본다.
        timed_out = any(
            r.status == UNVERIFIED and r.reason_code == RUN_TIMEOUT for r in results
        )

        paths = run_paths(self.state_dir, run_id)

        def write_evidence() -> tuple[str, str]:
            payload = json.dumps(
                build_json(root, detections, results), ensure_ascii=False, indent=2
            )
            paths["tmp_json"].write_text(payload, encoding="utf-8")
            paths["tmp_md"].write_text(
                build_markdown(root, detections, results), encoding="utf-8"
            )
            return self.crosscheck(paths["tmp_json"], root, verdict)

        code, detail = self._timed("evidence", write_evidence)
        after = self._timed("fp_after", fingerprint, root, policy)
        final = finalize(before, after, verdict, code, detail)

        if timed_out and final.reason_code == "ok":
            # 예산이 끝나 일부를 돌리지 못했다. 그 사실이 판정 코드에 남아야
            # state·로그·증빙·signature 어디서든 같은 이름으로 보인다.
            final = degrade(final, "run_timeout", "전체 검증 예산이 끝나 일부를 실행하지 못했다")

        # 디스크 문제로 보관에 실패한 것은 훅의 고장이 아니라 검증 불가다.
        # 예외로 새어 나가면 hook_internal_error 가 되어 원인이 흐려진다.
        try:
            archived = self._timed("archive", archive_run, self.state_dir, run_id)
            archive_detail = "유효한 증빙을 보관하지 못했다"
        except OSError as exc:
            archived, archive_detail = None, f"보관 중 I/O 오류 — {exc}"
        if archived is None:
            # 증빙을 남기지 못한 판정은 주장이지 증빙이 아니다.
            final = degrade(final, "archive_failed", archive_detail)

        saved, notified = self._timed(
            "state_publish",
            self.record,
            run_id, before, after, final, policy_hash, archived, base, previous,
            cas=True,
        )
        if not saved:
            self.log("casfail", "state_write_conflict", final.reason_code)
            return Outcome(
                self.unrecorded_exit(), "casfail", "state_write_conflict", run_id=run_id
            )

        try:
            published = (
                self._timed(
                    "state_publish", publish_latest, self.state_dir, run_id, final, archived
                )
                is not None
            )
        except OSError as exc:
            self.log("publishfail", "publish_failed", str(exc))
            published = False
        self.log(
            "run",
            final.reason_code,
            f"{final.verdict} | before={final.before_reason} "
            f"after={final.after_reason} evidence={final.evidence_code} "
            f"| {self._timing_note()}",
        )

        if not published and final.reason_code == "ok":
            # 통과했는데 사람이 읽을 것을 세우지 못했다. 판정은 저장됐지만 이
            # 실패의 signature 는 기록되지 않으므로, 기록 불가 오류의 규칙을
            # 따른다 -- 최초 2, 재호출 0. 재호출마다 2 면 무한 루프다.
            self.log("publishfail", "publish_failed", run_id)
            return Outcome(self.unrecorded_exit(), "run", "publish_failed", run_id=run_id)

        if final.verdict == PASS:
            if not published:
                # 위와 같은 규칙. 성공으로 끝내지 않되 루프도 만들지 않는다.
                self.log("publishfail", "publish_failed", run_id)
                return Outcome(
                    self.unrecorded_exit(), "run", "publish_failed", run_id=run_id
                )
            return Outcome(0, "run", final.reason_code, published=True, run_id=run_id)

        return Outcome(
            self.fail_exit(notified),
            "run",
            final.reason_code,
            notified=notified,
            published=published,
            run_id=run_id,
        )

    def _defer(
        self,
        run_id: str,
        before: Fingerprint,
        policy_hash: str,
        previous: HookState | None,
        note: str,
    ) -> Outcome:
        """검증하지 않고 연기한다.

        최신본을 먼저 무효화한다 -- 이전 PASS 가 남아 있으면 지금 코드의
        판정처럼 읽힌다. 그다음 상태를 저장하고, 저장 실패를 무시하지 않는다.
        """
        assert self.state_dir is not None
        self._invalidate(run_id)
        final = deferred("background_tasks_active", "배경 작업이 실행 중이다")
        base = replace(
            previous or HookState(),
            started_at=_now(),
            lock_backend=backend_name(),
            context_note=note,
        )
        saved, _ = self.record(
            run_id, before, before, final, policy_hash, None, base, previous, cas=False
        )
        if not saved:
            self.log("statefail", "state_write_failed", "연기 상태를 저장하지 못했다")
            return Outcome(
                self.unrecorded_exit(), "statefail", "state_write_failed", run_id=run_id
            )
        try:
            publish_latest(self.state_dir, run_id, final, None)
        except OSError as exc:
            self.log("publishfail", "publish_failed", f"{run_id}: {exc}")
        self.log("defer", "background_tasks_active", "검증하지 않았다")
        return Outcome(0, "defer", "background_tasks_active", run_id=run_id)

    def _runner_timeout(self) -> int:
        from .runners.base import DEFAULT_TIMEOUT

        return _int_env(self.env, "CLAIMTRAIL_RUN_TIMEOUT", DEFAULT_TIMEOUT)


def run(
    stdin_text: str,
    env: dict[str, str] | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Outcome:
    """훅 한 번. 예외가 나도 종료 코드로 바꿔 돌려준다."""
    runner = _Runner(stdin_text, env, clock, sleep)
    try:
        return runner.run()
    except Exception as exc:  # noqa: BLE001 - 훅이 예외로 죽으면 아무것도 못 남긴다
        runner.log("error", "hook_internal_error", f"{type(exc).__name__}: {exc}")
        return Outcome(runner.emergency_exit(), "error", "hook_internal_error", str(exc))


def main(argv: list[str] | None = None) -> int:
    import sys

    from .cli import utf8_stdio

    utf8_stdio()
    outcome = run(sys.stdin.read())
    if outcome.exit_code != 0:
        print(
            f"claimtrail 훅: {outcome.action} / {outcome.reason_code}. "
            f"{outcome.detail}",
            file=sys.stderr,
        )
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
