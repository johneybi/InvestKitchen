# InvestKitchen product plan

## 1. 제품 정의

InvestKitchen은 단순 종목 추천기나 시세 조회기가 아니다.

**외부 시장 정보, 여러 전문가의 관점, 사용자의 실제 포트폴리오와 투자 정책, 과거 결정, 투자 심리와 회고를 연결해 “지금 무엇을 왜 할지”를 판단하고 계속 관리하는 개인 투자 운영 시스템**이다.

핵심 산출물은 답변 한 번이 아니라 다음의 반복이다.

```text
현재 상태 파악
→ 중요한 외부 정보 수집
→ 서로 다른 관점 비교
→ 내 포트폴리오에 적용
→ 조건부 대응 전략
→ 실제 결정과 거래 기록
→ 사후 평가와 회고
→ 다음 판단에 반영
```

## 2. 해결하려는 문제

개인 투자 판단은 보통 여러 곳에 흩어져 있다.

- 증권사에는 계좌와 체결이 있다.
- 뉴스/영상/리포트에는 시장 정보가 있다.
- 전문가마다 서로 다른 해석을 한다.
- 메모에는 투자 아이디어가 있다.
- ChatGPT 대화에는 그날의 판단 맥락이 있다.
- 실제로 왜 샀고 왜 팔았는지는 시간이 지나면 흐려진다.

InvestKitchen은 이 정보를 한 데이터베이스에 무작정 섞는 대신 **출처와 권위를 구분한 상태로 연결**한다.

## 3. 사용자에게 보여줄 핵심 경험

### A. “오늘 어떻게 대응해야 하지?”

사용자가 자연어로 상황을 물으면 ChatGPT가 공개 정보와 InvestKitchen context를 결합해 답한다.

최종 표현은 단순 코멘트보다 **Strategy Board**에 가깝다.

- 현재 판단
- 근거와 반대 근거
- 계좌/자산별 현재 상태
- 가격 또는 상태 trigger
- 행동 조건
- 수량/금액/계좌
- 무효화 조건
- 기다려야 하는 경우의 조건

### B. “전문가들은 뭐라고 하고, 내 상황에는 어떻게 적용되지?”

전문가 신뢰도와 개별 자료의 증거 품질을 구분한다. 여러 견해를 고정 비율로 평균내지 않는다.

```text
Source view A
Source view B
독립적인 시장/포트폴리오 분석
→ agreement / conflict / uncertainty
→ 내 포트폴리오에서의 의미
```

### C. “내가 지금 충동적으로 판단하는 건 아닐까?”

Reflection은 부가 기능이 아니라 Decision Loop의 독립 capability다.

PRE reflection:

- 지금 하려는 행동
- 감정
- 가정
- bias 후보
- 반대 증거
- 기다릴 조건

POST reflection:

- 당시 판단과 실제 결과
- 맞았던 전제 / 틀렸던 전제
- 결과론적 해석을 피한 process review
- 다음 의사결정 규칙 후보

### D. “그 뒤로 무슨 일이 있었지?”

장기적으로 Decision과 Transaction을 함께 조회해:

```text
무슨 판단을 했는가
→ 실제로 무엇을 실행했는가
→ 결과가 어땠는가
→ 같은 상황에서 반복되는 패턴이 있는가
```

를 볼 수 있어야 한다.

## 4. AI clients와 Runtime의 역할

### ChatGPT가 잘하는 것

- 자연어 의도 파악
- 최신 공개 웹 조사
- 긴 자료 요약과 비교
- 논리적 추론
- 사용자와의 질문/대화
- Strategy Board 설명

### Codex가 맡을 수 있는 것

- InvestKitchen의 같은 MCP read surface 사용
- 사용자가 승인한 Knowledge 등록과 Portfolio observation 반영
- preview → approval → apply → read-back 운영 작업
- 개발/운영 진단과 제품 코드 작업

ChatGPT와 Codex가 서로 다른 상태를 가지면 안 된다. client별 tool 노출은 달라도
Portfolio / Knowledge authority와 write contract는 같은 Runtime을 사용한다.

### InvestKitchen Runtime이 가져야 하는 것

- Portfolio / Account / Position / Cash
- Policy / Mandate / Plan
- Transaction
- Decision / Outcome
- Knowledge / Evidence / provenance
- structured market state
- deterministic 계산
- trusted approval
- audit / permission
- Reflection record
- Scheduler / Monitor / Notification
- backup / recovery

