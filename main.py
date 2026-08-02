import os
import asyncio
import xmlrpc.client
from datetime import datetime, timedelta, time
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
KIMI_API_KEY = os.environ["KIMI_API_KEY"]
ODOO_URL = os.environ["ODOO_URL"]
ODOO_DB = os.environ["ODOO_DB"]
ODOO_USER = os.environ["ODOO_USER"]
ODOO_PASS = os.environ["ODOO_PASS"]

UTC_OFFSET = 6  # México: UTC-6 todo el año

client = OpenAI(api_key=KIMI_API_KEY, base_url="https://api.moonshot.ai/v1")
historial = {}

_uid_cache = None

def odoo_uid():
    global _uid_cache
    if _uid_cache is None:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        _uid_cache = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
    return _uid_cache

def odoo(model, method, args, kwargs=None):
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return models.execute_kw(ODOO_DB, odoo_uid(), ODOO_PASS, model, method, args, kwargs or {})

def ahora_mx():
    return datetime.utcnow() - timedelta(hours=UTC_OFFSET)

def consultar_inventario(termino):
    prods = odoo("product.product", "search_read",
                 [[["name", "ilike", termino]]],
                 {"fields": ["id", "name", "default_code"], "limit": 10})
    if not prods:
        return f"❌ No encontré productos con '{termino}'."
    lineas = []
    for p in prods:
        grupos = odoo("stock.quant", "read_group",
                      [[["product_id", "=", p["id"]]]],
                      {"fields": ["qty", "location_id"], "groupby": ["location_id"]})
        total = sum(g["qty"] for g in grupos)
        detalle = ", ".join(f"{g['location_id'][1]}: {g['qty']:.0f}" for g in grupos if g["qty"] != 0)
        lineas.append(f"📦 {p['name']} — Total: {total:.0f}" + (f"\n   ({detalle})" if detalle else ""))
    return "\n".join(lineas)

def resolver_fecha(texto):
    texto = (texto or "").strip().lower()
    hoy = ahora_mx().date()
    if texto in ("", "hoy"):
        return hoy, "hoy"
    if texto == "ayer":
        return hoy - timedelta(days=1), "ayer"
    try:
        return datetime.strptime(texto, "%Y-%m-%d").date(), texto
    except ValueError:
        return None, None

def _ordenes_del_dia(sucursal, fecha_txt):
    fecha, etiqueta = resolver_fecha(fecha_txt)
    if fecha is None:
        return None, None, f"❌ No entendí la fecha '{fecha_txt}'. Usa: hoy, ayer o AAAA-MM-DD."
    dia = fecha.strftime("%Y-%m-%d")
    dia_sig = (fecha + timedelta(days=1)).strftime("%Y-%m-%d")
    domain = [["date_order", ">=", dia + " 06:00:00"],
              ["date_order", "<", dia_sig + " 06:00:00"],
              ["state", "in", ["paid", "done", "invoiced"]]]
    if sucursal:
        domain.append(["session_id.config_id.name", "ilike", sucursal])
    orders = odoo("pos.order", "search_read", [domain],
                  {"fields": ["name", "amount_total", "session_id"], "limit": 500})
    return orders, (etiqueta, dia), None

