# Claimtrail 공개 샘플 프로젝트

외부 API·DB·자격증명이 필요 없는 작은 Python 패키지다. Claimtrail 을 처음 돌려 보는 용도로 만들었다.

설계된 결과 (설정 `../claimtrail.json` 으로 실행했을 때):

| 검사 | 결과 | 이유 |
|---|---|---|
| pytest | 통과 (5개) | `tests/test_calc.py` |
| lint | 통과 | `ruff check` 위반 없음 |
| format | **실패 (1파일)** | `sample_calc/calc.py` 가 일부러 `ruff format` 기준에 안 맞게 적혀 있다 |
| type-check · build · npm test | 확인하지 못함 | mypy·빌드·package.json 설정이 없어 탐지되지 않는다. 숨기지 않고 리포트에 남는다 |
| 전체 판정 | **실패, 종료 코드 1** | 필수 검사 중 format 이 실패 |

고쳐 보기: 이 폴더에서 `ruff format .` 을 실행한 뒤 다시 검사하면 판정이 통과(종료 코드 0)로 바뀐다. 그때의 통과는 **설정에 적힌 세 검사(pytest·lint·format)에 대한 통과**이고, 확인하지 못한 항목은 여전히 리포트에 남는다.

실행 방법은 저장소 루트의 `docs/getting-started.md` 를 본다. 설정 파일과 리포트는 이 폴더 **밖**에 둔다(설정은 `examples/claimtrail.json`, 리포트는 아무 곳이나 이 폴더 밖).
