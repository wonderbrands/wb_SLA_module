from odoo import models, api, fields
from datetime import datetime, timedelta
import re
import logging

logging.basicConfig(filename='SLA.log', level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
_logger = logging.getLogger(__name__)



utc_local = -6 #UTC CDMX

class Picking(models.Model):
    _inherit = 'stock.picking'

    #pick_up_date = fields.Datetime(string="Pick-Up Date",help='Field that show the Pick-Up date')
    crm_team_info = fields.Text(string="crm_team Information", compute="_compute_crm_team_info")

    @api.depends('pick_up_date')
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

    @api.depends('pick_up_date')
    def _compute_pickUp_date(self, crm_team, date, fullfilment):
        # El parametro 'crm_team' es crm_team de sales.order (equipo de ventas)
        """
            Calcula la fecha de pickup date de acuerdo al schedule del modelo marketplace.schedule
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
                            "monday to thursday": crm_team_schedule.monday_to_thursday_,
                            "friday": crm_team_schedule.friday,
                            "saturday": crm_team_schedule.saturday,
                            "sunday": crm_team_schedule.sunday,
                        }
            else:
                dic_crm_team_info = "No crm_team schedule found."


        try:

            # Checamos si el crm_team es Team Mercado Libre
            if dic_crm_team_info["crm_team"].name.lower().replace(" ", "") == 'team_mercadolibre':
                #print("ES MERCADO LIBRE")
                _logger.info("ES MERCADO LIBRE")
                dic_crm_team_info["flex"] =  crm_team_schedule.flex
                # Checamos si es envio Flex
                if fullfilment.lower() == "fbf":
                    #print("ES FLEX")
                    _logger.info("ES FLEX")
                    # Checamos si la orden entró antes de las 12:00 pm
                    #print(local_date.time())
                    _logger.info(local_date.time())
                    if local_date.time() <= datetime.strptime(limit_hour, '%H:%M:%S').time():
                        #print("Son menos de las ", limit_hour, " y se entregara hoy + x minutos")
                        _logger.info("Son menos de las ", limit_hour, " y se entregara hoy + x minutos")
                        pickUp_date = date + timedelta(minutes=int(dic_crm_team_info["flex"])) # pickup date + tiempo definido en schedule
                        return pickUp_date
                    # Si la orden entró después de las 12:00 pm
                    else:
                        #print("Son mas de las ", limit_hour, " y se entregara maniana al final del dia")
                        _logger.info("Son mas de las ", limit_hour, " y se entregara maniana al final del dia")
                        pickUp_date = local_date.replace(hour=0, minute=0, second=0)
                        pickUp_date = pickUp_date + timedelta(hours=(int(24+(23-utc_local))), minutes=int(59), seconds=int(00)) # Setea pickup date al dia siguiente a las 23:59 pm
                        return pickUp_date

            # Checamos si el crm_team es Team Shopify
            elif dic_crm_team_info["crm_team"].name.lower().replace(" ", "") == 'team_sitioweb':
                dic_crm_team_info["sameDay_nextDay"] = crm_team_schedule.sameDay_nextDay

                # ESTA PARTE SE ACTIVARA CUANDO EL ENVIO SAME-DAYE NEXT-DAY ESTE IMPLEMENTADO
                # SE EVALUARIA UN IF EXTRA PARA SABER SI ES ESE TIPO DE ENVIO
                # if local_date.time() <= datetime.strptime(limit_hour, '%H:%M:%S').time():
                #     pickUp_date = date + timedelta(minutes=int(dic_crm_team_info["sameDay_nextDay"]))
                #     return pickUp_date


            print(day_of_week)
            _logger.info(f'{day_of_week}')
            if day_of_week in ['monday', 'tuesday', 'wednesday', 'thursday']:
                print("DENTRO DE ['monday', 'tuesday', 'wednesday', 'thursday'] ")
                day_info = dic_crm_team_info["monday to thursday"]
                _logger.info(f'DENTRO DE ["monday", "tuesday", "wednesday", "thursday"] , HORAS: {day_info}')
                if day_info >= 0: # Verificamos que el dato sea positivo
                    pickUp_date = date + timedelta(hours=int(day_info))
                    return pickUp_date
                else:
                    raise ValueError("El formato del matketplace schedule es incorrecto, verificar que sea un numero de horas válido")
            else:
                _logger.info(f'Es fin de semana Viernes, Sabado o Domingo: {day_of_week}')
                day_info = dic_crm_team_info[day_of_week].split(' ')  # Separamos los parametros en una lista. ex -> [3,'16:00:00']
                print(type(dic_crm_team_info), dic_crm_team_info)
                # Si es Viernes, Sábado o Domingo, tomar la fecha y hora indicada
                print(type(day_info), day_info)
                pickUp_date = local_date.replace(hour=0, minute=0, second=0) # Se usa local_date con el UTC local porque si no puede setear el dia incorrecto a las 00:00:00
                if re.match(r'^\d{2}:\d{2}:\d{2}$',  day_info[1]):
                    if isinstance(day_info[0], str):
                        if day_info[0].lower() in ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sabado', 'domingo']:
                            print('***************IF day_info[0].lower() in dias de la semana :****************')
                            _timedelta = self._get_timeDelta_days(day_of_week, day_info[0].lower())
                            pickUp_date = pickUp_date + timedelta(days=int(_timedelta))
                            hours_str, minutes_str, seconds_str = day_info[1].split(":")
                            pickUp_date = pickUp_date + timedelta(hours=(int(hours_str) - utc_local), minutes=int(minutes_str), seconds=int(seconds_str))
                            print(f'El pickup date es: {pickUp_date + timedelta(hours=(utc_local))}')
                            return pickUp_date
                        else:
                            raise ValueError("El formato del marketplace schedule es incorrecto, verificar que sea un numero de días válido")
                    else:
                        raise ValueError("El formato del marketplace schedule es incorrecto, verificar que sea un numero de días válido")
                else:
                    raise ValueError("El formato del marketplace schedule incorrecto, verificar el formato '%H:%M:%S'")
        except KeyError as e:
            print(f'Error en crm_team_id, no existe un schedule pickup date para "{crm_team}", Error: ', e)
        except TypeError as e:
            print(f'Error de formato, Error: ', e)
        # except ValueError as e:
        #     print('El formato del crm_team schedule es incorrecto, verificar, Error: ', e)
        #     raise Exception("Este es mi mensaje de error")

    @api.depends('pick_up_date')
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
            print(f'partner_name= {partner_name}, origin= {origin}, date_order= {date_order}')
            _logger.info(f'partner_name= {partner_name}, origin= {origin}, date_order= {date_order}, fulfillment: {str(fulfillment)}')
        except:
            raise ("Error al obtener la informacion de la orden")

        return self._compute_pickUp_date(crm_team,date_order, fulfillment)

    def action_confirm(self):

        # print(f'\n VALOR DE pickup date: {self._get_order_values()}\n')
        # Llama al método original para confirmar la operación de recogida
        res = super(Picking, self).action_confirm()

        # Establece el valor de pick_up_date al momento de la confirmación
        try:
            for picking in self:
                pickup_date = self._get_order_values()
                _logger.info(f'PICKUPDATE: {pickup_date + timedelta(hours=utc_local)}\n')
                picking.pick_up_date = pickup_date
        except TypeError as e:
            picking.pick_up_date = None
            _logger.info(f'El team no tiene un SLA por lo que no se asigna un pickup-date')

        return res
