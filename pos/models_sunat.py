"""
Facturacion electronica SUNAT: comprobantes, configuracion, resumenes
diarios y cola de reintentos.

Viene de AdminPOS (engine/models.py) sin cambios de logica. Lo unico
adaptado: el comprobante cuelga de Venta/VentaDetalle y la configuracion
SUNAT es una sola (alla habia una por sede).
"""
import uuid
from decimal import Decimal

from django.db import models, transaction
from django.utils import timezone

from .models import Venta, VentaDetalle


class ComprobanteElectronico(models.Model):
    """Modelo base para comprobantes electrónicos (Boletas, Facturas, etc.)"""

    TIPOS_COMPROBANTE = [
        ('01', 'Factura'),
        ('03', 'Boleta'),
        ('07', 'Nota de Crédito'),
        ('08', 'Nota de Débito'),
    ]

    ESTADOS_SUNAT = [
        ('PENDIENTE', 'Pendiente de envío'),
        ('ENVIADO', 'Enviado a SUNAT'),
        ('ACEPTADO', 'Aceptado por SUNAT'),
        ('RECHAZADO', 'Rechazado por SUNAT'),
        ('ANULADO', 'Anulado'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tipo_comprobante = models.CharField(max_length=2, choices=TIPOS_COMPROBANTE, default='03')
    serie = models.CharField(max_length=4, help_text="Serie del comprobante (ej: B001)")
    numero = models.PositiveIntegerField(help_text="Número secuencial del comprobante")
    fecha_emision = models.DateTimeField(auto_now_add=True)
    fecha_vencimiento = models.DateField(null=True, blank=True)

    emisor_ruc = models.CharField(max_length=11)
    emisor_razon_social = models.CharField(max_length=200)
    emisor_direccion = models.CharField(max_length=200)
    emisor_ubigeo = models.CharField(max_length=6, help_text="Código de ubigeo")

    receptor_tipo_documento = models.CharField(max_length=1, choices=[
        ('1', 'DNI'),
        ('6', 'RUC'),
        ('0', 'Otro'),
    ], default='1')
    receptor_numero_documento = models.CharField(max_length=11)
    receptor_razon_social = models.CharField(max_length=200)
    receptor_direccion = models.CharField(max_length=200, blank=True, null=True)

    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    igv = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    estado_sunat = models.CharField(max_length=20, choices=ESTADOS_SUNAT, default='PENDIENTE')
    respuesta_sunat = models.TextField(blank=True, null=True)
    codigo_respuesta_sunat = models.CharField(max_length=10, blank=True, null=True)
    fecha_respuesta_sunat = models.DateTimeField(null=True, blank=True)

    xml_generado = models.TextField(blank=True, null=True)
    xml_firmado = models.TextField(blank=True, null=True)
    cdr_sunat = models.TextField(blank=True, null=True, help_text="Constancia de Recepción de SUNAT")

    venta_pos = models.ForeignKey(Venta, on_delete=models.CASCADE, related_name='comprobantes_electronicos', null=True, blank=True, db_constraint=False)

    # Campos para Notas de Credito/Debito (tipos 07/08)
    documento_modificado = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='notas_asociadas', db_constraint=False,
        help_text="Comprobante (boleta/factura) que esta nota modifica"
    )
    tipo_nota = models.CharField(
        max_length=2, blank=True, null=True,
        help_text="Catalogo 09 SUNAT: 01=Anulacion de la operacion, "
                  "06=Devolucion total, 07=Devolucion por item, ..."
    )
    motivo_nota = models.CharField(max_length=250, blank=True, null=True)

    moneda = models.CharField(max_length=3, default='PEN')
    tipo_cambio = models.DecimalField(max_digits=10, decimal_places=3, default=1.000)
    observaciones = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ['-fecha_emision']
        # unique_together ['serie','numero'] era demasiado estricto: impedia
        # que dos RUC distintos usaran la misma serie, cosa perfectamente
        # legitima. Y demasiado laxo: no distinguia el tipo de comprobante.
        constraints = [
            models.UniqueConstraint(
                fields=['emisor_ruc', 'tipo_comprobante', 'serie', 'numero'],
                name='unique_comprobante_emisor_tipo_serie_numero',
            ),
        ]

    def __str__(self):
        return f"{self.get_tipo_comprobante_display()} {self.serie}-{self.numero:08d}"

    @property
    def numero_completo(self):
        return f"{self.serie}-{self.numero:08d}"

    # Plazo SUNAT para comunicar la baja (RC/RA): 7 dias calendario desde el
    # dia siguiente de recibida la CDR aceptada.
    # Fuente: orientacion.sunat.gob.pe/3529-operatividad
    PLAZO_BAJA_DIAS = 7

    @property
    def fecha_limite_baja(self):
        """
        Ultimo dia (hora de Lima) en que SUNAT acepta la comunicacion de baja.

        Se cuenta desde la CDR (fecha_respuesta_sunat). Si no hay CDR
        registrada se usa la fecha de emision, que es lo conservador.
        """
        from datetime import timedelta
        base = self.fecha_respuesta_sunat or self.fecha_emision
        if not base:
            return None
        base_local = (timezone.localtime(base).date()
                      if timezone.is_aware(base) else base.date())
        return base_local + timedelta(days=self.PLAZO_BAJA_DIAS)

    @property
    def puede_anularse(self):
        """True si el comprobante sigue dentro de la ventana de baja."""
        if self.tipo_comprobante not in ('01', '03'):
            return False
        if self.estado_sunat != 'ACEPTADO':
            return False
        limite = self.fecha_limite_baja
        return limite is not None and timezone.localdate() <= limite

    @property
    def dias_restantes_baja(self):
        """Dias calendario que quedan para comunicar la baja. 0 si vencio."""
        limite = self.fecha_limite_baja
        if limite is None:
            return 0
        return max(0, (limite - timezone.localdate()).days)

    def generar_xml(self):
        # Las Notas de Credito usan su propio builder (CreditNote UBL 2.1).
        # El flujo de boletas y facturas de abajo queda intacto.
        if self.tipo_comprobante == '07':
            from .xml_nota_credito import generar_xml_nota_credito
            return generar_xml_nota_credito(self)

        from lxml import etree
        from django.utils.timezone import localtime
        from decimal import Decimal, ROUND_HALF_UP

        nsmap = {
            None: 'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2',
            'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
            'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
            'ext': 'urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2',
            'ds':  'http://www.w3.org/2000/09/xmldsig#',
        }
        cbc = lambda t: f"{{{nsmap['cbc']}}}{t}"
        cac = lambda t: f"{{{nsmap['cac']}}}{t}"

        root = etree.Element('Invoice', nsmap=nsmap)

        ubl_exts = etree.SubElement(root, '{%s}UBLExtensions' % nsmap['ext'])
        etree.SubElement(ubl_exts, '{%s}UBLExtension' % nsmap['ext']
                        ).append(etree.Element('{%s}ExtensionContent' % nsmap['ext']))

        etree.SubElement(root, cbc('UBLVersionID')).text = '2.1'
        etree.SubElement(root, cbc('CustomizationID')).text = '2.0'

        profile_id = etree.SubElement(root, cbc('ProfileID'))
        profile_id.set('schemeID', '0101')
        profile_id.set('schemeName', 'SUNAT:Catalogo 51')
        profile_id.set('schemeAgencyName', 'PE:SUNAT')
        profile_id.text = '0101'

        etree.SubElement(root, cbc('ID')).text = self.numero_completo
        fecha_local = localtime(self.fecha_emision)
        etree.SubElement(root, cbc('IssueDate')).text = fecha_local.strftime('%Y-%m-%d')
        etree.SubElement(root, cbc('IssueTime')).text = fecha_local.strftime('%H:%M:%S')

        itc = etree.SubElement(
            root,
            cbc('InvoiceTypeCode'),
            listAgencyName='PE:SUNAT',
            listID='0101',
            listName='Tipo de Documento',
            listSchemeURI='urn:pe:gob:sunat:cpe:see:gem:catalogos:catalogo51',
            listURI='urn:pe:gob:sunat:cpe:see:gem:catalogos:catalogo01',
            name='Tipo de Operacion'
        )
        itc.text = self.tipo_comprobante

        note1 = etree.SubElement(root, cbc('Note'), languageLocaleID='1000')
        note1.text = f'SON {self.total} SOLES'

        dcc = etree.SubElement(root, cbc('DocumentCurrencyCode'),
                            listID='ISO 4217 Alpha',
                            listName='Currency',
                            listAgencyName='United Nations Economic Commission for Europe')
        dcc.text = self.moneda

        sup_party = etree.SubElement(root, cac('AccountingSupplierParty'))
        etree.SubElement(sup_party, cbc('CustomerAssignedAccountID')).text = self.emisor_ruc
        etree.SubElement(sup_party, cbc('AdditionalAccountID')).text = '6'

        party = etree.SubElement(sup_party, cac('Party'))

        pid = etree.SubElement(party, cac('PartyIdentification'))
        etree.SubElement(pid, cbc('ID'), schemeID='6').text = self.emisor_ruc

        pname = etree.SubElement(party, cac('PartyName'))
        etree.SubElement(pname, cbc('Name')).text = self.emisor_razon_social

        tax = etree.SubElement(party, cac('PartyTaxScheme'))
        etree.SubElement(tax, cbc('CompanyID'), schemeID='6').text = self.emisor_ruc
        ts = etree.SubElement(tax, cac('TaxScheme'))
        etree.SubElement(ts, cbc('ID'), schemeID='UN/ECE 5153', schemeAgencyID='6').text = '1000'
        etree.SubElement(ts, cbc('Name')).text = 'IGV'
        etree.SubElement(ts, cbc('TaxTypeCode')).text = 'VAT'

        ple = etree.SubElement(party, cac('PartyLegalEntity'))
        etree.SubElement(ple, cbc('RegistrationName')).text = self.emisor_razon_social
        etree.SubElement(ple, cbc('CompanyID'), schemeID='6').text = self.emisor_ruc
        if self.tipo_comprobante == '01':
            reg_addr = etree.SubElement(ple, cac('RegistrationAddress'))
            if getattr(self, 'emisor_ubigeo', None):
                etree.SubElement(reg_addr, cbc('ID')).text = self.emisor_ubigeo
            etree.SubElement(reg_addr, cbc('AddressTypeCode')).text = '0000'
            country = etree.SubElement(reg_addr, cac('Country'))
            etree.SubElement(country, cbc('IdentificationCode')).text = 'PE'

        cus_party = etree.SubElement(root, cac('AccountingCustomerParty'))
        etree.SubElement(cus_party, cbc('CustomerAssignedAccountID')).text = self.receptor_numero_documento
        etree.SubElement(cus_party, cbc('AdditionalAccountID')).text = self.receptor_tipo_documento

        party = etree.SubElement(cus_party, cac('Party'))
        pid = etree.SubElement(party, cac('PartyIdentification'))
        etree.SubElement(pid, cbc('ID'), schemeID=self.receptor_tipo_documento).text = self.receptor_numero_documento

        pname = etree.SubElement(party, cac('PartyName'))
        etree.SubElement(pname, cbc('Name')).text = self.receptor_razon_social

        tax = etree.SubElement(party, cac('PartyTaxScheme'))
        etree.SubElement(tax, cbc('CompanyID'), schemeID=self.receptor_tipo_documento).text = self.receptor_numero_documento
        ts = etree.SubElement(tax, cac('TaxScheme'))
        etree.SubElement(ts, cbc('ID'), schemeID='UN/ECE 5153', schemeAgencyID='6').text = '1000'

        ple = etree.SubElement(party, cac('PartyLegalEntity'))
        etree.SubElement(ple, cbc('RegistrationName')).text = self.receptor_razon_social

        if self.tipo_comprobante == '01':
            forma_pago = 'Contado'
            try:
                if self.fecha_vencimiento and self.fecha_vencimiento > fecha_local.date():
                    forma_pago = 'Credito'
            except Exception:
                pass
            pay_terms = etree.SubElement(root, cac('PaymentTerms'))
            etree.SubElement(pay_terms, cbc('ID')).text = 'FormaPago'
            etree.SubElement(pay_terms, cbc('PaymentMeansID')).text = forma_pago
            if forma_pago == 'Credito':
                etree.SubElement(pay_terms, cbc('Amount'), currencyID=self.moneda).text = str(self.total)
                etree.SubElement(pay_terms, cbc('PaymentDueDate')).text = (
                    self.fecha_vencimiento.strftime('%Y-%m-%d') if self.fecha_vencimiento else fecha_local.strftime('%Y-%m-%d')
                )

        subtotal_base = Decimal('0.00')
        total_igv = Decimal('0.00')
        line_items = []
        for detalle in self.detalles.all():
            precio_unitario_con_igv = Decimal(str(detalle.precio_unitario))
            cantidad = Decimal(str(detalle.cantidad))

            valor_unitario_sin_igv = (precio_unitario_con_igv / Decimal('1.18')).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
            base_linea = (valor_unitario_sin_igv * cantidad).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            total_linea_con_igv = (precio_unitario_con_igv * cantidad).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            igv_linea = (total_linea_con_igv - base_linea).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            subtotal_base += base_linea
            total_igv += igv_linea
            line_items.append({
                'detalle': detalle,
                'precio_unitario_con_igv': precio_unitario_con_igv,
                'valor_unitario_sin_igv': valor_unitario_sin_igv,
                'base_linea': base_linea,
                'igv_linea': igv_linea,
            })

        tax_total = etree.SubElement(root, cac('TaxTotal'))
        etree.SubElement(tax_total, cbc('TaxAmount'), currencyID=self.moneda).text = str(total_igv)

        tax_sub = etree.SubElement(tax_total, cac('TaxSubtotal'))
        etree.SubElement(tax_sub, cbc('TaxableAmount'), currencyID=self.moneda).text = str(subtotal_base)
        etree.SubElement(tax_sub, cbc('TaxAmount'), currencyID=self.moneda).text = str(total_igv)

        tcat = etree.SubElement(tax_sub, cac('TaxCategory'))
        etree.SubElement(tcat, cbc('ID'), schemeID='UN/ECE 5305', schemeAgencyID='6').text = 'S'
        etree.SubElement(tcat, cbc('Percent')).text = '18.00'
        etree.SubElement(
            tcat, cbc('TaxExemptionReasonCode'),
            listName='SUNAT:Codigo de Tipo de AfectaciÃ³n del IGV',
            listAgencyName='PE:SUNAT'
        ).text = '10'
        tsch = etree.SubElement(tcat, cac('TaxScheme'))
        etree.SubElement(tsch, cbc('ID'), schemeID='UN/ECE 5153', schemeAgencyID='6').text = '1000'
        etree.SubElement(tsch, cbc('Name')).text = 'IGV'
        etree.SubElement(tsch, cbc('TaxTypeCode')).text = 'VAT'

        legal = etree.SubElement(root, cac('LegalMonetaryTotal'))
        etree.SubElement(legal, cbc('LineExtensionAmount'), currencyID=self.moneda).text = str(subtotal_base)
        total_con_igv = (subtotal_base + total_igv).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        etree.SubElement(legal, cbc('TaxInclusiveAmount'), currencyID=self.moneda).text = str(total_con_igv)
        etree.SubElement(legal, cbc('PayableAmount'),       currencyID=self.moneda).text = str(total_con_igv)

        for idx, li in enumerate(line_items, 1):
            detalle = li['detalle']
            line = etree.SubElement(root, cac('InvoiceLine'))
            etree.SubElement(line, cbc('ID')).text = str(idx)
            unit_code = (detalle.unidad_medida or 'NIU').strip().upper()
            if unit_code in ('', 'UND', 'UNI', 'UNID', 'UN', 'UNIT', 'ZZZ'):
                unit_code = 'NIU'
            if len(unit_code) > 3:
                unit_code = unit_code[:3]
            etree.SubElement(line, cbc('InvoicedQuantity'), unitCode=unit_code).text = str(detalle.cantidad)
            etree.SubElement(line, cbc('LineExtensionAmount'), currencyID=self.moneda).text = str(li['base_linea'])

            pricing = etree.SubElement(line, cac('PricingReference'))
            alt_price = etree.SubElement(pricing, cac('AlternativeConditionPrice'))
            etree.SubElement(alt_price, cbc('PriceAmount'), currencyID=self.moneda).text = str(li['precio_unitario_con_igv'].quantize(Decimal('0.01')))
            etree.SubElement(
                alt_price, cbc('PriceTypeCode'),
                listName='SUNAT:Catalogo 16',
                listAgencyName='PE:SUNAT',
                listURI='urn:pe:gob:sunat:cpe:see:gem:catalogos:catalogo16'
            ).text = '01'

            ttl = etree.SubElement(line, cac('TaxTotal'))
            etree.SubElement(ttl, cbc('TaxAmount'), currencyID=self.moneda).text = str(li['igv_linea'])

            tsub = etree.SubElement(ttl, cac('TaxSubtotal'))
            etree.SubElement(tsub, cbc('TaxableAmount'), currencyID=self.moneda).text = str(li['base_linea'])
            etree.SubElement(tsub, cbc('TaxAmount'),     currencyID=self.moneda).text = str(li['igv_linea'])

            tc = etree.SubElement(tsub, cac('TaxCategory'))
            etree.SubElement(tc, cbc('ID'), schemeID='UN/ECE 5305', schemeAgencyID='6').text = 'S'
            etree.SubElement(tc, cbc('Percent')).text = '18.00'
            etree.SubElement(
                tc, cbc('TaxExemptionReasonCode'),
                listName='SUNAT:Codigo de Tipo de AfectaciÃ³n del IGV',
                listAgencyName='PE:SUNAT'
            ).text = '10'

            ts = etree.SubElement(tc, cac('TaxScheme'))
            etree.SubElement(ts, cbc('ID'), schemeID='UN/ECE 5153', schemeAgencyID='6').text = '1000'
            etree.SubElement(ts, cbc('Name')).text = 'IGV'
            etree.SubElement(ts, cbc('TaxTypeCode')).text = 'VAT'

            item = etree.SubElement(line, cac('Item'))
            etree.SubElement(item, cbc('Description')).text = detalle.descripcion

            price = etree.SubElement(line, cac('Price'))
            etree.SubElement(price, cbc('PriceAmount'), currencyID=self.moneda).text = str(li['valor_unitario_sin_igv'])

        self.subtotal = subtotal_base
        self.igv = total_igv
        self.total = (subtotal_base + total_igv).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        xml_string = etree.tostring(root, encoding='unicode', pretty_print=True)
        self.xml_generado = xml_string
        self.save()
        return xml_string


class DetalleComprobanteElectronico(models.Model):
    """Detalle de items en un comprobante electrónico"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    comprobante = models.ForeignKey(ComprobanteElectronico, on_delete=models.CASCADE, related_name='detalles')

    descripcion = models.CharField(max_length=200)
    cantidad = models.DecimalField(max_digits=12, decimal_places=3, default=1)
    unidad_medida = models.CharField(max_length=3, default='NIU', help_text="Código de unidad de medida SUNAT")

    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)

    igv = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tipo_igv = models.CharField(max_length=2, default='10', help_text="Código de tipo de IGV")

    detalle_pos = models.ForeignKey(VentaDetalle, on_delete=models.CASCADE, null=True, blank=True, db_constraint=False)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"{self.descripcion} - {self.cantidad} x {self.precio_unitario}"

    def save(self, *args, **kwargs):
        unidad_normalizada = (self.unidad_medida or '').strip().upper()
        if unidad_normalizada in ('', 'UND', 'UNI', 'UNID', 'UN', 'UNIT'):
            unidad_normalizada = 'NIU'
        if len(unidad_normalizada) > 3:
            unidad_normalizada = unidad_normalizada[:3]
        if unidad_normalizada == 'ZZZ':
            unidad_normalizada = 'NIU'
        self.unidad_medida = unidad_normalizada or 'NIU'

        if not self.subtotal:
            self.subtotal = self.cantidad * self.precio_unitario

        if not self.igv:
            self.igv = self.subtotal * Decimal('0.18')

        super().save(*args, **kwargs)


class ConfiguracionSUNAT(models.Model):
    """Configuración para la integración con SUNAT"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    certificado_digital = models.FileField(upload_to='certificados/', null=True, blank=True)
    password_certificado = models.CharField(max_length=100, blank=True, null=True)

    #: Logo que sale impreso en boletas y facturas (en AdminPOS era el de la sede)
    logo = models.ImageField(upload_to='logos/', null=True, blank=True,
                             help_text='Opcional. Sale en el PDF de boletas y facturas.')

    serie_boleta = models.CharField(max_length=4, default='B001', help_text="Serie para boletas")
    serie_factura = models.CharField(max_length=4, default='F001', help_text="Serie para facturas")
    ultimo_numero_boleta = models.PositiveIntegerField(default=0)
    ultimo_numero_factura = models.PositiveIntegerField(default=0)
    ultimo_numero_nc_boleta = models.PositiveIntegerField(
        default=0, help_text="Correlativo de NC asociadas a boletas")
    ultimo_numero_nc_factura = models.PositiveIntegerField(
        default=0, help_text="Correlativo de NC asociadas a facturas")

    ambiente_sunat = models.CharField(max_length=1, choices=[
        ('1', 'Producción'),
        ('2', 'Certificación'),
    ], default='2')

    emisor_ruc = models.CharField(max_length=11, blank=True, null=True)
    emisor_razon_social = models.CharField(max_length=200, blank=True, null=True)
    emisor_direccion = models.CharField(max_length=200, blank=True, null=True)
    emisor_ubigeo = models.CharField(max_length=6, blank=True, null=True)

    url_sunat = models.URLField(blank=True, null=True)
    usuario_sunat = models.CharField(max_length=50, blank=True, null=True)
    password_sunat = models.CharField(max_length=50, blank=True, null=True)

    activo = models.BooleanField(default=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_modificacion = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Configuración SUNAT'
        verbose_name_plural = 'Configuraciones SUNAT'

    def __str__(self):
        return f"Configuración SUNAT - {self.emisor_razon_social or self.emisor_ruc or 'sin datos'}"

    def _incrementar_contador(self, campo):
        """Incrementa un correlativo de forma atomica (select_for_update)."""
        with transaction.atomic():
            config = ConfiguracionSUNAT.objects.select_for_update().get(pk=self.pk)
            nuevo_numero = getattr(config, campo) + 1
            setattr(config, campo, nuevo_numero)
            config.save(update_fields=[campo])

        setattr(self, campo, nuevo_numero)
        return nuevo_numero

    def get_siguiente_numero(self, tipo_comprobante):
        """
        Siguiente correlativo del comprobante.

        Va bajo select_for_update porque dos ventas simultaneas que reciban
        el mismo numero hacen que SUNAT rechace la segunda: no admite
        duplicados de serie-numero.
        """
        campo = {'03': 'ultimo_numero_boleta',
                 '01': 'ultimo_numero_factura'}.get(tipo_comprobante)
        if not campo:
            return 1
        return self._incrementar_contador(campo)

    def get_siguiente_numero_nota(self, tipo_doc_modificado):
        """Correlativo de Nota de Credito segun el documento que modifica."""
        campo = {
            '03': 'ultimo_numero_nc_boleta',
            '01': 'ultimo_numero_nc_factura',
        }.get(tipo_doc_modificado)
        if not campo:
            raise ValueError(
                f'Tipo de documento no soportado para NC: {tipo_doc_modificado}')
        return self._incrementar_contador(campo)


class ResumenDiario(models.Model):
    """Control de Resúmenes Diarios (RC) enviados a SUNAT (hoy se usan para bajas de boletas).

    Es la fuente de verdad del correlativo RC-YYYYMMDD-### y persiste el ticket
    que devuelve sendSummary para su posterior consulta con getStatus.
    """

    ESTADOS = [
        ('GENERADO', 'Generado'),
        ('ENVIADO', 'Enviado (con ticket)'),
        ('ACEPTADO', 'Aceptado por SUNAT'),
        ('RECHAZADO', 'Rechazado por SUNAT'),
        ('ERROR', 'Error'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resumen_id = models.CharField(max_length=20, unique=True, help_text="Identificador RC-YYYYMMDD-###")
    emisor_ruc = models.CharField(max_length=11)
    fecha_referencia = models.DateField(help_text="Fecha de emisión de las boletas incluidas")
    fecha_emision = models.DateField(help_text="Fecha de generación/envío del resumen")
    correlativo = models.PositiveIntegerField()

    ticket = models.CharField(max_length=50, blank=True, null=True)
    estado = models.CharField(max_length=12, choices=ESTADOS, default='GENERADO')
    codigo_respuesta = models.CharField(max_length=10, blank=True, null=True)
    respuesta_sunat = models.TextField(blank=True, null=True)

    comprobantes = models.ManyToManyField(
        ComprobanteElectronico, blank=True, related_name='resumenes_diarios'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Resumen Diario (RC)'
        verbose_name_plural = 'Resúmenes Diarios (RC)'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['emisor_ruc', 'fecha_emision', 'correlativo'],
                name='unique_resumen_por_ruc_fecha_correlativo',
            ),
        ]

    def __str__(self):
        return f"{self.resumen_id} [{self.estado}]"

    @classmethod
    def siguiente_correlativo(cls, emisor_ruc: str, fecha_emision) -> int:
        """Devuelve el siguiente correlativo del día para el RUC, de forma atómica."""
        from django.db import transaction
        with transaction.atomic():
            ultimo = (
                cls.objects.select_for_update()
                .filter(emisor_ruc=emisor_ruc, fecha_emision=fecha_emision)
                .order_by('-correlativo')
                .first()
            )
            return (ultimo.correlativo if ultimo else 0) + 1


class SUNATEnvioQueue(models.Model):
    """Cola de reintentos para envíos a SUNAT cuando el servicio no está disponible."""

    ESTADOS = [
        ('PENDIENTE', 'Pendiente'),
        ('ENVIANDO', 'Enviando'),
        ('COMPLETADO', 'Completado'),
        ('CANCELADO', 'Cancelado'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    comprobante = models.ForeignKey(
        'ComprobanteElectronico', on_delete=models.CASCADE,
        related_name='reintentos_sunat', null=True, blank=True, db_constraint=False
    )

    cbc_id = models.CharField(max_length=32, help_text="cbc:ID del comprobante (SERIE-NUMERO)")
    emisor_ruc = models.CharField(max_length=11)
    tipo = models.CharField(max_length=2, help_text="01=Factura, 03=Boleta")
    serie = models.CharField(max_length=4)
    numero = models.PositiveIntegerField()

    zip_nombre = models.CharField(max_length=128)
    zip_base64 = models.TextField()
    xml_sha1 = models.CharField(max_length=40)

    estado = models.CharField(max_length=12, choices=ESTADOS, default='PENDIENTE')
    intentos = models.PositiveIntegerField(default=0)
    max_intentos = models.PositiveIntegerField(default=5)
    next_retry_at = models.DateTimeField(null=True, blank=True)

    fault_code = models.CharField(max_length=50, blank=True, null=True)
    fault_string = models.TextField(blank=True, null=True)
    last_response_snippet = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['estado', 'next_retry_at']),
            models.Index(fields=['cbc_id']),
            models.Index(fields=['zip_nombre']),
        ]
        ordering = ['next_retry_at', 'created_at']

    def __str__(self):
        return f"{self.zip_nombre} [{self.estado}] intentos={self.intentos}/{self.max_intentos}"

    def schedule_next_retry(self):
        from datetime import timedelta
        steps = [30, 120, 300, 600, 600]
        idx = min(self.intentos, len(steps) - 1)
        seconds = steps[idx]
        self.next_retry_at = timezone.now() + timedelta(seconds=seconds)
