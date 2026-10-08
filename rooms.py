"""Любовные комнаты.

/marry — покупка (создаётся запись в БД и роль). Продлить может любой из пары, есть общая копилка. Голосовой канал создаётся, только когда кто-то
из пары заходит в голосовой канал «Войти». Когда в комнате никого не осталось — удаляется только канал;
роль и запись в БД остаются до конца срока или до /divorce.
"""
import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils import add_role, days_between, delete_later, notify_user, remove_role, send


def room_fund(room) -> int:
    return room["fund_owner"] + room["fund_partner"]


async def _spend_fund(db, room, amount: int):
    """Списывает amount из копилки пропорционально взносам."""
    fund = room_fund(room)
    if amount <= 0 or fund <= 0:
        return
    take_owner = amount * room["fund_owner"] // fund
    take_partner = amount - take_owner
    await db.set_room_fund(room["owner_id"], room["fund_owner"] - take_owner, room["fund_partner"] - take_partner)


async def extend_room(bot, user: discord.abc.User) -> tuple[bool, str]:
    """Продлевает комнату. Продлить может любой из пары: сначала тратится копилка,
    недостающее списывается с того, кто нажал кнопку."""
    db = bot.db
    room = await db.get_room_of(user.id)
    if not room:
        return False, "❌ У тебя нет любовной комнаты."
    price = await db.get_int("room_price")
    days = await db.get_int("room_days")
    from_fund = min(room_fund(room), price)
    rest = price - from_fund
    if rest > 0 and not await db.try_remove(user.id, rest):
        return False, (f"❌ Не хватает монет. Продление: {await db.fmt(price)} на {days} дн., "
                       f"в копилке {await db.fmt(room_fund(room))}, нужно доплатить {await db.fmt(rest)}.")
    await _spend_fund(db, room, from_fund)
    now = int(time.time())
    new_expiry = max(room["expires_at"], now) + days * 86400
    await db.set_room_expiry(room["owner_id"], new_expiry)
    if rest > 0:
        await db.add_transaction(now, user.id, 0, rest, 0, "room_renew", f"до {time.strftime('%d.%m.%Y', time.localtime(new_expiry))}")
    parts = []
    if from_fund:
        parts.append(f"из копилки {await db.fmt(from_fund)}")
    if rest:
        parts.append(f"с баланса {await db.fmt(rest)}")
    return True, f"✅ Любовная комната продлена до <t:{new_expiry}:D> ({', '.join(parts)})."


async def contribute_room(bot, user: discord.abc.User, amount: int) -> tuple[bool, str]:
    """Взнос в копилку комнаты (может любой из пары)."""
    db = bot.db
    room = await db.get_room_of(user.id)
    if not room:
        return False, "❌ У тебя нет любовной комнаты."
    if amount < 1:
        return False, "❌ Сумма должна быть больше нуля."
    if not await db.try_remove(user.id, amount):
        return False, f"❌ Не хватает монет. Твой баланс: {await db.fmt(await db.get_balance(user.id))}."
    await db.add_room_fund(room["owner_id"], room["owner_id"] == user.id, amount)
    await db.add_transaction(int(time.time()), user.id, 0, amount, 0, "room_fund")
    room = await db.get_room_of(user.id)
    price = await db.get_int("room_price")
    fund = room_fund(room)
    tail = "✅ Этого хватает на продление!" if fund >= price else f"До полной оплаты продления: {await db.fmt(price - fund)}."
    return True, f"💰 {user.mention} положил(а) в копилку комнаты **{await db.fmt(amount)}**. В копилке: {await db.fmt(fund)}. {tail}"


async def refund_fund(bot, room, reason: str):
    """Возвращает остаток копилки каждому его часть."""
    db = bot.db
    now = int(time.time())
    for uid, part in ((room["owner_id"], room["fund_owner"]), (room["partner_id"], room["fund_partner"])):
        if part > 0:
            await db.add_balance(uid, part)
            await db.add_transaction(now, 0, uid, part, 0, "room_refund", reason)
    await db.set_room_fund(room["owner_id"], 0, 0)


