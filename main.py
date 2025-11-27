# main.py

import os
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import aiosqlite
from datetime import datetime, timedelta, time, timezone
from collections import defaultdict
import calendar
import sys 

print("★★★★★ [자동 감지 올인 버전] 봇 실행! 상태 변경을 기다립니다. ★★★★★★")

import config

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
KST = timezone(timedelta(hours=9))

# 인텐트 설정 (매우 중요: 하나라도 꺼지면 감지를 못합니다)
intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
intents.message_content = True
intents.guilds = True 

bot = commands.Bot(command_prefix=config.BOT_PREFIX, intents=intents)
last_task_run = defaultdict(lambda: None)

# --- (DB 및 헬퍼 함수 생략 없이 포함) ---
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
    res = await cursor.fetchone()
    return res[0] if res and res[0] else 0

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
    print(f'✅ {bot.user} 로그인 성공! (ID: {config.VOICE_CHANNEL_ID} 감시 중)')
    
    # [진단] 봇이 시작하자마자 채널을 잘 보고 있는지 체크
    vc = bot.get_channel(config.VOICE_CHANNEL_ID)
    if vc:
        print(f"👀 감시 대상 채널 확인됨: {vc.name}")
    else:
        print(f"❌ [치명적 오류] 감시 대상 채널(ID: {config.VOICE_CHANNEL_ID})을 찾을 수 없습니다! 권한을 확인하세요.")

# ★★★ [핵심] 여기가 작동해야 합니다 ★★★
# 음성 채널 상태 변경 감지
@bot.event
async def on_voice_channel_status_update(channel, before, after):
    # 1. 로깅: 이벤트가 발생하는지 확인
    print(f"🔔 [이벤트 발생] {channel.name} 상태 변경: '{before}' -> '{after}'")

    # 2. ID 확인
    if channel.id != config.VOICE_CHANNEL_ID:
        print(f"   -> 무시함 (타겟 채널 아님)")
        return

    # 3. 텍스트 채널 확인
    text_channel = channel.guild.get_channel(config.TEXT_CHANNEL_ID)
    if not text_channel:
        print(f"❌ 오류: 텍스트 채널(ID: {config.TEXT_CHANNEL_ID})을 찾을 수 없음")
        return

    # 4. 공지 전송
    if after:
        # 누가 바꿨는지 찾기
        editor_mention = "누군가"
        try:
            async for entry in channel.guild.audit_logs(limit=1, action=discord.AuditLogAction.voice_channel_status_update):
                if entry.target.id == channel.id:
                    editor_mention = entry.user.mention
                    break
        except:
            print("⚠️ 감사 로그 조회 실패 (권한 부족 가능성)")

        await text_channel.send(f"📢 {editor_mention} 님이 '**{after}**' 집중 타임을 오픈했습니다! 함께 달려보세요! 🔥")
        print(f"✅ 공지 전송 완료: {after}")
    else:
        print("ℹ️ 상태가 지워짐 (공지 안 함)")

# --- 출석 체크 (유지) ---
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return
    text_channel = member.guild.get_channel(config.TEXT_CHANNEL_ID)
    if not text_channel: return

    target_id = config.VOICE_CHANNEL_ID
    is_join = (not before.channel or before.channel.id != target_id) and (after.channel and after.channel.id == target_id)
    is_leave = (before.channel and before.channel.id == target_id) and (not after.channel or after.channel.id != target_id)

    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        if is_join:
            cursor = await db.execute("SELECT check_in FROM active_sessions WHERE user_id = ?", (str(member.id),))
            if await cursor.fetchone() is None:
                now = datetime.now(KST)
                await db.execute("INSERT INTO active_sessions (user_id, check_in) VALUES (?, ?)", (str(member.id), now.isoformat()))
                await db.commit()
                print(f"➡ 입장: {member.display_name}")
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
                total = await get_today_total_duration(db, str(member.id), check_out.date().isoformat())
                h, r = divmod(total, 3600)
                m, _ = divmod(r, 60)
                print(f"⬅ 퇴장: {member.display_name}")
                await text_channel.send(f"{member.mention}님 수고하셨습니다! (오늘: {int(h)}시간 {int(m)}분)")

# --- 수동 명령어 (!집중 [내용] - 비상용) ---
@bot.event
async def on_message(message):
    if message.author.bot: return
    
    if isinstance(message.channel, discord.DMChannel):
        if message.content.startswith('!집중 '):
            content = message.content.replace('!집중', '').strip()
            if not bot.guilds: return
            guild = bot.guilds[0]
            text_channel = guild.get_channel(config.TEXT_CHANNEL_ID)
            
            if text_channel:
                member = guild.get_member(message.author.id)
                await text_channel.send(f"📢 {member.mention} 님이 '**{content}**' 집중 타임을 오픈했습니다! 함께 달려보세요! 🔥")
                await message.channel.send(f"✅ 수동 공지 완료: {content}")
            else:
                await message.channel.send("채널을 찾을 수 없습니다.")
        elif message.content.strip() == '!집중':
            await message.channel.send("💡 자동 감지가 안 되나요? `!집중 [할일]` 처럼 내용을 직접 적어주세요!")
    else:
        await bot.process_commands(message)

# --- Bot Commands & Scheduler (기존 동일) ---
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
    # 아주 간단한 생존 신고
    await ctx.send(f"✅ 봇 정상 작동 중! (v2.6.4)")

@tasks.loop(minutes=5)
async def main_scheduler():
    await bot.wait_until_ready()
    now = datetime.now(KST)
    today_str = now.date().isoformat()
    guild = bot.guilds[0] if bot.guilds else None
    if not guild: return
    channel = guild.get_channel(config.TEXT_CHANNEL_ID)
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
