import decimal
import os
import random
import sqlite3
import uuid
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USANDO_POSTGRES = bool(DATABASE_URL)

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "pagos.db"))

# --------------------------------------------------------------------------
# Guarda de seguridad: en producción NUNCA caer a SQLite
# --------------------------------------------------------------------------
# Sin esta guarda, si DATABASE_URL desaparece (base vencida, borrada o mal
# pegada) la app arranca igual contra SQLite sobre el disco efímero de Render:
# se ve todo normal, pero los datos se borran solos en el próximo deploy y sin
# ningún error visible. Preferimos que el deploy falle ruidosamente.
EN_PRODUCCION = bool(
    os.environ.get("RENDER")
    or os.environ.get("RENDER_SERVICE_ID")
    or os.environ.get("RENDER_EXTERNAL_URL")
)
PERMITIR_SQLITE = os.environ.get("PERMITIR_SQLITE") == "1"

if EN_PRODUCCION and not USANDO_POSTGRES and not PERMITIR_SQLITE:
    raise RuntimeError(
        "FALTA DATABASE_URL. La app está corriendo en Render sin conexión a "
        "Postgres, así que caería en SQLite sobre disco efímero y perdería "
        "todos los planes y pagos en el próximo deploy. Configurá DATABASE_URL "
        "en Environment. (Si de verdad querés una base descartable, poné "
        "PERMITIR_SQLITE=1.)"
    )

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
    "presupuesto_base": "500000",           # contado: saldo en 1 pago
    "presupuesto_financiado": "550000",     # tramo 1: de 2 cuotas hasta el tramo largo
    "presupuesto_financiado_largo": "600000",  # tramo 2: desde cuotas_tramo_largo
    "cuotas_tramo_largo": "6",              # cuota a partir de la cual rige el 2do precio
    "sena": "250000",                       # fija, nunca se divide
    "cuota_minima": "50000",                # no se ofrecen cuotas por debajo de esto
    "max_cuotas": "8",                      # tope de cuotas del saldo
    "mp_checkout_activo": "0",              # 1 = ofrecer pago con Mercado Pago
    "recargo_mp": "1.86",                   # % estimado para el 1er cobro. El simulador
                                            # de Mercado Pago muestra 1,53% a 35 días,
                                            # pero ese número es SIN IVA: el cargo real
                                            # es 1,53 x 1,21 = 1,8513%. Acá siempre se
                                            # guarda la tasa efectiva, con IVA incluido.
    "recargo_auto": "1",                    # 1 = usar la comisión real ya medida
    "recargo_observado": "0",               # % medido en el último pago (lo escribe el webhook)
    "recargo_margen": "0.3",                # % extra de colchón sobre lo medido
}

# Claves que llevan decimales (el resto se redondea a entero).
CONFIG_DECIMAL = {"recargo_mp", "recargo_observado", "recargo_margen"}


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
    return {
        k: (round(float(v), 4) if k in CONFIG_DECIMAL else int(float(v)))
        for k, v in conf.items()
    }


