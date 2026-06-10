# -*- coding: utf-8 -*-
"""
بوت الكبسولة الزمنية 🕰️ — زاويتنا
نسخة الأوامر الخطّية (Slash) مع رسائل خاصة:
كل خطوات إنشاء الكبسولة تطلع لك وحدك (ephemeral) — محد يشوف وش تكتب.
الكبسولة القروب تنفتح وتننشر بالقناة في وقت الفتح فقط.
"""

import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands, ButtonStyle, SelectOption, TextStyle
from discord.ext import commands, tasks
from discord.ui import View, Select, Modal, TextInput
from dateutil.relativedelta import relativedelta

# ───────────────────────── الإعدادات ─────────────────────────

KSA = timezone(timedelta(hours=3))                    # توقيت السعودية (UTC+3)
DB_PATH = os.environ.get("DB_PATH", "capsules.db")
GUILD_ID = os.environ.get("GUILD_ID")                 # اختياري: لمزامنة الأوامر فوراً بسيرفرك
MAX_MSG_LEN = 1500

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

DURATION_OPTIONS = [
    ("ساعة",     ("hours", 1)),
    ("يوم",      ("days", 1)),
    ("3 أيام",   ("days", 3)),
    ("أسبوع",    ("weeks", 1)),
    ("أسبوعين",  ("weeks", 2)),
    ("شهر",      ("months", 1)),
    ("3 أشهر",   ("months", 3)),
    ("6 أشهر",   ("months", 6)),
    ("سنة",      ("years", 1)),
    ("سنتين",    ("years", 2)),
]

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
            target      TEXT    NOT NULL,
            message     TEXT    NOT NULL,
            created_at  TEXT    NOT NULL,
            open_at     TEXT    NOT NULL,
            delivered   INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            guild_id            INTEGER PRIMARY KEY,
            capsule_channel_id  INTEGER
        )
        """
    )
    conn.commit()
    conn.close()


def set_capsule_channel(guild_id: int, channel_id: int):
    conn = db_connect()
    conn.execute(
        "INSERT INTO settings (guild_id, capsule_channel_id) VALUES (?,?) "
        "ON CONFLICT(guild_id) DO UPDATE SET capsule_channel_id=excluded.capsule_channel_id",
        (guild_id, channel_id),
    )
    conn.commit()
    conn.close()


def get_capsule_channel(guild_id):
    if guild_id is None:
        return None
    conn = db_connect()
    row = conn.execute(
        "SELECT capsule_channel_id FROM settings WHERE guild_id=?", (guild_id,)
    ).fetchone()
    conn.close()
    return row["capsule_channel_id"] if row else None


def save_capsule(user, guild_id, channel_id, target, text, open_at) -> int:
    now_utc = datetime.now(timezone.utc)
    conn = db_connect()
    cur = conn.execute(
        "INSERT INTO capsules "
        "(user_id, user_name, guild_id, channel_id, target, message, created_at, open_at, delivered) "
        "VALUES (?,?,?,?,?,?,?,?,0)",
        (user.id, str(user), guild_id, channel_id, target,
         text, now_utc.isoformat(), open_at.isoformat()),
    )
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    return cid


# ───────────────────────── أدوات مساعدة ─────────────────────────

ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"

def normalize_digits(text: str) -> str:
    table = {ord(a): str(i) for i, a in enumerate(ARABIC_DIGITS)}
    return text.translate(table)


def fmt_ksa(dt_utc: datetime) -> str:
    return dt_utc.astimezone(KSA).strftime("%Y-%m-%d %H:%M") + " (توقيت السعودية)"


def parse_date(text: str):
    text = normalize_digits(text.strip())
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if not m:
        return None
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        local = datetime(y, mo, d, 9, 0, 0, tzinfo=KSA)
        open_utc = local.astimezone(timezone.utc)
        return "past" if open_utc <= datetime.now(timezone.utc) else open_utc
    except ValueError:
        return None


def duration_to_open(unit: str, n: int) -> datetime:
    now_utc = datetime.now(timezone.utc)
    if unit == "hours":
        return now_utc + timedelta(hours=n)
    if unit == "days":
        return now_utc + timedelta(days=n)
    if unit == "weeks":
        return now_utc + timedelta(weeks=n)
    if unit == "months":
        return now_utc + relativedelta(months=n)
    if unit == "years":
        return now_utc + relativedelta(years=n)
    return now_utc


# ───────────────────────── الواجهات (أزرار/قوائم/نافذة) ─────────────────────────

class CapsuleModal(Modal):
    def __init__(self, parent, mode, unit=None, n=None):
        super().__init__(title="✍️ كبسولتك الزمنية")
        self.parent = parent
        self.mode = mode
        self.unit = unit
        self.n = n

        if mode == "date":
            self.date_input = TextInput(
                label="التاريخ (سنة-شهر-يوم)",
                placeholder="مثال: 2027-01-01",
                required=True,
                max_length=10,
            )
            self.add_item(self.date_input)

        self.msg_input = TextInput(
            label="نص الكبسولة (سري — محد بيشوفه)",
            placeholder="اكتب رسالتك هنا...",
            style=TextStyle.paragraph,
            required=True,
            max_length=MAX_MSG_LEN,
        )
        self.add_item(self.msg_input)

    async def on_submit(self, interaction: discord.Interaction):
        if self.mode == "duration":
            open_at = duration_to_open(self.unit, self.n)
        else:
            open_at = parse_date(self.date_input.value)
            if open_at == "past":
                await interaction.response.send_message(
                    "التاريخ هذا بالماضي 😅 استخدم /كبسولة من جديد", ephemeral=True)
                return
            if open_at is None:
                await interaction.response.send_message(
                    "صيغة التاريخ غلط 🤔 لازم YYYY-MM-DD. استخدم /كبسولة من جديد", ephemeral=True)
                return

        text = self.msg_input.value.strip()
        if not text:
            await interaction.response.send_message("الرسالة فاضية 🤔", ephemeral=True)
            return

        p = self.parent
        cid = save_capsule(interaction.user, p.guild_id, p.channel_id, p.target, text, open_at)

        if p.target == "dm":
            target_label = "تجيك بالخاص (DM)"
        else:
            ch_id = get_capsule_channel(p.guild_id) or p.channel_id
            ch = bot.get_channel(ch_id)
            ch_name = f"#{ch.name}" if ch and hasattr(ch, "name") else "القناة المحددة"
            target_label = f"تنفتح في {ch_name}"

        await interaction.response.edit_message(
            content=(
                f"✅ تم حفظ الكبسولة رقم #{cid}! (محد شاف محتواها)\n"
                f"• النوع: {target_label}\n"
                f"• تنفتح في: {fmt_ksa(open_at)}\n"
                "نشوفك وقتها 🕰️"
            ),
            view=None,
        )
        p.stop()


class TimeSelect(Select):
    def __init__(self, parent):
        self.parent = parent
        options = [SelectOption(label=lbl, value=str(i))
                   for i, (lbl, _) in enumerate(DURATION_OPTIONS)]
        options.append(SelectOption(label="📅 تاريخ محدد", value="custom"))
        super().__init__(placeholder="اختر مدة أو تاريخ الفتح...",
                         options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        val = self.values[0]
        if val == "custom":
            await interaction.response.send_modal(CapsuleModal(self.parent, mode="date"))
        else:
            unit, n = DURATION_OPTIONS[int(val)][1]
            await interaction.response.send_modal(
                CapsuleModal(self.parent, mode="duration", unit=unit, n=n))


class TimeView(View):
    def __init__(self, author_id, target, channel_id, guild_id):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.target = target
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.add_item(TimeSelect(self))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("هذي مو كبسولتك 🙂", ephemeral=True)
            return False
        return True


class TypeView(View):
    def __init__(self, author_id):
        super().__init__(timeout=180)
        self.author_id = author_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("هذي مو كبسولتك 🙂", ephemeral=True)
            return False
        return True

    async def _go_time(self, interaction, target):
        view = TimeView(
            self.author_id, target,
            interaction.channel.id,
            interaction.guild.id if interaction.guild else None,
        )
        await interaction.response.edit_message(
            content="⏳ اختر متى تنفتح الكبسولة:", view=view)
        self.stop()

    @discord.ui.button(label="📩 خاص (لي)", style=ButtonStyle.primary)
    async def private_btn(self, interaction, button):
        await self._go_time(interaction, "dm")

    @discord.ui.button(label="📢 قروب (للقناة)", style=ButtonStyle.success)
    async def group_btn(self, interaction, button):
        await self._go_time(interaction, "channel")

    @discord.ui.button(label="✖️ إلغاء", style=ButtonStyle.secondary)
    async def cancel_btn(self, interaction, button):
        await interaction.response.edit_message(content="تم الإلغاء ✅", view=None)
        self.stop()


class CancelSelect(Select):
    def __init__(self, author_id, rows):
        self.author_id = author_id
        options = []
        for r in rows[:25]:
            t = "خاص" if r["target"] == "dm" else "قروب"
            opening = datetime.fromisoformat(r["open_at"]).astimezone(KSA).strftime("%Y-%m-%d %H:%M")
            options.append(SelectOption(
                label=f"#{r['id']} • {t}",
                description=f"تنفتح: {opening}",
                value=str(r["id"]),
            ))
        super().__init__(placeholder="اختر كبسولة لإلغائها...",
                         options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        cid = int(self.values[0])
        conn = db_connect()
        row = conn.execute(
            "SELECT id FROM capsules WHERE id=? AND user_id=? AND delivered=0",
            (cid, self.author_id)).fetchone()
        if not row:
            conn.close()
            await interaction.response.send_message("ما لقيت الكبسولة 🤔", ephemeral=True)
            return
        conn.execute("DELETE FROM capsules WHERE id=?", (cid,))
        conn.commit()
        conn.close()
        await interaction.response.edit_message(
            content=f"🗑️ تم إلغاء الكبسولة رقم #{cid}", view=None)


class MyCapsulesView(View):
    def __init__(self, author_id, rows):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.add_item(CancelSelect(author_id, rows))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("هذي مو كبسولاتك 🙂", ephemeral=True)
            return False
        return True


# ───────────────────────── أوامر Slash ─────────────────────────

@bot.tree.command(name="كبسولة", description="سوّي كبسولة زمنية جديدة (خاصة لك أو للقروب)")
@app_commands.guild_only()
async def capsule_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🕰️ كبسولة زمنية جديدة!\nاختر نوعها:",
        view=TypeView(interaction.user.id),
        ephemeral=True,
    )


@bot.tree.command(name="كبسولاتي", description="اعرض كبسولاتك المنتظرة وألغِ أي وحدة")
@app_commands.guild_only()
async def mycapsules_cmd(interaction: discord.Interaction):
    conn = db_connect()
    rows = conn.execute(
        "SELECT id, target, open_at FROM capsules WHERE user_id=? AND delivered=0 ORDER BY open_at ASC",
        (interaction.user.id,),
    ).fetchall()
    conn.close()
    if not rows:
        await interaction.response.send_message("ما عندك كبسولات منتظرة الفتح 📭", ephemeral=True)
        return
    lines = ["🗂️ كبسولاتك المنتظرة:\n"]
    for r in rows:
        t = "خاص" if r["target"] == "dm" else "قروب"
        lines.append(f"#{r['id']} • {t} • تنفتح: {fmt_ksa(datetime.fromisoformat(r['open_at']))}")
    await interaction.response.send_message(
        "\n".join(lines), view=MyCapsulesView(interaction.user.id, rows), ephemeral=True)


@bot.tree.command(name="قناة", description="(للمشرفين) حدد هذي القناة كمكان فتح الكبسولات القروب")
@app_commands.guild_only()
async def channel_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("هذا الأمر للمشرفين فقط 🔒", ephemeral=True)
        return
    set_capsule_channel(interaction.guild.id, interaction.channel.id)
    await interaction.response.send_message(
        f"✅ تمام! الكبسولات القروب بتنفتح في #{interaction.channel.name}.", ephemeral=True)


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
            ch_id = get_capsule_channel(r["guild_id"]) or r["channel_id"]
            channel = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
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

_synced = False

@bot.event
async def on_ready():
    global _synced
    init_db()
    if not _synced:
        try:
            if GUILD_ID:
                g = discord.Object(id=int(GUILD_ID))
                bot.tree.copy_global_to(guild=g)
                cmds = await bot.tree.sync(guild=g)
            else:
                cmds = await bot.tree.sync()
            print(f"تم مزامنة {len(cmds)} أوامر")
            _synced = True
        except Exception as e:
            print(f"خطأ بالمزامنة: {e}")
    if not check_capsules.is_running():
        check_capsules.start()
    print(f"تم تسجيل الدخول كـ {bot.user} 🕰️")


if __name__ == "__main__":
    TOKEN = os.environ.get("DISCORD_TOKEN")
    if not TOKEN:
        raise RuntimeError("لازم تضيف متغير البيئة DISCORD_TOKEN في Railway")
    bot.run(TOKEN)