async def room_embed(bot, room) -> discord.Embed:
    """Карточка комнаты для /love_profile и панели администратора."""
    db = bot.db
    now = int(time.time())
    price = await db.get_int("room_price")
    embed = discord.Embed(title="💞 Любовная комната", color=discord.Color.magenta())
    embed.add_field(name="Пара", value=f"<@{room['owner_id']}> & <@{room['partner_id']}>")
    embed.add_field(name="Вместе", value=f"💕 {days_between(room['created_at'] or now, now)} дн.")
    embed.add_field(name="Действует до", value=f"<t:{room['expires_at']}:D> (<t:{room['expires_at']}:R>)", inline=False)
    fund = room_fund(room)
    embed.add_field(
        name="🐷 Копилка на продление",
        value=(f"{await db.fmt(fund)} из {await db.fmt(price)}\n"
               f"└ <@{room['owner_id']}>: {await db.fmt(room['fund_owner'])} · "
               f"<@{room['partner_id']}>: {await db.fmt(room['fund_partner'])}"),
        inline=False,
    )
    if room["channel_id"]:
        embed.add_field(name="Канал", value=f"<#{room['channel_id']}>", inline=False)
    else:
        embed.add_field(name="Канал", inline=False,
                        value=f"Сейчас не создан — зайдите в <#{config.LOVE_JOIN_CHANNEL_ID}>, и он появится.")
    return embed


class FundModal(discord.ui.Modal, title="Скинуться на продление комнаты"):
    amount = discord.ui.TextInput(label="Сколько монет положить в копилку", placeholder="например 1000", max_length=12)

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(self.amount.value.strip().replace(" ", ""))
        except ValueError:
            return await send(interaction, "❌ Нужно целое число.", ephemeral=True)
        ok, text = await contribute_room(self.bot, interaction.user, value)
        await send(interaction, text)


