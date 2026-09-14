"""한 번의 훅 실행을 기록하고, 다음 실행이 무엇을 할지 정한다.

hookscan 과 나눈 이유
    hookscan 은 '무엇을 볼 것인가'가 바뀔 때 손댄다 -- 모노레포 구조, 새
    언어, ignore 규칙, 경로 안전성. 이쪽은 '한 번의 실행을 어떻게 기록할
    것인가'가 바뀔 때 손댄다 -- 상태 필드, 알림 억제, 증빙 승격. 두 축은
    함께 움직이지 않는다. 모노레포 대응을 고치는 동안 이 파일은 그대로였다.

여기서 지키는 것
    1. 검증 중에 코드가 바뀌면 그 실행 결과를 PASS 로 확정하지 않는다.
    2. 실패·검증 불가는 '변경이 없다'는 이유로 사라지지 않는다.
    3. 기계 판정의 근거는 사람이 읽는 Markdown 문구가 아니라 JSON 이다.
    4. 알림을 멈추는 것과 판정을 바꾸는 것은 다른 일이다.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .hookscan import Fingerprint
from .runners.base import FAIL, PASS, UNVERIFIED

# 상태 파일 구조가 바뀌면 올린다. 이전 상태를 그대로 믿으면 안 되기 때문이다.
SCHEMA_VERSION = 3

# 한 Stop 연속 호출에서 재알림할 수 있는 최대 횟수.
# 상한이 없으면 훅이 에이전트를 무한히 깨울 수 있다.
MAX_REWAKES = 2

# 실행이 결론에 도달했는가. 이것만 나타낸다.
# freshness 와 verdict 는 별개 축이다 -- 한 필드에 섞으면 'stale 인데 완료'
# 같은 조합을 표현할 수 없게 되거나, 뜻이 겹쳐 판단이 흐려진다.
RUNNING = "running"
COMPLETED = "completed"
PHASES = (RUNNING, COMPLETED)

FRESH = "fresh"
STALE = "stale"
UNKNOWN = "unknown"

VALID_VERDICTS = (PASS, FAIL, UNVERIFIED)

# JSON 증빙이 반드시 가져야 하는 필드. 하나라도 없으면 판정 근거로 쓰지 않는다.
REQUIRED_EVIDENCE_FIELDS = ("tool", "version", "target", "ran_at", "verdict", "results")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def new_run_id() -> str:
    return uuid.uuid4().hex


def normalize_reason(reason: str) -> str:
    """사람이 읽는 설명을 떼고 기계용 코드만 남긴다.

    "git_index_unavailable: git index 를 읽지 못했다" -> "git_index_unavailable"
    signature 에 한국어 원문이 들어가면 문구를 다듬는 순간 같은 실패가 다른
    실패로 보인다.
    """
    return (reason or "").split(":", 1)[0].strip()


# --- 상태 -------------------------------------------------------------------


@dataclass
class HookState:
    """마지막 검증에 대해 아는 것 전부.

    fingerprint 를 셋으로 나눈다. 하나로 합치면 'stale 인데 verified' 같은
    모순 상태를 표현할 수 있게 되고, 표현할 수 있는 상태는 언젠가 저장된다.
    """

    schema: int = SCHEMA_VERSION
    run_id: str = ""
    input_fingerprint: str = ""
    post_fingerprint: str = ""
    verified_fingerprint: str = ""  # fresh 로 끝났을 때만 채운다
    phase: str = ""  # running | completed
    verdict: str = ""  # 기록되는 판정
    raw_verdict: str = ""  # 강등 전 원시 판정 (정보 보존용)
    freshness: str = ""  # fresh | stale | unknown
    reason_code: str = ""
    reason_detail: str = ""
    # 원인을 단계별로 나눠 남긴다. 하나로 합치면 어느 단계가 왜 실패했는지가
    # 사라진다. '측정을 시작조차 못 했다'와 '측정 후 재측정에 실패했다'는
    # 다른 사건이고, 후자는 검증이 실제로 돌았다는 뜻이다.
    before_reason: str = ""
    after_reason: str = ""
    # 증빙은 코드와 설명을 나눈다. 한 문자열에 섞으면 코드로 분기하려는
    # 쪽이 매번 파싱해야 하고, 설명 문구를 다듬는 순간 분기가 깨진다.
    evidence_code: str = ""
    evidence_detail: str = ""
    lock_backend: str = ""
    # Stop 페이로드에서 읽은 맥락. 키 이름만 남기고 값은 남기지 않는다.
    context_note: str = ""
    started_at: str = ""
    verified_at: str = ""
    tool_version: str = ""
    policy_hash: str = ""
    # 검증이 돈 실행 환경(인터프리터·설치 패키지·Node). 다르면 같은 파일에
    # 다른 답이 나올 수 있으므로 캐시 근거가 아니다.
    execution_context: str = ""
    # PASS 를 확정한 세션. 새 세션의 첫 Stop 은 이전 세션의 PASS 를 믿지 않는다.
    verified_session_id: str = ""
    evidence_path: str = ""
    # --- 알림 상태. 검증 상태와 섞지 않는다 ---
    last_notified_session_id: str = ""
    last_notified_signature: str = ""
    rewake_count: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict) -> HookState:
        """타입이 맞지 않으면 TypeError. 반쪽짜리 상태를 그럴듯하게 만들지 않는다.

        schema 가 "1" 이거나 rewake_count 가 "2" 인 상태를 조용히 받아들이면,
        나중에 비교와 증가 연산이 엉뚱하게 동작한다. 그때는 이미 그 상태로
        캐시 판단을 한 뒤다.
        """
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            expected = _FIELD_TYPES.get(key)
            if expected is None:
                continue
            if expected is int:
                # bool 은 int 의 하위형이다. rewake_count=True 를 받으면 안 된다.
                if not isinstance(value, int) or isinstance(value, bool):
                    raise TypeError(f"{key} 는 정수여야 한다: {value!r}")
            elif not isinstance(value, expected):
                raise TypeError(f"{key} 는 {expected.__name__} 이어야 한다: {value!r}")
            kwargs[key] = value
        state = cls(**kwargs)
        state.validate()
        return state

    def validate(self) -> None:
        """열거값과 필수 조합을 확인한다.

        모르는 값이나 모순된 조합을 그대로 받아들이면, 그 상태로 캐시 판단을
        한 뒤에야 이상을 눈치챈다.
        """
        if not self.phase:
            # 디스크에서 읽은 상태다. phase 가 없으면 무엇도 알 수 없는데,
            # 빈 정상 상태로 받아들이면 모르면서 안다고 말하는 셈이다.
            raise TypeError("phase 가 없다")

        for field_name, allowed in (
            ("phase", (RUNNING, COMPLETED)),
            ("freshness", ("", FRESH, STALE, UNKNOWN)),
            ("verdict", ("", *VALID_VERDICTS)),
            ("raw_verdict", ("", *VALID_VERDICTS)),
        ):
            value = getattr(self, field_name)
            if value not in allowed:
                raise TypeError(f"{field_name} 이 알 수 없는 값이다: {value!r}")

        if self.phase == RUNNING and self.verdict:
            raise TypeError("running 인데 판정이 있다")
        if self.phase == COMPLETED and not self.verdict:
            raise TypeError("completed 인데 판정이 없다")
        if self.verified_fingerprint and self.freshness != FRESH:
            raise TypeError("확정된 fingerprint 가 있는데 fresh 가 아니다")


# 상태 파일에서 읽을 때 확인하는 필드 타입. 문자열이어야 할 곳에 숫자가
# 들어오면 비교가 조용히 어긋난다.
_FIELD_TYPES: dict[str, type] = {
    name: (int if name in ("schema", "rewake_count") else str)
    for name in HookState.__dataclass_fields__
}


def atomic_write(target: Path, data: bytes) -> Path:
    """같은 폴더 임시 파일에 다 쓴 뒤 교체한다.

    같은 폴더여야 os.replace 가 파일시스템 경계를 넘지 않아 원자적이다.
    실패는 감추지 않고 그대로 올린다 -- 조용히 넘어가면 '안 썼다'와
    '반쯤 썼다'를 구분할 수 없고, 반쯤 쓴 증빙은 아무 말도 하지 못한다.
    """
    target = Path(target)
    tmp = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return target


def state_dir_for(root: Path, base: Path | None = None) -> Path:
    """프로젝트별 상태 폴더.

    폴더명만 쓰면 project-a/backend 와 project-b/backend 가 충돌한다. canonical
    경로 해시를 붙여 구분하되, 사람도 읽을 수 있게 이름을 남긴다.
    """
    canonical = str(Path(root).resolve())
    digest = _sha256(canonical.encode("utf-8"))[:12]
    name = Path(canonical).name or "root"
    if base is None:
        env_base = os.environ.get("CLAIMTRAIL_STATE_DIR")
        base = Path(env_base) if env_base else (Path.home() / ".claude" / "claimtrail")
    return Path(base) / f"{name}-{digest}"


def ensure_state_dir(path: Path) -> Path:
    """상태 폴더를 만든다. 남의 눈에 띄지 않게 권한을 좁힌다."""
    prev = os.umask(0o077)
    try:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        (path / "runs").mkdir(exist_ok=True)
        (path / "tmp").mkdir(exist_ok=True)
        if os.name != "nt":
            os.chmod(path, 0o700)
    finally:
        os.umask(prev)
    return path


STATE_MISSING = "missing"
STATE_OK = "ok"
STATE_INVALID = "invalid"
STATE_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class StateLoad:
    """상태를 왜 못 읽었는지까지 알려준다.

    셋을 뭉뚱그리면 대응을 나눌 수 없다.
      missing     첫 실행이다. 정상 진행한다.
      invalid     손상됐다. 재검증하고 그 사실을 기록한다.
      unreadable  권한이나 I/O 문제다. 환경 문제이므로 알려야 한다.
    """

    state: HookState | None
    status: str
    detail: str = ""


def load_state_result(state_dir: Path) -> StateLoad:
    p = Path(state_dir) / "state.json"
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return StateLoad(None, STATE_MISSING)
    except OSError as exc:
        return StateLoad(None, STATE_UNREADABLE, str(exc))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return StateLoad(None, STATE_INVALID, f"JSON 으로 읽지 못했다 — {exc}")
    if not isinstance(data, dict):
        return StateLoad(None, STATE_INVALID, "최상위가 객체가 아니다")
    try:
        return StateLoad(HookState.from_dict(data), STATE_OK)
    except TypeError as exc:
        return StateLoad(None, STATE_INVALID, str(exc))


def load_state(state_dir: Path) -> HookState | None:
    """읽지 못하면 None. 반쪽짜리 상태를 그럴듯하게 복원하지 않는다.

    왜 못 읽었는지가 필요하면 load_state_result 를 쓴다.
    """
    return load_state_result(state_dir).state


def save_state(state_dir: Path, state: HookState) -> Path:
    """같은 폴더의 임시 파일에 다 쓴 뒤 원자적으로 교체한다.

    임시 파일을 다른 폴더에 두면 os.replace 가 파일시스템 경계를 넘을 수
    있고 그러면 원자성이 깨진다. 쓰다가 죽어도 이전 상태가 그대로 남아야
    한다 -- 깨진 상태는 '이전 PASS 를 못 믿는다'로 이어지지만, 사라진
    상태는 아무 말도 하지 못한다.
    """
    prev = os.umask(0o077)
    try:
        return atomic_write(Path(state_dir) / "state.json", state.to_json().encode("utf-8"))
    finally:
        os.umask(prev)


def begin_run(
    state_dir: Path,
    run_id: str,
    before: Fingerprint,
    policy_hash: str,
    lock_backend: str = "",
    context_note: str = "",
    execution_context: str = "",
) -> Path:
    """검증을 시작한다고 기록한다.

    여기서 죽으면 phase 가 running 으로 남는다. 그게 목적이다 -- 결론이 없는
    실행을 다음 호출이 알아보고 재검증한다.
    """
    previous = load_state(state_dir) or HookState()
    return save_state(
        state_dir,
        replace(
            previous,  # 알림 상태(last_notified_*, rewake_count)는 그대로 이어받는다.
            schema=SCHEMA_VERSION,
            run_id=run_id,
            phase=RUNNING,
            input_fingerprint=before.digest or "",
            before_reason=before.reason,
            started_at=_now(),
            lock_backend=lock_backend,
            context_note=context_note,
            tool_version=__version__,
            policy_hash=policy_hash,
            execution_context=execution_context,
            verified_session_id="",
            # 과거 판정을 명시적으로 지운다. 남겨두면 실행 중에 이전 PASS 가
            # 현재 판정처럼 읽힌다.
            post_fingerprint="",
            verified_fingerprint="",
            verdict="",
            raw_verdict="",
            freshness="",
            reason_code="",
            reason_detail="",
            after_reason="",
            evidence_code="",
            evidence_detail="",
            evidence_path="",
            verified_at="",
        ),
    )


def save_state_cas(state_dir: Path, state: HookState, expected_run_id: str) -> bool:
    """begin_run 이 남긴 내 running 을 확정할 때만 저장한다.

    계약: 호출자는 OS 잠금을 보유한 상태여야 한다. 이 함수의 읽기와 쓰기
    사이에는 여전히 틈이 있어, 이것만으로 동시 실행을 막지 못한다. 잠금이
    1차 방어이고 이것은 늦게 깨어난 실행을 걸러내는 2차 방어다.

    거부 조건
      - 상태 없음: 내가 시작을 기록하지 못했다. 시작조차 안 한 실행의
        판정을 쓰면 안 된다.
      - 읽기 실패: 무엇을 덮어쓰는지 모른다. load_state 가 None 을 준다.
      - phase != running: 이미 확정된 것을 다시 확정하려는 것이다.
      - run_id 불일치: 내 실행이 아니다.

    후보 상태도 검사한다. run_id 가 다르면 남의 결과를 내 이름으로 쓰는
    것이고, phase 가 completed 가 아니면 확정 연산으로 확정되지 않은 것을
    쓰는 것이라 running 이 영원히 남는다.
    """
    if state.run_id != expected_run_id:
        return False
    if state.phase != COMPLETED:
        return False
    current = load_state(state_dir)
    if current is None:
        return False
    if current.phase != RUNNING:
        return False
    if current.run_id != expected_run_id:
        return False
    save_state(state_dir, state)
    return True


# --- 증빙 -------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceCheck:
    valid: bool
    verdict: str
    # ok | missing | empty | invalid_json | missing_field | bad_verdict | wrong_tool
    reason_code: str
    detail: str = ""


def validate_evidence(path: Path) -> EvidenceCheck:
    """기계 판정의 근거는 JSON 이다.

    Markdown 문구로 판정하면 문구를 다듬는 순간 훅이 오작동한다. 사람이
    읽는 글과 기계가 읽는 계약은 같은 파일일 수 없다.
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError:
        return EvidenceCheck(False, "", "missing", f"증빙 파일이 없다 — {p.name}")
    if not raw.strip():
        return EvidenceCheck(False, "", "empty", "증빙 파일이 비어 있다")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return EvidenceCheck(False, "", "invalid_json", f"JSON 으로 읽지 못했다 — {exc}")
    if not isinstance(data, dict):
        return EvidenceCheck(False, "", "invalid_json", "최상위가 객체가 아니다")
    if data.get("tool") != "claimtrail":
        return EvidenceCheck(False, "", "wrong_tool", f"tool={data.get('tool')!r}")
    missing = [f for f in REQUIRED_EVIDENCE_FIELDS if f not in data]
    if missing:
        return EvidenceCheck(False, "", "missing_field", f"빠진 필드: {', '.join(missing)}")
    verdict = data.get("verdict")
    if verdict not in VALID_VERDICTS:
        return EvidenceCheck(False, "", "bad_verdict", f"verdict={verdict!r}")
    return EvidenceCheck(True, verdict, "ok")


