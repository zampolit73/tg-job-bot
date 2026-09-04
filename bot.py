import asyncio
import json
import logging
import os
import re
import sys
import threading
from urllib.parse import quote_plus
from http.server import HTTPServer, BaseHTTPRequestHandler
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
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
    raise ValueError("Критические переменные не заданы!")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
groq_client = AsyncGroq(api_key=GROQ_API_KEY.strip())

ACTIVE_GROQ_MODEL = None

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
    logger.info(f"Health check запущен на порту {port}")
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
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 0.5)
        try:
            await msg.edit_text(text)
        except Exception:
            pass
    except TelegramAPIError:
        pass


# ----------------- ВЫБОР МОДЕЛЕЙ GROQ -----------------
async def find_working_groq_model() -> str:
    global ACTIVE_GROQ_MODEL
    candidate_models = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "gemma2-9b-it"]
    try:
        model_list = await groq_client.models.list()
        api_models = [m.id for m in model_list.data if m.active and "whisper" not in m.id and "guard" not in m.id]
        for am in api_models:
            if am not in candidate_models and "qwen" not in am:
                candidate_models.append(am)
    except Exception as e:
        logger.warning(f"Ошибка проверки моделей: {e}")

    for model in candidate_models:
        try:
            res = await asyncio.wait_for(
                groq_client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=5,
                ),
                timeout=3.0
            )
            if res.choices and res.choices[0].message.content:
                ACTIVE_GROQ_MODEL = model
                logger.info(f"✅ ВЫБРАНА МОДЕЛЬ GROQ: {ACTIVE_GROQ_MODEL}")
                return ACTIVE_GROQ_MODEL
        except Exception:
            continue

    ACTIVE_GROQ_MODEL = "llama-3.3-70b-versatile"
    return ACTIVE_GROQ_MODEL


