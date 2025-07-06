# main.py

import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import aiosqlite
from datetime import datetime, timedelta, time
from collections import defaultdict

# --- Local Imports ---
import config

# --- Bot Setup ---
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix=config.BOT_PREFIX, intents=intents)

# --- Global Variables ---
active_checkins = {}

# --- Database Functions ---
async def init_db():
    """데이터베이스와 테이블이 없으면 생성합니다."""
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                check_in TEXT NOT NULL,
                check_out TEXT NOT NULL,
                duration INTEGER NOT NULL,
                check_in_date TEXT NOT NULL
            )
        """)
        await db.commit()

# --- Helper Functions ---
def split_session_by_day(check_in: datetime, check_out: datetime):
    """자정을 넘긴 활동을 날짜별로 분리합니다."""
    sessions = []
    current_time = check_in
    while current_time.date() < check_out.date():
        end_of_day = datetime.combine(current_time.date(), time(23, 59, 59))
        sessions.append({
            "check_in": current_time.isoformat(),
            "check_out": end_of_day.isoformat(),
            "duration": (end_of_day - current_time).total_seconds()
        })
        current_time = end_of_day + timedelta(seconds=1)
    sessions.append({
        "check_in": current_time.isoformat(),
        "check_out": check_out.isoformat(),
        "duration": (check_out - current_time).total_seconds()
    })
    return sessions

# --- Bot Events ---
@bot.event
async def on_ready():
    """봇이 준비되면 DB를 초기화하고, 현재 음성 채널 상태를 확인하여 출석을 동기화합니다."""
    await init_db()
    print(f'{bot.user}으로 로그인 성공!')

    # [중요] 재시작 시 상태 복구 로직
    for guild in bot.guilds:
        voice_channel = discord.utils.get(guild.voice_channels, name=config.VOICE_CHANNEL_NAME)
        if voice_channel:
            for member in voice_channel.members:
                if not member.bot and member.id not in active_checkins:
                    # 봇이 꺼져있는 동안 들어온 멤버들의 출석을 지금부터 기록 시작
                    active_checkins[member.id] = datetime.now()
                    print(f"[상태 복구] {member.name}님이 이미 채널에 있어 출석을 시작합니다.")


@bot.event
async def on_voice_state_update(member, before, after):
    """사용자의 음성 채널 상태 변경을 감지하고 출석 기록 및 인원수 알림을 보냅니다."""
    if member.bot or before.channel == after.channel:
        return

    try:
        text_channel = discord.utils.get(member.guild.text_channels, name=config.TEXT_CHANNEL_NAME)
        if not text_channel:
            return # 채널이 없으면 조용히 종료
    except Exception:
        return # 예외 발생 시 조용히 종료

    # 사용자가 지정된 음성 채널에 '들어온' 경우
    if after.channel and after.channel.name == config.VOICE_CHANNEL_NAME:
        if member.id not in active_checkins:
            active_checkins[member.id] = datetime.now()
            print(f"{member.name}님이 '{config.VOICE_CHANNEL_NAME}' 채널에 입장.")

        current_member_count = len(after.channel.members)
        if current_member_count == 5:
            await text_channel.send("🎉 작업방 인원 5명 돌파! 다들 캠을 키고 모각디를 해주세요 🔥")
        elif current_member_count == 9:
            await text_channel.send("🚀 작업방에 전원 등장! 다들 캠을 켜고 열심히 작업해주세요 ✨")

    # 사용자가 지정된 음성 채널에서 '나간' 경우
    elif before.channel and before.channel.name == config.VOICE_CHANNEL_NAME:
        check_in_time = active_checkins.pop(member.id, None)
        if not check_in_time:
            return

        check_out_time = datetime.now()
        print(f"{member.name}님이 '{config.VOICE_CHANNEL_NAME}' 채널에서 퇴장.")
        
        async with aiosqlite.connect(config.DATABASE_NAME) as db:
            if check_in_time.date() == check_out_time.date():
                duration = (check_out_time - check_in_time).total_seconds()
                await db.execute(
                    "INSERT INTO attendance (user_id, check_in, check_out, duration, check_in_date) VALUES (?, ?, ?, ?, ?)",
                    (str(member.id), check_in_time.isoformat(), check_out_time.isoformat(), duration, check_in_time.date().isoformat())
                )
            else:
                sessions = split_session_by_day(check_in_time, check_out_time)
                for session in sessions:
                    check_in_dt = datetime.fromisoformat(session["check_in"])
                    await db.execute(
                        "INSERT INTO attendance (user_id, check_in, check_out, duration, check_in_date) VALUES (?, ?, ?, ?, ?)",
                        (str(member.id), session["check_in"], session["check_out"], session["duration"], check_in_dt.date().isoformat())
                    )
            await db.commit()

# --- Bot Commands ---
@bot.command()
async def 현황(ctx):
    """이번 주 출석 현황을 요약하고, 주간 목표 달성 여부를 함께 보여줍니다."""
    today = datetime.now().date()
    week_start = today - timedelta(days=today.weekday())
    days = [(week_start + timedelta(days=i)).isoformat() for i in range(7)]
    weekday_labels = config.WEEKDAY_LABELS.split()

    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        query = """
            SELECT user_id, check_in_date, SUM(duration) as total_duration
            FROM attendance
            WHERE check_in_date BETWEEN ? AND ?
            GROUP BY user_id, check_in_date
        """
        cursor = await db.execute(query, (days[0], days[-1]))
        records = await cursor.fetchall()

    user_stats = defaultdict(lambda: {"daily_status": {}, "pass_days": 0})
    for user_id, date_str, total_duration in records:
        stats = user_stats[user_id]
        if total_duration >= config.DAILY_GOAL_SECONDS:
            stats["daily_status"][date_str] = config.STATUS_ICONS["pass"]
            stats["pass_days"] += 1
        else:
            stats["daily_status"][date_str] = config.STATUS_ICONS["fail"]

    if not user_stats:
        await ctx.send("이번 주 출석 기록이 없습니다.")
        return

    response_lines = [
        "[ 이번 주 출석 현황 ]",
        " ".join(weekday_labels)
    ]
    
    for user_id, stats in user_stats.items():
        daily_line = " ".join([stats["daily_status"].get(d, config.STATUS_ICONS["no_record"]) for d in days])
        
        weekly_result = config.WEEKLY_STATUS_MESSAGES["pass"] if stats["pass_days"] >= config.WEEKLY_GOAL_DAYS else config.WEEKLY_STATUS_MESSAGES["fail"]
        
        try:
            member = await ctx.guild.fetch_member(user_id)
            user_display = member.mention
        except discord.NotFound:
            user_display = f"ID:{user_id}(서버에 없음)"

        response_lines.append(f"{user_display}: {daily_line}  {weekly_result}")

    await ctx.send("\n".join(response_lines))

@bot.command()
async def 데이터정리(ctx):
    """7일 이상된 출석 데이터를 삭제합니다."""
    seven_days_ago = (datetime.now().date() - timedelta(days=7)).isoformat()
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        await db.execute("DELETE FROM attendance WHERE check_in_date < ?", (seven_days_ago,))
        await db.commit()
    await ctx.send("7일 이전의 출석 데이터가 정리되었습니다!")

# --- Run Bot ---
if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("에러: DISCORD_BOT_TOKEN이 설정되지 않았습니다. .env 파일을 확인해주세요.")
