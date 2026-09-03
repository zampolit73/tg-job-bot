import asyncio
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

KNOWN_AGENCIES = (
    "кадровое", "рекрутинг", "recruitment", "staffing", "hr", "agency",
    "агентство", "selecty", "ancor", "анкор", "кадры", "outstaff", "аутстафф",
    "personnel", "talent", "staff", "headhunting", "подбор персонала",
    "ibs", "icl", "aston", "bell integrator", "neoflex", "epam", "reksoft", "andersen"
)

# ----------------- ВЕСОВАЯ ТАКСОНОМИЯ СТЕКА (WEIGHTED SCORING) -----------------
# Вес 1.0 — Базовый шум (встречается везде)
TIER1_COMMON = {
    "git", "linux", "docker", "rest", "api", "sql", "ci/cd", "ci", "cd",
    "python", "java", "c#", "javascript", "bash"
}

# Вес 2.5 — Профильный рабочий стек
TIER2_SPECIALIZED = {
    "kubernetes", "k8s", "helm", "ansible", "terraform", "postgresql", "postgres",
    "golang", "go", "spring", "fastapi", "django", "typescript", "react", "vue",
    "angular", "node.js", "redis", "rabbitmq", "kafka", "playwright", "selenium"
}

# Вес 5.0 — Архитектурные маркеры и специфика окружения (высочайшая точность)
TIER3_RARE_MARKERS = {
    "onprem", "on-premise", "bare-metal", "ceph", "victoriametrics", "clickhouse",
    "opensearch", "istio", "argocd", "flux", "vault", "nomad", "dwh", "greenplum",
    "altlinux", "astralinux", "redos", "pciss", "pci-dss", "ttm", "cloud native",
    "opentelemetry", "linkerd", "temporal", "camunda"
}

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


def extract_weighted_tokens(text: str) -> dict[str, float]:
    tokens = {}
    text_lower = text.lower()
    for word in TIER1_COMMON:
        if re.search(r"\b" + re.escape(word) + r"\b", text_lower):
            tokens[word] = 1.0
    for word in TIER2_SPECIALIZED:
        if re.search(r"\b" + re.escape(word) + r"\b", text_lower):
            tokens[word] = 2.5
    for word in TIER3_RARE_MARKERS:
        if re.search(r"\b" + re.escape(word) + r"\b", text_lower):
            tokens[word] = 5.0
    return tokens


def calculate_weighted_similarity(source_weights: dict[str, float], candidate_text: str) -> float:
    if not source_weights:
        return 0.0
    candidate_weights = extract_weighted_tokens(candidate_text)
    common_keys = set(source_weights.keys()).intersection(candidate_weights.keys())
    matched_score = sum(source_weights[k] for k in common_keys)
    total_possible = sum(source_weights.values())
    return matched_score / total_possible if total_possible > 0 else 0.0


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


# ----------------- ОТКРЫТЫЕ ИСТОЧНИКИ ВАКАНСИЙ -----------------
async def fetch_hh_rss(client: httpx.AsyncClient, query: str, count: int = 8, phrase_mode: bool = False) -> list:
    q_str = f'"{query}"' if phrase_mode else query
    url = f"https://hh.ru/rss/vacancies?text={quote_plus(q_str)}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = await client.get(url, headers=headers, timeout=3.0)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
        jobs = []
        for item in root.findall("./channel/item")[:count]:
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            desc = clean_html(item.findtext("description", ""))
            
            company_match = re.search(r"\((.*?)\)$", title)
            company = company_match.group(1).strip() if company_match else "Не указана"
            if is_agency(company):
                continue
            
            clean_title = re.sub(r"\s*\(.*?\)$", "", title).strip()
            jobs.append({
                "source": "hh.ru (RSS)",
                "title": clean_title,
                "company": company,
                "salary": "в описании",
                "url": link,
                "desc": desc[:350],
                "is_ngram": phrase_mode
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
                "source": "Хабр Карьера",
                "title": item.get("title", ""),
                "company": company,
                "salary": sal_str,
                "url": full_url,
                "desc": f"Стек: {skills}",
                "is_ngram": False
            })
        return jobs
    except Exception:
        return []


