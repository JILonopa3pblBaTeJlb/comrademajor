import asyncio
import re
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
from aiogram.exceptions import TelegramBadRequest

# Логирование этапа импорта
logging.basicConfig(level=logging.DEBUG)
logging.info("Этап: Импорт всех модулей завершён")

BOT_TOKEN = "ВАШ_ТОКЕН"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARTICLES = ["148", "205.2", "207.3", "230", "280", "280.1", "282", "298.1", "319", "354.1"]
DENIALS_FILE = os.path.join(BASE_DIR, "denials.txt")
user_queue = deque()
user_busy: Dict[int, bool] = {}

provider_failures = {}

# Загрузка провайдеров из файла
def load_providers(file_path):
    logging.info(f"Этап: Загрузка провайдеров из файла {file_path}")
    provider_models = {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                provider, models = line.split(": ", 1)
                provider_models[provider] = models.split(",")
        logging.info(f"Этап: Провайдеры успешно загружены: {provider_models}")
        return provider_models
    except FileNotFoundError:
        logging.error(f"Этап: Файл {file_path} не найден")
        return {}
    except Exception as e:
        logging.error(f"Этап: Ошибка при чтении {file_path}: {str(e)}")
        return {}

# Загружаем провайдеры
PROVIDERS_FILE = os.path.join(BASE_DIR, "providerslist.txt")
PROVIDER_MODELS = load_providers(PROVIDERS_FILE)

async def handle_with_queue(handler_func, message: Message, bot: Bot):
    user_id = message.from_user.id
    logging.info(f"Этап: Обработка очереди для пользователя {user_id}")
    if user_id in user_busy:
        logging.info(f"Этап: Пользователь {user_id} занят, запрос отклонён")
        return
    if user_id in user_queue:
        logging.info(f"Этап: Пользователь {user_id} уже в очереди")
        await message.answer("Ожидайте гражданин, ваш запрос уже в очереди на обработку.")
        return
    user_queue.append(user_id)
    logging.info(f"Этап: Пользователь {user_id} добавлен в очередь")
    while user_queue[0] != user_id:
        await asyncio.sleep(1)
    user_busy[user_id] = True
    try:
        logging.info(f"Этап: Выполнение функции обработки для пользователя {user_id}")
        await handler_func(message, bot)
    finally:
        user_queue.popleft()
        user_busy.pop(user_id, None)
        logging.info(f"Этап: Пользователь {user_id} удалён из очереди и освобождён")

def read_file(file_path):
    logging.info(f"Этап: Чтение файла {file_path}")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            logging.info(f"Этап: Файл {file_path} успешно прочитан")
            return content
    except FileNotFoundError:
        logging.error(f"Этап: Файл {file_path} не найден")
        return f"Ошибка: Файл {file_path} не найден."
    except Exception as e:
        logging.error(f"Этап: Ошибка при чтении файла {file_path}: {str(e)}")
        return f"Ошибка при чтении файла {file_path}: {str(e)}"

def get_denials():
    logging.info(f"Этап: Загрузка списка отказов из {DENIALS_FILE}")
    try:
        with open(DENIALS_FILE, "r", encoding="utf-8") as f:
            denials = [line.strip().lower() for line in f if line.strip()]
            logging.info(f"Этап: Список отказов успешно загружен: {denials}")
            return denials
    except FileNotFoundError:
        logging.error(f"Этап: Файл отказов {DENIALS_FILE} не найден")
        return []
    except Exception as e:
        logging.error(f"Этап: Ошибка при чтении файла отказов: {str(e)}")
        return []

def is_cyrillic(text):
    logging.info("Этап: Проверка текста на наличие кириллицы")
    result = any('\u0400' <= char <= '\u04FF' for char in text)
    logging.info(f"Этап: Результат проверки кириллицы: {result}")
    return result

def is_denial_response(response):
    logging.info("Этап: Проверка ответа на наличие фраз отказа")
    denials = get_denials()
    if not is_cyrillic(response):
        logging.info("Этап: Ответ не содержит кириллицу, считается отказом")
        return True
    result = any(phrase in response.lower() for phrase in denials)
    logging.info(f"Этап: Результат проверки отказа: {result}")
    return result

def load_articles():
    json_file = os.path.join(BASE_DIR, "articles.json")
    logging.info(f"Этап: Загрузка статей из файла {json_file}")
    try:
        with open(json_file, "r", encoding="utf-8") as f:
            articles = json.load(f)
            logging.info(f"Этап: Статьи успешно загружены: {articles}")
            return articles
    except FileNotFoundError:
        logging.error(f"Этап: Файл {json_file} не найден")
        return {"error": f"Файл {json_file} не найден."}
    except Exception as e:
        logging.error(f"Этап: Ошибка при чтении {json_file}: {str(e)}")
        return {"error": f"Ошибка при чтении {json_file}: {str(e)}"}

async def call_g4f_model(prompt: str) -> str:
    logging.info("Этап: Вызов модели g4f")
    try:
        client = Client()
        valid_response = None  # Храним первый валидный ответ
        for _ in range(10):
            now = time.time()
            active_providers = [
                p for p in PROVIDER_MODELS.keys()
                if PROVIDER_MODELS[p] and (p not in provider_failures or now - provider_failures[p] > 1800)  # 30 минут = 1800 секунд
            ]
            logging.info(f"Этап: Доступные провайдеры: {active_providers}")
            if not active_providers:
                provider_failures.clear()
                active_providers = list(PROVIDER_MODELS.keys())
                logging.info(f"Этап: Очищены провайдеры с ошибками, новые активные провайдеры: {active_providers}")
            selected_provider = random.choice(active_providers)
            available_models = PROVIDER_MODELS[selected_provider].copy()
            random.shuffle(available_models)
            logging.info(f"Этап: Выбран провайдер {selected_provider} с моделями {available_models}")
            for selected_model in available_models:
                try:
                    start_time = time.time()
                    logging.info(f"Этап: Запрос к модели {selected_model} провайдера {selected_provider}")
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
                        logging.info(f"Этап: Ответ от {selected_model} пустой, исключаем провайдера {selected_provider} на 30 минут")
                        provider_failures[selected_provider] = time.time()
                        continue
                    response_content = response.choices[0].message.content.strip()
                    logging.info(f"Этап: Получен ответ от {selected_model}: {response_content[:50]}...")
                    if is_denial_response(response_content):
                        logging.info(f"Этап: Ответ от {selected_model} содержит отказ, пробуем другую модель")
                        continue
                    # Сохраняем первый валидный ответ
                    if valid_response is None:
                        valid_response = response_content
                        logging.info(f"Этап: Сохранён валидный ответ от {selected_model}")
                    # Если ответ быстрый (< 55 секунд), возвращаем его сразу
                    if time.time() - start_time <= 55:
                        logging.info(f"Этап: Успешный быстрый ответ от {selected_model}")
                        return response_content
                    logging.info(f"Этап: Ответ от {selected_model} медленный, но сохранён как запасной")
                    continue
                except asyncio.TimeoutError:
                    logging.info(f"Этап: Таймаут при запросе к {selected_model}, исключаем провайдера {selected_provider} на 30 минут")
                    provider_failures[selected_provider] = time.time()
                    continue
                except Exception as e:
                    logging.error(f"Этап: Ошибка при запросе к {selected_model}: {str(e)}")
                    provider_failures[selected_provider] = time.time()
                    continue
        # Если есть валидный ответ, возвращаем его, даже если он медленный
        if valid_response is not None:
            logging.info("Этап: Возвращён сохранённый валидный ответ")
            return valid_response
        logging.error("Этап: Не удалось получить ответ от всех провайдеров")
        return "Ошибка: Не удалось получить ответ от провайдеров."
    except Exception as e:
        logging.error(f"Этап: Общая ошибка в call_g4f_model: {str(e)}")
        return f"Ошибка: {str(e)}"

async def safe_reply(message: Message, text: str, **kwargs):
    logging.info(f"Этап: Проверка возможности ответа на сообщение {message.message_id} для пользователя {message.from_user.id}")
    
    # Задержка 10 секунд перед началом обработки
    logging.info(f"Этап: Задержка 10 секунд перед отправкой ответа пользователю {message.from_user.id}")
    await asyncio.sleep(10)
    
    logging.info(f"Этап: Отправка ответа пользователю {message.from_user.id}")
    try:
        sent = await message.reply(text, **kwargs)
        logging.info(f"Этап: Ответ успешно отправлен пользователю {message.from_user.id}")
        
        # Проверка, реально ли это был reply
        if sent.reply_to_message is None:
            logging.warning(f"Этап: Сообщение {message.message_id} удалено во время обработки — не получилось ответить реплаем. Удаляем.")
            await sent.delete()
            user_busy.pop(message.from_user.id, None)
            if message.from_user.id in user_queue:
                user_queue.remove(message.from_user.id)
            return None
        else:
            logging.info(f"Этап: Ответили реплаем на {message.message_id}")
            return sent
    except TelegramBadRequest as e:
        if "REPLY_MESSAGE_NOT_FOUND" in str(e).lower():
            logging.info(f"Этап: Сообщение удалено, освобождаем пользователя {message.from_user.id}")
            user_busy.pop(message.from_user.id, None)
            if message.from_user.id in user_queue:
                user_queue.remove(message.from_user.id)
            return None
        logging.error(f"Этап: Ошибка при отправке ответа: {str(e)}")
        raise

async def safe_answer(message: Message, text: str, **kwargs):
    logging.info(f"Этап: Отправка сообщения пользователю {message.from_user.id}")
    try:
        response = await message.answer(text, **kwargs)
        logging.info(f"Этап: Сообщение успешно отправлено пользователю {message.from_user.id}")
        return response
    except TelegramBadRequest as e:
        if "message to be replied not found" in str(e).lower():
            logging.info(f"Этап: Сообщение для ответа не найдено, отправляем без reply_to")
            kwargs.pop("reply_to_message_id", None)
            response = await message.answer(text, **kwargs)
            logging.info(f"Этап: Сообщение отправлено без reply_to")
            return response
        logging.error(f"Этап: Ошибка при отправке сообщения: {str(e)}")
        raise

async def safe_edit_message_text(bot: Bot, text: str, chat_id: int, message_id: int):
    logging.info(f"Этап: Редактирование сообщения {message_id} в чате {chat_id}")
    try:
        await bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_id
        )
        logging.info(f"Этап: Сообщение {message_id} успешно отредактировано")
        return True
    except TelegramBadRequest as e:
        if "MESSAGE_ID_INVALID" in str(e) or "message to edit not found" in str(e).lower():
            logging.info(f"Этап: Сообщение {message_id} не найдено для редактирования")
            return False
        logging.error(f"Этап: Ошибка при редактировании сообщения: {str(e)}")
        raise

