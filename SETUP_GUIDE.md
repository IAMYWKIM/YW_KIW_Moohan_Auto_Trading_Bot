# 🛠️ ETF 무한매매 봇 — 완전 설치 가이드

> GCP Ubuntu 서버 + 키움증권 REST API + 텔레그램 봇  
> 처음부터 차근차근 따라하는 단계별 안내서

---

## 📋 전체 진행 순서

```
STEP 1  텔레그램 봇 생성 (5분)
STEP 2  키움증권 REST API 발급 (10분)
STEP 3  GCP 서버 생성 (15분)
STEP 4  서버 기본 환경 구축 (20분)
STEP 5  봇 코드 배포 (10분)
STEP 6  환경 변수 설정 (5분)
STEP 7  모의투자 테스트 (1일)
STEP 8  systemd 등록 및 자동 실행 (10분)
```

---

## STEP 1 — 텔레그램 봇 생성

### 1-1. BotFather에서 봇 생성

1. 텔레그램 앱에서 **@BotFather** 검색 후 채팅 시작
2. `/newbot` 입력
3. 봇 이름 입력 (예: `나의 ETF봇`)
4. 봇 사용자명 입력 — 반드시 `bot`으로 끝나야 함 (예: `MyETFTrader_bot`)
5. BotFather가 **API Token** 발급:
   ```
   1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
   ```
   → 이것이 `.env`의 `TELEGRAM_BOT_TOKEN`

### 1-2. 내 채팅방 ID 확인

1. 텔레그램에서 **@userinfobot** 검색 후 아무 메시지 전송
2. 응답에서 `Id: 123456789` 확인
   → 이것이 `.env`의 `TELEGRAM_CHAT_ID`

### 1-3. 봇 테스트

아래 URL을 브라우저에서 열어 봇이 정상 응답하는지 확인:
```
https://api.telegram.org/bot{YOUR_TOKEN}/getMe
```

---

## STEP 2 — 키움증권 REST API 발급

### 2-1. 키움증권 계좌 준비
- 키움증권 계좌 (실전투자용)
- 키움증권 모의투자 서비스 신청 (처음에는 모의로 테스트)

### 2-2. 개발자 센터 앱 등록

1. **키움 개발자 센터** 접속: https://developers.kiwoom.com
2. 로그인 → **앱 등록** 클릭
3. **모의투자 앱** 등록:
   - 앱명: `ETF_BOT_MOCK`
   - 서비스 타입: 국내주식
   - 사용 API: 매수/매도/잔고/시세 등 필요 항목 체크
4. 등록 완료 후 발급:
   - `App Key` → `.env`의 `KIWOOM_APP_KEY_MOCK`
   - `Secret Key` → `.env`의 `KIWOOM_SECRET_KEY_MOCK`
5. **실전투자 앱**도 동일하게 등록 (나중에 사용)

### 2-3. 계좌번호 확인
- 키움 HTS(영웅문) 또는 앱에서 계좌번호 확인
- 모의투자 계좌번호: 영웅문 모의투자 서비스에서 확인
- `.env`의 `KIWOOM_ACCOUNT_NO_MOCK` 에 입력

---

## STEP 3 — GCP 서버 생성

### 3-1. GCP 계정 설정

1. https://cloud.google.com 접속 → 계정 생성
2. **새 프로젝트** 생성 (예: `etf-trading-bot`)
3. 결제 계정 연결 (첫 90일 $300 무료 크레딧)

### 3-2. VM 인스턴스 생성

1. 메뉴 → **Compute Engine** → **VM 인스턴스** → **만들기**
2. 설정:
   ```
   이름:       etf-bot-server
   리전:       asia-northeast3 (서울)
   영역:       asia-northeast3-a
   머신 유형:  e2-micro  (월 약 $7, 무료 등급 해당)
   부팅 디스크: Ubuntu 22.04 LTS  30GB
   방화벽:     [✓] HTTP 허용, [✓] HTTPS 허용
   ```
3. **만들기** 클릭

### 3-3. 방화벽 규칙 (선택)
SSH(22번 포트)는 기본 허용.  
봇 자체는 외부 포트 불필요 → 추가 방화벽 설정 생략 가능.

### 3-4. 고정 IP 설정 (권장)

1. **VPC 네트워크** → **외부 IP 주소** → **예약**
2. 유형: **정적** → 이름 입력 → 연결
3. 이후 이 IP로만 SSH 접속

---

## STEP 4 — 서버 기본 환경 구축

GCP 콘솔에서 **SSH** 버튼 클릭 또는 로컬에서:
```bash
gcloud compute ssh etf-bot-server --zone=asia-northeast3-a
```

### 4-1. 시스템 업데이트

