"""
Bot Telegram @MiAgenteKimi2026_bot — Contador de Alika / CattleDogs
v4: MOVI amigable (por nombre), comparativo vs año pasado, cierre de caja
    por forma de pago, y fix de orden AGREGA vs PENDIENTES.
"""
import os
import json
import xmlrpc.client
import asyncio
import logging
import re
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

# Si Railway tiene un Volume montado en /data, úsalo (sobrevive a despliegues).
# Si no, usa el archivo local (se borra en cada deploy).
DATA_DIR = "/data" if os.path.isdir("/data") else "."
PENDIENTES_FILE = os.path.join(DATA_DIR, "pendientes.json")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Persistencia de pendientes ──────────────────────────────────────────────
def cargar_pendientes() -> Dict[str, List[Dict[str, Any]]]:
    """
    Carga las listas de pendientes: {"general": [...], "compras": [...], ...}
    Migra automáticamente el formato viejo (una sola lista) a "general".
    """
    try:
        with open(PENDIENTES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"general": []}

    if isinstance(data, list):  # formato viejo → migrar
        data = {"general": data}
    if "general" not in data:
        data["general"] = []
    return data


def guardar_pendientes(data: Dict[str, List[Dict[str, Any]]]):
    with open(PENDIENTES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalizar_lista(nombre: str) -> str:
    """
    Nombre de lista en minúsculas. Admite sub-listas con "/":
    "Casa / Despensa" → "casa/despensa"
    """
    partes = [" ".join(p.strip().split()) for p in nombre.strip().lower().split("/")]
    return "/".join(p for p in partes if p)


def listas_coincidentes(data: Dict[str, List[Dict[str, Any]]], nombre: str) -> List[str]:
    """
    Listas que coinciden con `nombre`: la lista exacta o sus sub-listas.
    "casa" → ["casa", "casa/despensa", "casa/mantenimiento"]
    """
    return sorted(k for k in data if k == nombre or k.startswith(nombre + "/"))


# ─── Odoo helpers ────────────────────────────────────────────────────────────
def odoo_auth() -> tuple:
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


def mapa_sucursales(models, uid, orders: List[Dict[str, Any]]) -> Dict[int, str]:
    """
    Odoo 8: pos.order NO tiene config_id; la sucursal se obtiene por
    session_id → pos.session.config_id. Devuelve {order_id: nombre_sucursal}.
    """
    ses_ids = list({o["session_id"][0] for o in orders if isinstance(o.get("session_id"), list)})
    mapa_ses: Dict[int, str] = {}
    if ses_ids:
        sessions = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            "pos.session", "read",
            [ses_ids],
            {"fields": ["config_id"]}
        )
        for s in sessions:
            cfg = s.get("config_id")
            nombre = cfg[1] if isinstance(cfg, list) else "?"
            mapa_ses[s["id"]] = nombre.replace(" (no usado)", "")
    resultado = {}
    for o in orders:
        ses = o.get("session_id")
        ses_id = ses[0] if isinstance(ses, list) else 0
        resultado[o["id"]] = mapa_ses.get(ses_id, "?")
    return resultado


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
            {"fields": ["name", "amount_total", "session_id", "date_order"]}
        )

        if not orders:
            return f"📭 No hay ventas registradas para {fecha_str}."

        sucursales = mapa_sucursales(models, uid, orders)
        total = sum(o["amount_total"] for o in orders)
        por_sucursal: Dict[str, float] = {}
        for o in orders:
            suc_name = sucursales.get(o["id"], "?")
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


def _ventas_en_rango(models, uid, inicio_utc: str, fin_utc: str) -> tuple:
    """Devuelve (total, num_tickets) de pos.order en un rango UTC."""
    domain = [
        ("date_order", ">=", inicio_utc),
        ("date_order", "<", fin_utc),
        ("state", "in", ["paid", "done", "invoiced"]),
    ]
    orders = models.execute_kw(
        ODOO_DB, uid, ODOO_PASS,
        "pos.order", "search_read",
        [domain],
        {"fields": ["amount_total"]}
    )
    return sum(o["amount_total"] for o in orders), len(orders)


