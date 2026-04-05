import asyncio
import uuid
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = "8357878054:AAH90lsvErdtDacheicT0pRPP0Sf_lw1wEg"
OWNER_ID = 6669987713

bot = Bot(token=TOKEN)
dp = Dispatcher()

pending_approvals = {}
waiting_for_comment = {}


def get_approval_keyboard(approval_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve:{approval_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{approval_id}")],
        [InlineKeyboardButton(text="💬 Оставить комментарий", callback_data=f"comment:{approval_id}")]
    ])


def get_new_document_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Отправить новый документ", callback_data="new_document")]
    ])


# ====================== СТАРТ ======================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == OWNER_ID:
        await message.answer(
            "👋 Добро пожаловать!\n\nНажмите кнопку ниже, чтобы отправить документ на одобрение.",
            reply_markup=get_new_document_keyboard()
        )
    else:
        await message.answer("✅ Вы рецензент. Ожидайте документы.")


# ====================== НОВЫЙ ДОКУМЕНТ ======================
@dp.callback_query(F.data == "new_document")
async def new_document_callback(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("Доступно только владельцу", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer("📄 Отправьте документ для одобрения.")


# ====================== ОТПРАВКА ДОКУМЕНТА ======================
@dp.message(F.document, lambda m: m.from_user.id == OWNER_ID)
async def owner_sent_document(message: types.Message):
    file_id = message.document.file_id
    approval_id = str(uuid.uuid4())

    pending_approvals[approval_id] = {
        "reviewer_id": None,
        "document_file_id": file_id,
        "review_chat_id": None,
        "review_message_id": None,
    }

    await message.answer("📄 Документ получен.\n\nВведите chat_id рецензента:")


# ====================== ВВОД CHAT_ID ======================
@dp.message(lambda m: m.from_user.id == OWNER_ID and m.text and m.text.isdigit())
async def owner_enter_reviewer_id(message: types.Message):
    reviewer_id = int(message.text)

    if not pending_approvals:
        await message.answer("❌ Нет активных заявок.")
        return

    approval_id = list(pending_approvals.keys())[-1]
    approval = pending_approvals[approval_id]
    approval["reviewer_id"] = reviewer_id

    keyboard = get_approval_keyboard(approval_id)

    sent_msg = await bot.send_document(
        chat_id=reviewer_id,
        document=approval["document_file_id"],
        caption="📄 Документ на одобрение\n\nВыберите действие ниже 👇",
        reply_markup=keyboard
    )

    approval["review_chat_id"] = reviewer_id
    approval["review_message_id"] = sent_msg.message_id

    await message.answer(f"✅ Документ отправлен рецензенту (ID: {reviewer_id})")


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

    await callback.answer()   # мгновенный ответ

    if action == "approve":
        owner_text = f"✅ <b>Одобрено</b>\nРецензент: @{reviewer.username or reviewer.id}"
        reviewer_text = "✅ Документ успешно одобрен!"

    elif action == "reject":
        owner_text = f"❌ <b>Отклонено</b>\nРецензент: @{reviewer.username or reviewer.id}"
        reviewer_text = "❌ Документ успешно отклонён!"

    else:  # comment
        await callback.answer("Напишите комментарий в следующем сообщении")
        waiting_for_comment[reviewer.id] = approval_id
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    # Отправляем результат владельцу
    await bot.send_message(OWNER_ID, owner_text, parse_mode="HTML")

    # Уведомление рецензенту
    await bot.send_message(
        chat_id=reviewer.id,
        text=reviewer_text,
        reply_to_message_id=callback.message.message_id
    )

    # Удаляем клавиатуру с исходного сообщения
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass

    # Завершаем заявку и показываем кнопку владельцу
    pending_approvals.pop(approval_id, None)

    await bot.send_message(
        OWNER_ID,
        "✅ Работа с документом завершена.\nМожете отправить следующий документ.",
        reply_markup=get_new_document_keyboard()
    )


# ====================== КОММЕНТАРИЙ ======================
@dp.message(lambda m: m.from_user.id in waiting_for_comment)
async def handle_comment(message: types.Message):
    reviewer_id = message.from_user.id
    approval_id = waiting_for_comment.pop(reviewer_id, None)

    if not approval_id or approval_id not in pending_approvals:
        return

    approval = pending_approvals[approval_id]

    await bot.send_message(
        OWNER_ID,
        f"💬 <b>Комментарий от рецензента</b>\n"
        f"@{message.from_user.username or reviewer_id}\n\n{message.text}",
        parse_mode="HTML"
    )

    await message.answer("✅ Комментарий отправлен владельцу.")

    # Уведомляем владельца, что можно отправлять новый документ
    pending_approvals.pop(approval_id, None)
    await bot.send_message(
        OWNER_ID,
        "✅ Получен комментарий.\nМожете отправить следующий документ.",
        reply_markup=get_new_document_keyboard()
    )


# ====================== ЗАПУСК ======================
async def main():
    print("🚀 Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())