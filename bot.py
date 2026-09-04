import asyncio
import json
import logging
import os
import re
import ssl
import sys
import threading
from urllib.parse import quote_plus
from http.server import HTTPServer, BaseHTTPRequestHandler
import asyncpg
import httpx
import defusedxml.ElementTree as ET
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from groq import AsyncGroq
from telethon import TelegramClient, events
from telethon.tl.types import User
from telethon.tl.functions.messages import GetDialogFiltersRequest
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

ACTIVE_FOLDERS = {}
ALLOWED_CHAT_IDS = set()

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

STOP_WORDS = {
    "для", "или", "как", "все", "при", "опыт", "работа", "года", "знание",
    "умение", "будет", "проект", "команда", "области", "должен", "также",
    "после", "более", "свыше", "общий", "желательно", "одной", "нескольких",
    "базе", "задачи", "плюсом", "уровень", "описание", "навыки", "обязательные"
}

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


# ----------------- РАБОТА С SUPABASE -----------------
async def init_db():
    global db_pool, ACTIVE_FOLDERS
    if not DATABASE_URL:
        logger.warning("DATABASE_URL не задан, работа с БД отключена.")
        return
    try:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        clean_url = DATABASE_URL.strip().replace("?sslmode=require", "")
        db_pool = await asyncpg.create_pool(
            clean_url,
            ssl=ssl_ctx,
            min_size=1,
            max_size=4,
            timeout=10.0
        )
        logger.info("Подключение к Supabase PostgreSQL успешно установлено!")

        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT folder_id, folder_title FROM active_folders;")
            ACTIVE_FOLDERS = {r["folder_id"]: r["folder_title"] for r in rows}
            logger.info(f"Загружено сохраненных папок из БД: {list(ACTIVE_FOLDERS.values())}")
    except Exception as e:
        logger.error(f"Не удалось подключиться к Supabase: {e}")
        db_pool = None


async def sync_folder_to_db(folder_id: int, folder_title: str, add: bool):
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            if add:
                await conn.execute(
                    "INSERT INTO active_folders (folder_id, folder_title) VALUES ($1, $2) ON CONFLICT (folder_id) DO UPDATE SET folder_title = $2;",
                    folder_id, folder_title
                )
            else:
                await conn.execute("DELETE FROM active_folders WHERE folder_id = $1;", folder_id)
    except Exception as e:
        logger.error(f"Ошибка синхронизации папки с БД: {e}")


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
        like_clauses = " OR ".join([f"post_text ILIKE ${i+1}" for i in range(len(terms))])
        params = [f"%{t}%" for t in terms]
        query = f"""
            SELECT chat_title, post_text, post_url 
            FROM vacancies 
            WHERE {like_clauses} 
            ORDER BY id DESC LIMIT 20;
        """
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            for row in rows:
                p_text = row['post_text']
                overlap = calculate_overlap_score(raw_brief, p_text)
                company_match = re.search(r"(?:в компанию|компания|проект|заказчик|в команду):\s*([A-Za-zА-Яа-я0-9_\-\s]{3,30})", p_text, re.IGNORECASE)
                company = company_match.group(1).strip() if company_match else row['chat_title']
                results.append({
                    "source": f"Supabase: {row['chat_title'][:20]}",
                    "title": f"Пост в {row['chat_title'][:20]}",
                    "company": company,
                    "salary": "в тексте",
                    "url": row['post_url'],
                    "desc": p_text[:400],
                    "overlap": overlap
                })
    except Exception as e:
        logger.warning(f"Ошибка поиска в Supabase: {e}")
    return results


# ----------------- РАБОТА С ПАПКАМИ TELEGRAM -----------------
def normalize_id(tg_id: int) -> int:
    s = str(tg_id)
    if s.startswith("-100"):
        return int(s[4:])
    elif s.startswith("-"):
        return int(s[1:])
    return tg_id


