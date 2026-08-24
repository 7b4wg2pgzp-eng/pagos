import os
import json
import functools
from datetime import datetime, date

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify

import db
import mp
import planes

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambiar-esta-clave-en-produccion")

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")
ALIAS_COBRO = os.environ.get("ALIAS_COBRO", "tu.alias.mp")

db.init_db()


def login_requerido(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logueado"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)

    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        usuario = request.form.get("usuario", "")
        clave = request.form.get("clave", "")
        if usuario == ADMIN_USER and clave and clave == ADMIN_PASS:
            session["logueado"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Usuario o clave incorrectos")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_requerido
def dashboard():
    clientes = db.listar_clientes()
    pendientes = [c for c in clientes if c["estado"] == "pendiente"]
    pagados = [c for c in clientes if c["estado"] == "pagado"]
    return render_template(
        "dashboard.html",
        pendientes=pendientes,
        pagados=pagados,
        alias=ALIAS_COBRO,
    )


@app.route("/clientes/nuevo", methods=["GET", "POST"])
@login_requerido
def nuevo_cliente():
    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        evento = request.form.get("evento", "").strip()
        telefono = request.form.get("telefono", "").strip()
        notas = request.form.get("notas", "").strip()
        try:
            monto_base = float(request.form.get("monto_base", "0").replace(",", "."))
        except ValueError:
            flash("Monto inválido")
            return redirect(url_for("nuevo_cliente"))

        if not nombre or monto_base <= 0:
            flash("Completá al menos el nombre y un monto mayor a 0")
            return redirect(url_for("nuevo_cliente"))

        cliente_id, monto = db.crear_cliente(nombre, evento, telefono, monto_base, notas)
        flash(f"Cliente creado. Monto exacto a transferir: ${monto:,.2f}")
        return redirect(url_for("detalle_cliente", cliente_id=cliente_id))

    return render_template("nuevo.html")


@app.route("/clientes/<int:cliente_id>")
@login_requerido
def detalle_cliente(cliente_id):
    cliente = db.obtener_cliente(cliente_id)
    if not cliente:
        flash("Cliente no encontrado")
        return redirect(url_for("dashboard"))
    return render_template("detalle.html", c=cliente, alias=ALIAS_COBRO)


@app.route("/clientes/<int:cliente_id>/marcar-pagado", methods=["POST"])
@login_requerido
def marcar_pagado_manual(cliente_id):
    db.marcar_pagado(cliente_id, mp_payment_id="manual")
    flash("Marcado como pagado manualmente")
    return redirect(url_for("detalle_cliente", cliente_id=cliente_id))


def _parsear_fecha(valor):
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _cuota_publica(row):
    """Recorta una fila de `clientes` a los campos que la calculadora
    pública puede ver (sin teléfono, notas ni mp_payment_id)."""
    return {
        "id": row["id"],
        "concepto": row["concepto"],
        "monto": float(row["monto_esperado"]),
        "fecha_vencimiento": row["fecha_vencimiento"],
        "estado": row["estado"],
    }


@app.route("/cuotas")
def calculadora_cuotas():
    """Página pública de la calculadora de cuotas para clientes."""
    return render_template("calculadora.html", alias=ALIAS_COBRO)


@app.route("/api/planes/opciones", methods=["POST"])
def api_opciones_plan():
    """Simulación (no escribe nada): dada una fecha de evento, devuelve
    todas las cantidades de cuotas posibles con su desglose calculado."""
    body = request.get_json(silent=True) or {}
    fecha_evento = _parsear_fecha(body.get("fecha_evento", ""))
    if not fecha_evento:
        return jsonify({"error": "fecha_evento inválida, formato YYYY-MM-DD"}), 400
    if fecha_evento < date.today():
        return jsonify({"error": "La fecha del evento ya pasó"}), 400

    max_cuotas = planes.cuotas_maximas_disponibles(fecha_evento)
    opciones = []
    for n in range(1, max_cuotas + 1):
        plan = planes.calcular_plan(fecha_evento, n)
        opciones.append(
            {
                "cantidad_cuotas": n,
                "total": sum(c["monto"] for c in plan),
                "cuotas": [
                    {
                        "concepto": c["concepto"],
                        "monto": c["monto"],
                        "fecha_vencimiento": c["fecha_vencimiento"].isoformat(),
                    }
                    for c in plan
                ],
            }
        )
    return jsonify({"max_cuotas": max_cuotas, "opciones": opciones})


@app.route("/api/planes", methods=["POST"])
def api_crear_plan():
    """Confirma y persiste el plan elegido: crea un registro real por cada
    cuota (misma lógica de montos únicos que usan los clientes del panel),
    para que el webhook de Mercado Pago las detecte igual que cualquier
    otro pago."""
    body = request.get_json(silent=True) or {}
    nombre = (body.get("nombre") or "Cliente calculadora").strip()
    telefono = (body.get("telefono") or "").strip()
    fecha_evento = _parsear_fecha(body.get("fecha_evento", ""))
    try:
        cantidad_cuotas = int(body.get("cantidad_cuotas"))
    except (TypeError, ValueError):
        return jsonify({"error": "cantidad_cuotas inválida"}), 400

    if not fecha_evento:
        return jsonify({"error": "fecha_evento inválida, formato YYYY-MM-DD"}), 400
    if fecha_evento < date.today():
        return jsonify({"error": "La fecha del evento ya pasó"}), 400

    max_cuotas = planes.cuotas_maximas_disponibles(fecha_evento)
    if cantidad_cuotas < 1 or cantidad_cuotas > max_cuotas:
        return jsonify({"error": f"cantidad_cuotas fuera de rango (máximo {max_cuotas})"}), 400

    # El monto de cada cuota se calcula acá, en el servidor, a partir de la
    # fecha y la cantidad de cuotas — nunca se confía en un monto mandado
    # por el cliente.
    cuotas_calc = planes.calcular_plan(fecha_evento, cantidad_cuotas)
    plan_token, ids = db.crear_plan(
        nombre=nombre,
        telefono=telefono,
        evento=fecha_evento.isoformat(),
        cuotas=cuotas_calc,
        notas="Creado desde la calculadora de cuotas",
    )
    plan = db.obtener_plan(plan_token)
    return jsonify(
        {
            "plan_token": plan_token,
            "alias": ALIAS_COBRO,
            "cuotas": [_cuota_publica(c) for c in plan],
        }
    )


@app.route("/api/planes/<token>")
def api_obtener_plan(token):
    plan = db.obtener_plan(token)
    if not plan:
        return jsonify({"error": "Plan no encontrado"}), 404
    return jsonify({"alias": ALIAS_COBRO, "cuotas": [_cuota_publica(c) for c in plan]})


@app.route("/sincronizar", methods=["POST"])
@login_requerido
def sincronizar():
    """Fallback: busca pagos recientes aprobados en Mercado Pago y los
    matchea contra clientes pendientes, por si algún webhook no llegó."""
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
            cliente = db.buscar_pendiente_por_monto(monto)
            if cliente:
                db.marcar_pagado(cliente["id"], mp_payment_id=str(data_id))
                resultado = f"pagado cliente {cliente['id']}"
            else:
                resultado = f"pago aprobado sin cliente que matchee (monto {monto})"
        else:
            resultado = f"pago status={pago.get('status')}"
    else:
        resultado = f"topic no manejado: {topic}"

    db.log_webhook(json.dumps({"args": dict(request.args), "body": json_body}), resultado)
    # Mercado Pago espera 200/201 rápido, si no reintenta.
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