```bash
sudo apt update && sudo apt upgrade -y
sudo timedatectl set-timezone Asia/Seoul
timedatectl   # KST 확인
```

### 4-2. Python 3.11 설치

```bash
sudo apt install -y python3.11 python3.11-venv python3.11-dev python3-pip git
python3.11 --version  # Python 3.11.x 확인
```

### 4-3. 프로젝트 디렉토리 생성

```bash
mkdir -p ~/kiw_moohan_trader
cd ~/kiw_moohan_trader
```

### 4-4. Python 가상환경 생성

```bash
python3.11 -m venv etf_venv
source etf_venv/bin/activate
pip install --upgrade pip
```

---

## STEP 5 — 봇 코드 배포

### 방법 A: GitHub 사용 (권장)

```bash
# GitHub에 업로드한 경우
cd ~/kiw_moohan_trader
git clone https://github.com/YOUR_USERNAME/etf-bot.git .
```

### 방법 B: 직접 파일 업로드 (SCP)

로컬 PC에서:
```bash
# 프로젝트 폴더 전체를 GCP 서버로 전송
gcloud compute scp --recurse \
  "C:/Users/YW/Documents/Claude/Projects/국내주식 2배ETF  무한매매 프로젝트/." \
  etf-bot-server:~/kiw_moohan_trader/ \
  --zone=asia-northeast3-a
```

### 방법 C: deploy 스크립트 (나중에 업데이트할 때)

```bash
# /usr/local/bin/deploy_etf 파일 생성
sudo tee /usr/local/bin/deploy_etf > /dev/null << 'EOF'
#!/bin/bash
set -e
PROJ_DIR=/home/$USER/kiw_moohan_trader
SRC_DIR=/tmp/etf_upload

echo "🚀 ETF 봇 배포 시작..."
cp $SRC_DIR/*.py $PROJ_DIR/
cp $SRC_DIR/modules/*.py $PROJ_DIR/modules/
find $PROJ_DIR -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
sudo systemctl restart etfbot
echo "✅ 배포 완료"
sudo systemctl status etfbot --no-pager
EOF

sudo chmod +x /usr/local/bin/deploy_etf
```

### 패키지 설치

```bash
cd ~/kiw_moohan_trader
source etf_venv/bin/activate
pip install -r requirements.txt
```

---

## STEP 6 — 환경 변수 설정

```bash
cd ~/kiw_moohan_trader
cp .env.example .env
nano .env
```

아래 내용을 실제 값으로 수정:
```bash
TRADE_MODE=MOCK

KIWOOM_APP_KEY_MOCK=실제_모의투자_앱키
KIWOOM_SECRET_KEY_MOCK=실제_모의투자_시크릿키
KIWOOM_ACCOUNT_NO_MOCK=실제_모의투자_계좌번호

KIWOOM_APP_KEY=실제_실전_앱키
KIWOOM_SECRET_KEY=실제_실전_시크릿키
KIWOOM_ACCOUNT_NO=실제_실전_계좌번호

TELEGRAM_BOT_TOKEN=실제_봇_토큰
TELEGRAM_CHAT_ID=실제_채팅방_ID
```

저장: `Ctrl+X` → `Y` → `Enter`

**.env 파일 보호** (중요!):
```bash
chmod 600 .env
```

---

## STEP 7 — 모의투자 테스트

### 7-1. API 연결 확인

```bash
cd ~/kiw_moohan_trader
source etf_venv/bin/activate
python3 -c "
from modules.kiwoom_api import KiwoomBroker
b = KiwoomBroker()
b.ping()
"
```

출력 예:
```
✅ 키움 REST API 연결 성공! [MOCK 모드]
   토큰 앞 20자: eyJhbGciOiJIUzI1N...
   토큰 만료:    2026-05-25 09:15
   계좌번호:     1234567890
   예수금:       10,000,000원
```

### 7-2. API 응답 필드 진단 (필드명 확인용)

```bash
python3 -c "
from modules.kiwoom_api import KiwoomBroker
b = KiwoomBroker()
b.debug_api_keys('ka10001', '122630')  # KODEX 레버리지 현재가 조회
b.debug_api_keys('kt00018')            # 계좌잔고 필드 확인
"
```

### 7-3. 봇 수동 실행 (로그 확인)

```bash
cd ~/kiw_moohan_trader
source etf_venv/bin/activate
python3 main.py
```

텔레그램으로 "ETF 봇 시작" 메시지가 오면 성공.  
`Ctrl+C`로 종료 후 로그 확인:
```bash
tail -f logs/etf_bot.log
```

### 7-4. 모의투자 1~2주 운영 후 실전 전환
1. 결과 확인: 체결 알림, 정산 리포트 정상 수신 확인
2. `.env`에서 `TRADE_MODE=REAL` 로 변경
3. 실전 계좌 API 키로 교체