def run_paths(state_dir: Path, run_id: str) -> dict[str, Path]:
    """실행마다 고유한 경로. 과거 파일을 다시 읽는 일이 없게 한다."""
    d = Path(state_dir)
    return {
        "tmp_json": d / "tmp" / f"evidence.{run_id}.json",
        "tmp_md": d / "tmp" / f"evidence.{run_id}.md",
        "run_json": d / "runs" / f"{run_id}.json",
        "run_md": d / "runs" / f"{run_id}.md",
        "latest_md": d / "evidence.md",
    }


def archive_run(state_dir: Path, run_id: str) -> Path | None:
    """원시 실행 증빙을 그대로 보관한다. 공개는 하지 않는다.

    무엇을 봤는지는 사실이므로 stale 이어도 보관한다. 다만 '지금 코드의
    최신 판정'으로 내보내는 것은 별개의 판단이라 publish_latest 가 맡는다.
    보관과 공개를 한 함수에 두면 언젠가 stale 결과가 최신본으로 새어 나간다.
    """
    paths = run_paths(state_dir, run_id)
    if not validate_evidence(paths["tmp_json"]).valid:
        return None
    os.replace(paths["tmp_json"], paths["run_json"])
    if paths["tmp_md"].is_file():
        os.replace(paths["tmp_md"], paths["run_md"])
    return paths["run_json"]


