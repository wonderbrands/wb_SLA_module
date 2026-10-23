# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class SlaOverrideWizard(models.TransientModel):
    _name = 'sla.override.wizard'
    _description = 'Wizard para Reasignación Manual / Empuje de SLA'

    new_sla_date = fields.Datetime(string="Nueva Fecha y Hora SLA", required=True)
    reason = fields.Selection([
        ('no_space', 'Falta de espacio en transporte / unidad'),
        ('carrier_cutoff', 'Corte de paquetería / retraso de recolección'),
        ('platform_rescheduled', 'Reprogramado por marketplace / plataforma'),
        ('customer_request', 'Petición del cliente'),
        ('other', 'Otro motivo'),
    ], string="Motivo de Reasignación", required=True, default='no_space')
    note = fields.Text(string="Notas / Observaciones Adicionales")

    marketplace = fields.Selection(
        selection='_get_marketplace_options',
        string="Filtrar por Marketplace",
        default='all',
        required=True,
        help="Permite aplicar el empuje de SLA únicamente a las órdenes del marketplace seleccionado, o a todas."
    )

    picking_ids = fields.Many2many('stock.picking', string="Albaranes")
    sale_ids = fields.Many2many('sale.order', string="Órdenes de Venta")

    @api.model
    def _get_marketplace_options(self):
        active_model = self._context.get('active_model')
        active_ids = self._context.get('active_ids', [])
        channels = set()

        if active_model == 'stock.picking' and active_ids:
            picks = self.env['stock.picking'].browse(active_ids)
            for p in picks:
                ch = p.sale_id.channel if p.sale_id else False
                if ch:
                    channels.add(ch.strip())
        elif active_model == 'sale.order' and active_ids:
            sales = self.env['sale.order'].browse(active_ids)
            for s in sales:
                if s.channel:
                    channels.add(s.channel.strip())

        options = [('all', 'Todos los Marketplaces seleccionados')]
        for c in sorted(list(channels)):
            options.append((c, c))
        return options

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

        # Filtrar por marketplace si se eligió uno específico
        target_sales = self.sale_ids
        target_picks = self.picking_ids
        if self.marketplace and self.marketplace != 'all':
            target_sales = target_sales.filtered(lambda s: (s.channel or '').strip() == self.marketplace)
            target_picks = target_picks.filtered(lambda p: (p.sale_id.channel if p.sale_id else '').strip() == self.marketplace)

        if not target_sales and not target_picks:
            raise UserError(_("No hay órdenes o albaranes que coincidan con el marketplace '%s'.") % self.marketplace)

        msg = (
            f"<b>SLA Reasignado / Empujado Manualmente por {user_name}</b><br/>"
            f"<b>Nueva Fecha SLA:</b> {self.new_sla_date}<br/>"
            f"<b>Motivo:</b> {reason_label}<br/>"
        )
        if self.note:
            msg += f"<b>Notas:</b> {self.note}<br/>"

        # 1. Actualizar Órdenes de Venta y registrar Historial
        for sale in target_sales:
            old_sla = sale.sla_date
            vals = {
                'sla_date': self.new_sla_date,
                'priority_date': self.new_sla_date,
                'sla_manual_override': True,
                'sla_override_reason': self.reason,
                'sla_override_note': self.note,
                'sla_override_date': now_dt,
                'sla_override_user_id': self.env.user.id,
            }
            if not sale.sla_original_date and old_sla:
                vals['sla_original_date'] = old_sla
            sale.write(vals)

            # Crear historial en la orden de venta
            self.env['sale.order.sla.history'].create({
                'sale_id': sale.id,
                'user_id': self.env.user.id,
                'date': now_dt,
                'old_sla_date': old_sla,
                'new_sla_date': self.new_sla_date,
                'reason': self.reason,
                'note': self.note,
            })

            try:
                sale.message_post(body=msg)
            except Exception:
                pass

        if target_sales:
            target_sales._compute_sla_priority()

        # 2. Actualizar Albaranes
        for pick in target_picks:
            old_pick_sla = pick.sla_date
            vals = {
                'sla_date': self.new_sla_date,
                'priority_date': self.new_sla_date,
                'sla_manual_override': True,
                'sla_override_reason': self.reason,
                'sla_override_note': self.note,
                'sla_override_date': now_dt,
                'sla_override_user_id': self.env.user.id,
            }
            if not pick.sla_original_date and old_pick_sla:
                vals['sla_original_date'] = old_pick_sla
            pick.write(vals)
            try:
                pick.message_post(body=msg)
            except Exception:
                pass

        if target_picks:
            target_picks._compute_sla_priority()

        # 3. Log en wmds.log si existe
        if 'wmds.log' in self.env:
            for pick in target_picks:
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
