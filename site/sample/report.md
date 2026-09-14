# 검증 증빙

- 대상: `/home/runner/work/claimtrail/claimtrail/examples/sample-project`
- 실행 시각: 2026-09-10T07:43:08+00:00
- 도구: claimtrail 0.5.1

## 판정 — 실패

- 검증 범위 설정: `/home/runner/work/claimtrail/claimtrail/examples/claimtrail.json`
- 필수 검사: pytest 통과, lint 통과, format 실패
- pytest 범위: `tests`

## 실행 환경

- Python: `/home/runner/work/_temp/ct-venv/bin/python` (3.12.14)
- Claimtrail 소스: `/home/runner/work/_temp/ct-venv/lib/python3.12/site-packages/claimtrail`
- 검증기: pytest 9.1.1 · ruff 0.16.6 · flake8 미설치 · mypy 확인하지 않음 · build 확인하지 않음

## 확인한 것

| 검증 | 결과 | 숫자 | 보고 시간 | 벽시계 |
|---|---|---|---|---|
| pytest | 통과 | 5개 중 5 통과 | 0.02s | 0.52s |
| lint | 통과 | 위반 없음 | 0.03s | 0.03s |
| format | 실패 | 형식 차이 1개 파일 | 0.03s | 0.03s |

### 실행한 명령 — pytest

```
/home/runner/work/_temp/ct-venv/bin/python -m pytest --junit-xml /tmp/tmptasgah3i/junit.xml -q tests
```

종료 코드: `0`

작업 폴더: `/home/runner/work/claimtrail/claimtrail/examples/sample-project`

### 실행한 명령 — lint

```
/home/runner/work/_temp/ct-venv/bin/python -m ruff check --output-format=json .
```

종료 코드: `0`

### 실행한 명령 — format

```
/home/runner/work/_temp/ct-venv/bin/python -m ruff format --check .
```

종료 코드: `1`

작업 폴더: `/home/runner/work/claimtrail/claimtrail/examples/sample-project`

## 실패한 항목

- `sample_calc/calc.py`
  - ruff format 결과와 다르다

## 확인하지 못한 것

- **type-check** — pyproject.toml/mypy.ini/setup.cfg/pyrightconfig.json 어디에도 mypy 또는 pyright 설정이 없음
- **build** — pyproject.toml에 [build-system]이 없고 setup.py도 없어 빌드할 패키지로 볼 근거가 없음
- **npm test** — package.json이 없어 Node 프로젝트로 볼 근거가 없음

## 탐지 근거

- pytest: 탐지됨 (2개 근거)
  - pyproject.toml: [tool.pytest.ini_options]
  - tests/ 아래 test_*.py 또는 *_test.py 1개
- lint: 탐지됨 (1개 근거)
  - pyproject.toml: [tool.ruff]
- type-check: 탐지되지 않음 — pyproject.toml/mypy.ini/setup.cfg/pyrightconfig.json 어디에도 mypy 또는 pyright 설정이 없음
- build: 탐지되지 않음 — pyproject.toml에 [build-system]이 없고 setup.py도 없어 빌드할 패키지로 볼 근거가 없음
- npm test: 탐지되지 않음 — package.json이 없어 Node 프로젝트로 볼 근거가 없음
- format: 탐지됨 (2개 근거)
  - claimtrail.json: format.tool = ruff
  - pyproject.toml: [tool.ruff]

---

이 리포트는 위에 적힌 명령을 **실제로 실행한** 결과다. 여기 적히지 않은 것은 확인되지 않았다.