def corte_ventas(sucursal="", fecha_txt=""):
    orders, info, err = _ordenes_del_dia(sucursal, fecha_txt)
    if err:
        return err
    etiqueta, dia = info
    donde = f" en {sucursal}" if sucursal else ""
    if not orders:
        return f"📊 Sin ventas registradas{donde} ({etiqueta}, {dia})."
    total = sum(o["amount_total"] for o in orders)

    if not sucursal:
        sesiones_ids = list({o["session_id"][0] for o in orders if o.get("session_id")})
        sesiones = odoo("pos.session", "search_read",
                        [[["id", "in", sesiones_ids]]],
                        {"fields": ["id", "config_id"]})
        mapa = {s["id"]: s["config_id"][1].replace(" (no usado)", "") for s in sesiones if s.get("config_id")}
        por_suc = {}
        for o in orders:
            nombre_suc = mapa.get(o["session_id"][0], "Otra") if o.get("session_id") else "Otra"
            t, m = por_suc.get(nombre_suc, (0, 0.0))
            por_suc[nombre_suc] = (t + 1, m + o["amount_total"])
        lineas = [f"📊 Ventas de {etiqueta} ({dia}) — TODAS las sucursales:"]
        for nombre_suc, (t, m) in sorted(por_suc.items()):
            lineas.append(f"🏪 {nombre_suc}: {t} tickets — ${m:,.2f}")
        lineas.append(f"💰 TOTAL: {len(orders)} tickets — ${total:,.2f} MXN")
        return "\n".join(lineas)

    return f"📊 Ventas de {etiqueta} ({dia}){donde}:\n🧾 {len(orders)} tickets\n💰 Total: ${total:,.2f} MXN"

def detalle_ventas(sucursal="", fecha_txt=""):
    orders, info, err = _ordenes_del_dia(sucursal, fecha_txt)
    if err:
        return err
    etiqueta, dia = info
    donde = f" en {sucursal}" if sucursal else ""
    if not orders:
        return f"📋 Sin ventas registradas{donde} ({etiqueta}, {dia})."
    order_ids = [o["id"] for o in orders]
    lineas_odoo = odoo("pos.order.line", "search_read",
                       [[["order_id", "in", order_ids]]],
                       {"fields": ["product_id", "qty", "price_subtotal_incl"], "limit": 1000})
    por_prod = {}
    for l in lineas_odoo:
        nombre = l["product_id"][1] if l.get("product_id") else "Sin nombre"
        q, m = por_prod.get(nombre, (0.0, 0.0))
        por_prod[nombre] = (q + l["qty"], m + l.get("price_subtotal_incl", 0.0))
    salida = [f"📋 Servicios/productos vendidos {etiqueta} ({dia}){donde}:"]
    for nombre, (q, m) in sorted(por_prod.items(), key=lambda x: -x[1][1]):
        cant = f"{q:.0f}" if q == int(q) else f"{q:.1f}"
        salida.append(f"• {cant}× {nombre} — ${m:,.2f}")
    salida.append(f"💰 Total: ${sum(m for _, m in por_prod.values()):,.2f} MXN")
    texto = "\n".join(salida)
    if len(texto) > 4000:
        texto = texto[:3950] + "\n… (lista recortada, pide por sucursal)"
    return texto

def registrar_movimiento(cantidad, producto, sucursal=""):
    prods = odoo("product.product", "search_read",
                 [[["name", "ilike", producto]]],
                 {"fields": ["id", "name", "uom_id"], "limit": 5})
    if not prods:
        return f"❌ No encontré el producto '{producto}'."
    if len(prods) > 1:
        nombres = ", ".join(p["name"] for p in prods)
        return f"⚠️ Encontré varios: {nombres}. Sé más específico."
    p = prods[0]
    dom_src = [["usage", "=", "internal"]]
    if sucursal:
        dom_src.append(["name", "ilike", sucursal])
    else:
        dom_src.append(["complete_name", "ilike", "WH/Stock"])
    src = odoo("stock.location", "search_read", [dom_src],
               {"fields": ["id", "complete_name"], "limit": 1})
    dst = odoo("stock.location", "search_read",
               [[["usage", "=", "inventory"]]],
               {"fields": ["id", "complete_name"], "limit": 1})
    if not src or not dst:
        return "❌ No encontré las ubicaciones de origen/destino en Odoo."
    move_id = odoo("stock.move", "create", [{
        "name": f"Uso en cabina: {p['name']}",
        "product_id": p["id"],
        "product_uom_qty": float(cantidad),
        "product_uom": p["uom_id"][0],
        "location_id": src[0]["id"],
        "location_dest_id": dst[0]["id"],
    }])
    odoo("stock.move", "action_done", [[move_id]])
    return f"✅ Desconté {cantidad} de '{p['name']}' desde {src[0]['complete_name']}."

