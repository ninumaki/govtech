# 🔥 Smart Fire Safety — Хакатон 2026 (Кейс 02)

Единый веб-сервис мониторинга пожарной безопасности, видео-верификации (Hikvision ISAPI) и диспетчеризации тревог для 3 ролей (**Дежурный оператор**, **Ответственный сотрудник**, **Техник**).

> ⚠️ **ДЕМО / ТЕСТОВЫЙ РЕЖИМ:** Демонстрация не заменяет штатную пожарную сигнализацию и не отправляет реальные вызовы в 101/112.

---

## 🚀 Быстрый запуск

```bash
pip install -r requirements.txt
python3 database.py   # пересоздать БД fire_safety.db с тестовыми данными (seed)
python3 app.py        # запуск сервера на http://127.0.0.1:5000
```

---

## 📂 Структура проекта и зоны ответственности команды

| Участник | Файлы / Модули | Зона ответственности (25%) |
| :--- | :--- | :--- |
| **1. Нүрдәулет** | `database.py`, `app.py` | **Core Backend & DB (Готово в базовом каркасе):** SQLite (`7 таблиц`), `POST /api/signals`, сверка дым+температура (`WARNING` / `FIRE`), анти-дубликат (`repeat_count`), `GET /api/state`, `Audit Log`. |
| **2. Дархан** | `app.py` (блок камер) + UI пульта | **Hikvision & IoT Simulator:** Подключение 3 камер Hikvision (`ISAPI/Streaming/channels/101/picture`, PTZ пресеты, Fallback-кадр) и панель симулятора с ползунками. |
| **3. Бекболат** | `static/index.html` | **2D-план этажей (SVG) & Панель Оператора:** Интерактивная схема 1 и 2 этажей с подсветкой комнат, карточка тревоги с видео и кнопками `ACK` / `ASSIGN`. |
| **4. Тоғжан** | `static/index.html` + Питч | **Роли «Ответственный» и «Техник»:** Закрытие тревог после осмотра (`RESOLVED` / `FALSE_ALARM`), список `OFFLINE`-тикетов, график плановых проверок (`inspections`) и презентация. |

---

## 🔌 Справка по API (Контракт для фронтенда и симулятора)

* `GET /api/state` — возвращает сразу все состояние системы (`buildings`, `zones`, `devices`, `alarms`, `tickets`, `inspections`, `audit_logs`).
* `POST /api/signals` — прием сигнала от датчиков/симулятора:
  ```json
  {"zone_id": 5, "smoke": 65, "temperature": 68}
  ```
* `POST /api/alarms/<id>/ack` — Оператор принимает тревогу в работу (`ACKNOWLEDGED`).
* `POST /api/alarms/<id>/assign` — Оператор назначает ответственного (`{"assigned_to": "Нурлан С."}`).
* `POST /api/alarms/<id>/resolve` — Ответственный закрывает тревогу (`{"resolution_type": "RESOLVED" | "FALSE_ALARM", "comment": "..."}`).
* `POST /api/devices/offline` — Симуляция обрыва связи (`{"zone_id": 5, "type": "smoke"}`) + создание авто-тикета для Техника.
* `POST /api/tickets/<id>/fix` — Техник восстанавливает связь с датчиком.
* `POST /api/inspections` и `POST /api/inspections/<id>/complete` — Управление графиком плановых проверок.
* `POST /api/reset` — Полный сброс базы данных к начальному состоянию перед демонстрацией жюри.

# govtech
