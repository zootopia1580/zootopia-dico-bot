# main.py

"""
디스코드 음성 채널 출석 체크 봇 (최적화 버전)
- 불필요한 연산 및 로그 제거
- 메시지 디테일 유지
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
import config

# --- Bot Setup ---
print("★★★★★ 봇 코드 실행 (최적화 버전) ★★★★★★")

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
        await db.execute("CREATE TABLE IF NOT EXISTS weekly_goals (user_id TEXT, goal_text TEXT, week_start_date TEXT, PRIMARY KEY (user_id, week_start_date))")
        await db.commit()

# --- Helper Functions ---
def get_this_monday_str():
    now = datetime.now(KST)
    return (now - timedelta(days=now.weekday())).date().isoformat()

def get_week_of_month(dt: datetime.date) -> int:
    first_day = dt.replace(day=1)
    adjusted_dom = dt.day + first_day.weekday()
    return (adjusted_dom - 1) // 7 + 1

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

async def get_weekly_goal_text(db, user_id, week_start_date):
    cursor = await db.execute("SELECT goal_text FROM weekly_goals WHERE user_id = ? AND week_start_date = ?", (user_id, week_start_date))
    res = await cursor.fetchone()
    return res[0] if res else None

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

# --- Report Generation Logic ---
async def generate_weekly_status_line(db, user_id, dates):
    daily_durations = await get_daily_durations(db, user_id, dates)
    daily_goal = config.get_user_goal(user_id) 
    line, pass_days = [], 0
    for d in dates:
        dur = daily_durations.get(d.isoformat(), 0)
        if dur >= daily_goal:
            line.append(config.STATUS_ICONS["pass"])
            pass_days += 1
        elif dur > 0: line.append(config.STATUS_ICONS["insufficient"])
        else: line.append(config.STATUS_ICONS["absent"])
    return " ".join(line), pass_days

async def build_grouped_report_body(guild, dates, is_final=False):
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        db_users = await get_all_users_for_month(db, dates[0].year, dates[0].month)
        all_user_ids = set(db_users)
        for g in config.USER_GROUPS.values():
            all_user_ids.update(str(uid) for uid in g["members"])
        
        report_sections = []
        
        # 1. 정의된 그룹
        for group_name, info in config.USER_GROUPS.items():
            lines = []
            for uid in info["members"]:
                uid_str = str(uid)
                member = guild.get_member(uid)
                if not member: continue

                status_line, pass_days = await generate_weekly_status_line(db, uid_str, dates)
                goal_text = await get_weekly_goal_text(db, uid_str, get_this_monday_str()) or "미설정"
                formatted_goal = goal_text.replace("\n", "\n      ") 
                
                user_info = f"{status_line} {member.mention}"
                if is_final:
                    result = "🎉 달성" if pass_days >= config.WEEKLY_GOAL_DAYS else "😥 미달성"
                    lines.append(f"{user_info} **{result}**")
                else:
                    lines.append(f"{user_info}\n   └ 🎯 {formatted_goal}")

            if lines:
                report_sections.append(f"\n**{group_name}**\n" + "\n".join(lines))

        # 2. 기타 인원 (그룹 미포함자)
        others = []
        for uid_str in all_user_ids:
            is_in_group = False
            for info in config.USER_GROUPS.values():
                if int(uid_str) in info["members"]:
                    is_in_group = True; break
            
            if not is_in_group:
                member = guild.get_member(int(uid_str))
                if member:
                    status_line, _ = await generate_weekly_status_line(db, uid_str, dates)
                    others.append(f"{status_line} {member.mention}")
        
        if others:
            report_sections.append("\n**👻 깍두기**\n" + "\n".join(others))
            
    return "\n".join(report_sections)

async def build_weekly_mid_report(guild, date):
    week_start = date - timedelta(days=date.weekday())
    dates = [week_start + timedelta(days=i) for i in range(4)]
    header = config.MESSAGE_HEADINGS["weekly_mid_check"].format(month=date.month, week=get_week_of_month(date))
    body = await build_grouped_report_body(guild, dates, is_final=False)
    return f"{header}\n\n`월 화 수 목` 현황입니다.\n{body}\n\n모두 목표 달성까지 파이팅! 🚀"

async def build_manual_weekly_check_report(guild, date):
    week_start = date - timedelta(days=date.weekday())
    num_days = date.weekday() + 1
    dates = [week_start + timedelta(days=i) for i in range(num_days)]
    weekday_labels = " ".join(["월", "화", "수", "목", "금", "토", "일"][:num_days])
    header = f"[📢 현재 주간 현황] {date.month}월 {get_week_of_month(date)}주차"
    body = await build_grouped_report_body(guild, dates, is_final=False)
    return f"{header}\n\n`{weekday_labels}`\n{body}"

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
    print(f'✅ {bot.user} 로그인 성공!')

# [기능 1] 상태 변경 감지
@bot.event
async def on_voice_channel_status_update(channel, before, after):
    if channel.id != config.VOICE_CHANNEL_ID: return
    text_channel = channel.guild.get_channel(config.TEXT_CHANNEL_ID)
    if not text_channel or not after: return

    editor = "누군가"
    try:
        async for entry in channel.guild.audit_logs(limit=1, action=discord.AuditLogAction.voice_channel_status_update):
            if entry.target.id == channel.id:
                editor = entry.user.mention; break
    except: pass
    await text_channel.send(f"📢 {editor} 님이 '**{after}**' 집중 타임을 오픈했습니다! 함께 달려보세요! 🔥")

# [기능 2] 출석 체크
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return
    
    target_id = config.VOICE_CHANNEL_ID
    # 채널 이동이 없는 경우(마이크만 끄거나 등)는 무시
    if before.channel == after.channel: return

    text_channel = member.guild.get_channel(config.TEXT_CHANNEL_ID)
    if not text_channel: return

    is_join = (not before.channel or before.channel.id != target_id) and (after.channel and after.channel.id == target_id)
    is_leave = (before.channel and before.channel.id == target_id) and (not after.channel or after.channel.id != target_id)

    if not (is_join or is_leave): return

    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        if is_join:
            # 중복 입장 방지 체크
            cursor = await db.execute("SELECT check_in FROM active_sessions WHERE user_id = ?", (str(member.id),))
            if await cursor.fetchone() is None:
                now = datetime.now(KST)
                await db.execute("INSERT INTO active_sessions (user_id, check_in) VALUES (?, ?)", (str(member.id), now.isoformat()))
                await db.commit()
                await text_channel.send(f"{member.mention}님, 작업 시작! 🔥")
        
        elif is_leave:
            cursor = await db.execute("SELECT check_in FROM active_sessions WHERE user_id = ?", (str(member.id),))
            row = await cursor.fetchone()
            if row:
                check_in = datetime.fromisoformat(row[0])
                check_out = datetime.now(KST)
                await db.execute("DELETE FROM active_sessions WHERE user_id = ?", (str(member.id),))
                
                # 날짜별 세션 분리 및 저장
                for s in split_session_by_day(check_in, check_out):
                    await db.execute("INSERT INTO attendance (user_id, check_in, check_out, duration, check_in_date) VALUES (?, ?, ?, ?, ?)", 
                                     (str(member.id), s["check_in"], s["check_out"], s["duration"], datetime.fromisoformat(s["check_in"]).date().isoformat()))
                await db.commit()
                
                # 메시지 전송
                total = await get_today_total_duration(db, str(member.id), check_out.date().isoformat())
                goal = config.get_user_goal(member.id)
                
                def fmt(sec): return f"{int(sec//3600)}시간 {int((sec%3600)//60)}분"
                msg = f"{member.mention}님 수고하셨습니다! 👏\n> 오늘 기록: **{fmt(total)}** / {fmt(goal)}"
                await text_channel.send(msg)

# [기능 3] DM 및 채팅 명령어 통합
@bot.event
async def on_message(message):
    if message.author.bot: return
    
    # DM 처리
    if isinstance(message.channel, discord.DMChannel):
        content = message.content.strip()
        
        # !목표
        if content.startswith('!목표 '):
            goal = content.replace('!목표', '', 1).strip()
            if not goal:
                await message.channel.send("내용을 입력해주세요. (예: `!목표 자소서 1개 완성`)")
                return
            
            week_start = get_this_monday_str()
            async with aiosqlite.connect(config.DATABASE_NAME) as db:
                await db.execute("""
                    INSERT INTO weekly_goals (user_id, goal_text, week_start_date) 
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, week_start_date) DO UPDATE SET goal_text = excluded.goal_text
                """, (str(message.author.id), goal, week_start))
                await db.commit()
            
            # 줄바꿈 예쁘게
            pretty_goal = goal.replace("\n", "\n> ")
            await message.channel.send(f"✅ 이번 주 목표가 저장되었습니다:\n> {pretty_goal}")
            
        # !집중 (수동/안내)
        elif content.startswith('!집중'):
            # 수동 입력 (!집중 내용)
            if content.startswith('!집중 '):
                task = content.replace('!집중', '', 1).strip()
                if not bot.guilds: return
                
                # 공통 채널/멤버 조회 로직
                guild = bot.guilds[0]
                text_channel = guild.get_channel(config.TEXT_CHANNEL_ID)
                member = guild.get_member(message.author.id)

                if text_channel and member:
                    await text_channel.send(f"📢 {member.mention} 님이 '**{task}**' 집중 타임을 오픈했습니다! 함께 달려보세요! 🔥")
                    await message.channel.send(f"✅ 공지 완료: {task}")
                else:
                    await message.channel.send("오류: 서버나 채널을 찾을 수 없습니다.")
            else:
                # 내용 없이 !집중 -> 안내
                await message.channel.send("💡 사용법: `!집중 [할일]` (직접 입력) 또는 음성 채널 상태를 변경해주세요!")

    # 채팅방 명령어
    else:
        await bot.process_commands(message)

# --- Bot Commands ---
@bot.command(name="현황")
async def weekly_check_command(ctx):
    await ctx.send("이번 주 출석 현황을 집계 중입니다... 🗓️")
    msg = await build_manual_weekly_check_report(ctx.guild, datetime.now(KST).date())
    await ctx.send(msg)

@bot.command(name="목표공지")
async def announce_weekly_goals(ctx):
    notice_channel = ctx.guild.get_channel(config.NOTICE_CHANNEL_ID)
    if not notice_channel:
        await ctx.send("❌ 설정된 공지 채널을 찾을 수 없습니다.")
        return

    week_start = get_this_monday_str()
    today = datetime.now(KST)
    msg_lines = [f"📢 **{today.month}월 {get_week_of_month(today.date())}주차 주간 목표**\n"]
    
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        for group_name, info in config.USER_GROUPS.items():
            group_lines = []
            for uid in info["members"]:
                goal_text = await get_weekly_goal_text(db, str(uid), week_start)
                if goal_text:
                    member = ctx.guild.get_member(uid)
                    name = member.display_name if member else "(알수없음)"
                    formatted_goal = goal_text.replace("\n", "\n      ")
                    group_lines.append(f"- **{name}**: {formatted_goal}")
            
            if group_lines:
                msg_lines.append(f"\n**{group_name}**")
                msg_lines.extend(group_lines)
    
    if len(msg_lines) == 1:
        await ctx.send("등록된 이번 주 목표가 아직 없습니다.")
    else:
        await notice_channel.send("\n".join(msg_lines))
        await ctx.send(f"✅ 공지 채널(<#{config.NOTICE_CHANNEL_ID}>)에 목표를 공유했습니다.")

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
    
    notice_channel = ctx.guild.get_channel(config.NOTICE_CHANNEL_ID)
    if not notice_channel:
        await ctx.send("공지 채널을 찾을 수 없어 현재 채널에 보냅니다.")
        notice_channel = ctx.channel

    await ctx.send(f"**{year}년 {month}월** 최종 결산 내역을 불러오는 중... 🏆")
    report = await build_monthly_final_report(ctx.guild, year, month)
    await notice_channel.send(report)

@bot.command(name="진단")
async def diagnose(ctx):
    await ctx.send("✅ 봇 정상 작동 중! (최적화 v4.0)")

# --- Scheduled Tasks ---
@tasks.loop(minutes=5)
async def main_scheduler():
    await bot.wait_until_ready()
    now = datetime.now(KST)
    today_str = now.date().isoformat()
    guild = bot.guilds[0] if bot.guilds else None
    if not guild: return
    
    text_channel = guild.get_channel(config.TEXT_CHANNEL_ID)
    notice_channel = guild.get_channel(config.NOTICE_CHANNEL_ID)

    # 1. 주간 중간 점검 (목요일 18시)
    if now.weekday() == 3 and now.hour == 18 and last_task_run["weekly_mid"] != today_str:
        last_task_run["weekly_mid"] = today_str
        if text_channel: await text_channel.send(await build_weekly_mid_report(guild, now.date()))

    # 2. 주간 최종 결산 (월요일 0시)
    if now.weekday() == 0 and now.hour == 0 and now.minute >= 5 and last_task_run["weekly_final"] != today_str:
        last_task_run["weekly_final"] = today_str
        msg = await build_manual_weekly_check_report(guild, (now - timedelta(days=1)).date())
        if text_channel: await text_channel.send(f"[✅ 주간 결산]\n{msg}")

    # 3. 월간 최종 정산 (매월 1일 1시) -> 공지 채널
    if now.day == 1 and now.hour == 1 and last_task_run["monthly_final"] != today_str:
        last_task_run["monthly_final"] = today_str
        target_date = now.date() - timedelta(days=1)
        year, month = target_date.year, target_date.month
        
        report = await build_monthly_final_report(guild, year, month)
        if notice_channel: 
            await notice_channel.send(report)
        
        async with aiosqlite.connect(config.DATABASE_NAME) as db:
            await db.execute("DELETE FROM attendance WHERE check_in_date < ?", (now.date().replace(day=1).isoformat(),))
            await db.commit()
        
        if text_channel: await text_channel.send(f"\n---\n*{month}월 데이터가 초기화되었습니다. {now.month}월도 파이팅!*")

# --- Run Bot ---
if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("에러: DISCORD_BOT_TOKEN이 설정되지 않았습니다. .env 파일을 확인해주세요.")
