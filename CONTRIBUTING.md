# InvestKitchen 작업 및 GitHub 운영 규칙

이 저장소는 **배포 가능한 제품의 기준 소스**다. GitHub의 공개 범위를 먼저 확인하고,
이슈·커밋·PR·Project에 실제 계좌 정보, 개인 판단 기록, credential, NAS 내부 경로,
원본 로그나 백업을 넣지 않는다. 예시는 합성 데이터로 작성한다.

## 저장소 역할과 변경 경로

| 위치 | 역할 | 변경 원칙 |
| --- | --- | --- |
| 이 Git 저장소 / GitHub | 제품 코드, 계약, 테스트, 문서, 배포 도구의 기준 | 리뷰 가능한 브랜치와 PR로 변경 |
| Synology 개인 개발 작업 공간 | 개인 실험, 메모, 실제 입력을 다루는 사적 작업 | 운영 디렉터리와 분리하고 GitHub 제품 원격에 연결하지 않음 |
| Synology `runtime-repo/` | 검증된 제품 소스의 배포용 checkout | 지정한 commit으로만 갱신; 임시 개발·개인 기록 금지 |
| Synology `runtime-data/`, `secrets/`, backups | 운영 상태·개인 데이터·비밀정보 | 제품 Git에 추가하지 않음; 배포 소스와 별도 보관 |

Synology 개인 작업 공간에 Git을 쓰더라도 별도 사적 저장소로 취급한다. 재사용할
코드나 문서는 제품 브랜치에서 다시 정리하고 개인 데이터와 호스트별 값을 제거한 뒤
합성 fixture, 필요한 테스트, 설명을 함께 제출한다. 개인 작업 공간을 제품 저장소에
통째로 동기화하거나 운영 checkout에서 직접 수정한 파일을 그대로 배포하지 않는다.
운영 중 긴급 수정도 변경 내용을 이 저장소의 브랜치·PR로 되돌려 기준 소스와 일치시킨다.

배포는 `deploy/synology/README.md`의 정확한 commit/image 검증, 사전 점검,
cutover, rollback 절차를 따른다. `main`에 merge되거나 이슈가 닫힌 것만으로
Synology 배포 완료 또는 운영 검증 완료라고 표시하지 않는다. `trademind-*` 경로·
환경 변수·이미지명은 현재 호환성 식별자이며, 별도 이행 계획 없이 변경하지 않는다.

## 이슈와 Project

- 기능, 버그, 운영·문서 작업 중 범위가 있는 변경은 이슈로 시작한다. 오타처럼
  범위가 작은 수정은 PR 본문만으로 기록할 수 있다.
- 이슈에는 문제와 기대 결과, 범위 밖의 일, 완료 조건, 검증 방법을 적는다. 운영
  작업은 사전 점검·rollback·운영 확인 방법도 적는다. 민감한 재현 자료는 올리지 않는다.
- GitHub Project를 사용할 때 이슈와 PR을 연결하고 `Backlog → Ready → In progress
  → In review → Done` 상태를 쓴다. 시작할 때 담당자를 정하고, 막히면 이슈에
  차단 사유를 적는다. Project가 아직 없다면 이 상태 규칙을 첫 Project에 적용한다.
- `Done`은 이슈의 완료 조건이 충족되고 관련 PR이 merge된 상태다. 배포/운영
  검증이 완료 조건이라면 그 결과를 확인한 뒤 닫는다. 미완료 후속 작업은 새 이슈로 남긴다.

## 브랜치, 커밋, PR

1. `main`의 최신 제품 상태에서 작업 브랜치를 만든다. 이름은
   `feat/<issue>-short-topic`, `fix/<issue>-short-topic`, `docs/<issue>-short-topic`,
   `chore/<issue>-short-topic` 형식을 쓴다. 이슈가 없는 작은 수정은 번호를 생략한다.
2. 커밋은 한 가지 의도로 묶고 `feat:`, `fix:`, `docs:`, `test:`, `refactor:`,
   `chore:` 같은 짧은 명령형 제목을 사용한다. 관련 이슈 번호는 커밋 본문이나 PR에
   적는다. 생성 데이터·비밀정보·운영 산출물은 stage하지 않는다.
3. PR에는 이슈 링크, 변경 내용, 검증 결과, 위험과 rollback, 배포 필요 여부를
   적는다. `git diff --cached`와 `git status`로 실제 제출 내용을 확인한다.
   코드 변경은 관련 테스트를 실행한다. 기본 전체 검증은
   `python3 -m pytest protocol/v1/tests -q`다.
4. 작성자가 diff와 검증 결과를 다시 확인하고 필요한 리뷰를 받은 뒤 `main`에
   merge한다. 브랜치 보호·필수 리뷰·필수 체크는 GitHub 저장소 설정에서 켜야
   실제 강제된다. 규칙 문서만으로 GitHub 설정이 적용되지는 않는다.

GitHub에서 관리 권한이 있을 때 `main` Ruleset에 PR을 통한 merge와 직접 push
차단을 설정한다. 리뷰가 가능한 운영 방식이면 필수 리뷰도 켠다. CI가 추가되면
안정적으로 통과하는 검증만 필수 체크로 지정한다. merge 후 작업 브랜치 자동 삭제를
켜고, Project의 상태 필드를 위 흐름에 맞춰 만든다. 이 저장소의 문서와 양식은
이 설정을 대신 적용하지 않는다.

## 배포와 상태 기록

PR과 이슈에 `planned`, `implemented`, `tested`, `deployed`,
`operationally verified`를 구별해 적는다. 배포 이슈에는 소스 commit, 이미지 식별자,
사전 점검 결과, cutover 결과, rollback 수단, 운영 확인 결과를 **비밀정보 없이** 남긴다.
현재 실제 제품 상태는 배포 이슈와 운영 문서의 확인 결과에 맞춘다.
