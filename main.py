"""
Bot Telegram @MiAgenteKimi2026_bot — Contador de Alika / CattleDogs
Reconstruido con sistema de pendientes persistente.
"""
import os
import json
import xmlrpc.client
import asyncio
import logging
from datetime import datetime, timedelta, time as dt_time
from typing import List, Dict, Any

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
import httpx

# ─── Configuración ───────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
KIMI_API_KEY   = os.environ.get("KIMI_API_KEY", "")
KIMI_BASE_URL  = "https://api.moonshot.ai/v1"
KIMI_MODEL     = "kimi-k2.6"

ODOO_URL  = "http://190.92.179.134:8077"
ODOO_DB   = "alika_salon"
ODOO_USER = "israel.becerril@alika.com.mx"
ODOO_PASS = "Alika5835"

OWNER_CHAT_ID = 1000342482

PENDIENTES_FILE = "pendientes.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Persistencia de pendientes ──────────────────────────────────────────────
def cargar_pendientes() -> List[Dict[str, Any]]:
    try:
        with open(PENDIENTES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def guardar_pendientes(pendientes: List[Dict[str, Any]]):
    with open(PENDIENTES_FILE, "w", encoding="utf-8") as f:
        json.dump(pendientes, f, ensure_ascii=False, indent=2)


# ─── Odoo helpers ────────────────────────────────────────────────────────────
def odoo_auth() -> tuple:
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


def rango_utc(fecha_str: str) -> tuple:
    """
    Convierte fecha local MX (UTC-6) a rango UTC.
    Día local D = ventana UTC de D 06:00 a D+1 06:00.
    """
    if fecha_str.lower() == "hoy":
        fecha_local = datetime.now()
    elif fecha_str.lower() == "ayer":
        fecha_local = datetime.now() - timedelta(days=1)
    else:
        fecha_local = datetime.strptime(fecha_str, "%Y-%m-%d")

    inicio_local = datetime(fecha_local.year, fecha_local.month, fecha_local.day, 0, 0, 0)
    fin_local = inicio_local + timedelta(days=1)

    inicio_utc = inicio_local + timedelta(hours=6)
    fin_utc = fin_local + timedelta(hours=6)

    return inicio_utc.strftime("%Y-%m-%d %H:%M:%S"), fin_utc.strftime("%Y-%m-%d %H:%M:%S")


def consultar_ventas(fecha_str: str) -> str:
    try:
        uid, models = odoo_auth()
        inicio, fin = rango_utc(fecha_str)

        domain = [
            ("date_order", ">=", inicio),
            ("date_order", "<", fin),
            ("state", "in", ["paid", "done", "invoiced"]),
        ]
        orders = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            "pos.order", "search_read",
            [domain],
            {"fields": ["name", "amount_total", "config_id", "date_order"]}
        )

        if not orders:
            return f"📭 No hay ventas registradas para {fecha_str}."

        total = sum(o["amount_total"] for o in orders)
        por_sucursal: Dict[str, float] = {}
        for o in orders:
            suc = o.get("config_id")
            suc_name = suc[1] if isinstance(suc, list) else str(suc)
            suc_name = suc_name.replace(" (no usado)", "")
            por_sucursal[suc_name] = por_sucursal.get(suc_name, 0) + o["amount_total"]

        lineas = [f"💰 *Ventas {fecha_str}*", f"Total: ${total:,.2f}", ""]
        for suc, monto in por_sucursal.items():
            lineas.append(f"• {suc}: ${monto:,.2f}")
        lineas.append(f"\n🎫 Tickets: {len(orders)}")
        return "\n".join(lineas)
    except Exception as e:
        logger.error(f"Error ventas: {e}")
        return f"❌ Error consultando ventas: {e}"


def consultar_detalle(fecha_str: str) -> str:
    try:
        uid, models = odoo_auth()
        inicio, fin = rango_utc(fecha_str)

        domain = [
            ("order_id.date_order", ">=", inicio),
            ("order_id.date_order", "<", fin),
            ("order_id.state", "in", ["paid", "done", "invoiced"]),
        ]
        lines = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            "pos.order.line", "search_read",
            [domain],
            {"fields": ["product_id", "price_subtotal_incl", "qty", "order_id"]}
        )

        if not lines:
            return f"📭 No hay detalle de ventas para {fecha_str}."

        productos: Dict[str, Dict[str, Any]] = {}
        for l in lines:
            prod = l.get("product_id")
            name = prod[1] if isinstance(prod, list) else str(prod)
            if name not in productos:
                productos[name] = {"cantidad": 0, "total": 0.0}
            productos[name]["cantidad"] += l.get("qty", 1)
            productos[name]["total"] += l.get("price_subtotal_incl", 0)

        lineas = [f"📋 *Detalle {fecha_str}*", ""]
        for name, data in sorted(productos.items(), key=lambda x: -x[1]["total"]):
            lineas.append(f"• {name}: {data['cantidad']} pz — ${data['total']:,.2f}")
        return "\n".join(lineas)
    except Exception as e:
        logger.error(f"Error detalle: {e}")
        return f"❌ Error consultando detalle: {e}"


