"""Firma del webhook de Mercado Pago.

Cubre el manifest documentado (HMAC-SHA256, data.id en minúsculas) y que un
aviso sin secret o con firma inválida no marque un pago.
"""
import hashlib
import hmac
import os
import tempfile
import unittest
from unittest import mock

_DIR = tempfile.mkdtemp(prefix="cuotas-webhook-")
os.environ["DB_PATH"] = os.path.join(_DIR, "pagos.db")
os.environ.pop("DATABASE_URL", None)
os.environ.pop("RENDER", None)
os.environ.pop("RENDER_SERVICE_ID", None)
os.environ.pop("RENDER_EXTERNAL_URL", None)
os.environ["MP_WEBHOOK_SECRET"] = "secreto-de-prueba"
# app.py no arranca sin SECRET_KEY y rechaza la clave de ejemplo del repo.
os.environ.setdefault("SECRET_KEY", "clave-de-prueba-para-tests")

import app as app_module
import db
import mp

SECRETO = "secreto-de-prueba"
TS = "1704908010"
REQUEST_ID = "req-1"
# HMAC-SHA256 de "id:999999999;request-id:req-1;ts:1704908010;" con SECRETO.
V1_NUMERICO = "2a47b2365a83f024a61bff9c79a971d10afe37cc9de61b34c183cb4eeb25384a"
# El mismo ts y request-id, con data.id "ORD01ABC" pasado a minúsculas.
V1_ALFANUMERICO = "9821eaf3070df9e0bb1f4ec1281e7ba72065b7f3fdbc1e03f4c9494f3db898b0"


def _headers(v1, request_id=REQUEST_ID, ts=TS):
    return {
        "x-signature": f"ts={ts},v1={v1}",
        "x-request-id": request_id,
    }


class ValidarFirmaTest(unittest.TestCase):
    def setUp(self):
        os.environ["MP_WEBHOOK_SECRET"] = SECRETO

    def test_vector_conocido_acepta_id_numerico(self):
        self.assertTrue(mp.validar_firma(_headers(V1_NUMERICO), "999999999"))

    def test_data_id_alfanumerico_se_valida_en_minusculas(self):
        # La doc de Mercado Pago pide bajar a minúsculas el data.id alfanumérico.
        self.assertTrue(mp.validar_firma(_headers(V1_ALFANUMERICO), "ORD01ABC"))
        manifest_mayus = f"id:ORD01ABC;request-id:{REQUEST_ID};ts:{TS};"
        v1_mayus = hmac.new(
            SECRETO.encode(), manifest_mayus.encode(), hashlib.sha256
        ).hexdigest()
        self.assertFalse(mp.validar_firma(_headers(v1_mayus), "ORD01ABC"))

    def test_orden_de_partes_y_espacios_no_rompen_la_firma_valida(self):
        headers = {
            "x-signature": f"v1={V1_NUMERICO}, ts={TS}",
            "x-request-id": REQUEST_ID,
        }
        self.assertTrue(mp.validar_firma(headers, "999999999"))

    def test_firma_distinta_o_incompleta_se_rechaza(self):
        self.assertFalse(mp.validar_firma(_headers("0" * 64), "999999999"))
        self.assertFalse(mp.validar_firma(_headers(V1_NUMERICO[:-1]), "999999999"))
        self.assertFalse(mp.validar_firma({}, "999999999"))
        self.assertFalse(mp.validar_firma({"x-signature": f"ts={TS}"}, "999999999"))
        self.assertFalse(mp.validar_firma(_headers(V1_NUMERICO), ""))

    def test_sin_secret_rechaza_aunque_la_firma_cierre(self):
        os.environ["MP_WEBHOOK_SECRET"] = "   "
        with self.assertLogs("mp", level="WARNING") as logs:
            self.assertIs(mp.validar_firma(_headers(V1_NUMERICO), "999999999"), False)
        os.environ.pop("MP_WEBHOOK_SECRET", None)
        self.assertIs(mp.validar_firma(_headers(V1_NUMERICO), "999999999"), False)
        texto = "\n".join(logs.output)
        self.assertIn("MP_WEBHOOK_SECRET", texto)
        self.assertNotIn(SECRETO, texto)

    def test_firma_invalida_avisa_sin_volcar_el_secret(self):
        with self.assertLogs("mp", level="WARNING") as logs:
            self.assertFalse(mp.validar_firma(_headers("f" * 64), "999999999"))
        texto = "\n".join(logs.output)
        self.assertIn("no coincide", texto)
        self.assertNotIn(SECRETO, texto)
        self.assertNotIn("f" * 64, texto)


class ExtraerDataIdTest(unittest.TestCase):
    def test_variantes_type_y_topic(self):
        self.assertEqual(
            mp.extraer_data_id({"data.id": "10", "type": "payment"}, {}),
            ("10", "payment"),
        )
        self.assertEqual(
            mp.extraer_data_id({"id": "11", "topic": "payment"}, {}),
            ("11", "payment"),
        )
        self.assertEqual(
            mp.extraer_data_id(
                {},
                {"type": "payment", "data": {"id": 12}},
            ),
            (12, "payment"),
        )
        self.assertEqual(
            mp.extraer_data_id(
                {},
                {"action": "payment.updated", "data": {"id": "13"}},
            ),
            ("13", "payment"),
        )


class WebhookEndpointTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        os.environ["MP_WEBHOOK_SECRET"] = SECRETO

    def _firma(self, data_id, request_id="req-endpoint"):
        manifest = f"id:{str(data_id).lower()};request-id:{request_id};ts:{TS};"
        v1 = hmac.new(SECRETO.encode(), manifest.encode(), hashlib.sha256).hexdigest()
        return {
            "x-signature": f"ts={TS},v1={v1}",
            "x-request-id": request_id,
        }

    def _cuota(self):
        return db.crear_cliente("Cliente prueba", "Evento", "", 100)

    def _pago(self, data_id, cuota_id, monto, status="approved"):
        return {
            "id": data_id,
            "status": status,
            "transaction_amount": monto,
            "external_reference": f"cuota:{cuota_id}",
            "fee_details": [{"amount": 1.23, "type": "mercadopago_fee"}],
            "transaction_details": {"net_received_amount": round(float(monto) - 1.23, 2)},
        }

    def _post(self, query, data_id, body=None, headers=None, secret_presente=True):
        if headers is None and secret_presente:
            headers = self._firma(data_id)
        return self.client.post(
            f"/webhook/mercadopago{query}",
            json=body if body is not None else {"type": "payment", "data": {"id": data_id}},
            headers=headers or {},
        )

    def test_firma_valida_marca_el_pago_y_responde_ok(self):
        cuota_id, monto = self._cuota()
        data_id = "555000111"
        pago = self._pago(data_id, cuota_id, monto)
        with mock.patch("mp.get_payment", return_value=pago) as get_payment:
            with self.assertNoLogs(app_module.app.logger, level="WARNING"):
                resp = self._post("?data.id=555000111&type=payment", data_id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "ok"})
        get_payment.assert_called_once_with("555000111")
        guardado = db.obtener_cliente(cuota_id)
        self.assertEqual(guardado["estado"], "pagado")
        self.assertEqual(guardado["mp_payment_id"], data_id)

    def test_variante_topic_e_id_tambien_procesa_si_la_firma_cierra(self):
        cuota_id, monto = self._cuota()
        data_id = "555000222"
        pago = self._pago(data_id, cuota_id, monto)
        with mock.patch("mp.get_payment", return_value=pago) as get_payment:
            resp = self._post("?topic=payment&id=555000222", data_id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "ok"})
        get_payment.assert_called_once_with("555000222")
        self.assertEqual(db.obtener_cliente(cuota_id)["estado"], "pagado")

    def test_id_en_el_body_se_procesa_si_la_firma_cierra(self):
        cuota_id, monto = self._cuota()
        data_id = "555000333"
        pago = self._pago(data_id, cuota_id, monto)
        body = {"action": "payment.created", "data": {"id": data_id}}
        with mock.patch("mp.get_payment", return_value=pago) as get_payment:
            resp = self._post("", data_id, body=body)
        self.assertEqual(resp.status_code, 200)
        get_payment.assert_called_once_with(data_id)
        self.assertEqual(db.obtener_cliente(cuota_id)["estado"], "pagado")

    def test_firma_valida_con_pago_no_aprobado_no_marca_la_cuota(self):
        cuota_id, monto = self._cuota()
        data_id = "555000444"
        pago = self._pago(data_id, cuota_id, monto, status="pending")
        with mock.patch("mp.get_payment", return_value=pago) as get_payment:
            resp = self._post("?data.id=555000444&type=payment", data_id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "ok"})
        get_payment.assert_called_once_with(data_id)
        self.assertEqual(db.obtener_cliente(cuota_id)["estado"], "pendiente")

    def test_sin_secret_responde_401_y_no_toca_el_pago(self):
        cuota_id, _monto = self._cuota()
        data_id = "555000555"
        headers = self._firma(data_id)
        os.environ.pop("MP_WEBHOOK_SECRET", None)
        with mock.patch("mp.get_payment") as get_payment:
            with self.assertLogs(app_module.app.logger, level="WARNING") as logs:
                resp = self._post(
                    "?data.id=555000555&type=payment",
                    data_id,
                    headers=headers,
                    secret_presente=False,
                )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json(), {"status": "firma invalida"})
        get_payment.assert_not_called()
        self.assertEqual(db.obtener_cliente(cuota_id)["estado"], "pendiente")
        texto = "\n".join(logs.output)
        self.assertIn("MP_WEBHOOK_SECRET", texto)
        self.assertNotIn(SECRETO, texto)

    def test_firma_ausente_o_falsa_responde_401_y_no_consulta_el_pago(self):
        cuota_id, _monto = self._cuota()
        data_id = "555000666"
        with mock.patch("mp.get_payment") as get_payment:
            with self.assertLogs(app_module.app.logger, level="WARNING") as logs:
                sin_firma = self._post(
                    "?data.id=555000666&type=payment",
                    data_id,
                    headers={},
                    secret_presente=False,
                )
                falsa = self._post(
                    "?data.id=555000666&type=payment",
                    data_id,
                    headers=_headers("ab" * 32),
                    secret_presente=False,
                )
        self.assertEqual(sin_firma.status_code, 401)
        self.assertEqual(falsa.status_code, 401)
        self.assertEqual(falsa.get_json(), {"status": "firma invalida"})
        get_payment.assert_not_called()
        self.assertEqual(db.obtener_cliente(cuota_id)["estado"], "pendiente")
        self.assertNotIn(SECRETO, "\n".join(logs.output))

    def test_otro_topic_sigue_respondiendo_ok_sin_marcar_pagos(self):
        cuota_id, _monto = self._cuota()
        with mock.patch("mp.get_payment") as get_payment:
            resp = self.client.post(
                "/webhook/mercadopago?data.id=1&type=merchant_order",
                json={"type": "merchant_order", "data": {"id": "1"}},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "ok"})
        get_payment.assert_not_called()
        self.assertEqual(db.obtener_cliente(cuota_id)["estado"], "pendiente")


if __name__ == "__main__":
    unittest.main()
