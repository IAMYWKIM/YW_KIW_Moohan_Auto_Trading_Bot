# ==============================================================
# [version.py] 버전 정보 단일 진실 공급원 (Single Source of Truth)
#
# 모든 버전 정보는 이 파일 하나에서만 관리합니다.
# telegram_bot.py, README.md, guide.html 등은 이 파일을 임포트해서 사용합니다.
#
# 수정 방법:
#   1. CURRENT 딕셔너리의 값을 변경
#   2. HISTORY 리스트 맨 앞에 새 항목 추가
#   3. python3 version.py 실행 → README.md, guide HTML 자동 업데이트
# ==============================================================

# ==============================================================
# 현재 버전 정보
# ==============================================================
CURRENT = {
    "version":     "6.0",
    "date":        "2026-05-29",
    "codename":    "AVWAP Dual Momentum",
    "bot_name":    "국내 ETF 무한매매 + AVWAP 봇",
    "description": "승승장군 issues #59~#66 완전 재분석, V-REV SMA5 알고리즘, AVWAP v2.0",
    "strategies":  ["INFINITE", "V-REV", "AVWAP"],
    "api":         "키움증권 REST API",
    "platform":    "GCP Ubuntu + python-telegram-bot v20+",
}

# 편의용 단축 접근자
VERSION       = CURRENT["version"]          # "5.0"
VERSION_TAG   = f"v{CURRENT['version']}"    # "v5.0"
BOT_NAME      = CURRENT["bot_name"]
RELEASE_DATE  = CURRENT["date"]

# ==============================================================
# 변경 이력 (최신 → 오래된 순)
# ==============================================================
HISTORY = [
    {
        "version": "6.0",
        "date":    "2026-05-29",
        "emoji":   "🔥",
        "changes": [
            "V-REV 알고리즘 완전 재구현 — 승승장군 원본 SMA5 기준 역추세 (issues #59 #61)",
            "V-REV 실행 시간: 09:10 편차계산 → 15:10 동시호가 LOC (원본과 동일)",
            "V-REV 매수: 현재가 < SMA5 시 동시호가 / 매도: 현재가 > SMA5 시 팝(Pop)",
            "scheduler_trade.py vrev_loc_start() 15:10 신규 Job 추가",
            "build_sync_plan() V-REV 모드 SMA5 기반 주문계획 표시",
        ],
    },
    {
        "version": "5.0",
        "date":    "2026-05-26",
        "emoji":   "⚡️",
        "changes": [
            "AVWAP 엔진 v2.0 — 진입조건 ② 방향 버그 수정 (5MA > VWAP, 이슈 #66 원문 기준)",
            "수수료 기반 최소 목표가 2.03% 적용 (국내 왕복 0.03% 반영)",
            "상세 매매 알림 완전 구현 (무한매매·AVWAP 진입·청산·하드스탑)",
            "/version 명령어 — 버전 정보 및 업데이트 히스토리 조회",
            "/history 명령어 — 졸업 명예의 전당 (cycle_log DB 테이블 추가)",
            "/settlement UI — AVWAP ON/OFF 토글 + 예산 설정 인라인 버튼",
            "version.py 단일 진실 공급원 생성 (이 파일)",
        ],
    },
    {
        "version": "4.0",
        "date":    "2026-05-24",
        "emoji":   "💎",
        "changes": [
            "구글 시트 알고리즘 완전 동기화 — 큰수(large_num) 기반 동시호가 매수",
            "plan_new_entry 줍줍 5개 추가 (구글 시트와 동일)",
            "/sync 통합 지시서 매수·매도 섹션 분리 UI",
            "수동 매수·매도 4단계 인라인 버튼 UI (종목→수량→가격→확인)",
            "/sync_db 키움 잔고 → DB 동기화 UI (불일치 항목 시각화)",
            "build_sync_plan() — /sync 화면 주문 계획 통합 함수",
        ],
    },
    {
        "version": "3.2",
        "date":    "2026-05-24",
        "emoji":   "🔧",
        "changes": [
            "텔레봇 통합 지시서·장부·설정·종목관리 UI 완성",
            "/ticker — 종목 추가·제거·활성화 인라인 버튼",
            "update.message → update.effective_message 일괄 수정",
            "SETTLE:{code} 콜백 버그 수정",
            "텔레그램 HR 렌더링 ━ 구분선 완전 제거",
        ],
    },
    {
        "version": "3.0",
        "date":    "2026-05-20",
        "emoji":   "⚖️",
        "changes": [
            "V-REV 리밸런싱 모드 추가 (Buy1/Buy2, Pop1/Pop2 레이어)",
            "DB reverse_day 컬럼 추가",
            "스케줄러 분리 — scheduler_core.py / scheduler_trade.py",
            "MarketCalendar — KRX 공휴일 오프라인 테이블 (2025~2026)",
        ],
    },
    {
        "version": "2.0",
        "date":    "2026-05-10",
        "emoji":   "⚔️",
        "changes": [
            "승승장군 알고리즘 7개 갭 전면 수정",
            "t_val — 당일 체결건수 → 누적 보유수량 기반 추적",
            "star_ratio / star_price 감소 곡선 계산 엔진 구현",
            "전반전(50:50)·후반전(100%)·리버스 모드 분리",
            "1/4 별값매도(LOC) + 나머지 목표가매도(LIMIT) 이중 구조",
        ],
    },
    {
        "version": "1.0",
        "date":    "2026-05-01",
        "emoji":   "🌱",
        "changes": [
            "키움증권 REST API 연결 (mockapi.kiwoom.com)",
            "python-telegram-bot v20+ run_polling() 적용",
            "systemd etfbot.service 등록",
            "기본 무한매매 로직 초기 구현",
        ],
    },
]

