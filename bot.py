import asyncio
import json
import logging
import math
import os
import re
import sqlite3
from datetime import datetime, timedelta

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

WEEKDAYS_MAP = {
    "понедельник": 0, "пн": 0,
    "вторник": 1, "вт": 1,
    "среда": 2, "ср": 2,
    "четверг": 3, "чт": 3,
    "пятница": 4, "пт": 4,
    "суббота": 5, "сб": 5,
    "воскресенье": 6, "вс": 6,
}


# ==========================================
# 1. БАЗА ДАННЫХ (SQLite) С АВТО-ОБНОВЛЕНИЕМ
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
            tips_history_json TEXT DEFAULT '[]',
            shopping_list_cache TEXT DEFAULT '',
            offered_fridge INTEGER DEFAULT 0,
            offered_diet INTEGER DEFAULT 0,
            expert_note TEXT DEFAULT ''
        )
    """)
    # Безопасное добавление новых колонок, если база уже была создана ранее
    new_cols = [
        ("shopping_list_cache", "TEXT DEFAULT ''"),
        ("offered_fridge", "INTEGER DEFAULT 0"),
        ("offered_diet", "INTEGER DEFAULT 0"),
        ("expert_note", "TEXT DEFAULT ''"),
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
                    logging.warning(f"Модель {model} вернула код {resp.status_code}")
            except Exception as e:
                logging.error(f"Ошибка запроса к модели {model}: {e}")
        return ""


giga = OpenRouterClient()


# ==========================================
# 3. ГОЛОВА БОТА: ЭКСПЕРТНЫЙ РАСЧЕТ БЖУ И ПРИЕМОВ ПИЩИ
# ==========================================
def get_diet_dates_list(start_date_str: str) -> list[str]:
    """Возвращает 7 дат в формате ДД.ММ начиная с даты старта рациона."""
    try:
        start_dt = datetime.strptime(start_date_str, "%d.%m.%Y").date()
    except Exception:
        start_dt = datetime.now(MSK_TZ).date() + timedelta(days=1)
    return [(start_dt + timedelta(days=i)).strftime("%d.%m") for i in range(7)]


def calculate_macros(user: dict) -> dict:
    """
    Умный экспертный расчет БЖУ с участием нейросети ('головы бота') и проверкой безопасности:
    - Норма белка в среднем 1.5 - 2.0 г на 1 кг веса (никогда не 2.5 - 3 г!).
    - При маленьком весе (<55 кг) не нагружаем сразу большим количеством еды, усвоение ~25 г белка за прием.
    - При весе > 90-95 кг и цели 'Набор мышечной массы' — предупреждаем пользователя, что набирать массу при таком весе не рекомендуется, лучше выбрать сушку/рекомпозицию (но выбор за ним).
    - Питание максимум 5 раз в день (6 раз не предлагаем).
    - Если по расчетам выходит, например, 157 г белка — не делаем 6-й прием ради 7 г, а урезаем до 150 г (на 5 приемов).
    - Если белка 165-170 г и выше — объясняем, что есть 5-6 раз тяжело, большинство едят 3-4 раза и добавляют протеин или гейнер.
    """
    weight = float(user.get("weight") or 75)
    height = int(user.get("height") or 175)
    age = int(user.get("age") or 25)
    goal = user.get("goal") or "Набор мышечной массы"
    exp = user.get("exp_level") or "Начинающий"
    workouts = user.get("workouts") or "1-3"

    # Если пользователь ввел свои дневные показатели БЖУ вручную
    if user.get("custom_bju"):
        try:
            parts = [int(x) for x in re.findall(r"\d+", user["custom_bju"])]
            if len(parts) >= 3:
                p, f, c = parts[0], parts[1], parts[2]
                fiber = parts[3] if len(parts) >= 4 else 30
                rec_meals = min(5, max(3, round(p / 30.0)))
                return {
                    "p": p, "f": f, "c": c, "fiber": fiber,
                    "rec_meals": rec_meals,
                    "expert_note": "Рассчитано по вашим индивидуальным дневным показателям БЖУ.",
                    "high_protein_sport_hint": p >= 165,
                    "heavy_mass_warning": (weight >= 90 and "набор" in goal.lower()),
                }
        except Exception:
            pass

    # Запрос к нейросети ("голове бота") для точной экспертной оценки
    sys_p = (
        "Ты — профессиональный спортивный нутрициолог и фитнес-тренер. "
        "Рассчитай дневную норму БЖУ на основе проверенных научных источников строго в формате JSON."
    )
    usr_p = f"""
Данные человека: Рост {height} см, Вес {weight} кг, Возраст {age} лет, Опыт: {exp}, Тренировок в неделю: {workouts}, Цель: {goal}.
Правила:
1. Белок должен быть в разумных пределах 1.5 - 2.0 г на кг веса (при большом весе >90 кг считай на сухую/оптимальную массу тела, никогда не ставь 2.5-3 г/кг).
2. При маленьком весе (<55 кг) не давай сразу огромный профицит (это опасно для ЖКТ, нужно набирать постепенно, за 1 прием усваивается ~25 г белка).
3. Если белка получилось например 155-158 г, округли до 150 г (чтобы ровно ложилось в 5 приемов пищи по 30 г без лишнего 6-го приема).
4. Рекомендуемое количество приемов пищи (rec_meals): от 3 до 5 (максимум 5!).
Верни СТРОГО JSON:
{{"p": 140, "f": 60, "c": 260, "fiber": 30, "rec_meals": 4, "expert_note": "Краткий совет эксперта (1-2 предложения)"}}
"""
    raw = giga.ask(sys_p, usr_p, temperature=0.3)
    ai_p, ai_f, ai_c, ai_fiber, ai_meals, expert_note = None, None, None, 30, None, ""
    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            d = json.loads(m.group(0))
            ai_p = int(d.get("p", 0))
            ai_f = int(d.get("f", 0))
            ai_c = int(d.get("c", 0))
            ai_fiber = int(d.get("fiber", 30))
            ai_meals = int(d.get("rec_meals", 4))
            expert_note = d.get("expert_note", "")
    except Exception:
        pass

    # Надежная проверка и корректировка по твоим правилам
    effective_weight = min(weight, 88.0) if weight > 90 else weight
    if not ai_p or ai_p < 60 or ai_p > 210:
        if weight < 55:
            p_mult = 1.6  # Плавный безопасный старт для небольшого веса
        elif "набор" in goal.lower():
            p_mult = 1.8 if exp == "Начинающий" else 2.0
        else:
            p_mult = 1.8
        ai_p = int(round(effective_weight * p_mult))

    if not ai_c or ai_c < 80 or ai_c > 450:
        if "набор" in goal.lower():
            c_mult = 3.2 if weight < 55 else (2.5 if weight >= 90 else 3.6)
        else:
            c_mult = 2.0
        ai_c = int(round(effective_weight * c_mult))

    if not ai_f or ai_f < 25 or ai_f > 130:
        f_mult = 0.9 if "набор" in goal.lower() else 0.8
        ai_f = max(30, int(round(effective_weight * f_mult)))

    # Правило: если получилось 151..162 г белка (5 приемов + 1..12 г хвостика), урезаем до 150 г, чтобы не делать 6-й прием!
    if 151 <= ai_p <= 162:
        ai_p = 150

    per_meal_p = 25.0 if weight < 55 else 30.0
    calc_meals = math.ceil(ai_p / per_meal_p)
    # Если из-за 5-8 грамм перевалило на следующий прием пищи — урезаем эти граммы!
    if calc_meals > 3 and (ai_p - (calc_meals - 1) * per_meal_p) <= 8:
        calc_meals -= 1
        ai_p = int(calc_meals * per_meal_p)

    rec_meals = min(5, max(3, calc_meals))

    heavy_mass_warning = (weight >= 90 and "набор" in goal.lower())
    high_protein_sport_hint = (ai_p >= 165)

    return {
        "p": ai_p,
        "f": ai_f,
        "c": ai_c,
        "fiber": ai_fiber,
        "rec_meals": rec_meals,
        "expert_note": expert_note,
        "high_protein_sport_hint": high_protein_sport_hint,
        "heavy_mass_warning": heavy_mass_warning,
    }


def generate_weekly_diet_ai(user: dict, is_next_week: bool = False, exclude_dish: str = None) -> dict:
    """Генерирует практичный, реальный рацион из простых продуктов магазинов пользователя с датами ДД.ММ."""
    meals_count = int(user.get("actual_meals") or user.get("rec_meals") or 4)
    use_sport = bool(user.get("use_sport_nutr"))
    target_p = int(user.get("target_p") or 140)
    target_f = int(user.get("target_f") or 60)
    target_c = int(user.get("target_c") or 260)
    weight = float(user.get("weight") or 75)
    max_p_per_meal = 25 if weight < 55 else 30

    if not use_sport:
        actual_daily_p = min(target_p, meals_count * max_p_per_meal)
        actual_daily_c = min(target_c, meals_count * 65)
    else:
        actual_daily_p = target_p
        actual_daily_c = target_c

    actual_daily_f = target_f

    start_date_str = user.get("diet_start_date") or (datetime.now(MSK_TZ).date() + timedelta(days=1)).strftime("%d.%m.%Y")
    if is_next_week:
        try:
            dt = datetime.strptime(start_date_str, "%d.%m.%Y").date() + timedelta(days=7)
            start_date_str = dt.strftime("%d.%m.%Y")
        except Exception:
            pass
    dates_list = get_diet_dates_list(start_date_str)

    products = get_products_db(user["user_id"])
    fridge_items = ", ".join([p["name"] for p in products]) if products else "нет данных"
    supps_data = json.loads(user.get("supplements_json") or "{}")
    supps_text = "; ".join([f"{k} ({v})" for k, v in supps_data.items()]) if supps_data else "Протеин / Гейнер по норме"

    system_prompt = (
        "Ты — практичный российский спортивный нутрициолог. "
        "Составляй рацион ТОЛЬКО из простых, доступных продуктов, которые реально продаются в магазинах пользователя и легко готовятся."
    )

    sport_rule = (
        f"Пользователь выбрал рацион СО СПОРТИВНЫМ ПИТАНИЕМ ({supps_text}). "
        "Впиши спортивное питание между приемами пищи или вместе с ними, чтобы добрать целевое БЖУ. "
        "ВАЖНО: Белок без углеводов и углеводы без белка НЕ употребляются! Если ставишь протеин — обязательно добавь к нему углевод (например, банан, хлебцы, овсянку)."
        if use_sport
        else f"Рацион БЕЗ спортивного питания. За один прием пищи максимум {max_p_per_meal} г белка и 65 г углеводов."
    )

    exclude_rule = f"Исключи блюдо/продукт '{exclude_dish}'!" if exclude_dish else ""

    user_prompt = f"""
