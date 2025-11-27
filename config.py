# config.py

# --- Discord Bot Settings ---
BOT_PREFIX = "!"

# --- Channel Settings (ID 기반) ---
# 고독한작업방 ID (이름이 바뀌어도 봇이 찾아갈 수 있습니다)
VOICE_CHANNEL_ID = 1339546362794086450

# 출석체크 채팅방 ID
TEXT_CHANNEL_ID = 1339546362567725081

# --- Attendance Rules ---
DAILY_GOAL_SECONDS = 7200  # 기본 하루 목표 (2시간) - 명단에 없는 사람에게 적용
WEEKLY_GOAL_DAYS = 4       # 주간 목표 달성 필요 일수 (4일)
MONTHLY_GOAL_WEEKS = 3     # 월간 목표 달성 필요 주수 (3주)

# --- Special User Settings (개별 목표 시간) ---
# 형식: "디스코드ID": 목표초(seconds)
SPECIAL_USER_GOALS = {
    # [취준이 - 4시간 (14400초)]
    "1339540906914746390": 14400, # 혜민
    "805463906620669972": 14400,  # 승주
    "1196364716147216415": 14400, # 수빈
    "967781976486608916": 14400,  # 지혜
    "900314845667295262": 14400,  # 지연
    "752488606353850408": 14400,  # 서현

    # [이준이 - 2시간 (7200초)]
    "1216225553686859788": 7200,  # 다인
    "900000344602443857": 7200,   # 선빈
    "968492300642697237": 7200,   # 성민
}

# --- Database Settings ---
DATABASE_NAME = "/data/attendance.db"

# --- Presentation Settings ---
# 출석 상태 아이콘
STATUS_ICONS = {
    "pass": "✅",        # 목표 달성
    "insufficient": "⚠️", # 시간 모자람
    "absent": "❌",      # 결석
}

# 자동 알림 메시지 제목
MESSAGE_HEADINGS = {
    "weekly_mid_check": "[🔥 주중 파이팅] {month}월 {week}주차 중간 점검",
    "weekly_final": "[✅ 주간 결산] {month}월 {week}주차 결과 확정",
    "monthly_mid_check": "[🚨 월간 중간 정산] {month}월 사용료 면제까지 남은 조건!",
    "monthly_final": "[🏆 월간 최종 정산] {month}월 결과 및 데이터 초기화",
}