def consultar_inventario(q: str) -> str:
    try:
        uid, models = odoo_auth()
        domain = [("name", "ilike", q)]
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            "product.product", "search_read",
            [domain],
            {"fields": ["name", "qty_available", "list_price"], "limit": 20}
        )
        if not products:
            return f"📭 No encontré productos con '{q}'."
        lineas = [f"📦 *Inventario: {q}*", ""]
        for p in products:
            lineas.append(f"• {p['name']}: {p.get('qty_available', 0)} uds — ${p.get('list_price', 0):,.2f}")
        return "\n".join(lineas)
    except Exception as e:
        logger.error(f"Error inventario: {e}")
        return f"❌ Error inventario: {e}"


def consultar_cliente(q: str) -> str:
    try:
        uid, models = odoo_auth()
        domain = ["|", ("name", "ilike", q), ("phone", "ilike", q)]
        clients = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            "res.partner", "search_read",
            [domain],
            {"fields": ["name", "phone", "email"], "limit": 10}
        )
        if not clients:
            return f"📭 No encontré clientes con '{q}'."
        lineas = [f"👤 *Clientes: {q}*", ""]
        for c in clients:
            lineas.append(f"• {c['name']} | {c.get('phone', 'N/A')} | {c.get('email', 'N/A')}")
        return "\n".join(lineas)
    except Exception as e:
        logger.error(f"Error cliente: {e}")
        return f"❌ Error cliente: {e}"


# ─── Kimi LLM ────────────────────────────────────────────────────────────────
async def llamar_kimi(user_msg: str, context_history: List[Dict] = None) -> str:
    system_msg = {
        "role": "system",
        "content": (
            "Eres el asistente contable de Israel Becerril, dueño de Salón Alika "
            "(Zaragoza y Juriquilla, Querétaro) y Barbería CattleDogs. "
            "Respondes en español, directo, con números exactos. "
            "Si no sabes algo, dilo. Nunca inventes datos. "
            "Reglas: solo lectura en Odoo excepto movimientos de inventario con confirmación. "
            "Nunca borres/renombres productos, clientes ni categorías. "
            "Zona horaria: México UTC-6."
        ),
    }

    msgs = [system_msg]
    if context_history:
        msgs.extend(context_history)
    msgs.append({"role": "user", "content": user_msg})

    # CRÍTICO: mantener system + últimos 10 mensajes
    msgs_truncados = [msgs[0]] + msgs[-10:]

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{KIMI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {KIMI_API_KEY}"},
            json={
                "model": KIMI_MODEL,
                "messages": msgs_truncados,
                "temperature": 1,  # FIJO: otra temperatura da error 400
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


# ─── Handlers de Telegram ────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *MiAgenteKimi2026* listo.\n"
        "Comandos: CORTE | DETALLE | BUSCAR | CLIENTE | MOVI | PENDIENTES",
        parse_mode="Markdown",
    )


async def cmd_corte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    fecha = args[0] if args else "hoy"
    texto = consultar_ventas(fecha)
    await update.message.reply_text(texto, parse_mode="Markdown")


async def cmd_detalle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    fecha = args[0] if args else "hoy"
    texto = consultar_detalle(fecha)
    await update.message.reply_text(texto, parse_mode="Markdown")


async def cmd_buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = " ".join(context.args)
    if not q:
        await update.message.reply_text("Uso: BUSCAR <nombre del producto>")
        return
    texto = consultar_inventario(q)
    await update.message.reply_text(texto, parse_mode="Markdown")


async def cmd_cliente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = " ".join(context.args)
    if not q:
        await update.message.reply_text("Uso: CLIENTE <nombre o teléfono>")
        return
    texto = consultar_cliente(q)
    await update.message.reply_text(texto, parse_mode="Markdown")


# ─── MOVI (movimientos de inventario) — solo con confirmación ────────────────
MOVI_WAIT_CONFIRM = 1

async def cmd_movi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Uso: MOVI <producto_id> <ubicación_destino_id> <cantidad>\n"
            "Ejemplo: MOVI 45 12 5"
        )
        return
    context.user_data["movi"] = {
        "product_id": int(args[0]),
        "location_dest_id": int(args[1]),
        "product_uom_qty": float(args[2]),
    }
    await update.message.reply_text(
        f"⚠️ ¿Registrar movimiento?\n"
        f"Producto: {args[0]} | Destino: {args[1]} | Cantidad: {args[2]}\n"
        f"Responde: *sí* para confirmar o *no* para cancelar.",
        parse_mode="Markdown",
    )
    return MOVI_WAIT_CONFIRM


