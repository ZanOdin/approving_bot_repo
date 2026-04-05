import asyncio
import uuid
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = "8357878054:AAH90lsvErdtDacheicT0pRPP0Sf_lw1wEg"
OWNER_ID = 6669987713

bot = Bot(token=TOKEN)
dp = Dispatcher()

pending_approvals = {}      # approval_id → данные
waiting_for_comment = {}    # reviewer_id → approval_id


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
            "👋 Добро пожаловать в бот одобрения документов!\n\n"
            "Нажмите кнопку ниже, чтобы отправить новый документ.",
            reply_markup=get_new_document_keyboard()
        )
    else:
        await message.answer("✅ Вы зарегистрированы как рецензент.\nОжидайте документы на одобрение.")


# ====================== КНОПКА "Отправить новый документ" ======================
@dp.callback_query(F.data == "new_document")
async def new_document_callback(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("Эта функция доступна только владельцу", show_alert=True)
        return

    await callback.answer()
    await callback.message.answer("📄 Отправьте документ, который нужно отправить на одобрение.")


# ====================== ВЛАДЕЛЕЦ ОТПРАВЛЯЕТ ДОКУМЕНТ ======================
@dp.message(F.document, lambda m: m.from_user.id == OWNER_ID)
async def owner_sent_document(message: types.Message):
    file_id = message.document.file_id
    approval_id = str(uuid.uuid4())

    pending_approvals[approval_id] = {
        "sender_id": OWNER_ID,
        "reviewer_id": None,
        "document_file_id": file_id,
        "review_chat_id": None,
        "review_message_id": None,
        "status": "pending"   # новый статус
    }

    await message.answer(
        "📄 Документ получен.\n\nВведите chat_id рецензента:",
        reply_markup=None  # кнопка "новый документ" не показывается пока
    )


# ====================== ВЛАДЕЛЕЦ ВВОДИТ CHAT_ID ======================
@dp.message(lambda m: m.from_user.id == OWNER_ID and m.text and m.text.isdigit())
async def owner_enter_reviewer_id(message: types.Message):
    try:
        reviewer_id = int(message.text)
    except ValueError:
        await message.answer("❌ Неверный chat_id. Введите только цифры.")
        return

    if not pending_approvals:
        await message.answer("❌ Нет активных заявок.")
        return

    approval_id = list(pending_approvals.keys())[-1]
    approval = pending_approvals[approval_id]
    approval["reviewer_id"] = reviewer_id

    keyboard = get_approval_keyboard(approval_id)

    try:
        sent_msg = await bot.send_document(
            chat_id=reviewer_id,
            document=approval["document_file_id"],
            caption="📄 Документ на одобрение\n\nВыберите действие ниже 👇",
            reply_markup=keyboard
        )

        approval["review_chat_id"] = reviewer_id
        approval["review_message_id"] = sent_msg.message_id

        await message.answer(f"✅ Документ отправлен рецензенту (ID: {reviewer_id})")
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки: {e}")


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

    # Мгновенный ответ
    await callback.answer()

    if action == "approve":
        await bot.send_message(
            OWNER_ID,
            f"✅ <b>Одобрено</b>\nРецензент: @{reviewer.username or reviewer.id}",
            parse_mode="HTML"
        )
        await callback.message.edit_text("✅ <b>Документ одобрен</b>", parse_mode="HTML")
        
        # Уведомление рецензенту
        await bot.send_message(
            reviewer.id,
            "✅ Документ успешно одобрен!",
            reply_to_message_id=callback.message.message_id
        )

    elif action == "reject":
        await bot.send_message(
            OWNER_ID,
            f"❌ <b>Отклонено</b>\nРецензент: @{reviewer.username or reviewer.id}",
            parse_mode="HTML"
        )
        await callback.message.edit_text("❌ <b>Документ отклонён</b>", parse_mode="HTML")
        
        # Уведомление рецензенту
        await bot.send_message(
            reviewer.id,
            "❌ Документ успешно отклонён!",
            reply_to_message_id=callback.message.message_id
        )

    elif action == "comment":
        await callback.answer("Напишите ваш комментарий в следующем сообщении")
        waiting_for_comment[reviewer.id] = approval_id
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    # После одобрения или отклонения — помечаем как завершённый и показываем кнопку владельцу
    approval["status"] = "completed"
    pending_approvals.pop(approval_id, None)

    # Показываем владельцу кнопку "Отправить новый документ"
    try:
        await bot.send_message(
            OWNER_ID,
            "✅ Работа с документом завершена.\nМожете отправить следующий документ.",
            reply_markup=get_new_document_keyboard()
        )
    except:
        pass


# ====================== ОБРАБОТКА КОММЕНТАРИЯ ======================
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
        f"@{message.from_user.username or reviewer_id}\n\n"
        f"{message.text}",
        parse_mode="HTML"
    )

    try:
        await bot.edit_message_text(
            chat_id=approval["review_chat_id"],
            message_id=approval["review_message_id"],
            text=f"📄 Документ на одобрение\n\n💬 Комментарий отправлен:\n{message.text[:400]}..."
        )
    except:
        pass

    await message.answer("✅ Комментарий успешно отправлен владельцу.")

    # После комментария тоже считаем работу завершённой
    pending_approvals.pop(approval_id, None)

    # Показываем владельцу кнопку нового документа
    await bot.send_message(
        OWNER_ID,
        "✅ Работа с документом завершена (получен комментарий).\nМожете отправить следующий.",
        reply_markup=get_new_document_keyboard()
    )


# ====================== ЗАПУСК ======================
async def main():
    print("🚀 Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())