# 배포 절차

PyPI 업로드는 **되돌릴 수 없습니다.** 같은 버전을 다시 올릴 수 없고, 삭제해도 그 버전 번호는 영구히 소진됩니다. 아래 순서를 지키세요.

## 0. 사전 조건 (사람이 직접 해야 하는 것)

자동화할 수 없는 단계입니다. CI는 이것들이 끝나 있어야 동작합니다.

1. **PyPI / TestPyPI 계정** — 각각 완전히 별개 계정입니다.

2. **2단계 인증(2FA) 활성화** — 선택이 아닙니다. 2FA를 켜기 전에는 게시 설정
   페이지(`/manage/account/publishing/`)에 접근하면 2FA 설정 화면으로
   리다이렉트됩니다. 인증 앱(TOTP) 또는 보안 장치 중 하나를 등록하세요.

   - **복구 코드를 반드시 저장하세요.** 인증 수단을 잃으면 유일한 복구 방법입니다.
   - **한 번 켜면 끌 수 없습니다.**
   - PyPI 와 TestPyPI 에서 각각 따로 해야 합니다.

3. **Trusted Publisher 등록** — API 토큰을 저장하지 않는 방식입니다. 각 사이트의
   *Publishing* 설정에서 "pending publisher"를 아래 값으로 추가합니다.

   | 항목 | 값 |
   |---|---|
   | PyPI Project Name | `claimtrail` |
   | Owner | `ddayhyun` |
   | Repository name | `claimtrail` |
   | Workflow name | `release.yml` |
   | Environment name | `release` |

4. **GitHub 환경 `release` 생성** — 저장소 Settings → Environments.
   *Required reviewers* 를 걸어두면 업로드 직전에 수동 승인 단계가 생깁니다.
   되돌릴 수 없는 작업이므로 설정을 권장합니다.

## 1. 리허설 (TestPyPI)

실제 배포 전에 반드시 한 번 돌립니다. 파이프라인 전체를 진짜 업로드 없이 검증합니다.

Actions → **Release** → *Run workflow* → 대상 `testpypi`

확인할 것:

- 빌드와 `twine check` 통과
- TestPyPI 페이지에서 README가 깨지지 않고 렌더되는가
- 분류자·링크·라이선스가 의도대로 보이는가

설치까지 확인하려면:

```bash
pip install --index-url https://test.pypi.org/simple/ claimtrail
claimtrail --version
```

## 2. 실제 배포

리허설이 통과한 뒤에만 진행합니다.

```bash
# pyproject.toml 의 version 과 반드시 같아야 한다.
# 다르면 CI가 업로드 전에 실패시킨다.
git tag v0.5.0
git push origin v0.5.0
```

태그를 밀면 Release 워크플로가 PyPI로 업로드합니다. `release` 환경에 검토자를 걸어뒀다면 승인 후 진행됩니다.

## 3. 배포 후

- **README의 설치 안내를 고칩니다.** 지금은 "아직 PyPI에 배포하지 않았습니다"라고
  적혀 있고, 이는 배포 전까지 사실입니다. 배포한 뒤에 `pip install claimtrail` 로
  바꾸세요. 순서를 뒤집으면 README가 거짓말을 하게 됩니다.
- 동명의 무관한 패키지(`claimcheck`)에 대한 경고는 그대로 두는 편이 좋습니다.

## 되돌릴 수 없는 것

| 작업 | 되돌리기 |
|---|---|
| PyPI 업로드 | **불가.** 삭제해도 같은 버전 재업로드 불가 |
| TestPyPI 업로드 | 사실상 불가하지만 실사용에 영향 없음 |
| git 태그 푸시 | 삭제 가능하나, 이미 업로드됐다면 의미 없음 |

## 알려진 제약

- **저장소가 private 인 동안에는** 패키지 메타데이터의 Homepage/Repository/Issues
  링크가 방문자에게 404 로 보입니다. README의 `git clone` 안내도 마찬가지입니다.
  공개 전환을 먼저 하거나, 링크가 깨진 채 배포된다는 점을 감수해야 합니다.
