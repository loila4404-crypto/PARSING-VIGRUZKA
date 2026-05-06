import asyncio
import asyncpg
import secrets
import os
import aiohttp

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

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
RENDER_URL = os.getenv("RENDER_URL")
MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "0"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден на сервере")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не найден на сервере")


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool = None


class AddTelegramUser(StatesGroup):
    waiting_for_username = State()


class AddWhatsAppNumber(StatesGroup):
    waiting_for_phone = State()


class CreateTelegramGroup(StatesGroup):
    waiting_for_group_name = State()


class RenameTelegramGroup(StatesGroup):
    waiting_for_group_id = State()
    waiting_for_new_name = State()


async def init_db():
    global db_pool

    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=3,
        ssl="require",
        command_timeout=30
    )

    async with db_pool.acquire() as conn:
        if MAIN_ADMIN_ID:
            await conn.execute(
                """
                insert into admins (telegram_id, username)
                values ($1, $2)
                on conflict (telegram_id) do nothing
                """,
                MAIN_ADMIN_ID,
                "main_admin"
            )

        await conn.execute(
            """
            create table if not exists tg_groups (
                id bigserial primary key,
                name text not null unique,
                created_by bigint,
                created_at timestamp with time zone default now()
            )
            """
        )

        await conn.execute(
            """
            alter table tg_users
            add column if not exists group_id bigint references tg_groups(id) on delete set null
            """
        )

        await conn.execute(
            """
            insert into tg_groups (name, created_by)
            values ('Без группы', 0)
            on conflict (name) do nothing
            """
        )


async def is_admin(telegram_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "select id from admins where telegram_id = $1",
            telegram_id
        )
        return row is not None


async def get_groups():
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "select id, name from tg_groups order by id asc"
        )


async def create_group(name: str, created_by: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            insert into tg_groups (name, created_by)
            values ($1, $2)
            on conflict (name) do nothing
            """,
            name,
            created_by
        )


async def rename_group(group_id: int, new_name: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            update tg_groups
            set name = $1
            where id = $2
            """,
            new_name,
            group_id
        )


async def delete_group(group_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            update tg_users
            set group_id = null
            where group_id = $1
            """,
            group_id
        )
        await conn.execute("delete from tg_groups where id = $1", group_id)


async def get_users(group_id: int | None = None):
    async with db_pool.acquire() as conn:
        if group_id is None:
            return await conn.fetch(
                """
                select tg_users.id, tg_users.username, tg_users.status, tg_users.group_id,
                       coalesce(tg_groups.name, 'Без группы') as group_name
                from tg_users
                left join tg_groups on tg_users.group_id = tg_groups.id
                order by tg_users.id asc
                """
            )

        return await conn.fetch(
            """
            select tg_users.id, tg_users.username, tg_users.status, tg_users.group_id,
                   coalesce(tg_groups.name, 'Без группы') as group_name
            from tg_users
            left join tg_groups on tg_users.group_id = tg_groups.id
            where tg_users.group_id = $1
            order by tg_users.id asc
            """,
            group_id
        )


async def add_user(username: str, added_by: int, group_id: int | None = None):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            insert into tg_users (username, status, added_by, group_id)
            values ($1, 'не проверен', $2, $3)
            on conflict (username) do update set group_id = excluded.group_id
            """,
            username,
            added_by,
            group_id
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
            [
                KeyboardButton(text="➕ Добавить Telegram"),
                KeyboardButton(text="➕ Добавить WhatsApp"),
            ],
            [
                KeyboardButton(text="📂 Группы"),
                KeyboardButton(text="➕ Создать группу"),
            ],
            [
                KeyboardButton(text="❌ Удалить контакт"),
                KeyboardButton(text="🔗 Создать ссылку доступа"),
            ],
        ],
        resize_keyboard=True
    )


def groups_keyboard(callback_prefix: str):
    async def _build():
        groups = await get_groups()
        rows = []
        for group in groups:
            rows.append([
                InlineKeyboardButton(
                    text=group["name"],
                    callback_data=f"{callback_prefix}_{group['id']}"
                )
            ])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    return _build


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


@dp.message(F.text == "📂 Группы")
async def show_groups(message: Message):
    if not await is_admin(message.from_user.id):
        return

    groups = await get_groups()

    if not groups:
        await message.answer("Групп пока нет")
        return

    for group in groups:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="👀 Посмотреть юзеров",
                        callback_data=f"show_group_{group['id']}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✏️ Переименовать",
                        callback_data=f"rename_group_{group['id']}"
                    ),
                    InlineKeyboardButton(
                        text="🗑 Удалить",
                        callback_data=f"delete_group_{group['id']}"
                    )
                ]
            ]
        )

        await message.answer(
            f"📂 Группа: {group['name']}\nID: {group['id']}",
            reply_markup=kb
        )

