# main.py

"""
디스코드 음성 채널 출석 체크 봇 (Discord Voice Channel Attendance Bot)
"""

import os
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import aiosqlite
from datetime import datetime, timedelta, time, timezone
from collections import defaultdict
import calendar
import sys # 버전 확인용

print("★★★★★ 최종 버전 봇 코드 실행 시작! (오류 수정 완료) ★★★★★★")

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
intents.dm_messages = True

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

# --- Scheduled Tasks (누락되었던 부분 복구) ---
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

# --- Bot Events ---
@bot.event
async def on_ready():
    await init_db()
    main_scheduler.start() # 이제 main_scheduler가 정의되어 있으므로 에러가 나지 않습니다.
    print(f'{bot.user}으로 로그인 성공!')
    print("메인 스케줄러가 시작되었습니다.")

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return
    text_channel = discord.utils.get(member.guild.text_channels, name=config.TEXT_CHANNEL_NAME)
    if not text_channel: return
    
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
                print(f"{member.display_name}님이 '{config.VOICE_CHANNEL_NAME}' 채널에 입장.")
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
                print(f"{member.display_name}님이 '{config.VOICE_CHANNEL_NAME}' 채널에서 퇴장.")
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
    if message.author.bot or not isinstance(message.channel, discord.DMChannel):
        if not isinstance(message.channel, discord.DMChannel) and not message.author.bot:
            await bot.process_commands(message)
        return

    if message.content.startswith('!집중'):
        command_content = message.content.strip()
        
        guild = bot.guilds[0] if bot.guilds else None
        if not guild:
            await message.channel.send("오류: 봇이 속한 서버를 찾을 수 없습니다.")
            return
        
        member = guild.get_member(message.author.id)
        if not member:
            await message.channel.send("오류: 서버에서 사용자님을 찾을 수 없습니다.")
            return

        text_channel = discord.utils.get(guild.text_channels, name=config.TEXT_CHANNEL_NAME)
        if not text_channel:
            await message.channel.send(f"오류: 서버에서 '{config.TEXT_CHANNEL_NAME}' 채널을 찾을 수 없습니다.")
            return

        # Case 1: !집중 (자동 불러오기)
        if command_content == '!집중':
            if not member.voice or not member.voice.channel or member.voice.channel.name != config.VOICE_CHANNEL_NAME:
                await message.channel.send(f"앗! '{config.VOICE_CHANNEL_NAME}' 음성 채널에 먼저 입장하셔야 `!집중` 명령어를 사용할 수 있어요. 😮")
                return
            
            try:
                task_description = member.voice.channel.status
            except AttributeError:
                await message.channel.send(f"⚠️ 환경 오류: discord.py 버전({discord.__version__})이 낮아 상태를 가져올 수 없습니다.")
                return
            except Exception as e:
                await message.channel.send(f"오류 발생: {e}")
                return

            if not task_description:
                await message.channel.send("음... 😅 음성 채널의 상태 메시지가 비어있어요.")
                return

            announcement = f"{member.mention} 님이 '**{task_description}**' 집중 타임을 오픈했습니다! 함께 달려보세요!"
            await text_channel.send(announcement)
            await message.channel.send(f"🔥 좋아요! '**{task_description}**' 집중 타임 시작을 모두에게 알렸어요. 파이팅! 💪")

        # Case 2: !집중 [내용] (수동 입력)
        elif command_content.startswith('!집중 '):
            task_description = command_content.replace('!집중', '', 1).strip()
            if not task_description:
                await message.channel.send("앗, 어떤 일에 집중할지 알려주세요!")
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
    version_info = f"🐍 Python: {sys.version.split()[0]}\n🤖 discord.py: {discord.__version__}"
    status_check = ""
    if ctx.author.voice and ctx.author.voice.channel:
        channel = ctx.author.voice.channel
        if 'status' in dir(channel):
            status_check = f"\n✅ '{channel.name}' 채널에 'status' 속성 존재함 (값: {getattr(channel, 'status', 'None')})"
        else:
            status_check = f"\n❌ '{channel.name}' 채널에 'status' 속성 없음"
    else:
        status_check = "\n⚠️ 음성 채널에 입장 후 다시 시도해주세요."
    await ctx.send(f"```{version_info}{status_check}```")

# --- Run Bot ---
if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("에러: DISCORD_BOT_TOKEN이 설정되지 않았습니다. .env 파일을 확인해주세요.")
