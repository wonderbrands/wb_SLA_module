# -*- coding: utf-8 -*-
{
    'name': 'Marketplace Schedule',
    'version': '18.0.1.0.0',
    'summary': 'Manage schedule for each marketplace',
    'description': 'This module allows you to manage the schedule for each marketplace.',
    'author': '"Sergio Guerrero"',
    'depends': ['base',
                'stock'],
    'data': [
        'security/ir.model.access.csv',
        'security/security_SLA.xml',
        'views/stock_picking_views.xml',
        'views/marketplace_schedule.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