async def call_groq_async(prompt: str, max_tokens: int = 1500, json_mode: bool = False) -> tuple[str, str]:
    global ACTIVE_GROQ_MODEL
    if not ACTIVE_GROQ_MODEL:
        await find_working_groq_model()

    safe_prompt = prompt if len(prompt) < 6000 else prompt[:6000]
    kwargs = {
        "model": ACTIVE_GROQ_MODEL,
        "messages": [{"role": "user", "content": safe_prompt}],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        res = await asyncio.wait_for(groq_client.chat.completions.create(**kwargs), timeout=10.0)
        content = res.choices[0].message.content
        if content and content.strip():
            return content, ""
    except Exception as e:
        err_msg = str(e)
        if "413" in err_msg or "too large" in err_msg.lower() or "tokens" in err_msg.lower():
            try:
                kwargs["messages"] = [{"role": "user", "content": safe_prompt[:3000]}]
                res = await asyncio.wait_for(groq_client.chat.completions.create(**kwargs), timeout=8.0)
                return res.choices[0].message.content, ""
            except Exception as e2:
                return "", str(e2)
        if json_mode and "json" in err_msg.lower():
            try:
                kwargs.pop("response_format", None)
                res = await asyncio.wait_for(groq_client.chat.completions.create(**kwargs), timeout=8.0)
                return res.choices[0].message.content, ""
            except Exception as e2:
                return "", str(e2)
        return "", err_msg

    return "", "Empty response"


# ----------------- РЕАЛЬНЫЙ ПОИСК ПО ВАШИМ ЧАТАМ И ДИАЛОГАМ -----------------
async def search_user_joined_chats(search_term: str, limit_results: int = 5) -> list:
    """Поиск по реальным чатам и группам, в которых состоит ваш Telegram-аккаунт"""
    if not telethon_client or not telethon_client.is_connected():
        logger.info("Telethon не подключен, поиск по чатам пропущен.")
        return []

    # Убираем односимвольные запросы вроде 'C', заменяя на специфичные термины
    clean_term = search_term.strip()
    if len(clean_term) <= 1:
        clean_term = "SIP"

    results = []
    logger.info(f"Запуск сканирования ваших чатов Telegram по запросу: '{clean_term}'...")

    try:
        dialog_count = 0
        # Перебираем первые 35 ваших активных чатов
        async for dialog in telethon_client.iter_dialogs(limit=35):
            if not (dialog.is_group or dialog.is_channel):
                continue
            dialog_count += 1

            try:
                async for message in telethon_client.iter_messages(dialog.entity, search=clean_term, limit=2):
                    if message.text and len(message.text) > 40:
                        post_text = clean_html(message.text)
                        
                        # Формируем ссылку на пост в чате
                        if getattr(dialog.entity, 'username', None):
                            msg_url = f"https://t.me/{dialog.entity.username}/{message.id}"
                        else:
                            clean_id = str(dialog.entity.id).replace("-100", "")
                            msg_url = f"https://t.me/c/{clean_id}/{message.id}"

                        company_match = re.search(r"(?:в компанию|компания|проект|заказчик|в команду):\s*([A-Za-zА-Яа-я0-9_\-\s]{3,30})", post_text, re.IGNORECASE)
                        company = company_match.group(1).strip() if company_match else dialog.name

                        results.append({
                            "source": f"Ваш чат: {dialog.name[:25]}",
                            "title": f"Пост в {dialog.name[:25]}",
                            "company": company,
                            "salary": "в посте",
                            "url": msg_url,
                            "desc": post_text[:300]
                        })
                        if len(results) >= limit_results:
                            break
            except Exception:
                continue

            if len(results) >= limit_results:
                break

        logger.info(f"Просканировано {dialog_count} ваших чатов, найдено совпадений: {len(results)}")
    except Exception as e:
        logger.warning(f"Ошибка при итерации диалогов: {e}")

    return results


# ----------------- ВНЕШНИЕ ИСТОЧНИКИ ДАННЫХ -----------------
async def hydrate_single_vacancy(client: httpx.AsyncClient, job: dict) -> dict:
    url = job.get("url", "")
    if not url or "t.me" in url:
        return job

    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = await client.get(url, headers=headers, timeout=2.0)
        if r.status_code == 200:
            text = clean_html(r.text)
            clean_snippet = " ".join(text.split()[:70])
            if len(clean_snippet) > 100:
                job["desc"] = clean_snippet[:450]
                if is_agency(job["company"], clean_snippet):
                    job["is_agency"] = True
    except Exception:
        pass
    return job


async def hydrate_all_vacancies(client: httpx.AsyncClient, vacancies: list) -> list:
    tasks = [hydrate_single_vacancy(client, v) for v in vacancies[:4]]
    hydrated = await asyncio.gather(*tasks, return_exceptions=True)
    return [item for item in hydrated if isinstance(item, dict) and not item.get("is_agency", False)]


async def search_google_custom(client: httpx.AsyncClient, query: str, count: int = 4) -> list:
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
        r = await client.get(url, params=params, timeout=2.5)
        if r.status_code in (429, 403):
            google_quota_exhausted = True
            return []
        if r.status_code != 200:
            return []

        jobs = []
        for item in r.json().get("items", []):
            title = item.get("title", "")
            link = item.get("link", "")
            snippet = clean_html(item.get("snippet", ""))
            company_match = re.search(r"(?:в компании|—)\s+([^|\-—\(\)]+)", title, re.IGNORECASE)
            company = company_match.group(1).strip() if company_match else "Прямой работодатель"

            if is_agency(company, snippet):
                continue

            jobs.append({
                "source": "Google Search",
                "title": title[:60],
                "company": company,
                "salary": "в описании",
                "url": link,
                "desc": snippet[:250]
            })
        return jobs
    except Exception:
        return []


async def fetch_habr(client: httpx.AsyncClient, query: str, count: int = 5) -> list:
    clean_q = CLEAN_QUERY_RE.sub(" ", query).strip()
    url = "https://career.habr.com/api/frontend/vacancies"
    params = {"q": clean_q, "per_page": count}
    headers = {"User-Agent": "Mozilla/5.0"}
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
                "desc": desc_text[:250],
            })
        return jobs
    except Exception:
        return []


async def fetch_superjob(client: httpx.AsyncClient, query: str, count: int = 4) -> list:
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
                "desc": desc[:250]
            })
        return jobs
    except Exception:
        return []


