import asyncio
import asyncpg
import secrets
import os

from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    CallbackQuery,
)

from config import BOT_TOKEN, DATABASE_URL


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool = None


class AddTelegramUser(StatesGroup):
    waiting_for_username = State()


class AddWhatsAppNumber(StatesGroup):
    waiting_for_phone = State()


async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)


async def is_admin(telegram_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "select id from admins where telegram_id = $1",
            telegram_id
        )
        return row is not None


async def get_users():
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "select id, username, status from tg_users order by id asc"
        )


async def add_user(username: str, added_by: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            insert into tg_users (username, status, added_by)
            values ($1, 'не проверен', $2)
            on conflict (username) do nothing
            """,
            username,
            added_by
        )


async def delete_user(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("delete from tg_users where id = $1", user_id)


async def get_wa_numbers():
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "select id, phone, status from wa_numbers order by id asc"
        )


async def add_wa_number(phone: str, added_by: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            insert into wa_numbers (phone, status, added_by)
            values ($1, 'не проверен', $2)
            on conflict (phone) do nothing
            """,
            phone,
            added_by
        )


async def delete_wa_number(number_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("delete from wa_numbers where id = $1", number_id)


def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 Telegram юзеры")],
            [KeyboardButton(text="➕ Добавить Telegram")],
            [KeyboardButton(text="📤 Выгрузить Telegram")],
            [KeyboardButton(text="📱 WhatsApp номера")],
            [KeyboardButton(text="➕ Добавить WhatsApp")],
            [KeyboardButton(text="📤 Выгрузить WhatsApp")],
            [KeyboardButton(text="🔗 Создать ссылку доступа")],
        ],
        resize_keyboard=True
    )


@dp.message(CommandStart())
async def start(message: Message):
    text = message.text or ""

    if text.startswith("/start invite_"):
        token = text.replace("/start invite_", "").strip()

        async with db_pool.acquire() as conn:
            invite = await conn.fetchrow(
                "select id, is_used from invites where token = $1",
                token
            )

            if not invite:
                await message.answer("❌ Ссылка недействительна")
                return

            if invite["is_used"]:
                await message.answer("❌ Ссылка уже использована")
                return

            await conn.execute(
                """
                insert into admins (telegram_id, username)
                values ($1, $2)
                on conflict (telegram_id) do nothing
                """,
                message.from_user.id,
                message.from_user.username
            )

            await conn.execute(
                """
                update invites
                set is_used = true, used_by = $1, used_at = now()
                where token = $2
                """,
                message.from_user.id,
                token
            )

        await message.answer("✅ Доступ выдан", reply_markup=main_menu())
        return

    if not await is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    await message.answer("Панель управления", reply_markup=main_menu())


