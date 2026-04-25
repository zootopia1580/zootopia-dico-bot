"""
디스코드 음성 채널 출석 봇 (개편버전)
"""

import os
import random
import calendar
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import aiosqlite
from datetime import datetime, timedelta, time, timezone
from collections import defaultdict
import config

print("★★★★★ 봇 실행! ★★★★★")

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

KST = timezone(timedelta(hours=9))

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix=config.BOT_PREFIX, intents=intents)
last_task_run = defaultdict(lambda: None)


# ──────────────────────────────────────────
# DB 초기화
# ──────────────────────────────────────────
async def init_db():
    import os
    os.makedirs("data", exist_ok=True)
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                check_in TEXT,
                check_out TEXT,
                duration INTEGER,
                check_in_date TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS active_sessions (
                user_id TEXT PRIMARY KEY,
                check_in TEXT
            )
        """)
        await db.commit()


# ──────────────────────────────────────────
# 헬퍼 함수
# ──────────────────────────────────────────
def fmt_time(seconds):
    h, r = divmod(int(seconds), 3600)
    m, _ = divmod(r, 60)
    return f"{h}시간 {m:02d}분"


def split_session_by_day(check_in, check_out):
    sessions = []
    current = check_in
    while current.date() < check_out.date():
        end = datetime.combine(current.date(), time(23, 59, 59), tzinfo=current.tzinfo)
        sessions.append({
            "check_in": current.isoformat(),
            "check_out": end.isoformat(),
            "duration": (end - current).total_seconds()
        })
        current = end + timedelta(seconds=1)
    sessions.append({
        "check_in": current.isoformat(),
        "check_out": check_out.isoformat(),
        "duration": (check_out - current).total_seconds()
    })
    return sessions


async def save_session(db, user_id, check_in_iso, check_out):
    check_in = datetime.fromisoformat(check_in_iso)
    split_sessions = split_session_by_day(check_in, check_out)
    for s in split_sessions:
        await db.execute(
            "INSERT INTO attendance (user_id, check_in, check_out, duration, check_in_date) VALUES (?,?,?,?,?)",
            (
                user_id,
                s["check_in"], s["check_out"], s["duration"],
                datetime.fromisoformat(s["check_in"]).date().isoformat()
            )
        )
    return split_sessions


async def get_duration_sum(db, user_id, date_str):
    cur = await db.execute(
        "SELECT SUM(duration) FROM attendance WHERE user_id=? AND check_in_date=?",
        (user_id, date_str)
    )
    row = await cur.fetchone()
    return row[0] if row and row[0] else 0


async def get_week_duration(db, user_id, week_dates):
    placeholders = ",".join("?" for _ in week_dates)
    cur = await db.execute(
        f"SELECT SUM(duration) FROM attendance WHERE user_id=? AND check_in_date IN ({placeholders})",
        [user_id] + [d.isoformat() for d in week_dates]
    )
    row = await cur.fetchone()
    return row[0] if row and row[0] else 0


async def get_month_duration(db, user_id, year, month):
    start = f"{year}-{month:02d}-01"
    end = f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]}"
    cur = await db.execute(
        "SELECT SUM(duration) FROM attendance WHERE user_id=? AND check_in_date BETWEEN ? AND ?",
        (user_id, start, end)
    )
    row = await cur.fetchone()
    return row[0] if row and row[0] else 0


async def get_all_users_this_month(db, year, month):
    start = f"{year}-{month:02d}-01"
    end = f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]}"
    cur = await db.execute(
        "SELECT DISTINCT user_id FROM attendance WHERE check_in_date BETWEEN ? AND ?",
        (start, end)
    )
    return [row[0] for row in await cur.fetchall()]


def get_week_dates(ref_date):
    monday = ref_date - timedelta(days=ref_date.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


def get_join_message(member, hour):
    if 18 <= hour < 22:
        pool = config.JOIN_MESSAGES_EVENING
    elif hour >= 22 or hour < 6:
        pool = config.JOIN_MESSAGES_NIGHT
    else:
        pool = config.JOIN_MESSAGES_DEFAULT
    return random.choice(pool).format(mention=member.mention)


def get_leave_message(member, hour):
    if 18 <= hour < 22:
        pool = config.LEAVE_MESSAGES_EVENING
    elif hour >= 22 or hour < 6:
        pool = config.LEAVE_MESSAGES_NIGHT
    else:
        pool = config.LEAVE_MESSAGES_DEFAULT
    return random.choice(pool).format(mention=member.mention)


# ──────────────────────────────────────────
# 주간 결산 임베드
# ──────────────────────────────────────────
async def build_weekly_embed(guild, week_dates):
    month = week_dates[0].month
    week_num = (week_dates[0].day - 1) // 7 + 1
    title = f"📊 {month}월 {week_num}주차 주간 결산"
    desc = "지난 한 주, 다들 얼마나 달렸나 봅시다 👀\n"

    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        members_data = []
        for member in guild.members:
            if member.bot:
                continue
            total = await get_week_duration(db, str(member.id), week_dates)
            if total > 0:
                members_data.append((member, total))

        if not members_data:
            return discord.Embed(title=title, description="이번 주 기록이 없어요 😅", color=0x5865F2)

        members_data.sort(key=lambda x: x[1], reverse=True)

        groups = defaultdict(list)
        for member, total in members_data:
            emoji, label = config.get_weekly_tier(total)
            groups[(emoji, label)].append((member, total))

        embed = discord.Embed(title=title, description=desc, color=0x5865F2)

        tier_order = [(e, l) for _, _, e, l in config.WEEKLY_TIERS]
        for emoji, label in tier_order:
            if (emoji, label) in groups:
                lines = ""
                for member, total in groups[(emoji, label)]:
                    lines += f"{member.display_name}   {fmt_time(total)}\n"
                embed.add_field(
                    name=f"{emoji} {label} 그룹",
                    value=lines,
                    inline=False
                )

        mvp_member, mvp_time = members_data[0]
        embed.add_field(
            name="이번 주 MVP 🥇",
            value=f"{mvp_member.mention} ({fmt_time(mvp_time)})",
            inline=False
        )
        embed.set_footer(text=f"{month}월 {week_num + 1}주차도 달려봅시다 💪")
        return embed


# ──────────────────────────────────────────
# 월간 결산
# ──────────────────────────────────────────
async def build_monthly_report(guild, year, month):
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        user_ids = await get_all_users_this_month(db, year, month)
        if not user_ids:
            return f"📅 {month}월 기록이 없어요."

        members_data = []
        for uid in user_ids:
            member = guild.get_member(int(uid))
            if not member or member.bot:
                continue
            total = await get_month_duration(db, uid, year, month)
            week_emojis = ""
            for week in calendar.monthcalendar(year, month):
                dates = [datetime(year, month, d).date() for d in week if d != 0]
                week_total = await get_week_duration(db, uid, dates)
                emoji, _ = config.get_weekly_tier(week_total)
                week_emojis += emoji
            members_data.append((member, total, week_emojis))

        if not members_data:
            return f"📅 {month}월 기록이 없어요."

        members_data.sort(key=lambda x: x[1], reverse=True)

        lines = [
            f"🗓 **{month}월 월간 결산**",
            "한 달 동안 정말 수고하셨어요 👏\n",
            "🏅 **이달의 순위**\n"
        ]

        medals = ["🥇", "🥈", "🥉"]
        for i, (member, total, week_emojis) in enumerate(members_data):
            medal = medals[i] if i < 3 else f"{i+1}위"
            lines.append(f"{medal} {member.display_name}   {fmt_time(total)}   {week_emojis}")

        mvp = members_data[0][0]
        lines.append(f"\n🏆 **이달의 MVP**   {mvp.mention} ({fmt_time(members_data[0][1])})")

        for member, total, week_emojis in members_data:
            if "⬜" not in week_emojis:
                lines.append(f"🔥 **개근상**   {member.mention} (한 주도 빠지지 않음!)")
                break

        if len(members_data) > 1:
            last = members_data[-1][0]
            lines.append(f"📈 **다음 달엔 더 달려봐요**   {last.mention} 💪")

        lines.append(f"\n---\n{month}월 데이터를 초기화합니다.\n{month + 1}월도 화이팅! 🚀")
        return "\n".join(lines)


# ──────────────────────────────────────────
# 봇 이벤트
# ──────────────────────────────────────────
@bot.event
async def on_ready():
    await init_db()
    main_scheduler.start()
    print(f"✅ {bot.user} 로그인 성공!")

    for guild in bot.guilds:
        voice_channel = guild.get_channel(config.VOICE_CHANNEL_ID)
        if not voice_channel:
            continue

        async with aiosqlite.connect(config.DATABASE_NAME) as db:
            current_members = {str(m.id) for m in voice_channel.members if not m.bot}

            cur = await db.execute("SELECT user_id, check_in FROM active_sessions")
            db_sessions = {row[0]: row[1] for row in await cur.fetchall()}

            for uid in list(db_sessions.keys()):
                if uid not in current_members:
                    check_out = datetime.now(KST)
                    await save_session(db, uid, db_sessions[uid], check_out)
                    await db.execute("DELETE FROM active_sessions WHERE user_id=?", (uid,))
                    print(f"오프라인 중 퇴장 처리: {uid}")

            for uid in current_members:
                if uid not in db_sessions:
                    now = datetime.now(KST)
                    await db.execute(
                        "INSERT INTO active_sessions (user_id, check_in) VALUES (?,?)",
                        (uid, now.isoformat())
                    )
                    print(f"신규 세션 시작: {uid}")

            await db.commit()


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    text_channel = member.guild.get_channel(config.TEXT_CHANNEL_ID)
    if not text_channel:
        return

    target_id = config.VOICE_CHANNEL_ID
    is_join = (
        (not before.channel or before.channel.id != target_id)
        and (after.channel and after.channel.id == target_id)
    )
    is_leave = (
        (before.channel and before.channel.id == target_id)
        and (not after.channel or after.channel.id != target_id)
    )

    if is_join:
        async with aiosqlite.connect(config.DATABASE_NAME) as db:
            cur = await db.execute(
                "SELECT check_in FROM active_sessions WHERE user_id=?", (str(member.id),)
            )
            if await cur.fetchone() is None:
                now = datetime.now(KST)
                await db.execute(
                    "INSERT INTO active_sessions (user_id, check_in) VALUES (?,?)",
                    (str(member.id), now.isoformat())
                )
                await db.commit()

        msg = get_join_message(member, datetime.now(KST).hour)
        await text_channel.send(msg)

        voice_channel = member.guild.get_channel(target_id)
        if voice_channel:
            count = len([m for m in voice_channel.members if not m.bot])
            if count in config.HEADCOUNT_MESSAGES:
                await text_channel.send(config.HEADCOUNT_MESSAGES[count])

    elif is_leave:
        async with aiosqlite.connect(config.DATABASE_NAME) as db:
            cur = await db.execute(
                "SELECT check_in FROM active_sessions WHERE user_id=?", (str(member.id),)
            )
            row = await cur.fetchone()
            if row:
                check_out = datetime.now(KST)
                split_sessions = await save_session(db, str(member.id), row[0], check_out)
                await db.execute(
                    "DELETE FROM active_sessions WHERE user_id=?", (str(member.id),)
                )
                await db.commit()

        # ★ 커밋 후 새 커넥션으로 읽기 ★
        async with aiosqlite.connect(config.DATABASE_NAME) as db2:
            today_str = check_out.date().isoformat()
            today_total = await get_duration_sum(db2, str(member.id), today_str)
            week_dates = get_week_dates(check_out.date())
            week_total = await get_week_duration(db2, str(member.id), week_dates)

            involved_dates = sorted(set(
                datetime.fromisoformat(s["check_in"]).date() for s in split_sessions
            ))

            emoji, label = config.get_weekly_tier(week_total)
            leave_msg = get_leave_message(member, check_out.hour)
            msg_lines = [leave_msg]

            if len(involved_dates) > 1:
                for d in involved_dates:
                    day_total = await get_duration_sum(db2, str(member.id), d.isoformat())
                    msg_lines.append(f"> {d.month}/{d.day}: {fmt_time(day_total)}")
            else:
                msg_lines.append(f"> 오늘: {fmt_time(today_total)}")

            msg_lines.append(f"> 이번 주 누적: {fmt_time(week_total)} {emoji} {label}")
            await text_channel.send("\n".join(msg_lines))


# ──────────────────────────────────────────
# 명령어
# ──────────────────────────────────────────
@bot.command(name="현황")
async def weekly_status(ctx):
    now = datetime.now(KST)
    week_dates = get_week_dates(now.date())
    embed = await build_weekly_embed(ctx.guild, week_dates)
    await ctx.send(embed=embed)


@bot.command(name="내기록")
async def my_record(ctx):
    now = datetime.now(KST)
    week_dates = get_week_dates(now.date())
    async with aiosqlite.connect(config.DATABASE_NAME) as db:
        week_total = await get_week_duration(db, str(ctx.author.id), week_dates)
        month_total = await get_month_duration(db, str(ctx.author.id), now.year, now.month)
    emoji, label = config.get_weekly_tier(week_total)
    await ctx.send(
        f"📊 **{ctx.author.display_name}** 님의 기록\n"
        f"> 이번 주: {fmt_time(week_total)} {emoji} {label}\n"
        f"> 이번 달: {fmt_time(month_total)}"
    )


@bot.command(name="진단")
async def diagnose(ctx):
    await ctx.send("✅ 봇 정상 작동 중!")


# ──────────────────────────────────────────
# 스케줄러
# ──────────────────────────────────────────
@tasks.loop(minutes=1)
async def main_scheduler():
    await bot.wait_until_ready()
    now = datetime.now(KST)
    today_str = now.date().isoformat()
    guild = bot.guilds[0] if bot.guilds else None
    if not guild:
        return

    text_channel = guild.get_channel(config.TEXT_CHANNEL_ID)

    if now.weekday() == 0 and now.hour == 9 and now.minute == 0 and last_task_run["weekly"] != today_str:
        last_task_run["weekly"] = today_str
        last_monday = now.date() - timedelta(days=7)
        week_dates = get_week_dates(last_monday)
        embed = await build_weekly_embed(guild, week_dates)
        if text_channel:
            await text_channel.send(embed=embed)

    if now.day == 1 and now.hour == 9 and now.minute == 0 and last_task_run["monthly"] != today_str:
        last_task_run["monthly"] = today_str
        last_month = now.date().replace(day=1) - timedelta(days=1)
        report = await build_monthly_report(guild, last_month.year, last_month.month)
        if text_channel:
            await text_channel.send(report)

        async with aiosqlite.connect(config.DATABASE_NAME) as db:
            start = f"{last_month.year}-{last_month.month:02d}-01"
            end = f"{last_month.year}-{last_month.month:02d}-{calendar.monthrange(last_month.year, last_month.month)[1]}"
            await db.execute(
                "DELETE FROM attendance WHERE check_in_date BETWEEN ? AND ?", (start, end)
            )
            await db.commit()


# ──────────────────────────────────────────
# 실행
# ──────────────────────────────────────────
if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("에러: 토큰 없음")
