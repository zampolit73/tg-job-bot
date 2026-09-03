import asyncio
import requests
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from google import genai

TELEGRAM_BOT_TOKEN = "8982024680:AAEwZQsfwx_BpdW5goe1ux3O94MT34Wfi3M"
GEMINI_API_KEY = "AQ.Ab8RN6KPLSbx1Qjh6ZiAcc5CTcHpUCyJWVcrlmqhhwEcvXVlSg"

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_API_KEY)


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
        "Отправь мне текст вакансии, а я найду предложения на **hh.ru** и **Хабр Карьере**,\n"
        "рассчитаю **процент совпадения** и оценю **вероятность компании**!"
    )


@dp.message(F.text)
async def handle_vacancy(message: Message):
    user_text = message.text
    status_msg = await message.answer(
        "⏳ Анализирую стек, собираю данные и считаю вероятности..."
    )

    keywords_prompt = f"""
    Выдели 2-3 главных поисковых слова для этой вакансии (роль и главный стек, например: 'Python FastAPI' или 'Product Manager').
    В ответе напиши ТОЛЬКО эти слова, без кавычек и лишнего текста:
    {user_text}
    """

    search_query = "IT"
    for model_name in ["gemini-3.5-flash-lite", "gemini-3.6-flash"]:
        try:
            kw_res = ai_client.models.generate_content(
                model=model_name, contents=keywords_prompt
            )
            if kw_res.text:
                search_query = kw_res.text.strip().replace("\n", " ")
                break
        except Exception:
            continue

    raw_vacancies = fetch_hh(search_query) + fetch_habr(search_query)

    if not raw_vacancies:
        await status_msg.edit_text("Не удалось найти вакансии по запросу.")
        return

    matching_prompt = f"""
    Ты — эксперт по анализу рынка труда.
    Исходная вакансия:
    {user_text}

    Найденные открытые вакансии:
    {raw_vacancies}

    Задачи:
    1. Рассчитай:
       - 🎯 Совпадение по роли/стеку: от 0% до 100%
       - 🕵️ Вероятность, что это та же компания: от 0% до 100%
    2. Выбери 3-5 наиболее релевантных вакансий (по убыванию совпадения).
    3. Оформи каждую строго по шаблону:
    🔹 [Должность]
    🏢 Компания: [Название]
    🌐 Источник: (hh.ru или Хабр Карьера)
    🎯 Совпадение по роли: [X]%
    🕵️ Вероятность, что это та же компания: [Y]%
    💰 Зарплата: [Вилка или 'не указана']
    🔗 Ссылка: [URL]
    💡 Комментарий: (1 краткое предложение сути сходства)
    """

    models_to_try = ["gemini-3.5-flash-lite", "gemini-3.6-flash"]
    result_text = None
    last_err = None

    for model in models_to_try:
        try:
            res = ai_client.models.generate_content(
                model=model, contents=matching_prompt
            )
            if res.text:
                result_text = res.text
                break
        except Exception as e:
            last_err = e
            await asyncio.sleep(1.5)

    if result_text:
        if len(result_text) > 4000:
            parts = [
                result_text[i : i + 4000]
                for i in range(0, len(result_text), 4000)
            ]
            await status_msg.edit_text(parts[0])
            for p in parts[1:]:
                await message.answer(p)
        else:
            await status_msg.edit_text(result_text)
    else:
        await status_msg.edit_text(f"Ошибка при поиске: {last_err}")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