async def refresh_all_allowed_chats():
    global ALLOWED_CHAT_IDS
    if not telethon_client or not telethon_client.is_connected():
        return

    if not ACTIVE_FOLDERS:
        ALLOWED_CHAT_IDS = set()
        return

    try:
        filters_result = await telethon_client(GetDialogFiltersRequest())
        chat_ids = set()

        for f in filters_result.filters:
            f_id = getattr(f, 'id', None)
            if f_id in ACTIVE_FOLDERS:
                peers = getattr(f, 'include_peers', []) + getattr(f, 'pinned_peers', [])
                for peer in peers:
                    try:
                        entity = await telethon_client.get_entity(peer)
                        norm_id = normalize_id(entity.id)
                        chat_ids.add(norm_id)
                    except Exception:
                        continue

        ALLOWED_CHAT_IDS = chat_ids
        logger.info(f"Объединено чатов из {len(ACTIVE_FOLDERS)} выбранных папок: {len(ALLOWED_CHAT_IDS)}")
    except Exception as e:
        logger.error(f"Ошибка пересчета чатов папок: {e}")


async def build_folders_keyboard() -> tuple[str, InlineKeyboardMarkup]:
    filters_result = await telethon_client(GetDialogFiltersRequest())
    keyboard = []

    reset_icon = "🔘" if not ACTIVE_FOLDERS else "⚪️"
    keyboard.append([InlineKeyboardButton(text=f"{reset_icon} Все чаты (без ограничений)", callback_data="f_toggle:0")])

    for f in filters_result.filters:
        f_id = getattr(f, 'id', None)
        f_title = getattr(f, 'title', None)
        if hasattr(f_title, 'text'):
            f_title = f_title.text

        if f_id and f_title:
            is_active = f_id in ACTIVE_FOLDERS
            icon = "☑️" if is_active else "◻️"
            btn_text = f"{icon} {f_title}"
            keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"f_toggle:{f_id}")])

    if ACTIVE_FOLDERS:
        selected_names = ", ".join([f"`{name}`" for name in ACTIVE_FOLDERS.values()])
        status_text = f"📂 Выбрано папок: **{len(ACTIVE_FOLDERS)}** ({selected_names})\n💬 Чатов под фильтром: **{len(ALLOWED_CHAT_IDS)}**"
    else:
        status_text = "🌐 Режим: **Все чаты без ограничений**"

    text = (
        f"📁 **ВЫБОР ПАПОК ДЛЯ СКАНИРОВАНИЯ**\n\n"
        f"{status_text}\n\n"
        f"Нажмите на папку, чтобы включить (☑️) или исключить (◻️) её из поиска:"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


# ----------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ -----------------
def clean_html(raw_html: str) -> str:
    return " ".join(HTML_TAG_RE.sub(" ", raw_html).split())


def extract_key_tokens(text: str) -> set:
    words = re.findall(r'[A-Za-zА-Яа-я0-9\+\#]{3,}', text.lower())
    return {w for w in words if w not in STOP_WORDS}


def extract_search_terms(text: str) -> list:
    raw_cleaned = text.replace("/", " ").replace("-", " ")
    abbrs = re.findall(r'\b[A-ZА-Я]{3,}\b', text)
    tech_stack = re.findall(r'\b[A-Za-z]{3,}\b', text)
    ru_tokens = list(extract_key_tokens(raw_cleaned))
    ru_tokens.sort(key=lambda x: len(x), reverse=True)

    terms = []
    for a in abbrs:
        if a.lower() not in STOP_WORDS and a not in terms:
            terms.append(a)

    for tech in tech_stack:
        t_title = tech.capitalize()
        if tech.lower() not in STOP_WORDS and t_title not in terms:
            terms.append(t_title)

    for t in ru_tokens:
        if t not in terms and len(terms) < 6:
            terms.append(t)

    return terms[:6] if terms else ["разработчик"]


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


async def call_groq_async(prompt: str, max_tokens: int = 1800, json_mode: bool = False) -> tuple[str, str]:
    global ACTIVE_GROQ_MODEL
    if not ACTIVE_GROQ_MODEL:
        await find_working_groq_model()

    safe_prompt = prompt if len(prompt) < 7000 else prompt[:7000]
    kwargs = {
        "model": ACTIVE_GROQ_MODEL,
        "messages": [{"role": "user", "content": safe_prompt}],
        "temperature": 0.2,
        "presence_penalty": 0.3,
        "frequency_penalty": 0.3,
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        res = await asyncio.wait_for(groq_client.chat.completions.create(**kwargs), timeout=14.0)
        content = res.choices[0].message.content
        if content and content.strip():
            return content, ""
    except Exception as e:
        err_msg = str(e)
        if "413" in err_msg or "too large" in err_msg.lower():
            try:
                kwargs["messages"] = [{"role": "user", "content": safe_prompt[:3500]}]
                res = await asyncio.wait_for(groq_client.chat.completions.create(**kwargs), timeout=8.0)
                return res.choices[0].message.content, ""
            except Exception as e2:
                return "", str(e2)
        return "", err_msg

    return "", "Empty response"


# ----------------- ПАРСИНГ ВНЕШНИХ ИСТОЧНИКОВ -----------------
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
                "desc": desc_text[:300],
                "overlap": 0.0
            })
        return jobs
    except Exception:
        return []


