import asyncio
import json
import os
import re
import requests
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message

# ----------------- КЛЮЧИ -----------------
TELEGRAM_BOT_TOKEN = "8982024680:AAEwZQsfwx_BpdW5goe1ux3O94MT34Wfi3M"
OPENROUTER_KEY = "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"
# ----------------------------------------

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# Черный список явных рекрутинговых агентств и посредников (чтобы искать прямых клиентов)
KNOWN_AGENCIES = [
    "кадровое", "рекрутинг", "recruitment", "staffing", "hr", "agency",
    "агентство", "selecty", "ancor", "анкор", "кадры", "outstaff", "аутстафф"
]


def call_ai(prompt: str, json_mode: bool = False) -> str:
    """Запрос через OpenRouter с автомаршрутизацией по бесплатным моделям."""
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY.strip()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://render.com",
        "X-Title": "SalesClientFinderBot",
    }

    models_to_try = [
        "openrouter/free",
        "openrouter/auto",
        "mistralai/mistral-small-3-instruct:free",
        "google/gemma-2-9b-it:free",
    ]

    for model in models_to_try:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            r = requests.post(url, headers=headers, json=payload, timeout=35)
            data = r.json()
            if "choices" in data and len(data["choices"]) > 0:
                return data["choices"][0]["message"]["content"]
        except Exception:
            continue

    return ""


def is_agency(company_name: str) -> bool:
    """Проверка, не является ли компания рекрутинговым агентством."""
    lower_name = company_name.lower()
    return any(agency_word in lower_name for agency_word in KNOWN_AGENCIES)


def fetch_hh(query: str, count: int = 15) -> list:
    """Парсинг hh.ru с отсевом кадровых агентств."""
    url = "https://api.hh.ru/vacancies"
    params = {"text": query, "area": 113, "per_page": count}
    headers = {"User-Agent": "SalesLeadHunter/2.0"}
    jobs = []
    try:
        r = requests.get(url, params=params, headers=headers, timeout=8).json()
        for item in r.get("items", []):
            company = item.get("employer", {}).get("name", "Не указана")
            # Отсекаем очевидных посредников
            if is_agency(company):
                continue

            sal = item.get("salary")
            sal_str = "не указана"
            if sal:
                f, t, c = (
                    sal.get("from") or "",
                    sal.get("to") or "",
                    sal.get("currency") or "",
                )
                sal_str = f"{f} - {t} {c}".strip()

            jobs.append({
                "source": "hh.ru",
                "title": item.get("name"),
                "company": company,
                "salary": sal_str,
                "url": item.get("alternate_url"),
                "desc": f"{item.get('snippet', {}).get('requirement', '')} {item.get('snippet', {}).get('responsibility', '')}",
            })
    except Exception:
        pass
    return jobs


def fetch_habr(query: str, count: int = 15) -> list:
    """Парсинг Хабр Карьеры с отсевом посредников."""
    url = "https://career.habr.com/api/frontend/vacancies"
    params = {"q": query, "per_page": count}
    headers = {"User-Agent": "SalesLeadHunter/2.0"}
    jobs = []
    try:
        r = requests.get(url, params=params, headers=headers, timeout=8).json()
        for item in r.get("list", []):
            company = item.get("company", {}).get("title", "Не указана")
            if is_agency(company):
                continue

            sal = item.get("salary", {})
            sal_str = "не указана"
            if sal and sal.get("formatted"):
                sal_str = sal.get("formatted")

            href = item.get("href", "")
            full_url = f"https://career.habr.com{href}" if href.startswith("/") else href

            jobs.append({
                "source": "Хабр Карьера",
                "title": item.get("title"),
                "company": company,
                "salary": sal_str,
                "url": full_url,
                "desc": " ".join([s.get("title", "") for s in item.get("skills", [])]),
            })
    except Exception:
        pass
    return jobs


