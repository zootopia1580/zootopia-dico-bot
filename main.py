# main.py

"""
디스코드 음성 채널 출석 체크 봇 (Discord Voice Channel Attendance Bot)

[기능]
- 지정된 음성 채널의 사용자 입장/퇴장 시간을 기록하여 총 활동 시간을 계산합니다.
- '/data/attendance.db' SQLite 데이터베이스에 모든 기록을 저장합니다.
- 주간/월간 목표 달성 여부를 자동으로 정산하고 보고합니다.
- 사용자가 음성 채널의 '상태'를 설정하면, 이를 감지하여 다른 사용자들에게 알립니다.

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

# 이 메시지는 Render 배포 로그에서 최신 코드가 적용되었는지 확인하기 위한 표식입니다.
print("★★★★★ 최종 버전 봇 코드 실행 시작! ★★★★★")

# --- Local Imports ---
import config

# --- Bot Setup ---
# .env 파일에서 환경 변수(봇 토큰 등)를 불러옵니다.
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
KST = timezone(timedelta(hours=9)) # 한국 시간대 설정

# 봇이 서버로부터 어떤 정보를 받을지 (Intents) 설정합니다.
# 음성 채널 상태, 서버 멤버 정보, 메시지 내용을 감지해야 하므로 모두 활성화합니다.
intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
intents.message_content = True

# 봇 객체를 생성합니다. 명령어 앞에는 '!'가 붙습니다.
bot = commands.Bot(command_prefix=config.BOT_PREFIX, intents=intents)

# --- Global State ---
# 스케줄러의 중복 실행을 방지하기 위해 마지막 실행 시간을 기록하는 변수입니다.
last_task_run = defaultdict(lambda: None)

# --- Database Functions ---
async def init_db():
    """
    의도: 봇 실행 시 데이터베이스와 필요한 테이블이 준비되도록 합니다.
    설명: attendance (출석 기록), active_sessions (현재 접속 중인 사용자) 두 개의 테이블을 생성합니다.
          테이블이 이미 존재하면 아무 작업도 하지 않습니다.
    """
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
    """
    의도: 특정 날짜가 그 달의 몇 주차에 해당하는지 계산합니다.
    설명: 월요일을 주의 시작으로 간주하여 주차를 계산합니다.
    """
    first_day = dt.replace(day=1)
    dom = dt.day
    adjusted_dom = dom + first_day.weekday()
    return (adjusted_dom - 1) // 7 + 1

def split_session_by_day(check_in: datetime, check_out: datetime):
    """
    의도: 사용자가 자정을 넘어 채널에 머물렀을 경우, 날짜별로 작업 시간을 정확히 나누기 위함입니다.
    설명: 예를 들어 23시에 들어와 01시에 나갔다면, 23:00-23:59 세션과 00:00-01:00 세션 두 개로 분리하여 반환합니다.
    """
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
    """
    의도: 특정 사용자의 특정 날짜 총 작업 시간을 초 단위로 가져옵니다.
    설명: 데이터베이스에서 해당 사용자와 날짜의 모든 'duration' 값을 합산합니다.
    """
    cursor = await db.execute("SELECT SUM(duration) FROM attendance WHERE user_id = ? AND check_in_date = ?", (user_id, date_str))
    row = await cursor.fetchone()
    return row[0] if row and row[0] is not None else 0

async def get_all_users_for_month(db, year: int, month: int):
    """
    의도: 월간 리포트 생성 시, 해당 월에 한 번이라도 참여한 모든 사용자를 조회하기 위함입니다.
    설명: 중복을 제외한 모든 user_id를 반환합니다.
    """
    start_date = f"{year}-{month:02d}-01"
    end_date = f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]}"
    cursor = await db.execute("SELECT DISTINCT user_id FROM attendance WHERE check_in_date BETWEEN ? AND ?", (start_date, end_date))
    rows = await cursor.fetchall()
    return [row[0] for row in rows]

async def get_daily_durations(db, user_id: str, dates: list) -> dict:
    """
    의도: 주간 리포트 생성 시, 특정 기간 동안의 일별 작업 시간을 한번에 효율적으로 조회하기 위함입니다.
    설명: 날짜 목록을 받아, 각 날짜별 총 작업 시간을 담은 딕셔너리를 반환합니다.
    """
    if not dates: return {}
    date_placeholders = ",".join("?" for d in dates)
    query = f"SELECT check_in_date, SUM(duration) FROM attendance WHERE user_id = ? AND check_in_date IN ({date_placeholders}) GROUP BY check_in_date"
    params = [user_id] + [d.isoformat() for d in dates]
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}

# --- Report Generation Logic ---
async def generate_weekly_status_line(db, user_id: str, dates: list):
    """
    의도: 주간 리포트에서 각 요일별 목표 달성 여부를 아이콘으로 표시하기 위함입니다.
    설명: 날짜 목록을 받아, 각 날짜의 작업 시간이 목표를 넘었는지에 따라 '✅', '⚠️', '❌' 아이콘으로 구성된 문자열과
          성공 일수를 반환합니다.
    """
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

# ... (리포트 생성 함수들은 기능이 명확하여 주석을 생략합니다) ...

# --- Bot Events ---
@bot.event
async def on_ready():
    """
    의도: 봇이 성공적으로 디스코드에 로그인하고 준비되었을 때 초기 작업을 수행합니다.
    설명: 데이터베이스를 초기화하고, 5분마다 실행되는 메인 스케줄러를 시작합니다.
    """
    await init_db()
    main_scheduler.start()
    print(f'{bot.user}으로 로그인 성공!')
    print("메인 스케줄러가 시작되었습니다.")

@bot.event
async def on_voice_state_update(member, before, after):
    """
    의도: 사용자가 음성 채널에 들어오거나 나갈 때 출석 체크를 자동으로 시작하고 종료합니다.
    설명: 지정된 음성 채널('고독한작업방')에 대한 입장/퇴장 이벤트를 감지하여,
          active_sessions 테이블에 시작 시간을 기록하거나, attendance 테이블에 최종 작업 시간을 기록합니다.
    """
    if member.bot or before.channel == after.channel:
        return

    text_channel = discord.utils.get(member.guild.text_channels, name=config.TEXT_CHANNEL_NAME)
    if not text_channel:
        return

    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        # 사용자가 목표 채널에 들어온 경우
        if after.channel and after.channel.name == config.VOICE_CHANNEL_NAME:
            cursor = await db.execute("SELECT check_in FROM active_sessions WHERE user_id = ?", (str(member.id),))
            is_already_checked_in = await cursor.fetchone()
            if not is_already_checked_in:
                check_in_time = datetime.now(KST)
                await db.execute("INSERT INTO active_sessions (user_id, check_in) VALUES (?, ?)", (str(member.id), check_in_time.isoformat()))
                await db.commit()
                print(f"{member.display_name}님이 '{config.VOICE_CHANNEL_NAME}' 채널에 입장. DB에 기록.")
                await text_channel.send(f"{member.mention}님, 작업 시작! 🔥")

        # 사용자가 목표 채널에서 나간 경우
        elif before.channel and before.channel.name == config.VOICE_CHANNEL_NAME:
            cursor = await db.execute("SELECT check_in FROM active_sessions WHERE user_id = ?", (str(member.id),))
            row = await cursor.fetchone()
            if row:
                check_in_time = datetime.fromisoformat(row[0])
                check_out_time = datetime.now(KST)
                
                await db.execute("DELETE FROM active_sessions WHERE user_id = ?", (str(member.id),))

                # 자정을 넘겼을 경우를 대비해 세션을 날짜별로 분리
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
async def on_guild_channel_update(before, after):
    """
    의도: 사용자가 음성 채널의 '상태'를 설정하여 공동 작업 세션을 시작하는 것을 알리기 위함입니다.
    설명: 채널 정보 업데이트 이벤트를 감지하여, '상태' 메시지가 새로 설정되었는지 확인합니다.
          '감사 로그'를 조회하여 상태를 변경한 사용자를 찾아내고, 출석체크 채널에 알림 메시지를 보냅니다.
          '감사 로그 보기' 권한이 필수적입니다.
    """
    if not isinstance(after, discord.VoiceChannel) or after.name != config.VOICE_CHANNEL_NAME:
        return

    # 상태가 비어있지 않은 새 값으로 변경되었는지 확인 (같은 내용으로 다시 설정해도 감지)
    if not after.status:
        return

    text_channel = discord.utils.get(after.guild.text_channels, name=config.TEXT_CHANNEL_NAME)
    if not text_channel:
        return

    try:
        # 감사 로그를 조회하여 어떤 사용자가 채널 상태를 변경했는지 찾습니다.
        async for entry in after.guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_update):
            if entry.target.id == after.id and entry.user:
                message = f"{entry.user.mention} 님이 '**{after.status}**' 작업방을 오픈했어요! 🎉"
                await text_channel.send(message)
                return # 작업자를 찾았으면 더 이상 로그를 찾지 않고 종료
    except discord.Forbidden:
        # '감사 로그 보기' 권한이 없을 경우를 대비한 예외 처리
        print("오류: '감사 로그 보기' 권한이 없어 감사 로그에 접근할 수 없습니다.")
        await text_channel.send(f"음성 채널 상태가 '**{after.status}**'(으)로 변경되었어요! (권한 부족으로 누가 바꿨는지는 알 수 없네요 😥)")
    except Exception as e:
        print(f"알 수 없는 오류 발생: {e}")

# --- Bot Commands ---
@bot.command(name="현황")
async def weekly_check_command(ctx):
    """
    의도: 사용자가 원할 때 현재까지의 주간 출석 현황을 바로 확인할 수 있도록 합니다.
    """
    await ctx.send("이번 주 출석 현황을 집계 중입니다... 🗓️")
    report_message = await build_manual_weekly_check_report(ctx.guild, datetime.now(KST).date())
    await ctx.send(report_message)

@bot.command(name="월간결산")
async def monthly_check_command(ctx, month: int = None):
    """
    의도: 사용자가 원할 때 지난달 또는 특정 월의 최종 결산 내역을 확인할 수 있도록 합니다.
    """
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

# --- Scheduled Tasks ---
@tasks.loop(minutes=5)
async def main_scheduler():
    """
    의도: 정해진 시간에 주간/월간 리포트를 자동으로 전송하고 월말에 데이터를 초기화합니다.
    설명: 5분마다 현재 시간을 확인하여, 특정 요일과 시간 조건에 맞는 리포트 전송 작업을 수행합니다.
          매월 1일에는 지난달 데이터를 삭제하여 데이터베이스를 관리합니다.
    """
    await bot.wait_until_ready()
    now = datetime.now(KST)
    today_str = now.date().isoformat()
    guild = bot.guilds[0] if bot.guilds else None
    if not guild: return
    channel = discord.utils.get(guild.text_channels, name=config.TEXT_CHANNEL_NAME)
    if not channel: return

    # 주간 중간 점검 (매주 목요일 18시)
    if now.weekday() == 3 and now.hour == 18 and last_task_run["weekly_mid"] != today_str:
        last_task_run["weekly_mid"] = today_str
        print(f"[{now}] 스케줄러: 주간 중간 점검 실행")
        await channel.send(await build_weekly_mid_report(guild, now.date()))

    # 주간 최종 결산 (매주 월요일 00:05 이후)
    if now.weekday() == 0 and now.hour == 0 and now.minute >= 5 and last_task_run["weekly_final"] != today_str:
        # ... (스케줄러의 세부 로직은 이전과 동일) ...

    # 월간 최종 정산 및 데이터 초기화 (매월 1일 01시)
    if now.day == 1 and now.hour == 1 and last_task_run["monthly_final"] != today_str:
        last_task_run["monthly_final"] = today_str
        print(f"[{now}] 스케줄러: 월간 최종 정산 및 데이터 삭제 실행")
        target_date = now.date() - timedelta(days=1)
        year, month = target_date.year, target_date.month
        report_message = await build_monthly_final_report(guild, year, month)
        await channel.send(report_message)
        
        # 지난달 데이터 삭제
        async with aiosqlite.connect(config.DATABASE_NAME) as db:
            await db.execute("DELETE FROM attendance WHERE check_in_date < ?", (now.date().replace(day=1).isoformat(),))
            await db.commit()
        
        final_message = f"\n---\n*{month}월의 모든 출석 데이터가 초기화됩니다. {now.month}월에도 함께 달려요!*"
        await channel.send(final_message)
        print(f"[{now}] {month}월 데이터 삭제 완료")

# --- Run Bot ---
if __name__ == "__main__":
    # 봇 토큰이 설정되어 있을 때만 봇을 실행합니다.
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("에러: DISCORD_BOT_TOKEN이 설정되지 않았습니다. .env 파일을 확인해주세요.")# main.py

import os
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import aiosqlite
from datetime import datetime, timedelta, time, timezone
from collections import defaultdict
import calendar

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
    print(f"[DEBUG] on_voice_state_update 이벤트 발생: {member.name} 님이 채널을 변경했습니다.") # <-- 음성 채널 디버깅 로그

    if member.bot or before.channel == after.channel:
        return

    text_channel = discord.utils.get(member.guild.text_channels, name=config.TEXT_CHANNEL_NAME)
    if not text_channel:
        return

    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        if after.channel and after.channel.name == config.VOICE_CHANNEL_NAME:
            cursor = await db.execute("SELECT check_in FROM active_sessions WHERE user_id = ?", (str(member.id),))
            is_already_checked_in = await cursor.fetchone()
            if not is_already_checked_in:
                check_in_time = datetime.now(KST)
                await db.execute("INSERT INTO active_sessions (user_id, check_in) VALUES (?, ?)", (str(member.id), check_in_time.isoformat()))
                await db.commit()
                print(f"{member.display_name}님이 '{config.VOICE_CHANNEL_NAME}' 채널에 입장. DB에 기록.")
                await text_channel.send(f"{member.mention}님, 작업 시작! 🔥")

        elif before.channel and before.channel.name == config.VOICE_CHANNEL_NAME:
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

# --- [DEBUG] 음성 채널 상태 메시지 변경 감지 이벤트 (디버깅용) ---
@bot.event
async def on_guild_channel_update(before, after):
    print("1. on_guild_channel_update 이벤트 발생") # <-- 디버깅 메시지 1

    # 음성 채널인지, 그리고 우리가 감시할 채널인지 확인
    if not isinstance(after, discord.VoiceChannel) or after.name != config.VOICE_CHANNEL_NAME:
        return

    print("2. 올바른 음성 채널 업데이트 감지") # <-- 디버깅 메시지 2

    # 채널 '상태'가 비어있지 않은 새 값으로 변경되었는지 확인
    if before.status == after.status or not after.status:
        return

    print(f"3. 상태 메시지 변경 확인: '{before.status}' -> '{after.status}'") # <-- 디버깅 메시지 3

    # 텍스트 채널 찾기
    text_channel = discord.utils.get(after.guild.text_channels, name=config.TEXT_CHANNEL_NAME)
    if not text_channel:
        print("오류: 텍스트 채널을 찾을 수 없습니다.")
        return
    
    print("4. 감사 로그 읽기 시도...") # <-- 디버깅 메시지 4
    try:
        # 감사 로그를 최근 5개까지 확인하여 정확도를 높입니다.
        async for entry in after.guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_update):
            print(f"5. 감사 로그 확인 중: [대상:{entry.target.name}] [유저:{entry.user.name}]") # <-- 디버깅 메시지 5
            if entry.target.id == after.id and entry.user:
                print(f"6. 작업자 찾음: {entry.user.name}") # <-- 디버깅 메시지 6
                message = f"{entry.user.mention} 님이 '**{after.status}**' 작업방을 오픈했어요! 🎉"
                await text_channel.send(message)
                print("7. 메시지 전송 성공!") # <-- 디버깅 메시지 7
                return # 메시지를 보냈으므로 함수 종료
    except discord.Forbidden:
        print("오류: '감사 로그 보기' 권한이 없어 감사 로그에 접근할 수 없습니다.")
        await text_channel.send(f"음성 채널 상태가 '**{after.status}**'(으)로 변경되었어요! (권한 부족으로 누가 바꿨는지는 알 수 없네요 😥)")
    except Exception as e:
        print(f"알 수 없는 오류 발생: {e}")

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

# --- Scheduled Tasks ---
@tasks.loop(minutes=5)
async def main_scheduler():
    await bot.wait_until_ready()
    now = datetime.now(KST)
    today_str = now.date().isoformat()
    guild = bot.guilds[0] if bot.guilds else None
    if not guild: return
    channel = discord.utils.get(guild.text_channels, name=config.TEXT_CHANNEL_NAME)
    if not channel: return

    if now.weekday() == 3 and now.hour == 18 and last_task_run["weekly_mid"] != today_str:
        last_task_run["weekly_mid"] = today_str
        print(f"[{now}] 스케줄러: 주간 중간 점검 실행")
        await channel.send(await build_weekly_mid_report(guild, now.date()))

    if now.weekday() == 0 and now.hour == 0 and now.minute >= 5 and last_task_run["weekly_final"] != today_str:
        last_task_run["weekly_final"] = today_str
        print(f"[{now}] 스케줄러: 주간 최종 결산 실행")
        last_sunday = now.date() - timedelta(days=1)
        week_start = last_sunday - timedelta(days=6)
        dates = [week_start + timedelta(days=i) for i in range(7)]
        header = config.MESSAGE_HEADINGS["weekly_final"].format(month=last_sunday.month, week=get_week_of_month(last_sunday))
        body = ["지난 한 주 모두 고생 많으셨습니다. 최종 출석 결과입니다.", "`월 화 수 목 금 토 일`"]
        async with aiosqlite.connect(config.DATABASE_NAME) as db:
            users = await get_all_users_for_month(db, last_sunday.year, last_sunday.month)
            successful_weeks_by_user = defaultdict(int)
            for user_id in users:
                for week in calendar.monthcalendar(last_sunday.year, last_sunday.month):
                    week_dates = [datetime(last_sunday.year, last_sunday.month, day).date() for day in week if day != 0 and datetime(last_sunday.year, last_sunday.month, day).date() <= last_sunday]
                    if not week_dates: continue
                    _, w_pass_days = await generate_weekly_status_line(db, user_id, week_dates)
                    if w_pass_days >= config.WEEKLY_GOAL_DAYS: successful_weeks_by_user[user_id] += 1
            for user_id in users:
                member = guild.get_member(int(user_id))
                if member:
                    status_line, pass_days = await generate_weekly_status_line(db, user_id, dates)
                    result = "달성! 🎉" if pass_days >= config.WEEKLY_GOAL_DAYS else "미달성 😥"
                    body.append(f"`{status_line}` {member.mention}   **{result}** (월간: {successful_weeks_by_user.get(user_id, 0)}주 성공)")
        body.append("\n새로운 한 주도 함께 파이팅입니다!")
        await channel.send("\n".join([header] + body))
        if get_week_of_month(last_sunday) == 3:
            print(f"[{now}] 스케줄러: 월간 중간 결산 실행")
            header = config.MESSAGE_HEADINGS["monthly_mid_check"].format(month=last_sunday.month)
            mid_body = [f"벌써 마지막 주네요! {last_sunday.month}월 사용료 면제 현황을 알려드립니다."]
            for user_id in users:
                weeks = successful_weeks_by_user.get(user_id, 0)
                member = guild.get_member(int(user_id))
                if member:
                    if weeks >= config.MONTHLY_GOAL_WEEKS: status = "사용료 면제 확정! 🥳"
                    elif weeks == config.MONTHLY_GOAL_WEEKS - 1: status = "마지막 주 목표 달성 시 면제 가능! 🔥"
                    else: status = "면제는 어려워졌지만, 남은 한 주도 파이팅! 💪"
                    mid_body.append(f"{member.mention}: 현재 **{weeks}주** 성공 - **{status}**")
            await channel.send("\n".join([header] + mid_body))

    if now.day == 1 and now.hour == 1 and last_task_run["monthly_final"] != today_str:
        last_task_run["monthly_final"] = today_str
        print(f"[{now}] 스케줄러: 월간 최종 정산 및 데이터 삭제 실행")
        target_date = now.date() - timedelta(days=1)
        year, month = target_date.year, target_date.month
        report_message = await build_monthly_final_report(guild, year, month)
        await channel.send(report_message)
        async with aiosqlite.connect(config.DATABASE_NAME) as db:
            await db.execute("DELETE FROM attendance WHERE check_in_date < ?", (now.date().replace(day=1).isoformat(),))
            await db.commit()
        final_message = f"\n---\n*{month}월의 모든 출석 데이터가 초기화됩니다. {now.month}월에도 함께 달려요!*"
        await channel.send(final_message)
        print(f"[{now}] {month}월 데이터 삭제 완료")

# --- Run Bot ---
if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("에러: DISCORD_BOT_TOKEN이 설정되지 않았습니다. .env 파일을 확인해주세요.")
