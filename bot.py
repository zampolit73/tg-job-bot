import asyncio
import json
import logging
import os
import re
import sys
import threading
from urllib.parse import quote_plus
from http.server import HTTPServer, BaseHTTPRequestHandler
import asyncpg
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from groq import AsyncGroq
from telethon import TelegramClient, events
from telethon.tl.types import User
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
DATABASE_URL = os.getenv("DATABASE_URL")

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
BOT_USER_ID = None
db_pool = None

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


# ----------------- РАБОТА С SUPABASE (POSTGRESQL) -----------------
async def init_db():
    global db_pool
    if not DATABASE_URL:
        logger.warning("DATABASE_URL не задан, работа с БД отключена.")
        return
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, timeout=10.0)
        logger.info("Подключение к Supabase PostgreSQL успешно установлено!")
    except Exception as e:
        logger.error(f"Не удалось подключиться к Supabase: {e}")
        db_pool = None


async def save_vacancy_to_db(chat_title: str, msg_id: int, chat_id: int, text: str, url: str):
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO vacancies (chat_title, message_id, chat_id, post_text, post_url)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (chat_id, message_id) DO NOTHING;
            """, chat_title, msg_id, chat_id, text, url)
    except Exception as e:
        logger.debug(f"Ошибка сохранения вакансии в БД: {e}")


async def search_vacancies_in_db(terms: list, raw_brief: str) -> list:
    if not db_pool or not terms:
        return []
    results = []
    try:
        # Ищем по ключевым терминам через ILIKE
        like_clauses = " OR ".join([f"post_text ILIKE ${i+1}" for i in range(len(terms))])
        params = [f"%{t}%" for t in terms]
        query = f"""
            SELECT chat_title, post_text, post_url 
            FROM vacancies 
            WHERE {like_clauses} 
            ORDER BY id DESC LIMIT 10;
        """
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            for row in rows:
                p_text = row['post_text']
                overlap = calculate_overlap_score(raw_brief, p_text)
                company_match = re.search(r"(?:в компанию|компания|проект|заказчик|в команду):\s*([A-Za-zА-Яа-я0-9_\-\s]{3,30})", p_text, re.IGNORECASE)
                company = company_match.group(1).strip() if company_match else row['chat_title']
                results.append({
                    "source": f"Supabase: {row['chat_title'][:25]}",
                    "title": f"Пост в {row['chat_title'][:25]}",
                    "company": company,
                    "salary": "в тексте",
                    "url": row['post_url'],
                    "desc": p_text[:300],
                    "overlap": overlap
                })
    except Exception as e:
        logger.warning(f"Ошибка поиска в Supabase: {e}")
    return results


# ----------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ -----------------
def clean_html(raw_html: str) -> str:
    return " ".join(HTML_TAG_RE.sub(" ", raw_html).split())


def extract_key_tokens(text: str) -> set:
    words = re.findall(r'[A-Za-zА-Яа-я0-9\+\#]{3,}', text.lower())
    stop_words = {"для", "или", "как", "все", "при", "опыт", "работа", "года", "знание", "умение", "будет"}
    return {w for w in words if w not in stop_words}


def calculate_overlap_score(brief_text: str, candidate_text: str) -> float:
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
                logger.info(f"ВЫБРАНА МОДЕЛЬ GROQ: {ACTIVE_GROQ_MODEL}")
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
        "temperature": 0.2,
        "presence_penalty": 0.4,
        "frequency_penalty": 0.4,
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        res = await asyncio.wait_for(groq_client.chat.completions.create(**kwargs), timeout=11.0)
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


# ----------------- БЫСТРЫЙ ОНЛАЙН-ПОИСК ПО TELEGRAM -----------------
async def search_joined_chats_global(search_terms: list, raw_brief: str, bot_id: int) -> list:
    if not telethon_client or not telethon_client.is_connected():
        return []

    found_posts = []
    seen_ids = set()

    for term in search_terms:
        clean_t = re.sub(r'[^\w\s]', '', term).strip()
        if len(clean_t) < 3:
            continue

        try:
            logger.info(f"Поиск в группах/каналах по маркеру: '{clean_t}'...")
            async for message in telethon_client.iter_messages(None, search=clean_t, limit=12):
                if not message.text or len(message.text) < 40:
                    continue

                if message.chat_id == bot_id:
                    continue

                chat = await message.get_chat()

                # Игнорируем ЛИЧНЫЕ ДИАЛОГИ (1 на 1)
                if isinstance(chat, User) or getattr(chat, 'is_user', False):
                    continue

                chat_title = getattr(chat, 'title', getattr(chat, 'username', 'Telegram Channel'))

                if "matcher" in chat_title.lower() or "bot" in chat_title.lower():
                    continue

                unique_key = f"{message.chat_id}_{message.id}"
                if unique_key in seen_ids:
                    continue
                seen_ids.add(unique_key)

                post_text = clean_html(message.text)
                overlap = calculate_overlap_score(raw_brief, post_text)

                if getattr(chat, 'username', None):
                    msg_url = f"https://t.me/{chat.username}/{message.id}"
                else:
                    clean_id = str(chat.id).replace("-100", "")
                    msg_url = f"https://t.me/c/{clean_id}/{message.id}"

                # Автоматически сохраняем найденный пост в Supabase
                asyncio.create_task(save_vacancy_to_db(chat_title, message.id, message.chat_id, post_text, msg_url))

                company_match = re.search(r"(?:в компанию|компания|проект|заказчик|в команду):\s*([A-Za-zА-Яа-я0-9_\-\s]{3,30})", post_text, re.IGNORECASE)
                company = company_match.group(1).strip() if company_match else chat_title

                found_posts.append({
                    "source": f"TG Группа: {chat_title[:25]}",
                    "title": f"Пост в {chat_title[:25]}",
                    "company": company,
                    "salary": "в тексте",
                    "url": msg_url,
                    "desc": post_text[:300],
                    "overlap": overlap
                })
        except Exception as e:
            logger.warning(f"Ошибка при поиске маркера '{clean_t}': {e}")

    found_posts.sort(key=lambda x: x.get("overlap", 0), reverse=True)
    return found_posts[:4]


# ----------------- ХАБР КАРЬЕРА -----------------
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


# ----------------- ФОНОВЫЙ LISTENER ТЕЛЕГРАМА В SUPABASE -----------------
def register_telethon_listener():
    if not telethon_client:
        return

    @telethon_client.on(events.NewMessage)
    async def handler_new_message(event):
        try:
            # Игнорируем приватные диалоги 1 на 1
            if event.is_private or not event.text or len(event.text) < 40:
                return

            text_lower = event.text.lower()
            # Фильтр ключевых слов вакансий
            if any(k in text_lower for k in ("вакансия", "ищем", "senior", "middle", "lead", "remote", "developer", "инженер")):
                chat = await event.get_chat()
                chat_title = getattr(chat, 'title', 'TG Группа')
                clean_id = str(chat.id).replace("-100", "")
                url = f"https://t.me/c/{clean_id}/{event.id}" if not getattr(chat, 'username', None) else f"https://t.me/{chat.username}/{event.id}"
                await save_vacancy_to_db(chat_title, event.id, chat.id, clean_html(event.text), url)
        except Exception:
            pass


# ----------------- ХЭНДЛЕРЫ AIOGRAM -----------------
@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "💼 Multi-Source OSINT Lead Hunter (с базой Supabase)\n\n"
        "Отправьте бриф вакансии. Бот выполнит моментальный поиск по облачной базе вакансий, "
        "вашим группам в Telegram и внешним источникам.",
        parse_mode=None
    )


@dp.message(F.text == "/debug_tg")
async def cmd_debug(message: Message):
    db_status = "✅ Подключена" if db_pool else "❌ Не подключена"
    if not telethon_client or not telethon_client.is_connected():
        await message.answer(f"База данных: {db_status}\nTelethon: ⚠️ Не подключен к Telegram.", parse_mode=None)
        return
    try:
        me = await telethon_client.get_me()
        dialogs = await telethon_client.get_dialogs(limit=12)
        group_lines = [f"- {d.name}" for d in dialogs if (d.is_group or d.is_channel)][:6]
        chats_list = "\n".join(group_lines) if group_lines else "Группы не найдены"
        response = (
            f"🗄 База данных Supabase: {db_status}\n"
            f"👤 Telethon аккаунт: {me.first_name} (@{me.username})\n"
            f"📂 Доступные группы и каналы:\n{chats_list}\n\n"
            f"🔒 Защита приватности: Личные переписки (1 на 1) исключены."
        )
        await message.answer(response, parse_mode=None)
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}", parse_mode=None)


@dp.message(F.text)
async def handle_vacancy(message: Message):
    global BOT_USER_ID
    user_text = message.text
    logger.info(f"Получен бриф ({len(user_text)} симв.) от {message.from_user.id}")

    if not BOT_USER_ID:
        me = await bot.get_me()
        BOT_USER_ID = me.id

    status_msg = await message.answer("⚡️ [1/3] Поиск ключевых сущностей...")

    detected_markers = []
    clean_words = re.findall(r'[A-Za-z0-9]{3,}', user_text)
    for word in clean_words:
        w_lower = word.lower()
        if w_lower not in {"the", "and", "for", "with", "from"} and word not in detected_markers:
            detected_markers.append(word)

    if "микрофронт" in user_text.lower():
        detected_markers.append("микрофронт")
    if "телефон" in user_text.lower() or "voip" in user_text.lower():
        detected_markers.append("VoIP")

    if not detected_markers:
        detected_markers = ["Angular", "JavaScript"]

    search_terms = detected_markers[:3]

    await safe_edit_status(status_msg, f"🔍 [2/3] Поиск в Supabase и рабочих группах по {search_terms}...")

    # 1. Сначала мгновенный поиск по локальной базе Supabase
    db_results = await search_vacancies_in_db(search_terms, user_text)

    # 2. Параллельный поиск по внешним источникам и активным чатам Telegram
    async with httpx.AsyncClient(follow_redirects=True) as http_client:
        tasks = [
            search_joined_chats_global(search_terms, user_text, BOT_USER_ID),
            fetch_habr(http_client, search_terms[0], 4),
        ]

        try:
            raw_results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10.0)
            valid_results = [item for sub in raw_results if isinstance(sub, list) for item in sub]
        except Exception as e:
            logger.error(f"Ошибка поиска: {e}")
            valid_results = []

        all_results = db_results + valid_results
        unique_vacancies = list({v["url"]: v for v in all_results if v.get("url")}.values())

        if not unique_vacancies:
            await safe_edit_status(status_msg, "❌ Совпадений в базе и рабочих группах не найдено.")
            return

    await safe_edit_status(status_msg, f"🧠 [3/3] Матричный скоринг {len(unique_vacancies[:3])} кандидатов...")

    compact_candidates = [
        {
            "id": idx,
            "company": v['company'],
            "source": v['source'],
            "url": v['url'],
            "overlap_percent": v.get('overlap', 0),
            "text": v['desc'][:200]
        }
        for idx, v in enumerate(unique_vacancies[:3], 1)
    ]

    prompt_matrix = f"""Ты — senior технический рекрутер. Сравни кандидатов с брифом.
