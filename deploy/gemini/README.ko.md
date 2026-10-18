# 도메인·외부 인증 계정 없이 Gemini 연결 시험하기

Plan 생성·교체·종료는 별도 권한과 독립적인 브라우저 승인이 필요한 선택 기능입니다. [native Plan 안내](../../docs/native-plan-lifecycle.md)를 참고하세요. 운영 배포와 실제 클라이언트 검증은 별도입니다.


이 구성은 **합성 데이터 시험용**이다. 직접 실행한 Keycloak이 로그인과 OAuth 토큰을
발급하고, Cloudflare Quick Tunnel이 임시 HTTPS 주소를 제공한다. 개인 포트폴리오나
운영 NAS 디렉터리를 연결하지 않는다. 상시 운영 배포는 별도 작업이다.

2026-10-14 검증: 로컬 TLS와 임시 공개 HTTPS 주소에서 실제 로그인·PKCE·서명된 읽기
권한, MCP 조회, 쓰기 거부, 범위 밖 조회 거부를 확인했다. 실제 Gemini 계정에 OAuth로
InvestKitchen을 등록하고 읽기 도구 6개를 발견했다. 영어 화면의 Argon 모드에서
`list_portfolios`, `get_portfolio_state`, `get_current_knowledge`를 호출해 빈
`portfolio-alpha`와 합성 Knowledge를 확인했으며, 인증된 HTTP 성공 응답도 확인했다.
한국어 채팅에서는 커넥터가 목록에 나타나지 않았고, Pro 모드는 일반 오류를 반환했다.
이 결과는 운영 배포나 개인 포트폴리오 연결 검증과는 별도다.

[Cloudflare 공식 안내](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)에
따르면 Quick Tunnel은 계정이나 도메인 없이 사용할 수 있지만 주소가 임시이며 SSE를
지원하지 않는다. InvestKitchen은 JSON 응답을 사용한다. 실제 Gemini 호환성은 연결
시험으로 확인해야 한다. 터널 재시작으로 주소가 바뀌면 새 시험 구성을 만든다.

