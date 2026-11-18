# 메모리가 작은 호스트의 Gemini 지연 완화

관련 이슈 #52. 보조 도구와 조회 최적화는 운영 활성화 후 실제 실행 소스·프로세스·보호 설정·자원 값을
확인했다. Gemini의 토큰 갱신 이후 반복 성공은 아직 수락하지 않았다.
인증 성공 뒤 Knowledge 등록이 약 84초 걸리고 디스크 busy 100%, 스왑 입출력과 높은
load가 함께 관측됐다. 같은 구간 SSH 연결도 지연됐다. 이것으로 모든 401이나 모델의
도구 미호출을 설명하지 않는다. 실제 인증·업무 실행·응답 전달을 각각 확인한다.

## cgroup v1의 한정된 자원 선호

`deploy/gemini/service_resources.py`는 legacy systemd/cgroup v1 호스트의 기존 두
Gemini 서비스에만 다음 reclaim 선호를 적용한다.

| 대상 | memory.swappiness | memory.soft_limit_in_bytes |
| --- | --- | --- |
| Gemini MCP | 0 | 128 MiB |
| 기존 Gemini 인증 서비스 | 0 | 96 MiB |

기존 hard memory limit은 읽어 보호하고 바꾸지 않는다. 사용자·포트폴리오·OAuth scope,
검증·회전·만료·credential, GPT, 다른 서비스, 전역 vm.swappiness, swap 장치는 바꾸지
않는다. soft limit은 RAM 예약이나 latency 보장이 아니다. cgroup v1 swappiness=0도
global reclaim 상황의 모든 swap을 금지하지 않는다.
[Linux cgroup-v1 메모리 문서](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v1/memory.html)

보조 도구는 실제 프로세스의 memory cgroup과 systemd의 서비스 cgroup이 일치하는지
확인한다. 지원되는 두 control 파일만 쓰고, 모든 대상·hard limit·선호 값의 drift를
쓰기 전에 검사한다. 부분 실패 시 이미 건드린 값을 역순으로 복원한다. 상태는 root 소유
0700 디렉터리의 0600 파일에 먼저 fsync해 보관한다. 서비스 재시작 때도 원래 baseline을
덮어쓰지 않는다. rollback은 저장된 선호를 복원하며 사용자 데이터 백업을 되돌리지 않는다.

운영자는 고정 소스와 보호된 baseline을 준비하고 root oneshot으로 적용한다. 재시작·부팅
뒤 cgroup이 다시 만들어지는 호스트에서는 같은 검증된 도구를 서비스 의존성에 연결하고
실제 적용값을 확인한다. 지원 여부를 확인하지 않고 최신 systemd의 MemorySwapMax나
MemoryLow를 legacy 버전에 넣지 않는다. 이 도구 자체는 서비스를 중지·재시작하지 않는다.

적용 뒤 서비스의 실제 RSS/swap·major fault 변화, disk busy와 응답 시간, 실제 토큰
갱신 이후 자연어 조회·등록·원문 재조회와 병렬 읽기를 대조한다. GPT 웹 회귀와 기존
인증 거부도 확인한다. 불확실한 상태에서 hard limit을 늘리거나 인증 캐시로 대체하지 않는다.

## 활성화 뒤 확인 범위 (2026-11-18)

제품 소스 `80251d3`의 관련 네 파일을 기존 운영 소스에 한정해 반영했다. 제품 시험
753개 통과·선택적 연동 8개 생략, 비공개 전환/부분 실패/복구 시험 13개 통과와
NAS 사전 검증 뒤 관리자가 활성화했다. 실행 인자와 소스 해시, 두 서비스의 실제
cgroup 선호, 기존 hard limit, 보호된 인증 설정과 기존 GPT health를 확인했다.
수정 소스 CI 두 건도 통과했다. 관리자 소유 활성화 영수증 본문은 일반 계정에서
읽을 수 없어, 사용자 전달 성공 출력과 실제 실행 상태를 별도로 대조했다.

기존 합성 노트는 배포된 코드의 읽기 전용 검사에서 원문·digest·빈 current_state가
일치했고, 제목 검색 결과에도 해당 generation이 포함됐다. cold 원문 조회는 약
11초, 같은 프로세스의 후속 조회는 0.4/0.3ms, 제목 검색은 약 465ms였다. 별도
진단 프로세스의 결과이므로 상주 서버 성능이나 Gemini 응답 시간으로 일반화하지 않는다.

새 Gemini 채팅은 갱신 요청에서 400을 받았고 업무 도구까지 진행하지 못했다.
현재 연결이 사용한 최신 OAuth 계열의 refresh는 전부 비활성, access는 전부
철회된 상태였다. 다른 과거 연결의 활성 refresh가 남아 있다는 사실은 해당 연결의
복구 근거가 아니다. 자원 설정으로 철회된 인증을 되살리지 않는다. 재인증 금지 조건과
실제 갱신 검증의 충돌은 사용자에게 알리고, 새 승인 없이 재연결하거나 DB의 인증
상태를 수정하지 않는다. 실제 신규 등록·별도 원문 읽기·수명 경과 후 반복 검증은
아직 성공하지 않았다.
