"""자동 도출 목록 — 세션이 도출한 확인 항목을 도구가 받아 증빙과 연결한다.

세션(Claude Code)이 "무엇을 확인해야 하는가"를 도출해 JSON 으로 쓰고, 이 모듈이
그 파일을 검증·제출·읽기한다. 세 가지를 지킨다.

- 목록은 주장이다. 제출 시점의 입력 지문(감시 대상 해시)과 생성 테스트 내용의
  해시를 도구가 붙인다. 세션이 적어 낸 값을 믿지 않는다.
- 훅은 현재 작업(상태 폴더·session_id·prompt_id)과 일치하는 파일만 읽는다.
  없으면 미수행, 형식이 틀리면 무효, 지문이 다르면 stale. 이전 작업의 목록으로
  대신하지 않는다.
- 이 모듈은 항목이 맞는지(도출의 정확성·충분성)를 판단하지 않는다. 접수와 형식,
  시점 일치까지만 확인한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .hookscan import WatchPolicy, fingerprint
from .redact import redact

SCHEMA_VERSION = 1
DERIVE_DIR = "derive"
RECORD_FILE = "derived_checks.json"
GENERATED_DIR = "generated"
# 활성화 표식. 훅 상태 폴더(저장소 밖)에 둔다. 있으면 "이 프로젝트는 도출을 기대한다"이고,
# 없으면 미수행을 비활성으로 적는다 -- 판정은 어느 쪽도 바꾸지 않는다.
ENABLED_FILE = "derive.enabled"

# 도출 상태. 앞 둘은 세션이 제출하는 값이고, 뒤 셋은 훅이 읽을 때 판정하는 값이다.
PERFORMED = "performed"
NOT_APPLICABLE = "not_applicable"
NOT_PERFORMED = "not_performed"
INVALID = "invalid"
STALE = "stale"
DOC_STATUSES = (PERFORMED, NOT_APPLICABLE)

STATUS_LABEL = {
    PERFORMED: "수행",
    NOT_APPLICABLE: "해당 없음",
    NOT_PERFORMED: "미수행",
    INVALID: "무효",
    STALE: "시점 불일치",
}

# 항목 종류. requirement = 요구사항 근거가 있음, characterization = 현재 동작을 그대로
# 적음(요구사항 근거 없음), question = 근거 부족으로 질문, needs_fixture = 대상
# fixture 가 필요해 첫 구현에서는 실행하지 않음.
ITEM_KINDS = ("requirement", "characterization", "question", "needs_fixture")
HOW_KEYS = ("existing", "generated", "none")
ITEM_REQUIRED = ("id", "kind", "behavior", "why", "basis", "how")
DOC_KEYS = ("schema", "status", "reason", "request", "items")

# 항목별 실행 증거 연결 상태. 증거 파일에서만 나온다 -- 세션의 설명으로 채우지 않는다.
LINK_UNLINKED = "unlinked"  # 연결 시도 전(도출이 수행·시점 일치가 아닐 때)
LINK_PASSED = "passed"
LINK_FAILED = "failed"
LINK_SKIPPED = "skipped"
LINK_DESELECTED = "deselected"
LINK_NOT_COLLECTED = "not_collected"
LINK_INCOMPLETE = "incomplete"  # 수집됐지만 결과 없음(중단·미완료)
LINK_NO_EVIDENCE = "no_evidence"  # 증거 파일 없음·불완전
LINK_NOT_RUN = "not_run"  # how.none — 질문·fixture 필요 등
LINK_LABEL = {
    LINK_UNLINKED: "미연결",
    LINK_PASSED: "통과",
    LINK_FAILED: "실패",
    LINK_SKIPPED: "skipped",
    LINK_DESELECTED: "선택 제외",
    LINK_NOT_COLLECTED: "미수집",
    LINK_INCOMPLETE: "실행 완료 확인 불가",
    LINK_NO_EVIDENCE: "증거 없음",
    LINK_NOT_RUN: "실행 안 함",
}

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class DeriveError(ValueError):
    """도출 파일을 받을 수 없다. 추측해서 고쳐 받지 않는다."""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- 활성화 표식 ----------------------------------------------------------------


def derive_enabled(state_dir: Path) -> bool:
    """이 프로젝트(상태 폴더)에 자동 도출이 켜져 있는가. 파일 존재만 본다."""
    return (Path(state_dir) / ENABLED_FILE).is_file()


def enable(state_dir: Path) -> Path:
    """표식을 만든다. 이미 있으면 그대로 둔다(켠 시각을 덮어쓰지 않는다)."""
    marker = Path(state_dir) / ENABLED_FILE
    if marker.is_file():
        return marker
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {"enabled_at": _now(), "tool_version": __version__}
    marker.write_bytes((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    return marker


def disable(state_dir: Path) -> bool:
    """표식을 지운다. 지웠으면 True, 원래 없었으면 False."""
    marker = Path(state_dir) / ENABLED_FILE
    if not marker.is_file():
        return False
    marker.unlink()
    return True


# --- 세션 되돌림 (2d) ---------------------------------------------------------------

# 되돌림 대상 연결 상태. 통과·skipped·선택 제외·실행 안 함(질문·fixture 필요)은 세션이 고칠
# 것이 아니거나 이미 선언한 것이라 깨우지 않는다.
NOTICE_LINK_STATUSES = (LINK_FAILED, LINK_NOT_COLLECTED, LINK_INCOMPLETE, LINK_NO_EVIDENCE)


def derive_notice(ds: DeriveStatus) -> str:
    """활성 프로젝트에서 세션을 깨울 이유. 없으면 빈 문자열. 기존 검사 판정과 무관하다."""
    if not ds.active:
        return ""
    if ds.status == NOT_PERFORMED:
        return f"자동 도출 미수행 — {ds.detail}"
    if ds.status in (INVALID, STALE):
        return f"도출 목록 {STATUS_LABEL[ds.status]} — {ds.detail}"
    if ds.status != PERFORMED:
        return ""
    bad = [i for i in ds.items if str(i.get("link_status") or "") in NOTICE_LINK_STATUSES]
    if not bad:
        return ""
    parts = []
    for item in bad:
        raw = str(item.get("link_status") or "")
        label = LINK_LABEL.get(raw, raw)
        detail = str(item.get("link_detail") or "")
        parts.append(f"{item.get('id')} {label}" + (f"({detail})" if detail else ""))
    return f"도출 항목 미확인 {len(bad)}개: " + "; ".join(parts)


def notice_signature(input_fingerprint: str, ds: DeriveStatus) -> str:
    """같은 되돌림 사유인지 판단하는 열쇠. 문구가 아니라 상태·해시·항목 ID 로 만든다."""
    bad = sorted(
        str(i.get("id"))
        for i in ds.items
        if str(i.get("link_status") or "") in NOTICE_LINK_STATUSES
    )
    return "|".join(["derive", input_fingerprint, ds.status, ds.derive_digest, ",".join(bad)])


def cli_invocation(env: Mapping[str, str] | None = None) -> str:
    """세션이 이 도구를 부를 명령.

    설치본이 PATH 에 있으면 `claimtrail`. 아니면(개발 체크아웃) 지금 이 코드를 돌리는
    파이썬으로 `-m claimtrail` 을 부르게 하고, 패키지 위치를 PYTHONPATH 로 준다.
    """
    e = os.environ if env is None else env
    if shutil.which("claimtrail", path=e.get("PATH", "")):
        return "claimtrail"
    py = Path(sys.executable).as_posix()
    src = Path(__file__).resolve().parents[1].as_posix()
    return f'PYTHONPATH="{src}" "{py}" -m claimtrail'


# --- 문서 검증 ----------------------------------------------------------------


def _validate_how(label: str, how: object, errors: list[str]) -> None:
    if not isinstance(how, dict) or len(how) != 1 or next(iter(how)) not in HOW_KEYS:
        errors.append(f"{label}: how 는 existing/generated/none 중 하나만 가진 객체여야 한다")
        return
    key, value = next(iter(how.items()))
    if key == "existing":
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(v, str) and "::" in v for v in value)
        ):
            errors.append(f"{label}: how.existing 은 'file::test' 문자열 목록이어야 한다")
    elif key == "generated":
        if not isinstance(value, str) or "::" not in value:
            errors.append(f"{label}: how.generated 는 'file.py::test' 형식이어야 한다")
    elif not isinstance(value, str) or not value.strip():
        errors.append(f"{label}: how.none 에는 실행하지 않는 이유가 필요하다")


def validate_document(data: object) -> list[str]:
    """도출 문서의 형식 오류 목록. 비어 있으면 형식은 맞다 -- 내용이 맞다는 뜻은 아니다."""
    if not isinstance(data, dict):
        return ["최상위는 객체여야 한다"]
    errors: list[str] = []
    if data.get("schema") != SCHEMA_VERSION:
        errors.append(f"schema 는 {SCHEMA_VERSION} 이어야 한다")
    status = data.get("status")
    if status not in DOC_STATUSES:
        errors.append(f"status 는 {'/'.join(DOC_STATUSES)} 중 하나여야 한다")
        return errors
    if status == NOT_APPLICABLE:
        if not str(data.get("reason") or "").strip():
            errors.append("not_applicable 에는 reason 이 필요하다")
        return errors

    items = data.get("items")
    if not isinstance(items, list) or not items:
        errors.append("performed 에는 items 가 1개 이상 필요하다")
        return errors
    seen: set[str] = set()
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"#{idx}: 항목은 객체여야 한다")
            continue
        label = str(item.get("id") or f"#{idx}")
        for key in ITEM_REQUIRED:
            if key not in item:
                errors.append(f"{label}: {key} 가 없다")
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            if item_id in seen:
                errors.append(f"{label}: id 중복")
            seen.add(item_id)
        elif "id" in item:
            errors.append(f"{label}: id 는 비어 있지 않은 문자열이어야 한다")
        kind = item.get("kind")
        if kind not in ITEM_KINDS:
            errors.append(f"{label}: kind 는 {'/'.join(ITEM_KINDS)} 중 하나여야 한다")
        for key in ("behavior", "why"):
            if key in item and not str(item[key] or "").strip():
                errors.append(f"{label}: {key} 가 비어 있다")
        if (
            kind in ("requirement", "characterization")
            and "basis" in item
            and not str(item["basis"] or "").strip()
        ):
            errors.append(f"{label}: basis 가 비어 있다 (근거 없는 항목은 question 으로)")
        if "how" in item:
            _validate_how(label, item["how"], errors)
    return errors


# --- 경로 ----------------------------------------------------------------------


def _check_id(name: str, value: str) -> str:
    if not _SAFE_ID.match(value or ""):
        raise DeriveError(f"{name} 가 비어 있거나 허용되지 않는 문자를 담고 있다: {value!r}")
    return value


def derive_dir(state_dir: Path, session_id: str, prompt_id: str) -> Path:
    """이번 작업의 도출 파일 위치. 식별자는 경로 조각이 되므로 문자를 제한한다."""
    return (
        Path(state_dir)
        / DERIVE_DIR
        / _check_id("session_id", session_id)
        / _check_id("prompt_id", prompt_id)
    )


def _generated_refs(doc: dict) -> set[str]:
    refs: set[str] = set()
    for item in doc.get("items") or []:
        how = item.get("how") if isinstance(item, dict) else None
        if isinstance(how, dict) and isinstance(how.get("generated"), str):
            refs.add(how["generated"].split("::", 1)[0])
    return refs


def _doc_part(record: dict) -> dict:
    return {k: record[k] for k in DOC_KEYS if k in record}


def _redacted(value: object) -> object:
    """증빙·로그에 내보내는 값의 자격증명을 가린다. 해시 계산은 이 함수를 거치지 않은
    원문으로 한다 -- 가림 결과로 해시하면 같은 목록이 가림 규칙 변경에 따라 달라진다."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redacted(v) for v in value]
    if isinstance(value, dict):
        return {k: _redacted(v) for k, v in value.items()}
    return value


