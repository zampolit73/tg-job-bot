import asyncio
import os
import re
import threading
from urllib.parse import quote_plus
from http.server import HTTPServer, BaseHTTPRequestHandler
import defusedxml.ElementTree as ET
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.enums import ParseMode
from groq import AsyncGroq
from telethon import TelegramClient
from telethon.sessions import StringSession

# ----------------- КОНФИГУРАЦИЯ ЧЕРЕЗ ENV -----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPERJOB_KEY = os.getenv("SUPERJOB_KEY")

TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_SESSION_STRING = os.getenv("TG_SESSION_STRING", "")

if not TELEGRAM_BOT_TOKEN or not GROQ_API_KEY:
    raise ValueError("Критические переменные окружения TELEGRAM_BOT_TOKEN или GROQ_API_KEY не заданы!")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
groq_client = AsyncGroq(api_key=GROQ_API_KEY.strip())

telethon_client = None
if TG_API_ID and TG_API_HASH and TG_SESSION_STRING:
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


def extract_tech_set(text: str) -> set:
    text_lower = text.lower()
    return {tech for tech in TECH_KEYWORDS if re.search(r"\b" + re.escape(tech) + r"\b", text_lower)}


def calculate_jaccard_similarity(set_a: set, set_b: set) -> float:
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a.intersection(set_b))
    union = len(set_a.union(set_b))
    return intersection / union if union > 0 else 0.0


# ----------------- GROQ CLIENT -----------------
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


# ----------------- ОТКРЫТЫЕ ИСТОЧНИКИ ВАКАНСИЙ -----------------

# 1. hh.ru через открытый RSS-поток (без API-токенов)
async def fetch_hh_rss(client: httpx.AsyncClient, query: str, count: int = 8) -> list:
    url = f"https://hh.ru/rss/vacancies?text={quote_plus(query)}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = await client.get(url, headers=headers, timeout=3.0)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
        jobs = []
        for item in root.findall("./channel/item")[:count]:
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            description = clean_html(item.findtext("description", ""))
            
            # В заголовке RSS hh.ru обычно: "Вакансия (Компания)"
            company_match = re.search(r"\((.*?)\)$", title)
            company = company_match.group(1).strip() if company_match else "Не указана"
            if is_agency(company):
                continue
            
            clean_title = re.sub(r"\s*\(.*?\)$", "", title).strip()
            jobs.append({
                "source": "hh.ru (RSS)",
                "title": clean_title,
                "company": company,
                "salary": "в описании",
                "url": link,
                "desc": description[:300]
            })
        return jobs
    except Exception:
        return []


# 2. Хабр Карьера (публичный JSON API)
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
                "source": "Хабр Карьера",
                "title": item.get("title", ""),
                "company": company,
                "salary": sal_str,
                "url": full_url,
                "desc": f"Стек: {skills}",
            })
        return jobs
    except Exception:
        return []


# 3. «Работа России» (Trudvsem OpenData API - госреестр прямых работодателей)
async def fetch_trudvsem(client: httpx.AsyncClient, query: str, count: int = 6) -> list:
    clean_q = CLEAN_QUERY_RE.sub(" ", query).strip()
    url = f"http://opendata.trudvsem.ru/api/v1/vacancies"
    params = {"text": clean_q, "limit": count}
    try:
        r = await client.get(url, params=params, timeout=3.0)
        data = r.json()
        vacancies = data.get("results", {}).get("vacancies", [])
        jobs = []
        for v in vacancies:
            vac = v.get("vacancy", {})
            company = vac.get("company", {}).get("name", "Не указана")
            if is_agency(company):
                continue
            sal_min = vac.get("salary_min", 0)
            sal_max = vac.get("salary_max", 0)
            sal_str = f"{sal_min} - {sal_max} руб." if (sal_min or sal_max) else "не указана"
            duty = clean_html(vac.get("duty", ""))
            requirement = clean_html(vac.get("requirement", {}).get("qualification", ""))
            jobs.append({
                "source": "Работа России",
                "title": vac.get("job-name", ""),
                "company": company,
                "salary": sal_str,
                "url": vac.get("vac_url", "https://trudvsem.ru"),
                "desc": f"{requirement} {duty}"[:300]
            })
        return jobs
    except Exception:
        return []


# 4. SuperJob API
async def fetch_superjob(client: httpx.AsyncClient, query: str, count: int = 5) -> list:
    if not SUPERJOB_KEY:
        return []
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


# 5. Telegram-каналы (Telethon)
async def _scan_single_channel(channel: str, search_term: str) -> list:
    results = []
    try:
        async for message in telethon_client.iter_messages(channel, search=search_term, limit=2):
            if message.text:
                post_text = clean_html(message.text)
                post_url = f"https://t.me/{channel}/{message.id}"
                company_match = re.search(r"(?:компания|проект|заказчик|в команду):\s*([A-Za-zА-Яа-я0-9_\-\s]{3,30})", post_text, re.IGNORECASE)
                company = company_match.group(1).strip() if company_match else f"@{channel}"
                results.append({
                    "source": f"Telegram (@{channel})",
                    "title": post_text[:40] + "...",
                    "company": company,
                    "salary": "в посте",
                    "url": post_url,
                    "desc": post_text[:250]
                })
    except Exception:
        pass
    return results


