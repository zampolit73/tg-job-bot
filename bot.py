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
from duckduckgo_search import DDGS

# ----------------- КОНФИГУРАЦИЯ -----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8982024680:AAGSIE8AbyboYoG1HcxLmI9-7ljX2JbTk7s")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "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")
SUPERJOB_KEY = "v3.r.137453308.2b27077a942fb8adcdba08488e08d669db756f70.9b015112521c7e90ef8c34fbc87e5b222fb2ea67"

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
groq_client = AsyncGroq(api_key=GROQ_API_KEY.strip())

# Предкомпилированные регулярные выражения
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
    target_role = 'CTO OR "Team Lead" OR "Head of Engineering" OR "Head of Infrastructure" OR "Engineering Manager"'
    query = f'site:linkedin.com/in "{clean_company}" ({target_role})'
    return f"https://www.google.com/search?q={quote_plus(query)}"


# ----------------- АСИНХРОННЫЙ LLM CLIENT -----------------
async def call_groq_async(prompt: str, max_tokens: int = 1600) -> tuple[str, str]:
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
                timeout=11.0
            )
            content = res.choices[0].message.content
            if content and content.strip():
                return content, ""
        except Exception as e:
            last_err = str(e)
            continue
    return "", last_err


# ----------------- АСИНХРОННЫЕ ПАРСЕРЫ -----------------
async def fetch_hh(client: httpx.AsyncClient, query: str, count: int = 8) -> list:
    url = "https://api.hh.ru/vacancies"
    params = {"text": query, "area": 113, "per_page": count}
    headers = {"User-Agent": "AsyncOSINT/13.0"}
    try:
        r = await client.get(url, params=params, headers=headers, timeout=3.0)
        data = r.json()
        jobs = []
        for item in data.get("items", []):
            company = item.get("employer", {}).get("name", "Не указана")
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
            })
        return jobs
    except Exception:
        return []


async def fetch_hh_full_details(client: httpx.AsyncClient, vacancy_id: str) -> str:
    url = f"https://api.hh.ru/vacancies/{vacancy_id}"
    headers = {"User-Agent": "AsyncOSINT/13.0"}
    try:
        r = await client.get(url, headers=headers, timeout=2.5)
        data = r.json()
        raw_desc = data.get("description", "")
        key_skills = " ".join([s.get("name", "") for s in data.get("key_skills", [])])
        return clean_html(f"{raw_desc} {key_skills}")[:350]
    except Exception:
        return ""


async def fetch_habr(client: httpx.AsyncClient, query: str, count: int = 8) -> list:
    clean_q = CLEAN_HABR_RE.sub(" ", query).strip()
    url = "https://career.habr.com/api/frontend/vacancies"
    params = {"q": clean_q, "per_page": count}
    headers = {"User-Agent": "AsyncOSINT/13.0"}
    try:
        r = await client.get(url, params=params, headers=headers, timeout=3.0)
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
            })
        return jobs
    except Exception:
        return []


async def fetch_superjob(client: httpx.AsyncClient, query: str, count: int = 5) -> list:
    clean_q = CLEAN_HABR_RE.sub(" ", query).strip()
    url = "https://api.superjob.ru/2.0/vacancies/"
    params = {"keyword": clean_q, "count": count}
    headers = {"User-Agent": "AsyncOSINT/13.0", "X-Api-App-Id": SUPERJOB_KEY}
    try:
        r = await client.get(url, params=params, headers=headers, timeout=3.0)
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
            })
        return jobs
    except Exception:
        return []