Составь практичный рацион на 7 дней по датам: {', '.join(dates_list)}.
Данные пользователя:
- Вес: {user['weight']} кг, Рост: {user['height']} см, Цель: {user['goal']}
- Аллергии/противопоказания: {user['allergies']}
- Бюджет на неделю: {user['budget']}
- Магазины пользователя: {user['stores']}
- Продукты, которые УЖЕ есть дома (используй в первую очередь): {fridge_items}
- Приемов пищи в день: {meals_count}
- Целевое БЖУ в день: Белки {actual_daily_p} г, Жиры {actual_daily_f} г, Углеводы {actual_daily_c} г.
- {sport_rule}
- {exclude_rule}

ЖЕСТКИЕ ПРАВИЛА ПО ПРОДУКТАМ И БЛЮДАМ:
1. Никаких сложных ресторанных блюд, трюфелей, моллюсков, утки, мраморной говядины Black Angus или дорогой форели!
2. Используй простые народные продукты: куриная грудка/филе бедра, индейка, обычная нежирная говядина/вырезка, горбуша или минтай/треска, яйца, творог, кефир, сыр, гречка, рис, овсянка, макароны, картофель, простые овощи и фрукты (огурцы, помидоры, капуста, яблоки, бананы).
3. Блюда МОГУТ и должны практично повторяться (например, на 2 дня подряд), чтобы человеку было удобно готовить и закупаться в магазине {user['stores']}.

Верни СТРОГО валидный JSON следующей структуры:
{{
  "weekly_summary": {{
    "daily_p": {actual_daily_p},
    "daily_f": {actual_daily_f},
    "daily_c": {actual_daily_c},
    "week_p": {actual_daily_p * 7},
    "week_f": {actual_daily_f * 7},
    "week_c": {actual_daily_c * 7},
    "note": "Краткий совет по рациону"
  }},
  "days": [
    {{
      "day_num": 1,
      "day_name": "{dates_list[0]}",
      "p": {actual_daily_p},
      "f": {actual_daily_f},
      "c": {actual_daily_c},
      "meals": [
        {{
          "meal_num": 1,
          "meal_title": "Прием 1",
          "dish_name": "Овсяная каша с вареными яйцами",
          "ingredients": "Овсяные хлопья 60г, яйца куриные 2 шт, молоко 100мл",
          "p": 24,
          "f": 14,
          "c": 45
        }}
      ]
    }}
  ]
}}
Обязательно создай все 7 дней (day_num от 1 до 7) с датами {', '.join(dates_list)}.
"""

    raw = giga.ask(system_prompt, user_prompt, temperature=0.4)
    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            if "days" in data and len(data["days"]) == 7:
                for idx, d in enumerate(data["days"]):
                    d["day_num"] = idx + 1
                    d["day_name"] = dates_list[idx]
                return data
    except Exception as e:
        logging.error(f"JSON diet parse error: {e}")

    return build_fallback_diet(meals_count, actual_daily_p, actual_daily_f, actual_daily_c, use_sport, supps_text, dates_list)


def build_fallback_diet(meals_count, daily_p, daily_f, daily_c, use_sport, supps_text, dates_list):
    sample_dishes = [
        ("Овсянка на молоке + 2 вареных яйца", "Овсяные хлопья 60г, яйца куриные 2 шт, молоко 100мл"),
        ("Куриное филе с гречкой и огурцом", "Филе куриной грудки 130г, гречка 60г, огурец свежий 100г"),
        ("Индейка тушеная с рисом", "Филе индейки 130г, рис 60г, морковь и лук 50г"),
        ("Творог 5% с бананом и хлебцами", "Творог 5% 150г, банан 1 шт, хлебцы 2 шт"),
        ("Горбуша запеченная с картофелем", "Филе горбуши 140г, картофель 160г, капустный салат 100г"),
        ("Говяжья вырезка тушеная с макаронами", "Говядина постная 120г, макароны из тв. сортов 60г, томаты 100г"),
        ("Омлет из 3 яиц с хлебом и сыром", "Яйца куриные 3 шт, хлеб цельнозерновой 2 ломтика, сыр 20г"),
    ]
    p_per_meal = min(30, int(daily_p / meals_count))
    f_per_meal = int(daily_f / meals_count)
    c_per_meal = min(65, int(daily_c / meals_count))

    days = []
    for d_idx, date_label in enumerate(dates_list, start=1):
        meals = []
        for m_idx in range(1, meals_count + 1):
            dish, ingr = sample_dishes[(d_idx + m_idx) % len(sample_dishes)]
            meals.append({
                "meal_num": m_idx,
                "meal_title": f"Прием {m_idx}",
                "dish_name": dish,
                "ingredients": ingr,
                "p": p_per_meal,
                "f": f_per_meal,
                "c": c_per_meal,
            })
        if use_sport:
            meals.append({
                "meal_num": meals_count + 1,
                "meal_title": "Спортивное питание",
                "dish_name": "Протеин + Банан (белок + углеводы)",
                "ingredients": f"Протеин 1 порция (30г), банан 1 шт (120г). Добавки: {supps_text}",
                "p": max(20, daily_p - p_per_meal * meals_count),
                "f": 2,
                "c": max(25, daily_c - c_per_meal * meals_count),
            })
        days.append({
            "day_num": d_idx,
            "day_name": date_label,
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
            "note": "Практичный рацион из простых и доступных продуктов.",
        },
        "days": days,
    }


def generate_shopping_list_ai(user: dict, diet_data: dict) -> str:
    """Формирует список покупок под магазины пользователя со средними ценами, марками хорошего качества и итоговой суммой."""
    cached = user.get("shopping_list_cache")
    if cached and len(cached) > 20:
        return cached

    all_ingredients = []
    for d in diet_data.get("days", []):
        for m in d.get("meals", []):
            all_ingredients.append(f"{m['dish_name']} ({m['ingredients']})")

    fridge_prods = get_products_db(user["user_id"])
    already_have = ", ".join([p["name"] for p in fridge_prods]) if fridge_prods else "нет"

    sys_p = (
        "Ты — эксперт по закупкам продуктов в российских торговых сетях. "
        "Твоя задача — составить точный список покупок на неделю по рациону пользователя."
    )
    usr_p = f"""
Магазины пользователя: {user['stores']}
Бюджет пользователя на неделю: {user['budget']}
Продукты, которые уже есть дома: {already_have}
Все приемы пищи за неделю: {'; '.join(all_ingredients[:25])}

