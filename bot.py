import asyncio
import json
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
from aiogram.exceptions import TelegramAPIError
from groq import AsyncGroq
from telethon import TelegramClient
from telethon.sessions import StringSession

# ----------------- КОНФИГУРАЦИЯ ЧЕРЕЗ ENV -----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPERJOB_KEY = os.getenv("SUPERJOB_KEY")

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID", "10092ea56d2504c84")

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

TARGET_TG_CHANNELS = [
    "normrabota", "it_jobs", "devops_jobs", "job_finder_dev",
    "qa_jobs", "jvmjobs", "forpython", "devjobs"
]

google_quota_exhausted = False

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


def build_lead_osint_url(company_name: str, target_role: str = "CTO") -> str:
    clean_company = CLEAN_NAME_RE.sub("", company_name).strip()
    query = f'site:linkedin.com/in "{clean_company}" ({target_role} OR "Head of Engineering" OR "Team Lead")'
    return f"https://www.google.com/search?q={quote_plus(query)}"


# Безопасное обновление текста статуса без зависаний
async def safe_edit_status(msg: Message, text: str):
    try:
        await msg.edit_text(text)
    except TelegramAPIError:
        pass


# ----------------- GROQ CLIENT -----------------
async def call_groq_async(prompt: str, max_tokens: int = 1500, json_mode: bool = False) -> tuple[str, str]:
    models = ("openai/gpt-oss-20b", "openai/gpt-oss-120b", "llama-3.3-70b-versatile")
    last_err = ""
    for model_name in models:
        try:
            kwargs = {
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
                "max_tokens": max_tokens,
            }
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}

            res = await asyncio.wait_for(
                groq_client.chat.completions.create(**kwargs),
                timeout=9.0
            )
            content = res.choices[0].message.content
            if content and content.strip():
                return content, ""
        except Exception as e:
            last_err = str(e)
            continue
    return "", last_err


# ----------------- ИСТОЧНИКИ ДАННЫХ -----------------
async def search_google_custom(client: httpx.AsyncClient, query: str, count: int = 6) -> list:
    global google_quota_exhausted
    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID or google_quota_exhausted:
        return []
    
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CSE_ID,
        "q": query,
        "num": min(count, 10),
        "gl": "ru",
        "hl": "ru"
    }
    
    try:
        r = await client.get(url, params=params, timeout=3.0)
        if r.status_code in (429, 403):
            google_quota_exhausted = True
            return []
        if r.status_code != 200:
            return []
            
        items = r.json().get("items", [])
        jobs = []
        for item in items:
            title = item.get("title", "")
            link = item.get("link", "")
            snippet = clean_html(item.get("snippet", ""))

            company_match = re.search(r"(?:в компании|—)\s+([^|\-—\(\)]+)", title, re.IGNORECASE)
            company = company_match.group(1).strip() if company_match else "Прямой работодатель"

            if is_agency(company):
                continue

            jobs.append({
                "source": "Google X-Ray",
                "title": title[:70],
                "company": company,
                "salary": "в описании",
                "url": link,
                "desc": snippet[:350]
            })
        return jobs
    except Exception:
        return []


async def fetch_habr(client: httpx.AsyncClient, query: str, count: int = 8) -> list:
    clean_q = CLEAN_QUERY_RE.sub(" ", query).strip()
    url = "https://career.habr.com/api/frontend/vacancies"
    params = {"q": clean_q, "per_page": count}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = await client.get(url, params=params, headers=headers, timeout=2.5)
        if r.status_code != 200:
            return []
        jobs = []
        for item in r.json().get("list", []):
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


async def fetch_trudvsem(client: httpx.AsyncClient, query: str, count: int = 5) -> list:
    clean_q = CLEAN_QUERY_RE.sub(" ", query).strip()
    url = "http://opendata.trudvsem.ru/api/v1/vacancies"
    params = {"text": clean_q, "limit": count}
    try:
        r = await client.get(url, params=params, timeout=2.5)
        if r.status_code != 200:
            return []
        vacancies = r.json().get("results", {}).get("vacancies", [])
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
                "desc": f"{requirement} {duty}"[:350]
            })
        return jobs
    except Exception:
        return []


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
        if r.status_code != 200:
            return []
        jobs = []
        for item in r.json().get("objects", []):
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
                "desc": desc[:300]
            })
        return jobs
    except Exception:
        return []


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
                    "desc": post_text[:300]
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
    try:
        tasks = [_scan_single_channel(ch, clean_term) for ch in TARGET_TG_CHANNELS[:4]]
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3.0)
        return [item for sub in results if isinstance(sub, list) for item in sub]
    except Exception:
        return []


