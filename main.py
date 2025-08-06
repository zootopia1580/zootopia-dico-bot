# main.py

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
active_checkins = {}
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
        end_of_day = datetime.combine(current_time.date(), time(23, 59, 59))
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
    dates = [week_start + timedelta(days=i) for i in range(4)] # 자동화 리포트는 월-목 고정
    header = config.MESSAGE_HEADINGS["weekly_mid_check"].format(month=report_date.month, week=get_week_of_month(report_date))
    body = ["주말까지 이틀 남았어요! 현재까지의 출석 현황입니다.", "`          월 화 수 목`"]
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        users = await get_all_users_for_month(db, report_date.year, report_date.month)
        for user_id in users:
            member = guild.get_member(int(user_id))
            if member:
                status_line, _ = await generate_weekly_status_line(db, user_id, dates)
                body.append(f"`{member.display_name:<8}: {status_line}`")
    body.append(f"\n> (✅: 달성, ⚠️: 모자람, ❌: 안 들어옴)\n\n아직 시간이 충분해요. 모두 목표를 향해 달려봐요! 🚀")
    return "\n".join([header] + body)
    
async def build_manual_weekly_check_report(guild: discord.Guild, report_date: datetime.date):
    """!현황 명령어를 위한 동적 리포트 생성"""
    week_start = report_date - timedelta(days=report_date.weekday())
    num_days_to_show = report_date.weekday() + 1
    dates = [week_start + timedelta(days=i) for i in range(num_days_to_show)]
    weekday_labels = ["월", "화", "수", "목", "금", "토", "일"]
    
    header = f"[📢 현재 주간 현황] {report_date.month}월 {get_week_of_month(report_date)}주차"
    labels_line = " ".join(weekday_labels[:num_days_to_show])
    body = [f"오늘까지의 출석 현황입니다.", f"`          {labels_line}`"]
    
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        users = await get_all_users_for_month(db, report_date.year, report_date.month)
        if not users:
             return "이번 달 출석 기록이 아직 없습니다."
        for user_id in users:
            member = guild.get_member(int(user_id))
            if member:
                status_line, _ = await generate_weekly_status_line(db, user_id, dates)
                body.append(f"`{member.display_name:<8}: {status_line}`")
    
    body.append(f"\n> (✅: 달성, ⚠️: 모자람, ❌: 안 들어옴)")
    return "\n".join([header] + body)

async def build_monthly_final_report(guild: discord.Guild, year: int, month: int):
    header = config.MESSAGE_HEADINGS["monthly_final"].format(month=month)
    exempt_users, charge_users = [], []
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        users = await get_all_users_for_month(db, year, month)
        if not users:
            return f"{year}년 {month}월의 출석 기록이 없습니다."
            
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
    for guild in bot.guilds:
        voice_channel = discord.utils.get(guild.voice_channels, name=config.VOICE_CHANNEL_NAME)
        if voice_channel:
            for member in voice_channel.members:
                if not member.bot and member.id not in active_checkins:
                    active_checkins[member.id] = datetime.now(KST)
                    print(f"[상태 복구] {member.display_name}님이 이미 채널에 있어 출석을 시작합니다.")

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot or before.channel == after.channel: return
    text_channel = discord.utils.get(member.guild.text_channels, name=config.TEXT_CHANNEL_NAME)
    if not text_channel: return
    if after.channel and after.channel.name == config.VOICE_CHANNEL_NAME:
        if member.id not in active_checkins:
            active_checkins[member.id] = datetime.now(KST)
            print(f"{member.display_name}님이 '{config.VOICE_CHANNEL_NAME}' 채널에 입장.")
            await text_channel.send(f"{member.mention}님, 작업 시작! 🔥")
    elif before.channel and before.channel.name == config.VOICE_CHANNEL_NAME:
        check_in_time = active_checkins.pop(member.id, None)
        if not check_in_time: return
        check_out_time = datetime.now(KST)
        print(f"{member.display_name}님이 '{config.VOICE_CHANNEL_NAME}' 채널에서 퇴장.")
        async with aiosqlite.connect(config.DATABASE_NAME) as db:
            sessions_to_insert = split_session_by_day(check_in_time, check_out_time)
            for session in sessions_to_insert:
                await db.execute("INSERT INTO attendance (user_id, check_in, check_out, duration, check_in_date) VALUES (?, ?, ?, ?, ?)",
                                 (str(member.id), session["check_in"], session["check_out"], session["duration"], datetime.fromisoformat(session["check_in"]).date().isoformat()))
            await db.commit()
            total_seconds_today = await get_today_total_duration(db, str(member.id), check_out_time.date().isoformat())
            hours, remainder = divmod(total_seconds_today, 3600)
            minutes, _ = divmod(remainder, 60)
            await text_channel.send(f"{member.mention}님, 오늘 누적 작업시간은 {int(hours):02d}시간 {int(minutes):02d}분입니다. 👏")

