# Tilda: карточка TZ Checker

Сайт `https://golos-platform.tilda.ws/` — статический. TZ Checker — это
FastAPI-приложение с SQLite и обращениями к LLM, поэтому **GitHub Pages его
не запустит**. На GitHub Pages можно положить только витрину/инструкцию,
а само приложение должно работать на сервере или на вашем компьютере.

Сначала получите рабочий HTTPS-адрес приложения, например:

```text
https://tz.golos.click/
```

## 1. Куда вставлять

В Tilda откройте страницу с блоком «Все сервисы платформы» → HTML-блок
(обычно T123), в котором лежит разметка `gls-cards`.

Вкладка «Внутренние сервисы» в текущей вёрстке имеет:

```html
<button class="gls-tab" data-cat="sience" role="tab" aria-selected="false">
  ...
  <span>Внутренние сервисы</span>
  <span class="gls-tab__count">· 4</span>
</button>
```

`data-cat="sience"` — не опечатка в задании, а текущее значение в коде Tilda.
Новую карточку нужно добавлять с тем же `data-cat="sience"`.

## 2. Вставить карточку

Найдите в HTML конец карточки `G.BIM`:

```html
<h3 class="gls-card__title">G.BIM</h3>
<p class="gls-card__desc">Дашборды по проектам</p>
<span class="gls-card__arrow" aria-hidden="true">→</span>
</a>
```

Сразу после её закрывающего `</a>` (и до закрывающего `</div>` блока
`gls-cards`) вставьте содержимое файла
[`deploy/tilda-card.html`](deploy/tilda-card.html):

```html
<a href="https://tz.example.com/?token=PASTE_TOKEN"
   target="_blank"
   rel="noopener noreferrer"
   class="gls-card gls-card-spot"
   data-cat="sience"
   aria-label="Открыть TZ Checker — проверка ТЗ по СТО">
  <div class="gls-card__icon-wrap" aria-hidden="true">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
      <path d="M6 3h9l4 4v14H6z" stroke="#0070F3" stroke-width="1.7" stroke-linejoin="round"></path>
      <path d="M14 3v5h5M9 13h6M9 17h4" stroke="#0070F3" stroke-width="1.7" stroke-linecap="round"></path>
    </svg>
  </div>
  <h3 class="gls-card__title">TZ Checker</h3>
  <p class="gls-card__desc">Проверка технических заданий по корпоративным СТО</p>
  <span class="gls-card__arrow" aria-hidden="true">→</span>
</a>
```

Замените:

- `https://tz.example.com/` — на адрес приложения;
- `PASTE_TOKEN` — на `TZ_CHECKER_TOKEN` с сервера.

## 3. Обновить счётчики

В той же вкладке:

```html
<span class="gls-tab__count">· 4</span>
```

замените на `· 5`.

В шапке сайта при желании обновите «12+ Сервисов» на «13+ Сервисов».

## 4. Проверка

1. Опубликуйте страницу в Tilda.
2. Откройте «Внутренние сервисы» — должна появиться карточка TZ Checker.
3. Нажмите её: приложение откроется в новой вкладке.
4. Токен из ссылки сохранится в `sessionStorage` вкладки и исчезнет из адресной строки.

## 5. Вариант с iframe

Если нужно встроить приложение прямо в страницу Tilda:

```html
<iframe
  src="https://tz.example.com/?token=PASTE_TOKEN"
  style="width:100%;height:900px;border:0;border-radius:16px"
  title="TZ Checker"
  allow="clipboard-read; clipboard-write">
</iframe>
```

Но:

- в `deploy/nginx.conf.example` стоит `X-Frame-Options: DENY` — для iframe
  замените на `SAMEORIGIN` или настройте CSP `frame-ancestors`;
- на мобильных iframe высотой 900 px может быть неудобен;
- токен виден в исходном коде страницы;
- надёжнее открывать приложение отдельной вкладкой, как G.Wiki/G.BIM.

## 6. Безопасность

- Сайт Tilda публичный. Ссылка на TZ Checker и токен в ней будут видны всем,
  кто откроет исходный код страницы. Для внутреннего сервиса этого мало:
  используйте VPN/корпоративный SSO или хотя бы ограничение по IP в Nginx.
- Не публикуйте `data/`, PDF из `РЕЕСТР СТО`, API-ключи и `.env`.
- Токен можно менять на сервере (`TZ_CHECKER_TOKEN`) и обновлять ссылку в Tilda.
- Если сервер в корпоративной сети, карточка на публичной Tilda будет работать
  только у сотрудников с доступом в эту сеть/VPN — это нормально.
