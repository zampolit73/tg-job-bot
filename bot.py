import asyncio
import os
import re
import threading
from urllib.parse import quote_plus
from http.server import HTTPServer, BaseHTTPRequestHandler
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.enums import ParseMode
from groq import AsyncGroq
from telethon import TelegramClient
from telethon.sessions import StringSession

# ----------------- КОНФИГУРАЦИЯ -----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8982024680:AAGSIE8AbyboYoG1HcxLmI9-7ljX2JbTk7s")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "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")
SUPERJOB_KEY = "v3.r.137453308.2b27077a942fb8adcdba08488e08d669db756f70.9b015112521c7e90ef8c34fbc87e5b222fb2ea67"

# Данные авторизации Telethon
TG_API_ID = 34645565
TG_API_HASH = "95a795c728a02edb5ed9bbe36289454d"
TG_SESSION_STRING = "1ApWapzMBuyXchwXzM24tiq9__tlPtWNNFsgnWBO-LAu6w4mz6YLBZ3Vz45ToetY0XPXoZNacT90-QLNUMazCumsgEiqaXdnS9tJNXGCIqyK5fWEzkJcVCivWmNllXbjV9FlHYvEwqHFEXTPw0Cpi7HTHiiZpyj_XD2jwkPPr9r9eVlYNOizF3YXEnYEE1CFPgZvh3p2H1DeixKdZzFmxmuYWvhB9QJahw1Mn1JNEgEFriRKoGHcyDt4CKFC9Q7p8xZ3cy4OeMyYleClP8YzT6IWZ_8pXCb_HfUu2L_phhy46VFNI2A5gtbry-ktXwmJrFWnqLXpAzTSCfIJACwKDvC6REtmOxN4="

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
groq_client = AsyncGroq(api_key=GROQ_API_KEY.strip())
telethon_client = TelegramClient(StringSession(TG_SESSION_STRING), TG_API_ID, TG_API_HASH)

HTML_TAG_RE = re.compile(r"<[^>]+>")
CLEAN_NAME_RE = re.compile(r'[\'\"«»@]')
CLEAN_QUERY_RE = re.compile(r'[^\w\s\+\#\.\-]')

KNOWN_AGENCIES = (
    "кадровое", "рекрутинг", "recruitment", "staffing", "hr", "agency",
    "агентство", "selecty", "ancor", "анкор", "кадры", "outstaff", "аутстафф",
    "personnel", "talent", "staff", "headhunting", "подбор персонала",
    "ibs", "icl", "aston", "bell integrator", "neoflex", "epam", "reksoft", "andersen"
)

TECH_KEYWORDS = (
    "c#", ".net", "asp.net", "python", "django", "fastapi", "flask",
    "java", "spring", "kotlin", "golang", "go", "php", "laravel",
    "javascript", "typescript", "react", "vue", "angular", "node.js",
    "devops", "kubernetes", "k8s", "docker", "ansible", "terraform",
    "qa", "тестировщик", "automation", "autotest", "selenium", "playwright",
    "data science", "ml", "системный аналитик", "бизнес-аналитик", "product manager",
    "ios", "swift", "android", "flutter", "1c", "1с", "sql", "clickhouse", "dwh"
)

TARGET_TG_CHANNELS = [
    "normrabota", "it_jobs", "devops_jobs", "job_finder_dev",
    "qa_jobs", "jvmjobs", "forpython", "devjobs"
]

# ----------------- HEALTH SERVER -----------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


# ----------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ -----------------
def clean_html(raw_html: str) -> str:
    return " ".join(HTML_TAG_RE.sub(" ", raw_html).split())


def is_agency(company_name: str) -> bool:
    lower_name = company_name.lower()
    return any(w in lower_name for w in KNOWN_AGENCIES)


def build_lead_osint_url(company_name: str) -> str:
    clean_company = CLEAN_NAME_RE.sub("", company_name).strip()
    target_role = 'CTO OR "Team Lead" OR "Head of Engineering" OR "Head of Infrastructure"'
    query = f'site:linkedin.com/in "{clean_company}" ({target_role})'
    return f"https://www.google.com/search?q={quote_plus(query)}"


def fallback_extract_keywords(text: str) -> list:
    text_lower = text.lower()
    found = [tech for tech in TECH_KEYWORDS if re.search(r"\b" + re.escape(tech) + r"\b", text_lower)]
    if found:
        return [f"NAME:({found[0]})", " ".join(found[:3]), found[0]]
    first_phrase = text.split("\n")[0][:30].strip()
    return [first_phrase if first_phrase else "IT Вакансия", "Kubernetes DevOps", "CI/CD"]


# ----------------- GROQ CLIENT (АКТУАЛЬНЫЕ МОДЕЛИ) -----------------
async def call_groq_async(prompt: str, max_tokens: int = 1500) -> tuple[str, str]:
    models = ("openai/gpt-oss-20b", "openai/gpt-oss-120b", "llama-3.3-70b-versatile")
    last_err = ""
    for model_name in models:
        try:
            res = await asyncio.wait_for(
                groq_client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=max_tokens
                ),
                timeout=9.0
            )
            content = res.choices[0].message.content
            if content and content.strip():
                return content, ""
        except Exception as e:
            last_err = str(e)
            continue
    return "", last_err


