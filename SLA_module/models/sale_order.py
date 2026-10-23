# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from datetime import datetime, timedelta
import logging

_logger = logging.getLogger(__name__)

UTC_LOCAL = -6


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
        return fields.Datetime.to_datetime(clean_str)
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
        ('other', 'Otro motivo'),
    ], string="Motivo Reasignación SLA", copy=False)
    sla_override_note = fields.Text(string="Nota / Justificación Reasignación", copy=False)
    sla_override_date = fields.Datetime(string="Fecha de Reasignación", copy=False)
    sla_override_user_id = fields.Many2one('res.users', string="Reasignado por", copy=False)
    sla_original_date = fields.Datetime(string="Fecha SLA Original", copy=False)

    sla_grace_applied = fields.Boolean(
        string="SLA de Gracia Aplicado",
        default=False,
        copy=False,
        help="Indica si la fecha de Yuju llegó vencida y se calculó un SLA de gracia a partir de la entrada a Odoo."
    )
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
                    order.sla_priority_label = 'Queda 1 día para la recolecta'
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
        """ Calcula SLA según marketplace.schedule """
        if not base_date:
            return False, None, False

        schedule = False
        if channel_name:
            schedule = self.env['marketplace.schedule'].search([
                ('marketplace', '=', channel_name)
            ], limit=1)
            if not schedule:
                schedules = self.env['marketplace.schedule'].search([])
                for sch in schedules:
                    if sch.marketplace and sch.marketplace.lower() in channel_name.lower():
                        schedule = sch
                        break

        if not schedule:
            return False, None, False

        local_date = base_date + timedelta(hours=UTC_LOCAL)
        day_of_week = local_date.strftime('%A').lower()
        limit_hour_time = datetime.strptime('12:00:00', '%H:%M:%S').time()

        m_clean = (channel_name or "").lower().replace(" ", "")
        fulfillment_clean = str(fulfillment).lower() if fulfillment else ""
        sla_final = False

        if ('mercadolibre' in m_clean or 'mercado' in m_clean) and (fulfillment_clean == 'fbf'):
            flex_mins = schedule.flex or 0
            if flex_mins > 0:
                if local_date.time() <= limit_hour_time:
                    sla_final = base_date + timedelta(minutes=flex_mins)
                else:
                    next_day = local_date.replace(hour=0, minute=0, second=0) + timedelta(days=1)
                    sla_final = next_day + timedelta(hours=11, minutes=59, seconds=59) - timedelta(hours=UTC_LOCAL)
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

            sla_final = target_local - timedelta(hours=UTC_LOCAL)
            if not schedule.only_business_days:
                is_weekend = target_local.weekday() in (5, 6)
                sla_final = self._get_business_day(sla_final, restrict_only_sunday=is_weekend)
        else:
            hours_to_add = 0
            is_weekend = False

            if day_of_week in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday']:
                hours_to_add = schedule.monday_to_friday_
            elif day_of_week == 'saturday':
                hours_to_add = schedule.saturday
                is_weekend = True
            elif day_of_week == 'sunday':
                hours_to_add = schedule.sunday
                is_weekend = True

            if hours_to_add >= 0:
                sla_final = local_date + timedelta(hours=hours_to_add)
                if m_clean in ['ventasdepiso', 'mayoreonaes']:
                    sla_final = sla_final.replace(hour=15, minute=0, second=0)
                    sla_final = sla_final - timedelta(hours=UTC_LOCAL)
                    sla_final = self._get_business_day(sla_final, False)
                else:
                    sla_final = sla_final.replace(hour=23, minute=59, second=59)
                    sla_final = sla_final - timedelta(hours=UTC_LOCAL)
                    restrict_sunday = True if (is_weekend and not schedule.only_business_days) else False
                    sla_final = self._get_business_day(sla_final, restrict_sunday)

        if schedule.auto_fill_dates and sla_final:
            return sla_final, None, sla_final

        return sla_final, None, None

    def calculate_and_assign_sla(self, force=False):
        """ Calcula y asigna SLA en la orden de venta y propaga a sus albaranes """
        for order in self:
            if order.sla_manual_override and not force:
                continue

            channel = order.channel or ''
            schedule = False
            if channel:
                schedule = self.env['marketplace.schedule'].search([
                    ('marketplace', '=', channel)
                ], limit=1)
                if not schedule:
                    schedules = self.env['marketplace.schedule'].search([])
                    for sch in schedules:
                        if sch.marketplace and sch.marketplace.lower() in channel.lower():
                            schedule = sch
                            break

            sla_source = schedule.sla_source if schedule else 'auto'
            sla = False
            prio = False
            grace = False

            # Evaluar yuju_due_date
            if sla_source in ('yuju', 'auto') and getattr(order, 'yuju_due_date', False):
                parsed_yuju = parse_yuju_date(order.yuju_due_date)
                if parsed_yuju:
                    # Comparar con fecha de ingreso a Odoo
                    entry_dt = order.create_date or order.date_order or fields.Datetime.now()
                    if parsed_yuju <= entry_dt:
                        # yuju_due_date ya venía vencida al ingresar a Odoo
                        # Aplicar SLA de gracia calculando según regla de schedule desde entrada
                        calc_sla, _, calc_prio = order._compute_sla_value_date(
                            channel, entry_dt, getattr(order, 'fulfillment', False)
                        )
                        if calc_sla:
                            sla = calc_sla
                            prio = calc_prio
                            grace = True
                        else:
                            sla = parsed_yuju
                            grace = False
                    else:
                        sla = parsed_yuju
                        if schedule and schedule.auto_fill_dates:
                            prio = parsed_yuju

            if not sla or sla_source == 'calculated':
                base_dt = order.date_order or order.create_date or fields.Datetime.now()
                calc_sla, _, calc_prio = order._compute_sla_value_date(
                    channel, base_dt, getattr(order, 'fulfillment', False)
                )
                if calc_sla:
                    sla = calc_sla
                    prio = calc_prio

            if sla:
                vals = {
                    'sla_date': sla,
                    'priority_date': prio or sla,
                    'sla_grace_applied': grace,
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
        orders = super(SaleOrder, self).create(vals_list)
        for order in orders:
            try:
                order.calculate_and_assign_sla()
            except Exception as e:
                _logger.error(f"Error asignando SLA al crear orden {order.name}: {e}")
        return orders

    def write(self, vals):
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