async def analyze_article(client, text, article_number, article_content, prompt_template):
    logging.info(f"Этап: Анализ статьи {article_number}")
    try:
        if article_content.startswith("Ошибка"):
            logging.info(f"Этап: Статья {article_number} содержит ошибку: {article_content}")
            return f"Article {article_number}:\n{article_content}\n"
        prompt = prompt_template.format(
            article_content=article_content,
            user_input=text
        )
        logging.info(f"Этап: Сформирован промпт для статьи {article_number}")
        result = await call_g4f_model(prompt)
        logging.info(f"Этап: Получен результат анализа статьи {article_number}: {result[:50]}...")
        return f"Article {article_number}:\n{result}\n"
    except Exception as e:
        logging.error(f"Этап: Ошибка при анализе статьи {article_number}: {str(e)}")
        return f"Article {article_number}:\nОшибка: {str(e)}\n"

async def process_report_with_prompt2(report: str) -> str:
    prompt2_file = os.path.join(BASE_DIR, "prompt2.txt")
    logging.info(f"Этап: Чтение prompt2 из файла {prompt2_file}")
    prompt2_template = read_file(prompt2_file)
    if "Ошибка" in prompt2_template:
        logging.error(f"Этап: Ошибка в prompt2: {prompt2_template}")
        return prompt2_template
    prompt = prompt2_template.format(report_content=report)
    logging.info("Этап: Сформирован промпт для обработки отчёта")
    result = await call_g4f_model(prompt)
    logging.info(f"Этап: Получен результат обработки отчёта: {result[:50]}...")
    return result

