from cryptography.hazmat.primitives.serialization import pkcs12
from django.contrib import admin, messages
from django.utils import timezone

from .models import (
    ComprobanteElectronico,
    ConfiguracionSUNAT,
    DetalleComprobanteElectronico,
    Producto,
    ResumenDiario,
    SUNATEnvioQueue,
    Venta,
    VentaDetalle,
)


@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'unidad', 'activo')
    list_filter = ('activo',)
    search_fields = ('nombre',)


class VentaDetalleInline(admin.TabularInline):
    model = VentaDetalle
    extra = 0
    can_delete = False
    readonly_fields = ('producto', 'descripcion', 'unidad', 'cantidad',
                       'precio_unitario', 'subtotal')


@admin.register(Venta)
class VentaAdmin(admin.ModelAdmin):
    list_display = ('numero', 'fecha', 'cliente_nombre', 'metodo_pago',
                    'total', 'anulada', 'vendedor')
    list_filter = ('anulada', 'metodo_pago')
    search_fields = ('numero', 'cliente_nombre', 'cliente_documento')
    # Sin date_hierarchy: necesita las tablas de zona horaria de MySQL, que
    # Wamp no trae cargadas, y la pagina revienta.
    readonly_fields = ('numero', 'total', 'vendedor', 'fecha')
    inlines = [VentaDetalleInline]


# ----------------------------------------------------------------------
# Facturacion electronica (SUNAT)
# ----------------------------------------------------------------------
def problema_del_certificado(config):
    """
    Devuelve un texto si el certificado guardado no sirve para firmar, o None.

    Abre el .p12 exactamente igual que lo hace el servicio de envio, asi que
    si aqui falla, la firma del comprobante tambien fallara.
    """
    if not config.certificado_digital:
        return 'Aún no se ha cargado el certificado digital.'
    try:
        with config.certificado_digital.open('rb') as f:
            contenido = f.read()
    except (OSError, ValueError):
        return 'No se puede leer el archivo del certificado. Vuelve a subirlo.'
    clave = (config.password_certificado or '').encode('utf-8')
    try:
        llave, cert, _ = pkcs12.load_key_and_certificates(contenido, clave)
    except ValueError:
        return ('La clave guardada no abre el archivo de certificado cargado '
                f'({config.certificado_digital.name.split("/")[-1]}). Con esta combinación '
                'los comprobantes no se podrán firmar.')
    if llave is None or cert is None:
        return 'El archivo no contiene la llave privada junto con el certificado.'
    if cert.not_valid_after_utc < timezone.now():
        return f'El certificado venció el {cert.not_valid_after_utc:%d/%m/%Y}.'
    return None


@admin.register(ConfiguracionSUNAT)
class ConfiguracionSUNATAdmin(admin.ModelAdmin):
    """Aqui se cargan el RUC, el certificado, la clave SOL y las series."""

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Se guarda siempre; si el certificado no abre, solo se avisa
        problema = problema_del_certificado(obj)
        if problema:
            messages.warning(request, f'Guardado, pero ojo: {problema}')
        else:
            messages.success(request, 'El certificado abre correctamente con la clave guardada.')

    list_display = ['emisor_razon_social', 'emisor_ruc', 'ambiente_sunat',
                    'serie_boleta', 'serie_factura', 'activo']
    fieldsets = (
        ('General', {'fields': ('activo', 'ambiente_sunat')}),
        ('Datos del emisor', {
            'fields': ('emisor_ruc', 'emisor_razon_social', 'emisor_direccion', 'emisor_ubigeo',
                       'logo')}),
        ('Certificado digital (.p12 / .pfx)', {
            'fields': ('certificado_digital', 'password_certificado')}),
        ('Usuario SOL secundario', {
            'fields': ('usuario_sunat', 'password_sunat', 'url_sunat')}),
        ('Series y correlativos', {
            'fields': ('serie_boleta', 'ultimo_numero_boleta', 'ultimo_numero_nc_boleta',
                       'serie_factura', 'ultimo_numero_factura', 'ultimo_numero_nc_factura')}),
    )

    def has_add_permission(self, request):
        # Una sola configuracion: el sistema usa la que este activa
        return not ConfiguracionSUNAT.objects.exists()


class DetalleComprobanteInline(admin.TabularInline):
    model = DetalleComprobanteElectronico
    extra = 0
    can_delete = False
    fields = readonly_fields = ['descripcion', 'cantidad', 'precio_unitario', 'subtotal', 'igv']


@admin.register(ComprobanteElectronico)
class ComprobanteElectronicoAdmin(admin.ModelAdmin):
    """Solo consulta: los comprobantes se emiten y anulan desde el POS."""
    list_display = ['numero_completo', 'tipo_comprobante', 'fecha_emision',
                    'receptor_razon_social', 'total', 'estado_sunat']
    list_filter = ['tipo_comprobante', 'estado_sunat']
    search_fields = ['serie', 'receptor_razon_social', 'receptor_numero_documento']
    inlines = [DetalleComprobanteInline]

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ResumenDiario)
class ResumenDiarioAdmin(admin.ModelAdmin):
    list_display = ('resumen_id', 'emisor_ruc', 'fecha_referencia', 'fecha_emision',
                    'ticket', 'estado', 'codigo_respuesta')
    list_filter = ('estado',)

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields] + ['comprobantes']

    def has_add_permission(self, request):
        return False


@admin.register(SUNATEnvioQueue)
class SUNATEnvioQueueAdmin(admin.ModelAdmin):
    list_display = ('zip_nombre', 'cbc_id', 'estado', 'intentos', 'max_intentos',
                    'next_retry_at', 'created_at')
    list_filter = ('estado',)

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False
