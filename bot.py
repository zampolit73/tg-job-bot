import asyncio
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from groq import Groq

# ----------------- КЛЮЧИ -----------------
TELEGRAM_BOT_TOKEN = "8982024680:AAEwZQsfwx_BpdW5goe1ux3O94MT34Wfi3M"
GROQ_API_KEY = "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"
# ----------------------------------------

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
groq_client = Groq(api_key=GROQ_API_KEY.strip())

KNOWN_AGENCIES = [
    "кадровое", "рекрутинг", "recruitment", "staffing", "hr", "agency",
    "агентство", "selecty", "ancor", "анкор", "кадры", "outstaff", "аутстафф"
]


# Фоновый мини-сервер для прохождения health-check на Render
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, format, *args):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


def is_agency(company_name: str) -> bool:
    lower_name = company_name.lower()
    return any(w in lower_name for w in KNOWN_AGENCIES)


def fetch_hh(query: str, count: int = 15) -> list:
    url = "https://api.hh.ru/vacancies"
    params = {"text": query, "area": 113, "per_page": count}
    headers = {"User-Agent": "SalesLeadHunter/5.0"}
    jobs = []
    try:
        r = requests.get(url, params=params, headers=headers, timeout=6).json()
        for item in r.get("items", []):
            company = item.get("employer", {}).get("name", "Не указана")
            if is_agency(company):
                continue
            sal = item.get("salary")
            sal_str = "не указана"
            if sal:
                f, t, c = (sal.get("from") or "", sal.get("to") or "", sal.get("currency") or "")
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
    url = "https://career.habr.com/api/frontend/vacancies"
    params = {"q": query, "per_page": count}
    headers = {"User-Agent": "SalesLeadHunter/5.0"}
    jobs = []
    try:
        r = requests.get(url, params=params, headers=headers, timeout=6).json()
        for item in r.get("list", []):
            company = item.get("company", {}).get("title", "Не указана")
            if is_agency(company):
                continue
            sal = item.get("salary", {})
            sal_str = sal.get("formatted") if sal and sal.get("formatted") else "не указана"
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
        "Я найду прямые компании с открытой потребностью, оценю вероятность и дам рекомендации для сейла."
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    user_text = message.text
    status_msg = await message.answer("🕵️ **Шаг 1/3:** Выделяю ключевые маркеры и поисковые комбинации...")

    # 1. Быстрое извлечение 2 поисковых фраз через Groq
    prompt_kw = f"""На основе описания вакансии сформируй 2 коротких поисковых запроса (по 2-3 слова).
Первый — точная роль со стеком (например: 'Product Analyst дашборды').
Второй — специфика задач или продукта (например: 'промодвижок витрины данных').
Выведи ТОЛЬКО эти 2 запроса через точку с запятой, без лишнего текста:
{user_text}"""

    try:
        kw_completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt_kw}],
            temperature=0.1,
            max_tokens=60
        )
        kw_res = kw_completion.choices[0].message.content
        queries = [q.strip() for q in kw_res.split(";") if len(q.strip()) > 2]
    except Exception:
        queries = ["Аналитик данных", "Product Analyst"]

    if not queries:
        queries = ["Аналитик данных", "Product Analyst"]

    await status_msg.edit_text(f"🔍 **Шаг 2/3:** Ищу прямых работодателей по: `{', '.join(queries[:2])}`...")

    # 2. Сбор вакансий из hh.ru и Хабр Карьеры
    raw_vacancies = []
    for q in queries[:2]:
        raw_vacancies.extend(fetch_hh(q))
        raw_vacancies.extend(fetch_habr(q))

    unique_vacancies = list({v["url"]: v for v in raw_vacancies}.values())

    if not unique_vacancies:
        await status_msg.edit_text("❌ Не удалось найти открытые вакансии прямых работодателей. Попробуйте передать текст с более четким стеком/ролью.")
        return

    await status_msg.edit_text(f"🧠 **Шаг 3/3:** Анализирую {len(unique_vacancies)} вакансий и вычисляю прямого заказчика...")

    # 3. Детальный скоринг и аналитика для сейлза
    prompt_match = f"""Ты — эксперт OSINT-аналитики для сейлз-команды в IT-аутстаффинге.
ОБЕЗЛИЧЕННЫЙ ЗАПРОС КЛИЕНТА (ОТ АГЕНТСТВА/КОНКУРЕНТА):
\"\"\"{user_text}\"\"\"

ОТКРЫТЫЕ ВАКАНСИИ ПРЯМЫХ КОМПАНИЙ:
{unique_vacancies[:14]}

ЗАДАЧА:
Вычисли ТОП-3 компаний, которые с наибольшей вероятностью являются конечным заказчиком или имеют идентичную горящую потребность.

Формат для каждой компании:
🎯 **[Компания]** — Вероятность совпадения: **[X]%**
🔹 Должность: [Название вакансии]
🌐 Источник: [hh.ru или Хабр Карьера] | 💰 Зарплата: [Вилка]
🔗 Ссылка: [URL]
🕵️ **Маркеры совпадения:** (2-3 конкретных факта: почему это они, сходство задач, терминов, продукта)
💡 **Как зайти сейлзу:** (кому писать в компании и какую боль закрывать)
"""

    try:
        analysis_completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt_match}],
            temperature=0.2,
            max_tokens=2000
        )
        result_text = analysis_completion.choices[0].message.content
    except Exception as e:
        await status_msg.edit_text(f"Ошибка при анализе: {e}")
        return

    if len(result_text) > 4000:
        parts = [result_text[i:i+4000] for i in range(0, len(result_text), 4000)]
        await status_msg.edit_text(parts[0])
        for p in parts[1:]:
            await message.answer(p)
    else:
        await status_msg.edit_text(result_text)


async def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
