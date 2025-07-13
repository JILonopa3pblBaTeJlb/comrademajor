import asyncio
import g4f
from g4f.client import Client
import json
import uuid
import os
import random
import time
import logging
from collections import deque
from typing import Dict
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile

BOT_TOKEN = "ВАШ:ТОКЕН"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARTICLES = ["148", "205.2", "207.3", "230", "280", "280.1", "282", "298.1", "319", "354.1"]
DENIALS_FILE = os.path.join(BASE_DIR, "denials.txt")
user_queue = deque()
user_busy: Dict[int, bool] = {}

logging.basicConfig(level=logging.INFO)
provider_failures = {}

PROVIDER_MODELS = {
    "OpenaiChat": ["gpt-4o-mini", "gpt-4o"],
    "Bing": ["gpt-4"],
    "Chatgpt4o": ["gpt-4o"],
    "Perplexity": ["sonar-medium-online"]
}

async def handle_with_queue(handler_func, message: Message, bot: Bot):
    user_id = message.from_user.id
    if user_id in user_busy:
        return
    if user_id in user_queue:
        await message.answer("Ожидайте гражданин, ваш запрос уже в очереди на обработку.")
        return
    user_queue.append(user_id)
    while user_queue[0] != user_id:
        await asyncio.sleep(1)
    user_busy[user_id] = True
    try:
        await handler_func(message, bot)
    finally:
        user_queue.popleft()
        user_busy.pop(user_id, None)

def read_file(file_path):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return f"Ошибка: Файл {file_path} не найден."
    except Exception as e:
        return f"Ошибка при чтении файла {file_path}: {str(e)}"

def get_denials():
    try:
        with open(DENIALS_FILE, "r", encoding="utf-8") as f:
            return [line.strip().lower() for line in f if line.strip()]
    except FileNotFoundError:
        return []
    except Exception as e:
        return []

def is_cyrillic(text):
    return any('\u0400' <= char <= '\u04FF' for char in text)

def is_denial_response(response):
    denials = get_denials()
    if not is_cyrillic(response):
        return True
    return any(phrase in response.lower() for phrase in denials)

def load_articles():
    json_file = os.path.join(BASE_DIR, "articles.json")
    try:
        with open(json_file, "r", encoding="utf-8") as f:
            articles = json.load(f)
            return articles
    except FileNotFoundError:
        return {"error": f"Файл {json_file} не найден."}
    except Exception as e:
        return {"error": f"Ошибка при чтении {json_file}: {str(e)}"}

async def call_g4f_model(prompt: str) -> str:
    try:
        client = Client()
        for _ in range(10):
            now = time.time()
            active_providers = [
                p for p in PROVIDER_MODELS.keys()
                if PROVIDER_MODELS[p] and (p not in provider_failures or now - provider_failures[p] > 600)
            ]
            if not active_providers:
                provider_failures.clear()
                active_providers = list(PROVIDER_MODELS.keys())
            selected_provider = random.choice(active_providers)
            available_models = PROVIDER_MODELS[selected_provider].copy()
            random.shuffle(available_models)
            for selected_model in available_models:
                try:
                    start_time = time.time()
                    response = client.chat.completions.create(
                        model=selected_model,
                        provider=getattr(g4f.Provider, selected_provider, None),
                        messages=[{"role": "user", "content": prompt}],
                        stream=False,
                        timeout=60
                    )
                    if asyncio.iscoroutine(response):
                        response = await asyncio.wait_for(response, timeout=60)
                    if response is None or not hasattr(response, 'choices') or not response.choices:
                        continue
                    response_content = response.choices[0].message.content.strip()
                    if is_denial_response(response_content):
                        continue
                    if time.time() - start_time > 55:
                        continue
                    return response_content
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    provider_failures[selected_provider] = time.time()
                    continue
        return "Ошибка: Не удалось получить ответ от провайдеров."
    except Exception as e:
        return f"Ошибка: {str(e)}"

async def analyze_article(client, text, article_number, article_content, prompt_template):
    try:
        if article_content.startswith("Ошибка"):
            return f"Article {article_number}:\n{article_content}\n"
        prompt = prompt_template.format(
            article_content=article_content,
            user_input=text
        )
        result = await call_g4f_model(prompt)
        return f"Article {article_number}:\n{result}\n"
    except Exception as e:
        return f"Article {article_number}:\nОшибка: {str(e)}\n"

