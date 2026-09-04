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
logger = logging.getLogger("OSINT_Matcher")

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
    "personnel", "talent", "staff", "headhunting", "подбор персонала"
)

OUTSTAFF_TEXT_MARKERS = (
    "наш клиент", "нашего клиента", "клиент —", "клиент:", "для нашего партнера",
    "проект заказчика", "на стороне заказчика", "аутстафф", "outstaff"
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
    logger.info(f"Health check сервер запущен на порту {port}")
    server.serve_forever()


# ----------------- СРАВНЕНИЕ ТЕКСТОВ НА СХОДСТВО -----------------
def clean_html(raw_html: str) -> str:
    return " ".join(HTML_TAG_RE.sub(" ", raw_html).split())


def extract_key_tokens(text: str) -> set:
    """Извлекает значимые технические слова, игнорируя предлоги"""
    words = re.findall(r'[A-Za-zА-Яа-я0-9\+\#\.\/\-]{3,}', text.lower())
    stop_words = {"для", "или", "как", "все", "при", "опыт", "работа", "года", "знание", "умение"}
    return {w for w in words if w not in stop_words}


def calculate_overlap_score(brief_text: str, candidate_text: str) -> float:
    """Вычисляет прямое процентное сходство между присланной вакансией и найденным постом"""
    brief_tokens = extract_key_tokens(brief_text)
    cand_tokens = extract_key_tokens(candidate_text)
    if not brief_tokens or not cand_tokens:
        return 0.0
    intersection = brief_tokens.intersection(cand_tokens)
    return round((len(intersection) / min(len(brief_tokens), len(cand_tokens))) * 100, 1)


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
        logger.warning(f"Ошибка проверки списка моделей: {e}")

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


# ----------------- ТОЧНЫЙ ПОИСК ПО ВАШИМ ЧАТАМ TELEGRAM -----------------
async def search_joined_chats_by_terms(search_terms: list, raw_brief: str) -> list:
    """Ищет по нескольким ключевым маркерам и сравнивает текст с оригинальным брифом"""
    if not telethon_client or not telethon_client.is_connected():
        logger.warning("Telethon не подключен.")
        return []

    found_posts = []
    seen_ids = set()

    for term in search_terms:
        clean_t = term.strip()
        if len(clean_t) < 3:
            continue

        try:
            logger.info(f"Поиск в диалогах Telegram по маркеру: '{clean_t}'...")
            async for dialog in telethon_client.iter_dialogs(limit=40):
                if not (dialog.is_group or dialog.is_channel):
                    continue

                try:
                    async for message in telethon_client.iter_messages(dialog.entity, search=clean_t, limit=4):
                        if not message.text or len(message.text) < 50:
                            continue

                        unique_key = f"{dialog.id}_{message.id}"
                        if unique_key in seen_ids:
                            continue
                        seen_ids.add(unique_key)

                        post_text = clean_html(message.text)
                        
                        # Расчет точного текстового сходства с присланным брифом
                        overlap = calculate_overlap_score(raw_brief, post_text)

                        # Если совпадение больше 25%, это явный кандидат
                        if overlap > 25.0 or clean_t.lower() in post_text.lower():
                            if getattr(dialog.entity, 'username', None):
                                msg_url = f"https://t.me/{dialog.entity.username}/{message.id}"
                            else:
                                clean_id = str(dialog.entity.id).replace("-100", "")
                                msg_url = f"https://t.me/c/{clean_id}/{message.id}"

                            company_match = re.search(r"(?:в компанию|компания|проект|заказчик|в команду):\s*([A-Za-zА-Яа-я0-9_\-\s]{3,30})", post_text, re.IGNORECASE)
                            company = company_match.group(1).strip() if company_match else dialog.name

                            found_posts.append({
                                "source": f"Ваш чат: {dialog.name[:25]}",
                                "title": f"Пост в {dialog.name[:25]} (Сходство текста: {overlap}%)",
                                "company": company,
                                "salary": "в тексте поста",
                                "url": msg_url,
                                "desc": post_text[:350],
                                "overlap": overlap
                            })
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"Ошибка при поиске маркера {clean_t}: {e}")

    # Сортируем: посты с наибольшим совпадением текста идут первыми
    found_posts.sort(key=lambda x: x.get("overlap", 0), reverse=True)
    return found_posts[:5]


# ----------------- ВНЕШНИЕ ИСТОЧНИКИ ДАННЫХ -----------------
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
                "desc": snippet[:250],
                "overlap": 0.0
            })
        return jobs
    except Exception:
        return []


async def fetch_habr(client: httpx.AsyncClient, query: str, count: int = 4) -> list:
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
                "overlap": 0.0
            })
        return jobs
    except Exception:
        return []