@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "💼 **B2B Lead Finder: Детектор прямого заказчика**\n\n"
        "Отправьте мне текст обезличенной вакансии конкурента или агентства.\n"
        "Я проанализирую маркеры продукта, архитектуру, стек, исключу посредников "
        "и вычислю прямые компании с открытой потребностью."
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    user_text = message.text
    status_msg = await message.answer(
        "🕵️ **Шаг 1/3:** Выделяю уникальные проектные маркеры и поисковые комбинации..."
    )

    # 1. Формирование нескольких точных поисковых запросов
    extract_prompt = f"""
Ты — OSINT-аналитик в B2B-продажах IT-услуг.
Твоя цель: помочь сейлзу найти конечного клиента, чью вакансию перепродают аутстафферы/агентства.

Текст вакансии:
{user_text}

Задачи:
1. Выдели редкую связку стека (например, не просто 'Java', а 'Java Spring Cloud Kafka ClickHouse').
2. Выдели отраслевые/бизнес маркеры (например, 'процессинг платежей', 'WMS логистика', 'highload e-commerce').
3. Сформируй 2 коротких поисковых запроса (по 2-4 слова каждый).

Выведи ответ СТРОГО в формате JSON без markdown и пояснений:
{{"queries": ["запрос_1", "запрос_2"]}}
"""

    search_queries = []
    ai_extract = call_ai(extract_prompt, json_mode=True)
    try:
        # Очистка от лишних markdown-оберток если модель их добавила
        clean_json = re.search(r"\{.*\}", ai_extract, re.DOTALL)
        if clean_json:
            parsed = json.loads(clean_json.group())
            search_queries = parsed.get("queries", [])
    except Exception:
        pass

    if not search_queries:
        search_queries = ["Python FastAPI", "Backend Developer"]

    await status_msg.edit_text(
        f"🔍 **Шаг 2/3:** Ищу прямых работодателей по маркерам: `{', '.join(search_queries)}`..."
    )

    # 2. Сбор вакансий по нескольким веткам запросов
    raw_vacancies = []
    for q in search_queries:
        raw_vacancies.extend(fetch_hh(q, count=10))
        raw_vacancies.extend(fetch_habr(q, count=10))

    # Удаляем дубликаты по URL
    unique_vacancies = list({v["url"]: v for v in raw_vacancies}.values())

    if not unique_vacancies:
        await status_msg.edit_text(
            "❌ Не удалось найти релевантные вакансии прямых работодателей. Попробуйте передать более подробное описание с задачами и стеком."
        )
        return

    await status_msg.edit_text(
        f"🧠 **Шаг 3/3:** Анализирую {len(unique_vacancies)} вакансий, отсеиваю посредников и вычисляю конечного заказчика..."
    )

    # 3. Глубокий скоринг и детекция прямого клиента
    deep_analysis_prompt = f"""
Ты — эксперт по расследованию и детекции конечных заказчиков для B2B-аутстаффинга.

ОБЕЗЛИЧЕННАЯ ВАКАНСИЯ АГЕНТСТВА/КОНКУРЕНТА:
\"\"\"{user_text}\"\"\"

НАЙДЕННЫЕ ВАКАНСИИ ПРЯМЫХ КОМПАНИЙ:
{unique_vacancies[:18]}

ТВОЯ ЦЕЛЬ:
Определить, какая из этих компаний с наибольшей вероятностью является КОНЕЧНЫМ ЗАКАЗЧИКОМ (тем, кто на самом деле нанимает этих людей).

Оценивай:
1. Совпадение специфических обязанностей и описания проекта.
2. Совпадение архитектуры и редких технологий.
3. Совпадение вилок зарплат или грейда.

ВЫВЕДИ ТОП-3 САМЫХ ПЕРСПЕКТИВНЫХ КОМПАНИЙ ДЛЯ ВЫХОДА СЕЙЛЗА:

Формат каждого кандидата:
🎯 **[Название компании]** — Вероятность, что это прямой заказчик: **[X]%**
🔹 Должность в оригинале: [Название вакансии]
🌐 Источник: [hh.ru или Хабр Карьера] | 💰 Зарплата: [Вилка или 'не указана']
🔗 Ссылка: [URL]
🕵️ **Улики и маркеры совпадения:** (2-3 конкретных факта, почему это именно они: стек, домен, схожие формулировки задач)
💡 **Рекомендация сейлзу:** (Как зайти: кого искать в LinkedIn/Telegram и под какую боль предлагать ресурсы)

Если ни одна компания не похожа на 100% того же заказчика, укажи те, у которых аналогичный стек и горящая потребность прямо сейчас.
"""

    result_text = call_ai(deep_analysis_prompt)

    if not result_text:
        await status_msg.edit_text("Ошибка при генерации аналитики. Попробуйте еще раз.")
        return

    if len(result_text) > 4000:
        parts = [result_text[i : i + 4000] for i in range(0, len(result_text), 4000)]
        await status_msg.edit_text(parts[0])
        for p in parts[1:]:
            await message.answer(p)
    else:
        await status_msg.edit_text(result_text)


async def handle_ping(request):
    return web.Response(text="Bot is running!")


async def main():
    port = int(os.environ.get("PORT", 10000))
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
