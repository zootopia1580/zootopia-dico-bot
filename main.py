# main.py

"""
디스코드 음성 채널 출석 체크 봇 (ID 기반 절대 좌표 버전)
"""

import os
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import aiosqlite
from datetime import datetime, timedelta, time, timezone
from collections import defaultdict
import calendar
import sys 

print("★★★★★ ID 기반 봇 가동 시작! (절대 좌표 모드) ★★★★★★")

import config

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
KST = timezone(timedelta(hours=9))

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
intents.message_content = True
intents.dm_messages = True
intents.guilds = True 

bot = commands.Bot(command_prefix=config.BOT_PREFIX, intents=intents)
last_task_run = defaultdict(lambda: None)

# --- Database Functions ---
async def init_db():
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS attendance (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, check_in TEXT, check_out TEXT, duration INTEGER, check_in_date TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS active_sessions (user_id TEXT PRIMARY KEY, check_in TEXT)")
        await db.commit()

def split_session_by_day(check_in, check_out):
    sessions = []
    current = check_in
    while current.date() < check_out.date():
        end = datetime.combine(current.date(), time(23, 59, 59), tzinfo=current.tzinfo)
        sessions.append({"check_in": current.isoformat(), "check_out": end.isoformat(), "duration": (end - current).total_seconds()})
        current = end + timedelta(seconds=1)
    sessions.append({"check_in": current.isoformat(), "check_out": check_out.isoformat(), "duration": (check_out - current).total_seconds()})
    return sessions

async def get_today_total_duration(db, user_id, date_str):
    cursor = await db.execute("SELECT SUM(duration) FROM attendance WHERE user_id = ? AND check_in_date = ?", (user_id, date_str))
    row = await cursor.fetchone()
    return row[0] if row and row[0] is not None else 0

async def get_all_users_for_month(db, year, month):
    start = f"{year}-{month:02d}-01"
    end = f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]}"
    cursor = await db.execute("SELECT DISTINCT user_id FROM attendance WHERE check_in_date BETWEEN ? AND ?", (start, end))
    return [row[0] for row in await cursor.fetchall()]

async def get_daily_durations(db, user_id, dates):
    if not dates: return {}
    placeholders = ",".join("?" for d in dates)
    query = f"SELECT check_in_date, SUM(duration) FROM attendance WHERE user_id = ? AND check_in_date IN ({placeholders}) GROUP BY check_in_date"
    cursor = await db.execute(query, [user_id] + [d.isoformat() for d in dates])
    return {row[0]: row[1] for row in await cursor.fetchall()}

async def generate_weekly_status_line(db, user_id, dates):
    daily_durations = await get_daily_durations(db, user_id, dates)
    daily_goal = config.SPECIAL_USER_GOALS.get(user_id, config.DAILY_GOAL_SECONDS)
    line, pass_days = [], 0
    for d in dates:
        dur = daily_durations.get(d.isoformat(), 0)
        if dur >= daily_goal:
            line.append(config.STATUS_ICONS["pass"])
            pass_days += 1
        elif dur > 0: line.append(config.STATUS_ICONS["insufficient"])
        else: line.append(config.STATUS_ICONS["absent"])
    return " ".join(line), pass_days

async def build_weekly_mid_report(guild, date):
    week_start = date - timedelta(days=date.weekday())
    dates = [week_start + timedelta(days=i) for i in range(4)]
    header = config.MESSAGE_HEADINGS["weekly_mid_check"].format(month=date.month, week=get_week_of_month(date))
    body = ["주말까지 이틀 남았어요! 현재까지의 출석 현황입니다.", "`월 화 수 목`"]
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        users = await get_all_users_for_month(db, date.year, date.month)
        for user_id in users:
            member = guild.get_member(int(user_id))
            if member:
                status_line, _ = await generate_weekly_status_line(db, user_id, dates)
                body.append(f"`{status_line}` {member.mention}")
    body.append(f"\n> (✅: 달성, ⚠️: 모자람, ❌: 안 들어옴)\n\n아직 시간이 충분해요. 모두 목표를 향해 달려봐요! 🚀")
    return "\n".join([header] + body)

async def build_manual_weekly_check_report(guild, date):
    week_start = date - timedelta(days=date.weekday())
    num_days = date.weekday() + 1
    dates = [week_start + timedelta(days=i) for i in range(num_days)]
    weekday_labels = ["월", "화", "수", "목", "금", "토", "일"]
    header = f"[📢 현재 주간 현황] {date.month}월 {get_week_of_month(date)}주차"
    labels_line = " ".join(weekday_labels[:num_days])
    body = [f"오늘까지의 출석 현황입니다.", f"`{labels_line}`"]
    
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        users = await get_all_users_for_month(db, date.year, date.month)
        if not users: return "아직 이번 달 활동 기록이 없네요."
        for user_id in users:
            member = guild.get_member(int(user_id))
            if member:
                status_line, _ = await generate_weekly_status_line(db, user_id, dates)
                body.append(f"`{status_line}` {member.mention}")
    body.append(f"\n> (✅: 달성, ⚠️: 모자람, ❌: 안 들어옴)")
    return "\n".join([header] + body)