# ==============================================================
# 헬퍼 함수
# ==============================================================

def get_version_string() -> str:
    """v5.0"""
    return VERSION_TAG


def get_full_title() -> str:
    """국내 ETF 무한매매 + AVWAP 봇 v5.0"""
    return f"{BOT_NAME} {VERSION_TAG}"


def get_telegram_version_text(active_symbols: list = None) -> str:
    """
    /version 명령어 텔레봇 출력용 텍스트 생성.
    active_symbols: [{"name": ..., "code": ..., "avwap_budget": ...}]
    """
    lines = [
        f"🔧 <b>[ 버전 및 업데이트 내역 ]</b>",
        f"",
        f"🚀 <b>{BOT_NAME}</b>",
        f"📌 현재 버전: <b>{VERSION_TAG}</b>",
        f"📅 릴리즈: {RELEASE_DATE}",
        f"🎯 전략: {' + '.join(CURRENT['strategies'])}",
    ]

    if active_symbols:
        lines += ["", "<b>[ 운용 종목 ]</b>"]
        for s in active_symbols:
            mode  = s.get("mode", "INFINITE")
            ab    = s.get("avwap_budget", 0)
            icon  = "💎" if mode == "INFINITE" else "⚖️"
            avwap = "  ⚡️AVWAP ON" if ab > 0 else ""
            lines.append(f"  {icon} {s['name']} ({s['code']}){avwap}")

    lines.append("")
    lines.append("<b>[ 업데이트 히스토리 ]</b>")

    for entry in HISTORY:
        lines += [
            "",
            f"{entry['emoji']} <b>{entry['version']}</b>  {entry['date']}",
        ]
        for change in entry["changes"][:4]:   # 최대 4개만 표시
            lines.append(f"  · {change}")

    lines += [
        "",
        "<b>[ 레퍼런스 ]</b>",
        "  무한매매 원작: 라오어님",
        "  AVWAP 엔진: 승승장군 V44~V79",
        "  github.com/pipios4006-boop/",
        "    KIS-API-Python-Trading-Bot-Example",
    ]

    return "\n".join(lines)


def get_readme_version_table() -> str:
    """README.md용 버전 테이블 마크다운 생성."""
    lines = [
        "| 버전 | 날짜 | 주요 변경 사항 |",
        "|------|------|--------------|",
    ]
    for entry in HISTORY:
        summary = entry["changes"][0]  # 첫 번째 변경사항만 요약
        star = " ★현재" if entry["version"] == VERSION else ""
        lines.append(
            f"| **v{entry['version']}{star}** "
            f"| {entry['date']} "
            f"| {summary} |"
        )
    return "\n".join(lines)


# ==============================================================
# CLI: python3 version.py 실행 시 문서 자동 업데이트
# ==============================================================
if __name__ == "__main__":
    import subprocess
    import sys
    import os

    print(f"🚀 {get_full_title()}")
    print(f"📅 릴리즈: {RELEASE_DATE}")
    print()

    # 어떤 파일을 업데이트할지 확인
    targets = {
        "README.md":          os.path.exists("README.md"),
        "telegram_bot.py":    os.path.exists("telegram_bot.py"),
        "ETF_Bot_Guide_v5.html": os.path.exists("ETF_Bot_Guide_v5.html"),
    }

    print("업데이트 대상:")
    for f, exists in targets.items():
        print(f"  {'✅' if exists else '❌ (없음)'} {f}")

    print()

    # 1. telegram_bot.py — VERSION 상수 업데이트
    if targets["telegram_bot.py"]:
        txt = open("telegram_bot.py").read()
        import re
        new_txt = re.sub(
            r'VERSION\s*=\s*"v[\d.]+"',
            f'VERSION = "{VERSION_TAG}"',
            txt
        )
        if new_txt != txt:
            open("telegram_bot.py", "w").write(new_txt)
            print(f"✅ telegram_bot.py — VERSION = \"{VERSION_TAG}\" 업데이트")
        else:
            print(f"✓  telegram_bot.py — 이미 최신 ({VERSION_TAG})")

    # 2. README.md — 버전 테이블 섹션 업데이트
    if targets["README.md"]:
        readme = open("README.md").read()
        table  = get_readme_version_table()
        # 버전 히스토리 섹션 교체
        new_readme = re.sub(
            r"(## 📋 버전 히스토리\n\n)[\s\S]*?(\n\n---)",
            r"\g<1>" + table + r"\g<2>",
            readme,
        )
        if new_readme != readme:
            open("README.md", "w").write(new_readme)
            print("✅ README.md — 버전 히스토리 테이블 업데이트")
        else:
            print("✓  README.md — 이미 최신")

    # 3. ETF_Bot_Guide_v5.html — 버전 배지 업데이트
    if targets["ETF_Bot_Guide_v5.html"]:
        html = open("ETF_Bot_Guide_v5.html").read()
        new_html = re.sub(
            r'<div class="hero-chip">v[\d.]+</div>',
            f'<div class="hero-chip">{VERSION_TAG}</div>',
            html
        )
        new_html = re.sub(
            r'Version [\d.]+ &nbsp;\|',
            f'Version {VERSION}  |',
            new_html
        )
        if new_html != html:
            open("ETF_Bot_Guide_v5.html", "w").write(new_html)
            print("✅ ETF_Bot_Guide_v5.html — 버전 배지 업데이트")
        else:
            print("✓  ETF_Bot_Guide_v5.html — 이미 최신")

    print()
    print("✅ 완료!")
    print()
    print("── 버전 테이블 미리보기 ──")
    print(get_readme_version_table())
