# -*- coding: utf-8 -*-
"""
بوت الكبسولة الزمنية 🕰️ — زاويتنا
يخلي الأعضاء يكتبون رسالة الحين، وتنحفظ، وتنفتح وتوصل في وقت محدد بالمستقبل.

- كبسولة خاصة : توصل للعضو بالخاص (DM)
- كبسولة للقروب: تنفتح وتنشر بالقناة في وقتها

تحديد الوقت: إما مدة (يوم/اسبوع/شهر/سنة) أو تاريخ محدد بصيغة YYYY-MM-DD
"""

import os
import re
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks
from dateutil.relativedelta import relativedelta

# ───────────────────────── الإعدادات ─────────────────────────

# توقيت السعودية (UTC+3 بدون توقيت صيفي)
KSA = timezone(timedelta(hours=3))

DB_PATH = os.environ.get("DB_PATH", "capsules.db")   # تقدرين تخلينه على فوليوم في Railway
PREFIX = "@"                                          # البادئة اللي تفضلينها
MAX_MSG_LEN = 1500                                    # أقصى طول لرسالة الكبسولة

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# الأعضاء اللي عندهم محادثة إنشاء كبسولة شغّالة الحين (عشان ما تتداخل الأوامر)
active_sessions = set()

CANCEL_WORDS = {"الغاء", "إلغاء", "الغ", "cancel", "كنسل"}
YES_WORDS = {"نعم", "اي", "ايه", "أيه", "ايوه", "تمام", "اوكي", "اوك", "yes", "y"}

# ───────────────────────── قاعدة البيانات ─────────────────────────

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS capsules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            user_name   TEXT,
            guild_id    INTEGER,
            channel_id  INTEGER,
            target      TEXT    NOT NULL,   -- 'dm' أو 'channel'
            message     TEXT    NOT NULL,
            created_at  TEXT    NOT NULL,   -- ISO UTC
            open_at     TEXT    NOT NULL,   -- ISO UTC
            delivered   INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


# ───────────────────────── أدوات مساعدة ─────────────────────────

ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"

def normalize_digits(text: str) -> str:
    """يحوّل الأرقام العربية (٠١٢..) إلى أرقام عادية (012..)"""
    table = {ord(a): str(i) for i, a in enumerate(ARABIC_DIGITS)}
    return text.translate(table)


def is_cancel(text: str) -> bool:
    return text.strip() in CANCEL_WORDS


def fmt_ksa(dt_utc: datetime) -> str:
    """يعرض تاريخ بتوقيت السعودية"""
    local = dt_utc.astimezone(KSA)
    return local.strftime("%Y-%m-%d %H:%M") + " (توقيت السعودية)"


def parse_open_time(text: str):
    """
    يرجّع datetime (UTC) لوقت فتح الكبسولة.
    يرجّع 'past' لو التاريخ بالماضي، أو None لو ما فهم الصيغة.
    """
    text = normalize_digits(text.strip())
    now_utc = datetime.now(timezone.utc)

    # 1) تاريخ محدد YYYY-MM-DD
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            local = datetime(y, mo, d, 9, 0, 0, tzinfo=KSA)  # 9 صباحاً بتوقيت السعودية
            open_utc = local.astimezone(timezone.utc)
            return "past" if open_utc <= now_utc else open_utc
        except ValueError:
            return None

    # 2) مدة: رقم + وحدة
    num_m = re.search(r"(\d+)", text)
    if not num_m:
        return None
    n = int(num_m.group(1))
    if n <= 0:
        return None

    if re.search(r"(ساعة|ساعات|ساعه)", text):
        return now_utc + timedelta(hours=n)
    if re.search(r"(يوم|ايام|أيام)", text):
        return now_utc + timedelta(days=n)
    if re.search(r"(اسبوع|أسبوع|اسابيع|أسابيع)", text):
        return now_utc + timedelta(weeks=n)
    if re.search(r"(شهر|اشهر|أشهر|شهور)", text):
        return now_utc + relativedelta(months=n)
    if re.search(r"(سنة|سنه|سنوات|سنين)", text):
        return now_utc + relativedelta(years=n)

    return None


async def wait_reply(ctx, timeout: int):
    """ينتظر رد من نفس الشخص في نفس القناة"""
    def check(m):
        return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id
    return await bot.wait_for("message", check=check, timeout=timeout)


# ───────────────────────── الأوامر ─────────────────────────

@bot.command(name="كبسولة")
async def create_capsule(ctx):
    if ctx.author.id in active_sessions:
        await ctx.send("عندك كبسولة قاعد تجهّزها حالياً 📝 كمّلها أو اكتب «الغاء».")
        return
    active_sessions.add(ctx.author.id)
    try:
        await run_capsule_flow(ctx)
    finally:
        active_sessions.discard(ctx.author.id)


