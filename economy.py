"""Ежедневная награда, баланс, переводы (с комиссией), топы."""
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import PagerView, calc_fee, fmt_num, fmt_voice, send

# Подписи операций в /history: (когда участник платит, когда участник получает)
TX_LABELS = {
    "pay": ("Перевод", "Перевод"),
    "role_buy": ("Покупка роли", "Продажа роли"),
    "role_renew": ("Продление роли", "Продление твоей роли"),
    "role_create": ("Создание роли", "Создание роли"),
    "role_lot_renew": ("Продление своей роли", "Продление своей роли"),
    "shop_buy": ("Покупка в магазине", "Покупка в магазине"),
    "daily": ("Ежедневная награда", "Ежедневная награда"),
    "casino": ("Проигрыш в казино", "Выигрыш в казино"),
    "duel": ("Проигрыш в дуэли", "Победа в дуэли"),
    "room_buy": ("Покупка любовной комнаты", "Покупка любовной комнаты"),
    "room_renew": ("Продление любовной комнаты", "Продление любовной комнаты"),
    "room_fund": ("Взнос в копилку комнаты", "Взнос в копилку комнаты"),
    "room_refund": ("Возврат из копилки", "Возврат из копилки комнаты"),
    "admin": ("Списано администратором", "Выдано администратором"),
}


async def format_tx(db, row, member_id: int) -> str:
    """Одна строка истории с точки зрения участника member_id."""
    outgoing = row["from_id"] == member_id
    label = TX_LABELS.get(row["kind"], ("Платёж", "Поступление"))[0 if outgoing else 1]
    other = row["to_id"] if outgoing else row["from_id"]
    who = ""
    if other and other != member_id:
        who = f" ➜ <@{other}>" if outgoing else f" ⬅ <@{other}>"
    extra = f" · {row['note']}" if row["note"] else ""
    if outgoing:
        return f"📤 <t:{row['ts']}:d> {label}{who}: **−{await db.fmt(row['amount'])}**{extra}"
    fee = f" (комиссия {await db.fmt(row['fee'])})" if row["fee"] else ""
    return f"📥 <t:{row['ts']}:d> {label}{who}: **+{await db.fmt(row['amount'] - row['fee'])}**{fee}{extra}"


