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
    "personnel", "talent", "staff", "headhunting", "подбор персонала",
    "ibs", "icl", "aston", "bell integrator", "neoflex", "epam", "reksoft", "andersen"
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

RUSSIAN_STOPWORDS = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то", "все", "она", "так",
    "его", "но", "да", "ты", "к", "у", "же", "вы", "за", "бы", "по", "только", "ее", "мне", "было",
    "вот", "от", "меня", "еще", "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг",
    "ли", "если", "уже", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь", "опять", "уж",
    "вам", "ведь", "там", "потом", "себя", "ничего", "ей", "может", "они", "тут", "где", "есть",
    "надо", "ней", "для", "мы", "тебя", "их", "чем", "была", "сам", "чтоб", "без", "будет", "будто",
    "про", "при", "опыт", "работа", "работы", "знание", "понимание", "обязанности", "требования"
}


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


def call_groq_safe(prompt: str, max_tokens: int = 1900) -> tuple[str, str]:
    models = ["llama-3.1-8b-instant", "openai/gpt-oss-20b", "qwen/qwen3.6-27b"]
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


def clean_html(raw_html: str) -> str:
    clean = re.sub(r"<[^>]+>", " ", raw_html)
    return " ".join(clean.split())


def extract_best_shingle(text: str, n_words: int = 4) -> str:
    lines = text.split("\n")
    cleaned_phrases = []
    for line in lines:
        words = re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9\-]+", line.lower())
        meaningful = [w for w in words if w not in RUSSIAN_STOPWORDS and len(w) > 2]
        if len(meaningful) >= n_words:
            cleaned_phrases.append(" ".join(meaningful[:n_words]))
    if cleaned_phrases:
        cleaned_phrases.sort(key=lambda p: sum(len(w) for w in p.split()), reverse=True)
        return f'"{cleaned_phrases[0]}"'
    return ""


def fallback_extract_keywords(text: str) -> list:
    found = []
    text_lower = text.lower()
    for tech in TECH_KEYWORDS:
        if re.search(r"\b" + re.escape(tech) + r"\b", text_lower):
            found.append(tech)
    if found:
        return [f"NAME:({found[0]})", " ".join(found[:3])]
    first_phrase = text.split("\n")[0][:30].strip()
    return [first_phrase if first_phrase else "Разработчик", "IT"]


def is_agency(company_name: str) -> bool:
    lower_name = company_name.lower()
    return any(w in lower_name for w in KNOWN_AGENCIES)


# 1. Поиск в hh.ru
def fetch_hh(query: str, count: int = 8) -> list:
    url = "https://api.hh.ru/vacancies"
    params = {"text": query, "area": 113, "per_page": count}
    headers = {"User-Agent": "MultiOSINTHunter/9.0"}
    jobs = []
    try:
        r = requests.get(url, params=params, headers=headers, timeout=4).json()
        for item in r.get("items", []):
            company = item.get("employer", {}).get("name", "Не указана")
            if is_agency(company):
                continue
            sal = item.get("salary")
            sal_str = f"{sal.get('from') or ''} - {sal.get('to') or ''} {sal.get('currency') or ''}".strip() if sal else "не указана"
            desc = f"{item.get('snippet', {}).get('requirement', '')} {item.get('snippet', {}).get('responsibility', '')}"
            jobs.append({
                "id": item.get("id"),
                "source": "hh.ru",
                "title": item.get("name"),
                "company": company,
                "salary": sal_str,
                "url": item.get("alternate_url"),
                "desc": clean_html(desc),
            })
    except Exception:
        pass
    return jobs


# Дозагрузка 100% текста hh.ru по ID
def fetch_hh_full_details(vacancy_id: str) -> str:
    url = f"https://api.hh.ru/vacancies/{vacancy_id}"
    headers = {"User-Agent": "MultiOSINTHunter/9.0"}
    try:
        r = requests.get(url, headers=headers, timeout=3).json()
        raw_desc = r.get("description", "")
        key_skills = " ".join([s.get("name", "") for s in r.get("key_skills", [])])
        return clean_html(f"{raw_desc} {key_skills}")[:600]
    except Exception:
        return ""


# 2. Хабр Карьера API
def fetch_habr(query: str, count: int = 8) -> list:
    clean_q = re.sub(r'[^\w\s\+\#\.\-]', ' ', query).strip()
    url = "https://career.habr.com/api/frontend/vacancies"
    params = {"q": clean_q, "per_page": count}
    headers = {"User-Agent": "MultiOSINTHunter/9.0"}
    jobs = []
    try:
        r = requests.get(url, params=params, headers=headers, timeout=4).json()
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
                "id": None,
                "source": "Хабр Карьера",
                "title": item.get("title"),
                "company": company,
                "salary": sal_str,
                "url": full_url,
                "desc": f"Стек: {skills}",
            })
    except Exception:
        pass
    return jobs


