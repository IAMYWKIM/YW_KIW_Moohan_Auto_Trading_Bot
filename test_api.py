#!/usr/bin/env python3
"""
키움 실전 API 필드명 확인 스크립트
서버에서 실행: python3 test_api.py
"""
import os, sys, json, requests

# .env 로드
env_path = os.path.join(os.path.dirname(__file__), ".env")
if not os.path.exists(env_path):
    env_path = "/home/iamywkim/kiw_moohan_trader/.env"

with open(env_path) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()

key    = os.getenv("KIWOOM_APP_KEY")
secret = os.getenv("KIWOOM_SECRET_KEY")
acct   = os.getenv("KIWOOM_ACCOUNT_NO")
url    = "https://api.kiwoom.com"

# 토큰
token = requests.post(f"{url}/oauth2/token", json={
    "grant_type": "client_credentials",
    "appkey": key, "secretkey": secret,
}, timeout=10).json()["token"]
print(f"✅ 토큰 발급 성공")

headers = {
    "Content-Type": "application/json;charset=UTF-8",
    "authorization": f"Bearer {token}",
    "api-id": "kt00001",
}

# kt00001 잔고 조회
r = requests.post(f"{url}/api/dostk/acnt",
    headers=headers,
    json={"acnt_no": acct, "qry_tp": "1"},
    timeout=10)
data = r.json()

print(f"\n=== kt00001 잔고 조회 ===")
print(f"return_code: {data.get('return_code')}")
print(f"return_msg:  {data.get('return_msg')}")
print()

# 핵심 필드 출력
key_fields = [
    ("entr",          "예수금"),
    ("ord_alow_amt",  "주문가능금액"),
    ("elwdpst_evlta", "평가금액"),
    ("pymn_alow_amt", "출금가능금액"),
    ("d2_entra",      "D+2 예수금"),
]
for field, label in key_fields:
    val = data.get(field, "없음")
    if val != "없음":
        try:
            num = int(str(val).lstrip("0") or "0")
            print(f"  {label:15} ({field}): {num:>15,}원")
        except:
            print(f"  {label:15} ({field}): {val}")

# 보유종목
items = data.get("stk_entr_prst", [])
print(f"\n=== 보유종목: {len(items)}개 ===")
if items:
    print("첫 번째 종목 전체 필드:")
    print(json.dumps(items[0], ensure_ascii=False, indent=2))
else:
    print("(현재 보유 종목 없음)")

print("\n✅ 테스트 완료")
