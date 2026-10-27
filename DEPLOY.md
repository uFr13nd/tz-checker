# Развёртывание TZ Checker на сервере

Приложение — FastAPI + SQLite, без внешней БД. Подходит для одного сервера и
небольшой команды. Фоновые проверки выполняются внутри процесса, поэтому
запускайте **один worker uvicorn** (см. «Ограничения»).

## 1. Docker Compose (рекомендуется)

```bash
cp .env.example .env
# откройте .env и задайте:
#   TZ_CHECKER_TOKEN=<длинный случайный токен>
#   TZ_CHECKER_API_KEY=<ключ провайдера>   (или настройте в интерфейсе)
python -c "import secrets; print(secrets.token_urlsafe(32))"   # генерация токена
docker compose up -d --build
docker compose logs -f
```

Приложение слушает `127.0.0.1:8000` (порт опубликован только на localhost).
Снаружи публикуйте через Nginx с TLS: см. `deploy/nginx.conf.example`.

Контейнер не запустится без `TZ_CHECKER_TOKEN` — это защита от случайного
открытого доступа к API.

Откройте `https://tz.example.com/?token=<токен>`. Приложение сохранит токен в
`sessionStorage` этой вкладки и уберёт его из адресной строки. Дальше все
запросы идут с заголовком `X-API-Token`.

## 2. Без Docker (systemd)

```bash
sudo useradd --system --home /opt/tz-checker --shell /usr/sbin/nologin tzchecker
sudo mkdir -p /opt/tz-checker /var/lib/tz-checker
sudo chown -R tzchecker:tzchecker /opt/tz-checker /var/lib/tz-checker
# скопируйте проект в /opt/tz-checker, затем:
cd /opt/tz-checker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo cp deploy/tz-checker.service /etc/systemd/system/
sudo cp .env.example /etc/tz-checker.env
sudo nano /etc/tz-checker.env      # токен, ключ, TZ_CHECKER_DATA_DIR=/var/lib/tz-checker
sudo systemctl daemon-reload
sudo systemctl enable --now tz-checker
```

Проверка: `curl http://127.0.0.1:8000/health` (без токена) и
`curl -H "X-API-Token: $TZ_CHECKER_TOKEN" http://127.0.0.1:8000/api/settings`.

## 3. Nginx и TLS

Используйте `deploy/nginx.conf.example`, затем:

```bash
sudo certbot --nginx -d tz.example.com
```

В Google Cloud Console для Google Sheets добавьте
`https://tz.example.com` в `Authorized JavaScript origins` OAuth Client ID.

## 4. Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `TZ_CHECKER_TOKEN` | — | Токен доступа; обязателен на сервере |
| `TZ_CHECKER_DATA_DIR` | `./data` | Каталог SQLite, настроек и журнала |
| `TZ_CHECKER_BASE_URL` | из settings | Base URL провайдера (приоритет над файлом) |
| `TZ_CHECKER_MODEL` | из settings | Модель |
| `TZ_CHECKER_API_KEY` | — | Ключ провайдера; **не записывается на диск** |
| `TZ_CHECKER_MAX_UPLOAD_MB` | 20 | Лимит на файл |
| `TZ_CHECKER_CONCURRENCY` | 3 | Параллельных запросов к LLM (1–8) |
| `TZ_CHECKER_USE_EMBEDDINGS` | 1 | Семантический поиск |
| `TZ_CHECKER_EMBEDDING_MODEL` | `text-embedding-3-small` | Модель эмбеддингов |
| `TZ_CHECKER_EMBEDDING_BASE_URL` | — | Отдельный Base URL для эмбеддингов (например, локальная Ollama) |
| `TZ_CHECKER_EMBEDDING_API_KEY` | — | Ключ для отдельного эмбеддинг-провайдера |
| `TZ_CHECKER_PRICE_INPUT` | 0 | $ за 1M входных токенов (для оценки) |
| `TZ_CHECKER_PRICE_OUTPUT` | 0 | $ за 1M выходных токенов |
| `TZ_CHECKER_ORIGINS` | — | Доп. разрешённые Origin через запятую |
| `TZ_CHECKER_OCR` | 0 | OCR для сканированных PDF |
| `TZ_CHECKER_LOG_LEVEL` | INFO | Уровень журнала |

Приоритет: переменные окружения → `data/settings.json` → значения по умолчанию.
Ключ из `TZ_CHECKER_API_KEY` никогда не сохраняется в файл.

## 5. Данные и резервное копирование

- `data/tz_checker.sqlite3` — библиотека СТО, эмбеддинги, прогоны, аудит.
- `data/settings.json` — настройки провайдера; ключ зашифрован DPAPI на Windows,
  на Linux хранится в файле с правами `600` (ограничьте доступ к каталогу).
- `data/tz-checker.log` — журнал с ротацией.

Бэкап (SQLite в WAL-режиме):

```bash
sqlite3 data/tz_checker.sqlite3 ".backup '/backup/tz-$(date +%F).sqlite3'"
cp data/settings.json /backup/settings-$(date +%F).json
```

Не копируйте файлы `-wal`/`-shm` отдельно от базы.

## 6. Обновление

```bash
git pull            # или распакуйте новую версию
docker compose up -d --build
```

Схема БД мигрирует автоматически при старте (`schema_meta`). Перед обновлением
сделайте бэкап. Прогоны, прерванные перезапуском, помечаются как `FAILED` —
их нужно запустить повторно.

## 7. Ограничения серверного режима

- **Один worker.** Прогоны выполняются в памяти процесса; при `--workers > 1`
  прогресс и отмена будут работать только на том worker, который принял запрос.
  Для горизонтального масштабирования нужна внешняя очередь (Redis/Celery).
- SQLite рассчитан на небольшую команду. При интенсивной параллельной записи
  возможны блокировки; приложение повторяет операции с `busy_timeout`.
- Файлы обрабатываются в памяти, исходники на диск не сохраняются.
- Сканы без текстового слоя требуют OCR (Tesseract + `requirements-ocr.txt`).

## 8. Чек-лист безопасности

- [ ] `TZ_CHECKER_TOKEN` задан и не опубликован.
- [ ] Порт 8000 доступен только с localhost / через Nginx.
- [ ] TLS включён, HTTP перенаправляется на HTTPS.
- [ ] `data/` и `.env` не входят в резервные копии общего доступа и в git.
- [ ] Ключ провайдера передан через переменную окружения или введён в интерфейсе.
- [ ] OAuth Client ID ограничен вашим доменом в Google Cloud.
- [ ] В документах нет материалов, запрещённых к передаче выбранному провайдеру.

## 9. Диагностика

| Симптом | Что проверить |
|---|---|
| 401 на все `/api/*` | токен в URL/заголовке, `TZ_CHECKER_TOKEN` |
| 403 при сохранении | Origin не совпадает с доменом; добавьте в `TZ_CHECKER_ORIGINS` |
| 503 при запуске анализа | не задан ключ облачного провайдера |
| Прогон «Частично» | часть пакетов не обработана; смотрите `errors` и журнал |
| Нет семантического поиска | провайдер не поддерживает `/embeddings`; включится FTS+стемы |
| Ошибка Google Sheets | origin не добавлен в OAuth Client ID, нет нужных листов |
