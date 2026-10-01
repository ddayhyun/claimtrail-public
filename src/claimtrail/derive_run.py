"""도출 항목을 실제 실행 증거에 연결하고, 생성 검사를 대상 밖에서 따로 실행한다.

항목의 상태는 증거 파일(claimtrail.evidence 플러그인 출력)에서만 나온다. 증거가 없거나
불완전하면 확인 불가로 남기고, 세션의 설명이나 개수 비교로 채우지 않는다. 연결은
가림 전 원본 식별자로 한다 -- 가림이 식별자를 바꾸면 연결이 어긋난다.

생성 검사는 첫 구현에서 독립 단위 검사로 제한한다. rootdir 는 생성 파일 폴더이고, 대상
루트는 import 경로(PYTHONPATH)와 설정 파일(-c)로만 쓴다. 대상 conftest 의 fixture 는
보이지 않는다(생성 파일이 대상 밖에 있으므로). fixture 가 필요한 항목은 `needs_fixture` 로
실행하지 않고 남긴다.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from .derive import (
    LINK_DESELECTED,
    LINK_FAILED,
    LINK_INCOMPLETE,
    LINK_NO_EVIDENCE,
    LINK_NOT_COLLECTED,
    LINK_NOT_RUN,
    LINK_PASSED,
    LINK_SKIPPED,
    LINK_UNLINKED,
    PERFORMED,
    DeriveStatus,
    _redacted_item,
)
from .runners.base import UNVERIFIED, RunResult
from .runners.pytest_runner import run_pytest

GENERATED_KIND = "derived-tests"

# 나쁜 쪽이 앞. 한 항목에 식별자가 여럿이면 가장 나쁜 상태를 따른다.
_RANK = (
    LINK_FAILED,
    LINK_NOT_COLLECTED,
    LINK_NO_EVIDENCE,
    LINK_INCOMPLETE,
    LINK_DESELECTED,
    LINK_SKIPPED,
    LINK_NOT_RUN,
    LINK_PASSED,
)

# 대상 pytest 설정 파일. 첫 번째로 있는 것을 `-c` 로 넘겨 pythonpath·addopts 를 살린다.
_CONFIG_FILES = ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")


def load_evidence(path: Path) -> dict | None:
    """증거 JSON. 없거나 읽지 못하면 None -- 그때 항목은 '증거 없음'이다."""
    try:
        data = json.loads(Path(path).read_bytes().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("reports"), dict) else None


def generated_nodeid_matches(nodeid: str, ref: str) -> bool:
    """생성 파일의 nodeid 는 rootdir 밖 경로라 접두가 환경마다 다르다. 파일명::테스트 로 맞춘다."""
    norm = nodeid.replace("\\", "/")
    ref = ref.replace("\\", "/")
    return norm == ref or norm.endswith("/" + ref)


def find_generated(evidence: dict, ref: str) -> str | None:
    """`file.py::test` 참조에 맞는 nodeid. 먼저 플러그인이 기록한 실제 파일 경로로 찾고
    (rootdir 밖 파일은 nodeid 에 경로가 없다), 없으면 nodeid 접미로 찾는다."""
    file_part, _, test_part = ref.replace("\\", "/").partition("::")
    paths = evidence.get("paths") or {}
    for nodeid, path in paths.items():
        name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
        if name == file_part and str(nodeid).partition("::")[2] == test_part:
            return str(nodeid)
    for key in ("reports", "deselected", "collected"):
        pool = evidence.get(key) or {}
        for nodeid in pool:
            if generated_nodeid_matches(str(nodeid), ref):
                return str(nodeid)
    return None


def _nodeid_status(nodeid: str, evidence: dict | None) -> tuple[str, str]:
    if evidence is None:
        return LINK_NO_EVIDENCE, "증거 파일이 없거나 읽지 못함"
    if nodeid in (evidence.get("deselected") or []):
        return LINK_DESELECTED, "선택 제외됨(-k/--deselect/PYTEST_ADDOPTS 등)"
    report = (evidence.get("reports") or {}).get(nodeid)
    if report is None:
        if nodeid in (evidence.get("collected") or []):
            return LINK_INCOMPLETE, "수집됐지만 결과가 없음(중단·미완료)"
        return LINK_NOT_COLLECTED, "이번 실행의 수집 목록에 없음"
    # 실패가 먼저다. 단계 순서대로 보다가 본문 skipped 에서 멈추면 정리(teardown) 실패가
    # 가려진다 -- PR #4 검토에서 실측된 결함. 세 단계를 다 본 뒤에 skipped 를 판정한다.
    phases = [(ph, report.get(ph)) for ph in ("setup", "call", "teardown")]
    for phase, p in phases:
        if p and p.get("outcome") == "failed":
            return LINK_FAILED, f"{phase} 실패: {p.get('cause', '')}".rstrip(": ")
    for phase, p in phases:
        if p and p.get("outcome") == "skipped":
            tag = "xfail" if p.get("wasxfail") else "skipped"
            return LINK_SKIPPED, f"{phase} {tag}: {p.get('reason', '')}".rstrip(": ")
    # 통과는 준비·본문·정리 세 단계가 모두 기록되고 통과했을 때만이다. call 만 통과한 채
    # teardown 기록이 없으면 정리 중 중단된 것일 수 있다. 개별 테스트 완료와 실행 전체
    # 완료도 다르다 -- 세션이 정상 종료되지 않은 증거의 통과는 완료로 세지 않는다.
    missing = [
        ph
        for ph in ("setup", "call", "teardown")
        if (report.get(ph) or {}).get("outcome") != "passed"
    ]
    if missing:
        return LINK_INCOMPLETE, f"{'·'.join(missing)} 결과가 없음(중단·미완료 가능)"
    if not evidence.get("session_finished"):
        return LINK_INCOMPLETE, "실행 세션이 정상 종료되지 않음(테스트 단계는 통과)"
    return LINK_PASSED, ""


def verified_evidence(evidence: dict | None, expected_invocation_id: str) -> dict | None:
    """기대한 실행 ID 의 증거만 쓴다. 다른 실행의 파일이 같은 자리에 남아 있어도 근거로
    삼지 않는다. 기대값이 없으면(단위 테스트·수동 호출) 대조하지 않는다."""
    if evidence is None:
        return None
    if expected_invocation_id and evidence.get("invocation_id") != expected_invocation_id:
        return None
    return evidence


def _worst(statuses: list[str]) -> str:
    for code in _RANK:
        if code in statuses:
            return code
    return LINK_INCOMPLETE


def link_items(
    raw_items: list[dict],
    existing: dict | None,
    generated: dict | None,
    generated_dir: Path | None,
) -> list[dict]:
    """항목마다 {id, link_status, link_detail, nodeids} 를 돌려준다. 입력은 가림 전 원본."""
    linked: list[dict] = []
    for item in raw_items:
        # 값을 변수에 담아야 타입이 좁혀진다. 사전이 아닌 how 는 확인 방법이 없는 항목으로
        # 읽는다(아래 else 분기: 실행 안 함) -- 이 동작은 그대로다.
        how_value = item.get("how")
        how: dict = how_value if isinstance(how_value, dict) else {}
        kind = str(item.get("kind") or "")
        nodeids: list[dict] = []
        if "existing" in how:
            for nodeid in how["existing"] or []:
                status, detail = _nodeid_status(str(nodeid), existing)
                nodeids.append(
                    {"nodeid": str(nodeid), "run": "existing", "status": status, "detail": detail}
                )
        elif "generated" in how:
            ref = str(how["generated"])
            matched = find_generated(generated, ref) if generated else None
            if generated is None:
                status, detail = LINK_NO_EVIDENCE, "생성 검사 실행 증거가 없음"
            elif matched is None:
                status, detail = LINK_NOT_COLLECTED, "생성 검사 실행에서 수집되지 않음"
            else:
                status, detail = _nodeid_status(matched, generated)
            nodeids.append(
                {"nodeid": matched or ref, "run": "generated", "status": status, "detail": detail}
            )
        else:
            reason = str(how.get("none") or "")
            if kind == "question":
                detail = f"질문 — {reason}"
            elif kind == "needs_fixture":
                detail = f"fixture 필요(첫 구현 미지원) — {reason}"
            else:
                detail = f"실행 안 함 — {reason}"
            nodeids.append({"nodeid": "", "run": "", "status": LINK_NOT_RUN, "detail": detail})
        status = _worst([n["status"] for n in nodeids])
        detail = "; ".join(n["detail"] for n in nodeids if n["status"] == status and n["detail"])
        linked.append(
            {"id": item.get("id"), "link_status": status, "link_detail": detail, "nodeids": nodeids}
        )
    return linked


def summarize_links(linked: list[dict]) -> dict:
    counts: dict = dict.fromkeys(_RANK, 0)
    for entry in linked:
        counts[entry["link_status"]] = counts.get(entry["link_status"], 0) + 1
    counts["total"] = len(linked)
    return counts


def evidence_meta(
    evidence: dict | None,
    path: Path,
    result: RunResult | None = None,
    expected_invocation_id: str = "",
) -> dict:
    """증빙에 남길 실행 메타. 증거 파일 자체는 별도 파일로 남고 여기에는 요약만 둔다."""
    meta: dict = {
        "evidence_path": str(path),
        "expected_invocation_id": expected_invocation_id,
        "invocation_match": bool(
            evidence is not None
            and (
                not expected_invocation_id
                or evidence.get("invocation_id") == expected_invocation_id
            )
        ),
    }
    if evidence is not None:
        meta.update(
            {
                "invocation_id": evidence.get("invocation_id", ""),
                "session_finished": bool(evidence.get("session_finished")),
                "collected_count": evidence.get("collected_count"),
                "selected_count": evidence.get("selected_count"),
                "deselected_count": len(evidence.get("deselected") or []),
                "collect_errors": len(evidence.get("collect_errors") or []),
                "rootdir": evidence.get("rootdir"),
            }
        )
    else:
        meta.update({"invocation_id": "", "session_finished": False})
    if result is not None:
        meta.update(
            {
                "status": result.status,
                "exit_code": result.exit_code,
                "command": list(result.command),
                "note": result.note,
                "reason_code": result.reason_code,
            }
        )
    return meta


def run_generated(
    root: Path,
    generated_dir: Path,
    evidence_path: Path,
    timeout: int,
    invocation_id: str,
) -> RunResult:
    """생성 검사를 대상 밖 폴더에서 따로 실행한다. rootdir 는 생성 폴더, 대상 루트는
    import 경로(PYTHONPATH)와 설정 파일(-c)이다."""
    files = sorted(Path(generated_dir).glob("test_*.py")) if Path(generated_dir).is_dir() else []
    if not files:
        return RunResult(
            kind=GENERATED_KIND,
            status=UNVERIFIED,
            cwd=str(root),
            note=f"생성 검사 파일이 없다: {generated_dir}",
        )
    # rootdir 는 생성 폴더다. 대상을 rootdir 로 두면 rootdir 밖 파일의 nodeid 가 `::test_x`
    # 로 경로를 잃어, 다른 파일의 같은 이름 테스트끼리 결과가 덮어써진다(실측). 대상의
    # 설정 파일(-c)과 import 경로(PYTHONPATH)는 그대로 쓴다.
    extra: list[str] = [
        "--rootdir",
        str(generated_dir),
        "--import-mode=importlib",
        "-p",
        "no:cacheprovider",
    ]
    for name in _CONFIG_FILES:
        cfg = Path(root) / name
        if cfg.is_file():
            extra += ["-c", str(cfg)]
            break
    existing = os.environ.get("PYTHONPATH", "")
    env_extra = {"PYTHONPATH": str(root) + (os.pathsep + existing if existing else "")}
    return run_pytest(
        Path(root),
        timeout=timeout,
        paths=[str(f) for f in files],
        evidence_path=evidence_path,
        invocation_id=invocation_id,
        extra_args=extra,
        env_extra=env_extra,
        kind=GENERATED_KIND,
    )


def attach_links(
    ds: DeriveStatus,
    existing: dict | None,
    existing_path: Path,
    generated: dict | None = None,
    generated_path: Path | None = None,
    generated_result: RunResult | None = None,
    expected_existing: str = "",
    expected_generated: str = "",
) -> DeriveStatus:
    """연결 결과와 실행 메타를 도출 상태에 붙인다. 표시 항목은 가림을 거친다.
    실행 ID 가 기대와 다른 증거는 연결에 쓰지 않는다(메타에는 불일치로 남긴다)."""
    runs = {"existing": evidence_meta(existing, existing_path, None, expected_existing)}
    if generated_path is not None:
        runs["generated"] = evidence_meta(
            generated, generated_path, generated_result, expected_generated
        )
    existing = verified_evidence(existing, expected_existing)
    generated = verified_evidence(generated, expected_generated)
    if ds.status != PERFORMED:
        return replace(ds, runs=runs)
    gen_dir = generated_path.parent if generated_path is not None else None
    linked = link_items(ds.raw_items, existing, generated, gen_dir)
    by_id = {entry["id"]: entry for entry in linked}
    raw_items = [dict(item, **_link_fields(by_id.get(item.get("id")))) for item in ds.raw_items]
    items = [_redacted_item(item) for item in raw_items]
    return replace(ds, raw_items=raw_items, items=items, runs=runs, summary=summarize_links(linked))


def _link_fields(entry: dict | None) -> dict:
    if entry is None:
        return {"link_status": LINK_UNLINKED, "link_detail": "", "nodeids": []}
    return {
        "link_status": entry["link_status"],
        "link_detail": entry["link_detail"],
        "nodeids": entry["nodeids"],
    }