# ----------------- ВЕБ-ПОИСК В ПОТОКАХ -----------------
def _sync_ddgs_search(search_query: str, count: int, is_tg: bool = False) -> list:
    jobs = []
    try:
        with DDGS(timeout=3.5) as ddgs:
            results = list(ddgs.text(search_query, max_results=count))
            for res in results:
                url, title, body = res.get("href", ""), res.get("title", ""), res.get("body", "")
                if is_tg and "t.me/" in url:
                    m = re.search(r"t\.me/([^/]+)", url)
                    c_name = f"@{m.group(1)}" if m else "Telegram"
                    jobs.append({
                        "id": None, "source": f"Telegram ({c_name})", "title": title[:50],
                        "company": c_name, "salary": "в посте", "url": url, "desc": body[:250]
                    })
                elif not is_tg:
                    domain = "Finder.vc" if "finder.vc" in url else "GeekLink" if "geeklink.io" in url else "VC.ru"
                    cand = title.split("—")[0].split("-")[0].split(":")[0].strip()
                    if not is_agency(cand):
                        jobs.append({
                            "id": None, "source": domain, "title": title[:50],
                            "company": cand, "salary": "в источнике", "url": url, "desc": body[:250]
                        })
    except Exception:
        pass
    return jobs


async def fetch_telegram_posts(query: str, count: int = 3) -> list:
    return await asyncio.to_thread(_sync_ddgs_search, f"site:t.me {query} вакансия", count, True)


async def fetch_niche_portals(query: str, count: int = 3) -> list:
    return await asyncio.to_thread(
        _sync_ddgs_search, f'(site:vc.ru/tribuna OR site:finder.vc OR site:geeklink.io) "{query}" вакансия', count, False
    )


