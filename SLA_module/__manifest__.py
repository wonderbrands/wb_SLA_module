# -*- coding: utf-8 -*-
{
    'name': 'Marketplace Schedule',
    'version': '2.4.0',
    'summary': 'Manage schedule for each marketplace',
    'description': 'This module allows you to manage the schedule for each marketplace.',
    'author': '"Sergio Guerrero"',
    'depends': ['base',
                'stock',
                'wb_prio_picking'],
    'data': [
        'security/ir.model.access.csv',
        'views/security_SLA.xml',
        'views/stock_picking_views.xml',
        'views/marketplace_schedule.xml',
        'views/view_stock_picking_form.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
