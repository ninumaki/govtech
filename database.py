import sqlite3
from datetime import datetime, timedelta

DB_PATH = "fire_safety.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_action(actor: str, role: str, action: str, details: str = "", zone_id: int = None, conn=None):
    """Запись события в единый журнал действий (Audit Log)."""
    close_after = False
    if conn is None:
        conn = get_db()
        close_after = True

    conn.execute(
        """
        INSERT INTO audit_logs (timestamp, actor, role, action, details, zone_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (now_str(), actor, role, action, details, zone_id),
    )
    if close_after:
        conn.commit()
        conn.close()


def init_db(force_reset: bool = False):
    """Создание всех 7 таблиц и заполнение начальными данными (Seed)."""
    conn = get_db()
    cur = conn.cursor()

    if force_reset:
        tables = [
            "audit_logs",
            "inspections",
            "tickets",
            "alarms",
            "devices",
            "zones",
            "buildings",
        ]
        for t in tables:
            cur.execute(f"DROP TABLE IF EXISTS {t}")

    # 1. Таблица зданий (buildings)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS buildings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            floors INTEGER NOT NULL DEFAULT 2,
            address TEXT
        )
        """
    )

    # 2. Таблица зон/комнат (zones)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS zones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            building_id INTEGER NOT NULL,
            floor INTEGER NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'NORMAL', -- NORMAL, WARNING, FIRE, OFFLINE
            smoke_level REAL NOT NULL DEFAULT 0.0,
            temperature REAL NOT NULL DEFAULT 22.0,
            camera_id TEXT DEFAULT 'cam_01',
            ptz_preset INTEGER DEFAULT 1,
            updated_at TEXT,
            FOREIGN KEY (building_id) REFERENCES buildings(id)
        )
        """
    )

    # 3. Таблица устройств (devices)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zone_id INTEGER NOT NULL,
            type TEXT NOT NULL, -- smoke, temperature, camera
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ONLINE', -- ONLINE, OFFLINE
            current_value REAL DEFAULT 0.0,
            unit TEXT DEFAULT '',
            trigger_count INTEGER NOT NULL DEFAULT 0,
            false_alarm_count INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            FOREIGN KEY (zone_id) REFERENCES zones(id)
        )
        """
    )

    # 4. Таблица тревог (alarms) с поддержкой анти-дубликата (repeat_count)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS alarms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zone_id INTEGER NOT NULL,
            severity TEXT NOT NULL, -- WARNING, FIRE
            status TEXT NOT NULL DEFAULT 'ACTIVE', -- ACTIVE, ACKNOWLEDGED, ASSIGNED, RESOLVED, FALSE_ALARM
            smoke_value REAL NOT NULL DEFAULT 0.0,
            temp_value REAL NOT NULL DEFAULT 0.0,
            max_smoke REAL NOT NULL DEFAULT 0.0,
            max_temp REAL NOT NULL DEFAULT 0.0,
            repeat_count INTEGER NOT NULL DEFAULT 1,
            assigned_to TEXT DEFAULT NULL,
            snapshot_url TEXT DEFAULT '/api/cameras/snapshot/default',
            resolution_note TEXT DEFAULT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT DEFAULT NULL,
            FOREIGN KEY (zone_id) REFERENCES zones(id)
        )
        """
    )

    # 5. Таблица тикетов неисправностей для Техника (tickets)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id INTEGER NOT NULL,
            zone_id INTEGER NOT NULL,
            issue_type TEXT NOT NULL DEFAULT 'OFFLINE', -- OFFLINE, FREQUENT_TRIGGERS, MAINTENANCE
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN', -- OPEN, FIXED
            created_at TEXT NOT NULL,
            fixed_at TEXT DEFAULT NULL,
            FOREIGN KEY (device_id) REFERENCES devices(id),
            FOREIGN KEY (zone_id) REFERENCES zones(id)
        )
        """
    )

    # 6. Таблица плановых проверок (inspections)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS inspections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zone_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            inspector TEXT NOT NULL DEFAULT 'Техник смены',
            scheduled_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING', -- PENDING, OVERDUE, COMPLETED
            completed_at TEXT DEFAULT NULL,
            notes TEXT DEFAULT '',
            FOREIGN KEY (zone_id) REFERENCES zones(id)
        )
        """
    )

    # 7. Таблица единого журнала действий (audit_logs)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            actor TEXT NOT NULL,
            role TEXT NOT NULL,
            action TEXT NOT NULL,
            details TEXT,
            zone_id INTEGER DEFAULT NULL
        )
        """
    )

    # Проверяем, нужно ли заполнять seed-данными
    cur.execute("SELECT COUNT(*) as cnt FROM buildings")
    if cur.fetchone()["cnt"] == 0:
        seed_data(conn)

    conn.commit()
    conn.close()


