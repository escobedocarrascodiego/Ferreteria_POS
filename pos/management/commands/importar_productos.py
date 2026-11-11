"""
Carga el catalogo desde el Excel de inventario.

    python manage.py importar_productos
    python manage.py importar_productos otra_ruta.xlsx

Lee la columna "Nombre del Producto". Se puede ejecutar varias veces: los
nombres que ya existen (sin distinguir mayusculas) se saltan.
"""
import unicodedata
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from openpyxl import load_workbook

from pos.models import Producto

POR_DEFECTO = settings.BASE_DIR / 'datos' / 'Inventario_Completo_Ferreteria.xlsx'


def clave(nombre):
    """
    Misma nocion de "igual" que usa MySQL en el indice unico: sin distinguir
    mayusculas ni tildes ("UNIÓN" y "union" son el mismo producto).
    """
    sin_tildes = unicodedata.normalize('NFD', nombre)
    return ''.join(c for c in sin_tildes if not unicodedata.combining(c)).casefold()


class Command(BaseCommand):
    help = 'Importa los nombres de productos desde el Excel de inventario.'

    def add_arguments(self, parser):
        parser.add_argument('archivo', nargs='?', default=str(POR_DEFECTO))

    def handle(self, *args, **opciones):
        ruta = Path(opciones['archivo'])
        if not ruta.exists():
            raise CommandError(f'No existe el archivo: {ruta}')

        hoja = load_workbook(ruta, read_only=True, data_only=True).worksheets[0]
        filas = hoja.iter_rows(values_only=True)

        cabecera = [str(c or '').strip().lower() for c in next(filas)]
        col = next((i for i, c in enumerate(cabecera) if 'nombre' in c), None)
        if col is None:
            raise CommandError('No encuentro una columna con "Nombre" en la primera fila.')

        existentes = {clave(n) for n in Producto.objects.values_list('nombre', flat=True)}
        nuevos, repetidos = [], []
        for fila in filas:
            valor = fila[col] if col < len(fila) else None
            nombre = ' '.join(str(valor or '').split())[:200]
            if not nombre:
                continue
            if clave(nombre) in existentes:
                repetidos.append(nombre)
                continue
            existentes.add(clave(nombre))
            nuevos.append(Producto(nombre=nombre))

        Producto.objects.bulk_create(nuevos)

        self.stdout.write(self.style.SUCCESS(f'Productos creados: {len(nuevos)}'))
        if repetidos:
            self.stdout.write(f'Saltados por repetidos ({len(repetidos)}): '
                              + ', '.join(repetidos))
