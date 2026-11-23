# Synology 장전판 HTTPS 수신

이 프로필은 [장전판 계약 v1](../../docs/preopen-market-edition-contract.ko.md)의
Windows 발행과 NAS 접수 구간을 실행한다. 전용 Compose 프로젝트가 LAN 주소 한 곳에
바인딩한다. ChatGPT·Gemini의 MCP 도구 및 사용자 지침과 거래용 Session Exchange는
이 수신 경로에 연결되지 않는다.

## 버전과 사전 점검

검토된 소스 커밋에서만 이미지를 만든다. 신뢰된 빌더에서 다음 순서로 아카이브를
만들고, NAS의 전용 배포 보관소에서 같은 SHA-256을 확인한 뒤 관리자 권한으로 로드한다.
운영 Git checkout을 바꾸거나 기존 MCP Compose 프로젝트와 합쳐 실행하지 않는다.

```sh
revision=$(git rev-parse --verify '<reviewed-ref>^{commit}')
image="investkitchen-market-edition:$revision"
git archive "$revision" protocol/v1 deploy/synology/market-edition.Dockerfile |
  docker build --platform linux/amd64 --provenance=false \
    --build-arg "INVESTKITCHEN_SOURCE_COMMIT=$revision" \
    -f deploy/synology/market-edition.Dockerfile -t "$image" -
docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}}' "$image"
docker save -o "market-edition-$revision.tar" "$image"
shasum -a 256 "market-edition-$revision.tar" > "market-edition-$revision.tar.sha256"
```

NAS에서도 같은 이미지 ID·`linux/amd64`·소스 revision을 확인한다. 신설 컨테이너의
사전 점검은 별도 테스트 프로젝트·루프백 포트·합성 데이터 디렉터리·합성 인증서와
정책으로 한다. NAS 배포 계정이 기존 Container Manager 배포 절차로 프로젝트를
관리할 수 있어야 한다. 운영 데이터가 들어 있는 볼륨이나 기존 라이브 설정은 그
시험에 사용하지 않는다.

```sh
python -m pip install -e '.[dev]'
python -m pytest -q protocol/v1/tests/test_market_edition_ingest.py protocol/v1/tests/test_market_edition_reader.py
docker compose --project-name investkitchen-market-edition-test --env-file "$test_env" \
  -f deploy/synology/market-edition.compose.yml config --quiet
```

합성 테스트 프로젝트에서 승인 없음, 올바르지 않은 판 ID·해시, 같은 ID의 다른 바이트,
부분 저장 및 재전송을 검증한다. 두 번째 접수증이 첫 번째와 동일하고 별도 읽기 연결에서
원본 해시·후보 수·판 행 한 개가 확인돼야 한다. 시험 프로젝트를 중지한 후 운영 전환한다.

## 신뢰와 서버 승인

기존 발급자를 재사용할 수 있으면 NAS 서버 및 Windows 발행자 인증서의 용도
(`serverAuth` / `clientAuth`), 이름·SAN, 체인, 유효기간과 개인 키 소유자를 먼저
대조한다. 새 발행자 키가 필요하면 Windows에서 생성하고 CSR만 승인 담당자에게
보낸다. NAS 서버 키는 NAS의 비밀 저장소에 둔다. Windows의 신뢰 CA는 승인된
경로에서 확인한다. 사설망 주소나 인증서 존재만으로 발행을 허가하지 않는다.

운영자 서명 키는 서버 및 발행자의 TLS 키와 별도로 관리한다. 승인 JSON의 필수 필드는
`policy_id, version, active, instance_id, action, market, kind, publisher_id, scope_id,
certificate_sha256, not_before, expires_at`이다. `version=1`, `active=true`,
`action=market_edition.publish`, `market=KR`, `kind=preopen`에 승인한 인스턴스·발행자·
범위·클라이언트 leaf 인증서 DER SHA-256 및 오프셋 있는 승인 기간을 고정한다. 운영자
Ed25519 개인 키로 승인 JSON의 정확한 바이트를 서명한다. 설치할
`publish-approval.json`은 승인 원문과 원시 서명을 Base64로 인코딩한 두 필드
`policy`, `signature`를 포함한다. 수신기에는 검증 공개 키만 들어간다.

