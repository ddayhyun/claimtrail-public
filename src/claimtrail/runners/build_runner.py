"""패키지를 실제로 빌드하고, 나온 산출물을 센다.

출력 문자열을 파싱하지 않는다. build 가 'Successfully built ...' 라고
말한 것을 믿는 대신, 산출물 폴더를 실제로 훑어서 무엇이 나왔는지 본다.
파일 목록이 곧 근거다.

빌드는 임시 폴더(--outdir)로 낸다. 검증 도구가 대상 프로젝트에 dist/ 를
만들어 놓으면 그건 관측이 아니라 변경이다. 임시 폴더는 실행이 끝나면
사라지므로, 무엇이 나왔는지는 리포트에 이름과 크기로 남긴다.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .base import (
    DEFAULT_TIMEOUT,
    FAIL,
    PASS,
    RUN_TIMEOUT,
    UNVERIFIED,
    RunResult,
    truncate,
)
from .process import cleanup_note, run_captured

KIND = "build"

WHEEL_SUFFIX = ".whl"
SDIST_SUFFIXES = (".tar.gz", ".zip")


def _command(outdir: str) -> list[str]:
    return [sys.executable, "-m", "build", "--outdir", outdir, "."]


def _collect(outdir: Path) -> list[tuple[str, int]]:
    """산출물 폴더를 훑는다. (이름, 크기) 목록을 이름순으로 돌려준다."""
    items: list[tuple[str, int]] = []
    for path in sorted(outdir.iterdir()):
        if path.is_file():
            try:
                items.append((path.name, path.stat().st_size))
            except OSError:
                items.append((path.name, 0))
    return items


def _kinds(names: list[str]) -> list[str]:
    kinds: list[str] = []
    if any(n.endswith(WHEEL_SUFFIX) for n in names):
        kinds.append("wheel")
    if any(n.endswith(SDIST_SUFFIXES) for n in names):
        kinds.append("sdist")
    return kinds


def _summary(artifacts: list[tuple[str, int]]) -> str:
    if not artifacts:
        return "산출물 없음"
    kinds = _kinds([n for n, _ in artifacts])
    label = f" ({', '.join(kinds)})" if kinds else ""
    return f"산출물 {len(artifacts)}개{label}"


def _note(artifacts: list[tuple[str, int]]) -> str:
    if not artifacts:
        return ""
    parts = [f"`{name}` ({size:,} bytes)" for name, size in artifacts]
    return "산출물: " + ", ".join(parts)


def run_build(root: Path, timeout: int = DEFAULT_TIMEOUT) -> RunResult:
    """패키지를 빌드한다. 실행 자체가 불가능하면 UNVERIFIED 로 남긴다."""
    with tempfile.TemporaryDirectory() as tmp:
        outdir = Path(tmp)
        command = _command(str(outdir))

        started = time.monotonic()
        try:
            proc = run_captured(command, cwd=str(root), timeout=timeout)
        except FileNotFoundError:
            return RunResult(
                kind=KIND,
                status=UNVERIFIED,
                command=command,
                note="python 실행 파일을 찾지 못했다.",
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(
                kind=KIND,
                status=UNVERIFIED,
                command=command,
                note=(
                    f"{timeout}초 안에 끝나지 않아 중단했다. 검증되지 않았다. "
                    "빌드는 격리 환경을 만들면서 네트워크를 쓸 수 있다." + cleanup_note(exc)
                ),
                reason_code=RUN_TIMEOUT,
                # 얼마나 기다리다 끊었는지는 사실이다. 남긴다.
                duration_sec=(elapsed := round(time.monotonic() - started, 2)),
                wall_sec=elapsed,
            )

        wall = round(time.monotonic() - started, 2)
        combined = (proc.stdout or "") + (proc.stderr or "")
        artifacts = _collect(outdir)

    if "No module named build" in combined:
        return RunResult(
            kind=KIND,
            status=UNVERIFIED,
            command=command,
            exit_code=proc.returncode,
            duration_sec=wall,
            wall_sec=wall,
            note="이 환경에 build가 설치되어 있지 않다. `pip install build` 후 다시 실행해야 한다.",
        )

    if proc.returncode != 0:
        return RunResult(
            kind=KIND,
            status=FAIL,
            command=command,
            exit_code=proc.returncode,
            duration_sec=wall,
            wall_sec=wall,
            summary=_summary(artifacts),
            note="빌드가 실패했다. " + truncate(combined, 400),
        )

    # 종료 코드는 0인데 나온 파일이 없다 — 빌드됐다고 볼 수 없다.
    if not artifacts:
        return RunResult(
            kind=KIND,
            status=UNVERIFIED,
            command=command,
            exit_code=proc.returncode,
            duration_sec=wall,
            wall_sec=wall,
            note=(
                "빌드가 종료 코드 0으로 끝났는데 산출물이 하나도 없다. "
                "빌드됐다고 볼 수 없다. " + truncate(combined, 200)
            ),
        )

    return RunResult(
        kind=KIND,
        status=PASS,
        command=command,
        exit_code=proc.returncode,
        total=len(artifacts),
        passed=len(artifacts),
        duration_sec=wall,
        wall_sec=wall,
        summary=_summary(artifacts),
        note=_note(artifacts),
    )