async def run_capsule_flow(ctx):
    # 1) نوع الكبسولة
    await ctx.send(
        "🕰️ كبسولة زمنية جديدة!\n\n"
        "تبيها خاصة لك ولا للقروب؟\n"
        "اكتب: «خاص» (بتوصلك بالخاص DM)  أو  «قروب» (بتنفتح بالقناة)\n"
        "للإلغاء في أي وقت اكتب: «الغاء»"
    )
    try:
        msg = await wait_reply(ctx, 120)
    except asyncio.TimeoutError:
        await ctx.send("انتهى الوقت ⏳ ابدأ من جديد بكتابة @كبسولة")
        return

    if is_cancel(msg.content):
        await ctx.send("تم الإلغاء ✅")
        return

    choice = msg.content.strip()
    if "خاص" in choice:
        target = "dm"
    elif "قروب" in choice or "قناة" in choice:
        target = "channel"
    else:
        await ctx.send("ما فهمت الخيار 🤔 ابدأ من جديد بكتابة @كبسولة")
        return

    # 2) نص الرسالة
    await ctx.send("تمام 👍 الحين اكتب رسالتك للكبسولة 📝")
    try:
        msg = await wait_reply(ctx, 600)
    except asyncio.TimeoutError:
        await ctx.send("انتهى الوقت ⏳ ابدأ من جديد بكتابة @كبسولة")
        return

    if is_cancel(msg.content):
        await ctx.send("تم الإلغاء ✅")
        return

    capsule_text = msg.content.strip()
    if not capsule_text:
        await ctx.send("الرسالة فاضية 🤔 ابدأ من جديد بكتابة @كبسولة")
        return
    if len(capsule_text) > MAX_MSG_LEN:
        await ctx.send(f"الرسالة طويلة شوي ✂️ خلّها أقل من {MAX_MSG_LEN} حرف. ابدأ من جديد بكتابة @كبسولة")
        return

    # 3) وقت الفتح
    await ctx.send(
        "متى تنفتح الكبسولة؟ ⏳\n\n"
        "تقدر تكتب مدة، مثل:\n"
        "• 7 ايام\n"
        "• 3 اسابيع\n"
        "• 6 اشهر\n"
        "• 1 سنة\n\n"
        "أو تاريخ محدد بالصيغة: YYYY-MM-DD\n"
        "مثال: 2027-01-01"
    )
    try:
        msg = await wait_reply(ctx, 300)
    except asyncio.TimeoutError:
        await ctx.send("انتهى الوقت ⏳ ابدأ من جديد بكتابة @كبسولة")
        return

    if is_cancel(msg.content):
        await ctx.send("تم الإلغاء ✅")
        return

    open_at = parse_open_time(msg.content)
    if open_at == "past":
        await ctx.send("التاريخ هذا بالماضي 😅 ابدأ من جديد وحط وقت بالمستقبل (@كبسولة)")
        return
    if open_at is None:
        await ctx.send("ما قدرت أفهم الوقت 🤔 ابدأ من جديد بكتابة @كبسولة")
        return

    # 4) التأكيد
    target_label = "تجيك بالخاص (DM)" if target == "dm" else "تنفتح وتنشر بالقناة هنا"
    await ctx.send(
        "📋 مراجعة الكبسولة:\n"
        f"• النوع: {target_label}\n"
        f"• تنفتح في: {fmt_ksa(open_at)}\n\n"
        "أكّدها؟ اكتب «نعم» للحفظ أو «لا» للإلغاء"
    )
    try:
        msg = await wait_reply(ctx, 120)
    except asyncio.TimeoutError:
        await ctx.send("انتهى الوقت ⏳ ابدأ من جديد بكتابة @كبسولة")
        return

    if msg.content.strip() not in YES_WORDS:
        await ctx.send("تم الإلغاء ✅")
        return

    # 5) الحفظ
    now_utc = datetime.now(timezone.utc)
    conn = db_connect()
    cur = conn.execute(
        "INSERT INTO capsules "
        "(user_id, user_name, guild_id, channel_id, target, message, created_at, open_at, delivered) "
        "VALUES (?,?,?,?,?,?,?,?,0)",
        (
            ctx.author.id,
            str(ctx.author),
            ctx.guild.id if ctx.guild else None,
            ctx.channel.id,
            target,
            capsule_text,
            now_utc.isoformat(),
            open_at.isoformat(),
        ),
    )
    conn.commit()
    cid = cur.lastrowid
    conn.close()

    await ctx.send(
        f"✅ تم حفظ الكبسولة رقم #{cid}!\n"
        f"بتنفتح في: {fmt_ksa(open_at)}\n"
        "نشوفك وقتها 🕰️"
    )


