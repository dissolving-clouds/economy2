"""Напоминания в ЛС о скором окончании срока ролей и любовных комнат.

Раз в 10 минут бот ищет роли из инвентаря и комнаты, срок которых закончится
в ближайшие N часов (N = настройка reminder_hours в /panel, 0 = выключено), и пишет владельцу в ЛС.
Каждое напоминание отправляется один раз; после продления срок отсчитывается заново.
Если ЛС закрыты и включена настройка reminder_fallback — участника упомянут в #корзина.
"""
import logging
import time

from discord.ext import commands, tasks

from cogs.rooms import room_fund
from utils import notify_user


class Reminders(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.remind.start()

    async def cog_unload(self):
        self.remind.cancel()

    @tasks.loop(minutes=10)
    async def remind(self):
        try:
            await self._remind()
        except Exception:
            logging.exception("Ошибка при отправке напоминаний")

    @remind.before_loop
    async def before_remind(self):
        await self.bot.wait_until_ready()

    async def _remind(self):
        db = self.bot.db
        hours = await db.get_int("reminder_hours")
        if hours <= 0:
            return
        guild = self.bot.main_guild()
        if guild is None:
            return
        fallback = bool(await db.get_int("reminder_fallback"))
        now = int(time.time())
        until = now + hours * 3600

        # ---------- роли из инвентаря ----------
        for it in await db.get_items_to_remind(now, until):
            role = guild.get_role(it["role_id"])
            if role is None:
                await db.mark_item_reminded(it["user_id"], it["role_id"])
                continue
            cr = await db.get_custom_role(it["role_id"])
            exp = it["expires_at"]
            if cr and cr["creator_id"] == it["user_id"]:
                text = (f"⏰ Твоя роль **{role.name}** на сервере **{guild.name}** закончится <t:{exp}:R> "
                        f"(<t:{exp}:f>).\n"
                        f"Если не продлить, она **удалится у всех**, кто её купил. "
                        f"Продление: {await db.fmt(cr['price'])} — `/inventory` → выбери роль → «Продлить».")
            elif cr:
                text = (f"⏰ Роль **{role.name}** на сервере **{guild.name}** закончится <t:{exp}:R> "
                        f"(<t:{exp}:f>).\n"
                        f"Продление: {await db.fmt(cr['price'])} — `/inventory` → выбери роль → «Продлить».")
            else:
                text = (f"⏰ Роль **{role.name}** на сервере **{guild.name}** закончится <t:{exp}:R> "
                        f"(<t:{exp}:f>).")
            await notify_user(self.bot, it["user_id"], text, fallback=fallback)
            await db.mark_item_reminded(it["user_id"], it["role_id"])

        # ---------- любовные комнаты ----------
        price = await db.get_int("room_price")
        days = await db.get_int("room_days")
        autorenew = bool(await db.get_int("room_autorenew"))
        for room in await db.get_rooms_to_remind(now, until):
            fund = room_fund(room)
            exp = room["expires_at"]
            if autorenew and fund >= price:
                tail = "В копилке хватает денег — комната **продлится автоматически**. 💕"
            else:
                tail = (f"Продление: {await db.fmt(price)} на {days} дн., в копилке {await db.fmt(fund)}.\n"
                        f"Продлить или скинуться в копилку — кнопками в `/profile` или `/love_profile`. "
                        f"Продлить может любой из вас.")
            text = (f"⏰ Ваша любовная комната с **{{partner}}** на сервере **{guild.name}** закончится "
                    f"<t:{exp}:R> (<t:{exp}:f>).\n{tail}")
            for uid, partner in ((room["owner_id"], room["partner_id"]), (room["partner_id"], room["owner_id"])):
                member = guild.get_member(partner)
                name = member.display_name if member else f"<@{partner}>"
                await notify_user(self.bot, uid, text.replace("{partner}", name), fallback=fallback)
            await db.mark_room_reminded(room["owner_id"])


async def setup(bot):
    await bot.add_cog(Reminders(bot))