@dp.message(F.text == "🔗 Создать ссылку доступа")
async def create_invite(message: Message):
    if not await is_admin(message.from_user.id):
        return

    token = secrets.token_urlsafe(8)

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            insert into invites (token, created_by)
            values ($1, $2)
            """,
            token,
            message.from_user.id
        )

    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=invite_{token}"

    await message.answer(f"🔗 Ссылка доступа:\n\n{link}")


@dp.message(F.text == "➕ Добавить Telegram")
async def add_telegram_button(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return

    await message.answer("Отправь @username")
    await state.set_state(AddTelegramUser.waiting_for_username)


@dp.message(AddTelegramUser.waiting_for_username)
async def process_telegram_username(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return

    username = message.text.replace("@", "").strip()

    if not username:
        await message.answer("Пустой username")
        return

    await add_user(username, message.from_user.id)

    await message.answer(f"Добавлен @{username}", reply_markup=main_menu())
    await state.clear()


@dp.message(F.text == "👥 Telegram юзеры")
async def show_telegram_users(message: Message):
    if not await is_admin(message.from_user.id):
        return

    users = await get_users()

    if not users:
        await message.answer("База Telegram пустая")
        return

    for user in users:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔗 Открыть чат",
                    url=f"https://t.me/{user['username']}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Удалить",
                    callback_data=f"delete_tg_{user['id']}"
                )
            ]
        ])

        await message.answer(
            f"@{user['username']}\nСтатус: {user['status']}",
            reply_markup=kb
        )


@dp.callback_query(F.data.startswith("delete_tg_"))
async def delete_telegram_callback(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return

    user_id = int(callback.data.split("_")[2])
    await delete_user(user_id)

    await callback.message.edit_text("❌ Telegram юзер удален")
    await callback.answer("Удалено")


@dp.message(F.text == "📤 Выгрузить Telegram")
async def export_telegram_users(message: Message):
    if not await is_admin(message.from_user.id):
        return

    users = await get_users()

    if not users:
        await message.answer("База Telegram пустая")
        return

    usernames = [f"@{user['username']}" for user in users]

    with open("users_export.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(usernames))

    text = "📤 Telegram юзеры выгружены:\n\n"
    text += "\n".join(usernames)
    text += f"\n\nВсего: {len(users)}"

    await message.answer(text, reply_markup=main_menu())


@dp.message(F.text == "➕ Добавить WhatsApp")
async def add_whatsapp_button(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return

    await message.answer("Отправь номер WhatsApp в формате +380991234567")
    await state.set_state(AddWhatsAppNumber.waiting_for_phone)


@dp.message(AddWhatsAppNumber.waiting_for_phone)
async def process_whatsapp_phone(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return

    phone = message.text.strip().replace(" ", "").replace("-", "")

    if not phone.startswith("+"):
        await message.answer("Номер должен начинаться с +, например +380991234567")
        return

    await add_wa_number(phone, message.from_user.id)

    await message.answer(f"Добавлен WhatsApp номер:\n{phone}", reply_markup=main_menu())
    await state.clear()


@dp.message(F.text == "📱 WhatsApp номера")
async def show_whatsapp_numbers(message: Message):
    if not await is_admin(message.from_user.id):
        return

    numbers = await get_wa_numbers()

    if not numbers:
        await message.answer("База WhatsApp пустая")
        return

    for number in numbers:
        clean_phone = number["phone"].replace("+", "")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔗 Открыть WhatsApp",
                    url=f"https://wa.me/{clean_phone}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Удалить",
                    callback_data=f"delete_wa_{number['id']}"
                )
            ]
        ])

        await message.answer(
            f"{number['phone']}\nСтатус: {number['status']}",
            reply_markup=kb
        )


@dp.callback_query(F.data.startswith("delete_wa_"))
async def delete_whatsapp_callback(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return

    number_id = int(callback.data.split("_")[2])
    await delete_wa_number(number_id)

    await callback.message.edit_text("❌ WhatsApp номер удален")
    await callback.answer("Удалено")


@dp.message(F.text == "📤 Выгрузить WhatsApp")
async def export_whatsapp_numbers(message: Message):
    if not await is_admin(message.from_user.id):
        return

    numbers = await get_wa_numbers()

    if not numbers:
        await message.answer("База WhatsApp пустая")
        return

    phones = [number["phone"] for number in numbers]

    with open("whatsapp_export.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(phones))

    text = "📤 WhatsApp номера выгружены:\n\n"
    text += "\n".join(phones)
    text += f"\n\nВсего: {len(phones)}"

    await message.answer(text, reply_markup=main_menu())


@dp.message(F.text.startswith("/add"))
async def add_user_command(message: Message):
    if not await is_admin(message.from_user.id):
        return

    parts = message.text.split()

    if len(parts) < 2:
        await message.answer("Напиши: /add @username")
        return

    username = parts[1].replace("@", "").strip()

    await add_user(username, message.from_user.id)

    await message.answer(f"Добавлен @{username}", reply_markup=main_menu())


async def handle(request):
    return web.Response(text="Bot is running")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main():
    await init_db()
    asyncio.create_task(start_web_server())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
