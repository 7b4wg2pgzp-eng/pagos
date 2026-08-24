"""Lógica de cálculo del plan de cuotas para eventos.

Reglas de negocio:
- Presupuesto base: $500.000. Seña: $250.000 (se paga siempre "hoy").
- 1 cuota (pago único): $500.000, todo hoy, sin seña separada.
- 2 cuotas: seña ($250.000) + 1 cuota de $250.000, vence 1 mes antes del evento.
- 3 o más cuotas: el total sube a $550.000 (recargo por financiar). Se resta la
  seña ($250.000) y el resto ($300.000) se reparte en partes iguales entre las
  cuotas restantes, la última cuota vence exactamente 1 mes antes del evento y
  las anteriores van hacia atrás, una por mes.
"""
from datetime import date
import calendar

import db

# Valores por defecto. Los reales se leen de la tabla `config`, editable
# desde el panel de gestión (db.leer_config()).
PRESUPUESTO_BASE = 500_000
PRESUPUESTO_FINANCIADO = 550_000
SENA = 250_000
MAX_CUOTAS = 12


def montos():
    """Lee los montos vigentes desde la configuración guardada."""
    c = db.leer_config()
    return c["presupuesto_base"], c["presupuesto_financiado"], c["sena"]


def restar_meses(fecha, n):
    """Resta n meses a una fecha, ajustando el día si el mes destino es más corto."""
    mes_total = fecha.month - 1 - n
    anio = fecha.year + mes_total // 12
    mes = mes_total % 12 + 1
    ultimo_dia = calendar.monthrange(anio, mes)[1]
    dia = min(fecha.day, ultimo_dia)
    return date(anio, mes, dia)


def meses_entre(a, b):
    """Cantidad de meses completos desde a hasta b (b >= a), contando por
    diferencia de mes/año, sin importar el día."""
    return (b.year - a.year) * 12 + (b.month - a.month)


def cuotas_maximas_disponibles(fecha_evento, hoy=None):
    """Cuántas cuotas mensuales (además de la seña) entran entre hoy y un mes
    antes del evento. Devuelve la cantidad total de cuotas (incluyendo la
    seña) permitida, con un piso de 1 (pago único) y techo MAX_CUOTAS.

    Se calcula probando de mayor a menor: la primera cuota (la más antigua)
    tiene que caer hoy o después, nunca en el pasado. Usar solo la resta de
    meses de calendario (sin mirar el día) podía dar una primera cuota ya
    vencida cuando el día del evento es menor al día de hoy."""
    hoy = hoy or date.today()
    limite = restar_meses(fecha_evento, 1)
    if limite < hoy:
        return 1
    for n in range(MAX_CUOTAS - 1, 0, -1):  # n = cuotas sin contar la seña
        primera = restar_meses(limite, n - 1)
        if primera >= hoy:
            return min(MAX_CUOTAS, n + 1)
    return 1


def calcular_plan(fecha_evento, cantidad_cuotas, hoy=None):
    """Devuelve la lista de cuotas: [{concepto, monto, fecha_vencimiento}, ...]
    fecha_vencimiento como date. monto en pesos, redondeado, ajustando la
    última cuota para que la suma cierre exacta."""
    hoy = hoy or date.today()
    cantidad_cuotas = int(cantidad_cuotas)

    base, financiado, sena = montos()

    if cantidad_cuotas <= 1:
        return [
            {"concepto": "Pago único", "monto": base, "fecha_vencimiento": hoy}
        ]

    total = base if cantidad_cuotas == 2 else financiado
    resto = total - sena
    n_cuotas = cantidad_cuotas - 1  # sin contar la seña

    limite = restar_meses(fecha_evento, 1)
    fechas = [restar_meses(limite, n_cuotas - 1 - i) for i in range(n_cuotas)]

    monto_cada_una = round(resto / n_cuotas)
    montos_cuotas = [monto_cada_una] * n_cuotas
    diferencia = resto - sum(montos_cuotas)
    montos_cuotas[-1] += diferencia

    plan = [{"concepto": "Seña", "monto": sena, "fecha_vencimiento": hoy}]
    for i, (fecha, monto) in enumerate(zip(fechas, montos_cuotas), start=1):
        plan.append(
            {
                "concepto": f"Cuota {i}/{n_cuotas}",
                "monto": monto,
                "fecha_vencimiento": fecha,
            }
        )
    return plan
