from odoo import models, fields, api, exceptions, _

class Picking(models.Model):
    _inherit = 'stock.picking'

    # campo nuevo 15/08/2024
    sla_date = fields.Datetime(string="SLA Date", help='Campo que muestra el SLA date')  # Este campo se le asignara un valor desde el script
    priority_date = fields.Datetime(string="Priority Date", help='Campo que muestra el Priority date')  # Este campo se le calcula posterioemnete
