# Gestión de pagos

Panel propio para cobrar señas/pagos de eventos por transferencia (CVU/alias),
sin comisión, con detección automática vía la API de Mercado Pago.

Cómo funciona: creás un cliente con un monto a cobrar. El sistema le suma
centavos únicos (ej $45.000 → $45.000,37) para poder identificar de quién es
cada transferencia. Le pasás el alias y el monto exacto al cliente. Cuando
transfiere, Mercado Pago te avisa por webhook, el sistema busca el pago,
matchea por monto y marca "pagado" solo.

## 1. Cuenta de developer en Mercado Pago

1. Entrá a https://www.mercadopago.com.ar/developers/panel con tu cuenta de
   Mercado Pago normal (la que ya usás para cobrar).
2. Creá una aplicación ("Tus integraciones" → "Crear aplicación"). Cualquier
   nombre, y elegí "Pagos online" como modelo de integración.
3. En la sección "Credenciales de producción" copiá el **Access Token**
   (empieza con `APP_USR-...`). Ese va en `MP_ACCESS_TOKEN`.
4. En "Webhooks" (dentro de tu aplicación) configurá la URL de notificación:
   `https://TU-SUBDOMINIO/webhook/mercadopago` y activá el evento **Pagos**.
   Ahí mismo te va a mostrar una **clave secreta** para validar la firma —
   esa va en `MP_WEBHOOK_SECRET`.

⚠️ Punto a confirmar con una prueba real (no está 100% documentado por MP):
las transferencias que te llegan directo al alias/CVU generalmente sí generan
un "pago" (`payment_type_id: account_money`) y disparan este mismo webhook,
igual que un cobro por checkout — es el mecanismo que usa mucha gente para
conciliar transferencias en Argentina. Pero conviene comprobarlo apenas
tengas el access token: hacete una transferencia de prueba de un monto raro
(ej $1,23) a tu propio alias desde otra cuenta y fijate en `/sincronizar`
(botón del panel) o en los logs si aparece. Si por algo no dispara el
webhook automáticamente, el botón "Sincronizar con Mercado Pago" del panel
cubre ese caso buscando los pagos recientes a mano.

## 2. Correr localmente

```bash
cd gestion-pagos
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# editá .env con tus datos
export $(cat .env | xargs)   # o usá python-dotenv si preferís
python app.py
```

Entrá a http://localhost:5000, logueate con `ADMIN_USER` / `ADMIN_PASS`.

## 3. Deploy en Render (mismo patrón que ya usás en EventPhotos)

1. Subí esta carpeta a un repo de GitHub.
2. En Render: **New → Web Service**, conectá el repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app`
5. Variables de entorno (Environment): cargá las mismas de `.env.example`
   con tus valores reales.
6. Deploy.

## 4. Subdominio en Cloudflare

Igual que hiciste con `fotospantalla.nicovasquezdjs.com`: un registro CNAME
en Cloudflare apuntando al host que te da Render, **DNS only** (nube gris,
sin proxy) para no romper la emisión de TLS de Render.

Ese subdominio (ej. `pagos.nicovasquezdjs.com`) es el que ponés como URL del
webhook en el panel de Mercado Pago Developers.

## 5. Probar el flujo completo

1. Creá un cliente de prueba con un monto chico (ej $10).
2. El sistema te va a dar el monto exacto con centavos (ej $10,42).
3. Transferite esos $10,42 a tu propio alias desde otra cuenta/billetera.
4. Esperá unos segundos y refrescá el dashboard — debería aparecer como
   pagado solo. Si no, probá el botón "Sincronizar con Mercado Pago".
5. Revisá la tabla `eventos_webhook` en `pagos.db` (o agregá un log a
   consola) para ver qué llegó exactamente si algo no matchea.

## Seguridad

- Cambiá `SECRET_KEY` y `ADMIN_PASS` antes de deployar — no dejes los valores
  de ejemplo.
- El endpoint `/webhook/mercadopago` valida la firma (`x-signature`) contra
  `MP_WEBHOOK_SECRET`. Sin ese secret configurado, no rechaza pero no valida
  — configuralo apenas lo tengas.
- No expongas el `MP_ACCESS_TOKEN` en el frontend ni en el repo (por eso
  `.env` está en `.gitignore`).
