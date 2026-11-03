# -*- coding: utf-8 -*-
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    _logger.info("SLA_module pre-migrate: Limpiando vistas y bindings obsoletos de stock.picking...")

    # 1. Eliminar vistas de stock.picking en ir_ui_view e ir_model_data
    cr.execute("""
        DELETE FROM ir_ui_view WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'SLA_module' AND name IN ('stock_picking_form_inherit_sla', 'stock_picking_list_inherit_sla')
        );
        DELETE FROM ir_model_data
        WHERE module = 'SLA_module' AND name IN ('stock_picking_form_inherit_sla', 'stock_picking_list_inherit_sla');
    """)

    # 2. Eliminar binding de acción a stock.picking para el wizard de SLA
    cr.execute("""
        DELETE FROM ir_act_window WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'SLA_module' AND name = 'action_sla_override_wizard'
        );
        DELETE FROM ir_model_data
        WHERE module = 'SLA_module' AND name = 'action_sla_override_wizard';
    """)

    _logger.info("SLA_module pre-migrate: Limpieza completada exitosamente.")
