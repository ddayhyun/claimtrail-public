from pathlib import Path

from claimtrail.detect import Detection
from claimtrail.report import (
    OUT_OF_SCOPE,
    build_json,
    build_markdown,
    collect_not_verified,
    overall_verdict,
)
from claimtrail.runners.pytest_runner import FAIL, PASS, UNVERIFIED, Failure, RunResult


def _det(found=True):
    return Detection(
        kind="pytest",
        found=found,
        signals=["tests/ 아래 test_*.py 3개"],
        reason="" if found else "근거 없음",
    )


def test_검증불가는_통과가_아니다():
    assert overall_verdict([]) == UNVERIFIED
    r = RunResult(kind="pytest", status=UNVERIFIED, note="pytest 없음")
    assert overall_verdict([r]) == UNVERIFIED


def test_실패가_하나라도_있으면_실패():
    ok = RunResult(kind="pytest", status=PASS)
    bad = RunResult(kind="pytest", status=FAIL)
    assert overall_verdict([ok, bad]) == FAIL


def test_리포트에_확인하지_못한_것이_반드시_들어간다(tmp_path: Path):
    r = RunResult(
        kind="pytest", status=PASS, command=["pytest"], exit_code=0,
        total=3, passed=3, failed=0, errors=0, skipped=0, duration_sec=0.1,
    )
    md = build_markdown(tmp_path, [_det()], [r])
    assert "## 확인하지 못한 것" in md
    # 확인하지 못한 것이 없어도 섹션을 비워 두지 않는다. 빈 섹션은
    # '다 확인했다'로 읽히는데, 그건 이 도구가 아는 범위에서만 참이다.
    assert "이 도구가 모르는 검증은 여전히 확인되지 않았다" in md
    assert "여기 적히지 않은 것은 확인되지 않았다" in md


def test_실패_테스트가_리포트에_드러난다(tmp_path: Path):
    r = RunResult(
        kind="pytest", status=FAIL, command=["pytest"], exit_code=1,
        total=2, passed=1, failed=1, errors=0, skipped=0, duration_sec=0.2,
        failures=[Failure(test="tests/test_x.py::test_y", message="AssertionError: 3 != 4")],
    )
    md = build_markdown(tmp_path, [_det()], [r])
    assert "판정 — 실패" in md
    assert "test_y" in md
    assert "AssertionError" in md


# --- 숫자 칸 렌더링 ---------------------------------------------------------


def _row(md: str, kind: str) -> str:
    return next(line for line in md.splitlines() if line.startswith(f"| {kind} "))


def test_숫자를_못_세는_러너에_None을_찍지_않는다(tmp_path: Path):
    """개수 형식으로 표현할 수 없는 러너가 'None개 중 None 통과'로 찍히면 안 된다."""
    r = RunResult(kind="build", status=PASS, command=["build"], exit_code=0, duration_sec=1.2)
    d = Detection(kind="build", found=True, signals=["pyproject.toml: [build-system]"])
    md = build_markdown(tmp_path, [d], [r])
    row = _row(md, "build")
    assert "None" not in row
    assert "—" in row


def test_summary가_있으면_숫자_칸에_그대로_들어간다(tmp_path: Path):
    r = RunResult(
        kind="lint", status=FAIL, command=["ruff"], exit_code=1,
        failed=3, duration_sec=0.4, summary="위반 3건",
    )
    d = Detection(kind="lint", found=True, signals=["pyproject.toml: [tool.ruff]"])
    md = build_markdown(tmp_path, [d], [r])
    assert "위반 3건" in _row(md, "lint")


def test_개수_형식은_그대로_유지된다(tmp_path: Path):
    r = RunResult(
        kind="pytest", status=PASS, command=["pytest"], exit_code=0,
        total=8, passed=8, failed=0, errors=0, skipped=0, duration_sec=0.1,
    )
    md = build_markdown(tmp_path, [_det()], [r])
    assert "8개 중 8 통과" in _row(md, "pytest")


# --- 지원 범위 --------------------------------------------------------------


def test_지원하게_된_검증은_범위_밖에서_빠진다():
    """지원을 시작한 검증이 OUT_OF_SCOPE 에 남아 있으면 리포트가 거짓말을 한다."""
    for supported in ("pytest", "lint", "type-check", "build", "npm test"):
        assert supported not in OUT_OF_SCOPE


def test_lint_위반이_실패_항목에_드러난다(tmp_path: Path):
    r = RunResult(
        kind="lint", status=FAIL, command=["ruff"], exit_code=1,
        failed=1, duration_sec=0.2, summary="위반 1건",
        failures=[Failure(test="src/a.py:1:8", message="F401 `os` imported but unused")],
    )
    d = Detection(kind="lint", found=True, signals=["pyproject.toml: [tool.ruff]"])
    md = build_markdown(tmp_path, [d], [r])
    assert "판정 — 실패" in md
    assert "src/a.py:1:8" in md
    assert "F401" in md