def seed_data(conn):
    cur = conn.cursor()
    ts = now_str()

    # 1. Здание: Главный корпус №1 (2 этажа)
    cur.execute(
        "INSERT INTO buildings (id, name, floors, address) VALUES (1, 'Главный корпус №1', 2, 'Астана, пр. Мангилик Ел, С2')"
    )

    # 2. Зоны (1-й и 2-й этажи)
    zones_seed = [
        # 1-й этаж
        (1, 1, 1, "Холл", "NORMAL", 2.0, 22.5, "cam_01", 1),
        (2, 1, 1, "Столовая", "NORMAL", 4.0, 24.0, "cam_02", 1),
        (3, 1, 1, "Склад", "NORMAL", 1.0, 20.0, "cam_03", 1),
        (4, 1, 1, "Электрощитовая", "NORMAL", 0.0, 25.5, "cam_02", 2),
        # 2-й этаж
        (5, 1, 2, "Серверная", "NORMAL", 0.0, 19.5, "cam_02", 3),
        (6, 1, 2, "Лаборатория", "NORMAL", 3.0, 23.0, "cam_01", 2),
        (7, 1, 2, "Архив", "NORMAL", 1.0, 21.0, "cam_03", 2),
        (8, 1, 2, "Коридор", "NORMAL", 1.0, 22.0, "cam_01", 3),
    ]

    for z in zones_seed:
        cur.execute(
            """
            INSERT INTO zones (id, building_id, floor, name, status, smoke_level, temperature, camera_id, ptz_preset, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*z, ts),
        )

    # 3. Устройства: в каждой комнате по 3 устройства (smoke, temperature, camera)
    for z in zones_seed:
        z_id, _, floor, z_name, _, smoke_val, temp_val, cam_id, _ = z
        cur.execute(
            """
            INSERT INTO devices (zone_id, type, name, status, current_value, unit, trigger_count, false_alarm_count, last_seen)
            VALUES (?, 'smoke', ?, 'ONLINE', ?, '%', 0, 0, ?)
            """,
            (z_id, f"Датчик дыма SD-{z_id}01 ({z_name})", smoke_val, ts),
        )
        cur.execute(
            """
            INSERT INTO devices (zone_id, type, name, status, current_value, unit, trigger_count, false_alarm_count, last_seen)
            VALUES (?, 'temperature', ?, 'ONLINE', ?, '°C', 0, 0, ?)
            """,
            (z_id, f"Датчик темп. TD-{z_id}02 ({z_name})", temp_val, ts),
        )
        cur.execute(
            """
            INSERT INTO devices (zone_id, type, name, status, current_value, unit, trigger_count, false_alarm_count, last_seen)
            VALUES (?, 'camera', ?, 'ONLINE', 1.0, 'stream', 0, 0, ?)
            """,
            (z_id, f"Камера Hikvision ({cam_id.upper()}) — {z_name}", ts),
        )

    # 4. Плановые проверки (Inspections) — для экрана Техника (Тоғжан)
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    next_week = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")

    inspections_seed = [
        (4, "Проверка датчиков дыма и тепловизора в Электрощитовой", "Техник Ерлан", yesterday, "OVERDUE", None, "Просрочено на 3 дня! Требуется срочный осмотр"),
        (5, "Регламентный тест системы пожаротушения Серверной", "Техник Ерлан", today, "PENDING", None, "Плановая ежемесячная проверка"),
        (2, "Очистка оптических камер датчиков дыма на кухне Столовой", "Техник Асхат", next_week, "PENDING", None, "Профилактика ложных срабатываний от пара"),
        (1, "Проверка купольной камеры и извещателей Холла 1-го этажа", "Техник Ерлан", yesterday, "COMPLETED", ts, "Все датчики в норме, оптика чистая"),
    ]
    for insp in inspections_seed:
        cur.execute(
            """
            INSERT INTO inspections (zone_id, title, inspector, scheduled_date, status, completed_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            insp,
        )

    # 5. Начальная запись в журнал действий
    log_action(
        actor="Система (Ядро)",
        role="SYSTEM",
        action="INIT_DB",
        details="База данных развернута. Загружен Главный корпус №1 (8 зон, 24 устройства).",
        conn=conn,
    )


if __name__ == "__main__":
    init_db(force_reset=True)
    print(f"[OK] База данных '{DB_PATH}' успешно создана и заполнена тестовыми данными!")