# --- Bot Commands ---
@bot.command(name="현황")
async def weekly_check_command(ctx):
    """현재 요일까지의 주간 출석 현황을 즉시 확인합니다."""
    await ctx.send("이번 주 현황을 조회합니다. 잠시만 기다려주세요...")
    report_message = await build_manual_weekly_check_report(ctx.guild, datetime.now(KST).date())
    await ctx.send(report_message)

@bot.command(name="월간결산")
async def monthly_check_command(ctx, month: int = None):
    """특정 월의 최종 결산 내역을 확인합니다. (데이터 삭제 없음)"""
    now = datetime.now(KST)
    year = now.year
    if month is None:
        target_date = now.date() - timedelta(days=now.day)
        month = target_date.month
    
    if not (1 <= month <= 12):
        await ctx.send("올바른 월(1-12)을 입력해주세요.")
        return

    await ctx.send(f"**{year}년 {month}월** 최종 결산 내역을 조회합니다. 잠시만 기다려주세요...")
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

    # 주간 중간 점검 (목 18:00)
    if now.weekday() == 3 and now.hour == 18 and last_task_run["weekly_mid"] != today_str:
        last_task_run["weekly_mid"] = today_str
        print(f"[{now}] 스케줄러: 주간 중간 점검 실행")
        await channel.send(await build_weekly_mid_report(guild, now.date()))

    # 주간 최종/월간 중간 결산 (월 00:05)
    if now.weekday() == 0 and now.hour == 0 and now.minute >= 5 and last_task_run["weekly_final"] != today_str:
        last_task_run["weekly_final"] = today_str
        print(f"[{now}] 스케줄러: 주간 최종 결산 실행")
        last_sunday = now.date() - timedelta(days=1)
        week_start = last_sunday - timedelta(days=6)
        dates = [week_start + timedelta(days=i) for i in range(7)]
        header = config.MESSAGE_HEADINGS["weekly_final"].format(month=last_sunday.month, week=get_week_of_month(last_sunday))
        body = ["지난주 출석 결과가 최종 확정되었습니다.", "`          월 화 수 목 금 토 일`"]
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
                    body.append(f"`{member.display_name:<8}: {status_line}`  **{result}** (월간: {successful_weeks_by_user.get(user_id, 0)}주 성공)")
        body.append("\n새로운 한 주도 함께 파이팅입니다!")
        await channel.send("\n".join([header] + body))
        if get_week_of_month(last_sunday) == 3:
            print(f"[{now}] 스케줄러: 월간 중간 결산 실행")
            header = config.MESSAGE_HEADINGS["monthly_mid_check"].format(month=last_sunday.month)
            mid_body = [f"이제 마지막 한 주만 남았어요!", f"(면제 조건: 총 {config.MONTHLY_GOAL_WEEKS}주 이상 성공)"]
            for user_id in users:
                weeks = successful_weeks_by_user.get(user_id, 0)
                member = guild.get_member(int(user_id))
                if member:
                    if weeks >= config.MONTHLY_GOAL_WEEKS: status = "사용료 면제 확정! 🥳"
                    elif weeks == config.MONTHLY_GOAL_WEEKS - 1: status = "마지막 주 목표 달성 시 면제 가능! 🔥"
                    else: status = "이번 달 면제는 어렵게 되었어요. 😥"
                    mid_body.append(f"{member.mention}: 현재 **{weeks}주** 성공 - **{status}**")
            await channel.send("\n".join([header] + mid_body))

    # 월간 최종 정산 및 데이터 삭제 (매월 1일 01:00)
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
