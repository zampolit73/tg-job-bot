import asyncio
import hashlib
import json
import logging
import os
import re
import ssl
import sys
import threading
from urllib.parse import quote_plus, unquote
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
    "базе", "задачи", "плюсом", "уровень", "описание", "навыки", "обязательные",
    "ищем", "компанию", "требуется", "формат", "локация", "ставка", "грейд", "мы"
}

TECH_SYNONYMS = {
    "kubernetes": ["k8s", "kube", "кубер", "openshift", "helm"],
    "k8s": ["kubernetes", "kube", "кубер", "helm"],
    "devops": ["sre", "platform engineer", "девопс", "инфраструктур", "ci/cd"],
    "sre": ["devops", "platform engineer"],
    "postgresql": ["postgres", "psql", "постгрес"],
    "postgres": ["postgresql", "psql"],
    "golang": ["go", "голанг"],
    "go": ["golang", "голанг"],
    "python": ["питон", "пайтон", "django", "fastapi"],
    "java": ["джава", "spring", "springboot"],
    "frontend": ["фронтенд", "react", "vue", "typescript"],
    "react": ["frontend", "фронтенд", "nextjs"],
    "qa": ["тестировщик", "тестирование", "autotests", "автотест"],
    "linux": ["линукс", "ubuntu", "debian", "centos", "redhat", "astralinux"],
    "docker": ["докер", "containerd"],
    "ansible": ["ансибл", "terraform", "iac"],
    "terraform": ["ansible", "iac"]
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


# ----------------- СЕМАНТИЧЕСКИЙ ОТПЕЧАТОК -----------------
def generate_content_hash(text: str) -> str:
    clean_text = HTML_TAG_RE.sub(" ", text.lower())
    words = re.findall(r'[a-zа-я0-9\+\#]{3,}', clean_text)
    meaningful_words = sorted(list({w for w in words if w not in STOP_WORDS}))
    signature = " ".join(meaningful_words[:30])
    return hashlib.md5(signature.encode("utf-8")).hexdigest()


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
            await conn.execute("""
                ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64);
                CREATE INDEX IF NOT EXISTS idx_vacancies_content_hash ON vacancies (content_hash);
            """)

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


async def save_vacancy_to_db(chat_title: str, msg_id: int, chat_id: int, text: str, url: str, fwd_source: str = ""):
    if not db_pool:
        return
    try:
        content_hash = generate_content_hash(text)
        saved_text = f"[FWD: {fwd_source}] {text}" if fwd_source else text

        async with db_pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id, chat_title FROM vacancies WHERE content_hash = $1 LIMIT 1;",
                content_hash
            )

            if existing:
                old_title = existing['chat_title']
                if chat_title not in old_title:
                    new_title = f"{old_title}, {chat_title}"[:80]
                    await conn.execute(
                        "UPDATE vacancies SET chat_title = $1 WHERE id = $2;",
                        new_title, existing['id']
                    )
                return

            await conn.execute("""
                INSERT INTO vacancies (chat_title, message_id, chat_id, post_text, post_url, content_hash)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (chat_id, message_id) DO NOTHING;
            """, chat_title, msg_id, chat_id, saved_text, url, content_hash)
    except Exception as e:
        logger.debug(f"Ошибка сохранения вакансии в БД: {e}")


