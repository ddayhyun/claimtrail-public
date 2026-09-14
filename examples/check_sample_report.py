"""샘플 리포트가 설계된 결과와 정확히 일치하는지 확인한다.

사용법: python check_sample_report.py <report.json> <claimtrail 종료 코드>

의도된 실패(형식 검사 1파일)만 허용한다. 예상 밖의 실패·검증 불가·다른 파일의 형식
차이는 모두 오류다 -- "실패가 나야 하니까 실패면 통과" 로 얼버무리지 않는다.
표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import importlib.util
import json
import platform
import sys
from pathlib import Path

EXPECTED_EXIT = 1


def _same_path(a: object, b: str) -> bool:
    """대소문자·구분자·심볼릭 링크 차이를 무시한 경로 비교. 둘 다 있을 때만 참."""
    if not isinstance(a, str) or not a or not b:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


EXPECTED_FORMAT_FAILURES = {"sample_calc/calc.py"}
EXPECTED_NOT_VERIFIED = {"type-check", "build", "npm test"}
EXPECTED_TESTS = 5


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("사용법: check_sample_report.py <report.json> <exit code>", file=sys.stderr)
        return 2
    report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    exit_code = int(argv[2])
    problems: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        if not cond:
            problems.append(msg)

    expect(exit_code == EXPECTED_EXIT, f"종료 코드 {exit_code} (기대 {EXPECTED_EXIT})")
    expect(report.get("verdict") == "fail", f"verdict={report.get('verdict')} (기대 fail)")

    results = {r["kind"]: r for r in report.get("results", [])}
    expect(set(results) == {"pytest", "lint", "format"}, f"실행된 검사 {sorted(results)}")

    py = results.get("pytest", {})
    expect(py.get("status") == "pass", f"pytest status={py.get('status')}")
    expect(
        py.get("total") == EXPECTED_TESTS, f"pytest total={py.get('total')} (기대 {EXPECTED_TESTS})"
    )
    expect(py.get("passed") == EXPECTED_TESTS, f"pytest passed={py.get('passed')}")
    expect(py.get("tests_ran") == EXPECTED_TESTS, f"pytest tests_ran={py.get('tests_ran')}")

    lint = results.get("lint", {})
    expect(
        lint.get("status") == "pass", f"lint status={lint.get('status')} note={lint.get('note')}"
    )

    fmt = results.get("format", {})
    expect(fmt.get("status") == "fail", f"format status={fmt.get('status')}")
    fmt_files = {f["test"].replace("\\", "/") for f in fmt.get("failures", [])}
    expect(
        fmt_files == EXPECTED_FORMAT_FAILURES,
        f"format 실패 파일 {sorted(fmt_files)} (기대 {sorted(EXPECTED_FORMAT_FAILURES)})",
    )

    scope = report.get("scope") or {}
    expect(
        scope.get("required_status") == {"pytest": "pass", "lint": "pass", "format": "fail"},
        f"required_status={scope.get('required_status')}",
    )
    expect(
        set(report.get("not_verified", [])) == EXPECTED_NOT_VERIFIED,
        f"not_verified={report.get('not_verified')}",
    )

    # 실행 환경은 이 스크립트를 리포트를 만든 그 Python 으로 돌린다는 전제에서 대조한다
    # (sample.yml 이 그렇게 한다). 다른 Python 으로 돌리면 아래 세 항목은 당연히 어긋난다.
    env = report.get("environment") or {}
    expect(bool(env.get("python_executable")), "environment.python_executable 비어 있음")
    expect(
        _same_path(env.get("python_executable"), sys.executable),
        f"python_executable={env.get('python_executable')} (이 인터프리터 {sys.executable})",
    )
    expect(
        env.get("python_version") == platform.python_version(),
        f"python_version={env.get('python_version')} (이 인터프리터 {platform.python_version()})",
    )
    spec = importlib.util.find_spec("claimtrail")
    installed = str(Path(spec.origin).resolve().parent) if spec and spec.origin else ""
    expect(
        _same_path(env.get("claimtrail_source"), installed),
        f"claimtrail_source={env.get('claimtrail_source')} (이 인터프리터의 설치 위치 {installed})",
    )
    tools = env.get("tools") or {}
    expect(
        tools.get("pytest") not in (None, "미설치", "조회 실패", "확인하지 않음"),
        f"tools.pytest={tools.get('pytest')}",
    )
    expect(
        tools.get("ruff") not in (None, "미설치", "조회 실패", "확인하지 않음"),
        f"tools.ruff={tools.get('ruff')}",
    )

    if problems:
        print("샘플 리포트가 설계와 다르다:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(
        f"OK: exit {exit_code}, verdict fail, pytest {py['passed']}/{py['total']}, lint pass, "
        f"format fail {sorted(fmt_files)}, not_verified {sorted(EXPECTED_NOT_VERIFIED)}, "
        f"python {env.get('python_version')} pytest {tools.get('pytest')} ruff {tools.get('ruff')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
