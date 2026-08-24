"""Helpers para hablar con la API de Mercado Pago:
- traer el detalle de un pago
- buscar pagos recientes (fallback de sincronización)
- validar la firma de un webhook
"""
import hashlib
import hmac
import os
import requests

MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "")
MP_WEBHOOK_SECRET = os.environ.get("MP_WEBHOOK_SECRET", "")

API_BASE = "https://api.mercadopago.com"


def get_payment(payment_id):
    """Trae el detalle completo de un pago por su ID."""
    resp = requests.get(
        f"{API_BASE}/v1/payments/{payment_id}",
        headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def search_recent_payments(limit=30):
    """Trae los últimos pagos/movimientos acreditados en la cuenta (fallback
    manual por si algún webhook no llegó, típico de transferencias)."""
    resp = requests.get(
        f"{API_BASE}/v1/payments/search",
        headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
        params={
            "sort": "date_created",
            "criteria": "desc",
            "limit": limit,
            "status": "approved",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def crear_preferencia(titulo, monto, referencia_externa, url_vuelta, url_webhook=None):
    """Arma una preferencia de Checkout Pro para cobrar UNA cuota.

    Solo medios de comisión baja: saldo de Mercado Pago y pago con
    transferencia desde cualquier banco o billetera (ambos en la banda de
    6-8 por mil). Se excluyen tarjetas de crédito y débito, efectivo y cajero,
    donde la comisión salta de ~0,6% a 2,99%-4,49% y el negocio deja de cerrar.

    Se deja habilitada la transferencia además del saldo a propósito: si solo
    aceptara saldo, el cliente que tiene la plata en el banco y no en Mercado
    Pago no podría pagar y se quedaría sin ninguna salida.

    `referencia_externa` es lo que después nos devuelve el webhook para saber
    exactamente qué cuota se pagó, sin depender del monto.

    Devuelve el dict de la preferencia; el link para mandar al cliente está en
    la clave `init_point`."""
    cuerpo = {
        "items": [{
            "title": titulo,
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": round(float(monto), 2),
        }],
        "external_reference": referencia_externa,
        "back_urls": {
            "success": url_vuelta,
            "pending": url_vuelta,
            "failure": url_vuelta,
        },
        "auto_return": "approved",
        # Fuera todo lo caro. Queda saldo en cuenta y pago con transferencia.
        "payment_methods": {
            "excluded_payment_types": [
                {"id": "credit_card"},
                {"id": "debit_card"},
                {"id": "prepaid_card"},
                {"id": "ticket"},
                {"id": "atm"},
            ],
            "installments": 1,
        },
    }
    if url_webhook:
        cuerpo["notification_url"] = url_webhook

    resp = requests.post(
        f"{API_BASE}/checkout/preferences",
        headers={
            "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json=cuerpo,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def costo_real_del_pago(pago):
    """Lee del pago lo que Mercado Pago cobró de verdad.

    No hay API pública que diga la comisión de tu cuenta, y las tablas
    publicadas varían según el plazo de acreditación configurado. Pero cada
    pago aprobado trae `fee_details` con lo efectivamente descontado y
    `net_received_amount` con lo que quedó. Esa es la única fuente exacta.

    Devuelve (bruto, comision, neto, tasa) o None si el pago no trae los datos.
    `tasa` es la comisión como porcentaje del bruto."""
    try:
        bruto = float(pago.get("transaction_amount") or 0)
    except (TypeError, ValueError):
        return None
    if bruto <= 0:
        return None

    comision = 0.0
    for f in (pago.get("fee_details") or []):
        try:
            comision += float(f.get("amount") or 0)
        except (TypeError, ValueError):
            continue

    detalles = pago.get("transaction_details") or {}
    neto = detalles.get("net_received_amount")
    try:
        neto = float(neto) if neto is not None else None
    except (TypeError, ValueError):
        neto = None

    # Si no vino el desglose pero sí el neto, la comisión se deduce.
    if comision <= 0 and neto is not None:
        comision = bruto - neto
    if neto is None:
        neto = bruto - comision
    if comision <= 0:
        return None

    return bruto, round(comision, 2), round(neto, 2), round(comision / bruto * 100, 4)


def extraer_data_id(args, json_body):
    """El formato de notificación de MP tiene variantes: ?data.id=..&type=payment
    (nuevo) o ?topic=payment&id=.. (viejo), o el id viene en el body JSON."""
    data_id = args.get("data.id") or args.get("id")
    topic = args.get("type") or args.get("topic")

    if not data_id and json_body:
        data = json_body.get("data") or {}
        data_id = data.get("id")
        topic = json_body.get("type") or json_body.get("action", "").split(".")[0]

    return data_id, topic


def validar_firma(headers, data_id):
    """Valida el header x-signature contra MP_WEBHOOK_SECRET.
    Devuelve True si es válida, False si no. Si no hay secret configurado
    todavía (por ejemplo mientras probás en local), no bloquea pero lo avisa
    devolviendo None."""
    if not MP_WEBHOOK_SECRET:
        return None

    signature = headers.get("x-signature", "")
    request_id = headers.get("x-request-id", "")

    ts = None
    v1 = None
    for part in signature.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key == "ts":
            ts = value.strip()
        elif key == "v1":
            v1 = value.strip()

    if not ts or not v1 or not data_id:
        return False

    manifest = f"id:{str(data_id).lower()};request-id:{request_id};ts:{ts};"
    expected = hmac.new(
        MP_WEBHOOK_SECRET.encode(), manifest.encode(), hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, v1)