def invalidate_latest(state_dir: Path, run_id: str) -> Path:
    """검증을 시작하기 전에 최신본을 무효화한다.

    검증 도중 죽으면 과거 PASS 가 그 자리에 남아 지금 코드의 판정처럼 보인다.
    사람이 읽는 산출물이 자기 유효기간보다 오래 살아남지 않게 한다.
    본문에 판정을 넣지 않는다 -- 아직 아무것도 확인하지 않았다.
    """
    notice = (
        "# 검증 증빙 — 진행 중\n\n"
        f"- 실행: `{run_id}`\n"
        f"- 시작: {_now()}\n\n"
        "검증이 진행 중이다. 이전 결과는 더 이상 지금 코드에 대한 주장이 아니다.\n"
        "이 파일이 이 상태로 남아 있다면 그 실행은 결론에 도달하지 못한 것이다.\n"
    )
    return atomic_write(run_paths(state_dir, run_id)["latest_md"], notice.encode("utf-8"))


def _plain_notice(run_id: str) -> bytes:
    """Markdown 본문이 없을 때 세우는 일반 고지. 판정 근거는 JSON 이다."""
    return (
        f"# 검증 증빙\n\n- 실행: `{run_id}`\n"
        f"- Markdown 본문이 없다. 판정 근거는 `runs/{run_id}.json` 이다.\n"
    ).encode()


