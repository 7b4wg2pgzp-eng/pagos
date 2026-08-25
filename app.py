import os
import json
import math
import time
import functools
from datetime import datetime, date

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

import db
import mp
import planes

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambiar-esta-clave-en-produccion")

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")
ALIAS_COBRO = os.environ.get("ALIAS_COBRO", "tu.alias.mp")
CVU_COBRO = os.environ.get("CVU_COBRO", "")

db.init_db()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def login_requerido(f):
    """Protege el panel de gestión (solo Nico)."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logueado"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)

    return wrapper


@app.template_filter("pesos")
def filtro_pesos(valor, decimales=2):
    """Formato argentino: 550002.12 -> 550.002,12"""
    try:
        n = float(valor)
    except (TypeError, ValueError):
        return valor
    s = f"{n:,.{decimales}f}"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _parsear_fecha(valor):
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _monto(valor, defecto=0.0):
    """Acepta tanto '1.234,56' (formato argentino) como '1234.56' (plano).

    Reglas:
      - Si hay coma, la coma es el separador decimal y los puntos son de miles.
      - Si solo hay puntos, se toman como separador de miles unicamente cuando
        el ultimo grupo tiene exactamente 3 digitos ('500.000'); si no, el
        punto es decimal ('123456.78').
    """
    s = str(valor).strip().replace(" ", "").replace("$", "")
    if not s:
        return defecto
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif "." in s:
        if len(s.rsplit(".", 1)[1]) == 3:
            s = s.replace(".", "")
    try:
        return round(float(s), 2)
    except (TypeError, ValueError):
        return defecto


def _validar_credenciales(usuario, clave, excepto_token=None, clave_obligatoria=True):
    """Reglas del usuario y la clave del cliente. Devuelve un mensaje de error,
    o None si está todo bien. Lo usan tanto la calculadora pública como el
    panel, así los dos caminos piden exactamente lo mismo."""
    if len(usuario) < 4:
        return "El usuario tiene que tener al menos 4 caracteres"
    if not usuario.replace(".", "").replace("_", "").replace("-", "").isalnum():
        return "El usuario solo puede tener letras, números, punto, guion y guion bajo"
    if clave or clave_obligatoria:
        if len(clave) < 6:
            return "La contraseña tiene que tener al menos 6 caracteres"
    if db.usuario_existe(usuario, excepto_token=excepto_token):
        return "Ese usuario ya está en uso, probá con otro"
    return None


def _cuota_publica(row):
    """Recorta una fila de `clientes` a los campos que el cliente puede ver."""
    return {
        "id": row["id"],
        "concepto": row["concepto"],
        "monto": float(row["monto_esperado"]),
        "fecha_vencimiento": row["fecha_vencimiento"],
        "estado": row["estado"],
    }


def tasa_comision(conf=None):
    """El porcentaje de comisión a usar para el cálculo, en %.

    Prioriza lo que Mercado Pago cobró de verdad en el último pago (lo mide el
    webhook) por sobre el valor estimado a mano: las tablas publicadas varían
    según el plazo de acreditación de cada cuenta y quedan viejas. Al medido se
    le suma un margen, porque la comisión puede moverse hacia arriba entre un
    pago y el siguiente y ahí la diferencia la pagarías vos."""
    conf = conf or db.leer_config()
    manual = float(conf.get("recargo_mp", 0) or 0)
    if not conf.get("recargo_auto"):
        return manual
    observado = float(conf.get("recargo_observado", 0) or 0)
    if observado <= 0:
        return manual  # todavía no hubo ningún pago del cual aprender
    return observado + float(conf.get("recargo_margen", 0) or 0)


def monto_con_recargo(monto, conf=None):
    """Lo que hay que cobrar para que, después de la comisión de Mercado Pago,
    quede neto el monto de la cuota. Se divide, no se suma: la comisión se
    calcula sobre el total cobrado, así que sumarle el porcentaje dejaría
    corto por la diferencia."""
    conf = conf or db.leer_config()
    r = tasa_comision(conf) / 100.0
    if r <= 0 or r >= 1:
        return round(float(monto), 2)
    # La base se trunca al peso antes de calcular, y el resultado se redondea
    # al peso de arriba. Truncar es lo que hace que la calculadora y el panel
    # muestren el MISMO número: la calculadora parte del monto redondo y el
    # panel del guardado, que arrastra los centavos únicos del matcheo por
    # transferencia. Sin esto los dos lados difieren en un peso, y un cliente
    # que compara pregunta. El centavo perdido lo cubre de sobra el margen.
    base = math.floor(float(monto))
    return float(math.ceil(base / (1 - r)))


def es_sena(concepto):
    return (concepto or "").strip().lower().startswith("seña")


def metodo_de_cobro(concepto, conf, mp_activo):
    """Con qué medio se cobra esta cuota.

    La seña se coordina en persona al firmar el contrato y se marca a mano
    desde el panel: no lleva botón de pago ni recargo. Es además la que da
    liquidez, porque entra meses antes del evento y sin comisión."""
    if conf.get("sena_manual") and es_sena(concepto):
        return "manual"
    if not mp_activo:
        return "transferencia"
    return "checkout"


def _respuesta_plan(plan_token, incluir_token=True):
    plan = db.obtener_plan(plan_token)
    conf = db.leer_config()
    mp_activo = bool(conf.get("mp_checkout_activo")) and bool(mp.MP_ACCESS_TOKEN)
    cuotas = []
    for c in plan:
        pub = _cuota_publica(c)
        pub["metodo"] = metodo_de_cobro(c["concepto"], conf, mp_activo)
        # El recargo solo existe donde hay comisión: la transferencia no lleva.
        if pub["metodo"] == "checkout" and c["estado"] != "pagado":
            pub["monto_mp"] = monto_con_recargo(c["monto_esperado"], conf)
        cuotas.append(pub)
    datos = {
        "alias": ALIAS_COBRO,
        "cvu": CVU_COBRO,
        "mp_activo": mp_activo,
        "cuotas": cuotas,
    }
    if incluir_token:
        datos["plan_token"] = plan_token
    cab = db.obtener_cabecera_plan(plan_token)
    if cab:
        datos["nombre"] = cab["nombre"]
        datos["usuario"] = cab["usuario"]
    return datos


# --------------------------------------------------------------------------
# Panel de gestión (privado)
# --------------------------------------------------------------------------

@app.route("/panel/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        usuario = request.form.get("usuario", "")
        clave = request.form.get("clave", "")
        if usuario == ADMIN_USER and clave and clave == ADMIN_PASS:
            session["logueado"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Usuario o clave incorrectos")
    return render_template("login.html")


@app.route("/panel/logout")
def logout():
    session.pop("logueado", None)
    return redirect(url_for("login"))


@app.route("/panel")
@login_requerido
def dashboard():
    """Panel de gestión: planes agrupados + cobros sueltos + configuración."""
    clientes = db.listar_clientes()
    cabeceras = {p["plan_token"]: p for p in db.listar_planes()}

    planes_agrupados = {}
    sueltos = []
    for c in clientes:
        token = c["plan_token"]
        if token:
            planes_agrupados.setdefault(token, []).append(c)
        else:
            sueltos.append(c)

    # Ordenar cada plan por vencimiento y armar el resumen
    lista_planes = []
    for token, cuotas in planes_agrupados.items():
        cuotas.sort(key=lambda x: (x["fecha_vencimiento"] or "", x["id"]))
        pagadas = sum(1 for c in cuotas if c["estado"] == "pagado")
        total = sum(float(c["monto_esperado"]) for c in cuotas)
        cobrado = sum(float(c["monto_esperado"]) for c in cuotas if c["estado"] == "pagado")
        cab = cabeceras.get(token)
        lista_planes.append(
            {
                "token": token,
                "cabecera": cab,
                "nombre": (cab["nombre"] if cab else None) or cuotas[0]["nombre"],
                "usuario": cab["usuario"] if cab else None,
                "fecha_evento": (cab["fecha_evento"] if cab else None) or cuotas[0]["evento"],
                "cuotas": cuotas,
                "pagadas": pagadas,
                "total_cuotas": len(cuotas),
                "monto_total": total,
                "monto_cobrado": cobrado,
                "completo": pagadas == len(cuotas),
            }
        )
    lista_planes.sort(key=lambda p: (p["completo"], p["fecha_evento"] or ""))

    return render_template(
        "dashboard.html",
        planes=lista_planes,
        sueltos=sueltos,
        alias=ALIAS_COBRO,
        cvu=CVU_COBRO,
        config=db.leer_config(),
        hoy=date.today().isoformat(),
    )


@app.route("/panel/config", methods=["POST"])
@login_requerido
def guardar_configuracion():
    nuevos = {}
    for clave in ("presupuesto_base", "presupuesto_financiado",
                  "presupuesto_financiado_largo", "cuotas_tramo_largo",
                  "sena", "cuota_minima", "max_cuotas"):
        valor = _monto(request.form.get(clave, ""), None)
        if valor is None or valor <= 0:
            flash(f"El valor de {clave.replace('_', ' ')} no es válido")
            return redirect(url_for("dashboard"))
        nuevos[clave] = valor
    if nuevos["sena"] >= nuevos["presupuesto_base"]:
        flash("La seña tiene que ser menor al precio de contado")
        return redirect(url_for("dashboard"))
    if nuevos["presupuesto_financiado"] < nuevos["presupuesto_base"]:
        flash("El precio financiado no puede ser menor al de contado")
        return redirect(url_for("dashboard"))
    if nuevos["presupuesto_financiado_largo"] < nuevos["presupuesto_financiado"]:
        flash("El precio del tramo largo no puede ser menor al financiado")
        return redirect(url_for("dashboard"))
    if nuevos["cuotas_tramo_largo"] < 2:
        flash("El tramo largo tiene que arrancar en la cuota 2 o más")
        return redirect(url_for("dashboard"))
    if nuevos["max_cuotas"] > 24:
        flash("El tope de cuotas no puede ser mayor a 24")
        return redirect(url_for("dashboard"))

    # Pago con saldo de Mercado Pago (opcional, se activa desde el panel).
    nuevos["mp_checkout_activo"] = 1 if request.form.get("mp_checkout_activo") == "si" else 0
    nuevos["recargo_auto"] = 1 if request.form.get("recargo_auto") == "si" else 0
    nuevos["sena_manual"] = 1 if request.form.get("sena_manual") == "si" else 0
    recargo = _monto(request.form.get("recargo_mp", ""), None)
    if recargo is None or recargo < 0 or recargo >= 30:
        flash("La comisión estimada tiene que ser un porcentaje entre 0 y 30")
        return redirect(url_for("dashboard"))
    nuevos["recargo_mp"] = recargo
    margen = _monto(request.form.get("recargo_margen", ""), None)
    if margen is None or margen < 0 or margen >= 10:
        flash("El margen de seguridad tiene que ser un porcentaje entre 0 y 10")
        return redirect(url_for("dashboard"))
    nuevos["recargo_margen"] = margen
    if nuevos["mp_checkout_activo"] and not mp.MP_ACCESS_TOKEN:
        flash("No se puede activar el pago con Mercado Pago: falta MP_ACCESS_TOKEN")
        return redirect(url_for("dashboard"))

    db.guardar_config(nuevos)
    flash("Montos actualizados. Los planes nuevos ya usan estos valores.")
    return redirect(url_for("dashboard"))


@app.route("/panel/planes/nuevo", methods=["POST"])
@login_requerido
def crear_plan_manual():
    """Crea un plan a mano desde el panel, por si falla el flujo del cliente."""
    nombre = request.form.get("nombre", "").strip() or "Sin nombre"
    fecha_evento = _parsear_fecha(request.form.get("fecha_evento", ""))
    try:
        cantidad = int(request.form.get("cuotas_saldo", request.form.get("cantidad_cuotas", "0")))
    except ValueError:
        cantidad = 0

    if not fecha_evento or cantidad < 1:
        flash("Completá la fecha del evento y una cantidad de cuotas válida")
        return redirect(url_for("dashboard"))

    # Usuario y clave son opcionales acá: si no los ponés, el plan queda sin
    # acceso para el cliente y se los podés cargar después desde el panel.
    usuario = request.form.get("usuario", "").strip()
    clave = request.form.get("clave", "")
    if usuario or clave:
        error = _validar_credenciales(usuario, clave)
        if error:
            flash(error)
            return redirect(url_for("dashboard"))

    cuotas = planes.calcular_plan(fecha_evento, cantidad)
    plan_token, _ = db.crear_plan(
        nombre=nombre,
        telefono="",
        evento=fecha_evento.isoformat(),
        cuotas=cuotas,
        notas="Creado a mano desde el panel",
    )
    db.registrar_plan(
        plan_token,
        nombre,
        usuario or None,
        generate_password_hash(clave) if usuario else None,
        fecha_evento.isoformat(),
        cantidad,
    )
    if usuario:
        flash(f"Plan creado para {nombre} ({len(cuotas)} cuotas). Ya puede entrar con «{usuario}».")
    else:
        flash(f"Plan creado para {nombre} ({len(cuotas)} cuotas), sin acceso de cliente.")
    return redirect(url_for("dashboard"))


@app.route("/panel/planes/<token>/credenciales", methods=["POST"])
@login_requerido
def credenciales_plan(token):
    """Asigna o cambia el usuario y la clave de un plan ya existente — para los
    creados a mano, o cuando el cliente se olvidó la contraseña."""
    cab = db.obtener_cabecera_plan(token)
    if not cab:
        flash("Plan no encontrado")
        return redirect(url_for("dashboard"))

    usuario = request.form.get("usuario", "").strip()
    clave = request.form.get("clave", "")

    # Si el plan ya tenía clave, dejar el campo vacío significa "no la cambies".
    ya_tenia_clave = bool(cab["password_hash"])
    error = _validar_credenciales(
        usuario, clave, excepto_token=token, clave_obligatoria=not ya_tenia_clave
    )
    if error:
        flash(error)
        return redirect(url_for("dashboard"))

    db.actualizar_credenciales_plan(
        token, usuario, generate_password_hash(clave) if clave else None
    )
    if clave:
        flash(f"Acceso guardado: usuario «{usuario}» con clave nueva.")
    else:
        flash(f"Usuario cambiado a «{usuario}». La clave sigue siendo la misma.")
    return redirect(url_for("dashboard"))


@app.route("/panel/planes/<token>/eliminar", methods=["POST"])
@login_requerido
def eliminar_plan_admin(token):
    db.eliminar_plan(token)
    flash("Plan eliminado por completo")
    return redirect(url_for("dashboard"))


@app.route("/panel/clientes/nuevo", methods=["GET", "POST"])
@login_requerido
def nuevo_cliente():
    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        evento = request.form.get("evento", "").strip()
        telefono = request.form.get("telefono", "").strip()
        notas = request.form.get("notas", "").strip()
        monto_base = _monto(request.form.get("monto_base", "0"))

        if not nombre or monto_base <= 0:
            flash("Completá al menos el nombre y un monto mayor a 0")
            return redirect(url_for("nuevo_cliente"))

        cliente_id, monto = db.crear_cliente(nombre, evento, telefono, monto_base, notas)
        flash(f"Cobro creado. Monto exacto a transferir: ${monto:,.2f}")
        return redirect(url_for("detalle_cliente", cliente_id=cliente_id))

    return render_template("nuevo.html")


@app.route("/panel/clientes/<int:cliente_id>", methods=["GET", "POST"])
@login_requerido
def detalle_cliente(cliente_id):
    cliente = db.obtener_cliente(cliente_id)
    if not cliente:
        flash("Cobro no encontrado")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        monto = _monto(request.form.get("monto_esperado", ""), None)
        if monto is None or monto <= 0:
            flash("El monto no es válido")
            return redirect(url_for("detalle_cliente", cliente_id=cliente_id))
        db.actualizar_cuota(
            cliente_id,
            nombre=request.form.get("nombre", "").strip() or cliente["nombre"],
            concepto=request.form.get("concepto", "").strip(),
            monto=monto,
            fecha_vencimiento=request.form.get("fecha_vencimiento", "").strip(),
            estado=("pagado" if request.form.get("estado") == "pagado" else "pendiente"),
            notas=request.form.get("notas", "").strip(),
        )
        flash("Cambios guardados")
        return redirect(url_for("detalle_cliente", cliente_id=cliente_id))

    return render_template("detalle.html", c=cliente, alias=ALIAS_COBRO, cvu=CVU_COBRO)


@app.route("/panel/clientes/<int:cliente_id>/marcar-pagado", methods=["POST"])
@login_requerido
def marcar_pagado_manual(cliente_id):
    db.marcar_pagado(cliente_id, mp_payment_id="manual")
    flash("Marcado como pagado manualmente")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/panel/clientes/<int:cliente_id>/eliminar", methods=["POST"])
@login_requerido
def eliminar_cuota_admin(cliente_id):
    db.eliminar_cuota(cliente_id)
    flash("Cuota eliminada")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/panel/sincronizar", methods=["POST"])
@login_requerido
def sincronizar():
    """Fallback: busca pagos recientes aprobados en Mercado Pago y los
    matchea contra cuotas pendientes, por si algún webhook no llegó."""
    try:
        pagos = mp.search_recent_payments(limit=30)
    except Exception as e:
        flash(f"No se pudo consultar Mercado Pago: {e}")
        return redirect(url_for("dashboard"))

    encontrados = 0
    for pago in pagos:
        monto = pago.get("transaction_amount")
        payment_id = pago.get("id")
        if monto is None:
            continue
        cliente = db.buscar_pendiente_por_monto(monto)
        if cliente:
            db.marcar_pagado(cliente["id"], mp_payment_id=str(payment_id))
            encontrados += 1

    flash(f"Sincronización manual: {encontrados} pago(s) matcheado(s)")
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------------------
# Backup y restauración
# --------------------------------------------------------------------------

@app.route("/panel/backup")
@login_requerido
def descargar_backup():
    """Baja todos los planes, cuotas y config en un JSON. Sirve tanto de copia
    de seguridad como para mudar la base a otro proveedor."""
    datos = db.exportar_todo()
    nombre = "backup-cuotas-{}.json".format(datetime.utcnow().strftime("%Y%m%d-%H%M"))
    cuerpo = json.dumps(datos, ensure_ascii=False, indent=2)
    return app.response_class(
        cuerpo,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@app.route("/panel/restaurar", methods=["POST"])
@login_requerido
def restaurar_backup():
    """Carga un archivo generado por /panel/backup."""
    archivo = request.files.get("backup")
    if not archivo or not archivo.filename:
        flash("Elegí un archivo de backup primero")
        return redirect(url_for("dashboard"))

    try:
        datos = json.loads(archivo.read().decode("utf-8"))
    except Exception:
        flash("El archivo no es un JSON válido")
        return redirect(url_for("dashboard"))

    forzar = request.form.get("reemplazar") == "si"
    try:
        insertadas = db.importar_todo(datos, forzar=forzar)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("dashboard"))
    except Exception as e:
        flash(f"No se pudo restaurar: {e}")
        return redirect(url_for("dashboard"))

    flash("Backup restaurado: {}.".format(
        ", ".join(f"{n} en {t}" for t, n in insertadas.items())))
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------------------
# Calculadora pública + área del cliente
# --------------------------------------------------------------------------

@app.route("/")
def calculadora_cuotas():
    """Página pública de la calculadora de cuotas: es la raíz del subdominio."""
    return render_template("calculadora.html", alias=ALIAS_COBRO, cvu=CVU_COBRO)


@app.route("/cuotas")
def calculadora_cuotas_legacy():
    """La calculadora vivía acá antes. Se mantiene redirigiendo para no
    romper links ya compartidos."""
    return redirect(url_for("calculadora_cuotas"), code=301)


@app.route("/api/planes/opciones", methods=["POST"])
def api_opciones_plan():
    """Simulación (no escribe nada): para una fecha de evento devuelve el
    precio, la seña, la fecha límite de pago y todas las alternativas de
    cuotas del saldo que entran en el tiempo disponible."""
    body = request.get_json(silent=True) or {}
    fecha_evento = _parsear_fecha(body.get("fecha_evento", ""))
    if not fecha_evento:
        return jsonify({"error": "Elegí la fecha de tu evento"}), 400
    if fecha_evento < date.today():
        return jsonify({"error": "Esa fecha ya pasó"}), 400

    conf = db.leer_config()
    validas = planes.cuotas_disponibles(fecha_evento)
    opciones = [planes.resumen_opcion(fecha_evento, n) for n in validas]

    # Si el cobro va por Mercado Pago, la calculadora tiene que mostrar el
    # mismo número que después se cobra. Si acá dijera $87.500 y en el panel
    # apareciera $88.358, el cliente ve que le cambiaron el precio.
    mp_activo = bool(conf.get("mp_checkout_activo")) and bool(mp.MP_ACCESS_TOKEN)
    # La seña puede ir por transferencia: en ese caso no lleva recargo, y
    # el total tiene que recalcularse sumando y no aplicándole el ajuste.
    sena_limpia = bool(conf.get("sena_manual"))

    def final(v):
        return monto_con_recargo(v, conf)

    def precio_vitrina(v):
        """Precio total a mostrar arriba de todo.

        Tiene que dar exactamente lo mismo que la suma de las cuotas de esa
        opción. Si la seña se cobra a mano no lleva recargo, así que el ajuste
        se aplica sólo al saldo: recargar el precio entero mostraría de más
        (la diferencia es el recargo sobre la seña) y el cliente vería dos
        números distintos en la misma pantalla.
        """
        if not mp_activo:
            return v
        if not sena_limpia:
            return final(v)
        sena = conf["sena"]
        saldo = v - sena
        if saldo <= 0:
            return v
        return round(sena + final(saldo), 2)

    if mp_activo:
        for op in opciones:
            for k in ("saldo", "monto_cuota", "ultima_cuota"):
                if op.get(k) is not None:
                    op[k] = final(op[k])
            if not sena_limpia and op.get("sena") is not None:
                op["sena"] = final(op["sena"])
            for c in op.get("cuotas", []):
                if c.get("monto") is None:
                    continue
                if sena_limpia and es_sena(c.get("concepto")):
                    continue
                c["monto"] = final(c["monto"])
            op["total"] = round(sum(c["monto"] for c in op.get("cuotas", [])), 2)

    return jsonify(
        {
            "precio_contado": precio_vitrina(conf["presupuesto_base"]),
            "precio_financiado": precio_vitrina(conf["presupuesto_financiado"]),
            "precio_financiado_largo": precio_vitrina(conf["presupuesto_financiado_largo"]),
            "sena": final(conf["sena"]) if (mp_activo and not conf.get("sena_manual")) else conf["sena"],
            "cuota_minima": conf["cuota_minima"],
            "limite_pago": planes.limite_pago(fecha_evento).isoformat(),
            "max_cuotas": max(validas),
            "hay_financiacion": len(validas) > 1,
            "mp_activo": mp_activo,
            "opciones": opciones,
        }
    )


@app.route("/api/planes", methods=["POST"])
def api_crear_plan():
    """Confirma el plan elegido y crea la cuenta del cliente. Cada cuota queda
    como un registro real, así el webhook de Mercado Pago la detecta igual que
    cualquier otro cobro."""
    body = request.get_json(silent=True) or {}
    nombre = (body.get("nombre") or "").strip()
    usuario = (body.get("usuario") or "").strip()
    clave = body.get("clave") or ""
    fecha_evento = _parsear_fecha(body.get("fecha_evento", ""))
    try:
        cuotas_saldo = int(body.get("cuotas_saldo", body.get("cantidad_cuotas")))
    except (TypeError, ValueError):
        return jsonify({"error": "Elegí en cuántas cuotas querés pagar"}), 400

    if not fecha_evento:
        return jsonify({"error": "Elegí la fecha de tu evento"}), 400
    if fecha_evento < date.today():
        return jsonify({"error": "Esa fecha ya pasó"}), 400
    error = _validar_credenciales(usuario, clave)
    if error:
        return jsonify({"error": error}), 409 if "ya está en uso" in error else 400

    validas = planes.cuotas_disponibles(fecha_evento)
    if cuotas_saldo not in validas:
        return jsonify(
            {"error": f"Para esa fecha el máximo es {max(validas)} cuota(s)"}
        ), 400

    # Los montos se calculan acá, en el servidor, a partir de la fecha y la
    # cantidad de cuotas — nunca se confía en un monto mandado por el cliente.
    cuotas_calc = planes.calcular_plan(fecha_evento, cuotas_saldo)

    # Red de seguridad: ninguna cuota del saldo puede quedar por debajo del
    # mínimo, ni vencer después de la fecha límite.
    conf = db.leer_config()
    limite = planes.limite_pago(fecha_evento)
    for c in cuotas_calc[1:]:
        if c["monto"] < conf["cuota_minima"]:
            return jsonify({"error": "Esa cantidad de cuotas da un monto por debajo del mínimo"}), 400
        if c["fecha_vencimiento"] > max(limite, date.today()):
            return jsonify({"error": "Esa cantidad de cuotas no entra antes de la fecha límite"}), 400
    plan_token, _ = db.crear_plan(
        nombre=nombre or usuario,
        telefono="",
        evento=fecha_evento.isoformat(),
        cuotas=cuotas_calc,
        notas="Creado desde la calculadora de cuotas",
    )
    db.registrar_plan(
        plan_token,
        nombre or usuario,
        usuario,
        generate_password_hash(clave),
        fecha_evento.isoformat(),
        cuotas_saldo,
    )
    session["plan_token"] = plan_token
    return jsonify(_respuesta_plan(plan_token))


@app.route("/api/planes/login", methods=["POST"])
def api_login_plan():
    """Login del cliente para volver a ver su plan desde cualquier dispositivo."""
    body = request.get_json(silent=True) or {}
    usuario = (body.get("usuario") or "").strip()
    clave = body.get("clave") or ""

    cab = db.obtener_plan_por_usuario(usuario) if usuario else None
    if not cab or not cab["password_hash"] or not check_password_hash(cab["password_hash"], clave):
        return jsonify({"error": "Usuario o contraseña incorrectos"}), 401

    session["plan_token"] = cab["plan_token"]
    return jsonify(_respuesta_plan(cab["plan_token"]))


@app.route("/api/planes/mio")
def api_mi_plan():
    """Devuelve el plan de la sesión actual del cliente, si está logueado."""
    token = session.get("plan_token")
    if not token:
        return jsonify({"error": "No hay sesión"}), 401
    if not db.obtener_plan(token):
        session.pop("plan_token", None)
        return jsonify({"error": "Plan no encontrado"}), 404
    return jsonify(_respuesta_plan(token))


@app.route("/api/planes/salir", methods=["POST"])
def api_salir_plan():
    session.pop("plan_token", None)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Pago con saldo de Mercado Pago (opcional)
# --------------------------------------------------------------------------

@app.route("/api/planes/pagar/<int:cliente_id>", methods=["POST"])
def api_pagar_cuota(cliente_id):
    """Crea la preferencia de Checkout para una cuota y devuelve el link."""
    conf = db.leer_config()
    if not conf.get("mp_checkout_activo"):
        return jsonify({"error": "El pago con Mercado Pago no está habilitado"}), 400
    if not mp.MP_ACCESS_TOKEN:
        return jsonify({"error": "Falta configurar el acceso a Mercado Pago"}), 500

    token = session.get("plan_token")
    if not token:
        return jsonify({"error": "No hay sesión"}), 401

    cuota = db.obtener_cliente(cliente_id)
    # La cuota tiene que existir y pertenecer al plan de la sesión: si no,
    # cualquiera podría generar links de pago de cuotas ajenas.
    if not cuota or cuota["plan_token"] != token:
        return jsonify({"error": "Cuota no encontrada"}), 404
    if cuota["estado"] == "pagado":
        return jsonify({"error": "Esa cuota ya está pagada"}), 409
    if metodo_de_cobro(cuota["concepto"], conf, True) != "checkout":
        return jsonify({"error": "La seña se coordina directamente con Nico"}), 400

    a_cobrar = monto_con_recargo(cuota["monto_esperado"], conf)
    raiz = request.url_root.rstrip("/")
    try:
        pref = mp.crear_preferencia(
            # Mercado Pago se come la barra del título: "Cuota 1/6" le llega
            # al cliente como "Cuota 16", que parece otra cuota.
            titulo="{} — {}".format(
                (cuota["concepto"] or "Cuota").replace("/", " de "),
                cuota["nombre"],
            ),
            monto=a_cobrar,
            referencia_externa=f"cuota:{cuota['id']}",
            url_vuelta=f"{raiz}/pago/vuelta",
            url_webhook=f"{raiz}/webhook/mercadopago",
        )
    except Exception as e:
        return jsonify({"error": f"No se pudo generar el pago: {e}"}), 502

    link = pref.get("init_point") or pref.get("sandbox_init_point")
    if not link:
        return jsonify({"error": "Mercado Pago no devolvió el link de pago"}), 502
    return jsonify({"init_point": link, "monto": a_cobrar})


@app.route("/api/planes/verificar", methods=["POST"])
def api_verificar_transferencia():
    """El cliente dice «ya transferí»: vamos a buscar el pago a Mercado Pago
    en vez de esperar al webhook.

    Existe porque el webhook puede demorar o no llegar, y sin esto el cliente
    se queda mirando una pantalla que no cambia. Está limitado en frecuencia:
    cada consulta es una llamada a la API de Mercado Pago."""
    token = session.get("plan_token")
    if not token:
        return jsonify({"error": "No hay sesión"}), 401
    if not mp.MP_ACCESS_TOKEN:
        return jsonify({"error": "No se puede verificar en este momento"}), 503

    ahora = time.time()
    ultima = session.get("ultima_verificacion", 0)
    if ahora - ultima < 15:
        # Sin error: devolvemos el plano como está, el cliente sigue esperando.
        return jsonify(_respuesta_plan(token, incluir_token=False))
    session["ultima_verificacion"] = ahora

    pendientes = [c for c in db.obtener_plan(token) if c["estado"] != "pagado"]
    if pendientes:
        try:
            pagos = mp.search_recent_payments(limit=30)
        except Exception:
            pagos = []
        # Matcheo por monto exacto, igual que el webhook, pero acotado a las
        # cuotas de este plan: nunca puede marcar la de otro cliente.
        montos = {round(float(c["monto_esperado"]), 2): c for c in pendientes}
        for pago in pagos:
            if pago.get("status") != "approved":
                continue
            try:
                monto = round(float(pago.get("transaction_amount")), 2)
            except (TypeError, ValueError):
                continue
            cuota = montos.pop(monto, None)
            if cuota is not None:
                db.marcar_pagado(cuota["id"], mp_payment_id=str(pago.get("id")))

    return jsonify(_respuesta_plan(token, incluir_token=False))


@app.route("/pago/vuelta")
def pago_vuelta():
    """Adonde vuelve el cliente desde Mercado Pago. El pago lo confirma el
    webhook, no esta pantalla — acá solo lo devolvemos a su plan."""
    estado = request.args.get("status") or request.args.get("collection_status") or ""
    return redirect(url_for("calculadora_cuotas", pago=estado or "vuelta"))


# --------------------------------------------------------------------------
# Webhook de Mercado Pago
# --------------------------------------------------------------------------

@app.route("/webhook/mercadopago", methods=["POST"])
def webhook_mercadopago():
    """Mercado Pago pega acá cada vez que se crea/actualiza un pago
    (incluye transferencias recibidas a tu cuenta, no solo checkout)."""
    json_body = request.get_json(silent=True) or {}
    data_id, topic = mp.extraer_data_id(request.args, json_body)

    resultado = "sin data_id"
    if data_id and topic == "payment":
        firma_ok = mp.validar_firma(request.headers, data_id)
        if firma_ok is False:
            resultado = "firma invalida"
            db.log_webhook(json.dumps({"args": dict(request.args), "body": json_body}), resultado)
            return jsonify({"status": "firma invalida"}), 401

        try:
            pago = mp.get_payment(data_id)
        except Exception as e:
            resultado = f"error consultando pago: {e}"
            db.log_webhook(json.dumps({"args": dict(request.args), "body": json_body}), resultado)
            return jsonify({"status": "error"}), 200

        if pago.get("status") == "approved":
            monto = pago.get("transaction_amount")
            cliente = None
            via = ""

            # 1) Checkout: la referencia dice exactamente qué cuota se pagó.
            #    Es exacto y no depende del monto, así que va primero.
            ref = str(pago.get("external_reference") or "")
            if ref.startswith("cuota:"):
                try:
                    cid = int(ref.split(":", 1)[1])
                except ValueError:
                    cid = None
                if cid:
                    candidato = db.obtener_cliente(cid)
                    if candidato is None:
                        resultado = f"referencia {ref} apunta a una cuota que no existe"
                    elif candidato["estado"] == "pagado":
                        resultado = f"cuota {cid} ya estaba pagada, no se toca"
                    else:
                        cliente = candidato
                        via = "referencia"

            # 2) Transferencia directa: matcheo por el monto con centavos únicos.
            if cliente is None and not resultado.startswith(("referencia", "cuota")):
                cliente = db.buscar_pendiente_por_monto(monto)
                via = "monto"

            if cliente:
                db.marcar_pagado(cliente["id"], mp_payment_id=str(data_id))
                resultado = f"pagado cliente {cliente['id']} (por {via})"

                # Aprender del pago: qué comisión cobró MP realmente y si el
                # neto alcanzó a cubrir la cuota. Con eso el próximo cobro se
                # calcula solo, sin depender de tablas publicadas.
                medido = mp.costo_real_del_pago(pago)
                if medido:
                    bruto, comision, neto, tasa = medido
                    try:
                        db.guardar_config({"recargo_observado": tasa})
                    except Exception:
                        pass
                    esperado = float(cliente["monto_esperado"])
                    faltante = round(esperado - neto, 2)
                    resultado += f" | comision real {tasa}% (${comision}), neto ${neto}"
                    if faltante > 0.5:
                        # El neto no llegó a cubrir la cuota: queda anotado en
                        # la propia cuota para que se vea en el panel.
                        resultado += f" | QUEDO CORTO ${faltante}"
                        try:
                            db.anotar_en_cuota(
                                cliente["id"],
                                f"Neto recibido ${neto} — faltaron ${faltante} "
                                f"(comisión real {tasa}%)",
                            )
                        except Exception:
                            pass
            elif not resultado.startswith(("referencia", "cuota")):
                resultado = f"pago aprobado sin cuota que matchee (monto {monto})"
        else:
            resultado = f"pago status={pago.get('status')}"
    else:
        resultado = f"topic no manejado: {topic}"

    db.log_webhook(json.dumps({"args": dict(request.args), "body": json_body}), resultado)
    # Mercado Pago espera 200/201 rápido, si no reintenta.
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
