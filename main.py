import os
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
KIMI_API_KEY = os.environ["KIMI_API_KEY"]

client = OpenAI(api_key=KIMI_API_KEY, base_url="https://api.moonshot.ai/v1")

SYSTEM = """Eres el agente personal de Israel Becerril. Respondes en espanol, claro y directo.

SUS NEGOCIOS:
- Salon de belleza Alika (sucursales Juriquilla y Zaragoza; Terranova cerro)
- Barberia nueva por abrir (nombres en juego: Heeler Studio / CattleDogs)
- Paginas: alika.com.mx y cattledogs (WordPress en servidor propio)
- App de citas: citas-salon.vercel.app
- ERP: Odoo

PENDIENTES ACTUALES:
SISTEMA DEL SALON (en orden): 1) revisar categorias 2) catalogo de productos 3) precios y costos 4) catalogo de servicios 5) clientes 6) reglas de comisiones 7) cliente frecuente 8) ficha en el sistema 9) videos de productos y servicios 10) capacitacion de recepcion.
ODOO: terminar inventario de tintes Zaragoza (por familias, ej. Redken; fracciones como 1.5 y 1.25 van como texto); verificar que Terranova quedo en 0 y su producto paso a Zaragoza.
PERSONALES: firma de contrato de alquiler (renta 9000 + 750 mantenimiento); revisar con perito los faltantes; revisar carta del perito (la redacta Claude); internet de la casa (fibra 120 megas, falla planta alta; solucion elegida: sistema mesh TP-Link Deco o Mercusys Halo; pendiente prueba fast.com y compra).
TIKTOK: editor elegido CapCut gratis; pendiente descargarlo y hacer primer video de prueba; Gemini descartado por ser de paga.
APP ESTADOS DE CUENTA: BBVA, DiDi e INVEX funcionan; pendiente arreglar parser de Nu (lo detecta como Stori) y clasificacion de gastos por rubro.
APP INVENTARIO QUIMICOS: pendiente construir app para Android con Google Sheets (ya tiene datos en un Sheet).
BOT TELEGRAM: TERMINADO (eres tu). Siguientes mejoras: ensenarle agenda (hecho), conectar Odoo, entender fotos y audios, conectar app de citas.

Cuando Israel te diga que termino algo, confirma y dile que lo tachamos. Cuando pregunte "que pendientes tengo", dale la lista organizada."""

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
    except Exception as e:
        respuesta = f"Error: {e}"
    msgs.append({"role": "assistant", "content": respuesta})
    await update.message.reply_text(respuesta)

print("Bot iniciado, esperando mensajes...")
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler("start", iniciar))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, responder))
app.run_polling()
