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

# ----------------- КОНФИГУРАЦИЯ -----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8982024680:AAGSIE8AbyboYoG1HcxLmI9-7ljX2JbTk7s")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "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")
SUPERJOB_KEY = "v3.r.137453308.2b27077a942fb8adcdba08488e08d669db756f70.9b015112521c7e90ef8c34fbc87e5b222fb2ea67"

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
groq_client = AsyncGroq(api_key=GROQ_API_KEY.strip())

HTML_TAG_RE = re.compile(r"<[^>]+>")
WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9\-]+")
CLEAN_NAME_RE = re.compile(r'[\'\"«»@]')
CLEAN_HABR_RE = re.compile(r'[^\w\s\+\#\.\-]')

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

RUSSIAN_STOPWORDS = frozenset({
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то", "все", "она", "так",
    "его", "но", "да", "ты", "к", "у", "же", "вы", "за", "бы", "по", "только", "ее", "мне", "было",
    "вот", "от", "меня", "еще", "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг",
    "ли", "если", "уже", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь", "опять", "уж",
    "вам", "ведь", "там", "потом", "себя", "ничего", "ей", "может", "они", "тут", "где", "есть",
    "надо", "ней", "для", "мы", "тебя", "их", "чем", "была", "сам", "чтоб", "без", "будет", "будто",
    "про", "при", "опыт", "работа", "работы", "знание", "понимание", "обязанности", "требования"
})

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


# ----------------- УТИЛИТЫ -----------------
def clean_html(raw_html: str) -> str:
    return " ".join(HTML_TAG_RE.sub(" ", raw_html).split())


def extract_best_shingle(text: str, n_words: int = 4) -> str:
    cleaned_phrases = []
    for line in text.splitlines():
        words = [w for w in WORD_RE.findall(line.lower()) if w not in RUSSIAN_STOPWORDS and len(w) > 2]
        if len(words) >= n_words:
            cleaned_phrases.append(" ".join(words[:n_words]))
    if cleaned_phrases:
        cleaned_phrases.sort(key=lambda p: sum(len(w) for w in p.split()), reverse=True)
        return f'"{cleaned_phrases[0]}"'
    return ""


def fallback_extract_keywords(text: str) -> list:
    text_lower = text.lower()
    found = [tech for tech in TECH_KEYWORDS if re.search(r"\b" + re.escape(tech) + r"\b", text_lower)]
    if found:
        return [f"NAME:({found[0]})", " ".join(found[:3]), found[0]]
    first_phrase = text.split("\n")[0][:30].strip()
    return [first_phrase if first_phrase else "IT Вакансия", "Разработчик", "Backend"]


def is_agency(company_name: str) -> bool:
    lower_name = company_name.lower()
    return any(w in lower_name for w in KNOWN_AGENCIES)


def build_lead_osint_url(company_name: str) -> str:
    clean_company = CLEAN_NAME_RE.sub("", company_name).strip()
    target_role = 'CTO OR "Team Lead" OR "Head of Engineering" OR "Head of Infrastructure"'
    query = f'site:linkedin.com/in "{clean_company}" ({target_role})'
    return f"https://www.google.com/search?q={quote_plus(query)}"


# ----------------- LLM CLIENT (СТРОГИЙ ТАЙМАУТ) -----------------
async def call_groq_async(prompt: str, max_tokens: int = 1500) -> tuple[str, str]:
    models = ("llama-3.1-8b-instant", "openai/gpt-oss-20b")
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


# ----------------- АСИНХРОННЫЕ ПАРСЕРЫ С ЖЕСТКИМ ТАЙМАУТОМ -----------------
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
                "employer_id": employer.get("id"),
                "source": "hh.ru",
                "title": item.get("name"),
                "company": company,
                "salary": sal_str,
                "url": item.get("alternate_url"),
                "desc": clean_html(desc),
                "footprint": ""
            })
        return jobs
    except Exception:
        return []


async def fetch_hh_full_details(client: httpx.AsyncClient, vacancy_id: str) -> str:
    url = f"https://api.hh.ru/vacancies/{vacancy_id}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = await client.get(url, headers=headers, timeout=2.0)
        data = r.json()
        raw_desc = data.get("description", "")
        key_skills = " ".join([s.get("name", "") for s in data.get("key_skills", [])])
        return clean_html(f"{raw_desc} {key_skills}")[:350]
    except Exception:
        return ""


async def verify_hh_company_footprint(client: httpx.AsyncClient, employer_id: str, context_keyword: str) -> str:
    if not employer_id or not context_keyword:
        return ""
    url = "https://api.hh.ru/vacancies"
    params = {"employer_id": employer_id, "text": context_keyword, "per_page": 2}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = await client.get(url, params=params, headers=headers, timeout=2.0)
        items = r.json().get("items", [])
        if items:
            other_titles = ", ".join([it.get("name", "") for it in items[:2]])
            return f" (Смежный наём: {other_titles})"
    except Exception:
        pass
    return ""


