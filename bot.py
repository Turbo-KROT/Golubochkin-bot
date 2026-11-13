import asyncio
import json
import logging
import math
import os
import re
import sqlite3
from datetime import datetime

import pytz
import requests
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

MSK_TZ = pytz.timezone("Europe/Moscow")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

DB_NAME = "nutrition_bot.db"


# ==========================================
# 1. БАЗА ДАННЫХ (SQLite)
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            mode TEXT DEFAULT 'both',
            custom_bju TEXT,
            height INTEGER,
            weight REAL,
            age INTEGER,
            exp_level TEXT,
            workouts TEXT,
            goal TEXT,
            has_supplements INTEGER DEFAULT 0,
            supplements_json TEXT DEFAULT '{}',
            allergies TEXT,
            budget TEXT,
            stores TEXT,
            target_p INTEGER,
            target_f INTEGER,
            target_c INTEGER,
            target_fiber INTEGER,
            rec_meals INTEGER,
            actual_meals INTEGER,
            use_sport_nutr INTEGER DEFAULT 0,
            notify_time TEXT DEFAULT '08:00',
            current_diet_json TEXT,
            next_diet_json TEXT,
            recipes_cache_json TEXT DEFAULT '{}',
            diet_start_date TEXT,
            last_month_check TEXT,
            tips_history_json TEXT DEFAULT '[]'
        )
    """)
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


def get_user(user_id: int) -> dict:
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def save_user_field(user_id: int, **kwargs):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if not c.fetchone():
        c.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
    for k, v in kwargs.items():
        c.execute(f"UPDATE users SET {k} = ? WHERE user_id = ?", (v, user_id))
    conn.commit()
    conn.close()


def add_product_db(user_id: int, name: str, exp_date: str):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    now_str = datetime.now(MSK_TZ).strftime("%d.%m.%Y")
    c.execute(
        "INSERT INTO products (user_id, name, exp_date, added_at) VALUES (?, ?, ?, ?)",
        (user_id, name, exp_date, now_str),
    )
    conn.commit()
    conn.close()


def get_products_db(user_id: int) -> list:
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM products WHERE user_id = ? ORDER BY id ASC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_product_db(prod_id: int, user_id: int):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM products WHERE id = ? AND user_id = ?", (prod_id, user_id))
    conn.commit()
    conn.close()


# ==========================================
# 2. РАБОТА С НЕЙРОСЕТЬЮ (OpenRouter Free)
# ==========================================
class OpenRouterClient:
    def __init__(self):
        # Список лучших бесплатных моделей (если одна перегружена, бот сам переключится на следующую)
        self.free_models = [
            "deepseek/deepseek-chat-v3-0324:free",
            "deepseek/deepseek-chat:free",
            "qwen/qwen-2.5-72b-instruct:free",
            "google/gemini-2.0-flash-exp:free",
            "meta-llama/llama-3.3-70b-instruct:free",
        ]

    def ask(self, system_prompt: str, user_prompt: str, temperature: float = 0.5) -> str:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/Golubochkin-bot",
            "X-Title": "Golubochkin Nutrition Bot",
        }
        for model in self.free_models:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": temperature,
            }
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=90)
                if resp.status_code == 200:
                    data = resp.json()
                    if "choices" in data and len(data["choices"]) > 0:
                        content = data["choices"][0]["message"].get("content", "")
                        if content:
                            return content
                else:
                    logging.warning(f"Модель {model} вернула код {resp.status_code}: {resp.text}")
            except Exception as e:
                logging.error(f"Ошибка запроса к модели {model}: {e}")
        return ""


giga = OpenRouterClient()


# ==========================================
# 3. ФИЗИОЛОГИЧЕСКИЙ РАСЧЕТ БЖУ И ЛИМИТОВ
# ==========================================
def calculate_macros(user: dict) -> dict:
    """
    Жесткие правила:
    - За 1 прием обычной пищи усваивается МАКСИМУМ 30 г белка и 60-70 г углеводов.
    - Для набора массы ~2 г белка на 1 кг веса (при фарме/профи до 2.3-2.5 г).
    - Белок никогда не идет без углеводов и наоборот.
    """
    if user.get("custom_bju"):
        try:
            parts = [int(x) for x in re.findall(r"\d+", user["custom_bju"])]
            if len(parts) >= 3:
                p, f, c = parts[0], parts[1], parts[2]
                fiber = parts[3] if len(parts) >= 4 else 30
                rec_meals = max(4, math.ceil(p / 30.0), math.ceil(c / 65.0))
                return {"p": p, "f": f, "c": c, "fiber": fiber, "rec_meals": rec_meals}
        except Exception:
            pass

    weight = float(user.get("weight") or 75)
    goal = user.get("goal") or "Набор мышечной массы"
    exp = user.get("exp_level") or "Начинающий"
    supps_str = (user.get("supplements_json") or "").lower()

    uses_pharma = any(w in supps_str for w in ["тестостерон", "фарм", "трен", "суст", "омнадрен", "курс", "метан"])

    if "набор" in goal.lower():
        p_mult = 2.3 if (exp == "Профессионал" and uses_pharma) else 2.0
        c_mult = 4.0
        f_mult = 1.0
    else:  # Сушка
        p_mult = 2.2
        c_mult = 2.2
        f_mult = 0.8

    target_p = int(round(weight * p_mult))
    target_c = int(round(weight * c_mult))
    target_f = max(25, int(round(weight * f_mult)))
    target_fiber = 30

    # Считаем рекомендуемое количество приемов пищи исходя из лимита 30 г белка и 65 г углеводов за раз
    rec_meals = max(4, math.ceil(target_p / 30.0), math.ceil(target_c / 65.0))
    if rec_meals > 7:
        rec_meals = 6

    return {
        "p": target_p,
        "f": target_f,
        "c": target_c,
        "fiber": target_fiber,
        "rec_meals": rec_meals,
    }


def generate_weekly_diet_ai(user: dict, is_next_week: bool = False, exclude_dish: str = None) -> dict:
    """Генерирует структуру рациона на 7 дней в формате JSON с учетом всех правил пользователя."""
    meals_count = int(user.get("actual_meals") or user.get("rec_meals") or 5)
    use_sport = bool(user.get("use_sport_nutr"))
    target_p = int(user.get("target_p") or 140)
    target_f = int(user.get("target_f") or 60)
    target_c = int(user.get("target_c") or 280)

    # ЖЕСТКОЕ ПРАВИЛО: Если приемов пищи меньше рекомендуемого и БЕЗ спортпита —
    # НЕ впихиваем весь белок/угли, а ограничиваем по 30г белка и 65г углеводов на 1 прием пищи!
    if not use_sport:
        actual_daily_p = min(target_p, meals_count * 30)
        actual_daily_c = min(target_c, meals_count * 65)
    else:
        actual_daily_p = target_p
        actual_daily_c = target_c

    actual_daily_f = target_f

    products = get_products_db(user["user_id"])
    fridge_items = ", ".join([p["name"] for p in products]) if products else "нет данных (любые из указанных магазинов)"
    supps_data = json.loads(user.get("supplements_json") or "{}")
    supps_text = "; ".join([f"{k} (периодичность: {v})" for k, v in supps_data.items()]) if supps_data else "Нет"

    system_prompt = (
        "Ты — профессиональный спортивный диетолог и шеф-повар из России. "
        "Твоя задача — составить разнообразный и вкусный рацион на 7 дней строго в формате валидного JSON без лишнего текста."
    )

    sport_rule = (
        f"Пользователь выбрал рацион СО СПОРТИВНЫМ ПИТАНИЕМ (его добавки: {supps_text}). "
        "Впиши его добавки в приемы пищи или между ними, чтобы добрать норму БЖУ. "
        "ВАЖНЕЙШЕЕ ПРАВИЛО: Белок без углеводов и углеводы без белка НЕ употребляются! "
        "Если ты включаешь в прием пищи Протеин, ты ОБЯЗАН добавить к нему источник углеводов (например, банан, овсянку, хлебцы). "
        "Фирму добавок НЕ указывай, только порции."
        if use_sport
        else "Пользователь выбрал рацион БЕЗ СПОРТИВНОГО ПИТАНИЯ. Строго запрещено предлагать больше 30 г белка и 65 г углеводов за один прием пищи!"
    )

    exclude_rule = f"Исключи блюдо '{exclude_dish}' и не используй его!" if exclude_dish else ""
    week_note = "Это рацион на СЛЕДУЮЩУЮ неделю — сделай блюда максимально отличными от предыдущих!" if is_next_week else ""

    user_prompt = f"""
