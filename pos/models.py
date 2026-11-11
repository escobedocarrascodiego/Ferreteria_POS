"""
Modelo de datos de la ferreteria.

Dos decisiones que lo diferencian de AdminPOS:

* El producto NO tiene precio ni stock. Es solo un nombre para buscarlo
  rapido; la cantidad y el precio se escriben al armar la boleta.
* La venta guarda su propia copia de la descripcion y del precio, asi que
  renombrar o desactivar un producto no altera las boletas ya emitidas.
"""
import re
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import IntegrityError, models, transaction


class Producto(models.Model):
    nombre = models.CharField(max_length=200, unique=True)
    unidad = models.CharField(max_length=20, default='UND',
                              help_text='UND, KG, M, BOLSA, CAJA...')
    activo = models.BooleanField(default=True, db_index=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['nombre']

    def __str__(self):
        return self.nombre


class Venta(models.Model):
    METODOS_PAGO = [
        ('EFECTIVO', 'Efectivo'),
        ('YAPE', 'Yape'),
        ('PLIN', 'Plin'),
        ('TARJETA', 'Tarjeta'),
        ('TRANSFERENCIA', 'Transferencia'),
    ]

    #: Correlativo INTERNO del ticket, p. ej. T001-00000001. No es el numero
    #: de la boleta/factura electronica: ese lo lleva ComprobanteElectronico.
    numero = models.CharField(max_length=20, unique=True, editable=False)
    cliente_nombre = models.CharField(max_length=200, blank=True)
    cliente_documento = models.CharField(max_length=11, blank=True,
                                         help_text='DNI o RUC')
    metodo_pago = models.CharField(max_length=20, choices=METODOS_PAGO,
                                   default='EFECTIVO')
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    anulada = models.BooleanField(default=False)
    vendedor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    fecha = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-fecha']

    def __str__(self):
        return f'{self.numero} - S/ {self.total}'

    def save(self, *args, **kwargs):
        if self.numero:
            return super().save(*args, **kwargs)

        # Si dos cajas venden a la vez, una choca contra el unique de
        # `numero`: se recalcula el correlativo y se vuelve a intentar.
        for intento in range(6):
            self.numero = self._siguiente_numero()
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                if intento == 5:
                    raise

    # --- Lo que necesita la facturacion electronica -------------------
    # Los precios se digitan CON IGV incluido; el comprobante lo desglosa.
    @property
    def subtotal(self):
        """Base imponible (total sin IGV)."""
        tasa = Decimal(str(settings.IGV_TASA))
        return (self.total / (1 + tasa)).quantize(Decimal('0.01'), ROUND_HALF_UP)

    @property
    def igv(self):
        return self.total - self.subtotal

    @property
    def cliente_dni(self):
        doc = (self.cliente_documento or '').strip()
        return doc if len(doc) == 8 and doc.isdigit() else ''

    @property
    def comprobante(self):
        """Boleta o factura electronica emitida para esta venta, si la hay."""
        for c in self.comprobantes_electronicos.all():
            if c.tipo_comprobante in ('01', '03'):
                return c
        return None

    @staticmethod
    def _siguiente_numero():
        serie = settings.SERIE_TICKET
        # El relleno con ceros hace que el orden alfabetico sea el numerico
        ultimo = (Venta.objects
                  .filter(numero__startswith=f'{serie}-')
                  .order_by('-numero')
                  .values_list('numero', flat=True)
                  .first())
        m = re.search(r'(\d+)$', ultimo or '')
        return f'{serie}-{(int(m.group(1)) if m else 0) + 1:08d}'


class VentaDetalle(models.Model):
    venta = models.ForeignKey(Venta, on_delete=models.CASCADE, related_name='detalles')
    #: Vacio cuando es un item escrito a mano que no esta en el catalogo
    producto = models.ForeignKey(Producto, on_delete=models.SET_NULL,
                                 null=True, blank=True)
    descripcion = models.CharField(max_length=200)
    unidad = models.CharField(max_length=20, default='UND')
    cantidad = models.DecimalField(max_digits=12, decimal_places=3)
    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)

    def calcular_subtotal(self):
        self.subtotal = (self.cantidad * self.precio_unitario).quantize(
            Decimal('0.01'), ROUND_HALF_UP)
        return self.subtotal

    @property
    def cantidad_fmt(self):
        """2.000 -> 2 ; 1.500 -> 1.5"""
        return f'{self.cantidad:f}'.rstrip('0').rstrip('.')

    def __str__(self):
        return f'{self.descripcion} x{self.cantidad_fmt}'


# Los modelos de facturacion electronica viven en su propio archivo, pero se
# exponen desde aqui porque sunat_service los importa de `.models`.
from .models_sunat import (  # noqa: E402,F401
    ComprobanteElectronico,
    ConfiguracionSUNAT,
    DetalleComprobanteElectronico,
    ResumenDiario,
    SUNATEnvioQueue,
)