def comparativo_mes() -> str:
    """Mes actual (al día de hoy) vs el mismo periodo del año pasado. MX = UTC-6."""
    try:
        uid, models = odoo_auth()
        hoy = datetime.now()

        ini_act = datetime(hoy.year, hoy.month, 1) + timedelta(hours=6)
        fin_act = datetime(hoy.year, hoy.month, hoy.day) + timedelta(days=1, hours=6)
        ini_ant = datetime(hoy.year - 1, hoy.month, 1) + timedelta(hours=6)
        fin_ant = datetime(hoy.year - 1, hoy.month, hoy.day) + timedelta(days=1, hours=6)

        fmt = "%Y-%m-%d %H:%M:%S"
        tot_act, tic_act = _ventas_en_rango(models, uid, ini_act.strftime(fmt), fin_act.strftime(fmt))
        tot_ant, tic_ant = _ventas_en_rango(models, uid, ini_ant.strftime(fmt), fin_ant.strftime(fmt))

        meses = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio",
                 "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
        nombre_mes = meses[hoy.month]

        lineas = [f"📊 *Comparativo {nombre_mes}* (del 1 al {hoy.day})", ""]
        lineas.append(f"• {hoy.year}: ${tot_act:,.2f} — {tic_act} tickets")
        lineas.append(f"• {hoy.year - 1}: ${tot_ant:,.2f} — {tic_ant} tickets")

        if tot_ant > 0:
            dif = ((tot_act - tot_ant) / tot_ant) * 100
            flecha = "📈" if dif >= 0 else "📉"
            lineas.append(f"\n{flecha} Diferencia: {dif:+.1f}% (${tot_act - tot_ant:+,.2f})")
        if tic_act > 0 and tic_ant > 0:
            lineas.append(f"🎫 Ticket promedio: ${tot_act / tic_act:,.2f} vs ${tot_ant / tic_ant:,.2f}")
        if tot_act == 0 and tot_ant == 0:
            lineas.append("\n📭 No hay ventas registradas en ninguno de los dos periodos.")
        return "\n".join(lineas)
    except Exception as e:
        logger.error(f"Error comparativo: {e}")
        return f"❌ Error en comparativo: {e}"


def cierre_caja(fecha_str: str) -> str:
    """Cierre de caja del día: totales por forma de pago y sucursal."""
    try:
        uid, models = odoo_auth()
        inicio, fin = rango_utc(fecha_str)

        # Odoo 8: no existe pos.payment. Los cobros van en
        # account.bank.statement.line ligados por pos.order.statement_ids
        domain = [
            ("date_order", ">=", inicio),
            ("date_order", "<", fin),
            ("state", "in", ["paid", "done", "invoiced"]),
        ]
        orders = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            "pos.order", "search_read",
            [domain],
            {"fields": ["statement_ids", "session_id"]}
        )
        if not orders:
            return f"📭 No hay ventas registradas para {fecha_str}."

        sucursales = mapa_sucursales(models, uid, orders)
        line_ids = []
        mapa_linea_suc = {}
        for o in orders:
            suc_name = sucursales.get(o["id"], "?")
            for lid in o.get("statement_ids", []):
                line_ids.append(lid)
                mapa_linea_suc[lid] = suc_name

        if not line_ids:
            return f"📭 No hay pagos registrados para {fecha_str}."

        lineas_pago = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            "account.bank.statement.line", "read",
            [line_ids],
            {"fields": ["amount", "journal_id"]}
        )

        por_metodo: Dict[str, float] = {}
        por_suc: Dict[str, Dict[str, float]] = {}
        total = 0.0
        for p in lineas_pago:
            met = p.get("journal_id")
            met_name = met[1] if isinstance(met, list) else "Otro"
            monto = p.get("amount", 0.0)
            suc = mapa_linea_suc.get(p["id"], "?")
            por_metodo[met_name] = por_metodo.get(met_name, 0) + monto
            por_suc.setdefault(suc, {})
            por_suc[suc][met_name] = por_suc[suc].get(met_name, 0) + monto
            total += monto

        lineas = [f"🧾 *Cierre de caja {fecha_str}*", f"Total cobrado: ${total:,.2f}", ""]
        lineas.append("*Por forma de pago:*")
        for met, monto in sorted(por_metodo.items(), key=lambda x: -x[1]):
            lineas.append(f"• {met}: ${monto:,.2f}")
        lineas.append("\n*Por sucursal:*")
        for suc, metodos in por_suc.items():
            lineas.append(f"🏪 {suc}: ${sum(metodos.values()):,.2f}")
            for met, monto in sorted(metodos.items(), key=lambda x: -x[1]):
                lineas.append(f"   ◦ {met}: ${monto:,.2f}")
        return "\n".join(lineas)
    except Exception as e:
        logger.error(f"Error cierre: {e}")
        return f"❌ Error en cierre de caja: {e}"


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
        "Comandos: CORTE | DETALLE | CIERRE | COMPARATIVO | BUSCAR | CLIENTE | MOVI | PENDIENTES | LISTAS\n"
        "Ejemplos: `Cierre hoy` · `Cómo voy vs el año pasado` · `Mueve 5 shampoo a Juriquilla` · `Agrega a compras: papel`",
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