# 3. SuperJob API (Enterprise, Ритейл, Госсектор)
def fetch_superjob(query: str, count: int = 6) -> list:
    clean_q = re.sub(r'[^\w\s\+\#\.\-]', ' ', query).strip()
    url = "https://api.superjob.ru/2.0/vacancies/"
    params = {"keyword": clean_q, "count": count}
    headers = {
        "User-Agent": "MultiOSINTHunter/9.0",
        "X-Api-App-Id": "v3.r.137453308.2b27077a942fb8adcdba08488e08d669db756f70.9b015112521c7e90ef8c34fbc87e5b222fb2ea67"
    }
    jobs = []
    try:
        r = requests.get(url, params=params, headers=headers, timeout=4).json()
        for item in r.get("objects", []):
            client = item.get("client", {})
            company = client.get("title", "Не указана")
            if is_agency(company):
                continue
            p_from = item.get("payment_from", 0)
            p_to = item.get("payment_to", 0)
            cur = item.get("currency", "")
            sal_str = f"{p_from if p_from else ''} - {p_to if p_to else ''} {cur}".strip() if (p_from or p_to) else "не указана"
            desc = clean_html(item.get("candidat", "") or item.get("work", ""))
            jobs.append({
                "id": None,
                "source": "SuperJob",
                "title": item.get("profession", ""),
                "company": company,
                "salary": sal_str,
                "url": item.get("link", ""),
                "desc": desc[:350],
            })
    except Exception:
        pass
    return jobs


# 4. Telegram Dorking (site:t.me) — поиск прямых постов в каналах
def fetch_telegram_posts(query: str, count: int = 4) -> list:
    jobs = []
    try:
        from duckduckgo_search import DDGS
        ddgs = DDGS(timeout=4)
        search_query = f"site:t.me {query} вакансия"
        results = list(ddgs.text(search_query, max_results=count))
        for res in results:
            title = res.get("title", "")
            url = res.get("href", "")
            body = res.get("body", "")
            if "t.me/" in url:
                # Извлекаем имя канала
                channel_match = re.search(r"t\.me/([^/]+)", url)
                channel_name = f"@{channel_match.group(1)}" if channel_match else "Telegram канал"
                jobs.append({
                    "id": None,
                    "source": f"Telegram ({channel_name})",
                    "title": title[:50],
                    "company": channel_name,
                    "salary": "в посте",
                    "url": url,
                    "desc": body[:350],
                })
    except Exception:
        pass
    return jobs


def build_lead_osint_url(company_name: str) -> str:
    clean_company = re.sub(r'[\'\"«»@]', '', company_name).strip()
    target_role = "CTO OR \"Team Lead\" OR \"Head of Analytics\" OR \"Engineering Manager\""
    query = f'site:linkedin.com/in "{clean_company}" ({target_role})'
    return f"https://www.google.com/search?q={quote_plus(query)}"