def restore_latest(state_dir: Path, run_id: str) -> Path | None:
    """건너뛸 때, 진행 중 고지를 보관된 증빙으로 되돌린다.

    건너뛰었는데 최신본이 '진행 중' 이면 사람이 읽을 것이 없다. 건너뛴 근거는
    보관된 그 실행의 증빙이므로 그것을 다시 세운다.

    Markdown 이 없고 JSON 만 유효하면 일반 고지를 세운다 -- 판정 근거는
    어차피 JSON 이고, 진행 중 고지를 남겨두는 것보다 낫다.

    없는 것과 못 읽는 것을 구분한다. FileNotFoundError 만 '없음'으로 보고
    나머지 I/O 오류는 올린다. 조용히 None 을 돌려주면 권한 문제로 못 읽은
    것이 '보관본이 없다'로 둔갑한다.

    Markdown 유무와 상관없이 JSON 을 먼저 본다. 건너뛴 근거는 JSON 이고,
    Markdown 이 있다고 근거가 생기지는 않는다. 건너뛰기는 PASS 일 때만
    일어나므로 PASS 증빙이 아니면 복원하지 않는다.
    """
    paths = run_paths(state_dir, run_id)
    check = validate_evidence(paths["run_json"])
    if not check.valid or check.verdict != PASS:
        return None
    try:
        body = paths["run_md"].read_bytes()
    except FileNotFoundError:
        body = _plain_notice(run_id)
    return atomic_write(paths["latest_md"], body)


