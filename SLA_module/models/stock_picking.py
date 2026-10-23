# -*- coding: utf-8 -*-
from odoo import models, fields, api
from datetime import datetime, timedelta
import logging

_logger = logging.getLogger(__name__)

UTC_LOCAL = -6

def parse_yuju_date(dt_str):
    if not dt_str:
        return False
    try:
        if dt_str.endswith('Z'):
            dt_str = dt_str[:-1]
        if 'T' in dt_str:
            date_part, time_part = dt_str.split('T')
            time_parts = time_part.split(':')
            hour = time_parts[0]
            minute = time_parts[1]
            second = time_parts[2].split('.')[0] if len(time_parts) > 2 else '00'
            clean_str = f"{date_part} {hour}:{minute}:{second}"
        else:
            clean_str = dt_str.split('.')[0]
        return fields.Datetime.to_datetime(clean_str)
    except Exception as e:
        _logger.error(f"Error parseando yuju_due_date '{dt_str}': {e}")
        return False

class Picking(models.Model):
    _inherit = 'stock.picking'

    sla_date = fields.Datetime(string="Fecha límite SLA")
    priority_date = fields.Datetime(string="Priority date", default=False)
    
    sla_priority_level = fields.Selection([
        ('critical_1h', 'Crítico (< 1h)'),
        ('urgent_2h', 'Urgente (< 2h)'),
        ('warning_6h', 'Advertencia (< 6h)'),
        ('notice_24h', 'En Tiempo (< 24h)'),
        ('normal', 'Normal'),
        ('overdue', 'Vencido')
    ], string="Nivel Prioridad SLA", compute="_compute_sla_priority", store=True)

    sla_priority_label = fields.Char(string="Aviso Recolección SLA", compute="_compute_sla_priority", store=True)
    marketplace_info = fields.Text(string="Marketplace Information", compute="_compute_marketplace_info")

    # Campos para Reasignación Manual de SLA
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

    @api.depends('sla_date', 'state', 'sla_manual_override')
    def _compute_sla_priority(self):
        now_utc = fields.Datetime.now()
        now_local = now_utc + timedelta(hours=UTC_LOCAL)
        for picking in self:
            if picking.state in ('done', 'cancel') or not picking.sla_date:
                picking.sla_priority_level = 'normal'
                picking.sla_priority_label = ''
                continue

            effective_sla = picking.sla_date
            so = getattr(picking, 'sale_id', False)
            if not effective_sla and so and getattr(so, 'yuju_due_date', False):
                parsed = parse_yuju_date(so.yuju_due_date)
                if parsed:
                    effective_sla = parsed

            if not effective_sla:
                picking.sla_priority_level = 'normal'
                picking.sla_priority_label = ''
                continue

            # Determinar si el SLA está en UTC o local
            channel = (getattr(so, 'channel', False) or '') if so else ''
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
                picking.sla_priority_level = 'overdue'
                picking.sla_priority_label = 'SLA Vencido'
            elif effective_sla <= target_now + timedelta(hours=1):
                picking.sla_priority_level = 'critical_1h'
                picking.sla_priority_label = 'Está a punto de llegar la recolecta (< 1h)'
            else:
                diff_hours = (effective_sla - target_now).total_seconds() / 3600.0
                if diff_hours <= 2.0:
                    picking.sla_priority_level = 'urgent_2h'
                    picking.sla_priority_label = 'Quedan 2 horas para la recolecta'
                elif diff_hours <= 6.0:
                    picking.sla_priority_level = 'warning_6h'
                    picking.sla_priority_label = 'Quedan 6 horas para la recolecta'
                elif diff_hours <= 24.0:
                    picking.sla_priority_level = 'notice_24h'
                    picking.sla_priority_label = 'Queda 1 día para la recolecta'
                else:
                    picking.sla_priority_level = 'normal'
                    picking.sla_priority_label = 'En tiempo'

    @api.model
    def _cron_update_sla_priorities(self):
        """ Cron job ejecutable periódicamente para recalcular y sincronizar SLA en órdenes y albaranes """
        # 1. Asegurar cálculo en ventas activas sin SLA
        orders_without_sla = self.env['sale.order'].search([
            ('state', '!=', 'cancel'),
            ('sla_date', '=', False),
        ], limit=500)
        for order in orders_without_sla:
            try:
                order.calculate_and_assign_sla()
            except Exception as e:
                _logger.error(f"Error calculando SLA cron para venta {order.name}: {e}")

        # 2. Asignar SLA a albaranes sin SLA
        pickings_without_sla = self.search([
            ('state', 'not in', ('done', 'cancel')),
            ('sla_date', '=', False),
            ('origin', '!=', False)
        ], limit=500)
        for picking in pickings_without_sla:
            try:
                sale_order = picking.sale_id
                if not sale_order:
                    sale_order_name = picking.origin.split(',')[0].strip()
                    sale_order = self.env['sale.order'].search([('name', '=', sale_order_name)], limit=1)

                if sale_order:
                    if not sale_order.sla_date:
                        sale_order.calculate_and_assign_sla()
                    if sale_order.sla_date:
                        picking.write({
                            'sla_date': sale_order.sla_date,
                            'priority_date': sale_order.priority_date or sale_order.sla_date,
                        })
            except Exception as e:
                _logger.error(f"Error sincronizando SLA cron para {picking.name}: {e}")

        active_pickings = self.search([
            ('state', 'not in', ('done', 'cancel')),
            ('sale_id', '!=', False),
            ('sla_date', '!=', False)
        ])
        if active_pickings:
            active_pickings._compute_sla_priority()
            _logger.info(f"Cron SLA Priorities: Se actualizaron {len(active_pickings)} transferencias.")

    def _compute_marketplace_info(self):
        """ Informativo para saber qué regla aplicó """
        for picking in self:
            picking.marketplace_info = f"Calculado: {fields.Datetime.now()}"

    def _get_business_day(self, date_val, restrict_only_sunday):
        if not date_val:
            return False
            
        weekday = date_val.weekday() # 0=Lunes, 6=Domingo

        if restrict_only_sunday:
            if weekday == 6: # Domingo -> Lunes (+1 día)
                return date_val + timedelta(days=1)
        else:
            if weekday == 5: # Sábado -> Lunes (+2 días)
                return date_val + timedelta(days=2)
            elif weekday == 6: # Domingo -> Lunes (+1 día)
                return date_val + timedelta(days=1)
        
        return date_val

    def _compute_sla_value_date(self, channel_name, date_order, fulfillment):
        if not date_order:
            return False, False, False

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
            _logger.info(f"SLA: No se encontró horario para el marketplace {channel_name}")
            return False, False, False

        local_date = date_order + timedelta(hours=UTC_LOCAL)
        day_of_week = local_date.strftime('%A').lower()
        
        limit_hour_time = datetime.strptime('12:00:00', '%H:%M:%S').time()
        
        m_clean = (channel_name or "").lower().replace(" ", "")
        fulfillment_clean = str(fulfillment).lower() if fulfillment else ""

        sla_final = False

        if ('mercadolibre' in m_clean or 'mercado' in m_clean) and (fulfillment_clean == 'fbf'):
            flex_mins = schedule.flex or 0
            if flex_mins > 0:
                if local_date.time() <= limit_hour_time:
                    sla_final = date_order + timedelta(minutes=flex_mins)
                else:
                    next_day = local_date.replace(hour=0, minute=0, second=0) + timedelta(days=1)
                    sla_final = next_day + timedelta(hours=11, minutes=59, seconds=59) - timedelta(hours=UTC_LOCAL)
        elif schedule.days_after_creation and schedule.days_after_creation > 0:
            if schedule.only_business_days:
                curr_date = local_date
                added = 0
                while added < schedule.days_after_creation:
                    curr_date += timedelta(days=1)
                    if curr_date.weekday() not in (5, 6): # 5=Sábado, 6=Domingo
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

    def action_confirm(self):
        """ Sobreescritura para calcular/heredar SLA al confirmar """
        res = super(Picking, self).action_confirm()

        for picking in self:
            if not picking.origin:
                continue
                
            sale_order = picking.sale_id
            if not sale_order:
                sale_order_name = picking.origin.split(',')[0].strip()
                sale_order = self.env['sale.order'].search([('name', '=', sale_order_name)], limit=1)
            
            if sale_order:
                try:
                    if not sale_order.sla_date:
                        sale_order.calculate_and_assign_sla()

                    if sale_order.sla_date:
                        vals = {
                            'sla_date': sale_order.sla_date,
                            'priority_date': sale_order.priority_date or sale_order.sla_date,
                            'sla_manual_override': sale_order.sla_manual_override,
                            'sla_override_reason': sale_order.sla_override_reason,
                            'sla_override_note': sale_order.sla_override_note,
                            'sla_override_date': sale_order.sla_override_date,
                            'sla_override_user_id': sale_order.sla_override_user_id.id if sale_order.sla_override_user_id else False,
                            'sla_original_date': sale_order.sla_original_date,
                        }
                        picking.write(vals)
                        _logger.info(f"SLA heredado de orden {sale_order.name} para picking {picking.name}: {sale_order.sla_date}")
                except Exception as e:
                    _logger.error(f"Error heredando SLA para picking {picking.name}: {e}")
            else:
                try:
                    # Albarán sin venta directa
                    date_order = picking.date or fields.Datetime.now()
                    sla, pup, prio = self._compute_sla_value_date(False, date_order, False)
                    if sla:
                        picking.write({
                            'sla_date': sla,
                            'priority_date': prio,
                        })
                except Exception as e:
                    _logger.error(f"Error calculando SLA para picking sin venta {picking.name}: {e}")

        return res
