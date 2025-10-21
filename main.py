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
    """
    의도: 봇 실행 시 데이터베이스와 필요한 테이블이 준비되도록 합니다.
    설명: attendance (출석 기록), active_sessions (현재 접속 중인 사용자) 두 개의 테이블을 생성합니다.
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
    """
    first_day = dt.replace(day=1)
    dom = dt.day
    adjusted_dom = dom + first_day.weekday()
    return (adjusted_dom - 1) // 7 + 1

def split_session_by_day(check_in: datetime, check_out: datetime):
    """
    의도: 사용자가 자정을 넘어 채널에 머물렀을 경우, 날짜별로 작업 시간을 정확히 나누기 위함입니다.
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
    """
    cursor = await db.execute("SELECT SUM(duration) FROM attendance WHERE user_id = ? AND check_in_date = ?", (user_id, date_str))
    row = await cursor.fetchone()
    return row[0] if row and row[0] is not None else 0

async def get_all_users_for_month(db, year: int, month: int):
    """
    의도: 월간 리포트 생성 시, 해당 월에 한 번이라도 참여한 모든 사용자를 조회하기 위함입니다.
    """
    start_date = f"{year}-{month:02d}-01"
    end_date = f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]}"
    cursor = await db.execute("SELECT DISTINCT user_id FROM attendance WHERE check_in_date BETWEEN ? AND ?", (start_date, end_date))
    rows = await cursor.fetchall()
    return [row[0] for row in rows]

async def get_daily_durations(db, user_id: str, dates: list) -> dict:
    """
    의도: 주간 리포트 생성 시, 특정 기간 동안의 일별 작업 시간을 한번에 효율적으로 조회하기 위함입니다.
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
    """
    의도: 봇이 성공적으로 디스코드에 로그인하고 준비되었을 때 초기 작업을 수행합니다.
    """
    await init_db()
    main_scheduler.start()
    print(f'{bot.user}으로 로그인 성공!')
    print("메인 스케줄러가 시작되었습니다.")

@bot.event
async def on_voice_state_update(member, before, after):
    """
    의도: 사용자가 음성 채널에 들어오거나 나갈 때 출석 체크를 자동으로 시작하고 종료합니다.
    """
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

@bot.event
async def on_guild_channel_update(before, after):
    """
    의도: 사용자가 음성 채널의 '상태'를 설정하여 공동 작업 세션을 시작하는 것을 알리기 위함입니다.
    """
    if not isinstance(after, discord.VoiceChannel) or after.name != config.VOICE_CHANNEL_NAME:
        return

    if not after.status:
        return

    text_channel = discord.utils.get(after.guild.text_channels, name=config.TEXT_CHANNEL_NAME)
    if not text_channel:
        return

    try:
        async for entry in after.guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_update):
            if entry.target.id == after.id and entry.user:
                message = f"{entry.user.mention} 님이 '**{after.status}**' 작업방을 오픈했어요! 🎉"
                await text_channel.send(message)
                return
    except discord.Forbidden:
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
    """
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
        # (이하 스케줄러 로직은 생략)

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
