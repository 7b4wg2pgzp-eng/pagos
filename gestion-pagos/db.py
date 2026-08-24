import os
import random
import sqlite3
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USANDO_POSTGRES = bool(DATABASE_URL)

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "pagos.db"))

if USANDO_POSTGRES:
    import psycopg2
    import psycopg2.extras


def get_db():
    if USANDO_POSTGRES:
        # Render exige SSL en la conexión; si el DATABASE_URL no lo especifica,
        # lo forzamos para evitar el error "SSL/TLS required".
        conn = psycopg2.connect(
            DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor,
            sslmode="require",
        )
        return conn
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _run(conn, sql_sqlite, sql_pg, params=()):
    """Ejecuta la variante de SQL que corresponda al backend activo."""
    cur = conn.cursor()
    cur.execute(sql_pg if USANDO_POSTGRES else sql_sqlite, params)
    return cur


def init_db():
    conn = get_db()
    if USANDO_POSTGRES:
        conn.cursor().execute(
            """
            CREATE TABLE IF NOT EXISTS clientes (
                id SERIAL PRIMARY KEY,
                nombre TEXT NOT NULL,
                evento TEXT,
                telefono TEXT,
                monto_esperado NUMERIC(12,2) NOT NULL,
                estado TEXT NOT NULL DEFAULT 'pendiente',
                mp_payment_id TEXT,
                fecha_pago TEXT,
                fecha_creacion TEXT NOT NULL,
                notas TEXT
            )
            """
        )
        conn.cursor().execute(
            """
            CREATE TABLE IF NOT EXISTS eventos_webhook (
                id SERIAL PRIMARY KEY,
                payload TEXT,
                fecha TEXT NOT NULL,
                resultado TEXT
            )
            """
        )
    else:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS clientes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                evento TEXT,
                telefono TEXT,
                monto_esperado REAL NOT NULL,
                estado TEXT NOT NULL DEFAULT 'pendiente',
                mp_payment_id TEXT,
                fecha_pago TEXT,
                fecha_creacion TEXT NOT NULL,
                notas TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS eventos_webhook (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT,
                fecha TEXT NOT NULL,
                resultado TEXT
            )
            """
        )
    conn.commit()
    conn.close()


def _monto_en_uso(conn, monto):
    cur = _run(
        conn,
        "SELECT 1 FROM clientes WHERE estado = 'pendiente' AND monto_esperado = ?",
        "SELECT 1 FROM clientes WHERE estado = 'pendiente' AND monto_esperado = %s",
        (monto,),
    )
    return cur.fetchone() is not None


def generar_monto_unico(conn, monto_base):
    """Suma centavos únicos (no usados por otro cliente pendiente) al monto base
    para poder matchear transferencias por monto exacto."""
    monto_base = round(float(monto_base), 0)
    intentos = list(range(1, 100))
    random.shuffle(intentos)
    for centavos in intentos:
        candidato = round(monto_base + centavos / 100, 2)
        if not _monto_en_uso(conn, candidato):
            return candidato
    return monto_base


def crear_cliente(nombre, evento, telefono, monto_base, notas=""):
    conn = get_db()
    monto = generar_monto_unico(conn, monto_base)
    fecha = datetime.utcnow().isoformat()
    if USANDO_POSTGRES:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO clientes (nombre, evento, telefono, monto_esperado, estado, fecha_creacion, notas)
            VALUES (%s, %s, %s, %s, 'pendiente', %s, %s)
            RETURNING id
            """,
            (nombre, evento, telefono, monto, fecha, notas),
        )
        cliente_id = cur.fetchone()["id"]
    else:
        cur = conn.execute(
            """
            INSERT INTO clientes (nombre, evento, telefono, monto_esperado, estado, fecha_creacion, notas)
            VALUES (?, ?, ?, ?, 'pendiente', ?, ?)
            """,
            (nombre, evento, telefono, monto, fecha, notas),
        )
        cliente_id = cur.lastrowid
    conn.commit()
    conn.close()
    return cliente_id, monto


def listar_clientes():
    conn = get_db()
    cur = _run(
        conn,
        "SELECT * FROM clientes ORDER BY estado ASC, fecha_creacion DESC",
        "SELECT * FROM clientes ORDER BY estado ASC, fecha_creacion DESC",
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def obtener_cliente(cliente_id):
    conn = get_db()
    cur = _run(
        conn,
        "SELECT * FROM clientes WHERE id = ?",
        "SELECT * FROM clientes WHERE id = %s",
        (cliente_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def buscar_pendiente_por_monto(monto):
    conn = get_db()
    cur = _run(
        conn,
        "SELECT * FROM clientes WHERE estado = 'pendiente' AND monto_esperado = ?",
        "SELECT * FROM clientes WHERE estado = 'pendiente' AND monto_esperado = %s",
        (round(float(monto), 2),),
    )
    row = cur.fetchone()
    conn.close()
    return row


def marcar_pagado(cliente_id, mp_payment_id=None):
    conn = get_db()
    fecha = datetime.utcnow().isoformat()
    _run(
        conn,
        "UPDATE clientes SET estado = 'pagado', mp_payment_id = ?, fecha_pago = ? WHERE id = ?",
        "UPDATE clientes SET estado = 'pagado', mp_payment_id = %s, fecha_pago = %s WHERE id = %s",
        (mp_payment_id, fecha, cliente_id),
    )
    conn.commit()
    conn.close()


def log_webhook(payload, resultado):
    conn = get_db()
    fecha = datetime.utcnow().isoformat()
    _run(
        conn,
        "INSERT INTO eventos_webhook (payload, fecha, resultado) VALUES (?, ?, ?)",
        "INSERT INTO eventos_webhook (payload, fecha, resultado) VALUES (%s, %s, %s)",
        (payload, fecha, resultado),
    )
    conn.commit()
    conn.close()
