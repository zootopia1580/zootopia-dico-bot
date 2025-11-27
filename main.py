# main.py

"""
디스코드 음성 채널 출석 체크 봇 (Discord Voice Channel Attendance Bot)

[기능]
- 지정된 음성 채널의 사용자 입장/퇴장 시간을 기록하여 총 활동 시간을 계산합니다.
- '/data/attendance.db' SQLite 데이터베이스에 모든 기록을 저장합니다.
- 주간/월간 목표 달성 여부를 자동으로 정산하고 보고합니다.
- 사용자가 음성 채널 입장 후 봇에게 '!집중' DM을 보내면,
  현재 사용자의 음성 상태에 설정된 채널 상태 메시지를 가져와 채널에 공지합니다.
- '!진단' 명령어를 통해 현재 봇이 구동 중인 환경과 라이브러리 버전을 확인합니다.

[배포 환경]
- 이 봇은 Render의 Background Worker 서비스를 통해 배포됩니다.
- GitHub 저장소의 main 브랜치에 코드가 Push 되면 자동으로 빌드 및 배포가 진행됩니다.
"""

import os
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import aiosqlite
from datetime import datetime, timedelta, time, timezone
from collections import defaultdict
import calendar
import sys # 버전 확인용 라이브러리

# 이 메시지는 Render 배포 로그에서 최신 코드가 적용되었는지 확인하기 위한 표식입니다.
print("★★★★★ 최종 버전 봇 코드 실행 시작! (진단 기능 탑재) ★★★★★★")

# --- Local Imports ---
import config

# --- Bot Setup ---
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
KST = timezone(timedelta(hours=9))

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
intents.message_content = True
intents.dm_messages = True # DM 메시지를 받기 위해 필요합니다.

bot = commands.Bot(command_prefix=config.BOT_PREFIX, intents=intents)

# --- Global State ---
last_task_run = defaultdict(lambda: None)

