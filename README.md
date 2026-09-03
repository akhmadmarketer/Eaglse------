# Eagles Instagram bot

Локальный прототип связывает Instagram Direct с OpenAI через Открытую линию
Bitrix24:

```text
Instagram → Bitrix24 → локальный обработчик → OpenAI → Bitrix24 → Instagram
```

## Подготовка

Установите зависимости:

```bash
python3 -m pip install -r requirements.txt
```

Создайте `.env.local` по примеру `.env.example` и заполните значения. Файл с
секретами исключён из Git.

## Запуск

Запустите обработчик:

```bash
python3 app.py
```

Откройте публичный HTTPS-туннель:

```bash
ngrok http 8000
```

Проверка сервера:

```bash
curl http://127.0.0.1:8000/health
```

## Регистрация бота в Bitrix24

Передайте скрипту публичный адрес обработчика:

```bash
python3 register_bitrix_bot.py https://example.ngrok-free.dev/bitrix/events
```

Сохраните числовой `bot_id` из ответа в `B24_BOT_ID`. Скрипт не выводит URL
вебхука и токен бота.

Получите список Открытых линий и подключите бота к нужной линии:

```bash
python3 bitrix_openline.py list
python3 bitrix_openline.py connect 1
```

## Локальные проверки

### Длинный диалог в терминале

В одном окне терминала запустите сервер:

```bash
python3 app.py
```

Во втором окне запустите интерактивный тест:

```bash
python3 test_chat.py
```

Бот сохраняет последние 20 сообщений отдельно для каждого локального сеанса и
чата Bitrix24. Команда `/reset` начинает разговор заново, `/exit` завершает
тест. Память хранится только в оперативной памяти и очищается после перезапуска
сервера.

Прямой запрос к OpenAI:

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"test-1","message":"Здравствуйте! Какие направления у вас есть?"}'
```

Имитация события Bitrix24 без отправки ответа в портал:

```bash
curl -X POST http://127.0.0.1:8000/bitrix/events \
  -H 'Content-Type: application/json' \
  -H 'X-Local-Test: 1' \
  -d '{
    "event":"ONIMBOTV2MESSAGEADD",
    "data":{
      "bot":{"id":456},
      "message":{"id":1001,"chatId":112,"authorId":77,"text":"Здравствуйте!"},
      "chat":{"id":112,"entityType":"LINES"}
    }
  }'
```

`B24_APPLICATION_TOKEN` необязателен для короткого локального теста. Если он
заполнен, обработчик отклоняет события Bitrix24 с несовпадающим токеном.

## База знаний

Подготовленная база находится в каталоге `knowledge/`: постоянные сведения
отделены от изменяемых цен, тренеров, событий и расписания.

Чтобы заново получить технический снимок публичного сайта:

```bash
python3 scrape_academy_eagles.py --output /tmp/eagles-academy-snapshot.json
```

Перед подключением к ответам бота необходимо закрыть вопросы из
`knowledge/review-needed.md`.

## Текущие ограничения

- используется временный системный промпт;
- история диалога и поиск по подготовленной базе знаний пока не подключены;
- запись, оплата и передача диалога сотруднику ещё не реализованы.
