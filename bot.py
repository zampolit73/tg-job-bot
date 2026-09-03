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
HH_URL_RE = re.compile(r"hh\.ru/vacancy/(\d+)")
HABR_URL_RE = re.compile(r"career\.habr\.com/vacancies/(\d+)")

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


# ----------------- ПАРСИНГ ПРЯМЫХ ССЫЛОК -----------------
async def resolve_url_input(client: httpx.AsyncClient, text: str) -> tuple[str, str]:
    hh_match = HH_URL_RE.search(text)
    if hh_match:
        vac_id = hh_match.group(1)
        url = f"https://api.hh.ru/vacancies/{vac_id}"
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            r = await client.get(url, headers=headers, timeout=3.0)
            data = r.json()
            title = data.get("name", "")
            desc = clean_html(data.get("description", ""))
            skills = " ".join([s.get("name", "") for s in data.get("key_skills", [])])
            return f"Роль: {title}\nСтек: {skills}\nОписание: {desc}", f"hh.ru/vacancy/{vac_id}"
        except Exception:
            pass

    habr_match = HABR_URL_RE.search(text)
    if habr_match:
        vac_id = habr_match.group(1)
        url = f"https://career.habr.com/api/frontend/vacancies/{vac_id}"
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            r = await client.get(url, headers=headers, timeout=3.0)
            data = r.json()
            title = data.get("title", "")
            desc = clean_html(data.get("description", ""))
            return f"Роль: {title}\nОписание: {desc}", f"career.habr.com/vacancies/{vac_id}"
        except Exception:
            pass

    return text, ""


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


# ----------------- АСИНХРОННЫЕ ПАРСЕРЫ -----------------
async def fetch_hh(client: httpx.AsyncClient, query: str, count: int = 8, phrase_mode: bool = False) -> list:
    url = "https://api.hh.ru/vacancies"
    q_param = f'"{query}"' if phrase_mode else CLEAN_QUERY_RE.sub(" ", query).strip()
    params = {"text": q_param, "area": 113, "per_page": count}
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
                "is_ngram_hit": phrase_mode
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
                "is_ngram_hit": False
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
                "is_ngram_hit": False
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
                    "id": None,
                    "source": f"Telegram (@{channel})",
                    "title": post_text[:40] + "...",
                    "company": company,
                    "salary": "в посте",
                    "url": post_url,
                    "desc": post_text[:250],
                    "is_ngram_hit": False
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
        "💼 **Multi-Source OSINT Lead Hunter (Smart Matcher)**\n\n"
        "Отправьте обезличенный бриф заявки или прямую ссылку на вакансию (hh.ru / Хабр).",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    user_input = message.text
    status_msg = await message.answer("⚡️ Анализирую входящие данные...")

    async with httpx.AsyncClient(follow_redirects=True) as http_client:
        processed_text, origin_url = await resolve_url_input(http_client, user_input)
        if origin_url:
            await status_msg.edit_text(f"🔗 Вакансия получена по ссылке `{origin_url}`. Извлекаю отпечатки задач...")

        prompt_kw = f"""Ты — OSINT-аналитик IT-рынка. Сформируй ровно 4 параметра через точку с запятой в одну строку:
1. Профильная должность (без кавычек). Пример: DevOps инженер
2. Связка из 2-3 ключевых технологий через пробел. Пример: Kubernetes Helm PostgreSQL
3. Самый специфичный термин стека или продукта (1-2 слова). Пример: Kafka или OnPrem
4. Уникальная N-грамма: дословная редкая фраза из 3-5 слов из блока задач/обязанностей (без кавычек). Пример: диагностика нетиповых окружений
Текст:
{processed_text[:800]}"""

        kw_res, _ = await call_groq_async(prompt_kw, max_tokens=65)
        queries = [q.strip() for q in kw_res.split(";") if len(q.strip()) > 1]

        role_query = queries[0] if len(queries) > 0 else "Разработчик"
        stack_query = queries[1] if len(queries) > 1 else "Kubernetes"
        tg_search_term = queries[2] if len(queries) > 2 else stack_query.split()[0]
        ngram_phrase = queries[3] if len(queries) > 3 else ""

        await status_msg.edit_text("⚡️ Сканирую базы (hh.ru, Хабр, SuperJob, Telegram)...")

        tasks = [
            fetch_hh(http_client, role_query, 8),
            fetch_habr(http_client, stack_query, 8),
            fetch_superjob(http_client, stack_query, 5),
            search_telegram_native(tg_search_term),
        ]

        # Дополнительный точечный N-граммный запрос по hh.ru
        if len(ngram_phrase.split()) >= 3:
            tasks.append(fetch_hh(http_client, ngram_phrase, 5, phrase_mode=True))

        try:
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=4.0)
        except Exception:
            results = []

        raw_vacancies = [item for sublist in results for item in sublist]
        unique_vacancies = list({v["url"]: v for v in raw_vacancies if v.get("url")}.values())

        if not unique_vacancies:
            await status_msg.edit_text("❌ Вакансии не найдены. Попробуйте передать текст с более конкретным описанием стека.")
            return

        # Локальный пре-скоринг (Jaccard Similarity)
        source_tech_set = extract_tech_set(processed_text)
        for vac in unique_vacancies:
            vac_tech_set = extract_tech_set(vac["title"] + " " + vac["desc"])
            jaccard = calculate_jaccard_similarity(source_tech_set, vac_tech_set)
            # Бонус 0.5 к рангу за точное N-граммное попадание
            vac["rank_score"] = jaccard + (0.5 if vac.get("is_ngram_hit") else 0.0)

        # Отбираем ТОП-7 самых релевантных позиций
        unique_vacancies.sort(key=lambda x: x["rank_score"], reverse=True)
        top_vacancies = unique_vacancies[:7]

        # Дозагрузка полных описаний hh.ru только для победителей пре-скоринга
        hh_candidates = [v for v in top_vacancies if v.get("id") and v["source"] == "hh.ru"][:3]
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

    await status_msg.edit_text(f"🧠 Скоринг ТОП-{len(top_vacancies)} позиций через Groq LPU...")

    compact_list = [
        f"ID {idx}: {v['company']} | {v['title']} | Источник: {v['source']} | URL: {v['url']} | Совпадение отпечатка: {'ДА' if v.get('is_ngram_hit') else 'НЕТ'} | Детали: {v['desc'][:260]}"
        for idx, v in enumerate(top_vacancies, 1)
    ]
    vacancies_payload = "\n".join(compact_list)

    prompt_match = f"""Ты — ведущий OSINT-аналитик по деанонимизации IT-заказчиков в аутстаффинге.
Агентство скопировало бриф прямого заказчика. Твоя цель — вычислить ТОП-3 работодателей, у которых взят этот проект.

ОРИГИНАЛЬНЫЙ БРИФ:
\"\"\"{processed_text[:700]}\"\"\"

ОТОБРАННЫЕ ВАКАНСИИ И ПУБЛИКАЦИИ:
{vacancies_payload}

ПРАВИЛА ОЦЕНКИ:
1. Если совпал 'отпечаток: ДА' — это 90-98% вероятности оригинала.
2. Базовые технологии (Linux, Git, Docker, SQL) дают не выше 40%.
3. Выведи строго ТОП-3.

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
    if telethon_client:
        try:
            await telethon_client.start()
        except Exception:
            pass
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