@dp.callback_query(F.data.startswith("rename_group_"))
async def rename_group_callback(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return

    group_id = int(callback.data.split("_")[2])

    await state.update_data(group_id=group_id)
    await callback.message.answer("Напиши новое название группы")
    await state.set_state(RenameTelegramGroup.waiting_for_new_name)
    await callback.answer()        


@dp.callback_query(F.data.startswith("delete_group_"))
async def delete_group_callback(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return

    group_id = int(callback.data.split("_")[2])

    await delete_group(group_id)

    await callback.message.edit_text("🗑 Группа удалена")
    await callback.answer("Удалено")

@dp.callback_query(F.data == "open_groups_list")
async def open_groups_list(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return

    kb = await groups_keyboard("show_group")()
    await callback.message.answer("Выбери группу:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("show_group_"))
async def show_group_users(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return

    group_id = int(callback.data.split("_")[2])
    users = await get_users(group_id=group_id)

    if not users:
        await callback.message.answer("В этой группе пока нет Telegram юзеров")
        await callback.answer()
        return

    for user in users:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть чат", url=f"https://t.me/{user['username']}")],
            [InlineKeyboardButton(text="❌ Удалить", callback_data=f"delete_tg_{user['id']}")]
        ])

        await callback.message.answer(
            f"@{user['username']}\nСтатус: {user['status']}\nГруппа: {user['group_name']}",
            reply_markup=kb
        )

    await callback.answer()


@dp.message(F.text == "➕ Создать группу")
async def create_group_button(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return

    await message.answer("Напиши название новой группы")
    await state.set_state(CreateTelegramGroup.waiting_for_group_name)


@dp.message(CreateTelegramGroup.waiting_for_group_name)
async def process_create_group(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return

    group_name = message.text.strip()

    if not group_name:
        await message.answer("Название группы пустое")
        return

    await create_group(group_name, message.from_user.id)
    await message.answer(f"✅ Группа создана: {group_name}", reply_markup=main_menu())
    await state.clear()


@dp.message(F.text == "✏️ Переименовать группу")
async def rename_group_button(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return

    groups = await get_groups()

    if not groups:
        await message.answer("Групп пока нет")
        return

    text = "Напиши ID группы, которую нужно переименовать:\n\n"
    for group in groups:
        text += f"ID {group['id']} — {group['name']}\n"

    await message.answer(text)
    await state.set_state(RenameTelegramGroup.waiting_for_group_id)


@dp.message(RenameTelegramGroup.waiting_for_group_id)
async def process_rename_group_id(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return

    if not message.text.strip().isdigit():
        await message.answer("Нужно отправить ID группы цифрой")
        return

    await state.update_data(group_id=int(message.text.strip()))
    await message.answer("Теперь напиши новое название группы")
    await state.set_state(RenameTelegramGroup.waiting_for_new_name)


@dp.message(RenameTelegramGroup.waiting_for_new_name)
async def process_rename_group_name(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return

    data = await state.get_data()
    group_id = data.get("group_id")
    new_name = message.text.strip()

    if not new_name:
        await message.answer("Название группы пустое")
        return

    await rename_group(group_id, new_name)
    await message.answer(f"✅ Группа переименована в: {new_name}", reply_markup=main_menu())
    await state.clear()


@dp.message(F.text == "➕ Добавить Telegram")
async def add_telegram_button(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return

    await message.answer("Отправь один или несколько Telegram username списком. Можно с @, без @ или ссылками t.me")
    await state.set_state(AddTelegramUser.waiting_for_username)


@dp.message(AddTelegramUser.waiting_for_username)
async def process_telegram_username(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return

    raw_text = message.text or ""

    usernames = []

    for line in raw_text.replace(",", "\n").replace(";", "\n").splitlines():
        item = line.strip()

        if not item:
            continue

        item = item.replace("https://t.me/", "")
        item = item.replace("http://t.me/", "")
        item = item.replace("t.me/", "")
        item = item.replace("@", "")
        item = item.strip().strip("/")

        if item and item not in usernames:
            usernames.append(item)

    if not usernames:
        await message.answer("Не нашёл ни одного username")
        return

    await state.update_data(usernames=usernames)

    kb = await groups_keyboard("add_to_group")()

    preview = "\n".join([f"@{u}" for u in usernames[:20]])
    extra = ""
    if len(usernames) > 20:
        extra = f"\n\nИ ещё: {len(usernames) - 20}"

    await message.answer(
        f"Нашёл username: {len(usernames)}\n\n{preview}{extra}\n\nВыбери группу:",
        reply_markup=kb
    )


@dp.callback_query(F.data.startswith("add_to_group_"))
async def add_telegram_to_group(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return

    group_id = int(callback.data.split("_")[3])
    data = await state.get_data()

    usernames = data.get("usernames", [])

    if not usernames:
        username = data.get("username")
        if username:
            usernames = [username]

    if not usernames:
        await callback.message.answer("Username не найден. Начни добавление заново")
        await state.clear()
        await callback.answer()
        return

    for username in usernames:
        await add_user(username, callback.from_user.id, group_id)

    await callback.message.answer(
        f"✅ Добавлено Telegram юзеров: {len(usernames)}",
        reply_markup=main_menu()
    )

    await state.clear()
    await callback.answer("Добавлено")


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
            [InlineKeyboardButton(text="🔗 Открыть чат", url=f"https://t.me/{user['username']}")],
            [InlineKeyboardButton(text="❌ Удалить", callback_data=f"delete_tg_{user['id']}")]
        ])

        await message.answer(
            f"@{user['username']}\nСтатус: {user['status']}\nГруппа: {user['group_name']}",
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

    usernames = [f"@{user['username']} | {user['group_name']}" for user in users]

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
            [InlineKeyboardButton(text="🔗 Открыть WhatsApp", url=f"https://wa.me/{clean_phone}")],
            [InlineKeyboardButton(text="❌ Удалить", callback_data=f"delete_wa_{number['id']}")]
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

    groups = await get_groups()
    default_group_id = groups[0]["id"] if groups else None

    await add_user(username, message.from_user.id, default_group_id)

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


async def self_ping():
    if not RENDER_URL:
        print("RENDER_URL не задан, автопинг выключен")
        return

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(RENDER_URL, timeout=20) as response:
                    print(f"Self ping: {response.status}")
        except Exception as e:
            print(f"Self ping error: {e}")

        await asyncio.sleep(300)


async def main():
    await init_db()
    asyncio.create_task(start_web_server())
    asyncio.create_task(self_ping())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
