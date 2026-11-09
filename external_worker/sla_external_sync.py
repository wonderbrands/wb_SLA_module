#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
External SLA Synchronization & Calculation Worker (ClickHouse + MySQL RDS -> Odoo XML-RPC)
Runs inside Podman under user 'sla-consumer' on 159.195.193.205.

Key Principles:
- Reads sale_order, wmds_log, stock_picking, and sla_sync_state from local ClickHouse (http://127.0.0.1:8123).
- Reads marketplace SLA dates from AWS RDS MySQL (amazon_orders, wm_orders, tiktok_orders, yuju_orders).
- Touches Odoo.sh Prod ONLY for writing (sale.order.write) when an order's SLA needs to be set or updated.
"""

import os
import re
import sys
import time
import json
import logging
import argparse
import xmlrpc.client
from datetime import datetime, timedelta
from collections import defaultdict

import requests
import pymysql
import pymysql.cursors

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sla-external-sync")

# --- Configuration from Environment ---
CH_URL = os.getenv("CH_URL", "http://127.0.0.1:8123")
CH_DB = os.getenv("CH_DB", "wonderbrands")
CH_USER = os.getenv("CH_USER", "default")
CH_PASSWORD = os.getenv("CH_PASSWORD", "")

MYSQL_HOST = os.getenv("MYSQL_HOST", "wonderbrands1.cuwd36ifbz5t.us-east-1.rds.amazonaws.com")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "demian")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "z54u5ChHNpF8")
MYSQL_DB = os.getenv("MYSQL_DB", "somos_reyes")

ODOO_URL = os.getenv("ODOO_URL", "https://somosreyes.odoo.com")
ODOO_DB = os.getenv("ODOO_DB", "wonderbrands2026-main-28990431")
ODOO_USER = os.getenv("ODOO_USER", "demian@wonderbrands.co")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD", "Demian123")

SYNC_INTERVAL_SECONDS = int(os.getenv("SYNC_INTERVAL_SECONDS", "600"))  # 10 min default
DEFAULT_DATE_FROM = os.getenv("SLA_DATE_FROM", "2026-10-01")
UTC_LOCAL_OFFSET = -6  # America/Mexico_City (UTC-6)

FULL_WAREHOUSE_CODES = {"MLSR", "MLTO", "FBA", "WLFL", "CPFL", "ELFL"}

# Channels whose sla_date in Odoo is stored in UTC (per wmds.mysql.config sla_timezone='utc')
UTC_CHANNELS = {"amazon", "walmart", "shopify"}

# Channels that have SLA calculation rules when Non-Full
MANAGED_CHANNELS = (
    "mercado",
    "tiktok",
    "tik tok",
    "amazon",
    "walmart",
    "coppel",
    "elektra",
    "shopify",
    "farmacias",
)


def ch_query(sql, data=None):
    """Execute a ClickHouse SQL query over HTTP."""
    params = {"database": CH_DB}
    if CH_USER:
        params["user"] = CH_USER
    if CH_PASSWORD:
        params["password"] = CH_PASSWORD
    body = sql if data is None else f"{sql}\n{data}"
    resp = requests.post(CH_URL, params=params, data=body.encode("utf-8"), timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"ClickHouse error ({resp.status_code}): {resp.text[:500]}")
    return resp.text


def ch_query_json(sql):
    """Execute a ClickHouse query and return list of dicts (FORMAT JSONEachRow)."""
    out = ch_query(f"{sql}\nFORMAT JSONEachRow")
    rows = []
    for line in out.strip().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def ensure_ch_state_table():
    """Ensure wonderbrands.sla_sync_state table exists in ClickHouse."""
    ddl = f"""
    CREATE TABLE IF NOT EXISTS {CH_DB}.sla_sync_state (
        odoo_internal_id Int64,
        name String,
        channel String,
        sla_date String,
        sla_source_type String,
        has_guide UInt8,
        synced_at DateTime64(6)
    ) ENGINE = ReplacingMergeTree(synced_at)
    ORDER BY odoo_internal_id
    """
    ch_query(ddl)


def parse_dt(val):
    """Parse datetime string or object into naive datetime."""
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    if not s or s.lower() in ("none", "null", "false", "\\n") or s.startswith("0000-00-00"):
        return None
    s = s.replace("T", " ").replace("Z", "")
    if "+" in s:
        s = s.split("+")[0].strip()
    if "." in s:
        s = s.split(".")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def next_business_day_end(local_dt):
    """
    Given a local Mexico datetime, return the next business day (Mon-Fri) at 23:59:59 local.
    Mon -> Tue, Tue -> Wed, Wed -> Thu, Thu -> Fri, Fri -> Mon, Sat -> Mon, Sun -> Mon.
    """
    nxt = local_dt + timedelta(days=1)
    while nxt.weekday() in (5, 6):  # 5=Saturday, 6=Sunday
        nxt += timedelta(days=1)
    return nxt.replace(hour=23, minute=59, second=59)


def is_utc_channel(channel_str):
    ch = (channel_str or "").strip().lower()
    return any(u in ch for u in UTC_CHANNELS)


def is_full_order_row(row):
    ff = (row.get("fulfillment") or "").strip().lower()
    if ff in ("fbc", "full"):
        return True
    wh = (row.get("warehouse_id") or "").strip()
    if not wh:
        return False
    if wh.upper() in FULL_WAREHOUSE_CODES or "fulfillment" in wh.lower():
        return True
    return False


def is_managed_channel(channel_str):
    ch = (channel_str or "").strip().lower()
    if not ch:
        return False
    return any(k in ch for k in MANAGED_CHANNELS)


def fetch_ch_orders(date_from):
    """
    Fetch active orders from ClickHouse wonderbrands.sale_order FINAL
    enriched with guide status from stock_picking and wmds_log.
    """
    sql = f"""
    WITH
        pick_guides AS (
            SELECT
                origin,
                max(carrier_tracking_ref) AS pick_tracking
            FROM {CH_DB}.stock_picking FINAL
            WHERE state != 'cancel'
              AND carrier_tracking_ref IS NOT NULL
              AND carrier_tracking_ref != ''
              AND carrier_tracking_ref != 'False'
              AND origin IS NOT NULL
              AND origin != ''
            GROUP BY origin
        ),
        guide_logs AS (
            SELECT
                sale,
                max(date) AS guide_log_date
            FROM {CH_DB}.wmds_log
            WHERE (log ILIKE '%guía%' OR log ILIKE '%anexos%')
              AND sale IS NOT NULL
              AND sale != ''
            GROUP BY sale
        )
    SELECT
        so.odoo_internal_id AS odoo_internal_id,
        coalesce(so.name, '') AS name,
        coalesce(so.channel, '') AS channel,
        coalesce(so.channel_order_reference, '') AS channel_order_reference,
        coalesce(so.channel_order_id, '') AS channel_order_id,
        coalesce(so.client_order_ref, '') AS client_order_ref,
        coalesce(so.fulfillment, '') AS fulfillment,
        coalesce(so.warehouse_id, '') AS warehouse_id,
        coalesce(so.state, '') AS state,
        toString(so.date_order) AS date_order,
        toString(so.create_date) AS create_date,
        toString(so.write_date) AS write_date,
        coalesce(so.yuju_due_date, '') AS yuju_due_date,
        coalesce(so.yuju_carrier_tracking_ref, '') AS yuju_carrier_tracking_ref,
        coalesce(so.data_tracking_readwrite, '') AS data_tracking_readwrite,
        coalesce(so.data_attachment_ids, '') AS data_attachment_ids,
        coalesce(so.attachments, '') AS attachments,
        coalesce(so.data_ready_to_pick, 0) AS data_ready_to_pick,
        coalesce(pg.pick_tracking, '') AS pick_tracking,
        toString(gl.guide_log_date) AS guide_log_date
    FROM {CH_DB}.sale_order AS so FINAL
    LEFT JOIN pick_guides AS pg ON pg.origin = so.name
    LEFT JOIN guide_logs AS gl ON gl.sale = so.name
    WHERE so.date_order >= '{date_from} 00:00:00'
      AND coalesce(so.state, '') != 'cancel'
      AND so.odoo_internal_id > 0
    """
    return ch_query_json(sql)


def fetch_ch_sync_state():
    """Fetch already-synced state from ClickHouse wonderbrands.sla_sync_state FINAL."""
    sql = f"""
    SELECT
        odoo_internal_id,
        sla_date,
        sla_source_type,
        has_guide
    FROM {CH_DB}.sla_sync_state FINAL
    """
    rows = ch_query_json(sql)
    return {int(r["odoo_internal_id"]): r for r in rows}


def fetch_mysql_sla_maps(orders):
    """
    Query MySQL RDS (somos_reyes) in batches for Amazon, Walmart, TikTok Shop, and Yuju orders.
    Returns dict of maps: {'amazon': {}, 'walmart': {}, 'tiktok': {}, 'yuju': {}}
    where values are UTC naive datetimes (for amazon/walmart/tiktok) or local datetimes (for yuju).
    """
    amazon_refs = set()
    walmart_refs = set()
    tiktok_refs = set()
    yuju_refs = set()

    for o in orders:
        if is_full_order_row(o):
            continue
        ch = (o.get("channel") or "").lower()
        refs = [
            (o.get("channel_order_reference") or "").strip(),
            (o.get("channel_order_id") or "").strip(),
            (o.get("client_order_ref") or "").strip(),
        ]
        refs = [r for r in refs if r and r.lower() != "false"]
        if not refs:
            continue
        if "amazon" in ch:
            amazon_refs.update(refs)
        elif "walmart" in ch:
            walmart_refs.update(refs)
        elif "tiktok" in ch or "tik tok" in ch:
            tiktok_refs.update(refs)
            if not (o.get("yuju_due_date") or "").strip():
                yuju_refs.update(refs)
        else:
            if not (o.get("yuju_due_date") or "").strip():
                yuju_refs.update(refs)

    maps = {"amazon": {}, "walmart": {}, "tiktok": {}, "yuju": {}}

    try:
        conn = pymysql.connect(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DB,
            connect_timeout=15,
            read_timeout=30,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except Exception as e:
        logger.error(f"MySQL RDS connection error: {e}")
        return maps

    try:
        cur = conn.cursor()
        chunk_size = 800

        # 1. Amazon (LatestShipDate in UTC)
        amz_list = list(amazon_refs)
        for i in range(0, len(amz_list), chunk_size):
            chunk = amz_list[i : i + chunk_size]
            fmt = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"SELECT AmazonOrderId, LatestShipDate FROM amazon_orders "
                f"WHERE AmazonOrderId IN ({fmt}) AND LatestShipDate IS NOT NULL",
                tuple(chunk),
            )
            for r in cur.fetchall():
                dt = parse_dt(r.get("LatestShipDate"))
                ref = (r.get("AmazonOrderId") or "").strip()
                if ref and dt:
                    maps["amazon"][ref] = dt

        # 2. Walmart (promiseDeliveryDate / JSON in UTC)
        wm_list = list(walmart_refs)
        for i in range(0, len(wm_list), chunk_size):
            chunk = wm_list[i : i + chunk_size]
            fmt = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"SELECT purchaseOrderId, customerOrderId, promiseDeliveryDate, shipments, orderLines "
                f"FROM wm_orders WHERE purchaseOrderId IN ({fmt}) OR customerOrderId IN ({fmt})",
                tuple(chunk) + tuple(chunk),
            )
            for r in cur.fetchall():
                dt_val = r.get("promiseDeliveryDate")
                if not dt_val or str(dt_val).startswith("0000-00-00"):
                    txt = (r.get("orderLines") or "") + (r.get("shipments") or "")
                    m = re.search(r"promiseDeliveryDate['\"]?\s*:\s*['\"]?([^'\",}]+)", txt)
                    if not m:
                        m = re.search(r"expectedShipmentShippedDate['\"]?\s*:\s*['\"]?([^'\",}]+)", txt)
                    if m:
                        dt_val = m.group(1).strip()
                dt = None
                if dt_val:
                    if isinstance(dt_val, (int, float)) or (isinstance(dt_val, str) and dt_val.isdigit()):
                        v = float(dt_val)
                        if v > 1e11:
                            v /= 1000.0
                        dt = datetime.utcfromtimestamp(v)
                    else:
                        dt = parse_dt(dt_val)
                if dt:
                    p_id = (str(r.get("purchaseOrderId") or "")).strip()
                    c_id = (str(r.get("customerOrderId") or "")).strip()
                    if p_id:
                        maps["walmart"][p_id] = dt
                    if c_id:
                        maps["walmart"][c_id] = dt

        # 3. TikTok Shop (rts_sla_time / tts_sla_time in UTC)
        tt_list = list(tiktok_refs)
        for i in range(0, len(tt_list), chunk_size):
            chunk = tt_list[i : i + chunk_size]
            fmt = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"SELECT id, rts_sla_time, tts_sla_time FROM tiktok_orders WHERE id IN ({fmt})",
                tuple(chunk),
            )
            for r in cur.fetchall():
                ref = (str(r.get("id") or "")).strip()
                dt = parse_dt(r.get("rts_sla_time")) or parse_dt(r.get("tts_sla_time"))
                if ref and dt:
                    maps["tiktok"][ref] = dt

        # 4. Yuju Orders (delivery_deadline)
        yj_list = list(yuju_refs)
        for i in range(0, len(yj_list), chunk_size):
            chunk = yj_list[i : i + chunk_size]
            fmt = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"SELECT order_id, reference, delivery_deadline FROM yuju_orders "
                f"WHERE (order_id IN ({fmt}) OR reference IN ({fmt})) AND delivery_deadline IS NOT NULL",
                tuple(chunk) + tuple(chunk),
            )
            for r in cur.fetchall():
                dt = parse_dt(r.get("delivery_deadline"))
                if dt:
                    oid = (str(r.get("order_id") or "")).strip()
                    ref = (str(r.get("reference") or "")).strip()
                    if oid:
                        maps["yuju"][oid] = dt
                    if ref:
                        maps["yuju"][ref] = dt

    except Exception as e:
        logger.error(f"Error querying MySQL tables: {e}")
    finally:
        conn.close()

    return maps


def row_has_guide(row):
    """Check if an order row from ClickHouse has a shipping guide assigned."""
    tr = (
        row.get("yuju_carrier_tracking_ref")
        or row.get("data_tracking_readwrite")
        or row.get("pick_tracking")
        or ""
    ).strip()
    if tr and tr.lower() not in ("false", "none", "null"):
        return True
    if int(row.get("data_ready_to_pick") or 0) == 1:
        return True
    for field in ("data_attachment_ids", "attachments"):
        v = (row.get(field) or "").strip()
        if v and v not in ("[]", "False", "false", "None", "null"):
            return True
    return False


def compute_target_sla(row, mysql_maps):
    """
    Compute (target_sla_str, source_type, has_guide_int) for a Non-Full order row.
    Respects:
    - UTC vs Mexico Local (UTC-6) per marketplace (UTC_CHANNELS = amazon, walmart, shopify)
    - Mercado Libre 2-stage rule when no Yuju SLA:
      1) No guide -> 15 calendar days from order date (23:59:59 local)
      2) With guide -> Next business day from guide date (23:59:59 local)
    - TikTok Shop: Yuju SLA -> MySQL tiktok_orders (rts_sla_time/tts_sla_time) -> Calculated (+1 business day)
    - Amazon / Walmart / Coppel / Elektra / Shopify / Farmacias: MySQL/Yuju -> Calculated (+1 business day)
    """
    ch = (row.get("channel") or "").strip()
    ch_low = ch.lower()
    if not is_managed_channel(ch_low):
        return None, None, 0

    is_utc = is_utc_channel(ch_low)
    has_guide = 1 if row_has_guide(row) else 0

    refs = [
        (row.get("channel_order_reference") or "").strip(),
        (row.get("channel_order_id") or "").strip(),
        (row.get("client_order_ref") or "").strip(),
    ]
    refs = [r for r in refs if r and r.lower() != "false"]

    order_dt_utc = parse_dt(row.get("date_order")) or parse_dt(row.get("create_date")) or datetime.utcnow()
    order_dt_local = order_dt_utc + timedelta(hours=UTC_LOCAL_OFFSET)

    # 1. Amazon
    if "amazon" in ch_low:
        for r in refs:
            if r in mysql_maps["amazon"]:
                dt_utc = mysql_maps["amazon"][r]
                return dt_utc.strftime("%Y-%m-%d %H:%M:%S"), "mysql_amazon", has_guide
        # Fallback calculated (+1 business day in local -> converted to UTC since Amazon is_utc=True)
        target_local = next_business_day_end(order_dt_local)
        target_utc = target_local - timedelta(hours=UTC_LOCAL_OFFSET)
        return target_utc.strftime("%Y-%m-%d %H:%M:%S"), "calc_amazon", has_guide

    # 2. Walmart
    if "walmart" in ch_low:
        for r in refs:
            if r in mysql_maps["walmart"]:
                dt_utc = mysql_maps["walmart"][r]
                return dt_utc.strftime("%Y-%m-%d %H:%M:%S"), "mysql_walmart", has_guide
        target_local = next_business_day_end(order_dt_local)
        target_utc = target_local - timedelta(hours=UTC_LOCAL_OFFSET)
        return target_utc.strftime("%Y-%m-%d %H:%M:%S"), "calc_walmart", has_guide

    # 3. Shopify (is_utc=True)
    if "shopify" in ch_low:
        yuju_dt = parse_dt(row.get("yuju_due_date"))
        if yuju_dt:
            target_utc = yuju_dt - timedelta(hours=UTC_LOCAL_OFFSET)
            return target_utc.strftime("%Y-%m-%d %H:%M:%S"), "yuju_shopify", has_guide
        target_local = next_business_day_end(order_dt_local)
        target_utc = target_local - timedelta(hours=UTC_LOCAL_OFFSET)
        return target_utc.strftime("%Y-%m-%d %H:%M:%S"), "calc_shopify", has_guide

    # 4. TikTok Shop (is_utc=False -> stored in Mexico Local Time)
    if "tiktok" in ch_low or "tik tok" in ch_low:
        yuju_dt = parse_dt(row.get("yuju_due_date"))
        if yuju_dt:
            return yuju_dt.strftime("%Y-%m-%d %H:%M:%S"), "yuju_tiktok", has_guide
        for r in refs:
            if r in mysql_maps["tiktok"]:
                # rts_sla_time in tiktok_orders is in UTC -> convert to Mexico Local (UTC-6)
                dt_utc = mysql_maps["tiktok"][r]
                dt_local = dt_utc + timedelta(hours=UTC_LOCAL_OFFSET)
                return dt_local.strftime("%Y-%m-%d %H:%M:%S"), "mysql_tiktok", has_guide
            if r in mysql_maps["yuju"]:
                dt_local = mysql_maps["yuju"][r]
                return dt_local.strftime("%Y-%m-%d %H:%M:%S"), "mysql_yuju_tiktok", has_guide
        # Fallback calculated for TikTok Shop without Yuju/MySQL SLA (+1 business day local)
        target_local = next_business_day_end(order_dt_local)
        return target_local.strftime("%Y-%m-%d %H:%M:%S"), "calc_tiktok", has_guide

    # 5. Mercado Libre (is_utc=False -> stored in Mexico Local Time)
    if "mercado" in ch_low:
        yuju_dt = parse_dt(row.get("yuju_due_date"))
        if yuju_dt:
            return yuju_dt.strftime("%Y-%m-%d %H:%M:%S"), "yuju_ml", has_guide
        for r in refs:
            if r in mysql_maps["yuju"]:
                dt_local = mysql_maps["yuju"][r]
                return dt_local.strftime("%Y-%m-%d %H:%M:%S"), "mysql_yuju_ml", has_guide
        # Regla 2 Etapas para Mercado Libre No-Full sin SLA de Yuju:
        # Etapa 1 (sin guía): 15 días naturales desde fecha de ingreso.
        # Etapa 2 (con guía): entrega al día siguiente hábil desde que se asignó la guía.
        if has_guide:
            guide_dt_utc = (
                parse_dt(row.get("guide_log_date"))
                or parse_dt(row.get("write_date"))
                or order_dt_utc
            )
            guide_dt_local = guide_dt_utc + timedelta(hours=UTC_LOCAL_OFFSET)
            target_local = next_business_day_end(guide_dt_local)
            return target_local.strftime("%Y-%m-%d %H:%M:%S"), "calc_ml_guide_next_bday", has_guide
        else:
            target_local = (order_dt_local + timedelta(days=15)).replace(hour=23, minute=59, second=59)
            return target_local.strftime("%Y-%m-%d %H:%M:%S"), "calc_ml_no_guide_15d", has_guide

    # 6. Coppel, Elektra, Farmacias del ahorro (is_utc=False -> stored in Mexico Local Time)
    yuju_dt = parse_dt(row.get("yuju_due_date"))
    if yuju_dt:
        return yuju_dt.strftime("%Y-%m-%d %H:%M:%S"), "yuju_local", has_guide
    for r in refs:
        if r in mysql_maps["yuju"]:
            dt_local = mysql_maps["yuju"][r]
            return dt_local.strftime("%Y-%m-%d %H:%M:%S"), "mysql_yuju_local", has_guide

    target_local = next_business_day_end(order_dt_local)
    return target_local.strftime("%Y-%m-%d %H:%M:%S"), "calc_local", has_guide


def write_ch_sync_state(state_rows):
    """Insert updated sync state rows into ClickHouse wonderbrands.sla_sync_state."""
    if not state_rows:
        return
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
    lines = []
    for r in state_rows:
        r["synced_at"] = now_str
        lines.append(json.dumps(r, ensure_ascii=False))
    payload = "\n".join(lines)
    ch_query(f"INSERT INTO {CH_DB}.sla_sync_state FORMAT JSONEachRow", data=payload)


def get_odoo_rpc():
    """Authenticate and return (uid, models_proxy) for Odoo XML-RPC."""
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise RuntimeError(f"Failed to authenticate with Odoo at {ODOO_URL}")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)
    return uid, models


def run_sync_cycle(date_from=DEFAULT_DATE_FROM, force_all=False):
    t0 = time.time()
    ensure_ch_state_table()

    logger.info(f"Fetching orders from ClickHouse (date_order >= {date_from})...")
    ch_orders = fetch_ch_orders(date_from)
    sync_state = {} if force_all else fetch_ch_sync_state()
    logger.info(f"Loaded {len(ch_orders)} orders from ClickHouse ({len(sync_state)} in state cache).")

    mysql_maps = fetch_mysql_sla_maps(ch_orders)
    logger.info(
        f"MySQL maps loaded: amazon={len(mysql_maps['amazon'])}, "
        f"walmart={len(mysql_maps['walmart'])}, tiktok={len(mysql_maps['tiktok'])}, "
        f"yuju={len(mysql_maps['yuju'])}"
    )

    # Group updates by target sla_date to minimize XML-RPC roundtrips
    updates_by_sla = defaultdict(list)  # sla_date_str -> list of order dicts
    full_to_clean = []
    stats_by_source = defaultdict(int)

    for row in ch_orders:
        oid = int(row["odoo_internal_id"])
        prev = sync_state.get(oid)

        if is_full_order_row(row):
            if not prev or prev.get("sla_source_type") != "full_excluded":
                full_to_clean.append(row)
            continue

        target_sla, source_type, has_guide = compute_target_sla(row, mysql_maps)
        if not target_sla:
            continue

        stats_by_source[source_type] += 1

        # Skip writing to Odoo if ClickHouse state cache shows identical sla_date & guide status
        if (
            prev
            and prev.get("sla_date") == target_sla
            and int(prev.get("has_guide") or 0) == has_guide
        ):
            continue

        updates_by_sla[target_sla].append(
            {
                "odoo_internal_id": oid,
                "name": row.get("name") or "",
                "channel": row.get("channel") or "",
                "sla_date": target_sla,
                "sla_source_type": source_type,
                "has_guide": has_guide,
            }
        )

    total_to_write = sum(len(v) for v in updates_by_sla.values())
    logger.info(
        f"Computed SLAs: {dict(stats_by_source)} | "
        f"Pending Odoo writes: {total_to_write} orders across {len(updates_by_sla)} distinct timestamps | "
        f"Full orders to verify/clean: {len(full_to_clean)}"
    )

    if total_to_write == 0 and len(full_to_clean) == 0:
        logger.info(f"Nothing to update in Odoo. Cycle finished in {time.time() - t0:.2f}s.")
        return

    uid, models = get_odoo_rpc()

    # Fetch IDs with manual override (by a user, not automatic prior_to_entry) in 1 lightweight call
    manual_override_ids = set()
    try:
        mo_ids = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "sale.order",
            "search",
            [[["sla_manual_override", "=", True], ["sla_override_reason", "!=", "prior_to_entry"]]],
        )
        manual_override_ids = set(mo_ids or [])
    except Exception as e:
        logger.warning(f"Could not fetch manual_override_ids: {e}")

    # 1. Clean Full orders in batches (only if they have sla_date set in Odoo, or write False directly in batches of 500)
    if full_to_clean:
        batch_size = 500
        cleaned_state_rows = []
        for i in range(0, len(full_to_clean), batch_size):
            batch = full_to_clean[i : i + batch_size]
            ids = [int(r["odoo_internal_id"]) for r in batch]
            try:
                # Find only those in this batch that actually have sla_date != False in Odoo
                dirty_ids = models.execute_kw(
                    ODOO_DB,
                    uid,
                    ODOO_PASSWORD,
                    "sale.order",
                    "search",
                    [[["id", "in", ids], ["sla_date", "!=", False]]],
                )
                if dirty_ids:
                    models.execute_kw(
                        ODOO_DB,
                        uid,
                        ODOO_PASSWORD,
                        "sale.order",
                        "write",
                        [
                            dirty_ids,
                            {
                                "sla_date": False,
                                "sla_manual_override": False,
                                "sla_priority_level": "normal",
                                "sla_priority_label": "",
                            },
                        ],
                    )
                    logger.info(f"Cleaned SLA on {len(dirty_ids)} Full orders in Odoo.")
                for r in batch:
                    cleaned_state_rows.append(
                        {
                            "odoo_internal_id": int(r["odoo_internal_id"]),
                            "name": r.get("name") or "",
                            "channel": r.get("channel") or "",
                            "sla_date": "",
                            "sla_source_type": "full_excluded",
                            "has_guide": 0,
                        }
                    )
            except Exception as e:
                logger.error(f"Error cleaning Full orders batch: {e}")

        if cleaned_state_rows:
            write_ch_sync_state(cleaned_state_rows)

    # 2. Write Non-Full SLAs to Odoo grouped by sla_date
    written_count = 0
    state_buffer = []

    for sla_str, items in updates_by_sla.items():
        # Batch up to 100 IDs per XML-RPC write call
        for i in range(0, len(items), 100):
            chunk = items[i : i + 100]
            ids = [x["odoo_internal_id"] for x in chunk if x["odoo_internal_id"] not in manual_override_ids]
            if not ids:
                state_buffer.extend(chunk)
                continue
            try:
                models.execute_kw(
                    ODOO_DB,
                    uid,
                    ODOO_PASSWORD,
                    "sale.order",
                    "write",
                    [ids, {"sla_date": sla_str, "sla_original_date": sla_str}],
                )
                written_count += len(ids)
                state_buffer.extend(chunk)
            except Exception as e:
                # If a record was deleted in Odoo or locked, fallback to writing existing IDs in chunk
                logger.warning(f"Batch write warning for {sla_str} ({len(ids)} ids): {e}")
                try:
                    existing_ids = models.execute_kw(
                        ODOO_DB,
                        uid,
                        ODOO_PASSWORD,
                        "sale.order",
                        "search",
                        [[["id", "in", ids], ["sla_manual_override", "=", False]]],
                    )
                    if existing_ids:
                        models.execute_kw(
                            ODOO_DB,
                            uid,
                            ODOO_PASSWORD,
                            "sale.order",
                            "write",
                            [existing_ids, {"sla_date": sla_str, "sla_original_date": sla_str}],
                        )
                        written_count += len(existing_ids)
                    state_buffer.extend(chunk)
                except Exception as inner_e:
                    logger.error(f"Fallback batch write error: {inner_e}")

            if len(state_buffer) >= 500:
                write_ch_sync_state(state_buffer)
                state_buffer.clear()

    if state_buffer:
        write_ch_sync_state(state_buffer)
        state_buffer.clear()

    logger.info(
        f"Sync cycle completed in {time.time() - t0:.2f}s. "
        f"Updated {written_count} Non-Full orders in Odoo."
    )


def main():
    parser = argparse.ArgumentParser(description="External ClickHouse/MySQL -> Odoo SLA Worker")
    parser.add_argument("--once", action="store_true", help="Run a single sync cycle and exit")
    parser.add_argument("--force", action="store_true", help="Ignore ClickHouse state cache and push all SLAs")
    parser.add_argument("--date-from", default=DEFAULT_DATE_FROM, help="Start date YYYY-MM-DD (default: 2026-10-01)")
    args = parser.parse_args()

    if args.once:
        run_sync_cycle(date_from=args.date_from, force_all=args.force)
        return

    logger.info(
        f"Starting SLA External Sync Daemon (interval={SYNC_INTERVAL_SECONDS}s, date_from={args.date_from})"
    )
    first_run = True
    while True:
        try:
            run_sync_cycle(date_from=args.date_from, force_all=(args.force and first_run))
            first_run = False
        except Exception as e:
            logger.error(f"Unhandled error in sync cycle: {e}", exc_info=True)
        time.sleep(SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
