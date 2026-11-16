"""Health check, redirección del login y flags de la cookie de sesión."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlparse

# Tiene que estar listo antes de importar: app.py lee la clave al arrancar.
_DIR = tempfile.mkdtemp(prefix="cuotas-salud-")
os.environ.setdefault("DB_PATH", os.path.join(_DIR, "pagos.db"))
os.environ["SECRET_KEY"] = "clave-de-prueba-para-tests"
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASS"] = "clave-de-test"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("RENDER", None)
os.environ.pop("RENDER_SERVICE_ID", None)
os.environ.pop("RENDER_EXTERNAL_URL", None)

import app as app_module

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USUARIO = os.environ["ADMIN_USER"]
CLAVE = os.environ["ADMIN_PASS"]
CLAVE_EJEMPLO = "cambiar-esta-clave-en-produccion"


def _destino(location):
    parsed = urlparse(location)
    return parsed.path or location


class SaludTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_health_responde_200_sin_base_ni_red(self):
        def boom(*_args, **_kwargs):
            raise AssertionError("el health check no tiene que salir de la app")

        with mock.patch("db.get_db", boom), mock.patch("requests.get", boom), mock.patch(
            "requests.post", boom
        ):
            resp = self.client.get("/health")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "ok"})

    def test_health_trae_las_cabeceras_minimas(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(resp.headers["X-Frame-Options"], "DENY")
        self.assertEqual(resp.headers["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertIn("frame-ancestors 'none'", resp.headers["Content-Security-Policy"])

    def test_la_calculadora_y_el_login_siguen_abriendo(self):
        inicio = self.client.get("/")
        login = self.client.get("/panel/login")
        self.assertEqual(inicio.status_code, 200)
        self.assertIn(b"<html", inicio.data.lower())
        self.assertEqual(login.status_code, 200)
        self.assertEqual(inicio.headers["X-Frame-Options"], "DENY")
        self.assertEqual(login.headers["Referrer-Policy"], "strict-origin-when-cross-origin")


class SiguienteSeguroTest(unittest.TestCase):
    def test_acepta_rutas_relativas_del_sitio(self):
        for ruta in ("/panel", "/panel/clientes/nuevo", "/panel?ok=1", "/cuotas"):
            self.assertEqual(app_module._siguiente_seguro(ruta), ruta)

    def test_rechaza_destinos_que_se_van_del_sitio(self):
        for malo in (
            None,
            "",
            "   ",
            "panel",
            "https://evil.example/robo",
            "http://evil.example",
            "//evil.example",
            "///evil.example",
            "/\\evil.example",
            "\\\\evil.example",
            "javascript:alert(1)",
            "/panel:otro",
            "/panel\nSet-Cookie:a=b",
            " https://evil.example",
            "/panel ",
        ):
            self.assertIsNone(app_module._siguiente_seguro(malo), malo)


class LoginRedirectTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def _entrar(self, siguiente):
        return self.client.post(
            "/panel/login",
            query_string={"next": siguiente},
            data={"usuario": USUARIO, "clave": CLAVE},
            base_url="https://localhost",
        )

    def test_next_externo_cae_en_el_panel(self):
        for siguiente in (
            "https://evil.example/robo",
            "//evil.example/robo",
            "/\\evil.example",
            "https://cuotas.nicovasquez.com.ar/panel",
        ):
            resp = self._entrar(siguiente)
            self.assertEqual(resp.status_code, 302, siguiente)
            self.assertNotIn("evil.example", resp.headers["Location"])
            self.assertEqual(_destino(resp.headers["Location"]), "/panel")

    def test_next_codificado_como_protocolo_relativo_se_rechaza(self):
        resp = self.client.post(
            "/panel/login?next=%2F%2Fevil.example%2Frobo",
            data={"usuario": USUARIO, "clave": CLAVE},
            base_url="https://localhost",
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(_destino(resp.headers["Location"]), "/panel")
        self.assertNotIn("evil.example", resp.headers["Location"])

    def test_next_relativo_se_respeta(self):
        resp = self._entrar("/panel/clientes/nuevo")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(_destino(resp.headers["Location"]), "/panel/clientes/nuevo")


class CookieSesionTest(unittest.TestCase):
    def test_la_cookie_de_sesion_pide_secure_httponly_y_samesite_lax(self):
        client = app_module.app.test_client()
        resp = client.post(
            "/panel/login",
            data={"usuario": USUARIO, "clave": CLAVE},
            base_url="https://localhost",
        )
        cookies = resp.headers.getlist("Set-Cookie")
        self.assertTrue(cookies)
        sesion = next(c for c in cookies if c.startswith("session="))
        self.assertIn("Secure", sesion)
        self.assertIn("HttpOnly", sesion)
        self.assertIn("SameSite=Lax", sesion)
        self.assertEqual(app_module.app.config["SESSION_COOKIE_SECURE"], True)
        self.assertEqual(app_module.app.config["SESSION_COOKIE_HTTPONLY"], True)
        self.assertEqual(app_module.app.config["SESSION_COOKIE_SAMESITE"], "Lax")

        panel = client.get("/panel", base_url="https://localhost")
        self.assertEqual(panel.status_code, 200)


class ArranqueSecretTest(unittest.TestCase):
    def _entorno(self, secret):
        env = os.environ.copy()
        for clave in ("RENDER", "RENDER_SERVICE_ID", "RENDER_EXTERNAL_URL", "DATABASE_URL"):
            env.pop(clave, None)
        env["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="cuotas-boot-"), "pagos.db")
        if secret is None:
            env.pop("SECRET_KEY", None)
        else:
            env["SECRET_KEY"] = secret
        return env

    def _importar(self, secret):
        return subprocess.run(
            [sys.executable, "-c", "import app"],
            cwd=RAIZ,
            env=self._entorno(secret),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_sin_secret_no_arranca(self):
        resultado = self._importar(None)
        self.assertNotEqual(resultado.returncode, 0)
        self.assertIn("SECRET_KEY", resultado.stderr)
        self.assertNotIn("clave-de-prueba-para-tests", resultado.stderr)

    def test_la_clave_de_ejemplo_no_arranca(self):
        for valor in (CLAVE_EJEMPLO, "  " + CLAVE_EJEMPLO + "  ", "   "):
            resultado = self._importar(valor)
            self.assertNotEqual(resultado.returncode, 0, valor)
            self.assertIn("SECRET_KEY", resultado.stderr)
            self.assertNotIn(CLAVE_EJEMPLO, resultado.stderr)

    def test_una_clave_distinta_arranca_y_el_health_responde(self):
        env = self._entorno("una-clave-distinta-de-la-de-ejemplo")
        codigo = (
            "import app\n"
            "resp = app.app.test_client().get('/health')\n"
            "assert resp.status_code == 200\n"
            "assert resp.get_json() == {'status': 'ok'}\n"
        )
        resultado = subprocess.run(
            [sys.executable, "-c", codigo],
            cwd=RAIZ,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(resultado.returncode, 0, resultado.stderr)


if __name__ == "__main__":
    unittest.main()