async def process_report_with_prompt2(report: str) -> str:
    prompt2_file = os.path.join(BASE_DIR, "prompt2.txt")
    prompt2_template = read_file(prompt2_file)
    if "Ошибка" in prompt2_template:
        return prompt2_template
    prompt = prompt2_template.format(report_content=report)
    result = await call_g4f_model(prompt)
    return result

def clean_report(report: str) -> str:
    lines = report.split('\n')
    cleaned_lines = [line for line in lines if line.strip() not in ["```markdown", "```"]]
    return '\n'.join(cleaned_lines)

async def analyze_text(text, prompt_template, articles, message: Message, bot: Bot):
    if len(text) > 2000:
        await message.reply("Извините, мы не принимаем тексты длиннее 2000 символов.")
        return None
    progress_message = await message.reply("Производится лингвистическая экспертиза: [          ] 0%")
    client = Client()
    results = []
    total_articles = len([a for a in articles if not articles[a].startswith("Ошибка")])
    completed = 0
    for article_number, article_content in articles.items():
        if article_content.startswith("Ошибка"):
            completed += 1
        else:
            result = await analyze_article(client, text, article_number, article_content, prompt_template)
            if "Applicability: Yes" in result:
                results.append(result)
            completed += 1
            progress = int((completed / total_articles) * 100)
            filled = int(progress / 10)
            bar = "█" * filled + " " * (10 - filled)
            await bot.edit_message_text(
                text=f"Анализ: [{bar}] {progress}%",
                chat_id=message.chat.id,
                message_id=progress_message.message_id
            )
    await bot.edit_message_text(
        text="Экспертиза завешена: [██████████] 100%",
        chat_id=message.chat.id,
        message_id=progress_message.message_id
    )
    await asyncio.sleep(0.5)
    await bot.edit_message_text(
        text="Ожидайте ответа вашего следователя...",
        chat_id=message.chat.id,
        message_id=progress_message.message_id
    )
    if not results:
        return "Состава преступления не обнаружено. Но это еще ничего не значит. "
    report = "\n".join(results)
    cleaned_report = clean_report(report)
    report_id = str(uuid.uuid4())
    report_file = os.path.join(BASE_DIR, "report.txt")
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(cleaned_report)
    final_response = await process_report_with_prompt2(cleaned_report)
    return final_response, report_file, report_id

async def start_command(message: Message):
    await message.answer(
        "Здравствуйте! Хорошо, что вы к нам вовремя обратились. Расскажите же нам скорее все,",
        reply_to_message_id=message.message_id
    )

async def analyze_command(message: Message, bot: Bot):
    prompt_file = os.path.join(BASE_DIR, "prompt.txt")
    prompt_template = read_file(prompt_file)
    if "Ошибка" in prompt_template:
        await message.answer(prompt_template, reply_to_message_id=message.message_id)
        return
    articles = load_articles()
    if "error" in articles:
        await message.answer(articles["error"], reply_to_message_id=message.message_id)
        return
    text = message.text.replace("/analyze", "").strip()
    if not text:
        await message.answer(
            "Гражданин, отправьте сообщение для анализа не более 2000 символов длиной",
            reply_to_message_id=message.message_id
        )
        return
    result = await analyze_text(text, prompt_template, articles, message, bot)
    if not result:
        return
    if isinstance(result, str):
        await message.answer(result, reply_to_message_id=message.message_id)
        return
    final_response, report_file, report_id = result
    MAX_LEN = 4000
    for part in (final_response[i:i+MAX_LEN] for i in range(0, len(final_response), MAX_LEN)):
        await message.answer(part, reply_to_message_id=message.message_id)
    await message.answer(
        "Ваша судьба в ваших руках, гражданин.",
        reply_markup=get_post_analysis_keyboard(),
        reply_to_message_id=message.message_id
    )
    await bot.send_document(
        chat_id=message.chat.id,
        document=FSInputFile(report_file, filename=f"report_{report_id}.txt"),
        caption="Здесь результаты лингвистической экспертизы.",
        reply_to_message_id=message.message_id
    )

