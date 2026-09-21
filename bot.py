import asyncio
import json
import os
import re
import time
from datetime import date, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import ReplyKeyboardBuilder

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup

# ================== НАСТРОЙКИ ==================

# Токен теперь берём из переменной окружения, а не хардкодим в файле.
# Перед первым запуском: export BOT_TOKEN="твой_токен_от_BotFather"
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError(
        "Не найден BOT_TOKEN. Задай переменную окружения: export BOT_TOKEN='...' "
        "(старый токен светился в коде — перевыпусти его у @BotFather через /revoke)"
    )

# Страница со списком дз. Подтверждено скриншотом сайта — этот урл реальный,
# и на нём уже видны задания с дедлайном на завтра, не только "строго сегодня".
HOMEWORK_URL = "https://stupenionlaincentr.ru/admin/homework/hwindexact.php"
LOGIN_URL = "https://stupenionlaincentr.ru/admin/login.php"

STATE_FILE = "bot_state.json"
CHECK_INTERVAL_SECONDS = 3600  # раз в час

# Сколько headless Chrome могут работать ОДНОВРЕМЕННО (и в ручных проверках,
# и в фоновой). Остальные запросы просто встают в очередь и ждут своей очереди —
# это защищает Mac от попытки поднять сразу 25 браузеров при наплыве юзеров.
MAX_CONCURRENT_BROWSERS = 3
browser_semaphore = asyncio.Semaphore(MAX_CONCURRENT_BROWSERS)


async def get_tomorrow_homework_limited(login: str, password: str):
    async with browser_semaphore:
        return await asyncio.to_thread(get_tomorrow_homework, login, password)

DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
ID_RE = re.compile(r"id=(\d+)")

# ================== ПЕРСИСТЕНТНОЕ СОСТОЯНИЕ ==================
# users: {tg_user_id: {"login": ..., "password": ...}}
# notified: {tg_user_id: [id1, id2, ...]}  — какие дз уже отправляли


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"users": {}, "notified": {}}


def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


db = load_state()


class Registration(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()


bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


def main_menu():
    builder = ReplyKeyboardBuilder()
    builder.row(types.KeyboardButton(text="🔍 Проверить ДЗ на завтра"))
    builder.row(types.KeyboardButton(text="⚙️ Настройки"))
    return builder.as_markup(resize_keyboard=True)


# ================== ПАРСИНГ САЙТА ==================

def get_tomorrow_homework(login: str, password: str):
    """
    Возвращает список dict: {"id": str, "text": str} — только те задания,
    у которых дедлайн приходится на завтрашнюю дату.
    В случае ошибки логина/парсинга возвращает None.
    """
    tomorrow_str = (date.today() + timedelta(days=1)).strftime("%d.%m.%Y")

    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("window-size=1920,1080")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)

    try:
        driver.get(LOGIN_URL)
        wait = WebDriverWait(driver, 15)

        login_input = wait.until(EC.presence_of_element_located((By.NAME, "login")))
        pass_input = driver.find_element(By.NAME, "password")

        login_input.send_keys(login)
        pass_input.send_keys(password)

        btn = wait.until(
            EC.element_to_be_clickable((By.XPATH, "//input[@value='Войти' or @name='log_in']"))
        )
        btn.click()
        time.sleep(5)

        driver.get(HOMEWORK_URL)
        time.sleep(3)

        soup = BeautifulSoup(driver.page_source, "html.parser")
        results = []

        for row in soup.find_all("tr"):
            cols = row.find_all("td")
            if len(cols) < 3:
                continue

            deadline = cols[0].get_text(strip=True)
            subject = cols[1].get_text(strip=True)

            if "предмет" in subject.lower() or not subject:
                continue  # заголовок таблицы

            date_match = DATE_RE.search(deadline)
            if not date_match or date_match.group(1) != tomorrow_str:
                continue  # не на завтра — пропускаем

            task = cols[2].get_text(strip=True)

            id_match = ID_RE.search(str(row))
            hw_id = id_match.group(1) if id_match else f"{deadline}|{subject}|{task}"

            results.append({
                "id": hw_id,
                "text": f"📅 *{deadline}*\n📘 *{subject}*\n📝 {task}",
            })

        return results

    except Exception as e:
        print(f"Ошибка парсинга у логина {login}: {e}")
        return None
    finally:
        driver.quit()