def _buscar_producto_odoo(models, uid, nombre: str):
    """Busca un producto por nombre. Devuelve dict o None."""
    prods = models.execute_kw(
        ODOO_DB, uid, ODOO_PASS,
        "product.product", "search_read",
        [[("name", "ilike", nombre)]],
        {"fields": ["name", "uom_id", "qty_available"], "limit": 5}
    )
    return prods


def _buscar_ubicacion_odoo(models, uid, nombre: str):
    """Busca una ubicación interna por nombre (sucursal)."""
    locs = models.execute_kw(
        ODOO_DB, uid, ODOO_PASS,
        "stock.location", "search_read",
        [[("name", "ilike", nombre), ("usage", "=", "internal")]],
        {"fields": ["name"], "limit": 5}
    )
    return locs


async def cmd_movi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    MOVI amigable:  MOVI 5 shampoo a Juriquilla
    También acepta:  mueve 5 shampoo a Juriquilla / pasa 3 ceras a Zaragoza
    Modo viejo con IDs: MOVI 45 12 5
    """
    texto = " ".join(context.args) if context.args else ""

    # ─── Modo viejo: 3 números (producto_id ubicación_id cantidad) ───
    partes = texto.split()
    if len(partes) == 3 and all(p.replace(".", "").isdigit() for p in partes):
        context.user_data["movi"] = {
            "product_id": int(partes[0]),
            "location_dest_id": int(partes[1]),
            "product_uom_qty": float(partes[2]),
            "product_uom": 1,
            "desc": f"Producto ID {partes[0]} | Destino ID {partes[1]} | Cantidad: {partes[2]}",
        }
        await update.message.reply_text(
            f"⚠️ ¿Registrar movimiento?\n{context.user_data['movi']['desc']}\n"
            f"Responde: *sí* para confirmar o *no* para cancelar.",
            parse_mode="Markdown",
        )
        return MOVI_WAIT_CONFIRM

    # ─── Modo amigable: <cantidad> <producto> a <sucursal> ───
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s+(.+?)\s+(?:a|en|hacia)\s+(.+?)\s*$", texto, re.IGNORECASE)
    if not m:
        await update.message.reply_text(
            "Para mover inventario escribe:\n"
            "• `MOVI 5 shampoo a Juriquilla`\n"
            "• o con IDs: `MOVI 45 12 5`",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    cantidad = float(m.group(1))
    nombre_prod = m.group(2).strip()
    nombre_dest = m.group(3).strip()

    try:
        uid, models = odoo_auth()
    except Exception as e:
        await update.message.reply_text(f"❌ Error conectando a Odoo: {e}")
        return ConversationHandler.END

    prods = _buscar_producto_odoo(models, uid, nombre_prod)
    if not prods:
        await update.message.reply_text(f"📭 No encontré ningún producto con '{nombre_prod}'.")
        return ConversationHandler.END

    locs = _buscar_ubicacion_odoo(models, uid, nombre_dest)
    if not locs:
        await update.message.reply_text(f"📭 No encontré la ubicación '{nombre_dest}'.")
        return ConversationHandler.END

    prod = prods[0]
    loc = locs[0]
    aviso = ""
    if len(prods) > 1:
        otros = ", ".join(p["name"] for p in prods[1:4])
        aviso = f"\n(Encontré varios: usaré *{prod['name']}*. Otros: {otros})"

    context.user_data["movi"] = {
        "product_id": prod["id"],
        "location_dest_id": loc["id"],
        "product_uom_qty": cantidad,
        "product_uom": prod["uom_id"][0] if isinstance(prod.get("uom_id"), list) else 1,
        "desc": (f"Producto: *{prod['name']}* (hay {prod.get('qty_available', 0)} uds)\n"
                 f"Destino: *{loc['name']}*\nCantidad: *{cantidad}*"),
    }
    await update.message.reply_text(
        f"⚠️ ¿Lo registro?\n{context.user_data['movi']['desc']}{aviso}\n\n"
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
                "product_uom": datos.get("product_uom", 1),
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


async def cmd_comparativo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(comparativo_mes(), parse_mode="Markdown")


async def cmd_cierre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fecha = context.args[0] if context.args else "hoy"
    await update.message.reply_text(cierre_caja(fecha), parse_mode="Markdown")


# ─── SISTEMA DE PENDIENTES (con listas) ──────────────────────────────────────
def _resolver_lista_y_numero(args, data):
    """
    De args como ["compras","2"] o ["2"] devuelve (nombre_lista, numero, resto).
    Si el primer arg no es número, se toma como nombre de lista.
    """
    if not args:
        return None, None, []
    try:
        int(args[0])
        return "general", int(args[0]), args[1:]
    except ValueError:
        nombre = normalizar_lista(args[0])
        num = None
        resto = args[1:]
        if resto:
            try:
                num = int(resto[0])
                resto = resto[1:]
            except ValueError:
                pass
        return nombre, num, resto


async def cmd_listas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra todas las listas con su conteo de pendientes."""
    data = cargar_pendientes()
    lineas = ["🗂️ *Tus listas de pendientes:*", ""]
    for nombre, items in data.items():
        activos = sum(1 for p in items if not p.get("hecho"))
        if items or nombre == "general":
            lineas.append(f"• *{nombre}*: {activos} pendiente(s)")
    lineas.append("\nPara crear una: `NUEVA LISTA <nombre>`")
    lineas.append("Para ver una: `PENDIENTES <nombre>`")
    await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")