async def fetch_vc_vacancies(client: httpx.AsyncClient, query: str) -> list:
    try:
        url = "https://api.vc.ru/v2.8/vacancies"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = await client.get(url, headers=headers, timeout=2.5)
        if r.status_code != 200:
            return []
        jobs = []
        q_lower = query.lower()
        items = r.json().get("result", {}).get("items", [])
        for item in items:
            title = item.get("title", "")
            desc = clean_html(item.get("description", ""))
            if q_lower in title.lower() or q_lower in desc.lower():
                company = item.get("company", {}).get("name", "Компания с VC")
                sal = item.get("salary", {}).get("title", "не указана")
                post_url = f"https://vc.ru/job/{item.get('id', '')}"
                if is_agency(company, desc):
                    continue
                jobs.append({
                    "source": "VC.ru Вакансии",
                    "title": title,
                    "company": company,
                    "salary": sal,
                    "url": post_url,
                    "desc": desc[:300],
                    "overlap": 0.0
                })
        return jobs[:3]
    except Exception:
        return []


async def fetch_geeklink_rss(client: httpx.AsyncClient, query: str) -> list:
    try:
        url = "https://geeklink.io/feed/"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        r = await client.get(url, headers=headers, timeout=3.0)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
        jobs = []
        q_lower = query.lower()
        for item in root.findall(".//item"):
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            desc = clean_html(item.findtext("description", ""))
            if q_lower in title.lower() or q_lower in desc.lower():
                company_match = re.search(r"в\s+([A-Za-zА-Яа-я0-9_\-\s]{2,25})", title)
                company = company_match.group(1).strip() if company_match else "IT Компания"
                if is_agency(company, desc):
                    continue
                jobs.append({
                    "source": "GeekLink",
                    "title": title,
                    "company": company,
                    "salary": "в описании",
                    "url": link,
                    "desc": desc[:300],
                    "overlap": 0.0
                })
        return jobs[:3]
    except Exception:
        return []


async def fetch_finder_vc(client: httpx.AsyncClient, query: str) -> list:
    try:
        url = f"https://finder.vc/api/vacancies?search={quote_plus(query)}"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = await client.get(url, headers=headers, timeout=2.5)
        if r.status_code != 200:
            return []
        data = r.json().get("data", [])
        jobs = []
        for item in data[:3]:
            title = item.get("title", "")
            desc = clean_html(item.get("description", ""))
            company = item.get("company_name", "Удалённый проект")
            sal = item.get("salary_text", "не указана")
            slug = item.get("slug", "")
            full_url = f"https://finder.vc/vacancies/{slug}" if slug else "https://finder.vc"
            if is_agency(company, desc):
                continue
            jobs.append({
                "source": "Finder.vc (Remote)",
                "title": title,
                "company": company,
                "salary": sal,
                "url": full_url,
                "desc": desc[:300],
                "overlap": 0.0
            })
        return jobs
    except Exception:
        return []


