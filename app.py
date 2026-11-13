from flask import Flask, request, jsonify, send_from_directory, Response
from database import get_db, init_db, log_action, now_str
import os

app = Flask(__name__, static_folder="static", static_url_path="")

# Инициализируем базу данных при старте сервера
init_db(force_reset=False)


def evaluate_signal(smoke: float, temperature: float) -> str:
    """
    Логика порогов и сверки (Корреляция датчиков — задача №1 Нүрдәулета):
    - WARNING (Желтый): только 1 показатель умеренно (temp 45..56.9°C ИЛИ smoke 15..35%)
    - FIRE (Красный): temp >= 57°C ИЛИ smoke > 35% ИЛИ оба датчика сработали одновременно (temp >= 45 И smoke >= 15)
    - NORMAL (Зеленый): ниже порогов
    """
    high_temp = temperature >= 57.0
    high_smoke = smoke > 35.0
    mod_temp = 45.0 <= temperature < 57.0
    mod_smoke = 15.0 <= smoke <= 35.0

    if high_temp or high_smoke or (mod_temp and mod_smoke):
        return "FIRE"
    if mod_temp or mod_smoke:
        return "WARNING"
    return "NORMAL"


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ==============================================================================
# 1. ГЛАВНЫЙ ЭНДПОИНТ СОСТОЯНИЯ (ДЛЯ ВСЕХ РОЛЕЙ И 2D-КАРТЫ)
# ==============================================================================
@app.route("/api/state", methods=["GET"])
def get_state():
    """
    Возвращает полное состояние системы одним JSON:
    buildings, zones, devices, active_alarms, tickets, inspections, audit_logs.
    """
    conn = get_db()
    cur = conn.cursor()

    buildings = [dict(r) for r in cur.execute("SELECT * FROM buildings").fetchall()]
    zones = [dict(r) for r in cur.execute("SELECT * FROM zones ORDER BY floor, id").fetchall()]
    devices = [dict(r) for r in cur.execute("SELECT * FROM devices ORDER BY zone_id, id").fetchall()]

    alarms = [
        dict(r)
        for r in cur.execute(
            """
            SELECT a.*, z.name as zone_name, z.floor as floor, z.camera_id as camera_id
            FROM alarms a
            JOIN zones z ON a.zone_id = z.id
            ORDER BY CASE WHEN a.status IN ('ACTIVE', 'ACKNOWLEDGED', 'ASSIGNED') THEN 0 ELSE 1 END,
                     a.updated_at DESC
            LIMIT 50
            """
        ).fetchall()
    ]

    tickets = [
        dict(r)
        for r in cur.execute(
            """
            SELECT t.*, d.name as device_name, d.type as device_type, z.name as zone_name, z.floor as floor
            FROM tickets t
            JOIN devices d ON t.device_id = d.id
            JOIN zones z ON t.zone_id = z.id
            ORDER BY CASE WHEN t.status = 'OPEN' THEN 0 ELSE 1 END, t.created_at DESC
            """
        ).fetchall()
    ]

    inspections = [
        dict(r)
        for r in cur.execute(
            """
            SELECT i.*, z.name as zone_name, z.floor as floor
            FROM inspections i
            JOIN zones z ON i.zone_id = z.id
            ORDER BY CASE i.status WHEN 'OVERDUE' THEN 0 WHEN 'PENDING' THEN 1 ELSE 2 END, i.scheduled_date ASC
            """
        ).fetchall()
    ]

    audit_logs = [
        dict(r)
        for r in cur.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 60").fetchall()
    ]

    conn.close()
    return jsonify(
        {
            "demo_mode": True,
            "timestamp": now_str(),
            "buildings": buildings,
            "zones": zones,
            "devices": devices,
            "alarms": alarms,
            "tickets": tickets,
            "inspections": inspections,
            "audit_logs": audit_logs,
        }
    )


