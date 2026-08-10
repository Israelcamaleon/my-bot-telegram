"""
Bot Telegram @MiAgenteKimi2026_bot — Contador de Alika / CattleDogs
v2: Parser de lenguaje natural para fechas.
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
            suc_name = suc[1] if isinstance(suc, list) else str(s
