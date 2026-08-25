"""Puente hacia la aplicación real, que vive en la raíz del repo.

Render arranca este servicio con `cd gestion-pagos && gunicorn app:app`.
Durante un tiempo hubo acá una copia completa de la aplicación, y eso hizo que
varios deploys salieran verdes sin cambiar nada: se actualizaba la raíz y
producción seguía corriendo esta copia vieja, sin ningún error visible.

Este archivo ya no tiene lógica propia. Carga `app.py` de la raíz bajo otro
nombre de módulo (si lo importara como `app` se importaría a sí mismo) y pone
la raíz primero en el path, así `db`, `mp` y `planes` también salen de ahí.
Hay un solo código: el de la raíz.
"""
import importlib.util
import os
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

_ruta = os.path.join(RAIZ, "app.py")
_spec = importlib.util.spec_from_file_location("app_raiz", _ruta)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["app_raiz"] = _mod
_spec.loader.exec_module(_mod)

app = _mod.app
