import asyncio
import json
import logging
import os
import re
import sys
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

# ----------------- ЛОГИРОВАНИЕ -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("OSINT_Bot")

# ----------------- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ -----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPERJOB_KEY = os.getenv("SUPERJOB_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID", "10092ea56d2504c84")

TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_SESSION_STRING = os.getenv("TG_SESSION_STRING", "")

if not TELEGRAM_BOT_TOKEN or not GROQ_API_KEY:
    logger.critical("TELEGRAM_BOT_TOKEN или GROQ_API_KEY не заданы!")
    raise ValueError("TELEGRAM_BOT_TOKEN и GROQ_API_KEY обязательны!")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
groq_client = AsyncGroq(api_key=GROQ_API_KEY.strip())

# Динамический список проверенных моделей Groq
AVAILABLE_GROQ_MODELS = []

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

OUTSTAFF_TEXT_MARKERS = (
    "наш клиент", "нашего клиента", "клиент —", "клиент:", "для нашего партнера",
    "проект заказчика", "на стороне заказчика", "аутстафф", "outstaff",
    "предоставление персонала", "аккредитованная it-компания ищет для",
    "в интересах компании"
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
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"Health check сервер запущен на порту {port}")
    server.serve_forever()


# ----------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ -----------------
def clean_html(raw_html: str) -> str:
    return " ".join(HTML_TAG_RE.sub(" ", raw_html).split())


def is_agency(company_name: str, text: str = "") -> bool:
    lower_name = company_name.lower()
    if any(w in lower_name for w in KNOWN_AGENCIES):
        return True
    if text:
        lower_text = text.lower()
        if any(marker in lower_text for marker in OUTSTAFF_TEXT_MARKERS):
            return True
    return False


def build_lead_osint_url(company_name: str, target_role: str = "CTO") -> str:
    clean_company = CLEAN_NAME_RE.sub("", company_name).strip()
    query = f'site:linkedin.com/in "{clean_company}" ({target_role} OR "Head of Engineering" OR "Team Lead")'
    return f"https://www.google.com/search?q={quote_plus(query)}"


async def safe_edit_status(msg: Message, text: str):
    try:
        await msg.edit_text(text)
    except TelegramAPIError:
        pass


# ----------------- ДИНАМИЧЕСКИЙ ВЫБОР МОДЕЛЕЙ GROQ -----------------
async def update_available_groq_models():
    """Запрашивает у API список моделей, доступных именно вашему аккаунту"""
    global AVAILABLE_GROQ_MODELS
    try:
        models_data = await groq_client.models.list()
        valid_ids = [m.id for m in models_data.data if m.active]
        # Приоритет: llama, qwen, gemma, остальные чат-модели
        sorted_models = sorted(
            valid_ids,
            key=lambda x: (
                0 if "llama" in x.lower() and "70b" in x.lower() else
                1 if "llama" in x.lower() else
                2 if "qwen" in x.lower() else
                3 if "gemma" in x.lower() else 4
            )
        )
        if sorted_models:
            AVAILABLE_GROQ_MODELS = sorted_models
            logger.info(f"Доступные модели Groq: {AVAILABLE_GROQ_MODELS[:4]}")
            return
    except Exception as e:
        logger.warning(f"Не удалось получить список моделей через API: {e}")

    # Запасной статический список
    AVAILABLE_GROQ_MODELS = [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "gemma2-9b-it"
    ]


async def call_groq_async(prompt: str, max_tokens: int = 1500, json_mode: bool = False) -> tuple[str, str]:
    global AVAILABLE_GROQ_MODELS
    if not AVAILABLE_GROQ_MODELS:
        await update_available_groq_models()

    last_err = ""
    for model_name in AVAILABLE_GROQ_MODELS[:5]:
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
                timeout=12.0
            )
            content = res.choices[0].message.content
            if content and content.strip():
                return content, ""
        except Exception as e:
            last_err = str(e)
            logger.warning(f"Модель {model_name} вернула ошибку: {e}. Пробуем следующий вариант...")
            # Если ошибка вызвана json_mode, пробуем ту же модель без него
            if json_mode and "json" in str(e).lower():
                try:
                    kwargs.pop("response_format", None)
                    res = await asyncio.wait_for(
                        groq_client.chat.completions.create(**kwargs),
                        timeout=12.0
                    )
                    content = res.choices[0].message.content
                    if content and content.strip():
                        return content, ""
                except Exception:
                    pass
            continue
    return "", last_err