@bot.command(name="كبسولاتي")
async def my_capsules(ctx):
    if ctx.author.id in active_sessions:
        return
    conn = db_connect()
    rows = conn.execute(
        "SELECT id, target, open_at FROM capsules WHERE user_id=? AND delivered=0 ORDER BY open_at ASC",
        (ctx.author.id,),
    ).fetchall()
    conn.close()

    if not rows:
        await ctx.send("ما عندك كبسولات منتظرة الفتح 📭")
        return

    lines = ["🗂️ كبسولاتك المنتظرة:\n"]
    for r in rows:
        t = "خاص" if r["target"] == "dm" else "قروب"
        opening = fmt_ksa(datetime.fromisoformat(r["open_at"]))
        lines.append(f"#{r['id']} • {t} • تنفتح: {opening}")
    lines.append("\nلإلغاء كبسولة اكتب: @الغاء [الرقم]")
    await ctx.send("\n".join(lines))


@bot.command(name="الغاء")
async def cancel_capsule(ctx, capsule_id: int = None):
    if ctx.author.id in active_sessions:
        return
    if capsule_id is None:
        await ctx.send("اكتب رقم الكبسولة، مثال: @الغاء 3")
        return
    conn = db_connect()
    row = conn.execute(
        "SELECT id FROM capsules WHERE id=? AND user_id=? AND delivered=0",
        (capsule_id, ctx.author.id),
    ).fetchone()
    if not row:
        conn.close()
        await ctx.send("ما لقيت كبسولة بهالرقم باسمك 🤔")
        return
    conn.execute("DELETE FROM capsules WHERE id=?", (capsule_id,))
    conn.commit()
    conn.close()
    await ctx.send(f"🗑️ تم إلغاء الكبسولة رقم #{capsule_id}")


@bot.command(name="مساعدة")
async def help_cmd(ctx):
    await ctx.send(
        "🕰️ بوت الكبسولة الزمنية — الأوامر:\n\n"
        "@كبسولة — تسوي كبسولة جديدة (خاصة لك أو للقروب) وتختار وقت فتحها\n"
        "@كبسولاتي — تشوف كبسولاتك المنتظرة الفتح\n"
        "@الغاء [الرقم] — تلغي كبسولة قبل ما تنفتح\n"
        "@مساعدة — تعرض هذي القائمة\n\n"
        "⏳ الوقت تحدده بمدة (مثل: 7 ايام / 3 اسابيع / 6 اشهر / 1 سنة) أو بتاريخ (YYYY-MM-DD)"
    )


# ───────────────────────── التسليم التلقائي ─────────────────────────

async def deliver_capsule(r) -> bool:
    created = fmt_ksa(datetime.fromisoformat(r["created_at"]))
    body = r["message"]
    try:
        if r["target"] == "dm":
            user = bot.get_user(r["user_id"]) or await bot.fetch_user(r["user_id"])
            if user is None:
                return False
            await user.send(
                "🕰️ كبسولتك الزمنية انفتحت!\n\n"
                f"كتبتها في: {created}\n"
                "وهذي رسالتك لنفسك:\n\n"
                f"«{body}»"
            )
            return True
        else:
            channel = bot.get_channel(r["channel_id"]) or await bot.fetch_channel(r["channel_id"])
            if channel is None:
                return False
            await channel.send(
                "🕰️ انفتحت كبسولة زمنية!\n\n"
                f"من: <@{r['user_id']}>\n"
                f"كُتبت في: {created}\n"
                "الرسالة:\n\n"
                f"«{body}»"
            )
            return True
    except discord.Forbidden:
        # الخاص مقفل أو ما عندنا صلاحية — نعتبرها سُلّمت عشان ما تعلق وتتكرر
        return True
    except Exception as e:
        print(f"خطأ بتسليم الكبسولة #{r['id']}: {e}")
        return False


@tasks.loop(seconds=60)
async def check_capsules():
    now_utc = datetime.now(timezone.utc)
    conn = db_connect()
    rows = conn.execute("SELECT * FROM capsules WHERE delivered=0").fetchall()
    due = [r for r in rows if datetime.fromisoformat(r["open_at"]) <= now_utc]
    for r in due:
        if await deliver_capsule(r):
            conn.execute("UPDATE capsules SET delivered=1 WHERE id=?", (r["id"],))
            conn.commit()
    conn.close()


@check_capsules.before_loop
async def before_check():
    await bot.wait_until_ready()


# ───────────────────────── التشغيل ─────────────────────────

@bot.event
async def on_ready():
    init_db()
    if not check_capsules.is_running():
        check_capsules.start()
    print(f"تم تسجيل الدخول كـ {bot.user} 🕰️")


if __name__ == "__main__":
    TOKEN = os.environ.get("DISCORD_TOKEN")
    if not TOKEN:
        raise RuntimeError("لازم تضيف متغير البيئة DISCORD_TOKEN في Railway")
    bot.run(TOKEN)
