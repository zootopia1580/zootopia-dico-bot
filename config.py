# config.py

# --- Discord Bot Settings ---
BOT_PREFIX = "!"

# --- Channel Settings (ID 기반) ---
# 고독한작업방 ID
VOICE_CHANNEL_ID = 1339546362794086450

# 출석체크 채팅방 ID
TEXT_CHANNEL_ID = 1339546362567725081

# --- Attendance Rules ---
DAILY_GOAL_SECONDS = 7200  # 하루 목표 시간 (2시간)
WEEKLY_GOAL_DAYS = 4      # 주간 목표 달성 필요 일수 (4일)
MONTHLY_GOAL_WEEKS = 3    # 월간 목표 달성 필요 주수 (3주)

# --- Special User Settings ---
SPECIAL_USER_GOALS = {
    "1339540906914746390": 14400  # 4시간
}

# --- Database Settings ---
DATABASE_NAME = "/data/attendance.db"

# --- Presentation Settings ---
STATUS_ICONS = {
    "pass": "✅",
    "insufficient": "⚠️",
    "absent": "❌",
}

MESSAGE_HEADINGS = {
    "weekly_mid_check": "[🔥 주중 파이팅] {month}월 {week}주차 중간 점검",
    "weekly_final": "[✅ 주간 결산] {month}월 {week}주차 결과 확정",
    "monthly_mid_check": "[🚨 월간 중간 정산] {month}월 사용료 면제까지 남은 조건!",
    "monthly_final": "[🏆 월간 최종 정산] {month}월 결과 및 데이터 초기화",
}
