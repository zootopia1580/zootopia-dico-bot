# config.py

# --- Discord Bot Settings ---
BOT_PREFIX = "!"

# --- Channel Settings (ID 기반) ---
# 고독한작업방 ID
VOICE_CHANNEL_ID = 1339546362794086450

# 출석체크 채팅방 ID (입/퇴장 알림, 주간 리포트용)
TEXT_CHANNEL_ID = 1339546362567725081

# [NEW] 공지 채널 ID (주간 목표 공지, 월간 최종 정산용)
NOTICE_CHANNEL_ID = 1339546362567725084

# --- Attendance Rules ---
DAILY_GOAL_SECONDS = 7200  # 기본 2시간
WEEKLY_GOAL_DAYS = 4       # 4일
MONTHLY_GOAL_WEEKS = 3     # 3주

# --- User Groups & Goals ---
USER_GROUPS = {
    "🔥 취준이 (일 4시간)": {
        "goal_seconds": 14400,
        "members": [
            1339540906914746390, # 혜민
            805463906620669972,  # 승주
            1196364716147216415, # 수빈
            967781976486608916,  # 지혜
            900314845667295262,  # 지연
            752488606353850408   # 서현
        ]
    },
    "💼 이준이 (일 2시간)": {
        "goal_seconds": 7200,
        "members": [
            1216225553686859788, # 다인
            900000344602443857,  # 선빈
            968492300642697237   # 성민
        ]
    }
}

def get_user_goal(user_id):
    uid = int(user_id)
    for group in USER_GROUPS.values():
        if uid in group["members"]:
            return group["goal_seconds"]
    return DAILY_GOAL_SECONDS

# --- Database Settings ---
DATABASE_NAME = "/data/attendance.db"

# --- Presentation Settings ---
STATUS_ICONS = {"pass": "✅", "insufficient": "⚠️", "absent": "❌"}
MESSAGE_HEADINGS = {
    "weekly_mid_check": "[🔥 주중 파이팅] {month}월 {week}주차 중간 점검",
    "weekly_final": "[✅ 주간 결산] {month}월 {week}주차 결과 확정",
    "monthly_mid_check": "[🚨 월간 중간 정산] {month}월 사용료 면제까지 남은 조건!",
    "monthly_final": "[🏆 월간 최종 정산] {month}월 결과 및 데이터 초기화",
}
