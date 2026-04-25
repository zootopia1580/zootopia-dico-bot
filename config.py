import os

# --- Discord Bot Settings ---
BOT_PREFIX = "!"

# --- Channel Settings ---
VOICE_CHANNEL_ID  = 1339546362794086450
TEXT_CHANNEL_ID   = 1339546362567725081
NOTICE_CHANNEL_ID = 1339546362567725084

# --- Database ---
DATABASE_NAME = "data/attendance.db"

# --- 주간 시간 단계 ---
WEEKLY_TIERS = [
    (9*3600,  None,    "🏆", "레전드"),
    (7*3600,  9*3600,  "💪", "거의 다 왔어"),
    (5*3600,  7*3600,  "⚡", "달리는 중"),
    (3*3600,  5*3600,  "🔥", "불붙었다"),
    (1,       3*3600,  "🌱", "뭐라도 했잖아"),
    (0,       1,       "⬜", "이번 주 쉬었나요"),
]

def get_weekly_tier(total_seconds):
    for min_s, max_s, emoji, label in WEEKLY_TIERS:
        if total_seconds >= min_s and (max_s is None or total_seconds < max_s):
            return emoji, label
    return "⬜", "이번 주 쉬었나요"

# --- 입장 멘트 ---
JOIN_MESSAGES_DEFAULT = [
    "{mention} 님 입장! 🔥 다들 자극받으세요~",
    "{mention} 님이 오셨습니다 👀 슬슬 자리 잡으실 분?",
    "{mention} 님 작업 시작! 💪 같이 달리실 분 계신가요?",
    "{mention} 님 등장 ✨ 오늘도 열심히 하실 것 같은 분위기인데요?",
    "{mention} 님이 먼저 시작하셨네요 🏃 늦으시면 안 됩니다!",
]

JOIN_MESSAGES_EVENING = [
    "{mention} 님 퇴근 후 바로 달리러 오셨어요 🔥 다들 보셨죠?",
    "{mention} 님 칼퇴 후 즉시 입장 💨 자극 한 번 받아가세요~",
    "{mention} 님 저녁에도 이러시면 다들 따라올 수밖에 없죠 👏",
]

JOIN_MESSAGES_NIGHT = [
    "{mention} 님 새벽에도 오셨네요 🌙 이 분위기 다들 느끼시죠?",
    "{mention} 님 지금 이 시간에 입장하시면 저도 자극받습니다 😤",
    "{mention} 님 새벽 자기계발 스타트 🌙 같이 하실 분?",
]

# --- 퇴장 멘트 ---
LEAVE_MESSAGES_DEFAULT = [
    "{mention} 님 오늘도 수고하셨어요 👏",
    "{mention} 님 고생하셨어요! 오늘 잘 달리셨네요 🔥",
    "{mention} 님 퇴장! 오늘 하루도 열심히 하셨습니다 ✨",
    "{mention} 님 오늘 작업 마무리! 내일도 달려봐요 💪",
]

LEAVE_MESSAGES_EVENING = [
    "{mention} 님 퇴근 후 달리고 가시네요 👏 대단합니다",
    "{mention} 님 저녁 시간 알차게 쓰셨어요 🔥",
    "{mention} 님 오늘 저녁도 알차게! 수고하셨어요 ✨",
]

LEAVE_MESSAGES_NIGHT = [
    "{mention} 님 새벽까지 하셨네요, 푹 쉬세요 🌙",
    "{mention} 님 이 시간까지 고생하셨어요 😤 내일도 화이팅!",
    "{mention} 님 새벽 작업 마무리! 오늘도 대단하셨어요 🌙",
]

# --- 인원수 이벤트 ---
HEADCOUNT_MESSAGES = {
    2: "어? 작업방에 2명이 모였어요 👀 혼자 계신 분들 슬슬 오실 타이밍!",
    3: "작업방 3명 돌파 🔥 분위기 달아오르고 있어요~",
    4: "벌써 4명이나 계시네요 💪 합류 안 하시면 손해예요!",
    5: "작업방 절반 이상 모였습니다 ⚡ 이 에너지 느껴지시나요?",
    6: "6명 입장 완료 🎯 슬슬 다 모이는 것 같은데요?",
    7: "7명이나 계세요 🔥🔥 오늘 작업방 분위기 심상치 않습니다",
    8: "8명 달성 👀 이제 한 명만 더!",
    9: "🎉 전원 집합!! 오늘 이 순간 기억해두세요. 역대급입니다",
}
