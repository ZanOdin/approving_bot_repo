import asyncio
import uuid
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.exceptions import TelegramBadRequest

TOKEN = "8357878054:AAH90lsvErdtDacheicT0pRPP0Sf_lw1wEg"
OWNER_ID = 29571769

bot = Bot(token=TOKEN)
dp = Dispatcher()

DB_NAME = "reviewers.db"

pending_approvals = {}
waiting_for_comment = {}
owner_pending_files = {}
owner_message_ids = []
current_reviewer_id = None


# ====================== ОЧИСТКА ЧАТА ВЛАДЕЛЬЦА ======================
async def clear_owner_chat():
    for msg_id in owner_message_ids.copy():
        try:
            await bot.delete_message(chat_id=OWNER_ID, message_id=msg_id)
        except TelegramBadRequest:
            pass
    owner_message_ids.clear()


# ====================== БАЗА ДАННЫХ ======================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reviewers (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS review_queue (
                approval_id TEXT PRIMARY KEY,
                reviewer_id INTEGER,
                status TEXT DEFAULT 'pending'
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS review_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                approval_id TEXT,
                file_id TEXT,
                file_name TEXT,
                file_caption TEXT,
                FOREIGN KEY (approval_id) REFERENCES review_queue(approval_id)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        await db.commit()

async def add_reviewer(user_id: int, username: str = None, first_name: str = None):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            'INSERT OR REPLACE INTO reviewers (user_id, username, first_name) VALUES (?, ?, ?)',
            (user_id, username, first_name)
        )
        await db.commit()

async def get_all_reviewers():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, username, first_name FROM reviewers") as cursor:
            return await cursor.fetchall()

async def save_review_to_db(approval_id, reviewer_id, files):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            'INSERT INTO review_queue (approval_id, reviewer_id) VALUES (?, ?)',
            (approval_id, reviewer_id)
        )
        for f in files:
            await db.execute(
                'INSERT INTO review_files (approval_id, file_id, file_name, file_caption) VALUES (?, ?, ?, ?)',
                (approval_id, f["file_id"], f["file_name"], f.get("file_caption") or "")
            )
        await db.commit()

async def get_files_for_review(approval_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT file_id, file_name, file_caption FROM review_files WHERE approval_id=?",
            (approval_id,)
        ) as cursor:
            return await cursor.fetchall()

async def get_review_info(approval_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT reviewer_id FROM review_queue WHERE approval_id=?",
            (approval_id,)
        ) as cursor:
            return await cursor.fetchone()

async def mark_review_done(approval_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE review_queue SET status='done' WHERE approval_id=?",
            (approval_id,)
        )
        await db.commit()

async def get_setting(key: str):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
        await db.commit()


# ====================== ТЕКУЩИЙ РЕЦЕНЗЕНТ ======================
async def get_current_reviewer():
    global current_reviewer_id
    if current_reviewer_id:
        return current_reviewer_id
    saved = await get_setting("current_reviewer_id")
    if saved:
        current_reviewer_id = int(saved)
    return current_reviewer_id

async def set_current_reviewer(reviewer_id: int):
    global current_reviewer_id
    current_reviewer_id = reviewer_id
    await set_setting("current_reviewer_id", str(reviewer_id))


# ====================== КЛАВИАТУРЫ ======================
def get_owner_reply_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📄 Отправить файлы"), KeyboardButton(text="👥 Сменить рецензента")]
        ],
        resize_keyboard=True,
        persistent=True
    )

def get_reviewer_reply_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📂 Полученные документы")]
        ],
        resize_keyboard=True,
        persistent=True
    )

def get_reviewer_inline_keyboard(reviewers):
    keyboard = []
    for user_id, username, first_name in reviewers:
        name = f"{first_name or ''} @{username}" if username else f"{first_name or 'Пользователь'} (ID: {user_id})"
        keyboard.append([InlineKeyboardButton(text=name[:30], callback_data=f"select:{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_action_keyboard(approval_id, file_index):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{approval_id}:{file_index}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{approval_id}:{file_index}")],
        [InlineKeyboardButton(text="💬 Комментарий", callback_data=f"comment:{approval_id}:{file_index}")]
    ])