def clean_report(report: str) -> str:
    logging.info("Этап: Очистка отчёта")
    # Удаляем markdown-блоки
    lines = report.split('\n')
    cleaned_lines = [line for line in lines if line.strip() not in ["```markdown", "```"]]
    cleaned_text = '\n'.join(cleaned_lines)

    # Удаляем всё от начала блока Sponsor до конца
    sponsor_trigger = re.search(r"\n*[-]{3,}\s*\n*\s*\*\*Sponsor\*\*", cleaned_text, re.IGNORECASE)
    if sponsor_trigger:
        cleaned_text = cleaned_text[:sponsor_trigger.start()].rstrip()
    logging.info("Этап: Отчёт успешно очищен")
    return cleaned_text

async def analyze_text(text, prompt_template, articles, message: Message, bot: Bot):
    logging.info(f"Этап: Начало анализа текста для пользователя {message.from_user.id}")
    if len(text) > 2000:
        logging.info(f"Этап: Текст слишком длинный (>2000 символов), пользователь {message.from_user.id}")
        await safe_reply(message, "Извините, мы не принимаем тексты длиннее 2000 символов.")
        return None

    progress_message = await safe_reply(message, "Производится лингвистическая экспертиза: [          ] 0%")
    if not progress_message:
        logging.info(f"Этап: Сообщение удалено, анализ прерван для {message.from_user.id}")
        return None

    client = Client()
    results = []
    total_articles = len([a for a in articles if not articles[a].startswith("Ошибка")])
    completed = 0
    logging.info(f"Этап: Начало анализа {total_articles} статей")

    for article_number, article_content in articles.items():
        if article_content.startswith("Ошибка"):
            completed += 1
            logging.info(f"Этап: Пропущена статья {article_number} из-за ошибки")
        else:
            result = await analyze_article(client, text, article_number, article_content, prompt_template)
            if "Applicability: Yes" in result:
                results.append(result)
            completed += 1
            progress = int((completed / total_articles) * 100)
            filled = int(progress / 10)
            bar = "█" * filled + " " * (10 - filled)
            logging.info(f"Этап: Прогресс анализа: {progress}%")
            ok = await safe_edit_message_text(
                bot=bot,
                text=f"Анализ: [{bar}] {progress}%",
                chat_id=message.chat.id,
                message_id=progress_message.message_id
            )
            if not ok:
                logging.info(f"Этап: Сообщение удалено, анализ прерван для {message.from_user.id}")
                user_busy.pop(message.from_user.id, None)
                if message.from_user.id in user_queue:
                    user_queue.remove(message.from_user.id)
                return None

    logging.info("Этап: Анализ завершён")
    await safe_edit_message_text(
        bot=bot,
        text="Экспертиза завешена: [██████████] 100%",
        chat_id=message.chat.id,
        message_id=progress_message.message_id
    )
    await asyncio.sleep(0.5)
    await safe_edit_message_text(
        bot=bot,
        text="Ожидайте ответа вашего следователя...",
        chat_id=message.chat.id,
        message_id=progress_message.message_id
    )

    if not results:
        logging.info("Этап: Состав преступления не обнаружен")
        return "Состава преступления не обнаружено. Но это еще ничего не значит."

    report = "\n".join(results)
    logging.info("Этап: Формирование отчёта")
    cleaned_report = clean_report(report)
    report_id = str(uuid.uuid4())
    report_file = os.path.join(BASE_DIR, "report.txt")
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(cleaned_report)
    logging.info(f"Этап: Отчёт сохранён в файл {report_file}")

    final_response = await process_report_with_prompt2(cleaned_report)
    logging.info("Этап: Получен финальный ответ после обработки отчёта")
    return final_response, report_file, report_id

