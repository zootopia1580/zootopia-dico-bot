# main.py

"""
디스코드 음성 채널 출석 체크 봇 (ID 기반 + 상태 텍스트 감지)
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

print("★★★★★ 봇 실행! (상태 텍스트 감지 버전) ★★★★★★")

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

# --- Database Functions (변경 없음) ---
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
    # main_scheduler.start() # 스케줄러 필요시 주석 해제
    print(f'✅ {bot.user} 로그인 성공!')

# ★★★ [핵심 1] 음성 채널 상태 텍스트 감지 ★★★
@bot.event
async def on_voice_channel_status_update(channel, before, after):
    # ID 확인
    if channel.id != config.VOICE_CHANNEL_ID:
        return

    # 텍스트 채널 확인
    text_channel = channel.guild.get_channel(config.TEXT_CHANNEL_ID)
    if not text_channel:
        return

    # after 변수에는 변경된 '상태 텍스트'가 들어옵니다. (예: "테스트")
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

# ★★★ [핵심 2] 출석 체크 ★★★
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return
    text_channel = member.guild.get_channel(config.TEXT_CHANNEL_ID)
    if not text_channel: return

    target_id = config.VOICE_CHANNEL_ID
    
    is_join = (not before.channel or before.channel.id != target_id) and \
              (after.channel and after.channel.id == target_id)
    is_leave = (before.channel and before.channel.id == target_id) and \
               (not after.channel or after.channel.id != target_id)

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
                for s in split_session_by_day(check_in, check_out):
                    await db.execute("INSERT INTO attendance (user_id, check_in, check_out, duration, check_in_date) VALUES (?, ?, ?, ?, ?)", 
                                     (str(member.id), s["check_in"], s["check_out"], s["duration"], datetime.fromisoformat(s["check_in"]).date().isoformat()))
                await db.commit()
                total = await get_today_total_duration(db, str(member.id), check_out.date().isoformat())
                h, r = divmod(total, 3600)
                m, _ = divmod(r, 60)
                await text_channel.send(f"{member.mention}님 수고하셨습니다! (오늘: {int(h)}시간 {int(m)}분)")

# ★★★ [핵심 3] 수동 명령 (!집중) ★★★
@bot.event
async def on_message(message):
    if message.author.bot: return
    
    if isinstance(message.channel, discord.DMChannel):
        if message.content.strip() == '!집중':
            # 봇이 있는 서버 찾기
            if not bot.guilds: return
            guild = bot.guilds[0]
            
            # 텍스트 채널 찾기
            text_channel = guild.get_channel(config.TEXT_CHANNEL_ID)
            if not text_channel:
                await message.channel.send("오류: 채팅방을 찾을 수 없습니다.")
                return

            # 음성 채널 정보 가져오기 (ID 사용)
            try:
                target_channel = await bot.fetch_channel(config.VOICE_CHANNEL_ID)
                # 여기서 status를 가져옵니다. ("테스트" 같은 텍스트)
                status_text = getattr(target_channel, 'status', None)
                
                if status_text:
                    # 채팅방 공지
                    member = guild.get_member(message.author.id)
                    await text_channel.send(f"{member.mention} 님이 '**{status_text}**' 집중 타임을 오픈했습니다! 함께 달려보세요!")
                    await message.channel.send(f"🔥 알림 전송 완료: {status_text}")
                else:
                    await message.channel.send(f"음성 채널 상태가 비어있습니다. '{target_channel.name}' 채널 상태를 먼저 설정해주세요.")
            except Exception as e:
                await message.channel.send(f"오류 발생: {e}")

    else:
        await bot.process_commands(message)

# --- [NEW] 수정된 진단 명령어 ---
@bot.command(name="진단")
async def diagnose(ctx):
    import discord
    import sys
    version_info = f"🐍 Python: {sys.version.split()[0]}\n🤖 discord.py: {discord.__version__}"
    
    # ID로 채널 확인
    try:
        target_vc = await bot.fetch_channel(config.VOICE_CHANNEL_ID)
        target_tc = await bot.fetch_channel(config.TEXT_CHANNEL_ID)
        
        # ★ 여기가 수정되었습니다: status 값을 직접 출력합니다 ★
        current_status = getattr(target_vc, 'status', '없음(None)')
        
        msg = f"""
[채널 연결 상태]
음성방 이름: {target_vc.name}
음성방 상태(Status): {current_status}  <-- 여기에 '테스트'가 나와야 합니다!
채팅방 이름: {target_tc.name}
"""
    except Exception as e:
        msg = f"\n❌ 채널 정보 조회 실패: {e}"

    await ctx.send(f"```{version_info}{msg}```")

# --- Run Bot ---
if __name__ == "__main__":
    bot.run(TOKEN)
