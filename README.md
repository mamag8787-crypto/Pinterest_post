# 🤖 Pinterest Video Bot

Telegram-бот, который принимает видео и публикует их в Pinterest как видео-пины с AI-описанием на тему ИИ.

## Что делает бот

1. Принимает видео в Telegram (до 20 MB)
2. Генерирует через Claude AI: заголовок, описание, хештеги
3. Публикует видео-пин в Pinterest
4. Отправляет отчёт с ссылкой на пин

---

## Быстрый старт (Railway)

### 1. Получи необходимые токены

**Telegram:**
- Создай бота у [@BotFather](https://t.me/BotFather) → получишь `TELEGRAM_BOT_TOKEN`
- Узнай свой ID у [@userinfobot](https://t.me/userinfobot) → `ALLOWED_USER_ID`

**Pinterest:**
- Зайди на [developers.pinterest.com](https://developers.pinterest.com/apps/)
- Создай приложение (тип: Web)
- В разделе "Access Token" сгенерируй токен со скоупами:
  - `boards:read`
  - `pins:read`
  - `pins:write`
  - `media:write`  ← **обязательно для видео**
- Это твой `PINTEREST_ACCESS_TOKEN`
- Найди `PINTEREST_BOARD_ID`: сделай запрос к API или возьми из URL доски

**Найти Board ID через API:**
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  https://api.pinterest.com/v5/boards
```

**Anthropic:**
- Зайди на [console.anthropic.com](https://console.anthropic.com/)
- API Keys → Create Key → `ANTHROPIC_API_KEY`

---

### 2. Деплой на Railway

1. Загрузи код в GitHub репозиторий
2. Зайди на [railway.app](https://railway.app)
3. New Project → Deploy from GitHub repo
4. В настройках проекта добавь переменные окружения (из `.env.example`)
5. Railway автоматически запустит бота

---

### 3. Запуск локально

```bash
# Клонируй/скачай проект
cd pinterest-bot

# Установи зависимости
pip install -r requirements.txt

# Создай .env файл
cp .env.example .env
# Заполни значения в .env

# Запусти
python bot.py
```

---

## Структура проекта

```
pinterest-bot/
├── bot.py                # Основная логика Telegram-бота
├── pinterest_client.py   # Pinterest API (регистрация медиа, загрузка, создание пина)
├── content_generator.py  # Генерация описаний через Claude AI
├── requirements.txt      # Зависимости Python
├── railway.toml          # Конфиг для Railway
└── .env.example          # Шаблон переменных окружения
```

---

## Ограничения

| Параметр | Лимит |
|---|---|
| Размер видео через Telegram Bot API | 20 MB |
| Форматы видео Pinterest | MP4, MOV |
| Длительность видео Pinterest | 4 сек — 15 мин |
| Обработка видео Pinterest | 30–120 сек после загрузки |

---

## Расширение функционала (TODO)

- [ ] Очередь постинга (1 видео в день по расписанию)
- [ ] Выбор доски командой `/board`
- [ ] Превью описания перед публикацией с кнопками ✅/❌
- [ ] Статистика: `/stats` — сколько опубликовано за месяц
- [ ] Поддержка нескольких Pinterest-аккаунтов
- [ ] Смена темы описаний командой `/topic недвижимость`