def test_lint를_실행하지_못하면_확인하지_못한_것에_들어간다(tmp_path: Path):
    r = RunResult(kind="lint", status=UNVERIFIED, note="이 환경에 ruff가 설치되어 있지 않다.")
    d = Detection(kind="lint", found=True, signals=["pyproject.toml: [tool.ruff]"])
    md = build_markdown(tmp_path, [d], [r])
    assert "## 확인하지 못한 것" in md
    assert "ruff가 설치되어 있지 않다" in md
    assert "판정 — 검증 불가" in md


def test_확인하지_못한_것이_있으면_반드시_나열된다(tmp_path: Path):
    """탐지되지 않은 검증은 리포트에서 빠지지 않는다."""
    found = Detection(kind="pytest", found=True, signals=["tests/ 아래 test_*.py 3개"])
    missing = Detection(kind="lint", found=False, reason="ruff 또는 flake8 설정이 없음")
    r = RunResult(
        kind="pytest", status=PASS, command=["pytest"], exit_code=0,
        total=3, passed=3, failed=0, errors=0, skipped=0, duration_sec=0.1,
    )
    md = build_markdown(tmp_path, [found, missing], [r])
    assert "**lint** — ruff 또는 flake8 설정이 없음" in md


# --- Markdown 과 JSON 의 일치 (R-02) -----------------------------------------
#
# 형식이 다르다고 사실이 달라지면 그건 증빙이 아니라 광고다. JSON 은 오랫동안
# 빈 목록만 냈고 Markdown 만 사실을 말했다. 아래 테스트가 그 간극을 막는다.


def _mixed():
    """통과 1건, 실행 불가 1건, 탐지 실패 1건이 섞인 입력."""
    dets = [
        Detection(kind="pytest", found=True, signals=["tests/ 아래 test_*.py 3개"]),
        Detection(kind="lint", found=True, signals=["pyproject.toml: [tool.ruff]"]),
        Detection(kind="npm test", found=False, reason="package.json이 없음"),
    ]
    res = [
        RunResult(
            kind="pytest", status=PASS, command=["pytest"], exit_code=0,
            total=3, passed=3, failed=0, errors=0, skipped=0, duration_sec=0.1,
        ),
        RunResult(kind="lint", status=UNVERIFIED, note="이 환경에 ruff가 설치되어 있지 않다."),
    ]
    return dets, res


def _section(md: str, title: str) -> list[str]:
    """제목이 같은 절의 본문만 잘라낸다. 다른 절의 '- ' 줄에 걸리지 않게."""
    body, inside = [], False
    for line in md.splitlines():
        if line.startswith("## "):
            inside = line.strip() == title
            continue
        if inside:
            body.append(line)
    return body


def _kinds_in_markdown(md: str) -> set[str]:
    return {
        line.split("**")[1]
        for line in _section(md, "## 확인하지 못한 것")
        if line.startswith("- **")
    }


def test_JSON도_탐지되지_않은_검증을_말한다(tmp_path: Path):
    dets, res = _mixed()
    assert "npm test" in build_json(tmp_path, dets, res)["not_verified"]


def test_JSON도_실행하지_못한_검증을_말한다(tmp_path: Path):
    dets, res = _mixed()
    assert "lint" in build_json(tmp_path, dets, res)["not_verified"]


def test_통과한_검증은_확인하지_못한_것에_없다(tmp_path: Path):
    dets, res = _mixed()
    assert "pytest" not in build_json(tmp_path, dets, res)["not_verified"]


def test_Markdown과_JSON의_미검증_집합이_같다(tmp_path: Path):
    dets, res = _mixed()
    md = build_markdown(tmp_path, dets, res)
    data = build_json(tmp_path, dets, res)
    assert _kinds_in_markdown(md) == set(data["not_verified"])
    assert _kinds_in_markdown(md) == {"lint", "npm test"}


def test_not_verified는_문자열_목록으로_유지된다(tmp_path: Path):
    """기존 소비자가 기대하는 타입을 바꾸지 않는다. 바뀐 것은 값뿐이다."""
    data = build_json(tmp_path, *_mixed())
    assert isinstance(data["not_verified"], list)
    assert all(isinstance(x, str) for x in data["not_verified"])