def _redacted_item(item: dict) -> dict:
    """항목 하나(사전)를 표시용으로 가린 새 사전. 원본은 바꾸지 않는다.

    `_redacted` 는 값의 종류를 보존한다(사전을 넣으면 사전이 나온다). 타입 검사기는 그
    대응을 알 수 없으므로 여기서 실제로 확인한다. 사전이 아닌 것이 나오면 가림이 깨진
    것이다 -- 빈 사전으로 조용히 바꾸지 않고 멈춘다.
    """
    out = _redacted(item)
    if not isinstance(out, dict):
        raise TypeError(f"가린 항목이 사전이 아니다: {type(out).__name__}")
    return out


def _digest(doc: dict, generated: dict[str, bytes]) -> str:
    """목록 JSON 과 생성 테스트 내용을 함께 해시한다. 같은 파일 경로에 다른
    assertion 이 들어와도 다른 목록으로 본다."""
    h = hashlib.sha256()
    h.update(json.dumps(_doc_part(doc), ensure_ascii=False, sort_keys=True).encode("utf-8"))
    for name in sorted(generated):
        h.update(b"\0")
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(generated[name])
    return h.hexdigest()


# --- 제출 ----------------------------------------------------------------------


@dataclass(frozen=True)
class SubmitResult:
    path: Path
    status: str
    derive_digest: str
    input_fingerprint: str