def publish_latest(
    state_dir: Path,
    run_id: str,
    final: Finalized,
    archived: Path | None,
) -> Path | None:
    """fresh 로 끝났고 실제로 보관된 실행만 최신 증빙으로 공개한다.

    stale 실행의 원시 판정이 PASS 였더라도, 검증이 본 코드와 지금 코드가
    다르므로 그 PASS 는 지금 코드에 대한 주장이 아니다.

    보관되지 않았으면 보관 경로를 안내하지 않는다. 없는 파일을 가리키는
    안내문은 조사할 때 사람을 헤매게 한다.
    """
    paths = run_paths(state_dir, run_id)
    latest = paths["latest_md"]

    # archived 인자를 그대로 믿지 않는다. 호출자가 넘긴 경로가 실제로
    # 있고 유효한지 여기서 확인한다 -- 없는 증빙을 근거로 공개하면
    # 그 공개는 주장이지 증빙이 아니다.
    stored = validate_evidence(paths["run_json"])
    has_evidence = archived is not None and stored.valid

    # 디스크에 남은 판정과 이번 실행의 판정이 어긋나면 직렬화나 기록이
    # 어긋난 것이다. 그 Markdown 을 최신본으로 세우면 디스크의 사실과
    # 다른 것을 공개하게 된다.
    consistent = (not has_evidence) or (
        stored.verdict == final.verdict == final.raw_verdict
    )
    mismatch = final.reason_code == "ok" and has_evidence and not consistent

    publishable = (
        final.freshness == FRESH
        and final.reason_code == "ok"
        and has_evidence
        and consistent
    )

    if publishable:
        try:
            body = paths["run_md"].read_bytes()
        except FileNotFoundError:
            # Markdown 없이 JSON 만 남긴 실행. 실패가 아니라 알려진 상태다.
            body = _plain_notice(run_id)
        return atomic_write(latest, body)

    lines = [
        "# 검증 증빙 — 최신본 없음",
        "",
        f"- 실행: `{run_id}`",
        f"- freshness: `{final.freshness}`",
        f"- 원인: `{final.reason_code}`",
        f"- 원시 판정: `{final.raw_verdict or '(실행하지 않음)'}`",
    ]
    if mismatch:
        lines.append(
            f"- 불일치: `evidence_verdict_mismatch` "
            f"(디스크=`{stored.verdict}`, 이번 실행=`{final.verdict}`)"
        )
    for label, value in (
        ("before", final.before_reason),
        ("after", final.after_reason),
        ("evidence", final.evidence_code),
        ("evidence 상세", final.evidence_detail),
        ("상세", final.reason_detail),
    ):
        if value:
            lines.append(f"- {label}: `{value}`")
    lines.append("")
    if has_evidence:
        lines.append(f"그 실행의 원시 결과는 `runs/{run_id}.json` 에 그대로 남아 있다.")
    else:
        lines.append("이번 실행의 증빙은 **보관되지 않았다**.")
        lines.append(f"보관본 확인 결과: `{stored.reason_code}`")
        lines.append(f"거부된 원본이 있다면 `tmp/rejected/{run_id}.json` 에 있다.")
        lines.append("훅 로그의 같은 시각 줄을 함께 볼 것.")
    lines.append("")
    lines.append("검증이 본 코드와 지금 코드가 다르거나 확인 자체를 못 했다.")
    lines.append("이 결과를 지금 코드의 판정으로 공개하지 않는다. **이것은 통과가 아니다.**")
    lines.append("")
    atomic_write(latest, "\n".join(lines).encode("utf-8"))
    return None


# --- 판정 확정 --------------------------------------------------------------


@dataclass(frozen=True)
class Finalized:
    freshness: str
    verdict: str
    raw_verdict: str
    verified_fingerprint: str
    reason_code: str
    reason_detail: str = ""
    before_reason: str = ""
    after_reason: str = ""
    evidence_code: str = ""
    evidence_detail: str = ""
    # 검증을 실행하지 않고 연기했는가. 알림과 재호출 예산을 쓰지 않는다.
    is_deferred: bool = False


