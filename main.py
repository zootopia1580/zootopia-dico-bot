# main.py

import os
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import aiosqlite
from datetime import datetime, timedelta, time, timezone
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

async def get_today_total_duration(db, user_id: str, date_str: str) -> int:
    """특정 사용자의 오늘 총 작업 시간을 초 단위로 반환합니다."""
    cursor = await db.execute(
        "SELECT SUM(duration) FROM attendance WHERE user_id = ? AND check_in_date = ?",
        (user_id, date_str)
    )
    row = await cursor.fetchone()
    return row[0] if row and row[0] is not None else 0

async def generate_weekly_report(guild: discord.Guild) -> str:
    """주간 출석 현황 리포트 문자열을 생성합니다."""
    today = datetime.now().date()
    week_start = today - timedelta(days=today.weekday())
    days = [(week_start + timedelta(days=i)).isoformat() for i in range(7)]

    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        query = "SELECT user_id, check_in_date, SUM(duration) as total_duration FROM attendance WHERE check_in_date BETWEEN ? AND ? GROUP BY user_id, check_in_date"
        cursor = await db.execute(query, (days[0], days[-1]))
        records = await cursor.fetchall()

    if not records:
        return "이번 주 출석 기록이 없습니다."

    user_stats = defaultdict(lambda: {"daily_status": {}, "pass_days": 0})
    for user_id_str, date_str, total_duration in records:
        daily_goal = config.SPECIAL_USER_GOALS.get(user_id_str, config.DAILY_GOAL_SECONDS)
        stats = user_stats[user_id_str]
        if total_duration >= daily_goal:
            stats["daily_status"][date_str] = config.STATUS_ICONS["pass"]
            stats["pass_days"] += 1
        else:
            stats["daily_status"][date_str] = config.STATUS_ICONS["fail"]

    weekday_labels = config.WEEKDAY_LABELS.split()
    response_lines = ["[ 이번 주 출석 현황 ]", " ".join(weekday_labels)]

    for user_id_str, stats in user_stats.items():
        daily_line = " ".join([stats["daily_status"].get(d, config.STATUS_ICONS["no_record"]) for d in days])
        weekly_result = config.WEEKLY_STATUS_MESSAGES["pass"] if stats["pass_days"] >= config.WEEKLY_GOAL_DAYS else config.WEEKLY_STATUS_MESSAGES["fail"]
        
        try:
            member = await guild.fetch_member(int(user_id_str))
            user_display = member.mention
        except (discord.NotFound, ValueError):
            user_display = f"ID:{user_id_str}(서버에 없음)"

        response_lines.append(f"{user_display}: {daily_line}  {weekly_result}")

    return "\n".join(response_lines)

# --- Bot Events ---
@bot.event
async def on_ready():
    """봇이 준비되면 DB를 초기화하고, 자동 리포트 작업을 시작하며, 현재 음성 채널 상태를 동기화합니다."""
    await init_db()
    send_weekly_report.start()  # 자동 리포트 작업 시작
    print(f'{bot.user}으로 로그인 성공!')
    print(f"자동 주간 리포트 기능이 활성화되었습니다. 매주 일요일 18:00에 전송됩니다.")

    for guild in bot.guilds:
        voice_channel = discord.utils.get(guild.voice_channels, name=config.VOICE_CHANNEL_NAME)
        if voice_channel:
            for member in voice_channel.members:
                if not member.bot and member.id not in active_checkins:
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
            return
    except Exception:
        return

    # 사용자가 지정된 음성 채널에 '들어온' 경우
    if after.channel and after.channel.name == config.VOICE_CHANNEL_NAME:
        if member.id not in active_checkins:
            active_checkins[member.id] = datetime.now()
            print(f"{member.name}님이 '{config.VOICE_CHANNEL_NAME}' 채널에 입장.")
            await text_channel.send(f"{member.mention}님, 작업 시작! 🔥")

        current_member_count = len(after.channel.members)
        if current_member_count == 3:
            await text_channel.send("💻 작업방 인원 3명! 다들 캠을 켜고 집중해주세요!")
        elif current_member_count == 5:
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
            # 1. 방금 끝난 세션 DB에 저장
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

            # 2. 오늘 하루의 총 누적 작업 시간 계산
            today_str = datetime.now().date().isoformat()
            total_seconds_today = await get_today_total_duration(db, str(member.id), today_str)

            hours, remainder = divmod(total_seconds_today, 3600)
            minutes, _ = divmod(remainder, 60)
            
            duration_text = f"{int(hours):02d}시간 {int(minutes):02d}분"

            # 3. 누적 시간을 담아 퇴장 메시지 전송
            await text_channel.send(f"{member.mention}님, 오늘 작업시간 {duration_text} 👏")

# --- Bot Commands ---
@bot.command()
async def 현황(ctx):
    """이번 주 출석 기록이 있는 모든 사용자의 현황을 보여줍니다."""
    report_message = await generate_weekly_report(ctx.guild)
    await ctx.send(report_message)


@bot.command()
async def 데이터정리(ctx):
    """7일 이상된 출석 데이터를 삭제합니다."""
    seven_days_ago = (datetime.now().date() - timedelta(days=7)).isoformat()
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        await db.execute("DELETE FROM attendance WHERE check_in_date < ?", (seven_days_ago,))
        await db.commit()
    await ctx.send("7일 이전의 출석 데이터가 정리되었습니다!")

# --- Scheduled Tasks ---
# 한국 시간(KST, UTC+9) 기준 매일 저녁 6시(18:00)에 실행되도록 설정
KST = timezone(timedelta(hours=9))
report_time = time(hour=18, minute=0, tzinfo=KST)

@tasks.loop(time=report_time)
async def send_weekly_report():
    """매주 일요일에 주간 리포트를 자동으로 전송합니다."""
    # 루프가 실행되는 오늘이 일요일(weekday() == 6)인지 확인
    if datetime.now(KST).weekday() == 6:
        print(f"[{datetime.now(KST)}] 정기 주간 리포트를 전송합니다.")
        for guild in bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=config.TEXT_CHANNEL_NAME)
            if channel:
                report_message = await generate_weekly_report(guild)
                await channel.send(report_message)
            else:
                print(f"'{guild.name}' 서버에서 '{config.TEXT_CHANNEL_NAME}' 채널을 찾을 수 없습니다.")

# --- Run Bot ---
if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("에러: DISCORD_BOT_TOKEN이 설정되지 않았습니다. .env 파일을 확인해주세요.")
