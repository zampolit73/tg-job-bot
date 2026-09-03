import asyncio
import os
import re
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from groq import Groq

# ----------------- КЛЮЧИ -----------------
TELEGRAM_BOT_TOKEN = "8982024680:AAGSIE8AbyboYoG1HcxLmI9-7ljX2JbTk7s"
GROQ_API_KEY = "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"
# ----------------------------------------

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
groq_client = Groq(api_key=GROQ_API_KEY.strip())

KNOWN_AGENCIES = [
    "кадровое", "рекрутинг", "recruitment", "staffing", "hr", "agency",
    "агентство", "selecty", "ancor", "анкор", "кадры", "outstaff", "аутстафф"
]

TECH_KEYWORDS = [
    "c#", ".net", "asp.net", "python", "django", "fastapi", "flask",
    "java", "spring", "kotlin", "golang", "go", "php", "laravel",
    "javascript", "typescript", "react", "vue", "angular", "node.js",
    "devops", "kubernetes", "k8s", "docker", "ansible", "terraform",
    "qa", "тестировщик", "automation", "autotest", "selenium", "playwright",
    "data science", "ml", "системный аналитик", "бизнес-аналитик", "product manager",
    "ios", "swift", "android", "flutter", "1c", "1с", "sql", "clickhouse", "dwh"
]


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


def call_groq(prompt: str, max_tokens: int = 1500) -> str:
    """Вызов рабочей модели Groq с автопереключением."""
    models_to_try = ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"]
    for model_name in models_to_try:
        try:
            res = groq_client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=max_tokens
            )
            content = res.choices[0].message.content
            if content and len(content.strip()) > 0:
                return content
        except Exception:
            continue
    return ""


def fallback_extract_keywords(text: str) -> list:
    found = []
    text_lower = text.lower()
    for tech in TECH_KEYWORDS:
        if re.search(r"\b" + re.escape(tech) + r"\b", text_lower):
            found.append(tech)
    if found:
        return [" ".join(found[:3]), found[0]]
    first_phrase = text.split("\n")[0][:30].strip()
    return [first_phrase if first_phrase else "IT Вакансия", "Разработчик"]


def is_agency(company_name: str) -> bool:
    lower_name = company_name.lower()
    return any(w in lower_name for w in KNOWN_AGENCIES)


# 1. HeadHunter API
def fetch_hh(query: str, count: int = 10) -> list:
    url = "https://api.hh.ru/vacancies"
    params = {"text": query, "area": 113, "per_page": count}
    headers = {"User-Agent": "MultiSourceHunter/2.0"}
    jobs = []
    try:
        r = requests.get(url, params=params, headers=headers, timeout=5).json()
        for item in r.get("items", []):
            company = item.get("employer", {}).get("name", "Не указана")
            if is_agency(company):
                continue
            sal = item.get("salary")
            sal_str = f"{sal.get('from') or ''} - {sal.get('to') or ''} {sal.get('currency') or ''}".strip() if sal else "не указана"
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


# 2. Хабр Карьера API
def fetch_habr(query: str, count: int = 10) -> list:
    url = "https://career.habr.com/api/frontend/vacancies"
    params = {"q": query, "per_page": count}
    headers = {"User-Agent": "MultiSourceHunter/2.0"}
    jobs = []
    try:
        r = requests.get(url, params=params, headers=headers, timeout=5).json()
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


# 3. Веб-поиск с защитой от сбоев
def fetch_web_safe(query: str, count: int = 4) -> list:
    jobs = []
    try:
        from duckduckgo_search import DDGS
        ddgs = DDGS(timeout=5)
        results = list(ddgs.text(f"{query} вакансия", max_results=count))
        for res in results:
            title = res.get("title", "")
            company_cand = title.split("—")[0].split("-")[0].split(":")[0].strip()
            if not is_agency(company_cand):
                jobs.append({
                    "source": "Веб-поиск",
                    "title": title,
                    "company": company_cand,
                    "salary": "не указана",
                    "url": res.get("href", ""),
                    "desc": res.get("body", ""),
                })
    except Exception:
        pass
    return jobs


@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "💼 **Multi-Source B2B Lead Finder**\n\n"
        "Отправьте мне текст любой вакансии.\n"
        "Я найду прямых работодателей, исключу кадровые агентства и сформирую рекомендации для выхода на ЛПР."
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    user_text = message.text
    status_msg = await message.answer("🕵️ **Шаг 1/3:** Выделяю маркеры проекта и поисковые связки...")

    prompt_kw = f"""Выдели ключевую роль, стек и уникальные проектные термины.
Сформируй ровно 2 поисковых запроса (по 2-3 слова).
Пример: 'Аналитик дашборды; промодвижок витрины данных' или 'C# ASP.NET; REST API SOLID'.
Выведи ТОЛЬКО 2 фразы через точку с запятой:
{user_text}"""

    kw_res = call_groq(prompt_kw, max_tokens=60)
    queries = [q.strip() for q in kw_res.split(";") if len(q.strip()) > 1]
    if not queries:
        queries = fallback_extract_keywords(user_text)

    await status_msg.edit_text(
        f"🔍 **Шаг 2/3:** Ищу прямые вакансии по маркерам: `{', '.join(queries[:2])}`..."
    )

    raw_vacancies = []
    for q in queries[:2]:
        raw_vacancies.extend(fetch_hh(q, count=8))
        raw_vacancies.extend(fetch_habr(q, count=8))
        raw_vacancies.extend(fetch_web_safe(q, count=3))

    unique_vacancies = list({v["url"]: v for v in raw_vacancies if v.get("url")}.values())

    if not unique_vacancies:
        await status_msg.edit_text("❌ Не удалось найти прямые вакансии. Попробуйте передать текст с более четким названием технологий.")
        return

    await status_msg.edit_text(f"🧠 **Шаг 3/3:** Анализирую {len(unique_vacancies)} предложений и вычисляю прямого заказчика...")

    prompt_match = f"""Ты — OSINT-аналитик сейлз-команды в IT-аутстаффинге.
ОБЕЗЛИЧЕННЫЙ ЗАПРОС КЛИЕНТА (ОТ АГЕНТСТВА/КОНКУРЕНТА):
\"\"\"{user_text}\"\"\"

ОТКРЫТЫЕ ВАКАНСИИ ПРЯМЫХ КОМПАНИЙ:
{unique_vacancies[:12]}

ЗАДАЧА:
Вычисли ТОП-3 компаний, которые с наибольшей вероятностью являются прямым конечным заказчиком или имеют идентичную горящую потребность.

Формат для каждой компании:
🎯 **[Компания]** — Вероятность совпадения: **[X]%**
🔹 Должность: [Название вакансии]
🌐 Источник: [hh.ru / Хабр Карьера / Веб-поиск] | 💰 Зарплата: [Вилка или 'не указана']
🔗 Ссылка: [URL]
🕵️ **Маркеры совпадения:** (2-3 конкретных факта: почему это они, сходство задач, терминов промодвижка/витрин/дашбордов или стека)
💡 **Как зайти сейлзу:** (кому писать и с каким оффером обращаться)
"""

    result_text = call_groq(prompt_match, max_tokens=1800)
    
    if not result_text:
        await status_msg.edit_text("⚠️ Ошибка: Groq API временно не вернул ответ. Проверьте статус ключа в console.groq.com.")
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
    # Сбрасываем старые зависшие апдейты Telegram перед стартом
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