# ----------------- АСИНХРОННЫЕ ПАРСЕРЫ -----------------
async def fetch_hh(client: httpx.AsyncClient, query: str, count: int = 8) -> list:
    url = "https://api.hh.ru/vacancies"
    params = {"text": query, "area": 113, "per_page": count}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = await client.get(url, params=params, headers=headers, timeout=2.5)
        data = r.json()
        jobs = []
        for item in data.get("items", []):
            employer = item.get("employer", {})
            company = employer.get("name", "Не указана")
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
        return jobs
    except Exception:
        return []


async def fetch_hh_full_details(client: httpx.AsyncClient, vacancy_id: str) -> str:
    url = f"https://api.hh.ru/vacancies/{vacancy_id}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = await client.get(url, headers=headers, timeout=1.8)
        data = r.json()
        raw_desc = data.get("description", "")
        key_skills = " ".join([s.get("name", "") for s in data.get("key_skills", [])])
        return clean_html(f"{raw_desc} {key_skills}")[:350]
    except Exception:
        return ""


async def fetch_habr(client: httpx.AsyncClient, query: str, count: int = 8) -> list:
    clean_q = CLEAN_QUERY_RE.sub(" ", query).strip()
    url = "https://career.habr.com/api/frontend/vacancies"
    params = {"q": clean_q, "per_page": count}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = await client.get(url, params=params, headers=headers, timeout=2.5)
        data = r.json()
        jobs = []
        for item in data.get("list", []):
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
        return jobs
    except Exception:
        return []


async def fetch_superjob(client: httpx.AsyncClient, query: str, count: int = 5) -> list:
    words = [w for w in CLEAN_QUERY_RE.sub(" ", query).split() if len(w) > 2][:2]
    clean_q = " ".join(words)
    if not clean_q:
        return []
    url = "https://api.superjob.ru/2.0/vacancies/"
    params = {"keyword": clean_q, "count": count}
    headers = {"User-Agent": "Mozilla/5.0", "X-Api-App-Id": SUPERJOB_KEY}
    try:
        r = await client.get(url, params=params, headers=headers, timeout=2.5)
        data = r.json()
        jobs = []
        for item in data.get("objects", []):
            company = item.get("client", {}).get("title", "Не указана")
            if is_agency(company):
                continue
            p_from, p_to = item.get("payment_from", 0), item.get("payment_to", 0)
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
                "desc": desc[:250],
            })
        return jobs
    except Exception:
        return []


# ----------------- TELETHON SEARCH ПО КАНАЛАМ -----------------
async def search_telegram_native(search_term: str) -> list:
    jobs = []
    if not telethon_client.is_connected():
        return jobs

    clean_term = search_term.strip()
    if not clean_term:
        return jobs

    for channel in TARGET_TG_CHANNELS[:4]:
        try:
            async for message in telethon_client.iter_messages(channel, search=clean_term, limit=2):
                if message.text:
                    post_text = clean_html(message.text)
                    post_url = f"https://t.me/{channel}/{message.id}"
                    company_match = re.search(r"(?:компания|проект|заказчик|в команду):\s*([A-Za-zА-Яа-я0-9_\-\s]{3,30})", post_text, re.IGNORECASE)
                    company = company_match.group(1).strip() if company_match else f"@{channel}"

                    jobs.append({
                        "id": None,
                        "source": f"Telegram (@{channel})",
                        "title": post_text[:40] + "...",
                        "company": company,
                        "salary": "в посте",
                        "url": post_url,
                        "desc": post_text[:250],
                    })
        except Exception:
            continue
    return jobs


