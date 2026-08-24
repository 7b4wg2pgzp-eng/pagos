"""Cálculo del plan de pago de un evento.

Reglas del negocio
------------------
- Precio de contado: $500.000. Seña: $250.000, SIEMPRE, y nunca se divide.
- La seña se abona al confirmar el plan (hoy) y no cuenta como cuota.
- Saldo en 1 pago   -> precio total $500.000, saldo $250.000.
- Saldo en 2 a 5    -> precio total $550.000, saldo financiado $300.000.
- Saldo en 6 o más  -> segundo tramo de financiación, precio total $600.000,
  saldo financiado $350.000. Así la cuota larga no queda tan chica.
  (el saldo financiado NUNCA es 250.000: el precio total sube)
- Cuota mínima: $50.000. Tope: 8 cuotas.
- El saldo tiene que quedar cancelado como máximo 1 mes antes del evento:
  la última cuota vence exactamente en esa fecha límite y las anteriores
  van hacia atrás, una por mes.
- La cantidad de cuotas ofrecidas depende del tiempo que haya entre hoy y
  la fecha límite; nunca se ofrece una cuota que venza después del límite.

En este módulo "cuotas" siempre significa cuotas DEL SALDO (sin la seña):
cuotas_saldo = 1 es la opción de contado.
"""
from datetime import date
import calendar

import db

# Valores por defecto; los vigentes se leen de la tabla `config`, editable
# desde el panel de gestión.
PRESUPUESTO_BASE = 500_000
PRESUPUESTO_FINANCIADO = 550_000
PRESUPUESTO_FINANCIADO_LARGO = 600_000
CUOTAS_TRAMO_LARGO = 6
SENA = 250_000
CUOTA_MINIMA = 50_000
MAX_CUOTAS = 8


def montos():
    """Configuración vigente de precios y financiación."""
    return db.leer_config()


def precio_total(n, conf=None):
    """Precio final según en cuántas cuotas se abone el saldo."""
    c = conf or montos()
    if n <= 1:
        return c["presupuesto_base"]
    if n < c["cuotas_tramo_largo"]:
        return c["presupuesto_financiado"]
    return c["presupuesto_financiado_largo"]


def tramo(n, conf=None):
    """Etiqueta del tramo de precio: 'contado', 'financiado' o 'financiado_largo'."""
    c = conf or montos()
    if n <= 1:
        return "contado"
    return "financiado" if n < c["cuotas_tramo_largo"] else "financiado_largo"


def restar_meses(fecha, n):
    """Resta n meses a una fecha, ajustando el día si el mes destino es más corto."""
    mes_total = fecha.month - 1 - n
    anio = fecha.year + mes_total // 12
    mes = mes_total % 12 + 1
    ultimo_dia = calendar.monthrange(anio, mes)[1]
    return date(anio, mes, min(fecha.day, ultimo_dia))


def meses_entre(a, b):
    """Meses de calendario desde a hasta b, sin mirar el día."""
    return (b.year - a.year) * 12 + (b.month - a.month)


def limite_pago(fecha_evento):
    """Fecha límite para tener el saldo cancelado: 1 mes antes del evento."""
    return restar_meses(fecha_evento, 1)


def cuotas_disponibles(fecha_evento, hoy=None):
    """Lista de cantidades de cuotas del saldo ofrecibles para esa fecha.

    Siempre incluye el 1 (contado, disponible aunque el evento sea mañana).
    Para financiar en n cuotas hacen falta n huecos mensuales entre hoy y el
    límite: con la última cuota en el límite, la primera cae n-1 meses antes
    y tiene que quedar después de hoy (la seña ya se paga hoy). Eso da
    n <= meses_entre(hoy, limite).

    Se evalúa n por n en vez de calcular un único techo, porque el saldo
    cambia de tramo (y por lo tanto el valor de la cuota también) al llegar
    a `cuotas_tramo_largo`.
    """
    hoy = hoy or date.today()
    conf = montos()
    sena, cuota_min, tope = conf["sena"], conf["cuota_minima"], conf["max_cuotas"]
    por_tiempo = meses_entre(hoy, limite_pago(fecha_evento))

    validas = [1]
    for n in range(2, tope + 1):
        if n > por_tiempo:
            continue
        saldo = precio_total(n, conf) - sena
        if cuota_min > 0 and saldo / n < cuota_min:
            continue
        validas.append(n)
    return validas


def calcular_plan(fecha_evento, cuotas_saldo, hoy=None):
    """Plan completo: [{concepto, monto, fecha_vencimiento}, ...].

    `cuotas_saldo` son las cuotas del SALDO, sin contar la seña:
      1  -> contado  (seña + 1 pago del saldo, total $500.000)
      2+ -> financiado (seña + n cuotas, total $550.000)

    El primer ítem siempre es la seña, con vencimiento hoy.
    """
    hoy = hoy or date.today()
    n = max(1, int(cuotas_saldo))

    conf = montos()
    sena = conf["sena"]
    total = precio_total(n, conf)
    saldo = total - sena

    limite = limite_pago(fecha_evento)

    if n == 1:
        # Un solo pago del saldo. Si el evento está tan cerca que el límite ya
        # pasó, el saldo se abona hoy junto con la seña.
        fechas = [max(limite, hoy)]
    else:
        fechas = [restar_meses(limite, n - 1 - i) for i in range(n)]

    base = round(saldo / n)
    montos_cuotas = [base] * n
    montos_cuotas[-1] += saldo - sum(montos_cuotas)  # la última absorbe el redondeo

    plan = [{"concepto": "Seña", "monto": sena, "fecha_vencimiento": hoy}]
    for i, (fecha, monto) in enumerate(zip(fechas, montos_cuotas), start=1):
        concepto = "Saldo" if n == 1 else "Cuota {}/{}".format(i, n)
        plan.append({"concepto": concepto, "monto": monto, "fecha_vencimiento": fecha})
    return plan


def resumen_opcion(fecha_evento, cuotas_saldo, hoy=None):
    """Datos de una opción, listos para mostrar en la calculadora."""
    plan = calcular_plan(fecha_evento, cuotas_saldo, hoy)
    sena = plan[0]["monto"]
    cuotas = plan[1:]
    total = sena + sum(c["monto"] for c in cuotas)
    montos_cuotas = [c["monto"] for c in cuotas]
    return {
        "cuotas_saldo": len(cuotas),
        "financiado": len(cuotas) > 1,
        "tramo": tramo(len(cuotas)),
        "sena": sena,
        "saldo": total - sena,
        "monto_cuota": montos_cuotas[0],
        "ultima_cuota": montos_cuotas[-1],
        "cuotas_iguales": len(set(montos_cuotas)) == 1,
        "total": total,
        "cuotas": [
            {
                "concepto": c["concepto"],
                "monto": c["monto"],
                "fecha_vencimiento": c["fecha_vencimiento"].isoformat(),
            }
            for c in plan
        ],
    }