# ==============================================================================
# 2. ПРИЕМ СИГНАЛОВ И ЗАЩИТА ОТ ДУБЛЕЙ (ЗАДАЧА №1 — НҮРДӘУЛЕТ)
# ==============================================================================
@app.route("/api/signals", methods=["POST"])
def receive_signal():
    """
    Принимает JSON: {"zone_id": 5, "smoke": 65, "temperature": 68}
    Сверяет пороги, обновляет датчики и реализует Deduplication (repeat_count).
    """
    data = request.get_json(force=True) or {}
    zone_id = int(data.get("zone_id", 1))
    smoke = float(data.get("smoke", 0.0))
    temperature = float(data.get("temperature", 22.0))
    ts = now_str()

    conn = get_db()
    cur = conn.cursor()

    zone = cur.execute("SELECT * FROM zones WHERE id = ?", (zone_id,)).fetchone()
    if not zone:
        conn.close()
        return jsonify({"error": f"Zone {zone_id} not found"}), 404

    zone_name = zone["name"]
    severity = evaluate_signal(smoke, temperature)

    # Обновляем текущие показания устройств в зоне
    cur.execute(
        "UPDATE devices SET current_value = ?, last_seen = ?, status = 'ONLINE' WHERE zone_id = ? AND type = 'smoke'",
        (smoke, ts, zone_id),
    )
    cur.execute(
        "UPDATE devices SET current_value = ?, last_seen = ?, status = 'ONLINE' WHERE zone_id = ? AND type = 'temperature'",
        (temperature, ts, zone_id),
    )

    if severity == "NORMAL":
        # Проверяем, нет ли открытой тревоги. Если нет — ставим NORMAL
        open_alarm = cur.execute(
            "SELECT id FROM alarms WHERE zone_id = ? AND status IN ('ACTIVE', 'ACKNOWLEDGED', 'ASSIGNED')",
            (zone_id,),
        ).fetchone()
        new_zone_status = zone["status"] if open_alarm else "NORMAL"
        cur.execute(
            "UPDATE zones SET smoke_level = ?, temperature = ?, status = ?, updated_at = ? WHERE id = ?",
            (smoke, temperature, new_zone_status, ts, zone_id),
        )
        log_action(
            actor="IoT Датчики",
            role="SENSOR",
            action="SIGNAL_NORMAL",
            details=f"Зона «{zone_name}»: Дым {smoke}%, Темп {temperature}°C (Норма)",
            zone_id=zone_id,
            conn=conn,
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "evaluation": "NORMAL", "deduplicated": False})

    # Увеличиваем счетчик срабатываний датчиков
    if smoke >= 15.0:
        cur.execute(
            "UPDATE devices SET trigger_count = trigger_count + 1 WHERE zone_id = ? AND type = 'smoke'",
            (zone_id,),
        )
    if temperature >= 45.0:
        cur.execute(
            "UPDATE devices SET trigger_count = trigger_count + 1 WHERE zone_id = ? AND type = 'temperature'",
            (zone_id,),
        )

    # ПРОВЕРКА НА ДУБЛИКАТЫ (Deduplication):
    # Ищем открытую тревогу в этой же зоне
    existing = cur.execute(
        """
        SELECT * FROM alarms
        WHERE zone_id = ? AND status IN ('ACTIVE', 'ACKNOWLEDGED', 'ASSIGNED')
        ORDER BY id DESC LIMIT 1
        """,
        (zone_id,),
    ).fetchone()

    snapshot_url = f"/api/cameras/snapshot/{zone['camera_id']}?zone_id={zone_id}"

    if existing:
        # Дубликат! Не создаем новую тревогу, а увеличиваем repeat_count
        new_repeat = existing["repeat_count"] + 1
        new_max_smoke = max(existing["max_smoke"], smoke)
        new_max_temp = max(existing["max_temp"], temperature)
        # Если был WARNING, а пришел FIRE — повышаем уровень до FIRE
        new_severity = "FIRE" if (existing["severity"] == "FIRE" or severity == "FIRE") else "WARNING"

        cur.execute(
            """
            UPDATE alarms
            SET severity = ?, smoke_value = ?, temp_value = ?,
                max_smoke = ?, max_temp = ?, repeat_count = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                new_severity,
                smoke,
                temperature,
                new_max_smoke,
                new_max_temp,
                new_repeat,
                ts,
                existing["id"],
            ),
        )
        cur.execute(
            "UPDATE zones SET status = ?, smoke_level = ?, temperature = ?, updated_at = ? WHERE id = ?",
            (new_severity, smoke, temperature, ts, zone_id),
        )
        log_action(
            actor="Анти-дубликат (Ядро)",
            role="SYSTEM",
            action="ALARM_DEDUPLICATED",
            details=f"Зона «{zone_name}»: повторный сигнал #{new_repeat} объединен с тревогой #{existing['id']} (Дым {smoke}%, Темп {temperature}°C -> {new_severity})",
            zone_id=zone_id,
            conn=conn,
        )
        conn.commit()
        conn.close()
        return jsonify(
            {
                "status": "ok",
                "evaluation": new_severity,
                "deduplicated": True,
                "alarm_id": existing["id"],
                "repeat_count": new_repeat,
            }
        )
    else:
        # Создаем новую тревогу
        cur.execute(
            """
            INSERT INTO alarms (
                zone_id, severity, status, smoke_value, temp_value,
                max_smoke, max_temp, repeat_count, snapshot_url, created_at, updated_at
            )
            VALUES (?, ?, 'ACTIVE', ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (zone_id, severity, smoke, temperature, smoke, temperature, snapshot_url, ts, ts),
        )
        alarm_id = cur.lastrowid
        cur.execute(
            "UPDATE zones SET status = ?, smoke_level = ?, temperature = ?, updated_at = ? WHERE id = ?",
            (severity, smoke, temperature, ts, zone_id),
        )
        log_action(
            actor="Система корреляции",
            role="SYSTEM",
            action=f"ALARM_{severity}",
            details=f"Создана тревога #{alarm_id} в зоне «{zone_name}» (Дым {smoke}%, Темп {temperature}°C). Авто-снапшот камеры {zone['camera_id']}.",
            zone_id=zone_id,
            conn=conn,
        )
        conn.commit()
        conn.close()
        return jsonify(
            {
                "status": "ok",
                "evaluation": severity,
                "deduplicated": False,
                "alarm_id": alarm_id,
                "repeat_count": 1,
            }
        )


# ==============================================================================
# 3. ЖУРНАЛ ДЕЙСТВИЙ AUDIT LOG (ЗАДАЧА №1 — НҮРДӘУЛЕТ)
# ==============================================================================
@app.route("/api/audit_logs", methods=["GET"])
def get_audit_logs():
    conn = get_db()
    logs = [
        dict(r)
        for r in conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 100").fetchall()
    ]
    conn.close()
    return jsonify(logs)


# ==============================================================================
# 4. ЭНДПОИНТЫ ОПЕРАТОРА И ОТВЕТСТВЕННОГО (ДЛЯ БЕКБОЛАТА И ТОҒЖАН)
# ==============================================================================
@app.route("/api/alarms/<int:alarm_id>/ack", methods=["POST"])
def ack_alarm(alarm_id):
    """Оператор принимает тревогу в работу."""
    conn = get_db()
    cur = conn.cursor()
    alarm = cur.execute("SELECT * FROM alarms WHERE id = ?", (alarm_id,)).fetchone()
    if not alarm:
        conn.close()
        return jsonify({"error": "Alarm not found"}), 404

    ts = now_str()
    cur.execute(
        "UPDATE alarms SET status = 'ACKNOWLEDGED', updated_at = ? WHERE id = ?",
        (ts, alarm_id),
    )
    log_action(
        actor="Дежурный оператор",
        role="OPERATOR",
        action="ACK_ALARM",
        details=f"Тревога #{alarm_id} принята в работу оператором.",
        zone_id=alarm["zone_id"],
        conn=conn,
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "alarm_id": alarm_id, "new_status": "ACKNOWLEDGED"})