# ====================== ПЕРЕГЕНЕРАЦИЯ ДОКУМЕНТОВ У РЕЦЕНЗЕНТА ======================
async def regenerate_reviewer_docs(reviewer_id: int):
    active = {
        aid: data for aid, data in pending_approvals.items()
        if data.get("reviewer_id") == reviewer_id
    }

    if not active:
        await bot.send_message(reviewer_id, "📭 Активных документов нет.")
        return

    for approval_id, data in active.items():
        for file_index, msg_id in list(data.get("reviewer_file_msg_ids", {}).items()):
            try:
                await bot.delete_message(chat_id=reviewer_id, message_id=msg_id)
            except TelegramBadRequest:
                pass
        data["reviewer_file_msg_ids"] = {}

    total_packs = len(active)
    for pack_num, (approval_id, data) in enumerate(active.items(), 1):
        files = data["files"]
        done_files = data.get("done_files", set())

        if total_packs > 1:
            await bot.send_message(
                reviewer_id,
                f"📦 Пакет {pack_num} из {total_packs} · {len(files)} файл(ов)"
            )

        for i, f in enumerate(files):
            if i in done_files:
                continue  # Уже обработанные не показываем

            caption_text = None
            if f.get("file_caption"):
                caption_text = f"📝 <i>{f['file_caption']}</i>"

            sent = await bot.send_document(
                chat_id=reviewer_id,
                document=f["file_id"],
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=get_action_keyboard(approval_id, i)
            )
            data["reviewer_file_msg_ids"][i] = sent.message_id


# ====================== СТАРТ ======================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await add_reviewer(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name
    )

    if message.from_user.id == OWNER_ID:
        reviewer_id = await get_current_reviewer()
        reviewer_info = ""
        if reviewer_id:
            reviewers = await get_all_reviewers()
            for uid, uname, fname in reviewers:
                if uid == reviewer_id:
                    reviewer_info = f"\n👤 Текущий рецензент: {fname or ''} {'@'+uname if uname else ''}".strip()
                    break

        await message.answer(
            f"👋 Добро пожаловать, владелец!{reviewer_info}\n\n"
            "Просто отправьте файл — он уйдёт текущему рецензенту.\n"
            "Для смены рецензента нажмите «👥 Сменить рецензента».",
            reply_markup=get_owner_reply_keyboard()
        )
    else:
        await message.answer(
            "✅ Вы зарегистрированы как рецензент.\nОжидайте документы на одобрение.",
            reply_markup=get_reviewer_reply_keyboard()
        )


