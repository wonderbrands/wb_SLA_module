# -*- coding: utf-8 -*-
from odoo import models, fields, api

class MarketplaceSchedule(models.Model):
    _name = 'marketplace.schedule'
    _description = 'Marketplace Schedule'
    _rec_name = 'crm_team'

    # marketplace = fields.Many2one('res.partner', string='Marketplace') # Comentado en tu original
    crm_team = fields.Many2one('crm.team', string='Equipo de ventas', required=True)
    
    monday_to_friday_ = fields.Integer(string='Lunes a Viernes (horas)')
    saturday = fields.Integer(string='Sábado (horas)')
    sunday = fields.Integer(string='Domingo (horas)')
    
    auto_fill_dates = fields.Boolean(string="Auto-completado de Priority-date", default=False)
    
    # Campos condicionales
    flex = fields.Integer(string='Flex (minutos)')
    sameDay_nextDay = fields.Integer(string='Same-day/Next-day (minutos)')
    
    # Auxiliares para control de vista (Invisible logic)
    auxiliar_1 = fields.Char(string='Es MercadoLibre', store=True)
    auxiliar_2 = fields.Char(string='Es Sitio Web', store=True)

    @api.onchange('crm_team')
    def _onchange_crm_team(self):
        if self.crm_team and self.crm_team.name:
            team_clean = self.crm_team.name.lower().replace(" ", "")
            
            # Lógica para Mercado Libre
            if 'mercadolibre' in team_clean:
                self.auxiliar_1 = 'True'
            else:
                self.auxiliar_1 = 'False'
            
            # Lógica para Sitio Web
            if 'sitioweb' in team_clean:
                self.auxiliar_2 = 'True'
            else:
                self.auxiliar_2 = 'False'