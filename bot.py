import os
import re
import io
import math
import json
import base64
import asyncio
import sqlite3
import logging
from datetime import datetime, timedelta
import pytz
import requests
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "YOUR_OPENROUTER_API_KEY")
SCANNER_WEBAPP_URL = os.getenv("SCANNER_WEBAPP_URL", "https://turbo-krot.github.io/Golubochkin-bot/scanner.html")
DB_NAME = "nutrition_bot.db"
MSK_TZ = pytz.timezone("Europe/Moscow")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)
scheduler = AsyncIOScheduler(timezone=MSK_TZ)

# ==========================================
# БАЗА ДАННЫХ (SQLite) С АВТО-МИГРАЦИЕЙ
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        mode TEXT,
        custom_bju TEXT,
        height INTEGER,
        weight REAL,
        age INTEGER,
        exp_level TEXT,
        workouts TEXT,
        goal TEXT,
        has_supplements INTEGER,
        supplements_json TEXT,
        allergies TEXT,
        budget INTEGER,
        stores TEXT,
        target_p INTEGER,
        target_f INTEGER,
        target_c INTEGER,
        target_fiber INTEGER,
        rec_meals INTEGER,
        actual_meals INTEGER,
        use_sport_nutr INTEGER,
        notify_time TEXT,
        current_diet_json TEXT,
        next_diet_json TEXT,
        recipes_cache_json TEXT,
        diet_start_date TEXT,
        last_month_check TEXT,
        tips_history_json TEXT DEFAULT '[]',
        shopping_list_cache TEXT,
        offered_fridge INTEGER DEFAULT 0,
        offered_diet INTEGER DEFAULT 0,
        expert_note TEXT
    )
    """)
    new_cols = [
        ("shopping_list_cache", "TEXT"),
        ("offered_fridge", "INTEGER DEFAULT 0"),
        ("offered_diet", "INTEGER DEFAULT 0"),
        ("expert_note", "TEXT")
    ]
    for col_name, col_def in new_cols:
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")
        except sqlite3.OperationalError:
            pass

    c.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT,
        exp_date TEXT,
        added_at TEXT
    )
    """)
    conn.commit()
    conn.close()

def get_user(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
    conn.close()
    return dict(row)

def update_user(user_id: int, **kwargs):
    if not kwargs:
        return
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    keys = ", ".join([f"{k} = ?" for k in kwargs.keys()])
    vals = list(kwargs.values()) + [user_id]
    c.execute(f"UPDATE users SET {keys} WHERE user_id = ?", vals)
    conn.commit()
    conn.close()

def add_product_db(user_id: int, name: str, exp_date: str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    now_str = datetime.now(MSK_TZ).strftime("%d.%m.%Y")
    c.execute("INSERT INTO products (user_id, name, exp_date, added_at) VALUES (?, ?, ?, ?)",
              (user_id, name, exp_date, now_str))
    conn.commit()
    conn.close()

def get_products_db(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE user_id = ? ORDER BY id ASC", (user_id,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def delete_product_db(prod_id: int, user_id: int):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM products WHERE id = ? AND user_id = ?", (prod_id, user_id))
    conn.commit()
    conn.close()

# ==========================================
# FSM СОСТОЯНИЯ
# ==========================================
class OnboardingSG(StatesGroup):
    bju_choice = State()
    manual_bju_input = State()
    height = State()
    weight = State()
    age = State()
    exp_level = State()
    workouts = State()
    goal = State()
    high_weight_confirm = State()
    has_supplements = State()
    supplements_list = State()
    supplement_freq = State()
    allergies = State()
    budget = State()
    stores = State()
    notify_time = State()
    start_day_choice = State()
    meals_ready_choice = State()
    meals_count_input = State()
    sport_nutr_choice = State()

class DietReplaceSG(StatesGroup):
    removing_dish = State()

class ProductSG(StatesGroup):
    waiting_product_input = State()
    waiting_voice_input = State()
    waiting_draft_edit = State()
    waiting_bought_product = State()

# ==========================================
# AI КЛИЕНТ (УМНЫЙ ДИЕТОЛОГ И ПОВАР)
# ==========================================
FREE_MODELS = [
    "deepseek/deepseek-chat-v3-0324:free",
    "deepseek/deepseek-chat:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
    "meta-llama/llama-3.3-70b-instruct:free"
]

def _sync_ai_call(prompt: str, system: str, temperature: float) -> str:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    for model in FREE_MODELS:
        try:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ],
                "temperature": temperature
            }
            r = requests.post(url, headers=headers, json=payload, timeout=75)
            if r.status_code == 200:
                data = r.json()
                content = data["choices"][0]["message"]["content"]
                if content:
                    return content.strip()
        except Exception as e:
            logging.warning(f"Model {model} failed: {e}")
            continue
    return ""

async def ask_ai(prompt: str, system: str = "Ты профессиональный спортивный нутрициолог, диетолог и шеф-повар.", temperature: float = 0.4) -> str:
    return await asyncio.to_thread(_sync_ai_call, prompt, system, temperature)

