"""증빙 리포트를 만든다.

이 도구의 핵심은 '무엇이 통과했나'가 아니라
'무엇을 실제로 확인했고, 무엇은 확인하지 못했나'를 함께 남기는 것이다.
확인하지 못한 것을 적지 않으면 그건 증빙이 아니라 광고다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import Config
from .detect import Detection
from .environment import environment_lines
from .redact import redact
from .runners.base import FAIL, PASS, UNVERIFIED, Failure, RunResult

# 아직 지원하지 않는 검증 — 숨기지 않고 명시한다.
# 지금은 비어 있다. 이 도구가 '검증 종류'로 이름 붙인 것은 모두 실행할 수
# 있게 됐다는 뜻이지, 확인할 게 남지 않았다는 뜻이 아니다.
OUT_OF_SCOPE: dict[str, str] = {}

VERDICT_LABEL = {
    PASS: "통과",
    FAIL: "실패",
    UNVERIFIED: "검증 불가",
}

EXIT_CODE = {PASS: 0, FAIL: 1, UNVERIFIED: 2}

# 확인하지 못한 이유를 기계가 읽을 수 있게 분류한다. UNVERIFIED 는
# runners.base 의 상태값을 그대로 쓴다 -- 같은 뜻에 이름을 둘로 만들지 않는다.
NOT_DETECTED = "not_detected"
OUT_OF_SCOPE_CODE = "out_of_scope"


@dataclass(frozen=True)
class NotVerified:
    """확인하지 못한 검증 하나. 목록 안에서 kind 는 유일하다."""

    kind: str
    reason_code: str
    detail: str


def collect_not_verified(
    detections: list[Detection],
    results: list[RunResult],
) -> list[NotVerified]:
    """'확인하지 못한 것'을 한 곳에서 계산한다.

    Markdown 과 JSON 이 각자 계산하면 한쪽만 고치는 순간 갈라진다. 실제로
    갈라져 있었다 -- JSON 은 늘 빈 목록을 냈고 Markdown 만 사실을 말했다.
    두 형식이 같은 사실을 말하려면 계산도 하나여야 한다.

    순서는 detections 순서, 그다음 OUT_OF_SCOPE 순서로 결정된다. 같은 kind
    가 두 번 들어오면 먼저 온 것만 남긴다 -- 같은 검증을 두 번 적으면
    '확인하지 못한 것'의 개수가 부풀려진다.
    """
    items: list[NotVerified] = []
    seen: set[str] = set()

    def add(kind: str, reason_code: str, detail: str) -> None:
        if kind in seen:
            return
        seen.add(kind)
        items.append(NotVerified(kind=kind, reason_code=reason_code, detail=detail))

    for d in detections:
        if not d.found:
            add(d.kind, NOT_DETECTED, d.reason)
            continue
        matching = [r for r in results if r.kind == d.kind]
        if matching and matching[0].status == UNVERIFIED:
            add(d.kind, UNVERIFIED, matching[0].note)

    for kind, reason in OUT_OF_SCOPE.items():
        add(kind, OUT_OF_SCOPE_CODE, reason)

    return items


def required_status(
    results: list[RunResult], required: tuple[str, ...] | list[str]
) -> dict[str, str]:
    """필수 검사마다 실제 상태. 결과가 아예 없으면 "missing" -- 탐지되지 않았거나
    실행되지 않은 것이다. 이것을 통과로 세지 않는 것이 이 표의 목적이다."""
    status: dict[str, str] = {}
    for kind in required:
        matching = [r for r in results if r.kind == kind]
        status[kind] = matching[0].status if matching else "missing"
    return status


def overall_verdict(
    results: list[RunResult],
    required: tuple[str, ...] | list[str] = (),
) -> str:
    """검증 불가를 성공으로 취급하지 않는다. 그게 이 도구의 존재 이유다.

    required 가 있으면 규칙이 하나 더 붙는다: 필수 검사 중 하나라도 실행되지
    않았거나(missing) 검증 불가면 전체는 통과가 아니라 검증 불가다. 실패가
    있으면 그건 어느 쪽이든 실패다. required 가 비어 있으면 예전과 같다.
    """
    if any(r.status == FAIL for r in results):
        return FAIL
    if required:
        if any(s != PASS for s in required_status(results, required).values()):
            return UNVERIFIED
        return PASS
    if results and all(r.status == PASS for r in results):
        return PASS
    return UNVERIFIED


def _numbers(r: RunResult) -> str:
    """'확인한 것' 표의 숫자 칸.

    러너마다 셀 수 있는 것이 다르다. pytest 는 테스트 개수를, lint 는 위반
    건수를 센다. 개수 형식으로 표현할 수 없는 러너는 summary 를 채워 보낸다.
    아무것도 세지 못했으면 숫자를 지어내지 말고 '—' 를 적는다.
    """
    if r.summary:
        return r.summary
    if r.total is None:
        return "—"

    numbers = f"{r.total}개 중 {r.passed} 통과"
    if r.failed:
        numbers += f", {r.failed} 실패"
    if r.errors:
        numbers += f", {r.errors} 오류"
    if r.skipped:
        numbers += f", {r.skipped} 건너뜀"
    return numbers


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _clean_result(r: RunResult) -> RunResult:
    """리포트에 실리는 동적 문자열에서 자격증명을 가린 복사본.

    러너가 truncate 로 이미 가렸더라도 여기서 한 번 더 한다 -- 리포트로 들어오는
    마지막 관문이 하나여야 Markdown 과 JSON 이 같은 것을 감춘다. 명령은 이 도구가
    만든 것이라 값이 섞이지 않지만 같은 규칙을 적용한다. 숫자·상태·원인 코드는
    건드리지 않는다.
    """
    return replace(
        r,
        command=[redact(c) for c in r.command],
        failures=[Failure(test=redact(f.test), message=redact(f.message)) for f in r.failures],
        note=redact(r.note),
        summary=redact(r.summary),
    )


def _clean_detection(d: Detection) -> Detection:
    return replace(d, signals=[redact(s) for s in d.signals], reason=redact(d.reason))


def sanitize(
    detections: list[Detection], results: list[RunResult]
) -> tuple[list[Detection], list[RunResult]]:
    """두 형식이 공유하는 가림 관문. 원본 객체는 바꾸지 않는다."""
    return [_clean_detection(d) for d in detections], [_clean_result(r) for r in results]


def _scope_lines(config: Config, results: list[RunResult], verdict: str) -> list[str]:
    """설정 범위를 판정 바로 아래 적는다. 통과는 '이 범위의 통과' 다."""
    lines: list[str] = []
    lines.append(f"- 검증 범위 설정: `{config.source}`")
    if config.required:
        status = required_status(results, config.required)
        label = {PASS: "통과", FAIL: "실패", UNVERIFIED: "검증 불가", "missing": "미실행"}
        parts = [f"{k} {label.get(v, v)}" for k, v in status.items()]
        lines.append(f"- 필수 검사: {', '.join(parts)}")
        not_ok = [k for k, v in status.items() if v != PASS]
        if verdict == PASS:
            lines.append(
                "> 이 통과는 **위 설정 범위의 통과**다. 설정에 없는 검사는 확인하지 않았다."
            )
        elif verdict == UNVERIFIED and not_ok:
            lines.append(
                f"> 필수 검사 {', '.join(not_ok)} 이(가) 통과 상태가 아니다. "
                "**이것은 통과가 아니다.**"
            )
    if config.pytest_paths:
        lines.append(f"- pytest 범위: `{' '.join(config.pytest_paths)}`")
    lines.append("")
    return lines


def build_markdown(
    root: Path,
    detections: list[Detection],
    results: list[RunResult],
    config: Config | None = None,
    environment: dict[str, object] | None = None,
) -> str:
    detections, results = sanitize(detections, results)
    required = config.required if config is not None else ()
    verdict = overall_verdict(results, required)
    lines: list[str] = []

    lines.append("# 검증 증빙")
    lines.append("")
    lines.append(f"- 대상: `{root}`")
    lines.append(f"- 실행 시각: {_now()}")
    lines.append(f"- 도구: claimtrail {__version__}")
    lines.append("")
    lines.append(f"## 판정 — {VERDICT_LABEL[verdict]}")
    lines.append("")

    if config is not None:
        lines.extend(_scope_lines(config, results, verdict))

    if verdict == UNVERIFIED:
        lines.append("> 검증을 실행하지 못했다. **이것은 통과가 아니다.**")
        lines.append("")

    # --- 실행 환경 -------------------------------------------------------
    # 호출자가 수집해 넘길 때만 적는다. 넘기지 않은 리포트(훅, 예전 스냅샷)는 그대로다.
    if environment is not None:
        lines.append("## 실행 환경")
        lines.append("")
        lines.extend(environment_lines(environment))
        lines.append("")

    # --- 확인한 것 -------------------------------------------------------
    lines.append("## 확인한 것")
    lines.append("")

    ran = [r for r in results if r.status in (PASS, FAIL)]
    if not ran:
        lines.append("실제로 실행된 검증이 없다.")
        lines.append("")
    else:
        # 보고 시간은 도구가 말한 것(pytest 는 JUnit 합계), 벽시계는 프로세스가 실제로
        # 쓴 시간. pytest 에서 둘은 10초 넘게 다를 수 있다.
        lines.append("| 검증 | 결과 | 숫자 | 보고 시간 | 벽시계 |")
        lines.append("|---|---|---|---|---|")
        for r in ran:
            duration = f"{r.duration_sec}s" if r.duration_sec is not None else "-"
            wall = f"{r.wall_sec}s" if r.wall_sec is not None else "-"
            lines.append(
                f"| {r.kind} | {VERDICT_LABEL[r.status]} | {_numbers(r)} | {duration} | {wall} |"
            )
        lines.append("")

        for r in ran:
            lines.append(f"### 실행한 명령 — {r.kind}")
            lines.append("")
            lines.append("```")
            lines.append(r.command_str)
            lines.append("```")
            lines.append("")
            lines.append(f"종료 코드: `{r.exit_code}`")
            lines.append("")
            # 작업 폴더와 단계는 값이 있을 때만 적는다. 없던 시절의 리포트와
            # 문자 단위로 같아야 하는 스냅샷이 있다.
            if r.cwd:
                lines.append(f"작업 폴더: `{r.cwd}`")
                lines.append("")
            if r.phase == "collection":
                ran_label = "알 수 없음" if r.tests_ran is None else f"{r.tests_ran}개"
                lines.append(
                    f"**수집 단계에서 중단됐다.** 위 숫자는 수집 오류 항목을 센 것이고 "
                    f"테스트 실행 결과가 아니다. 실행된 것으로 확인되는 테스트: {ran_label}."
                )
                lines.append("")
            # note 는 UNVERIFIED 뿐 아니라 통과/실패에도 붙을 수 있다.
            # 예: build 는 통과했을 때 산출물 이름을 여기에 남긴다.
            if r.note:
                lines.append(r.note)
                lines.append("")

    # --- 실패 상세 -------------------------------------------------------
    failing = [r for r in ran if r.failures]
    if failing:
        lines.append("## 실패한 항목")
        lines.append("")
        for r in failing:
            for f in r.failures:
                lines.append(f"- `{f.test}`")
                if f.message:
                    lines.append(f"  - {f.message}")
        lines.append("")

    # --- 확인하지 못한 것 (핵심) ------------------------------------------
    lines.append("## 확인하지 못한 것")
    lines.append("")

    not_verified = collect_not_verified(detections, results)

    if not_verified:
        for item in not_verified:
            lines.append(f"- **{item.kind}** — {item.detail}")
    else:
        # 비어 있다고 '다 확인했다'로 읽히면 안 된다. 그것도 과장이다.
        lines.append(
            "이 도구가 아는 검증 중에는 확인하지 못한 것이 없다. "
            "이 도구가 모르는 검증은 여전히 확인되지 않았다."
        )
    lines.append("")

    # --- 탐지 근거 -------------------------------------------------------
    lines.append("## 탐지 근거")
    lines.append("")
    for d in detections:
        lines.append(f"- {d.summary()}")
        for s in d.signals:
            lines.append(f"  - {s}")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "이 리포트는 위에 적힌 명령을 **실제로 실행한** 결과다. "
        "여기 적히지 않은 것은 확인되지 않았다."
    )
    lines.append("")

    return "\n".join(lines)


def build_json(
    root: Path,
    detections: list[Detection],
    results: list[RunResult],
    config: Config | None = None,
    environment: dict[str, object] | None = None,
) -> dict:
    detections, results = sanitize(detections, results)
    not_verified = collect_not_verified(detections, results)
    required = config.required if config is not None else ()
    scope = None
    if config is not None:
        scope = {
            "source": str(config.source),
            "required": list(config.required),
            "required_status": required_status(results, config.required),
            "pytest_paths": list(config.pytest_paths),
            "format_tool": config.format_tool,
        }
    return {
        "tool": "claimtrail",
        "version": __version__,
        "target": str(root),
        "ran_at": _now(),
        "verdict": overall_verdict(results, required),
        # 설정이 없으면 None -- 예전 리포트에는 이 키가 없었고, 값의 뜻은
        # '자동 탐지 범위' 그대로다.
        "scope": scope,
        # 호출자가 수집한 실행 환경(environment.collect_environment). Markdown 과 같은
        # dict 다. 넘기지 않은 실행(훅)은 None -- 수집하지 않았다는 뜻이다.
        "environment": environment,
        "detections": [
            {
                "kind": d.kind,
                "found": d.found,
                "signals": d.signals,
                "reason": d.reason,
            }
            for d in detections
        ],
        "results": [
            {
                "kind": r.kind,
                "status": r.status,
                "command": r.command,
                "exit_code": r.exit_code,
                "total": r.total,
                "passed": r.passed,
                "failed": r.failed,
                "errors": r.errors,
                "skipped": r.skipped,
                "duration_sec": r.duration_sec,
                "wall_sec": r.wall_sec,
                "failures": [{"test": f.test, "message": f.message} for f in r.failures],
                "summary": r.summary,
                "note": r.note,
                # 아래는 추가 키다. 예전 소비자는 모르는 키를 무시한다.
                "cwd": r.cwd,
                "reason_code": r.reason_code,
                "phase": r.phase,
                "tests_ran": r.tests_ran,
            }
            for r in results
        ],
        # 기존 소비자와의 호환을 위해 kind 문자열 목록을 유지한다.
        # 달라진 것은 타입이 아니라 값이다 -- 예전에는 항상 비어 있었다.
        "not_verified": [i.kind for i in not_verified],
        "not_verified_detail": [
            {"kind": i.kind, "reason_code": i.reason_code, "detail": i.detail} for i in not_verified
        ],
    }