# ====================== КНОПКИ ВЛАДЕЛЬЦА ======================
@dp.message(F.text == "📄 Отправить файлы")
async def owner_send_files_btn(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return
    reviewer_id = await get_current_reviewer()
    if not reviewer_id:
        await message.answer("Сначала выберите рецензента — нажмите «👥 Сменить рецензента»")
        return
    msg = await message.answer(
        "📎 Отправляйте файлы. К каждому можно добавить подпись.\nКогда закончите — /send"
    )
    owner_message_ids.append(msg.message_id)

@dp.message(F.text == "👥 Сменить рецензента")
async def owner_change_reviewer_btn(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return
    reviewers = await get_all_reviewers()
    reviewers = [r for r in reviewers if r[0] != OWNER_ID]
    if not reviewers:
        await message.answer("Пока нет ни одного рецензента. Попросите людей написать боту /start")
        return
    msg = await message.answer("👥 Выберите рецензента:", reply_markup=get_reviewer_inline_keyboard(reviewers))
    owner_message_ids.append(msg.message_id)


# ====================== КНОПКА РЕЦЕНЗЕНТА ======================
@dp.message(F.text == "📂 Полученные документы")
async def reviewer_docs_btn(message: types.Message):
    if message.from_user.id == OWNER_ID:
        return
    await regenerate_reviewer_docs(message.from_user.id)


# ====================== ВЫБОР РЕЦЕНЗЕНТА ======================
@dp.callback_query(F.data.startswith("select:"))
async def select_reviewer(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        return

    reviewer_id = int(callback.data.split(":")[1])
    await set_current_reviewer(reviewer_id)

    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    if callback.message.message_id in owner_message_ids:
        owner_message_ids.remove(callback.message.message_id)

    reviewers = await get_all_reviewers()
    name = str(reviewer_id)
    for uid, uname, fname in reviewers:
        if uid == reviewer_id:
            name = f"{fname or ''} {'@'+uname if uname else ''}".strip()
            break

    await callback.answer()
    await callback.message.answer(
        f"✅ Рецензент выбран: {name}\n\n"
        "Просто отправляйте файлы — они уйдут ему напрямую.\n"
        "Когда загрузите все файлы одного пакета — /send",
        reply_markup=get_owner_reply_keyboard()
    )


# ====================== ВЛАДЕЛЕЦ ОТПРАВЛЯЕТ ФАЙЛЫ ======================
@dp.message(F.document, lambda m: m.from_user.id == OWNER_ID)
async def owner_sent_document(message: types.Message):
    owner_message_ids.append(message.message_id)

    reviewer_id = await get_current_reviewer()
    if not reviewer_id:
        msg = await message.answer("Сначала выберите рецензента — нажмите «👥 Сменить рецензента»")
        owner_message_ids.append(msg.message_id)
        return

    if OWNER_ID not in owner_pending_files:
        owner_pending_files[OWNER_ID] = {"reviewer_id": reviewer_id, "files": []}

    file_caption = message.caption or ""
    owner_pending_files[OWNER_ID]["files"].append({
        "file_id": message.document.file_id,
        "file_name": message.document.file_name or "Без названия",
        "file_caption": file_caption
    })

    count = len(owner_pending_files[OWNER_ID]["files"])
    caption_info = f" · подпись: «{file_caption[:40]}»" if file_caption else ""

    msg = await message.answer(
        f"📎 Файл добавлен{caption_info} · всего: {count}\n"
        "Отправьте ещё или /send чтобы отправить рецензенту."
    )
    owner_message_ids.append(msg.message_id)


# ====================== ОТПРАВКА ПАКЕТА ======================
@dp.message(Command("send"))
async def send_to_reviewer(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return

    owner_message_ids.append(message.message_id)

    if OWNER_ID not in owner_pending_files or not owner_pending_files[OWNER_ID]["files"]:
        msg = await message.answer("Нет файлов для отправки.")
        owner_message_ids.append(msg.message_id)
        return

    data = owner_pending_files.pop(OWNER_ID)
    reviewer_id = data["reviewer_id"]
    files = data["files"]
    approval_id = str(uuid.uuid4())

    pending_approvals[approval_id] = {
        "reviewer_id": reviewer_id,
        "files": files,
        "reviewer_file_msg_ids": {},
        "done_files": set()
    }
    await save_review_to_db(approval_id, reviewer_id, files)

    try:
        for i, f in enumerate(files):
            caption_text = None
            if f.get("file_caption"):
                caption_text = f"📝 <i>{f['file_caption']}</i>"

            sent = await bot.send_document(
                chat_id=reviewer_id,
                document=f["file_id"],
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=get_action_keyboard(approval_id, i)
            )
            pending_approvals[approval_id]["reviewer_file_msg_ids"][i] = sent.message_id

        await clear_owner_chat()

    except Exception as e:
        msg = await message.answer(f"❌ Не удалось отправить: {e}")
        owner_message_ids.append(msg.message_id)
        pending_approvals.pop(approval_id, None)
        await mark_review_done(approval_id)


# ====================== ОБРАБОТКА КНОПОК РЕЦЕНЗЕНТА ======================
@dp.callback_query(F.data.startswith(("approve:", "reject:", "comment:")))
async def process_callback(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    action = parts[0]
    approval_id = parts[1]
    file_index = int(parts[2])

    if approval_id not in pending_approvals:
        files_db = await get_files_for_review(approval_id)
        info = await get_review_info(approval_id)
        if not files_db or not info:
            await callback.answer("Заявка уже обработана или не найдена", show_alert=True)
            return
        pending_approvals[approval_id] = {
            "reviewer_id": info[0],
            "files": [
                {"file_id": f[0], "file_name": f[1], "file_caption": f[2]}
                for f in files_db
            ],
            "reviewer_file_msg_ids": {},
            "done_files": set()
        }

    approval = pending_approvals[approval_id]
    reviewer = callback.from_user

    if reviewer.id != approval.get("reviewer_id"):
        await callback.answer("Это не ваша заявка", show_alert=True)
        return

    await callback.answer()

    if action == "comment":
        # Запоминаем ожидание комментария
        waiting_for_comment[reviewer.id] = {
            "approval_id": approval_id,
            "file_index": file_index
        }
        # Убираем только кнопки — сообщение с файлом НЕ трогаем
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await bot.send_message(reviewer.id, "💬 Напишите ваш комментарий к этому файлу:")
        return

    await _finalize_file(approval_id, approval, file_index, action, reviewer)


async def _finalize_file(approval_id, approval, file_index, action, reviewer):
    """Финализируем ответ по конкретному файлу"""
    file = approval["files"][file_index]
    reviewer_name = f"@{reviewer.username}" if reviewer.username else str(reviewer.id)
    action_label = "✅ Одобрено" if action == "approve" else "❌ Отклонено"

    # --- Владелец ---
    await clear_owner_chat()

    owner_caption = f"{action_label}\n👤 {reviewer_name}"
    if file.get("file_caption"):
        owner_caption += f"\n📝 {file['file_caption']}"

    await bot.send_document(
        chat_id=OWNER_ID,
        document=file["file_id"],
        caption=owner_caption
    )

    # --- Рецензент: удаляем старое сообщение с файлом, шлём новое ---
    old_msg_id = approval["reviewer_file_msg_ids"].get(file_index)
    if old_msg_id:
        try:
            await bot.delete_message(chat_id=reviewer.id, message_id=old_msg_id)
        except TelegramBadRequest:
            pass

    reviewer_caption = action_label
    if file.get("file_caption"):
        reviewer_caption += f"\n📝 {file['file_caption']}"

    await bot.send_document(
        chat_id=reviewer.id,
        document=file["file_id"],
        caption=reviewer_caption
    )

    # Помечаем файл готовым
    approval["done_files"].add(file_index)
    if len(approval["done_files"]) >= len(approval["files"]):
        await mark_review_done(approval_id)
        pending_approvals.pop(approval_id, None)


# ====================== КОММЕНТАРИЙ РЕЦЕНЗЕНТА ======================
# Этот хендлер должен быть ВЫШЕ общих текстовых хендлеров
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_any_text(message: types.Message):
    reviewer = message.from_user

    # Сначала проверяем — ждём ли комментарий от этого пользователя
    if reviewer.id in waiting_for_comment:
        await _handle_comment(message)
        return

    # Иначе — кнопки reply-клавиатуры обработаются своими хендлерами выше,
    # остальное игнорируем


async def _handle_comment(message: types.Message):
    reviewer = message.from_user
    data = waiting_for_comment.pop(reviewer.id)

    approval_id = data["approval_id"]
    file_index = data["file_index"]

    if approval_id not in pending_approvals:
        await message.answer("Не удалось найти заявку. Возможно, она уже обработана.")
        return

    approval = pending_approvals[approval_id]
    file = approval["files"][file_index]
    reviewer_name = f"@{reviewer.username}" if reviewer.username else str(reviewer.id)

    # --- Владелец ---
    await clear_owner_chat()

    owner_caption = f"💬 {reviewer_name}:\n\n{message.text}"
    if file.get("file_caption"):
        owner_caption += f"\n\n📝 {file['file_caption']}"

    await bot.send_document(
        chat_id=OWNER_ID,
        document=file["file_id"],
        caption=owner_caption
    )

    # --- Рецензент: удаляем сообщение с файлом (кнопки уже убраны), шлём новое ---
    old_msg_id = approval["reviewer_file_msg_ids"].get(file_index)
    if old_msg_id:
        try:
            await bot.delete_message(chat_id=reviewer.id, message_id=old_msg_id)
        except TelegramBadRequest:
            pass

    reviewer_caption = f"💬 Комментарий отправлен\n\n{message.text}"
    if file.get("file_caption"):
        reviewer_caption += f"\n\n📝 {file['file_caption']}"

    await bot.send_document(
        chat_id=reviewer.id,
        document=file["file_id"],
        caption=reviewer_caption
    )

    # Удаляем текстовое сообщение комментария
    try:
        await bot.delete_message(chat_id=reviewer.id, message_id=message.message_id)
    except TelegramBadRequest:
        pass

    # Помечаем файл готовым
    approval["done_files"].add(file_index)
    if len(approval["done_files"]) >= len(approval["files"]):
        await mark_review_done(approval_id)
        pending_approvals.pop(approval_id, None)


# ====================== ЗАПУСК ======================
async def main():
    await init_db()
    print("🚀 Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())