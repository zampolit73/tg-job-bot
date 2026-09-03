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

# Словарь для аварийного распознавания стека без ИИ
TECH_KEYWORDS = [
    "c#", ".net", "asp.net", "python", "django", "fastapi", "flask",
    "java", "spring", "kotlin", "golang", "go", "php", "laravel",
    "javascript", "typescript", "react", "vue", "angular", "node.js",
    "devops", "kubernetes", "k8s", "docker", "ansible", "terraform",
    "qa", "тестировщик", "automation", "autotest", "selenium", "playwright",
    "data science", "ml", "machine learning", "computer vision", "nlp",
    "системный аналитик", "бизнес-аналитик", "product manager", "project manager",
    "ios", "swift", "android", "flutter", "react native", "1c", "1с"
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
    models_to_try = [
        "llama-3.1-8b-instant",
        "llama-3.3-70b-versatile",
    ]
    for model_name in models_to_try:
        try:
            res = groq_client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=max_tokens
            )
            return res.choices[0].message.content
        except Exception:
            continue
    return ""


def fallback_extract_keywords(text: str) -> list:
    """Аварийное извлечение IT-стека из текста регулярками, если API временно недоступен."""
    found = []
    text_lower = text.lower()
    for tech in TECH_KEYWORDS:
        pattern = r"\b" + re.escape(tech) + r"\b"
        if re.search(pattern, text_lower):
            found.append(tech)
    if found:
        return [" ".join(found[:3]), found[0]]
    return [text.split("\n")[0][:35].strip(), "IT"]


def is_agency(company_name: str) -> bool:
    lower_name = company_name.lower()
    return any(w in lower_name for w in KNOWN_AGENCIES)


def fetch_hh(query: str, count: int = 15) -> list:
    url = "https://api.hh.ru/vacancies"
    params = {"text": query, "area": 113, "per_page": count}
    headers = {"User-Agent": "UniversalITLeadHunter/6.0"}
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
    headers = {"User-Agent": "UniversalITLeadHunter/6.0"}
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
        "💼 **Universal B2B IT Lead Finder**\n\n"
        "Отправьте мне текст любой IT-вакансии (Backend, Frontend, DevOps, Mobile, QA, Analytics, Management).\n"
        "Я определю стек, найду прямых заказчиков на hh.ru и Хабр Карьере и рассчитаю вероятности совпадения."
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    user_text = message.text
    status_msg = await message.answer("🕵️ **Шаг 1/3:** Сканирую IT-стек и формулирую поисковые запросы...")

    # 1. Универсальное извлечение роли и стека под любую IT-дисциплину
    prompt_kw = f"""Определи точную IT-специализацию по тексту (разработка, devops, тестирования, аналитика, дизайн или менеджмент).
Сформируй 2 коротких поисковых запроса (по 2-3 слова).
Запрос 1: точное название роли + главный язык/технология (например: 'C# Developer' или 'DevOps Kubernetes' или 'Системный аналитик BPMN').
Запрос 2: специфический инструмент/фреймворк из текста (например: 'ASP.NET Core REST' или 'PostgreSQL ClickHouse' или 'Playwright TypeScript').
Выведи ТОЛЬКО эти два запроса через точку с запятой, без комментариев и кавычек:
{user_text}"""

    kw_res = call_groq(prompt_kw, max_tokens=60)
    queries = [q.strip() for q in kw_res.split(";") if len(q.strip()) > 1]

    # Если ИИ дал сбой, используем аварийный семантический парсер
    if not queries:
        queries = fallback_extract_keywords(user_text)

    await status_msg.edit_text(f"🔍 **Шаг 2/3:** Ищу прямых работодателей по: `{', '.join(queries[:2])}`...")

    # 2. Сбор вакансий
    raw_vacancies = []
    for q in queries[:2]:
        raw_vacancies.extend(fetch_hh(q))
        raw_vacancies.extend(fetch_habr(q))

    unique_vacancies = list({v["url"]: v for v in raw_vacancies}.values())

    if not unique_vacancies:
        await status_msg.edit_text("❌ Не удалось найти прямые вакансии по этому стеку. Попробуйте передать текст с более явным указанием технологий.")
        return

    await status_msg.edit_text(f"🧠 **Шаг 3/3:** Анализирую {len(unique_vacancies)} вакансий и вычисляю конечного заказчика...")

    # 3. Скоринг прямого заказчика
    prompt_match = f"""Ты — OSINT-аналитик сейлз-команды в IT-аутстаффинге.
ОБЕЗЛИЧЕННЫЙ ЗАПРОС КЛИЕНТА (ОТ АГЕНТСТВА/КОНКУРЕНТА):
\"\"\"{user_text}\"\"\"

ОТКРЫТЫЕ ВАКАНСИИ ПРЯМЫХ КОМПАНИЙ:
{unique_vacancies[:14]}

ЗАДАЧА:
Вычисли ТОП-3 компаний, которые с наибольшей вероятностью являются конечным заказчиком или имеют идентичный горящий запрос на таких специалистов.

Формат для каждой компании:
🎯 **[Компания]** — Вероятность совпадения: **[X]%**
🔹 Должность: [Название вакансии]
🌐 Источник: [hh.ru или Хабр Карьера] | 💰 Зарплата: [Вилка]
🔗 Ссылка: [URL]
🕵️ **Маркеры совпадения:** (2-3 конкретных факта: совпадение редкого стека, архитектурных требований, специфики продукта)
💡 **Как зайти сейлзу:** (кому писать в компании — CTO/Team Lead/Head of QA и под какую задачу предлагать ресурсы)
"""

    result_text = call_groq(prompt_match, max_tokens=1800)
    if not result_text:
        await status_msg.edit_text("Не удалось сформировать отчет. Попробуйте еще раз.")
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