async def search_vacancies_in_db(terms: list, raw_brief: str) -> list:
    if not db_pool or not terms:
        return []
    results = []
    seen_hashes = set()
    try:
        like_clauses = " OR ".join([f"post_text ILIKE ${i+1}" for i in range(len(terms))])
        params = [f"%{t}%" for t in terms]
        query = f"""
            SELECT chat_title, post_text, post_url, content_hash 
            FROM vacancies 
            WHERE {like_clauses} 
            ORDER BY id DESC LIMIT 35;
        """
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            for row in rows:
                c_hash = row.get('content_hash') or generate_content_hash(row['post_text'])
                if c_hash in seen_hashes:
                    continue
                seen_hashes.add(c_hash)

                p_text = row['post_text']
                overlap = calculate_overlap_score(raw_brief, p_text)
                
                fwd_info = ""
                fwd_match = re.search(r"^\[FWD:\s*([^\]]+)\]", p_text)
                if fwd_match:
                    fwd_info = fwd_match.group(1).strip()
                    p_text = p_text.replace(fwd_match.group(0), "").strip()

                company_match = re.search(r"(?:в компанию|компания|проект|заказчик|в команду):\s*([A-Za-zА-Яа-я0-9_\-\s]{3,30})", p_text, re.IGNORECASE)
                company = company_match.group(1).strip() if company_match else row['chat_title'].split(",")[0].strip()
                
                results.append({
                    "source": row['chat_title'][:30],
                    "title": f"Пост в {row['chat_title'][:30]}",
                    "company": company,
                    "salary": "в тексте",
                    "url": row['post_url'],
                    "desc": p_text[:400],
                    "overlap": overlap,
                    "fwd_source": fwd_info,
                    "content_hash": c_hash,
                    "is_external": False
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
    keyboard.append([InlineKeyboardButton(text=f"{reset_icon} Все чаты (без ограничений)", callbackdata="f_toggle:0")])

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


# ----------------- ИСПРАВЛЕНИЕ 1 И 3: ЯКОРНЫЙ ПОИСК И MIN-OVERLAP -----------------
def clean_html(raw_html: str) -> str:
    return " ".join(HTML_TAG_RE.sub(" ", raw_html).split())


def extract_key_tokens(text: str) -> set:
    words = re.findall(r'[A-Za-zА-Яа-я0-9\+\#]{2,}', text.lower())
    return {w for w in words if w not in STOP_WORDS}


# ИСПРАВЛЕНИЕ 1: Извлекаем устойчивые связки терминов (вместо обрезки до 2 слов)
def extract_anchor_phrases(text: str) -> list:
    clean = text.replace("\n", " ")
    tech_matches = re.findall(r'\b[A-Za-z\+\#]{2,}\b', clean)
    unique_tech = []
    for t in tech_matches:
        if t.lower() not in STOP_WORDS and t.lower() not in [x.lower() for x in unique_tech]:
            unique_tech.append(t)

    queries = []
    if len(unique_tech) >= 3:
        queries.append(" ".join(unique_tech[:3]))  # например: "Kubernetes Helm Postgres"
    if len(unique_tech) >= 2:
        queries.append(" ".join(unique_tech[:2]))  # например: "Kubernetes Helm"
    if unique_tech:
        queries.append(unique_tech[0])

    return queries


def expand_search_terms(text: str) -> list:
    tokens = list(extract_key_tokens(text))
    expanded = []
    for token in tokens:
        if token not in expanded:
            expanded.append(token)
        for syn in TECH_SYNONYMS.get(token, []):
            if syn not in expanded:
                expanded.append(syn)
    return expanded[:8] if expanded else ["разработчик"]


# ИСПРАВЛЕНИЕ 3: Двусторонний расчет overlap (без штрафа за разницу в длине текста)
def calculate_overlap_score(brief_text: str, candidate_text: str) -> float:
    brief_tokens = extract_key_tokens(brief_text)
    cand_tokens = extract_key_tokens(candidate_text)
    if not brief_tokens or not cand_tokens:
        return 0.0

    augmented_cand = set(cand_tokens)
    for ct in cand_tokens:
        if ct in TECH_SYNONYMS:
            augmented_cand.update(TECH_SYNONYMS[ct])

    matches = 0
    for bt in brief_tokens:
        syns = set(TECH_SYNONYMS.get(bt, []))
        syns.add(bt)
        if any(s in augmented_cand for s in syns):
            matches += 1

    # Важно: берем min(), чтобы короткий пост из чата получал справедливые 80-100%
    base_len = min(len(brief_tokens), len(cand_tokens))
    return round((matches / base_len) * 100, 1)


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


async def extract_forward_metadata(message) -> str:
    if not message.fwd_from:
        return ""
    try:
        fwd = message.fwd_from
        if getattr(fwd, 'from_name', None):
            return f"Переслано от: {fwd.from_name}"
        if getattr(fwd, 'from_id', None):
            try:
                entity = await telethon_client.get_entity(fwd.from_id)
                title = getattr(entity, 'title', getattr(entity, 'username', getattr(entity, 'first_name', '')))
                username = f"(@{entity.username})" if getattr(entity, 'username', None) else ""
                return f"Канал-первоисточник: {title} {username}".strip()
            except Exception:
                pass
        if getattr(fwd, 'post_author', None):
            return f"Автор оригинального поста: {fwd.post_author}"
    except Exception:
        pass
    return ""


# ----------------- ИСПРАВЛЕНИЕ 2: ГЛУБОКИЙ ПОИСК В TELEGRAM (ДО 100 ПОСТОВ) -----------------
async def search_joined_chats_deep(raw_brief: str, bot_id: int) -> list:
    if not telethon_client or not telethon_client.is_connected():
        return []

    found_posts = []
    seen_ids = set()
    chat_targets = list(ALLOWED_CHAT_IDS) if ALLOWED_CHAT_IDS else [None]

    search_queries = extract_anchor_phrases(raw_brief)
    if not search_queries:
        search_queries = expand_search_terms(raw_brief)[:3]

    for target_chat in chat_targets:
        for q in search_queries:
            clean_q = q.strip()
            if len(clean_q) < 3:
                continue

            try:
                # УВЕЛИЧЕННАЯ ГЛУБИНА: 80 постов для выбранных папок, 35 для общего скана
                limit_scan = 80 if target_chat else 35
                async for message in telethon_client.iter_messages(target_chat, search=clean_q, limit=limit_scan):
                    if not message.text or len(message.text) < 30:
                        continue

                    if message.chat_id == bot_id:
                        continue

                    chat = await message.get_chat()
                    if isinstance(chat, User) or getattr(chat, 'is_user', False):
                        continue

                    if ALLOWED_CHAT_IDS and normalize_id(chat.id) not in ALLOWED_CHAT_IDS:
                        continue

                    unique_key = f"{normalize_id(message.chat_id)}_{message.id}"
                    if unique_key in seen_ids:
                        continue
                    seen_ids.add(unique_key)

                    chat_title = getattr(chat, 'title', getattr(chat, 'username', 'Канал'))
                    post_text = clean_html(message.text)
                    overlap = calculate_overlap_score(raw_brief, post_text)
                    fwd_source = await extract_forward_metadata(message)

                    if getattr(chat, 'username', None):
                        msg_url = f"https://t.me/{chat.username}/{message.id}"
                    else:
                        clean_id = str(chat.id).replace("-100", "")
                        msg_url = f"https://t.me/c/{clean_id}/{message.id}"

                    asyncio.create_task(save_vacancy_to_db(chat_title, message.id, message.chat_id, post_text, msg_url, fwd_source))

                    company_match = re.search(r"(?:в компанию|компания|проект|заказчик|в команду):\s*([A-Za-zА-Яа-я0-9_\-\s]{3,30})", post_text, re.IGNORECASE)
                    company = company_match.group(1).strip() if company_match else chat_title

                    found_posts.append({
                        "source": chat_title[:30],
                        "title": f"Пост в {chat_title[:30]}",
                        "company": company,
                        "salary": "в тексте",
                        "url": msg_url,
                        "desc": post_text[:400],
                        "overlap": overlap,
                        "fwd_source": fwd_source,
                        "content_hash": generate_content_hash(post_text),
                        "is_external": False
                    })
            except Exception as e:
                logger.debug(f"Поиск в {target_chat}: {e}")

    found_posts.sort(key=lambda x: x.get("overlap", 0), reverse=True)
    return found_posts


# ----------------- ПАРСИНГ ВНЕШНИХ ИСТОЧНИКОВ -----------------
async def fetch_habr(client: httpx.AsyncClient, query: str, raw_brief: str) -> list:
    clean_q = CLEAN_QUERY_RE.sub(" ", query).strip()
    url = "https://career.habr.com/api/frontend/vacancies"
    params = {"q": clean_q, "per_page": 4}
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
            title = item.get("title", "")
            desc_text = f"{title}. Стек: {skills}"

            if is_agency(company, desc_text):
                continue

            overlap = calculate_overlap_score(raw_brief, desc_text)
            jobs.append({
                "source": "Хабр Карьера",
                "title": title,
                "company": company,
                "salary": sal_str,
                "url": full_url,
                "desc": desc_text[:350],
                "overlap": overlap,
                "fwd_source": "",
                "content_hash": generate_content_hash(desc_text),
                "is_external": True
            })
        return jobs
    except Exception:
        return []


async def fetch_telegram_dorks(client: httpx.AsyncClient, query: str, raw_brief: str) -> list:
    try:
        clean_q = CLEAN_QUERY_RE.sub(" ", query).strip()
        search_query = f'site:t.me/s/ "{clean_q}" "вакансия"'
        url = "https://html.duckduckgo.com/html/"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        data = {"q": search_query, "b": ""}

        r = await client.post(url, data=data, headers=headers, timeout=3.0)
        if r.status_code != 200:
            return []

        raw_results = re.findall(
            r'<a class="result__url" href="[^"]*uddg=([^"&]+)[^"]*">.*?</a>.*?<a class="result__snippet[^>]*>(.*?)</a>',
            r.text, re.DOTALL
        )

        jobs = []
        for enc_url, snippet in raw_results[:3]:
            decoded_url = unquote(enc_url)
            match = re.search(r"t\.me/(?:s/)?([a-zA-Z0-9_]+)/(\d+)", decoded_url)
            if not match:
                continue

            channel_username = match.group(1)
            msg_id = match.group(2)
            post_url = f"https://t.me/{channel_username}/{msg_id}"
            desc = clean_html(snippet)

            if len(desc) < 30 or is_agency(channel_username, desc):
                continue

            overlap = calculate_overlap_score(raw_brief, desc)
            jobs.append({
                "source": f"TG: @{channel_username}",
                "title": f"Пост в @{channel_username}",
                "company": f"@{channel_username}",
                "salary": "в тексте",
                "url": post_url,
                "desc": desc[:350],
                "overlap": overlap,
                "fwd_source": "",
                "content_hash": generate_content_hash(desc),
                "is_external": True
            })

        return jobs
    except Exception:
        return []


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


async def call_groq_async(prompt: str, max_tokens: int = 2500, json_mode: bool = False) -> tuple[str, str]:
    global ACTIVE_GROQ_MODEL
    if not ACTIVE_GROQ_MODEL:
        await find_working_groq_model()

    safe_prompt = prompt if len(prompt) < 8500 else prompt[:8500]
    kwargs = {
        "model": ACTIVE_GROQ_MODEL,
        "messages": [{"role": "user", "content": safe_prompt}],
        "temperature": 0.15,
        "presence_penalty": 0.2,
        "frequency_penalty": 0.2,
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        res = await asyncio.wait_for(groq_client.chat.completions.create(**kwargs), timeout=16.0)
        content = res.choices[0].message.content
        if content and content.strip():
            return content, ""
    except Exception as e:
        return "", str(e)

    return "", "Empty response"


# ----------------- ХЭНДЛЕРЫ AIOGRAM -----------------
@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "💼 **Multi-Source OSINT Lead Hunter**\n\n"
        "Отправьте бриф или описание вакансии — бот выполнит поиск по вашим Telegram-папкам с увеличенной глубиной и точным сопоставлением стека.\n\n"
        "Команды:\n"
        "• /set_folder — выбор папок Telegram для поиска\n"
        "• /debug_tg — статус базы и выбранных папок",
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

    status_msg = await message.answer("⚡️ [1/3] Построение связок ключевого стека...")

    anchor_queries = extract_anchor_phrases(user_text)
    primary_q = anchor_queries[0] if anchor_queries else "разработчик"
    scope_desc = f"{len(ACTIVE_FOLDERS)} папкам" if ACTIVE_FOLDERS else "всем чатам"

    await safe_edit_status(status_msg, f"🔍 [2/3] Глубокое сканирование Telegram ({scope_desc}) и базы...")

    # Сканирование с увеличенной глубиной
    tg_task = search_joined_chats_deep(user_text, BOT_USER_ID)
    db_task = search_vacancies_in_db(expand_search_terms(user_text), user_text)

    tg_results, db_results = await asyncio.gather(tg_task, db_task)
    internal_results = tg_results + db_results

    # Внешние источники пока опрашиваем параллельно (пункт 4 обсудим позже)
    async with httpx.AsyncClient(follow_redirects=True) as http_client:
        web_tasks = [
            fetch_habr(http_client, primary_q, user_text),
            fetch_telegram_dorks(http_client, primary_q, user_text)
        ]
        web_results = await asyncio.gather(*web_tasks, return_exceptions=True)
        valid_web = [item for sub in web_results if isinstance(sub, list) for item in sub]
        all_collected = internal_results + valid_web

    # Семантическая дедупликация
    seen_hashes = set()
    deduped = []
    for v in all_collected:
        h = v.get("content_hash") or generate_content_hash(v.get("desc", ""))
        if h not in seen_hashes and v.get("url"):
            seen_hashes.add(h)
            deduped.append(v)

    if not deduped:
        await safe_edit_status(status_msg, "❌ Совпадений по источникам не найдено.")
        return

    # Сортируем строго по честному overlap_score
    deduped.sort(key=lambda x: x.get("overlap", 0), reverse=True)
    candidates_pool = deduped[:6]

    await safe_edit_status(status_msg, f"🧠 [3/3] OSINT-анализ и деанонимизация заказчика...")

    compact_candidates = [
        {
            "id": idx,
            "company": v['company'],
            "source": v['source'],
            "url": v['url'],
            "overlap_calc": v.get('overlap', 0),
            "fwd_origin": v.get('fwd_source', ''),
            "text": v['desc'][:320]
        }
        for idx, v in enumerate(candidates_pool, 1)
    ]

    prompt_matrix = f"""Ты — технический IT-аудитор и OSINT-аналитик.
Сопоставь найденные посты с входящим текстом.

ВХОДЯЩИЙ ЗАПРОС:
{user_text[:500]}

КАНДИДАТЫ (ОТСОРТИРОВАНЫ ПО СОВПАДЕНИЮ):
{json.dumps(compact_candidates, ensure_ascii=False)}

ПРАВИЛА:
1. match_reasoning: сопоставь стек кандидата с запросом. Если это тот же самый пост или точное совпадение стека — ставь скор 90-100%.
2. score:
   - 85-100%: Полное совпадение ядра стека и роли.
   - 55-84%: Роль совпадает, но есть отличия по инструментам.
   - 0-45%: Другая специальность (Frontend вместо DevOps), курсы, резюме -> ставь меньше 50%!
3. end_client_name:
   - Если есть `fwd_origin` — это 100% улика, пиши строго имя источника.
   - Если on-premise + PaaS = Финтех (Сбер, Т-Банк, ВТБ, Альфа).
   - Если e-com + гибридное облако = X5 Group, Ozon, Wildberries, Яндекс.
   - Если явных маркеров нет — пиши "Прямой IT-бизнес". НЕ выдумывай корпорации на пустом месте!

Формат JSON:
{{
  "results": [
    {{
      "company": "...",
      "score": 95,
      "role": "...",
      "url": "...",
      "source": "...",
      "stack_match": "...",
      "match_reasoning": "...",
      "end_client_name": "...",
      "end_client_evidence": "...",
      "strategy_lpr": "...",
      "strategy_pain": "...",
      "strategy_value": "...",
      "strategy_hook": "..."
    }}
  ]
}}"""

    result_json_str, err = await call_groq_async(prompt_matrix, max_tokens=2500, json_mode=True)
    parsed_candidates = []

    try:
        parsed_data = json.loads(result_json_str)
        parsed_candidates = parsed_data.get("results", [])
    except Exception:
        pass

    if not parsed_candidates:
        for idx, v in enumerate(candidates_pool[:4], 1):
            calc_score = int(v.get("overlap", 40))
            parsed_candidates.append({
                "company": v["company"],
                "score": max(50, min(98, calc_score + 10)),
                "role": "IT Специалист",
                "url": v["url"],
                "source": v["source"],
                "stack_match": "Стек из описания",
                "end_client_name": "Прямой IT-бизнес",
                "end_client_evidence": "Совпадение по стеку и профилю задач.",
                "strategy_lpr": "CTO / Team Lead",
                "strategy_pain": "Потребность в закрытии задач проекта",
                "strategy_value": "Предоставление готовых инженеров",
                "strategy_hook": "Добрый день! Видим потребность в специалисте. Актуально взглянуть на профили наших инженеров?"
            })

    parsed_candidates.sort(key=lambda x: int(x.get("score", 0)), reverse=True)
    qualified_leads = [item for item in parsed_candidates if int(item.get("score", 0)) >= 50][:4]

    if not qualified_leads:
        qualified_leads = parsed_candidates[:1]

    await safe_edit_status(status_msg, f"🏁 Найдено **{len(qualified_leads)}** точных совпадений:")

    for rank, item in enumerate(qualified_leads, 1):
        company = item.get("company", "Не указана")
        score = int(item.get("score", 50))
        role = item.get("role", "IT Специалист")
        url = item.get("url", "#")
        source = item.get("source", "Источник")
        stack = item.get("stack_match", "Технологии в тексте")
        
        c_name = item.get("end_client_name", "Прямой IT-бизнес")
        c_evidence = item.get("end_client_evidence", "Определено по специфике задач.")
        
        lpr = item.get("strategy_lpr", "CTO / Head of Infrastructure")
        pain = item.get("strategy_pain", "Нехватка рук на ключевые задачи")
        value_prop = item.get("strategy_value", "Закрытие операционки готовыми инженерами")
        hook = item.get("strategy_hook", "Добрый день! Обратили внимание на вакансию. Актуально взглянуть на профили?")

        client_block = (
            "> 🕵️ **OSINT-расследование заказчика:**\n"
            f"> **Вероятный бенефициар:** `{c_name}`\n"
            f"> **Улика:** _{c_evidence}_\n\n"
        )

        card = (
            f"**#{rank}. {company} • {score}%**\n"
            f"{role}\n\n"
            f"**Стек:** {stack}\n"
            f"**Где:** {source}\n\n"
            f"{client_block}"
            f"🎯 **Фокус:** {lpr}\n"
            f"🚨 **Боль:** {pain}\n"
            f"💎 **Оффер:** {value_prop}\n\n"
            f"> 💬 **Питч для контакта:**\n"
            f"> {hook}"
        )

        buttons = [
            [
                InlineKeyboardButton(text="↗ Открыть вакансию", url=url),
                InlineKeyboardButton(text="🕵️ Найти ЛПР", url=build_lead_osint_url(company))
            ]
        ]

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
