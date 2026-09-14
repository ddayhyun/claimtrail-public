"""claimtrail 명령줄 진입점."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import __version__
from .config import CONFIG_FILENAME, Config, ConfigError, find_config
from .detect import Detection, detect_all
from .environment import collect_environment
from .report import EXIT_CODE, build_json, build_markdown, overall_verdict
from .runners.base import DEFAULT_TIMEOUT, RUN_TIMEOUT, UNVERIFIED, RunResult
from .runners.build_runner import run_build
from .runners.format_runner import run_format
from .runners.lint_runner import run_lint
from .runners.npm_runner import run_npm_test
from .runners.pytest_runner import run_pytest
from .runners.typecheck_runner import run_typecheck

# 탐지 종류 -> 실행기. 러너를 늘릴 때 _cmd_run 을 고치지 않고 여기만 늘린다.
# 값 타입을 적어 둔다. 적지 않으면 mypy 가 러너 시그니처의 교집합을 추론하는데,
# Python 3.9 의 mypy 는 이를 "unknown type" 으로 보고 아래 호출을 거부한다(CI 에서 발견).
RUNNERS: dict[str, Callable[..., RunResult]] = {
    "pytest": run_pytest,
    "lint": run_lint,
    "format": run_format,
    "type-check": run_typecheck,
    "build": run_build,
    "npm test": run_npm_test,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claimtrail",
        description=(
            "에이전트가 '완료했습니다'라고 말할 때, 그 주장을 실제로 검사하고 증빙을 남긴다."
        ),
    )
    parser.add_argument("--version", action="version", version=f"claimtrail {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    config_help = (
        f"검증 범위 설정 파일({CONFIG_FILENAME}). 생략하면 <대상>/{CONFIG_FILENAME} 이 "
        "있을 때만 읽는다. 없으면 자동 탐지만 한다."
    )

    detect_cmd = sub.add_parser("detect", help="무엇을 검증할 수 있는지만 찾아본다 (실행 안 함)")
    detect_cmd.add_argument("path", nargs="?", default=".", help="대상 경로 (기본: 현재 폴더)")
    detect_cmd.add_argument("--config", help=config_help)

    run_cmd = sub.add_parser("run", help="탐지한 검증을 실제로 실행하고 증빙 리포트를 낸다")
    run_cmd.add_argument("path", nargs="?", default=".", help="대상 경로 (기본: 현재 폴더)")
    run_cmd.add_argument("--format", choices=["md", "json"], default="md", help="출력 형식")
    run_cmd.add_argument("-o", "--output", help="리포트를 저장할 파일 경로")
    run_cmd.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"검증 제한 시간(초, 기본 {DEFAULT_TIMEOUT})",
    )
    run_cmd.add_argument("--config", help=config_help)
    return parser


def _resolve(path_str: str) -> Path:
    root = Path(path_str).expanduser().resolve()
    if not root.is_dir():
        print(f"오류: 폴더가 아니다 — {root}", file=sys.stderr)
        raise SystemExit(2)
    return root


def _load_config(root: Path, explicit: str | None) -> Config | None:
    try:
        return find_config(root, explicit)
    except ConfigError as exc:
        # 설정을 읽지 못했으면 추측으로 돌리지 않는다. 검증 불가와 같은 코드다.
        print(f"오류: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_CODE[UNVERIFIED]) from exc


def _cmd_detect(args: argparse.Namespace) -> int:
    root = _resolve(args.path)
    config = _load_config(root, args.config)
    detections = detect_all(root, config)

    print(f"대상: {root}")
    if config is not None:
        print(f"설정: {config.source}")
        if config.required:
            print(f"  필수 검사: {', '.join(config.required)}")
        if config.pytest_paths:
            print(f"  pytest 경로: {' '.join(config.pytest_paths)}")
    print()
    for d in detections:
        print(d.summary())
        for s in d.signals:
            print(f"  - {s}")
    print("\n실행하려면: claimtrail run")
    return 0 if any(d.found for d in detections) else 2


def _run_one(kind: str, root: Path, timeout: int, config: Config | None) -> RunResult:
    """러너 하나를 설정에 맞춰 부른다.

    항상 RUNNERS 를 거친다 -- 훅 테스트는 이 표를 가짜로 바꿔 호출 횟수를 센다.
    설정이 없거나 해당 항목이 비어 있으면 호출 형태가 예전과 완전히 같다.
    """
    runner = RUNNERS[kind]
    extra: dict[str, object] = {}
    if kind == "pytest" and config is not None and config.pytest_paths:
        extra["paths"] = config.pytest_paths
    if kind == "format" and config is not None and config.format_tool:
        extra["tool"] = config.format_tool
    return runner(root, timeout=timeout, **extra)


def execute(
    root: Path,
    timeout: int,
    deadline: float | None = None,
    detections: list[Detection] | None = None,
    config: Config | None = None,
) -> tuple[list[Detection], list[RunResult]]:
    """탐지하고 실행한다. 한 번만.

    형식마다 따로 돌리면 시간이 두 배가 되고, 그 사이에 코드가 바뀌면 두
    형식이 서로 다른 사실을 말한다. 같은 결과 객체에서 렌더링해야 한다.

    deadline 은 time.monotonic() 기준 전체 예산의 끝이다. 주면 러너마다
    남은 시간으로 줄여 넘긴다 -- 러너별 timeout 만으로는 총합이 유계가
    아니라서, 러너가 늘수록 전체 시간이 함께 늘어난다.
    time.time() 을 쓰지 않는다. 시스템 시각이 바뀌면 예산이 무너진다.

    예산이 끝난 뒤 남은 검증은 실행하지 않고 UNVERIFIED 로 남긴다.
    실행하지 않은 것을 통과로 적지 않는다.

    config 는 일반 CLI 가 넘긴다. 훅은 넘기지 않는다(None) -- 훅의 탐지·실행·
    지문은 이 인자가 생기기 전과 같다.
    """
    # 호출자가 이미 탐지했으면 다시 하지 않는다. 훅은 환경 지문을 위해
    # 먼저 탐지하고, 같은 결과로 실행해야 두 답이 어긋나지 않는다.
    if detections is None:
        detections = detect_all(root, config)
    results: list[RunResult] = []
    for d in detections:
        if d.kind not in RUNNERS or not d.found:
            continue
        if deadline is None:
            results.append(_run_one(d.kind, root, timeout, config))
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append(
                RunResult(
                    kind=d.kind,
                    status=UNVERIFIED,
                    note="전체 검증 예산이 끝나 실행하지 않았다.",
                    reason_code=RUN_TIMEOUT,
                )
            )
            continue
        results.append(_run_one(d.kind, root, max(1, min(timeout, int(remaining))), config))
    return detections, results


def _cmd_run(args: argparse.Namespace) -> int:
    root = _resolve(args.path)
    config = _load_config(root, args.config)
    detections, results = execute(root, args.timeout, config=config)

    # 실행 환경은 한 번만 모아 두 형식이 같은 값을 쓴다. 수집이 실패해도 판정과
    # 종료 코드는 검증 결과에서만 나온다 -- 환경 정보는 설명이지 근거가 아니다.
    environment: dict[str, object]
    try:
        environment = collect_environment(r.kind for r in results)
    except Exception as exc:  # noqa: BLE001 - 어떤 실패든 리포트에 적고 판정은 유지한다
        environment = {"error": f"{type(exc).__name__}: {exc}"}

    if args.format == "json":
        content = json.dumps(
            build_json(root, detections, results, config, environment),
            ensure_ascii=False,
            indent=2,
        )
    else:
        content = build_markdown(root, detections, results, config, environment)

    if args.output:
        out = Path(args.output).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")
        print(f"증빙 리포트를 저장했다: {out}")
    else:
        print(content)

    required = config.required if config is not None else ()
    return EXIT_CODE[overall_verdict(results, required)]


def utf8_stdio() -> None:
    """콘솔 코드페이지가 무엇이든 UTF-8 로 읽고 쓴다.

    Claude Code 는 stdin 에 UTF-8 JSON 을 보내고 stderr 를 그대로 보여 준다.
    로케일이 cp1252 면 한글 cwd 가 깨지고, 한글 메시지를 쓰다 프로세스가
    종료 코드 1 로 죽는다 -- 판정이 아니라 고장으로 읽힌다. Windows CI 에서
    처음 드러났다. 이 저장소를 개발한 PC 는 cp949 라 보이지 않았다.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        # 이미 읽은 스트림 등은 바꿀 수 없다. 그대로 둔다.
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def main(argv=None) -> int:
    utf8_stdio()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "detect":
        return _cmd_detect(args)
    return _cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