async def fetch_trudvsem(client: httpx.AsyncClient, query: str, count: int = 6) -> list:
    clean_q = CLEAN_QUERY_RE.sub(" ", query).strip()
    url = "http://opendata.trudvsem.ru/api/v1/vacancies"
    params = {"text": clean_q, "limit": count}
    try:
        r = await client.get(url, params=params, timeout=3.0)
        data = r.json()
        vacancies = data.get("results", {}).get("vacancies", [])
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
                "desc": f"{requirement} {duty}"[:350],
                "is_ngram": False
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
                "source": "SuperJob",
                "title": item.get("profession", ""),
                "company": company,
                "salary": sal_str,
                "url": item.get("link", ""),
                "desc": desc[:300],
                "is_ngram": False
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
                    "desc": post_text[:300],
                    "is_ngram": False
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
        "💼 **Multi-Source OSINT Lead Hunter (Pro Scorer)**\n\n"
        "Отправьте бриф вакансии. Бот выполнит каскадный поиск по открытым базам с взвешенным скорингом редких маркеров.",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    user_text = message.text
    status_msg = await message.answer("⚡️ Каскадный анализ текста и извлечение маркеров...")

    prompt_kw = f"""Ты — OSINT-аналитик IT-рынка. Сформируй ровно 4 поисковых параметра через точку с запятой в одну строку:
1. Должность без минус-слов. Пример: DevOps инженер
2. 2-3 ключевые технологии. Пример: Kubernetes Ansible Terraform
3. 1 редкий маркер окружения/инфраструктуры (Tier 3). Пример: OnPrem или Ceph или ClickHouse
4. Уникальная N-грамма: дословная фраза из 3-4 слов из блока задач/требований (без кавычек). Пример: миграция в закрытый контур
Текст:
{user_text[:800]}"""

    kw_res, _ = await call_groq_async(prompt_kw, max_tokens=65)
    queries = [q.strip() for q in kw_res.split(";") if len(q.strip()) > 1]
    
    role_query = queries[0] if len(queries) > 0 else "Разработчик"
    stack_query = queries[1] if len(queries) > 1 else "Kubernetes"
    rare_term = queries[2] if len(queries) > 2 else stack_query.split()[0]
    ngram_phrase = queries[3] if len(queries) > 3 else ""

    await status_msg.edit_text("⚡️ Запуск каскадных поисковых волн...")

    async with httpx.AsyncClient(follow_redirects=True) as http_client:
        tasks = [
            fetch_habr(http_client, stack_query, 8),
            fetch_trudvsem(http_client, role_query, 6),
            fetch_superjob(http_client, stack_query, 5),
            fetch_hh_rss(http_client, f"{role_query} {rare_term}", 6),
            search_telegram_native(rare_term),
        ]

        # Каскадная Волна 1: Точный поиск N-граммы
        if len(ngram_phrase.split()) >= 3:
            tasks.append(fetch_hh_rss(http_client, ngram_phrase, 5, phrase_mode=True))

        try:
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=4.5)
        except Exception:
            results = []

    raw_vacancies = [item for sublist in results for item in sublist]
    unique_vacancies = list({v["url"]: v for v in raw_vacancies if v.get("url")}.values())

    if not unique_vacancies:
        await status_msg.edit_text("❌ Вакансии не найдены. Попробуйте уточнить ключевой стек.")
        return

    # Локальный взвешенный скоринг
    source_weights = extract_weighted_tokens(user_text)
    for vac in unique_vacancies:
        base_score = calculate_weighted_similarity(source_weights, vac["title"] + " " + vac["desc"])
        ngram_bonus = 0.6 if vac.get("is_ngram") else 0.0
        vac["final_rank"] = base_score + ngram_bonus

    unique_vacancies.sort(key=lambda x: x["final_rank"], reverse=True)
    top_candidates = unique_vacancies[:7]

    await status_msg.edit_text(f"🧠 Глубокий 3D-скоринг ТОП-{len(top_candidates)} позиций...")

    compact_list = [
        f"ID {idx}: {v['company']} | {v['title']} | Источник: {v['source']} | URL: {v['url']} | Совпадение N-граммы: {'ДА' if v.get('is_ngram') else 'НЕТ'} | Описание: {v['desc'][:260]}"
        for idx, v in enumerate(top_candidates, 1)
    ]
    vacancies_payload = "\n".join(compact_list)

    prompt_match = f"""Ты — ведущий OSINT-аналитик по деанонимизации IT-заказчиков в аутстаффинге.
Агентство скопировало бриф прямого работодателя. Твоя задача — вычислить ТОП-3 компаний с глубокой 3D-оценкой.

ОРИГИНАЛЬНЫЙ БРИФ:
\"\"\"{user_text[:800]}\"\"\"

КАНДИДАТЫ ДЛЯ АНАЛИЗА:
{vacancies_payload}

ПРАВИЛА ОЦЕНКИ ПО 3 ПРОЕКЦИЯМ:
1. Совпадение стека: оценивай не базовые инструменты (Docker, Git), а профильные (Tier 2/3).
2. Архитектурная задача: совпадает ли цель (миграция, закрытый контур, highload, разработка с нуля)?
3. Инфраструктурные ограничения: тип среды (OnPrem/Cloud/Hybrid, требования регуляторов).
Если совпала N-грамма (цитата задач) — это 90-98% вероятности оригинала.

ОФОРМИ СТРОГО ПО ШАБЛОНУ:
══════════════════════════════
🏢 **КОМПАНИЯ:** [Название компании или канал]
🎯 **Общий скор:** [XX]%
🎲 **Вероятность оригинала:** [🟢 Высокая (85-98%) / 🟡 Средняя (55-80%) / 🟠 Косвенная (30-50%)]
📌 **Позиция:** [Название вакансии]
💰 **Зарплата:** [Вилка или 'Не указана']
🔗 **Ссылка:** [Открыть источник](URL)
🏛 **Источник:** [Хабр / Работа России / hh.ru / SuperJob / Telegram]

📊 **3D-Анализ соответствия:**
• **Стек и инструменты:** [Совпадение профильных технологий]
• **Проектная задача:** [Что совпало по обязанностям/целям проекта]
• **Инфраструктура/Контур:** [Специфика окружения: OnPrem, облака, финтех и т.д.]

💡 **Стратегия выхода для сейлза:**
• **К кому идти:** [Точная роль ЛПР: Head of Infrastructure, CTO, Lead DevOps]
• **Болевая точка:** [Главная боль/задача проекта]
• **Холодный хук:** [1-2 предложения хук для первого контакта в LinkedIn/Telegram]
══════════════════════════════
"""

    result_text, err = await call_groq_async(prompt_match, max_tokens=1600)
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
