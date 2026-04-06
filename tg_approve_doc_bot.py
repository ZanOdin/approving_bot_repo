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
reviewer_message_ids = {}

# Текущий активный рецензент (сохраняется между сессиями в БД)
current_reviewer_id = None


# ====================== ОЧИСТКА ЧАТОВ ======================
async def clear_owner_chat():
    for msg_id in owner_message_ids.copy():
        try:
            await bot.delete_message(chat_id=OWNER_ID, message_id=msg_id)
        except TelegramBadRequest:
            pass
    owner_message_ids.clear()

def track_reviewer_msg(reviewer_id: int, message_id: int):
    reviewer_message_ids.setdefault(reviewer_id, {})[message_id] = message_id


# ====================== КЛАВИАТУРЫ ======================
def get_owner_reply_keyboard():
    """Постоянная клавиатура внизу чата для владельца"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📄 Отправить файлы"), KeyboardButton(text="👥 Сменить рецензента")]
        ],
        resize_keyboard=True,
        persistent=True
    )

def get_reviewer_reply_keyboard():
    """Постоянная клавиатура внизу чата для рецензента"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Мои задачи")]
        ],
        resize_keyboard=True,
        persistent=True
    )

def get_new_document_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Отправить следующий файл", callback_data="new_document")]
    ])

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


# ====================== ПОЛУЧИТЬ ТЕКУЩЕГО РЕЦЕНЗЕНТА ======================
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
                    reviewer_info = f"\n👤 Текущий рецензент: {fname or ''} {'@'+uname if uname else ''}"
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