class RoomButtonsView(discord.ui.View):
    """Кнопки комнаты в /profile и /love_profile. Постоянные: работают и после перезапуска бота."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Продлить комнату", emoji="💞", style=discord.ButtonStyle.primary,
                       custom_id="profile:extend_room")
    async def extend(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, text = await extend_room(self.bot, interaction.user)
        await send(interaction, text)

    @discord.ui.button(label="Скинуться в копилку", emoji="🐷", style=discord.ButtonStyle.success,
                       custom_id="profile:room_fund")
    async def fund(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.bot.db.get_room_of(interaction.user.id):
            return await send(interaction, "❌ У тебя нет любовной комнаты.")
        await interaction.response.send_modal(FundModal(self.bot))


class RoomOfferView(discord.ui.View):
    """Предложение комнаты: партнёр должен согласиться или отказаться."""

    def __init__(self, cog, owner: discord.Member, partner: discord.Member):
        super().__init__(timeout=120)
        self.cog = cog
        self.owner = owner
        self.partner = partner
        self.message: discord.Message | None = None
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.partner.id:
            await send(interaction, "⛔ Это предложение адресовано не тебе.")
            return False
        return True

    async def on_timeout(self):
        if self.message and not self.done:
            embed = discord.Embed(description="⌛ Время вышло — предложение не принято.", color=discord.Color.greyple())
            try:
                await self.message.edit(content=None, embed=embed, view=None)
            except discord.HTTPException:
                pass
            delete_later(self.message)

    @discord.ui.button(label="Согласиться", emoji="💞", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return
        self.done = True
        self.stop()
        await interaction.response.defer()
        ok, text = await self.cog.create_room(interaction.guild, self.owner, self.partner)
        embed = discord.Embed(description=text, color=discord.Color.green() if ok else discord.Color.red())
        msg = await interaction.edit_original_response(content=None, embed=embed, view=None)
        delete_later(msg)

    @discord.ui.button(label="Отказаться", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        self.stop()
        embed = discord.Embed(
            description=f"💔 {self.partner.mention} отказался(лась) от комнаты с {self.owner.mention}.",
            color=discord.Color.greyple(),
        )
        await interaction.response.edit_message(content=None, embed=embed, view=None)
        delete_later(self.message)


class Rooms(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.locks: dict[int, asyncio.Lock] = {}  # owner_id -> замок, чтобы не создать канал дважды
        self.busy: set[int] = set()               # каналы, которые только что созданы

    async def cog_load(self):
        self.bot.add_view(RoomButtonsView(self.bot))  # кнопки в старых сообщениях работают после перезапуска
        self.cleanup.start()

    async def cog_unload(self):
        self.cleanup.cancel()

    # ---------- покупка ----------
    async def create_room(self, guild: discord.Guild, owner: discord.Member, partner: discord.Member) -> tuple[bool, str]:
        db = self.bot.db
        if await db.get_room_of(owner.id) or await db.get_room_of(partner.id):
            return False, "❌ У одного из вас уже есть любовная комната."

        price = await db.get_int("room_price")
        days = await db.get_int("room_days")
        if not await db.try_remove(owner.id, price):
            return False, f"❌ У {owner.mention} не хватает монет. Цена комнаты: {await db.fmt(price)}."

        role_id = await db.get_int("room_role_id")
        role = guild.get_role(role_id) if role_id else None
        roles_ok = True
        if role:
            first = await add_role(guild, owner.id, role, "Любовная комната")
            second = await add_role(guild, partner.id, role, "Любовная комната")
            roles_ok = first and second

        now = int(time.time())
        expires = now + days * 86400
        await db.add_room(owner.id, partner.id, expires, role.id if role else 0, now)
        await db.add_transaction(now, owner.id, 0, price, 0, "room_buy", f"с <@{partner.id}>")

        text = (
            f"💞 {owner.mention} и {partner.mention}, поздравляем — любовная комната куплена!\n"
            f"Цена: {await db.fmt(price)} · действует до <t:{expires}:D> (<t:{expires}:R>).\n"
            f"Чтобы попасть в неё, зайдите в голосовой канал <#{config.LOVE_JOIN_CHANNEL_ID}>."
        )
        if role and roles_ok:
            text += f"\nВам выдана роль {role.mention}."
        elif role:
            text += "\n⚠️ Роль выдать не удалось — администратору нужно проверить, что роль бота выше неё."
        return True, text

    # ---------- голосовой канал комнаты ----------
    def _lock(self, owner_id: int) -> asyncio.Lock:
        return self.locks.setdefault(owner_id, asyncio.Lock())

    async def _target_category(self, guild: discord.Guild, join_channel) -> discord.CategoryChannel | None:
        if join_channel is not None and join_channel.category is not None:
            return join_channel.category  # та же категория, где стоит «Войти»
        cat_id = await self.bot.db.get_int("room_category_id")
        found = guild.get_channel(cat_id) if cat_id else None
        return found if isinstance(found, discord.CategoryChannel) else None

    async def _get_or_create_channel(self, guild, room, join_channel):
        """Возвращает голосовой канал комнаты, создавая его при необходимости."""
        async with self._lock(room["owner_id"]):
            room = await self.bot.db.get_room_of(room["owner_id"])  # свежие данные
            if room is None:
                return None
            if room["channel_id"]:
                channel = guild.get_channel(room["channel_id"])
                if channel is not None:
                    return channel

            owner = guild.get_member(room["owner_id"])
            partner = guild.get_member(room["partner_id"])
            pair = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, stream=True)
            # Видят все, заходить могут только двое
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, connect=True, manage_channels=True, move_members=True
                ),
            }
            for m in (owner, partner):
                if m is not None:
                    overwrites[m] = pair
            names = " & ".join(m.display_name for m in (owner, partner) if m) or "Любовная комната"
            channel = await guild.create_voice_channel(
                name=f"💞 {names}"[:100],
                category=await self._target_category(guild, join_channel),
                overwrites=overwrites,
                reason="Вход в любовную комнату",
            )
            self.busy.add(channel.id)
            await self.bot.db.set_room_channel(room["owner_id"], channel.id)
            return channel

    async def finish_room(self, room, reason: str) -> bool:
        """Полностью завершает комнату: канал, роли пары и запись в БД. False — если не получилось."""
        guild = self.bot.main_guild()
        if room["channel_id"]:
            channel = self.bot.get_channel(room["channel_id"])
            if channel is not None:
                try:
                    await channel.delete(reason=reason)
                except discord.HTTPException:
                    return False
        if guild and room["role_id"]:
            role = guild.get_role(room["role_id"])
            for uid in (room["owner_id"], room["partner_id"]):
                await remove_role(guild, uid, role, reason)
        fresh = await self.bot.db.get_room_of(room["owner_id"])
        if fresh and fresh["owner_id"] == room["owner_id"]:
            await refund_fund(self.bot, fresh, "комната закрыта")
        await self.bot.db.delete_room(room["owner_id"])
        return True

    async def _notify(self, text: str):
        channel = self.bot.get_channel(config.CART_CHANNEL_ID)
        if channel:
            try:
                await channel.send(text, delete_after=config.MESSAGE_LIFETIME)
            except discord.HTTPException:
                pass

    # ---------- события голосовых каналов ----------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot or before.channel == after.channel:
            return
        try:
            # 1) вошёл в «Войти»
            if after.channel is not None and after.channel.id == config.LOVE_JOIN_CHANNEL_ID:
                await self._handle_join(member, after.channel)

            # 2) вышел из комнаты — если она опустела, удаляем только канал
            if before.channel is not None and before.channel.id != config.LOVE_JOIN_CHANNEL_ID:
                room = await self.bot.db.get_room_by_channel(before.channel.id)
                if room and not before.channel.members:
                    await self._drop_channel(room, before.channel)
        except Exception:
            logging.exception("Ошибка в обработке голосовых каналов комнат")

    async def _drop_channel(self, room, channel):
        self.busy.discard(channel.id)
        try:
            await channel.delete(reason="Из любовной комнаты вышли все")
        except discord.NotFound:
            pass
        except discord.HTTPException:
            return
        await self.bot.db.set_room_channel(room["owner_id"], 0)

    async def _handle_join(self, member: discord.Member, join_channel: discord.VoiceChannel):
        guild = member.guild
        room = await self.bot.db.get_room_of(member.id)
        if not room or room["expires_at"] <= int(time.time()):
            try:
                await member.move_to(None, reason="Нет любовной комнаты")  # отключаем от «Войти»
            except discord.HTTPException:
                pass
            await self._notify(f"💔 {member.mention}, у тебя нет любовной комнаты. Купить её можно командой /marry.")
            return

        try:
            channel = await self._get_or_create_channel(guild, room, join_channel)
        except discord.HTTPException:
            await self._notify("❌ Не удалось создать комнату. Администратору нужно проверить права бота "
                               "(«Управление каналами» и «Перемещение участников»).")
            return
        if channel is None:
            return
        try:
            await member.move_to(channel, reason="Вход в любовную комнату")
        except discord.HTTPException:
            # участник успел выйти — если канал пустой, убираем его
            if not channel.members:
                fresh = await self.bot.db.get_room_of(member.id)
                if fresh:
                    await self._drop_channel(fresh, channel)
        finally:
            self.busy.discard(channel.id)

    # ---------- автоматическая очистка ----------
    @tasks.loop(minutes=5)
    async def cleanup(self):
        try:
            now = int(time.time())
            for room in await self.bot.db.all_rooms():
                if room["expires_at"] <= now:
                    if await self._try_autorenew(room):
                        continue
                    if await self.finish_room(room, "Срок любовной комнаты истёк"):
                        for uid in (room["owner_id"], room["partner_id"]):
                            await notify_user(self.bot, uid, "💔 Срок вашей любовной комнаты закончился, и она удалена. "
                                                             "Остаток копилки (если был) возвращён на баланс.",
                                              fallback=False)
                    continue
                if room["channel_id"] and room["channel_id"] not in self.busy:
                    channel = self.bot.get_channel(room["channel_id"])
                    if channel is None:
                        await self.bot.db.set_room_channel(room["owner_id"], 0)
                    elif not channel.members:
                        await self._drop_channel(room, channel)
        except Exception:
            logging.exception("Ошибка при очистке комнат")

    async def _try_autorenew(self, room) -> bool:
        """Продлевает просроченную комнату из копилки, если это включено и денег хватает."""
        db = self.bot.db
        if not await db.get_int("room_autorenew"):
            return False
        price = await db.get_int("room_price")
        if room_fund(room) < price:
            return False
        days = await db.get_int("room_days")
        await _spend_fund(db, room, price)
        new_expiry = max(room["expires_at"], int(time.time())) + days * 86400
        await db.set_room_expiry(room["owner_id"], new_expiry)
        for uid in (room["owner_id"], room["partner_id"]):
            await notify_user(self.bot, uid, f"💞 Ваша любовная комната автоматически продлена из копилки "
                                             f"до <t:{new_expiry}:D>.", fallback=False)
        return True

    @cleanup.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    # ---------- команды ----------
    @app_commands.command(name="marry", description="Предложить партнёру любовную комнату")
    @app_commands.describe(partner="С кем будет комната")
    @app_commands.guild_only()
    async def marry(self, interaction: discord.Interaction, partner: discord.Member):
        db = self.bot.db
        owner = interaction.user

        if partner.bot or partner.id == owner.id:
            return await send(interaction, "❌ Выбери другого человека (не себя и не бота).")
        if await db.get_room_of(owner.id) or await db.get_room_of(partner.id):
            return await send(interaction, "❌ У тебя или у партнёра уже есть любовная комната.")

        price = await db.get_int("room_price")
        days = await db.get_int("room_days")
        if await db.get_balance(owner.id) < price:
            return await send(interaction, f"❌ Не хватает монет. Цена комнаты: {await db.fmt(price)} на {days} дн.")

        view = RoomOfferView(self, owner, partner)
        embed = discord.Embed(
            title="💞 Предложение любовной комнаты",
            description=(
                f"{owner.mention} предлагает {partner.mention} общую комнату на {days} дн.\n"
                f"Оплачивает {owner.mention}: **{await db.fmt(price)}**.\n\n"
                f"{partner.mention}, ты согласен(на)? У тебя есть 2 минуты."
            ),
            color=discord.Color.magenta(),
        )
        await interaction.response.send_message(content=partner.mention, embed=embed, view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="love_profile", description="Информация о твоей любовной комнате")
    async def love_profile(self, interaction: discord.Interaction):
        room = await self.bot.db.get_room_of(interaction.user.id)
        if not room:
            return await send(interaction, "У тебя нет любовной комнаты. Купить её можно командой /marry.")
        embed = await room_embed(self.bot, room)
        embed.set_footer(text="Продлить может любой из пары: сначала тратится копилка, остальное — с баланса.")
        msg = await send(interaction, embed=embed, view=RoomButtonsView(self.bot))
        delete_later(msg, config.MESSAGE_LIFETIME * 3)  # с кнопками — висит подольше

    @app_commands.command(name="divorce", description="Удалить любовную комнату (копилка возвращается, оплата — нет)")
    async def divorce(self, interaction: discord.Interaction):
        room = await self.bot.db.get_room_of(interaction.user.id)
        if not room:
            return await send(interaction, "❌ У тебя нет любовной комнаты.")
        await interaction.response.defer()
        partner_id = room["partner_id"] if room["owner_id"] == interaction.user.id else room["owner_id"]
        if await self.finish_room(room, f"Расставание ({interaction.user})"):
            await send(interaction, f"💔 {interaction.user.mention} и <@{partner_id}> расстались. Комната удалена, "
                                    f"остаток копилки возвращён каждому его часть.")
        else:
            await send(interaction, "❌ Не удалось удалить канал комнаты.")


async def setup(bot):
    await bot.add_cog(Rooms(bot))