# ----------------- ХЭНДЛЕРЫ -----------------
@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "💼 **Multi-Source OSINT Lead Hunter**\n\n"
        "Отправьте бриф. Бот выполнит каскадный поиск по открытым базам и Google X-Ray.",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    global google_quota_exhausted
    user_text = message.text
    status_msg = await message.answer("⚡️ [1/3] Извлечение сущностей проекта...")

    # Шаг 1: Извлечение структурированного профиля
    prompt_extract = f"""Ты — senior технический рекрутер и OSINT-аналитик.
Разбери бриф на сущности и верни JSON:
{{
  "role": "название позиции",
  "domain": "домен (Fintech, E-commerce, Retail или Общий)",
  "infra_type": "тип среды (OnPrem, Cloud, Bare-metal или Не указан)",
  "must_have": ["список", "редких", "технологий"],
  "core_challenge": "главная задача (1 предложение)",
  "google_query": "3-4 ключевых слова (роль + 2 технологии)",
  "ngram_phrase": "дословная фраза из 3-4 слов из обязанностей"
}}
БРИФ:
\"\"\"{user_text[:900]}\"\"\""""

    extract_raw, _ = await call_groq_async(prompt_extract, max_tokens=300, json_mode=True)
    try:
        brief_profile = json.loads(extract_raw)
    except Exception:
        brief_profile = {
            "role": "Разработчик",
            "domain": "Общий",
            "infra_type": "Не указан",
            "must_have": [],
            "core_challenge": "Разработка сервисов",
            "google_query": user_text.split()[:3],
            "ngram_phrase": ""
        }

    role_query = brief_profile.get("role", "Разработчик")
    google_q = brief_profile.get("google_query", "Разработчик")
    rare_techs = brief_profile.get("must_have", [])
    rare_term = rare_techs[0] if rare_techs else "Backend"
    ngram_phrase = brief_profile.get("ngram_phrase", "")

    mode_info = "Хабр, SuperJob, Работа России, TG" if google_quota_exhausted else "Google CSE, Хабр, SuperJob, TG"
    await safe_edit_status(status_msg, f"🔍 [2/3] Поиск кандидатов через [{mode_info}]...")

    # Шаг 2: Параллельный опрос с защитой от зависания
    async with httpx.AsyncClient(follow_redirects=True) as http_client:
        tasks = [
            fetch_habr(http_client, rare_term, 6),
            fetch_trudvsem(http_client, role_query, 5),
            fetch_superjob(http_client, rare_term, 5),
            search_telegram_native(rare_term),
        ]

        if not google_quota_exhausted and GOOGLE_API_KEY:
            tasks.append(search_google_custom(http_client, google_q, count=6))
            if len(ngram_phrase.split()) >= 3:
                tasks.append(search_google_custom(http_client, f'"{ngram_phrase}"', count=3))

        try:
            # Жесткий общий таймаут 4 секунды с возвратом частичных результатов
            raw_results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=4.0)
            valid_results = [item for sub in raw_results if isinstance(sub, list) for item in sub]
        except Exception:
            valid_results = []

    unique_vacancies = list({v["url"]: v for v in valid_results if v.get("url")}.values())

    if not unique_vacancies:
        await safe_edit_status(status_msg, "❌ Вакансии не найдены в открытых источниках. Попробуйте передать более развернутый бриф.")
        return

    await safe_edit_status(status_msg, f"🧠 [3/3] Матричный скоринг {len(unique_vacancies[:8])} кандидатов в Groq LPU...")

    compact_candidates = [
        f"КАНДИДАТ {idx}:\n"
        f"Компания: {v['company']}\n"
        f"Должность: {v['title']}\n"
        f"Источник: {v['source']}\n"
        f"URL: {v['url']}\n"
        f"Описание: {v['desc']}\n"
        for idx, v in enumerate(unique_vacancies[:8], 1)
    ]
    candidates_payload = "\n".join(compact_candidates)

    # Шаг 3: Матричный скоринг
    prompt_matrix = f"""Ты — беспощадный OSINT-аудитор. Отсей ложные совпадения и найди оригинального заказчика.

ЭТАЛОННЫЙ ПРОФИЛЬ:
{json.dumps(brief_profile, ensure_ascii=False, indent=2)}

СПИСОК ВАКАНСИЙ:
{candidates_payload}

ПРАВИЛА:
1. КРИТЕРИИ ДИСКВАЛИФИКАЦИИ (СКОР <= 35%):
   - Разные архитектуры (Cloud вместо OnPrem).
   - Не совпал must_have стек.
2. ВЕСА:
   - [40%] Архитектурный вызов ('core_challenge').
   - [35%] Полнота must_have стека.
   - [25%] Специфика среды и домен.
3. Дословная цитата задач в описании дает 95-99%.

Выведи ТОП-3 кандидатов по шаблону:
══════════════════════════════
🏢 **КОМПАНИЯ:** [Название компании или канал]
🎯 **Матричный скор:** [XX]%
🎲 **Статус деанонимизации:** [🟢 Точный оригинал (85-99%) / 🟡 Высокая вероятность (60-84%) / 🟠 Слабое сходство (30-59%)]
📌 **Позиция:** [Название должности]
💰 **Зарплата:** [Вилка или 'Не указана']
🔗 **Ссылка:** [Открыть источник](URL)
🏛 **Источник:** [Google / Хабр / Работа России / SuperJob / Telegram]

⚖️ **Аудит по матрице:**
• **Must-have стек:** [Что совпало, чего не хватает]
• **Архитектурный вызов:** [Реальная проектная задача]

💡 **Стратегия выхода для сейлза:**
• **К кому идти:** [Роль ЛПР: CTO, Lead DevOps, Head of Eng]
• **Болевая точка:** [Главная боль проекта]
• **Холодный хук:** [1-2 предложения хук для первого контакта]
══════════════════════════════"""

    result_text, err = await call_groq_async(prompt_matrix, max_tokens=1500)
    if not result_text:
        await safe_edit_status(status_msg, f"⚠️ Ошибка обработки: {err}")
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

    fallback_badge = "\nℹ️ *Режим работы: Открытые API (квота Google исчерпана)*\n" if google_quota_exhausted else ""
    final_output = f"🎯 **ОТЧЁТ МАТРИЧНОЙ ДЕАНОНИМИЗАЦИИ**{fallback_badge}\n\n" + "\n".join(enhanced_lines)

    if len(final_output) > 4000:
        parts = [final_output[i:i+4000] for i in range(0, len(final_output), 4000)]
        await safe_edit_status(status_msg, parts[0])
        for p in parts[1:]:
            await message.answer(p, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    else:
        await safe_edit_status(status_msg, final_output)


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