def buscar_cliente(nombre):
    partners = odoo("res.partner", "search_read",
                    [[["name", "ilike", nombre], ["customer", "=", True]]],
                    {"fields": ["name", "phone", "mobile"], "limit": 5})
    if not partners:
        return f"❌ No encontré clientes con '{nombre}'."
    return "👤 Clientes:\n" + "\n".join(
        f"• {c['name']} — {c.get('phone') or c.get('mobile') or 'sin teléfono'}"
        for c in partners)

PENDIENTES_TXT = """📌 PENDIENTES:

🏪 Sistema del salón (en orden):
1. Categorías 2. Catálogo de productos 3. Precios y costos 4. Catálogo de servicios 5. Clientes 6. Reglas de comisiones 7. Cliente frecuente 8. Ficha en el sistema 9. Videos 10. Capacitación de recepción

🗄️ Odoo: inventario de tintes Zaragoza; poner en cero tintes Terranova

👤 Personal: firma contrato alquiler ($9,000 + $750); revisar faltantes con perito; carta del perito (Claude); internet casa (fast.com + mesh); TikTok (CapCut); apps (estados de cuenta Nu, inventario químicos)"""

def reporte_matutino():
    try:
        ventas = corte_ventas("", "ayer")
    except Exception as e:
        ventas = f"❌ No pude consultar ventas: {e}"
    hoy = ahora_mx().strftime("%d/%m/%Y")
    return f"☀️ Buenos días, Israel — {hoy}\n\n{ventas}\n\n{PENDIENTES_TXT}"

SYSTEM = """Eres el agente personal de Israel Becerril (Salón Alika, sucursales Zaragoza y Juriquilla; barbería CattleDogs; ERP Odoo).

REGLA #1 — COMANDOS ODOO (TIENE PRIORIDAD SOBRE TODO):
Cuando el usuario pida datos de Odoo, NO respondas con texto normal. Responde ÚNICAMENTE con una línea de comando:

- Inventario/existencias → BUSCAR: <termino>
- Ventas totales, corte, cobros, cuánto vendí → CORTE: <sucursal o vacío> | <hoy, ayer o AAAA-MM-DD>
- Detalle de QUÉ se vendió (servicios, productos, tickets desglosados) → DETALLE: <sucursal o vacío> | <hoy, ayer o AAAA-MM-DD>
- Registrar uso/salida de producto → MOVI: <cantidad> | <producto> | <sucursal o vacío>
- Datos de un cliente → CLIENTE: <nombre>

Ejemplos (copia el formato exacto):
Usuario: "cuánto vendí hoy" → CORTE: | hoy
Usuario: "cuánto vendí ayer en total y por sucursal" → CORTE: | ayer
Usuario: "cuánto vendí ayer en Zaragoza" → CORTE: Zaragoza | ayer
Usuario: "desglosa los tickets de ayer" → DETALLE: | ayer
Usuario: "qué servicios se vendieron ayer" → DETALLE: | ayer
Usuario: "qué se vendió hoy en Juriquilla" → DETALLE: Juriquilla | hoy
Usuario: "cuántos tintes Hidracolor hay" → BUSCAR: Hidracolor
Usuario: "usé 1 tinte 4.52" → MOVI: 1 | Hidracolor 4.52 |
Usuario: "búscame al cliente María" → CLIENTE: María

NUNCA inventes datos de Odoo. NUNCA digas "no tengo acceso": SÍ tienes acceso vía comandos. Si la pregunta menciona ventas, servicios vendidos, tickets, inventario, productos o clientes, SIEMPRE emite el comando. Usa CORTE para totales y DETALLE cuando pidan desglose, servicios o qué se vendió.

REGLA #2 — AGENDA:
Eres el índice de pendientes de Israel. Cuando pregunte "qué tengo pendiente", clasifica y lista por proyecto.

Sistema del salón (en orden): 1. Categorías 2. Catálogo de productos 3. Precios y costos 4. Catálogo de servicios 5. Clientes 6. Reglas de comisiones 7. Cliente frecuente 8. Ficha en el sistema 9. Videos 10. Capacitación de recepción.

Odoo: terminar inventario de tintes Zaragoza; poner en cero tintes Terranova.

Pendientes personales: firma de contrato de alquiler ($9,000 + $750 mantenimiento); revisar con perito los faltantes; revisar carta del perito (la redacta Claude); internet casa (prueba fast.com + comprar mesh); TikTok (primer video en CapCut); app estados de cuenta (parser Nu); app inventario de químicos.

Proyecto bot: Telegram @MiAgenteKimi2026_bot conectado a agenda y Odoo. Después: agente en app de citas y pasarela de pagos.

Responde en español, directo y conciso. Si la pregunta es fuera de Odoo, responde normal."""