def finalize(
    before: Fingerprint,
    after: Fingerprint,
    raw_verdict: str,
    evidence_code: str = "",
    evidence_detail: str = "",
) -> Finalized:
    """실행 전후를 비교해 판정을 확정한다.

    전후가 다르면 원시 PASS 를 확정하지 않는다. 검증이 본 코드와 지금 코드가
    다르므로, 그 PASS 는 지금 코드에 대한 주장이 아니다.
    raw_verdict 는 버리지 않고 남긴다 -- 무엇을 봤는지는 사실이다.

    구체적인 원인을 fingerprint_unavailable 로 뭉개지 않는다. git index 를
    못 읽은 것과 감시 대상이 하나도 없는 것은 다른 사건이고, 다르게 고쳐야
    한다. reason_code 에 정규화 코드를 실어 끝까지 나른다.

    우선순위는 before -> after -> stale -> evidence 다. 뒤 단계는 앞 단계가
    성립해야 의미가 있다. 다만 뒤 단계의 원인도 버리지 않고 함께 남긴다.
    """
    def made(freshness: str, verdict: str, fp: str, code: str) -> Finalized:
        return Finalized(
            freshness=freshness,
            verdict=verdict,
            raw_verdict=raw_verdict,
            verified_fingerprint=fp,
            reason_code=code,
            before_reason=before.reason,
            after_reason=after.reason,
            evidence_code=evidence_code,
            evidence_detail=evidence_detail,
        )

    if not before.ok:
        code = f"fp_before_unavailable:{normalize_reason(before.reason)}"
        return made(UNKNOWN, UNVERIFIED, "", code)
    if not after.ok:
        code = f"fp_after_unavailable:{normalize_reason(after.reason)}"
        return made(UNKNOWN, UNVERIFIED, "", code)
    if before.digest != after.digest:
        return made(STALE, UNVERIFIED, "", "changed_during_run")
    if evidence_code:
        # 증빙을 믿을 수 없으면 그 실행의 판정도 믿을 수 없다.
        return made(FRESH, UNVERIFIED, "", f"evidence_invalid:{evidence_code}")
    assert before.digest is not None
    return made(FRESH, raw_verdict, before.digest, "ok")


def deferred(reason_code: str, detail: str = "") -> Finalized:
    """검증을 실행하지 않고 연기했을 때의 결과.

    돌린 뒤 강등하는 것과 다르다. 배경 작업이 도는 중이면 턴이 실제로 끝난
    것이 아니므로, 수십 초를 쓰고 나서 무효 처리할 이유가 없다.
    raw_verdict 는 비어 있다 -- 본 것이 없기 때문이다.

    설명은 reason_detail 에 넣는다. evidence 칸에 적으면 증빙에 문제가 있는
    것처럼 읽혀 원인이 뒤바뀐다.
    """
    return Finalized(
        UNKNOWN, UNVERIFIED, "", "", reason_code, reason_detail=detail, is_deferred=True
    )


def degrade(final: Finalized, reason_code: str, detail: str = "") -> Finalized:
    """확정된 판정을 UNVERIFIED 로 낮춘다. 원시 판정은 남긴다.

    검증 자체는 끝났지만 그 결과를 지금 코드의 판정으로 세울 수 없을 때
    쓴다 -- 증빙을 보관하지 못했거나, 예산이 끝나 일부를 돌리지 못했거나.
    무엇을 봤는지는 사실이므로 raw_verdict 는 그대로 둔다.
    """
    return replace(
        final,
        verdict=UNVERIFIED,
        verified_fingerprint="",
        reason_code=reason_code,
        reason_detail=detail,
    )


def build_state(
    run_id: str,
    before: Fingerprint,
    after: Fingerprint,
    final: Finalized,
    policy_hash: str,
    evidence_path: Path | None,
    previous: HookState | None = None,
    execution_context: str = "",
    session_id: str = "",
) -> HookState:
    """검증 결과를 상태로 옮긴다. 알림 상태는 이전 값을 이어받는다."""
    base = previous or HookState()
    return replace(
        base,
        schema=SCHEMA_VERSION,
        run_id=run_id,
        input_fingerprint=before.digest or "",
        post_fingerprint=after.digest or "",
        verified_fingerprint=final.verified_fingerprint,
        phase=COMPLETED,
        verdict=final.verdict,
        raw_verdict=final.raw_verdict,
        freshness=final.freshness,
        reason_code=final.reason_code,
        reason_detail=final.reason_detail,
        before_reason=final.before_reason,
        after_reason=final.after_reason,
        evidence_code=final.evidence_code,
        evidence_detail=final.evidence_detail,
        # 연기는 확인한 것이 없다. verified_at 을 채우면 그 시각이 거짓이 된다.
        verified_at="" if final.is_deferred else _now(),
        tool_version=__version__,
        policy_hash=policy_hash,
        execution_context=execution_context,
        # 연기는 확인한 것이 없으니 어느 세션의 PASS 도 아니다.
        verified_session_id="" if final.is_deferred else session_id,
        evidence_path=str(evidence_path) if evidence_path else "",
    )