---

## STEP 8 — systemd 서비스 등록 (24시간 자동 실행)

### 8-1. 서비스 파일 생성

```bash
sudo tee /etc/systemd/system/etfbot.service > /dev/null << EOF
[Unit]
Description=국내 ETF 무한매매 봇
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=/home/$(whoami)/kiw_moohan_trader
Environment="TZ=Asia/Seoul"
ExecStart=/home/$(whoami)/kiw_moohan_trader/etf_venv/bin/python3 main.py
Restart=always
RestartSec=30
StandardOutput=append:/home/$(whoami)/kiw_moohan_trader/logs/systemd.log
StandardError=append:/home/$(whoami)/kiw_moohan_trader/logs/systemd_error.log

[Install]
WantedBy=multi-user.target
EOF
```

### 8-2. 서비스 등록 및 시작

```bash
sudo systemctl daemon-reload
sudo systemctl enable etfbot    # 부팅 시 자동 시작
sudo systemctl start etfbot     # 즉시 시작
```

### 8-3. 상태 확인

```bash
sudo systemctl status etfbot

# 실시간 로그 확인
journalctl -u etfbot -f
tail -f ~/kiw_moohan_trader/logs/etf_bot.log
```

### 8-4. 유용한 관리 명령어

```bash
# 봇 재시작 (코드 변경 후)
sudo systemctl restart etfbot

# 봇 중지
sudo systemctl stop etfbot

# 봇 로그 (최근 100줄)
sudo journalctl -u etfbot -n 100

# 에러만 확인
grep ERROR ~/kiw_moohan_trader/logs/etf_bot.log | tail -20
```

---

## 🔄 코드 업데이트 방법

### GitHub 사용 시

```bash
cd ~/kiw_moohan_trader
git pull origin main
sudo systemctl restart etfbot
```

### 직접 파일 교체 시

로컬 PC에서:
```bash
# 변경된 파일만 전송
gcloud compute scp \
  "C:/Users/YW/Documents/.../modules/trading_engine.py" \
  etf-bot-server:~/kiw_moohan_trader/modules/ \
  --zone=asia-northeast3-a

# 서버에서 재시작
gcloud compute ssh etf-bot-server --zone=asia-northeast3-a \
  --command="sudo systemctl restart etfbot"
```

---

## 📊 텔레그램 봇 명령어 확장 (향후 구현 예정)

봇 채팅창에서 직접 조회/제어:

| 명령어 | 기능 |
|---|---|
| `/status` | 보유 포지션 + 실시간 손익 |
| `/balance` | 예수금 + 계좌 총 평가금액 |
| `/positions` | 종목별 회차·평단가 |
| `/buy 122630 10` | 수동 매수 (10주) |
| `/sell 122630` | 수동 전량 매도 |
| `/halt` | 봇 거래 일시 정지 |
| `/resume` | 봇 거래 재개 |
| `/config` | 현재 설정 조회 |

---

## ❗ 자주 발생하는 문제

### Q1. 토큰 발급 실패
```
RuntimeError: 토큰 발급 실패: UNAUTHORIZED
```
→ `.env`의 APP_KEY, SECRET_KEY 오타 확인  
→ 키움 개발자 센터에서 앱 승인 여부 확인

### Q2. 현재가 조회 실패 (0 반환)
```
[Broker] API 오류 [ka10001] rc=1: ...
```
→ `debug_api_keys('ka10001')` 실행하여 실제 필드명 확인  
→ 장 마감 후에는 일부 API 제한될 수 있음

### Q3. 429 Too Many Requests
→ Rate Limiter 자동 처리 (5초 대기 후 재시도)  
→ 반복 시 `_RateLimiter(max_calls=3)` 로 더 낮춤

### Q4. 봇이 주말에도 실행됨 (주문은 안 됨)
→ 정상. Market Calendar가 주말·공휴일 감지하여 주문 차단  
→ 로그에 "거래일 아님" 표시

### Q5. GCP 서버 비용
→ e2-micro 인스턴스 기준 **월 약 $7~10** (서울 리전)  
→ 무료 등급(f1-micro)은 서울 리전 미지원 — e2-micro 사용 권장

---

## 🔐 보안 체크리스트

- [ ] `.env` 파일 권한: `chmod 600 .env`
- [ ] `.gitignore`에 `.env` 포함 확인
- [ ] GCP 서비스 계정 최소 권한 원칙 적용
- [ ] SSH 키 기반 인증 (패스워드 비활성화)
- [ ] 실전 API 키는 최소 권한(주문 전용)으로 발급
- [ ] 봇 계좌에 필요 금액 이상 예수금 미보유

---

*작성일: 2026-05-24 | 봇 버전: v2.0*
