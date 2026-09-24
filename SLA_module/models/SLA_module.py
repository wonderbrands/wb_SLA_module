# -*- coding: utf-8 -*-
from odoo import models, fields, api

class MarketplaceSchedule(models.Model):
    _name = 'marketplace.schedule'
    _description = 'Marketplace Schedule'
    _rec_name = 'marketplace'

    def _get_marketplace_selection(self):
        self.env.cr.execute("SELECT DISTINCT channel FROM sale_order WHERE channel IS NOT NULL AND channel != '' ORDER BY channel")
        raw_channels = [r[0] for r in self.env.cr.fetchall() if r[0]]
        channels = sorted(list(set(c.strip() for c in raw_channels if c and c.strip())))
        selection = [(c, c) for c in channels]
        if not selection:
            selection = [('default', 'General / Por Defecto')]
        return selection

    marketplace = fields.Selection(selection='_get_marketplace_selection', string='Marketplace', required=True)
    
    sla_source = fields.Selection([
        ('auto', 'Automático / Híbrido (Usar Yuju si existe, si no Calcular)'),
        ('yuju', 'Yuju (Fecha límite de despacho del Marketplace)'),
        ('calculated', 'Cálculo por Horario (SLA Schedule)')
    ], string='Origen de SLA', default='auto', required=True)

    collection_cutoff_time = fields.Float(string='Hora Límite de Recolección (ej. 17.0 = 5:00 PM)', default=17.0)
    days_after_creation = fields.Integer(string='Días a partir de la creación de la venta', help='Número de días naturales a sumar desde la fecha de creación de la venta para calcular el SLA.', default=0)
    only_business_days = fields.Boolean(string='Solo contar días hábiles', help='Si está activo, los sábados y domingos no cuentan para el cálculo de días del SLA.', default=False)

    monday_to_friday_ = fields.Float(string='Lunes a Viernes (horas)', default=24.0)
    saturday = fields.Float(string='Sábado (horas)', default=0.0)
    sunday = fields.Float(string='Domingo (horas)', default=0.0)
    
    auto_fill_dates = fields.Boolean(string="Auto-completado de Priority-date", default=False)
    
    # Campos condicionales
    flex = fields.Integer(string='Flex (minutos)')
    sameDay_nextDay = fields.Integer(string='Same-day/Next-day (minutos)')
    
    # Auxiliares para control de vista (Invisible logic)
    auxiliar_1 = fields.Char(string='Es MercadoLibre', store=True)
    auxiliar_2 = fields.Char(string='Es Sitio Web', store=True)

    @api.onchange('marketplace')
    def _onchange_marketplace(self):
        if self.marketplace:
            m_clean = str(self.marketplace).lower().replace(" ", "")
            
            # Lógica para Mercado Libre
            if 'mercadolibre' in m_clean or 'mercado' in m_clean:
                self.auxiliar_1 = 'True'
            else:
                self.auxiliar_1 = 'False'
            
            # Lógica para Sitio Web / Shopify
            if 'shopify' in m_clean or 'sitioweb' in m_clean or 'web' in m_clean:
                self.auxiliar_2 = 'True'
            else:
                self.auxiliar_2 = 'False'