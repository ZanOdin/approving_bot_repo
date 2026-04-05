import asyncio
import uuid
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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


# ====================== ОЧИСТКА ЧАТОВ ======================
async def clear_owner_chat():
    for msg_id in owner_message_ids.copy():
        try:
            await bot.delete_message(chat_id=OWNER_ID, message_id=msg_id)
        except TelegramBadRequest:
            pass
    owner_message_ids.clear()

async def clear_reviewer_chat(reviewer_id: int):
    for msg_id in reviewer_message_ids.get(reviewer_id, []).copy():
        try:
            await bot.delete_message(chat_id=reviewer_id, message_id=msg_id)
        except TelegramBadRequest:
            pass
    reviewer_message_ids[reviewer_id] = []

def track_reviewer_msg(reviewer_id: int, message_id: int):
    reviewer_message_ids.setdefault(reviewer_id, []).append(message_id)


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


# ====================== КЛАВИАТУРЫ ======================
def get_new_document_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Отправить новый документ", callback_data="new_document")]
    ])

def get_reviewer_keyboard(reviewers):
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


# ====================== СТАРТ ======================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await add_reviewer(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name
    )

    if message.from_user.id == OWNER_ID:
        msg = await message.answer(
            "👋 Добро пожаловать, владелец!\n\n"
            "Нажмите кнопку ниже, чтобы начать отправку документов.",
            reply_markup=get_new_document_keyboard()
        )
        owner_message_ids.append(msg.message_id)
    else:
        await message.answer(
            "✅ Вы зарегистрированы как рецензент.\nОжидайте документы на одобрение."
        )


# ====================== ВЫБОР РЕЦЕНЗЕНТА ======================
@dp.callback_query(F.data == "new_document")
async def new_document_callback(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("Только владелец может отправлять документы", show_alert=True)
        return

    reviewers = await get_all_reviewers()
    reviewers = [r for r in reviewers if r[0] != OWNER_ID]

    if not reviewers:
        await callback.answer(
            "Пока нет ни одного рецензента. Попросите людей написать боту /start",
            show_alert=True
        )
        return

    await callback.answer()
    msg = await callback.message.answer(
        "👥 Выберите рецензента из списка:",
        reply_markup=get_reviewer_keyboard(reviewers)
    )
    owner_message_ids.append(msg.message_id)


# ====================== ВЛАДЕЛЕЦ ВЫБИРАЕТ РЕЦЕНЗЕНТА ======================
@dp.callback_query(F.data.startswith("select:"))
async def select_reviewer(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        return

    reviewer_id = int(callback.data.split(":")[1])
    owner_pending_files[OWNER_ID] = {"reviewer_id": reviewer_id, "files": []}

    await callback.answer()
    msg = await callback.message.answer(
        "✅ Рецензент выбран.\n\n"
        "📎 Отправляйте файлы. К каждому можно добавить подпись прямо в Telegram "
        "(поле под файлом перед отправкой).\n\n"
        "Когда загрузите все файлы — нажмите /send"
    )
    owner_message_ids.append(msg.message_id)


# ====================== ВЛАДЕЛЕЦ ОТПРАВЛЯЕТ ФАЙЛЫ ======================
@dp.message(F.document, lambda m: m.from_user.id == OWNER_ID)
async def owner_sent_document(message: types.Message):
    owner_message_ids.append(message.message_id)

    if OWNER_ID not in owner_pending_files:
        msg = await message.answer(
            "Сначала выберите рецензента через кнопку «Отправить новый документ»"
        )
        owner_message_ids.append(msg.message_id)
        return

    file_name = message.document.file_name or "Без названия"
    file_caption = message.caption or ""

    owner_pending_files[OWNER_ID]["files"].append({
        "file_id": message.document.file_id,
        "file_name": file_name,
        "file_caption": file_caption
    })

    count = len(owner_pending_files[OWNER_ID]["files"])
    caption_info = f" (подпись: «{file_caption[:40]}»)" if file_caption else " (без подписи)"

    msg = await message.answer(
        f"📎 Добавлен: «{file_name}»{caption_info}\n"
        f"Всего файлов: {count}\n\n"
        "Отправьте ещё или /send чтобы отправить рецензенту."
    )
    owner_message_ids.append(msg.message_id)


# ====================== ОТПРАВКА ПАКЕТА РЕЦЕНЗЕНТУ ======================
@dp.message(Command("send"))
async def send_to_reviewer(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return

    owner_message_ids.append(message.message_id)

    if OWNER_ID not in owner_pending_files or not owner_pending_files[OWNER_ID]["files"]:
        msg = await message.answer(
            "Нет файлов для отправки. Сначала выберите рецензента и прикрепите файлы."
        )
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
        # Отправляем файлы рецензенту сразу, без очередей
        info_msg = await bot.send_message(
            reviewer_id,
            f"📬 Новый пакет документов · 📎 Файлов: {len(files)}"
        )
        track_reviewer_msg(reviewer_id, info_msg.message_id)

        for i, f in enumerate(files):
            caption_text = f"📄 <b>{f['file_name']}</b>"
            if f.get("file_caption"):
                caption_text += f"\n\n📝 <i>{f['file_caption']}</i>"

            sent = await bot.send_document(
                chat_id=reviewer_id,
                document=f["file_id"],
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=get_action_keyboard(approval_id, i)
            )
            pending_approvals[approval_id]["reviewer_file_msg_ids"][i] = sent.message_id
            track_reviewer_msg(reviewer_id, sent.message_id)

        msg = await message.answer(
            f"✅ Пакет из {len(files)} файл(ов) отправлен рецензенту.\n\nОжидайте ответа..."
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

    owner_caption = f"{action_label}\n👤 Рецензент: {reviewer_name}"
    if file.get("file_caption"):
        owner_caption += f"\n📝 Подпись: «{file['file_caption']}»"

    await bot.send_document(
        chat_id=OWNER_ID,
        document=file["file_id"],
        caption=owner_caption
    )
    await bot.send_message(
        OWNER_ID,
        "✅ Готово. Отправьте следующий документ.",
        reply_markup=get_new_document_keyboard()
    )

    # --- Рецензент: удаляем старое сообщение с файлом, отправляем новое ---
    old_msg_id = approval.get("reviewer_file_msg_ids", {}).get(file_index)
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

    owner_caption = f"💬 Комментарий рецензента\n👤 {reviewer_name}\n\n{message.text}"
    if file.get("file_caption"):
        owner_caption += f"\n\n📝 Подпись: «{file['file_caption']}»"

    await bot.send_document(
        chat_id=OWNER_ID,
        document=file["file_id"],
        caption=owner_caption
    )
    await bot.send_message(
        OWNER_ID,
        "✅ Готово. Отправьте следующий документ.",
        reply_markup=get_new_document_keyboard()
    )

    # --- Рецензент: удаляем старое сообщение с файлом, отправляем новое ---
    old_msg_id = approval.get("reviewer_file_msg_ids", {}).get(file_index)
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