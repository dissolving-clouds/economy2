"""Инвентарь ролей: надеть, убрать в хранилище, продлить, подарить, удалить."""
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import PagerView, TempView, add_role, calc_fee, delete_later, remove_role, send


# ---------------------------------------------------------------- логика

async def renew_item(bot, user, role_id: int) -> tuple[bool, str]:
    """Продлевает роль. Автор продлевает свой лот, покупатель — свою покупку."""
    db = bot.db
    now = int(time.time())
    days = await db.get_int("user_role_days")

    item = await db.get_item(user.id, role_id)
    if not item or (item["expires_at"] and item["expires_at"] <= now):
        return False, "❌ Этой роли нет в твоём инвентаре."
    if item["expires_at"] == 0:
        return False, "♾ Эта роль выдана навсегда — продлевать не нужно."
    cr = await db.get_custom_role(role_id)
    if cr is None:
        return False, "❌ Эта роль больше не существует."

    price = cr["price"]

    if cr["creator_id"] == user.id:  # продление лота автором
        if not await db.try_remove(user.id, price):
            return False, f"❌ Не хватает монет. Продление стоит {await db.fmt(price)}."
        new_exp = max(cr["expires_at"], now) + days * 86400
        await db.update_custom_expiry(role_id, new_exp)
        await db.set_item_expiry(user.id, role_id, new_exp)
        await db.add_transaction(now, user.id, 0, price, 0, "role_lot_renew", f"роль <@&{role_id}>")
        return True, f"✅ Роль продлена до <t:{new_exp}:D> за {await db.fmt(price)}."

    # продление покупателем
    if cr["expires_at"] - now < 86400:
        return False, "❌ Автор скоро не продлит эту роль, продление недоступно."
    if not await db.try_remove(user.id, price):
        return False, f"❌ Не хватает монет. Продление стоит {await db.fmt(price)}."
    percent = await db.get_int("role_commission")
    fee = calc_fee(price, percent)
    await db.add_balance(cr["creator_id"], price - fee)
    await db.add_transaction(now, user.id, cr["creator_id"], price, fee, "role_renew", f"роль <@&{role_id}>")
    new_exp = max(item["expires_at"], now) + days * 86400
    await db.set_item_expiry(user.id, role_id, new_exp)
    return True, f"✅ Роль продлена до <t:{new_exp}:D> за {await db.fmt(price)}. Автор получил {await db.fmt(price - fee)}."


# ---------------------------------------------------------------- подарок

