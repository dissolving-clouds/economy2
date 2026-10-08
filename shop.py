"""Магазин ролей: роли сервера (навсегда) и роли игроков (на ограниченный срок)."""
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils import PagerView, add_role, calc_fee, remove_role, role_is_manageable, send


# ---------------------------------------------------------------- покупки

async def buy_server_role(bot, interaction: discord.Interaction, role_id: int):
    """Покупка роли из магазина администратора (выдаётся навсегда)."""
    db, guild, member = bot.db, interaction.guild, interaction.user

    async def say(text):
        await send(interaction, text)

    role = guild.get_role(role_id)
    item = await db.get_shop_role(role_id)
    if role is None or item is None:
        return await say("❌ Эта роль больше не продаётся.")
    if await db.get_item(member.id, role_id) or role in member.roles:
        return await say("✅ У тебя уже есть эта роль.")
    if not role_is_manageable(guild, role):
        return await say("❌ Я не могу выдать эту роль: она выше моей. Подними роль бота выше в настройках сервера.")

    price = item["price"]
    if not await db.try_remove(member.id, price):
        balance = await db.get_balance(member.id)
        return await say(f"❌ Не хватает монет. Цена: {await db.fmt(price)}, у тебя: {await db.fmt(balance)}.")
    if not await add_role(guild, member.id, role, "Покупка в магазине"):
        await db.add_balance(member.id, price)
        return await say("❌ Не удалось выдать роль, монеты возвращены.")

    await db.add_item(member.id, role_id, 0, 1)
    await db.add_transaction(int(time.time()), member.id, 0, price, 0, "shop_buy", f"роль {role.name}")
    await say(f"🎉 {member.mention} купил роль {role.mention} за {await db.fmt(price)}! Она лежит в /inventory.")


async def buy_player_role(bot, interaction: discord.Interaction, role_id: int):
    """Покупка роли, созданной другим игроком."""
    db, guild, member = bot.db, interaction.guild, interaction.user

    async def say(text):
        await send(interaction, text)

    role = guild.get_role(role_id)
    cr = await db.get_custom_role(role_id)
    now = int(time.time())
    if role is None or cr is None or not cr["listed"]:
        return await say("❌ Эта роль больше не продаётся.")
    if cr["creator_id"] == member.id:
        return await say("❌ Это твоя собственная роль.")
    if await db.get_item(member.id, role_id):
        return await say("✅ Эта роль уже есть в твоём инвентаре. Продлить её можно в /inventory.")
    if cr["expires_at"] - now < 86400:
        return await say("❌ Лот скоро закончится (автор не продлил роль), покупка сейчас недоступна.")

    price = cr["price"]
    if not await db.try_remove(member.id, price):
        balance = await db.get_balance(member.id)
        return await say(f"❌ Не хватает монет. Цена: {await db.fmt(price)}, у тебя: {await db.fmt(balance)}.")
    if not await add_role(guild, member.id, role, "Покупка роли игрока"):
        await db.add_balance(member.id, price)
        return await say("❌ Не удалось выдать роль, монеты возвращены.")

    days = await db.get_int("user_role_days")
    percent = await db.get_int("role_commission")
    fee = calc_fee(price, percent)
    await db.add_balance(cr["creator_id"], price - fee)
    await db.add_transaction(now, member.id, cr["creator_id"], price, fee, "role_buy", f"роль {role.name}")
    await db.add_item(member.id, role_id, now + days * 86400, 1)
    await say(
        f"🎉 {member.mention} купил роль {role.mention} на {days} дн. за {await db.fmt(price)}! "
        f"<@{cr['creator_id']}> получает **{await db.fmt(price - fee)}** за продажу (комиссия {percent}%). "
        f"Продлевать роль нужно в /inventory."
    )


# ---------------------------------------------------------------- интерфейс

class ShopSelect(discord.ui.Select):
    def __init__(self, bot, options):
        super().__init__(placeholder="Выбери роль для покупки…", options=options, row=0)
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()  # меню остаётся, результат придёт отдельным сообщением
        kind, role_id = self.values[0].split(":")
        if kind == "s":
            await buy_server_role(self.bot, interaction, int(role_id))
        else:
            await buy_player_role(self.bot, interaction, int(role_id))


class ShopView(PagerView):
    """Магазин со страницами. entries — список словарей:
    {kind: 's'|'p', role, price, text (строка для embed)}."""

    def __init__(self, bot, user: discord.abc.User, entries: list[dict]):
        super().__init__(owner_id=None, total=len(entries), page_size=config.PAGE_SIZE, row=1)
        self.bot = bot
        self.user = user
        self.entries = entries
        self.select: ShopSelect | None = None

    async def render(self) -> discord.Embed:
        self.update_pager()
        db = self.bot.db
        start = self.page * self.page_size
        chunk = self.entries[start:start + self.page_size]

        if self.select is not None:
            self.remove_item(self.select)
        options = [
            discord.SelectOption(
                label=e["role"].name[:100], value=f"{e['kind']}:{e['role'].id}",
                description=(f"{e['price']} · " + ("роль сервера" if e["kind"] == "s" else "роль игрока"))[:100],
            )
            for e in chunk
        ]
        self.select = ShopSelect(self.bot, options)
        self.add_item(self.select)

        parts = []
        server = [e["text"] for e in chunk if e["kind"] == "s"]
        player = [e["text"] for e in chunk if e["kind"] == "p"]
        if server:
            parts.append("**🏛 Роли сервера (навсегда)**\n" + "\n\n".join(server))
        if player:
            parts.append("**🎨 Роли игроков**\n" + "\n\n".join(player))
        embed = discord.Embed(title="🏪 Магазин ролей", description="\n\n".join(parts)[:4096],
                              color=discord.Color.blurple())
        embed.set_footer(text=f"Страница {self.page + 1}/{self.pages} · позиций: {self.total} · "
                              f"баланс {self.user.display_name}: {await db.get_balance(self.user.id)}")
        return embed