async def movi_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip().lower()
    if texto not in ("sí", "si", "yes"):
        await update.message.reply_text("❌ Movimiento cancelado.")
        return ConversationHandler.END

    datos = context.user_data.get("movi")
    if not datos:
        await update.message.reply_text("❌ No hay movimiento pendiente.")
        return ConversationHandler.END

    try:
        uid, models = odoo_auth()
        move_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            "stock.move", "create", [[{
                "name": "Movimiento desde bot",
                "product_id": datos["product_id"],
                "location_id": 1,
                "location_dest_id": datos["location_dest_id"],
                "product_uom_qty": datos["product_uom_qty"],
                "product_uom": 1,
            }]]
        )
        models.execute_kw(ODOO_DB, uid, ODOO_PASS, "stock.move", "action_done", [[move_id]])
        await update.message.reply_text(f"✅ Movimiento registrado (ID {move_id}).")
    except Exception as e:
        logger.error(f"Error MOVI: {e}")
        await update.message.reply_text(f"❌ Error registrando movimiento: {e}")

    return ConversationHandler.END


async def movi_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Movimiento cancelado.")
    return ConversationHandler.END


# ─── SISTEMA DE PENDIENTES ───────────────────────────────────────────────────
async def cmd_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista los pendientes numerados."""
    lista = cargar_pendientes()
    if not lista:
        await update.message.reply_text("📭 No tienes pendientes activos.")
        return

    lineas = ["📝 *Tus pendientes:*", ""]
    for i, p in enumerate(lista, 1):
        estado = "✅" if p.get("hecho") else "⬜"
        fecha = p.get("creado", "")[:10]
        lineas.append(f"{estado} *{i}.* {p['texto']} _(creado {fecha})_")
    lineas.append("\nPara quitar uno: `QUITAR <número>` o `YA TERMINÉ <número>`")
    await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")


async def cmd_agrega_pendiente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Agrega un nuevo pendiente."""
    texto = " ".join(context.args)
    if not texto:
        await update.message.reply_text("Uso: AGREGA <texto del pendiente>")
        return

    lista = cargar_pendientes()
    lista.append({
        "texto": texto,
        "creado": datetime.now().isoformat(),
        "hecho": False,
    })
    guardar_pendientes(lista)
    await update.message.reply_text(f"✅ Pendiente agregado: *{texto}*", parse_mode="Markdown")


async def cmd_quitar_pendiente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quita un pendiente por número."""
    args = context.args
    if not args:
        await update.message.reply_text("Uso: QUITAR <número del pendiente>")
        return

    try:
        num = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Escribe un número. Ejemplo: QUITAR 2")
        return

    lista = cargar_pendientes()
    if num < 1 or num > len(lista):
        await update.message.reply_text(f"❌ No existe el pendiente {num}. Tienes {len(lista)} pendientes.")
        return

    eliminado = lista.pop(num - 1)
    guardar_pendientes(lista)
    await update.message.reply_text(
        f"🗑️ Pendiente *{num}* eliminado: _{eliminado['texto']}_\n"
        f"Te quedan {len(lista)} pendientes.",
        parse_mode="Markdown",
    )


async def cmd_ya_termine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Marca un pendiente como terminado (o lo quita)."""
    args = context.args
    if not args:
        await update.message.reply_text("Uso: YA TERMINÉ <número del pendiente>")
        return

    try:
        num = int(args[0])
    except ValueError:
        # Intentar buscar por texto
        texto_buscar = " ".join(args).lower()
        lista = cargar_pendientes()
        for i, p in enumerate(lista):
            if texto_buscar in p["texto"].lower():
                eliminado = lista.pop(i)
                guardar_pendientes(lista)
                await update.message.reply_text(
                    f"✅ Pendiente eliminado: _{eliminado['texto']}_",
                    parse_mode="Markdown",
                )
                return
        await update.message.reply_text("❌ No encontré ese pendiente.")
        return

    lista = cargar_pendientes()
    if num < 1 or num > len(lista):
        await update.message.reply_text(f"❌ No existe el pendiente {num}.")
        return

    eliminado = lista.pop(num - 1)
    guardar_pendientes(lista)
    await update.message.reply_text(
        f"✅ ¡Bien hecho! Pendiente *{num}* completado: _{eliminado['texto']}_\n"
        f"Te quedan {len(lista)} pendientes.",
        parse_mode="Markdown",
    )