def test_reason_code와_detail이_Markdown_의미와_일치한다(tmp_path: Path):
    dets, res = _mixed()
    md = build_markdown(tmp_path, dets, res)
    detail = build_json(tmp_path, dets, res)["not_verified_detail"]

    codes = {i["kind"]: i["reason_code"] for i in detail}
    assert codes == {"npm test": "not_detected", "lint": UNVERIFIED}

    # detail 문자열이 Markdown 에 그대로 들어 있어야 두 형식이 같은 말을 한 것이다.
    for item in detail:
        assert f"- **{item['kind']}** — {item['detail']}" in md


def test_같은_kind는_한_번만_나온다():
    """같은 검증을 두 번 적으면 '확인하지 못한 것'의 개수가 부풀려진다."""
    dets = [
        Detection(kind="npm test", found=False, reason="첫 번째 이유"),
        Detection(kind="npm test", found=False, reason="두 번째 이유"),
        Detection(kind="build", found=False, reason="build 근거 없음"),
    ]
    items = collect_not_verified(dets, [])
    assert [i.kind for i in items] == ["npm test", "build"]
    assert items[0].detail == "첫 번째 이유"


def test_출력_순서가_결정적이다():
    """같은 입력에 다른 순서가 나오면 두 증빙을 비교할 수 없다."""
    dets = [
        Detection(kind="build", found=False, reason="근거 없음"),
        Detection(kind="npm test", found=False, reason="package.json 없음"),
        Detection(kind="lint", found=False, reason="설정 없음"),
    ]
    first = collect_not_verified(dets, [])
    assert [i.kind for i in first] == ["build", "npm test", "lint"]
    for _ in range(5):
        assert collect_not_verified(dets, []) == first


def test_UNVERIFIED_판정은_JSON_verdict에도_그대로_남는다(tmp_path: Path):
    data = build_json(tmp_path, *_mixed())
    assert data["verdict"] == UNVERIFIED


# --- 전체 문자열 스냅샷 -------------------------------------------------------
#
# 골든은 변경 전(HEAD) 구현이 만든 것이다. 현재 코드로 만들면
# '현재 코드가 현재 코드와 같다'는 동어반복이 되어 아무것도 지키지 못한다.
# root 는 상대 경로 한 조각이라 플랫폼별 구분자에 영향받지 않는다.

SNAPSHOT = Path(__file__).parent / "data" / "report_snapshot.md"


def _snapshot_input():
    dets = [
        Detection(
            kind="pytest", found=True,
            signals=["pyproject.toml: [tool.pytest.ini_options]", "tests/ 아래 test_*.py 6개"],
        ),
        Detection(kind="lint", found=True, signals=["pyproject.toml: [tool.ruff]"]),
        Detection(kind="type-check", found=True, signals=["pyproject.toml: [tool.mypy]"]),
        Detection(
            kind="build", found=True,
            signals=["pyproject.toml: [build-system] (backend: hatchling.build)"],
        ),
        Detection(
            kind="npm test", found=False,
            reason="package.json이 없어 Node 프로젝트로 볼 근거가 없음",
        ),
    ]
    res = [
        RunResult(
            kind="pytest", status=PASS, command=["python", "-m", "pytest", "-q"], exit_code=0,
            total=12, passed=10, failed=0, errors=0, skipped=2, duration_sec=1.5,
        ),
        RunResult(
            kind="lint", status=FAIL, command=["python", "-m", "ruff", "check", "."], exit_code=1,
            failed=1, duration_sec=0.2, summary="위반 1건",
            failures=[Failure(test="src/a.py:1:8", message="F401 `os` imported but unused")],
        ),
        RunResult(
            kind="type-check", status=UNVERIFIED, command=["python", "-m", "mypy", "."],
            note="이 환경에 mypy가 설치되어 있지 않다. 설치 후 다시 실행해야 한다.",
        ),
        RunResult(
            kind="build", status=PASS, command=["python", "-m", "build"], exit_code=0,
            duration_sec=3.0, summary="산출물 2개 (wheel, sdist)",
        ),
    ]
    return dets, res


def test_Markdown_전체가_변경_전과_문자_단위로_같다(monkeypatch):
    import claimtrail.report as report_mod

    monkeypatch.setattr(report_mod, "_now", lambda: "2026-01-01T00:00:00+09:00")
    dets, res = _snapshot_input()
    md = build_markdown(Path("fixture-root"), dets, res)
    assert md == SNAPSHOT.read_text(encoding="utf-8")


def test_스냅샷_입력에서도_두_형식이_같은_말을_한다(monkeypatch):
    import claimtrail.report as report_mod

    monkeypatch.setattr(report_mod, "_now", lambda: "2026-01-01T00:00:00+09:00")
    dets, res = _snapshot_input()
    md = build_markdown(Path("fixture-root"), dets, res)
    data = build_json(Path("fixture-root"), dets, res)
    assert _kinds_in_markdown(md) == set(data["not_verified"])
    assert set(data["not_verified"]) == {"type-check", "npm test"}
