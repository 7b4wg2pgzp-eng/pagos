import os
import random
import sqlite3
import uuid
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


def _ph():
    """Placeholder de parámetros: %s en Postgres, ? en SQLite."""
    return "%s" if USANDO_POSTGRES else "?"


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
        # Migración: columnas para la calculadora de cuotas (planes). Se
        # agregan sobre una tabla que puede ya existir en producción.
        for col_sql in (
            "ALTER TABLE clientes ADD COLUMN IF NOT EXISTS fecha_vencimiento TEXT",
            "ALTER TABLE clientes ADD COLUMN IF NOT EXISTS concepto TEXT",
            "ALTER TABLE clientes ADD COLUMN IF NOT EXISTS plan_token TEXT",
        ):
            conn.cursor().execute(col_sql)
        conn.cursor().execute(
            """
            CREATE TABLE IF NOT EXISTS planes (
                plan_token TEXT PRIMARY KEY,
                nombre TEXT,
                usuario TEXT UNIQUE,
                password_hash TEXT,
                fecha_evento TEXT,
                cantidad_cuotas INTEGER,
                fecha_creacion TEXT NOT NULL
            )
            """
        )
        conn.cursor().execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                clave TEXT PRIMARY KEY,
                valor TEXT NOT NULL
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
                notas TEXT,
                fecha_vencimiento TEXT,
                concepto TEXT,
                plan_token TEXT
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
        # Migración para bases SQLite creadas antes de agregar estas columnas.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(clientes)").fetchall()}
        for col in ("fecha_vencimiento", "concepto", "plan_token"):
            if col not in cols:
                conn.execute(f"ALTER TABLE clientes ADD COLUMN {col} TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS planes (
                plan_token TEXT PRIMARY KEY,
                nombre TEXT,
                usuario TEXT UNIQUE,
                password_hash TEXT,
                fecha_evento TEXT,
                cantidad_cuotas INTEGER,
                fecha_creacion TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                clave TEXT PRIMARY KEY,
                valor TEXT NOT NULL
            )
            """
        )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Configuración editable desde el panel (montos del presupuesto)
# --------------------------------------------------------------------------

CONFIG_DEFECTO = {
    "presupuesto_base": "500000",
    "presupuesto_financiado": "550000",
    "sena": "250000",
}


def leer_config():
    """Devuelve el dict de configuración, completando con los valores por
    defecto las claves que todavía no se hayan guardado."""
    conf = dict(CONFIG_DEFECTO)
    try:
        conn = get_db()
        cur = _run(conn, "SELECT clave, valor FROM config", "SELECT clave, valor FROM config")
        for row in cur.fetchall():
            clave = row["clave"] if not isinstance(row, tuple) else row[0]
            valor = row["valor"] if not isinstance(row, tuple) else row[1]
            if clave in conf:
                conf[clave] = valor
        conn.close()
    except Exception:
        # Si la tabla todavía no existe (primer arranque), usamos los defaults.
        pass
    return {k: int(float(v)) for k, v in conf.items()}


def guardar_config(nuevos):
    """Guarda (upsert) las claves de configuración recibidas."""
    conn = get_db()
    for clave, valor in nuevos.items():
        if clave not in CONFIG_DEFECTO:
            continue
        _run(
            conn,
            "INSERT INTO config (clave, valor) VALUES (?, ?) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
            "INSERT INTO config (clave, valor) VALUES (%s, %s) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
            (clave, str(int(valor))),
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


def usuario_existe(usuario):
    conn = get_db()
    cur = _run(
        conn,
        "SELECT 1 FROM planes WHERE LOWER(usuario) = ?",
        "SELECT 1 FROM planes WHERE LOWER(usuario) = %s",
        (usuario.strip().lower(),),
    )
    existe = cur.fetchone() is not None
    conn.close()
    return existe


def registrar_plan(plan_token, nombre, usuario, password_hash, fecha_evento, cantidad_cuotas):
    """Guarda la cabecera del plan con las credenciales del cliente."""
    conn = get_db()
    fecha = datetime.utcnow().isoformat()
    _run(
        conn,
        """INSERT INTO planes (plan_token, nombre, usuario, password_hash,
                               fecha_evento, cantidad_cuotas, fecha_creacion)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        """INSERT INTO planes (plan_token, nombre, usuario, password_hash,
                               fecha_evento, cantidad_cuotas, fecha_creacion)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (plan_token, nombre, (usuario or "").strip() or None, password_hash,
         fecha_evento, cantidad_cuotas, fecha),
    )
    conn.commit()
    conn.close()


def obtener_plan_por_usuario(usuario):
    conn = get_db()
    cur = _run(
        conn,
        "SELECT * FROM planes WHERE LOWER(usuario) = ?",
        "SELECT * FROM planes WHERE LOWER(usuario) = %s",
        (usuario.strip().lower(),),
    )
    row = cur.fetchone()
    conn.close()
    return row


def obtener_cabecera_plan(plan_token):
    conn = get_db()
    cur = _run(
        conn,
        "SELECT * FROM planes WHERE plan_token = ?",
        "SELECT * FROM planes WHERE plan_token = %s",
        (plan_token,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def listar_planes():
    """Cabeceras de todos los planes, más recientes primero."""
    conn = get_db()
    cur = _run(
        conn,
        "SELECT * FROM planes ORDER BY fecha_creacion DESC",
        "SELECT * FROM planes ORDER BY fecha_creacion DESC",
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def actualizar_cuota(cliente_id, nombre, concepto, monto, fecha_vencimiento, estado, notas):
    """Edición manual de una cuota desde el panel."""
    conn = get_db()
    fecha_pago = datetime.utcnow().isoformat() if estado == "pagado" else None
    if estado == "pagado":
        _run(
            conn,
            """UPDATE clientes SET nombre = ?, concepto = ?, monto_esperado = ?,
                   fecha_vencimiento = ?, estado = ?, notas = ?,
                   fecha_pago = COALESCE(fecha_pago, ?) WHERE id = ?""",
            """UPDATE clientes SET nombre = %s, concepto = %s, monto_esperado = %s,
                   fecha_vencimiento = %s, estado = %s, notas = %s,
                   fecha_pago = COALESCE(fecha_pago, %s) WHERE id = %s""",
            (nombre, concepto, monto, fecha_vencimiento, estado, notas, fecha_pago, cliente_id),
        )
    else:
        _run(
            conn,
            """UPDATE clientes SET nombre = ?, concepto = ?, monto_esperado = ?,
                   fecha_vencimiento = ?, estado = ?, notas = ?,
                   fecha_pago = NULL, mp_payment_id = NULL WHERE id = ?""",
            """UPDATE clientes SET nombre = %s, concepto = %s, monto_esperado = %s,
                   fecha_vencimiento = %s, estado = %s, notas = %s,
                   fecha_pago = NULL, mp_payment_id = NULL WHERE id = %s""",
            (nombre, concepto, monto, fecha_vencimiento, estado, notas, cliente_id),
        )
    conn.commit()
    conn.close()


def eliminar_cuota(cliente_id):
    conn = get_db()
    _run(conn, "DELETE FROM clientes WHERE id = ?", "DELETE FROM clientes WHERE id = %s", (cliente_id,))
    conn.commit()
    conn.close()


def eliminar_plan(plan_token):
    """Borra el plan entero: sus cuotas y su cabecera/credenciales."""
    conn = get_db()
    _run(conn, "DELETE FROM clientes WHERE plan_token = ?",
         "DELETE FROM clientes WHERE plan_token = %s", (plan_token,))
    _run(conn, "DELETE FROM planes WHERE plan_token = ?",
         "DELETE FROM planes WHERE plan_token = %s", (plan_token,))
    conn.commit()
    conn.close()


def crear_plan(nombre, telefono, evento, cuotas, notas=""):
    """Crea un plan de cuotas: una fila en `clientes` por cada cuota
    (seña incluida), todas con el mismo plan_token y cada una con su propio
    monto único (para poder matchear la transferencia), concepto y
    fecha_vencimiento. `cuotas` es la lista que devuelve
    planes.calcular_plan(): [{concepto, monto, fecha_vencimiento}, ...].
    Devuelve (plan_token, [cliente_id, ...])."""
    plan_token = uuid.uuid4().hex
    fecha_creacion = datetime.utcnow().isoformat()
    conn = get_db()
    ids = []
    try:
        for cuota in cuotas:
            monto = generar_monto_unico(conn, cuota["monto"])
            fecha_venc = cuota["fecha_vencimiento"]
            fecha_venc_str = fecha_venc.isoformat() if hasattr(fecha_venc, "isoformat") else str(fecha_venc)
            if USANDO_POSTGRES:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO clientes
                        (nombre, evento, telefono, monto_esperado, estado, fecha_creacion,
                         notas, fecha_vencimiento, concepto, plan_token)
                    VALUES (%s, %s, %s, %s, 'pendiente', %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (nombre, evento, telefono, monto, fecha_creacion, notas,
                     fecha_venc_str, cuota["concepto"], plan_token),
                )
                cliente_id = cur.fetchone()["id"]
            else:
                cur = conn.execute(
                    """
                    INSERT INTO clientes
                        (nombre, evento, telefono, monto_esperado, estado, fecha_creacion,
                         notas, fecha_vencimiento, concepto, plan_token)
                    VALUES (?, ?, ?, ?, 'pendiente', ?, ?, ?, ?, ?)
                    """,
                    (nombre, evento, telefono, monto, fecha_creacion, notas,
                     fecha_venc_str, cuota["concepto"], plan_token),
                )
                cliente_id = cur.lastrowid
            ids.append(cliente_id)
        conn.commit()
    finally:
        conn.close()
    return plan_token, ids


def obtener_plan(plan_token):
    """Devuelve todas las cuotas (filas de `clientes`) que pertenecen a un
    plan, ordenadas por fecha de vencimiento."""
    conn = get_db()
    cur = _run(
        conn,
        "SELECT * FROM clientes WHERE plan_token = ? ORDER BY fecha_vencimiento ASC, id ASC",
        "SELECT * FROM clientes WHERE plan_token = %s ORDER BY fecha_vencimiento ASC, id ASC",
        (plan_token,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


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