# --- 캐시 재사용 ------------------------------------------------------------


@dataclass(frozen=True)
class SkipDecision:
    skip: bool
    reason: str


def can_skip(
    state: HookState | None,
    current: Fingerprint,
    policy_hash: str,
    state_dir: Path,
    tool_version: str = __version__,
    execution_context: str = "",
    session_id: str = "",
) -> SkipDecision:
    """건너뛸 수 있는 상태는 'fresh + PASS' 하나뿐이다.

    실패·검증 불가·stale 은 파일이 그대로여도 다시 검증한다. 그러지 않으면
    실패가 '변경 없음'이라는 이유로 조용히 사라진다.
    도구 버전·스키마·감시 정책이 바뀌어도 재사용하지 않는다 -- 같은 파일에
    다른 답이 나올 수 있기 때문이다.
    state_dir 는 필수다. 그 run_id 의 증빙이 실제로 있고 유효한지 확인하지
    않고 건너뛰면, 증빙이 아니라 주장을 근거로 검증을 생략하는 것이다.
    """
    if state is None:
        return SkipDecision(False, "이전 상태 없음")
    if state.schema != SCHEMA_VERSION:
        return SkipDecision(False, f"hookstate schema 변경 ({state.schema} -> {SCHEMA_VERSION})")
    if state.tool_version != tool_version:
        # 오류가 아니다. 같은 파일에 다른 답이 나올 수 있으니 재검증할 뿐이다.
        return SkipDecision(
            False,
            f"claimtrail 버전 변경 — 재검증한다 ({state.tool_version} -> {tool_version})",
        )
    if state.policy_hash != policy_hash:
        return SkipDecision(False, "감시 정책 변경")
    # 같은 세션의 반복 Stop 만 빠르게 지나간다. 새 세션의 첫 Stop 은 며칠
    # 전 PASS 를 믿지 않는다.
    if not session_id or state.verified_session_id != session_id:
        return SkipDecision(False, "다른 세션의 PASS — 새 세션은 다시 검증한다")
    # 환경 지문이 없으면 환경이 같은지 모른다. 모르면 검증한다.
    if not execution_context:
        return SkipDecision(False, "실행 환경 지문 없음")
    if state.execution_context != execution_context:
        return SkipDecision(False, "실행 환경 변경")
    if state.phase != COMPLETED:
        # 결론이 없는 실행이다. 무슨 파일이 남아 있든 다시 확인한다.
        return SkipDecision(False, f"이전 실행이 완료되지 않음 (phase={state.phase or '없음'})")
    if not current.ok:
        return SkipDecision(False, "fingerprint 를 계산하지 못함")
    if not current.cacheable:
        # 루트 밖·깨진·디렉터리 링크가 있다. 검증기는 그것을 따라 읽는데
        # 우리는 읽지 않았다. 읽지 않은 것을 근거로 건너뛰지 않는다.
        shown = ", ".join(current.uncacheable[:3])
        return SkipDecision(False, f"캐시 불가 입력 (링크): {shown}")
    if state.freshness != FRESH:
        return SkipDecision(False, f"이전 freshness={state.freshness or '없음'}")
    if state.verdict != PASS:
        return SkipDecision(False, f"이전 판정={state.verdict or '없음'}")
    if not state.verified_fingerprint:
        return SkipDecision(False, "확정된 fingerprint 없음")
    if state.verified_fingerprint != current.digest:
        return SkipDecision(False, "fingerprint 변경")
    if not state.run_id:
        return SkipDecision(False, "run_id 없음")
    check = validate_evidence(run_paths(state_dir, state.run_id)["run_json"])
    if not check.valid:
        return SkipDecision(False, f"증빙이 유효하지 않음 ({check.reason_code})")
    if check.verdict != PASS:
        return SkipDecision(False, f"증빙의 판정={check.verdict}")
    return SkipDecision(True, "fresh + PASS, 동일 fingerprint")


# --- 알림 -------------------------------------------------------------------


def failure_signature(
    input_fingerprint: str,
    verdict: str,
    reason_code: str,
    before_code: str = "",
    after_code: str = "",
    evidence_code: str = "",
) -> str:
    """같은 실패인지 판단하는 열쇠.

    출력 원문을 쓰면 소요 시간이나 임시 경로 때문에 매번 달라져 루프 차단이
    동작하지 않는다. 변하지 않는 값만 쓴다.

    세 단계 원인을 각각 넣는다. before 만 다른 실패와 evidence 만 다른 실패는
    다른 사건이고, 사람에게 다시 알려야 한다.
    """
    parts = (input_fingerprint, verdict, reason_code, before_code, after_code, evidence_code)
    return _sha256("\0".join(parts).encode("utf-8"))[:16]


