"""Мелкие вспомогательные функции."""
import asyncio
import os

import discord

import config


def calc_fee(amount: int, percent: int) -> int:
    """Комиссия в монетах (округляется вверх)."""
    return -(-amount * percent // 100)


def fmt_num(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def fmt_voice(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    return f"{hours} ч {rest // 60} мин"


def days_between(start: int, end: int) -> int:
    """Сколько полных дней прошло (минимум 1 — первый день тоже считается)."""
    return max(1, (end - start) // 86400 + 1)


def pages_count(total: int, size: int) -> int:
    return max(1, -(-total // size))


def role_is_manageable(guild: discord.Guild, role: discord.Role) -> bool:
    """Может ли бот выдавать эту роль (она ниже роли бота и не служебная)."""
    return not role.is_default() and not role.managed and role < guild.me.top_role


async def fetch_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member:
        return member
    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.HTTPException):
        return None


async def add_role(guild: discord.Guild, user_id: int, role: discord.Role | None, reason: str) -> bool:
    member = await fetch_member(guild, user_id)
    if member is None or role is None:
        return False
    try:
        await member.add_roles(role, reason=reason)
        return True
    except discord.HTTPException:
        return False


async def remove_role(guild: discord.Guild, user_id: int, role: discord.Role | None, reason: str) -> bool:
    """True, если роли у человека больше нет (убрали или её и не было)."""
    member = await fetch_member(guild, user_id)
    if member is None or role is None:
        return True
    if role not in member.roles:
        return True
    try:
        await member.remove_roles(role, reason=reason)
        return True
    except discord.HTTPException:
        return False


# ---------- сообщения: публичные, удаляются через config.MESSAGE_LIFETIME секунд ----------
_tasks: set = set()


async def safe_delete(message) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except discord.HTTPException:
        pass


def delete_later(message, delay: int | None = None) -> None:
    """Удалит сообщение через delay секунд (по умолчанию config.MESSAGE_LIFETIME)."""
    if message is None:
        return
    delay = config.MESSAGE_LIFETIME if delay is None else delay

    async def runner():
        await asyncio.sleep(delay)
        await safe_delete(message)

    task = asyncio.create_task(runner())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def send(interaction: discord.Interaction, content=None, *, embed=None, view=None,
               keep: bool = False, ephemeral: bool = False):
    """Отправляет публичное сообщение (или followup, если ответ уже был) и планирует его удаление.
    keep=True — не удалять (профиль, баланс). Возвращает отправленное сообщение."""
    kwargs = {}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    if ephemeral:
        kwargs["ephemeral"] = True
    if interaction.response.is_done():
        msg = await interaction.followup.send(wait=True, **kwargs)
    else:
        await interaction.response.send_message(**kwargs)
        msg = await interaction.original_response()
    if view is not None and hasattr(view, "message"):
        view.message = msg
    if not keep and not ephemeral and view is None:
        delete_later(msg)
    return msg


def gif_file(game: str) -> discord.File | None:
    """Возвращает discord.File с гифкой игры (или None, если файла нет).
    Файл нужно создавать заново для каждой отправки."""
    name = config.GIFS.get(game)
    if not name:
        return None
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), config.GIF_DIR, name)
    if not os.path.isfile(path):
        return None
    return discord.File(path, filename=name)


def gif_embed(game: str, title: str, text: str, color=None) -> discord.Embed:
    """Embed «игра идёт» с картинкой-гифкой (attachment://...)."""
    embed = discord.Embed(title=title, description=text, color=color or discord.Color.blurple())
    name = config.GIFS.get(game)
    if name:
        embed.set_image(url=f"attachment://{name}")
    return embed


class TempView(discord.ui.View):
    """Меню, которое исчезает через config.MESSAGE_LIFETIME секунд после последнего нажатия.
    owner_id — кто может нажимать кнопки (None = любой)."""

    def __init__(self, owner_id: int | None = None, timeout: float | None = None):
        super().__init__(timeout=config.MESSAGE_LIFETIME if timeout is None else timeout)
        self.owner_id = owner_id
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await send(interaction, "⛔ Это меню открыл другой участник. Вызови свою команду.")
            return False
        return True

    async def on_timeout(self):
        await safe_delete(self.message)


class PagerView(TempView):
    """Меню со страницами: кнопки ◀ / ▶ и счётчик. Наследники переопределяют render()."""

    def __init__(self, owner_id: int | None, total: int, page_size: int, row: int = 2):
        super().__init__(owner_id=owner_id)
        self.page = 0
        self.page_size = page_size
        self.total = total
        self._prev = discord.ui.Button(emoji="◀", style=discord.ButtonStyle.secondary, row=row)
        self._info = discord.ui.Button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True, row=row)
        self._next = discord.ui.Button(emoji="▶", style=discord.ButtonStyle.secondary, row=row)
        self._prev.callback = self._go_prev
        self._next.callback = self._go_next
        self._pager_shown = False

    @property
    def pages(self) -> int:
        return pages_count(self.total, self.page_size)

    def update_pager(self):
        """Показывает кнопки листания, только если страниц больше одной."""
        self.page = min(self.page, self.pages - 1)
        need = self.pages > 1
        if need and not self._pager_shown:
            for b in (self._prev, self._info, self._next):
                self.add_item(b)
        elif not need and self._pager_shown:
            for b in (self._prev, self._info, self._next):
                self.remove_item(b)
        self._pager_shown = need
        self._info.label = f"{self.page + 1}/{self.pages}"
        self._prev.disabled = self.page == 0
        self._next.disabled = self.page >= self.pages - 1

    async def render(self) -> discord.Embed:  # pragma: no cover - переопределяется
        raise NotImplementedError

    async def _go_prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        await self._redraw(interaction)

    async def _go_next(self, interaction: discord.Interaction):
        self.page = min(self.pages - 1, self.page + 1)
        await self._redraw(interaction)

    async def _redraw(self, interaction: discord.Interaction):
        embed = await self.render()
        await interaction.response.edit_message(embed=embed, view=self)


async def notify_user(bot, user_id: int, text: str, *, fallback: bool = True) -> bool:
    """Пишет участнику в ЛС. Если ЛС закрыты и fallback=True — упоминает его в #корзина.
    Возвращает True, если сообщение ушло в ЛС."""
    user = bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except discord.HTTPException:
            user = None
    if user is not None:
        try:
            await user.send(text)
            return True
        except discord.HTTPException:
            pass
    if fallback:
        channel = bot.get_channel(config.CART_CHANNEL_ID)
        if channel is not None:
            try:
                await channel.send(f"<@{user_id}> {text}", delete_after=config.REMINDER_FALLBACK_LIFETIME)
            except discord.HTTPException:
                pass
    return False