# ====================== КНОПКИ REPLY-КЛАВИАТУРЫ ВЛАДЕЛЬЦА ======================
@dp.message(F.text == "📄 Отправить файлы")
async def owner_send_files_btn(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return
    reviewer_id = await get_current_reviewer()
    if not reviewer_id:
        await message.answer(
            "Сначала выберите рецензента — нажмите «👥 Сменить рецензента»"
        )
        return
    await message.answer(
        "📎 Отправляйте файлы. К каждому можно добавить подпись прямо в Telegram.\n"
        "Когда загрузите все — нажмите /send"
    )

@dp.message(F.text == "👥 Сменить рецензента")
async def owner_change_reviewer_btn(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return
    await show_reviewer_list(message)

async def show_reviewer_list(message: types.Message):
    reviewers = await get_all_reviewers()
    reviewers = [r for r in reviewers if r[0] != OWNER_ID]
    if not reviewers:
        await message.answer(
            "Пока нет ни одного рецензента. Попросите людей написать боту /start"
        )
        return
    msg = await message.answer(
        "👥 Выберите рецензента:",
        reply_markup=get_reviewer_inline_keyboard(reviewers)
    )
    owner_message_ids.append(msg.message_id)


# ====================== КНОПКИ REPLY-КЛАВИАТУРЫ РЕЦЕНЗЕНТА ======================
@dp.message(F.text == "📋 Мои задачи")
async def reviewer_tasks_btn(message: types.Message):
    if message.from_user.id == OWNER_ID:
        return
    # Показываем сколько активных заявок у рецензента
    count = sum(
        1 for a in pending_approvals.values()
        if a.get("reviewer_id") == message.from_user.id
    )
    if count:
        await message.answer(f"📋 У вас {count} активных пакет(ов) на проверку.")
    else:
        await message.answer("📭 Активных задач нет.")


# ====================== INLINE: ВЫБОР РЕЦЕНЗЕНТА ======================
@dp.callback_query(F.data == "new_document")
async def new_document_callback(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("Только владелец может отправлять документы", show_alert=True)
        return
    await callback.answer()
    owner_pending_files[OWNER_ID] = {"files": [], "reviewer_id": await get_current_reviewer()}
    await callback.message.answer(
        "📎 Отправляйте файлы. Когда закончите — /send"
    )

@dp.callback_query(F.data.startswith("select:"))
async def select_reviewer(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        return

    reviewer_id = int(callback.data.split(":")[1])
    await set_current_reviewer(reviewer_id)

    # Удаляем сообщение со списком рецензентов
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
        "📎 Теперь просто отправляйте файлы — они уйдут ему напрямую.\n"
        "Когда загрузите все файлы одного пакета — нажмите /send",
        reply_markup=get_owner_reply_keyboard()
    )


# ====================== ВЛАДЕЛЕЦ ОТПРАВЛЯЕТ ФАЙЛЫ ======================
@dp.message(F.document, lambda m: m.from_user.id == OWNER_ID)
async def owner_sent_document(message: types.Message):
    owner_message_ids.append(message.message_id)

    reviewer_id = await get_current_reviewer()
    if not reviewer_id:
        msg = await message.answer(
            "Сначала выберите рецензента — нажмите «👥 Сменить рецензента»"
        )
        owner_message_ids.append(msg.message_id)
        return

    # Инициализируем накопление файлов если ещё не начато
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
        "reviewer_file_msg_ids": {}
    }
    await save_review_to_db(approval_id, reviewer_id, files)

    try:
        for i, f in enumerate(files):
            caption_text = ""
            if f.get("file_caption"):
                caption_text = f"📝 <i>{f['file_caption']}</i>"

            sent = await bot.send_document(
                chat_id=reviewer_id,
                document=f["file_id"],
                caption=caption_text if caption_text else None,
                parse_mode="HTML",
                reply_markup=get_action_keyboard(approval_id, i)
            )
            # Сохраняем message_id каждого файла у рецензента
            pending_approvals[approval_id]["reviewer_file_msg_ids"][i] = sent.message_id

        # Чистим служебные сообщения у владельца и показываем кнопку
        await clear_owner_chat()
        msg = await message.answer(
            f"✅ Отправлено ({len(files)} файл(ов)). Ожидайте ответа.",
            reply_markup=get_new_document_keyboard()
        )
        owner_message_ids.append(msg.message_id)

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
            "reviewer_file_msg_ids": {}
        }

    approval = pending_approvals[approval_id]
    reviewer = callback.from_user

    if reviewer.id != approval.get("reviewer_id"):
        await callback.answer("Это не ваша заявка", show_alert=True)
        return

    await callback.answer()

    if action == "comment":
        waiting_for_comment[reviewer.id] = {
            "approval_id": approval_id,
            "file_index": file_index
        }
        await callback.message.answer("💬 Напишите ваш комментарий к этому файлу:")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except:
            pass
        return

    file = approval["files"][file_index]
    reviewer_name = f"@{reviewer.username}" if reviewer.username else str(reviewer.id)
    action_label = "✅ Одобрено" if action == "approve" else "❌ Отклонено"

    # --- Владелец: чистим, отправляем файл с результатом ---
    await clear_owner_chat()

    owner_caption = f"{action_label}\n👤 {reviewer_name}"
    if file.get("file_caption"):
        owner_caption += f"\n📝 {file['file_caption']}"

    await bot.send_document(
        chat_id=OWNER_ID,
        document=file["file_id"],
        caption=owner_caption
    )
    await bot.send_message(
        OWNER_ID,
        "Отправьте следующий файл или выберите действие.",
        reply_markup=get_new_document_keyboard()
    )

    # --- Рецензент: удаляем старое сообщение с файлом по сохранённому message_id ---
    old_msg_id = approval["reviewer_file_msg_ids"].get(file_index)
    if old_msg_id:
        try:
            await bot.delete_message(chat_id=reviewer.id, message_id=old_msg_id)
        except TelegramBadRequest:
            pass

    reviewer_caption = action_label
    if file.get("file_caption"):
        reviewer_caption += f"\n📝 {file['file_caption']}"

    sent = await bot.send_document(
        chat_id=reviewer.id,
        document=file["file_id"],
        caption=reviewer_caption
    )
    # Обновляем message_id на новое (финальное) сообщение
    approval["reviewer_file_msg_ids"][file_index] = sent.message_id

    await mark_review_done(approval_id)
    pending_approvals.pop(approval_id, None)


# ====================== КОММЕНТАРИЙ РЕЦЕНЗЕНТА ======================
@dp.message(F.text, lambda m: m.from_user.id in waiting_for_comment)
async def handle_comment(message: types.Message):
    reviewer = message.from_user
    data = waiting_for_comment.pop(reviewer.id, None)
    if not data:
        return

    approval_id = data["approval_id"]
    file_index = data["file_index"]

    if approval_id not in pending_approvals:
        await message.answer("Не удалось найти заявку. Возможно, она уже обработана.")
        return

    approval = pending_approvals[approval_id]
    file = approval["files"][file_index]
    reviewer_name = f"@{reviewer.username}" if reviewer.username else str(reviewer.id)

    # --- Владелец: чистим, отправляем файл + комментарий ---
    await clear_owner_chat()

    owner_caption = f"💬 {reviewer_name}:\n\n{message.text}"
    if file.get("file_caption"):
        owner_caption += f"\n\n📝 {file['file_caption']}"

    await bot.send_document(
        chat_id=OWNER_ID,
        document=file["file_id"],
        caption=owner_caption
    )
    await bot.send_message(
        OWNER_ID,
        "Отправьте следующий файл или выберите действие.",
        reply_markup=get_new_document_keyboard()
    )

    # --- Рецензент: удаляем старое сообщение с файлом ---
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

    # Удаляем текстовое сообщение с комментарием рецензента
    try:
        await bot.delete_message(chat_id=reviewer.id, message_id=message.message_id)
    except TelegramBadRequest:
        pass

    await mark_review_done(approval_id)
    pending_approvals.pop(approval_id, None)


# ====================== ЗАПУСК ======================
async def main():
    await init_db()
    print("🚀 Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())