@dataclass(frozen=True)
class NotifyDecision:
    notify: bool
    reason: str
    rewake_count: int


def should_notify(
    state: HookState | None,
    signature: str,
    session_id: str,
    stop_hook_active: bool,
    max_rewakes: int = MAX_REWAKES,
) -> NotifyDecision:
    """다시 깨울지 정한다.

    요구는 '계속 종료 2'가 아니라 '실패 증빙을 PASS 로 바꾸지 않으면서 무한
    루프를 만들지 않는다'다. 그래서 알릴지 말지만 정하고, 저장되는 판정은
    건드리지 않는다.

    stop_hook_active=False 는 새 연속 호출의 시작이다. 이전 억제 상태를
    이어받지 않는다 -- 이어받으면 한 번 막힌 알림이 영원히 막힌다.
    """
    if not stop_hook_active:
        return NotifyDecision(True, "최초 호출", 1)

    prev = state.rewake_count if state else 0
    same_session = state is not None and state.last_notified_session_id == session_id

    # 다른 세션은 자기 몫의 알림 예산을 가진다. 이전 세션이 상한을 다 썼다는
    # 이유로 새 세션의 첫 알림까지 막으면, 그 세션은 실패를 영영 못 듣는다.
    if not same_session:
        return NotifyDecision(True, "다른 세션", 1)

    if state is not None and state.last_notified_signature == signature:
        return NotifyDecision(False, "동일 fingerprint·동일 실패가 이미 전달됨", prev)
    if prev >= max_rewakes:
        return NotifyDecision(False, f"재알림 상한 {max_rewakes} 도달", prev)
    return NotifyDecision(True, "이전과 다른 실패", prev + 1)


def apply_notification(
    state: HookState,
    decision: NotifyDecision,
    signature: str,
    session_id: str,
) -> HookState:
    """알림 사실만 기록한다. 판정은 절대 건드리지 않는다."""
    if not decision.notify:
        return replace(state, rewake_count=decision.rewake_count)
    return replace(
        state,
        last_notified_session_id=session_id,
        last_notified_signature=signature,
        rewake_count=decision.rewake_count,
    )


# --- Stop 입력 --------------------------------------------------------------


@dataclass(frozen=True)
class StopInput:
    hook_event_name: str = ""
    session_id: str = ""
    cwd: str = ""
    stop_hook_active: bool = False
    # 배경 작업이 돌고 있으면 턴이 실제로 끝난 것이 아니다. 지금 잰 것은
    # 곧 달라질 상태이므로, 수십 초를 쓰고 나서 무효 처리할 이유가 없다.
    background_active: bool = False
    session_crons_active: bool = False
    # 모르는 필드는 파싱하지 않는다. 키 이름만 남겨 실제 스키마를 경험적으로
    # 알아낸다. 값은 남기지 않는다 -- 내용이 로그로 새면 안 된다.
    unknown_keys: tuple[str, ...] = ()
    # 입력 자체를 읽지 못했는가. 읽지 못한 것과 값이 비어 있는 것은 다르다.
    valid: bool = True


def _nonempty_list(value: object) -> bool:
    """리스트이고 비어 있지 않은가. 다른 타입은 없는 것으로 본다."""
    return isinstance(value, list) and len(value) > 0


def parse_stop_input(raw: str) -> StopInput:
    """훅이 stdin 으로 받는 JSON. 깨져 있어도 훅을 죽이지 않는다."""
    try:
        data = json.loads(raw or "")
    except ValueError:
        return StopInput(valid=False)
    if not isinstance(data, dict):
        return StopInput(valid=False)
    known = {
        "hook_event_name",
        "session_id",
        "cwd",
        "stop_hook_active",
        "background_tasks",
        "session_crons",
        "transcript_path",
        # 정상 필드다. unknown 에 넣으면 진짜 모르는 필드가 그 속에 묻힌다.
        "permission_mode",
        "last_assistant_message",
        # 공식 공통 입력. agent_id/agent_type 은 서브에이전트 문맥에서만 온다.
        # scratchpad_dir 는 실제 입력에서 관측됐지만 문서에 없어 unknown 으로 둔다.
        "prompt_id",
        "effort",
        "agent_id",
        "agent_type",
    }
    return StopInput(
        hook_event_name=str(data.get("hook_event_name") or ""),
        session_id=str(data.get("session_id") or ""),
        cwd=str(data.get("cwd") or ""),
        # bool() 로 감싸면 문자열 "false" 가 참이 된다. 그러면 루프 차단이
        # 거꾸로 동작한다 -- 최초 호출을 재호출로 오인한다.
        stop_hook_active=data.get("stop_hook_active") is True,
        background_active=_nonempty_list(data.get("background_tasks")),
        session_crons_active=_nonempty_list(data.get("session_crons")),
        unknown_keys=tuple(sorted(k for k in data if k not in known)),
    )
