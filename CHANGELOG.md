# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии — [SemVer](https://semver.org/lang/ru/).
Коммиты — [Conventional Commits](https://www.conventionalcommits.org/ru/v1.0.0/).

## [3.0.0] — 2026-10-25

Первый релиз форка. База — [kavore/remnawave-tg-shop](https://github.com/kavore/remnawave-tg-shop) `9ef68bf`
(v2.7.0 + 7 коммитов, 2026-03-17). Проверено на Remnawave backend 3.4.7 / node 3.4.3.

### ⚠ BREAKING CHANGES

- **Требуется Remnawave ≥ 3.0.** Панель 3.x удалила `users.uuid`; пользователи адресуются числовым `id`.
  С панелью 2.x эта версия не работает.
- **Миграция БД `0004_panel_user_id`** (применяется автоматически при старте): добавляет `users.panel_user_id`
  (уникальный индекс) и `subscriptions.panel_user_id`, снимает `NOT NULL` с `subscriptions.panel_user_uuid`.
  Старые колонки `panel_user_uuid` сохраняются как legacy. Заполнение `panel_user_id` для существующих
  пользователей — автоматической синхронизацией при старте (по `telegramId`) или заранее, SQL по карте
  `uuid → id`, выгруженной из панели до обновления.
- **Уведомления об окончании подписки** приходят только по событию панели `user.expiration`
  (вместо удалённых `user.expires_in_*` / `user.expired_24_hours_ago`). В панели нужны
  `WEBHOOK_ENABLED=true`, `WEBHOOK_URL=https://<бот>/webhook/panel`, `WEBHOOK_SECRET_HEADER` = `PANEL_WEBHOOK_SECRET` бота
  (≥ 32 символов, только `[A-Za-z0-9]`), `EXPIRATION_NOTIFICATIONS_ENABLED=true`,
  `EXPIRATION_NOTIFICATIONS=[-72, -48, -24, 24]`.
- **Шифрование ссылок Happ (CRYPT4) недоступно:** эндпоинт `/system/tools/happ/encrypt` удалён в панели;
  используйте `CRYPT4_ENABLED=False`.

### Added

- Продление оплаченных периодов (все платёжные провайдеры) и бонусных дней (промокоды, рефералы, админ)
  считается **от срока в панели** (`expireAt`), а не от копии в БД бота; для только что созданного
  пользователя панели — от текущего момента (#1).
- Тесты (pytest): клиент API панели по контракту 3.4.x, вебхуки панели, расчёт базы продления.
- CI (GitHub Actions): pyflakes (неопределённые имена), pytest, alembic upgrade/downgrade/upgrade на PostgreSQL 17,
  сборка образа linux/amd64 и публикация в `ghcr.io/lysak0v/remnawave-tg-shop` (из `main` и тегов `v*`).

### Changed

- Поиск пользователя по Telegram ID — `GET /users/stream?telegramId=` (точное совпадение, пагинация курсором)
  вместо удалённого `/users/by-telegram-id`.
- `PATCH /users`, действия `enable/disable`, `DELETE`, устройства (`/hwid/devices/{userId}`,
  `POST /hwid/devices/delete {userId, hwid}`) — по числовому `id`.
- Синхронизация с панелью сопоставляет пользователей по `id`/`telegramId`.
- Карточка пользователя в админке показывает «ID в панели» (новый ключ локализации `admin_user_panel_id_label`).
- Образ в примерах `docker-compose*.yml` — `ghcr.io/lysak0v/remnawave-tg-shop`.
- Обработка вебхука `user.expiration` и переход на числовой id портированы из
  [gldkru/remnawave-tg-shop](https://github.com/gldkru/remnawave-tg-shop) (`1251bb7`, `639c72f`) с доработками.

### Fixed

- Ответ «не найден» распознаётся по коду из `details.errorCode` (`A025`/`A062`) или HTTP 404; раньше код
  читался с верхнего уровня ответа и проверка не срабатывала. `A040` больше не считается «пользователь удалён».
- `DELETE /users/{id}` с пустым ответом 2xx считается успешным.
- Исправления в портированном коде: падение карточки пользователя в админке (срез по числовому id),
  неопределённая переменная в синхронизации, утерянный параметр `traffic_limit_strategy` для ГБ-пакетов.

### Removed

- Workflow публикации образа в Docker Hub upstream-проекта.

[3.0.0]: https://github.com/lysak0v/remnawave-tg-shop/releases/tag/v3.0.0
