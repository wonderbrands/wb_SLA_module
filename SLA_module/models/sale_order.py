# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from datetime import datetime, timedelta
import logging

_logger = logging.getLogger(__name__)

UTC_LOCAL = -6

FULL_WAREHOUSE_CODES = ('MLSR', 'MLTO', 'FBA', 'WLFL', 'CPFL', 'ELFL')

CHANNEL_NORMALIZATION_MAP = {
    'mercado libre méxico': 'Mercado Libre Mexico',
    'mercado libre mexico': 'Mercado Libre Mexico',
    'mercado libre': 'Mercado Libre Mexico',
    'mercado libre mexico.': 'Mercado Libre Mexico',
    'merado libre': 'Mercado Libre Mexico',
    'mercado lubre': 'Mercado Libre Mexico',
    'tik tok shop': 'TikTok Shop',
    'tiktok': 'TikTok Shop',
    'tiktok shop': 'TikTok Shop',
    'liquidaciones': 'Liquidacion',
    'liquidacion': 'Liquidacion',
    'venta empleado': 'Ventas Empleado',
    'venta emplado': 'Ventas Empleado',
    'ventas empleado': 'Ventas Empleado',
    'walmart 1p': 'Walmart',
    'nerna': 'Merma',
}


def normalize_channel(channel_str):
    if not channel_str or not isinstance(channel_str, str):
        return channel_str
    clean = channel_str.strip()
    return CHANNEL_NORMALIZATION_MAP.get(clean.lower(), clean)



def parse_yuju_date(dt_str, is_utc=False):
    if not dt_str:
        return False
    try:
        clean_str = str(dt_str).strip()
        if clean_str.endswith('Z'):
            clean_str = clean_str[:-1]
        if 'T' in clean_str:
            date_part, time_part = clean_str.split('T')
            time_parts = time_part.split(':')
            hour = time_parts[0]
            minute = time_parts[1]
            second = time_parts[2].split('.')[0] if len(time_parts) > 2 else '00'
            clean_str = f"{date_part} {hour}:{minute}:{second}"
        else:
            clean_str = clean_str.split('.')[0]
        local_dt = fields.Datetime.to_datetime(clean_str)
        if not local_dt:
            return False
        # yuju_due_date viene en hora local CDMX (UTC-6).
        # Si el canal se maneja en 'local' (is_utc=False), se conserva en hora local;
        # si el canal se maneja en 'utc' (is_utc=True), se convierte a UTC.
        return (local_dt - timedelta(hours=UTC_LOCAL)) if is_utc else local_dt
    except Exception as e:
        _logger.error(f"Error parseando yuju_due_date '{dt_str}': {e}")
        return False