@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "💼 **Multi-Source OSINT Lead Hunter**\n\n"
        "Отправьте обезличенный текст вакансии/заявки.\n"
        "Я просканирую **hh.ru, Хабр, SuperJob и Telegram-каналы (site:t.me)**, чтобы вычислить прямого работодателя.",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    user_text = message.text
    status_msg = await message.answer("⚡️ **Шаг 1/2:** Синтез маркеров и параллельный опрос hh.ru, Хабр, SuperJob, Telegram...")

    exact_shingle = extract_best_shingle(user_text)

    prompt_kw = f"""Выдели ключевую роль и технологическое ядро.
Сформируй ровно 2 запроса через точку с запятой:
1. Роль для hh.ru с оператором NAME:(...). Например: NAME:("Product Analyst") или NAME:("C#").
2. Стек из 2-3 инструментов. Например: 'ClickHouse Superset' или 'PostgreSQL EF Core'.
Выведи ТОЛЬКО 2 запроса через точку с запятой:
{user_text[:500]}"""

    kw_res, _ = call_groq_safe(prompt_kw, max_tokens=40)
    queries = [q.strip() for q in kw_res.split(";") if len(q.strip()) > 1]
    if not queries:
        queries = fallback_extract_keywords(user_text)

    # Параллельный сбор по всем базам
    tg_search_query = exact_shingle if exact_shingle else queries[1] if len(queries) > 1 else queries[0]
    
    tasks = [
        asyncio.to_thread(fetch_hh, queries[0], 8),
        asyncio.to_thread(fetch_habr, queries[1] if len(queries) > 1 else queries[0], 8),
        asyncio.to_thread(fetch_superjob, queries[1] if len(queries) > 1 else queries[0], 6),
        asyncio.to_thread(fetch_telegram_posts, tg_search_query, 4),
    ]

    results = await asyncio.gather(*tasks)
    raw_vacancies = [item for sublist in results for item in sublist]
    unique_vacancies = list({v["url"]: v for v in raw_vacancies if v.get("url")}.values())

    if not unique_vacancies:
        await status_msg.edit_text("❌ Не удалось обнаружить прямых позиций. Уточните описание стека.")
        return

    # Дозагрузка полных описаний hh.ru
    hh_candidates = [v for v in unique_vacancies if v.get("id") and v["source"] == "hh.ru"][:5]
    if hh_candidates:
        full_text_tasks = [asyncio.to_thread(fetch_hh_full_details, v["id"]) for v in hh_candidates]
        full_texts = await asyncio.gather(*full_text_tasks)
        for cand, full_desc in zip(hh_candidates, full_texts):
            if full_desc:
                cand["desc"] = full_desc

    await status_msg.edit_text(f"🧠 **Шаг 2/2:** OSINT-сопоставление {len(unique_vacancies)} позиций из 4 источников...")

    compact_list = []
    for idx, v in enumerate(unique_vacancies[:14], 1):
        compact_list.append(
            f"ID {idx}: {v['company']} | {v['title']} | Источник: {v['source']} | URL: {v['url']} | Детали: {v['desc'][:400]}"
        )
    vacancies_payload = "\n".join(compact_list)

    prompt_match = f"""Ты — ведущий OSINT-аналитик по деанонимизации IT-заказчиков в аутстаффинге.
Агентство прислало обезличенный запрос. Твоя задача — вычислить ТОП-3 прямых работодателей, у которых взят этот проект.

ОРИГИНАЛЬНЫЙ ОБЕЗЛИЧЕННЫЙ ЗАПРОС:
\"\"\"{user_text[:800]}\"\"\"

НАЙДЕННЫЕ ВАКАНСИИ И ПОСТЫ (hh.ru, Хабр, SuperJob, Telegram):
{vacancies_payload}

ПРАВИЛА ОЦЕНКИ:
1. Ищи совпадение уникальных задач, формулировок и архитектуры проектов.
2. Если совпал пост в Telegram или SuperJob — приоритезируй его как прямой первоисточник.
3. Отсекай компании с чужим стеком.

ОФОРМИ СТРОГО ПО ШАБЛОНУ:
══════════════════════════════
🏢 **КОМПАНИЯ:** [Название компании или канал первоисточника]
🎯 **Соответствие стека:** [XX]%
🎲 **Вероятность статуса заказчика:** [🟢 Высокая (80-95%) / 🟡 Средняя (50-75%) / 🟠 Косвенная (30-45%)]
📌 **Позиция:** [Название должности]
💰 **Зарплата:** [Вилка или 'Не указана']
🔗 **Вакансия/Пост:** [Открыть источник](URL)
🏛 **Источник:** [hh.ru / Хабр Карьера / SuperJob / Telegram]

🔍 **Факторы совпадения:**
• [1-2 конкретных маркера: совпадение редких задач, терминов или архитектуры]

💡 **Стратегия выхода для сейлза:**
• **К кому идти:** [Должность ЛПР или контакт из Telegram]
• **Болевая точка:** [Какую текущую проблему в проекте закроет аутстафф]
• **Первый контакт:** [Краткая фраза первого сообщения]
══════════════════════════════
"""

    result_text, err = call_groq_safe(prompt_match, max_tokens=1800)

    if not result_text:
        await status_msg.edit_text(f"⚠️ Ошибка генерации: {err}")
        return

    lines = result_text.split("\n")
    enhanced_lines = []
    current_company = ""

    for line in lines:
        if "🏢 **КОМПАНИЯ:**" in line:
            current_company = line.replace("🏢 **КОМПАНИЯ:**", "").strip()
            enhanced_lines.append(line)
        elif "• **К кому идти:**" in line and current_company and not current_company.startswith("@"):
            osint_url = build_lead_osint_url(current_company)
            enhanced_lines.append(line)
            enhanced_lines.append(f"• **Поиск контактов ЛПР:** [Найти профили в LinkedIn/Google]({osint_url})")
        else:
            enhanced_lines.append(line)

    final_header = "🎯 **ОТЧЁТ ПО ДЕАНОНИМИЗАЦИИ ПРЯМОГО ЗАКАЗЧИКА**\n\n"
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
