# 메모리가 작은 호스트의 Gemini 지연 완화

관련 이슈 #52. 현재 상태는 보조 도구 구현·합성 시험이며 운영 적용과 웹 검증은 별도다.
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
