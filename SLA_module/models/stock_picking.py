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

    @api.depends('sla_date', 'state')
    def _compute_sla_priority(self):
        now_utc = fields.Datetime.now()
        for picking in self:
            if picking.state in ('done', 'cancel') or not picking.sla_date:
                picking.sla_priority_level = 'normal'
                picking.sla_priority_label = ''
                continue

            effective_sla = picking.sla_date
            so = picking.sale_id
            if not effective_sla and so and getattr(so, 'yuju_due_date', False):
                parsed = parse_yuju_date(so.yuju_due_date)
                if parsed:
                    effective_sla = parsed

            if not effective_sla:
                picking.sla_priority_level = 'normal'
                picking.sla_priority_label = ''
                continue

            if effective_sla < now_utc:
                picking.sla_priority_level = 'overdue'
                picking.sla_priority_label = 'SLA Vencido'
            elif effective_sla <= now_utc + timedelta(hours=1):
                picking.sla_priority_level = 'critical_1h'
                picking.sla_priority_label = 'Está a punto de llegar la recolecta (< 1h)'
            else:
                diff_hours = (effective_sla - now_utc).total_seconds() / 3600.0
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
        """ Cron job ejecutable cada hora para recalcular alertas de prioridad """
        pickings_without_sla = self.search([
            ('state', 'not in', ('done', 'cancel')),
            ('sale_id', '!=', False),
            ('sla_date', '=', False),
            ('origin', '!=', False)
        ])
        for picking in pickings_without_sla:
            try:
                sale_order = picking.sale_id
                if not sale_order:
                    sale_order_name = picking.origin.split(',')[0].strip()
                    sale_order = self.env['sale.order'].search([('name', '=', sale_order_name)], limit=1)

                if sale_order:
                    channel = sale_order.channel or ''
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
                    sla, pup, prio = False, None, False

                    if sla_source in ('yuju', 'auto') and getattr(sale_order, 'yuju_due_date', False):
                        parsed_yuju = parse_yuju_date(sale_order.yuju_due_date)
                        if parsed_yuju:
                            sla = parsed_yuju
                            if schedule and schedule.auto_fill_dates:
                                prio = parsed_yuju

                    if not sla or sla_source == 'calculated':
                        fulfillment = getattr(sale_order, 'fulfillment', False)
                        date_order = sale_order.date_order
                        sla, pup, prio = self._compute_sla_value_date(channel, date_order, fulfillment)

                    if sla:
                        picking.write({
                            'sla_date': sla,
                            'priority_date': prio,
                        })
            except Exception as e:
                _logger.error(f"Error calculando SLA cron para {picking.name}: {e}")

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
            cutoff_float = schedule.collection_cutoff_time if schedule.collection_cutoff_time else 17.0
            cutoff_hours = int(cutoff_float)
            cutoff_minutes = int(round((cutoff_float - cutoff_hours) * 60))
            
            target_local = local_date.replace(hour=cutoff_hours, minute=cutoff_minutes, second=0) + timedelta(days=schedule.days_after_creation)
            sla_final = target_local - timedelta(hours=UTC_LOCAL)
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
                    sla_final = sla_final.replace(hour=12, minute=0, second=0)
                    sla_final = sla_final - timedelta(hours=UTC_LOCAL)
                    restrict_sunday = True if is_weekend else False
                    sla_final = self._get_business_day(sla_final, restrict_sunday)

        if schedule.auto_fill_dates and sla_final:
            return sla_final, None, sla_final
        
        return sla_final, None, None

    def action_confirm(self):
        """ Sobreescritura para calcular SLA al confirmar """
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
                    channel = sale_order.channel or ''
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
                    sla, pup, prio = False, None, False

                    if sla_source in ('yuju', 'auto') and getattr(sale_order, 'yuju_due_date', False):
                        parsed_yuju = parse_yuju_date(sale_order.yuju_due_date)
                        if parsed_yuju:
                            sla = parsed_yuju
                            if schedule and schedule.auto_fill_dates:
                                prio = parsed_yuju

                    if not sla or sla_source == 'calculated':
                        fulfillment = getattr(sale_order, 'fulfillment', False)
                        date_order = sale_order.date_order
                        sla, pup, prio = self._compute_sla_value_date(channel, date_order, fulfillment)

                    if sla:
                        picking.write({
                            'sla_date': sla,
                            'priority_date': prio,
                        })
                        _logger.info(f"SLA asignado para {picking.name}: {sla} (Origen: {sla_source})")
                    
                except Exception as e:
                    _logger.error(f"Error calculando SLA para picking {picking.name}: {e}")

        return res