@app.route("/api/alarms/<int:alarm_id>/assign", methods=["POST"])
def assign_alarm(alarm_id):
    """Оператор назначает Ответственного сотрудника на проверку помещения."""
    data = request.get_json(force=True) or {}
    assignee = data.get("assigned_to", "Ответственный смены (Нурлан С.)")

    conn = get_db()
    cur = conn.cursor()
    alarm = cur.execute("SELECT * FROM alarms WHERE id = ?", (alarm_id,)).fetchone()
    if not alarm:
        conn.close()
        return jsonify({"error": "Alarm not found"}), 404

    ts = now_str()
    cur.execute(
        "UPDATE alarms SET status = 'ASSIGNED', assigned_to = ?, updated_at = ? WHERE id = ?",
        (assignee, ts, alarm_id),
    )
    log_action(
        actor="Дежурный оператор",
        role="OPERATOR",
        action="ASSIGN_ALARM",
        details=f"Тревога #{alarm_id} назначена на: {assignee}",
        zone_id=alarm["zone_id"],
        conn=conn,
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "alarm_id": alarm_id, "new_status": "ASSIGNED", "assigned_to": assignee})


@app.route("/api/alarms/<int:alarm_id>/resolve", methods=["POST"])
def resolve_alarm(alarm_id):
    """Ответственный сотрудник закрывает тревогу после осмотра (RESOLVED или FALSE_ALARM)."""
    data = request.get_json(force=True) or {}
    resolution_type = data.get("resolution_type", "RESOLVED")  # RESOLVED или FALSE_ALARM
    comment = data.get("comment", "Осмотрено на месте, угроза устранена.")

    conn = get_db()
    cur = conn.cursor()
    alarm = cur.execute("SELECT * FROM alarms WHERE id = ?", (alarm_id,)).fetchone()
    if not alarm:
        conn.close()
        return jsonify({"error": "Alarm not found"}), 404

    ts = now_str()
    zone_id = alarm["zone_id"]

    cur.execute(
        """
        UPDATE alarms
        SET status = ?, resolution_note = ?, updated_at = ?, resolved_at = ?
        WHERE id = ?
        """,
        (resolution_type, comment, ts, ts, alarm_id),
    )

    # Возвращаем зону в норму (NORMAL) и сбрасываем текущие показатели
    cur.execute(
        "UPDATE zones SET status = 'NORMAL', smoke_level = 1.0, temperature = 22.0, updated_at = ? WHERE id = ?",
        (ts, zone_id),
    )
    cur.execute(
        "UPDATE devices SET current_value = 1.0 WHERE zone_id = ? AND type = 'smoke'",
        (zone_id,),
    )
    cur.execute(
        "UPDATE devices SET current_value = 22.0 WHERE zone_id = ? AND type = 'temperature'",
        (zone_id,),
    )

    if resolution_type == "FALSE_ALARM":
        cur.execute(
            "UPDATE devices SET false_alarm_count = false_alarm_count + 1 WHERE zone_id = ? AND type IN ('smoke', 'temperature')",
            (zone_id,),
        )

    log_action(
        actor="Ответственный сотрудник",
        role="RESPONSIBLE",
        action=f"RESOLVE_{resolution_type}",
        details=f"Тревога #{alarm_id} закрыта ({resolution_type}). Комментарий: {comment}",
        zone_id=zone_id,
        conn=conn,
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "alarm_id": alarm_id, "new_status": resolution_type})


