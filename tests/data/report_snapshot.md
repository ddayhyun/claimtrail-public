# 검증 증빙

- 대상: `fixture-root`
- 실행 시각: 2026-01-01T00:00:00+09:00
- 도구: claimtrail 0.5.1

## 판정 — 실패

## 확인한 것

| 검증 | 결과 | 숫자 | 보고 시간 | 벽시계 |
|---|---|---|---|---|
| pytest | 통과 | 12개 중 10 통과, 2 건너뜀 | 1.5s | - |
| lint | 실패 | 위반 1건 | 0.2s | - |
| build | 통과 | 산출물 2개 (wheel, sdist) | 3.0s | - |

### 실행한 명령 — pytest

```
python -m pytest -q
```

종료 코드: `0`

### 실행한 명령 — lint

```
python -m ruff check .
```

종료 코드: `1`

### 실행한 명령 — build

```
python -m build
```

종료 코드: `0`

## 실패한 항목

- `src/a.py:1:8`
  - F401 `os` imported but unused

## 확인하지 못한 것

- **type-check** — 이 환경에 mypy가 설치되어 있지 않다. 설치 후 다시 실행해야 한다.
- **npm test** — package.json이 없어 Node 프로젝트로 볼 근거가 없음

## 탐지 근거

- pytest: 탐지됨 (2개 근거)
  - pyproject.toml: [tool.pytest.ini_options]
  - tests/ 아래 test_*.py 6개
- lint: 탐지됨 (1개 근거)
  - pyproject.toml: [tool.ruff]
- type-check: 탐지됨 (1개 근거)
  - pyproject.toml: [tool.mypy]
- build: 탐지됨 (1개 근거)
  - pyproject.toml: [build-system] (backend: hatchling.build)
- npm test: 탐지되지 않음 — package.json이 없어 Node 프로젝트로 볼 근거가 없음

---

이 리포트는 위에 적힌 명령을 **실제로 실행한** 결과다. 여기 적히지 않은 것은 확인되지 않았다.