async def start_command(message: Message):
    logging.info(f"Этап: Обработка команды /start для пользователя {message.from_user.id}")
    await safe_answer(
        message,
        "Здравствуйте! Хорошо, что вы к нам вовремя обратились. Расскажите же нам скорее все,",
        reply_to_message_id=message.message_id
    )

async def analyze_command(message: Message, bot: Bot):
    logging.info(f"Этап: Обработка команды /analyze для пользователя {message.from_user.id}")
    prompt_file = os.path.join(BASE_DIR, "prompt.txt")
    prompt_template = read_file(prompt_file)
    if "Ошибка" in prompt_template:
        logging.error(f"Этап: Ошибка в prompt: {prompt_template}")
        await safe_reply(message, prompt_template)
        return

    articles = load_articles()
    if "error" in articles:
        logging.error(f"Этап: Ошибка при загрузке статей: {articles['error']}")
        await safe_reply(message, articles["error"])
        return

    text = message.text.replace("/analyze", "").strip()
    if not text:
        logging.info(f"Этап: Пустой текст для анализа, пользователь {message.from_user.id}")
        await safe_reply(
            message,
            "Гражданин, отправьте сообщение для анализа не более 2000 символов длиной"
        )
        return

    result = await analyze_text(text, prompt_template, articles, message, bot)
    if not result:
        logging.info(f"Этап: Анализ текста не выполнен для пользователя {message.from_user.id}")
        return

    if isinstance(result, str):
        logging.info(f"Этап: Отправка результата анализа (строка): {result[:50]}...")
        await safe_reply(message, result)
        return

    final_response, report_file, report_id = result
    MAX_LEN = 4000
    logging.info("Этап: Отправка финального ответа пользователю")
    for part in (final_response[i:i + MAX_LEN] for i in range(0, len(final_response), MAX_LEN)):
        await safe_reply(message, part)

    await safe_reply(
        message,
        "Ваша судьба в ваших руках, гражданин.",
        reply_markup=get_post_analysis_keyboard()
    )

    logging.info(f"Этап: Отправка документа с отчётом {report_file}")
    await bot.send_document(
        chat_id=message.chat.id,
        document=FSInputFile(report_file, filename=f"report_{report_id}.txt"),
        caption="Здесь результаты лингвистической экспертизы.",
        reply_to_message_id=message.message_id
    )