async def build_monthly_final_report(guild, year, month):
    header = config.MESSAGE_HEADINGS["monthly_final"].format(month=month)
    exempt, charge = [], []
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        users = await get_all_users_for_month(db, year, month)
        if not users: return "기록이 없습니다."
        for user_id in users:
            success_weeks = 0
            for week in calendar.monthcalendar(year, month):
                dates = [datetime(year, month, d).date() for d in week if d != 0]
                if not dates: continue
                _, pass_days = await generate_weekly_status_line(db, user_id, dates)
                if pass_days >= config.WEEKLY_GOAL_DAYS: success_weeks += 1
            member = guild.get_member(int(user_id))
            if member:
                line = f"• {member.mention} ({success_weeks}주 성공)"
                if success_weeks >= config.MONTHLY_GOAL_WEEKS: exempt.append(line)
                else: charge.append(line)
    body = [f"{year}년 {month}월 최종 정산 결과입니다.", "\n**🎉 면제 대상**"] + (exempt if exempt else ["- 없음"]) + ["\n**😥 부과 대상**"] + (charge if charge else ["- 없음"])
    return "\n".join([header] + body)

# --- Bot Events ---

@bot.event
async def on_ready():
    await init_db()
    main_scheduler.start()
    print(f'✅ {bot.user} 로그인 성공! (ID 기반 감시 중)')
    # 봇이 제대로 채널을 보고 있는지 확인용 로그
    vc = bot.get_channel(config.VOICE_CHANNEL_ID)
    tc = bot.get_channel(config.TEXT_CHANNEL_ID)
    print(f"🎯 음성 채널 확인: {vc.name if vc else '못 찾음'} (ID: {config.VOICE_CHANNEL_ID})")
    print(f"🎯 텍스트 채널 확인: {tc.name if tc else '못 찾음'} (ID: {config.TEXT_CHANNEL_ID})")

# ★★★ [핵심 1] 음성 채널 상태 변경 감지 (ID 사용) ★★★
@bot.event
async def on_voice_channel_status_update(channel, before, after):
    # ID로 비교하므로 이름이 달라도 상관없습니다.
    if channel.id != config.VOICE_CHANNEL_ID:
        return

    # 텍스트 채널도 ID로 찾습니다.
    text_channel = channel.guild.get_channel(config.TEXT_CHANNEL_ID)
    if not text_channel:
        print("❌ 텍스트 채널을 찾을 수 없습니다.")
        return

    # 상태가 비어있지 않을 때만 공지
    if after:
        editor = None
        try:
            async for entry in channel.guild.audit_logs(limit=1, action=discord.AuditLogAction.voice_channel_status_update):
                if entry.target.id == channel.id:
                    editor = entry.user
                    break
        except:
            pass

        if editor:
            await text_channel.send(f"📢 {editor.mention}님이 '**{after}**' 집중 타임을 오픈했습니다! 함께 달려보세요! 🔥")
        else:
            await text_channel.send(f"📢 누군가 '**{after}**' 집중 타임을 오픈했습니다! 함께 달려보세요! 🔥")

