# InvestKitchen architecture

이 문서는 InvestKitchen의 **제품 책임 경계와 target architecture**를 설명한다.

## 1. 가장 중요한 경계

> **ChatGPT는 이해하고, 조사하고, 추론하고, 설명한다. InvestKitchen은 기억하고, 증명하고, 계산하고, 상태를 유지하고, 권한을 통제하고, 계속 실행한다.**

이 경계 때문에 ChatGPT 대화 기억이나 한 번의 답변이 Portfolio의 권위 있는 상태가 될 수 없다. 개인 투자 상태는 Runtime의 명시적 데이터에서만 가져온다.

## 2. 전체 구조

```text
┌─────────────────────────────────────────────────────┐
│ ChatGPT                                              │
│                                                     │
│ 자연어 의도 · 공개 웹 조사 · 비교 · 추론 · 설명      │
│ PRE/POST 질문 · Strategy Board 표현 · 사용자 대화     │
└───────────────────────┬─────────────────────────────┘
                        │ MCP / Secure MCP Tunnel
                        ▼
┌─────────────────────────────────────────────────────┐
│ InvestKitchen Runtime — Synology                    │
│                                                     │
│ Gateway / Capability Registry / Context Composer    │
│       │                                             │
│       ├─ Portfolio / Policy                         │
│       ├─ Knowledge / Evidence / Claims              │
│       ├─ Market / Macro Providers                   │
│       ├─ Decision / Transaction History             │
│       ├─ Reflection                                 │
│       ├─ Approval / Authorization                    │
│       └─ Automation / Monitor / Paper   [future]    │
│                                                     │
│ Persistent personal state · secrets · backups       │
└─────────────────────────────────────────────────────┘
```

Mac은 개발·관리용일 뿐 운영 의존성이 아니다. Synology Runtime과 Tunnel이 살아 있으면 Mac이 꺼져 있어도 ChatGPT가 InvestKitchen에 접근할 수 있는 구조를 목표로 하며, 현재 Synology 단독 연결까지 운영 검증했다.

## 3. 제품을 움직이는 세 개의 Loop

### Knowledge Loop

```text
Source
  → Evidence
  → Claim Candidate
  → 검토 / provenance / freshness
  → Canonical Knowledge
```

시장 데이터, 전문가 발언, 공식 자료, 사용자 제공 자료를 단순 텍스트 더미로 취급하지 않는다. 무엇이 누구에게서 언제 나왔는지, 직접 발언인지 요약인지, 현재도 적용 가능한지를 분리한다.

`canonical`은 “절대 진실”이라는 뜻이 아니라 **InvestKitchen이 현재 사용할 수 있도록 등록된 지식 상태**를 뜻한다.

### Decision Loop

```text
User situation
  → DecisionRequest
  → DecisionContext
  → Assessment / Advice
  → 사용자 확인
  → Decision
  → Transaction
  → Outcome / Reflection
```

중요한 구분:

- Advice는 사용자의 Decision이 아니다.
- 사용자의 Decision은 실제 거래 발생과 다르다.
- 거래 발생 증거는 canonical state write 승인과 다르다.
- Strategy Board는 조건과 무효화까지 포함한 표현 계층이지 자동 주문이 아니다.

### Automation Loop

```text
Scheduler / Collector / Monitor / Paper
  → Event
  → Notification
  → User
  → fresh Decision Loop
```

Automation은 매 tick마다 LLM을 호출하지 않는다. deterministic 조건 감시는 Runtime이 계속 실행하고, 의미 있는 이벤트가 생겼을 때 사람과 ChatGPT의 판단 루프로 돌아온다.

## 4. Domain objects

주요 durable object는 다음과 같다.

```text
Portfolio
Account
Position
CashObservation
Policy / Mandate

Evidence
Claim
KnowledgeGeneration

DecisionRequest
DecisionContext
Decision
Transaction
Outcome

ReflectionSession
ReflectionRecord          [future persistence]

Monitor                   [future]
Event                     [future]
Notification              [future]
PaperIntent / PaperFill   [future]
```

객체는 가능한 한 version/supersede 방식으로 이력을 남기고, 기존 기록을 조용히 덮어쓰지 않는다.

## 5. Capability architecture

제품 내부 모듈끼리 파일 경로를 직접 공유하거나 Provider끼리 직접 호출하는 대신, normalized Capability contract로 연결한다.

### Data capabilities

- `portfolio.state`
- `knowledge.current`
- `knowledge.search`
- `market.quote`
- `market.ohlcv`
- 향후 macro / external evidence provider

### Intelligence / interaction capabilities

- `decision.context`
- 향후 perspective coordination / technical / thesis / portfolio advice
- `reflection.session`

### Durable history / control

- `decision.history`
- `transaction.history`
- trusted approval
- authorization / audit

### Automation

- `monitor.create` 이후 확장
- notification
- collector
- paper
- scheduler / worker runtime

`Context Composer`는 여러 capability 결과를 묶을 뿐 투자 판단을 대신하지 않는다. required capability가 unavailable/stale/blocked/error이면 `decision_ready=false`가 될 수 있어야 한다.