def submit(
    state_dir: Path,
    root: Path,
    policy: WatchPolicy,
    session_id: str,
    prompt_id: str,
    doc: dict,
    generated_files: list[Path] | tuple[Path, ...] = (),
) -> SubmitResult:
    """도출 문서를 받아 상태 폴더에 기록한다.

    입력 지문은 여기서 계산한다 -- 세션이 적은 값을 쓰면 '언제의 코드에 대한
    목록인가' 를 세션의 말에 맡기게 된다. 생성 테스트는 사본을 남기고 내용 해시를
    기록한다. 원본 저장소에는 아무것도 쓰지 않는다.
    """
    errors = validate_document(doc)
    if errors:
        raise DeriveError("; ".join(errors))
    target = derive_dir(state_dir, session_id, prompt_id)

    provided: dict[str, Path] = {}
    for p in generated_files:
        path = Path(p)
        if path.name in provided:
            raise DeriveError(f"생성 파일 이름이 겹친다: {path.name}")
        provided[path.name] = path
    missing = sorted(_generated_refs(doc) - set(provided))
    if missing:
        raise DeriveError(f"참조한 생성 파일이 없다: {', '.join(missing)}")
    contents: dict[str, bytes] = {}
    for name, path in provided.items():
        try:
            contents[name] = path.read_bytes()
        except OSError as exc:
            raise DeriveError(f"생성 파일을 읽지 못했다: {path} — {exc}") from exc

    fp = fingerprint(root, policy)
    if not fp.ok or fp.digest is None:
        raise DeriveError(f"입력 지문을 계산하지 못했다: {fp.reason or '원인 불명'}")

    digest = _digest(doc, contents)
    record = _doc_part(doc)
    record.update(
        {
            "session_id": session_id,
            "prompt_id": prompt_id,
            "submitted_at": _now(),
            "input_fingerprint": fp.digest,
            "policy_hash": policy.policy_hash(),
            "tool_version": __version__,
            "generated": {name: _sha256(data) for name, data in contents.items()},
            "derive_digest": digest,
        }
    )

    gen_dir = target / GENERATED_DIR
    gen_dir.mkdir(parents=True, exist_ok=True)
    for name, data in contents.items():
        (gen_dir / name).write_bytes(data)
    path = target / RECORD_FILE
    tmp = target / (RECORD_FILE + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return SubmitResult(path, str(doc["status"]), digest, fp.digest)


# --- 읽기 (훅 쪽) --------------------------------------------------------------


@dataclass
class DeriveStatus:
    status: str
    detail: str = ""
    derive_digest: str = ""
    items: list[dict] = field(default_factory=list)  # 표시용(가림). 연결·해시에 쓰지 않는다
    request: str = ""
    path: str = ""
    # 연결·해시용 원본 항목. 가림을 거치지 않는다 -- 식별자가 바뀌면 연결이 어긋난다.
    raw_items: list[dict] = field(default_factory=list)
    # 기존 실행·생성 실행의 증거 메타(invocation_id, 수집·선택 수, 완료 여부 …).
    runs: dict = field(default_factory=dict)
    # 연결 상태별 개수. 연결 전에는 비어 있다.
    summary: dict = field(default_factory=dict)
    # 활성화 표식 유무. 미수행일 때 "기대했는데 안 했다"와 "애초에 안 켰다"를 가른다.
    active: bool = False

    @property
    def cache_ok(self) -> bool:
        """이 상태를 근거로 이전 PASS 를 재사용해도 되는가. 무효·stale 은 안 된다."""
        return self.status not in (INVALID, STALE)


def load_derive(
    state_dir: Path,
    session_id: str,
    prompt_id: str,
    current_digest: str | None,
    policy_hash: str,
    active: bool = False,
) -> DeriveStatus:
    """현재 작업의 도출 파일을 읽고 상태를 판정한다. 이전 작업의 파일은 보지 않는다.

    `active` 는 활성화 표식 유무다. 미수행의 이유 문구만 바꾸고 판정·캐시는 바꾸지 않는다.
    """
    ds = _load_derive(state_dir, session_id, prompt_id, current_digest, policy_hash)
    ds.active = active
    if ds.status == NOT_PERFORMED:
        prefix = "활성 프로젝트 — 도출 미수행" if active else "자동 도출 비활성(표식 없음)"
        ds.detail = f"{prefix}: {ds.detail}"
    return ds


def _load_derive(
    state_dir: Path,
    session_id: str,
    prompt_id: str,
    current_digest: str | None,
    policy_hash: str,
) -> DeriveStatus:
    if not session_id or not prompt_id:
        return DeriveStatus(
            NOT_PERFORMED, "작업 식별자(session_id·prompt_id)가 없어 도출 목록을 찾지 않았다"
        )
    try:
        target = derive_dir(state_dir, session_id, prompt_id)
    except DeriveError as exc:
        return DeriveStatus(NOT_PERFORMED, str(exc))
    path = target / RECORD_FILE
    if not path.is_file():
        return DeriveStatus(NOT_PERFORMED, "이번 작업의 도출 파일이 없다", path=str(path))

    try:
        record = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return DeriveStatus(INVALID, f"JSON 으로 읽지 못했다 — {exc}", path=str(path))
    if not isinstance(record, dict):
        return DeriveStatus(INVALID, "최상위가 객체가 아니다", path=str(path))
    if record.get("session_id") != session_id or record.get("prompt_id") != prompt_id:
        return DeriveStatus(INVALID, "파일 안 식별자가 현재 작업과 다르다", path=str(path))
    errors = validate_document(record)
    if errors:
        return DeriveStatus(INVALID, "; ".join(errors), path=str(path))
    for key in ("input_fingerprint", "policy_hash", "derive_digest"):
        if not str(record.get(key) or ""):
            return DeriveStatus(
                INVALID, f"{key} 가 없다 — 도구가 제출한 파일이 아니다", path=str(path)
            )
    generated = record.get("generated") or {}
    if not isinstance(generated, dict):
        return DeriveStatus(INVALID, "generated 는 객체여야 한다", path=str(path))

    digest = str(record["derive_digest"])
    contents: dict[str, bytes] = {}
    for name, expected in generated.items():
        try:
            data = (target / GENERATED_DIR / str(name)).read_bytes()
        except OSError:
            return DeriveStatus(STALE, f"생성 파일이 없다: {name}", digest, path=str(path))
        if _sha256(data) != expected:
            return DeriveStatus(
                STALE, f"생성 테스트가 제출 뒤 바뀌었다: {name}", digest, path=str(path)
            )
        contents[str(name)] = data
    if _digest(record, contents) != digest:
        return DeriveStatus(INVALID, "derive_digest 가 내용과 맞지 않는다", path=str(path))

    if record["policy_hash"] != policy_hash:
        return DeriveStatus(STALE, "감시 정책이 제출 시점과 다르다", digest, path=str(path))
    if current_digest is None or record["input_fingerprint"] != current_digest:
        return DeriveStatus(
            STALE,
            "제출 뒤 입력이 바뀌었다 — 목록은 이전 코드에 대한 것이다",
            digest,
            path=str(path),
        )

    # 여기서부터는 사람·로그에 보이는 값이다. 세션이 적은 문장에 비밀값이 섞였을 수 있으므로
    # 가린다. digest 는 위에서 원문으로 이미 확정됐다.
    status = str(record["status"])
    if status == NOT_APPLICABLE:
        reason = str(_redacted(str(record.get("reason") or "")))
        return DeriveStatus(NOT_APPLICABLE, reason, digest, path=str(path))
    raw_items = [dict(item, link_status=LINK_UNLINKED) for item in record.get("items") or []]
    items = [_redacted_item(item) for item in raw_items]
    return DeriveStatus(
        PERFORMED,
        f"항목 {len(items)}개 접수, 형식 확인",
        digest,
        items,
        str(_redacted(str(record.get("request") or ""))),
        str(path),
        raw_items=raw_items,
    )


# --- 증빙 표시 ------------------------------------------------------------------


def derive_section(ds: DeriveStatus) -> dict:
    """JSON 증빙의 derive 절."""
    return {
        "status": ds.status,
        "active": ds.active,
        "detail": ds.detail,
        "derive_digest": ds.derive_digest,
        "request": ds.request,
        "items": ds.items,
        "path": ds.path,
        "runs": ds.runs,
        "summary": ds.summary,
    }


def _how_text(how: object) -> str:
    if not isinstance(how, dict) or not how:
        return "—"
    key, value = next(iter(how.items()))
    if key == "existing" and isinstance(value, list):
        return "기존: " + ", ".join(f"`{v}`" for v in value)
    if key == "generated":
        return f"생성: `{value}`"
    return f"실행 안 함: {value}"


def _one_line(text: object) -> str:
    """자유 입력을 한 줄로 만든다.

    사유·요청·항목 문구는 검증이 비어 있는지만 보므로 줄바꿈과 '## ...' 가 그대로 들어온다.
    줄바꿈이 남으면 문구가 줄 시작에 놓여 절 제목을 흉내 낼 수 있다. str.split() 은
    \\r·\\u2028 같은 줄 구분 문자까지 공백으로 본다. digest 는 원문으로 계산하므로 바뀌지 않는다.
    """
    return " ".join(str(text).split())


def derive_markdown(ds: DeriveStatus) -> list[str]:
    """Markdown 증빙의 '## 자동 도출' 절. 기존 검사 판정과 별개의 축이다."""
    label = STATUS_LABEL.get(ds.status, ds.status)
    lines = ["## 자동 도출", "", f"- 자동 도출: {label} — {_one_line(ds.detail)}"]
    if ds.request:
        lines.append(f"- 요청: {_one_line(ds.request)}")
    if ds.summary:
        parts = [
            f"{LINK_LABEL[k]} {ds.summary[k]}"
            for k in (
                LINK_PASSED,
                LINK_FAILED,
                LINK_SKIPPED,
                LINK_DESELECTED,
                LINK_NOT_COLLECTED,
                LINK_INCOMPLETE,
                LINK_NO_EVIDENCE,
                LINK_NOT_RUN,
            )
            if ds.summary.get(k)
        ]
        lines.append(f"- 항목 {ds.summary.get('total', 0)}개: " + ", ".join(parts))
    for key, name in (("existing", "기존 검사 실행"), ("generated", "생성 검사 실행")):
        meta = ds.runs.get(key)
        if meta:
            lines.append(f"- {name}: {_one_line(_run_meta_text(meta))}")
    if ds.status == PERFORMED and ds.items:
        lines.append("")
        lines.append("| ID | 종류 | 확인할 동작 | 이유 | 근거 | 확인 방법 | 실행 증거 |")
        lines.append("|---|---|---|---|---|---|---|")
        for item in ds.items:
            cells = (
                item.get("id", ""),
                item.get("kind", ""),
                item.get("behavior", ""),
                item.get("why", ""),
                item.get("basis", "") or "—",
                _how_text(item.get("how")),
                _link_text(item),
            )
            lines.append("| " + " | ".join(_one_line(c) for c in cells) + " |")
    lines.append("")
    lines.append(
        "> 이 절은 도출 목록의 접수·형식·시점 확인과 항목별 실행 증거 연결 결과다. 도출이 "
        "맞거나 충분하다는 뜻이 아니며, 생성 검사의 실패는 결함 후보이지 확정이 아니다. "
        "위 판정(기존 검사)과는 별개의 축이다."
    )
    lines.append("")
    return lines


def _link_text(item: dict) -> str:
    status = str(item.get("link_status") or LINK_UNLINKED)
    text = LINK_LABEL.get(status, status)
    detail = str(item.get("link_detail") or "")
    return f"{text} — {detail}" if detail else text


def _run_meta_text(meta: dict) -> str:
    parts = []
    if meta.get("invocation_id"):
        parts.append(f"실행 ID `{meta['invocation_id']}`")
    if meta.get("status"):
        parts.append(f"결과 {meta['status']}")
    if meta.get("collected_count") is not None:
        parts.append(
            f"수집 {meta.get('collected_count')} · 선택 {meta.get('selected_count')} "
            f"· 선택 제외 {meta.get('deselected_count', 0)}"
        )
    parts.append("세션 종료 확인" if meta.get("session_finished") else "세션 종료 미확인")
    if meta.get("note"):
        parts.append(str(meta["note"]))
    return ", ".join(parts)
