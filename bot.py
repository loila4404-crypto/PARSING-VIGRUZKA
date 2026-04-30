import asyncio
import asyncpg
import secrets

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


class AddUser(StatesGroup):
    waiting_for_username = State()


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


def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 Юзеры")],
            [KeyboardButton(text="➕ Добавить юзера")],
            [KeyboardButton(text="📤 Выгрузить юзеров")],
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


@dp.message(F.text == "➕ Добавить юзера")
async def add_user_button(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return

    await message.answer("Отправь @username")
    await state.set_state(AddUser.waiting_for_username)


@dp.message(AddUser.waiting_for_username)
async def process_username(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return

    username = message.text.replace("@", "").strip()

    if not username:
        await message.answer("Пустой username")
        return

    await add_user(username, message.from_user.id)

    await message.answer(f"Добавлен @{username}", reply_markup=main_menu())
    await state.clear()


@dp.message(F.text == "👥 Юзеры")
async def show_users(message: Message):
    if not await is_admin(message.from_user.id):
        return

    users = await get_users()

    if not users:
        await message.answer("База пустая")
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
                    callback_data=f"delete_{user['id']}"
                )
            ]
        ])

        await message.answer(
            f"@{user['username']}\nСтатус: {user['status']}",
            reply_markup=kb
        )


@dp.callback_query(F.data.startswith("delete_"))
async def delete_user_callback(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return

    user_id = int(callback.data.split("_")[1])
    await delete_user(user_id)

    await callback.message.edit_text("❌ Юзер удален")
    await callback.answer("Удалено")


@dp.message(F.text == "📤 Выгрузить юзеров")
async def export_users(message: Message):
    if not await is_admin(message.from_user.id):
        return

    users = await get_users()

    if not users:
        await message.answer("База пустая")
        return

    usernames = [f"@{user['username']}" for user in users]

    with open("users_export.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(usernames))

    text = "📤 Юзеры выгружены:\n\n"
    text += "\n".join(usernames)
    text += f"\n\nВсего: {len(users)}"

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


from aiohttp import web
import os

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
