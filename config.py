# config.py

# --- Discord Bot Settings ---
BOT_PREFIX = "!"

# --- Channel Settings ---
# 봇이 작동할 음성 채널과 텍스트 채널의 이름
VOICE_CHANNEL_NAME = "고독한작업방"
TEXT_CHANNEL_NAME = "출석체크"

# --- Attendance Rules ---
# 하루 출석으로 인정될 최소 시간 (초 단위)
# 2시간 = 2 * 60 * 60
DAILY_GOAL_SECONDS = 7200

# 주간 목표 달성을 위해 필요한 최소 출석일수
WEEKLY_GOAL_DAYS = 4

# --- Special User Settings ---
# 특정 사용자를 위한 예외 규칙 (사용자 ID: 목표 시간(초))
SPECIAL_USER_GOALS = {
    "1339540906914746390": 14400  # 4시간
}

# --- Database Settings ---
DATABASE_NAME = "/data/attendance.db"

# --- Presentation Settings ---
WEEKDAY_LABELS = "월 화 수 목 금 토 일"
STATUS_ICONS = {
    "pass": "✅",  # 하루 목표 달성
    "fail": "❌",  # 하루 목표 실패
    "no_record": "👻" # 기록 없음
}
WEEKLY_STATUS_MESSAGES = {
    "pass": "(이번주 목표 달성! 🎉)",
    "fail": "(이번주 목표 미달성... 😥)"
}
