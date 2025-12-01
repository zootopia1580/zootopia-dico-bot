# main.py

"""
디스코드 음성 채널 출석 체크 봇 (자정 넘김 기록 복구 버전)
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
import re

print("★★★★★ 봇 실행! (자정 넘김 기록 복구 완료) ★★★★★★")

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
    return "".join(line), pass_days

def format_clean_goal(text):
    if not text: return "미설정"
    lines = text.split('\n')
    formatted_lines = []
    for line in lines:
        line = line.strip()
        if not line: continue
        line = re.sub(r"^[-*•・>]\s*", "", line)
        formatted_lines.append(f"▫️ {line}")
    return "\n".join(formatted_lines)

async def build_grouped_report_body(guild, dates, is_final=False):
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        db_users = await get_all_users_for_month(db, dates[0].year, dates[0].month)
        all_user_ids = set(db_users)
        for g in config.USER_GROUPS.values():
            all_user_ids.update(str(uid) for uid in g["members"])
        
        report_data = {}
        
        for group_name, info in config.USER_GROUPS.items():
            group_content = ""
            for uid in info["members"]:
                uid_str = str(uid)
                member = guild.get_member(uid)
                if not member: continue

                status_line, pass_days = await generate_weekly_status_line(db, uid_str, dates)
                goal_text = await get_weekly_goal_text(db, uid_str, get_this_monday_str()) or "미설정"
                
                formatted_goal = format_clean_goal(goal_text)
                user_header = f"{status_line} **{member.display_name}**"
                
                if is_final:
                    result = "🎉 **달성**" if pass_days >= config.WEEKLY_GOAL_DAYS else "😥 미달성"
                    group_content += f"{user_header} - {result}\n"
                else:
                    group_content += f"{user_header}\n> {formatted_goal.replace(chr(10), chr(10) + '>    ')}\n\n"

            if group_content:
                report_data[group_name] = group_content

        others_content = ""
        for uid_str in all_user_ids:
            is_in_group = False
            for info in config.USER_GROUPS.values():
                if int(uid_str) in info["members"]:
                    is_in_group = True; break
            if not is_in_group:
                member = guild.get_member(int(uid_str))
                if member:
                    status_line, _ = await generate_weekly_status_line(db, uid_str, dates)
                    others_content += f"{status_line} {member.mention}\n"
        
        if others_content:
            report_data["👻 깍두기"] = others_content
            
    return report_data

async def build_weekly_embed(guild, date, is_final=False):
    week_start = date - timedelta(days=date.weekday())
    if is_final:
        dates = [week_start + timedelta(days=i) for i in range(7)]
        title = config.MESSAGE_HEADINGS["weekly_final"].format(month=date.month, week=get_week_of_month(date))
        desc = "지난 한 주 모두 고생 많으셨습니다! 최종 결과입니다."
        color = 0x00FF00
    else:
        dates = [week_start + timedelta(days=i) for i in range(4)]
        title = config.MESSAGE_HEADINGS["weekly_mid_check"].format(month=date.month, week=get_week_of_month(date))
        desc = "주말까지 이틀 남았어요! `월 화 수 목` 현황과 목표를 확인하세요."
        color = 0xFFA500

    data = await build_grouped_report_body(guild, dates, is_final)
    embed = discord.Embed(title=title, description=desc, color=color)
    for group_name, content in data.items():
        if content:
            if len(content) > 1024: content = content[:1020] + "..."
            embed.add_field(name=group_name, value=content, inline=False)
    embed.set_footer(text="모두 목표 달성까지 파이팅! 🚀")
    return embed

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

# [수정됨] 자정 넘김 처리 로직 복구
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
                await text_channel.send(f"{member.mention}님, 작업 시작! 🔥")
        
        elif is_leave:
            cursor = await db.execute("SELECT check_in FROM active_sessions WHERE user_id = ?", (str(member.id),))
            row = await cursor.fetchone()
            if row:
                check_in = datetime.fromisoformat(row[0])
                check_out = datetime.now(KST)
                await db.execute("DELETE FROM active_sessions WHERE user_id = ?", (str(member.id),))
                
                # 세션 분리 및 저장
                split_sessions = split_session_by_day(check_in, check_out)
                for s in split_sessions:
                    await db.execute("INSERT INTO attendance (user_id, check_in, check_out, duration, check_in_date) VALUES (?, ?, ?, ?, ?)", 
                                     (str(member.id), s["check_in"], s["check_out"], s["duration"], datetime.fromisoformat(s["check_in"]).date().isoformat()))
                await db.commit()
                
                # ★ 여기부터 수정됨: 날짜별로 출력 ★
                involved_dates = sorted(list(set([datetime.fromisoformat(s["check_in"]).date() for s in split_sessions])))
                goal = config.get_user_goal(member.id)
                
                def fmt_time(seconds):
                    h, r = divmod(seconds, 3600)
                    m, _ = divmod(r, 60)
                    return f"{int(h):02d}시간 {int(m):02d}분"
                
                msg_lines = [f"{member.mention}님 수고하셨습니다! 👏"]
                
                for d in involved_dates:
                    total = await get_today_total_duration(db, str(member.id), d.isoformat())
                    # 날짜별 기록 출력 (예: > 23일: 02시간 09분 / 04시간 00분)
                    msg_lines.append(f"> {d.day}일: **{fmt_time(total)}** / {fmt_time(goal)}")
                
                await text_channel.send("\n".join(msg_lines))

@bot.event
async def on_message(message):
    if message.author.bot: return
    
    if isinstance(message.channel, discord.DMChannel):
        content = message.content.strip()
        if content.startswith('!목표 '):
            goal = content.replace('!목표', '', 1).strip()
            if not goal:
                await message.channel.send("내용을 입력해주세요.")
                return
            week_start = get_this_monday_str()
            async with aiosqlite.connect(config.DATABASE_NAME) as db:
                await db.execute("""
                    INSERT INTO weekly_goals (user_id, goal_text, week_start_date) 
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, week_start_date) DO UPDATE SET goal_text = excluded.goal_text
                """, (str(message.author.id), goal, week_start))
                await db.commit()
            
            pretty_goal = format_clean_goal(goal)
            formatted_display = pretty_goal.replace("\n", "\n>    ")
            await message.channel.send(f"✅ 이번 주 목표가 저장되었습니다:\n> {formatted_display}")
            
        elif content.startswith('!집중'):
            if content.startswith('!집중 '):
                task = content.replace('!집중', '', 1).strip()
                if not bot.guilds: return
                guild = bot.guilds[0]
                text_channel = guild.get_channel(config.TEXT_CHANNEL_ID)
                member = guild.get_member(message.author.id)
                if text_channel and member:
                    await text_channel.send(f"📢 {member.mention} 님이 '**{task}**' 집중 타임을 오픈했습니다! 함께 달려보세요! 🔥")
                    await message.channel.send(f"✅ 공지 완료: {task}")
            else:
                await message.channel.send("💡 사용법: `!집중 [할일]` (직접 입력)")
    else:
        await bot.process_commands(message)

# --- Bot Commands ---
@bot.command(name="현황")
async def weekly_check_command(ctx):
    await ctx.send("이번 주 출석 현황을 집계 중입니다... 🗓️")
    embed = await build_weekly_embed(ctx.guild, datetime.now(KST).date(), is_final=False)
    await ctx.send(embed=embed)

@bot.command(name="목표공지")
async def announce_weekly_goals(ctx):
    notice_channel = bot.get_channel(config.NOTICE_CHANNEL_ID)
    if not notice_channel:
        if ctx.channel: await ctx.send("❌ 공지 채널을 찾을 수 없습니다.")
        return

    week_start = get_this_monday_str()
    today = datetime.now(KST)
    
    header_msg = f"📢 **{today.month}월 {get_week_of_month(today.date())}주차 주간 목표**\n이번 주도 힘차게 달려봅시다! 🔥"
    
    embeds = []
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        for group_name, info in config.USER_GROUPS.items():
            embed = discord.Embed(title=f"{group_name}", color=0x3498db) # 아이콘은 제목에 포함하지 않음
            has_member = False
            for uid in info["members"]:
                goal_text = await get_weekly_goal_text(db, str(uid), week_start)
                if goal_text:
                    member = notice_channel.guild.get_member(uid)
                    name = member.display_name if member else "(알수없음)"
                    
                    formatted_goal = format_clean_goal(goal_text)
                    formatted_goal_quoted = formatted_goal.replace("\n", "\n> ")
                    
                    embed.add_field(name=name, value=f"> {formatted_goal_quoted}", inline=False)
                    has_member = True
            
            if has_member:
                embeds.append(embed)
    
    if embeds:
        await notice_channel.send(content=header_msg, embeds=embeds)
        if ctx.channel and not isinstance(ctx.channel, discord.DMChannel):
            await ctx.send(f"✅ 공지 채널에 목표를 공유했습니다.")
    else:
        if ctx.channel: await ctx.send("등록된 목표가 없습니다.")

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
    
    notice_channel = bot.get_channel(config.NOTICE_CHANNEL_ID) or ctx.channel
    report = await build_monthly_final_report(ctx.guild, year, month)
    await notice_channel.send(report)

@bot.command(name="진단")
async def diagnose(ctx):
    await ctx.send("✅ 봇 정상 작동 중! (v15.0 - 자정 기록 복구)")

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

    if now.weekday() == 3 and now.hour == 18 and last_task_run["weekly_mid"] != today_str:
        last_task_run["weekly_mid"] = today_str
        if text_channel: 
            embed = await build_weekly_embed(guild, now.date(), is_final=False)
            await text_channel.send(embed=embed)

    if now.weekday() == 0 and now.hour == 0 and now.minute >= 5 and last_task_run["weekly_final"] != today_str:
        last_task_run["weekly_final"] = today_str
        if text_channel:
            embed = await build_weekly_embed(guild, (now - timedelta(days=1)).date(), is_final=True)
            await text_channel.send(embed=embed)

    if now.day == 1 and now.hour == 1 and last_task_run["monthly_final"] != today_str:
        last_task_run["monthly_final"] = today_str
        year, month = (now.date() - timedelta(days=1)).year, (now.date() - timedelta(days=1)).month
        
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
        print("에러: 토큰 없음")
