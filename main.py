import os
import xmlrpc.client
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
KIMI_API_KEY = os.environ["KIMI_API_KEY"]
ODOO_URL = os.environ["ODOO_URL"]
ODOO_DB = os.environ["ODOO_DB"]
ODOO_USER = os.environ["ODOO_USER"]
ODOO_PASS = os.environ["ODOO_PASS"]

client = OpenAI(api_key=KIMI_API_KEY, base_url="https://api.moonshot.ai/v1")

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)
_uid = {}

def odoo_uid():
    if "uid" not in _uid:
        _uid["uid"] = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
    return _uid["uid"]

def odoo(model, method, args, kwargs=None):
    return models.execute_kw(ODOO_DB, odoo_uid(), ODOO_PASS, model, method, args, kwargs or {})

def consultar_inventario(termino):
    prods = odoo("product.product", "search_read",
                 [[["name", "ilike", termino]]],
                 {"fields": ["name", "qty_available"], "limit": 10})
    if not prods:
        return f"No encontre productos con: {termino}"
    lineas = []
    for p in prods:
        grupos = odoo("stock.quant", "read_group",
                      [[["product_id", "=", p["id"]], ["qty", ">", 0]],
                       ["qty", "location_id"], ["location_id"]])
        detalle = ", ".join(f"{g['location_id'][1]}: {g['qty']:g}" for g in grupos) or "sin existencias"
        lineas.append(f"{p['name']} | total {p['qty_available']:g} | {detalle}")
    return "\n".join(lineas)

SYSTEM = """Eres el agente personal de Israel Becerril. Respondes en espanol, claro y directo.

SUS NEGOCIOS:
- Salon de belleza Alika (sucursales Juriquilla y Zaragoza; Terranova cerro)
- Barberia Cattledogs
- Paginas: alika.com.mx y cattledogs (WordPress)
- App de citas: citas-salon.vercel.app
- ERP: Odoo 8 con sucursales Zaragoza, Juriquilla y Terranova

PENDIENTES ACTUALES:
SISTEMA DEL SALON (en orden): 1) revisar categorias 2) catalogo de productos 3) precios y costos 4) catalogo de servicios 5) clientes 6) reglas de comisiones 7) cliente frecuente 8) ficha en el sistema 9) videos de productos y servicios 10) capacitacion de recepcion.
ODOO: terminar inventario de tintes Zaragoza (por familias; fracciones como 1.5 y 1.25 van como texto); verificar que Terranova quedo en 0 y su producto paso a Zaragoza.
PERSONALES: firma de contrato de alquiler; revisar con perito los faltantes; revisar carta del perito (la redacta Claude); internet de la casa (prueba fast.com y comprar sistema mesh).
TIKTOK: descargar CapCut gratis y hacer primer video de prueba.
APP ESTADOS DE CUENTA: arreglar parser de Nu y clasificacion de gastos por rubro.
APP INVENTARIO QUIMICOS: construir app Android con Google Sheets.

REGLA ESPECIAL DE INVENTARIO:
Si el usuario pregunta por inventario, existencias, stock, o cuantos productos/tintes hay de algo, NO inventes la respuesta. Responde UNICAMENTE con:
BUSCAR: <nombre del producto o familia a buscar>
Ejemplo: si pregunta "cuantos tintes redken 07T tengo", respondes: BUSCAR: Redken 07T
Para cualquier otro tema responde normal."""

historial = {}

async def iniciar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hola Israel! Soy tu agente personal. En que te ayudo?")

async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    texto = update.message.text
    msgs = historial.setdefault(chat_id, [{"role": "system", "content": SYSTEM}])
    msgs.append({"role": "user", "content": texto})
    try:
        r = client.chat.completions.create(model="kimi-k2.6", messages=msgs[-20:])
        respuesta = r.choices[0].message.content
        if respuesta.strip().upper().startswith("BUSCAR:"):
            termino = respuesta.split(":", 1)[1].strip()
            await update.message.reply_text(f"Consultando Odoo: {termino}...")
            try:
                datos = consultar_inventario(termino)
            except Exception as e:
                datos = f"No pude conectarme a Odoo: {e}"
            msgs.append({"role": "user", "content": f"Resultados reales del inventario:\n{datos}\n\nRespondeme con estos datos, claro y breve."})
            r2 = client.chat.completions.create(model="kimi-k2.6", messages=msgs[-20:])
            respuesta = r2.choices[0].message.content
    except Exception as e:
        respuesta = f"Error: {e}"
    msgs.append({"role": "assistant", "content": respuesta})
    await update.message.reply_text(respuesta)

print("Bot iniciado, esperando mensajes...")
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler("start", iniciar))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, responder))
app.run_polling()