def ejecutar_comando(texto):
    for linea in texto.strip().splitlines():
        linea = linea.strip()
        if linea.upper().startswith("BUSCAR:"):
            return consultar_inventario(linea.split(":", 1)[1].strip())
        if linea.upper().startswith("CORTE:"):
            partes = [x.strip() for x in linea.split(":", 1)[1].split("|")]
            sucursal = partes[0] if partes else ""
            fecha = partes[1] if len(partes) > 1 and partes[1] else "hoy"
            return corte_ventas(sucursal, fecha)
        if linea.upper().startswith("DETALLE:"):
            partes = [x.strip() for x in linea.split(":", 1)[1].split("|")]
            sucursal = partes[0] if partes else ""
            fecha = partes[1] if len(partes) > 1 and partes[1] else "hoy"
            return detalle_ventas(sucursal, fecha)
        if linea.upper().startswith("MOVI:"):
            partes = [x.strip() for x in linea.split(":", 1)[1].split("|")]
            if len(partes) < 2:
                return "⚠️ Formato de movimiento incompleto."
            return registrar_movimiento(partes[0], partes[1], partes[2] if len(partes) > 2 else "")
        if linea.upper().startswith("CLIENTE:"):
            return buscar_cliente(linea.split(":", 1)[1].strip())
    return None

def responder(chat_id, texto_usuario):
    msgs = historial.setdefault(chat_id, [{"role": "system", "content": SYSTEM}])
    msgs.append({"role": "user", "content": texto_usuario})
    r = client.chat.completions.create(model="kimi-k2.6", messages=[msgs[0]] + msgs[-10:])
    contenido = r.choices[0].message.content
    print("LLM respondió:", contenido)
    resultado = ejecutar_comando(contenido)
    if resultado is not None:
        msgs.append({"role": "assistant", "content": contenido})
        return resultado
    msgs.append({"role": "assistant", "content": contenido})
    return contenido

async def enviar_reporte(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    try:
        texto = await asyncio.to_thread(reporte_matutino)
    except Exception as e:
        texto = f"☀️ Buenos días, Israel. (No pude armar el reporte: {e})"
    await context.bot.send_message(chat_id=chat_id, text=texto)

def programar_reporte(app, chat_id):
    nombre = f"reporte_{chat_id}"
    for job in app.job_queue.get_jobs_by_name(nombre):
        job.schedule_removal()
    # 9:00 AM México = 15:00 UTC
    app.job_queue.run_daily(enviar_reporte, time=time(15, 0), data=chat_id, name=nombre)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    programar_reporte(context.application, update.effective_chat.id)
    await update.message.reply_text(
        "Listo, Israel. Soy tu agente: agenda + Odoo. ¿Qué revisamos?\n\n"
        "☀️ Además: a partir de mañana te mando el reporte de ventas + pendientes cada mañana a las 9:00 AM."
    )

async def mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    programar_reporte(context.application, chat_id)  # asegura que el reporte quede activo
    aviso = await update.message.reply_text("⏳ Consultando...")
    try:
        respuesta = await asyncio.to_thread(responder, chat_id, update.message.text)
    except Exception as e:
        respuesta = f"❌ Error al consultar: {e}"
    await aviso.edit_text(respuesta)

app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje))
app.run_polling()



