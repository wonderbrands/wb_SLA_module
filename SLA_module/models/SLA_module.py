# -*- coding: utf-8 -*-
from odoo import models, fields, api

class MarketplaceSchedule(models.Model):
    _name = 'marketplace.schedule'
    _description = 'Marketplace Schedule'

    # Definir los campos
    #marketplace = fields.Char(string='Marketplace', required=True)
    marketplace = fields.Many2one('res.partner', string='Marketplace')
    monday_to_thursday = fields.Char(string='Lunes a Jueves (horas)')
    friday = fields.Char(string='Viernes (día y hora)')
    saturday = fields.Char(string='Sábado (día y hora)')
    sunday = fields.Char(string='Domingo (día y hora)')
    flex = fields.Integer(string='Flex (minutos)')
    sameDay_nextDay = fields.Integer(string='Same-day/Next-day (minutos)')
    auxiliar_1 = fields.Char(string='auxiliar1')
    auxiliar_2 = fields.Char(string='auxiliar2')
    #mercado_libre_id = fields.Char(string='ID de MercadoLibre', compute='_compute_mercado_libre_id', store=True)

    @api.onchange('marketplace')
    def _onchange_marketplace(self):
        print("Dentro de onchage marketplace")
        if isinstance(self.marketplace.name, str):
            if self.marketplace.name.lower().strip() == 'mercadolibre':
                print(self.marketplace.name.lower().strip())
                self.auxiliar_1 = 'True'
            elif self.marketplace.name.lower().strip() == 'shopify':
                self.auxiliar_2 = 'True'
            else:
                'False'
