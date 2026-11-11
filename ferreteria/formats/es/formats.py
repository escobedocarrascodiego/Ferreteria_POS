# -*- coding: utf-8 -*-
"""
Formatos regionales para Peru.

Django no trae locale es_PE y el locale 'es' (Espana) usa coma decimal
(1.234,56). En Peru se usa punto decimal y coma de millares (1,234.56).
"""

DECIMAL_SEPARATOR = '.'
THOUSAND_SEPARATOR = ','
NUMBER_GROUPING = 3

DATE_FORMAT = 'd/m/Y'
DATETIME_FORMAT = 'd/m/Y H:i'
SHORT_DATE_FORMAT = 'd/m/Y'
SHORT_DATETIME_FORMAT = 'd/m/Y H:i'
TIME_FORMAT = 'H:i'

DATE_INPUT_FORMATS = ['%d/%m/%Y', '%Y-%m-%d']
DATETIME_INPUT_FORMATS = ['%d/%m/%Y %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M']
