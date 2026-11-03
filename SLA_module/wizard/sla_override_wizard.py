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
        ('prior_to_entry', 'SLA recibido previo a fecha de ingreso'),
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

    sale_ids = fields.Many2many('sale.order', string="Órdenes de Venta")

    @api.model
    def _get_marketplace_options(self):
        active_ids = self._context.get('active_ids', [])
        channels = set()

        if active_ids:
            sales = self.env['sale.order'].browse(active_ids)
            for s in sales:
                ch = getattr(s, 'channel', False)
                if ch:
                    channels.add(ch.strip())

        options = [('all', 'Todos los Marketplaces seleccionados')]
        for c in sorted(list(channels)):
            options.append((c, c))
        return options

    @api.model
    def default_get(self, fields_list):
        res = super(SlaOverrideWizard, self).default_get(fields_list)
        active_ids = self._context.get('active_ids', [])
        if active_ids:
            sales = self.env['sale.order'].browse(active_ids)
            res['sale_ids'] = [(6, 0, sales.ids)]
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
        if self.marketplace and self.marketplace != 'all':
            target_sales = target_sales.filtered(lambda s: (getattr(s, 'channel', False) or '').strip() == self.marketplace)

        if not target_sales:
            raise UserError(_("No hay órdenes de venta que coincidan con el marketplace '%s'.") % self.marketplace)

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

        # 2. Log en wmds.log si existe
        if 'wmds.log' in self.env:
            for sale in target_sales:
                try:
                    self.env['wmds.log'].sudo().create({
                        'stock_picking': False,
                        'sale_order': sale.id,
                        'user': self.env.user.id,
                        'date': now_dt,
                        'log': f"SLA Empujado/Reasignado a {self.new_sla_date}. Motivo: {reason_label}."
                    })
                except Exception:
                    pass

        return {'type': 'ir.actions.act_window_close'}
