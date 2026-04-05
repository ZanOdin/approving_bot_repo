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

# ✅ Объявляем глобальные словари
pending_approvals = {}
waiting_for_comment = {}

# ====================== РАБОТА С БАЗОЙ ======================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reviewers (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT
            )
        ''')
        await db.commit()

async def add_reviewer(user_id: int, username: str = None, first_name: str = None):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            INSERT OR REPLACE INTO reviewers (user_id, username, first_name)
            VALUES (?, ?, ?)
        ''', (user_id, username, first_name))
        await db.commit()

async def get_all_reviewers():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, username, first_name FROM reviewers") as cursor:
            return await cursor.fetchall()


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
        await message.answer("✅ Вы успешно зарегистрированы как рецензент.\nОжидайте документы на одобрение.")


# ====================== ВЫБОР РЕЦЕНЗЕНТА ======================
@dp.callback_query(F.data == "new_document")
async def new_document_callback(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("Только владелец может отправлять документы", show_alert=True)
        return

    reviewers = await get_all_reviewers()
    if not reviewers:
        await callback.answer("Пока нет ни одного рецензента. Попросите людей написать боту /start", show_alert=True)
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

    if not hasattr(dp, "pending_document"):
        dp.pending_document = {}

    dp.pending_document[OWNER_ID] = reviewer_id

    await callback.answer()
    await callback.message.answer(
        f"✅ Рецензент выбран (ID: {reviewer_id})\n\n"
        "Теперь отправьте документ, который нужно одобрить."
    )


# ====================== ОТПРАВКА ДОКУМЕНТА ======================
@dp.message(F.document, lambda m: m.from_user.id == OWNER_ID)
async def owner_sent_document(message: types.Message):
    if not hasattr(dp, "pending_document") or OWNER_ID not in dp.pending_document:
        await message.answer("Сначала выберите рецензента через кнопку «Отправить новый документ»")
        return

    reviewer_id = dp.pending_document.pop(OWNER_ID)
    file_id = message.document.file_id
    approval_id = str(uuid.uuid4())

    # ✅ Сохраняем заявку — это было пропущено!
    pending_approvals[approval_id] = {
        "reviewer_id": reviewer_id,
        "file_id": file_id,
    }

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{approval_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{approval_id}")],
        [InlineKeyboardButton(text="💬 Комментарий", callback_data=f"comment:{approval_id}")]
    ])

    try:
        await bot.send_document(
            chat_id=reviewer_id,
            document=file_id,
            caption="📄 Документ на одобрение\n\nВыберите действие:",
            reply_markup=keyboard
        )
        await message.answer(f"✅ Документ успешно отправлен рецензенту (ID: {reviewer_id})")
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить: {e}")
        pending_approvals.pop(approval_id, None)  # Чистим если не отправилось


# ====================== ОБРАБОТКА КНОПОК ======================
@dp.callback_query(F.data.startswith(("approve:", "reject:", "comment:")))
async def process_callback(callback: types.CallbackQuery):
    action, approval_id = callback.data.split(":", 1)

    if approval_id not in pending_approvals:
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    approval = pending_approvals[approval_id]
    reviewer = callback.from_user

    if reviewer.id != approval.get("reviewer_id"):
        await callback.answer("Это не ваша заявка", show_alert=True)
        return

    await callback.answer()

    if action == "approve":
        owner_text = f"✅ <b>Одобрено</b>\nРецензент: @{reviewer.username or reviewer.id}"
        await bot.send_message(OWNER_ID, owner_text, parse_mode="HTML")
        await callback.message.answer("✅ Документ успешно одобрен!")

    elif action == "reject":
        owner_text = f"❌ <b>Отклонено</b>\nРецензент: @{reviewer.username or reviewer.id}"
        await bot.send_message(OWNER_ID, owner_text, parse_mode="HTML")
        await callback.message.answer("❌ Документ успешно отклонён!")

    elif action == "comment":
        waiting_for_comment[reviewer.id] = approval_id
        await callback.message.answer("💬 Пожалуйста, напишите ваш комментарий к документу ниже:")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except:
            pass
        return  # Не завершаем заявку — ждём комментарий

    # Убираем клавиатуру и завершаем заявку
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass

    pending_approvals.pop(approval_id, None)

    await bot.send_message(
        OWNER_ID,
        "✅ Работа с документом завершена.\nМожете отправить следующий документ.",
        reply_markup=get_new_document_keyboard()
    )


# ====================== ОБРАБОТКА КОММЕНТАРИЯ ======================
@dp.message(F.text, lambda m: m.from_user.id in waiting_for_comment)
async def handle_comment(message: types.Message):
    reviewer = message.from_user
    approval_id = waiting_for_comment.pop(reviewer.id, None)

    if not approval_id:
        return

    comment_text = (
        f"💬 <b>Комментарий рецензента</b>\n"
        f"От: @{reviewer.username or reviewer.id}\n\n"
        f"{message.text}"
    )
    await bot.send_message(OWNER_ID, comment_text, parse_mode="HTML")
    await message.answer("✅ Комментарий отправлен владельцу.")

    pending_approvals.pop(approval_id, None)

    await bot.send_message(
        OWNER_ID,
        "✅ Работа с документом завершена.\nМожете отправить следующий документ.",
        reply_markup=get_new_document_keyboard()
    )


# ====================== ЗАПУСК ======================
async def main():
    await init_db()
    print("🚀 Бот запущен с базой рецензентов...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())