# ----------------- FULL PAGE HYDRATION -----------------
async def hydrate_single_vacancy(client: httpx.AsyncClient, job: dict) -> dict:
    url = job.get("url", "")
    if not url or "t.me" in url:
        return job

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

    try:
        r = await client.get(url, headers=headers, timeout=2.5)
        if r.status_code == 200:
            text = clean_html(r.text)
            clean_snippet = " ".join(text.split()[:300])
            if len(clean_snippet) > 200:
                job["desc"] = clean_snippet
                if is_agency(job["company"], clean_snippet):
                    job["is_agency"] = True
    except Exception:
        pass
    return job


async def hydrate_all_vacancies(client: httpx.AsyncClient, vacancies: list) -> list:
    tasks = [hydrate_single_vacancy(client, v) for v in vacancies[:6]]
    hydrated = await asyncio.gather(*tasks, return_exceptions=True)
    clean_list = []
    for item in hydrated:
        if isinstance(item, dict) and not item.get("is_agency", False):
            clean_list.append(item)
    return clean_list


# ----------------- ИСТОЧНИКИ ПОИСКА -----------------
async def search_google_custom(client: httpx.AsyncClient, query: str, count: int = 5) -> list:
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
            logger.warning("Квота Google CSE исчерпана (429/403).")
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

            if is_agency(company, snippet):
                continue

            jobs.append({
                "source": "Google Search",
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
            sal = item.get("salary", {})
            sal_str = sal.get("formatted") if sal and sal.get("formatted") else "не указана"
            href = item.get("href", "")
            full_url = f"https://career.habr.com{href}" if href.startswith("/") else href
            skills = ", ".join([s.get("title", "") for s in item.get("skills", [])])
            desc_text = f"Стек: {skills}"

            if is_agency(company, desc_text):
                continue

            jobs.append({
                "source": "Хабр Карьера",
                "title": item.get("title", ""),
                "company": company,
                "salary": sal_str,
                "url": full_url,
                "desc": desc_text,
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
            p_from, p_to = item.get("payment_from", 0), item.get("payment_to", 0)
            cur = item.get("currency", "")
            sal_str = f"{p_from if p_from else ''} - {p_to if p_to else ''} {cur}".strip() if (p_from or p_to) else "не указана"
            desc = clean_html(item.get("candidat", "") or item.get("work", ""))

            if is_agency(company, desc):
                continue

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

                if is_agency(company, post_text):
                    continue

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


# ----------------- ХЭНДЛЕРЫ AIOGRAM -----------------
@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "💼 **Multi-Source OSINT Lead Hunter**\n\n"
        "Отправьте бриф вакансии. Бот автоматически подберет активную модель нейросети, "
        "сформирует каскадные дорки, выкачает страницы и выполнит скоринг.",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    global google_quota_exhausted
    user_text = message.text
    logger.info(f"Получен бриф ({len(user_text)} симв.)")

    status_msg = await message.answer("⚡️ [1/4] Извлечение сущностей проекта...")

    prompt_extract = f"""Ты — senior технический рекрутер и OSINT-аналитик.
Разбери бриф и верни JSON со строгой структурой:
{{
  "role": "название роли",
  "domain": "домен (Fintech, E-commerce, GameDev или Общий)",
  "infra_type": "тип среды (OnPrem, Cloud или Не указан)",
  "must_have": ["список", "только", "редких", "технологий"],
  "core_challenge": "главная проектная задача (1 предложение)",
  "dork_exact_quote": "дословная фраза из 3-5 слов из обязанностей без кавычек",
  "dork_tech_cluster": "роль + 2 технологии для поиска",
  "dork_ats_search": "название ключевого фреймворка"
}}
БРИФ:
\"\"\"{user_text[:900]}\"\"\""""

    extract_raw, err = await call_groq_async(prompt_extract, max_tokens=350, json_mode=True)
    try:
        brief_profile = json.loads(extract_raw)
    except Exception:
        brief_profile = {
            "role": "Разработчик",
            "domain": "Общий",
            "infra_type": "Не указан",
            "must_have": [],
            "core_challenge": "Разработка сервисов",
            "dork_exact_quote": "",
            "dork_tech_cluster": " ".join(user_text.split()[:3]),
            "dork_ats_search": "Frontend"
        }

    rare_techs = brief_profile.get("must_have", [])
    primary_tech = rare_techs[0] if rare_techs else brief_profile.get("dork_ats_search", "Angular")
    exact_quote = brief_profile.get("dork_exact_quote", "")
    cluster_query = brief_profile.get("dork_tech_cluster", primary_tech)

    await safe_edit_status(status_msg, "🔍 [2/4] Запуск поисковых волн (Google X-Ray, Хабр, ATS, SuperJob, TG)...")

    async with httpx.AsyncClient(follow_redirects=True) as http_client:
        tasks = [
            fetch_habr(http_client, primary_tech, 6),
            fetch_superjob(http_client, primary_tech, 5),
            search_telegram_native(primary_tech),
        ]

        if not google_quota_exhausted and GOOGLE_API_KEY:
            if len(exact_quote.split()) >= 3:
                tasks.append(search_google_custom(http_client, f'"{exact_quote}"', count=3))

            tasks.append(search_google_custom(http_client, cluster_query, count=5))

            ats_query = f"(site:huntflow.io OR site:potok.io) {primary_tech} {brief_profile.get('role', '')}"
            tasks.append(search_google_custom(http_client, ats_query, count=4))

        try:
            raw_results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=4.5)
            valid_results = [item for sub in raw_results if isinstance(sub, list) for item in sub]
        except Exception as e:
            logger.error(f"Ошибка при поиске: {e}")
            valid_results = []

        unique_vacancies = list({v["url"]: v for v in valid_results if v.get("url")}.values())

        if not unique_vacancies:
            await safe_edit_status(status_msg, "❌ Совпадений не найдено. Попробуйте передать бриф с более конкретным техническим описанием.")
            return

        await safe_edit_status(status_msg, f"🌐 [3/4] Выкачка полных страниц {len(unique_vacancies[:6])} кандидатов (Full Body)...")
        hydrated_vacancies = await hydrate_all_vacancies(http_client, unique_vacancies)

    if not hydrated_vacancies:
        hydrated_vacancies = unique_vacancies[:6]

    await safe_edit_status(status_msg, f"🧠 [4/4] Матричный скоринг {len(hydrated_vacancies[:6])} кандидатов через Groq LPU...")

    compact_candidates = [
        f"КАНДИДАТ {idx}:\n"
        f"Компания: {v['company']}\n"
        f"Должность: {v['title']}\n"
        f"Источник: {v['source']}\n"
        f"URL: {v['url']}\n"
        f"Полный текст: {v['desc']}\n"
        for idx, v in enumerate(hydrated_vacancies[:6], 1)
    ]
    candidates_payload = "\n".join(compact_candidates)

    prompt_matrix = f"""Ты — беспощадный OSINT-аудитор. Отсей кадровых посредников и найди истинного прямого заказчика.

ЭТАЛОННЫЙ ПРОФИЛЬ ПРОЕКТА:
{json.dumps(brief_profile, ensure_ascii=False, indent=2)}

СПИСОК ВАКАНСИЙ (С ПОЛНЫМ ТЕКСТОМ):
{candidates_payload}

ПРАВИЛА ОЦЕНКИ:
1. КРИТЕРИИ ДИСКВАЛИФИКАЦИИ (СКОР <= 35%):
   - Если кандидат является аутстафф/рекрутинг-посредником (фразы 'наш партнер', 'клиент' и т.д.).
   - Не совпал стек фреймворков или архитектура.
2. ВЕСА ДЛЯ НАЧИСЛЕНИЯ БАЛЛОВ:
   - [45%] Архитектурный вызов ('core_challenge') и схожесть реальных задач.
   - [35%] Покрытие редкого стека ('must_have').
   - [20%] Домен бизнеса.
3. Если совпала дословная цитата обязанностей — вероятность 95-99%.

Выведи ТОП-3 кандидатов строго по шаблону:
══════════════════════════════
🏢 **КОМПАНИЯ:** [Название прямого заказчика или канал]
🎯 **Матричный скор:** [XX]%
🎲 **Статус деанонимизации:** [🟢 Точный оригинал (85-99%) / 🟡 Высокая вероятность (60-84%) / 🟠 Слабое сходство (30-59%)]
📌 **Позиция:** [Название должности]
💰 **Зарплата:** [Вилка или 'Не указана']
🔗 **Ссылка:** [Открыть источник](URL)
🏛 **Источник:** [Google ATS / Google Search / Хабр / SuperJob / Telegram]

⚖️ **Аудит по матрице:**
• **Must-have стек:** [Что конкретно совпало, чего нет]
• **Архитектурный вызов:** [Сравнение задач проекта]
• **Проверка на аутстафф:** [Прямой заказчик или посредник]

💡 **Стратегия выхода для сейлза:**
• **К кому идти:** [Роль ЛПР: CTO, Lead Frontend, Head of Engineering]
• **Болевая точка:** [Главная боль проекта]
• **Холодный хук:** [1-2 предложения хук для первого контакта]
══════════════════════════════"""

    result_text, err = await call_groq_async(prompt_matrix, max_tokens=1600)
    if not result_text:
        await safe_edit_status(status_msg, f"⚠️ Не удалось выполнить скоринг через Groq: {err}")
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

    fallback_badge = "\nℹ️ *Режим: Открытые API (квота Google исчерпана)*\n" if google_quota_exhausted else ""
    final_output = f"🎯 **ОТЧЁТ МАТРИЧНОЙ ДЕАНОНИМИЗАЦИИ**{fallback_badge}\n\n" + "\n".join(enhanced_lines)

    if len(final_output) > 4000:
        parts = [final_output[i:i+4000] for i in range(0, len(final_output), 4000)]
        await safe_edit_status(status_msg, parts[0])
        for p in parts[1:]:
            await message.answer(p, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    else:
        await safe_edit_status(status_msg, final_output)


# ----------------- СТАРТ ПРИЛОЖЕНИЯ -----------------
async def init_telethon():
    global telethon_client
    if not telethon_client:
        logger.info("Telethon: Ключи не заданы, пропуск.")
        return
    try:
        await asyncio.wait_for(telethon_client.connect(), timeout=4.0)
        if not await telethon_client.is_user_authorized():
            logger.warning("Telethon: Сессия не авторизована, мониторинг отключен.")
            telethon_client = None
        else:
            logger.info("Telethon успешно авторизован.")
    except Exception as e:
        logger.warning(f"Telethon ошибка: {e}")
        telethon_client = None


async def main():
    logger.info("Инициализация сервиса...")
    threading.Thread(target=run_health_server, daemon=True).start()

    # Получаем список доступных моделей Groq
    await update_available_groq_models()

    await init_telethon()

    try:
        await asyncio.wait_for(bot.delete_webhook(drop_pending_updates=True), timeout=4.0)
        logger.info("Webhook сброшен.")
    except Exception as e:
        logger.warning(f"Предупреждение delete_webhook: {e}")

    logger.info(">>> Запуск polling aiogram. Бот слушает сообщения! <<<")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
