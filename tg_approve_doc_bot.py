import asyncio
import uuid
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = "8357878054:AAH90lsvErdtDacheicT0pRPP0Sf_lw1wEg"
OWNER_ID = 29571769

bot = Bot(token=TOKEN)
dp = Dispatcher()

DB_NAME = "reviewers.db"

# Глобальные словари
pending_approvals = {}       # approval_id -> данные заявки
waiting_for_comment = {}     # reviewer_id -> approval_id
owner_pending_files = {}     # OWNER_ID -> {"reviewer_id": ..., "files": [...], "caption": ...}
waiting_for_owner_caption = {}  # OWNER_ID -> True (ждём подпись от владельца)


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
                owner_caption TEXT,
                status TEXT DEFAULT 'pending'
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS review_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                approval_id TEXT,
                file_id TEXT,
                file_name TEXT,
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

async def save_review_to_db(approval_id, reviewer_id, files, owner_caption):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            'INSERT INTO review_queue (approval_id, reviewer_id, owner_caption) VALUES (?, ?, ?)',
            (approval_id, reviewer_id, owner_caption or "")
        )
        for f in files:
            await db.execute(
                'INSERT INTO review_files (approval_id, file_id, file_name) VALUES (?, ?, ?)',
                (approval_id, f["file_id"], f["file_name"])
            )
        await db.commit()

async def get_pending_reviews_for_reviewer(reviewer_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT approval_id, owner_caption FROM review_queue WHERE reviewer_id=? AND status='pending'",
            (reviewer_id,)
        ) as cursor:
            return await cursor.fetchall()

async def get_files_for_review(approval_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT file_id, file_name FROM review_files WHERE approval_id=?",
            (approval_id,)
        ) as cursor:
            return await cursor.fetchall()

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

def get_done_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Следующий документ", callback_data="next_review")]
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
        await message.answer(
            "👋 Добро пожаловать, владелец!\n\n"
            "Нажмите кнопку ниже, чтобы отправить документ на одобрение.",
            reply_markup=get_new_document_keyboard()
        )
    else:
        pending = await get_pending_reviews_for_reviewer(message.from_user.id)
        count = len(pending)
        if count > 0:
            await message.answer(
                f"✅ Вы зарегистрированы как рецензент.\n"
                f"📋 У вас {count} непроверенных документов.\n\n"
                "Нажмите /queue чтобы начать проверку."
            )
        else:
            await message.answer("✅ Вы зарегистрированы как рецензент.\nОжидайте документы на одобрение.")


# ====================== ОЧЕРЕДЬ РЕЦЕНЗЕНТА ======================
@dp.message(Command("queue"))
async def show_queue(message: types.Message):
    if message.from_user.id == OWNER_ID:
        await message.answer("Эта команда только для рецензентов.")
        return

    reviewer_id = message.from_user.id
    pending = await get_pending_reviews_for_reviewer(reviewer_id)

    if not pending:
        await message.answer("📭 У вас нет документов на проверку.")
        return

    await message.answer(f"📋 У вас {len(pending)} документов в очереди.\nПоказываю первый:")
    await send_next_review(reviewer_id)


async def send_next_review(reviewer_id: int):
    pending = await get_pending_reviews_for_reviewer(reviewer_id)
    if not pending:
        await bot.send_message(reviewer_id, "🎉 Все документы проверены! Очередь пуста.")
        return

    approval_id, owner_caption = pending[0]
    files = await get_files_for_review(approval_id)

    # Восстанавливаем в памяти (если бот перезапускался)
    if approval_id not in pending_approvals:
        pending_approvals[approval_id] = {
            "reviewer_id": reviewer_id,
            "files": [{"file_id": f[0], "file_name": f[1]} for f in files],
            "owner_caption": owner_caption
        }

    remaining = len(pending)
    header = f"📋 Документов в очереди: {remaining}\n\n"

    if owner_caption:
        header += f"📝 <b>Комментарий от владельца:</b>\n{owner_caption}\n\n"

    header += f"📎 Файлов в пакете: {len(files)}"

    # Отправляем все файлы
    for i, (file_id, file_name) in enumerate(files):
        if i == 0:
            # К первому файлу крепим заголовок и кнопки
            await bot.send_document(
                chat_id=reviewer_id,
                document=file_id,
                caption=header,
                parse_mode="HTML",
                reply_markup=get_action_keyboard(approval_id)
            )
        else:
            await bot.send_document(
                chat_id=reviewer_id,
                document=file_id,
                caption=f"📎 Файл {i + 1}/{len(files)}: {file_name}"
            )


