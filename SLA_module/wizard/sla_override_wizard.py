# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class SlaOverrideWizard(models.TransientModel):
    _name = 'sla.override.wizard'
    _description = 'Wizard para Reasignación Manual de SLA'

    new_sla_date = fields.Datetime(string="Nueva Fecha y Hora SLA", required=True)
    reason = fields.Selection([
        ('no_space', 'Falta de espacio en transporte / unidad'),
        ('carrier_cutoff', 'Corte de paquetería / retraso de recolección'),
        ('platform_rescheduled', 'Reprogramado por marketplace / plataforma'),
        ('customer_request', 'Petición del cliente'),
        ('other', 'Otro motivo'),
    ], string="Motivo de Reasignación", required=True, default='no_space')
    note = fields.Text(string="Notas / Observaciones Adicionales")

    picking_ids = fields.Many2many('stock.picking', string="Albaranes")
    sale_ids = fields.Many2many('sale.order', string="Órdenes de Venta")

    @api.model
    def default_get(self, fields_list):
        res = super(SlaOverrideWizard, self).default_get(fields_list)
        active_model = self._context.get('active_model')
        active_ids = self._context.get('active_ids', [])

        if active_model == 'stock.picking' and active_ids:
            pickings = self.env['stock.picking'].browse(active_ids)
            res['picking_ids'] = [(6, 0, pickings.ids)]
            sales = pickings.mapped('sale_id').filtered(lambda s: bool(s))
            if sales:
                res['sale_ids'] = [(6, 0, sales.ids)]
        elif active_model == 'sale.order' and active_ids:
            sales = self.env['sale.order'].browse(active_ids)
            res['sale_ids'] = [(6, 0, sales.ids)]
            pickings = self.env['stock.picking'].search([
                ('sale_id', 'in', sales.ids),
                ('state', 'not in', ('done', 'cancel'))
            ])
            if pickings:
                res['picking_ids'] = [(6, 0, pickings.ids)]
        return res

    def action_apply_override(self):
        self.ensure_one()
        if not self.new_sla_date:
            raise UserError(_("Debe especificar la nueva fecha y hora para el SLA."))

        reason_label = dict(self._fields['reason'].selection).get(self.reason, self.reason)
        now_dt = fields.Datetime.now()
        user_name = self.env.user.name

        msg = (
            f"<b>SLA Reasignado Manualmente por {user_name}</b><br/>"
            f"<b>Nueva Fecha SLA:</b> {self.new_sla_date}<br/>"
            f"<b>Motivo:</b> {reason_label}<br/>"
        )
        if self.note:
            msg += f"<b>Notas:</b> {self.note}<br/>"

        for pick in self.picking_ids:
            vals = {
                'sla_date': self.new_sla_date,
                'priority_date': self.new_sla_date,
                'sla_manual_override': True,
                'sla_override_reason': self.reason,
                'sla_override_note': self.note,
                'sla_override_date': now_dt,
                'sla_override_user_id': self.env.user.id,
            }
            if not pick.sla_original_date and pick.sla_date:
                vals['sla_original_date'] = pick.sla_date
            pick.write(vals)
            try:
                pick.message_post(body=msg)
            except Exception:
                pass

        if self.picking_ids:
            self.picking_ids._compute_sla_priority()

        for sale in self.sale_ids:
            vals = {
                'sla_manual_override': True,
                'sla_override_reason': self.reason,
                'sla_override_note': self.note,
                'sla_override_date': now_dt,
                'sla_override_user_id': self.env.user.id,
            }
            sale.write(vals)
            try:
                sale.message_post(body=msg)
            except Exception:
                pass

        if 'wmds.log' in self.env:
            for pick in self.picking_ids:
                try:
                    self.env['wmds.log'].sudo().create({
                        'stock_picking': pick.id,
                        'sale_order': pick.sale_id.id if pick.sale_id else False,
                        'user': self.env.user.id,
                        'date': now_dt,
                        'log': f"SLA Empujado/Reasignado a {self.new_sla_date}. Motivo: {reason_label}."
                    })
                except Exception:
                    pass

        return {'type': 'ir.actions.act_window_close'}
