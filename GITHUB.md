# Публикация на GitHub

## Сначала о безопасности

В проекте есть файлы, которые **нельзя** публиковать:

| Что | Почему |
|---|---|
| `data/settings.json` | API-ключ провайдера (даже зашифрованный DPAPI) |
| `data/tz_checker.sqlite3` | история проверок и тексты корпоративных СТО |
| `data/tz-checker.log` | пути, имена документов, рабочие прогоны |
| `РЕЕСТР СТО/` | исходные корпоративные PDF — интеллектуальная собственность |
| `.venv/` | локальное окружение, сотни мегабайт |

Всё перечисленное уже добавлено в `.gitignore`. Для демонстрации используйте
безопасные примеры из `samples/` — они синтетические.

**Рекомендую создать приватный репозиторий** (`Private`), пока не уверены, что
в коде и истории нет чувствительных данных. Публичный репозиторий с историей
коммитов скрыть задним числом нельзя — ключи и документы останутся в истории.

## 1. Установить Git

На этом компьютере Git не найден. Установите один из вариантов:

- **Git for Windows**: https://git-scm.com/download/win (при установке оставьте
  компонент «Git Bash» и «Git Credential Manager»);
- **GitHub Desktop**: https://desktop.github.com — проще, если не хотите
  работать с командной строкой;
- через `winget`: `winget install --id Git.Git -e --source winget`.

После установки закройте и снова откройте терминал, проверьте:

```powershell
git --version
```

## 2. Создать репозиторий на GitHub

1. GitHub → **New repository**.
2. Имя, например `tz-checker`.
3. **Private** (рекомендуется) или Public.
4. **Не добавляйте** README, `.gitignore` и лицензию — они уже есть в проекте.
5. Создайте репозиторий и скопируйте URL вида
   `https://github.com/ВАШ_ЛОГИН/tz-checker.git`.

## 3. Репозиторий уже подготовлен

Локальный git-репозиторий уже создан, ветка `main`, первый коммит:

```text
ed622c3 TZ Checker 0.3.0: semantic search, background checks, Docker deploy
```

В индекс попали только 47 файлов: код, тесты, примеры и документация.
`data/`, `.venv/`, `РЕЕСТР СТО/`, PDF, логи и ключи не добавлены — это
проверено через `git status` и `git ls-files`.

### Вариант A — GitHub Desktop (проще всего)

На компьютере уже установлен GitHub Desktop (в нём же есть Git):

1. Откройте **GitHub Desktop**.
2. **File → Add Local Repository**.
3. Укажите папку `C:\Users\kuprikovva\Desktop\РАЗРАБОТКА\ТЗ проверка ИИ`.
4. Нажмите **Publish repository**.
5. Имя: `tz-checker`, поставьте галочку **Keep this code private**.
6. **Publish repository**.

GitHub Desktop создаст репозиторий и запушит коммит. Дальше откройте
**Actions** на GitHub — тесты запустятся автоматически.

### Вариант B — командная строка

Если Git доступен в PATH (или используйте Git из GitHub Desktop):

```powershell
cd "C:\Users\kuprikovva\Desktop\РАЗРАБОТКА\ТЗ проверка ИИ"

git remote add origin https://github.com/ВАШ_ЛОГИН/tz-checker.git
git push -u origin main
```

При первом push Git попросит авторизацию через GitHub Credential Manager
или Personal Access Token (classic, scope `repo`). **Не отправляйте токен
в чат и не коммитьте его в файлы.**

Проверить состояние перед push:

```powershell
git status
git log --oneline -1
git ls-files | Select-String -Pattern "\.venv/|^data/|РЕЕСТР|\.pdf$|settings\.json"
```

Последняя команда не должна находить ничего, кроме `data/README.txt`.

## 4. Что произойдёт после push

В репозитории уже настроен GitHub Actions — `.github/workflows/tests.yml`:

- тесты `pytest` на Ubuntu и Windows, Python 3.11 и 3.13;
- отдельная сборка Docker-образа.

Тестам не нужны API-ключи: они используют временную папку данных и
мок-провайдера. Результаты появятся на вкладке **Actions**.

Если какой-то job упадёт — пришлите ссылку/лог, поправим. Локально тесты
запускаются так:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

## 5. Если Git установить нельзя

Можно загрузить проект через веб-интерфейс GitHub:

1. Создайте пустой репозиторий.
2. На странице репозитория: **Add file → Upload files**.
3. Перетащите **только** папки `app`, `deploy`, `tests`, `samples`, `.github`
   и файлы `README_RU.md`, `DEPLOY.md`, `GITHUB.md`, `requirements*.txt`,
   `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `.env.example`,
   `.gitignore`, `.gitattributes`, `start-local.bat`.
4. Не загружайте `.venv`, `data`, `РЕЕСТР СТО`, PDF, логи, архивы.

Веб-загрузка не создаёт историю коммитов и неудобна для обновлений, но
подходит для первого ознакомления.

Для этого же подготовлен чистый архив `dist/tz-checker-github.zip`:
46 файлов, только код, тесты, примеры и документация. Внутри нет `data/`,
PDF, `.venv`, логов и ключей. Архив не входит в git (каталог `dist/` исключён).

## 6. Проверка перед публикацией

Быстрый чек-лист:

- [ ] `git status --short` не показывает `.venv`, `data`, `РЕЕСТР СТО`, PDF;
- [ ] в `data/settings.json` нечего публиковать — он вообще не в индексе;
- [ ] в коде нет токенов и ключей: `git grep -n -E "sk-|api_key\s*=\s*['\"]"`;
- [ ] репозиторий создан как Private, если есть сомнения;
- [ ] Actions зелёные;
- [ ] в `samples/` только синтетические документы.

Если случайно закоммитили ключ — **отзовите его у провайдера**. Удаление
файла новым коммитом не убирает ключ из истории; для очистки истории нужен
`git filter-repo` или пересоздание репозитория.