# ----------------- ОБРАБОТЧИКИ СООБЩЕНИЙ -----------------
@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "💼 **Multi-Source OSINT Lead Hunter (Pro Edition)**\n\n"
        "Отправьте обезличенный бриф. Бот выполнит параллельный скоринг по **hh.ru, Хабр, SuperJob и Telegram-каналам**.",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    user_text = message.text
    status_msg = await message.answer("⚡️ Сканирую базы (hh.ru, Хабр, SuperJob, Telegram)...")

    prompt_kw = f"""Ты — OSINT-аналитик IT-рынка. Сформируй ровно 3 запроса через точку с запятой в одну строку:
1. Роль для hh.ru строго с оператором NAME:(...). Пример: NAME:("DevOps") или NAME:("Backend").
2. Связка из 2-3 ключевых технологий через пробел. Пример: 'Kubernetes Helm PostgreSQL'.
3. Самый специфичный термин стека или продукта для точного поиска (1-2 слова). Пример: 'Kafka' или 'OnPrem'.
Текст:
{user_text[:500]}"""

    kw_res, _ = await call_groq_async(prompt_kw, max_tokens=50)
    queries = [q.strip() for q in kw_res.split(";") if len(q.strip()) > 1]
    if not queries or len(queries) < 2:
        queries = fallback_extract_keywords(user_text)

    role_query = queries[0]
    stack_query = queries[1]
    tg_search_term = queries[2] if len(queries) > 2 else queries[1].split()[0]

    async with httpx.AsyncClient(follow_redirects=True) as http_client:
        tasks = [
            fetch_hh(http_client, role_query, 8),
            fetch_habr(http_client, stack_query, 8),
            fetch_superjob(http_client, stack_query, 5),
            search_telegram_native(tg_search_term),
        ]

        try:
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=4.0)
        except Exception:
            results = []

        raw_vacancies = [item for sublist in results for item in sublist]
        unique_vacancies = list({v["url"]: v for v in raw_vacancies if v.get("url")}.values())

        if not unique_vacancies:
            await status_msg.edit_text("❌ Вакансии не найдены. Попробуйте передать текст с более конкретным описанием стека.")
            return

        hh_candidates = [v for v in unique_vacancies if v.get("id") and v["source"] == "hh.ru"][:4]
        if hh_candidates:
            try:
                full_texts = await asyncio.wait_for(
                    asyncio.gather(*[fetch_hh_full_details(http_client, v["id"]) for v in hh_candidates]),
                    timeout=1.8
                )
                for cand, full_desc in zip(hh_candidates, full_texts):
                    if full_desc:
                        cand["desc"] = full_desc
            except Exception:
                pass

    await status_msg.edit_text(f"🧠 Скоринг {len(unique_vacancies)} позиций через Groq LPU...")

    compact_list = [
        f"ID {idx}: {v['company']} | {v['title']} | Источник: {v['source']} | URL: {v['url']} | Детали: {v['desc'][:260]}"
        for idx, v in enumerate(unique_vacancies[:14], 1)
    ]
    vacancies_payload = "\n".join(compact_list)

    prompt_match = f"""Ты — ведущий OSINT-аналитик по деанонимизации IT-заказчиков в аутстаффинге.
Агентство скопировало бриф прямого заказчика. Твоя цель — вычислить ТОП-3 работодателей, у которых взят этот проект.

ОРИГИНАЛЬНЫЙ БРИФ:
\"\"\"{user_text[:700]}\"\"\"

НАЙДЕННЫЕ ВАКАНСИИ И ПУБЛИКАЦИИ:
{vacancies_payload}

ПРАВИЛА ОЦЕНКИ:
1. Базовые технологии (Linux, Git, Docker, SQL) не дают права ставить высокий скор (не выше 40%).
2. Ставь 🟢 Высокую вероятность (80-95%) ТОЛЬКО за совпадение архитектурных задач, специфических окружений или редких связок.
3. Отсекай компании с непрофильным стеком.

ОФОРМИ СТРОГО ПО ШАБЛОНУ:
══════════════════════════════
🏢 **КОМПАНИЯ:** [Название компании или канал]
🎯 **Соответствие стека:** [XX]%
🎲 **Вероятность статуса заказчика:** [🟢 Высокая (80-95%) / 🟡 Средняя (50-75%) / 🟠 Косвенная (30-45%)]
📌 **Позиция:** [Название должности]
💰 **Зарплата:** [Вилка или 'Не указана']
🔗 **Вакансия/Пост:** [Открыть источник](URL)
🏛 **Источник:** [hh.ru / Хабр / SuperJob / Telegram]

🔍 **Факторы совпадения:**
• [Что конкретно совпало: редкие технологии, продуктовые задачи или дословные фразы]

💡 **Стратегия выхода для сейлза:**
• **К кому идти:** [Точная роль ЛПР: Head of Infrastructure, CTO, Lead DevOps]
• **Болевая точка:** [Какая острая проектная боль видна в тексте]
• **Первый контакт:** [Готовый 1-2 предложения хук для сообщения в Telegram/LinkedIn]
══════════════════════════════
"""

    result_text, err = await call_groq_async(prompt_match, max_tokens=1500)
    if not result_text:
        await status_msg.edit_text(f"⚠️ Ошибка генерации: {err}")
        return

    enhanced_lines = []
    current_company = ""
    for line in result_text.splitlines():
        if "🏢 **КОМПАНИЯ:**" in line:
            current_company = line.replace("🏢 **КОМПАНИЯ:**", "").strip()
            enhanced_lines.append(line)
        elif "• **К кому идти:**" in line and current_company and not current_company.startswith("@"):
            osint_url = build_lead_osint_url(current_company)
            enhanced_lines.append(line)
            enhanced_lines.append(f"• **Поиск контактов ЛПР:** [Найти профили в LinkedIn/Google]({osint_url})")
        else:
            enhanced_lines.append(line)

    final_output = "🎯 **ОТЧЁТ ПО ДЕАНОНИМИЗАЦИИ ПРЯМОГО ЗАКАЗЧИКА**\n\n" + "\n".join(enhanced_lines)

    if len(final_output) > 4000:
        parts = [final_output[i:i+4000] for i in range(0, len(final_output), 4000)]
        await status_msg.edit_text(parts[0], parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        for p in parts[1:]:
            await message.answer(p, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    else:
        await status_msg.edit_text(final_output, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


async def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    try:
        await telethon_client.start()
    except Exception:
        pass
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