```sh
openssl x509 -in "$publisher_cert" -outform DER | openssl dgst -sha256
openssl pkeyutl -sign -rawin -inkey "$operator_signing_key" -in "$approved_json" -out "$signature"
python3 - "$approved_json" "$signature" <<'PY'
import base64
import json
import pathlib
import sys

def encoded(path):
    return base64.b64encode(pathlib.Path(path).read_bytes()).decode("ascii")

print(json.dumps({"policy": encoded(sys.argv[1]), "signature": encoded(sys.argv[2])}))
PY
```

출력은 비밀 저장소에 원자적으로 설치한다. 정책 교체나 철회는 현재 승인 파일을 원자적으로
교체하거나 제거한다. 수신기는 매 요청과 커밋 직전에 현재 승인 파일을 다시 읽는다.
인증서·운영자 공개 키 교체는 서비스를 재기동한다. 운영자 서명 키와 Windows 발행자
개인 키는 Compose에 마운트하지 않는다.

## 운영 전환과 복구

별도 환경 파일에 `MARKET_EDITION_IMAGE`, `MARKET_EDITION_UID`,
`MARKET_EDITION_GID`, `MARKET_EDITION_LAN_IP`, `MARKET_EDITION_PORT`,
`MARKET_EDITION_STATE`, `MARKET_EDITION_TRUST`, `MARKET_EDITION_INSTANCE`를 둔다.
전용 비특권 UID가 NAS 로컬 상태 디렉터리와 비밀 파일에 필요한 최소 권한을
갖도록 준비한다. 상태와 신뢰 디렉터리는 각각 운영 데이터와 비밀 영역에 둔다.
신뢰 파일 이름은 Compose의 `/trust` 인수를 따른다. LAN IP는 NAS의 특정 주소여야
하며 전체 인터페이스 또는 공개 프록시에는 게시하지 않는다. NAS 방화벽에서
Windows가 쓰는 내부 연결만 허용하는지 확인한다.

기존 배포 설정·이미지·백업을 확인하고, 전용 DB가 이미 있으면 SQLite의 backup API로
일관 백업을 만든다. 읽기 전용 연결에 `immutable`을 사용하거나 라이브 DB 파일을
단독 복사하지 않는다. 운영 DB에 기존 판이 있으면 그대로 사용한다. 테스트 프로젝트와
다른 이름으로 배포한다.

```sh
docker compose --project-name investkitchen-market-edition --env-file "$prod_env" \
  -f deploy/synology/market-edition.compose.yml config --quiet
docker compose --project-name investkitchen-market-edition --env-file "$prod_env" \
  -f deploy/synology/market-edition.compose.yml up -d --no-build --pull never
```

Windows의 기존 전송 파일을 그대로 읽어 검증한 접수증을 같은 로컬 볼륨의 별도 파일에
기록한다. `--receipt`의 부모 디렉터리는 Windows 발행 사용자만 쓸 수 있는 NTFS
폴더로 준비한다. Windows에서 접수 후 같은 인수로 재실행해 HTTP 200과 최초
`revision` 및 `accepted_at`이 돌아오는지 확인한다. 첫 요청이 HTTP 200이라면
기존 접수의 재확인이다.

```powershell
py -3 .\send-market-edition.py --input $edition --receipt $receipt --nas $nasOrigin `
  --nas-ca $nasCa --publisher-cert $publisherCert --publisher-key $publisherKey `
  --publisher-id $publisherId --expect-sha256 $expectedHash
```

별도 NAS 읽기 연결에서 SQLite `mode=ro`로 연다. 인증된 자료 범위와 판 ID로
`editions` 행 한 개를 찾고 저장 `payload`의 SHA-256을 다시 계산해
`payload_sha256` 및 Windows 접수증과 대조한다. 같은 범위와 판 ID의
`candidates` 행 수를 원문 배열 길이와 대조한다. `revision`·`accepted_at`도
접수증과 같아야 한다.

신설 서비스의 롤백은 전용 Compose 프로젝트만 `stop market-edition`으로 중지한다.
이전 버전이 이미 있으면 검증된 직전 이미지·환경 파일로 같은 프로젝트를 복구한다.
접수 데이터와 영구 볼륨은 보존한다. 저장 자체의 복구는 수신을 중지한 상태에서
백업을 별도 위치로 복원한 뒤 `quick_check`, 판·후보 수, 원본 해시 및 접수증을
대조해 수행한다.