async def text_message(message: Message, bot: Bot):
    prompt_file = os.path.join(BASE_DIR, "prompt.txt")
    prompt_template = read_file(prompt_file)
    if "Ошибка" in prompt_template:
        await message.answer(prompt_template, reply_to_message_id=message.message_id)
        return
    articles = load_articles()
    if "error" in articles:
        await message.answer(articles["error"], reply_to_message_id=message.message_id)
        return
    text = message.text.strip()
    result = await analyze_text(text, prompt_template, articles, message, bot)
    if not result:
        return
    if isinstance(result, str):
        await message.answer(result, reply_to_message_id=message.message_id)
        return
    final_response, report_file, report_id = result
    MAX_LEN = 4000
    for part in (final_response[i:i+MAX_LEN] for i in range(0, len(final_response), MAX_LEN)):
        await message.answer(part, reply_to_message_id=message.message_id)
    await message.answer(
        "Ваша судьба в ваших руках, гражданин.",
        reply_markup=get_post_analysis_keyboard(),
        reply_to_message_id=message.message_id
    )
    await bot.send_document(
        chat_id=message.chat.id,
        document=FSInputFile(report_file, filename=f"report_{report_id}.txt"),
        caption="Здесь результаты лингвистической экспертизы.",
        reply_to_message_id=message.message_id
    )


def get_post_analysis_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Штраф на месте", callback_data="pay_fine")],
        [InlineKeyboardButton(text="💾 Бланк самодоноса", callback_data="get_blank")],
        [InlineKeyboardButton(text="📞 Звонок другу", callback_data="repent")],
        [InlineKeyboardButton(text="💼 Брянск-Север", callback_data="bryansk_north")]
    ])

def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(bot=bot)

    dp.message.register(start_command, Command(commands=["start"]))

    @dp.message(Command(commands=["analyze"]))
    async def analyze_handler(message: Message):
        await handle_with_queue(analyze_command, message, bot)

    @dp.message(F.text)
    async def text_handler(message: Message):
        text = message.text.strip()

#ПОРОГ СРАБАТЫВАНИЯ 50 И БОЛЕЕ СИМВОЛОВ
        if len(text) <= 50:
            return

        async def reply_analyze(message: Message, bot: Bot):
            prompt_file = os.path.join(BASE_DIR, "prompt.txt")
            prompt_template = read_file(prompt_file)
            if "Ошибка" in prompt_template:
                await message.reply(prompt_template)
                return
            articles = load_articles()
            if "error" in articles:
                await message.reply(articles["error"])
                return
            result = await analyze_text(text, prompt_template, articles, message, bot)
            if not result:
                return
            if isinstance(result, str):
                await message.reply(result)
                return
            final_response, report_file, report_id = result
            MAX_LEN = 4000
            for part in (final_response[i:i+MAX_LEN] for i in range(0, len(final_response), MAX_LEN)):
                await message.reply(part)
            await message.reply(
                "Ваша судьба в ваших руках, гражданин.",
                reply_markup=get_post_analysis_keyboard()
            )
            await bot.send_document(
                chat_id=message.chat.id,
                document=FSInputFile(report_file, filename=f"report_{report_id}.txt"),
                caption="Здесь результаты лингвистической экспертизы.",
                reply_to_message_id=message.message_id
            )

        await handle_with_queue(reply_analyze, message, bot)

    @dp.callback_query(lambda c: c.data == "pay_fine")
    async def handle_pay_fine(callback: CallbackQuery):
        file_path = os.path.join(BASE_DIR, "qrcode.png")
        if os.path.exists(file_path):
            await callback.message.answer_photo(
                photo=FSInputFile(file_path),
                caption="💳 Отсканируйте QR-код для моментальной оплаты.",
                reply_to_message_id=callback.message.message_id
            )
        else:
            await callback.message.answer("QR-код не найден, напишите в поддержку.", reply_to_message_id=callback.message.message_id)
        await callback.answer()

    @dp.callback_query(lambda c: c.data == "get_blank")
    async def handle_get_blank(callback: CallbackQuery):
        file_path = os.path.join(BASE_DIR, "blank.doc")
        if os.path.exists(file_path):
            await callback.message.answer_document(
                FSInputFile(file_path, filename="blank.doc"),
                caption="Вот ваш бланк для самодоноса. Заполните, распечатайте и вышлите нам копию заказным письмом.",
                reply_to_message_id=callback.message.message_id
            )
        else:
            await callback.message.answer("Бланк не найден.", reply_to_message_id=callback.message.message_id)
        await callback.answer()

    @dp.callback_query(lambda c: c.data == "repent")
    async def handle_repent(callback: CallbackQuery):
        await callback.message.answer("Вы уже использовали свое право на звонок.", reply_to_message_id=callback.message.message_id)
        await callback.answer()

    @dp.callback_query(lambda c: c.data == "bryansk_north")
    async def handle_bryansk(callback: CallbackQuery):
        await callback.message.answer("Статус 17 подтверждён.", reply_to_message_id=callback.message.message_id)
        await callback.answer()

    asyncio.run(dp.start_polling(bot))

if __name__ == "__main__":
    main()
