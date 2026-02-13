# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from datetime import datetime, timedelta
import logging

_logger = logging.getLogger(__name__)

# Configuración manual de zona horaria (CDMX UTC-6)
UTC_LOCAL = -6 

class Picking(models.Model):
    _inherit = 'stock.picking'

    # Campos SLA unificados
    sla_date = fields.Datetime(
        string="SLA Date", 
        help='Fecha límite calculada según el esquema del equipo de ventas'
    )
    priority_date = fields.Datetime(
        string="Priority Date", 
        help='Fecha de prioridad calculada'
    )
    
    # Campo informativo del team
    crm_team_info = fields.Text(string="crm_team Information", compute="_compute_crm_team_info")

    pick_up_date = fields.Datetime(string="Fecha de Recolección")

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

        # Buscar el horario del equipo
        # Usamos limit=1 para evitar errores de singleton
        schedule = self.env['marketplace.schedule'].search([
            ('crm_team.name', '=', crm_team_name)
        ], limit=1)

        if not schedule:
            _logger.info(f"SLA: No se encontró horario para el team {crm_team_name}")
            return False, False, False

        # Ajuste de zona horaria manual
        local_date = date_order + timedelta(hours=UTC_LOCAL)
        day_of_week = local_date.strftime('%A').lower() # monday, tuesday...
        
        limit_hour_time = datetime.strptime('12:00:00', '%H:%M:%S').time()
        
        team_clean = crm_team_name.lower().replace(" ", "")
        fulfillment_clean = str(fulfillment).lower() if fulfillment else ""

        sla_final = False

        #  MERCADO LIBRE + FLEX 
        if ('mercadolibre' in team_clean) and (fulfillment_clean == 'fbf'):
            flex_mins = schedule.flex or 0
            if flex_mins > 0:
                if local_date.time() <= limit_hour_time:
                    # Antes de las 12:00 -> Hoy + mins
                    sla_final = date_order + timedelta(minutes=flex_mins)
                else:
                    # Después de las 12:00 -> Mañana a las 11:59:59 AM (ajustado a UTC)
                    next_day = local_date.replace(hour=0, minute=0, second=0) + timedelta(days=1)
                    # Revertimos lógica inversa de la V15.0 para setear hora // 24 + (11 - (-6)) = 24 + 17 = 41 horas 
                    # Queremos las 11:59 AM hora local del día siguiente.
                    # 11:59 AM Local = 17:59 PM UTC (si es UTC-6)
                    sla_final = next_day + timedelta(hours=11, minutes=59, seconds=59) - timedelta(hours=UTC_LOCAL)
        
        # RESTO DE CASOS
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
                # Sumar horas
                sla_final = local_date + timedelta(hours=hours_to_add)
                
                # Piso o Mayoreo NAES -> Fija a las 3:00 PM
                if team_clean in ['ventasdepiso', 'mayoreonaes']:
                    # 3:00 PM Local = 15:00
                    sla_final = sla_final.replace(hour=15, minute=0, second=0)
                    # Convertir de vuelta a UTC
                    sla_final = sla_final - timedelta(hours=UTC_LOCAL)
                    
                    # Ajustar días hábiles (Normal)
                    sla_final = self._get_business_day(sla_final, False)
                else:
                    # Ajustar a las 12:00 PM
                    sla_final = sla_final.replace(hour=12, minute=0, second=0)
                    sla_final = sla_final - timedelta(hours=UTC_LOCAL)
                    
                    # Ajustar días hábiles (Restringido o normal segun fin de semana)
                    restrict_sunday = True if is_weekend else False
                    sla_final = self._get_business_day(sla_final, restrict_sunday)

        # Retorno de valores segun configuración auto_fill
        if schedule.auto_fill_dates and sla_final:
            return sla_final, None, sla_final # sla, pickup, priority
        
        return sla_final, None, None

    def action_confirm(self):
        """ Sobreescritura para calcular SLA al confirmar """
        res = super(Picking, self).action_confirm()

        for picking in self:
            if not picking.origin:
                continue
                
            # Buscar orden de venta relacionada
            # Tomamos la primera order
            sale_order_name = picking.origin.split(',')[0].strip()
            
            sale_order = self.env['sale.order'].search([('name', '=', sale_order_name)], limit=1)
            
            if sale_order and sale_order.team_id:
                try:
                    # Extraer datos necesarios
                    crm_team = sale_order.team_id.name
                    # Verificar si existe campo fulfillment en sale.order (custom)
                    fulfillment = getattr(sale_order, 'fulfillment', False)
                    date_order = sale_order.date_order

                    # Calcular
                    sla, pup, prio = self._compute_sla_value_date(crm_team, date_order, fulfillment)
                    
                    # Escribir en el picking
                    picking.write({
                        'sla_date': sla,
                        'priority_date': prio,
                        # 'pick_up_date': pup # Descomentar si usas este campo
                    })
                    
                    _logger.info(f"SLA calculado para {picking.name}: {sla}")
                    
                except Exception as e:
                    _logger.error(f"Error calculando SLA para picking {picking.name}: {e}")
            else:
                 _logger.info(f"Picking {picking.name} sin orden de venta o equipo de ventas asociado.")

        return res