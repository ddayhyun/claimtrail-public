---
name: claimtrail-derive
description: Claimtrail 자동 도출이 켜진 프로젝트에서 코드 변경이나 검사 요청을 받았을 때, 이번 작업에서 확인해야 할 동작을 근거와 함께 도출하고 부족한 검사를 독립 단위 테스트로 작성해 `claimtrail derive submit` 으로 제출한다. UserPromptSubmit 훅이 "[claimtrail] 이 프로젝트는 자동 도출이 켜져 있다" 로 시작하는 문맥을 넣었을 때 사용한다. 설명만 하는 대화에는 not-applicable 제출로 답한다.
---

# claimtrail-derive — 확인 항목 도출과 제출

훅이 준 `session_id`·`prompt_id`·상태 폴더·제출 명령을 그대로 쓴다. 지어내지 않는다.
제출은 **접수와 형식 확인**이다. 통과 판정은 Stop 훅이 실제 실행 증거로 한다.

## 1. 언제

- 코드 변경 요청, "현재 상태를 확인해줘" 같은 검사 요청 → 도출·제출.
- 설명·질문만 있는 대화 → `--not-applicable --reason "<이유>"` 로 제출.
- 판단이 어려우면 도출한다. 미제출은 증빙에 "자동 도출: 미수행"으로 남는다.

## 2. 무엇을 읽는가

1. 요청 문장과 이번에 바꾼 파일(diff).
2. README·문서에서 그 동작에 대한 요구사항 문장.
3. 기존 테스트의 **assertion**(이름만 보지 않는다). 무엇을 어떤 기대값과 대조하는지 읽는다.

## 3. 항목 도출 규칙

- 정상·경계·오류·호환성 조건 중 이번 변경과 관련 있는 것만 고른다.
- 항목마다: `behavior`(확인할 동작), `why`(필요한 이유), `basis`(근거: `파일:줄` 또는 README 문장), `how`.
- `how` 는 셋 중 하나:
  - `{"existing": ["tests/test_x.py::test_name", ...]}` — 기존 테스트가 그 기대 동작을 실제로 단언할 때만.
    `existing` 은 이번 훅 검사 범위(`claimtrail.json` 의 `pytest.paths`)에 포함된 테스트를 연결한다.
    범위 밖 테스트는 미수집으로 남으며, 범위 확대 또는 별도 검증 필요성을 보고한다.
    생성 검사는 대상 fixture 없이 독립 실행할 수 있는 경우에 사용한다.
  - `{"generated": "test_derived_<작업>.py::test_name"}` — 부족한 검사를 새로 썼을 때.
  - `{"none": "<실행하지 않는 이유>"}` — 질문·fixture 필요 등.
- `kind`:
  - `requirement` — 요구사항 근거가 있음(basis 필수).
  - `characterization` — 요구사항 근거 없이 현재 동작을 그대로 적음. 통과해도 "옳다"는 뜻이 아니다.
  - `question` — 기대 동작을 정할 근거가 부족. 입력·실제 출력을 `why` 에 적고 사용자에게 질문한다.
  - `needs_fixture` — 대상 `conftest.py` 의 fixture 나 DB·외부 서비스가 필요. 첫 구현에서는 실행하지 않는다.
- 함수를 호출만 하고 결과를 대조하지 않는 테스트는 기대 동작 확인이 아니다. 반환값·예외·상태 변화를 단언해야 한다.

## 4. 생성 테스트 규칙

- 원본 저장소 안에 파일을 만들지 않는다. 임시 위치(훅이 알려 준 scratchpad 또는 상태 폴더 `tmp/`)에 쓰고 `--generated` 로 넘긴다.
- **독립 단위 검사**만: 대상 패키지를 import 해 순수 함수·메서드를 직접 부른다. 대상 `conftest.py` 의 fixture, DB, 네트워크, 파일 쓰기는 쓰지 않는다(그런 항목은 `needs_fixture`).
- 파일 이름은 `test_derived_*.py`, 테스트 이름은 파일 안에서 유일하게.
- 생성 테스트를 통과시키려고 assertion 을 약화하거나 대상 코드를 고치지 않는다.
- 전체 테스트 스위트를 직접 돌리지 않는다. Stop 훅이 기존 검사와 생성 검사를 실행한다.

## 5. 제출

목록 JSON:

```json
{
  "schema": 1,
  "status": "performed",
  "request": "<요청 한 줄 요약>",
  "items": [
    {"id": "D1", "kind": "requirement", "behavior": "...", "why": "...", "basis": "README.md:12",
     "how": {"generated": "test_derived_login.py::test_locked_rejected"}},
    {"id": "D2", "kind": "requirement", "behavior": "...", "why": "...", "basis": "tests/test_auth.py:5",
     "how": {"existing": ["tests/test_auth.py::test_ok"]}},
    {"id": "D3", "kind": "question", "behavior": "...", "why": "근거 없음: 입력 X 에 실제 출력 Y", "basis": "",
     "how": {"none": "사용자 확인 필요"}}
  ]
}
```

훅이 준 명령 그대로:

```bash
claimtrail derive submit <대상 루트> --session-id <id> --prompt-id <id> --file <목록.json> --generated <생성 테스트.py>
```

제출 결과의 `status`·`derive_digest` 를 답에 한 줄로 적는다. 제출이 거부되면(형식 오류·참조 파일 없음) 고쳐서 다시 제출한다.

## 6. 답변에 적을 것

- 도출 항목 수와 그중 생성 검사·질문 수.
- 질문 항목은 사용자에게 그대로 묻는다.
- "확인했다"고 쓰지 않는다. 확인은 Stop 훅 증빙(`evidence.md` 의 "## 자동 도출" 절)이 한다.