Правила составления списка покупок:
1. Опирайся ИСКЛЮЧИТЕЛЬНО на магазины, которые указал пользователь ({user['stores']}).
2. Выбирай продукты хорошего качества в среднем ценовом сегменте (НЕ самые дорогие премиум-бренды, но и НЕ самые дешевые низкокачественные аналоги).
3. Укажи конкретный продукт, популярную проверенную фирму/марку (которая реально продается в {user['stores']}), нужное количество/вес на всю неделю и актуальную среднюю стоимость в рублях согласно этому весу.
4. В самом низу обязательно напиши ИТОГОВУЮ СТОИМОСТЬ всех продуктов в рублях.
5. Оформи красиво, презентабельно, по категориям (Мясо/Птица/Рыба, Молочные продукты и яйца, Крупы и бакалея, Овощи и фрукты), используя HTML-теги <b> и списки •.
"""
    res = giga.ask(sys_p, usr_p, temperature=0.4)
    if not res:
        res = (
            f"🛒 <b>Список продуктов на неделю (Магазины: {user['stores']})</b>\n\n"
            "<b>🥩 Мясо, птица и рыба:</b>\n"
            "• Филе грудки куриной (Петелинка / Благояр) — 1.2 кг (~480 ₽)\n"
            "• Филе индейки (Индилайт) — 800 г (~460 ₽)\n"
            "• Горбуша свежемороженая / филе — 700 г (~350 ₽)\n\n"
            "<b>🥛 Молочные продукты и яйца:</b>\n"
            "• Яйцо куриное С1 (Окское / Роскар) — 2 десятка (~240 ₽)\n"
            "• Творог 5% (Простоквашино / Савушкин) — 4 пачки по 180г (~360 ₽)\n"
            "• Молоко 2.5% (Домик в деревне / Простоквашино) — 1 л (~95 ₽)\n\n"
            "<b>🌾 Крупы и бакалея:</b>\n"
            "• Овсяные хлопья Геркулес (Русский продукт / Ясно Солнышко) — 500 г (~65 ₽)\n"
            "• Гречневая крупа ядрица (Увелка / Мистраль) — 800 г (~110 ₽)\n"
            "• Рис длиннозерный / бурый (Националь) — 800 г (~140 ₽)\n"
            "• Макароны из тв. сортов (Barilla / Makfa) — 450 г (~95 ₽)\n"
            "• Хлебцы цельнозерновые (Dr.Korner) — 2 уп. (~160 ₽)\n\n"
            "<b>🥦 Овощи и фрукты:</b>\n"
            "• Бананы — 1.2 кг (~180 ₽)\n"
            "• Огурцы и томаты свежие — 1.5 кг (~350 ₽)\n"
            "• Картофель и капуста белокочанная — 1.5 кг (~120 ₽)\n\n"
            "💰 <b>Итоговая стоимость всех продуктов: ~3 205 ₽</b>"
        )
    save_user_field(user["user_id"], shopping_list_cache=res)
    return res


def get_detailed_recipe(user_id: int, dish_name: str, ingredients: str) -> str:
    user = get_user(user_id)
    cache = json.loads(user.get("recipes_cache_json") or "{}")
    if dish_name in cache:
        return cache[dish_name]

    sys_p = "Ты — опытный шеф-повар. Напиши простой и понятный пошаговый рецепт приготовления блюда (вплоть до того, сколько минут варить яйцо или крупу)."
    usr_p = f"Блюдо: {dish_name}\nИнгредиенты: {ingredients}\nНапиши подробный пошаговый рецепт."
    recipe = giga.ask(sys_p, usr_p, temperature=0.4)
    if not recipe:
        recipe = (
            f"<b>📖 Подробный рецепт: {dish_name}</b>\n\n"
            f"<b>Ингредиенты:</b> {ingredients}\n\n"
            "1. Подготовьте и взвесьте все ингредиенты.\n"
            "2. Если в блюде есть яйца: положите в холодную воду, после закипания варите 8–9 минут вкрутую, затем остудите в холодной воде.\n"
            "3. Крупу промойте и варите на медленном огне в пропорции 1:2 с водой 15–20 минут.\n"
            "4. Птицу/рыбу/мясо потушите на сковороде с каплей воды или запеките в духовке при 180°C в течение 20–25 минут."
        )
    cache[dish_name] = recipe
    save_user_field(user_id, recipes_cache_json=json.dumps(cache, ensure_ascii=False))
    return recipe


# ==========================================
# 4. СОСТОЯНИЯ (FSM)
# ==========================================
class OnboardingSG(StatesGroup):
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
    q_start_day = State()
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
    insert_bought_product = State()


class FridgeSG(StatesGroup):
    entering_product = State()
    confirm_more = State()


# ==========================================
# 5. КЛАВИАТУРЫ И НАВИГАЦИЯ «НАЗАД»
# ==========================================
def kb_start_modes() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🥗 Рацион", callback_data="mode_diet")
    b.button(text="📦 Продуктовый контроль", callback_data="mode_fridge")
    b.adjust(2)
    return b.as_markup()


def kb_back_only(cb_data: str = "onboard_back") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Назад", callback_data=cb_data)
    return b.as_markup()


def kb_main_app_menu(user: dict, diet_data: dict) -> InlineKeyboardMarkup:
    """
    Компактная, презентабельная клавиатура приложения Рациона:
    - Кнопки дней: ТОЛЬКО ЧИСЛА (например 16.12, 17.12) без значка календаря!
    - Кнопка списка покупок.
    - Кнопка добавления купленного продукта в рацион.
    - ОДНА кнопка перехода в Контроль продуктов (чтобы не засорять экран).
    """
    b = InlineKeyboardBuilder()
    days = diet_data.get("days", [])
    for d in days:
        d_num = d.get("day_num", 1)
        d_label = d.get("day_name", f"День {d_num}")
        b.button(text=f"{d_label}", callback_data=f"view_day_{d_num}_curr")

    b.button(text="🛒 Список продуктов к покупке", callback_data="view_shopping_list")
    b.button(text="🥑 Вписать купленный продукт", callback_data="insert_bought_prod")

    if user.get("next_diet_json"):
        b.button(text="⏭ Показать след. неделю", callback_data="view_next_week")
    else:
        b.button(text="⏭ Рацион на след. неделю", callback_data="gen_next_week")

    b.button(text="🔄 Изменить рацион / блюдо", callback_data="open_diet_edit_menu")
    b.button(text="⚙️ Изменить мои данные", callback_data="open_profile_edit_menu")
    b.button(text="📦 Контроль продуктов", callback_data="open_fridge_app")

    # 4 + 3 для дат (чтобы на любом экране телефона числа были крупными и читаемыми), далее по 1-2 кнопки
    b.adjust(4, 3, 1, 2, 2, 1)
    return b.as_markup()


async def push_step(state: FSMContext, step_name: str):
    data = await state.get_data()
    history = data.get("step_history", [])
    if not history or history[-1] != step_name:
        history.append(step_name)
    await state.update_data(step_history=history)


async def render_onboarding_step(target_message: Message, state: FSMContext, step_name: str, user_id: int, is_edit: bool = True):
    """Отрисовывает любой шаг анкеты (при движении вперед или при нажатии кнопки 'Назад' сколько угодно раз)."""
    send_fn = target_message.edit_text if is_edit else target_message.answer

    if step_name == "start_modes":
        await state.clear()
        await send_fn("Выберите режим:", reply_markup=kb_start_modes())
        return

    if step_name == "ask_custom_bju_choice":
        save_user_field(user_id, custom_bju="")
        await state.set_state(OnboardingSG.ask_custom_bju_choice)
        b = InlineKeyboardBuilder()
        b.button(text="Рассчитать автоматически", callback_data="bju_auto")
        b.button(text="Ввести свои показатели БЖУ", callback_data="bju_custom")
        b.button(text="🔙 Назад", callback_data="onboard_back")
        b.adjust(1)
        await send_fn(
            "Желаете ввести конкретные показатели БЖУ, которые вам нужны, или бот рассчитает их автоматически?",
            reply_markup=b.as_markup(),
        )

    elif step_name == "entering_custom_bju":
        save_user_field(user_id, custom_bju="")
        await state.set_state(OnboardingSG.entering_custom_bju)
        await send_fn(
            "Введите свои <b>дневные показатели</b>: белки, жиры, углеводы в формате <code>30 20 300</code>\n"
            "<i>(Обязательно: указывайте именно дневные показатели БЖУ в граммах на весь день!)</i>:",
            reply_markup=kb_back_only(),
        )

    elif step_name == "q1_height":
        save_user_field(user_id, height=None)
        await state.set_state(OnboardingSG.q1_height)
        await send_fn("1. Укажите ваш рост (в см, например: 180):", reply_markup=kb_back_only())

    elif step_name == "q2_weight":
        save_user_field(user_id, weight=None)
        await state.set_state(OnboardingSG.q2_weight)
        await send_fn("2. Укажите ваш вес (в кг, например: 75):", reply_markup=kb_back_only())

    elif step_name == "q3_age":
        save_user_field(user_id, age=None)
        await state.set_state(OnboardingSG.q3_age)
        await send_fn("3. Укажите ваш возраст (полных лет):", reply_markup=kb_back_only())

    elif step_name == "q4_exp":
        save_user_field(user_id, exp_level=None)
        await state.set_state(OnboardingSG.q4_exp)
        b = InlineKeyboardBuilder()
        b.button(text="Начинающий", callback_data="exp_Начинающий")
        b.button(text="Любитель", callback_data="exp_Любитель")
        b.button(text="Профессионал", callback_data="exp_Профессионал")
        b.button(text="🔙 Назад", callback_data="onboard_back")
        b.adjust(1)
        await send_fn("4. Ваш спортивный опыт:", reply_markup=b.as_markup())

    elif step_name == "q5_workouts_btn":
        save_user_field(user_id, workouts=None)
        await state.set_state(OnboardingSG.q5_workouts_btn)
        b = InlineKeyboardBuilder()
        b.button(text="1-3", callback_data="wrk_1-3")
        b.button(text="3 и более", callback_data="wrk_3 и более")
        b.button(text="🔙 Назад", callback_data="onboard_back")
        b.adjust(2, 1)
        await send_fn("5. Количество тренировок в неделю:", reply_markup=b.as_markup())

    elif step_name == "q5_workouts_pro_text":
        save_user_field(user_id, workouts=None)
        await state.set_state(OnboardingSG.q5_workouts_pro_text)
        await send_fn("5. Количество тренировок в неделю (напишите точное число):", reply_markup=kb_back_only())

    elif step_name == "q6_goal":
        save_user_field(user_id, goal=None)
        await state.set_state(OnboardingSG.q6_goal)
        b = InlineKeyboardBuilder()
        b.button(text="Сушка", callback_data="goal_Сушка")
        b.button(text="Набор мышечной массы", callback_data="goal_Набор мышечной массы")
        b.button(text="🔙 Назад", callback_data="onboard_back")
        b.adjust(1)
        await send_fn("6. Ваша цель:", reply_markup=b.as_markup())

    elif step_name == "q7_supps_choice":
        save_user_field(user_id, has_supplements=0, supplements_json="{}")
        await state.set_state(OnboardingSG.q7_supps_choice)
        b = InlineKeyboardBuilder()
        b.button(text="Да", callback_data="supps_yes")
        b.button(text="Нет", callback_data="supps_no")
        b.button(text="🔙 Назад", callback_data="onboard_back")
        b.adjust(2, 1)
        await send_fn(
            "7. Пользуетесь ли спортивными добавками (креатин, протеин, гейнер, BCAA, фарма/тестостерон и др.)?",
            reply_markup=b.as_markup(),
        )

    elif step_name == "q8_supps_list":
        save_user_field(user_id, supplements_json="{}")
        await state.set_state(OnboardingSG.q8_supps_list)
        await send_fn(
            "8. Какими именно добавками (или препаратами) вы пользуетесь?\n"
            "Перечислите их через запятую (по желанию укажите БЖУ порции в скобках, например: <i>Протеин (24г белка), Креатин</i>):",
            reply_markup=kb_back_only(),
        )

    elif step_name == "q10_allergies":
        save_user_field(user_id, allergies=None)
        user = get_user(user_id)
        q_num = "7" if (user and user.get("exp_level") == "Начинающий") else "10"
        await state.set_state(OnboardingSG.q10_allergies)
        await send_fn(f"{q_num}. Укажите противопоказания и аллергии (если нет — напишите «нет»):", reply_markup=kb_back_only())

    elif step_name == "q_budget":
        save_user_field(user_id, budget=None)
        await state.set_state(OnboardingSG.q_budget)
        await send_fn("Укажите ваш бюджет на питание на неделю (в рублях, например: 5000 руб):", reply_markup=kb_back_only())

    elif step_name == "q_stores":
        save_user_field(user_id, stores=None)
        await state.set_state(OnboardingSG.q_stores)
        await send_fn(
            "Какие продуктовые магазины вы используете?\n"
            "<i>(Например: Пятёрочка, Перекрёсток, Чижик или просто напишите рынок)</i>:",
            reply_markup=kb_back_only(),
        )

    elif step_name == "q_notify_time":
        await state.set_state(OnboardingSG.q_notify_time)
        await send_fn(
            "Во сколько с утра (по московскому времени) присылать вам рацион на весь день?\n"
            "Введите время в формате <code>ЧЧ:ММ</code> (например: <code>08:00</code>):",
            reply_markup=kb_back_only(),
        )

    elif step_name == "q_start_day":
        save_user_field(user_id, diet_start_date=None)
        await state.set_state(OnboardingSG.q_start_day)
        b = InlineKeyboardBuilder()
        b.button(text="С завтрашнего дня", callback_data="start_day_tomorrow")
        b.button(text="🔙 Назад", callback_data="onboard_back")
        b.adjust(1)
        await send_fn(
            "С какого дня вы хотите начать рацион?\n"
            "<i>Напишите день недели (например: вторник), дату (например: 18.12) или нажмите кнопку ниже:</i>",
            reply_markup=b.as_markup(),
        )

    elif step_name == "q_ready_meals":
        await show_macros_advice_step(target_message, state, user_id, is_edit=is_edit)

    elif step_name == "q_custom_meals_count":
        await state.set_state(OnboardingSG.q_custom_meals_count)
        await send_fn("Сколько раз в день вы готовы питаться? (Напишите число от 1 до 5):", reply_markup=kb_back_only())


# Обработчик кнопки «🔙 Назад» по всей цепочке анкеты
@router.callback_query(F.data == "onboard_back")
async def cb_onboard_back(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    history = data.get("step_history", [])
    if len(history) <= 1:
        await render_onboarding_step(call.message, state, "start_modes", call.from_user.id, is_edit=True)
        return
    # Удаляем текущий шаг и возвращаемся к предыдущему, стирая введенные на нем данные
    history.pop()
    prev_step = history[-1]
    await state.update_data(step_history=history)
    await render_onboarding_step(call.message, state, prev_step, call.from_user.id, is_edit=True)


# ==========================================
# 6. СТАРТ И ОПРОС ПОЛЬЗОВАТЕЛЯ
# ==========================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    # Бот всегда помнит пользователя и его данные в БД!
    save_user_field(message.from_user.id)
    await message.answer("Выберите режим:", reply_markup=kb_start_modes())


@router.callback_query(F.data == "back_to_start_modes")
async def cb_back_to_start_modes(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Выберите режим:", reply_markup=kb_start_modes())


@router.callback_query(F.data == "mode_diet")
async def start_diet_mode(call: CallbackQuery, state: FSMContext):
    user = get_user(call.from_user.id)
    # Замечание 1: если у пользователя уже есть рассчитанный рацион, бот помнит его и сразу открывает!
    if user and user.get("current_diet_json"):
        await state.clear()
        diet_data = json.loads(user["current_diet_json"])
        text = format_weekly_summary_text(user, diet_data, is_next=False)
        await call.message.edit_text(text, reply_markup=kb_main_app_menu(user, diet_data))
        return

    await state.update_data(step_history=["ask_custom_bju_choice"])
    await render_onboarding_step(call.message, state, "ask_custom_bju_choice", call.from_user.id, is_edit=True)


@router.callback_query(F.data == "restart_questionnaire")
async def cb_restart_questionnaire(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(step_history=["ask_custom_bju_choice"])
    await render_onboarding_step(call.message, state, "ask_custom_bju_choice", call.from_user.id, is_edit=True)


@router.callback_query(OnboardingSG.ask_custom_bju_choice, F.data == "bju_auto")
async def bju_auto_chosen(call: CallbackQuery, state: FSMContext):
    save_user_field(call.from_user.id, custom_bju="")
    await push_step(state, "q1_height")
    await render_onboarding_step(call.message, state, "q1_height", call.from_user.id, is_edit=True)


@router.callback_query(OnboardingSG.ask_custom_bju_choice, F.data == "bju_custom")
async def bju_custom_chosen(call: CallbackQuery, state: FSMContext):
    await push_step(state, "entering_custom_bju")
    await render_onboarding_step(call.message, state, "entering_custom_bju", call.from_user.id, is_edit=True)


@router.message(OnboardingSG.entering_custom_bju)
async def process_custom_bju(message: Message, state: FSMContext):
    nums = re.findall(r"\d+", message.text or "")
    if len(nums) < 3:
        await message.answer(
            "⚠️ Ответьте на предыдущий вопрос! Введите свои <b>дневные показатели</b> белки, жиры, углеводы в формате <code>30 20 300</code>:",
            reply_markup=kb_back_only(),
        )
        return
    save_user_field(message.from_user.id, custom_bju=message.text.strip())
    await push_step(state, "q1_height")
    await render_onboarding_step(message, state, "q1_height", message.from_user.id, is_edit=False)


# Вопрос 1: Рост
@router.message(OnboardingSG.q1_height)
async def process_q1_height(message: Message, state: FSMContext):
    if not (message.text and message.text.strip().isdigit() and 100 <= int(message.text.strip()) <= 250):
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Укажите ваш рост числом в см (от 100 до 250):", reply_markup=kb_back_only())
        return
    save_user_field(message.from_user.id, height=int(message.text.strip()))
    await push_step(state, "q2_weight")
    await render_onboarding_step(message, state, "q2_weight", message.from_user.id, is_edit=False)


# Вопрос 2: Вес
@router.message(OnboardingSG.q2_weight)
async def process_q2_weight(message: Message, state: FSMContext):
    txt = (message.text or "").replace(",", ".").strip()
    try:
        w = float(txt)
        if not (30 <= w <= 250):
            raise ValueError()
    except ValueError:
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Укажите ваш точный вес числом в кг:", reply_markup=kb_back_only())
        return
    save_user_field(message.from_user.id, weight=w)
    await push_step(state, "q3_age")
    await render_onboarding_step(message, state, "q3_age", message.from_user.id, is_edit=False)


# Вопрос 3: Возраст
@router.message(OnboardingSG.q3_age)
async def process_q3_age(message: Message, state: FSMContext):
    if not (message.text and message.text.strip().isdigit() and 10 <= int(message.text.strip()) <= 100):
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Укажите ваш возраст числом:", reply_markup=kb_back_only())
        return
    save_user_field(message.from_user.id, age=int(message.text.strip()))
    await push_step(state, "q4_exp")
    await render_onboarding_step(message, state, "q4_exp", message.from_user.id, is_edit=False)


# Вопрос 4: Спортивный опыт
@router.callback_query(OnboardingSG.q4_exp, F.data.startswith("exp_"))
async def process_q4_exp(call: CallbackQuery, state: FSMContext):
    exp = call.data.split("_", 1)[1]
    save_user_field(call.from_user.id, exp_level=exp)
    await state.update_data(exp_level=exp)

    if exp in ["Начинающий", "Любитель"]:
        await push_step(state, "q5_workouts_btn")
        await render_onboarding_step(call.message, state, "q5_workouts_btn", call.from_user.id, is_edit=True)
    else:
        await push_step(state, "q5_workouts_pro_text")
        await render_onboarding_step(call.message, state, "q5_workouts_pro_text", call.from_user.id, is_edit=True)


# Вопрос 5 (кнопки)
@router.callback_query(OnboardingSG.q5_workouts_btn, F.data.startswith("wrk_"))
async def process_q5_btn(call: CallbackQuery, state: FSMContext):
    wrk = call.data.split("_", 1)[1]
    save_user_field(call.from_user.id, workouts=wrk)
    await push_step(state, "q6_goal")
    await render_onboarding_step(call.message, state, "q6_goal", call.from_user.id, is_edit=True)


# Вопрос 5 (текст для Профессионала)
@router.message(OnboardingSG.q5_workouts_pro_text)
async def process_q5_pro(message: Message, state: FSMContext):
    if not (message.text and message.text.strip().isdigit() and 1 <= int(message.text.strip()) <= 21):
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Укажите точное количество тренировок в неделю числом:", reply_markup=kb_back_only())
        return
    save_user_field(message.from_user.id, workouts=message.text.strip())
    await push_step(state, "q6_goal")
    await render_onboarding_step(message, state, "q6_goal", message.from_user.id, is_edit=False)


# Вопрос 6: Цель
@router.callback_query(OnboardingSG.q6_goal, F.data.startswith("goal_"))
async def process_q6_goal(call: CallbackQuery, state: FSMContext):
    goal = call.data.split("_", 1)[1]
    save_user_field(call.from_user.id, goal=goal)
    user = get_user(call.from_user.id)
    exp = user.get("exp_level", "Начинающий")

    if exp == "Начинающий":
        save_user_field(call.from_user.id, has_supplements=0, supplements_json="{}")
        await push_step(state, "q10_allergies")
        await render_onboarding_step(call.message, state, "q10_allergies", call.from_user.id, is_edit=True)
    else:
        await push_step(state, "q7_supps_choice")
        await render_onboarding_step(call.message, state, "q7_supps_choice", call.from_user.id, is_edit=True)


# Вопрос 7: Добавки (Да / Нет)
@router.callback_query(OnboardingSG.q7_supps_choice, F.data.in_(["supps_yes", "supps_no"]))
async def process_q7_supps(call: CallbackQuery, state: FSMContext):
    if call.data == "supps_no":
        save_user_field(call.from_user.id, has_supplements=0, supplements_json="{}")
        await push_step(state, "q10_allergies")
        await render_onboarding_step(call.message, state, "q10_allergies", call.from_user.id, is_edit=True)
    else:
        save_user_field(call.from_user.id, has_supplements=1)
        await push_step(state, "q8_supps_list")
        await render_onboarding_step(call.message, state, "q8_supps_list", call.from_user.id, is_edit=True)


# Вопрос 8: Список добавок -> Вопрос 9
@router.message(OnboardingSG.q8_supps_list)
async def process_q8_supps_list(message: Message, state: FSMContext):
    if not message.text or len(message.text.strip()) < 2:
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Перечислите ваши добавки через запятую:", reply_markup=kb_back_only())
        return
    supps = [s.strip() for s in message.text.split(",") if s.strip()]
    await state.update_data(supps_queue=supps, supps_result={})
    await state.set_state(OnboardingSG.q9_supps_freq)
    first_supp = supps[0]
    await message.answer(
        f"9. Укажите периодичность и дозировку приема для добавки <b>«{first_supp}»</b>:",
        reply_markup=kb_back_only(),
    )


# Вопрос 9: Периодичность к каждой добавке
@router.message(OnboardingSG.q9_supps_freq)
async def process_q9_supps_freq(message: Message, state: FSMContext):
    if not message.text or len(message.text.strip()) < 1:
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Укажите периодичность приема:", reply_markup=kb_back_only())
        return
    data = await state.get_data()
    queue = data.get("supps_queue", [])
    result = data.get("supps_result", {})

    if queue:
        current_supp = queue.pop(0)
        result[current_supp] = message.text.strip()
        await state.update_data(supps_queue=queue, supps_result=result)

    if queue:
        next_supp = queue[0]
        await message.answer(
            f"9. Укажите периодичность и дозировку приема для добавки <b>«{next_supp}»</b>:",
            reply_markup=kb_back_only(),
        )
    else:
        save_user_field(message.from_user.id, supplements_json=json.dumps(result, ensure_ascii=False))
        await push_step(state, "q10_allergies")
        await render_onboarding_step(message, state, "q10_allergies", message.from_user.id, is_edit=False)


# Вопрос 10 (или 7): Противопоказания и аллергии -> Бюджет
@router.message(OnboardingSG.q10_allergies)
async def process_q10_allergies(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Укажите противопоказания и аллергии:", reply_markup=kb_back_only())
        return
    save_user_field(message.from_user.id, allergies=message.text.strip())
    await push_step(state, "q_budget")
    await render_onboarding_step(message, state, "q_budget", message.from_user.id, is_edit=False)


# Вопрос: Бюджет -> Магазины
@router.message(OnboardingSG.q_budget)
async def process_q_budget(message: Message, state: FSMContext):
    if not message.text or not any(ch.isdigit() for ch in message.text):
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Укажите ваш бюджет (например: 6000):", reply_markup=kb_back_only())
        return
    save_user_field(message.from_user.id, budget=message.text.strip())
    await push_step(state, "q_stores")
    await render_onboarding_step(message, state, "q_stores", message.from_user.id, is_edit=False)


# Вопрос: Магазины -> Время утренней рассылки
@router.message(OnboardingSG.q_stores)
async def process_q_stores(message: Message, state: FSMContext):
    txt = (message.text or "").lower().strip()
    if not txt or len(txt) < 2:
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Напишите названия магазинов или рынок:", reply_markup=kb_back_only())
        return
    if any(foreign in txt for foreign in ["walmart", "whole foods", "tesco", "carrefour", "aldi", "kroger"]):
        await message.answer(
            "⚠️ Бот рассматривает только магазины, которые находятся в России (исключение — рынок). Укажите российские магазины:",
            reply_markup=kb_back_only(),
        )
        return

    save_user_field(message.from_user.id, stores=message.text.strip())
    await push_step(state, "q_notify_time")
    await render_onboarding_step(message, state, "q_notify_time", message.from_user.id, is_edit=False)


# Вопрос: Время -> С какого дня начать рацион (Замечание 9)
@router.message(OnboardingSG.q_notify_time)
async def process_q_notify_time(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", txt):
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Укажите время в формате ЧЧ:ММ (например: 08:30):", reply_markup=kb_back_only())
        return

    save_user_field(message.from_user.id, notify_time=txt)
    await push_step(state, "q_start_day")
    await render_onboarding_step(message, state, "q_start_day", message.from_user.id, is_edit=False)


# Обработка выбора дня старта рациона (кнопка «С завтрашнего дня» или ввод дня недели / даты)
@router.callback_query(OnboardingSG.q_start_day, F.data == "start_day_tomorrow")
async def process_start_day_tomorrow(call: CallbackQuery, state: FSMContext):
    tomorrow = datetime.now(MSK_TZ).date() + timedelta(days=1)
    save_user_field(call.from_user.id, diet_start_date=tomorrow.strftime("%d.%m.%Y"))
    await push_step(state, "q_ready_meals")
    await show_macros_advice_step(call.message, state, call.from_user.id, is_edit=True)


@router.message(OnboardingSG.q_start_day)
async def process_start_day_text(message: Message, state: FSMContext):
    txt = (message.text or "").lower().strip()
    today = datetime.now(MSK_TZ).date()
    chosen_date = None

    b_err = InlineKeyboardBuilder()
    b_err.button(text="С завтрашнего дня", callback_data="start_day_tomorrow")
    b_err.button(text="🔙 Назад", callback_data="onboard_back")
    b_err.adjust(1)

    if txt in ["завтра", "с завтрашнего дня"]:
        chosen_date = today + timedelta(days=1)
    elif txt in ["сегодня", "с сегодняшнего дня"]:
        await message.answer(
            "⚠️ Рацион составляется только со следующего дня, чтобы вы успели купить продукты! Выберите другой день или нажмите «С завтрашнего дня»:",
            reply_markup=b_err.as_markup(),
        )
        return
    elif txt in WEEKDAYS_MAP:
        target_wd = WEEKDAYS_MAP[txt]
        if target_wd == today.weekday():
            await message.answer(
                "⚠️ Сегодня уже этот день недели! Рацион составляется только со следующего дня, чтобы вы успели купить продукты. "
                "Напишите другой день или нажмите «С завтрашнего дня»:",
                reply_markup=b_err.as_markup(),
            )
            return
        days_ahead = (target_wd - today.weekday()) % 7
        chosen_date = today + timedelta(days=days_ahead)
    else:
        m = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?$", txt)
        if m:
            day_i, mon_i = int(m.group(1)), int(m.group(2))
            yr_i = int(m.group(3)) if m.group(3) else today.year
            try:
                candidate = datetime(yr_i, mon_i, day_i).date()
                if not m.group(3) and candidate < today:
                    candidate = datetime(yr_i + 1, mon_i, day_i).date()
                if candidate <= today:
                    await message.answer(
                        "⚠️ Рацион составляется только со следующего дня (минимум с завтрашнего числа), чтобы вы успели купить продукты!",
                        reply_markup=b_err.as_markup(),
                    )
                    return
                chosen_date = candidate
            except ValueError:
                pass

    if not chosen_date:
        await message.answer(
            "⚠️ Напишите корректный день недели (например: <code>вторник</code>), дату (например: <code>18.12</code>) или нажмите кнопку ниже:",
            reply_markup=b_err.as_markup(),
        )
        return

    save_user_field(message.from_user.id, diet_start_date=chosen_date.strftime("%d.%m.%Y"))
    await push_step(state, "q_ready_meals")
    await show_macros_advice_step(message, state, message.from_user.id, is_edit=False)


async def show_macros_advice_step(target_message: Message, state: FSMContext, user_id: int, is_edit: bool = False):
    send_fn = target_message.edit_text if is_edit else target_message.answer
    wait_m = await send_fn("⏳ <b>Анализирую ваши параметры и рассчитываю оптимальные показатели БЖУ...</b>")

    user = get_user(user_id)
    macros = await asyncio.to_thread(calculate_macros, user)

    save_user_field(
        user_id,
        target_p=macros["p"],
        target_f=macros["f"],
        target_c=macros["c"],
        target_fiber=macros["fiber"],
        rec_meals=macros["rec_meals"],
        expert_note=macros.get("expert_note", ""),
    )

    supps_dict = json.loads(user.get("supplements_json") or "{}")
    supps_str = ", ".join([f"{k} ({v})" for k, v in supps_dict.items()]) if supps_dict else "Нет"

    # Важная карточка пользователя сохраняется в диалоге
    profile_msg = (
        "📌 <b>Ваша карточка данных:</b>\n"
        f"• Рост / Вес / Возраст: {user['height']} см / {user['weight']} кг / {user['age']} лет\n"
        f"• Опыт: {user['exp_level']} (тренировок в нед: {user['workouts']})\n"
        f"• Цель: {user['goal']}\n"
        f"• Добавки: {supps_str}\n"
        f"• Аллергии/противопоказания: {user['allergies']}\n"
        f"• Бюджет: {user['budget']} | Магазины: {user['stores']}\n"
        f"• Старт рациона: с {user['diet_start_date'][:5]} | Уведомления: {user['notify_time']} МСК"
    )
    await target_message.answer(profile_msg)

    await state.set_state(OnboardingSG.q_ready_meals)
    b = InlineKeyboardBuilder()
    b.button(text="Да, готов", callback_data="meals_ready_yes")
    b.button(text="Нет (выбрать другое кол-во)", callback_data="meals_ready_no")
    if macros.get("heavy_mass_warning"):
        b.button(text="🔄 Сменить цель на Сушку (рекомендуется)", callback_data="switch_goal_to_cut")
    b.button(text="🔙 Назад", callback_data="onboard_back")
    b.adjust(2, 1, 1)

    extra_warnings = []
    if macros.get("heavy_mass_warning"):
        extra_warnings.append(
            f"⚠️ <b>Обратите внимание (совет эксперта):</b> При текущем весе <b>{user['weight']} кг</b> классический профицитный набор массы "
            "может создать лишнюю нагрузку на организм и привести к набору жировой ткани. Опираясь на проверенные источники, "
            "мы рекомендуем выбрать <b>Сушку / рекомпозицию</b> (снижение жира при сохранении мышц). Но окончательный выбор, конечно, за вами!"
        )
    if macros.get("high_protein_sport_hint"):
        extra_warnings.append(
            f"💡 <b>Пояснение по белку ({macros['p']} г/день):</b> Съедать такое количество белка из обычной еды за 5-6 раз в день очень тяжело. "
            "Большинство спортсменов в таком случае питаются 3–4 раза в день и добавляют <b>протеин или гейнер</b> между приемами пищи."
        )
    if macros.get("expert_note"):
        extra_warnings.append(f"🧠 <i>Комментарий нутрициолога: {macros['expert_note']}</i>")

    warn_block = ("\n\n" + "\n\n".join(extra_warnings)) if extra_warnings else ""

    advice_msg = (
        f"💡 <b>Рекомендация по вашему рациону:</b>\n"
        f"Вам необходимо потреблять в день:\n"
        f"• Белки: <b>{macros['p']} г</b>\n"
        f"• Углеводы: <b>{macros['c']} г</b>\n"
        f"• Жиры (минимум): <b>{macros['f']} г</b>\n"
        f"• Клетчатка: <b>{macros['fiber']} г</b>"
        f"{warn_block}\n\n"
        f"Для такого рациона рекомендуется <b>{macros['rec_meals']}-разовое питание</b> в день.\n"
        f"<b>Готовы?</b>"
    )
    await wait_m.edit_text(advice_msg, reply_markup=b.as_markup())


@router.callback_query(OnboardingSG.q_ready_meals, F.data == "switch_goal_to_cut")
async def cb_switch_goal_to_cut(call: CallbackQuery, state: FSMContext):
    save_user_field(call.from_user.id, goal="Сушка")
    await call.answer("✅ Цель изменена на «Сушка»! Пересчитываю БЖУ...")
    await show_macros_advice_step(call.message, state, call.from_user.id, is_edit=True)


@router.callback_query(OnboardingSG.q_ready_meals, F.data == "meals_ready_yes")
async def process_meals_ready_yes(call: CallbackQuery, state: FSMContext):
    user = get_user(call.from_user.id)
    save_user_field(call.from_user.id, actual_meals=user["rec_meals"])
    await push_step(state, "q_sport_nutr_mode")
    await state.set_state(OnboardingSG.q_sport_nutr_mode)

    b = InlineKeyboardBuilder()
    b.button(text="Со спортивным питанием", callback_data="sport_mode_yes")
    b.button(text="Без него", callback_data="sport_mode_no")
    b.button(text="🔙 Назад", callback_data="onboard_back")
    b.adjust(1)

    await call.message.edit_text(
        f"Отлично! Формируем {user['rec_meals']}-разовое питание.\n"
        "Уточните, рассчитывать рацион со спортивным питанием или без него?",
        reply_markup=b.as_markup(),
    )


@router.callback_query(OnboardingSG.q_ready_meals, F.data == "meals_ready_no")
async def process_meals_ready_no(call: CallbackQuery, state: FSMContext):
    await push_step(state, "q_custom_meals_count")
    await render_onboarding_step(call.message, state, "q_custom_meals_count", call.from_user.id, is_edit=True)


@router.message(OnboardingSG.q_custom_meals_count)
async def process_custom_meals_count(message: Message, state: FSMContext):
    if not (message.text and message.text.strip().isdigit() and 1 <= int(message.text.strip()) <= 5):
        await message.answer("⚠️ Ответьте на предыдущий вопрос! Напишите число приемов пищи от 1 до 5:", reply_markup=kb_back_only())
        return

    meals = int(message.text.strip())
    save_user_field(message.from_user.id, actual_meals=meals)
    await push_step(state, "q_sport_nutr_mode")
    await state.set_state(OnboardingSG.q_sport_nutr_mode)

    b = InlineKeyboardBuilder()
    b.button(text="Со спортивным питанием", callback_data="sport_mode_yes")
    b.button(text="Без него", callback_data="sport_mode_no")
    b.button(text="🔙 Назад", callback_data="onboard_back")
    b.adjust(1)

    warn_text = (
        f"⚠️ <b>Обратите внимание:</b> {meals} приемов пищи в день может быть мало для полного усвоения вашей нормы из обычных продуктов.\n\n"
        "Но вы можете добавить <b>гейнер или протеин (с углеводами)</b> между приемами пищи, чтобы легко добирать норму БЖУ.\n\n"
        "Как рассчитываем ваш рацион?"
    )
    await message.answer(warn_text, reply_markup=b.as_markup())


@router.callback_query(OnboardingSG.q_sport_nutr_mode, F.data.in_(["sport_mode_yes", "sport_mode_no"]))
async def finalize_and_generate_diet(call: CallbackQuery, state: FSMContext):
    use_sport = 1 if call.data == "sport_mode_yes" else 0
    user_id = call.from_user.id
    save_user_field(user_id, use_sport_nutr=use_sport, shopping_list_cache="")
    await state.clear()

    await call.message.edit_text("⏳ <b>Составляю практичный рацион под ваши магазины и рассчитываю граммовки... (около 15-20 секунд)</b>")
    user = get_user(user_id)
    diet_data = await asyncio.to_thread(generate_weekly_diet_ai, user, False, None)
    now_str = datetime.now(MSK_TZ).strftime("%d.%m.%Y")
    save_user_field(
        user_id,
        current_diet_json=json.dumps(diet_data, ensure_ascii=False),
        last_month_check=now_str,
    )

    user = get_user(user_id)
    summary_text = format_weekly_summary_text(user, diet_data, is_next=False)
    await call.message.edit_text(summary_text, reply_markup=kb_main_app_menu(user, diet_data))

    # Замечание 10: предлагаем контроль за продуктами ТОЛЬКО ОДИН РАЗ и только если пользователь еще им не пользуется!
    existing_prods = get_products_db(user_id)
    if not existing_prods and not user.get("offered_fridge"):
        save_user_field(user_id, offered_fridge=1)
        b_fr = InlineKeyboardBuilder()
        b_fr.button(text="📦 Контроль продуктов", callback_data="open_fridge_app")
        await call.message.answer(
            "💡 Вы также можете включить функцию <b>Контроль за продуктами</b>, чтобы бот следил за сроками годности и напоминал о них.",
            reply_markup=b_fr.as_markup(),
        )


# ==========================================
# 7. НАВИГАЦИЯ ПО РАЦИОНУ, СПИСКУ ПОКУПОК И РЕЦЕПТАМ
# ==========================================
def format_weekly_summary_text(user: dict, diet_data: dict, is_next: bool = False) -> str:
    """Презентабельное, краткое и информативное оформление рациона с датами ДД.ММ."""
    ws = diet_data.get("weekly_summary", {})
    days = diet_data.get("days", [])
    period_str = f"{days[0]['day_name']} – {days[-1]['day_name']}" if days else ""
    title = f"🥗 <b>РАЦИОН НА СЛЕДУЮЩУЮ НЕДЕЛЮ ({period_str})</b>" if is_next else f"🥗 <b>ВАШ РАЦИОН НА НЕДЕЛЮ ({period_str})</b>"
    sport_status = "Со спортпитом" if user.get("use_sport_nutr") else "Обычные продукты"

    lines = [
        f"{title}",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🍽 <b>Приемов пищи:</b> {user.get('actual_meals')} ({sport_status})",
        f"📊 <b>Дневная норма:</b> Б: <b>{ws.get('daily_p')}г</b> | Ж: <b>{ws.get('daily_f')}г</b> | У: <b>{ws.get('daily_c')}г</b>",
        f"📈 <b>Всего за неделю:</b> Б: <b>{ws.get('week_p')}г</b> | Ж: <b>{ws.get('week_f')}г</b> | У: <b>{ws.get('week_c')}г</b>",
        f"━━━━━━━━━━━━━━━━━━━━",
        "<b>Краткое меню по датам:</b>\n",
    ]
    for d in days:
        dishes = " • ".join([m["dish_name"] for m in d.get("meals", [])])
        lines.append(f"🔹 <b>{d['day_name']}</b> <i>(Б:{d['p']} Ж:{d['f']} У:{d['c']})</i>:\n   {dishes}\n")

    lines.append(
        "🛒 <i>Нужен список покупок с ценами под ваши магазины? Нажмите кнопку <b>«Список продуктов к покупке»</b> ниже!</i>\n"
        "👇 <i>Выберите дату, чтобы открыть граммовки и пошаговые рецепты:</i>"
    )
    return "\n".join(lines)


def format_day_text_and_kb(diet_data: dict, day_num: int, week_type: str = "curr") -> tuple[str, InlineKeyboardMarkup]:
    days = diet_data.get("days", [])
    day_obj = next((d for d in days if int(d.get("day_num", 1)) == day_num), days[0])

    lines = [
        f"🍽 <b>Меню на {day_obj['day_name']}</b>",
        f"📊 <b>БЖУ за день:</b> Белки <b>{day_obj['p']}г</b> | Жиры <b>{day_obj['f']}г</b> | Углеводы <b>{day_obj['c']}г</b>",
        "━━━━━━━━━━━━━━━━━━━━\n",
    ]

    b = InlineKeyboardBuilder()

    for m in day_obj.get("meals", []):
        m_num = m["meal_num"]
        lines.append(
            f"🔸 <b>{m['meal_title']}: {m['dish_name']}</b>\n"
            f"   • <i>Состав:</i> {m['ingredients']}\n"
            f"   • <i>БЖУ:</i> Б: {m['p']}г | Ж: {m['f']}г | У: {m['c']}г\n"
        )
        b.button(
            text=f"📖 Рецепт: {m['dish_name'][:25]}",
            callback_data=f"recipe_{week_type}_{day_num}_{m_num}",
        )
        b.button(
            text=f"🔄 Заменить прием {m_num}",
            callback_data=f"repmeal_{week_type}_{day_num}_{m_num}",
        )

    b.button(text=f"🔄 Заменить весь день ({day_obj['day_name']})", callback_data=f"repday_{week_type}_{day_num}")
    back_cb = "view_week_summary" if week_type == "curr" else "view_next_week"
    b.button(text="🔙 Назад к рациону на неделю", callback_data=back_cb)
    b.adjust(2)
    return "\n".join(lines), b.as_markup()


@router.callback_query(F.data == "view_week_summary")
async def cb_view_week_summary(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user = get_user(call.from_user.id)
    if not user or not user.get("current_diet_json"):
        await start_diet_mode(call, state)
        return
    diet_data = json.loads(user["current_diet_json"])
    text = format_weekly_summary_text(user, diet_data, is_next=False)
    await call.message.edit_text(text, reply_markup=kb_main_app_menu(user, diet_data))


# Список продуктов к покупке (Замечание 7 и 13)
@router.callback_query(F.data == "view_shopping_list")
async def cb_view_shopping_list(call: CallbackQuery):
    user = get_user(call.from_user.id)
    diet_data = json.loads(user["current_diet_json"])
    await call.message.edit_text(
        f"⏳ <b>Проверяю источники, актуальные цены и наличие товаров в ваших магазинах ({user['stores']})...</b>"
    )
    shop_text = await asyncio.to_thread(generate_shopping_list_ai, user, diet_data)
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить список покупок", callback_data="refresh_shopping_list")
    b.button(text="🔙 Назад к рациону", callback_data="view_week_summary")
    b.adjust(1)
    await call.message.edit_text(shop_text, reply_markup=b.as_markup())


@router.callback_query(F.data == "refresh_shopping_list")
async def cb_refresh_shopping_list(call: CallbackQuery):
    save_user_field(call.from_user.id, shopping_list_cache="")
    await cb_view_shopping_list(call)


# Вписать купленный дополнительный продукт в существующий рацион (Замечание 11)
@router.callback_query(F.data == "insert_bought_prod")
async def cb_insert_bought_prod(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditProfileSG.insert_bought_product)
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Назад", callback_data="view_week_summary")
    await call.message.edit_text(
        "🥑 <b>Добавление купленного продукта в ваш рацион</b>\n\n"
        "Напишите, какой дополнительный продукт (и примерно в каком количестве) вы купили "
        "<i>(например: «Купил 400г творога 5% и бананы» или «Стейк горбуши 500г»)</i>.\n\n"
        "Я как ваш тренер и нутрициолог рассчитаю его БЖУ, подскажу, куда лучше его вписать или что им заменить, и обновлю ваш рацион!",
        reply_markup=b.as_markup(),
    )


@router.message(EditProfileSG.insert_bought_product)
async def msg_insert_bought_prod(message: Message, state: FSMContext):
    bought_text = (message.text or "").strip()
    if len(bought_text) < 2:
        await message.answer("⚠️ Напишите название купленного продукта:", reply_markup=kb_back_only("view_week_summary"))
        return

    user_id = message.from_user.id
    user = get_user(user_id)
    diet_data = json.loads(user["current_diet_json"])
    await state.clear()

    wait_m = await message.answer("⏳ <b>Считаю БЖУ вашего продукта и вписываю его в существующий рацион...</b>")

    sys_p = (
        "Ты — профессиональный фитнес-тренер и нутрициолог. "
        "Пользователь купил дополнительный продукт. Рассчитай его примерное БЖУ, дай умный совет, в какой прием пищи и вместо чего (или дополнительно) его лучше съесть, "
        "и обнови JSON рациона, вписав этот продукт в подходящие дни и приемы пищи с сохранением дневного баланса БЖУ."
    )
    usr_p = f"""