async def fetch_habr(client: httpx.AsyncClient, query: str, count: int = 8) -> list:
    clean_q = CLEAN_HABR_RE.sub(" ", query).strip()
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
                "id": None, "employer_id": None, "source": "Хабр Карьера",
                "title": item.get("title"), "company": company, "salary": sal_str,
                "url": full_url, "desc": f"Стек: {skills}", "footprint": ""
            })
        return jobs
    except Exception:
        return []


async def fetch_superjob(client: httpx.AsyncClient, query: str, count: int = 5) -> list:
    clean_q = CLEAN_HABR_RE.sub(" ", query).strip()
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
                "id": None, "employer_id": None, "source": "SuperJob",
                "title": item.get("profession", ""), "company": company, "salary": sal_str,
                "url": item.get("link", ""), "desc": desc[:250], "footprint": ""
            })
        return jobs
    except Exception:
        return []


# ----------------- БЕЗОПАСНЫЙ ВЕБ-ПОИСК БЕЗ ЗАВИСАНИЙ -----------------
def safe_sync_ddg(query: str, count: int, mode: str) -> list:
    """Обертка с принудительным тайм-аутом, исключающая бесконечные зависания на Render."""
    from duckduckgo_search import DDGS
    jobs = []
    try:
        # Лимит времени на сетевой сокет внутри библиотеки - 2 секунды
        with DDGS(timeout=2.0) as ddgs:
            results = list(ddgs.text(query, max_results=count))
            for res in results:
                url, title, body = res.get("href", ""), res.get("title", ""), res.get("body", "")
                if mode == "tg" and "t.me/" in url:
                    m = re.search(r"t\.me/([^/]+)", url)
                    c_name = f"@{m.group(1)}" if m else "Telegram"
                    jobs.append({
                        "id": None, "employer_id": None, "source": f"Telegram ({c_name})", "title": title[:50],
                        "company": c_name, "salary": "в посте", "url": url, "desc": body[:250], "footprint": ""
                    })
                elif mode == "ats":
                    domain = "Huntflow" if "huntflow" in url else "Potok" if "potok" in url else "Talantix"
                    cand = title.split("—")[0].split("-")[0].split("|")[0].strip()
                    jobs.append({
                        "id": None, "employer_id": None, "source": f"ATS ({domain})", "title": title[:50],
                        "company": cand, "salary": "в вакансии", "url": url, "desc": body[:250], "footprint": ""
                    })
                elif mode == "archive" and "hh.ru/vacancy" in url:
                    cand = title.split("—")[0].split("-")[0].split(":")[0].strip()
                    jobs.append({
                        "id": None, "employer_id": None, "source": "Кэш hh.ru", "title": title[:50],
                        "company": cand, "salary": "в архиве", "url": url, "desc": f"[Архив]: {body[:250]}", "footprint": ""
                    })
    except Exception:
        pass
    return jobs


async def fast_web_search(query: str, count: int, mode: str) -> list:
    try:
        # Если DDG не укладывается в 2.5 секунды — задача сбрасывается и поиск продолжается
        return await asyncio.wait_for(asyncio.to_thread(safe_sync_ddg, query, count, mode), timeout=2.5)
    except Exception:
        return []