class GiftOfferView(discord.ui.View):
    """Предложение подарка: публичное сообщение с кнопками для получателя."""

    def __init__(self, bot, giver, receiver, role_id: int):
        super().__init__(timeout=180)
        self.bot = bot
        self.giver = giver
        self.receiver = receiver
        self.role_id = role_id
        self.message: discord.Message | None = None
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.receiver.id:
            await send(interaction, "⛔ Этот подарок адресован не тебе.")
            return False
        return True

    async def on_timeout(self):
        if self.message and not self.done:
            embed = discord.Embed(description="⌛ Время вышло — подарок не принят.", color=discord.Color.greyple())
            try:
                await self.message.edit(content=None, embed=embed, view=None)
            except discord.HTTPException:
                pass
            delete_later(self.message)

    async def transfer(self, guild: discord.Guild) -> tuple[bool, str]:
        """Подарить можно только роль, которую создал сам даритель. Он сохраняет её у себя,
        а получатель получает копию на оставшийся срок роли."""
        db = self.bot.db
        now = int(time.time())
        cr = await db.get_custom_role(self.role_id)
        role = guild.get_role(self.role_id)
        if cr is None or role is None or cr["expires_at"] <= now:
            return False, "❌ Подарок больше недоступен: роль удалена или её срок истёк."
        if cr["creator_id"] != self.giver.id:
            return False, "❌ Дарить можно только роли, которые ты сам создал."
        if role.permissions.administrator:
            return False, "❌ Роли с правами администратора дарить нельзя."
        if await db.get_item(self.receiver.id, self.role_id):
            return False, "❌ У получателя уже есть эта роль."

        await db.add_item(self.receiver.id, self.role_id, cr["expires_at"], 1)
        await add_role(guild, self.receiver.id, role, "Получил роль в подарок")
        return True, (f"🎁 {self.receiver.mention} принял подарок: роль {role.mention} от {self.giver.mention}! "
                      f"Роль действует до <t:{cr['expires_at']}:D>.")

    @discord.ui.button(label="Принять", emoji="🎁", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return
        self.done = True
        self.stop()
        await interaction.response.defer()
        ok, text = await self.transfer(interaction.guild)
        embed = discord.Embed(description=text, color=discord.Color.green() if ok else discord.Color.red())
        msg = await interaction.edit_original_response(content=None, embed=embed, view=None)
        delete_later(msg)

    @discord.ui.button(label="Отказаться", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        self.stop()
        embed = discord.Embed(
            description=f"🏳️ {self.receiver.mention} отказался от подарка.", color=discord.Color.greyple()
        )
        await interaction.response.edit_message(content=None, embed=embed, view=None)
        delete_later(self.message)


class GiftTargetView(TempView):
    def __init__(self, bot, giver, role_id: int):
        super().__init__(owner_id=giver.id, timeout=60)
        self.bot = bot
        self.giver = giver
        self.role_id = role_id

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Кому подарить?")
    async def pick(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        target = select.values[0]
        if target.bot or target.id == self.giver.id:
            return await send(interaction, "❌ Выбери другого участника (не себя и не бота).")
        if await self.bot.db.get_item(target.id, self.role_id):
            return await send(interaction, "❌ У этого участника уже есть такая роль.")

        role = interaction.guild.get_role(self.role_id)
        view = GiftOfferView(self.bot, self.giver, target, self.role_id)
        embed = discord.Embed(
            title="🎁 Тебе хотят подарить роль!",
            description=f"{self.giver.mention} дарит {target.mention} роль {role.mention if role else '—'}.\n"
                        f"Ты принимаешь подарок? У тебя есть 3 минуты.",
            color=discord.Color.orange(),
        )
        channel = self.bot.get_channel(config.CART_CHANNEL_ID) or interaction.channel
        view.message = await channel.send(content=target.mention, embed=embed, view=view)
        await interaction.response.edit_message(content=f"✅ Предложение отправлено в <#{channel.id}>.", view=None)
        delete_later(self.message)
        self.stop()


# ---------------------------------------------------------------- удаление роли автором

class ConfirmDeleteView(TempView):
    def __init__(self, bot, user, role_id: int):
        super().__init__(owner_id=user.id, timeout=30)
        self.bot = bot
        self.user = user
        self.role_id = role_id

    @discord.ui.button(label="Да, удалить навсегда", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = self.bot.db
        cr = await db.get_custom_role(self.role_id)
        if cr is None or cr["creator_id"] != self.user.id:
            await interaction.response.edit_message(content="❌ Роль уже удалена или это не твоя роль.", view=None)
            return delete_later(self.message)
        role = interaction.guild.get_role(self.role_id)
        if role:
            try:
                await role.delete(reason="Автор удалил свою роль")
            except discord.HTTPException:
                await interaction.response.edit_message(content="❌ Не удалось удалить роль.", view=None)
                return delete_later(self.message)
        await db.delete_custom_role(self.role_id)
        await interaction.response.edit_message(content="🗑 Роль удалена. Откройте /inventory заново, чтобы обновить список.", view=None)
        delete_later(self.message)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Отменено.", view=None)
        delete_later(self.message)


# ---------------------------------------------------------------- сам инвентарь

class ItemSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="Выбери роль…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        view: "InventoryView" = self.view
        view.selected = int(self.values[0])
        await interaction.response.defer()
        await view.refresh(interaction)


class InventoryView(PagerView):
    def __init__(self, bot, guild: discord.Guild, user: discord.Member):
        # меню исчезает через 20 секунд после последнего нажатия; листание — третьей строкой кнопок
        super().__init__(owner_id=user.id, total=0, page_size=config.PAGE_SIZE, row=2)
        self.bot = bot
        self.guild = guild
        self.user = user
        self.selected: int | None = None
        self.select_item: ItemSelect | None = None

    async def build(self) -> discord.Embed:
        """Собирает embed текущей страницы и пересоздаёт выпадающий список."""
        db = self.bot.db
        now = int(time.time())
        items = [it for it in await db.get_inventory(self.user.id, now) if self.guild.get_role(it["role_id"])]
        self.total = len(items)
        self.update_pager()
        start = self.page * self.page_size
        lines, options = [], []

        for it in items[start:start + self.page_size]:
            role = self.guild.get_role(it["role_id"])
            state = "надета" if it["equipped"] else "в хранилище"
            icon = "👕" if it["equipped"] else "📦"
            term = "навсегда" if not it["expires_at"] else f"до <t:{it['expires_at']}:D> (<t:{it['expires_at']}:R>)"
            line = f"{icon} {role.mention} — {state}, {term}"
            cr = await db.get_custom_role(it["role_id"])
            if cr:
                if cr["creator_id"] == self.user.id:
                    sale = "" if cr["listed"] else ", снята с продажи"
                    line += f"\n└ ✍️ Твоя роль, цена {cr['price']}{sale}"
                else:
                    line += f"\n└ Автор <@{cr['creator_id']}>, цена продления {cr['price']}"
            lines.append(line)
            options.append(discord.SelectOption(
                label=role.name[:100], value=str(role.id), description=state, default=(role.id == self.selected)
            ))

        if self.select_item is not None:
            self.remove_item(self.select_item)
            self.select_item = None
        if options:
            self.select_item = ItemSelect(options)
            self.add_item(self.select_item)

        embed = discord.Embed(
            title="🎒 Инвентарь ролей",
            description="\n\n".join(lines) or "Пока пусто. Купи роль в /shop или создай свою через /role_create.",
            color=discord.Color.blurple(),
        )
        footer = "Выбери роль в списке, затем нажми кнопку. Дарить можно только роли, которые ты создал сам."
        if self.pages > 1:
            footer = f"Страница {self.page + 1}/{self.pages} · " + footer
        embed.set_footer(text=footer)
        return embed

    async def render(self) -> discord.Embed:
        return await self.build()

    async def refresh(self, interaction: discord.Interaction):
        embed = await self.build()
        await interaction.edit_original_response(embed=embed, view=self)

    async def require_selected(self, interaction: discord.Interaction):
        if self.selected is None:
            await send(interaction, "Сначала выбери роль в списке выше.")
            return None
        item = await self.bot.db.get_item(self.user.id, self.selected)
        if not item or (item["expires_at"] and item["expires_at"] <= int(time.time())):
            await send(interaction, "❌ Этой роли уже нет в инвентаре.")
            return None
        return item

    @discord.ui.button(label="Надеть", emoji="👕", style=discord.ButtonStyle.primary, row=1)
    async def equip(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self.require_selected(interaction)
        if not item:
            return
        await interaction.response.defer()
        role = self.guild.get_role(item["role_id"])
        if role is None or not await add_role(self.guild, self.user.id, role, "Надел роль из инвентаря"):
            await send(interaction, "❌ Не удалось выдать роль (роль бота должна быть выше неё).")
        else:
            await self.bot.db.set_equipped(self.user.id, item["role_id"], 1)
        await self.refresh(interaction)

    @discord.ui.button(label="В хранилище", emoji="📦", style=discord.ButtonStyle.secondary, row=1)
    async def store(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self.require_selected(interaction)
        if not item:
            return
        await interaction.response.defer()
        role = self.guild.get_role(item["role_id"])
        if not await remove_role(self.guild, self.user.id, role, "Убрал роль в хранилище"):
            await send(interaction, "❌ Не удалось снять роль.")
        else:
            await self.bot.db.set_equipped(self.user.id, item["role_id"], 0)
        await self.refresh(interaction)

    @discord.ui.button(label="Продлить", emoji="🔁", style=discord.ButtonStyle.success, row=1)
    async def renew(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self.require_selected(interaction)
        if not item:
            return
        await interaction.response.defer()
        ok, text = await renew_item(self.bot, self.user, item["role_id"])
        await send(interaction, text)
        await self.refresh(interaction)

    @discord.ui.button(label="Подарить", emoji="🎁", style=discord.ButtonStyle.primary, row=1)
    async def gift(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self.require_selected(interaction)
        if not item:
            return
        role = self.guild.get_role(item["role_id"])
        if role is None:
            return await send(interaction, "❌ Роль не найдена.")
        cr = await self.bot.db.get_custom_role(role.id)
        if not cr or cr["creator_id"] != self.user.id:
            return await send(interaction, "❌ Дарить можно только роли, которые ты создал сам.")
        if role.permissions.administrator:
            return await send(interaction, "❌ Роли с правами администратора дарить нельзя.")
        view = GiftTargetView(self.bot, self.user, role.id)
        await send(interaction, f"Кому подарить роль {role.mention}? (у тебя она останется)", view=view)

    @discord.ui.button(label="Удалить роль", emoji="🗑", style=discord.ButtonStyle.danger, row=1)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        item = await self.require_selected(interaction)
        if not item:
            return
        cr = await self.bot.db.get_custom_role(item["role_id"])
        if not cr or cr["creator_id"] != self.user.id:
            return await send(interaction, "❌ Удалить можно только роль, которую создал ты.")
        await send(
            interaction,
            "⚠️ Роль удалится у всех, кто её купил, без возврата денег. Точно удалить?",
            view=ConfirmDeleteView(self.bot, self.user, item["role_id"]),
        )


class Inventory(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="inventory", description="Твой инвентарь ролей")
    @app_commands.guild_only()
    async def inventory(self, interaction: discord.Interaction):
        view = InventoryView(self.bot, interaction.guild, interaction.user)
        embed = await view.build()
        await send(interaction, embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(Inventory(bot))