# ----------------- ХЭНДЛЕРЫ AIOGRAM -----------------
@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "💼 **Multi-Source OSINT Lead Hunter**\n\n"
        "Отправьте бриф. Бот выполнит поиск по вашим Telegram-чатам, проверит дословные "
        "совпадения и выдаст матричный скоринг прямого заказчика.",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    global google_quota_exhausted
    user_text = message.text
    logger.info(f"Получен бриф ({len(user_text)} симв.) от {message.from_user.id}")

    status_msg = await message.answer("⚡️ [1/4] Извлечение ключевых маркеров для поиска...")

    # Автоматическое извлечение самых редких технических маркеров прямо из текста
    detected_markers = []
    candidates_raw = re.findall(r'\b[A-Za-z0-9\.\/\-]{3,}\b', user_text)
    known_tech = {"sip", "rtp", "rtcp", "voip", "g.711", "g.729", "h.264", "webrtc", "netfilter", "iptables", "valgrind", "cmake", "nginx", "angular"}
    for word in candidates_raw:
        if word.lower() in known_tech and word not in detected_markers:
            detected_markers.append(word)

    if not detected_markers:
        detected_markers = ["VoIP", "SIP", "Linux"]

    prompt_extract = f"""Ты — technical recruiter. Разбери бриф и верни JSON:
{{
  "role": "название роли",
  "domain": "домен проекта",
  "must_have": ["список", "редких", "технологий"],
  "core_challenge": "главная проектная задача (1 предложение)",
  "dork_exact_quote": "дословная строгая фраза из 3-4 слов из обязанностей"
}}
БРИФ:
\"\"\"{user_text[:700]}\"\"\""""

    extract_raw, err = await call_groq_async(prompt_extract, max_tokens=300, json_mode=True)
    try:
        brief_profile = json.loads(extract_raw)
    except Exception:
        brief_profile = {
            "role": "Разработчик C",
            "domain": "VoIP / Streaming",
            "must_have": detected_markers,
            "core_challenge": "Разработка сетевых сервисов",
            "dork_exact_quote": ""
        }

    search_terms = list(set(detected_markers + brief_profile.get("must_have", [])))[:4]
    primary_term = search_terms[0] if search_terms else "VoIP"
    exact_quote = brief_profile.get("dork_exact_quote", "")

    await safe_edit_status(status_msg, f"🔍 [2/4] Сканирование чатов Telegram по маркерам {search_terms}...")

    async with httpx.AsyncClient(follow_redirects=True) as http_client:
        tasks = [
            search_joined_chats_by_terms(search_terms, user_text),
            fetch_habr(http_client, primary_term, 4),
        ]

        if not google_quota_exhausted and GOOGLE_API_KEY:
            if len(exact_quote.split()) >= 3:
                tasks.append(search_google_custom(http_client, f'"{exact_quote}"', count=3))
            tasks.append(search_google_custom(http_client, f"{primary_term} вакансия", count=3))

        try:
            raw_results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5.5)
            valid_results = [item for sub in raw_results if isinstance(sub, list) for item in sub]
        except Exception as e:
            logger.error(f"Ошибка поиска: {e}")
            valid_results = []

        # Удаляем дубли по URL
        unique_vacancies = list({v["url"]: v for v in valid_results if v.get("url")}.values())

        if not unique_vacancies:
            await safe_edit_status(status_msg, "❌ Совпадений в чатах не найдено. Проверьте, есть ли этот аккаунт в нужном чате.")
            return

    await safe_edit_status(status_msg, f"🧠 [3/4] Сравнение вакансии с {len(unique_vacancies[:4])} кандидатами...")

    compact_candidates = [
        f"КАНДИДАТ {idx}:\n"
        f"Компания/Чат: {v['company']}\n"
        f"Источник: {v['source']}\n"
        f"URL: {v['url']}\n"
        f"Текстовое совпадение с брифом: {v.get('overlap', 0)}%\n"
        f"Описание/Пост: {v['desc'][:300]}\n"
        for idx, v in enumerate(unique_vacancies[:4], 1)
    ]
    candidates_payload = "\n".join(compact_candidates)

    prompt_matrix = f"""Ты — строгий OSINT-аудитор. Сравни найденные посты с оригинальным брифом.

ИСХОДНЫЙ БРИФ:
\"\"\"{user_text[:600]}\"\"\"

НАЙДЕННЫЕ ВАКАНСИИ:
{candidates_payload}

ПРАВИЛА:
1. Если кандидат взят из Telegram-чата и в описании совпадают протоколы (SIP, RTP, VoIP, Linux) или совпадение > 30% — ставь скор 85-99% и статус '🟢 Точный оригинал'.
2. Если это общая вакансия без специфики VoIP/RTP — ставь скор <= 40%.

Выведи ТОП кандидатов по шаблону:
══════════════════════════════
🏢 **КОМПАНИЯ:** [Название компании или чата]
🎯 **Матричный скор:** [XX]%
🎲 **Статус деанонимизации:** [🟢 Точный оригинал (85-99%) / 🟡 Высокая вероятность (60-84%) / 🟠 Слабое сходство (30-59%)]
📌 **Позиция:** [Название должности]
💰 **Зарплата:** [Вилка или 'Не указана']
🔗 **Ссылка:** [Открыть источник/пост](URL)
🏛 **Источник:** [Telegram Чат / Google / Хабр]

⚖️ **Аудит по матрице:**
• **Совпадение стека:** [Что совпало по тексту]
• **Сравнение с брифом:** [Насколько совпали обязанности]
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

    fallback_badge = "\nℹ️ *Режим: Сквозное сравнение Telegram-чатов*\n"
    final_output = f"🎯 **ОТЧЁТ МАТРИЧНОГО СРАВНЕНИЯ**{fallback_badge}\n\n" + "\n".join(enhanced_lines)

    if len(final_output) > 4000:
        parts = [final_output[i:i+4000] for i in range(0, len(final_output), 4000)]
        await safe_edit_status(status_msg, parts[0])
        for p in parts[1:]:
            await message.answer(p, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    else:
        await safe_edit_status(status_msg, final_output)


# ----------------- БЕЗОПАСНЫЙ СТАРТ -----------------
async def init_telethon():
    global telethon_client
    if not telethon_client:
        return
    try:
        await asyncio.wait_for(telethon_client.connect(), timeout=3.0)
        if not await telethon_client.is_user_authorized():
            logger.warning("Telethon не авторизован.")
            telethon_client = None
        else:
            logger.info("Telethon успешно авторизован! Поиск по чатам активен.")
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