async def cmd_nueva_lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Crea una lista nueva."""
    nombre = normalizar_lista(" ".join(context.args))
    if not nombre:
        await update.message.reply_text("Uso: `NUEVA LISTA <nombre>`", parse_mode="Markdown")
        return
    data = cargar_pendientes()
    if nombre in data:
        await update.message.reply_text(f"ℹ️ La lista *{nombre}* ya existe.", parse_mode="Markdown")
        return
    data[nombre] = []
    guardar_pendientes(data)
    await update.message.reply_text(
        f"✅ Lista *{nombre}* creada.\n"
        f"Agrega algo así: `AGREGA A {nombre}: tu pendiente`",
        parse_mode="Markdown",
    )


async def cmd_borrar_lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Borra una lista completa con sus pendientes."""
    nombre = normalizar_lista(" ".join(context.args))
    if not nombre:
        await update.message.reply_text("Uso: `BORRAR LISTA <nombre>`", parse_mode="Markdown")
        return
    if nombre == "general":
        await update.message.reply_text("⚠️ La lista *general* no se puede borrar.", parse_mode="Markdown")
        return
    data = cargar_pendientes()
    coincide = listas_coincidentes(data, nombre)
    if not coincide:
        await update.message.reply_text(f"❌ No existe la lista *{nombre}*.", parse_mode="Markdown")
        return
    total = 0
    for nombre_l in coincide:
        total += len(data.pop(nombre_l, []))
    guardar_pendientes(data)
    if len(coincide) > 1:
        await update.message.reply_text(
            f"🗑️ Borradas {len(coincide)} listas de *{nombre}* ({', '.join(coincide)}) con {total} pendiente(s).",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"🗑️ Lista *{nombre}* borrada con {total} pendiente(s).",
            parse_mode="Markdown",
        )