# ----------------- ОБРАБОТЧИКИ СООБЩЕНИЙ -----------------
@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "💼 **Multi-Source OSINT Lead Hunter (Turbo)**\n\n"
        "Отправьте текст заявки. Поиск проходит по hh.ru, Хабр, SuperJob, Telegram и ATS без задержек.",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    user_text = message.text
    status_msg = await message.answer("⚡️ Сканирую базы (hh.ru, Хабр, SuperJob, Telegram, ATS)...")

    exact_shingle = extract_best_shingle(user_text)

    prompt_kw = f"""Выдели роль и стек без общих слов (Senior, удаленно).
Сформируй 3 запроса через точку с запятой:
1. Запрос роли для hh.ru: NAME:(...). Пример: NAME:("DevOps").
2. Редкая связка 2-3 инструментов стека. Пример: 'Minio OnPremise Kafka'.
3. Продуктовая специфика/задачи. Пример: 'траблшутинг окружений'.
Выведи ТОЛЬКО 3 запроса через точку с запятой в одну строку:
{user_text[:500]}"""

    kw_res, _ = await call_groq_async(prompt_kw, max_tokens=45)
    queries = [q.strip() for q in kw_res.split(";") if len(q.strip()) > 1]
    if not queries or len(queries) < 2:
        queries = fallback_extract_keywords(user_text)

    tg_query = exact_shingle if exact_shingle else queries[1]
    ats_query = queries[2] if len(queries) > 2 else queries[1]

    # Сбор данных с жестким ограничением общего времени ожидания в 4 секунды
    try:
        async with httpx.AsyncClient(http2=True) as http_client:
            tasks = [
                fetch_hh(http_client, queries[0], 6),
                fetch_habr(http_client, queries[1], 6),
                fetch_superjob(http_client, queries[1], 4),
                fast_web_search(f"site:t.me {tg_query} вакансия", 3, "tg"),
                fast_web_search(f"site:hh.ru/vacancy {exact_shingle if exact_shingle else queries[1]}", 3, "archive"),
                fast_web_search(f'(site:huntflow.io OR site:potok.io) "{ats_query}"', 3, "ats"),
            ]
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=4.0)
    except asyncio.TimeoutError:
        results = []

    raw_vacancies = [item for sublist in results for item in sublist]
    unique_vacancies = list({v["url"]: v for v in raw_vacancies if v.get("url")}.values())

    if not unique_vacancies:
        await status_msg.edit_text("❌ Не удалось найти совпадений по открытым базам. Попробуйте уточнить описание стека.")
        return

    # Быстрое фоновое обогащение для топ-3 позиций hh.ru (не дольше 2 секунд)
    hh_candidates = [v for v in unique_vacancies if v.get("id") and v["source"] == "hh.ru"][:3]
    if hh_candidates:
        try:
            async with httpx.AsyncClient(http2=True) as http_client:
                enrich_tasks = [fetch_hh_full_details(http_client, v["id"]) for v in hh_candidates]
                fp_tasks = [
                    verify_hh_company_footprint(http_client, v["employer_id"], queries[1].split()[0])
                    for v in hh_candidates
                ]
                full_texts, footprints = await asyncio.wait_for(
                    asyncio.gather(asyncio.gather(*enrich_tasks), asyncio.gather(*fp_tasks)),
                    timeout=2.0
                )
                for cand, full_desc, fp in zip(hh_candidates, full_texts, footprints):
                    if full_desc:
                        cand["desc"] = full_desc
                    if fp:
                        cand["footprint"] = fp
        except Exception:
            pass

    await status_msg.edit_text(f"🧠 Скоринг {len(unique_vacancies)} позиций через Groq LPU...")

    compact_list = [
        f"ID {idx}: {v['company']} | {v['title']} | Источник: {v['source']} | URL: {v['url']} | Детали: {v['desc'][:220]}{v.get('footprint', '')}"
        for idx, v in enumerate(unique_vacancies[:10], 1)
    ]
    vacancies_payload = "\n".join(compact_list)

    prompt_match = f"""Ты — строгий OSINT-следователь по деанонимизации IT-заказчиков в аутстаффинге.
Вычисли ТОП-3 прямых работодателей, у которых агентство скопировало эту заявку.

ОРИГИНАЛЬНЫЙ БРИФ:
\"\"\"{user_text[:700]}\"\"\"

НАЙДЕННЫЕ ВАКАНСИИ И ПУБЛИКАЦИИ:
{vacancies_payload}

ПРАВИЛА:
1. За общий базовый стек (Linux, Docker, SQL) — вероятность НЕ ВЫШЕ 35%.
2. Ставь 🟢 Высокую вероятность (80-95%) ТОЛЬКО за совпадение редких связок, терминов или формулировок задач.
3. Отметка 'Смежный наём' — сильное подтверждение прямого заказчика.

ОФОРМИ СТРОГО ПО ШАБЛОНУ:
══════════════════════════════
🏢 **КОМПАНИЯ:** [Название компании или канал]
🎯 **Соответствие стека:** [XX]%
🎲 **Вероятность статуса заказчика:** [🟢 Высокая (80-95%) / 🟡 Средняя (50-75%) / 🟠 Косвенная (30-45%)]
📌 **Позиция:** [Название должности]
💰 **Зарплата:** [Вилка или 'Не указана']
🔗 **Вакансия/Пост:** [Открыть источник](URL)
🏛 **Источник:** [hh.ru / Хабр / SuperJob / Telegram / ATS / Кэш hh.ru]

🔍 **Факторы совпадения:**
• [Что конкретно совпало: редкие технологии, продуктовые задачи или дословные фразы]

💡 **Стратегия выхода для сейлза:**
• **К кому идти:** [Точная роль ЛПР: Head of Infrastructure, CTO, Lead DevOps]
• **Болевая точка:** [Какая острая проектная боль видна в тексте]
• **Первый контакт:** [Готовый хук для сообщения в 1-2 предложения]
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
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