class SaleOrderSlaHistory(models.Model):
    _name = 'sale.order.sla.history'
    _description = 'Historial de Reasignación de SLA en Orden de Venta'
    _order = 'date desc, id desc'

    sale_id = fields.Many2one('sale.order', string="Orden de Venta", required=True, ondelete='cascade', index=True)
    user_id = fields.Many2one('res.users', string="Usuario", default=lambda self: self.env.user, readonly=True)
    date = fields.Datetime(string="Fecha del Cambio", default=fields.Datetime.now, readonly=True)
    old_sla_date = fields.Datetime(string="Fecha SLA Anterior", readonly=True)
    new_sla_date = fields.Datetime(string="Nueva Fecha SLA", readonly=True)
    reason = fields.Selection([
        ('no_space', 'Falta de espacio en transporte / unidad'),
        ('carrier_cutoff', 'Corte de paquetería / retraso de recolección'),
        ('platform_rescheduled', 'Reprogramado por marketplace / plataforma'),
        ('customer_request', 'Petición del cliente'),
        ('prior_to_entry', 'SLA recibido previo a fecha de ingreso'),
        ('other', 'Otro motivo'),
    ], string="Motivo", readonly=True)
    note = fields.Text(string="Nota / Justificación", readonly=True)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    sla_date = fields.Datetime(string="Fecha límite SLA", index=True, copy=False)
    
    sla_priority_level = fields.Selection([
        ('critical_1h', 'Crítico (< 1h)'),
        ('urgent_2h', 'Urgente (< 2h)'),
        ('warning_6h', 'Advertencia (< 6h)'),
        ('notice_24h', 'En Tiempo (< 24h)'),
        ('normal', 'Normal'),
        ('overdue', 'Vencido')
    ], string="Nivel Prioridad SLA", compute="_compute_sla_priority", store=True)

    sla_priority_label = fields.Char(string="Aviso Recolección SLA", compute="_compute_sla_priority", store=True)

    sla_manual_override = fields.Boolean(string="SLA Reasignado Manualmente", default=False, copy=False)
    sla_override_reason = fields.Selection([
        ('no_space', 'Falta de espacio en transporte / unidad'),
        ('carrier_cutoff', 'Corte de paquetería / retraso de recolección'),
        ('platform_rescheduled', 'Reprogramado por marketplace / plataforma'),
        ('customer_request', 'Petición del cliente'),
        ('prior_to_entry', 'SLA recibido previo a fecha de ingreso'),
        ('other', 'Otro motivo'),
    ], string="Motivo Reasignación SLA", copy=False)
    sla_override_note = fields.Text(string="Nota / Justificación Reasignación", copy=False)
    sla_override_date = fields.Datetime(string="Fecha de Reasignación", copy=False)
    sla_override_user_id = fields.Many2one('res.users', string="Reasignado por", copy=False)
    sla_original_date = fields.Datetime(string="Fecha SLA Original", copy=False)

    sla_history_ids = fields.One2many(
        'sale.order.sla.history',
        'sale_id',
        string="Historial de Modificaciones SLA"
    )

    @api.depends('sla_date', 'state', 'sla_manual_override')
    def _compute_sla_priority(self):
        now_utc = fields.Datetime.now()
        for order in self:
            if order.state == 'cancel' or not order.sla_date:
                order.sla_priority_level = 'normal'
                order.sla_priority_label = ''
                continue

            effective_sla = order.sla_date
            if effective_sla < now_utc:
                order.sla_priority_level = 'overdue'
                order.sla_priority_label = 'SLA Vencido'
            elif effective_sla <= now_utc + timedelta(hours=1):
                order.sla_priority_level = 'critical_1h'
                order.sla_priority_label = 'Está a punto de llegar la recolecta (< 1h)'
            else:
                diff_hours = (effective_sla - now_utc).total_seconds() / 3600.0
                if diff_hours <= 2.0:
                    order.sla_priority_level = 'urgent_2h'
                    order.sla_priority_label = 'Quedan 2 horas para la recolecta'
                elif diff_hours <= 6.0:
                    order.sla_priority_level = 'warning_6h'
                    order.sla_priority_label = 'Quedan 6 horas para la recolecta'
                elif diff_hours <= 24.0:
                    order.sla_priority_level = 'notice_24h'
                    order.sla_priority_label = 'Quedan unas horas para la recolecta'
                else:
                    order.sla_priority_level = 'normal'
                    order.sla_priority_label = 'En tiempo'

    def _is_full_order(self):
        """
        Determina si una orden de venta es Full (Fulfillment by Marketplace: ML Full, Amazon FBA, Walmart WFS, etc.).
        - fulfillment == 'fbc' (o 'full')
        - warehouse_id con código en FULL_WAREHOUSE_CODES o nombre que contenga 'fulfillment'
        Nota: 'fbm' (Drop/Seller) y 'fbf' (Flex) NO son Full y operan desde el almacén propio (WH).
        """
        self.ensure_one()
        ff = (getattr(self, 'fulfillment', '') or '').strip().lower()
        if ff in ('fbc', 'full'):
            return True
        wh = self.warehouse_id
        if wh:
            wh_code = (wh.code or '').strip().upper()
            wh_name = (wh.name or '').strip().lower()
            if wh_code in FULL_WAREHOUSE_CODES or 'fulfillment' in wh_name:
                return True
        return False

    @api.model
    def _get_non_full_domain(self):
        """ Dominio ORM para excluir órdenes Full (fbc o almacenes de Fulfillment) """
        return [
            '|', ('fulfillment', '=', False), ('fulfillment', 'not in', ['fbc', 'full']),
            '|', ('warehouse_id', '=', False),
            '&', ('warehouse_id.code', 'not in', list(FULL_WAREHOUSE_CODES)),
                 ('warehouse_id.name', 'not ilike', 'fulfillment'),
        ]

    @api.model
    def _should_exclude_full_for_channel(self, channel_name, schedule=None):
        """
        Evalúa si para el marketplace dado deben excluirse las órdenes Full según marketplace.schedule.
        Por defecto es True salvo que exista una regla específica con exclude_full = False.
        """
        if schedule is None and channel_name:
            clean_ch = normalize_channel(channel_name)
            schedule = self.env['marketplace.schedule'].sudo().search([
                ('marketplace', '=ilike', clean_ch.strip())
            ], limit=1)
        if schedule and 'exclude_full' in schedule._fields:
            return bool(schedule.exclude_full)
        return True

    def _has_shipping_guide(self):
        """
        Determina si la orden de venta ya cuenta con guía de envío asignada:
        - yuju_carrier_tracking_ref / data_tracking_readwrite con valor
        - adjuntos en data_attachment_ids / attachments (sale.order.attachment)
        - data_ready_to_pick activo
        - algún stock.picking activo con carrier_tracking_ref
        """
        self.ensure_one()
        tracking = (getattr(self, 'yuju_carrier_tracking_ref', '') or '').strip()
        if tracking and tracking.lower() != 'false':
            return True
        if getattr(self, 'data_ready_to_pick', False):
            return True
        attach_ids = getattr(self, 'data_attachment_ids', False) or getattr(self, 'attachments', False)
        if attach_ids and len(attach_ids) > 0:
            return True
        for picking in getattr(self, 'picking_ids', []):
            if picking.state != 'cancel' and (getattr(picking, 'carrier_tracking_ref', '') or '').strip():
                return True
        return False

    def _get_guide_assigned_date(self):
        """
        Obtiene la fecha (UTC naive) en que se agregó la guía de envío a la orden.
        Busca primero en wmds.log o en sale.order.attachment, y si no existe usa el momento actual.
        """
        self.ensure_one()
        try:
            if 'wmds.log' in self.env:
                log_rec = self.env['wmds.log'].sudo().search([
                    ('sale', '=', self.id),
                    ('log', 'ilike', 'guía'),
                ], order='date desc, id desc', limit=1)
                if log_rec and log_rec.date:
                    return log_rec.date
        except Exception:
            pass
        try:
            attach_ids = getattr(self, 'data_attachment_ids', False) or getattr(self, 'attachments', False)
            if attach_ids:
                dates = [a.create_date for a in attach_ids if getattr(a, 'create_date', False)]
                if dates:
                    return max(dates)
        except Exception:
            pass
        return fields.Datetime.now()

    def _get_next_business_day_end(self, local_dt):
        """
        Retorna el día siguiente hábil (Lunes-Viernes) a las 23:59:59 en hora local.
        Si local_dt es Viernes, Sábado o Domingo, el día siguiente hábil es Lunes.
        """
        next_day = local_dt + timedelta(days=1)
        while next_day.weekday() in (5, 6):  # 5=Sábado, 6=Domingo
            next_day += timedelta(days=1)
        return next_day.replace(hour=23, minute=59, second=59)

    def _get_business_day(self, date_val, restrict_only_sunday):
        if not date_val:
            return False
        weekday = date_val.weekday()  # 0=Lunes, 6=Domingo
        if restrict_only_sunday:
            if weekday == 6:  # Domingo -> Lunes (+1 día)
                return date_val + timedelta(days=1)
        else:
            if weekday == 5:  # Sábado -> Lunes (+2 días)
                return date_val + timedelta(days=2)
            elif weekday == 6:  # Domingo -> Lunes (+1 día)
                return date_val + timedelta(days=1)
        return date_val

    def _compute_sla_value_date(self, channel_name, base_date, fulfillment):
        """ Calcula SLA según marketplace.schedule específica """
        if not base_date or not channel_name:
            return False, None, False

        clean_channel = normalize_channel(channel_name)
        # Búsqueda estricta por nombre de marketplace específico
        schedule = self.env['marketplace.schedule'].search([
            ('marketplace', '=ilike', clean_channel.strip())
        ], limit=1)

        # SOLO debe calcularse si existe una regla por marketplace específica y su origen lo permite
        if not schedule or schedule.sla_source == 'yuju':
            return False, None, False

        fulfillment_clean = str(fulfillment).lower().strip() if fulfillment else ""
        if getattr(schedule, 'exclude_full', True):
            if fulfillment_clean in ('fbc', 'full') or (len(self) == 1 and self._is_full_order()):
                return False, None, False

        local_date = base_date + timedelta(hours=UTC_LOCAL)
        m_clean = (clean_channel or "").lower().replace(" ", "")
        sla_final = False

        # Determinar si el marketplace se almacena en UTC o Local según wmds.mysql.config
        is_utc = False
        if clean_channel:
            ch_low = clean_channel.lower()
            if 'amazon' in ch_low or 'walmart' in ch_low:
                is_utc = True
            try:
                cfg = self.env['wmds.mysql.config'].sudo().search([
                    ('marketplace', 'ilike', clean_channel.strip())
                ], limit=1)
                if cfg and cfg.sla_timezone:
                    is_utc = (cfg.sla_timezone == 'utc')
            except Exception:
                pass

        if ('mercadolibre' in m_clean or 'mercado' in m_clean) and (fulfillment_clean == 'fbf') and (schedule.flex and schedule.flex > 0):
            flex_mins = schedule.flex
            limit_hour_time = datetime.strptime('12:00:00', '%H:%M:%S').time()
            if local_date.time() <= limit_hour_time:
                sla_final = local_date + timedelta(minutes=flex_mins)
            else:
                next_day = local_date.replace(hour=0, minute=0, second=0) + timedelta(days=1)
                sla_final = next_day + timedelta(hours=11, minutes=59, seconds=59)
            if is_utc:
                sla_final = sla_final - timedelta(hours=UTC_LOCAL)
        elif ('mercadolibre' in m_clean or 'mercado' in m_clean) and len(self) == 1 and not getattr(self, 'yuju_due_date', False):
            # Regla Mercado Libre No-Full sin SLA de Yuju:
            # 1) Al ingresar la orden (sin guía de envío): 15 días naturales desde el ingreso.
            # 2) En cuanto se agrega la guía de envío: entrega al día siguiente hábil.
            if self._has_shipping_guide():
                guide_dt = self._get_guide_assigned_date() or base_date
                guide_local = guide_dt + timedelta(hours=UTC_LOCAL)
                target_local = self._get_next_business_day_end(guide_local)
            else:
                target_local = (local_date + timedelta(days=15)).replace(hour=23, minute=59, second=59)
            sla_final = target_local if not is_utc else (target_local - timedelta(hours=UTC_LOCAL))
        elif schedule.days_after_creation and schedule.days_after_creation > 0:
            if schedule.only_business_days:
                curr_date = local_date
                added = 0
                while added < schedule.days_after_creation:
                    curr_date += timedelta(days=1)
                    if curr_date.weekday() not in (5, 6):
                        added += 1
                target_local = curr_date.replace(hour=23, minute=59, second=59)
            else:
                target_local = local_date.replace(hour=23, minute=59, second=59) + timedelta(days=schedule.days_after_creation)

            # Restricción de fin de semana aplicada sobre hora local
            if not schedule.only_business_days:
                is_weekend = target_local.weekday() in (5, 6)
                target_local = self._get_business_day(target_local, restrict_only_sunday=is_weekend)

            sla_final = target_local if not is_utc else (target_local - timedelta(hours=UTC_LOCAL))
        else:
            # Horarios por día de la semana
            curr_local = local_date
            hours_to_add = 0.0
            days_advanced = 0

            # Si el día actual no tiene horas configuradas (> 0), avanzar al siguiente día que sí opere
            for _ in range(7):
                dow = curr_local.strftime('%A').lower()
                if dow in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday']:
                    h = schedule.monday_to_friday_
                elif dow == 'saturday':
                    h = schedule.saturday
                elif dow == 'sunday':
                    h = schedule.sunday
                else:
                    h = 0.0

                if h > 0:
                    hours_to_add = h
                    break
                curr_local = (curr_local + timedelta(days=1)).replace(hour=0, minute=0, second=0)
                days_advanced += 1

            if hours_to_add > 0:
                if days_advanced > 0:
                    target_local = curr_local.replace(hour=23, minute=59, second=59)
                else:
                    target_local = (local_date + timedelta(hours=hours_to_add)).replace(hour=23, minute=59, second=59)

                if m_clean in ['ventasdepiso', 'mayoreonaes']:
                    target_local = target_local.replace(hour=15, minute=0, second=0)
                    target_local = self._get_business_day(target_local, False)
                else:
                    is_weekend = target_local.weekday() in (5, 6)
                    target_local = self._get_business_day(target_local, restrict_only_sunday=is_weekend)

                sla_final = target_local if not is_utc else (target_local - timedelta(hours=UTC_LOCAL))

        return sla_final, None, sla_final

    def calculate_and_assign_sla(self, force=False):
        """ Calcula y asigna SLA en la orden de venta y propaga a sus albaranes """
        for order in self:
            if order.sla_manual_override and not force:
                continue

            channel = normalize_channel(order.channel or '')
            if not channel:
                continue

            # Buscar regla ESPECÍFICA para este marketplace
            schedule = self.env['marketplace.schedule'].search([
                ('marketplace', '=ilike', channel.strip())
            ], limit=1)

            # Si la orden es Full y la regla excluye Full (activo por defecto), no calcular ni asignar SLA
            exclude_full = self._should_exclude_full_for_channel(channel, schedule=schedule)
            if exclude_full and order._is_full_order():
                if order.sla_date and (not order.sla_manual_override or order.sla_override_reason == 'prior_to_entry'):
                    order.write({
                        'sla_date': False,
                        'sla_manual_override': False,
                        'sla_priority_level': 'normal',
                        'sla_priority_label': '',
                    })
                continue

            sla = False

            # Si NO existe regla específica para este marketplace, NO calcular SLA
            if not schedule:
                # Solo respetar yuju_due_date si viene directo del marketplace
                if getattr(order, 'yuju_due_date', False):
                    parsed_yuju = parse_yuju_date(order.yuju_due_date)
                    if parsed_yuju:
                        sla = parsed_yuju

                if order.sla_date != sla:
                    vals = {'sla_date': sla}
                    if not order.sla_original_date and sla:
                        vals['sla_original_date'] = sla
                    order.write(vals)
                continue

            sla_source = schedule.sla_source or 'auto'

            # Evaluar yuju_due_date directamente si la regla lo contempla
            if sla_source in ('yuju', 'auto') and getattr(order, 'yuju_due_date', False):
                parsed_yuju = parse_yuju_date(order.yuju_due_date)
                if parsed_yuju:
                    sla = parsed_yuju

            # SOLO calcular si la regla específica lo indica explícitamente:
            # - 'calculated': siempre calcula
            # - 'auto': calcula SOLO si no vino fecha de Yuju
            # - 'yuju': NUNCA calcula (si falta Yuju, queda sin SLA)
            if (sla_source == 'calculated') or (sla_source == 'auto' and not sla):
                base_dt = order.date_order or order.create_date or fields.Datetime.now()
                calc_sla, _, _ = order._compute_sla_value_date(
                    channel, base_dt, getattr(order, 'fulfillment', False)
                )
                if calc_sla:
                    sla = calc_sla

            if sla:
                # -------------------------------------------------------------
                # Verificación de SLA previo a fecha de ingreso
                # (ej. pedidos de Mercado Libre retenidos por antifraude o ingresados tarde)
                # -------------------------------------------------------------
                entry_dt = order.create_date or order.date_order or fields.Datetime.now()
                if order.create_date and order.date_order:
                    entry_dt = max(order.create_date, order.date_order)

                is_utc = False
                if channel:
                    ch_low = channel.lower()
                    if 'amazon' in ch_low or 'walmart' in ch_low:
                        is_utc = True
                    try:
                        cfg = self.env['wmds.mysql.config'].sudo().search([
                            ('marketplace', 'ilike', channel.strip())
                        ], limit=1)
                        if cfg and cfg.sla_timezone:
                            is_utc = (cfg.sla_timezone == 'utc')
                    except Exception:
                        pass

                entry_compare = entry_dt if is_utc else (entry_dt + timedelta(hours=UTC_LOCAL))

                if sla < entry_compare:
                    old_sla = sla
                    calc_sla, _, calc_prio = order._compute_sla_value_date(
                        channel, entry_dt, getattr(order, 'fulfillment', False)
                    )

                    new_sla = False
                    if calc_sla:
                        cand_sla = calc_sla if is_utc else (calc_sla + timedelta(hours=UTC_LOCAL))
                        if cand_sla > entry_compare:
                            new_sla = cand_sla

                    if not new_sla:
                        # Fallback a siguiente ventana de recolección
                        limit_hour = datetime.strptime('12:00:00', '%H:%M:%S').time()
                        if entry_compare.time() <= limit_hour:
                            target_local = entry_compare.replace(hour=23, minute=59, second=59)
                        else:
                            target_local = (entry_compare + timedelta(days=1)).replace(hour=11, minute=59, second=59)
                        target_local = order._get_business_day(target_local, restrict_only_sunday=False)
                        new_sla = target_local if not is_utc else (target_local - timedelta(hours=UTC_LOCAL))

                    now_dt = fields.Datetime.now()
                    now_user_id = self.env.user.id if self.env.user else 1
                    reason = 'prior_to_entry'
                    entry_str = entry_compare.strftime('%Y-%m-%d %H:%M:%S')
                    old_sla_str = old_sla.strftime('%Y-%m-%d %H:%M:%S') if hasattr(old_sla, 'strftime') else str(old_sla)
                    new_sla_str = new_sla.strftime('%Y-%m-%d %H:%M:%S') if hasattr(new_sla, 'strftime') else str(new_sla)

                    note = (
                        f"Empuje automático: SLA original ({old_sla_str}) era previo a la fecha de ingreso a Odoo "
                        f"({entry_str}). Reasignado automáticamente a {new_sla_str}."
                    )

                    vals = {
                        'sla_date': new_sla,
                        'sla_original_date': old_sla,
                        'sla_manual_override': True,
                        'sla_override_reason': reason,
                        'sla_override_note': note,
                        'sla_override_date': now_dt,
                        'sla_override_user_id': now_user_id,
                    }
                    if not order.sla_original_date and old_sla:
                        vals['sla_original_date'] = old_sla
                    order.write(vals)

                    # Registrar en historial de modificaciones SLA
                    self.env['sale.order.sla.history'].create({
                        'sale_id': order.id,
                        'user_id': now_user_id,
                        'date': now_dt,
                        'old_sla_date': old_sla,
                        'new_sla_date': new_sla,
                        'reason': reason,
                        'note': note,
                    })

                    # Registrar nota en chatter
                    msg = (
                        f"<b>SLA Empujado Automáticamente al Ingreso</b><br/>"
                        f"<b>Fecha de Ingreso Odoo:</b> {entry_str}<br/>"
                        f"<b>SLA Original Marketplace:</b> {old_sla_str}<br/>"
                        f"<b>Nueva Fecha SLA Asignada:</b> {new_sla_str}<br/>"
                        f"<b>Motivo:</b> SLA recibido previo a fecha de ingreso al almacén.<br/>"
                    )
                    try:
                        order.message_post(body=msg)
                    except Exception:
                        pass

                    continue

                vals = {
                    'sla_date': sla,
                }
                if not order.sla_original_date and sla:
                    vals['sla_original_date'] = sla
                order.write(vals)

    @api.model
    def _cron_update_sla_priorities(self):
        """ Cron job ejecutable periódicamente para recalcular y sincronizar SLA en órdenes de venta y albaranes """
        # 1. Limpiar SLA de órdenes Full (fbc / almacenes Fulfillment) donde exclude_full esté activo
        allowed_full_channels = [
            normalize_channel(s.marketplace)
            for s in self.env['marketplace.schedule'].sudo().search([('exclude_full', '=', False)])
            if s.marketplace
        ]
        if allowed_full_channels:
            self.env.cr.execute("""
                UPDATE sale_order s
                SET sla_date = NULL,
                    sla_manual_override = false,
                    sla_priority_level = 'normal',
                    sla_priority_label = ''
                FROM stock_warehouse w
                WHERE w.id = s.warehouse_id
                  AND s.sla_date IS NOT NULL
                  AND (COALESCE(s.sla_manual_override, false) = false OR s.sla_override_reason = 'prior_to_entry')
                  AND (
                      lower(COALESCE(s.fulfillment, '')) IN ('fbc', 'full')
                      OR upper(COALESCE(w.code, '')) IN %s
                      OR lower(COALESCE(w.name, '')) LIKE '%%fulfillment%%'
                  )
                  AND COALESCE(s.channel, '') NOT IN %s;
            """, (FULL_WAREHOUSE_CODES, tuple(allowed_full_channels)))
        else:
            self.env.cr.execute("""
                UPDATE sale_order s
                SET sla_date = NULL,
                    sla_manual_override = false,
                    sla_priority_level = 'normal',
                    sla_priority_label = ''
                FROM stock_warehouse w
                WHERE w.id = s.warehouse_id
                  AND s.sla_date IS NOT NULL
                  AND (COALESCE(s.sla_manual_override, false) = false OR s.sla_override_reason = 'prior_to_entry')
                  AND (
                      lower(COALESCE(s.fulfillment, '')) IN ('fbc', 'full')
                      OR upper(COALESCE(w.code, '')) IN %s
                      OR lower(COALESCE(w.name, '')) LIKE '%%fulfillment%%'
                  );
            """, (FULL_WAREHOUSE_CODES,))

        # 2. Buscar órdenes Drop / Flex (no Full) sin SLA para calcularles su SLA
        active_schedules = self.env['marketplace.schedule'].sudo().search([])
        calc_channels = list({
            s.marketplace.strip() for s in active_schedules
            if s.marketplace and s.sla_source in ('auto', 'calculated')
        })
        domain_missing = [
            ('state', '!=', 'cancel'),
            ('sla_date', '=', False),
        ] + self._get_non_full_domain()

        if calc_channels:
            domain_missing += [
                '|',
                ('channel', 'in', calc_channels),
                '&', ('yuju_due_date', '!=', False), ('yuju_due_date', '!=', ''),
            ]
        else:
            domain_missing += [
                ('yuju_due_date', '!=', False),
                ('yuju_due_date', '!=', ''),
            ]

        orders_without_sla = self.search(domain_missing, order='date_order desc, id desc', limit=1000)
        for order in orders_without_sla:
            try:
                order.calculate_and_assign_sla()
            except Exception as e:
                _logger.error(f"Error calculando SLA cron para venta {order.name}: {e}")

        # Actualización masiva por SQL de niveles de prioridad y etiquetas en sale_order
        sql_so = """
            UPDATE sale_order
            SET sla_priority_level = CASE
                    WHEN sla_date < (NOW() AT TIME ZONE 'UTC') THEN 'overdue'
                    WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '1 hour' THEN 'critical_1h'
                    WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '2 hours' THEN 'urgent_2h'
                    WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '6 hours' THEN 'warning_6h'
                    WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '24 hours' THEN 'notice_24h'
                    ELSE 'normal'
                END,
                sla_priority_label = CASE
                    WHEN sla_date < (NOW() AT TIME ZONE 'UTC') THEN 'SLA Vencido'
                    WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '1 hour' THEN 'Está a punto de llegar la recolecta (< 1h)'
                    WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '2 hours' THEN 'Quedan 2 horas para la recolecta'
                    WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '6 hours' THEN 'Quedan 6 horas para la recolecta'
                    WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '24 hours' THEN 'Quedan unas horas para la recolecta'
                    ELSE 'En tiempo'
                END
            WHERE state != 'cancel'
              AND sla_date IS NOT NULL
              AND (
                  sla_priority_level IS DISTINCT FROM CASE
                      WHEN sla_date < (NOW() AT TIME ZONE 'UTC') THEN 'overdue'
                      WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '1 hour' THEN 'critical_1h'
                      WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '2 hours' THEN 'urgent_2h'
                      WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '6 hours' THEN 'warning_6h'
                      WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '24 hours' THEN 'notice_24h'
                      ELSE 'normal'
                  END
              );
        """
        self.env.cr.execute(sql_so)
        updated_so = self.env.cr.rowcount

        # Actualización masiva por SQL de niveles de prioridad y etiquetas en stock_picking (solo si existen columnas almacenadas)
        updated_sp = 0
        self.env.cr.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'stock_picking' AND column_name = 'sla_date'
        """)
        if self.env.cr.fetchone():
            sql_sp = """
                UPDATE stock_picking
                SET sla_priority_level = CASE
                        WHEN sla_date < (NOW() AT TIME ZONE 'UTC') THEN 'overdue'
                        WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '1 hour' THEN 'critical_1h'
                        WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '2 hours' THEN 'urgent_2h'
                        WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '6 hours' THEN 'warning_6h'
                        WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '24 hours' THEN 'notice_24h'
                        ELSE 'normal'
                    END,
                    sla_priority_label = CASE
                        WHEN sla_date < (NOW() AT TIME ZONE 'UTC') THEN 'SLA Vencido'
                        WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '1 hour' THEN 'Está a punto de llegar la recolecta (< 1h)'
                        WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '2 hours' THEN 'Quedan 2 horas para la recolecta'
                        WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '6 hours' THEN 'Quedan 6 horas para la recolecta'
                        WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '24 hours' THEN 'Quedan unas horas para la recolecta'
                        ELSE 'En tiempo'
                    END
                WHERE state NOT IN ('done', 'cancel')
                  AND sla_date IS NOT NULL
                  AND (
                      sla_priority_level IS DISTINCT FROM CASE
                          WHEN sla_date < (NOW() AT TIME ZONE 'UTC') THEN 'overdue'
                          WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '1 hour' THEN 'critical_1h'
                          WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '2 hours' THEN 'urgent_2h'
                          WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '6 hours' THEN 'warning_6h'
                          WHEN sla_date <= (NOW() AT TIME ZONE 'UTC') + interval '24 hours' THEN 'notice_24h'
                          ELSE 'normal'
                      END
                  );
            """
            self.env.cr.execute(sql_sp)
            updated_sp = self.env.cr.rowcount

        _logger.info(f"Cron SLA Priorities: Actualizadas {updated_so} órdenes de venta y {updated_sp} albaranes.")
        return {'updated_so': updated_so, 'updated_sp': updated_sp}

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('channel'):
                vals['channel'] = normalize_channel(vals['channel'])
        orders = super(SaleOrder, self).create(vals_list)
        for order in orders:
            try:
                order.calculate_and_assign_sla()
            except Exception as e:
                _logger.error(f"Error asignando SLA al crear orden {order.name}: {e}")
        return orders

    def write(self, vals):
        if vals.get('channel'):
            vals['channel'] = normalize_channel(vals['channel'])
        res = super(SaleOrder, self).write(vals)
        trigger_fields = {
            'channel', 'date_order', 'fulfillment', 'yuju_due_date', 'warehouse_id',
            'yuju_carrier_tracking_ref', 'data_tracking_readwrite',
            'data_attachment_ids', 'attachments', 'data_ready_to_pick',
        }
        if trigger_fields.intersection(vals.keys()):
            guide_fields = {
                'yuju_carrier_tracking_ref', 'data_tracking_readwrite',
                'data_attachment_ids', 'attachments', 'data_ready_to_pick',
            }
            is_guide_update = bool(guide_fields.intersection(vals.keys()))
            for order in self:
                if not order.sla_manual_override or (is_guide_update and order.sla_override_reason == 'prior_to_entry'):
                    try:
                        order.calculate_and_assign_sla(force=is_guide_update and order.sla_override_reason == 'prior_to_entry')
                    except Exception as e:
                        _logger.error(f"Error recalculando SLA en write de orden {order.name}: {e}")
        return res

    def action_open_sla_override_wizard(self):
        """ Abre el wizard para reasignar / empujar SLA desde la orden """
        self.ensure_one()
        return {
            'name': _('Reasignar / Empujar SLA'),
            'type': 'ir.actions.act_window',
            'res_model': 'sla.override.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'active_model': 'sale.order',
                'active_ids': [self.id],
                'active_id': self.id,
            }
        }


class StockPickingSLA(models.Model):
    _inherit = 'stock.picking'

    def write(self, vals):
        res = super(StockPickingSLA, self).write(vals)
        if 'carrier_tracking_ref' in vals and vals.get('carrier_tracking_ref'):
            for picking in self:
                order = picking.sale_id
                if order and (not order.sla_manual_override or order.sla_override_reason == 'prior_to_entry'):
                    try:
                        order.calculate_and_assign_sla(force=(order.sla_override_reason == 'prior_to_entry'))
                    except Exception as e:
                        _logger.error(f"Error recalculando SLA al asignar guía en picking {picking.name}: {e}")
        return res