# ==============================================================================
# 5. ЭНДПОИНТЫ СИМУЛЯТОРА ОБРЫВА И ТЕХНИКА (ДЛЯ ДАРХАНА И ТОҒЖАН)
# ==============================================================================
@app.route("/api/devices/offline", methods=["POST"])
def simulate_offline():
    """Симуляция потери связи с датчиком в выбранной зоне + создание авто-тикета Технику."""
    data = request.get_json(force=True) or {}
    zone_id = int(data.get("zone_id", 1))
    device_type = data.get("type", "smoke")

    conn = get_db()
    cur = conn.cursor()
    dev = cur.execute(
        "SELECT * FROM devices WHERE zone_id = ? AND type = ? LIMIT 1",
        (zone_id, device_type),
    ).fetchone()
    if not dev:
        conn.close()
        return jsonify({"error": "Device not found"}), 404

    ts = now_str()
    cur.execute("UPDATE devices SET status = 'OFFLINE', last_seen = ? WHERE id = ?", (ts, dev["id"]))
    cur.execute("UPDATE zones SET status = 'OFFLINE', updated_at = ? WHERE id = ?", (ts, zone_id))

    # Проверяем, нет ли уже открытого тикета для этого устройства
    existing_ticket = cur.execute(
        "SELECT id FROM tickets WHERE device_id = ? AND status = 'OPEN'", (dev["id"],)
    ).fetchone()

    ticket_id = existing_ticket["id"] if existing_ticket else None
    if not existing_ticket:
        cur.execute(
            """
            INSERT INTO tickets (device_id, zone_id, issue_type, description, status, created_at)
            VALUES (?, ?, 'OFFLINE', ?, 'OPEN', ?)
            """,
            (dev["id"], zone_id, f"Потеря связи с устройством {dev['name']} (Heartbeat timeout)", ts),
        )
        ticket_id = cur.lastrowid

    log_action(
        actor="Мониторинг связи",
        role="SYSTEM",
        action="DEVICE_OFFLINE",
        details=f"Обрыв связи с «{dev['name']}». Создан авто-тикет #{ticket_id} для Техника.",
        zone_id=zone_id,
        conn=conn,
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "device_id": dev["id"], "ticket_id": ticket_id})


@app.route("/api/tickets/<int:ticket_id>/fix", methods=["POST"])
def fix_ticket(ticket_id):
    """Техник восстанавливает связь с устройством и закрывает тикет."""
    conn = get_db()
    cur = conn.cursor()
    ticket = cur.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not ticket:
        conn.close()
        return jsonify({"error": "Ticket not found"}), 404

    ts = now_str()
    cur.execute("UPDATE tickets SET status = 'FIXED', fixed_at = ? WHERE id = ?", (ts, ticket_id))
    cur.execute("UPDATE devices SET status = 'ONLINE', last_seen = ? WHERE id = ?", (ts, ticket["device_id"]))

    # Если в зоне нет активных тревог, возвращаем статус зоны в NORMAL
    open_alarm = cur.execute(
        "SELECT severity FROM alarms WHERE zone_id = ? AND status IN ('ACTIVE', 'ACKNOWLEDGED', 'ASSIGNED') LIMIT 1",
        (ticket["zone_id"],),
    ).fetchone()
    new_zone_status = open_alarm["severity"] if open_alarm else "NORMAL"
    cur.execute("UPDATE zones SET status = ?, updated_at = ? WHERE id = ?", (new_zone_status, ts, ticket["zone_id"]))

    log_action(
        actor="Техник",
        role="TECHNICIAN",
        action="TICKET_FIXED",
        details=f"Тикет #{ticket_id} закрыт: связь с устройством восстановлена, тест пройден.",
        zone_id=ticket["zone_id"],
        conn=conn,
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "ticket_id": ticket_id})


