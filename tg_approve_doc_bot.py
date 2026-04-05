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
owner_message_ids = []  # message_id всех служебных сообщений владельцу


# ====================== ОЧИСТКА ЧАТА ВЛАДЕЛЬЦА ======================
async def clear_owner_chat():
    """Удаляем все служебные сообщения в чате с владельцем"""
    for msg_id in owner_message_ids.copy():
        try:
            await bot.delete_message(chat_id=OWNER_ID, message_id=msg_id)
        except TelegramBadRequest:
            pass
    owner_message_ids.clear()

async def send_owner(text: str, reply_markup=None, parse_mode=None) -> types.Message:
    """Отправляем сообщение владельцу и запоминаем его для удаления"""
    msg = await bot.send_message(
        chat_id=OWNER_ID,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode
    )
    owner_message_ids.append(msg.message_id)
    return msg


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

async def get_pending_reviews_for_reviewer(reviewer_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT approval_id FROM review_queue WHERE reviewer_id=? AND status='pending'",
            (reviewer_id,)
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]

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

def get_action_keyboard(approval_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{approval_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{approval_id}")],
        [InlineKeyboardButton(text="💬 Комментарий", callback_data=f"comment:{approval_id}")]
    ])

def get_next_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Следующий документ", callback_data="next_review")]
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
        pending = await get_pending_reviews_for_reviewer(message.from_user.id)
        if pending:
            await message.answer(
                f"✅ Вы зарегистрированы как рецензент.\n"
                f"📋 У вас {len(pending)} непроверенных пакет(ов).\n\n"
                "Нажмите /queue чтобы начать проверку."
            )
        else:
            await message.answer(
                "✅ Вы зарегистрированы как рецензент.\nОжидайте документы на одобрение."
            )


# ====================== ОЧЕРЕДЬ РЕЦЕНЗЕНТА ======================
@dp.message(Command("queue"))
async def show_queue(message: types.Message):
    if message.from_user.id == OWNER_ID:
        await message.answer("Эта команда только для рецензентов.")
        return

    pending = await get_pending_reviews_for_reviewer(message.from_user.id)
    if not pending:
        await message.answer("📭 У вас нет документов на проверку.")
        return

    await message.answer(f"📋 В очереди: {len(pending)} пакет(ов). Показываю первый:")
    await send_next_review(message.from_user.id)


async def send_next_review(reviewer_id: int):
    pending = await get_pending_reviews_for_reviewer(reviewer_id)
    if not pending:
        await bot.send_message(reviewer_id, "🎉 Очередь пуста! Все документы проверены.")
        return

    approval_id = pending[0]
    files = await get_files_for_review(approval_id)

    if approval_id not in pending_approvals:
        pending_approvals[approval_id] = {
            "reviewer_id": reviewer_id,
            "files": [
                {"file_id": f[0], "file_name": f[1], "file_caption": f[2]}
                for f in files
            ]
        }

    remaining = len(pending)
    await bot.send_message(
        reviewer_id,
        f"📋 Пакет документов ({remaining} в очереди)\n📎 Файлов в пакете: {len(files)}"
    )

    for i, (file_id, file_name, file_caption) in enumerate(files):
        caption_text = f"📄 <b>{file_name}</b>"
        if file_caption:
            caption_text += f"\n\n📝 <i>{file_caption}</i>"

        if i == len(files) - 1:
            await bot.send_document(
                chat_id=reviewer_id,
                document=file_id,
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=get_action_keyboard(approval_id)
            )
        else:
            await bot.send_document(
                chat_id=reviewer_id,
                document=file_id,
                caption=caption_text,
                parse_mode="HTML"
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
    # Само сообщение с файлом тоже запоминаем
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
        "Отправьте ещё файлы или /send чтобы отправить рецензенту."
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
        "files": files
    }
    await save_review_to_db(approval_id, reviewer_id, files)

    pending = await get_pending_reviews_for_reviewer(reviewer_id)
    queue_size = len(pending)

    try:
        if queue_size > 1:
            await bot.send_message(
                reviewer_id,
                f"📬 Новый пакет документов ({len(files)} файл(ов)).\n"
                f"📋 Всего в очереди: {queue_size}\n\n"
                "Нажмите /queue чтобы просмотреть."
            )
        else:
            await send_next_review(reviewer_id)

        msg = await message.answer(
            f"✅ Пакет из {len(files)} файл(ов) отправлен рецензенту.\n"
            f"В его очереди теперь {queue_size} пакет(ов).\n\n"
            "Ожидайте ответа..."
        )
        owner_message_ids.append(msg.message_id)

    except Exception as e:
        msg = await message.answer(f"❌ Не удалось отправить: {e}")
        owner_message_ids.append(msg.message_id)
        pending_approvals.pop(approval_id, None)
        await mark_review_done(approval_id)


