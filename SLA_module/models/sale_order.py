# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from datetime import datetime, timedelta
import logging

_logger = logging.getLogger(__name__)

UTC_LOCAL = -6

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



def parse_yuju_date(dt_str):
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
        # yuju_due_date viene en hora local CDMX (UTC-6); convertir a UTC para guardar en DB Odoo
        return local_dt - timedelta(hours=UTC_LOCAL)
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
    priority_date = fields.Datetime(string="Priority Date", default=False, copy=False)
    
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
        now_local = now_utc + timedelta(hours=UTC_LOCAL)
        for order in self:
            if order.state == 'cancel' or not order.sla_date:
                order.sla_priority_level = 'normal'
                order.sla_priority_label = ''
                continue

            effective_sla = order.sla_date
            channel = getattr(order, 'channel', False) or ''
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

            target_now = now_utc if is_utc else now_local

            if effective_sla < target_now:
                order.sla_priority_level = 'overdue'
                order.sla_priority_label = 'SLA Vencido'
            elif effective_sla <= target_now + timedelta(hours=1):
                order.sla_priority_level = 'critical_1h'
                order.sla_priority_label = 'Está a punto de llegar la recolecta (< 1h)'
            else:
                diff_hours = (effective_sla - target_now).total_seconds() / 3600.0
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

        local_date = base_date + timedelta(hours=UTC_LOCAL)
        m_clean = (clean_channel or "").lower().replace(" ", "")
        fulfillment_clean = str(fulfillment).lower() if fulfillment else ""
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

        if ('mercadolibre' in m_clean or 'mercado' in m_clean) and (fulfillment_clean == 'fbf'):
            flex_mins = schedule.flex or 0
            limit_hour_time = datetime.strptime('12:00:00', '%H:%M:%S').time()
            if flex_mins > 0:
                if local_date.time() <= limit_hour_time:
                    sla_final = local_date + timedelta(minutes=flex_mins)
                else:
                    next_day = local_date.replace(hour=0, minute=0, second=0) + timedelta(days=1)
                    sla_final = next_day + timedelta(hours=11, minutes=59, seconds=59)
                if is_utc:
                    sla_final = sla_final - timedelta(hours=UTC_LOCAL)
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

        if schedule.auto_fill_dates and sla_final:
            return sla_final, None, sla_final

        return sla_final, None, None

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

            sla = False
            prio = False

            # Si NO existe regla específica para este marketplace, NO calcular SLA
            if not schedule:
                # Solo respetar yuju_due_date si viene directo del marketplace
                if getattr(order, 'yuju_due_date', False):
                    parsed_yuju = parse_yuju_date(order.yuju_due_date)
                    if parsed_yuju:
                        sla = parsed_yuju
                        prio = parsed_yuju

                if order.sla_date != sla:
                    order.write({'sla_date': sla, 'priority_date': prio})
                continue

            sla_source = schedule.sla_source or 'auto'

            # Evaluar yuju_due_date directamente si la regla lo contempla
            if sla_source in ('yuju', 'auto') and getattr(order, 'yuju_due_date', False):
                parsed_yuju = parse_yuju_date(order.yuju_due_date)
                if parsed_yuju:
                    sla = parsed_yuju
                    if schedule.auto_fill_dates:
                        prio = parsed_yuju

            # SOLO calcular si la regla específica lo indica explícitamente:
            # - 'calculated': siempre calcula
            # - 'auto': calcula SOLO si no vino fecha de Yuju
            # - 'yuju': NUNCA calcula (si falta Yuju, queda sin SLA)
            if (sla_source == 'calculated') or (sla_source == 'auto' and not sla):
                base_dt = order.date_order or order.create_date or fields.Datetime.now()
                calc_sla, _, calc_prio = order._compute_sla_value_date(
                    channel, base_dt, getattr(order, 'fulfillment', False)
                )
                if calc_sla:
                    sla = calc_sla
                    prio = calc_prio

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
                        'priority_date': new_sla,
                        'sla_original_date': old_sla,
                        'sla_manual_override': True,
                        'sla_override_reason': reason,
                        'sla_override_note': note,
                        'sla_override_date': now_dt,
                        'sla_override_user_id': now_user_id,
                    }
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

                    # Sincronizar transferencias abiertas
                    open_pickings = order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))
                    if open_pickings:
                        open_pickings.write({
                            'sla_date': new_sla,
                            'priority_date': new_sla,
                            'sla_original_date': old_sla,
                            'sla_manual_override': True,
                            'sla_override_reason': reason,
                            'sla_override_note': note,
                            'sla_override_date': now_dt,
                            'sla_override_user_id': now_user_id,
                        })
                        open_pickings._compute_sla_priority()

                    continue

                vals = {
                    'sla_date': sla,
                    'priority_date': prio or sla,
                }
                order.write(vals)

                # Sincronizar transferencias abiertas
                open_pickings = order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))
                if open_pickings:
                    open_pickings.write({
                        'sla_date': sla,
                        'priority_date': prio or sla,
                    })
                    open_pickings._compute_sla_priority()

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
        trigger_fields = {'channel', 'date_order', 'fulfillment', 'yuju_due_date'}
        if trigger_fields.intersection(vals.keys()):
            for order in self:
                if not order.sla_manual_override:
                    try:
                        order.calculate_and_assign_sla()
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
