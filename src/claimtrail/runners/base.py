"""러너들이 공유하는 결과 타입.

러너가 늘어나도 서로를 import 하지 않도록 공용 타입은 여기에 모은다.
pytest 러너 안에 두면 새 러너가 pytest 러너에 의존하게 되는데,
그건 의존 방향이 거꾸로다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..redact import redact

PASS = "pass"
FAIL = "fail"
UNVERIFIED = "unverified"

DEFAULT_TIMEOUT = 900

# 기계가 읽는 원인 코드. note 는 사람이 읽는 글이라 문구를 다듬는
# 순간 파싱이 깨진다. 판단은 이 코드로 한다.
RUN_TIMEOUT = "run_timeout"
# pytest 가 테스트를 돌리기 전, 수집 단계에서 죽었다. 결과 파일은 나오지만
# 거기 적힌 항목은 테스트가 아니라 '어느 파일을 import 하지 못했다' 다.
COLLECTION_ERROR = "collection_error"


@dataclass
class Failure:
    """실패한 항목 하나. 테스트 실패일 수도, lint 위반일 수도 있다."""

    test: str
    message: str


@dataclass
class RunResult:
    """하나의 검증 실행 결과. '못 돌았음'과 '돌았는데 실패'를 구분한다."""

    kind: str
    status: str
    command: list[str] = field(default_factory=list)
    exit_code: int | None = None
    total: int | None = None
    passed: int | None = None
    failed: int | None = None
    errors: int | None = None
    skipped: int | None = None
    duration_sec: float | None = None
    # 프로세스 벽시계. pytest 는 duration_sec 이 JUnit 보고 시간(수집·기동 제외)이라
    # 둘이 다르다. 다른 러너는 같은 값이다.
    wall_sec: float | None = None
    failures: list[Failure] = field(default_factory=list)
    note: str = ""
    # 기계가 읽는 원인. 비어 있으면 특별한 사유가 없다는 뜻이다.
    reason_code: str = ""
    # 테스트 개수 형식('N개 중 M 통과')으로 표현할 수 없는 러너가 쓴다.
    # 예: lint 는 '위반 3건', build 는 산출물 이름. 비어 있으면 개수 형식을 쓴다.
    summary: str = ""
    # 명령을 실행한 작업 폴더. 같은 명령도 어디서 돌렸느냐에 따라 수집 범위가
    # 달라진다. 비어 있으면 리포트에 적지 않는다.
    cwd: str = ""
    # pytest 전용. "collection" 이면 위 total/errors 는 수집 오류 항목을 센 값이지
    # 테스트를 돌린 결과가 아니다. 비어 있으면 그런 구분이 필요 없는 러너다.
    phase: str = ""
    # pytest 전용. JUnit testcase 개수에서 수집 오류 항목과 건너뛴(skipped) 항목을 뺀
    # 값 -- 실제로 돌았다고 확인되는 테스트 수. None 은 '셀 근거가 없다' 이지 0 이 아니다.
    tests_ran: int | None = None

    @property
    def command_str(self) -> str:
        return " ".join(self.command)


def truncate(text: str, limit: int = 300) -> str:
    """외부 출력을 리포트용 한 줄로 줄인다. 자격증명 가림이 먼저다 -- 먼저 자르면
    잘린 조각이 패턴에 안 맞아 비밀값의 앞부분이 남는다."""
    text = redact(" ".join((text or "").split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"