# ====================== СЛЕДУЮЩИЙ ДОКУМЕНТ В ОЧЕРЕДИ ======================
@dp.callback_query(F.data == "next_review")
async def next_review_callback(callback: types.CallbackQuery):
    await callback.answer()
    await send_next_review(callback.from_user.id)


# ====================== ОБРАБОТКА КНОПОК РЕЦЕНЗЕНТА ======================
@dp.callback_query(F.data.startswith(("approve:", "reject:", "comment:")))
async def process_callback(callback: types.CallbackQuery):
    action, approval_id = callback.data.split(":", 1)

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
            ]
        }

    approval = pending_approvals[approval_id]
    reviewer = callback.from_user

    if reviewer.id != approval.get("reviewer_id"):
        await callback.answer("Это не ваша заявка", show_alert=True)
        return

    await callback.answer()

    if action == "comment":
        waiting_for_comment[reviewer.id] = approval_id
        await callback.message.answer("💬 Напишите ваш комментарий к документам:")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except:
            pass
        return

    # Чистим весь служебный мусор в чате владельца
    await clear_owner_chat()

    files = approval["files"]
    reviewer_name = f"@{reviewer.username}" if reviewer.username else str(reviewer.id)
    action_label = "✅ Одобрено" if action == "approve" else "❌ Отклонено"

    # Отправляем файлы обратно владельцу — это финальные сообщения, не трекаем
    for i, f in enumerate(files):
        caption_text = f"{action_label}\n👤 Рецензент: {reviewer_name}\n📄 {f['file_name']}"
        if f.get("file_caption"):
            caption_text += f"\n📝 Ваша подпись: «{f['file_caption']}»"

        await bot.send_document(
            chat_id=OWNER_ID,
            document=f["file_id"],
            caption=caption_text
        )

    # Кнопка следующего — тоже финальная, не трекаем
    await bot.send_message(
        OWNER_ID,
        "✅ Готово. Отправьте следующий документ.",
        reply_markup=get_new_document_keyboard()
    )

    reviewer_reply = "✅ Вы одобрили документы." if action == "approve" else "❌ Вы отклонили документы."
    await callback.message.answer(reviewer_reply, reply_markup=get_next_keyboard())

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass

    await mark_review_done(approval_id)
    pending_approvals.pop(approval_id, None)


# ====================== КОММЕНТАРИЙ РЕЦЕНЗЕНТА ======================
@dp.message(F.text, lambda m: m.from_user.id in waiting_for_comment)
async def handle_comment(message: types.Message):
    reviewer = message.from_user
    approval_id = waiting_for_comment.pop(reviewer.id, None)

    if not approval_id or approval_id not in pending_approvals:
        await message.answer("Не удалось найти заявку. Возможно, она уже обработана.")
        return

    approval = pending_approvals[approval_id]
    files = approval["files"]
    reviewer_name = f"@{reviewer.username}" if reviewer.username else str(reviewer.id)

    # Чистим служебные сообщения владельца
    await clear_owner_chat()

    # Отправляем файлы + комментарий владельцу — финальные, не трекаем
    for i, f in enumerate(files):
        caption_text = (
            f"💬 Комментарий рецензента\n"
            f"👤 {reviewer_name}\n"
            f"📄 {f['file_name']}"
        )
        if f.get("file_caption"):
            caption_text += f"\n📝 Ваша подпись: «{f['file_caption']}»"
        if i == 0:
            caption_text += f"\n\n💬 {message.text}"

        await bot.send_document(
            chat_id=OWNER_ID,
            document=f["file_id"],
            caption=caption_text
        )

    await bot.send_message(
        OWNER_ID,
        "✅ Готово. Отправьте следующий документ.",
        reply_markup=get_new_document_keyboard()
    )

    await message.answer("✅ Комментарий отправлен владельцу.", reply_markup=get_next_keyboard())

    await mark_review_done(approval_id)
    pending_approvals.pop(approval_id, None)


# ====================== ЗАПУСК ======================
async def main():
    await init_db()
    print("🚀 Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())