async def cmd_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista pendientes: todos, o solo de una lista si se pasa su nombre."""
    data = cargar_pendientes()
    nombre = normalizar_lista(" ".join(context.args)) if context.args else ""

    if nombre:
        coincide = listas_coincidentes(data, nombre)
        if not coincide:
            await update.message.reply_text(f"❌ No existe la lista *{nombre}*.", parse_mode="Markdown")
            return
        lineas = []
        for nombre_l in coincide:
            items = data[nombre_l]
            if not items:
                continue
            lineas.append(f"🗂️ *{nombre_l.upper()}*")
            for i, p in enumerate(items, 1):
                estado = "✅" if p.get("hecho") else "⬜"
                fecha = p.get("creado", "")[:10]
                lineas.append(f"{estado} *{i}.* {p['texto']} _(creado {fecha})_")
            lineas.append("")
        if not lineas:
            await update.message.reply_text(f"📭 La lista *{nombre}* está vacía.", parse_mode="Markdown")
            return
        lineas.append(f"Para quitar uno: `QUITAR {coincide[0]} <número>`")
        await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")
        return

    # Todas las listas
    hay_algo = any(items for items in data.values())
    if not hay_algo:
        await update.message.reply_text("📭 No tienes pendientes activos.")
        return
    lineas = ["📝 *Tus pendientes:*"]
    for nombre_l, items in data.items():
        if not items:
            continue
        lineas.append(f"\n🗂️ *{nombre_l.upper()}*")
        for i, p in enumerate(items, 1):
            estado = "✅" if p.get("hecho") else "⬜"
            lineas.append(f"{estado} *{i}.* {p['texto']}")
    lineas.append("\nPara quitar: `QUITAR <lista> <número>` o `YA TERMINÉ <lista> <número>`")
    await update.message.reply_text("\n".join(lineas), parse_mode="Markdown")


async def cmd_agrega_pendiente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Agrega un pendiente.
    AGREGA <texto>              → lista general
    AGREGA A <lista>: <texto>   → esa lista (la crea si no existe)
    """
    texto = " ".join(context.args)
    if not texto:
        await update.message.reply_text(
            "Uso: `AGREGA <texto>` o `AGREGA A <lista>: <texto>`",
            parse_mode="Markdown",
        )
        return

    nombre = "general"
    m = re.match(r"(?i)^a\s+([^:]+?):\s*(.+)$", texto)
    if m:
        nombre = normalizar_lista(m.group(1))
        texto = m.group(2).strip()

    # Varios pendientes separados por comas: "jamón, leche, huevo"
    partes = [t.strip() for t in texto.split(",") if t.strip()]
    if not partes:
        await update.message.reply_text("❌ No entendí el pendiente. Ejemplo: `AGREGA A casa/despensa: jamón, leche`", parse_mode="Markdown")
        return

    data = cargar_pendientes()
    lista_nueva = nombre not in data
    data.setdefault(nombre, [])
    for t in partes:
        data[nombre].append({
            "texto": t,
            "creado": datetime.now().isoformat(),
            "hecho": False,
        })
    guardar_pendientes(data)

    aviso = f" (lista recién creada)" if lista_nueva and nombre != "general" else ""
    agregados = ", ".join(f"_{t}_" for t in partes)
    await update.message.reply_text(
        f"✅ Agregado(s) a *{nombre}*{aviso}: {agregados}",
        parse_mode="Markdown",
    )


async def cmd_quitar_pendiente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """QUITAR <número> o QUITAR <lista> <número>."""
    args = context.args
    if not args:
        await update.message.reply_text("Uso: `QUITAR <número>` o `QUITAR <lista> <número>`", parse_mode="Markdown")
        return

    data = cargar_pendientes()
    nombre, num, _ = _resolver_lista_y_numero(args, data)
    if nombre not in data:
        await update.message.reply_text(f"❌ No existe la lista *{nombre}*.", parse_mode="Markdown")
        return
    if num is None:
        await update.message.reply_text("❌ Escribe un número. Ejemplo: `QUITAR compras 2`", parse_mode="Markdown")
        return

    lista = data[nombre]
    if num < 1 or num > len(lista):
        await update.message.reply_text(f"❌ No existe el pendiente {num} en *{nombre}*.", parse_mode="Markdown")
        return

    eliminado = lista.pop(num - 1)
    guardar_pendientes(data)
    await update.message.reply_text(
        f"🗑️ Eliminado de *{nombre}*: _{eliminado['texto']}_\n"
        f"Quedan {len(lista)} en esa lista.",
        parse_mode="Markdown",
    )