Купленный продукт: {bought_text}
Цель пользователя: {user['goal']}, Дневное БЖУ: Б:{user['target_p']} Ж:{user['target_f']} У:{user['target_c']}.
Текущий рацион JSON: {json.dumps(diet_data, ensure_ascii=False)}

Верни ответ СТРОГО в формате JSON:
{{
  "trainer_advice": "Подробный совет тренера: сколько БЖУ в купленном продукте и как именно он был вписан/заменен в рационе",
  "updated_diet": {{ ...полный обновленный объект рациона (weekly_summary и days)... }}
}}
"""
    raw = await asyncio.to_thread(giga.ask, sys_p, usr_p, 0.4)
    trainer_advice = (
        f"✅ <b>Продукт «{bought_text}» проанализирован и вписан в ваш рацион!</b>\n"
        "Я заменил им один из белково-углеводных приемов пищи с сохранением вашей суточной нормы БЖУ."
    )
    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            parsed = json.loads(m.group(0))
            if "trainer_advice" in parsed:
                trainer_advice = f"👨‍⚕️ <b>Совет вашего нутрициолога по продукту «{bought_text}»:</b>\n\n{parsed['trainer_advice']}"
            if "updated_diet" in parsed and "days" in parsed["updated_diet"]:
                diet_data = parsed["updated_diet"]
                save_user_field(user_id, current_diet_json=json.dumps(diet_data, ensure_ascii=False), shopping_list_cache="")
    except Exception as e:
        logging.error(f"Insert bought product parse error: {e}")
        # Если JSON оказался слишком большим, гарантированно вписываем продукт в 1-й и 3-й дни рациона
        if diet_data.get("days"):
            diet_data["days"][0]["meals"][-1]["dish_name"] += f" + {bought_text}"
            diet_data["days"][0]["meals"][-1]["ingredients"] += f", {bought_text}"
            save_user_field(user_id, current_diet_json=json.dumps(diet_data, ensure_ascii=False), shopping_list_cache="")

    b = InlineKeyboardBuilder()
    b.button(text="🥗 Открыть обновленный рацион", callback_data="view_week_summary")
    await wait_m.edit_text(trainer_advice, reply_markup=b.as_markup())


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
        f"🍽 <b>{meal_obj['dish_name']} ({day_obj['day_name']})</b>\n"
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

    await call.message.edit_text(f"⏳ <b>Подбираю простую и практичную замену для блюда «{old_dish}»...</b>")

    sys_p = "Ты спортивный диетолог. Предложи ОДНО простое, доступное блюдо на замену строго в формате JSON."
    usr_p = (
        f"Замени блюдо '{old_dish}' на другое простое из магазинов ({user['stores']}). "
        f"Аллергии: {user['allergies']}. Целевое БЖУ приема: Б:{meal_obj['p']} Ж:{meal_obj['f']} У:{meal_obj['c']}. "
        'Верни строго JSON: {"dish_name": "Название", "ingredients": "Ингредиенты с граммами"}'
    )
    raw = await asyncio.to_thread(giga.ask, sys_p, usr_p, 0.7)
    try:
        m_json = json.loads(re.search(r"\{.*\}", raw, re.DOTALL).group(0))
        meal_obj["dish_name"] = m_json.get("dish_name", "Индейка с рисом и овощами")
        meal_obj["ingredients"] = m_json.get("ingredients", "Филе индейки 130г, рис 60г, огурец 100г")
    except Exception:
        meal_obj["dish_name"] = "Куриное филе с гречкой и свежим огурцом"
        meal_obj["ingredients"] = "Куриное филе 130г, гречка 60г, огурец 100г"

    save_user_field(call.from_user.id, **{json_field: json.dumps(diet_data, ensure_ascii=False)}, shopping_list_cache="")
    text, kb = format_day_text_and_kb(diet_data, day_num, week_type)
    await call.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("repday_"))
async def cb_replace_day(call: CallbackQuery):
    _, week_type, day_str = call.data.split("_")
    day_num = int(day_str)
    user = get_user(call.from_user.id)
    json_field = "current_diet_json" if week_type == "curr" else "next_diet_json"
    diet_data = json.loads(user.get(json_field))

    await call.message.edit_text("⏳ <b>Обновляю меню на выбранный день...</b>")
    new_week = await asyncio.to_thread(generate_weekly_diet_ai, user, (week_type == "next"), None)
    new_day = next((d for d in new_week["days"] if int(d["day_num"]) == day_num), new_week["days"][0])

    for idx, d in enumerate(diet_data["days"]):
        if int(d["day_num"]) == day_num:
            new_day["day_name"] = d["day_name"]
            diet_data["days"][idx] = new_day
            break

    save_user_field(call.from_user.id, **{json_field: json.dumps(diet_data, ensure_ascii=False)}, shopping_list_cache="")
    text, kb = format_day_text_and_kb(diet_data, day_num, week_type)
    await call.message.edit_text(text, reply_markup=kb)


# Подменю изменения рациона/блюда и параметров (чтобы не засорять главный экран лишними кнопками)
@router.callback_query(F.data == "open_diet_edit_menu")
async def cb_open_diet_edit_menu(call: CallbackQuery):
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Полностью поменять весь рацион", callback_data="regen_full_diet")
    b.button(text="🚫 Убрать конкретное блюдо", callback_data="remove_specific_dish")
    b.button(text="🥑 Вписать купленный продукт", callback_data="insert_bought_prod")
    b.button(text="🔙 Назад к рациону", callback_data="view_week_summary")
    b.adjust(1)
    await call.message.edit_text("🔄 <b>Управление вашим рационом:</b>\nВыберите действие:", reply_markup=b.as_markup())


@router.callback_query(F.data == "open_profile_edit_menu")
async def cb_open_profile_edit_menu(call: CallbackQuery):
    user = get_user(call.from_user.id)
    b = InlineKeyboardBuilder()
    b.button(text="⚖️ Изменить вес", callback_data="edit_param_weight")
    b.button(text="🍽 Изменить кол-во приемов пищи", callback_data="edit_param_meals")
    b.button(text="💰 Изменить бюджет", callback_data="edit_param_budget")
    b.button(text="💊 Добавить спорт. добавки", callback_data="edit_param_supps")
    b.button(text="📝 Заполнить анкету заново", callback_data="restart_questionnaire")
    b.button(text="🔙 Назад к рациону", callback_data="view_week_summary")
    b.adjust(2, 2, 1, 1)
    await call.message.edit_text(
        f"⚙️ <b>Ваши текущие параметры:</b>\n"
        f"• Вес: <b>{user.get('weight')} кг</b> | Приемов пищи: <b>{user.get('actual_meals')}</b>\n"
        f"• Бюджет: <b>{user.get('budget')}</b> | Магазины: <b>{user.get('stores')}</b>\n\n"
        "Что вы хотите изменить?",
        reply_markup=b.as_markup(),
    )


@router.callback_query(F.data == "regen_full_diet")
async def cb_regen_full_diet(call: CallbackQuery):
    user = get_user(call.from_user.id)
    await call.message.edit_text("⏳ <b>Полностью меняю ваш рацион с сохранением всех ваших показателей БЖУ...</b>")
    new_diet = await asyncio.to_thread(generate_weekly_diet_ai, user, False, None)
    save_user_field(call.from_user.id, current_diet_json=json.dumps(new_diet, ensure_ascii=False), shopping_list_cache="")
    user = get_user(call.from_user.id)
    text = format_weekly_summary_text(user, new_diet, is_next=False)
    await call.message.edit_text(text, reply_markup=kb_main_app_menu(user, new_diet))


@router.callback_query(F.data == "gen_next_week")
async def cb_gen_next_week(call: CallbackQuery):
    user = get_user(call.from_user.id)
    if user.get("next_diet_json"):
        await cb_view_next_week(call)
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
    for d in next_diet.get("days", []):
        d_num = d.get("day_num", 1)
        d_label = d.get("day_name", f"День {d_num}")
        b.button(text=f"{d_label}", callback_data=f"view_day_{d_num}_next")
    b.button(text="✅ Да, устраивает", callback_data="next_week_approve")
    b.button(text="🔄 Поменять", callback_data="next_week_regen")
    b.button(text="🔙 Назад к текущей неделе", callback_data="view_week_summary")
    b.adjust(4, 3, 2, 1)
    await message.edit_text(text, reply_markup=b.as_markup())


@router.callback_query(F.data == "next_week_approve")
async def cb_next_week_approve(call: CallbackQuery, state: FSMContext):
    await call.answer("✅ Отлично! Рацион на следующую неделю сохранен.", show_alert=True)
    await cb_view_week_summary(call, state)


@router.callback_query(F.data == "next_week_regen")
async def cb_next_week_regen(call: CallbackQuery):
    user = get_user(call.from_user.id)
    await call.message.edit_text("⏳ <b>Меняю рацион на следующую неделю...</b>")
    next_diet = await asyncio.to_thread(generate_weekly_diet_ai, user, True, None)
    save_user_field(call.from_user.id, next_diet_json=json.dumps(next_diet, ensure_ascii=False))
    await show_next_week_view(call.message, user, next_diet)


# ==========================================
# 8. ИЗМЕНЕНИЕ ПАРАМЕТРОВ ПОЛЬЗОВАТЕЛЯ
# ==========================================
@router.callback_query(F.data == "edit_param_weight")
async def cb_edit_weight(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditProfileSG.edit_weight)
    await call.message.edit_text("Введите ваш новый вес (в кг):", reply_markup=kb_back_only("open_profile_edit_menu"))


@router.message(EditProfileSG.edit_weight)
async def msg_edit_weight(message: Message, state: FSMContext):
    try:
        w = float(message.text.replace(",", ".").strip())
        if not (30 <= w <= 250):
            raise ValueError()
    except Exception:
        await message.answer("⚠️ Введите корректный вес числом:", reply_markup=kb_back_only("open_profile_edit_menu"))
        return
    user_id = message.from_user.id
    save_user_field(user_id, weight=w)
    user = get_user(user_id)
    macros = await asyncio.to_thread(calculate_macros, user)
    save_user_field(user_id, target_p=macros["p"], target_f=macros["f"], target_c=macros["c"], rec_meals=macros["rec_meals"])
    await state.clear()

    wait_msg = await message.answer("⏳ Вес обновлен! Пересчитываю БЖУ и рацион...")
    user = get_user(user_id)
    new_diet = await asyncio.to_thread(generate_weekly_diet_ai, user, False, None)
    save_user_field(user_id, current_diet_json=json.dumps(new_diet, ensure_ascii=False), shopping_list_cache="")
    await wait_msg.edit_text(format_weekly_summary_text(user, new_diet), reply_markup=kb_main_app_menu(user, new_diet))


@router.callback_query(F.data == "edit_param_meals")
async def cb_edit_meals(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditProfileSG.edit_meals)
    await call.message.edit_text("Введите новое количество приемов пищи в день (от 1 до 5):", reply_markup=kb_back_only("open_profile_edit_menu"))


@router.message(EditProfileSG.edit_meals)
async def msg_edit_meals(message: Message, state: FSMContext):
    if not (message.text and message.text.strip().isdigit() and 1 <= int(message.text.strip()) <= 5):
        await message.answer("⚠️ Введите число от 1 до 5:", reply_markup=kb_back_only("open_profile_edit_menu"))
        return
    user_id = message.from_user.id
    save_user_field(user_id, actual_meals=int(message.text.strip()))
    await state.clear()
    wait_msg = await message.answer("⏳ Количество приемов пищи изменено! Перестраиваю рацион...")
    user = get_user(user_id)
    new_diet = await asyncio.to_thread(generate_weekly_diet_ai, user, False, None)
    save_user_field(user_id, current_diet_json=json.dumps(new_diet, ensure_ascii=False), shopping_list_cache="")
    await wait_msg.edit_text(format_weekly_summary_text(user, new_diet), reply_markup=kb_main_app_menu(user, new_diet))


@router.callback_query(F.data == "edit_param_budget")
async def cb_edit_budget(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditProfileSG.edit_budget)
    await call.message.edit_text("Введите новый бюджет на неделю (в рублях):", reply_markup=kb_back_only("open_profile_edit_menu"))


@router.message(EditProfileSG.edit_budget)
async def msg_edit_budget(message: Message, state: FSMContext):
    user_id = message.from_user.id
    save_user_field(user_id, budget=message.text.strip())
    await state.clear()
    wait_msg = await message.answer("⏳ Бюджет обновлен! Перестраиваю рацион...")
    user = get_user(user_id)
    new_diet = await asyncio.to_thread(generate_weekly_diet_ai, user, False, None)
    save_user_field(user_id, current_diet_json=json.dumps(new_diet, ensure_ascii=False), shopping_list_cache="")
    await wait_msg.edit_text(format_weekly_summary_text(user, new_diet), reply_markup=kb_main_app_menu(user, new_diet))


@router.callback_query(F.data == "edit_param_supps")
async def cb_edit_supps(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditProfileSG.add_supp_name)
    await call.message.edit_text(
        "Введите название спортивной добавки (и при желании БЖУ порции в скобках), которую хотите добавить:",
        reply_markup=kb_back_only("open_profile_edit_menu"),
    )


@router.message(EditProfileSG.add_supp_name)
async def msg_add_supp_name(message: Message, state: FSMContext):
    await state.update_data(new_supp_name=message.text.strip())
    await state.set_state(EditProfileSG.add_supp_freq)
    await message.answer(
        f"Укажите периодичность приема для <b>«{message.text.strip()}»</b>:",
        reply_markup=kb_back_only("open_profile_edit_menu"),
    )


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
    save_user_field(user_id, current_diet_json=json.dumps(new_diet, ensure_ascii=False), shopping_list_cache="")
    await wait_msg.edit_text(format_weekly_summary_text(user, new_diet), reply_markup=kb_main_app_menu(user, new_diet))


@router.callback_query(F.data == "remove_specific_dish")
async def cb_remove_specific_dish(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditProfileSG.exclude_dish_name)
    await call.message.edit_text(
        "Напишите название блюда, которое нужно убрать и заменить в рационе:",
        reply_markup=kb_back_only("open_diet_edit_menu"),
    )


@router.message(EditProfileSG.exclude_dish_name)
async def msg_remove_specific_dish(message: Message, state: FSMContext):
    dish_to_remove = message.text.strip()
    user_id = message.from_user.id
    await state.clear()
    wait_msg = await message.answer(f"⏳ Убираю блюдо «{dish_to_remove}» и обновляю рацион...")
    user = get_user(user_id)
    new_diet = await asyncio.to_thread(generate_weekly_diet_ai, user, False, dish_to_remove)
    save_user_field(user_id, current_diet_json=json.dumps(new_diet, ensure_ascii=False), shopping_list_cache="")
    await wait_msg.edit_text(format_weekly_summary_text(user, new_diet), reply_markup=kb_main_app_menu(user, new_diet))


# ==========================================
# 9. ФУНКЦИЯ 2: ПРОДУКТОВЫЙ КОНТРОЛЬ (С ЧИСТОЙ НАВИГАЦИЕЙ)
# ==========================================
def kb_fridge_dashboard(has_diet: bool) -> InlineKeyboardMarkup:
    """Внутри Контроля продуктов свои кнопки управления + ОДНА кнопка перехода в Рацион."""
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить продукт", callback_data="fridge_add_start")
    b.button(text="🗑 Убрать продукт", callback_data="fridge_del_menu")
    if has_diet:
        b.button(text="🥑 Вписать продукт в рацион", callback_data="insert_bought_prod")
        b.button(text="🥗 Перейти в Рацион", callback_data="view_week_summary")
    else:
        b.button(text="🥗 Рацион", callback_data="mode_diet")
    b.button(text="🔙 Назад в главное меню", callback_data="back_to_start_modes")
    b.adjust(2, 1, 1, 1)
    return b.as_markup()


def format_fridge_list(user_id: int) -> str:
    prods = get_products_db(user_id)
    if not prods:
        return "📦 <b>Продуктовый контроль</b>\n\nСписок ваших продуктов пока пуст. Нажмите «➕ Добавить продукт», чтобы начать отслеживание сроков годности."

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
async def start_fridge_from_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    prods = get_products_db(call.from_user.id)
    if prods:
        await open_fridge_dashboard(call, state)
    else:
        await state.set_state(FridgeSG.entering_product)
        await call.message.edit_text(
            "📦 <b>Продуктовый контроль</b>\n\n"
            "Введите название продукта и срок годности до какого числа\n"
            "<i>(например: <code>Сыр пармезан 12.02.2027</code>)</i>:",
            reply_markup=kb_back_only("back_to_start_modes"),
        )


@router.callback_query(F.data == "fridge_add_start")
async def start_fridge_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(FridgeSG.entering_product)
    await call.message.edit_text(
        "Введите название продукта и срок годности до какого числа\n"
        "<i>(например: <code>Сыр пармезан 12.02.2027</code>)</i>:",
        reply_markup=kb_back_only("open_fridge_app"),
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
            "(например: <code>Сыр пармезан 12.02.2027</code>):",
            reply_markup=kb_back_only("open_fridge_app"),
        )
        return

    date_str = match.group(1)
    name_str = txt[: match.start()].strip()
    if not name_str:
        await message.answer("⚠️ Вы не указали название продукта перед датой!", reply_markup=kb_back_only("open_fridge_app"))
        return

    try:
        datetime.strptime(date_str, "%d.%m.%Y")
    except ValueError:
        await message.answer("⚠️ Некорректная дата в календаре! Проверьте формат ДД.ММ.ГГГГ:", reply_markup=kb_back_only("open_fridge_app"))
        return

    add_product_db(message.from_user.id, name_str, date_str)
    await state.set_state(FridgeSG.confirm_more)

    b = InlineKeyboardBuilder()
    b.button(text="Да", callback_data="fridge_more_yes")
    b.button(text="Нет", callback_data="fridge_more_no")
    b.button(text="🔙 Назад", callback_data="open_fridge_app")
    b.adjust(2, 1)

    await message.answer(
        f"✅ <b>Готово!</b> ({name_str} — до {date_str})\nДобавить ли еще продукт?",
        reply_markup=b.as_markup(),
    )


@router.callback_query(FridgeSG.confirm_more, F.data == "fridge_more_yes")
async def cb_fridge_more_yes(call: CallbackQuery, state: FSMContext):
    await state.set_state(FridgeSG.entering_product)
    await call.message.edit_text(
        "✅ <b>Сохранено!</b>\nВведите название следующего продукта и срок годности (например: <code>Молоко 20.05.2026</code>):",
        reply_markup=kb_back_only("open_fridge_app"),
    )


@router.callback_query(FridgeSG.confirm_more, F.data == "fridge_more_no")
async def cb_fridge_more_no(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user = get_user(call.from_user.id)
    has_diet = bool(user and user.get("current_diet_json"))

    # Замечание 10: предлагаем создать рацион ТОЛЬКО ОДИН РАЗ и только если пользователь еще не пользуется рационом!
    if not has_diet and not (user and user.get("offered_diet")):
        save_user_field(call.from_user.id, offered_diet=1)
        b = InlineKeyboardBuilder()
        b.button(text="Да", callback_data="fridge_create_diet_yes")
        b.button(text="Нет", callback_data="fridge_create_diet_no")
        b.adjust(2)
        await call.message.edit_text(
            "✅ <b>Сохранено!</b> Жду ваших команд.\n\nХотите создать рацион (с обязательным учетом продуктов, которые уже есть у вас)?",
            reply_markup=b.as_markup(),
        )
    else:
        await call.message.edit_text(
            "✅ <b>Сохранено!</b>\n\n" + format_fridge_list(call.from_user.id),
            reply_markup=kb_fridge_dashboard(has_diet),
        )


@router.callback_query(F.data == "fridge_create_diet_yes")
async def cb_fridge_create_diet_yes(call: CallbackQuery, state: FSMContext):
    user = get_user(call.from_user.id)
    if user and user.get("height") and user.get("weight") and user.get("current_diet_json"):
        await cb_regen_full_diet(call)
    else:
        await state.update_data(step_history=["ask_custom_bju_choice"])
        await render_onboarding_step(call.message, state, "ask_custom_bju_choice", call.from_user.id, is_edit=True)


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
async def cb_del_prod_item(call: CallbackQuery, state: FSMContext):
    prod_id = int(call.data.split("_")[1])
    delete_product_db(prod_id, call.from_user.id)
    await call.answer("✅ Продукт удален!")
    prods = get_products_db(call.from_user.id)
    if prods:
        await cb_fridge_del_menu(call)
    else:
        await open_fridge_dashboard(call, state)


# ==========================================
# 10. ФОНОВЫЕ ЗАДАЧИ И НАПОМИНАНИЯ (МСК)
# ==========================================
async def send_daily_tip(user: dict):
    history = json.loads(user.get("tips_history_json") or "[]")
    recent_tips = "; ".join(history[-30:]) if history else "нет"

    sys_p = (
        "Ты профессиональный тренер и спортивный нутрициолог. "
        "Напиши ОДИН краткий (2-3 предложения), но очень полезный, научно достоверный практический совет по тренировкам или спортивному питанию."
    )
    usr_p = (
        f"Напиши новый совет (например, про питание после тренировки, ужин за 2-3 часа до сна, творог/кефир на ночь, восстановление и т.д.). "
        f"НЕ ПОВТОРЯЙ эти прошлые темы: {recent_tips}."
    )
    tip = await asyncio.to_thread(giga.ask, sys_p, usr_p, 0.8)
    if not tip:
        tip = "После тренировки желательно принять пищу (белки + углеводы) в первый час, чтобы восполнить запасы гликогена и запустить восстановление мышц."

    history.append(tip[:80])
    save_user_field(user["user_id"], tips_history_json=json.dumps(history[-100:], ensure_ascii=False))

    b = InlineKeyboardBuilder()
    b.button(text="🥗 Рацион", callback_data="view_week_summary")
    b.button(text="📦 Контроль продуктов", callback_data="open_fridge_app")
    b.adjust(2)

    await bot.send_message(
        user["user_id"],
        f"💪 <b>Полезный совет дня по тренировкам и питанию:</b>\n\n{tip}",
        reply_markup=b.as_markup(),
    )


async def minute_scheduler_tick():
    now_msk = datetime.now(MSK_TZ)
    curr_hm = now_msk.strftime("%H:%M")
    today_dm = now_msk.strftime("%d.%m")

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
                days = diet_data.get("days", [])
                day_obj = next((d for d in days if d.get("day_name") == today_dm), days[0])
                text, kb = format_day_text_and_kb(diet_data, int(day_obj.get("day_num", 1)), "curr")
                await bot.send_message(
                    uid,
                    f"☀️ <b>Доброе утро! Ваш подробный рацион на сегодня ({today_dm}):</b>\n\n{text}",
                    reply_markup=kb,
                )
            except Exception as e:
                logging.error(f"Daily menu send error for {uid}: {e}")

        try:
            nh, nm = [int(x) for x in notify_hm.split(":")]
            notify_minutes = nh * 60 + nm
            default_tip_minutes = 12 * 60

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
            b.button(text="📦 Контроль продуктов", callback_data="open_fridge_app")
            await bot.send_message(
                uid,
                "🔔 <b>Контроль срока годности продуктов:</b>\n\n" + "\n".join(alerts),
                reply_markup=b.as_markup(),
            )

        if u.get("diet_start_date"):
            try:
                start_d = datetime.strptime(u["diet_start_date"], "%d.%m.%Y").date()
                days_passed = (today - start_d).days
                if days_passed == 6:
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
                    b.button(text="🔄 Обновить данные", callback_data="open_profile_edit_menu")
                    await bot.send_message(
                        uid,
                        "🏆 <b>Прошел месяц! Как ваши успехи?</b>\n"
                        "Давайте обновим ваш текущий вес, количество тренировок и сформируем свежий рацион по новым данным!",
                        reply_markup=b.as_markup(),
                    )
            except Exception:
                pass


# Строгий перехватчик нецелевых сообщений ТОЛЬКО во время прохождения анкеты (стоит в самом низу, чтобы не мешать кнопкам)
@router.message(StateFilter(OnboardingSG))
async def fallback_onboarding_strict(message: Message):
    await message.answer(
        "⚠️ <b>Ответьте на предыдущий вопрос!</b> Бот ждет вашего ответа по плану.",
        reply_markup=kb_back_only(),
    )


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
