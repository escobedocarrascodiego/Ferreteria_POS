"""Builder del XML CreditNote UBL 2.1 para Notas de Crédito electrónicas (tipo 07).

Sigue la misma convención que el builder de Invoice en ComprobanteElectronico.generar_xml
(precios con IGV incluido, base = precio / 1.18, afectación gravada '10'), más los nodos
propios de la NC: cac:DiscrepancyResponse (motivo, Catálogo 09) y cac:BillingReference
(documento que modifica).

Orden de elementos según el XSD de CreditNote (importante para SUNAT):
  ext:UBLExtensions, cbc:*, cac:DiscrepancyResponse, cac:BillingReference,
  [cac:Signature se inserta al firmar], cac:AccountingSupplierParty,
  cac:AccountingCustomerParty, cac:TaxTotal, cac:LegalMonetaryTotal, cac:CreditNoteLine+
"""
from decimal import Decimal, ROUND_HALF_UP

from lxml import etree
from django.utils.timezone import localtime

from .monto_letras import monto_en_letras

NSMAP_NC = {
    None: 'urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2',
    'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
    'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
    'ext': 'urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2',
    'ds': 'http://www.w3.org/2000/09/xmldsig#',
}


def generar_xml_nota_credito(comprobante) -> str:
    """Genera el XML de la NC, actualiza totales/xml_generado del comprobante y lo retorna."""
    cbc = lambda t: f"{{{NSMAP_NC['cbc']}}}{t}"
    cac = lambda t: f"{{{NSMAP_NC['cac']}}}{t}"

    doc_mod = comprobante.documento_modificado
    if doc_mod is None:
        raise ValueError("La Nota de Crédito no tiene documento modificado asociado")
    if doc_mod.tipo_comprobante not in ('01', '03'):
        raise ValueError("La NC solo puede modificar boletas (03) o facturas (01)")

    root = etree.Element('CreditNote', nsmap=NSMAP_NC)

    # UBLExtensions (contenedor vacío donde luego se inserta la firma)
    ubl_exts = etree.SubElement(root, '{%s}UBLExtensions' % NSMAP_NC['ext'])
    etree.SubElement(ubl_exts, '{%s}UBLExtension' % NSMAP_NC['ext']).append(
        etree.Element('{%s}ExtensionContent' % NSMAP_NC['ext'])
    )

    etree.SubElement(root, cbc('UBLVersionID')).text = '2.1'
    etree.SubElement(root, cbc('CustomizationID')).text = '2.0'
    etree.SubElement(root, cbc('ID')).text = comprobante.numero_completo

    fecha_local = localtime(comprobante.fecha_emision)
    etree.SubElement(root, cbc('IssueDate')).text = fecha_local.strftime('%Y-%m-%d')
    etree.SubElement(root, cbc('IssueTime')).text = fecha_local.strftime('%H:%M:%S')

    # ------- Cálculo de líneas (mismo criterio que el Invoice: precio incluye IGV) -------
    subtotal_base = Decimal('0.00')
    total_igv = Decimal('0.00')
    line_items = []
    for detalle in comprobante.detalles.all():
        precio_unitario_con_igv = Decimal(str(detalle.precio_unitario))
        cantidad = Decimal(str(detalle.cantidad))

        valor_unitario_sin_igv = (precio_unitario_con_igv / Decimal('1.18')).quantize(
            Decimal('0.0001'), rounding=ROUND_HALF_UP)
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

    if not line_items:
        raise ValueError("La Nota de Crédito no tiene detalles")

    total_con_igv = (subtotal_base + total_igv).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    # Leyenda con monto en letras (Catálogo 52 - 1000)
    note = etree.SubElement(root, cbc('Note'), languageLocaleID='1000')
    note.text = f"SON {monto_en_letras(total_con_igv, comprobante.moneda)}"

    dcc = etree.SubElement(root, cbc('DocumentCurrencyCode'),
                           listID='ISO 4217 Alpha',
                           listName='Currency',
                           listAgencyName='United Nations Economic Commission for Europe')
    dcc.text = comprobante.moneda

    # ------- Motivo de la nota (Catálogo 09) -------
    discrepancy = etree.SubElement(root, cac('DiscrepancyResponse'))
    etree.SubElement(discrepancy, cbc('ReferenceID')).text = doc_mod.numero_completo
    etree.SubElement(discrepancy, cbc('ResponseCode')).text = comprobante.tipo_nota or '01'
    etree.SubElement(discrepancy, cbc('Description')).text = (
        comprobante.motivo_nota or 'Anulación de la operación'
    )

    # ------- Documento que se modifica -------
    billing_ref = etree.SubElement(root, cac('BillingReference'))
    inv_ref = etree.SubElement(billing_ref, cac('InvoiceDocumentReference'))
    etree.SubElement(inv_ref, cbc('ID')).text = doc_mod.numero_completo
    etree.SubElement(inv_ref, cbc('DocumentTypeCode')).text = doc_mod.tipo_comprobante

    # ------- Emisor (misma estructura que el Invoice) -------
    sup_party = etree.SubElement(root, cac('AccountingSupplierParty'))
    etree.SubElement(sup_party, cbc('CustomerAssignedAccountID')).text = comprobante.emisor_ruc
    etree.SubElement(sup_party, cbc('AdditionalAccountID')).text = '6'

    party = etree.SubElement(sup_party, cac('Party'))
    pid = etree.SubElement(party, cac('PartyIdentification'))
    etree.SubElement(pid, cbc('ID'), schemeID='6').text = comprobante.emisor_ruc
    pname = etree.SubElement(party, cac('PartyName'))
    etree.SubElement(pname, cbc('Name')).text = comprobante.emisor_razon_social
    tax = etree.SubElement(party, cac('PartyTaxScheme'))
    etree.SubElement(tax, cbc('CompanyID'), schemeID='6').text = comprobante.emisor_ruc
    ts = etree.SubElement(tax, cac('TaxScheme'))
    etree.SubElement(ts, cbc('ID'), schemeID='UN/ECE 5153', schemeAgencyID='6').text = '1000'
    etree.SubElement(ts, cbc('Name')).text = 'IGV'
    etree.SubElement(ts, cbc('TaxTypeCode')).text = 'VAT'
    ple = etree.SubElement(party, cac('PartyLegalEntity'))
    etree.SubElement(ple, cbc('RegistrationName')).text = comprobante.emisor_razon_social
    etree.SubElement(ple, cbc('CompanyID'), schemeID='6').text = comprobante.emisor_ruc

    # ------- Receptor -------
    cus_party = etree.SubElement(root, cac('AccountingCustomerParty'))
    etree.SubElement(cus_party, cbc('CustomerAssignedAccountID')).text = comprobante.receptor_numero_documento
    etree.SubElement(cus_party, cbc('AdditionalAccountID')).text = comprobante.receptor_tipo_documento

    party = etree.SubElement(cus_party, cac('Party'))
    pid = etree.SubElement(party, cac('PartyIdentification'))
    etree.SubElement(pid, cbc('ID'), schemeID=comprobante.receptor_tipo_documento).text = comprobante.receptor_numero_documento
    pname = etree.SubElement(party, cac('PartyName'))
    etree.SubElement(pname, cbc('Name')).text = comprobante.receptor_razon_social
    ple = etree.SubElement(party, cac('PartyLegalEntity'))
    etree.SubElement(ple, cbc('RegistrationName')).text = comprobante.receptor_razon_social

    # ------- Totales de impuestos -------
    tax_total = etree.SubElement(root, cac('TaxTotal'))
    etree.SubElement(tax_total, cbc('TaxAmount'), currencyID=comprobante.moneda).text = str(total_igv)
    tax_sub = etree.SubElement(tax_total, cac('TaxSubtotal'))
    etree.SubElement(tax_sub, cbc('TaxableAmount'), currencyID=comprobante.moneda).text = str(subtotal_base)
    etree.SubElement(tax_sub, cbc('TaxAmount'), currencyID=comprobante.moneda).text = str(total_igv)
    tcat = etree.SubElement(tax_sub, cac('TaxCategory'))
    etree.SubElement(tcat, cbc('ID'), schemeID='UN/ECE 5305', schemeAgencyID='6').text = 'S'
    etree.SubElement(tcat, cbc('Percent')).text = '18.00'
    etree.SubElement(
        tcat, cbc('TaxExemptionReasonCode'),
        listName='SUNAT:Codigo de Tipo de Afectación del IGV',
        listAgencyName='PE:SUNAT'
    ).text = '10'
    tsch = etree.SubElement(tcat, cac('TaxScheme'))
    etree.SubElement(tsch, cbc('ID'), schemeID='UN/ECE 5153', schemeAgencyID='6').text = '1000'
    etree.SubElement(tsch, cbc('Name')).text = 'IGV'
    etree.SubElement(tsch, cbc('TaxTypeCode')).text = 'VAT'

    # ------- Totales monetarios -------
    legal = etree.SubElement(root, cac('LegalMonetaryTotal'))
    etree.SubElement(legal, cbc('LineExtensionAmount'), currencyID=comprobante.moneda).text = str(subtotal_base)
    etree.SubElement(legal, cbc('TaxInclusiveAmount'), currencyID=comprobante.moneda).text = str(total_con_igv)
    etree.SubElement(legal, cbc('PayableAmount'), currencyID=comprobante.moneda).text = str(total_con_igv)

    # ------- Líneas -------
    for idx, li in enumerate(line_items, 1):
        detalle = li['detalle']
        line = etree.SubElement(root, cac('CreditNoteLine'))
        etree.SubElement(line, cbc('ID')).text = str(idx)

        unit_code = (detalle.unidad_medida or 'NIU').strip().upper()
        if unit_code in ('', 'UND', 'UNI', 'UNID', 'UN', 'UNIT', 'ZZZ'):
            unit_code = 'NIU'
        if len(unit_code) > 3:
            unit_code = unit_code[:3]
        etree.SubElement(line, cbc('CreditedQuantity'), unitCode=unit_code).text = str(detalle.cantidad)
        etree.SubElement(line, cbc('LineExtensionAmount'), currencyID=comprobante.moneda).text = str(li['base_linea'])

        pricing = etree.SubElement(line, cac('PricingReference'))
        alt_price = etree.SubElement(pricing, cac('AlternativeConditionPrice'))
        etree.SubElement(alt_price, cbc('PriceAmount'), currencyID=comprobante.moneda).text = str(
            li['precio_unitario_con_igv'].quantize(Decimal('0.01')))
        etree.SubElement(
            alt_price, cbc('PriceTypeCode'),
            listName='SUNAT:Catalogo 16',
            listAgencyName='PE:SUNAT',
            listURI='urn:pe:gob:sunat:cpe:see:gem:catalogos:catalogo16'
        ).text = '01'

        ttl = etree.SubElement(line, cac('TaxTotal'))
        etree.SubElement(ttl, cbc('TaxAmount'), currencyID=comprobante.moneda).text = str(li['igv_linea'])
        tsub = etree.SubElement(ttl, cac('TaxSubtotal'))
        etree.SubElement(tsub, cbc('TaxableAmount'), currencyID=comprobante.moneda).text = str(li['base_linea'])
        etree.SubElement(tsub, cbc('TaxAmount'), currencyID=comprobante.moneda).text = str(li['igv_linea'])
        tc = etree.SubElement(tsub, cac('TaxCategory'))
        etree.SubElement(tc, cbc('ID'), schemeID='UN/ECE 5305', schemeAgencyID='6').text = 'S'
        etree.SubElement(tc, cbc('Percent')).text = '18.00'
        etree.SubElement(
            tc, cbc('TaxExemptionReasonCode'),
            listName='SUNAT:Codigo de Tipo de Afectación del IGV',
            listAgencyName='PE:SUNAT'
        ).text = '10'
        ts = etree.SubElement(tc, cac('TaxScheme'))
        etree.SubElement(ts, cbc('ID'), schemeID='UN/ECE 5153', schemeAgencyID='6').text = '1000'
        etree.SubElement(ts, cbc('Name')).text = 'IGV'
        etree.SubElement(ts, cbc('TaxTypeCode')).text = 'VAT'

        item = etree.SubElement(line, cac('Item'))
        etree.SubElement(item, cbc('Description')).text = detalle.descripcion

        price = etree.SubElement(line, cac('Price'))
        etree.SubElement(price, cbc('PriceAmount'), currencyID=comprobante.moneda).text = str(li['valor_unitario_sin_igv'])

    # Actualizar totales y persistir (mismo comportamiento que el builder de Invoice)
    comprobante.subtotal = subtotal_base
    comprobante.igv = total_igv
    comprobante.total = total_con_igv

    xml_string = etree.tostring(root, encoding='unicode', pretty_print=True)
    comprobante.xml_generado = xml_string
    comprobante.save()
    return xml_string
