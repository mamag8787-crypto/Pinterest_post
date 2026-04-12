import os
import json
import random
import logging
import httpx

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_CHANNEL_LINK = os.getenv("TELEGRAM_CHANNEL_LINK", "https://t.me/yourchannel")

# Разнообразные углы подачи для AI-темы — чтобы не повторяться
CONTENT_ANGLES = [
    "как ИИ меняет продажи и бизнес в 2025 году",
    "как использовать ИИ для автоматизации рутины",
    "ИИ-инструменты, которые экономят часы работы",
    "как ИИ помогает предпринимателям зарабатывать больше",
    "будущее уже здесь: ИИ в повседневном бизнесе",
    "как ИИ заменяет целые отделы компаний",
    "нейросети для контента: быстро, дёшево, эффективно",
    "ИИ как конкурентное преимущество для малого бизнеса",
    "автоматизация через ИИ: с чего начать прямо сейчас",
    "как за 10 минут сделать то, на что раньше уходил день"
]

SYSTEM_PROMPT = """Ты — эксперт по контент-маркетингу и ИИ-технологиям. 
Создаёшь цепляющий контент для Pinterest на русском языке.
Аудитория: предприниматели, эксперты, руководители, которые хотят автоматизировать бизнес через ИИ.
Отвечай ТОЛЬКО валидным JSON без markdown и без пояснений."""


async def generate_pin_content() -> tuple[str, str, str]:
    """
    Генерирует контент для Pinterest пина через Claude API.
    Возвращает: (title, description, hashtags)
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY не задан, используем fallback контент")
        return _fallback_content()

    angle = random.choice(CONTENT_ANGLES)

    user_prompt = f"""Создай контент для видео-пина в Pinterest на тему: "{angle}"

Верни JSON строго в таком формате:
{{
  "title": "заголовок пина до 100 символов — конкретный, с цифрой или провокацией",
  "description": "описание 150-300 символов — польза + интрига + призыв перейти в Telegram ({TELEGRAM_CHANNEL_LINK})",
  "hashtags": "10-15 хештегов через пробел на русском и английском, начиная с #"
}}

Требования:
- Заголовок: конкретный, без воды, можно с цифрой (5 инструментов, 3 шага)
- Описание: польза в первом предложении, в конце — призыв перейти в Telegram
- Хештеги: микс русских (#ИскусственныйИнтеллект, #Автоматизация) и английских (#AI, #ChatGPT)"""

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-5",
                    "max_tokens": 600,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_prompt}]
                }
            )
            response.raise_for_status()
            data = response.json()
            raw_text = data["content"][0]["text"].strip()

            # Убираем возможные markdown-обёртки
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]

            parsed = json.loads(raw_text)
            title = parsed["title"]
            description = parsed["description"]
            hashtags = parsed["hashtags"]

            logger.info(f"Контент сгенерирован: '{title}'")
            return title, description, hashtags

    except json.JSONDecodeError as e:
        logger.error(f"Ошибка парсинга JSON от Claude: {e}")
        return _fallback_content()
    except Exception as e:
        logger.error(f"Ошибка генерации контента: {e}")
        return _fallback_content()


def _fallback_content() -> tuple[str, str, str]:
    """Запасной контент если API недоступен"""
    fallbacks = [
        (
            "5 ИИ-инструментов, которые заменят половину твоей команды",
            f"Нейросети уже сейчас делают за предпринимателей продажи, контент и аналитику. "
            f"Смотри видео и подписывайся на Telegram за практическими схемами → {TELEGRAM_CHANNEL_LINK}",
            "#ИскусственныйИнтеллект #Автоматизация #Нейросети #Бизнес #ИИинструменты "
            "#AI #ChatGPT #Automation #Business #ArtificialIntelligence #MachineLearning "
            "#AItools #Productivity #Tech2025 #ЦифровойБизнес"
        ),
        (
            "ИИ автоматизирует бизнес: конкретные схемы 2025",
            f"Как руководители экономят 20+ часов в неделю с помощью нейросетей. "
            f"Детальные инструкции в Telegram → {TELEGRAM_CHANNEL_LINK}",
            "#ИИ #НейросетиДляБизнеса #Автоматизация #ChatGPT #Claude "
            "#AI #BusinessAutomation #AIMarketing #DigitalBusiness #Productivity "
            "#ArtificialIntelligence #AItools #Tech #Инновации #ЦифровизацияБизнеса"
        ),
    ]
    import random
    return random.choice(fallbacks)
