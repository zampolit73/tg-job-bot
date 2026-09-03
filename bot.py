import asyncio
import os
import requests
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message

# ----------------- КЛЮЧИ -----------------
TELEGRAM_BOT_TOKEN = "8982024680:AAEwZQsfwx_BpdW5goe1ux3O94MT34Wfi3M"
GEMINI_KEY = os.environ.get(
    "GEMINI_API_KEY",
    "AQ.Ab8RN6Jyloyq3bkfvMyL7OkXUQqsBBDGXnORlOmQmZGbvKEDYQ",
)
# ----------------------------------------

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()


def call_gemini_rest(prompt: str) -> str:
    """Прямой REST-запрос к Gemini API с поддержкой токенов формата AQ."""
    # Передаем ключ и в заголовок Bearer, и в URL для гарантированной авторизации
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}"
    headers = {
        "Authorization": f"Bearer {GEMINI_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=25)
        res_json = r.json()
        if "candidates" in res_json:
            return res_json["candidates"][0]["content"]["parts"][0]["text"]
        elif "error" in res_json:
            # Резервная попытка через light-модель
            url_alt = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_KEY}"
            r_alt = requests.post(
                url_alt, headers=headers, json=payload, timeout=25
            )
            res_alt = r_alt.json()
            if "candidates" in res_alt:
                return res_alt["candidates"][0]["content"]["parts"][0]["text"]
            return f"Ответ API: {res_json['error'].get('message', res_json)}"
        return "Не удалось разобрать ответ нейросети."
    except Exception as e:
        return f"Ошибка соединения с Gemini: {e}"


def fetch_hh(query: str, count: int = 8) -> list:
    url = "https://api.hh.ru/vacancies"
    params = {"text": query, "area": 113, "per_page": count}
    headers = {"User-Agent": "JobHunterBot/1.0"}
    jobs = []
    try:
        r = requests.get(url, params=params, headers=headers, timeout=8).json()
        for item in r.get("items", []):
            sal = item.get("salary")
            sal_str = "не указана"
            if sal:
                f, t, c = (
                    sal.get("from") or "",
                    sal.get("to") or "",
                    sal.get("currency") or "",
                )
                sal_str = f"{f} - {t} {c}".strip()
            jobs.append({
                "source": "hh.ru",
                "title": item.get("name"),
                "company": item.get("employer", {}).get("name", "Не указана"),
                "salary": sal_str,
                "url": item.get("alternate_url"),
                "desc": f"{item.get('snippet', {}).get('requirement', '')} {item.get('snippet', {}).get('responsibility', '')}",
            })
    except Exception:
        pass
    return jobs


def fetch_habr(query: str, count: int = 8) -> list:
    url = "https://career.habr.com/api/frontend/vacancies"
    params = {"q": query, "per_page": count}
    headers = {"User-Agent": "JobHunterBot/1.0"}
    jobs = []
    try:
        r = requests.get(url, params=params, headers=headers, timeout=8).json()
        for item in r.get("list", []):
            sal = item.get("salary", {})
            sal_str = "не указана"
            if sal and sal.get("formatted"):
                sal_str = sal.get("formatted")
            company = item.get("company", {}).get("title", "Не указана")
            href = item.get("href", "")
            full_url = (
                f"https://career.habr.com{href}"
                if href.startswith("/")
                else href
            )
            jobs.append({
                "source": "Хабр Карьера",
                "title": item.get("title"),
                "company": company,
                "salary": sal_str,
                "url": full_url,
                "desc": " ".join(
                    [s.get("title", "") for s in item.get("skills", [])]
                ),
            })
    except Exception:
        pass
    return jobs


@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет!\n\n"
        "Отправь мне текст любой вакансии, а я найду аналогичные предложения на **hh.ru** и **Хабр Карьере**,\n"
        "рассчитаю **процент совпадения** и оценю **вероятность компании**!"
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    user_text = message.text
    status_msg = await message.answer(
        "⏳ Анализирую стек, собираю данные и считаю вероятности..."
    )

    keywords_prompt = f"Выдели 2-3 поисковых слова для вакансии (роль и главный стек, например 'Python FastAPI'). Выведи ТОЛЬКО эти слова:\n{user_text}"
    kw_result = call_gemini_rest(keywords_prompt)
    search_query = (
        kw_result.strip().replace("\n", " ")
        if "Ответ API" not in kw_result
        else "IT"
    )

    raw_vacancies = fetch_hh(search_query) + fetch_habr(search_query)
    if not raw_vacancies:
        await status_msg.edit_text(
            "Не удалось найти открытые вакансии по этому запросу."
        )
        return

    matching_prompt = f"""
    Ты — эксперт по подбору персонала.
    Исходная вакансия:
    {user_text}

    Найденные вакансии:
    {raw_vacancies}

    Задачи:
    1. Рассчитай:
       - 🎯 Совпадение по роли/стеку: от 0% до 100%
       - 🕵️ Вероятность, что это та же компания: от 0% до 100%
    2. Выбери 3-5 наиболее релевантных вакансий (по убыванию совпадения).
    3. Оформи строго по шаблону:
    🔹 [Должность]
    🏢 Компания: [Название]
    🌐 Источник: (hh.ru или Хабр Карьера)
    🎯 Совпадение по роли: [X]%
    🕵️ Вероятность, что это та же компания: [Y]%
    💰 Зарплата: [Вилка или 'не указана']
    🔗 Ссылка: [URL]
    💡 Комментарий: (1 краткое предложение: суть сходства)
    """

    result_text = call_gemini_rest(matching_prompt)

    if len(result_text) > 4000:
        parts = [
            result_text[i : i + 4000] for i in range(0, len(result_text), 4000)
        ]
        await status_msg.edit_text(parts[0])
        for p in parts[1:]:
            await message.answer(p)
    else:
        await status_msg.edit_text(result_text)


async def handle_ping(request):
    return web.Response(text="Bot is running!")


async def main():
    port = int(os.environ.get("PORT", 10000))
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
