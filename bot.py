import asyncio
import os
import re
import threading
from urllib.parse import quote_plus
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
    "агентство", "selecty", "ancor", "анкор", "кадры", "outstaff", "аутстафф",
    "personnel", "talent", "staff", "headhunting", "подбор персонала"
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


def call_groq_safe(prompt: str, max_tokens: int = 2000) -> tuple[str, str]:
    models = [
        "llama-3.1-8b-instant",
        "openai/gpt-oss-20b",
        "qwen/qwen3.6-27b"
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
        return [f"NAME:({found[0]})", " ".join(found[:3]), f'"{found[0]}"']
    first_phrase = text.split("\n")[0][:30].strip()
    return [first_phrase if first_phrase else "Разработчик", "IT", "Backend"]


def is_agency(company_name: str) -> bool:
    lower_name = company_name.lower()
    return any(w in lower_name for w in KNOWN_AGENCIES)


# 1. HeadHunter API с поддержкой расширенных операторов поиска
def fetch_hh(query: str, count: int = 10) -> list:
    url = "https://api.hh.ru/vacancies"
    params = {"text": query, "area": 113, "per_page": count}
    headers = {"User-Agent": "DeepOSINTHunter/5.0"}
    jobs = []
    try:
        r = requests.get(url, params=params, headers=headers, timeout=6).json()
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
                "desc": desc[:300],
            })
    except Exception:
        pass
    return jobs


# 2. Хабр Карьера API
def fetch_habr(query: str, count: int = 10) -> list:
    clean_q = re.sub(r'[^\w\s\+\#\.\-]', ' ', query).strip()
    url = "https://career.habr.com/api/frontend/vacancies"
    params = {"q": clean_q, "per_page": count}
    headers = {"User-Agent": "DeepOSINTHunter/5.0"}
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
            skills = ", ".join([s.get("title", "") for s in item.get("skills", [])])
            jobs.append({
                "source": "Хабр Карьера",
                "title": item.get("title"),
                "company": company,
                "salary": sal_str,
                "url": full_url,
                "desc": f"Стек: {skills[:250]}",
            })
    except Exception:
        pass
    return jobs


# 3. Веб-поиск (Exact Match по фразам в кавычках)
def fetch_web_safe(query: str, count: int = 3) -> list:
    jobs = []
    try:
        from duckduckgo_search import DDGS
        ddgs = DDGS(timeout=5)
        search_str = query if '"' in query else f"{query} вакансия"
        results = list(ddgs.text(search_str, max_results=count))
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
                    "desc": res.get("body", "")[:250],
                })
    except Exception:
        pass
    return jobs


def build_lead_osint_url(company_name: str) -> str:
    """Генерация прямой ссылки на поиск руководителей компании в LinkedIn через Google."""
    clean_company = re.sub(r'[\'\"«»]', '', company_name).strip()
    target_role = "CTO OR \"Team Lead\" OR \"Head of Analytics\" OR \"Engineering Manager\""
    query = f'site:linkedin.com/in "{clean_company}" ({target_role})'
    return f"https://www.google.com/search?q={quote_plus(query)}"