@app.route("/api/inspections", methods=["POST"])
def create_inspection():
    """Добавление новой плановой проверки (экран Техника)."""
    data = request.get_json(force=True) or {}
    zone_id = int(data.get("zone_id", 1))
    title = data.get("title", "Плановая проверка извещателей")
    scheduled_date = data.get("scheduled_date", now_str()[:10])
    inspector = data.get("inspector", "Техник смены")

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO inspections (zone_id, title, inspector, scheduled_date, status, notes)
        VALUES (?, ?, ?, ?, 'PENDING', 'Запланировано через панель Техника')
        """,
        (zone_id, title, inspector, scheduled_date),
    )
    insp_id = cur.lastrowid
    log_action(
        actor="Техник",
        role="TECHNICIAN",
        action="INSPECTION_CREATED",
        details=f"Добавлена плановая проверка #{insp_id}: «{title}» на {scheduled_date}",
        zone_id=zone_id,
        conn=conn,
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "inspection_id": insp_id})


@app.route("/api/inspections/<int:insp_id>/complete", methods=["POST"])
def complete_inspection(insp_id):
    """Отметка плановой проверки как выполненной."""
    conn = get_db()
    cur = conn.cursor()
    insp = cur.execute("SELECT * FROM inspections WHERE id = ?", (insp_id,)).fetchone()
    if not insp:
        conn.close()
        return jsonify({"error": "Inspection not found"}), 404

    ts = now_str()
    cur.execute(
        "UPDATE inspections SET status = 'COMPLETED', completed_at = ?, notes = 'Выполнено в срок, замечаний нет' WHERE id = ?",
        (ts, insp_id),
    )
    log_action(
        actor="Техник",
        role="TECHNICIAN",
        action="INSPECTION_COMPLETED",
        details=f"Плановая проверка #{insp_id} («{insp['title']}») отмечена выполненной.",
        zone_id=insp["zone_id"],
        conn=conn,
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "inspection_id": insp_id})


# ==============================================================================
# 6. ЗАГЛУШКА КАМЕР HIKVISION (FALLBACK ДЛЯ ДАРХАНА)
# ==============================================================================
@app.route("/api/cameras/snapshot/<cam_id>", methods=["GET"])
def camera_snapshot_fallback(cam_id):
    """
    Генерирует SVG-снапшот с таймстампом (Fallback), если физическая камера Hikvision не подключена.
    Дархан сможет расширить этот роут реальным ISAPI-запросом к 3 камерам.
    """
    zone_id = request.args.get("zone_id", "1")
    ts = now_str()
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">
      <rect width="100%" height="100%" fill="#0f172a"/>
      <rect x="16" y="16" width="608" height="328" rx="8" fill="#1e293b" stroke="#334155" stroke-width="2"/>
      <circle cx="45" cy="45" r="8" fill="#ef4444"/>
      <text x="62" y="50" fill="#f8fafc" font-family="monospace" font-size="15" font-weight="bold">REC • HIKVISION {cam_id.upper()} (ZONE #{zone_id})</text>
      <text x="410" y="50" fill="#94a3b8" font-family="monospace" font-size="13">{ts}</text>
      <text x="320" y="175" fill="#64748b" font-family="sans-serif" font-size="18" text-anchor="middle">Видеопоток ISAPI / Fallback Snapshot</text>
      <text x="320" y="205" fill="#38bdf8" font-family="monospace" font-size="14" text-anchor="middle">Камера готова к интеграции (Модуль Дархана)</text>
    </svg>"""
    return Response(svg, mimetype="image/svg+xml")


@app.route("/api/reset", methods=["POST"])
def reset_database():
    """Полный сброс базы к начальному состоянию перед выступлением жюри."""
    init_db(force_reset=True)
    return jsonify({"status": "ok", "message": "База данных сброшена к исходным демо-данным"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🔥 Smart Fire Safety Server запущен на http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=True)