# --- Database Functions ---
async def init_db():
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS active_sessions (
                user_id TEXT PRIMARY KEY NOT NULL,
                check_in TEXT NOT NULL
            )
        """)
        await db.commit()

# --- Helper Functions ---
def get_week_of_month(dt: datetime.date) -> int:
    first_day = dt.replace(day=1)
    dom = dt.day
    adjusted_dom = dom + first_day.weekday()
    return (adjusted_dom - 1) // 7 + 1

def split_session_by_day(check_in: datetime, check_out: datetime):
    sessions = []
    current_time = check_in
    while current_time.date() < check_out.date():
        end_of_day = datetime.combine(current_time.date(), time(23, 59, 59), tzinfo=current_time.tzinfo)
        sessions.append({
            "check_in": current_time.isoformat(), "check_out": end_of_day.isoformat(),
            "duration": (end_of_day - current_time).total_seconds()})
        current_time = end_of_day + timedelta(seconds=1)
    sessions.append({
        "check_in": current_time.isoformat(), "check_out": check_out.isoformat(),
        "duration": (check_out - current_time).total_seconds()})
    return sessions

async def get_today_total_duration(db, user_id: str, date_str: str) -> int:
    cursor = await db.execute("SELECT SUM(duration) FROM attendance WHERE user_id = ? AND check_in_date = ?", (user_id, date_str))
    row = await cursor.fetchone()
    return row[0] if row and row[0] is not None else 0

async def get_all_users_for_month(db, year: int, month: int):
    start_date = f"{year}-{month:02d}-01"
    end_date = f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]}"
    cursor = await db.execute("SELECT DISTINCT user_id FROM attendance WHERE check_in_date BETWEEN ? AND ?", (start_date, end_date))
    rows = await cursor.fetchall()
    return [row[0] for row in rows]

async def get_daily_durations(db, user_id: str, dates: list) -> dict:
    if not dates: return {}
    date_placeholders = ",".join("?" for d in dates)
    query = f"SELECT check_in_date, SUM(duration) FROM attendance WHERE user_id = ? AND check_in_date IN ({date_placeholders}) GROUP BY check_in_date"
    params = [user_id] + [d.isoformat() for d in dates]
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}

# --- Report Generation Logic ---
async def generate_weekly_status_line(db, user_id: str, dates: list):
    daily_durations = await get_daily_durations(db, user_id, dates)
    daily_goal = config.SPECIAL_USER_GOALS.get(user_id, config.DAILY_GOAL_SECONDS)
    line, pass_days = [], 0
    for d in dates:
        duration = daily_durations.get(d.isoformat(), 0)
        if duration >= daily_goal:
            line.append(config.STATUS_ICONS["pass"])
            pass_days += 1
        elif duration > 0: line.append(config.STATUS_ICONS["insufficient"])
        else: line.append(config.STATUS_ICONS["absent"])
    return " ".join(line), pass_days

async def build_weekly_mid_report(guild: discord.Guild, report_date: datetime.date):
    week_start = report_date - timedelta(days=report_date.weekday())
    dates = [week_start + timedelta(days=i) for i in range(4)]
    header = config.MESSAGE_HEADINGS["weekly_mid_check"].format(month=report_date.month, week=get_week_of_month(report_date))
    body = ["주말까지 이틀 남았어요! 현재까지의 출석 현황입니다.", "`월 화 수 목`"]
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        users = await get_all_users_for_month(db, report_date.year, report_date.month)
        for user_id in users:
            member = guild.get_member(int(user_id))
            if member:
                status_line, _ = await generate_weekly_status_line(db, user_id, dates)
                body.append(f"`{status_line}` {member.mention}")
    body.append(f"\n> (✅: 달성, ⚠️: 모자람, ❌: 안 들어옴)\n\n아직 시간이 충분해요. 모두 목표를 향해 달려봐요! 🚀")
    return "\n".join([header] + body)
    
async def build_manual_weekly_check_report(guild: discord.Guild, report_date: datetime.date):
    week_start = report_date - timedelta(days=report_date.weekday())
    num_days_to_show = report_date.weekday() + 1
    dates = [week_start + timedelta(days=i) for i in range(num_days_to_show)]
    weekday_labels = ["월", "화", "수", "목", "금", "토", "일"]
    
    header = f"[📢 현재 주간 현황] {report_date.month}월 {get_week_of_month(report_date)}주차"
    labels_line = " ".join(weekday_labels[:num_days_to_show])
    body = [f"오늘까지의 출석 현황입니다.", f"`{labels_line}`"]
    
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        users = await get_all_users_for_month(db, report_date.year, report_date.month)
        if not users:
            return "아직 이번 달 활동 기록이 없네요. 지금 바로 시작해보세요! 💪"
        for user_id in users:
            member = guild.get_member(int(user_id))
            if member:
                status_line, _ = await generate_weekly_status_line(db, user_id, dates)
                body.append(f"`{status_line}` {member.mention}")
    
    body.append(f"\n> (✅: 달성, ⚠️: 모자람, ❌: 안 들어옴)")
    return "\n".join([header] + body)

async def build_monthly_final_report(guild: discord.Guild, year: int, month: int):
    header = config.MESSAGE_HEADINGS["monthly_final"].format(month=month)
    exempt_users, charge_users = [], []
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        users = await get_all_users_for_month(db, year, month)
        if not users:
            return f"해당 월에는 출석 기록이 존재하지 않습니다."
        for user_id in users:
            total_successful_weeks = 0
            for week in calendar.monthcalendar(year, month):
                week_dates = [datetime(year, month, day).date() for day in week if day != 0]
                if not week_dates: continue
                _, pass_days_in_week = await generate_weekly_status_line(db, user_id, week_dates)
                if pass_days_in_week >= config.WEEKLY_GOAL_DAYS:
                    total_successful_weeks += 1
            member = guild.get_member(int(user_id))
            if member:
                user_line = f"• {member.mention} ({total_successful_weeks}주 성공)"
                if total_successful_weeks >= config.MONTHLY_GOAL_WEEKS: exempt_users.append(user_line)
                else: charge_users.append(user_line)

    body = [f"{year}년 {month}월 한 달간 모두 수고하셨습니다! 최종 사용료 정산 결과입니다."]
    body.append("\n**🎉 사용료 면제 대상**")
    body.extend(exempt_users if exempt_users else ["- 대상자가 없습니다."])
    body.append("\n**😥 사용료 부과 대상**")
    body.extend(charge_users if charge_users else ["- 대상자가 없습니다."])
    return "\n".join([header] + body)

# --- Bot Events ---
@bot.event
async def on_ready():
    await init_db()
    main_scheduler.start()
    print(f'{bot.user}으로 로그인 성공!')
    print("메인 스케줄러가 시작되었습니다.")

@bot.event
async def on_voice_state_update(member, before, after):
    """
    의도: 사용자의 음성 채널 '입장'과 '퇴장'만을 감지하여 출석을 기록합니다.
    """
    if member.bot:
        return

    text_channel = discord.utils.get(member.guild.text_channels, name=config.TEXT_CHANNEL_NAME)
    if not text_channel:
        return
    
    is_join = (before.channel is None or before.channel.name != config.VOICE_CHANNEL_NAME) and \
              (after.channel is not None and after.channel.name == config.VOICE_CHANNEL_NAME)

    is_leave = (before.channel is not None and before.channel.name == config.VOICE_CHANNEL_NAME) and \
               (after.channel is None or after.channel.name != config.VOICE_CHANNEL_NAME)

    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        if is_join:
            cursor = await db.execute("SELECT check_in FROM active_sessions WHERE user_id = ?", (str(member.id),))
            if await cursor.fetchone() is None:
                check_in_time = datetime.now(KST)
                await db.execute("INSERT INTO active_sessions (user_id, check_in) VALUES (?, ?)", (str(member.id), check_in_time.isoformat()))
                await db.commit()
                print(f"{member.display_name}님이 '{config.VOICE_CHANNEL_NAME}' 채널에 입장. DB에 기록.")
                await text_channel.send(f"{member.mention}님, 작업 시작! 🔥")
        
        elif is_leave:
            cursor = await db.execute("SELECT check_in FROM active_sessions WHERE user_id = ?", (str(member.id),))
            row = await cursor.fetchone()
            if row:
                check_in_time = datetime.fromisoformat(row[0])
                check_out_time = datetime.now(KST)
                
                await db.execute("DELETE FROM active_sessions WHERE user_id = ?", (str(member.id),))

                sessions_to_insert = split_session_by_day(check_in_time, check_out_time)
                for session in sessions_to_insert:
                    await db.execute("INSERT INTO attendance (user_id, check_in, check_out, duration, check_in_date) VALUES (?, ?, ?, ?, ?)",
                                     (str(member.id), session["check_in"], session["check_out"], session["duration"], datetime.fromisoformat(session["check_in"]).date().isoformat()))
                
                await db.commit()
                print(f"{member.display_name}님이 '{config.VOICE_CHANNEL_NAME}' 채널에서 퇴장. DB 업데이트.")

                involved_dates = sorted(list(set([datetime.fromisoformat(s["check_in"]).date() for s in sessions_to_insert])))
                time_report_parts = []
                for report_date in involved_dates:
                    total_seconds = await get_today_total_duration(db, str(member.id), report_date.isoformat())
                    hours, remainder = divmod(total_seconds, 3600)
                    minutes, _ = divmod(remainder, 60)
                    time_report_parts.append(f"> {report_date.day}일 총 작업 시간: {int(hours):02d}시간 {int(minutes):02d}분")
                
                time_report_message = "\n".join(time_report_parts)
                await text_channel.send(f"{member.mention}님 수고하셨습니다! 👏\n{time_report_message}")

@bot.event
async def on_message(message):
    """
    의도: 봇에게 오는 개인 메시지(DM)를 감지하여 '!집중' 명령어 처리.
          '!집중'만 입력 시: 현재 음성 채널 상태를 가져와 공지.
          '!집중 [내용]' 입력 시: 입력된 [내용]을 공지.
    """
    # Ignore messages from the bot itself or non-DM messages initially
    if message.author.bot or not isinstance(message.channel, discord.DMChannel):
        # Process commands only if it's not a DM and not from the bot
        if not isinstance(message.channel, discord.DMChannel) and not message.author.bot:
            await bot.process_commands(message)
        return # Stop processing if it's a bot message or not a relevant DM

    # --- DM Processing ---
    # Check if the DM starts with '!집중'
    if message.content.startswith('!집중'):
        command_content = message.content.strip() # Remove leading/trailing whitespace

        # --- Case 1: Automatic fetch (!집중 only) ---
        if command_content == '!집중':
            # 1. Find the guild (server)
            guild = bot.guilds[0] if bot.guilds else None
            if not guild:
                await message.channel.send("오류: 봇이 속한 서버를 찾을 수 없습니다.")
                return

            # 2. Find the member object in the guild
            member = guild.get_member(message.author.id)
            if not member:
                await message.channel.send("오류: 서버에서 사용자님을 찾을 수 없습니다.")
                return

            # 3. Check if the member is in the target voice channel
            if not member.voice or not member.voice.channel or member.voice.channel.name != config.VOICE_CHANNEL_NAME:
                await message.channel.send(f"앗! '{config.VOICE_CHANNEL_NAME}' 음성 채널에 먼저 입장하셔야 `!집중` 명령어를 사용할 수 있어요. 😮")
                return

            # 4. Try to get the voice channel status
            try:
                # ★★★ [중요] Render 환경 디버깅용 - status 속성이 있는지 확인합니다 ★★★
                task_description = member.voice.channel.status
            except AttributeError:
                await message.channel.send(f"⚠️ 서버 환경 오류: `discord.py` 라이브러리 버전이 낮아 채널 상태를 가져올 수 없습니다.\n(현재 버전: {discord.__version__})")
                return 
            except Exception as e:
                await message.channel.send(f"채널 상태를 가져오는 중 예상치 못한 오류가 발생했어요: {e}")
                return

            if not task_description:
                await message.channel.send("음... 😅 음성 채널의 상태 메시지가 비어있어요. 먼저 채널 상태를 설정해주세요!")
                return

            # 5. Find the announcement channel
            text_channel = discord.utils.get(guild.text_channels, name=config.TEXT_CHANNEL_NAME)
            if not text_channel:
                await message.channel.send(f"오류: 서버에서 '{config.TEXT_CHANNEL_NAME}' 채널을 찾을 수 없습니다.")
                return

            # 6. Send the announcement
            announcement = f"{member.mention} 님이 '**{task_description}**' 집중 타임을 오픈했습니다! 함께 달려보세요!"
            await text_channel.send(announcement)

            # 7. Send confirmation DM
            await message.channel.send(f"🔥 좋아요! '**{task_description}**' 집중 타임 시작을 모두에게 알렸어요. 파이팅! 💪")

        # --- Case 2: Manual input (!집중 [text]) ---
        elif command_content.startswith('!집중 '):
            task_description = command_content.replace('!집중', '', 1).strip() 

            if not task_description:
                await message.channel.send("앗, 어떤 일에 집중할지 알려주세요! 🤔 (예: `!집중 최종 기획서 마무리`)")
                return

            guild = bot.guilds[0] if bot.guilds else None
            if not guild:
                await message.channel.send("오류: 봇이 속한 서버를 찾을 수 없습니다.")
                return

            text_channel = discord.utils.get(guild.text_channels, name=config.TEXT_CHANNEL_NAME)
            if not text_channel:
                await message.channel.send(f"오류: 서버에서 '{config.TEXT_CHANNEL_NAME}' 채널을 찾을 수 없습니다.")
                return
            
            announcement = f"{message.author.mention} 님이 '**{task_description}**' 집중 타임을 오픈했습니다! 함께 달려보세요!"
            await text_channel.send(announcement)

            await message.channel.send(f"🔥 좋아요! '**{task_description}**' 집중 타임 시작을 모두에게 알렸어요. 파이팅! 💪")

# --- Bot Commands ---
@bot.command(name="현황")
async def weekly_check_command(ctx):
    await ctx.send("이번 주 출석 현황을 집계 중입니다... 🗓️")
    report_message = await build_manual_weekly_check_report(ctx.guild, datetime.now(KST).date())
    await ctx.send(report_message)

@bot.command(name="월간결산")
async def monthly_check_command(ctx, month: int = None):
    now = datetime.now(KST)
    year = now.year
    if month is None:
        target_date = now.date() - timedelta(days=now.day)
        month = target_date.month
    
    if not (1 <= month <= 12):
        await ctx.send("올바른 월(1-12)을 입력해주세요.")
        return

    await ctx.send(f"**{year}년 {month}월** 최종 결산 내역을 불러오는 중... 🏆")
    report_message = await build_monthly_final_report(ctx.guild, year, month)
    await ctx.send(report_message)

# --- [NEW] 진단 명령어 ---
@bot.command(name="진단")
async def diagnose(ctx):
    import discord
    import sys
    
    # 1. 현재 설치된 라이브러리 버전 확인
    version_info = f"🐍 Python 버전: {sys.version}\n🤖 discord.py 버전: {discord.__version__}"
    
    # 2. 사용자가 음성 채널에 있는지 확인하고, 채널 속성 뜯어보기
    status_check = ""
    if ctx.author.voice and ctx.author.voice.channel:
        channel = ctx.author.voice.channel
        # 채널 객체가 가진 모든 속성 이름을 가져옵니다.
        attributes = dir(channel)
        
        if 'status' in attributes:
            status_check = f"\n✅ '{channel.name}' 채널에 'status' 속성이 존재합니다! (값: {getattr(channel, 'status', 'None')})"
        else:
            status_check = f"\n❌ '{channel.name}' 채널에 'status' 속성이 없습니다.\n(이것은 라이브러리가 구버전이라는 강력한 증거입니다)"
    else:
        status_check = "\n⚠️ 음성 채널에 들어온 상태로 '!진단'을 입력하면 더 자세히 볼 수 있어요."

    await ctx.send(f"```{version_info}{status_check}```")

# --- Run Bot ---
if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("에러: DISCORD_BOT_TOKEN이 설정되지 않았습니다. .env 파일을 확인해주세요.")