def clean_json_str(raw: str) -> str:
    raw = re.sub(r"```json\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"```\s*", "", raw)
    start_brace = raw.find("{")
    start_bracket = raw.find("[")
    if start_brace != -1 and (start_bracket == -1 or start_brace < start_bracket):
        end = raw.rfind("}")
        if end != -1:
            return raw[start_brace:end+1]
    elif start_bracket != -1:
        end = raw.rfind("]")
        if end != -1:
            return raw[start_bracket:end+1]
    return raw.strip()

# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДАТ И НАВИГАЦИИ
# ==========================================
def get_msk_now() -> datetime:
    return datetime.now(MSK_TZ)

def get_diet_dates_list(start_date_str: str) -> list:
    try:
        start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
    except Exception:
        start_dt = get_msk_now() + timedelta(days=1)
    return [(start_dt + timedelta(days=i)).strftime("%d.%m") for i in range(7)]

async def safe_edit(message: Message, text: str, reply_markup: InlineKeyboardMarkup = None):
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception:
        try:
            await message.edit_text(text, reply_markup=reply_markup)
        except Exception:
            await message.answer(text, reply_markup=reply_markup)

async def try_delete_user_msg(message: Message):
    try:
        await message.delete()
    except Exception:
        pass

def back_kb(cb_data: str = "onboard_back") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=cb_data)]
    ])

async def push_step(state: FSMContext, step_name: str):
    data = await state.get_data()
    history = data.get("history", [])
    if not history or history[-1] != step_name:
        history.append(step_name)
    await state.update_data(history=history)

# ==========================================
# УМНЫЙ РАСЧЕТ БЖУ И ПРИЕМОВ ПИЩИ (ГОЛОВА БОТА)
# ==========================================
async def calculate_smart_bju_and_meals(u: dict) -> dict:
    w = float(u["weight"] or 70)
    h = int(u["height"] or 175)
    age = int(u["age"] or 25)
    goal = u["goal"] or "Набор мышечной массы"
    exp = u["exp_level"] or "Начинающий"
    workouts = u["workouts"] or "1-3"

    if u["custom_bju"]:
        p, f, c = map(int, u["custom_bju"].split())
        fiber = 30
        expert_note = "Использованы ваши индивидуальные дневные показатели БЖУ."
    else:
        prompt = f"""Рассчитай дневную норму БЖУ и клетчатки для человека:
Рост: {h} см, Вес: {w} кг, Возраст: {age} лет, Опыт: {exp}, Тренировок в неделю: {workouts}, Цель: {goal}.

СТРОГИЕ ПРАВИЛА БЕЗОПАСНОЙ И ПРОВЕРЕННОЙ НУТРИЦИОЛОГИИ:
1. Никаких заоблачных цифр! Средняя норма белка — от 1.5 до 2.0 г на 1 кг веса. Никогда не ставь 2.5-3 г на кг.
2. Если вес человека маленький (например, меньше 55 кг), организм должен привыкать постепенно: белок рассчитывай так, чтобы за один прием пищи выходило около 25 г белка (не перегружай ЖКТ с первого дня).
3. Жиры: около 0.8 - 1.1 г на кг веса.
4. Углеводы: сбалансированно под цель ({goal}) и активность ({workouts}).

Верни СТРОГО JSON без лишнего текста:
{{"p": число_белков, "f": число_жиров, "c": число_углеводов, "fiber": число_клетчатки, "note": "краткое пояснение диетолога в 1 предложение"}}"""
        res = await ask_ai(prompt, system="Отвечай строго в формате JSON.", temperature=0.2)
        try:
            d = json.loads(clean_json_str(res))
            p = int(d.get("p", round(w * 1.7)))
            f = int(d.get("f", round(w * 0.9)))
            c = int(d.get("c", round(w * 3.2)))
            fiber = int(d.get("fiber", 30))
            expert_note = d.get("note", "Сбалансированный расчет по проверенным нормам (1.5–2 г белка на кг).")
        except Exception:
            if w < 55:
                p = round(w * 1.6)
            else:
                p = round(w * (1.8 if goal == "Сушка" else 1.7))
            f = round(w * 0.9)
            c = round(w * (2.2 if goal == "Сушка" else 3.5))
            fiber = 30
            expert_note = "Сбалансированный расчет по проверенным нормам (1.5–2 г белка на кг)."

    # Умное округление и распределение по приемам пищи (максимум 5 приемов, никогда не 6!)
    per_meal_p = 25 if w < 55 else 32
    raw_meals = p / per_meal_p

    if raw_meals <= 3.3:
        rec_meals = 3
    elif raw_meals <= 4.3:
        rec_meals = 4
    else:
        rec_meals = 5

    # Если при делении на 5 остается маленький "хвостик" (например 157г -> 7г лишних),
    # не делаем 6-й прием и не пихаем остаток, а аккуратно округляем до 150-160г:
    if rec_meals == 5 and 150 < p < 165 and not u["custom_bju"]:
        p = 150

    return {
        "p": p,
        "f": f,
        "c": c,
        "fiber": fiber,
        "rec_meals": rec_meals,
        "note": expert_note
    }

# ==========================================
# ГЕНЕРАЦИЯ РАЦИОНА И СПИСКА ПОКУПОК ЧЕРЕЗ ИИ
# ==========================================
async def generate_weekly_diet_json(u: dict, start_date_str: str, forbidden_dish: str = None) -> dict:
    dates_list = get_diet_dates_list(start_date_str)
    supps_info = "Нет"
    if u["has_supplements"] and u["supplements_json"]:
        supps = json.loads(u["supplements_json"])
        supps_info = ", ".join([f"{s['name']} ({s.get('freq', 'по инструкции')})" for s in supps])

    sport_nutr_rule = (
        "Пользователь использует спортивное питание / добавки. Обязательно впиши их в приемы пищи. "
        "ВАЖНОЕ ПРАВИЛО: протеин усваивается с углеводами, поэтому НИКОГДА не ставь пустой протеин отдельно — всегда комбинируй его с углеводами (например: банан, овсянка, тост, хлебцы или каша)."
        if u["use_sport_nutr"] else
        "Без спортивного питания — только обычная доступная еда."
    )
    forbid_rule = f"СТРОГО ИСКЛЮЧИ блюдо/продукт: {forbidden_dish}!" if forbidden_dish else ""

    prompt = f"""Составь практичный, вкусный и простой план питания на 7 дней по датам: {", ".join(dates_list)}.
Параметры пользователя:
- Пол/возраст/вес/рост: {u['age']} лет, {u['weight']} кг, {u['height']} см
- Опыт и тренировки: {u['exp_level']}, тренировок в неделю: {u['workouts']}
- Цель: {u['goal']}
- Целевое дневное БЖУ: Белки {u['target_p']}г, Жиры {u['target_f']}г, Углеводы {u['target_c']}г, Клетчатка {u['target_fiber']}г
- Количество приемов пищи в день: СТРОГО {u['actual_meals']} (за 1 прием пищи усваивается в среднем 25-35г белка и до 75г углеводов)
- Бюджет на неделю: {u['budget']} руб.
- Магазины: {u['stores']}
- Аллергии и противопоказания: {u['allergies']}
- Добавки пользователя: {supps_info}
- {sport_nutr_rule}
{forbid_rule}

ВАЖНЕЙШИЕ ТРЕБОВАНИЯ К ПРОСТОТЕ И ПРАКТИЧНОСТИ БЛЮД:
1. Никаких ресторанных изысков! ЗАПРЕЩЕНЫ: утка, форель, говядина Блэк Ангус, трюфельное масло, моллюски, дорогие стейки, артишоки и редкие ингредиенты.
2. Используй простые, базовые и понятные продукты из магазинов ({u['stores']}): куриная грудка/бедро, индейка, обычная постная говядина, яйца, творог, горбуша/минтай/треска, гречка, рис, овсянка, макароны из тв. сортов, картофель, сезонные овощи и фрукты.
3. Практичность: блюда могут разумно повторяться на 2 дня (чтобы человек не стоял у плиты по 5 раз каждый день с нуля), но рацион на неделю должен быть разнообразным и вкусным.
4. Все граммовки указывай точно (в граммах сухого/сырого или готового продукта).
5. БЖУ на каждый прием пищи НЕ ДОЛЖЕН быть одинаковым под копирку! У каждого блюда свой естественный баланс БЖУ (например, на завтрак больше углеводов, на ужин больше белка и клетчатки), а в сумме за день они должны давать около целевых показателей ({u['target_p']}г Б, {u['target_f']}г Ж, {u['target_c']}г У).
6. Поле "title" у каждого приема пищи ОБЯЗАТЕЛЬНО должно содержать полное название блюда с граммовками.

Верни ответ СТРОГО в формате валидного JSON такой структуры:
{{
  "days": [
    {{
      "date": "{dates_list[0]}",
      "meals": [
        {{
          "num": 1,
          "title": "Овсяная каша (70г хлопьев) на молоке 150мл + 3 вареных яйца",
          "weight": "360 г",
          "p": 31,
          "f": 18,
          "c": 52,
          "short_ingredients": "Овсяные хлопья 70г, молоко 150мл, яйца куриные 3 шт"
        }},
        {{
          "num": 2,
          "title": "Филе куриной грудки на гриле (150г) + гречка отварная (180г) + салат из огурцов",
          "weight": "430 г",
          "p": 42,
          "f": 7,
          "c": 64,
          "short_ingredients": "Филе куриное 150г, гречка 70г сух., огурец 100г"
        }}
      ]
    }}
  ]
}}
В массиве "days" должно быть ровно 7 элементов с датами: {", ".join(dates_list)}. В каждом дне ровно {u['actual_meals']} приемов пищи."""

    res = await ask_ai(prompt, system="Отвечай строго валидным JSON без комментариев.", temperature=0.3)
    res = clean_json_str(res)
    try:
        data = json.loads(res)
        # Жестко проставляем реальные числа ДД.ММ по реальному времени
        for i, d_item in enumerate(data.get("days", [])):
            if i < len(dates_list):
                d_item["date"] = dates_list[i]
        return data
    except Exception as e:
        logging.error(f"Diet JSON parse error: {e} | Raw: {res}")
        return None

async def generate_shopping_list_ai(u: dict, diet_data: dict) -> str:
    prompt = f"""На основе этого 7-дневного рациона составь точный список покупок на неделю:
Рацион: {json.dumps(diet_data, ensure_ascii=False)}

Магазины пользователя: {u['stores']}
Недельный бюджет пользователя: {u['budget']} руб.

ПРАВИЛА СОСТАВЛЕНИЯ КОРЗИНЫ:
1. Подбери конкретные продукты (среднего хорошего качества, доступные марки из указанных магазинов: {u['stores']}).
2. Посчитай общий нужный вес/количество каждого продукта на всю неделю (например: Филе куриной грудки — 2.2 кг, Гречневая крупа — 1 пачка 900г, Яйца С1 — 2 десятка и т.д.).
3. Укажи примерную актуальную цену за каждую позицию в рублях и в самом конце напиши **ИТОГО: ~ХХХХ руб.** (постарайся уложиться в бюджет {u['budget']} руб.).
4. Оформи список чисто, аккуратно, по категориям (Мясо/Птица/Рыба, Крупы и гарниры, Молочное и яйца, Овощи и фрукты, Бакалея)."""
    return await ask_ai(prompt, temperature=0.3)

# ==========================================
# /START И ГЛАВНОЕ МЕНЮ РЕЖИМОВ
# ==========================================
def main_modes_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🥗 Рацион", callback_data="mode_diet")],
        [InlineKeyboardButton(text="📦 Продуктовый контроль", callback_data="mode_products")]
    ])

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    u = get_user(message.from_user.id)
    if u.get("current_diet_json"):
        text, kb = render_diet_day(message.from_user.id, 0)
        await message.answer(text, reply_markup=kb, parse_mode="Markdown")
        return
    await message.answer("Выберите режим:", reply_markup=main_modes_kb())

@router.callback_query(F.data == "back_to_modes")
async def cb_back_to_modes(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(callback.message, "Выберите режим:", main_modes_kb())

# ==========================================
# НАВИГАЦИЯ «НАЗАД» ПО ШАГАМ АНКЕТЫ
# ==========================================
async def render_onboarding_step(message: Message, state: FSMContext, step: str, uid: int):
    u = get_user(uid)
    if step == "bju_choice":
        await state.set_state(OnboardingSG.bju_choice)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Рассчитать БЖУ автоматически", callback_data="bju_auto")],
            [InlineKeyboardButton(text="✍️ Ввести свои дневные показатели", callback_data="bju_manual")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_modes")]
        ])
        await safe_edit(message, "Как определим вашу дневную норму БЖУ?", kb)

    elif step == "manual_bju_input":
        await state.set_state(OnboardingSG.manual_bju_input)
        await safe_edit(
            message,
            "Введите свои **дневные** показатели: белки, жиры, углеводы в формате `30 20 300`\n"
            "*(обратите внимание: указывается общая норма на весь день)*:",
            back_kb()
        )

    elif step == "height":
        await state.set_state(OnboardingSG.height)
        await safe_edit(message, "1️⃣ Какой у вас рост (в см)?", back_kb())

    elif step == "weight":
        await state.set_state(OnboardingSG.weight)
        await safe_edit(message, "2️⃣ Какой у вас вес (в кг)?", back_kb())

    elif step == "age":
        await state.set_state(OnboardingSG.age)
        await safe_edit(message, "3️⃣ Сколько вам полных лет?", back_kb())

    elif step == "exp_level":
        await state.set_state(OnboardingSG.exp_level)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢 Начинающий", callback_data="exp_Начинающий")],
            [InlineKeyboardButton(text="🟡 Любитель", callback_data="exp_Любитель")],
            [InlineKeyboardButton(text="🔴 Профессионал", callback_data="exp_Профессионал")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="onboard_back")]
        ])
        await safe_edit(message, "4️⃣ Ваш уровень подготовки в тренировках?", kb)

    elif step == "workouts":
        await state.set_state(OnboardingSG.workouts)
        if u["exp_level"] == "Профессионал":
            await safe_edit(message, "5️⃣ Напишите точное количество ваших тренировок в неделю (числом):", back_kb())
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="1–3 тренировки", callback_data="work_1-3")],
                [InlineKeyboardButton(text="3 и более", callback_data="work_3 и более")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="onboard_back")]
            ])
            await safe_edit(message, "5️⃣ Сколько раз в неделю вы тренируетесь?", kb)

    elif step == "goal":
        await state.set_state(OnboardingSG.goal)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Сушка", callback_data="goal_Сушка")],
            [InlineKeyboardButton(text="💪 Набор мышечной массы", callback_data="goal_Набор мышечной массы")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="onboard_back")]
        ])
        await safe_edit(message, "6️⃣ Какая ваша главная цель?", kb)

    elif step == "has_supplements":
        await state.set_state(OnboardingSG.has_supplements)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Да", callback_data="supp_yes"),
             InlineKeyboardButton(text="Нет", callback_data="supp_no")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="onboard_back")]
        ])
        await safe_edit(message, "7️⃣ Принимаете ли вы какие-либо добавки или спортивное питание?", kb)

    elif step == "supplements_list":
        await state.set_state(OnboardingSG.supplements_list)
        await safe_edit(
            message,
            "8️⃣ Напишите через запятую какие добавки, спортпит или препараты вы принимаете.\n"
            "*(Если там есть БЖУ, можете указать рядом)*:",
            back_kb()
        )

    elif step == "allergies":
        await state.set_state(OnboardingSG.allergies)
        num = "7️⃣" if u["exp_level"] == "Начинающий" else "🔟"
        await safe_edit(
            message,
            f"{num} Есть ли у вас противопоказания, болезни ЖКТ или аллергия на продукты?\n"
            "*(Напишите подробно или отправьте «Нет»)*:",
            back_kb()
        )

    elif step == "budget":
        await state.set_state(OnboardingSG.budget)
        await safe_edit(message, "💰 Какой у вас недельный бюджет на питание (в рублях)? Например: `5000`", back_kb())

    elif step == "stores":
        await state.set_state(OnboardingSG.stores)
        await safe_edit(
            message,
            "🛒 В каких магазинах вы закупаетесь продуктами?\n"
            "*(Например: Пятёрочка, Перекрёсток, Чижик или просто напишите рынок)*:",
            back_kb()
        )

    elif step == "notify_time":
        await state.set_state(OnboardingSG.notify_time)
        await safe_edit(
            message,
            "⏰ В какое время (по МСК) вам удобно получать утреннее уведомление с рационом на день?\n"
            "Напишите в формате `ЧЧ:ММ` (например `08:30`):",
            back_kb()
        )

    elif step == "start_day_choice":
        await state.set_state(OnboardingSG.start_day_choice)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ С завтрашнего дня", callback_data="start_tomorrow")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="onboard_back")]
        ])
        await safe_edit(
            message,
            "📆 С какого дня составить рацион?\n\n"
            "Рацион формируется минимум со следующего дня, чтобы вы успели ознакомиться со списком и закупить продукты.\n"
            "Нажмите кнопку **«С завтрашнего дня»** или напишите день недели / дату (например `18.12`):",
            kb
        )

@router.callback_query(F.data == "onboard_back")
async def cb_onboard_back(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    history = data.get("history", [])
    uid = callback.from_user.id

    if len(history) <= 1:
        await state.clear()
        await safe_edit(callback.message, "Выберите режим:", main_modes_kb())
        return

    current_step = history.pop()
    prev_step = history[-1]
    await state.update_data(history=history)

    # Очищаем предыдущее сохраненное значение при откате назад
    field_map = {
        "manual_bju_input": "custom_bju",
        "height": "height",
        "weight": "weight",
        "age": "age",
        "exp_level": "exp_level",
        "workouts": "workouts",
        "goal": "goal",
        "has_supplements": "has_supplements",
        "supplements_list": "supplements_json",
        "allergies": "allergies",
        "budget": "budget",
        "stores": "stores",
        "notify_time": "notify_time"
    }
    if current_step in field_map:
        update_user(uid, **{field_map[current_step]: None})
    if prev_step in field_map:
        update_user(uid, **{field_map[prev_step]: None})

    await render_onboarding_step(callback.message, state, prev_step, uid)

# ==========================================
# ПРОХОЖДЕНИЕ АНКЕТЫ «РАЦИОН»
# ==========================================
@router.callback_query(F.data == "mode_diet")
async def start_diet_mode(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    update_user(uid, mode="diet")
    u = get_user(uid)
    if u.get("current_diet_json"):
        await state.clear()
        text, kb = render_diet_day(uid, 0)
        await safe_edit(callback.message, text, kb)
        return

    await state.clear()
    await state.update_data(history=["bju_choice"], prompt_mid=callback.message.message_id)
    await render_onboarding_step(callback.message, state, "bju_choice", uid)

@router.callback_query(OnboardingSG.bju_choice, F.data.in_({"bju_auto", "bju_manual"}))
async def process_bju_choice(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if callback.data == "bju_auto":
        update_user(uid, custom_bju=None)
        await push_step(state, "height")
        await render_onboarding_step(callback.message, state, "height", uid)
    else:
        await push_step(state, "manual_bju_input")
        await render_onboarding_step(callback.message, state, "manual_bju_input", uid)

@router.message(OnboardingSG.manual_bju_input)
async def process_manual_bju(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    parts = txt.split()
    data = await state.get_data()
    prompt_mid = data.get("prompt_mid")
    await try_delete_user_msg(message)

    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        await bot.edit_message_text(
            "⚠️ Неверный формат! Введите 3 целых числа через пробел — ваши **дневные** Белки Жиры Углеводы (например: `140 70 280`):",
            chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=back_kb()
        )
        return

    update_user(message.from_user.id, custom_bju=f"{parts[0]} {parts[1]} {parts[2]}")
    await push_step(state, "height")
    await bot.edit_message_text("1️⃣ Какой у вас рост (в см)?", chat_id=message.chat.id, message_id=prompt_mid, reply_markup=back_kb())
    await state.set_state(OnboardingSG.height)

@router.message(OnboardingSG.height)
async def process_height(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    data = await state.get_data()
    prompt_mid = data.get("prompt_mid")
    await try_delete_user_msg(message)

    if not txt.isdigit() or not (100 <= int(txt) <= 250):
        await bot.edit_message_text(
            "⚠️ Пожалуйста, введите реальный рост в сантиметрах числом (например: `178`):",
            chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=back_kb()
        )
        return

    update_user(message.from_user.id, height=int(txt))
    await push_step(state, "weight")
    await bot.edit_message_text("2️⃣ Какой у вас вес (в кг)?", chat_id=message.chat.id, message_id=prompt_mid, reply_markup=back_kb())
    await state.set_state(OnboardingSG.weight)

@router.message(OnboardingSG.weight)
async def process_weight(message: Message, state: FSMContext):
    txt = (message.text or "").strip().replace(",", ".")
    data = await state.get_data()
    prompt_mid = data.get("prompt_mid")
    await try_delete_user_msg(message)

    try:
        w = float(txt)
        if not (30 <= w <= 260):
            raise ValueError()
    except ValueError:
        await bot.edit_message_text(
            "⚠️ Введите корректный вес в килограммах числом (например: `75` или `72.5`):",
            chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=back_kb()
        )
        return

    update_user(message.from_user.id, weight=w)
    await push_step(state, "age")
    await bot.edit_message_text("3️⃣ Сколько вам полных лет?", chat_id=message.chat.id, message_id=prompt_mid, reply_markup=back_kb())
    await state.set_state(OnboardingSG.age)

@router.message(OnboardingSG.age)
async def process_age(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    data = await state.get_data()
    prompt_mid = data.get("prompt_mid")
    await try_delete_user_msg(message)

    if not txt.isdigit() or not (12 <= int(txt) <= 100):
        await bot.edit_message_text(
            "⚠️ Введите ваш возраст полным числом (от 12 до 100):",
            chat_id=message.chat.id, message_id=prompt_mid, reply_markup=back_kb()
        )
        return

    update_user(message.from_user.id, age=int(txt))
    await push_step(state, "exp_level")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Начинающий", callback_data="exp_Начинающий")],
        [InlineKeyboardButton(text="🟡 Любитель", callback_data="exp_Любитель")],
        [InlineKeyboardButton(text="🔴 Профессионал", callback_data="exp_Профессионал")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="onboard_back")]
    ])
    await bot.edit_message_text("4️⃣ Ваш уровень подготовки в тренировках?", chat_id=message.chat.id, message_id=prompt_mid, reply_markup=kb)
    await state.set_state(OnboardingSG.exp_level)

@router.callback_query(OnboardingSG.exp_level, F.data.startswith("exp_"))
async def process_exp_level(callback: CallbackQuery, state: FSMContext):
    exp = callback.data.split("_", 1)[1]
    uid = callback.from_user.id
    update_user(uid, exp_level=exp)
    await push_step(state, "workouts")
    await render_onboarding_step(callback.message, state, "workouts", uid)

@router.callback_query(OnboardingSG.workouts, F.data.startswith("work_"))
async def process_workouts_btn(callback: CallbackQuery, state: FSMContext):
    w_cnt = callback.data.split("_", 1)[1]
    uid = callback.from_user.id
    update_user(uid, workouts=w_cnt)
    await push_step(state, "goal")
    await render_onboarding_step(callback.message, state, "goal", uid)

@router.message(OnboardingSG.workouts)
async def process_workouts_pro(message: Message, state: FSMContext):
    u = get_user(message.from_user.id)
    data = await state.get_data()
    prompt_mid = data.get("prompt_mid")
    await try_delete_user_msg(message)

    if u["exp_level"] != "Профессионал":
        await bot.send_message(message.chat.id, "⚠️ Ответьте на предыдущий вопрос! Выберите вариант кнопкой.")
        return

    txt = (message.text or "").strip()
    if not txt.isdigit() or not (1 <= int(txt) <= 21):
        await bot.edit_message_text(
            "⚠️ Введите количество тренировок в неделю числом (от 1 до 21):",
            chat_id=message.chat.id, message_id=prompt_mid, reply_markup=back_kb()
        )
        return

    update_user(message.from_user.id, workouts=txt)
    await push_step(state, "goal")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Сушка", callback_data="goal_Сушка")],
        [InlineKeyboardButton(text="💪 Набор мышечной массы", callback_data="goal_Набор мышечной массы")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="onboard_back")]
    ])
    await bot.edit_message_text("6️⃣ Какая ваша главная цель?", chat_id=message.chat.id, message_id=prompt_mid, reply_markup=kb)
    await state.set_state(OnboardingSG.goal)

@router.callback_query(OnboardingSG.goal, F.data.startswith("goal_"))
async def process_goal(callback: CallbackQuery, state: FSMContext):
    goal = callback.data.split("_", 1)[1]
    uid = callback.from_user.id
    update_user(uid, goal=goal)
    u = get_user(uid)

    # Умная проверка: если вес >= 90 кг и человек выбрал "Набор мышечной массы"
    if float(u["weight"] or 0) >= 90 and goal == "Набор мышечной массы":
        await state.set_state(OnboardingSG.high_weight_confirm)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Переключить на Сушку (Рекомендуется)", callback_data="switch_goal_to_cut")],
            [InlineKeyboardButton(text="💪 Оставить Набор мышечной массы", callback_data="keep_goal_bulk")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="onboard_back")]
        ])
        await safe_edit(
            callback.message,
            f"⚠️ **Рекомендация нутрициолога:**\n\n"
            f"При текущем весе **{u['weight']} кг** профицит калорий и набор массы могут создать лишнюю нагрузку на сердечно-сосудистую систему и суставы, а также увеличить процент жира.\n\n"
            f"Я рекомендую выбрать режим **«Сушка»** (качественная рекомпозиция с сохранением мышц). Но итоговое решение за вами:",
            kb
        )
        return

    await proceed_after_goal(callback.message, state, uid)

@router.callback_query(OnboardingSG.high_weight_confirm, F.data.in_({"switch_goal_to_cut", "keep_goal_bulk"}))
async def process_high_weight_decision(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if callback.data == "switch_goal_to_cut":
        update_user(uid, goal="Сушка")
    else:
        update_user(uid, goal="Набор мышечной массы")
    await proceed_after_goal(callback.message, state, uid)

async def proceed_after_goal(message: Message, state: FSMContext, uid: int):
    u = get_user(uid)
    if u["exp_level"] == "Начинающий":
        update_user(uid, has_supplements=0, supplements_json="[]")
        await push_step(state, "allergies")
        await render_onboarding_step(message, state, "allergies", uid)
    else:
        await push_step(state, "has_supplements")
        await render_onboarding_step(message, state, "has_supplements", uid)

@router.callback_query(OnboardingSG.has_supplements, F.data.in_({"supp_yes", "supp_no"}))
async def process_has_supps(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if callback.data == "supp_no":
        update_user(uid, has_supplements=0, supplements_json="[]")
        await push_step(state, "allergies")
        await render_onboarding_step(callback.message, state, "allergies", uid)
    else:
        update_user(uid, has_supplements=1)
        await push_step(state, "supplements_list")
        await render_onboarding_step(callback.message, state, "supplements_list", uid)

@router.message(OnboardingSG.supplements_list)
async def process_supps_list(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    data = await state.get_data()
    prompt_mid = data.get("prompt_mid")
    await try_delete_user_msg(message)

    items = [i.strip() for i in txt.split(",") if i.strip()]
    if not items:
        await bot.edit_message_text("⚠️ Перечислите добавки через запятую:", chat_id=message.chat.id, message_id=prompt_mid, reply_markup=back_kb())
        return

    supps_struct = [{"name": it, "freq": ""} for it in items]
    await state.update_data(supps_struct=supps_struct, supp_idx=0)
    await state.set_state(OnboardingSG.supplement_freq)
    await bot.edit_message_text(
        f"9️⃣ Укажите дозировку и как часто вы принимаете **{items[0]}** (например: `1 порция каждый день после тренировки`):",
        chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=back_kb()
    )

@router.message(OnboardingSG.supplement_freq)
async def process_supp_freq(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    data = await state.get_data()
    prompt_mid = data.get("prompt_mid")
    supps_struct = data.get("supps_struct", [])
    idx = data.get("supp_idx", 0)
    await try_delete_user_msg(message)

    supps_struct[idx]["freq"] = txt
    idx += 1
    if idx < len(supps_struct):
        await state.update_data(supps_struct=supps_struct, supp_idx=idx)
        await bot.edit_message_text(
            f"9️⃣ Укажите дозировку и частоту приема для **{supps_struct[idx]['name']}**:",
            chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=back_kb()
        )
    else:
        update_user(message.from_user.id, supplements_json=json.dumps(supps_struct, ensure_ascii=False))
        await push_step(state, "allergies")
        await bot.edit_message_text(
            "🔟 Есть ли у вас противопоказания, болезни ЖКТ или аллергия на продукты?\n*(Напишите подробно или отправьте «Нет»)*:",
            chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=back_kb()
        )
        await state.set_state(OnboardingSG.allergies)

@router.message(OnboardingSG.allergies)
async def process_allergies(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    data = await state.get_data()
    prompt_mid = data.get("prompt_mid")
    await try_delete_user_msg(message)

    update_user(message.from_user.id, allergies=txt)
    await push_step(state, "budget")
    await bot.edit_message_text(
        "💰 Какой у вас недельный бюджет на питание (в рублях)? Например: `5000`",
        chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=back_kb()
    )
    await state.set_state(OnboardingSG.budget)

@router.message(OnboardingSG.budget)
async def process_budget(message: Message, state: FSMContext):
    txt = re.sub(r"\D", "", message.text or "")
    data = await state.get_data()
    prompt_mid = data.get("prompt_mid")
    await try_delete_user_msg(message)

    if not txt or int(txt) < 1000:
        await bot.edit_message_text(
            "⚠️ Укажите реальный недельный бюджет в рублях числом (например: `4500`):",
            chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=back_kb()
        )
        return

    update_user(message.from_user.id, budget=int(txt))
    await push_step(state, "stores")
    await bot.edit_message_text(
        "🛒 В каких магазинах вы закупаетесь продуктами?\n*(Например: Пятёрочка, Перекрёсток, Чижик или просто напишите рынок)*:",
        chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=back_kb()
    )
    await state.set_state(OnboardingSG.stores)

@router.message(OnboardingSG.stores)
async def process_stores(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    data = await state.get_data()
    prompt_mid = data.get("prompt_mid")
    await try_delete_user_msg(message)

    if len(txt) < 2:
        await bot.edit_message_text(
            "⚠️ Напишите название хотя бы одного магазина или «рынок»:",
            chat_id=message.chat.id, message_id=prompt_mid, reply_markup=back_kb()
        )
        return

    update_user(message.from_user.id, stores=txt)
    await push_step(state, "notify_time")
    await bot.edit_message_text(
        "⏰ В какое время (по МСК) вам удобно получать утреннее уведомление с рационом на день?\nНапишите в формате `ЧЧ:ММ` (например `08:30`):",
        chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=back_kb()
    )
    await state.set_state(OnboardingSG.notify_time)

@router.message(OnboardingSG.notify_time)
async def process_notify_time(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    data = await state.get_data()
    prompt_mid = data.get("prompt_mid")
    await try_delete_user_msg(message)

    if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", txt):
        await bot.edit_message_text(
            "⚠️ Неверный формат времени! Введите время по МСК в формате `ЧЧ:ММ`, например `08:00`:",
            chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=back_kb()
        )
        return

    update_user(message.from_user.id, notify_time=txt)
    await push_step(state, "start_day_choice")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ С завтрашнего дня", callback_data="start_tomorrow")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="onboard_back")]
    ])
    await bot.edit_message_text(
        "📆 С какого дня составить рацион?\n\n"
        "Рацион формируется минимум со следующего дня, чтобы вы успели ознакомиться со списком и закупить продукты.\n"
        "Нажмите кнопку **«С завтрашнего дня»** или напишите день недели / дату (например `18.12`):",
        chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=kb
    )
    await state.set_state(OnboardingSG.start_day_choice)

@router.callback_query(OnboardingSG.start_day_choice, F.data == "start_tomorrow")
async def process_start_tomorrow(callback: CallbackQuery, state: FSMContext):
    tomorrow = (get_msk_now() + timedelta(days=1)).strftime("%Y-%m-%d")
    update_user(callback.from_user.id, diet_start_date=tomorrow)
    await show_bju_and_meals_question(callback.message, state, callback.from_user.id)

@router.message(OnboardingSG.start_day_choice)
async def process_start_day_text(message: Message, state: FSMContext):
    txt = (message.text or "").strip().lower()
    data = await state.get_data()
    prompt_mid = data.get("prompt_mid")
    await try_delete_user_msg(message)

    now = get_msk_now()
    weekdays_map = {
        "понедельник": 0, "вторник": 1, "среда": 2, "среду": 2,
        "четверг": 3, "пятница": 4, "пятницу": 4, "суббота": 5, "субботу": 5, "воскресенье": 6
    }

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ С завтрашнего дня", callback_data="start_tomorrow")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="onboard_back")]
    ])

    target_dt = None
    if txt in ["сегодня", now.strftime("%d.%m"), now.strftime("%d.%m.%Y")]:
        await bot.edit_message_text(
            "⚠️ **На сегодняшний день рацион не составляется!**\n"
            "Рацион формируется минимум со следующего дня, чтобы вы успели подготовиться и купить продукты в магазине.\n\n"
            "Выберите **«С завтрашнего дня»** или укажите будущую дату:",
            chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=kb
        )
        return

    if txt in weekdays_map:
        w_target = weekdays_map[txt]
        if w_target == now.weekday():
            await bot.edit_message_text(
                "⚠️ Вы указали сегодняшний день недели! Рацион составляется минимум со следующего дня, чтобы вы успели купить продукты.\n\n"
                "Нажмите **«С завтрашнего дня»** или укажите другой день:",
                chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=kb
            )
            return
        days_ahead = (w_target - now.weekday() + 7) % 7
        target_dt = now + timedelta(days=days_ahead)
    else:
        m = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?$", txt)
        if m:
            d, mo, yr = int(m.group(1)), int(m.group(2)), int(m.group(3) or now.year)
            try:
                cand = MSK_TZ.localize(datetime(yr, mo, d))
                if cand.date() <= now.date():
                    await bot.edit_message_text(
                        "⚠️ Дата должна быть не раньше завтрашнего дня, чтобы вы успели закупить продукты! Укажите будущую дату или нажмите кнопку ниже:",
                        chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=kb
                    )
                    return
                target_dt = cand
            except Exception:
                pass

    if not target_dt:
        await bot.edit_message_text(
            "⚠️ Не удалось распознать день. Нажмите кнопку **«С завтрашнего дня»** или введите дату в формате `ДД.ММ`:",
            chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=kb
        )
        return

    update_user(message.from_user.id, diet_start_date=target_dt.strftime("%Y-%m-%d"))
    msg_obj = await bot.edit_message_text("⏳ Рассчитываю ваши показатели...", chat_id=message.chat.id, message_id=prompt_mid)
    await show_bju_and_meals_question(msg_obj, state, message.from_user.id)

async def show_bju_and_meals_question(message: Message, state: FSMContext, uid: int):
    u = get_user(uid)
    calc = await calculate_smart_bju_and_meals(u)
    update_user(
        uid,
        target_p=calc["p"],
        target_f=calc["f"],
        target_c=calc["c"],
        target_fiber=calc["fiber"],
        rec_meals=calc["rec_meals"],
        expert_note=calc["note"]
    )
    u = get_user(uid)

    # Сохраняем важную карточку профиля и БЖУ отдельным сообщением в чате, чтобы она не пропадала
    summary_card = (
        f"📌 **Ваша персональная карта питания**\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👤 Параметры: {u['height']} см | {u['weight']} кг | {u['age']} лет\n"
        f"🏋️ Уровень: {u['exp_level']} ({u['workouts']} тр./нед.)\n"
        f"🎯 Цель: **{u['goal']}**\n"
        f"🛒 Магазины: {u['stores']} (бюджет ~{u['budget']} ₽/нед.)\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 **Дневная норма БЖУ:**\n"
        f"• Белки: **{calc['p']} г**\n"
        f"• Жиры: **{calc['f']} г**\n"
        f"• Углеводы: **{calc['c']} г**\n"
        f"• Клетчатка: **{calc['fiber']} г**\n"
        f"💡 _{calc['note']}_"
    )
    await bot.send_message(message.chat.id, summary_card, parse_mode="Markdown")

    high_p_advice = ""
    if calc["p"] >= 165:
        high_p_advice = (
            "\n\n💡 **Совет тренера:** При высокой норме белка кушать 5 раз в день только обычной твердой пищей бывает тяжело. "
            "Большинство людей в таком случае питаются **3–4 раза в день**, а остаток добирают порцией протеина или гейнера."
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Да, готов ({calc['rec_meals']} приемов)", callback_data="meals_ready_yes")],
        [InlineKeyboardButton(text="❌ Нет, хочу меньше приемов", callback_data="meals_ready_no")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="onboard_back")]
    ])

    q_text = (
        f"🍽 Чтобы комфортно усвоить вашу норму (по ~30 г белка за раз без перегрузки ЖКТ), "
        f"рекомендуется **{calc['rec_meals']} приемов пищи** в день.{high_p_advice}\n\n"
        f"Готовы ли вы питаться **{calc['rec_meals']} раза в день**?"
    )
    new_prompt = await bot.send_message(message.chat.id, q_text, parse_mode="Markdown", reply_markup=kb)
    try:
        await message.delete()
    except Exception:
        pass
    await state.update_data(prompt_mid=new_prompt.message_id)
    await state.set_state(OnboardingSG.meals_ready_choice)

@router.callback_query(OnboardingSG.meals_ready_choice, F.data == "meals_ready_yes")
async def process_meals_ready_yes(callback: CallbackQuery, state: FSMContext):
    u = get_user(callback.from_user.id)
    update_user(callback.from_user.id, actual_meals=u["rec_meals"])
    await ask_sport_nutr_choice(callback.message, state)

@router.callback_query(OnboardingSG.meals_ready_choice, F.data == "meals_ready_no")
async def process_meals_ready_no(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OnboardingSG.meals_count_input)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="3 приема", callback_data="setmeals_3"),
         InlineKeyboardButton(text="4 приема", callback_data="setmeals_4")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="onboard_back")]
    ])
    await safe_edit(callback.message, "Выберите комфортное количество приемов пищи в день (3 или 4):", kb)

@router.callback_query(OnboardingSG.meals_count_input, F.data.startswith("setmeals_"))
async def process_setmeals(callback: CallbackQuery, state: FSMContext):
    m_cnt = int(callback.data.split("_")[1])
    update_user(callback.from_user.id, actual_meals=m_cnt)
    await ask_sport_nutr_choice(callback.message, state)

async def ask_sport_nutr_choice(message: Message, state: FSMContext):
    await state.set_state(OnboardingSG.sport_nutr_choice)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🥤 Со спортивным питанием", callback_data="sportnutr_1")],
        [InlineKeyboardButton(text="🍗 Без него (только обычная еда)", callback_data="sportnutr_0")]
    ])
    await safe_edit(
        message,
        "Как составить ваш рацион: **со спортивным питанием** (протеин/гейнер поможет легко закрыть норму) или **без него**?",
        kb
    )

@router.callback_query(OnboardingSG.sport_nutr_choice, F.data.startswith("sportnutr_"))
async def process_sport_nutr_final(callback: CallbackQuery, state: FSMContext):
    use_sn = int(callback.data.split("_")[1])
    uid = callback.from_user.id
    u = get_user(uid)

    # Если человек выбрал меньше приемов пищи и БЕЗ спортпита — ограничиваем БЖУ под реальное усвоение за прием (до 35г белка и 75г углеводов)
    if use_sn == 0 and u["actual_meals"] < u["rec_meals"]:
        max_p = u["actual_meals"] * 35
        max_c = u["actual_meals"] * 75
        new_p = min(u["target_p"], max_p)
        new_c = min(u["target_c"], max_c)
        update_user(uid, use_sport_nutr=0, target_p=new_p, target_c=new_c)
    else:
        update_user(uid, use_sport_nutr=use_sn)

    await state.clear()
    await safe_edit(callback.message, "⏳ **Составляю ваш рацион на 7 дней из простых и доступных блюд...**\n_Это займет около 15–20 секунд._")

    u = get_user(uid)
    diet_json = await generate_weekly_diet_json(u, u["diet_start_date"])
    if not diet_json or "days" not in diet_json:
        await safe_edit(callback.message, "⚠️ Сервер ИИ временно перегружен. Нажмите кнопку ниже, чтобы повторить генерацию:",
                        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Повторить", callback_data="regen_full_diet")]]))
        return

    update_user(
        uid,
        current_diet_json=json.dumps(diet_json, ensure_ascii=False),
        recipes_cache_json="{}",
        shopping_list_cache=None,
        last_month_check=get_msk_now().strftime("%Y-%m-%d")
    )

    text, kb = render_diet_day(uid, 0)
    await safe_edit(callback.message, text, kb)

    # Предлагаем вторую функцию (Продуктовый контроль) ровно 1 раз, если она еще не задействована
    u = get_user(uid)
    prods = get_products_db(uid)
    if not prods and not u.get("offered_fridge"):
        update_user(uid, offered_fridge=1)
        offer_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 Попробовать Контроль продуктов", callback_data="switch_to_fridge")]
        ])
        await bot.send_message(
            callback.message.chat.id,
            "💡 **Кстати!** У меня есть вторая удобная функция — **«📦 Продуктовый контроль»**.\n"
            "Вы можете заносить туда купленные продукты со сроками годности, и я заранее предупрежу вас, прежде чем что-то испортится в холодильнике!",
            parse_mode="Markdown",
            reply_markup=offer_kb
        )

# Защита от ответов не по плану во время анкеты
@router.message(OnboardingSG.bju_choice)
@router.message(OnboardingSG.exp_level)
@router.message(OnboardingSG.goal)
@router.message(OnboardingSG.high_weight_confirm)
@router.message(OnboardingSG.has_supplements)
@router.message(OnboardingSG.meals_ready_choice)
@router.message(OnboardingSG.meals_count_input)
@router.message(OnboardingSG.sport_nutr_choice)
async def fallback_onboarding_buttons_only(message: Message):
    await try_delete_user_msg(message)
    warn = await message.answer("⚠️ Ответьте на предыдущий вопрос! Бот ждет вашего ответа по плану (нажмите на одну из кнопок выше).")
    await asyncio.sleep(3.5)
    try:
        await warn.delete()
    except Exception:
        pass

# ==========================================
# ОТОБРАЖЕНИЕ РАЦИОНА, РЕЦЕПТОВ, ЗАМЕН И СПИСКА ПОКУПОК
# ==========================================
def render_diet_day(uid: int, day_idx: int):
    u = get_user(uid)
    diet = json.loads(u["current_diet_json"])
    days = diet.get("days", [])
    if day_idx >= len(days):
        day_idx = 0
    d_obj = days[day_idx]

    # Вычисляем реальные даты со следующего дня (или от даты старта рациона)
    try:
        if u["diet_start_date"]:
            start_dt = datetime.strptime(u["diet_start_date"], "%Y-%m-%d")
        else:
            start_dt = get_msk_now() + timedelta(days=1)
    except Exception:
        start_dt = get_msk_now() + timedelta(days=1)

    real_full_date = (start_dt + timedelta(days=day_idx)).strftime("%d.%m")
    meals = d_obj.get("meals", [])

    text = (
        f"🥗 **Рацион на {real_full_date}**\n"
        f"🎯 Дневная цель: **Б:** {u['target_p']}г | **Ж:** {u['target_f']}г | **У:** {u['target_c']}г\n"
    )
    if u["expert_note"]:
        text += f"💬 _{u['expert_note']}_\n"
    text += "━━━━━━━━━━━━━━━\n\n"

    kb = []
    for m_idx, m in enumerate(meals):
        if isinstance(m, str):
            m = {"num": m_idx + 1, "title": m}
        m_num = m.get("num", m_idx + 1)
        m_title = (
            m.get("title") or m.get("dish") or m.get("name") or
            m.get("meal") or m.get("description") or m.get("text") or
            m.get("items") or m.get("short_ingredients") or f"Прием пищи #{m_num}"
        )
        m_weight = m.get("weight") or m.get("grams") or m.get("portion") or "350 г"
        m_p = m.get("p", m.get("b", m.get("protein", m.get("proteins", 0))))
        m_f = m.get("f", m.get("j", m.get("fat", m.get("fats", 0))))
        m_c = m.get("c", m.get("u", m.get("carbs", m.get("carbohydrates", 0))))
        text += (
            f"🍽 **{m_num}. {m_title}**\n"
            f"⚖️ Порция: {m_weight}\n"
            f"📊 Б: {m_p}г | Ж: {m_f}г | У: {m_c}г\n\n"
        )
        kb.append([
            InlineKeyboardButton(text=f"📖 Рецепт #{m_num}", callback_data=f"recipe_{day_idx}_{m_idx}"),
            InlineKeyboardButton(text=f"🔄 Заменить #{m_num}", callback_data=f"repmeal_{day_idx}_{m_idx}")
        ])

    # Кнопки внизу — строго числа месяца (например 14, 15, 16...) в соответствии с рационом
    day_btns = []
    for i in range(len(days)):
        day_num_only = (start_dt + timedelta(days=i)).strftime("%d")
        mark = f"• {day_num_only} •" if i == day_idx else day_num_only
        day_btns.append(InlineKeyboardButton(text=mark, callback_data=f"viewday_{i}"))

    kb.append(day_btns[:4])
    if len(day_btns) > 4:
        kb.append(day_btns[4:])

    kb.append([
        InlineKeyboardButton(text=f"🔄 Заменить {real_full_date}", callback_data=f"repday_{day_idx}"),
        InlineKeyboardButton(text="🔄 Заменить весь рацион", callback_data="regen_full_diet")
    ])
    kb.append([
        InlineKeyboardButton(text="🚫 Убрать блюдо", callback_data="remove_specific_dish"),
        InlineKeyboardButton(text="🛒 Список продуктов к покупке", callback_data="view_shopping_list")
    ])
    kb.append([
        InlineKeyboardButton(text="🥑 Вписать купленный продукт", callback_data="insert_bought_prod"),
        InlineKeyboardButton(text="⏭ Рацион на след. неделю", callback_data="gen_next_week")
    ])
    kb.append([InlineKeyboardButton(text="📦 Контроль продуктов", callback_data="switch_to_fridge")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_modes")])

    return text, InlineKeyboardMarkup(inline_keyboard=kb)

@router.callback_query(F.data.startswith("viewday_"))
async def cb_view_day(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    day_idx = int(callback.data.split("_")[1])
    text, kb = render_diet_day(callback.from_user.id, day_idx)
    await safe_edit(callback.message, text, kb)

@router.callback_query(F.data.startswith("recipe_"))
async def cb_show_recipe(callback: CallbackQuery):
    _, d_idx, m_idx = callback.data.split("_")
    d_idx, m_idx = int(d_idx), int(m_idx)
    uid = callback.from_user.id
    u = get_user(uid)
    diet = json.loads(u["current_diet_json"])
    meal = diet["days"][d_idx]["meals"][m_idx]
    if isinstance(meal, str):
        meal = {"num": m_idx + 1, "title": meal}

    m_title = (
        meal.get("title") or meal.get("dish") or meal.get("name") or
        meal.get("meal") or meal.get("description") or meal.get("text") or f"Прием пищи #{m_idx + 1}"
    )
    m_weight = meal.get("weight") or meal.get("grams") or "350 г"
    m_p = meal.get("p", meal.get("b", 30))
    m_f = meal.get("f", meal.get("j", 15))
    m_c = meal.get("c", meal.get("u", 50))
    m_ingr = meal.get("short_ingredients") or m_title

    if meal.get("recipe") and len(str(meal.get("recipe"))) > 30:
        rec_text = meal["recipe"]
    else:
        cache = json.loads(u["recipes_cache_json"] or "{}")
        cache_key = f"{d_idx}_{m_idx}_{m_title}"
        if cache_key not in cache:
            await safe_edit(callback.message, f"👨‍🍳 **Готовлю подробный пошаговый рецепт для:**\n_{m_title}_...")
            prompt = (
                f"Напиши простой, понятный и подробный пошаговый рецепт приготовления блюда:\n"
                f"Блюдо: {m_title}\n"
                f"Ингредиенты: {m_ingr}\n"
                f"Вес порции: {m_weight} (Б: {m_p}г, Ж: {m_f}г, У: {m_c}г).\n"
                f"Распиши точные граммовки и шаги приготовления без воды."
            )
            rec_text = await ask_ai(prompt, temperature=0.3)
            cache[cache_key] = rec_text
            update_user(uid, recipes_cache_json=json.dumps(cache, ensure_ascii=False))
        else:
            rec_text = cache[cache_key]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Вернуться обратно", callback_data=f"viewday_{d_idx}")]
    ])
    await safe_edit(callback.message, f"📖 **Рецепт: {m_title}**\n\n{rec_text}", kb)

@router.callback_query(F.data.startswith("repmeal_"))
async def cb_replace_meal(callback: CallbackQuery):
    _, d_idx, m_idx = callback.data.split("_")
    d_idx, m_idx = int(d_idx), int(m_idx)
    uid = callback.from_user.id
    u = get_user(uid)
    diet = json.loads(u["current_diet_json"])
    old_meal = diet["days"][d_idx]["meals"][m_idx]
    if isinstance(old_meal, str):
        old_meal = {"num": m_idx + 1, "title": old_meal}

    old_title = old_meal.get("title") or old_meal.get("dish") or old_meal.get("name") or f"Прием пищи #{m_idx + 1}"
    old_num = old_meal.get("num", m_idx + 1)
    old_p = old_meal.get("p", old_meal.get("b", 30))
    old_f = old_meal.get("f", old_meal.get("j", 15))
    old_c = old_meal.get("c", old_meal.get("u", 50))

    await safe_edit(callback.message, f"🔄 **Подбираю простую и доступную замену вместо:**\n_{old_title}_...")
    prompt = f"""Замени этот прием пищи на другой из простых базовых продуктов (магазины: {u['stores']}, аллергии: {u['allergies']}).
Старое блюдо (НЕ ПОВТОРЯТЬ): {old_title}
Ориентировочное БЖУ этого приема пищи: Белки ~{old_p}г, Жиры ~{old_f}г, Углеводы ~{old_c}г.
Никаких дорогих или редких ингредиентов!
Верни СТРОГО JSON одного приема пищи:
{{"num": {old_num}, "title": "Полное название блюда и состав с граммовками", "weight": "350 г", "p": {old_p}, "f": {old_f}, "c": {old_c}, "short_ingredients": "..."}}"""

    res = await ask_ai(prompt, system="Отвечай строго валидным JSON.", temperature=0.5)
    try:
        new_meal = json.loads(clean_json_str(res))
        diet["days"][d_idx]["meals"][m_idx] = new_meal
        update_user(uid, current_diet_json=json.dumps(diet, ensure_ascii=False), shopping_list_cache=None)
    except Exception:
        pass

    text, kb = render_diet_day(uid, d_idx)
    await safe_edit(callback.message, text, kb)

@router.callback_query(F.data.startswith("repday_"))
async def cb_replace_day(callback: CallbackQuery):
    d_idx = int(callback.data.split("_")[1])
    uid = callback.from_user.id
    u = get_user(uid)
    diet = json.loads(u["current_diet_json"])
    day_date = diet["days"][d_idx]["date"]

    await safe_edit(callback.message, f"🔄 **Полностью обновляю меню на {day_date}...**")
    prompt = f"""Составь новое меню на 1 день ({day_date}) из простых базовых продуктов (магазины: {u['stores']}, аллергии: {u['allergies']}).
Дневная цель БЖУ: Белки {u['target_p']}г, Жиры {u['target_f']}г, Углеводы {u['target_c']}г.
Количество приемов пищи: {u['actual_meals']}.
Никаких уток, форели и дорогих стейков — только простые повседневные блюда.
Верни СТРОГО JSON объекта дня:
{{"date": "{day_date}", "meals": [{{"num": 1, "title": "...", "weight": "...", "p": 30, "f": 15, "c": 50, "short_ingredients": "..."}}]}}"""

    res = await ask_ai(prompt, system="Отвечай строго валидным JSON.", temperature=0.5)
    try:
        new_day = json.loads(clean_json_str(res))
        if "meals" in new_day:
            diet["days"][d_idx] = new_day
            update_user(uid, current_diet_json=json.dumps(diet, ensure_ascii=False), shopping_list_cache=None)
    except Exception:
        pass

    text, kb = render_diet_day(uid, d_idx)
    await safe_edit(callback.message, text, kb)

@router.callback_query(F.data == "regen_full_diet")
async def cb_regen_full_diet(callback: CallbackQuery):
    uid = callback.from_user.id
    u = get_user(uid)
    await safe_edit(callback.message, "🔄 **Составляю полностью новый рацион на 7 дней...**\n_Подождите 15–20 секунд._")
    start_dt = u["diet_start_date"] or (get_msk_now() + timedelta(days=1)).strftime("%Y-%m-%d")
    new_diet = await generate_weekly_diet_json(u, start_dt)
    if new_diet and "days" in new_diet:
        update_user(uid, current_diet_json=json.dumps(new_diet, ensure_ascii=False), recipes_cache_json="{}", shopping_list_cache=None)
    text, kb = render_diet_day(uid, 0)
    await safe_edit(callback.message, text, kb)

@router.callback_query(F.data == "remove_specific_dish")
async def cb_remove_dish_prompt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(DietReplaceSG.removing_dish)
    await state.update_data(prompt_mid=callback.message.message_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Вернуться обратно", callback_data="viewday_0")]])
    await safe_edit(
        callback.message,
        "🚫 Напишите название блюда или продукта, который вам не нравится (я уберу его из рациона и заменю на другое доступное блюдо):",
        kb
    )

@router.message(DietReplaceSG.removing_dish)
async def process_remove_dish(message: Message, state: FSMContext):
    dish = (message.text or "").strip()
    data = await state.get_data()
    prompt_mid = data.get("prompt_mid")
    await try_delete_user_msg(message)
    await state.clear()

    uid = message.from_user.id
    u = get_user(uid)
    await bot.edit_message_text(f"🔄 Убираю **{dish}** и перестраиваю ваш рацион...", chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown")
    new_diet = await generate_weekly_diet_json(u, u["diet_start_date"], forbidden_dish=dish)
    if new_diet and "days" in new_diet:
        update_user(uid, current_diet_json=json.dumps(new_diet, ensure_ascii=False), shopping_list_cache=None)
    text, kb = render_diet_day(uid, 0)
    await bot.edit_message_text(text, chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=kb)

@router.callback_query(F.data == "view_shopping_list")
async def cb_view_shopping_list(callback: CallbackQuery):
    uid = callback.from_user.id
    u = get_user(uid)
    if not u.get("shopping_list_cache"):
        await safe_edit(callback.message, f"🛒 **Составляю список продуктов и проверяю цены в магазинах ({u['stores']})...**")
        diet = json.loads(u["current_diet_json"])
        shop_text = await generate_shopping_list_ai(u, diet)
        update_user(uid, shopping_list_cache=shop_text)
    else:
        shop_text = u["shopping_list_cache"]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Пересчитать корзину", callback_data="refresh_shopping_list")],
        [InlineKeyboardButton(text="🔙 Вернуться к рациону", callback_data="viewday_0")]
    ])
    await safe_edit(callback.message, f"🛒 **Список продуктов к покупке на неделю**\n\n{shop_text}", kb)

@router.callback_query(F.data == "refresh_shopping_list")
async def cb_refresh_shopping_list(callback: CallbackQuery):
    update_user(callback.from_user.id, shopping_list_cache=None)
    await cb_view_shopping_list(callback)

@router.callback_query(F.data == "gen_next_week")
async def cb_gen_next_week(callback: CallbackQuery):
    uid = callback.from_user.id
    u = get_user(uid)
    await safe_edit(callback.message, "⏭ **Формирую новый рацион на следующую неделю (на 7 дней вперед)...**")
    next_start = (get_msk_now() + timedelta(days=7)).strftime("%Y-%m-%d")
    next_diet = await generate_weekly_diet_json(u, next_start)
    if next_diet and "days" in next_diet:
        update_user(uid, current_diet_json=json.dumps(next_diet, ensure_ascii=False), diet_start_date=next_start, shopping_list_cache=None)
    text, kb = render_diet_day(uid, 0)
    await safe_edit(callback.message, text, kb)

# ==========================================
# ВПИСАТЬ КУПЛЕННЫЙ ПРОДУКТ В РАЦИОН
# ==========================================
@router.callback_query(F.data == "insert_bought_prod")
async def cb_insert_bought_prod(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProductSG.waiting_bought_product)
    await state.update_data(prompt_mid=callback.message.message_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Вернуться обратно", callback_data="viewday_0")]])
    await safe_edit(
        callback.message,
        "🥑 **Вписать купленный продукт в рацион**\n\n"
        "Напишите, какой продукт вы купили и его вес/количество (например: `Творог 5% 400г` или `Стейк индейки 500г`).\n"
        "ИИ-тренер посчитает его БЖУ и аккуратно впишет его в ваш текущий рацион:",
        kb
    )

@router.message(ProductSG.waiting_bought_product)
async def process_insert_bought_prod(message: Message, state: FSMContext):
    prod_txt = (message.text or "").strip()
    data = await state.get_data()
    prompt_mid = data.get("prompt_mid")
    await try_delete_user_msg(message)
    await state.clear()

    uid = message.from_user.id
    u = get_user(uid)
    if not u.get("current_diet_json"):
        await bot.edit_message_text(
            "⚠️ У вас еще не создан рацион! Перейдите в режим «🥗 Рацион», чтобы составить меню.",
            chat_id=message.chat.id, message_id=prompt_mid,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🥗 Создать рацион", callback_data="mode_diet")]])
        )
        return

    await bot.edit_message_text(f"⏳ Считаю БЖУ продукта **{prod_txt}** и вписываю его в ваш рацион...", chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown")
    diet = json.loads(u["current_diet_json"])
    prompt = f"""Пользователь дополнительно купил продукт: "{prod_txt}".
Его целевое дневное БЖУ: Белки {u['target_p']}г, Жиры {u['target_f']}г, Углеводы {u['target_c']}г.
Текущий рацион: {json.dumps(diet, ensure_ascii=False)}

1. Посчитай БЖУ купленного продукта.
2. Впиши его в ближайшие дни рациона (заменив или скорректировав подходящие приемы пищи так, чтобы дневная норма БЖУ не нарушилась).
Верни СТРОГО JSON формата:
{{
  "trainer_comment": "Краткий совет тренера: сколько БЖУ в продукте и в какие дни/приемы пищи он был вписан",
  "updated_diet": {{ ...полный обновленный объект рациона с массивом days... }}
}}"""

    res = await ask_ai(prompt, system="Отвечай строго валидным JSON.", temperature=0.3)
    comment = f"Продукт «{prod_txt}» успешно учтен в вашем рационе!"
    try:
        parsed = json.loads(clean_json_str(res))
        if "updated_diet" in parsed and "days" in parsed["updated_diet"]:
            update_user(uid, current_diet_json=json.dumps(parsed["updated_diet"], ensure_ascii=False), shopping_list_cache=None)
        if parsed.get("trainer_comment"):
            comment = parsed["trainer_comment"]
    except Exception:
        pass

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🥗 Открыть обновленный рацион", callback_data="viewday_0")]])
    await bot.edit_message_text(f"✅ **Готово!**\n\n💬 {comment}", chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=kb)

# ==========================================
# ПРОДУКТОВЫЙ КОНТРОЛЬ (СРОКИ ГОДНОСТИ)
# ==========================================
@router.callback_query(F.data.in_({"mode_products", "switch_to_fridge", "back_to_fridge"}))
async def start_products_mode(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = callback.from_user.id
    update_user(uid, mode="products")
    await show_product_control(uid, callback.message)

    # Однократное ненавязчивое предложение функции «Рацион», если она еще не заполнена
    u = get_user(uid)
    if not u.get("current_diet_json") and not u.get("offered_diet"):
        update_user(uid, offered_diet=1)
        offer_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🥗 Попробовать персональный Рацион", callback_data="mode_diet")]
        ])
        await bot.send_message(
            callback.message.chat.id,
            "💡 **Кстати!** Я также умею составлять персональный **«🥗 Рацион питания»** под ваши цели, бюджет и магазины с подробными рецептами и списком покупок!",
            parse_mode="Markdown",
            reply_markup=offer_kb
        )

async def show_product_control(uid: int, message: Message):
    prods = get_products_db(uid)
    now_date = get_msk_now().date()
    lines = ["📦 **Продуктовый контроль (Ваш холодильник):**\n"]

    if not prods:
        lines.append("_Ваш список пока пуст. Добавьте продукты, чтобы я следил за их сроком годности!_")
    else:
        for idx, p in enumerate(prods, 1):
            exp_dt = datetime.strptime(p["exp_date"], "%d.%m.%Y").date()
            diff = (exp_dt - now_date).days
            if diff < 0:
                status = f"❌ **СТУХ!** ({p['exp_date']})"
            elif diff == 0:
                status = f"⚠️ Истекает сегодня! ({p['exp_date']})"
            elif diff == 1:
                status = f"⚠️ Стухнет завтра! ({p['exp_date']})"
            elif diff <= 5:
                status = f"⏳ Осталось {diff} дн. (до {p['exp_date']})"
            else:
                status = f"✅ до {p['exp_date']}"
            lines.append(f"{idx}. **{p['name']}** — {status}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить продукт", callback_data="prod_add"),
         InlineKeyboardButton(text="➖ Убрать продукт", callback_data="prod_del")],
        [InlineKeyboardButton(text="🥑 Вписать продукт в рацион", callback_data="insert_bought_prod")],
        [InlineKeyboardButton(text="🥗 Перейти в Рацион", callback_data="mode_diet")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_modes")]
    ])
    await safe_edit(message, "\n".join(lines), kb)

def render_draft_screen(items: list):
    lines = "\n".join([f"{i + 1}. {it['name']} — {it['date']}" for i, it in enumerate(items)])
    text = (
        f"📝 **Черновик продуктов (проверьте перед сохранением):**\n\n"
        f"{lines}\n\n"
        f"Всё верно? Вы можете подтвердить сохранение или исправить список:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить и сохранить", callback_data="draft_confirm")],
        [InlineKeyboardButton(text="✏️ Исправить черновик", callback_data="draft_edit")],
        [InlineKeyboardButton(text="➕ Добавить ещё", callback_data="prod_add")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="prod_add")]
    ])
    return text, kb

async def parse_products_with_ai(raw_text: str) -> list:
    now_str = get_msk_now().strftime("%d.%m.%Y")
    prompt = f"""Сегодняшняя дата: {now_str}.
Пользователь передал список продуктов для холодильника:
"{raw_text}"

Правила разбора:
1. Выдели каждый продукт, его фирму (если названа) и количество.
2. Если пользователь назвал разное количество с разными сроками (например: "2 творога Твой день до 12.02.2026 и 3 до 15.02.2026"), создай две отдельные записи:
   - "Творог Твой день (2 шт.)" со сроком "12.02.2026"
   - "Творог Твой день (3 шт.)" со сроком "15.02.2026"
3. Если у свежего продукта без маркировки (мясо, овощи, фрукты) не назван срок годности, сам рассчитай среднестатистический срок хранения в холодильнике от сегодняшней даты ({now_str}).
4. Верни СТРОГО JSON массив объектов формата:
[
  {{"name": "Название продукта", "date": "ДД.ММ.ГГГГ"}}
]"""
    res = await ask_ai(prompt, system="Отвечай строго валидным JSON массивом.", temperature=0.1)
    try:
        arr = json.loads(clean_json_str(res))
        valid = []
        for item in arr:
            n = str(item.get("name", "")).strip()
            d = str(item.get("date", "")).strip()
            datetime.strptime(d, "%d.%m.%Y")
            if n:
                valid.append({"name": n, "date": d})
        return valid
    except Exception:
        return []

@router.callback_query(F.data == "prod_add")
async def prod_add_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    webapp_url = (
        f"{SCANNER_WEBAPP_URL}?bot={BOT_TOKEN}"
        f"&or={OPENROUTER_API_KEY}"
        f"&cid={callback.message.chat.id}"
        f"&mid={callback.message.message_id}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 Сканировать ЧЗ", web_app=WebAppInfo(url=webapp_url))],
        [
            InlineKeyboardButton(text="🎙 Голосовое", callback_data="prod_add_voice"),
            InlineKeyboardButton(text="✍️ Вручную", callback_data="prod_add_manual")
        ],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_fridge")]
    ])
    await safe_edit(
        callback.message,
        "➕ **Добавление продуктов**\n\n"
        "Выберите способ добавления:\n"
        "⚠️ _Продукты без маркировки (свежие овощи, фрукты, мясо на развес) вводятся вручную._",
        kb
    )

@router.callback_query(F.data == "prod_add_manual")
async def prod_add_manual_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProductSG.waiting_product_input)
    await state.update_data(prompt_mid=callback.message.message_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="prod_add")]])
    await safe_edit(
        callback.message,
        "✍️ **Ручной ввод**\n\n"
        "Напишите название продукта (желательно с фирмой) и дату окончания срока годности:\n"
        "`Сыр пармезан 12.02.2027`\n\n"
        "💡 _Для свежих продуктов без маркировки можно написать просто название — бот сам рассчитает средний срок хранения._",
        kb
    )

@router.callback_query(F.data == "prod_add_voice")
async def prod_add_voice_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProductSG.waiting_voice_input)
    await state.update_data(prompt_mid=callback.message.message_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="prod_add")]])
    await safe_edit(
        callback.message,
        "🎙 **Голосовой ввод**\n\n"
        "Запишите голосовое сообщение: назовите продукт и срок годности (**желательно с фирмой**, чтобы не перепутать разные марки).\n\n"
        "💡 _Например: «2 творога Твой день до 12.02.2026 и 3 до 15.02.2026»._",
        kb
    )

@router.message(ProductSG.waiting_product_input)
async def prod_add_process(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    data = await state.get_data()
    prompt_mid = data.get("prompt_mid")
    await try_delete_user_msg(message)

    m = re.match(r"^(.+?)\s+(\d{2}\.\d{2}\.\d{4})$", txt)
    items = []
    if m and "," not in txt and "\n" not in txt and " и " not in txt.lower():
        name, dt_str = m.group(1).strip(), m.group(2).strip()
        try:
            datetime.strptime(dt_str, "%d.%m.%Y")
            items = [{"name": name, "date": dt_str}]
        except ValueError:
            items = []

    if not items:
        items = await parse_products_with_ai(txt)

    if not items:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="prod_add")]])
        err_text = "⚠️ Не удалось распознать продукт и дату. Попробуйте написать, например: `Сыр Ламбер 12.02.2027`"
        if prompt_mid:
            try:
                await bot.edit_message_text(err_text, chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=kb)
                return
            except Exception:
                pass
        await message.answer(err_text, parse_mode="Markdown", reply_markup=kb)
        return

    existing = data.get("draft_items", [])
    existing.extend(items)
    await state.update_data(draft_items=existing)
    draft_text, draft_kb = render_draft_screen(existing)

    if prompt_mid:
        try:
            await bot.edit_message_text(draft_text, chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=draft_kb)
            return
        except Exception:
            pass
    await message.answer(draft_text, parse_mode="Markdown", reply_markup=draft_kb)

@router.message(ProductSG.waiting_voice_input, F.voice)
async def prod_voice_process(message: Message, state: FSMContext):
    import subprocess
    import tempfile
    data = await state.get_data()
    prompt_mid = data.get("prompt_mid")
    await try_delete_user_msg(message)

    if prompt_mid:
        try:
            await bot.edit_message_text("⏳ **Распознаю голосовое сообщение...**", chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown")
        except Exception:
            pass

    file_info = await bot.get_file(message.voice.file_id)
    bio = io.BytesIO()
    await bot.download_file(file_info.file_path, destination=bio)
    ogg_bytes = bio.getvalue()

    def _transcribe_voice_sync() -> str:
        with tempfile.TemporaryDirectory() as tmpdir:
            ogg_path = os.path.join(tmpdir, "voice.ogg")
            wav_path = os.path.join(tmpdir, "voice.wav")
            with open(ogg_path, "wb") as f:
                f.write(ogg_bytes)

            # Конвертируем голосовой .ogg в чистый .wav
            subprocess.run(
                ["ffmpeg", "-y", "-i", ogg_path, "-ar", "16000", "-ac", "1", wav_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )

            # 1. Точное распознавание русской речи через Google SpeechRecognition
            if os.path.exists(wav_path):
                try:
                    import speech_recognition as sr
                    rec = sr.Recognizer()
                    with sr.AudioFile(wav_path) as source:
                        audio_data = rec.record(source)
                    text_ru = rec.recognize_google(audio_data, language="ru-RU")
                    if text_ru:
                        return text_ru.strip()
                except Exception as e:
                    logging.warning(f"SR error: {e}")

            # 2. Резервный канал через мультимодальные модели OpenRouter (WAV)
            if os.path.exists(wav_path):
                with open(wav_path, "rb") as wf:
                    b64_wav = base64.b64encode(wf.read()).decode("utf-8")
                headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
                for m_name in ["google/gemini-2.0-flash-exp:free", "google/gemini-2.0-flash-lite-preview-02-05:free"]:
                    try:
                        payload = {
                            "model": m_name,
                            "temperature": 0.1,
                            "messages": [{
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "Напиши дословно на русском языке, что сказано в аудиозаписи."},
                                    {"type": "input_audio", "input_audio": {"data": b64_wav, "format": "wav"}}
                                ]
                            }]
                        }
                        r = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=40)
                        if r.status_code == 200:
                            ans = r.json()["choices"][0]["message"]["content"].strip()
                            if ans:
                                return ans
                    except Exception:
                        pass
        return ""

    spoken_text = await asyncio.to_thread(_transcribe_voice_sync)
    items = []
    if spoken_text:
        items = await parse_products_with_ai(spoken_text)

    if not items:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="prod_add")]])
        err_msg = "⚠️ Не удалось разобрать продукты и даты в голосовом сообщении. Попробуйте записать ещё раз."
        if prompt_mid:
            await bot.edit_message_text(err_msg, chat_id=message.chat.id, message_id=prompt_mid, reply_markup=kb)
        else:
            await message.answer(err_msg, reply_markup=kb)
        return

    existing = data.get("draft_items", [])
    existing.extend(items)
    await state.update_data(draft_items=existing)
    draft_text, draft_kb = render_draft_screen(existing)

    if prompt_mid:
        await bot.edit_message_text(draft_text, chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=draft_kb)
    else:
        await message.answer(draft_text, parse_mode="Markdown", reply_markup=draft_kb)

def extract_draft_from_message_text(msg_text: str) -> list:
    items = []
    for line in (msg_text or "").splitlines():
        m = re.match(r"^\d+\.\s+(.+?)\s+[—\-]\s+(\d{2}\.\d{2}\.\d{4})$", line.strip())
        if m:
            items.append({"name": m.group(1).strip(), "date": m.group(2).strip()})
    return items

@router.callback_query(F.data == "draft_confirm")
async def draft_confirm_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    items = data.get("draft_items") or extract_draft_from_message_text(callback.message.text)
    if not items:
        await callback.answer("Черновик пуст!", show_alert=True)
        return

    for it in items:
        add_product_db(callback.from_user.id, it["name"], it["date"])

    await state.clear()
    await safe_edit(callback.message, "✅ **Сохранено!**")
    await asyncio.sleep(0.8)
    await show_product_control(callback.from_user.id, callback.message)

@router.callback_query(F.data == "draft_edit")
async def draft_edit_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    items = data.get("draft_items") or extract_draft_from_message_text(callback.message.text)
    await state.set_state(ProductSG.waiting_draft_edit)
    await state.update_data(draft_items=items, prompt_mid=callback.message.message_id)

    current_lines = "\n".join([f"{it['name']} {it['date']}" for it in items])
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад к черновику", callback_data="draft_back")]])
    await safe_edit(
        callback.message,
        "✏️ **Исправление черновика**\n\n"
        "Скопируйте список ниже, исправьте нужные названия или даты и отправьте ответным сообщением:\n\n"
        f"`{current_lines}`",
        kb
    )

@router.callback_query(F.data == "draft_back")
async def draft_back_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    items = data.get("draft_items", [])
    if not items:
        await prod_add_menu(callback, state)
        return
    draft_text, draft_kb = render_draft_screen(items)
    await safe_edit(callback.message, draft_text, draft_kb)

@router.message(ProductSG.waiting_draft_edit)
async def draft_edit_save(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    data = await state.get_data()
    prompt_mid = data.get("prompt_mid")
    await try_delete_user_msg(message)

    new_items = []
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(.+?)\s+(\d{2}\.\d{2}\.\d{4})$", line)
        if m:
            new_items.append({"name": m.group(1).strip(), "date": m.group(2).strip()})

    if not new_items:
        new_items = await parse_products_with_ai(txt)

    if new_items:
        await state.update_data(draft_items=new_items)
        draft_text, draft_kb = render_draft_screen(new_items)
        if prompt_mid:
            await bot.edit_message_text(draft_text, chat_id=message.chat.id, message_id=prompt_mid, parse_mode="Markdown", reply_markup=draft_kb)
            return
        await message.answer(draft_text, parse_mode="Markdown", reply_markup=draft_kb)

@router.callback_query(F.data == "prod_del")
async def prod_del_list(callback: CallbackQuery):
    prods = get_products_db(callback.from_user.id)
    if not prods:
        await callback.answer("Список продуктов пуст!", show_alert=True)
        return
    kb = []
    for p in prods:
        kb.append([InlineKeyboardButton(text=f"❌ {p['name']} ({p['exp_date']})", callback_data=f"delprod_{p['id']}")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_fridge")])
    await safe_edit(callback.message, "Выберите продукт, который вы съели или выбросили:", InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("delprod_"))
async def prod_del_confirm(callback: CallbackQuery):
    pid = int(callback.data.split("_")[1])
    delete_product_db(pid, callback.from_user.id)
    await show_product_control(callback.from_user.id, callback.message)

# ==========================================
# ФОНОВЫЕ ЗАДАЧИ: УВЕДОМЛЕНИЯ, СОВЕТЫ В 12:00, КОНТРОЛЬ СРОКОВ
# ==========================================
async def send_daily_tip_to_user(uid: int):
    u = get_user(uid)
    hist = json.loads(u.get("tips_history_json") or "[]")
    avoid_str = "; ".join(hist[-25:]) if hist else "нет"
    prompt = (
        f"Напиши один короткий, научно обоснованный и практически полезный совет (3–5 предложений) "
        f"по тренировкам, восстановлению или спортивному питанию (цель пользователя: {u.get('goal') or 'здоровье и форма'}).\n"
        f"НЕ ПОВТОРЯЙ темы из этого списка: {avoid_str}."
    )
    tip = await ask_ai(prompt, temperature=0.7)
    if tip:
        short_topic = tip[:50].replace("\n", " ")
        hist.append(short_topic)
        update_user(uid, tips_history_json=json.dumps(hist[-50:], ensure_ascii=False))
        try:
            await bot.send_message(uid, f"💡 **Совет дня по питанию и тренировкам:**\n\n{tip}", parse_mode="Markdown")
        except Exception:
            pass

async def cron_minute_dispatcher():
    now = get_msk_now()
    cur_hm = now.strftime("%H:%M")
    cur_date_str = now.strftime("%d.%m")

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users")
    users = [dict(r) for r in c.fetchall()]
    conn.close()

    for u in users:
        uid = u["user_id"]
        nt = u.get("notify_time")

        # 1. Ежедневное утреннее напоминание рациона
        if nt and nt == cur_hm and u.get("current_diet_json"):
            diet = json.loads(u["current_diet_json"])
            days = diet.get("days", [])
            day_idx = 0
            for idx, d in enumerate(days):
                if d.get("date") == cur_date_str:
                    day_idx = idx
                    break
            text, kb = render_diet_day(uid, day_idx)
            try:
                await bot.send_message(uid, f"☀️ **Доброе утро! Ваш план питания на сегодня ({cur_date_str}):**\n\n{text}", parse_mode="Markdown", reply_markup=kb)
            except Exception:
                pass

            # На 7-й день рациона предлагаем составить рацион на следующую неделю
            if day_idx == len(days) - 1:
                try:
                    n_kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⏭ Составить рацион на след. неделю", callback_data="gen_next_week")]
                    ])
                    await bot.send_message(uid, "📆 Сегодня 7-й день вашего недельного рациона! Хотите сформировать свежее меню и список покупок на следующую неделю?", reply_markup=n_kb)
                except Exception:
                    pass

        # 2. Ежедневный совет в 12:00 МСК (с защитой от пересечения с утренним уведомлением в пределах 40 минут)
        if u.get("current_diet_json"):
            tip_hm = "12:00"
            if nt:
                nh, nm = map(int, nt.split(":"))
                diff_mins = abs((nh * 60 + nm) - (12 * 60))
                if diff_mins < 40:
                    shifted = (datetime(2000, 1, 1, nh, nm) + timedelta(minutes=40)).strftime("%H:%M")
                    tip_hm = shifted
            if cur_hm == tip_hm:
                asyncio.create_task(send_daily_tip_to_user(uid))

        # 3. Ежедневная проверка сроков годности продуктов в 09:00 МСК
        if cur_hm == "09:00":
            prods = get_products_db(uid)
            alerts = []
            for p in prods:
                exp_dt = datetime.strptime(p["exp_date"], "%d.%m.%Y").date()
                diff = (exp_dt - now.date()).days
                if diff == 5:
                    alerts.append(f"⏳ **{p['name']}** — до конца срока годности осталось 5 дней ({p['exp_date']}).")
                elif diff == 2:
                    alerts.append(f"⚠️ **{p['name']}** — осталось всего 2 дня ({p['exp_date']})!")
                elif diff == 1:
                    alerts.append(f"🚨 **{p['name']}** стухнет завтра ({p['exp_date']})!")
                elif diff <= 0:
                    alerts.append(f"❌ **{p['name']} стух! {p['exp_date']}** — выбросьте его и удалите из списка!")
            if alerts:
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📦 Открыть холодильник", callback_data="switch_to_fridge")]])
                try:
                    await bot.send_message(uid, "🔔 **Контроль сроков годности:**\n\n" + "\n".join(alerts), parse_mode="Markdown", reply_markup=kb)
                except Exception:
                    pass

        # 4. Ежемесячный опрос изменений (раз в 30 дней в 10:00)
        if cur_hm == "10:00" and u.get("last_month_check"):
            try:
                last_dt = datetime.strptime(u["last_month_check"], "%Y-%m-%d").date()
                if (now.date() - last_dt).days >= 30:
                    update_user(uid, last_month_check=now.strftime("%Y-%m-%d"))
                    m_kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔄 Обновить вес / цель / бюджет", callback_data="mode_diet")],
                        [InlineKeyboardButton(text="✅ Оставить всё как есть", callback_data="viewday_0")]
                    ])
                    await bot.send_message(
                        uid,
                        "🗓 **Прошел месяц тренировок и режима!**\n"
                        "Изменился ли ваш вес, цель, количество тренировок или бюджет? Мы можем пересчитать ваше БЖУ и обновить рацион!",
                        parse_mode="Markdown",
                        reply_markup=m_kb
                    )
            except Exception:
                pass

async def main():
    init_db()
    scheduler.add_job(cron_minute_dispatcher, "cron", second=0)
    scheduler.start()
    logging.info("Bot started successfully!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