# ----------------- ОНЛАЙН-ПОИСК В TELEGRAM -----------------
async def search_joined_chats_global(search_terms: list, raw_brief: str, bot_id: int) -> list:
    if not telethon_client or not telethon_client.is_connected():
        return []

    found_posts = []
    seen_ids = set()
    chat_targets = list(ALLOWED_CHAT_IDS) if ALLOWED_CHAT_IDS else [None]

    for target_chat in chat_targets:
        for term in search_terms:
            clean_t = term.strip()
            if len(clean_t) < 3:
                continue

            try:
                limit_scan = 40 if target_chat else 20
                async for message in telethon_client.iter_messages(target_chat, search=clean_t, limit=limit_scan):
                    if not message.text or len(message.text) < 40:
                        continue

                    if message.chat_id == bot_id:
                        continue

                    chat = await message.get_chat()
                    if isinstance(chat, User) or getattr(chat, 'is_user', False):
                        continue

                    if ALLOWED_CHAT_IDS and normalize_id(chat.id) not in ALLOWED_CHAT_IDS:
                        continue

                    chat_title = getattr(chat, 'title', getattr(chat, 'username', 'Канал'))
                    if "matcher" in chat_title.lower() or "bot" in chat_title.lower():
                        continue

                    unique_key = f"{normalize_id(message.chat_id)}_{message.id}"
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

                    asyncio.create_task(save_vacancy_to_db(chat_title, message.id, message.chat_id, post_text, msg_url))

                    company_match = re.search(r"(?:в компанию|компания|проект|заказчик|в команду):\s*([A-Za-zА-Яа-я0-9_\-\s]{3,30})", post_text, re.IGNORECASE)
                    company = company_match.group(1).strip() if company_match else chat_title

                    found_posts.append({
                        "source": f"Telegram: {chat_title[:20]}",
                        "title": f"Пост в {chat_title[:20]}",
                        "company": company,
                        "salary": "в тексте",
                        "url": msg_url,
                        "desc": post_text[:400],
                        "overlap": overlap
                    })
            except Exception as e:
                logger.debug(f"Поиск в {target_chat}: {e}")

    found_posts.sort(key=lambda x: x.get("overlap", 0), reverse=True)
    return found_posts[:12]


# ----------------- ФОНОВЫЙ СЛУШАТЕЛЬ В SUPABASE -----------------
def register_telethon_listener():
    if not telethon_client:
        return

    @telethon_client.on(events.NewMessage)
    async def handler_new_message(event):
        try:
            if event.is_private or not event.text or len(event.text) < 30:
                return

            if ALLOWED_CHAT_IDS and normalize_id(event.chat_id) not in ALLOWED_CHAT_IDS:
                return

            text_lower = event.text.lower()
            markers = ("вакансия", "ищем", "senior", "middle", "lead", "remote", "developer", "инженер", "асутп", "devops", "kubernetes")
            if any(k in text_lower for k in markers):
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
        "💼 **Multi-Source OSINT Lead Hunter**\n\n"
        "Отправьте бриф или описание вакансии — бот выполнит поиск по выбранным папкам, "
        "базе Supabase и внешним источникам, выдаст до 5 вариантов по убыванию соответствия "
        "и попытается сдеанонить конечного заказчика для лидирующей позиции.\n\n"
        "Команды:\n"
        "• /set_folder — выбор папок Telegram для поиска\n"
        "• /debug_tg — статус базы и папок",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text == "/set_folder")
async def cmd_set_folder(message: Message):
    if not telethon_client or not telethon_client.is_connected():
        await message.answer("⚠️ Telethon не подключен к Telegram.", parse_mode=None)
        return

    status = await message.answer("🔄 Загружаю ваши папки...")
    try:
        await refresh_all_allowed_chats()
        text, markup = await build_folders_keyboard()
        await status.edit_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await status.edit_text(f"⚠️ Ошибка получения папок: {e}", parse_mode=None)