async def search_telegram_native(search_term: str) -> list:
    if not telethon_client or not telethon_client.is_connected():
        return []
    clean_term = search_term.strip()
    if not clean_term:
        return []
    tasks = [_scan_single_channel(ch, clean_term) for ch in TARGET_TG_CHANNELS[:5]]
    results = await asyncio.gather(*tasks)
    return [item for sublist in results for item in sublist]


# ----------------- ОБРАБОТЧИКИ СООБЩЕНИЙ -----------------
@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "💼 **Multi-Source OSINT Lead Hunter**\n\n"
        "Отправьте текст брифа. Бот проверит базы **Хабр Карьеры, Работа России, SuperJob, hh.ru (RSS) и Telegram-каналы**.",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    user_text = message.text
    status_msg = await message.answer("⚡️ Извлекаю параметры поиска...")

    prompt_kw = f"""Ты — OSINT-аналитик IT-рынка. Сформируй ровно 3 поисковых запроса через точку с запятой в одну строку:
1. Профильная должность (без кавычек). Пример: DevOps инженер
2. 2-3 ключевых технологии через пробел. Пример: Kubernetes Helm PostgreSQL
3. 1 редкий маркер стека/задачи. Пример: OnPrem или ClickHouse
Текст:
{user_text[:600]}"""

    kw_res, _ = await call_groq_async(prompt_kw, max_tokens=50)
    queries = [q.strip() for q in kw_res.split(";") if len(q.strip()) > 1]
    
    role_query = queries[0] if len(queries) > 0 else "Разработчик"
    stack_query = queries[1] if len(queries) > 1 else "Kubernetes"
    rare_term = queries[2] if len(queries) > 2 else stack_query.split()[0]

    await status_msg.edit_text("⚡️ Опрашиваю открытые базы (Хабр, Работа России, SuperJob, hh RSS, TG)...")

    async with httpx.AsyncClient(follow_redirects=True) as http_client:
        tasks = [
            fetch_habr(http_client, stack_query, 8),
            fetch_trudvsem(http_client, role_query, 6),
            fetch_superjob(http_client, stack_query, 5),
            fetch_hh_rss(http_client, f"{role_query} {rare_term}", 6),
            search_telegram_native(rare_term),
        ]

        try:
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=4.5)
        except Exception:
            results = []

    raw_vacancies = [item for sublist in results for item in sublist]
    unique_vacancies = list({v["url"]: v for v in raw_vacancies if v.get("url")}.values())

    if not unique_vacancies:
        await status_msg.edit_text("❌ По открытым базам совпадений не найдено. Попробуйте уточнить ключевой стек.")
        return

    # Локальный пре-скоринг по пересечению терминов
    source_tech_set = extract_tech_set(user_text)
    for vac in unique_vacancies:
        vac_tech_set = extract_tech_set(vac["title"] + " " + vac["desc"])
        vac["rank_score"] = calculate_jaccard_similarity(source_tech_set, vac_tech_set)

    unique_vacancies.sort(key=lambda x: x["rank_score"], reverse=True)
    top_vacancies = unique_vacancies[:8]

    await status_msg.edit_text(f"🧠 Скоринг ТОП-{len(top_vacancies)} позиций через Groq LPU...")

    compact_list = [
        f"ID {idx}: {v['company']} | {v['title']} | Источник: {v['source']} | URL: {v['url']} | Детали: {v['desc'][:240]}"
        for idx, v in enumerate(top_vacancies, 1)
    ]
    vacancies_payload = "\n".join(compact_list)

    prompt_match = f"""Ты — ведущий OSINT-аналитик по деанонимизации IT-заказчиков.
Агентство скопировало бриф прямого работодателя. Вычисли ТОП-3 компаний, у которых взят этот проект.

ОРИГИНАЛЬНЫЙ БРИФ:
\"\"\"{user_text[:700]}\"\"\"

НАЙДЕННЫЕ ПОЗИЦИИ:
{vacancies_payload}

ПРАВИЛА ОЦЕНКИ:
1. Базовые навыки (Git, Linux, Docker) дают скор не выше 40%.
2. 🟢 Высокая вероятность (80-95%) — только за редкие связки, специфику задач и окружения.
3. Отсекай кадровые агентства.

ОФОРМИ СТРОГО ПО ШАБЛОНУ:
══════════════════════════════
🏢 **КОМПАНИЯ:** [Название компании или канала]
🎯 **Соответствие стека:** [XX]%
🎲 **Вероятность статуса заказчика:** [🟢 Высокая (80-95%) / 🟡 Средняя (50-75%) / 🟠 Косвенная (30-45%)]
📌 **Позиция:** [Название должности]
💰 **Зарплата:** [Вилка или 'Не указана']
🔗 **Ссылка:** [Открыть источник](URL)
🏛 **Источник:** [Хабр / Работа России / hh.ru / SuperJob / Telegram]

🔍 **Факторы совпадения:**
• [Что конкретно совпало: технологии, задачи проекта или окружение]

💡 **Стратегия выхода для сейлза:**
• **К кому идти:** [Роль ЛПР: CTO, Head of QA, Lead DevOps]
• **Болевая точка:** [Какая острая инженерная задача описана]
• **Первый контакт:** [Готовый короткий хук для сообщения в LinkedIn/TG]
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
    if telethon_client:
        try:
            await telethon_client.start()
        except Exception:
            pass
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