Составь рацион на 7 дней (Понедельник - Воскресенье).
Параметры пользователя:
- Рост: {user['height']} см, Вес: {user['weight']} кг, Возраст: {user['age']}
- Опыт: {user['exp_level']}, Тренировок в неделю: {user['workouts']}, Цель: {user['goal']}
- Противопоказания и аллергии: {user['allergies']}
- Бюджет на неделю: {user['budget']}
- Разрешенные магазины (ТОЛЬКО РФ): {user['stores']}
- Продукты, которые УЖЕ ЕСТЬ дома (обязательно используй их в первую очередь): {fridge_items}
- Количество основных приемов пищи в день: {meals_count}
- Целевое БЖУ в день: Белки {actual_daily_p} г, Жиры {actual_daily_f} г, Углеводы {actual_daily_c} г.
- {sport_rule}
- {exclude_rule} {week_note}

Верни ответ СТРОГО в формате JSON следующей структуры (без markdown-оберток вокруг, только чистый JSON):
{{
  "weekly_summary": {{
    "daily_p": {actual_daily_p},
    "daily_f": {actual_daily_f},
    "daily_c": {actual_daily_c},
    "week_p": {actual_daily_p * 7},
    "week_f": {actual_daily_f * 7},
    "week_c": {actual_daily_c * 7},
    "note": "Краткое заключение по рациону и добавкам"
  }},
  "days": [
    {{
      "day_num": 1,
      "day_name": "Понедельник",
      "p": {actual_daily_p},
      "f": {actual_daily_f},
      "c": {actual_daily_c},
      "meals": [
        {{
          "meal_num": 1,
          "meal_title": "Прием пищи 1 (Завтрак)",
          "dish_name": "Овсяная каша с вареными яйцами",
          "ingredients": "Овсяные хлопья 60г, яйцо куриное 2 шт, молоко 100мл",
          "p": 24,
          "f": 14,
          "c": 45
        }}
      ]
    }}
  ]
}}
Обязательно сгенерируй все 7 дней (day_num от 1 до 7), и в каждом дне ровно {meals_count} приемов пищи (+ перекусы спортпитом, если со спортивным питанием).
"""

    raw = giga.ask(system_prompt, user_prompt, temperature=0.4)
    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            if "days" in data and len(data["days"]) > 0:
                return data
    except Exception as e:
        logging.error(f"JSON parse error: {e}")

    return build_fallback_diet(meals_count, actual_daily_p, actual_daily_f, actual_daily_c, use_sport, supps_text)


def build_fallback_diet(meals_count, daily_p, daily_f, daily_c, use_sport, supps_text):
    days_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    sample_dishes = [
        ("Овсянка на молоке + 2 вареных яйца", "Овсянка 60г, яйца 2 шт, молоко 100мл"),
        ("Куриная грудка с гречкой и огурцом", "Филе куриное 130г, гречка сухая 60г, огурец 100г"),
        ("Индейка тушеная с бурым рисом", "Филе индейки 130г, бурый рис 60г, зелень"),
        ("Творог 5% с бананом и хлебцами", "Творог 150г, банан 1 шт, ржаные хлебцы 2 шт"),
        ("Запеченная треска с картофелем и салатом", "Филе трески 150г, картофель 150г, капуста 100г"),
        ("Говядина постная с макаронами из тв. сортов", "Говядина 120г, макароны 60г, томаты 100г"),
        ("Омлет из 3 яиц с цельнозерновым тостом", "Яйца 3 шт, хлеб ц/з 2 ломтика, сыр 20г"),
    ]
    p_per_meal = min(30, int(daily_p / meals_count))
    f_per_meal = int(daily_f / meals_count)
    c_per_meal = min(65, int(daily_c / meals_count))

    days = []
    for d_idx, d_name in enumerate(days_names, start=1):
        meals = []
        for m_idx in range(1, meals_count + 1):
            dish, ingr = sample_dishes[(d_idx + m_idx) % len(sample_dishes)]
            meals.append({
                "meal_num": m_idx,
                "meal_title": f"Прием пищи {m_idx}",
                "dish_name": dish,
                "ingredients": ingr,
                "p": p_per_meal,
                "f": f_per_meal,
                "c": c_per_meal,
            })
        if use_sport:
            meals.append({
                "meal_num": meals_count + 1,
                "meal_title": "Спортивное питание (между приемами пищи)",
                "dish_name": "Протеиновый коктейль + Банан (белок + углеводы)",
                "ingredients": f"Сывороточный протеин 1 порция (30г), банан 1 шт (120г). Добавки по плану: {supps_text}",
                "p": max(20, daily_p - p_per_meal * meals_count),
                "f": 2,
                "c": max(25, daily_c - c_per_meal * meals_count),
            })
        days.append({
            "day_num": d_idx,
            "day_name": d_name,
            "p": daily_p,
            "f": daily_f,
            "c": daily_c,
            "meals": meals,
        })

    return {
        "weekly_summary": {
            "daily_p": daily_p,
            "daily_f": daily_f,
            "daily_c": daily_c,
            "week_p": daily_p * 7,
            "week_f": daily_f * 7,
            "week_c": daily_c * 7,
            "note": "Сбалансированный рацион (не более 30г белка и 65г углеводов за 1 прием обычной пищи).",
        },
        "days": days,
    }


def get_detailed_recipe(user_id: int, dish_name: str, ingredients: str) -> str:
    """Получает подробный пошаговый рецепт любого блюда (вплоть до вареного яйца) с кэшированием."""
    user = get_user(user_id)
    cache = json.loads(user.get("recipes_cache_json") or "{}")
    if dish_name in cache:
        return cache[dish_name]

    sys_p = "Ты — опытный шеф-повар. Напиши максимально понятный, пошаговый рецепт приготовления блюда (даже если там есть простые элементы вроде вареного яйца — распиши сколько минут варить каждый ингредиент)."
    usr_p = f"Блюдо: {dish_name}\nИнгредиенты: {ingredients}\nНапиши подробный пошаговый рецепт с точным временем приготовления."
    recipe = giga.ask(sys_p, usr_p, temperature=0.4)
    if not recipe:
        recipe = (
            f"<b>📖 Подробный рецепт: {dish_name}</b>\n\n"
            f"<b>Ингредиенты:</b> {ingredients}\n\n"
            "1. Подготовьте и взвесьте все ингредиенты.\n"
            "2. Если в блюде есть яйца: положите в холодную воду, после закипания варите 8-9 минут (вкрутую) или 4-5 минут (всмятку), затем охладите в холодной воде.\n"
            "3. Крупы промойте и варите на медленном огне в пропорции 1:2 до готовности (15-20 минут).\n"
            "4. Мясо/птицу/рыбу запекайте в духовке при 180°C (25 минут) или обжарьте на антипригарной сковороде без лишнего масла."
        )
    cache[dish_name] = recipe
    save_user_field(user_id, recipes_cache_json=json.dumps(cache, ensure_ascii=False))
    return recipe


# ==========================================
# 4. СОСТОЯНИЯ АНКЕТЫ (FSM)
# ==========================================
class OnboardingSG(StatesGroup):
    choosing_mode = State()
    ask_custom_bju_choice = State()
    entering_custom_bju = State()
    q1_height = State()
    q2_weight = State()
    q3_age = State()
    q4_exp = State()
    q5_workouts_btn = State()
    q5_workouts_pro_text = State()
    q6_goal = State()
    q7_supps_choice = State()
    q8_supps_list = State()
    q9_supps_freq = State()
    q10_allergies = State()
    q_budget = State()
    q_stores = State()
    q_notify_time = State()
    q_ready_meals = State()
    q_custom_meals_count = State()
    q_sport_nutr_mode = State()


class EditProfileSG(StatesGroup):
    edit_weight = State()
    edit_meals = State()
    edit_budget = State()
    add_supp_name = State()
    add_supp_freq = State()
    exclude_dish_name = State()
    monthly_checkin = State()


class FridgeSG(StatesGroup):
    entering_product = State()
    confirm_more = State()


# ==========================================
# 5. КЛАВИАТУРЫ (В ФОРМАТЕ ПРИЛОЖЕНИЯ)
# ==========================================
def kb_start_modes() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🥗 Рацион", callback_data="mode_diet")
    b.button(text="📦 Продуктовый контроль", callback_data="mode_fridge")
    b.adjust(2)
    return b.as_markup()


def kb_main_app_menu(has_next_week: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    days_short = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    for i, d in enumerate(days_short, start=1):
        b.button(text=f"📅 {d}", callback_data=f"view_day_{i}_curr")

    b.button(text="📊 Сводка БЖУ на неделю", callback_data="view_week_summary")
    if has_next_week:
        b.button(text="⏭ Показать след. неделю", callback_data="view_next_week")
    else:
        b.button(text="⏭ Запросить рацион на след. неделю", callback_data="gen_next_week")

    b.button(text="🔄 Изменить весь рацион", callback_data="regen_full_diet")
    b.button(text="🚫 Убрать конкретное блюдо", callback_data="remove_specific_dish")

    b.button(text="⚖️ Изменить вес", callback_data="edit_param_weight")
    b.button(text="🍽 Изменить кол-во приемов", callback_data="edit_param_meals")
    b.button(text="💰 Изменить бюджет", callback_data="edit_param_budget")
    b.button(text="💊 Добавить спорт. добавки", callback_data="edit_param_supps")

    b.button(text="📦 Продуктовый контроль (Холодильник)", callback_data="open_fridge_app")
    b.button(text="➕ Добавить продукт", callback_data="fridge_add_start")
    b.button(text="🗑 Убрать продукт", callback_data="fridge_del_menu")

    b.adjust(7, 1, 1, 2, 2, 2, 1, 2)
    return b.as_markup()


# ==========================================
# 6. СТАРТ И ОПРОС ПОЛЬЗОВАТЕЛЯ (РАЦИОН)
# ==========================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    save_user_field(message.from_user.id)
    await state.set_state(OnboardingSG.choosing_mode)
    await message.answer("Выберите режим:", reply_markup=kb_start_modes())


@router.callback_query(F.data == "mode_diet")
async def start_diet_mode(call: CallbackQuery, state: FSMContext):
    save_user_field(call.from_user.id, mode="diet")
    await state.set_state(OnboardingSG.ask_custom_bju_choice)
    b = InlineKeyboardBuilder()
    b.button(text="Рассчитать автоматически", callback_data="bju_auto")
    b.button(text="Ввести свои показатели БЖУ", callback_data="bju_custom")
    b.adjust(1)
    await call.message.edit_text(
        "Желаете ввести конкретные показатели БЖУ, которые вам нужны, или бот рассчитает их автоматически?",
        reply_markup=b.as_markup(),
    )


@router.callback_query(OnboardingSG.ask_custom_bju_choice, F.data == "bju_auto")
async def bju_auto_chosen(call: CallbackQuery, state: FSMContext):
    save_user_field(call.from_user.id, custom_bju="")
    await state.set_state(OnboardingSG.q1_height)
    await call.message.edit_text("1. Укажите ваш рост (в см, например: 180):")


@router.callback_query(OnboardingSG.ask_custom_bju_choice, F.data == "bju_custom")
async def bju_custom_chosen(call: CallbackQuery, state: FSMContext):
    await state.set_state(OnboardingSG.entering_custom_bju)
    await call.message.edit_text(
        "Введите ваши целевые показатели Белки, Жиры, Углеводы в граммах через пробел (например: <code>150 60 300</code>):"
    )


@router.message(OnboardingSG.entering_custom_bju)
async def process_custom_bju(message: Message, state: FSMContext):
    nums = re.findall(r"\d+", message.text or "")
    if len(nums) < 3:
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Укажите 3 числа (Белки Жиры Углеводы), например: 150 60 300")
        return
    save_user_field(message.from_user.id, custom_bju=message.text)
    await state.set_state(OnboardingSG.q1_height)
    await message.answer("1. Укажите ваш рост (в см, например: 180):")


# Вопрос 1: Рост
@router.message(OnboardingSG.q1_height)
async def process_q1_height(message: Message, state: FSMContext):
    if not (message.text and message.text.strip().isdigit() and 100 <= int(message.text.strip()) <= 250):
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Укажите ваш рост числом в см (от 100 до 250):")
        return
    save_user_field(message.from_user.id, height=int(message.text.strip()))
    await state.set_state(OnboardingSG.q2_weight)
    await message.answer("2. Укажите ваш вес (в кг, например: 75):")


# Вопрос 2: Вес
@router.message(OnboardingSG.q2_weight)
async def process_q2_weight(message: Message, state: FSMContext):
    txt = (message.text or "").replace(",", ".").strip()
    try:
        w = float(txt)
        if not (30 <= w <= 250):
            raise ValueError()
    except ValueError:
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Укажите ваш точный вес числом в кг:")
        return
    save_user_field(message.from_user.id, weight=w)
    await state.set_state(OnboardingSG.q3_age)
    await message.answer("3. Укажите ваш возраст (полных лет):")


# Вопрос 3: Возраст
@router.message(OnboardingSG.q3_age)
async def process_q3_age(message: Message, state: FSMContext):
    if not (message.text and message.text.strip().isdigit() and 10 <= int(message.text.strip()) <= 100):
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Укажите ваш возраст числом:")
        return
    save_user_field(message.from_user.id, age=int(message.text.strip()))
    await state.set_state(OnboardingSG.q4_exp)
    b = InlineKeyboardBuilder()
    b.button(text="Начинающий", callback_data="exp_Начинающий")
    b.button(text="Любитель", callback_data="exp_Любитель")
    b.button(text="Профессионал", callback_data="exp_Профессионал")
    b.adjust(1)
    await message.answer("4. Ваш спортивный опыт:", reply_markup=b.as_markup())


# Вопрос 4: Спортивный опыт
@router.callback_query(OnboardingSG.q4_exp, F.data.startswith("exp_"))
async def process_q4_exp(call: CallbackQuery, state: FSMContext):
    exp = call.data.split("_", 1)[1]
    save_user_field(call.from_user.id, exp_level=exp)
    await state.update_data(exp_level=exp)

    if exp in ["Начинающий", "Любитель"]:
        await state.set_state(OnboardingSG.q5_workouts_btn)
        b = InlineKeyboardBuilder()
        b.button(text="1-3", callback_data="wrk_1-3")
        b.button(text="3 и более", callback_data="wrk_3 и более")
        b.adjust(2)
        await call.message.edit_text("5. Количество тренировок в неделю:", reply_markup=b.as_markup())
    else:
        await state.set_state(OnboardingSG.q5_workouts_pro_text)
        await call.message.edit_text("5. Количество тренировок в неделю (напишите точное число):")


# Вопрос 5 (кнопки для Начинающего и Любителя)
@router.callback_query(OnboardingSG.q5_workouts_btn, F.data.startswith("wrk_"))
async def process_q5_btn(call: CallbackQuery, state: FSMContext):
    wrk = call.data.split("_", 1)[1]
    save_user_field(call.from_user.id, workouts=wrk)
    await state.set_state(OnboardingSG.q6_goal)
    b = InlineKeyboardBuilder()
    b.button(text="Сушка", callback_data="goal_Сушка")
    b.button(text="Набор мышечной массы", callback_data="goal_Набор мышечной массы")
    b.adjust(1)
    await call.message.edit_text("6. Ваша цель:", reply_markup=b.as_markup())


# Вопрос 5 (текст для Профессионала)
@router.message(OnboardingSG.q5_workouts_pro_text)
async def process_q5_pro(message: Message, state: FSMContext):
    if not (message.text and message.text.strip().isdigit() and 1 <= int(message.text.strip()) <= 21):
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Укажите точное количество тренировок в неделю числом:")
        return
    save_user_field(message.from_user.id, workouts=message.text.strip())
    await state.set_state(OnboardingSG.q6_goal)
    b = InlineKeyboardBuilder()
    b.button(text="Сушка", callback_data="goal_Сушка")
    b.button(text="Набор мышечной массы", callback_data="goal_Набор мышечной массы")
    b.adjust(1)
    await message.answer("6. Ваша цель:", reply_markup=b.as_markup())


# Вопрос 6: Цель
@router.callback_query(OnboardingSG.q6_goal, F.data.startswith("goal_"))
async def process_q6_goal(call: CallbackQuery, state: FSMContext):
    goal = call.data.split("_", 1)[1]
    save_user_field(call.from_user.id, goal=goal)
    data = await state.get_data()
    exp = data.get("exp_level", "Начинающий")

    if exp == "Начинающий":
        save_user_field(call.from_user.id, has_supplements=0, supplements_json="{}")
        await state.set_state(OnboardingSG.q10_allergies)
        await call.message.edit_text("7. Укажите противопоказания и аллергии (если нет — напишите «нет»):")
    else:
        await state.set_state(OnboardingSG.q7_supps_choice)
        b = InlineKeyboardBuilder()
        b.button(text="Да", callback_data="supps_yes")
        b.button(text="Нет", callback_data="supps_no")
        b.adjust(2)
        await call.message.edit_text(
            "7. Пользуетесь ли спортивными добавками (креатин, протеин, гейнер, BCAA, фарма/тестостерон и др.)?",
            reply_markup=b.as_markup(),
        )


# Вопрос 7: Добавки (Да / Нет)
@router.callback_query(OnboardingSG.q7_supps_choice, F.data.in_(["supps_yes", "supps_no"]))
async def process_q7_supps(call: CallbackQuery, state: FSMContext):
    if call.data == "supps_no":
        save_user_field(call.from_user.id, has_supplements=0, supplements_json="{}")
        await state.set_state(OnboardingSG.q10_allergies)
        await call.message.edit_text("10. Укажите противопоказания и аллергии (если нет — напишите «нет»):")
    else:
        save_user_field(call.from_user.id, has_supplements=1)
        await state.set_state(OnboardingSG.q8_supps_list)
        await call.message.edit_text(
            "8. Какими именно добавками (или препаратами) вы пользуетесь?\n"
            "Перечислите их через запятую (по желанию можете сразу указать БЖУ вашей купленной добавки в скобках, например: <i>Протеин (24г белка), Креатин, Омега-3</i>):"
        )


# Вопрос 8: Список добавок -> переход к Вопросу 9
@router.message(OnboardingSG.q8_supps_list)
async def process_q8_supps_list(message: Message, state: FSMContext):
    if not message.text or len(message.text.strip()) < 2:
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Перечислите ваши добавки через запятую:")
        return
    supps = [s.strip() for s in message.text.split(",") if s.strip()]
    await state.update_data(supps_queue=supps, supps_result={})
    await state.set_state(OnboardingSG.q9_supps_freq)
    first_supp = supps[0]
    await message.answer(f"9. Укажите периодичность и дозировку приема для добавки <b>«{first_supp}»</b>:")


# Вопрос 9: Цикл опроса периодичности по каждой добавке
@router.message(OnboardingSG.q9_supps_freq)
async def process_q9_supps_freq(message: Message, state: FSMContext):
    if not message.text or len(message.text.strip()) < 1:
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Укажите периодичность приема:")
        return
    data = await state.get_data()
    queue = data.get("supps_queue", [])
    result = data.get("supps_result", {})

    current_supp = queue.pop(0)
    result[current_supp] = message.text.strip()
    await state.update_data(supps_queue=queue, supps_result=result)

    if queue:
        next_supp = queue[0]
        await message.answer(f"9. Укажите периодичность и дозировку приема для добавки <b>«{next_supp}»</b>:")
    else:
        save_user_field(message.from_user.id, supplements_json=json.dumps(result, ensure_ascii=False))
        await state.set_state(OnboardingSG.q10_allergies)
        await message.answer("10. Укажите противопоказания и аллергии (если нет — напишите «нет»):")


# Вопрос 10 (или 7 у начинающих): Противопоказания и аллергии -> Бюджет
@router.message(OnboardingSG.q10_allergies)
async def process_q10_allergies(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Укажите противопоказания и аллергии:")
        return
    save_user_field(message.from_user.id, allergies=message.text.strip())
    await state.set_state(OnboardingSG.q_budget)
    await message.answer("Укажите ваш бюджет на питание на неделю (в рублях, например: 5000 руб):")


# Вопрос: Бюджет -> Магазины РФ
@router.message(OnboardingSG.q_budget)
async def process_q_budget(message: Message, state: FSMContext):
    if not message.text or not any(ch.isdigit() for ch in message.text):
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Укажите ваш бюджет (например: 6000):")
        return
    save_user_field(message.from_user.id, budget=message.text.strip())
    await state.set_state(OnboardingSG.q_stores)
    await message.answer(
        "Какие продуктовые магазины вы используете?\n"
        "<i>(Напишите магазины, которые есть в России, например: Пятёрочка, Магнит, Перекрёсток, ВкусВилл, Лента, или укажите Рынок)</i>:"
    )


# Вопрос: Магазины -> Время утренней рассылки
@router.message(OnboardingSG.q_stores)
async def process_q_stores(message: Message, state: FSMContext):
    txt = (message.text or "").lower().strip()
    if not txt or len(txt) < 3:
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Напишите названия российских магазинов (или рынок):")
        return
    if any(foreign in txt for foreign in ["walmart", "whole foods", "tesco", "carrefour", "aldi", "kroger"]):
        await message.answer(
            "⚠️ Бот рассматривает только магазины, которые находятся в России (исключение — рынок). "
            "Пожалуйста, укажите российские магазины:"
        )
        return

    save_user_field(message.from_user.id, stores=message.text.strip())
    await state.set_state(OnboardingSG.q_notify_time)
    await message.answer(
        "Во сколько с утра (по московскому времени) присылать вам рацион на весь день?\n"
        "Введите время в формате <code>ЧЧ:ММ</code> (например: <code>08:00</code>):"
    )


# Вопрос: Время -> Сохранение сводки в диалоге и предложение количества приемов пищи
@router.message(OnboardingSG.q_notify_time)
async def process_q_notify_time(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", txt):
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Укажите время в формате ЧЧ:ММ (например: 08:30):")
        return

    user_id = message.from_user.id
    save_user_field(user_id, notify_time=txt)
    user = get_user(user_id)
    macros = calculate_macros(user)

    save_user_field(
        user_id,
        target_p=macros["p"],
        target_f=macros["f"],
        target_c=macros["c"],
        target_fiber=macros["fiber"],
        rec_meals=macros["rec_meals"],
    )

    supps_dict = json.loads(user.get("supplements_json") or "{}")
    supps_str = ", ".join([f"{k} ({v})" for k, v in supps_dict.items()]) if supps_dict else "Нет"

    profile_msg = (
        "📌 <b>Ваша карточка данных (сохранена в истории диалога):</b>\n"
        f"• Рост / Вес / Возраст: {user['height']} см / {user['weight']} кг / {user['age']} лет\n"
        f"• Опыт: {user['exp_level']} (тренировок в нед: {user['workouts']})\n"
        f"• Цель: {user['goal']}\n"
        f"• Добавки: {supps_str}\n"
        f"• Аллергии/противопоказания: {user['allergies']}\n"
        f"• Бюджет: {user['budget']} | Магазины: {user['stores']}\n"
        f"• Ежедневная отправка меню: {txt} МСК"
    )
    await message.answer(profile_msg)

    await state.set_state(OnboardingSG.q_ready_meals)
    b = InlineKeyboardBuilder()
    b.button(text="Да", callback_data="meals_ready_yes")
    b.button(text="Нет", callback_data="meals_ready_no")
    b.adjust(2)

    advice_msg = (
        f"💡 <b>Рекомендация по вашему рациону:</b>\n"
        f"Вам необходимо потреблять в день:\n"
        f"• Белки: <b>{macros['p']} г</b>\n"
        f"• Углеводы: <b>{macros['c']} г</b>\n"
        f"• Жиры (минимум): <b>{macros['f']} г</b>\n"
        f"• Клетчатка: <b>{macros['fiber']} г</b>\n\n"
        f"<i>(Так как за один прием обычной пищи усваивается максимум 30 г белка и 60–70 г углеводов)</i>\n"
        f"Для такого рациона рекомендуется <b>{macros['rec_meals']}-разовое питание</b> в день.\n\n"
        f"<b>Готовы?</b>"
    )
    await message.answer(advice_msg, reply_markup=b.as_markup())


@router.callback_query(OnboardingSG.q_ready_meals, F.data == "meals_ready_yes")
async def process_meals_ready_yes(call: CallbackQuery, state: FSMContext):
    user = get_user(call.from_user.id)
    save_user_field(call.from_user.id, actual_meals=user["rec_meals"])
    await state.set_state(OnboardingSG.q_sport_nutr_mode)

    b = InlineKeyboardBuilder()
    b.button(text="Со спортивным питанием", callback_data="sport_mode_yes")
    b.button(text="Без него", callback_data="sport_mode_no")
    b.adjust(1)

    await call.message.edit_text(
        f"Отлично! Формируем {user['rec_meals']}-разовое питание.\n"
        "Уточните, рассчитывать рацион со спортивным питанием или без него?",
        reply_markup=b.as_markup(),
    )


@router.callback_query(OnboardingSG.q_ready_meals, F.data == "meals_ready_no")
async def process_meals_ready_no(call: CallbackQuery, state: FSMContext):
    await state.set_state(OnboardingSG.q_custom_meals_count)
    await call.message.edit_text("Сколько раз в день вы готовы питаться? (Напишите число от 1 до 6):")


@router.message(OnboardingSG.q_custom_meals_count)
async def process_custom_meals_count(message: Message, state: FSMContext):
    if not (message.text and message.text.strip().isdigit() and 1 <= int(message.text.strip()) <= 6):
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Напишите число приемов пищи от 1 до 6:")
        return

    meals = int(message.text.strip())
    save_user_field(message.from_user.id, actual_meals=meals)
    await state.set_state(OnboardingSG.q_sport_nutr_mode)

    b = InlineKeyboardBuilder()
    b.button(text="Со спортивным питанием", callback_data="sport_mode_yes")
    b.button(text="Без него", callback_data="sport_mode_no")
    b.adjust(1)

    warn_text = (
        f"⚠️ <b>Обратите внимание:</b> {meals} приемов пищи в день — этого мало для полного усвоения вашей нормы из обычных продуктов "
        f"(за один прием пищи усваивается не более 30 г белка и 60–70 г углеводов).\n\n"
        "Однако бот может добавить <b>гейнер или протеин (с углеводами)</b> между основными приемами пищи, "
        "если вы не будете успевать столько питаться.\n\n"
        "Как рассчитываем ваш рацион?"
    )
    await message.answer(warn_text, reply_markup=b.as_markup())


@router.callback_query(OnboardingSG.q_sport_nutr_mode, F.data.in_(["sport_mode_yes", "sport_mode_no"]))
async def finalize_and_generate_diet(call: CallbackQuery, state: FSMContext):
    use_sport = 1 if call.data == "sport_mode_yes" else 0
    user_id = call.from_user.id
    save_user_field(user_id, use_sport_nutr=use_sport)
    await state.clear()

    await call.message.edit_text("⏳ <b>Анализирую данные, цены в магазинах и составляю ваш рацион на неделю... (это займет около 15-20 секунд)</b>")
    user = get_user(user_id)
    diet_data = await asyncio.to_thread(generate_weekly_diet_ai, user, False, None)
    now_str = datetime.now(MSK_TZ).strftime("%d.%m.%Y")
    save_user_field(
        user_id,
        current_diet_json=json.dumps(diet_data, ensure_ascii=False),
        diet_start_date=now_str,
        last_month_check=now_str,
    )

    summary_text = format_weekly_summary_text(user, diet_data, is_next=False)
    await call.message.edit_text(summary_text, reply_markup=kb_main_app_menu(has_next_week=False))

    b_fr = InlineKeyboardBuilder()
    b_fr.button(text="📦 Включить контроль продуктов", callback_data="open_fridge_app")
    await call.message.answer(
        "💡 <b>Совет:</b> Вы также можете включить <b>Контроль за продуктами</b>, чтобы бот следил за сроками годности в вашем холодильнике и напоминал о них!",
        reply_markup=b_fr.as_markup(),
    )


@router.message(StateFilter(OnboardingSG))
async def fallback_onboarding_strict(message: Message):
    await message.answer("⚠️ <b>Ответьте на предыдущий вопрос!</b> Бот ждет вашего ответа по плану.")


@router.callback_query(StateFilter(OnboardingSG))
async def fallback_onboarding_cb(call: CallbackQuery):
    await call.answer("⚠️ Ответьте на текущий вопрос анкеты!", show_alert=True)


# ==========================================
# 7. НАВИГАЦИЯ ПО РАЦИОНУ И РЕЦЕПТАМ (SPA)
# ==========================================
def format_weekly_summary_text(user: dict, diet_data: dict, is_next: bool = False) -> str:
    ws = diet_data.get("weekly_summary", {})
    title = "📅 <b>РАЦИОН НА СЛЕДУЮЮЩУЮ НЕДЕЛЮ</b>" if is_next else "📋 <b>ВАШ РАЦИОН НА НЕДЕЛЮ (Сводка)</b>"
    sport_status = "Со спортивным питанием" if user.get("use_sport_nutr") else "Без спортивного питания (лимит 30г белка/прием)"

    lines = [
        f"{title}",
        f"Режим: <i>{sport_status}</i> | Приемов пищи: <b>{user.get('actual_meals')}</b>",
        "",
        f"🔹 <b>БЖУ в день:</b> Белки: <b>{ws.get('daily_p')} г</b> | Жиры: <b>{ws.get('daily_f')} г</b> | Углеводы: <b>{ws.get('daily_c')} г</b>",
        f"🔸 <b>БЖУ в сумме за неделю:</b> Белки: <b>{ws.get('week_p')} г</b> | Жиры: <b>{ws.get('week_f')} г</b> | Углеводы: <b>{ws.get('week_c')} г</b>",
        "",
        "<b>Краткий обзор по дням:</b>",
    ]
    for d in diet_data.get("days", []):
        dishes = ", ".join([m["dish_name"] for m in d.get("meals", [])])
        lines.append(f"• <b>{d['day_name']}</b> (Б:{d['p']} Ж:{d['f']} У:{d['c']}): {dishes}")

    lines.append("\n👇 <i>Нажмите на любой день недели ниже, чтобы увидеть подробные блюда, граммовки и открыть пошаговые рецепты:</i>")
    return "\n".join(lines)


def format_day_text_and_kb(diet_data: dict, day_num: int, week_type: str = "curr") -> tuple[str, InlineKeyboardMarkup]:
    days = diet_data.get("days", [])
    day_obj = next((d for d in days if int(d.get("day_num", 1)) == day_num), days[0])

    lines = [
        f"📅 <b>{day_obj['day_name']} ({'Текущая неделя' if week_type == 'curr' else 'Следующая неделя'})</b>",
        f"Суточное БЖУ: <b>Белки {day_obj['p']} г | Жиры {day_obj['f']} г | Углеводы {day_obj['c']} г</b>\n",
    ]

    b = InlineKeyboardBuilder()

    for m in day_obj.get("meals", []):
        m_num = m["meal_num"]
        lines.append(
            f"🍽 <b>{m['meal_title']}: {m['dish_name']}</b>\n"
            f"   <i>Ингредиенты:</i> {m['ingredients']}\n"
            f"   <i>БЖУ приема:</i> Б: {m['p']}г | Ж: {m['f']}г | У: {m['c']}г\n"
        )
        b.button(
            text=f"📖 Рецепт: {m['dish_name'][:28]}",
            callback_data=f"recipe_{week_type}_{day_num}_{m_num}",
        )
        b.button(
            text=f"🔄 Заменить прием {m_num}",
            callback_data=f"repmeal_{week_type}_{day_num}_{m_num}",
        )

    b.button(text="🔄 Заменить весь этот день", callback_data=f"repday_{week_type}_{day_num}")
    b.button(text="🔙 Вернуться к сводке недели", callback_data="view_week_summary")
    b.adjust(2)
    return "\n".join(lines), b.as_markup()


@router.callback_query(F.data == "view_week_summary")
async def cb_view_week_summary(call: CallbackQuery):
    user = get_user(call.from_user.id)
    if not user or not user.get("current_diet_json"):
        await call.message.edit_text("У вас еще не создан рацион.", reply_markup=kb_start_modes())
        return
    diet_data = json.loads(user["current_diet_json"])
    has_next = bool(user.get("next_diet_json"))
    text = format_weekly_summary_text(user, diet_data, is_next=False)
    await call.message.edit_text(text, reply_markup=kb_main_app_menu(has_next_week=has_next))


@router.callback_query(F.data.startswith("view_day_"))
async def cb_view_day(call: CallbackQuery):
    _, _, day_str, week_type = call.data.split("_")
    day_num = int(day_str)
    user = get_user(call.from_user.id)
    json_field = "current_diet_json" if week_type == "curr" else "next_diet_json"
    diet_data = json.loads(user.get(json_field) or user["current_diet_json"])
    text, kb = format_day_text_and_kb(diet_data, day_num, week_type)
    await call.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("recipe_"))
async def cb_open_recipe(call: CallbackQuery):
    _, week_type, day_str, meal_str = call.data.split("_")
    day_num, meal_num = int(day_str), int(meal_str)
    user = get_user(call.from_user.id)

    json_field = "current_diet_json" if week_type == "curr" else "next_diet_json"
    diet_data = json.loads(user.get(json_field) or user["current_diet_json"])
    day_obj = next((d for d in diet_data["days"] if int(d["day_num"]) == day_num), diet_data["days"][0])
    meal_obj = next((m for m in day_obj["meals"] if int(m["meal_num"]) == meal_num), day_obj["meals"][0])

    await call.message.edit_text(f"⏳ <b>Открываю подробный рецепт блюда «{meal_obj['dish_name']}»...</b>")
    recipe_text = await asyncio.to_thread(
        get_detailed_recipe, call.from_user.id, meal_obj["dish_name"], meal_obj["ingredients"]
    )

    b = InlineKeyboardBuilder()
    b.button(text="🔙 Вернуться обратно", callback_data=f"view_day_{day_num}_{week_type}")

    full_text = (
        f"🍽 <b>{meal_obj['dish_name']}</b>\n"
        f"<i>БЖУ порции: Белки {meal_obj['p']}г | Жиры {meal_obj['f']}г | Углеводы {meal_obj['c']}г</i>\n\n"
        f"{recipe_text}"
    )
    await call.message.edit_text(full_text, reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("repmeal_"))
async def cb_replace_single_meal(call: CallbackQuery):
    _, week_type, day_str, meal_str = call.data.split("_")
    day_num, meal_num = int(day_str), int(meal_str)
    user = get_user(call.from_user.id)
    json_field = "current_diet_json" if week_type == "curr" else "next_diet_json"
    diet_data = json.loads(user.get(json_field))

    day_obj = next((d for d in diet_data["days"] if int(d["day_num"]) == day_num), diet_data["days"][0])
    meal_obj = next((m for m in day_obj["meals"] if int(m["meal_num"]) == meal_num), day_obj["meals"][0])
    old_dish = meal_obj["dish_name"]

    await call.message.edit_text(f"⏳ <b>Подбираю замену для блюда «{old_dish}»...</b>")

    sys_p = "Ты спортивный диетолог. Предложи ОДНО новое блюдо на замену строго в формате JSON."
    usr_p = (
        f"Замени блюдо '{old_dish}' на другое из магазинов РФ ({user['stores']}). "
        f"Аллергии: {user['allergies']}. Целевое БЖУ приема: Б:{meal_obj['p']} Ж:{meal_obj['f']} У:{meal_obj['c']} (макс 30г белка и 65г углеводов!). "
        'Верни строго JSON: {"dish_name": "Название", "ingredients": "Ингредиенты с граммами"}'
    )
    raw = await asyncio.to_thread(giga.ask, sys_p, usr_p, 0.7)
    try:
        m_json = json.loads(re.search(r"\{.*\}", raw, re.DOTALL).group(0))
        meal_obj["dish_name"] = m_json.get("dish_name", "Филе индейки с рисом и овощами")
        meal_obj["ingredients"] = m_json.get("ingredients", "Индейка 130г, рис 60г, овощи 100г")
    except Exception:
        meal_obj["dish_name"] = "Творожная запеканка с бананом / Куриное филе с гречкой"
        meal_obj["ingredients"] = "Куриное филе 130г, гречка 60г, зелень"

    save_user_field(call.from_user.id, **{json_field: json.dumps(diet_data, ensure_ascii=False)})
    text, kb = format_day_text_and_kb(diet_data, day_num, week_type)
    await call.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("repday_"))
async def cb_replace_day(call: CallbackQuery):
    _, week_type, day_str = call.data.split("_")
    day_num = int(day_str)
    user = get_user(call.from_user.id)
    json_field = "current_diet_json" if week_type == "curr" else "next_diet_json"
    diet_data = json.loads(user.get(json_field))

    await call.message.edit_text("⏳ <b>Полностью обновляю меню на выбранный день...</b>")
    new_week = await asyncio.to_thread(generate_weekly_diet_ai, user, True, None)
    new_day = next((d for d in new_week["days"] if int(d["day_num"]) == day_num), new_week["days"][0])

    for idx, d in enumerate(diet_data["days"]):
        if int(d["day_num"]) == day_num:
            diet_data["days"][idx] = new_day
            break

    save_user_field(call.from_user.id, **{json_field: json.dumps(diet_data, ensure_ascii=False)})
    text, kb = format_day_text_and_kb(diet_data, day_num, week_type)
    await call.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "regen_full_diet")
async def cb_regen_full_diet(call: CallbackQuery):
    user = get_user(call.from_user.id)
    await call.message.edit_text("⏳ <b>Полностью меняю ваш рацион с сохранением всех ваших показателей БЖУ...</b>")
    new_diet = await asyncio.to_thread(generate_weekly_diet_ai, user, True, None)
    save_user_field(call.from_user.id, current_diet_json=json.dumps(new_diet, ensure_ascii=False))
    text = format_weekly_summary_text(user, new_diet, is_next=False)
    await call.message.edit_text(text, reply_markup=kb_main_app_menu(has_next_week=bool(user.get("next_diet_json"))))


@router.callback_query(F.data == "gen_next_week")
async def cb_gen_next_week(call: CallbackQuery):
    user = get_user(call.from_user.id)
    if user.get("next_diet_json"):
        await call.answer("Рацион на следующую неделю уже сформирован (доступно максимум на 1 неделю вперед)!", show_alert=True)
        return
    await call.message.edit_text("⏳ <b>Формирую рацион на следующую неделю (максимум на 1 неделю вперед)...</b>")
    next_diet = await asyncio.to_thread(generate_weekly_diet_ai, user, True, None)
    save_user_field(call.from_user.id, next_diet_json=json.dumps(next_diet, ensure_ascii=False))
    await show_next_week_view(call.message, user, next_diet)


@router.callback_query(F.data == "view_next_week")
async def cb_view_next_week(call: CallbackQuery):
    user = get_user(call.from_user.id)
    if not user.get("next_diet_json"):
        await cb_gen_next_week(call)
        return
    next_diet = json.loads(user["next_diet_json"])
    await show_next_week_view(call.message, user, next_diet)


async def show_next_week_view(message: Message, user: dict, next_diet: dict):
    text = format_weekly_summary_text(user, next_diet, is_next=True) + "\n\n<b>Устраивает ли вас этот рацион на следующую неделю?</b>"
    b = InlineKeyboardBuilder()
    days_short = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    for i, d in enumerate(days_short, start=1):
        b.button(text=f"📅 {d} (След.)", callback_data=f"view_day_{i}_next")
    b.button(text="✅ Да, устраивает", callback_data="next_week_approve")
    b.button(text="🔄 Поменять", callback_data="next_week_regen")
    b.button(text="🔙 Назад к текущей неделе", callback_data="view_week_summary")
    b.adjust(7, 2, 1)
    await message.edit_text(text, reply_markup=b.as_markup())


@router.callback_query(F.data == "next_week_approve")
async def cb_next_week_approve(call: CallbackQuery):
    await call.answer("✅ Отлично! Рацион на следующую неделю сохранен.", show_alert=True)
    await cb_view_week_summary(call)


@router.callback_query(F.data == "next_week_regen")
async def cb_next_week_regen(call: CallbackQuery):
    user = get_user(call.from_user.id)
    await call.message.edit_text("⏳ <b>Меняю рацион на следующую неделю...</b>")
    next_diet = await asyncio.to_thread(generate_weekly_diet_ai, user, True, None)
    save_user_field(call.from_user.id, next_diet_json=json.dumps(next_diet, ensure_ascii=False))
    await show_next_week_view(call.message, user, next_diet)


# ==========================================
# 8. ИЗМЕНЕНИЕ ПАРАМЕТРОВ В ЛЮБОЙ МОМЕНТ
# ==========================================
@router.callback_query(F.data == "edit_param_weight")
async def cb_edit_weight(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditProfileSG.edit_weight)
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Отмена", callback_data="cancel_edit_param")
    await call.message.edit_text("Введите ваш новый вес (в кг):", reply_markup=b.as_markup())


@router.message(EditProfileSG.edit_weight)
async def msg_edit_weight(message: Message, state: FSMContext):
    try:
        w = float(message.text.replace(",", ".").strip())
        if not (30 <= w <= 250):
            raise ValueError()
    except Exception:
        await message.answer("⚠️ Введите корректный вес числом:")
        return
    user_id = message.from_user.id
    save_user_field(user_id, weight=w)
    user = get_user(user_id)
    macros = calculate_macros(user)
    save_user_field(user_id, target_p=macros["p"], target_f=macros["f"], target_c=macros["c"], rec_meals=macros["rec_meals"])
    await state.clear()

    wait_msg = await message.answer("⏳ Вес обновлен! Пересчитываю БЖУ и рацион...")
    user = get_user(user_id)
    new_diet = await asyncio.to_thread(generate_weekly_diet_ai, user, False, None)
    save_user_field(user_id, current_diet_json=json.dumps(new_diet, ensure_ascii=False))
    await wait_msg.edit_text(format_weekly_summary_text(user, new_diet), reply_markup=kb_main_app_menu(bool(user.get("next_diet_json"))))


@router.callback_query(F.data == "edit_param_meals")
async def cb_edit_meals(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditProfileSG.edit_meals)
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Отмена", callback_data="cancel_edit_param")
    await call.message.edit_text("Введите новое количество приемов пищи в день (от 1 до 6):", reply_markup=b.as_markup())


@router.message(EditProfileSG.edit_meals)
async def msg_edit_meals(message: Message, state: FSMContext):
    if not (message.text and message.text.strip().isdigit() and 1 <= int(message.text.strip()) <= 6):
        await message.answer("⚠️ Введите число от 1 до 6:")
        return
    user_id = message.from_user.id
    save_user_field(user_id, actual_meals=int(message.text.strip()))
    await state.clear()
    wait_msg = await message.answer("⏳ Количество приемов пищи изменено! Перестраиваю рацион...")
    user = get_user(user_id)
    new_diet = await asyncio.to_thread(generate_weekly_diet_ai, user, False, None)
    save_user_field(user_id, current_diet_json=json.dumps(new_diet, ensure_ascii=False))
    await wait_msg.edit_text(format_weekly_summary_text(user, new_diet), reply_markup=kb_main_app_menu(bool(user.get("next_diet_json"))))


@router.callback_query(F.data == "edit_param_budget")
async def cb_edit_budget(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditProfileSG.edit_budget)
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Отмена", callback_data="cancel_edit_param")
    await call.message.edit_text("Введите новый бюджет на неделю (в рублях):", reply_markup=b.as_markup())


@router.message(EditProfileSG.edit_budget)
async def msg_edit_budget(message: Message, state: FSMContext):
    user_id = message.from_user.id
    save_user_field(user_id, budget=message.text.strip())
    await state.clear()
    wait_msg = await message.answer("⏳ Бюджет обновлен! Перестраиваю рацион...")
    user = get_user(user_id)
    new_diet = await asyncio.to_thread(generate_weekly_diet_ai, user, False, None)
    save_user_field(user_id, current_diet_json=json.dumps(new_diet, ensure_ascii=False))
    await wait_msg.edit_text(format_weekly_summary_text(user, new_diet), reply_markup=kb_main_app_menu(bool(user.get("next_diet_json"))))


@router.callback_query(F.data == "edit_param_supps")
async def cb_edit_supps(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditProfileSG.add_supp_name)
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Отмена", callback_data="cancel_edit_param")
    await call.message.edit_text(
        "Введите название спортивной добавки (и при желании БЖУ порции в скобках), которую хотите добавить:",
        reply_markup=b.as_markup(),
    )


@router.message(EditProfileSG.add_supp_name)
async def msg_add_supp_name(message: Message, state: FSMContext):
    await state.update_data(new_supp_name=message.text.strip())
    await state.set_state(EditProfileSG.add_supp_freq)
    await message.answer(f"Укажите периодичность приема для <b>«{message.text.strip()}»</b>:")


@router.message(EditProfileSG.add_supp_freq)
async def msg_add_supp_freq(message: Message, state: FSMContext):
    data = await state.get_data()
    supp_name = data["new_supp_name"]
    user_id = message.from_user.id
    user = get_user(user_id)
    supps = json.loads(user.get("supplements_json") or "{}")
    supps[supp_name] = message.text.strip()
    save_user_field(user_id, has_supplements=1, use_sport_nutr=1, supplements_json=json.dumps(supps, ensure_ascii=False))
    await state.clear()

    wait_msg = await message.answer("⏳ Добавка включена в план! Обновляю рацион...")
    user = get_user(user_id)
    new_diet = await asyncio.to_thread(generate_weekly_diet_ai, user, False, None)
    save_user_field(user_id, current_diet_json=json.dumps(new_diet, ensure_ascii=False))
    await wait_msg.edit_text(format_weekly_summary_text(user, new_diet), reply_markup=kb_main_app_menu(bool(user.get("next_diet_json"))))


@router.callback_query(F.data == "remove_specific_dish")
async def cb_remove_specific_dish(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditProfileSG.exclude_dish_name)
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Отмена", callback_data="cancel_edit_param")
    await call.message.edit_text("Напишите название блюда, которое нужно убрать и заменить в рационе:", reply_markup=b.as_markup())


@router.message(EditProfileSG.exclude_dish_name)
async def msg_remove_specific_dish(message: Message, state: FSMContext):
    dish_to_remove = message.text.strip()
    user_id = message.from_user.id
    await state.clear()
    wait_msg = await message.answer(f"⏳ Убираю блюдо «{dish_to_remove}» и обновляю рацион...")
    user = get_user(user_id)
    new_diet = await asyncio.to_thread(generate_weekly_diet_ai, user, False, dish_to_remove)
    save_user_field(user_id, current_diet_json=json.dumps(new_diet, ensure_ascii=False))
    await wait_msg.edit_text(format_weekly_summary_text(user, new_diet), reply_markup=kb_main_app_menu(bool(user.get("next_diet_json"))))


@router.callback_query(F.data == "cancel_edit_param")
async def cb_cancel_edit(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb_view_week_summary(call)


# ==========================================
# 9. ФУНКЦИЯ 2: ПРОДУКТОВЫЙ КОНТРОЛЬ
# ==========================================
def kb_fridge_dashboard(has_diet: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить продукт", callback_data="fridge_add_start")
    b.button(text="🗑 Убрать продукт", callback_data="fridge_del_menu")
    if has_diet:
        b.button(text="🥗 Перейти к моему рациону", callback_data="view_week_summary")
        b.button(text="🔄 Пересчитать рацион с учетом продуктов", callback_data="regen_full_diet")
    else:
        b.button(text="🥗 Создать рацион из моих продуктов", callback_data="mode_diet")
    b.adjust(2, 1, 1)
    return b.as_markup()


def format_fridge_list(user_id: int) -> str:
    prods = get_products_db(user_id)
    if not prods:
        return "📦 <b>Продуктовый контроль</b>\n\nСписок ваших продуктов пока пуст."

    today = datetime.now(MSK_TZ).date()
    lines = ["📦 <b>Продуктовый контроль (Ваши продукты):</b>\n"]
    for p in prods:
        try:
            exp_d = datetime.strptime(p["exp_date"], "%d.%m.%Y").date()
            days_left = (exp_d - today).days
            if days_left < 0:
                status = f"❌ <b>СТУХ! ({p['exp_date']})</b>"
            elif days_left == 0:
                status = f"⚠️ Истекает сегодня ({p['exp_date']})"
            elif days_left == 1:
                status = f"⏳ Стухнет завтра (до {p['exp_date']})"
            elif days_left <= 5:
                status = f"⚠️ Стухнет через {days_left} дн. (до {p['exp_date']})"
            else:
                status = f"✅ Годен до {p['exp_date']} ({days_left} дн.)"
        except Exception:
            status = f"до {p['exp_date']}"
        lines.append(f"• <b>{p['name']}</b> — {status}")
    return "\n".join(lines)


@router.callback_query(F.data == "mode_fridge")
@router.callback_query(F.data == "fridge_add_start")
async def start_fridge_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(FridgeSG.entering_product)
    await call.message.edit_text(
        "Введите название продукта и срок годности до какого числа\n"
        "<i>(например: <code>Сыр пармезан 12.02.2027</code>)</i>:"
    )


@router.callback_query(F.data == "open_fridge_app")
async def open_fridge_dashboard(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user = get_user(call.from_user.id)
    has_diet = bool(user and user.get("current_diet_json"))
    await call.message.edit_text(format_fridge_list(call.from_user.id), reply_markup=kb_fridge_dashboard(has_diet))


@router.message(FridgeSG.entering_product)
async def process_fridge_product_input(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    match = re.search(r"(\d{2}\.\d{2}\.\d{4})$", txt)
    if not match:
        await message.answer(
            "⚠️ Укажите название и дату окончания срока годности в формате ДД.ММ.ГГГГ "
            "(например: <code>Сыр пармезан 12.02.2027</code>):"
        )
        return

    date_str = match.group(1)
    name_str = txt[: match.start()].strip()
    if not name_str:
        await message.answer("⚠️ Вы не указали название продукта перед датой!")
        return

    try:
        datetime.strptime(date_str, "%d.%m.%Y")
    except ValueError:
        await message.answer("⚠️ Некорректная дата в календаре! Проверьте формат ДД.ММ.ГГГГ:")
        return

    add_product_db(message.from_user.id, name_str, date_str)
    await state.set_state(FridgeSG.confirm_more)

    b = InlineKeyboardBuilder()
    b.button(text="Да", callback_data="fridge_more_yes")
    b.button(text="Нет", callback_data="fridge_more_no")
    b.adjust(2)

    await message.answer(
        f"✅ <b>Готово!</b> ({name_str} — до {date_str})\nДобавить ли еще продукт?",
        reply_markup=b.as_markup(),
    )


@router.callback_query(FridgeSG.confirm_more, F.data == "fridge_more_yes")
async def cb_fridge_more_yes(call: CallbackQuery, state: FSMContext):
    await state.set_state(FridgeSG.entering_product)
    await call.message.edit_text(
        "✅ <b>Сохранено!</b>\nВведите название следующего продукта и срок годности (например: <code>Молоко 20.05.2026</code>):"
    )


@router.callback_query(FridgeSG.confirm_more, F.data == "fridge_more_no")
async def cb_fridge_more_no(call: CallbackQuery, state: FSMContext):
    await state.clear()
    b = InlineKeyboardBuilder()
    b.button(text="Да", callback_data="fridge_create_diet_yes")
    b.button(text="Нет", callback_data="fridge_create_diet_no")
    b.adjust(2)

    await call.message.edit_text(
        "✅ <b>Сохранено!</b> Жду ваших команд.\n\nХотите создать рацион (с обязательным учетом продуктов, которые уже есть у вас)?",
        reply_markup=b.as_markup(),
    )


@router.callback_query(F.data == "fridge_create_diet_yes")
async def cb_fridge_create_diet_yes(call: CallbackQuery, state: FSMContext):
    user = get_user(call.from_user.id)
    if user and user.get("height") and user.get("weight"):
        await cb_regen_full_diet(call)
    else:
        await start_diet_mode(call, state)


@router.callback_query(F.data == "fridge_create_diet_no")
async def cb_fridge_create_diet_no(call: CallbackQuery, state: FSMContext):
    await open_fridge_dashboard(call, state)


@router.callback_query(F.data == "fridge_del_menu")
async def cb_fridge_del_menu(call: CallbackQuery):
    prods = get_products_db(call.from_user.id)
    if not prods:
        await call.answer("Список продуктов пуст!", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for p in prods:
        b.button(text=f"❌ {p['name']} ({p['exp_date']})", callback_data=f"delprod_{p['id']}")
    b.button(text="🔙 Назад", callback_data="open_fridge_app")
    b.adjust(1)
    await call.message.edit_text("Нажмите на продукт, который хотите убрать из списка:", reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("delprod_"))
async def cb_del_prod_item(call: CallbackQuery):
    prod_id = int(call.data.split("_")[1])
    delete_product_db(prod_id, call.from_user.id)
    await call.answer("✅ Продукт удален!")
    await cb_fridge_del_menu(call)


# ==========================================
# 10. ФОНОВЫЕ ЗАДАЧИ И НАПОМИНАНИЯ (МСК)
# ==========================================
async def send_daily_tip(user: dict):
    """Генерирует и отправляет уникальный краткий совет по тренировкам и питанию без повторов."""
    history = json.loads(user.get("tips_history_json") or "[]")
    recent_tips = "; ".join(history[-30:]) if history else "нет"

    sys_p = (
        "Ты профессиональный тренер и спортивный нутрициолог. "
        "Напиши ОДИН краткий (2-3 предложения), но очень полезный, научно достоверный практический совет по тренировкам или спортивному питанию."
    )
    usr_p = (
        f"Напиши новый совет (например, про белково-углеводное окно после тренировки, питание за 2-3 часа до сна, казеин/творог/кефир на ночь, гидратацию, восстановление ЦНС и т.д.). "
        f"Категорически НЕ ПОВТОРЯЙ эти прошлые темы: {recent_tips}."
    )
    tip = await asyncio.to_thread(giga.ask, sys_p, usr_p, 0.8)
    if not tip:
        tip = "После тренировки желательно принять пищу (белки + углеводы) в первый час, чтобы восполнить запасы гликогена и запустить синтез мышечного белка."

    history.append(tip[:80])
    save_user_field(user["user_id"], tips_history_json=json.dumps(history[-100:], ensure_ascii=False))

    b = InlineKeyboardBuilder()
    b.button(text="🥗 Открыть рацион", callback_data="view_week_summary")
    b.button(text="📦 Мои продукты", callback_data="open_fridge_app")
    b.adjust(2)

    await bot.send_message(
        user["user_id"],
        f"💪 <b>Полезный совет дня по тренировкам и питанию:</b>\n\n{tip}",
        reply_markup=b.as_markup(),
    )


async def minute_scheduler_tick():
    now_msk = datetime.now(MSK_TZ)
    curr_hm = now_msk.strftime("%H:%M")
    weekday_num = now_msk.isoweekday()

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users")
    users = [dict(r) for r in c.fetchall()]
    conn.close()

    for u in users:
        uid = u["user_id"]
        notify_hm = u.get("notify_time") or "08:00"

        if u.get("current_diet_json") and curr_hm == notify_hm:
            try:
                diet_data = json.loads(u["current_diet_json"])
                text, kb = format_day_text_and_kb(diet_data, weekday_num, "curr")
                await bot.send_message(
                    uid,
                    f"☀️ <b>Доброе утро! Ваш подробный рацион на сегодня:</b>\n\n{text}",
                    reply_markup=kb,
                )
            except Exception as e:
                logging.error(f"Daily menu send error for {uid}: {e}")

        try:
            nh, nm = [int(x) for x in notify_hm.split(":")]
            notify_minutes = nh * 60 + nm
            default_tip_minutes = 12 * 60  # 12:00

            if abs(default_tip_minutes - notify_minutes) < 40 and default_tip_minutes >= notify_minutes:
                tip_minutes = notify_minutes + 40
            else:
                tip_minutes = default_tip_minutes

            tip_hm = f"{tip_minutes // 60:02d}:{tip_minutes % 60:02d}"
            if curr_hm == tip_hm:
                await send_daily_tip(u)
        except Exception as e:
            logging.error(f"Tip schedule error for {uid}: {e}")


async def daily_morning_checks():
    today = datetime.now(MSK_TZ).date()

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users")
    users = [dict(r) for r in c.fetchall()]
    conn.close()

    for u in users:
        uid = u["user_id"]
        prods = get_products_db(uid)
        alerts = []

        for p in prods:
            try:
                exp_d = datetime.strptime(p["exp_date"], "%d.%m.%Y").date()
                diff = (exp_d - today).days
                if diff == 5:
                    alerts.append(f"⚠️ <b>{p['name']}</b> стухнет через 5 дней, срок годности до {p['exp_date']}")
                elif diff == 2:
                    alerts.append(f"⚠️ <b>{p['name']}</b> стухнет через 2 дня, срок годности до {p['exp_date']}")
                elif diff == 1:
                    alerts.append(f"⏳ <b>{p['name']}</b> стухнет завтра, срок годности до {p['exp_date']}")
                elif diff <= 0:
                    alerts.append(f"❌ <b>{p['name']} стух! {p['exp_date']}</b>")
            except Exception:
                pass

        if alerts:
            b = InlineKeyboardBuilder()
            b.button(text="➕ Добавить продукт", callback_data="fridge_add_start")
            b.button(text="🗑 Убрать продукт", callback_data="fridge_del_menu")
            b.button(text="📦 Открыть список продуктов", callback_data="open_fridge_app")
            b.adjust(2, 1)
            await bot.send_message(
                uid,
                "🔔 <b>Контроль срока годности продуктов:</b>\n\n" + "\n".join(alerts),
                reply_markup=b.as_markup(),
            )

        if u.get("diet_start_date"):
            try:
                start_d = datetime.strptime(u["diet_start_date"], "%d.%m.%Y").date()
                days_passed = (today - start_d).days
                if days_passed > 0 and days_passed % 7 == 0:
                    b = InlineKeyboardBuilder()
                    b.button(text="Да", callback_data="gen_next_week")
                    b.button(text="Нет", callback_data="view_week_summary")
                    b.adjust(2)
                    await bot.send_message(
                        uid,
                        "📅 <b>Сегодня последний день вашего недельного рациона!</b>\nПоказать рацион на следующую неделю?",
                        reply_markup=b.as_markup(),
                    )
            except Exception:
                pass

        if u.get("last_month_check"):
            try:
                last_m = datetime.strptime(u["last_month_check"], "%d.%m.%Y").date()
                if (today - last_m).days >= 30:
                    save_user_field(uid, last_month_check=today.strftime("%d.%m.%Y"))
                    b = InlineKeyboardBuilder()
                    b.button(text="🔄 Обновить анкету и пересчитать рацион", callback_data="mode_diet")
                    b.button(text="⚖️ Быстро обновить только вес", callback_data="edit_param_weight")
                    b.adjust(1)
                    await bot.send_message(
                        uid,
                        "🏆 <b>Прошел месяц! Как ваши успехи?</b>\n"
                        "Давайте обновим ваш текущий вес, количество тренировок и сформируем свежий рацион по новым данным!",
                        reply_markup=b.as_markup(),
                    )
            except Exception:
                pass


# ==========================================
# 11. ЗАПУСК БОТА
# ==========================================
async def main():
    init_db()
    scheduler = AsyncIOScheduler(timezone=MSK_TZ)
    scheduler.add_job(minute_scheduler_tick, "cron", second=0)
    scheduler.add_job(daily_morning_checks, "cron", hour=9, minute=0)
    scheduler.start()

    logging.info("Бот успешно запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