Keycloak은 개발 모드와 시험용 DB로 실행한다. 인터넷에 관리자 경로와 master realm을
노출하지 않으며, 외부 인증 서비스 가입이나 Google OAuth 앱 생성은 필요하지 않다.
클라이언트는 미리 등록하고, 정확한 콜백 주소와 PKCE S256을 사용한다.
[Keycloak MCP 인증 안내](https://www.keycloak.org/securing-apps/mcp-authz-server)를 참고한다.

## 준비와 이미지 빌드

Docker와 Compose, Python 3.11 이상이 필요하다. 신뢰하는 로컬 빌더에서 제품 커밋으로
빌드한다. NAS 운영 checkout에서 빌드하거나 파일을 수정하지 않는다.

```sh
GEMINI_COMMIT=$(git rev-parse HEAD)
sh deploy/gemini/build-images.sh "$GEMINI_COMMIT"
GEMINI_TRIAL=/tmp/investkitchen-gemini-trial
python3 deploy/gemini/trial.py bootstrap --out "$GEMINI_TRIAL" \
  --image "investkitchen-http:gemini-$GEMINI_COMMIT" \
  --edge-image "investkitchen-gemini-edge:gemini-$GEMINI_COMMIT"
```

시험 폴더는 Git 밖의 새 빈 폴더여야 한다. 생성된 자격증명, DB와 임시 주소는 제품
저장소에 넣지 않는다. `bootstrap`은 실제 개인 데이터를 가져오는 옵션이 없으며 빈
합성 포트폴리오만 만든다. 아래 명령은 저장소 최상위에서 실행한다.

## 임시 HTTPS 주소 할당

```sh
docker compose --env-file "$GEMINI_TRIAL/trial.env" \
  -f deploy/gemini/compose.trial.yml up -d edge tunnel
docker compose --env-file "$GEMINI_TRIAL/trial.env" \
  -f deploy/gemini/compose.trial.yml logs --tail 40 tunnel
```

로그에서 할당된 `https://…trycloudflare.com` 주소를 확인한다. 이 단계의 프록시는
503만 반환한다. 도메인을 구매하거나 NAS 80/443 포트를 변경하지 않는다.

## 정확한 OAuth 콜백 주소로 구성

Gemini 웹의 **맞춤설정 → 커넥터 → 맞춤 앱 만들기**에서 할당된 주소에 `/mcp`를 붙여
입력하고 **추가 설정 → 리디렉션 URI 복사**를 누른다. 이 단계에서는 다음을 누르지 않는다.
이 버튼이 제공한 정확한 OAuth `redirect_uri`를 사용한다. 주소는 사용자와 MCP 호스트에
연결되므로 다른 계정이나 다른 임시 주소에 재사용하지 않는다. Gemini CLI, Enterprise,
Gemini Apps의 콜백 주소를 서로 대신 쓰지 않는다. 해당 버튼이 없으면 공식 안내나 실제
인증 요청으로 정확한 주소를 확인한다. 확인하지 못했으면 계정 연결이 미완료인 상태다.
와일드카드나 추정 주소로 권한을 넓히지 않는다.

```sh
GEMINI_ORIGIN=https://assigned-name.trycloudflare.com
GEMINI_CALLBACK=https://verified-client.example.test/callback
python3 deploy/gemini/trial.py configure --out "$GEMINI_TRIAL" \
  --origin "$GEMINI_ORIGIN" --redirect-uri "$GEMINI_CALLBACK"
docker compose --env-file "$GEMINI_TRIAL/trial.env" \
  -f deploy/gemini/compose.trial.yml exec edge \
  caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose --env-file "$GEMINI_TRIAL/trial.env" \
  -f deploy/gemini/compose.trial.yml up -d --no-deps auth
```

위 주소는 예시이므로 실제 할당 주소와 확인한 콜백으로 바꾼다. 프록시는 **reload**해서
터널의 네트워크와 주소를 유지한다. 이미 구성한 폴더의 `configure`는 재실행을 거부한다.
Keycloak의 `--import-realm`은 기존 realm을 덮어쓰지 않으므로 JSON 수정만으로 기존
클라이언트 설정이 바뀌지는 않는다. 주소 변경 시 새 폴더·새 Compose 프로젝트를 쓴다.

## 인증과 MCP 시험

```sh
python3 -m venv /tmp/investkitchen-gemini-venv
/tmp/investkitchen-gemini-venv/bin/pip install '.[remote]'
PYTHONPATH=. /tmp/investkitchen-gemini-venv/bin/python deploy/gemini/verify_trial.py \
  --out "$GEMINI_TRIAL" --oauth-only
docker compose --env-file "$GEMINI_TRIAL/trial.env" \
  -f deploy/gemini/compose.trial.yml run --rm -e INVESTKITCHEN_PREFLIGHT_ONLY=1 runtime
docker compose --env-file "$GEMINI_TRIAL/trial.env" \
  -f deploy/gemini/compose.trial.yml up -d --no-deps runtime
PYTHONPATH=. /tmp/investkitchen-gemini-venv/bin/python deploy/gemini/verify_trial.py \
  --out "$GEMINI_TRIAL"
```

인증 서버 기동이 끝난 뒤 실행한다. 검증 도구는 합성 계정으로 HTTPS 로그인, 정확한
콜백 제한, PKCE, 서명·issuer·audience·사용자·클라이언트·scope를 확인하고 공개 RS256
검증 키만 `oauth/public-jwks.json`에 저장한다. 토큰·비밀번호는 출력하지 않는다.
전체 검증은 실제 MCP 조회, 인증 없는 접근 거부, 쓰기 도구 거부, 다른 포트폴리오
조회 거부까지 확인한다. 오류가 나면 검증이 끝난 것으로 표시하지 않는다.

## Gemini 웹 앱 등록

현재 UI에는 **맞춤설정 → 커넥터 → 맞춤 앱 만들기**가 있다. 계정별 메뉴는 다를 수
있다. MCP 서버 URL에 `임시 HTTPS 주소/mcp`를 넣고, **추가 설정**의 클라이언트 ID와
클라이언트 비밀번호에 외부 시험 폴더의 `credentials.json` 값을 입력한다. 로그인에는
같은 파일의 합성 계정을 사용한다. 실제 사용자 비밀번호나 증권사 credential이 아니다.

**다음**을 누르면 Google의 맞춤 앱 신뢰·개인정보 동의 화면이 추가로 나온다.
Gemini가 접근하는 스킬·연결된 서비스의 정보도 이 MCP 서버에 전달될 수 있다는
안내다. 서버의 합성 데이터 읽기 권한은 서버가 반환하는 정보의 범위를 제한하며,
Gemini가 보내는 정보의 범위를 제한하지는 않는다. 별도의 동의 내용을 확인한 뒤
연결하고, 이 시험에는 합성 데이터 조회 요청만 사용한다. Google 계정 연결 동의와
합성 계정 로그인을 마치면, 도구 목록을 확인하고 앱 이름 `InvestKitchen`으로 저장한다.

연결 완료 표시와 실제 도구 호출을 확인한 뒤에만 Gemini 연결 검증 완료로 기록한다.
`investkitchen-read` 스킬 등록과 MCP 앱 연결은 별도다. 시험 포트폴리오가 비어 있다는
답을 확인하고, 실제 보유 종목처럼 해석하지 않는다. 시장 제공자는 이 구성에서 꺼져 있다.

등록은 됐지만 채팅에서 도구가 보이지 않으면
[영어 Gemini 화면](https://gemini.google.com/app?hl=en)의 `@` 목록을 확인한다. 이번 시험에서는
한국어 화면에 스킬만 나타났고, 영어 화면에는 **Connectors → InvestKitchen**이
나타났다. 커넥터를 선택한 뒤 합성 조회 요청을 입력한다. 화면 언어 선택은 지역 자격을
변경하는 방법이 아니다.

## 도메인 없이 로컬 HTTPS부터 검증하는 방법

공개 터널과 Google 계정을 사용하기 전에 새 시험 폴더를 만들고 `configure`의 origin을
`https://localhost:18443`, 콜백을 `http://127.0.0.1:18444/callback`으로 지정한다.
Compose 명령에 `-f deploy/gemini/compose.local.yml`을 추가하고 `edge auth`를 실행한다.
로컬 CA 공개 인증서를 아래처럼 저장하고 모든 `verify_trial.py` 호출에
`--ca "$GEMINI_TRIAL/local-ca.crt"`를 추가한다. 시스템 신뢰 저장소를 변경하지 않는다.

```sh
docker compose --env-file "$GEMINI_TRIAL/trial.env" \
  -f deploy/gemini/compose.trial.yml -f deploy/gemini/compose.local.yml exec -T edge \
  cat /data/caddy/pki/authorities/local/root.crt > "$GEMINI_TRIAL/local-ca.crt"
```

인증서 확인을 끄는 `-k` 옵션을 사용하지 않는다. 이 로컬 검증은 Google 연결 검증과
다르며, 로컬 주소를 Gemini 웹 앱에 등록할 수 있다는 뜻이 아니다.

## 종료·rollback과 운영 전환

```sh
docker compose --env-file "$GEMINI_TRIAL/trial.env" \
  -f deploy/gemini/compose.trial.yml stop runtime auth tunnel edge
```

Gemini에 등록한 시험 앱이 있다면 해당 앱도 해제한다. 시험 구성만 중지하므로 기존
InvestKitchen 배포의 rollback은 필요하지 않다. DB volume과 외부 폴더는 자동 삭제하지
않는다. 이 파일들을 보관할 경우 자격증명과 인증 DB를 비밀정보로 취급한다.

상시 운영 전환에는 고정 HTTPS 주소, 지원되는 운영 인증 구성과 DB, 비밀정보 관리,
백업, 공개 키 교체, 실제 사용자 권한 검증 및 NAS의 정식 배포 절차가 필요하다.
이 시험의 성공을 실계좌 연결이나 운영 검증 완료로 표시하지 않는다.