## 6. Client integration

Domain Protocol은 transport-neutral하게 유지한다. 현재 client integration은:

```text
Protocol: TradeMind/InvestKitchen v1 compatibility contracts
ChatGPT Pro: OpenAI Secure MCP Tunnel → MCP stdio (read surface)
Codex: SSH → MCP stdio (read + bounded advisory write)
Runtime transport: stdio
Production host: Synology NAS
```

두 client는 같은 Portfolio/Knowledge/native state를 사용하지만 별도 Principal/Grant를
가진다. ChatGPT Pro의 현재 custom MCP 제한 때문에 direct mutation은 노출하지 않는다.
Codex는 Knowledge commit과 Portfolio observation update에만 `operation.approve`를 포함한
별도 grant를 사용한다.

`TradeMind`라는 문자열은 현재 코드와 schema의 **compatibility namespace**에 남아 있다. 제품 identity는 InvestKitchen이며, 운영 안정성을 해치지 않는 시점에 namespace migration을 별도로 한다.

Frontend/ChatGPT는 다음을 알아서는 안 된다.

- 로컬 파일 경로
- DB schema
- broker credential
- raw audit journal
- 내부 provider implementation 이름

## 7. Personal data boundary

제품 코드와 개인 데이터는 물리적으로 분리한다.

```text
Git repository
  InvestKitchen product code / contracts / docs

Runtime personal storage
  personal-data/
    portfolios/
    knowledge/
  state/
    native-write/
    approvals/
  backups/
  secrets/
```

현재 Synology는 기존 운영 호환 경로를 사용한다.

```text
/volume1/docker/trademind/
  runtime-repo/
  runtime-data/
    personal/
    state/
    backups/
  runtime-images/
  secrets/
```

이 경로명은 제품명이 아니라 **현재 deployment compatibility identifier**다.

## 8. Write / approval boundary

1.0의 write reference는 Decision과 Transaction만 다룬다.

```text
WriteRequest
  → side-effect-free MutationPreview
  → Trusted ApprovalReceipt
  → server-side verification
  → append-only commit
  → history projection
```

서버는 다음을 exact binding으로 확인해야 한다.

- payload digest
- preview / target / base version
- user / client / credential binding
- permission
- portfolio scope
- expiry

caller가 `user_confirmed=true` 같은 boolean을 보낸다고 권한이 생기지 않는다.

현재 write path는 Knowledge/Portfolio advisory flow까지 구현되어 있다. ChatGPT Pro에는
mutation tool을 노출하지 않는다. write-capable MCP client는 server-owned Principal/Grant로
bounded preview/apply actions를 받으며, 운영 설정상 mutating apply tool은 client의 approval
UI를 통과하도록 둔다. 이 UI gate는 client-side 실행 통제이며 서버가 human click 자체를
cryptographically attest하는 것은 아니다. 서버는 별도로 exact prior preview, confirmation,
scope, freshness, expiry, replay를 검증한다.

## 9. Extension model

장기적으로 InvestKitchen은 외부 Provider / Analysis / Reflection / Automation을 Extension으로 붙일 수 있어야 한다.

원칙:

- deny by default
- object/data × operation 단위 permission
- `read_all` / `write_all` 금지
- network/background는 기본 deny
- Extension별 private storage 분리
- ordinary Extension은 canonical storage 직접 수정 금지
- secret은 scope별 사용만 허용하고 export 금지
- permission 확대 upgrade는 재승인
- Runtime이 permission을 enforce할 수 없으면 fail closed
- Web GPT 자체도 superuser가 아니다

예약 권한의 예:

- permission grant
- extension install
- secret export
- portfolio identity modify
- transaction confirm authority
- live order
- kernel policy modify

### 현재 보안 한계

현재 Secure MCP Tunnel과 Python MCP child는 stdio 때문에 같은 컨테이너 안에 있다. API key 값은 MCP command/env에 넘기지 않지만, 동일 container filesystem이라는 점에서 OS-level secret isolation은 아니다.

**third-party Extension을 실제 허용하기 전에는 Tunnel과 MCP/Extension worker의 OS 경계를 분리해야 한다.**

## 10. Deployment architecture

Synology production은 NAS에서 source build를 하지 않는다.

```text
Trusted builder
  → linux/amd64 image build
  → OpenAI runtime release SHA-256 verify
  → source commit label verify
  → archive 자체 재-load 검증
  → SHA-256 sidecar

Synology
  → exact archive transfer
  → docker load
  → platform / revision / runtime version verify
  → native personal-data MCP preflight
  → cutover
  → health=healthy
```

같은 Tunnel ID를 Mac과 NAS에서 동시에 실행하지 않는다.

## 11. 의도적으로 아직 결정하지 않은 것

다음은 필요가 생길 때까지 특정 기술로 고정하지 않는다.

- 장기 DB engine
- HTTP MCP / Unix socket 중 다음 transport
- Extension sandbox 기술
- public Plugin distribution hosting
- 모바일 전용 frontend 필요 여부
- live broker order integration

아키텍처 계약을 먼저 안정시키고 구현 기술을 그 뒤에 선택한다.