# ★★★ [핵심 2] 출석 체크 (ID 사용) ★★★
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return
    
    # 채널 ID로 입장/퇴장 판단
    target_id = config.VOICE_CHANNEL_ID
    
    is_join = (not before.channel or before.channel.id != target_id) and \
              (after.channel and after.channel.id == target_id)
    is_leave = (before.channel and before.channel.id == target_id) and \
               (not after.channel or after.channel.id != target_id)

    text_channel = member.guild.get_channel(config.TEXT_CHANNEL_ID)
    if not text_channel: return

    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        if is_join:
            cursor = await db.execute("SELECT check_in FROM active_sessions WHERE user_id = ?", (str(member.id),))
            if await cursor.fetchone() is None:
                now = datetime.now(KST)
                await db.execute("INSERT INTO active_sessions (user_id, check_in) VALUES (?, ?)", (str(member.id), now.isoformat()))
                await db.commit()
                print(f"{member.display_name} 입장 (ID 일치)")
                await text_channel.send(f"{member.mention}님, 작업 시작! 🔥")
        
        elif is_leave:
            cursor = await db.execute("SELECT check_in FROM active_sessions WHERE user_id = ?", (str(member.id),))
            row = await cursor.fetchone()
            if row:
                check_in = datetime.fromisoformat(row[0])
                check_out = datetime.now(KST)
                await db.execute("DELETE FROM active_sessions WHERE user_id = ?", (str(member.id),))
                
                for s in split_session_by_day(check_in, check_out):
                    await db.execute("INSERT INTO attendance (user_id, check_in, check_out, duration, check_in_date) VALUES (?, ?, ?, ?, ?)", 
                                     (str(member.id), s["check_in"], s["check_out"], s["duration"], datetime.fromisoformat(s["check_in"]).date().isoformat()))
                await db.commit()
                
                print(f"{member.display_name} 퇴장 (ID 일치)")
                total = await get_today_total_duration(db, str(member.id), check_out.date().isoformat())
                h, m = divmod(total // 60, 60) # total is seconds? No, wait logic uses divmod on remainder.
                # Logic Check: split_session returns seconds. get_today returns seconds.
                h, r = divmod(total, 3600)
                m, _ = divmod(r, 60)
                await text_channel.send(f"{member.mention}님 수고하셨습니다! (오늘: {int(h)}시간 {int(m)}분)")

# ★★★ [핵심 3] 수동 명령 (ID 사용) ★★★
@bot.event
async def on_message(message):
    if message.author.bot: return
    
    # DM 처리
    if isinstance(message.channel, discord.DMChannel):
        if message.content.startswith('!집중'):
            content = message.content.replace('!집중', '').strip()
            
            # 봇이 있는 첫 번째 서버를 찾음 (단일 서버 가정)
            if not bot.guilds: return
            guild = bot.guilds[0]
            member = guild.get_member(message.author.id)
            
            if not member: return

            # 텍스트 채널 가져오기 (ID 기반)
            text_channel = guild.get_channel(config.TEXT_CHANNEL_ID)
            if not text_channel:
                await message.channel.send("오류: 공지할 채널을 찾을 수 없습니다.")
                return

            # 1. 내용 없이 '!집중'만 친 경우 -> 자동 불러오기
            if not content:
                # 유저가 목표 채널에 있는지 ID로 확인
                if not member.voice or not member.voice.channel or member.voice.channel.id != config.VOICE_CHANNEL_ID:
                    await message.channel.send(f"앗! 먼저 목표 음성 채널에 입장해주세요.")
                    return
                
                # ID로 채널 객체를 확실하게 가져옴 (Fetch)
                target_channel = await bot.fetch_channel(config.VOICE_CHANNEL_ID)
                status = getattr(target_channel, 'status', None)
                
                if status:
                    await text_channel.send(f"{member.mention} 님이 '**{status}**' 집중 타임을 오픈했습니다! 함께 달려보세요!")
                    await message.channel.send(f"🔥 알림 전송 완료!")
                else:
                    await message.channel.send("채널 상태가 비어있습니다. 상태를 먼저 설정하거나 `!집중 [내용]`으로 입력해주세요.")
            
            # 2. '!집중 [내용]'으로 입력한 경우 -> 수동 알림
            else:
                await text_channel.send(f"📢 {member.mention}님이 '**{content}**' 집중 타임을 오픈했습니다! 함께 달려보세요! 🔥")
                await message.channel.send(f"✅ 수동 알림 전송 완료: {content}")
    
    else:
        await bot.process_commands(message)

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

@bot.command(name="진단")
async def diagnose(ctx):
    import discord
    import sys
    version_info = f"🐍 Python: {sys.version.split()[0]}\n🤖 discord.py: {discord.__version__}"
    
    # ID로 채널 확인
    try:
        target_vc = ctx.guild.get_channel(config.VOICE_CHANNEL_ID)
        vc_status = f"✅ 확인됨: {target_vc.name}"
    except:
        vc_status = "❌ ID로 음성 채널을 찾을 수 없음"

    try:
        target_tc = ctx.guild.get_channel(config.TEXT_CHANNEL_ID)
        tc_status = f"✅ 확인됨: {target_tc.name}"
    except:
        tc_status = "❌ ID로 텍스트 채널을 찾을 수 없음"

    await ctx.send(f"```{version_info}\n\n[채널 연결 상태]\n음성방: {vc_status}\n채팅방: {tc_status}```")

# --- Scheduled Tasks ---
@tasks.loop(minutes=5)
async def main_scheduler():
    await bot.wait_until_ready()
    now = datetime.now(KST)
    today_str = now.date().isoformat()
    guild = bot.guilds[0] if bot.guilds else None
    if not guild: return
    channel = guild.get_channel(config.TEXT_CHANNEL_ID) # ID로 변경
    if not channel: return

    if now.weekday() == 3 and now.hour == 18 and last_task_run["weekly_mid"] != today_str:
        last_task_run["weekly_mid"] = today_str
        await channel.send(await build_weekly_mid_report(guild, now.date()))

    if now.weekday() == 0 and now.hour == 0 and now.minute >= 5 and last_task_run["weekly_final"] != today_str:
        last_task_run["weekly_final"] = today_str
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