Верни ТОЛЬКО валидный JSON со списком проверенных кандидатов. Никакого лишнего текста.

БРИФ:
{user_text[:350]}

КАНДИДАТЫ:
{json.dumps(compact_candidates, ensure_ascii=False)}

Формат JSON ответа:
{{
  "results": [
    {{
      "company": "Название компании или чата",
      "score": 85,
      "status": "Точный оригинал",
      "role": "Название должности",
      "salary": "Вилка или Не указана",
      "url": "ссылка",
      "source": "источник",
      "stack_match": "кратко совпадение стека",
      "challenge_match": "кратко задачи",
      "target_lpr": "Роль ЛПР (CTO, Lead Frontend)",
      "pain_point": "боль проекта",
      "hook": "предложение для первого контакта"
    }}
  ]
}}"""

    result_json_str, err = await call_groq_async(prompt_matrix, max_tokens=1200, json_mode=True)

    output_lines = ["🎯 **ОТЧЁТ МАТРИЧНОГО СРАВНЕНИЯ**\n"]
    parsed_candidates = []

    try:
        parsed_data = json.loads(result_json_str)
        parsed_candidates = parsed_data.get("results", [])
    except Exception:
        pass

    if not parsed_candidates:
        for v in unique_vacancies[:3]:
            score = 90 if v.get("overlap", 0) > 20 else 45
            status = "🟢 Точный оригинал" if score > 80 else "🟠 Возможное совпадение"
            parsed_candidates.append({
                "company": v["company"],
                "score": score,
                "status": status,
                "role": "Позиция из брифа",
                "salary": v.get("salary", "Не указана"),
                "url": v["url"],
                "source": v["source"],
                "stack_match": f"Текстовое совпадение: {v.get('overlap', 0)}%",
                "challenge_match": "Совпадение по ключевым маркерам",
                "target_lpr": "CTO / Head of Engineering",
                "pain_point": "Поиск профильных инженеров",
                "hook": "Добрый день! Увидели вашу открытую потребность в профильном сообществе..."
            })

    for item in parsed_candidates:
        company = item.get("company", "Не указана")
        score = item.get("score", 50)
        status = item.get("status", "Среднее сходство")
        if not status.startswith("🟢") and not status.startswith("🟡") and not status.startswith("🟠"):
            status = "🟢 " + status if score >= 80 else "🟡 " + status if score >= 60 else "🟠 " + status

        role = item.get("role", "Разработчик")
        salary = item.get("salary", "Не указана")
        url = item.get("url", "#")
        source = item.get("source", "Telegram")
        stack = item.get("stack_match", "Частичное совпадение")
        challenge = item.get("challenge_match", "Общий профиль")
        lpr = item.get("target_lpr", "CTO")
        pain = item.get("pain_point", "Высокая нагрузка")
        hook = item.get("hook", "Здравствуйте!")

        osint_part = ""
        if company and not company.startswith("@") and "группа" not in company.lower() and "чат" not in company.lower():
            osint_url = build_lead_osint_url(company)
            osint_part = f"• **Поиск контактов ЛПР:** [Найти профили в LinkedIn/Google]({osint_url})\n"

        block = (
            f"🏢 **КОМПАНИЯ:** {company}\n"
            f"🎯 **Матричный скор:** {score}%\n"
            f"🎲 **Статус деанонимизации:** {status}\n"
            f"📌 **Позиция:** {role}\n"
            f"💰 **Зарплата:** {salary}\n"
            f"🔗 **Ссылка:** [Открыть источник/пост]({url})\n"
            f"🏛 **Источник:** {source}\n\n"
            f"⚖️ **Аудит по матрице:**\n"
            f"• **Совпадение стека:** {stack}\n"
            f"• **Сравнение с брифом:** {challenge}\n\n"
            f"💡 **Стратегия выхода для сейлза:**\n"
            f"• **К кому идти:** {lpr}\n"
            f"{osint_part}"
            f"• **Болевая точка:** {pain}\n"
            f"• **Холодный хук:** {hook}\n"
            f"──────────────────────────────\n"
        )
        output_lines.append(block)

    final_output = "\n".join(output_lines)

    try:
        if len(final_output) > 4000:
            parts = [final_output[i:i+4000] for i in range(0, len(final_output), 4000)]
            await safe_edit_status(status_msg, parts[0])
            for p in parts[1:]:
                await message.answer(p, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        else:
            await safe_edit_status(status_msg, final_output)
    except Exception:
        await safe_edit_status(status_msg, final_output.replace("*", "").replace("`", ""))


# ----------------- БЕЗОПАСНЫЙ СТАРТ -----------------
async def init_telethon():
    global telethon_client
    if not telethon_client:
        return
    try:
        await asyncio.wait_for(telethon_client.connect(), timeout=4.0)
        if not await telethon_client.is_user_authorized():
            logger.warning("Telethon не авторизован.")
            telethon_client = None
        else:
            logger.info("Telethon успешно авторизован! Регистрация фонового слушателя...")
            register_telethon_listener()
    except Exception as e:
        logger.warning(f"Telethon пропущен: {e}")
        telethon_client = None


async def main():
    logger.info("Инициализация сервиса...")
    threading.Thread(target=run_health_server, daemon=True).start()
    await init_db()
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