# ----------------- ХЭНДЛЕРЫ AIOGRAM -----------------
@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "💼 **Multi-Source OSINT Lead Hunter**\n\n"
        "Отправьте бриф. Бот просканирует ваши Telegram-чаты, карьерные базы и Google X-Ray "
        "с матричным скорингом прямого заказчика.",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    global google_quota_exhausted
    user_text = message.text
    logger.info(f"Получен бриф ({len(user_text)} симв.) от {message.from_user.id}")

    status_msg = await message.answer("⚡️ [1/4] Извлечение редких технологий и профиля...")

    prompt_extract = f"""Ты — senior технический рекрутер и OSINT-аналитик.
Разбери бриф и верни JSON:
{{
  "role": "название роли",
  "domain": "домен проекта",
  "infra_type": "OnPrem или Cloud или Linux",
  "must_have": ["список", "редких", "технологий"],
  "tg_search_keyword": "одно самое редкое длинное слово для поиска в Telegram (например, SIP, VoIP, RTP, G.711, WebRTC)",
  "core_challenge": "главная проектная задача",
  "dork_exact_quote": "дословная фраза из 3-5 слов из обязанностей без кавычек",
  "dork_tech_cluster": "роль + 2 технологии"
}}
БРИФ:
\"\"\"{user_text[:700]}\"\"\""""

    extract_raw, err = await call_groq_async(prompt_extract, max_tokens=300, json_mode=True)
    try:
        brief_profile = json.loads(extract_raw)
    except Exception:
        brief_profile = {
            "role": "C Разработчик",
            "domain": "VoIP / Streaming",
            "infra_type": "Linux",
            "must_have": ["SIP", "RTP", "Linux"],
            "tg_search_keyword": "SIP",
            "core_challenge": "Разработка высоконагруженных сетевых сервисов",
            "dork_exact_quote": "",
            "dork_tech_cluster": "C SIP RTP"
        }

    tg_keyword = brief_profile.get("tg_search_keyword") or "SIP"
    if len(tg_keyword) <= 1:
        tg_keyword = "SIP"

    exact_quote = brief_profile.get("dork_exact_quote", "")
    cluster_query = brief_profile.get("dork_tech_cluster", tg_keyword)

    await safe_edit_status(status_msg, f"🔍 [2/4] Поиск в ваших Telegram-чатах по '{tg_keyword}' и открытых базах...")

    async with httpx.AsyncClient(follow_redirects=True) as http_client:
        tasks = [
            fetch_habr(http_client, tg_keyword, 5),
            fetch_superjob(http_client, tg_keyword, 4),
            search_user_joined_chats(tg_keyword, limit_results=5),
        ]

        if not google_quota_exhausted and GOOGLE_API_KEY:
            if len(exact_quote.split()) >= 3:
                tasks.append(search_google_custom(http_client, f'"{exact_quote}"', count=3))
            tasks.append(search_google_custom(http_client, cluster_query, count=4))

        try:
            raw_results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5.0)
            valid_results = [item for sub in raw_results if isinstance(sub, list) for item in sub]
        except Exception as e:
            logger.error(f"Ошибка поиска: {e}")
            valid_results = []

        unique_vacancies = list({v["url"]: v for v in valid_results if v.get("url")}.values())

        if not unique_vacancies:
            await safe_edit_status(status_msg, "❌ Совпадений не найдено. Убедитесь, что бот добавлен в нужные чаты или расширьте бриф.")
            return

        await safe_edit_status(status_msg, f"🌐 [3/4] Обработка {len(unique_vacancies[:4])} кандидатов...")
        hydrated_vacancies = await hydrate_all_vacancies(http_client, unique_vacancies)

    if not hydrated_vacancies:
        hydrated_vacancies = unique_vacancies[:4]

    await safe_edit_status(status_msg, f"🧠 [4/4] Матричный скоринг через Groq ({ACTIVE_GROQ_MODEL})...")

    compact_candidates = [
        f"КАНДИДАТ {idx}:\n"
        f"Компания/Чат: {v['company']}\n"
        f"Должность: {v['title']}\n"
        f"Источник: {v['source']}\n"
        f"URL: {v['url']}\n"
        f"Текст поста: {v['desc'][:300]}\n"
        for idx, v in enumerate(hydrated_vacancies[:4], 1)
    ]
    candidates_payload = "\n".join(compact_candidates)

    prompt_matrix = f"""Ты — OSINT-аудитор. Отсей посредников и выяви прямого заказчика.

ЭТАЛОН:
{json.dumps(brief_profile, ensure_ascii=False)}

ВАКАНСИИ ИЗ ЧАТОВ И БАЗ:
{candidates_payload}

ПРАВИЛА:
1. КРИТЕРИИ ДИСКВАЛИФИКАЦИИ (СКОР <= 35%):
   - Аутстафф/посредник (фразы 'клиент', 'партнер').
   - Не совпал стек (например, нет SIP/RTP/VoIP).
2. ВЕСА: 45% задачи, 35% стек, 20% домен. Если пост из Telegram-чата совпадает по стеку и задачам — скор 80-99%.

Выведи ТОП кандидатов строго по шаблону:
══════════════════════════════
🏢 **КОМПАНИЯ:** [Название компании или чата]
🎯 **Матричный скор:** [XX]%
🎲 **Статус деанонимизации:** [🟢 Точный оригинал (85-99%) / 🟡 Высокая вероятность (60-84%) / 🟠 Слабое сходство (30-59%)]
📌 **Позиция:** [Название должности]
💰 **Зарплата:** [Вилка или 'Не указана']
🔗 **Ссылка:** [Открыть источник/пост](URL)
🏛 **Источник:** [Telegram Чат / Google / Хабр / SuperJob]

⚖️ **Аудит по матрице:**
• **Must-have стек:** [Что совпало, чего нет]
• **Архитектурный вызов:** [Сравнение задач проекта]
• **Проверка на аутстафф:** [Прямой заказчик или посредник]

💡 **Стратегия выхода для сейлза:**
• **К кому идти:** [Роль ЛПР: CTO, Lead Developer, Head of Engineering]
• **Болевая точка:** [Главная боль проекта]
• **Холодный хук:** [1-2 предложения хук для первого контакта]
══════════════════════════════"""

    result_text, err = await call_groq_async(prompt_matrix, max_tokens=1400)
    if not result_text:
        await safe_edit_status(status_msg, f"⚠️ Не удалось выполнить скоринг: {err}")
        return

    enhanced_lines = []
    current_company = ""
    for line in result_text.splitlines():
        if "🏢 **КОМПАНИЯ:**" in line:
            current_company = line.replace("🏢 **КОМПАНИЯ:**", "").strip()
            enhanced_lines.append(line)
        elif "• **К кому идти:**" in line and current_company and not current_company.startswith("@") and "чат" not in current_company.lower():
            osint_url = build_lead_osint_url(current_company)
            enhanced_lines.append(line)
            enhanced_lines.append(f"• **Поиск контактов ЛПР:** [Найти профили в LinkedIn/Google]({osint_url})")
        else:
            enhanced_lines.append(line)

    fallback_badge = "\nℹ️ *Режим: Открытые API + Telegram-диалоги*\n" if google_quota_exhausted else ""
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
        logger.warning("Telethon: переменные не заданы в Environment.")
        return
    try:
        await asyncio.wait_for(telethon_client.connect(), timeout=3.0)
        if not await telethon_client.is_user_authorized():
            logger.warning("Telethon не авторизован.")
            telethon_client = None
        else:
            logger.info("Telethon успешно авторизован! Доступ к чатам открыт.")
    except Exception as e:
        logger.warning(f"Telethon пропущен: {e}")
        telethon_client = None


async def main():
    logger.info("Инициализация сервиса...")
    threading.Thread(target=run_health_server, daemon=True).start()
    await find_working_groq_model()
    await init_telethon()

    try:
        await asyncio.wait_for(bot.delete_webhook(drop_pending_updates=True), timeout=3.0)
        logger.info("Webhook сброшен.")
    except Exception as e:
        logger.warning(f"delete_webhook: {e}")

    logger.info(">>> Запуск polling aiogram. Бот слушает сообщения! <<<")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