async def text_message(message: Message, bot: Bot):
    logging.info(f"Этап: Обработка текстового сообщения от пользователя {message.from_user.id}")
    prompt_file = os.path.join(BASE_DIR, "prompt.txt")
    prompt_template = read_file(prompt_file)
    if "Ошибка" in prompt_template:
        logging.error(f"Этап: Ошибка в prompt: {prompt_template}")
        await safe_reply(message, prompt_template)
        return

    articles = load_articles()
    if "error" in articles:
        logging.error(f"Этап: Ошибка при загрузке статей: {articles['error']}")
        await safe_reply(message, articles["error"])
        return

    text = message.text.strip()
    result = await analyze_text(text, prompt_template, articles, message, bot)
    if not result:
        logging.info(f"Этап: Анализ текста не выполнен для пользователя {message.from_user.id}")
        return

    if isinstance(result, str):
        logging.info(f"Этап: Отправка результата анализа (строка): {result[:50]}...")
        await safe_reply(message, result)
        return

    final_response, report_file, report_id = result
    MAX_LEN = 4000
    logging.info("Этап: Отправка финального ответа пользователю")
    for part in (final_response[i:i + MAX_LEN] for i in range(0, len(final_response), MAX_LEN)):
        await safe_reply(message, part)

    await safe_reply(
        message,
        "Ваша судьба в ваших руках, гражданин.",
        reply_markup=get_post_analysis_keyboard()
    )

    logging.info(f"Этап: Отправка документа с отчётом {report_file}")
    await bot.send_document(
        chat_id=message.chat.id,
        document=FSInputFile(report_file, filename=f"report_{report_id}.txt"),
        caption="Здесь результаты лингвистической экспертизы.",
        reply_to_message_id=message.message_id
    )

def get_post_analysis_keyboard():
    logging.info("Этап: Формирование клавиатуры после анализа")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Штраф на месте", callback_data="pay_fine")],
        [InlineKeyboardButton(text="💾 Бланк самодоноса", callback_data="get_blank")],
        [InlineKeyboardButton(text="📞 Звонок другу", callback_data="repent")],
        [InlineKeyboardButton(text="💼 Брянск-Север", callback_data="bryansk_north")]
    ])
    logging.info("Этап: Клавиатура сформирована")
    return keyboard