# ─── Mensaje libre (pasa por Kimi) ───────────────────────────────────────────
async def mensaje_libre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    logger.info(f"Usuario dijo: {texto}")

    # Detectar comandos en texto libre
    upper = texto.strip().upper()
    palabras = texto.strip().split()

    if upper.startswith("CORTE") or upper.startswith("VENTAS"):
        fecha = palabras[1] if len(palabras) > 1 else "hoy"
        resp = consultar_ventas(fecha)
        await update.message.reply_text(resp, parse_mode="Markdown")
        return

    if upper.startswith("DETALLE"):
        fecha = palabras[1] if len(palabras) > 1 else "hoy"
        resp = consultar_detalle(fecha)
        await update.message.reply_text(resp, parse_mode="Markdown")
        return

    if upper.startswith("BUSCAR "):
        q = " ".join(palabras[1:])
        resp = consultar_inventario(q)
        await update.message.reply_text(resp, parse_mode="Markdown")
        return

    if upper.startswith("CLIENTE "):
        q = " ".join(palabras[1:])
        resp = consultar_cliente(q)
        await update.message.reply_text(resp, parse_mode="Markdown")
        return

    if upper.startswith("PENDIENTES") or upper.startswith("AGENDA") or upper.startswith("MIS PENDIENTES"):
        await cmd_pendientes(update, context)
        return

    if upper.startswith("AGREGA ") or upper.startswith("AGREGAR "):
        q = " ".join(palabras[1:])
        context.args = palabras[1:]
        await cmd_agrega_pendiente(update, context)
        return

    if upper.startswith("QUITAR "):
        context.args = palabras[1:]
        await cmd_quitar_pendiente(update, context)
        return

    if upper.startswith("YA TERMINÉ ") or upper.startswith("YA TERMINE ") or upper.startswith("LISTO "):
        context.args = palabras[2:] if len(palabras) > 2 else []
        await cmd_ya_termine(update, context)
        return

    # Si no coincide con nada, pasar al LLM
    try:
        respuesta = await llamar_kimi(texto)
        logger.info(f"LLM respondió: {respuesta[:100]}")
        await update.message.reply_text(respuesta)
    except Exception as e:
        logger.error(f"Error LLM: {e}")
        await update.message.reply_text("❌ Error conectando con Kimi. Intenta de nuevo.")


# ─── Reporte automático 9:00 AM ──────────────────────────────────────────────
async def reporte_diario(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Enviando reporte diario 9 AM")
    ayer = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    texto = consultar_ventas(ayer)
    await context.bot.send_message(
        chat_id=OWNER_CHAT_ID,
        text=f"📊 *Reporte automático — {ayer}*\n\n{texto}",
        parse_mode="Markdown",
    )


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Borrar webhook por si quedó activo (evita Conflict)
    asyncio.get_event_loop().run_until_complete(application.bot.delete_webhook(drop_pending_updates=True))

    # Handlers de comandos con /
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("corte", cmd_corte))
    application.add_handler(CommandHandler("detalle", cmd_detalle))
    application.add_handler(CommandHandler("buscar", cmd_buscar))
    application.add_handler(CommandHandler("cliente", cmd_cliente))
    application.add_handler(CommandHandler("pendientes", cmd_pendientes))
    application.add_handler(CommandHandler("agrega", cmd_agrega_pendiente))
    application.add_handler(CommandHandler("quitar", cmd_quitar_pendiente))
    application.add_handler(CommandHandler("ya_termine", cmd_ya_termine))

    # Conversación MOVI
    movi_conv = ConversationHandler(
        entry_points=[CommandHandler("movi", cmd_movi)],
        states={
            MOVI_WAIT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, movi_confirm)],
        },
        fallbacks=[CommandHandler("cancelar", movi_cancel)],
    )
    application.add_handler(movi_conv)

    # Mensaje libre (sin /)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_libre))

    # JobQueue: reporte 9:00 AM hora México (UTC-6 → 15:00 UTC)
    job_queue = application.job_queue
    job_queue.run_daily(reporte_diario, time=dt_time(hour=15, minute=0))

    logger.info("Bot iniciado. Esperando mensajes...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