@dp.callback_query(F.data.startswith("f_toggle:"))
async def handle_folder_toggle_callback(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    global ACTIVE_FOLDERS

    if filter_id == 0:
        for f_id in list(ACTIVE_FOLDERS.keys()):
            await sync_folder_to_db(f_id, "", add=False)
        ACTIVE_FOLDERS.clear()
        await refresh_all_allowed_chats()
        await call.answer("Режим сброшен: сканируются все группы ✅", show_alert=False)
    else:
        filters_result = await telethon_client(GetDialogFiltersRequest())
        f_title = f"Папка {filter_id}"
        for f in filters_result.filters:
            if getattr(f, 'id', None) == filter_id:
                title_obj = getattr(f, 'title', None)
                f_title = title_obj.text if hasattr(title_obj, 'text') else str(title_obj)
                break

        if filter_id in ACTIVE_FOLDERS:
            del ACTIVE_FOLDERS[filter_id]
            await sync_folder_to_db(filter_id, f_title, add=False)
            await call.answer(f"Папка '{f_title}' выключена ◻️", show_alert=False)
        else:
            ACTIVE_FOLDERS[filter_id] = f_title
            await sync_folder_to_db(filter_id, f_title, add=True)
            await call.answer(f"Папка '{f_title}' включена ☑️", show_alert=False)

        await refresh_all_allowed_chats()

    try:
        text, markup = await build_folders_keyboard()
        await call.message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass


@dp.message(F.text == "/debug_tg")
async def cmd_debug(message: Message):
    db_status = "✅ Подключена" if db_pool else "❌ Не подключена"
    if not telethon_client or not telethon_client.is_connected():
        await message.answer(f"🗄 База данных Supabase: {db_status}\nTelethon: ⚠️ Не подключен к Telegram.", parse_mode=None)
        return
    try:
        me = await telethon_client.get_me()
        if ACTIVE_FOLDERS:
            folders_str = ", ".join([f"**{name}**" for name in ACTIVE_FOLDERS.values()])
            folder_info = f"📁 Активные папки: {folders_str}\n💬 Чатов под фильтром: **{len(ALLOWED_CHAT_IDS)}**"
        else:
            folder_info = "🌐 Охват: **Все чаты без фильтра**"

        response = (
            f"🗄 **База данных Supabase:** {db_status}\n"
            f"👤 **Telethon аккаунт:** {me.first_name} (@{me.username})\n"
            f"{folder_info}\n\n"
            f"🔒 *Личные диалоги (1-на-1) исключены из поиска.*"
        )
        await message.answer(response, parse_mode=ParseMode.MARKDOWN)
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

    status_msg = await message.answer("⚡️ [1/3] Извлечение ключевых маркеров...")

    search_terms = extract_search_terms(user_text)
    primary_query = search_terms[0] if search_terms else "разработчик"
    scope_desc = f"{len(ACTIVE_FOLDERS)} папкам" if ACTIVE_FOLDERS else "всем чатам"

    await safe_edit_status(status_msg, f"🔍 [2/3] Глубокий сбор по Telegram ({scope_desc}), Supabase, VC, GeekLink...")

    # 1. Поиск по базе Supabase
    db_results = await search_vacancies_in_db(search_terms, user_text)

    # 2. Параллельный сбор по всем источникам
    async with httpx.AsyncClient(follow_redirects=True) as http_client:
        tasks = [
            search_joined_chats_global(search_terms, user_text, BOT_USER_ID),
            fetch_habr(http_client, primary_query, 4),
            fetch_vc_vacancies(http_client, primary_query),
            fetch_geeklink_rss(http_client, primary_query),
            fetch_finder_vc(http_client, primary_query),
        ]

        try:
            raw_results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=16.0)
            valid_results = [item for sub in raw_results if isinstance(sub, list) for item in sub]
        except Exception as e:
            logger.error(f"Ошибка параллельного поиска: {e}")
            valid_results = []

        all_results = db_results + valid_results
        unique_vacancies = list({v["url"]: v for v in all_results if v.get("url")}.values())

        if not unique_vacancies:
            await safe_edit_status(status_msg, "❌ Совпадений по источникам не найдено.")
            return

    # Берем до 8 сырых кандидатов на вход модели
    candidates_pool = unique_vacancies[:8]

    await safe_edit_status(status_msg, f"🧠 [3/3] Матричный скоринг {len(candidates_pool)} лидов и OSINT-анализ...")

    compact_candidates = [
        {
            "id": idx,
            "company": v['company'],
            "source": v['source'],
            "url": v['url'],
            "overlap_percent": v.get('overlap', 0),
            "text": v['desc'][:260]
        }
        for idx, v in enumerate(candidates_pool, 1)
    ]

    prompt_matrix = f"""Ты — Senior IT-Sales и детектив OSINT по закрытым вакансиям. Оцени кандидатов на соответствие брифу.
Верни ТОЛЬКО валидный JSON со списком проверенных позиций (выдай до 5 позиций).
ОБЯЗАТЕЛЬНО: для кандидата с самым высоким скором (топ-1) сделай предположение, кто может быть реальным конечным клиентом/заказчиком позиции (укажи сферу или конкретные компании типа Яндекс, Сбер, X5, Ростелеком, промышленный холдинг и т.д. на основе формулировок задач и стека) в поле "end_client_guess".

БРИФ:
{user_text[:450]}

КАНДИДАТЫ:
{json.dumps(compact_candidates, ensure_ascii=False)}

Формат ответа:
{{
  "results": [
    {{
      "company": "Название компании/чата",
      "score": 92,
      "role": "Точная позиция",
      "salary": "Вилка или Не указана",
      "url": "ссылка",
      "source": "Точное название источника",
      "stack_match": "совпадение по технологиям",
      "challenge_match": "основные задачи",
      "target_lpr": "Роль ЛПР (CTO, Lead DevOps)",
      "pain_point": "ключевая проблема проекта",
      "hook": "точный хук для первого сообщения",
      "end_client_guess": "Только для топ-1: Название или профиль конечного заказчика + почему"
    }}
  ]
}}"""

    result_json_str, err = await call_groq_async(prompt_matrix, max_tokens=1800, json_mode=True)
    parsed_candidates = []

    try:
        parsed_data = json.loads(result_json_str)
        parsed_candidates = parsed_data.get("results", [])
    except Exception:
        pass

    if not parsed_candidates:
        for idx, v in enumerate(candidates_pool[:5], 1):
            score = 90 if v.get("overlap", 0) > 20 else max(40, 85 - idx * 10)
            parsed_candidates.append({
                "company": v["company"],
                "score": score,
                "role": "Профильная позиция",
                "salary": v.get("salary", "Не указана"),
                "url": v["url"],
                "source": v["source"],
                "stack_match": f"Текстовый overlap: {v.get('overlap', 0)}%",
                "challenge_match": "Совпадение по стеку технологий",
                "target_lpr": "CTO / Head of Engineering",
                "pain_point": "Закрытие потребности в инженерах",
                "hook": "Добрый день! Увидели вашу открытую позицию...",
                "end_client_guess": "Финтех / Крупный enterprise-холдинг со своим on-premise контуром" if idx == 1 else ""
            })

    # СТРОГАЯ СОРТИРОВКА: Сверху максимальное совпадение, далее по убыванию
    parsed_candidates.sort(key=lambda x: int(x.get("score", 0)), reverse=True)
    final_top = parsed_candidates[:5]

    await safe_edit_status(status_msg, f"🏁 Найдено **{len(final_top)}** релевантных позиций (отсортировано от лучшего совпадения):")

    for rank, item in enumerate(final_top, 1):
        company = item.get("company", "Не указана")
        score = int(item.get("score", 50))
        role = item.get("role", "Инженер / Разработчик")
        salary = item.get("salary", "Не указана")
        url = item.get("url", "#")
        source = item.get("source", "Telegram")
        stack = item.get("stack_match", "Частичное совпадение")
        challenge = item.get("challenge_match", "Общий стек")
        lpr = item.get("target_lpr", "CTO")
        pain = item.get("pain_point", "Высокая нагрузка")
        hook = item.get("hook", "Здравствуйте!")
        client_guess = item.get("end_client_guess", "").strip()

        badge = "🔥" if score >= 85 else "⚡️" if score >= 65 else "🔎"
        status_label = "Точный оригинал" if score >= 85 else "Высокое соответствие" if score >= 65 else "Частичное совпадение"

        guess_part = ""
        if rank == 1 and client_guess:
            guess_part = f"🕵️ **Предполагаемый конечный клиент:**\n_{client_guess}_\n\n"

        card = (
            f"**#{rank} {badge} {company}** — `{score}% совпадение`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏛 **Источник:** `{source}`\n"
            f"🎯 **Статус:** {status_label}\n"
            f"📌 **Позиция:** {role}\n"
            f"💰 **Вилка:** {salary}\n\n"
            f"{guess_part}"
            f"⚙️ **Аудит стека:**\n"
            f"• **Технологии:** {stack}\n"
            f"• **Задачи:** {challenge}\n\n"
            f"💼 **Стратегия выхода для сейлза:**\n"
            f"• **К кому идти:** {lpr}\n"
            f"• **Боль заказчика:** {pain}\n"
            f"• **Хук:** _{hook}_\n"
        )

        buttons = [[InlineKeyboardButton(text=f"🔗 Открыть в {source[:20]}", url=url)]]
        if company and not company.startswith("@") and "чат" not in company.lower() and "канал" not in company.lower():
            buttons.append([InlineKeyboardButton(text="🕵️ Найти ЛПР в LinkedIn", url=build_lead_osint_url(company))])

        reply_markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(card, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)


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
            await refresh_all_allowed_chats()
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
