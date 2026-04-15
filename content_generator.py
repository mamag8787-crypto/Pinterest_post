import os
import json
import random
import logging
import httpx

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-20241022")
PROFILE_CTA = "Ссылка в шапке профиля."

CONTENT_ANGLES = [
    "автоматизация бизнеса через ИИ",
    "ИИ для контента и продаж",
    "нейросети для экономии времени",
    "ИИ-инструменты для предпринимателей",
    "как ускорить работу с помощью ИИ",
]

SYSTEM_PROMPT = """Ты создаёшь короткий контент для Pinterest на русском языке.
Аудитория: предприниматели, эксперты, руководители.
Верни только валидный JSON без markdown и пояснений."""

async def generate_pin_content() -> tuple[str, str, str]:
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY не задан, использую fallback")
        return _fallback_content()

    angle = random.choice(CONTENT_ANGLES)

    user_prompt = f"""Создай контент для видео-пина в Pinterest на тему: "{angle}"

Верни JSON строго в формате:
{{
  "title": "короткий заголовок до 70 символов",
  "description": "очень короткое описание 70-140 символов",
  "hashtags": "6-8 хештегов через пробел, начиная с #"
}}

Требования:
- Заголовок: конкретный, цепкий, без воды
- Описание: 1-2 коротких предложения
- В конце описания фраза: "{PROFILE_CTA}"
- Не вставляй ссылок
- Не пиши длинно
- Хештеги: микс русских и английских"""

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": 220,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
            )
            response.raise_for_status()
            data = response.json()
            raw_text = data["content"][0]["text"].strip()

            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]

            parsed = json.loads(raw_text)
            title = str(parsed["title"]).strip()
            description = str(parsed["description"]).strip()
            hashtags = str(parsed["hashtags"]).strip()

            logger.info(f"Контент сгенерирован: '{title}'")
            return title, description, hashtags

    except json.JSONDecodeError as e:
        logger.error(f"Ошибка парсинга JSON от Anthropic: {e}")
        return _fallback_content()
    except Exception as e:
        logger.error(f"Ошибка генерации контента: {e}")
        return _fallback_content()


def _fallback_content() -> tuple[str, str, str]:
    fallbacks = [
        (
            "3 ИИ-инструмента для экономии времени",
            "Рутину можно сократить в разы. Ссылка в шапке профиля.",
            "#ИИ #Автоматизация #Бизнес #AI #ChatGPT #Productivity #Aitools"
        ),
        (
            "Как ускорить работу с помощью ИИ",
            "Контент и рутина делаются быстрее. Ссылка в шапке профиля.",
            "#ИскусственныйИнтеллект #АвтоматизацияБизнеса #AI #Business #Efficiency #ChatGPT"
        ),
    ]
    return random.choice(fallbacks)
