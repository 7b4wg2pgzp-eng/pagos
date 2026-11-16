"""Entorno común, antes de que cualquier test importe la app.

`SECRET_KEY`, el usuario del panel y la base se leen al importar. Este
archivo corre primero cuando los tests se lanzan como paquete.
"""
import os
import tempfile

_DIR = tempfile.mkdtemp(prefix="cuotas-tests-")
os.environ.setdefault("DB_PATH", os.path.join(_DIR, "pagos.db"))
os.environ["SECRET_KEY"] = "clave-de-prueba-para-tests"
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASS"] = "clave-de-test"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("RENDER", None)
os.environ.pop("RENDER_SERVICE_ID", None)
os.environ.pop("RENDER_EXTERNAL_URL", None)
