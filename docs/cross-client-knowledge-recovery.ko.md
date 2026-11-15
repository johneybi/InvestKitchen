# ChatGPT와 Gemini의 Knowledge 등록 결과 확인

계좌 상세 조회와 Knowledge 등록을 두 클라이언트에서 완결하는 첫 출시의 서버
변경이다. 적용 응답이 끊기거나 더 최신 기록이 생겨도 원래 등록 ID와 해시로
결과를 확인한다. 개발 기준은 `main`의 `015541a`와 선행 PR #35·#37의
`30f8c42`다. 이 문서는 제품 구현과 합성 시험 계약을 설명하며 운영 배포나
실제 웹 모델의 수행 성공을 뜻하지 않는다.

## 현재 구현과 변경 범위

| 항목 | 처리 |
| --- | --- |
| 계좌 발견·상세 조회 | 기존 `list_portfolios`, `get_portfolio_state`, `get_account_state` 재사용. 읽기에 쓰기 승인을 요구하지 않음 |
| Knowledge 검증·저장 | 기존 schema, 원문 digest, 기본 버전, 승인 receipt, journal 잠금과 단일 사용 apply 유지 |
| 특정 등록 결과 | 새 `get_knowledge_generation`을 stdio·HTTP에 추가. `knowledge.read`로 보호하고 native store가 있을 때만 노출 |
| 승인 안내 | preview의 `approval_requirement`가 로컬 trusted connector 확인과 HTTP의 별도 브라우저 승인을 구분 |
| 응답 유실·재조회 실패 | preview와 apply에 `recovery` 조회 인자 제공. apply 후 조회 실패가 성공한 commit을 거절로 바꾸지 않음 |
| 공통 지침 | 버전 `2026-11-15.2`. 실제 승인 계약·결과 조회·단순 질문의 짧은 경로를 안내 |

새 범용 분석 엔진, 새로운 재무 공급자, 개인 정책 저장소, 주문 실행, 스케줄러는
추가하지 않는다. 웹 AI는 실제 확보한 자료로 해석과 제안을 만들고 서버는
정본·수치·승인·결과 확인을 제공한다. 기존 프로젝트 전체 규칙 이관은 별도 추적한다.

## 조회 계약

```json
{
  "generation_id": "knowledge-generation:acceptance-note-v1",
  "expected_payload_digest": "<original preview payload_digest>",
  "include_content": true
}
```

`expected_payload_digest`는 선택 필드다. 실제 preview 또는 신뢰된 준비 과정에서
받은 SHA-256만 전달하고 모델이 해시를 추측하지 않는다. 이 예시의 꺾쇠 문자열은
자리표시자이며 실제 호출에는 64자리 소문자 16진수 해시가 필요하다.

| 결과 | 의미와 다음 행동 |
| --- | --- |
| `commit_status=committed`, `expected_payload_matches=true` | 원래 ID·내용의 native commit 확인. 다시 apply하지 않음 |
| `committed`, `expected_payload_matches=false` | 같은 ID의 내용 불일치. 충돌을 대조하고 자동 재등록 금지 |
| `not_observed` | 조회 시점에 native journal에 없음. 진행 중 쓰기나 다른 저장소까지 실패했다는 증거가 아니므로 결과를 재확인 |
| `unknown`, `status=error` | 잠금·저장소·검증 오류 등으로 판단 불가. 원본 오류/경로를 노출하지 않으며 재적용 금지 |
| `is_latest=false` | 후속 기록이 존재하지만 조회한 원래 commit은 유지됨. 최신 뷰로 원래 등록 성공을 판단하지 않음 |

모든 응답의 `retry_apply_safe`는 false다. 조회 자체가 권한이나 새 승인을 만들지
않는다. 이 도구는 native Knowledge 세대의 정본 관찰이며 모든 도메인의 작업 상태
조회나 자동 재시도 서비스가 아니다. 미확인 결과는 운영자가 해당 작업을 대조할 때까지
미확인으로 유지한다. 새로운 후보가 필요하면 변경된 최신 기준과 새 승인이 필요하다.

`include_content=false`는 메타데이터만 반환한다. true일 때도 한 요청의 정본 내용은
65,536 UTF-8 bytes 이내만 포함한다. 초과하면 `omitted_size_limit`과 null content를
반환하며 묵시적으로 일부를 전체라고 보여 주지 않는다. 페이지 분할은 아직 없다.
이 한도는 네트워크 처리 시간이나 전체 응답 크기의 실측 보장이 아니다.