def guardar_config(nuevos):
    """Guarda (upsert) las claves de configuración recibidas."""
    conn = get_db()
    for clave, valor in nuevos.items():
        if clave not in CONFIG_DEFECTO:
            continue
        texto = str(round(float(valor), 4)) if clave in CONFIG_DECIMAL else str(int(valor))
        _run(
            conn,
            "INSERT INTO config (clave, valor) VALUES (?, ?) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
            "INSERT INTO config (clave, valor) VALUES (%s, %s) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
            (clave, texto),
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


def usuario_existe(usuario, excepto_token=None):
    """¿Ya hay otro plan con ese usuario? `excepto_token` deja fuera al propio
    plan, para poder guardarle de nuevo el mismo usuario sin falso choque."""
    conn = get_db()
    if excepto_token:
        cur = _run(
            conn,
            "SELECT 1 FROM planes WHERE LOWER(usuario) = ? AND plan_token <> ?",
            "SELECT 1 FROM planes WHERE LOWER(usuario) = %s AND plan_token <> %s",
            (usuario.strip().lower(), excepto_token),
        )
    else:
        cur = _run(
            conn,
            "SELECT 1 FROM planes WHERE LOWER(usuario) = ?",
            "SELECT 1 FROM planes WHERE LOWER(usuario) = %s",
            (usuario.strip().lower(),),
        )
    existe = cur.fetchone() is not None
    conn.close()
    return existe


def actualizar_credenciales_plan(plan_token, usuario, password_hash):
    """Asigna o cambia el usuario y la clave de un plan ya creado.

    Si password_hash es None, se conserva la clave que ya tenía (sirve para
    cambiar solo el nombre de usuario)."""
    conn = get_db()
    if password_hash is None:
        _run(
            conn,
            "UPDATE planes SET usuario = ? WHERE plan_token = ?",
            "UPDATE planes SET usuario = %s WHERE plan_token = %s",
            (usuario, plan_token),
        )
    else:
        _run(
            conn,
            "UPDATE planes SET usuario = ?, password_hash = ? WHERE plan_token = ?",
            "UPDATE planes SET usuario = %s, password_hash = %s WHERE plan_token = %s",
            (usuario, password_hash, plan_token),
        )
    conn.commit()
    conn.close()


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


# --------------------------------------------------------------------------
# Backup y restauración (exportar/importar toda la base como JSON)
# --------------------------------------------------------------------------

# Columnas de negocio de cada tabla. El log de webhooks queda afuera a
# propósito: es diagnóstico, no hace falta para reconstruir el estado.
TABLAS_BACKUP = {
    "planes": ["plan_token", "nombre", "usuario", "password_hash",
               "fecha_evento", "cantidad_cuotas", "fecha_creacion"],
    "clientes": ["id", "nombre", "evento", "telefono", "monto_esperado", "estado",
                 "mp_payment_id", "fecha_pago", "fecha_creacion", "notas",
                 "fecha_vencimiento", "concepto", "plan_token"],
    "config": ["clave", "valor"],
}

VERSION_BACKUP = 1


def _valor_json(v):
    """Convierte tipos de la base a algo serializable a JSON."""
    if isinstance(v, decimal.Decimal):
        return float(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _fila_a_dict(row, columnas):
    d = dict(row)
    return {c: _valor_json(d.get(c)) for c in columnas}


def exportar_todo():
    """Snapshot completo de los datos de negocio, listo para guardar como JSON
    o para volver a cargar con importar_todo()."""
    conn = get_db()
    datos = {
        "version": VERSION_BACKUP,
        "exportado": datetime.utcnow().isoformat(),
        "backend": "postgres" if USANDO_POSTGRES else "sqlite",
    }
    try:
        for tabla, columnas in TABLAS_BACKUP.items():
            sql = "SELECT * FROM {} ".format(tabla)
            cur = _run(conn, sql, sql)
            datos[tabla] = [_fila_a_dict(r, columnas) for r in cur.fetchall()]
    finally:
        conn.close()
    return datos


def contar_filas():
    """Cuántas filas hay hoy en cada tabla de negocio."""
    conn = get_db()
    conteo = {}
    try:
        for tabla in TABLAS_BACKUP:
            sql = "SELECT COUNT(*) AS n FROM {}".format(tabla)
            cur = _run(conn, sql, sql)
            fila = cur.fetchone()
            conteo[tabla] = int(dict(fila)["n"] if not isinstance(fila, tuple) else fila[0])
    finally:
        conn.close()
    return conteo


def _resetear_secuencia_clientes(conn):
    """Después de insertar ids explícitos, la secuencia de Postgres queda
    atrasada y el próximo INSERT chocaría con un id ya usado."""
    if not USANDO_POSTGRES:
        return
    conn.cursor().execute(
        "SELECT setval(pg_get_serial_sequence('clientes', 'id'), "
        "COALESCE((SELECT MAX(id) FROM clientes), 1))"
    )


def importar_todo(datos, forzar=False):
    """Carga un backup hecho con exportar_todo().

    Por seguridad solo importa sobre tablas vacías. Con forzar=True borra lo
    que haya y deja exactamente el contenido del backup.
    Devuelve un dict con la cantidad de filas insertadas por tabla."""
    if not isinstance(datos, dict) or "clientes" not in datos or "planes" not in datos:
        raise ValueError("El archivo no parece un backup de esta plataforma.")
    if int(datos.get("version", 0)) > VERSION_BACKUP:
        raise ValueError(
            "El backup es de una versión más nueva que esta app (v{}).".format(
                datos.get("version"))
        )

    if not forzar:
        actuales = contar_filas()
        con_datos = [t for t, n in actuales.items() if t != "config" and n > 0]
        if con_datos:
            raise ValueError(
                "La base ya tiene datos ({}). Marcá 'reemplazar' si querés "
                "pisarlos.".format(", ".join(
                    "{}: {}".format(t, actuales[t]) for t in con_datos))
            )

    conn = get_db()
    insertadas = {}
    try:
        if forzar:
            for tabla in ("clientes", "planes", "config"):
                sql = "DELETE FROM {}".format(tabla)
                _run(conn, sql, sql)

        for tabla, columnas in TABLAS_BACKUP.items():
            filas = datos.get(tabla) or []
            marcas = ", ".join([_ph()] * len(columnas))
            sql = "INSERT INTO {} ({}) VALUES ({})".format(
                tabla, ", ".join(columnas), marcas)
            n = 0
            for fila in filas:
                valores = tuple(fila.get(c) for c in columnas)
                _run(conn, sql, sql, valores)
                n += 1
            insertadas[tabla] = n

        _resetear_secuencia_clientes(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return insertadas


def anotar_en_cuota(cliente_id, texto):
    """Agrega una línea a las notas de una cuota, sin pisar lo que ya había."""
    conn = get_db()
    cur = _run(
        conn,
        "SELECT notas FROM clientes WHERE id = ?",
        "SELECT notas FROM clientes WHERE id = %s",
        (cliente_id,),
    )
    fila = cur.fetchone()
    previo = ""
    if fila is not None:
        previo = (dict(fila).get("notas") if not isinstance(fila, tuple) else fila[0]) or ""
    nuevo = (previo + "\n" + texto).strip() if previo else texto
    _run(
        conn,
        "UPDATE clientes SET notas = ? WHERE id = ?",
        "UPDATE clientes SET notas = %s WHERE id = %s",
        (nuevo, cliente_id),
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
