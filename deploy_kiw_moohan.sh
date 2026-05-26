#!/bin/bash
# ============================================================
# 키움 2배ETF 무한매매 봇 배포 스크립트
# 실행: bash ~/deploy_kiw_moohan.sh
# ============================================================

TARGET=~/kiw_moohan_trader
SERVICE=etfbot

echo "📦 홈의 .py / .json / .md / .txt 파일을 kiw_moohan_trader로 이동 중..."
moved=0
for f in ~/*.py ~/config.json ~/requirements.txt ~/README.md ~/SETUP_GUIDE.md; do
    if [ -f "$f" ]; then
        mv "$f" "$TARGET/"
        echo "  ✅ $(basename $f) 이동 완료"
        moved=$((moved + 1))
    fi
done
if [ $moved -eq 0 ]; then
    echo "  ℹ️  이동할 파일 없음"
fi

echo "🗑️  캐시 삭제 중..."
find "$TARGET" -name "*.pyc" -delete
find "$TARGET" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
echo "  ✅ 캐시 삭제 완료"

echo "🔄 $SERVICE 재시작 중..."
sudo systemctl restart "$SERVICE"
sleep 3

echo ""
echo "📋 서비스 상태:"
sudo systemctl status "$SERVICE" --no-pager -l

echo ""
echo "📋 최근 로그 (etf_bot.log):"
tail -n 20 "$TARGET/logs/etf_bot.log" 2>/dev/null || echo "  로그 파일 없음"
