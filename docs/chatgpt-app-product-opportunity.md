# ChatGPT App Product Opportunity

## 목적

개인용 InvestKitchen에서 검증한 구조 중, 일반 사용자에게도 독립적인 가치가 있는 부분을 ChatGPT용 공개 앱으로 제품화할 가능성을 기록한다.

이 문서는 현재 개인용 InvestKitchen 구현 범위와 분리된 **후속 제품 탐색 문서**다. 현재 작업 우선순위나 runtime 배포를 변경하지 않는다.

## 제품 가설

공개판은 증권사 주문 실행 도구가 아니라, **사용자가 신뢰하는 정보와 자신의 Portfolio 맥락을 연결해 투자 판단을 정리하는 ChatGPT-native 의사결정 워크스페이스**로 정의한다.

핵심 사용 흐름:

1. 사용자가 뉴스, 리포트, 유튜브, 전문가/멘토 코멘트 등 자료를 등록한다.
2. 자료는 speaker/source/provenance를 보존한 구조화된 Knowledge로 정리된다.
3. 사용자는 어떤 발언자를 어느 정도 반영할지 단기/중기/장기별 weighting을 정할 수 있다.
4. 서로 다른 의견의 공통점, 충돌, 유보 상태를 비교한다.
5. 사용자의 Portfolio 또는 관심종목과 연결해 현재 노출에 어떤 의미가 있는지 본다.
6. 최종 출력은 주문 실행이 아니라 판단 기준, 조건, invalidation, 다음 확인 포인트를 정리한다.
7. 사용자의 이전 판단과 근거는 Decision Journal로 추적 가능하게 남긴다.

## 공개판의 핵심 모듈

### 1. Portfolio

- 실제 Portfolio 또는 관심종목 입력
- 초기 공개판은 직접 입력/CSV/import 중심으로 시작 가능
- 증권사 API 연결은 초기 필수 범위가 아님

### 2. Sources / Knowledge

- 뉴스, 리포트, 영상, 전문가 코멘트 등록
- speaker, source authority, effective time, provenance 보존
- 외부 견해와 사용자 정책/Portfolio fact를 분리

### 3. Opinion Weighting

InvestKitchen legacy에서 이미 사용한 구조를 일반화한다.

- horizon별 `anchor_weights`
- `supplemental_pool`
- `reserve_weight`
- `speaker_multipliers`
- `speaker_approach_multipliers`
- `speaker_approach_decision_roles`
- 새 자료가 추가됐다는 이유만으로 anchor weight를 자동 변경하지 않음
- 관련 근거가 없는 발언자 몫을 다른 발언자에게 자동 재배분하지 않고 reserve로 유지

핵심 제품 메시지는 특정 전문가 추천이 아니라 **“누구의 의견을 얼마나 참고할지는 사용자가 정한다”**에 둔다.

### 4. Decision Board / Journal

- 무엇을 봤는지
- 어떤 조건에서 판단이 달라지는지
- 현재 thesis가 유지/약화/무효화됐는지
- 다음 확인할 데이터가 무엇인지
- 과거에 왜 그런 결정을 했는지

을 연결한다.

## 개인용 InvestKitchen과 공개 앱의 분리

### InvestKitchen Personal

- 개인용 InvestKitchen의 실제 private Portfolio
- NAS/private runtime
- brokerage account API
- private Knowledge
- account sync
- Policy write
- Opinion Weighting write
- Codex 기반 승인형 write

### Public ChatGPT App

- 일반화된 Portfolio context
- Sources / Knowledge
- Speaker Weighting
- Opinion Consensus
- Thesis / Decision Board
- Decision Journal
- 주문 실행은 포함하지 않음

공개판은 개인용 InvestKitchen의 모든 기능을 노출하는 것이 아니라, **일반화 가능한 의사결정 엔진을 별도 제품으로 추출**한다.

## ChatGPT 앱으로서의 차별점 후보

일반적인 투자 AI 챗봇과 달리 다음을 제품 중심으로 삼는다.

- 여러 의견을 단순 요약하지 않고 **사용자 정의 weighting**으로 조율
- 의견 간 충돌을 숨기지 않고 별도 상태로 보존
- 확신이 부족하면 억지 consensus 대신 reserve/unknown을 유지
- 외부 전문가 견해와 사용자의 실제 Portfolio/정책을 분리
- “무엇을 살까”보다 **왜 판단했고 무엇이 바뀌면 다시 판단할지**를 지속적으로 관리
- 같은 질문이라도 단기/중기/장기 horizon에 따라 다른 speaker mix를 사용

## 초기 MVP 후보

첫 공개 버전은 아래 네 덩어리로 제한하는 것이 유력하다.

1. Portfolio / Watchlist
2. Sources / Knowledge
3. Speaker Weighting + Opinion Consensus
4. Decision Board / Journal

증권사 API, 주문 실행, 복잡한 자산 동기화는 이후 별도 검토한다.

## 현재 구현에서 재사용 가능한 자산

- canonical Knowledge / speaker / provenance 구조
- current Knowledge + historical Knowledge 분리
- Policy / Portfolio / external opinion authority 분리
- legacy Opinion Weighting 정책
- 새 native Opinion Weighting store / consensus capability
- DecisionContext composition
- Decision / Transaction history 구조
- preview → approval → apply → read-back write 패턴

## 제품화 전에 추가로 검토할 것

- 공개 앱용 데이터 모델에서 개인식별/계좌정보 최소화
- 자료 등록 UX와 source verification
- 공개 앱용 Policy/Opinion 설정 UI
- Portfolio import 범위
- 앱 심사/금융 관련 정책 요구사항
- 개인정보처리방침 및 데이터 보존 정책
- monetization 가능 시점과 모델
- 개인용 InvestKitchen 코드와 공개 서비스 코드의 경계

## 현재 결론

InvestKitchen 전체를 그대로 공개하기보다, **Portfolio + Sources + Opinion Weighting + Decision Board**를 일반화한 ChatGPT-native 투자 의사결정 앱으로 분리하는 방향은 별도 제품 탐색 가치가 있다.

현재 우선순위는 개인용 InvestKitchen 안정화이며, 이 문서는 후속 제품 탐색의 출발점으로만 유지한다.