@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "💼 **Deep OSINT B2B Lead Hunter**\n\n"
        "Отправьте мне текст заявки/вакансии (стек, требования, задачи).\n"
        "Я запущу поиск по операторам hh.ru, Хабра и веб-цитатам, "
        "определю конечного заказчика и сформирую ссылки на контакты ЛПР.",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    user_text = message.text
    status_msg = await message.answer("🔍 **Шаг 1/3:** Глубокий OSINT-анализ и синтез точных операторов...")

    prompt_kw = f"""Ты — эксперт по поисковому синтаксису hh.ru и поисковых систем.
Изучи текст и сформируй ровно 3 запроса через точку с запятой:
1. Запрос для названия роли в hh.ru строго через оператор NAME:(...). Например: NAME:("Product Analyst") или NAME:("C#" OR ".NET").
2. Ядро технологического стека (2-3 главных инструмента). Например: 'ClickHouse Superset SQL' или 'ASP.NET Core PostgreSQL'.
3. Самая редкая уникальная цитата/словосочетание из текста в кавычках (Exact Match). Например: '"промодвижка"' или '"витрины данных"' или '"микросервисы gRPC"'.

Выведи ТОЛЬКО эти 3 запроса через точку с запятой, без кавычек вокруг всего ответа:
{user_text[:700]}"""

    kw_res, _ = call_groq_safe(prompt_kw, max_tokens=70)
    queries = [q.strip() for q in kw_res.split(";") if len(q.strip()) > 1]
    if not queries or len(queries) < 2:
        queries = fallback_extract_keywords(user_text)

    search_display = " • ".join([f"`{q}`" for q in queries[:3]])
    await status_msg.edit_text(
        f"🌐 **Шаг 2/3:** Сканирую базы по точным операторам:\n{search_display}\n\n*Задействованы: синтаксис hh.ru, Хабр и Exact-Match веб-поиск...*",
        parse_mode=ParseMode.MARKDOWN
    )

    raw_vacancies = []
    # Запрос 1: в hh.ru с поддержкой синтаксиса NAME:
    raw_vacancies.extend(fetch_hh(queries[0], count=10))
    # Запрос 2: стек в Хабр и hh.ru
    if len(queries) > 1:
        raw_vacancies.extend(fetch_habr(queries[1], count=8))
        raw_vacancies.extend(fetch_hh(queries[1], count=6))
    # Запрос 3: веб-поиск по редкой цитате в кавычках
    if len(queries) > 2:
        raw_vacancies.extend(fetch_web_safe(queries[2], count=4))

    unique_vacancies = list({v["url"]: v for v in raw_vacancies if v.get("url")}.values())

    if not unique_vacancies:
        await status_msg.edit_text("❌ Не удалось найти открытые позиции по данным точным маркерам. Попробуйте передать текст с более развернутым описанием обязанностей.")
        return

    await status_msg.edit_text(
        f"📊 **Шаг 3/3:** Скоринг {len(unique_vacancies)} позиций и формирование карточек выхода на ЛПР...",
        parse_mode=ParseMode.MARKDOWN
    )

    compact_list = []
    for idx, v in enumerate(unique_vacancies[:14], 1):
        compact_list.append(
            f"[{idx}] Компания: {v['company']} | Роль: {v['title']} | Источник: {v['source']} | URL: {v['url']} | Детали: {v['desc']}"
        )
    vacancies_payload = "\n".join(compact_list)

    prompt_match = f"""Ты — ведущий OSINT-аналитик агентства IT-аутстаффинга.
ТЕКСТ ОБЕЗЛИЧЕННОЙ ЗАЯВКИ:
\"\"\"{user_text[:900]}\"\"\"

СПИСОК НАЙДЕННЫХ ПОЗИЦИЙ РАБОТОДАТЕЛЕЙ:
{vacancies_payload}

ЗАДАЧА:
Выбери ТОП-3 наиболее вероятных прямых заказчиков.
Оформи ответ СТРОГО по следующему формату карточек для каждой компании:

══════════════════════════════
🏢 **КОМПАНИЯ:** [Название компании]
🎯 **Соответствие стека:** [XX]%
🎲 **Вероятность статуса заказчика:** [🟢 Высокая (80-95%) / 🟡 Средняя (50-75%) / 🟠 Косвенная (30-45%)]
📌 **Позиция:** [Название должности]
💰 **Зарплата:** [Вилка или 'Не указана']
🔗 **Вакансия:** [Открыть страницу с вакансией](URL)
🏛 **Источник:** [hh.ru / Хабр Карьера / Веб-поиск]

🔍 **Факторы совпадения:**
• [1-2 конкретных факта: совпадение формулировок задач, архитектуры, инструментов]

💡 **Стратегия выхода для сейлза:**
• **К кому идти:** [Должность ЛПР: например, CTO / Head of Analytics / Team Lead]
• **Болевая точка:** [Какую текущую проблему в проекте закроет аутстафф]
• **Первый контакт:** [Краткая фраза первого сообщения]
══════════════════════════════
"""

    result_text, err = call_groq_safe(prompt_match, max_tokens=1900)

    if not result_text:
        await status_msg.edit_text(f"⚠️ Ошибка вызова Groq API: {err}")
        return

    # Автоматическое добавление ссылки на поиск ЛПР в LinkedIn/Google
    lines = result_text.split("\n")
    enhanced_lines = []
    current_company = ""

    for line in lines:
        if "🏢 **КОМПАНИЯ:**" in line:
            current_company = line.replace("🏢 **КОМПАНИЯ:**", "").strip()
            enhanced_lines.append(line)
        elif "• **К кому идти:**" in line and current_company:
            osint_url = build_lead_osint_url(current_company)
            enhanced_lines.append(line)
            enhanced_lines.append(f"• **Поиск контактов ЛПР:** [Найти профили в LinkedIn/Google]({osint_url})")
        else:
            enhanced_lines.append(line)

    final_header = "🎯 **ОТЧЁТ ПО ВЫЧИСЛЕНИЮ ПРЯМОГО ЗАКАЗЧИКА**\n\n"
    final_output = final_header + "\n".join(enhanced_lines)

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
