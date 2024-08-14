# -*- coding: utf-8 -*-
from odoo import models, fields, api

class MarketplaceSchedule(models.Model):
    _name = 'marketplace.schedule'
    _description = 'Marketplace Schedule'

    # Definir los campos
    #marketplace = fields.Char(string='Marketplace', required=True)
    marketplace = fields.Many2one('res.partner', string='Marketplace')
    crm_team = fields.Many2one('crm.team', string='Equipo de CRM')
    monday_to_friday_ = fields.Integer(string='Lunes a Viernes (horas)')
    # friday = fields.Char(string='Viernes (día y hora)')
    saturday = fields.Integer(string='Sábado (día y hora)')
    sunday = fields.Integer(string='Domingo (día y hora)')
    flex = fields.Integer(string='Flex (minutos)')
    sameDay_nextDay = fields.Integer(string='Same-day/Next-day (minutos)')
    auxiliar_1 = fields.Char(string='auxiliar1')
    auxiliar_2 = fields.Char(string='auxiliar2')
    #mercado_libre_id = fields.Char(string='ID de MercadoLibre', compute='_compute_mercado_libre_id', store=True)

    @api.onchange('crm_team')
    def _onchange_marketplace(self):
        print("Dentro de onchage marketplace")
        if isinstance(self.crm_team.name, str):
            print("Es un STR o no?", self.crm_team.name, type(self.crm_team.name),
                  self.crm_team.name.lower().replace(" ", ""))
            if self.crm_team.name.lower().replace(" ", "") == 'team_mercadolibre':
                print(self.crm_team.name.lower().replace(" ", ""))
                self.auxiliar_1 = 'True'
            elif self.crm_team.name.lower().replace(" ", "") == 'team_sitioweb':
                self.auxiliar_2 = 'True'
            else:
                'False'