async def cmd_ya_termine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    YA TERMINÉ <número> | YA TERMINÉ <lista> <número> | YA TERMINÉ <lista> <texto a buscar>
    """
    args = context.args
    if not args:
        await update.message.reply_text("Uso: `YA TERMINÉ <número>` o `YA TERMINÉ <lista> <número>`", parse_mode="Markdown")
        return

    data = cargar_pendientes()
    nombre, num, resto = _resolver_lista_y_numero(args, data)

    if nombre not in data:
        # Quizás escribió texto a buscar en general, ej. "YA TERMINÉ agua"
        texto_buscar = " ".join(args).lower()
        for i, p in enumerate(data["general"]):
            if texto_buscar in p["texto"].lower():
                eliminado = data["general"].pop(i)
                guardar_pendientes(data)
                await update.message.reply_text(f"✅ Completado: _{eliminado['texto']}_", parse_mode="Markdown")
                return
        await update.message.reply_text(f"❌ No existe la lista *{nombre}* ni un pendiente con ese texto.")
        return

    lista = data[nombre]
    if num is not None:
        if num < 1 or num > len(lista):
            await update.message.reply_text(f"❌ No existe el pendiente {num} en *{nombre}*.", parse_mode="Markdown")
            return
        eliminado = lista.pop(num - 1)
    else:
        texto_buscar = " ".join(resto).lower() if resto else ""
        eliminado = None
        if texto_buscar:
            for i, p in enumerate(lista):
                if texto_buscar in p["texto"].lower():
                    eliminado = lista.pop(i)
                    break
        if eliminado is None:
            await update.message.reply_text("❌ No encontré ese pendiente.")
            return

    guardar_pendientes(data)
    await update.message.reply_text(
        f"✅ ¡Bien hecho! Completado en *{nombre}*: _{eliminado['texto']}_\n"
        f"Quedan {len(lista)} en esa lista.",
        parse_mode="Markdown",
    )


# ─── Mensaje libre (pasa por Kimi) ───────────────────────────────────────────
async def mensaje_libre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    logger.info(f"Usuario dijo: {texto}")

    upper = texto.strip().upper()
    palabras = texto.strip().split()

    # ─── Confirmación pendiente de MOVI (sí/no) ────────────────────────────
    if context.user_data.get("movi") and upper in ("SÍ", "SI", "YES", "NO"):
        await movi_confirm(update, context)
        return

    # ─── Detectar COMPARATIVO (antes que CORTE: "compara ventas..." tiene VENTAS) ───
    if ("COMPARATIVO" in upper or "CÓMO VOY" in upper or "COMO VOY" in upper
            or ("VS" in upper and ("AÑO" in upper or "PASADO" in upper))):
        await update.message.reply_text(comparativo_mes(), parse_mode="Markdown")
        return

    # ─── Detectar CIERRE DE CAJA (también antes que CORTE) ────────────────
    if "CIERRE" in upper:
        fecha = "hoy"
        for p in palabras:
            p_upper = p.upper()
            if p_upper in ("HOY", "AYER"):
                fecha = p.lower()
                break
            if re.match(r"^\d{4}-\d{2}-\d{2}$", p):
                fecha = p
                break
        await update.message.reply_text(cierre_caja(fecha), parse_mode="Markdown")
        return

    # ─── Detectar MOVI en lenguaje natural (mueve/pasa/traslada) ──────────
    if any(upper.startswith(k) for k in ["MUEVE ", "MOVER ", "PASA ", "TRASLADA ", "MOVI "]):
        context.args = palabras[1:]
        await cmd_movi(update, context)
        return

    # ─── Detectar CORTE / VENTAS ───────────────────────────────────────────
    if any(k in upper for k in ["CORTE", "VENTAS", "VENTA"]):
        fecha = "hoy"
        for p in palabras:
            p_upper = p.upper()
            if p_upper in ("HOY", "AYER"):
                fecha = p.lower()
                break
            # Detectar fecha tipo 2026-08-09
            if re.match(r"^\d{4}-\d{2}-\d{2}$", p):
                fecha = p
                break
        resp = consultar_ventas(fecha)
        await update.message.reply_text(resp, parse_mode="Markdown")
        return

    # ─── Detectar DETALLE ──────────────────────────────────────────────────
    if "DETALLE" in upper:
        fecha = "hoy"
        for p in palabras:
            p_upper = p.upper()
            if p_upper in ("HOY", "AYER"):
                fecha = p.lower()
                break
            if re.match(r"^\d{4}-\d{2}-\d{2}$", p):
                fecha = p
                break
        resp = consultar_detalle(fecha)
        await update.message.reply_text(resp, parse_mode="Markdown")
        return

    # ─── Detectar BUSCAR ───────────────────────────────────────────────────
    if upper.startswith("BUSCAR "):
        q = " ".join(palabras[1:])
        resp = consultar_inventario(q)
        await update.message.reply_text(resp, parse_mode="Markdown")
        return

    # ─── Detectar CLIENTE ──────────────────────────────────────────────────
    if upper.startswith("CLIENTE "):
        q = " ".join(palabras[1:])
        resp = consultar_cliente(q)
        await update.message.reply_text(resp, parse_mode="Markdown")
        return

    # ─── 1. ACCIONES de pendientes primero ─────────────────────────────────
    # (van antes que la consulta PENDIENTES para que "Agrega pendiente ..."
    #  no caiga en la lista de pendientes por error)

    # ─── Detectar AGREGA ───────────────────────────────────────────────────
    if any(upper.startswith(k) for k in ["AGREGA ", "AGREGAR ", "NUEVO PENDIENTE ", "AÑADIR "]):
        for k in ["AGREGA ", "AGREGAR ", "NUEVO PENDIENTE ", "AÑADIR "]:
            if upper.startswith(k):
                q = texto[len(k):].strip()
                break
        else:
            q = " ".join(palabras[1:])
        context.args = [q]
        await cmd_agrega_pendiente(update, context)
        return

    # ─── Detectar QUITAR ───────────────────────────────────────────────────
    if upper.startswith("QUITAR "):
        context.args = palabras[1:]
        await cmd_quitar_pendiente(update, context)
        return

    # ─── Detectar YA TERMINÉ ───────────────────────────────────────────────
    if any(upper.startswith(k) for k in ["YA TERMINÉ ", "YA TERMINE ", "LISTO ", "HECHO ", "COMPLETADO "]):
        for k in ["YA TERMINÉ ", "YA TERMINE ", "LISTO ", "HECHO ", "COMPLETADO "]:
            if upper.startswith(k):
                resto = texto[len(k):].strip()
                break
        context.args = resto.split() if resto else []
        await cmd_ya_termine(update, context)
        return

    # ─── Listas de pendientes: crear, borrar, ver ──────────────────────────
    if upper.startswith("NUEVA LISTA ") or upper.startswith("NUEVA LISTA"):
        context.args = palabras[2:]
        await cmd_nueva_lista(update, context)
        return

    if upper.startswith("BORRAR LISTA "):
        context.args = palabras[2:]
        await cmd_borrar_lista(update, context)
        return

    if upper in ("LISTAS", "MIS LISTAS"):
        await cmd_listas(update, context)
        return

    # ─── 2. CONSULTA de pendientes después ─────────────────────────────────
    if any(k in upper for k in ["PENDIENTES", "AGENDA", "MIS PENDIENTES"]):
        # "PENDIENTES compras" → solo esa lista; "PENDIENTES" → todas
        if upper.startswith("PENDIENTES "):
            context.args = palabras[1:]
        elif upper.startswith("AGENDA "):
            context.args = palabras[1:]
        else:
            context.args = []
        await cmd_pendientes(update, context)
        return

    # ─── Si no coincide con nada, pasar al LLM ─────────────────────────────
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
    application.add_handler(CommandHandler("comparativo", cmd_comparativo))
    application.add_handler(CommandHandler("cierre", cmd_cierre))
    application.add_handler(CommandHandler("listas", cmd_listas))
    application.add_handler(CommandHandler("nueva_lista", cmd_nueva_lista))
    application.add_handler(CommandHandler("borrar_lista", cmd_borrar_lista))

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
