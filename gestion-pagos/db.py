import sqlite3
import os
import random
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "pagos.db"))


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
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
    row = conn.execute(
        "SELECT 1 FROM clientes WHERE estado = 'pendiente' AND monto_esperado = ?",
        (monto,),
    ).fetchone()
    return row is not None


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
    # fallback extremo: usar el monto base tal cual
    return monto_base


def crear_cliente(nombre, evento, telefono, monto_base, notas=""):
    conn = get_db()
    monto = generar_monto_unico(conn, monto_base)
    cur = conn.execute(
        """
        INSERT INTO clientes (nombre, evento, telefono, monto_esperado, estado, fecha_creacion, notas)
        VALUES (?, ?, ?, ?, 'pendiente', ?, ?)
        """,
        (nombre, evento, telefono, monto, datetime.utcnow().isoformat(), notas),
    )
    conn.commit()
    cliente_id = cur.lastrowid
    conn.close()
    return cliente_id, monto


def listar_clientes():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM clientes ORDER BY estado ASC, fecha_creacion DESC"
    ).fetchall()
    conn.close()
    return rows


def obtener_cliente(cliente_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
    conn.close()
    return row


def buscar_pendiente_por_monto(monto):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM clientes WHERE estado = 'pendiente' AND monto_esperado = ?",
        (round(float(monto), 2),),
    ).fetchone()
    conn.close()
    return row


def marcar_pagado(cliente_id, mp_payment_id=None):
    conn = get_db()
    conn.execute(
        """
        UPDATE clientes
        SET estado = 'pagado', mp_payment_id = ?, fecha_pago = ?
        WHERE id = ?
        """,
        (mp_payment_id, datetime.utcnow().isoformat(), cliente_id),
    )
    conn.commit()
    conn.close()


def log_webhook(payload, resultado):
    conn = get_db()
    conn.execute(
        "INSERT INTO eventos_webhook (payload, fecha, resultado) VALUES (?, ?, ?)",
        (payload, datetime.utcnow().isoformat(), resultado),
    )
    conn.commit()
    conn.close()