class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.expire_roles.start()

    async def cog_unload(self):
        self.expire_roles.cancel()

    # -------- /shop
    @app_commands.command(name="shop", description="Магазин ролей")
    @app_commands.guild_only()
    async def shop(self, interaction: discord.Interaction):
        db = self.bot.db
        guild = interaction.guild
        now = int(time.time())
        entries = []

        for item in await db.get_shop_roles():
            role = guild.get_role(item["role_id"])
            if role is None:
                continue
            text = f"{role.mention} — **{await db.fmt(item['price'])}**"
            if item["description"]:
                text += f"\n└ {item['description']}"
            entries.append({"kind": "s", "role": role, "price": item["price"], "text": text})

        days = await db.get_int("user_role_days")
        for cr in await db.get_listed_custom_roles(now):
            role = guild.get_role(cr["role_id"])
            if role is None:
                continue
            text = (f"{role.mention} — **{await db.fmt(cr['price'])}** на {days} дн.\n"
                    f"└ Автор: <@{cr['creator_id']}> · лот до <t:{cr['expires_at']}:D>")
            entries.append({"kind": "p", "role": role, "price": cr["price"], "text": text})

        if not entries:
            return await send(interaction, "🏪 В магазине пока пусто. Создай свою роль командой /role_create!")

        view = ShopView(self.bot, interaction.user, entries)
        embed = await view.render()
        await send(interaction, embed=embed, view=view)

    # -------- /role_create
    @app_commands.command(name="role_create", description="Создать свою роль и выставить её на продажу")
    @app_commands.describe(
        name="Название роли",
        price=f"Цена продажи (от {config.ROLE_MIN_PRICE}). Столько же стоит создание и продление",
        color="Цвет в формате #RRGGBB (необязательно)",
    )
    @app_commands.guild_only()
    async def role_create(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 32],
        price: app_commands.Range[int, config.ROLE_MIN_PRICE, config.ROLE_PRICE_CAP],
        color: str | None = None,
    ):
        db, guild, member = self.bot.db, interaction.guild, interaction.user
        name = name.strip()
        if not name:
            return await send(interaction, "❌ Название не может быть пустым.")

        limit = await db.get_int("user_role_limit")
        if await db.count_custom_roles_of(member.id) >= limit:
            return await send(interaction, f"❌ Можно создать не больше {limit} ролей.")

        colour = discord.Colour.default()
        if color:
            raw = color.strip()
            if re.fullmatch(r"[0-9a-fA-F]{6}", raw):
                raw = "#" + raw
            try:
                colour = discord.Colour.from_str(raw)
            except ValueError:
                return await send(interaction, "❌ Цвет нужно указать так: #ff66aa")

        if not await db.try_remove(member.id, price):
            return await send(interaction, f"❌ Не хватает монет. Создание роли стоит {await db.fmt(price)} (равно цене продажи).")

        await interaction.response.defer()
        try:
            role = await guild.create_role(name=name, colour=colour, reason=f"Роль игрока {member}")
        except discord.HTTPException:
            await db.add_balance(member.id, price)
            return await send(interaction, "❌ Не удалось создать роль (проверь права бота). Монеты возвращены.")

        days = await db.get_int("user_role_days")
        expires = int(time.time()) + days * 86400
        await db.add_custom_role(role.id, member.id, price, expires)
        await db.add_transaction(int(time.time()), member.id, 0, price, 0, "role_create", f"роль {role.name}")
        await db.add_item(member.id, role.id, expires, 1)
        await add_role(guild, member.id, role, "Создатель роли")

        embed = discord.Embed(title="🎨 Новая роль в магазине!", color=colour)
        embed.description = f"{member.mention} создал роль {role.mention}"
        embed.add_field(name="Цена", value=await db.fmt(price))
        embed.add_field(name="Действует до", value=f"<t:{expires}:D>")
        embed.set_footer(text=f"Продлевать роль нужно каждые {days} дн. в /inventory, иначе она удалится.")
        await send(interaction, embed=embed)

    # -------- Автоматическое удаление просроченных ролей
    @tasks.loop(minutes=5)
    async def expire_roles(self):
        try:
            await self._expire_roles()
        except Exception:
            logging.exception("Ошибка при удалении просроченных ролей")

    async def _expire_roles(self):
        guild = self.bot.main_guild()
        if guild is None:
            return
        db = self.bot.db
        now = int(time.time())

        # 1) роли игроков, которые автор не продлил — удаляем совсем
        for cr in await db.get_expired_custom_roles(now):
            role = guild.get_role(cr["role_id"])
            if role:
                try:
                    await role.delete(reason="Срок роли игрока истёк")
                except discord.HTTPException:
                    logging.warning("Не удалось удалить роль %s, попробую позже", cr["role_id"])
                    continue
            await db.delete_custom_role(cr["role_id"])

        # 2) роли, срок которых у конкретного владельца закончился
        for it in await db.get_expired_items(now):
            role = guild.get_role(it["role_id"])
            if not await remove_role(guild, it["user_id"], role, "Срок роли истёк"):
                continue
            await db.remove_item(it["user_id"], it["role_id"])

        # 3) роли, которые кто-то удалил вручную в Discord
        for cr in await db.all_custom_roles():
            if guild.get_role(cr["role_id"]) is None:
                await db.delete_custom_role(cr["role_id"])

    @expire_roles.before_loop
    async def before_expire(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Shop(bot))
