import os
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
KIMI_API_KEY = os.environ["KIMI_API_KEY"]

client = OpenAI(api_key=KIMI_API_KEY, base_url="https://api.moonshot.ai/v1")

SYSTEM = ("Eres el asistente personal de Israel. Le ayudas con su agenda, "
          "su salon de belleza Alika, su barberia, inventarios y pendientes. "
          "Respondes en espanol, claro y directo.")

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