원시 journal·승인 receipt·내부 감사 필드는 반환하지 않는다. 공개용 참조 변환이
발생하면 `content_status=public_projection`과 별도 `content_digest`로 표시하고
`content_matches_committed_payload=false`를 반환한다. 로컬 파일 경로나 민감한
구조 필드가 남은 내용은 `withheld_private_locator`로 생략한다. 정본의 저장 해시와
클라이언트에 반환한 내용의 해시는 서로 다른 검증이다.

`verification.commit_and_payload_digest`는 저장 구조·digest 검사만 의미한다.
사용자 입력의 verified 표시는 유지되지만 `source_truth_verified`와
`semantic_fidelity_verified`는 false다. 사용자 저장 승인도 투자 주장의 사실성이나
AI가 원문을 모두 읽었다는 증거가 아니다.

## 승인과 재조회

로컬 preview는 `trusted_connector_confirmation`, HTTP preview는
`separate_authenticated_browser_review`를 반환한다. HTTP만 실제 `approval_url`을
추가하며 기존 cookie 인증·동일 origin·CSRF·subject/client 바인딩을 유지한다.
두 경로 모두 `model_may_approve=false`다. 승인 절차를 통과한 기존 apply만 사용하고
응답의 안내 문자열이 새 승인 증거가 되지는 않는다. 로컬 경로의 trusted connector
경계와 원격 브라우저 경계를 서로 대체하거나 원격에 로컬 apply를 직접 노출하지 않는다.

preview에 포함된 `recovery.tool`과 `recovery.arguments`는 원래 generation ID와
digest를 보존한다. 저장 후 읽기가 실패하면 apply는 `status=applied`와
`read_back.status=unavailable`, `matches=null`을 반환한다. 저장 사실과 재조회
미확인을 나눠 알리고 같은 apply를 재전송하지 않는다. 다른 AI는 자기 연결의 유효한
읽기 권한으로 같은 ID를 조회하며 승인 receipt를 공유하지 않는다.

## 검증과 첫 출시 절차

`protocol/v1/tests/test_knowledge_recovery.py`는 다음을 합성 자료로 검증한다.

- 정확한 원문·빈 객체 보존, 이후 기록·새 읽기 인스턴스에서 원래 ID 재조회
- object/JSON-string HTTP 입력, 브라우저 승인 전 거절, 저장 직후 응답 유실과 단일 commit
- HTTP 저장을 stdio에서 재조회하고 같은 대화에 해당하는 연속 읽기 10회
- 쓰기 grant 만료 뒤에도 허용된 읽기는 가능하며 쓰기는 차단
- 두 commit 사이에 재조회가 끼어도 원래 저장의 일치를 확인
- 성공한 commit 후 조회 실패, 미관측·해시 충돌·잠금/손상 오류의 구분
- 읽기 권한·backend 없는 도구 발견 차단, 크기 제한·비공개 참조 생략

기존 승인·만료·변조·동시성·재시작 시험도 함께 실행한다. 자동 시험의 HTTP/stdio
성공은 실제 ChatGPT/Gemini의 도구 선택·추론·다른 PC 사용 시험이 아니다.

첫 운영 수용 시험은 명시적으로 앱을 지정한 대표 모델/모드 하나씩을 기록한 후
진행한다. 조합별 새 채팅 3회(그중 다른 PC 1회), 연속 읽기 10회, 인증 갱신,
승인·응답 유실·충돌의 지정 변형을 실행한다. 기대 권한 거절은 정확한 안내까지
확인하며 정상 경로에서 반복 401이나 수동 재연결 의존이 있으면 불합격이다.
불필요한 질문·중복 호출·수동 복구와 첫 유용한 답변/완료 시간을 측정하고 승인
대기 시간은 분리한다. 치명적 오류를 평균 성공률로 상쇄하지 않는다.

착수 전 운영자가 배포 source와 노출 도구·입력 profile·유효 grant·승인 경로를
확인한다. 만료된 grant는 이 기능 배포만으로 갱신하지 않는다. 이후 공급자와 예약
분석 실행 주체 결정은 첫 출시를 막지 않는다.

## 배포와 복구

소스와 설정 변경은 식별된 커밋/이미지로 사전 검증하고 각 클라이언트의 기존 경로를
보존해 전환한다. 이번 변경은 journal 형식을 바꾸지 않고 새로운 읽기와 응답 필드를
추가한다. 이전 바이너리로 복구할 때도 새로 저장된 정상 기록을 과거 데이터 백업으로
덮어쓰지 않는다. 원문 조회 한도와 생략 상태는 사용자에게 명시한다.

구현·로컬 시험·CI·배포·실제 웹 모델 시험·운영 검증의 상태는 각각 기록한다.
PR 병합이나 단위 시험 개수만으로 양쪽 제품 지원 완료라고 표시하지 않는다.

관련 작업: #38, 선행 PR #35·#37.
