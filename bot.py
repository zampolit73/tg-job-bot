import asyncio
import os
import re
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.enums import ParseMode
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


def call_groq_safe(prompt: str, max_tokens: int = 1500) -> tuple[str, str]:
    models = [
        "openai/gpt-oss-20b",
        "qwen/qwen3.6-27b",
        "llama-3.1-8b-instant",
    ]
    last_err = ""
    for model_name in models:
        try:
            res = groq_client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=max_tokens
            )
            content = res.choices[0].message.content
            if content and len(content.strip()) > 0:
                return content, ""
        except Exception as e:
            last_err = str(e)
            continue
    return "", last_err


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


def fetch_hh(query: str, count: int = 8) -> list:
    url = "https://api.hh.ru/vacancies"
    params = {"text": query, "area": 113, "per_page": count}
    headers = {"User-Agent": "MultiSourceHunter/3.0"}
    jobs = []
    try:
        r = requests.get(url, params=params, headers=headers, timeout=5).json()
        for item in r.get("items", []):
            company = item.get("employer", {}).get("name", "Не указана")
            if is_agency(company):
                continue
            sal = item.get("salary")
            sal_str = f"{sal.get('from') or ''} - {sal.get('to') or ''} {sal.get('currency') or ''}".strip() if sal else "не указана"
            desc = f"{item.get('snippet', {}).get('requirement', '')} {item.get('snippet', {}).get('responsibility', '')}"
            jobs.append({
                "source": "hh.ru",
                "title": item.get("name"),
                "company": company,
                "salary": sal_str,
                "url": item.get("alternate_url"),
                "desc": desc[:250],
            })
    except Exception:
        pass
    return jobs


def fetch_habr(query: str, count: int = 8) -> list:
    url = "https://career.habr.com/api/frontend/vacancies"
    params = {"q": query, "per_page": count}
    headers = {"User-Agent": "MultiSourceHunter/3.0"}
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
            skills = ", ".join([s.get("title", "") for s in item.get("skills", [])])
            jobs.append({
                "source": "Хабр Карьера",
                "title": item.get("title"),
                "company": company,
                "salary": sal_str,
                "url": full_url,
                "desc": f"Навыки: {skills[:200]}",
            })
    except Exception:
        pass
    return jobs


def fetch_web_safe(query: str, count: int = 3) -> list:
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
                    "desc": res.get("body", "")[:200],
                })
    except Exception:
        pass
    return jobs


@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "💼 **OSINT B2B Lead Finder**\n\n"
        "Отправьте мне обезличенный текст заявки/вакансии.\n"
        "Я просканирую рынок, определю вероятного конечного заказчика и подготовлю структуру для выхода сейлза.",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    user_text = message.text
    status_msg = await message.answer("🔍 **Этап 1/3:** Выделяю ключевой стек и уникальные поисковые токены...")

    # Улучшенный промпт генерации запросов: акцент на редких сущностях
    prompt_kw = f"""Ты — специалист по поиску в базах вакансий (hh.ru, Хабр).
Проанализируй текст и выдели 2 точных поисковых запроса (по 2-3 слова).
Запрос 1 (Стек): главная роль + 1-2 обязательные технологии (например: 'C# ASP.NET' или 'Системный аналитик BPMN').
Запрос 2 (Уникальный маркер): редкие термины, отражающие специфику задач (например: 'промодвижок витрины' или 'highload clickhouse' или 'микросервисы gRPC').

Выведи ТОЛЬКО эти два запроса через точку с запятой, без кавычек и пояснений:
{user_text[:600]}"""

    kw_res, _ = call_groq_safe(prompt_kw, max_tokens=50)
    queries = [q.strip() for q in kw_res.split(";") if len(q.strip()) > 1]
    if not queries:
        queries = fallback_extract_keywords(user_text)

    await status_msg.edit_text(
        f"🌐 **Этап 2/3:** Сканирую базы работодателей по ключам:\n`{queries[0]}` | `{queries[1] if len(queries) > 1 else ''}`..."
    )

    raw_vacancies = []
    for q in queries[:2]:
        raw_vacancies.extend(fetch_hh(q, count=7))
        raw_vacancies.extend(fetch_habr(q, count=7))
        raw_vacancies.extend(fetch_web_safe(q, count=2))

    unique_vacancies = list({v["url"]: v for v in raw_vacancies if v.get("url")}.values())

    if not unique_vacancies:
        await status_msg.edit_text("❌ Не удалось найти прямые вакансии. Попробуйте передать текст с более конкретным описанием стека.")
        return

    await status_msg.edit_text(f"📊 **Этап 3/3:** Скоринг и подготовка аналитики по {len(unique_vacancies)} вакансиям...")

    compact_list = []
    for idx, v in enumerate(unique_vacancies[:12], 1):
        compact_list.append(
            f"[{idx}] Компания: {v['company']} | Роль: {v['title']} | Источник: {v['source']} | URL: {v['url']} | Описание: {v['desc']}"
        )
    vacancies_payload = "\n".join(compact_list)

    # Улучшенный промпт оформления: строгий OSINT-формат карточек
    prompt_match = f"""Ты — ведущий OSINT-аналитик агентства IT-аутстаффинга.
ИСХОДНЫЙ ТЕКСТ ЗАПРОСА КЛИЕНТА:
\"\"\"{user_text[:800]}\"\"\"

СПИСОК НАЙДЕННЫХ ВАКАНСИЙ РАБОТОДАТЕЛЕЙ:
{vacancies_payload}

ЗАДАЧА:
Выбери ТОП-3 наиболее вероятных прямых заказчиков. 
Оформи вывод строго по шаблону ниже для каждого кандидата. Используй аккуратную разметку и разделители.

---
🏢 **[Название Компании]** (Вероятность: **XX%**)
📌 **Вакансия:** [Название должности]
💰 **Зарплатная вилка:** [Вилка или 'По договоренности']
🔗 **Ссылка:** [Открыть вакансию](URL)
🏛 **Источник:** [hh.ru / Хабр / Веб]

🔍 **Факты совпадения:**
* [Конкретное совпадение по технологиям или фреймворкам]
* [Совпадение по продукту, задачам или терминологии из текста]

🎯 **Стратегия захода для сейлза:**
* **Кому писать:** [Роль ЛПР: CTO, Head of QA, Head of Analytics, Team Lead]
* **Болевая точка:** [Какую проблему решает аутстафф в этом проекте]
* **Оффер:** [Короткая фраза первого контакта]
---
"""

    result_text, err = call_groq_safe(prompt_match, max_tokens=1700)
    
    if not result_text:
        await status_msg.edit_text(f"⚠️ Ошибка вызова Groq API: {err}")
        return

    # Заголовок отчёта
    final_output = "📋 **РЕЗУЛЬТАТЫ OSINT-АНАЛИЗА ЗАКАЗЧИКА**\n\n" + result_text

    if len(final_output) > 4000:
        parts = [final_output[i:i+4000] for i in range(0, len(final_output), 4000)]
        await status_msg.edit_text(parts[0], parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        for p in parts[1:]:
            await message.answer(p, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    else:
        await status_msg.edit_text(final_output, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


async def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