class HistoryView(PagerView):
    def __init__(self, bot, viewer_id: int, member: discord.abc.User, total: int):
        super().__init__(owner_id=viewer_id, total=total, page_size=config.PAGE_SIZE + 2, row=0)
        self.bot = bot
        self.member = member

    async def render(self) -> discord.Embed:
        self.update_pager()
        db = self.bot.db
        rows = await db.get_transactions(self.member.id, self.page_size, self.page * self.page_size)
        lines = [await format_tx(db, r, self.member.id) for r in rows]
        embed = discord.Embed(
            title=f"📜 История операций — {self.member.display_name}",
            description="\n".join(lines) or "Пусто.", color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Страница {self.page + 1}/{self.pages} · всего операций: {self.total}")
        return embed


class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="daily", description="Забрать ежедневную награду")
    async def daily(self, interaction: discord.Interaction):
        db = self.bot.db
        user = await db.get_user(interaction.user.id)
        now = int(time.time())
        cooldown = await db.get_int("daily_cooldown_hours") * 3600
        last = user["last_daily"]

        if now - last < cooldown:
            ready_at = last + cooldown
            return await send(interaction, f"⏳ {interaction.user.mention}, награда уже получена. "
                                           f"Следующая будет доступна <t:{ready_at}:R>.")

        if last != 0 and now - last <= cooldown + 86400:
            streak = user["streak"] + 1
        else:
            streak = 1

        base = await db.get_int("daily_amount")
        bonus = min(streak - 1, 6) * await db.get_int("daily_streak_bonus")
        reward = base + bonus

        await db.claim_daily(interaction.user.id, now, streak, reward)
        await db.add_transaction(now, 0, interaction.user.id, reward, 0, "daily", f"серия {streak} дн.")
        balance = await db.get_balance(interaction.user.id)

        embed = discord.Embed(title=f"🎁 Ежедневная награда — {interaction.user.display_name}", color=discord.Color.green())
        embed.add_field(name="Получено", value=await db.fmt(reward))
        embed.add_field(name="Серия", value=f"🔥 {streak} дн.")
        embed.add_field(name="Баланс", value=await db.fmt(balance))
        if bonus:
            embed.set_footer(text=f"В награду входит бонус за серию: +{bonus}")
        await send(interaction, embed=embed)

    @app_commands.command(name="balance", description="Показать баланс")
    @app_commands.describe(member="Чей баланс показать (по умолчанию твой)")
    async def balance(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        balance = await self.bot.db.get_balance(member.id)
        embed = discord.Embed(
            description=f"💰 Баланс {member.mention}: **{await self.bot.db.fmt(balance)}**",
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed, delete_after=20)  # удаляется через 20 секунд

    @app_commands.command(name="pay", description="Перевести монеты другому участнику (с комиссией)")
    @app_commands.describe(member="Кому", amount="Сколько списать с тебя")
    async def pay(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1]):
        db = self.bot.db
        if member.bot or member.id == interaction.user.id:
            return await send(interaction, "❌ Нельзя переводить себе или боту.")

        percent = await db.get_int("transfer_commission")
        fee = calc_fee(amount, percent)
        received = amount - fee
        if received < 1:
            return await send(interaction, "❌ Слишком маленькая сумма: после комиссии ничего не останется.")

        if not await db.try_remove(interaction.user.id, amount):
            return await send(interaction, "❌ Недостаточно монет.")
        await db.add_balance(member.id, received)
        await db.add_transaction(int(time.time()), interaction.user.id, member.id, amount, fee, "pay")

        embed = discord.Embed(title="💸 Перевод монет", color=discord.Color.green())
        embed.description = f"{interaction.user.mention} ➜ {member.mention}"
        embed.add_field(name="Списано", value=await db.fmt(amount))
        embed.add_field(name=f"Комиссия {percent}%", value=await db.fmt(fee))
        embed.add_field(name="Получено", value=await db.fmt(received))
        await send(
            interaction,
            content=f"{member.mention}, {interaction.user.mention} перевёл(а) вам **{await db.fmt(received)}**",
            embed=embed,
            keep=True,
        )

    @app_commands.command(name="history", description="История всех операций с монетами")
    @app_commands.describe(member="Чью историю показать (по умолчанию твою)")
    async def history(self, interaction: discord.Interaction, member: discord.Member | None = None):
        db = self.bot.db
        member = member or interaction.user
        total = await db.count_transactions(member.id)
        if not total:
            return await send(interaction, f"📜 У {member.mention} пока нет операций.")
        view = HistoryView(self.bot, interaction.user.id, member, total)
        embed = await view.render()
        if view.pages > 1:
            await send(interaction, embed=embed, view=view)  # меню исчезнет через 20 секунд без нажатий
        else:
            await send(interaction, embed=embed)

    @app_commands.command(name="top", description="Рейтинги участников")
    @app_commands.describe(category="По какому показателю (по умолчанию — монеты)")
    @app_commands.choices(category=[
        app_commands.Choice(name="💰 Монеты", value="coins"),
        app_commands.Choice(name="💬 Сообщения", value="messages"),
        app_commands.Choice(name="🎙 Голосовые каналы", value="voice"),
    ])
    async def top(self, interaction: discord.Interaction, category: app_commands.Choice[str] | None = None):
        db = self.bot.db
        kind = category.value if category else "coins"

        if kind == "coins":
            title = "🏆 Топ по монетам"
            rows = [(r["user_id"], await db.fmt(r["balance"])) for r in await db.top(10)]
        elif kind == "messages":
            title = "🏆 Топ по сообщениям"
            rows = [(r["user_id"], fmt_num(r["value"])) for r in await db.top_stat("messages", 10)]
        else:
            title = "🏆 Топ по времени в голосовых"
            rows = [(r["user_id"], fmt_voice(r["value"])) for r in await db.top_stat("voice_seconds", 10)]

        if not rows:
            return await send(interaction, "Пока здесь пусто.")

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (uid, value) in enumerate(rows):
            place = medals[i] if i < 3 else f"**{i + 1}.**"
            lines.append(f"{place} <@{uid}> — {value}")
        embed = discord.Embed(title=title, description="\n".join(lines), color=discord.Color.gold())
        await send(interaction, embed=embed)


async def setup(bot):
    await bot.add_cog(Economy(bot))
