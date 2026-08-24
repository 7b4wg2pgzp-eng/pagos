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