# ----------------- ОБРАБОТЧИКИ TELEGRAM -----------------
@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "💼 **Multi-Source OSINT Lead Hunter**\n\n"
        "Отправьте обезличенный текст заявки от агентства/посредника.\n"
        "Бот проанализирует контекст задач, просканирует рынок и найдет конечного заказчика.",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    user_text = message.text
    status_msg = await message.answer("⚡️ Сканирую базы (hh.ru, Хабр, SuperJob, Telegram, VC, Finder)...")

    exact_shingle = extract_best_shingle(user_text)

    # УЛУЧШЕННЫЙ ПРОМПТ №1: Анти-банальность и редкие связки
    prompt_kw = f"""Ты — OSINT-аналитик. Твоя задача — извлечь из заявки маркеры, которые выведут на ОРИГИНАЛЬНОГО работодателя, а не на посредников.

ПРАВИЛА:
1. ИСКЛЮЧИ грейды и общие слова: "Senior", "Middle", "Junior", "опыт", "разработка", "команда", "удаленно".
2. Запрос 1 (Роль): Главная специализация для hh.ru строго через оператор NAME:(...). Пример: NAME:("DevOps") или NAME:("Системный аналитик").
3. Запрос 2 (Уникальная связка стека): 2-3 термина, которые редко встречаются вместе, но есть в тексте (например: 'Minio OnPremise' или 'Kafka Opensearch Kubernetes').
4. Запрос 3 (Специфика проекта/домен): Редкое словосочетание задач или продукта (например: 'траблшутинг инфраструктуры' или 'платформенные сервисы').

Выведи ТОЛЬКО 3 запроса через точку с запятой в одну строку:
{user_text[:600]}"""

    kw_res, _ = await call_groq_async(prompt_kw, max_tokens=55)
    queries = [q.strip() for q in kw_res.split(";") if len(q.strip()) > 1]
    if not queries or len(queries) < 2:
        queries = fallback_extract_keywords(user_text)

    tg_query = exact_shingle if exact_shingle else queries[1]
    niche_query = queries[2] if len(queries) > 2 else queries[1]

    # Полностью асинхронный параллельный сбор через HTTPX
    async with httpx.AsyncClient(http2=True) as http_client:
        tasks = [
            fetch_hh(http_client, queries[0], 6),
            fetch_habr(http_client, queries[1], 6),
            fetch_superjob(http_client, queries[1], 4),
            fetch_telegram_posts(tg_query, 3),
            fetch_niche_portals(niche_query, 3),
        ]
        results = await asyncio.gather(*tasks)

        raw_vacancies = [item for sublist in results for item in sublist]
        unique_vacancies = list({v["url"]: v for v in raw_vacancies if v.get("url")}.values())

        if not unique_vacancies:
            await status_msg.edit_text("❌ Вакансии работодателей не найдены. Попробуйте передать текст с более конкретным описанием стека.")
            return

        # Дозагрузка полных описаний hh.ru в том же пуле
        hh_candidates = [v for v in unique_vacancies if v.get("id") and v["source"] == "hh.ru"][:3]
        if hh_candidates:
            full_texts = await asyncio.gather(*[fetch_hh_full_details(http_client, v["id"]) for v in hh_candidates])
            for cand, full_desc in zip(hh_candidates, full_texts):
                if full_desc:
                    cand["desc"] = full_desc

    await status_msg.edit_text(f"🧠 Скоринг {len(unique_vacancies)} позиций через Groq LPU...")

    compact_list = [
        f"ID {idx}: {v['company']} | {v['title']} | Источник: {v['source']} | URL: {v['url']} | Детали: {v['desc'][:250]}"
        for idx, v in enumerate(unique_vacancies[:12], 1)
    ]
    vacancies_payload = "\n".join(compact_list)

    # УЛУЧШЕННЫЙ ПРОМПТ №2: Zero-Tolerance к несовпадениям и поиск почерка задач
    prompt_match = f"""Ты — строгий OSINT-следователь по деанонимизации IT-заказчиков в аутстаффинге.
Агентство скопировало бриф прямого клиента и стёрло название компании. Твоя цель — вскрыть первоисточник.

ОРИГИНАЛЬНЫЙ БРИФ:
\"\"\"{user_text[:800]}\"\"\"

НАЙДЕННЫЕ ВАКАНСИИ И ПУБЛИКАЦИИ:
{vacancies_payload}

ЖЁСТКИЕ ПРАВИЛА ВЕРИФИКАЦИИ:
1. ШТРАФ ЗА БАНАЛЬНОСТЬ: Совпадение только по базовым вещам (Linux, Git, Docker, SQL, REST) НЕ ДАЁТ права ставить статус прямого заказчика. За совпадение только общего стека — вероятность не выше 35%.
2. МАРКЕР ПЕРВОИСТОЧНИКА: Ставь 🟢 Высокую вероятность (80-95%) ТОЛЬКО если совпадают:
   - Специфические задачи (например, OnPrem + траблшутинг нетиповых окружений);
   - Редкие связки компонентов (например, Minio + Opensearch + Yandex Cloud);
   - Смысловые формулировки обязанностей (следы копирования оригинального текста).
3. ОТСЕВ: Если в запросе DevOps, а вакансия системного администратора офиса — игнорируй такого кандидата.

ОФОРМИ СТРОГО ПО ШАБЛОНУ:
══════════════════════════════
🏢 **КОМПАНИЯ:** [Название компании или канал]
🎯 **Соответствие стека:** [XX]%
🎲 **Вероятность статуса заказчика:** [🟢 Высокая (80-95%) / 🟡 Средняя (50-75%) / 🟠 Косвенная (30-45%)]
📌 **Позиция:** [Название должности]
💰 **Зарплата:** [Вилка или 'Не указана']
🔗 **Вакансия/Пост:** [Открыть источник](URL)
🏛 **Источник:** [hh.ru / Хабр / SuperJob / Telegram / Finder.vc / VC.ru]

🔍 **Факторы совпадения:**
• [Что конкретно совпало: редкие технологии, продуктовые задачи или дословные фразы]
• [Почему это именно прямой заказчик, а не просто похожая вакансия]

💡 **Стратегия выхода для сейлза:**
• **К кому идти:** [Точная роль ЛПР: Head of Infrastructure, CTO, Lead DevOps]
• **Болевая точка:** [Какая острая проектная боль видна в тексте]
• **Первый контакт:** [Готовый 1-2 предложения хук для сообщения в Telegram/LinkedIn]
══════════════════════════════
"""

    result_text, err = await call_groq_async(prompt_match, max_tokens=1600)
    if not result_text:
        await status_msg.edit_text(f"⚠️ Ошибка генерации: {err}")
        return

    # Добавление прямых ссылок на ЛПР
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