def main():
    logging.info("Этап: Запуск бота")
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(bot=bot)

    dp.message.register(start_command, Command(commands=["start"]))

    @dp.message(Command(commands=["analyze"]))
    async def analyze_handler(message: Message):
        logging.info(f"Этап: Регистрация команды /analyze для пользователя {message.from_user.id}")
        await handle_with_queue(analyze_command, message, bot)

    @dp.message(F.text)
    async def text_handler(message: Message):
        text = message.text.strip()
        logging.info(f"Этап: Обработка текста от пользователя {message.from_user.id}, длина: {len(text)}")

        # ПОРОГ СРАБАТЫВАНИЯ, 50 СИМВОЛОВ
        if len(text) <= 50:
            logging.info(f"Этап: Текст короче 50 символов, игнорируем")
            return

        async def reply_analyze(message: Message, bot: Bot):
            logging.info(f"Этап: Начало анализа текста для пользователя {message.from_user.id}")
            prompt_file = os.path.join(BASE_DIR, "prompt.txt")
            prompt_template = read_file(prompt_file)
            if "Ошибка" in prompt_template:
                logging.error(f"Этап: Ошибка в prompt: {prompt_template}")
                await safe_reply(message, prompt_template)
                return

            articles = load_articles()
            if "error" in articles:
                logging.error(f"Этап: Ошибка при загрузке статей: {articles['error']}")
                await safe_reply(message, articles["error"])
                return

            text = message.text.strip()
            result = await analyze_text(text, prompt_template, articles, message, bot)
            if not result:
                logging.info(f"Этап: Анализ текста не выполнен")
                return

            if isinstance(result, str):
                logging.info(f"Этап: Отправка результата анализа (строка): {result[:50]}...")
                await safe_reply(message, result)
                return

            final_response, report_file, report_id = result
            MAX_LEN = 4000
            logging.info("Этап: Отправка финального ответа пользователю")
            for part in (final_response[i:i + MAX_LEN] for i in range(0, len(final_response), MAX_LEN)):
                await safe_reply(message, part)

            await safe_reply(
                message,
                "Ваша судьба в ваших руках, гражданин.",
                reply_markup=get_post_analysis_keyboard()
            )

            logging.info(f"Этап: Отправка документа с отчётом {report_file}")
            await bot.send_document(
                chat_id=message.chat.id,
                document=FSInputFile(report_file, filename=f"report_{report_id}.txt"),
                caption="Здесь результаты лингвистической экспертизы.",
                reply_to_message_id=message.message_id
            )
        await handle_with_queue(reply_analyze, message, bot)

    @dp.callback_query(lambda c: c.data == "pay_fine")
    async def handle_pay_fine(callback: CallbackQuery):
        logging.info(f"Этап: Обработка callback pay_fine для пользователя {callback.from_user.id}")
        file_path = os.path.join(BASE_DIR, "qrcode.png")
        if os.path.exists(file_path):
            logging.info(f"Этап: Отправка QR-кода пользователю {callback.from_user.id}")
            await safe_answer(
                callback.message,
                "💳 Отсканируйте QR-код для моментальной оплаты.",
                reply_to_message_id=callback.message.message_id
            )
            await callback.message.answer_photo(
                photo=FSInputFile(file_path),
                caption=None
            )
        else:
            logging.error(f"Этап: QR-код не найден: {file_path}")
            await safe_answer(callback.message, "QR-код не найден, напишите в поддержку.",
                              reply_to_message_id=callback.message.message_id)
        await callback.answer()

    @dp.callback_query(lambda c: c.data == "get_blank")
    async def handle_get_blank(callback: CallbackQuery):
        logging.info(f"Этап: Обработка callback get_blank для пользователя {callback.from_user.id}")
        file_path = os.path.join(BASE_DIR, "blank.doc")
        if os.path.exists(file_path):
            logging.info(f"Этап: Отправка бланка самодоноса пользователю {callback.from_user.id}")
            await callback.message.answer_document(
                FSInputFile(file_path, filename="blank.doc"),
                caption="Вот ваш бланк для самодоноса. Заполните, распечатайте и вышлите нам копию заказным письмом.",
                reply_to_message_id=callback.message.message_id
            )
        else:
            logging.error(f"Этап: Бланк не найден: {file_path}")
            await safe_answer(callback.message, "Бланк не найден.",
                              reply_to_message_id=callback.message.message_id)
        await callback.answer()

    @dp.callback_query(lambda c: c.data == "repent")
    async def handle_repent(callback: CallbackQuery):
        logging.info(f"Этап: Обработка callback repent для пользователя {callback.from_user.id}")
        await safe_answer(callback.message, "Вы уже использовали свое право на звонок.",
                          reply_to_message_id=callback.message.message_id)
        await callback.answer()

    @dp.callback_query(lambda c: c.data == "bryansk_north")
    async def handle_bryansk(callback: CallbackQuery):
        logging.info(f"Этап: Обработка callback bryansk_north для пользователя {callback.from_user.id}")
        await safe_answer(callback.message, "Статус 17 подтверждён.",
                          reply_to_message_id=callback.message.message_id)
        await callback.answer()

    logging.info("Этап: Запуск поллинга бота")
    asyncio.run(dp.start_polling(bot))

if __name__ == "__main__":
    main()