# ====================== ВЫБОР РЕЦЕНЗЕНТА ======================
@dp.callback_query(F.data == "new_document")
async def new_document_callback(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("Только владелец может отправлять документы", show_alert=True)
        return

    reviewers = await get_all_reviewers()
    # Убираем самого владельца из списка рецензентов
    reviewers = [r for r in reviewers if r[0] != OWNER_ID]

    if not reviewers:
        await callback.answer(
            "Пока нет ни одного рецензента. Попросите людей написать боту /start",
            show_alert=True
        )
        return

    await callback.answer()
    await callback.message.answer(
        "👥 Выберите рецензента из списка:",
        reply_markup=get_reviewer_keyboard(reviewers)
    )


# ====================== ВЛАДЕЛЕЦ ВЫБИРАЕТ РЕЦЕНЗЕНТА ======================
@dp.callback_query(F.data.startswith("select:"))
async def select_reviewer(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        return

    reviewer_id = int(callback.data.split(":")[1])
    owner_pending_files[OWNER_ID] = {"reviewer_id": reviewer_id, "files": [], "caption": None}
    waiting_for_owner_caption[OWNER_ID] = True

    await callback.answer()
    await callback.message.answer(
        f"✅ Рецензент выбран.\n\n"
        "✏️ Напишите комментарий для рецензента (или отправьте /skip чтобы пропустить):"
    )


# ====================== ВЛАДЕЛЕЦ ПИШЕТ КОММЕНТАРИЙ ======================
@dp.message(Command("skip"))
async def skip_caption(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return
    if OWNER_ID not in waiting_for_owner_caption:
        return

    waiting_for_owner_caption.pop(OWNER_ID)
    await message.answer(
        "✅ Комментарий пропущен.\n\n"
        "📎 Теперь отправьте файлы (можно несколько). Когда закончите — нажмите /send"
    )


@dp.message(F.text, lambda m: m.from_user.id == OWNER_ID and m.from_user.id in waiting_for_owner_caption)
async def owner_writes_caption(message: types.Message):
    owner_pending_files[OWNER_ID]["caption"] = message.text
    waiting_for_owner_caption.pop(OWNER_ID)
    await message.answer(
        "✅ Комментарий сохранён.\n\n"
        "📎 Теперь отправьте файлы (можно несколько). Когда закончите — нажмите /send"
    )


# ====================== ВЛАДЕЛЕЦ ОТПРАВЛЯЕТ ФАЙЛЫ ======================
@dp.message(F.document, lambda m: m.from_user.id == OWNER_ID)
async def owner_sent_document(message: types.Message):
    if OWNER_ID not in owner_pending_files:
        await message.answer("Сначала выберите рецензента через кнопку «Отправить новый документ»")
        return

    if OWNER_ID in waiting_for_owner_caption:
        await message.answer("Сначала напишите комментарий для рецензента (или /skip)")
        return

    file_name = message.document.file_name or "Без названия"
    owner_pending_files[OWNER_ID]["files"].append({
        "file_id": message.document.file_id,
        "file_name": file_name
    })

    count = len(owner_pending_files[OWNER_ID]["files"])
    await message.answer(
        f"📎 Файл «{file_name}» добавлен ({count} шт.)\n\n"
        "Отправьте ещё файлы или нажмите /send чтобы отправить рецензенту."
    )


# ====================== ОТПРАВКА ПАКЕТА РЕЦЕНЗЕНТУ ======================
@dp.message(Command("send"))
async def send_to_reviewer(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return

    if OWNER_ID not in owner_pending_files or not owner_pending_files[OWNER_ID]["files"]:
        await message.answer("Нет файлов для отправки. Сначала выберите рецензента и прикрепите файлы.")
        return

    data = owner_pending_files.pop(OWNER_ID)
    reviewer_id = data["reviewer_id"]
    files = data["files"]
    caption = data.get("caption")

    approval_id = str(uuid.uuid4())

    # Сохраняем в память и БД
    pending_approvals[approval_id] = {
        "reviewer_id": reviewer_id,
        "files": files,
        "owner_caption": caption
    }
    await save_review_to_db(approval_id, reviewer_id, files, caption)

    # Уведомляем рецензента
    pending = await get_pending_reviews_for_reviewer(reviewer_id)
    queue_size = len(pending)

    try:
        if queue_size > 1:
            # Файлы добавлены в очередь
            await bot.send_message(
                reviewer_id,
                f"📬 Вам поступил новый пакет документов ({len(files)} файл(ов)).\n"
                f"📋 Всего в очереди: {queue_size}\n\n"
                f"Нажмите /queue чтобы просмотреть все."
            )
        else:
            # Очередь была пустая — показываем сразу
            await send_next_review(reviewer_id)

        await message.answer(
            f"✅ Пакет из {len(files)} файл(ов) отправлен рецензенту.\n"
            f"В его очереди теперь {queue_size} задани(й)."
        )
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить: {e}")
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

    # Пробуем достать из памяти, если нет — из БД
    if approval_id not in pending_approvals:
        # Проверяем в БД
        files_db = await get_files_for_review(approval_id)
        if not files_db:
            await callback.answer("Заявка уже обработана или не найдена", show_alert=True)
            return
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT reviewer_id, owner_caption FROM review_queue WHERE approval_id=?",
                (approval_id,)
            ) as cursor:
                row = await cursor.fetchone()
        if not row:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        pending_approvals[approval_id] = {
            "reviewer_id": row[0],
            "files": [{"file_id": f[0], "file_name": f[1]} for f in files_db],
            "owner_caption": row[1]
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

    # Формируем сообщение владельцу
    files = approval["files"]
    action_label = "✅ Одобрено" if action == "approve" else "❌ Отклонено"
    reviewer_name = f"@{reviewer.username}" if reviewer.username else f"ID: {reviewer.id}"

    owner_text = (
        f"{action_label}\n"
        f"Рецензент: {reviewer_name}\n"
        f"📎 Файлов: {len(files)}"
    )

    # Отправляем владельцу файлы обратно с результатом
    for i, f in enumerate(files):
        if i == 0:
            await bot.send_document(
                chat_id=OWNER_ID,
                document=f["file_id"],
                caption=owner_text,
                parse_mode="HTML"
            )
        else:
            await bot.send_document(
                chat_id=OWNER_ID,
                document=f["file_id"],
                caption=f"📎 {f['file_name']}"
            )

    reviewer_text = "✅ Вы одобрили документы." if action == "approve" else "❌ Вы отклонили документы."
    await callback.message.answer(
        reviewer_text + "\n\nЕсли есть ещё документы в очереди — нажмите кнопку ниже.",
        reply_markup=get_done_keyboard()
    )

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass

    await mark_review_done(approval_id)
    pending_approvals.pop(approval_id, None)

    await bot.send_message(
        OWNER_ID,
        "✅ Работа с документом завершена. Можете отправить следующий.",
        reply_markup=get_new_document_keyboard()
    )


# ====================== ОБРАБОТКА КОММЕНТАРИЯ РЕЦЕНЗЕНТА ======================
@dp.message(F.text, lambda m: m.from_user.id in waiting_for_comment)
async def handle_comment(message: types.Message):
    reviewer = message.from_user
    approval_id = waiting_for_comment.pop(reviewer.id, None)

    if not approval_id or approval_id not in pending_approvals:
        await message.answer("Не удалось найти заявку. Возможно, она уже обработана.")
        return

    approval = pending_approvals[approval_id]
    files = approval["files"]
    reviewer_name = f"@{reviewer.username}" if reviewer.username else f"ID: {reviewer.id}"

    owner_text = (
        f"💬 <b>Комментарий рецензента</b>\n"
        f"От: {reviewer_name}\n\n"
        f"{message.text}\n\n"
        f"📎 Файлов в пакете: {len(files)}"
    )

    # Отправляем владельцу файлы + комментарий
    for i, f in enumerate(files):
        if i == 0:
            await bot.send_document(
                chat_id=OWNER_ID,
                document=f["file_id"],
                caption=owner_text,
                parse_mode="HTML"
            )
        else:
            await bot.send_document(
                chat_id=OWNER_ID,
                document=f["file_id"],
                caption=f"📎 {f['file_name']}"
            )

    await message.answer(
        "✅ Комментарий и файлы отправлены владельцу.\n\nЕсли есть ещё документы — нажмите кнопку ниже.",
        reply_markup=get_done_keyboard()
    )

    await mark_review_done(approval_id)
    pending_approvals.pop(approval_id, None)

    await bot.send_message(
        OWNER_ID,
        "✅ Работа с документом завершена. Можете отправить следующий.",
        reply_markup=get_new_document_keyboard()
    )


# ====================== ЗАПУСК ======================
async def main():
    await init_db()
    print("🚀 Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())