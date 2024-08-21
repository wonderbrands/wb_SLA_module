import time

from odoo import models, api, fields
from datetime import datetime, timedelta
import re
import logging

logging.basicConfig(filename='SLA.log', level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
_logger = logging.getLogger(__name__)



utc_local = -6 #UTC CDMX

class Picking(models.Model):
    _inherit = 'stock.picking'

    #sla_date = fields.Datetime(string="Pick-Up Date",help='Field that show the Pick-Up date')
    crm_team_info = fields.Text(string="crm_team Information", compute="_compute_crm_team_info")

    @api.depends('sla_date')
    def _get_timeDelta_days(self, init_day, goal_day):
        days_of_week = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
            "lunes": 0,
            "martes": 1,
            "miercoles": 2,
            "jueves": 3,
            "viernes": 4,
            "sabado": 5,
            "domingo": 6
        }

        return (days_of_week[goal_day.lower()] - days_of_week[init_day.lower()]) % 7

    @api.depends('sla_date')
    def _get_business_day(self, sla_value_date, restrict_only_sunday):
        if restrict_only_sunday:
            if sla_value_date.weekday() == 6:  # 6 Si sla_date es domingo vamonos al Lunes
                sla_value_date = sla_value_date + timedelta(hours=24)
                return sla_value_date
            else:
                return sla_value_date
        else:
            if sla_value_date.weekday() == 5:  # 5 Sisladate es sabado vamonos al Lunes
                sla_value_date = sla_value_date + timedelta(hours=48)
                return sla_value_date
            elif sla_value_date.weekday() == 6:  # 6 Si sla_date es domingo vamonos al Lunes
                sla_value_date = sla_value_date + timedelta(hours=24)
                return sla_value_date
            else:
                return sla_value_date

    @api.depends('sla_date')
    def _auto_fill_dates_method(self, auto_fill_dates, sla_date):
        if auto_fill_dates:
            return sla_date,sla_date,sla_date
        else:
            return sla_date, None, None

    @api.depends('sla_date')
    def _compute_sla_value_date(self, crm_team, date, fullfilment):
        # El parametro 'crm_team' es crm_team de sales.order (equipo de ventas)
        """
            Calcula la fecha de SLA date de acuerdo al schedule del modelo marketplace.schedule
        """

        # Esta variable se usa para hacer calculos precisos con date harcodeada, ya que la hora que
        # se manjea dentro del script es UTC 0, si la variable date se exporta a odoo, hace el calculo
        # directo en su sistema.
        local_date = date + timedelta(hours=utc_local)
        # Obtener el día de la semana a partir de la fecha proporcionada
        day_of_week = local_date.strftime('%A').lower()

        # Hora limite en para saber si un envio toma la opcion A o la B (antes o depsues de...)
        limit_hour = '12:00:00'
        #print(date + timedelta(hours=utc_local))
        print(day_of_week, "\n****************************************\n")

        for picking in self:
            crm_team_schedule = self.env['marketplace.schedule'].search([('crm_team', '=', crm_team)])


            if crm_team_schedule:
                dic_crm_team_info = {
                            "crm_team": crm_team_schedule.crm_team,
                            "monday to friday": crm_team_schedule.monday_to_friday_,
                            "saturday": crm_team_schedule.saturday,
                            "sunday": crm_team_schedule.sunday,
                            "auto_fill_dates":crm_team_schedule.auto_fill_dates,
                        }
            else:
                dic_crm_team_info = "No crm_team schedule found."


        try:
            # Si el check esta activo, el valor de sla_date, pick_up_date y priority_date sera el mismo
            auto_fill_dates = dic_crm_team_info["auto_fill_dates"]
            team_name = dic_crm_team_info["crm_team"].name.lower().replace(" ", "")
            # Checamos si el crm_team es Team Mercado Libre
            if team_name == 'team_mercadolibre':
                #print("ES MERCADO LIBRE")
                _logger.info("ES MERCADO LIBRE")

                dic_crm_team_info["flex"] =  crm_team_schedule.flex
                # Checamos si es envio Flex
                if fullfilment.lower() == "fbf":
                    #print("ES FLEX")
                    _logger.info("ES FLEX")
                    # Nuevo SLA
                    _logger.info(local_date.time())
                    if int(dic_crm_team_info["flex"]) > 0:
                        if local_date.time() <= datetime.strptime(limit_hour, '%H:%M:%S').time():
                            _logger.info("Son menos de las ", limit_hour, f" y se entregara hoy + {str(dic_crm_team_info['flex'])} minutos")
                            sla_value_date = date + timedelta(minutes=int(dic_crm_team_info["flex"]))  # fecha orden + tiempo definido en schedule
                            return self._auto_fill_dates_method(auto_fill_dates, sla_value_date)
                        # Si la orden entró después de las 12:00 pm
                        else:
                            _logger.info("Son mas de las ", limit_hour, " y se entregara el siguinete dia")
                            sla_value_date = local_date.replace(hour=0, minute=0, second=0)
                            sla_value_date = sla_value_date + timedelta(hours=(int(24 + (11 - utc_local))), minutes=int(59), seconds=int(59))  # Setea SLA date al dia siguiente a las 11:59Ñ59 am
                            return self._auto_fill_dates_method(auto_fill_dates, sla_value_date)
                    else:
                        sla_value_date = None
                        return self._auto_fill_dates_method(auto_fill_dates, sla_value_date)

                else: # SI NO ES FLEX (COLECTA)
                    if day_of_week in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday']:
                        day_info = dic_crm_team_info["monday to friday"]
                        if day_info >= 0:  # Verificamos que el dato sea positivo
                            sla_value_date = local_date + timedelta(hours=int(day_info))
                            sla_value_date = sla_value_date.replace(hour=12 - utc_local, minute=0, second=0)
                            sla_value_date =  self._get_business_day(sla_value_date, False)
                            return self._auto_fill_dates_method(auto_fill_dates, sla_value_date)
                    elif day_of_week == 'saturday':
                        day_info = dic_crm_team_info['satuday']
                        if day_info >= 0:  # Verificamos que el dato sea positivo
                            sla_value_date = local_date + timedelta(hours=int(day_info))
                            sla_value_date = sla_value_date.replace(hour=12 - utc_local, minute=0, second=0)
                            sla_value_date = self._get_business_day(sla_value_date, False)
                            return self._auto_fill_dates_method(auto_fill_dates, sla_value_date)

                    elif day_of_week == 'sunday':
                        day_info = dic_crm_team_info['sunday']
                        if day_info >= 0:  # Verificamos que el dato sea positivo
                            sla_value_date = local_date + timedelta(hours=int(day_info))
                            sla_value_date = sla_value_date.replace(hour=12 - utc_local, minute=0, second=0)
                            sla_value_date = self._get_business_day(sla_value_date, False)
                            return self._auto_fill_dates_method(auto_fill_dates, sla_value_date)

            elif team_name == 'team_amazon':
                if day_of_week in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday']:
                    day_info = dic_crm_team_info["monday to friday"]
                    if day_info >= 0:  # Verificamos que el dato sea positivo
                        sla_value_date = local_date + timedelta(hours=int(day_info))
                        sla_value_date = sla_value_date.replace(hour=12 - utc_local, minute=0, second=0)
                        sla_value_date = self._get_business_day(sla_value_date, False)
                        return self._auto_fill_dates_method(auto_fill_dates, sla_value_date)
                elif day_of_week == 'saturday':
                    day_info = dic_crm_team_info['satuday']
                    if day_info >= 0:  # Verificamos que el dato sea positivo
                        sla_value_date = local_date + timedelta(hours=int(day_info))
                        sla_value_date = sla_value_date.replace(hour=12 - utc_local, minute=0, second=0)
                        sla_value_date = self._get_business_day(sla_value_date, False)
                        return self._auto_fill_dates_method(auto_fill_dates, sla_value_date)

                elif day_of_week == 'sunday':
                    day_info = dic_crm_team_info['sunday']
                    if day_info >= 0:  # Verificamos que el dato sea positivo
                        sla_value_date = local_date + timedelta(hours=int(day_info))
                        sla_value_date = sla_value_date.replace(hour=12 - utc_local, minute=0, second=0)
                        sla_value_date = self._get_business_day(sla_value_date, False)
                        return self._auto_fill_dates_method(auto_fill_dates, sla_value_date)


            # RESTO DE EQUIPOS
            else:
                _logger.info(f'{day_of_week}')
                if day_of_week in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday']:
                    print("DENTRO DE ['monday', 'tuesday', 'wednesday', 'thursday', 'friday'] ")
                    day_info = dic_crm_team_info["monday to friday"]
                    _logger.info(f'DENTRO DE ["monday", "tuesday", "wednesday", "thursday", "friday"] , HORAS: {day_info}')
                    if day_info >= 0: # Verificamos que el dato sea positivo
                        sla_value_date = local_date + timedelta(hours=int(day_info))
                        if team_name == 'ventasdepiso' or team_name == 'mayoreo_naes':  # Ventas de piso o Mayoreo_NAES
                            sla_value_date = sla_value_date + timedelta(hours=utc_local)
                            sla_value_date = sla_value_date.replace(hour=15-utc_local, minute=0, second=0) # Hora despues de las 3:00 pm
                            sla_value_date = self._get_business_day(sla_value_date, False)
                            return self._auto_fill_dates_method(auto_fill_dates, sla_value_date)
                        sla_value_date = (self._get_business_day(sla_value_date, True)) - timedelta(hours=utc_local)
                        return self._auto_fill_dates_method(auto_fill_dates, sla_value_date)
                    else:
                        raise ValueError("El formato del matketplace schedule es incorrecto, verificar que sea un numero de horas válido")
                else: # Fin de semana
                    _logger.info(f'Es fin de semana Sabado o Domingo: {day_of_week}')
                    if day_of_week == 'saturday':
                        day_info = dic_crm_team_info["saturday"]
                        if day_info >= 0:
                            sla_value_date = local_date + timedelta(hours=int(day_info))
                            if team_name == 'ventasdepiso' or team_name == 'mayoreo_naes':  # Ventas de piso o Mayoreo_NAES
                                sla_value_date = sla_value_date + timedelta(hours=utc_local)
                                sla_value_date = sla_value_date.replace(hour=15-utc_local, minute=0, second=0)
                                sla_value_date = self._get_business_day(sla_value_date, False)
                                return self._auto_fill_dates_method(auto_fill_dates, sla_value_date)
                            sla_value_date = (self._get_business_day(sla_value_date, True)) - timedelta(hours=utc_local)
                            return self._auto_fill_dates_method(auto_fill_dates, sla_value_date)

                    elif day_of_week == 'sunday':
                        day_info = dic_crm_team_info["sunday"]
                        if day_info >= 0:
                            sla_value_date = local_date + timedelta(hours=int(day_info))
                            if team_name == 'ventasdepiso' or team_name == 'mayoreo_naes':  # Ventas de piso o Mayoreo_NAES
                                sla_value_date = sla_value_date + timedelta(hours=utc_local)
                                sla_value_date = sla_value_date.replace(hour=15-utc_local, minute=0, second=0)
                                sla_value_date = self._get_business_day(sla_value_date, False)
                                return self._auto_fill_dates_method(auto_fill_dates, sla_value_date)
                            sla_value_date = (self._get_business_day(sla_value_date, True)) - timedelta(hours=utc_local)
                            return self._auto_fill_dates_method(auto_fill_dates, sla_value_date)
                    else:
                        raise("Este equipo de ventas no tiene un SLA asignado para la fecha de hoy")

        except KeyError as e:
            print(f'Error en crm_team_id, no existe un schedule SLA date para "{crm_team}", Error: ', e)
        except TypeError as e:
            print(f'Error de formato, Error: ', e)
        # except ValueError as e:
        #     print('El formato del crm_team schedule es incorrecto, verificar, Error: ', e)
        #     raise Exception("Este es mi mensaje de error")

    @api.depends('sla_date')
    def _get_order_values(self):
        """
            Se hace una busqueda en los registros de sale.order para la orden que se esta trabajando
            Se obtienen los valores del cliente (crm_team) y el numero de orden
        """

        # origin = self.origin
        # order = self.env['sale.order'].search([('name', '=', origin)], limit=1)

        list_orders=[]
        for order in self:
            new=order.origin
            list_orders.append(new)

        origin = list_orders[0]
        order = self.env['sale.order'].search([('name', '=', origin)], limit=1)

        #raise Exception('Mnual RAISE', list_orders)

        try:
            partner_name = order.partner_id.name
            crm_team = order.team_id.name
            fulfillment = order.fulfillment
            if fulfillment == False:
                fulfillment = str(fulfillment) # Convertimos a string si el crm_team no tiene el campo fulfillment
            date_order = order.date_order # + timedelta(hours=utc_local)  #UTC -6 CDMX
            print(f'partner_name= {partner_name}, origin= {origin}, date_order= {date_order}, fulfillment: {str(fulfillment)}')
            _logger.info(f'partner_name= {partner_name}, origin= {origin}, date_order= {date_order}, fulfillment: {str(fulfillment)}')
        except:
            raise ("Error al obtener la informacion de la orden")

        return self._compute_sla_value_date(crm_team,date_order, fulfillment)

    def action_confirm(self):

        # print(f'\n VALOR DE pickup date: {self._get_order_values()}\n')
        # Llama al método original para confirmar la operación de recogida
        res = super(Picking, self).action_confirm()

        # Establece el valor de sla_date al momento de la confirmación
        try:
            for picking in self:
                sla_value_date, pick_up_date, priority_date = self._get_order_values()
                _logger.info(f'SLA_DATE: {sla_value_date + timedelta(hours=utc_local)}\n')
                picking.sla_date = sla_value_date
                picking.pick_up_date = pick_up_date
                picking.priority_date = priority_date
        except TypeError as e:
            picking.sla_date = None
            _logger.info(f'El team no tiene un SLA por lo que no se asigna un SLA-date: {e}')

        return res