ChatGPT 대화 내용만으로 현재 Portfolio를 추측해서는 안 된다.
어떤 AI client도 `user_confirmed=true` 같은 client-side 주장만으로 mutation 권한을 얻지 않는다.

## 5. 세 개의 제품 Loop

### 5.1 Research / Knowledge Loop

목표: 외부 정보를 다시 사용할 수 있는 증거와 claim으로 만든다.

```text
수집
→ 출처 식별
→ Evidence
→ Claim 후보
→ 검토
→ canonical Knowledge
→ supersede / stale / expire
```

### 5.2 Decision Loop

목표: “좋아 보인다”를 실제 사용자의 상황에 적용 가능한 판단으로 바꾼다.

```text
질문/상황
→ DecisionContext
→ Advice / Assessment
→ 사용자 결정
→ Decision record
→ Transaction
→ Outcome
→ Reflection
```

### 5.3 Automation Loop

목표: 사용자가 계속 ChatGPT를 보고 있지 않아도 중요한 상태 변화를 놓치지 않는다.

```text
Scheduler / Collector
→ deterministic Monitor
→ Event
→ Notification
→ 사용자가 다시 Decision Loop 진입
```

## 6. Capability Packs

Pack은 배포 프로필이 아니라 제품 capability의 조합이다.

### Research Pack

- Evidence / Claim ingestion
- Knowledge current/search
- source provenance
- freshness / supersession
- Perspective coordination

### Decision Pack

- Portfolio / Policy
- Market / Macro context
- DecisionContext
- Strategy Board support
- Decision / Transaction history

### Reflect Pack

- PRE reflection
- POST reflection
- Decision/Outcome linking
- 반복되는 판단 패턴 분석

### Automation Pack

- Scheduler
- Collector
- Monitor
- Event / Notification
- optional Paper

### Full

위 capability들을 한 Runtime에서 조합한 개인 운영 환경.

## 7. InvestKitchen 1.0 범위

1.0에서 우선 완성할 범위:

1. **Portfolio authority** — 현재 상태를 Runtime이 안정적으로 제공
2. **Knowledge authority** — 출처와 freshness가 있는 Knowledge 조회
3. **Decision Context** — 사용자의 상황과 요구 capability를 안전하게 합성
4. **Decision / Transaction history** — 판단과 실행을 durable하게 연결
5. **Reflection** — PRE/POST 흐름과 persistence
6. **Market Provider** — current quote / OHLCV의 normalized read capability
7. **Monitor** — 조건 감시와 neutral event
8. **Synology always-on runtime** — Mac 없이 지속 실행
9. **Backup / recovery** — 개인 데이터와 history의 복구 경로
10. **Extension contract** — 향후 외부 기능이 내부 구현을 침범하지 않는 경계

### 1.0에서 하지 않는 것

- 자동 실거래 주문
- LLM이 매 tick 시장을 감시하는 구조
- 출처 불명의 투자 의견을 사실처럼 canonical 등록
- ChatGPT memory를 Portfolio DB로 사용
- Extension의 무제한 파일/secret/network 접근
- 다수 사용자를 위한 public SaaS 운영 최적화

## 8. 의사결정 표현 원칙

제품의 투자 답변은 가능한 한 다음을 분리한다.

```text
확인된 개인 계좌 사실
출처가 명시된 외부 견해
독립 분석
일치 / 충돌 / 불확실성
조건부 행동 옵션
무효화 조건
```

사용자의 선호는 utility를 바꿀 수 있지만 사실의 증거 강도를 바꾸지 않는다.

## 9. 장기 제품 방향

InvestKitchen이 충분히 성숙하면 사용자는 ChatGPT에서 다음과 같이 자연스럽게 쓸 수 있어야 한다.

```text
“오늘 내 포트폴리오를 보고 대응 전략 짜줘.”
“지난번 이 판단은 왜 했고 결과는 어땠어?”
“이 전문가의 최근 견해가 바뀌었는지 찾아서 내 계좌에 적용해줘.”
“이 조건이 발생하면 알려줘.”
“지금 매수하고 싶은데 PRE reflection부터 해보자.”
```

이때 사용자는 파일 위치, DB, provider, Tunnel, schema를 몰라도 된다. 그 복잡성을 감추는 것이 InvestKitchen Runtime의 역할이다.
