import asyncio
import os
import re
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from groq import Groq
from duckduckgo_search import DDGS

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

TECH_KEYWORDS = [
    "c#", ".net", "asp.net", "python", "django", "fastapi", "flask",
    "java", "spring", "kotlin", "golang", "go", "php", "laravel",
    "javascript", "typescript", "react", "vue", "angular", "node.js",
    "devops", "kubernetes", "k8s", "docker", "ansible", "terraform",
    "qa", "тестировщик", "automation", "autotest", "selenium", "playwright",
    "data science", "ml", "системный аналитик", "бизнес-аналитик", "product manager",
    "ios", "swift", "android", "flutter", "1c", "1с"
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
    models_to_try = ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"]
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
    found = []
    text_lower = text.lower()
    for tech in TECH_KEYWORDS:
        if re.search(r"\b" + re.escape(tech) + r"\b", text_lower):
            found.append(tech)
    if found:
        return [" ".join(found[:3]), found[0]]
    return [text.split("\n")[0][:35].strip(), "IT"]


def is_agency(company_name: str) -> bool:
    lower_name = company_name.lower()
    return any(w in lower_name for w in KNOWN_AGENCIES)


# 1. HeadHunter
def fetch_hh(query: str, count: int = 10) -> list:
    url = "https://api.hh.ru/vacancies"
    params = {"text": query, "area": 113, "per_page": count}
    headers = {"User-Agent": "MultiSourceHunter/1.0"}
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


# 2. Хабр Карьера
def fetch_habr(query: str, count: int = 10) -> list:
    url = "https://career.habr.com/api/frontend/vacancies"
    params = {"q": query, "per_page": count}
    headers = {"User-Agent": "MultiSourceHunter/1.0"}
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


# 3. Веб-поисковик (DuckDuckGo: ищет по всему интернету, карьерным сайтам и блогам без API-ключей)
def fetch_web_search(query: str, count: int = 5) -> list:
    jobs = []
    try:
        ddgs = DDGS()
        # Ищем по ключевым словам и карьерным маркерам
        search_query = f"{query} вакансия карьера"
        results = list(ddgs.text(search_query, max_results=count))
        for res in results:
            title = res.get("title", "")
            url = res.get("href", "")
            snippet = res.get("body", "")

            # Извлекаем потенциальное имя компании из заголовка
            company_candidate = title.split("—")[0].split("-")[0].split(":")[0].strip()
            if not is_agency(company_candidate):
                jobs.append({
                    "source": "Веб-поиск (Google/Яндекс)",
                    "title": title,
                    "company": company_candidate,
                    "salary": "не указана",
                    "url": url,
                    "desc": snippet,
                })
    except Exception:
        pass
    return jobs


@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "💼 **Multi-Source B2B Lead Finder**\n\n"
        "Отправьте мне текст вакансии (требования, задачи, стек).\n"
        "Я просканирую: **hh.ru**, **Хабр Карьеру** и **открытый веб-поиск**, "
        "чтобы найти прямого заказчика."
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    user_text = message.text
    status_msg = await message.answer("🕵️ **Шаг 1/3:** Сканирую стек и формирую поисковые запросы...")

    prompt_kw = f"""Определи ключевой стек и редкие фразы из текста вакансии.
Сформируй 2 поисковых запроса:
1. Роль и главный стек (например: 'C# .NET Backend').
2. Уникальная фраза или связка библиотек из текста (например: 'мониторинг промодвижка' или 'ASP.NET Core SOLID').
Выведи ТОЛЬКО 2 запроса через точку с запятой:
{user_text}"""

    kw_res = call_groq(prompt_kw, max_tokens=60)
    queries = [q.strip() for q in kw_res.split(";") if len(q.strip()) > 1]
    if not queries:
        queries = fallback_extract_keywords(user_text)

    await status_msg.edit_text(
        f"🔍 **Шаг 2/3:** Опрашиваю hh.ru, Хабр и Веб по запросам: `{', '.join(queries[:2])}`..."
    )

    raw_vacancies = []
    # Сбор по всем открытым источникам
    for q in queries[:2]:
        raw_vacancies.extend(fetch_hh(q, count=8))
        raw_vacancies.extend(fetch_habr(q, count=8))
        raw_vacancies.extend(fetch_web_search(q, count=4))

    unique_vacancies = list({v["url"]: v for v in raw_vacancies}.values())

    if not unique_vacancies:
        await status_msg.edit_text("❌ Не удалось найти данные по открытым источникам. Попробуйте передать текст с более четким стеком.")
        return

    await status_msg.edit_text(f"🧠 **Шаг 3/3:** Анализирую {len(unique_vacancies)} найденных ссылок из всех источников...")

    prompt_match = f"""Ты — OSINT-аналитик сейлз-команды в IT-аутстаффинге.
ОБЕЗЛИЧЕННЫЙ ТЕКСТ ВАКАНСИИ:
\"\"\"{user_text}\"\"\"

НАЙДЕННЫЕ ССЫЛКИ И ВАКАНСИИ ИЗ РАЗНЫХ ИСТОЧНИКОВ:
{unique_vacancies[:15]}

ЗАДАЧА:
Вычисли ТОП-3 компаний, которые с наибольшей вероятностью являются прямым конечным заказчиком или имеют аналогичную горящую потребность.

Формат для каждой компании:
🎯 **[Компания]** — Вероятность совпадения: **[X]%**
🔹 Название вакансии/страницы: [Название]
🌐 Источник: [hh.ru / Хабр Карьера / Веб-поиск] | 💰 Зарплата: [Вилка или 'не указана']
🔗 Ссылка: [URL]
🕵️ **Маркеры совпадения:** (2-3 конкретных факта: совпадение редкого стека, формулировок задач, архитектуры)
💡 **Как зайти сейлзу:** (кому писать и с каким оффером обращаться)
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
