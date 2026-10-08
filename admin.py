"""Панель управления: /panel (только для роли администратора бота)."""
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
from checks import admin_only, is_bot_admin
from cogs.rooms import room_embed


async def build_panel_embed(bot, guild: discord.Guild) -> discord.Embed:
    db = bot.db
    users, total, shop_count, custom_count, rooms = await db.stats()
    embed = discord.Embed(title="🛠 Панель управления экономикой", color=discord.Color.blurple())
    embed.add_field(
        name="💰 Экономика",
        value=(
            f"Валюта: {await db.get('currency')}\n"
            f"Daily: {await db.get('daily_amount')} (+{await db.get('daily_streak_bonus')} за день серии)\n"
            f"Кулдаун daily: {await db.get('daily_cooldown_hours')} ч.\n"
            f"Награда за голосовой онлайн: {await db.get('voice_reward')} за минуту"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎲 Казино и дуэли",
        value=f"Макс. ставка: {await db.get('max_bet')}\nКомиссия дуэли: {await db.get('duel_commission')}%",
        inline=False,
    )
    cat_id = await db.get_int("room_category_id")
    role_id = await db.get_int("room_role_id")
    embed.add_field(
        name="💞 Любовные комнаты",
        value=(
            f"Цена: {await db.get('room_price')} · срок: {await db.get('room_days')} дн.\n"
            f"Запасная категория: {f'<#{cat_id}>' if cat_id else 'не задана'} (обычно комнаты создаются рядом с «Войти»)\n"
            f"Выдаваемая роль: {f'<@&{role_id}> (`{role_id}`)' if role_id else 'не задана'}\n"
            f"Автопродление из копилки: {'включено' if await db.get_int('room_autorenew') else 'выключено'} · "
            f"в копилках сейчас: {await db.rooms_fund_total()}"
        ),
        inline=False,
    )
    hours = await db.get_int("reminder_hours")
    embed.add_field(
        name="🔔 Напоминания в ЛС",
        value=(
            (f"За {hours} ч. до конца срока ролей и комнат" if hours else "Выключены") + "\n"
            f"Если ЛС закрыты — упоминание в #корзина: {'да' if await db.get_int('reminder_fallback') else 'нет'}"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎨 Комиссии и роли игроков",
        value=(
            f"Комиссия переводов: {await db.get('transfer_commission')}%\n"
            f"Комиссия продажи ролей: {await db.get('role_commission')}%\n"
            f"Срок роли игрока: {await db.get('user_role_days')} дн. · лимит ролей на игрока: {await db.get('user_role_limit')}\n"
            f"Минимальная цена роли: {config.ROLE_MIN_PRICE}"
        ),
        inline=False,
    )
    embed.add_field(
        name="📊 Статистика",
        value=(
            f"Игроков: {users} · монет в обороте: {total}\n"
            f"Ролей сервера в магазине: {shop_count} · ролей игроков на продаже: {custom_count}\n"
            f"Активных комнат: {rooms}"
        ),
        inline=False,
    )
    return embed


# Спецификация поля: (ключ, подпись, тип, минимум, максимум). Типы: int / str / role / category
ECONOMY_FIELDS = [
    ("currency", "Значок валюты (эмодзи или текст)", "str", None, None),
    ("daily_amount", "Награда за /daily", "int", 0, None),
    ("daily_streak_bonus", "Бонус за каждый день серии", "int", 0, None),
    ("daily_cooldown_hours", "Кулдаун daily (часы)", "int", 1, None),
    ("voice_reward", "Монет за минуту в голосовом (0 = выкл.)", "int", 0, 100000),
]
CASINO_FIELDS = [
    ("max_bet", "Максимальная ставка", "int", 1, None),
    ("duel_commission", "Комиссия дуэли (0-100 %)", "int", 0, 100),
]
ROOM_FIELDS = [
    ("room_price", "Цена комнаты", "int", 0, None),
    ("room_days", "Срок комнаты (дни)", "int", 1, None),
    ("room_category_id", "ID запасной категории (0 = нет)", "category", 0, None),
    ("room_role_id", "ID выдаваемой роли (0 = не выдавать)", "role", 0, None),
    ("room_autorenew", "Автопродление из копилки (1 = да, 0 = нет)", "int", 0, 1),
]
REMINDER_FIELDS = [
    ("reminder_hours", "За сколько часов напоминать (0 = выкл.)", "int", 0, 168),
    ("reminder_fallback", "Если ЛС закрыты — писать в #корзина (1/0)", "int", 0, 1),
]
MARKET_FIELDS = [
    ("transfer_commission", "Комиссия переводов (0-100 %)", "int", 0, 100),
    ("role_commission", "Комиссия продажи ролей (0-100 %)", "int", 0, 100),
    ("user_role_days", "Срок роли игрока (дни)", "int", 1, None),
    ("user_role_limit", "Сколько ролей может создать игрок", "int", 0, None),
]


class SettingsModal(discord.ui.Modal):
    """Универсальное окно настроек: строит поля по списку specs."""

    def __init__(self, bot, title: str, specs, current: dict):
        super().__init__(title=title)
        self.bot = bot
        self.specs = specs
        self.inputs = {}
        for key, label, kind, lo, hi in specs:
            field = discord.ui.TextInput(label=label, default=str(current[key]), max_length=100)
            self.inputs[key] = field
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        parsed = {}
        for key, label, kind, lo, hi in self.specs:
            raw = self.inputs[key].value.strip()
            if kind == "str":
                if not raw:
                    return await interaction.response.send_message(f"❌ «{label}» не может быть пустым.", ephemeral=True)
                parsed[key] = raw
                continue
            try:
                value = int(raw)
            except ValueError:
                return await interaction.response.send_message(f"❌ «{label}» должно быть целым числом.", ephemeral=True)
            if (lo is not None and value < lo) or (hi is not None and value > hi):
                return await interaction.response.send_message(f"❌ «{label}»: значение вне допустимых границ.", ephemeral=True)
            if kind == "role" and value:
                role = guild.get_role(value)
                if role is None:
                    return await interaction.response.send_message("❌ Роль с таким ID не найдена на сервере.", ephemeral=True)
                if role.is_default() or role.managed or role >= guild.me.top_role:
                    return await interaction.response.send_message(
                        "❌ Я не смогу выдавать эту роль: она выше роли бота или служебная.", ephemeral=True
                    )
            if kind == "category" and value and not isinstance(guild.get_channel(value), discord.CategoryChannel):
                return await interaction.response.send_message("❌ Категория с таким ID не найдена.", ephemeral=True)
            parsed[key] = str(value)
        for key, value in parsed.items():
            await self.bot.db.set(key, value)
        await interaction.response.send_message("✅ Сохранено! Нажми «🔄 Обновить» на панели.", ephemeral=True)


class MoneyModal(discord.ui.Modal, title="Выдать / забрать монеты"):
    amount = discord.ui.TextInput(label="Сумма (минус = забрать)", placeholder="например 500 или -200")

    def __init__(self, bot, target: discord.abc.User):
        super().__init__()
        self.bot = bot
        self.target = target

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(self.amount.value.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Нужно целое число.", ephemeral=True)
        db = self.bot.db
        old_balance = await db.get_balance(self.target.id)
        new_balance = await db.add_balance(self.target.id, value)
        delta = new_balance - old_balance  # баланс не уходит ниже 0, поэтому считаем реальное изменение
        now = int(time.time())
        note = f"админ <@{interaction.user.id}>"
        if delta > 0:
            await db.add_transaction(now, 0, self.target.id, delta, 0, "admin", note)
        elif delta < 0:
            await db.add_transaction(now, self.target.id, 0, -delta, 0, "admin", note)
        verb = "выдано" if value >= 0 else "забрано"
        await interaction.response.send_message(
            f"✅ {self.target.mention}: {verb} {abs(value)}. Новый баланс: {await self.bot.db.fmt(new_balance)}",
            ephemeral=True,
        )


class PriceModal(discord.ui.Modal, title="Добавить роль в магазин"):
    price = discord.ui.TextInput(label="Цена", placeholder="например 2500")
    description = discord.ui.TextInput(label="Описание (необязательно)", required=False, max_length=80)

    def __init__(self, bot, role: discord.Role):
        super().__init__()
        self.bot = bot
        self.role = role

    async def on_submit(self, interaction: discord.Interaction):
        try:
            price = int(self.price.value.strip())
            if price < 0:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("❌ Цена должна быть целым числом ≥ 0.", ephemeral=True)
        await self.bot.db.add_shop_role(self.role.id, price, self.description.value.strip())
        await interaction.response.send_message(f"✅ Роль {self.role.mention} добавлена в магазин за {price}.", ephemeral=True)


class PickUserView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Выбери участника")
    async def pick(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.send_modal(MoneyModal(self.bot, select.values[0]))


class PickRoleView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Выбери роль для продажи")
    async def pick(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0]
        if role.is_default() or role.managed:
            return await interaction.response.send_message("❌ Эту роль нельзя продавать.", ephemeral=True)
        if role.permissions.administrator:
            return await interaction.response.send_message("❌ Роли с правами администратора продавать нельзя.", ephemeral=True)
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                "❌ Эта роль выше роли бота. Подними роль бота выше в настройках сервера.", ephemeral=True
            )
        await interaction.response.send_modal(PriceModal(self.bot, role))


class RoomDaysModal(discord.ui.Modal, title="Изменить срок комнаты"):
    days = discord.ui.TextInput(label="Сколько дней добавить (минус = убрать)", placeholder="например 7 или -3")

    def __init__(self, bot, owner_id: int):
        super().__init__()
        self.bot = bot
        self.owner_id = owner_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(self.days.value.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Нужно целое число.", ephemeral=True)
        db = self.bot.db
        room = await db.get_room_of(self.owner_id)
        if not room:
            return await interaction.response.send_message("❌ Комнаты уже нет.", ephemeral=True)
        new_exp = max(int(time.time()) + 60, room["expires_at"] + value * 86400)
        await db.set_room_expiry(room["owner_id"], new_exp)
        await interaction.response.send_message(f"✅ Комната теперь действует до <t:{new_exp}:f>.", ephemeral=True)


class AdminRoomView(discord.ui.View):
    def __init__(self, bot, owner_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return is_bot_admin(interaction.user)

    @discord.ui.button(label="Изменить срок", emoji="📅", style=discord.ButtonStyle.primary)
    async def change_days(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RoomDaysModal(self.bot, self.owner_id))

    @discord.ui.button(label="Удалить комнату", emoji="💔", style=discord.ButtonStyle.danger)
    async def delete_room(self, interaction: discord.Interaction, button: discord.ui.Button):
        room = await self.bot.db.get_room_of(self.owner_id)
        if not room:
            return await interaction.response.edit_message(content="❌ Комнаты уже нет.", embed=None, view=None)
        await interaction.response.defer()
        rooms_cog = self.bot.get_cog("Rooms")
        ok = await rooms_cog.finish_room(room, f"Удалено администратором {interaction.user}")
        text = "✅ Комната удалена, копилка возвращена участникам." if ok else "❌ Не удалось удалить канал комнаты."
        await interaction.edit_original_response(content=text, embed=None, view=None)


class PickRoomUserView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Выбери участника (любого из пары)")
    async def pick(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        room = await self.bot.db.get_room_of(select.values[0].id)
        if not room:
            return await interaction.response.send_message("У этого участника нет любовной комнаты.", ephemeral=True)
        embed = await room_embed(self.bot, room)
        await interaction.response.send_message(embed=embed, view=AdminRoomView(self.bot, room["owner_id"]), ephemeral=True)


class RemoveListingSelect(discord.ui.Select):
    def __init__(self, bot, options):
        super().__init__(placeholder="Какую роль снять с продажи?", options=options)
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        kind, role_id = self.values[0].split(":")
        role_id = int(role_id)
        if kind == "s":
            await self.bot.db.remove_shop_role(role_id)
        else:
            await self.bot.db.set_custom_listed(role_id, 0)
        await interaction.response.send_message(
            "✅ Роль снята с продажи. Те, кто уже купил её, оставят её у себя до конца срока.", ephemeral=True
        )


class PanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=900)
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_bot_admin(interaction.user):
            return True
        await interaction.response.send_message("⛔ Только для администраторов бота.", ephemeral=True)
        return False

    async def open_settings(self, interaction, title, specs):
        current = {key: await self.bot.db.get(key) for key, *_ in specs}
        await interaction.response.send_modal(SettingsModal(self.bot, title, specs, current))

    @discord.ui.button(label="Экономика", emoji="💰", style=discord.ButtonStyle.primary, row=0)
    async def economy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_settings(interaction, "Настройки экономики", ECONOMY_FIELDS)

    @discord.ui.button(label="Казино и дуэли", emoji="🎲", style=discord.ButtonStyle.primary, row=0)
    async def casino(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_settings(interaction, "Казино и дуэли", CASINO_FIELDS)

    @discord.ui.button(label="Комнаты", emoji="💞", style=discord.ButtonStyle.primary, row=0)
    async def rooms(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_settings(interaction, "Любовные комнаты", ROOM_FIELDS)

    @discord.ui.button(label="Комиссии и роли", emoji="🎨", style=discord.ButtonStyle.primary, row=0)
    async def market(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_settings(interaction, "Комиссии и роли игроков", MARKET_FIELDS)

    @discord.ui.button(label="Напоминания", emoji="🔔", style=discord.ButtonStyle.primary, row=0)
    async def reminders(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.open_settings(interaction, "Напоминания в ЛС", REMINDER_FIELDS)

    @discord.ui.button(label="Выдать/забрать монеты", emoji="💸", style=discord.ButtonStyle.success, row=1)
    async def money(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Кому выдать или у кого забрать монеты?", view=PickUserView(self.bot), ephemeral=True)

    @discord.ui.button(label="Добавить роль в магазин", emoji="🏪", style=discord.ButtonStyle.success, row=1)
    async def add_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Выбери роль, которую хочешь продавать:", view=PickRoleView(self.bot), ephemeral=True)

    @discord.ui.button(label="Снять роль с продажи", emoji="🗑", style=discord.ButtonStyle.danger, row=1)
    async def remove_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = self.bot.db
        guild = interaction.guild
        options = []
        for item in await db.get_shop_roles():
            role = guild.get_role(item["role_id"])
            name = role.name if role else f"Удалённая роль ({item['role_id']})"
            options.append(discord.SelectOption(
                label=name[:100], description=f"Роль сервера · цена {item['price']}", value=f"s:{item['role_id']}"
            ))
        for cr in await db.get_listed_custom_roles(0):
            role = guild.get_role(cr["role_id"])
            name = role.name if role else f"Удалённая роль ({cr['role_id']})"
            options.append(discord.SelectOption(
                label=name[:100], description=f"Роль игрока · цена {cr['price']}", value=f"p:{cr['role_id']}"
            ))
        if not options:
            return await interaction.response.send_message("В магазине нет ролей.", ephemeral=True)
        view = discord.ui.View(timeout=120)
        view.add_item(RemoveListingSelect(self.bot, options[:25]))
        await interaction.response.send_message("Выбери роль, которую нужно снять с продажи:", view=view, ephemeral=True)

    @discord.ui.button(label="Комната участника", emoji="💞", style=discord.ButtonStyle.secondary, row=1)
    async def room_manage(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Чью любовную комнату открыть?", view=PickRoomUserView(self.bot), ephemeral=True
        )

    @discord.ui.button(label="Обновить", emoji="🔄", style=discord.ButtonStyle.secondary, row=2)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=await build_panel_embed(self.bot, interaction.guild), view=self)


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="panel", description="Панель управления экономикой (для администраторов бота)")
    @admin_only()
    @app_commands.guild_only()
    async def panel(self, interaction: discord.Interaction):
        embed = await build_panel_embed(self.bot, interaction.guild)
        await interaction.response.send_message(embed=embed, view=PanelView(self.bot), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Admin(bot))
