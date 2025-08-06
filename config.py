# config.py

# --- Discord Bot Settings ---
BOT_PREFIX = "!"

# --- Channel Settings ---
VOICE_CHANNEL_NAME = "고독한작업방"
TEXT_CHANNEL_NAME = "출석체크"

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
# 출석 상태 아이콘 정의
STATUS_ICONS = {
    "pass": "✅",         # 목표 달성
    "insufficient": "⚠️", # 시간 모자람
    "absent": "❌",       # 접속 안함
}

# 자동 알림 메시지 제목
MESSAGE_HEADINGS = {
    "weekly_mid_check": "[🔥 주중 파이팅] {month}월 {week}주차 중간 점검",
    "weekly_final": "[✅ 주간 결산] {month}월 {week}주차 결과 확정",
    "monthly_mid_check": "[🚨 월간 중간 정산] {month}월 사용료 면제까지 남은 조건!",
    "monthly_final": "[🏆 월간 최종 정산] {month}월 결과 및 데이터 초기화",
}
