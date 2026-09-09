# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from datetime import datetime, timedelta, timezone
import logging

_logger = logging.getLogger(__name__)

# Configuración manual de zona horaria (CDMX UTC-6)
UTC_LOCAL = -6 

def parse_yuju_date(date_str):
    """ Parsea la fecha yuju_due_date a datetime UTC ingenuo para Odoo """
    if not date_str:
        return False
    date_str = str(date_str).strip()
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        pass
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d'):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            pass
    return False

class Picking(models.Model):
    _inherit = 'stock.picking'

    # Campos SLA unificados
    sla_date = fields.Datetime(
        string="SLA Date", 
        help='Fecha límite calculada según el esquema del equipo de ventas o Yuju'
    )
    priority_date = fields.Datetime(
        string="Priority Date", 
        help='Fecha de prioridad calculada'
    )
    
    sla_hours_remaining = fields.Float(
        string="Horas Restantes SLA", 
        compute="_compute_sla_priority", 
        store=True
    )
    sla_priority_level = fields.Selection([
        ('critical_1h', 'Crítico (< 1 hr)'),
        ('urgent_2h', 'Urgente (< 2 hrs)'),
        ('warning_6h', 'Advertencia (< 6 hrs)'),
        ('notice_24h', 'En Tiempo (< 24 hrs)'),
        ('normal', 'Normal (> 24 hrs)'),
        ('overdue', 'SLA Vencido')
    ], string="Nivel de Prioridad SLA", compute="_compute_sla_priority", store=True)
    
    sla_priority_label = fields.Char(
        string="Aviso de Recolección", 
        compute="_compute_sla_priority", 
        store=True
    )

    # Campo informativo del team
    crm_team_info = fields.Text(string="crm_team Information", compute="_compute_crm_team_info")

    pick_up_date = fields.Datetime(string="Fecha de Recolección")

    @api.depends('sla_date', 'state')
    def _compute_sla_priority(self):
        now = fields.Datetime.now()
        for picking in self:
            if picking.state in ('done', 'cancel') or not picking.sla_date:
                picking.sla_hours_remaining = 0.0
                picking.sla_priority_level = 'normal'
                picking.sla_priority_label = ''
                continue

            delta = (picking.sla_date - now).total_seconds() / 3600.0
            picking.sla_hours_remaining = delta

            if delta < 0:
                picking.sla_priority_level = 'overdue'
                picking.sla_priority_label = 'SLA Vencido'
            elif delta <= 1.0:
                picking.sla_priority_level = 'critical_1h'
                picking.sla_priority_label = 'Está a punto de llegar la recolecta'
            elif delta <= 2.0:
                picking.sla_priority_level = 'urgent_2h'
                picking.sla_priority_label = 'Quedan 2 horas para la recolecta'
            elif delta <= 6.0:
                picking.sla_priority_level = 'warning_6h'
                picking.sla_priority_label = 'Quedan 6 horas para la recolecta'
            elif delta <= 24.0:
                picking.sla_priority_level = 'notice_24h'
                picking.sla_priority_label = 'Queda 1 día para la recolecta'
            else:
                picking.sla_priority_level = 'normal'
                picking.sla_priority_label = 'En tiempo'

    @api.model
    def _cron_update_sla_priorities(self):
        """ Cron job ejecutable cada hora para recalcular alertas de prioridad """
        # 1. Calcular sla_date para transferencias activas que aún no lo tienen
        pickings_without_sla = self.search([
            ('state', 'not in', ('done', 'cancel')),
            ('sla_date', '=', False),
            ('origin', '!=', False)
        ])
        for picking in pickings_without_sla:
            try:
                sale_order_name = picking.origin.split(',')[0].strip()
                sale_order = self.env['sale.order'].search([('name', '=', sale_order_name)], limit=1)
                if sale_order and sale_order.team_id:
                    crm_team = sale_order.team_id.name
                    schedule = self.env['marketplace.schedule'].search([
                        ('crm_team', '=', sale_order.team_id.id)
                    ], limit=1)
                    if not schedule:
                        schedule = self.env['marketplace.schedule'].search([
                            ('crm_team.name', '=', crm_team)
                        ], limit=1)

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
                        sla, pup, prio = self._compute_sla_value_date(crm_team, date_order, fulfillment)

                    if sla:
                        picking.write({
                            'sla_date': sla,
                            'priority_date': prio,
                        })
            except Exception as e:
                _logger.error(f"Error calculando SLA cron para {picking.name}: {e}")

        # 2. Recalcular niveles y etiquetas para todas las transferencias activas con SLA
        active_pickings = self.search([
            ('state', 'not in', ('done', 'cancel')),
            ('sla_date', '!=', False)
        ])
        if active_pickings:
            active_pickings._compute_sla_priority()
            _logger.info(f"Cron SLA Priorities: Se actualizaron {len(active_pickings)} transferencias.")

    def _compute_crm_team_info(self):
        """ Informativo para saber qué regla aplicó """
        for picking in self:
            picking.crm_team_info = f"Calculado: {fields.Datetime.now()}"

    def _get_business_day(self, date_val, restrict_only_sunday):
        """ Ajusta la fecha si cae en fin de semana """
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

    def _compute_sla_value_date(self, crm_team_name, date_order, fulfillment):
        """
        Calcula la fecha SLA basada en el Schedule.
        """
        if not crm_team_name or not date_order:
            return False, False, False

        schedule = self.env['marketplace.schedule'].search([
            ('crm_team.name', '=', crm_team_name)
        ], limit=1)

        if not schedule:
            _logger.info(f"SLA: No se encontró horario para el team {crm_team_name}")
            return False, False, False

        local_date = date_order + timedelta(hours=UTC_LOCAL)
        day_of_week = local_date.strftime('%A').lower()
        
        limit_hour_time = datetime.strptime('12:00:00', '%H:%M:%S').time()
        
        team_clean = crm_team_name.lower().replace(" ", "")
        fulfillment_clean = str(fulfillment).lower() if fulfillment else ""

        sla_final = False

        if ('mercadolibre' in team_clean) and (fulfillment_clean == 'fbf'):
            flex_mins = schedule.flex or 0
            if flex_mins > 0:
                if local_date.time() <= limit_hour_time:
                    sla_final = date_order + timedelta(minutes=flex_mins)
                else:
                    next_day = local_date.replace(hour=0, minute=0, second=0) + timedelta(days=1)
                    sla_final = next_day + timedelta(hours=11, minutes=59, seconds=59) - timedelta(hours=UTC_LOCAL)
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
                
                if team_clean in ['ventasdepiso', 'mayoreonaes']:
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
                
            sale_order_name = picking.origin.split(',')[0].strip()
            sale_order = self.env['sale.order'].search([('name', '=', sale_order_name)], limit=1)
            
            if sale_order and sale_order.team_id:
                try:
                    crm_team = sale_order.team_id.name
                    schedule = self.env['marketplace.schedule'].search([
                        ('crm_team', '=', sale_order.team_id.id)
                    ], limit=1)
                    if not schedule:
                        schedule = self.env['marketplace.schedule'].search([
                            ('crm_team.name', '=', crm_team)
                        ], limit=1)

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
                        sla, pup, prio = self._compute_sla_value_date(crm_team, date_order, fulfillment)

                    picking.write({
                        'sla_date': sla,
                        'priority_date': prio,
                    })
                    
                    _logger.info(f"SLA asignado para {picking.name}: {sla} (Origen: {sla_source})")
                    
                except Exception as e:
                    _logger.error(f"Error calculando SLA para picking {picking.name}: {e}")
            else:
                 _logger.info(f"Picking {picking.name} sin orden de venta o equipo de ventas asociado.")

        return res