# ================== ФОНОВАЯ ПРОВЕРКА ==================

state_lock = asyncio.Lock()


async def check_one_user(user_id_str: str, creds: dict):
    user_id = int(user_id_str)

    homework = await get_tomorrow_homework_limited(creds["login"], creds["password"])

    if homework is None:
        # ошибка логина/сайта — не трогаем notified, просто пробуем в следующий час
        return

    known_ids = set(db["notified"].get(user_id_str, []))
    current_ids = {item["id"] for item in homework}
    new_items = [item for item in homework if item["id"] not in known_ids]

    if new_items:
        text = "\n\n---\n\n".join(item["text"] for item in new_items)
        try:
            await bot.send_message(
                user_id,
                f"🔔 *Новое дз на завтра!*\n\n{text}",
                parse_mode="Markdown",
            )
        except Exception as e:
            print(f"Не смог отправить сообщение {user_id}: {e}")

    # запоминаем всё, что видели сейчас (включая старое) — чтобы не дублировать
    async with state_lock:
        db["notified"][user_id_str] = list(known_ids | current_ids)
        save_state()


async def scheduled_check():
    while True:
        print("⏰ Проверяю дз на завтра для всех пользователей...")

        # Запускаем проверки для всех юзеров параллельно — реальный параллелизм
        # всё равно ограничен MAX_CONCURRENT_BROWSERS через семафор внутри
        # get_tomorrow_homework_limited, так что железо не захлебнётся.
        tasks = [
            check_one_user(user_id_str, creds)
            for user_id_str, creds in list(db["users"].items())
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


# ================== ОБРАБОТЧИКИ КОМАНД ==================

@dp.message(Command("start"))
@dp.message(lambda m: m.text == "⚙️ Настройки")
async def cmd_start(message: types.Message, state: FSMContext):
    await message.answer("🚀 Привет! Введи свой **логин**:", parse_mode="Markdown")
    await state.set_state(Registration.waiting_for_login)


@dp.message(Registration.waiting_for_login)
async def process_login(message: types.Message, state: FSMContext):
    await state.update_data(login=message.text)
    await message.answer("🔑 Теперь введи **пароль**:", parse_mode="Markdown")
    await state.set_state(Registration.waiting_for_password)


@dp.message(Registration.waiting_for_password)
async def process_password(message: types.Message, state: FSMContext):
    data = await state.get_data()
    uid = str(message.from_user.id)

    db["users"][uid] = {"login": data["login"], "password": message.text}
    db["notified"].setdefault(uid, [])
    async with state_lock:
        save_state()

    await state.clear()
    await message.answer(
        "✅ Готово! Раз в час буду проверять и пришлю уведомление, "
        "если появится новое дз на завтра.",
        reply_markup=main_menu(),
    )


@dp.message(lambda m: m.text == "🔍 Проверить ДЗ на завтра")
@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    uid = str(message.from_user.id)
    user = db["users"].get(uid)
    if not user:
        return await message.answer("Сначала нажми /start")

    await message.answer("📡 *Подключаюсь и смотрю дз на завтра...*", parse_mode="Markdown")
    homework = await get_tomorrow_homework_limited(user["login"], user["password"])

    if homework is None:
        return await message.answer("❌ Не получилось зайти на сайт. Проверь логин/пароль (/start).")

    if not homework:
        return await message.answer("🎉 На завтра дз пока нет (или ещё не выложили).")

    text = "\n\n---\n\n".join(item["text"] for item in homework)
    await message.answer("📋 *Дз на завтра:*\n\n" + text, parse_mode="Markdown")

    # ручная проверка тоже помечает задания как "уже виденные",
    # чтобы через час не пришло повторное уведомление по тем же id
    async with state_lock:
        known_ids = set(db["notified"].get(uid, []))
        current_ids = {item["id"] for item in homework}
        db["notified"][uid] = list(known_ids | current_ids)
        save_state()


# ================== ЗАПУСК ==================

async def main():
    print("🚀 Бот запущен! Проверка дз на завтра — раз в час.")
    asyncio.create_task(scheduled_check())
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nБот выключен.")
