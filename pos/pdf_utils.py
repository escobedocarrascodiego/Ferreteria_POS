"""
Utilidades para generación de PDF de representación impresa de comprobantes electrónicos
"""
from pathlib import Path
from datetime import datetime
import io
import base64
from typing import Dict, Any, Optional
from decimal import Decimal

from reportlab.lib.pagesizes import A4
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, Flowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch, mm
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
import qrcode
from lxml import etree

# Namespaces UBL
NSMAP = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "ext": "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
    "ccts": "urn:un:unece:uncefact:documentation:2",
    "qdt": "urn:oasis:names:specification:ubl:schema:xsd:QualifiedDatatypes-2",
    "sac": "urn:sunat:names:specification:ubl:peru:schema:xsd:SunatAggregateComponents-1",
    "udt": "urn:un:unece:uncefact:data:specification:UnqualifiedDataTypesSchemaModule:2",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}


def resolver_logo(ruta_logo: Optional[str] = None) -> Optional[str]:
    """
    Ruta del logo a imprimir.

    Se prefiere el logo de la SEDE. Antes esto apuntaba fijo a
    static/images/logo.jpg, asi que todas las sedes salian con el mismo
    logo y no habia manera de cambiarlo desde la aplicacion.
    """
    if ruta_logo and Path(ruta_logo).exists():
        return ruta_logo
    return None


def extraer_datos_boleta(xml_firmado: str, cdr: Optional[str] = None) -> Dict[str, Any]:
    """Extrae los datos del XML firmado para generar el PDF"""
    try:
        if not xml_firmado:
            raise ValueError("XML firmado está vacío o es None")

        root = etree.fromstring(xml_firmado.encode("utf-8"))

        # Determinar tipo (Invoice: boleta/factura; CreditNote: nota de credito)
        root_localname = etree.QName(root.tag).localname
        tipo_code_elem = root.find(".//cbc:InvoiceTypeCode", namespaces=NSMAP)
        tipo_label = "BOLETA ELECTRÓNICA"
        if root_localname == "CreditNote":
            tipo_label = "NOTA DE CRÉDITO ELECTRÓNICA"
        elif tipo_code_elem is not None and (tipo_code_elem.text or "").strip() == "01":
            tipo_label = "FACTURA ELECTRÓNICA"

        datos = {
            "tipo_documento": tipo_label,
            "numero": root.find(".//cbc:ID", namespaces=NSMAP).text if root.find(".//cbc:ID", namespaces=NSMAP) is not None else "",
            "fecha_emision": root.find(".//cbc:IssueDate", namespaces=NSMAP).text if root.find(".//cbc:IssueDate", namespaces=NSMAP) is not None else "",
            "hora_emision": root.find(".//cbc:IssueTime", namespaces=NSMAP).text if root.find(".//cbc:IssueTime", namespaces=NSMAP) is not None else "",
            "moneda": root.find(".//cbc:DocumentCurrencyCode", namespaces=NSMAP).text if root.find(".//cbc:DocumentCurrencyCode", namespaces=NSMAP) is not None else "PEN",
        }

        # Leyenda SON (Catálogo 52 - 1000)
        try:
            note_1000 = root.find(".//cbc:Note[@languageLocaleID='1000']", namespaces=NSMAP)
            datos["leyenda_son"] = note_1000.text if (note_1000 is not None and note_1000.text) else ""
        except Exception:
            datos["leyenda_son"] = ""

        # Emisor
        emisor_party = root.find(".//cac:AccountingSupplierParty/cac:Party", namespaces=NSMAP)
        if emisor_party is not None:
            datos["emisor"] = {
                "ruc": emisor_party.find(".//cac:PartyIdentification/cbc:ID", namespaces=NSMAP).text if emisor_party.find(".//cac:PartyIdentification/cbc:ID", namespaces=NSMAP) is not None else "",
                "razon_social": emisor_party.find(".//cac:PartyName/cbc:Name", namespaces=NSMAP).text if emisor_party.find(".//cac:PartyName/cbc:Name", namespaces=NSMAP) is not None else "",
                "direccion": emisor_party.find(".//cac:PostalAddress/cbc:StreetName", namespaces=NSMAP).text if emisor_party.find(".//cac:PostalAddress/cbc:StreetName", namespaces=NSMAP) is not None else "",
                "correo": "",  # opcional si lo tienes en el XML
                "telefono": "",  # opcional
            }
        else:
            datos["emisor"] = {"ruc": "", "razon_social": "", "direccion": "", "correo": "", "telefono": ""}

        # Receptor
        receptor_party = root.find(".//cac:AccountingCustomerParty/cac:Party", namespaces=NSMAP)
        if receptor_party is not None:
            party_id = receptor_party.find(".//cac:PartyIdentification/cbc:ID", namespaces=NSMAP)
            datos["receptor"] = {
                "tipo_documento": party_id.get("schemeID") if party_id is not None else "1",
                "numero_documento": party_id.text if party_id is not None else "",
                "razon_social": receptor_party.find(".//cac:PartyName/cbc:Name", namespaces=NSMAP).text if receptor_party.find(".//cac:PartyName/cbc:Name", namespaces=NSMAP) is not None else "",
                "direccion": "",  # si está en tu XML, extraerla
            }
        else:
            datos["receptor"] = {"tipo_documento": "1", "numero_documento": "", "razon_social": "", "direccion": ""}

        # Totales
        subtotal = "0.00"
        igv = "0.00"
        total = "0.00"

        monetary_total = root.find(".//cac:LegalMonetaryTotal", namespaces=NSMAP)
        if monetary_total is not None:
            subtotal_elem = monetary_total.find(".//cbc:TaxExclusiveAmount", namespaces=NSMAP) or monetary_total.find(".//cbc:LineExtensionAmount", namespaces=NSMAP)
            if subtotal_elem is not None and (subtotal_elem.text or "").strip():
                subtotal = subtotal_elem.text
            total_elem = monetary_total.find(".//cbc:PayableAmount", namespaces=NSMAP) or monetary_total.find(".//cbc:TaxInclusiveAmount", namespaces=NSMAP)
            if total_elem is not None and (total_elem.text or "").strip():
                total = total_elem.text

        try:
            header_tax_total = root.find("./cac:TaxTotal", namespaces=NSMAP)
            if header_tax_total is not None:
                tax_amount_elem = header_tax_total.find("./cbc:TaxAmount", namespaces=NSMAP)
                if tax_amount_elem is not None and (tax_amount_elem.text or "").strip():
                    igv = f"{Decimal(tax_amount_elem.text):.2f}"
            if igv == "0.00":
                igv = f"{(Decimal(total or '0') - Decimal(subtotal or '0')):.2f}"
        except Exception:
            try:
                igv = f"{(Decimal(total or '0') - Decimal(subtotal or '0')):.2f}"
            except Exception:
                igv = "0.00"

        datos["totales"] = {"subtotal": subtotal, "igv": igv, "total": total}

        # Ítems
        items = []
        lineas = root.findall(".//cac:InvoiceLine", namespaces=NSMAP) or \
                 root.findall(".//cac:CreditNoteLine", namespaces=NSMAP)
        for line in lineas:
            desc_elem = line.find(".//cac:Item/cbc:Description", namespaces=NSMAP)
            qty_elem = line.find(".//cbc:InvoicedQuantity", namespaces=NSMAP)
            if qty_elem is None:
                qty_elem = line.find(".//cbc:CreditedQuantity", namespaces=NSMAP)
            price_elem = line.find(".//cac:Price/cbc:PriceAmount", namespaces=NSMAP)
            amount_elem = line.find(".//cbc:LineExtensionAmount", namespaces=NSMAP)
            code_elem = line.find(".//cac:Item/cac:SellersItemIdentification/cbc:ID", namespaces=NSMAP) \
                        or line.find(".//cac:Item/cac:StandardItemIdentification/cbc:ID", namespaces=NSMAP) \
                        or line.find(".//cbc:ID", namespaces=NSMAP)
            item = {
                "descripcion": desc_elem.text if desc_elem is not None else "",
                "cantidad": qty_elem.text if qty_elem is not None else "1",
                "precio_unitario": price_elem.text if price_elem is not None else "0.00",
                "subtotal": amount_elem.text if amount_elem is not None else "0.00",
                "unidad_medida": (qty_elem.get("unitCode") if qty_elem is not None else ""),
                "codigo": code_elem.text if code_elem is not None else "",
            }
            items.append(item)
        datos["items"] = items

        # CDR
        if cdr:
            try:
                cdr_root = etree.fromstring(cdr.encode("utf-8"))
                status_elem = cdr_root.find(".//cbc:ResponseCode", namespaces=NSMAP)
                if status_elem is not None:
                    response_code = status_elem.text
                    if response_code == "0":
                        datos["estado_sunat"] = "ACEPTADO POR SUNAT"
                    else:
                        datos["estado_sunat"] = f"RECHAZADO POR SUNAT (Código: {response_code})"
                else:
                    datos["estado_sunat"] = "ACEPTADO POR SUNAT"

                digest_elem = cdr_root.find(".//ds:DigestValue", namespaces=NSMAP)
                datos["hash_cdr"] = digest_elem.text if digest_elem is not None else ""

                response_date_elem = cdr_root.find(".//cbc:ResponseDate", namespaces=NSMAP)
                datos["fecha_respuesta_sunat"] = response_date_elem.text if response_date_elem is not None else ""
            except Exception as e:
                print(f"Error procesando CDR: {e}")
                datos["estado_sunat"] = "ACEPTADO POR SUNAT (CDR no procesable)"
                datos["hash_cdr"] = ""
                datos["fecha_respuesta_sunat"] = ""
        else:
            datos["estado_sunat"] = "Enviado a SUNAT - pendiente de CDR"
            datos["hash_cdr"] = ""
            datos["fecha_respuesta_sunat"] = ""

        # Cadena QR
        datos["cadena_qr"] = generar_cadena_qr(datos)
        return datos

    except Exception as e:
        print(f"Error extraer_datos_boleta: {e}")
        return {
            "tipo_documento": "BOLETA ELECTRÓNICA",
            "numero": "",
            "fecha_emision": "",
            "hora_emision": "",
            "moneda": "PEN",
            "emisor": {"ruc": "", "razon_social": "", "direccion": ""},
            "receptor": {"tipo_documento": "1", "numero_documento": "", "razon_social": ""},
            "totales": {"subtotal": "0.00", "igv": "0.00", "total": "0.00"},
            "items": [],
            "estado_sunat": "Error procesando datos",
            "hash_cdr": "",
            "fecha_respuesta_sunat": "",
            "cadena_qr": "",
        }


def generar_cadena_qr(datos: Dict[str, Any]) -> str:
    """Genera la cadena QR según el formato estándar de SUNAT"""
    try:
        if not datos:
            return ""
        ruc = datos.get("emisor", {}).get("ruc", "") or ""
        numero = datos.get("numero", "") or ""
        serie = numero.split("-")[0] if "-" in numero else ""
        numero_doc = numero.split("-")[1] if "-" in numero else numero
        tipo_doc_label = (datos.get("tipo_documento", "") or "").upper()
        if "FACTURA" in tipo_doc_label:
            tipo = "01"
        elif "CRÉDITO" in tipo_doc_label or "CREDITO" in tipo_doc_label:
            tipo = "07"
        else:
            tipo = "03"
        igv = datos.get("totales", {}).get("igv", "0.00") or "0.00"
        total = datos.get("totales", {}).get("total", "0.00") or "0.00"
        fecha = datos.get("fecha_emision", "") or ""
        doc_cliente = datos.get("receptor", {}).get("numero_documento", "") or ""
        tipo_doc_cliente = datos.get("receptor", {}).get("tipo_documento", "1") or "1"
        return f"{ruc}|{tipo}|{serie}|{numero_doc}|{igv}|{total}|{fecha}|{doc_cliente}|{tipo_doc_cliente}"
    except Exception as e:
        print(f"Error generando cadena QR: {e}")
        return ""


def extraer_hash_cdr(cdr: str) -> str:
    """Extrae el hash del CDR si está disponible"""
    try:
        if not cdr:
            return ""
        root = etree.fromstring(cdr.encode("utf-8"))
        digest = root.find(".//ds:DigestValue", namespaces=NSMAP)
        return digest.text if digest is not None else ""
    except:
        return ""


def generar_pdf_boleta(xml_firmado: str, cdr: Optional[str] = None,
                       logo_path: Optional[str] = None) -> bytes:
    """
    Genera el PDF:
      - Boleta: ticket térmico 80 mm.
      - Factura: A4 con layout de 2 columnas, tabla de ítems, totales, QR.

    logo_path: ruta en disco del logo de la SEDE emisora. Si no se pasa, el
    comprobante sale sin logo (antes salia uno fijo del proyecto).
    """
    try:
        if not xml_firmado:
            raise ValueError("XML firmado está vacío o es None")

        datos = extraer_datos_boleta(xml_firmado, cdr)
        datos['logo_path'] = logo_path

        buffer = io.BytesIO()
        es_factura = "FACTURA" in (datos.get("tipo_documento") or "")

        # === Estilos base ===
        styles = getSampleStyleSheet()
        gray_head = colors.HexColor("#666666")
        gray_line = colors.HexColor("#BFC4CC")
        gray_light = colors.HexColor("#E5E7EB")
        gray_fill = colors.HexColor("#F2F4F7")

        if es_factura:
            # Márgenes y medidas para A4
            page_width, page_height = A4
            left_margin = right_margin = 16 * mm
            top_margin = 14 * mm
            bottom_margin = 16 * mm

            doc = SimpleDocTemplate(
                buffer,
                pagesize=(page_width, page_height),
                leftMargin=left_margin,
                rightMargin=right_margin,
                topMargin=top_margin,
                bottomMargin=bottom_margin,
            )

            # Estilos de párrafo
            p_small = ParagraphStyle('p_small', parent=styles['Normal'], fontName='Helvetica', fontSize=9, leading=11)
            p_base = ParagraphStyle('p_base', parent=styles['Normal'], fontName='Helvetica', fontSize=10, leading=12)
            p_muted = ParagraphStyle('p_muted', parent=styles['Normal'], fontName='Helvetica', fontSize=9, leading=11, textColor=gray_head)
            p_bold = ParagraphStyle('p_bold', parent=p_base, fontName='Helvetica-Bold')
            p_right = ParagraphStyle('p_right', parent=p_base, alignment=TA_RIGHT)
            p_right_bold = ParagraphStyle('p_right_bold', parent=p_right, fontName='Helvetica-Bold', fontSize=12)
            h_factura = ParagraphStyle('h_factura', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=16, alignment=TA_CENTER, leading=18)
            h_serie = ParagraphStyle('h_serie', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=11, alignment=TA_CENTER)

            story = []

            # ===== Encabezado (2 columnas 70/30) =====
            content_w = (page_width - left_margin - right_margin)
            left_w = content_w * 0.70
            right_w = content_w * 0.30

            # Columna izquierda: logo de la sede + datos de la empresa
            logo_img = None
            ruta = resolver_logo(datos.get('logo_path'))
            if ruta:
                try:
                    logo_img = Image(ruta, width=26 * mm, height=26 * mm)
                except Exception:
                    logo_img = None

            emisor = datos.get('emisor', {})
            emisor_lines = []
            if emisor.get('razon_social'):
                emisor_lines.append(Paragraph(f"<b>{emisor['razon_social']}</b>", p_base))
            if emisor.get('ruc'):
                emisor_lines.append(Paragraph(f"RUC: {emisor['ruc']}", p_small))
            if emisor.get('direccion'):
                emisor_lines.append(Paragraph(emisor['direccion'], p_small))
            if emisor.get('correo'):
                emisor_lines.append(Paragraph(emisor['correo'], p_small))
            if emisor.get('telefono'):
                emisor_lines.append(Paragraph(emisor['telefono'], p_small))
            if not emisor_lines:
                emisor_lines = [Paragraph("", p_small)]

            # emisor_box: 1 columna, N filas (evita el error de columnas)
            emisor_box = Table([[line] for line in emisor_lines], colWidths=[left_w])
            emisor_box.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, -1), 0),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ]))

            # left_block: 1 columna, 2 filas (logo arriba, emisor abajo)
            left_block_rows = []
            left_block_rows.append([logo_img if logo_img else Paragraph("", p_base)])
            left_block_rows.append([emisor_box])
            left_block = Table(left_block_rows, colWidths=[left_w])
            left_block.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ]))

            # Columna derecha: caja FACTURA + serie
            right_box = Table(
                [
                    [Paragraph("FACTURA ELECTRÓNICA", h_factura)],
                    [Paragraph(datos.get("numero", ""), h_serie)],
                ],
                colWidths=[right_w],
            )
            right_box.setStyle(TableStyle([
                ('BOX', (0, 0), (-1, -1), 0.8, gray_line),
                ('LEFTPADDING', (0, 0), (-1, -1), 8),
                ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ]))

            # Encabezado final 70/30
            header_table = Table([[left_block, right_box]], colWidths=[left_w, right_w])
            header_table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]))
            story.append(header_table)
            story.append(Spacer(1, 8))


            # ===== Bloque datos cliente / comprobante (2 columnas 50/50) =====
            receptor = datos.get('receptor', {})
            fecha = datos.get('fecha_emision', '')
            hora = datos.get('hora_emision', '')

            # Formateo fecha/hora
            try:
                fecha_display = datetime.strptime(fecha, '%Y-%m-%d').strftime('%d/%m/%Y') if fecha else ''
            except Exception:
                fecha_display = fecha
            try:
                hora_display = datetime.strptime(hora[:8], '%H:%M:%S').strftime('%I:%M %p') if hora else ''
            except Exception:
                hora_display = hora

            # Columnas
            col_izq = [
                [Paragraph("<b>Cliente:</b>", p_muted), Paragraph(receptor.get('razon_social', '') or "—", p_base)],
                [Paragraph("<b>RUC/DNI:</b>", p_muted), Paragraph(receptor.get('numero_documento', '') or "—", p_base)],
                [Paragraph("<b>Dirección:</b>", p_muted), Paragraph(receptor.get('direccion', '') or "", p_base)],
            ]
            col_der = [
                [Paragraph("<b>Fecha de emisión:</b>", p_muted), Paragraph(fecha_display or "—", p_base)],
                [Paragraph("<b>Fecha de vencimiento:</b>", p_muted), Paragraph(fecha_display or "—", p_base)],  # ajusta si tienes otro campo
                [Paragraph("<b>Condición de venta:</b>", p_muted), Paragraph("Inmediata", p_base)],
                [Paragraph("<b>Forma de pago:</b>", p_muted), Paragraph("Contado", p_base)],
            ]

            def tabla_kv(rows):
                t = Table(rows, colWidths=[38 * mm, (page_width - left_margin - right_margin) * 0.5 - 38 * mm])
                t.setStyle(TableStyle([
                    ('LINEBELOW', (0, 0), (-1, -1), 0.4, gray_light),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('TOPPADDING', (0, 0), (-1, -1), 2),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
                ]))
                return t

            datos_table = Table(
                [[tabla_kv(col_izq), tabla_kv(col_der)]],
                colWidths=[(page_width - left_margin - right_margin) * 0.5,
                           (page_width - left_margin - right_margin) * 0.5],
            )
            story.append(datos_table)
            story.append(Spacer(1, 10))

            # ===== Tabla de ítems =====
            items = datos.get('items', [])
            if items:
                # Cabecera
                headers = ["CANT.", "UNIDAD", "DESCRIPCIÓN", "P. UNIT", "DTO.", "TOTAL"]
                table_width = (page_width - left_margin - right_margin)
                col_widths = [
                    0.08 * table_width,   # CANT
                    0.10 * table_width,   # UNIDAD
                    0.52 * table_width,   # DESCRIPCIÓN
                    0.12 * table_width,   # P. UNIT
                    0.08 * table_width,   # DTO.
                    0.10 * table_width,   # TOTAL
                ]

                thead = Table([headers], colWidths=col_widths)
                thead.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), gray_fill),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, 0), 11),
                    ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
                    ('TOPPADDING', (0, 0), (-1, 0), 4),
                    ('BOTTOMPADDING', (0, 0), (-1, 0), 4),
                    ('LINEBELOW', (0, 0), (-1, 0), 0.6, gray_line),
                ]))
                story.append(thead)

                rows = []
                for item in items:
                    desc = Paragraph(item.get('descripcion', ''), ParagraphStyle('desc', fontName='Helvetica', fontSize=10, leading=12))
                    cantidad = f"{Decimal(item.get('cantidad','0') or '0'):.2f}"
                    punit = f"{Decimal(item.get('precio_unitario','0') or '0'):.2f}"
                    subtotal = f"{Decimal(item.get('subtotal','0') or '0'):.2f}"
                    rows.append([
                        cantidad,
                        item.get('unidad_medida','') or '',
                        desc,
                        punit,
                        "0.00",   # si tienes descuento por ítem, cámbialo aquí
                        subtotal,
                    ])

                tbody = Table(rows, colWidths=col_widths, repeatRows=0)
                tbody.setStyle(TableStyle([
                    ('FONTSIZE', (0, 0), (-1, -1), 10),
                    ('ALIGN', (0, 0), (0, -1), 'RIGHT'),     # CANT.
                    ('ALIGN', (3, 0), (-1, -1), 'RIGHT'),    # numéricos
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('LINEBELOW', (0, 0), (-1, -1), 0.25, gray_light),
                    ('TOPPADDING', (0, 0), (-1, -1), 3),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                ]))
                story.append(tbody)
                story.append(Spacer(1, 8))

            # ===== SON (monto en letras) a la izquierda =====
            son_txt = datos.get('leyenda_son', '')
            if son_txt:
                son_txt = son_txt.replace('SON', '').strip()
            story.append(Paragraph(f"<b>Son:</b> {son_txt or ''}", p_base))
            story.append(Spacer(1, 6))

            # ===== Totales a la derecha =====
            totales = datos.get('totales', {})
            gravado = f"{Decimal(totales.get('subtotal','0') or '0'):.2f}"
            exonerado = "0.00"  # si manejas, colócalo desde XML
            inafecto  = "0.00"
            descuentos = "0.00"  # globales si aplica
            igv = f"{Decimal(totales.get('igv','0') or '0'):.2f}"
            total_str = f"{Decimal(totales.get('total','0') or '0'):.2f}"

            tot_rows = [
                [Paragraph("OP. GRAVADAS:", p_base), Paragraph(f"S/ {gravado}", p_right)],
                [Paragraph("OP. EXONERADAS:", p_base), Paragraph(f"S/ {exonerado}", p_right)],
                [Paragraph("OP. INAFECTAS:", p_base), Paragraph(f"S/ {inafecto}", p_right)],
                [Paragraph("DESCUENTOS:", p_base), Paragraph(f"S/ {descuentos}", p_right)],
                [Paragraph("IGV (18%):", p_base), Paragraph(f"S/ {igv}", p_right)],
                [Paragraph("<b>TOTAL A PAGAR:</b>", p_bold), Paragraph(f"<b>S/ {total_str}</b>", p_right_bold)],
            ]
            tot_table_width = (page_width - left_margin - right_margin) * 0.48
            tot_tbl = Table(tot_rows, colWidths=[tot_table_width - 40 * mm, 40 * mm], hAlign='RIGHT')
            tot_tbl.setStyle(TableStyle([
                ('LINEABOVE', (0, 0), (-1, 0), 0.6, gray_line),
                ('LINEBELOW', (0, 4), (-1, 4), 0.4, gray_line),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]))
            story.append(tot_tbl)
            story.append(Spacer(1, 10))

            # ===== QR y validación SUNAT (abajo) =====
            cadena_qr = datos.get('cadena_qr', '')
            if cadena_qr and cadena_qr.strip():
                try:
                    qr_buf = io.BytesIO()
                    qrcode.make(cadena_qr).save(qr_buf, format="PNG")
                    qr_buf.seek(0)
                    qr_img = Image(qr_buf, width=38 * mm, height=38 * mm)
                    qr_img.hAlign = 'LEFT'
                    story.append(qr_img)
                except Exception as e:
                    print(f"Error generando QR: {e}")

            estado_sunat = datos.get('estado_sunat', 'Estado desconocido')
            story.append(Spacer(1, 6))
            story.append(Paragraph("<b>ACEPTADO POR SUNAT</b>" if "ACEPTADO" in estado_sunat else estado_sunat, p_base))

            hash_cdr = datos.get('hash_cdr', '')
            if hash_cdr:
                story.append(Paragraph(f"Código Hash: {hash_cdr}", p_small))

            fecha_respuesta = datos.get('fecha_respuesta_sunat', '')
            if fecha_respuesta:
                story.append(Paragraph(f"Fecha respuesta SUNAT: {fecha_respuesta}", p_small))

            story.append(Spacer(1, 8))
            story.append(Paragraph("Representación impresa de la FACTURA ELECTRÓNICA", ParagraphStyle('foot', parent=p_small, alignment=TA_CENTER, textColor=gray_head)))

            # Build
            doc.build(story)
            buffer.seek(0)
            return buffer.getvalue()

        # ============= RUTA DE BOLETA (SIN CAMBIOS RELEVANTES) =============
        ticket_width = 80 * mm
        page_width, page_height = ticket_width, None  # altura dinámica
        left_margin = 4 * mm
        right_margin = 4 * mm
        top_margin = 6 * mm
        bottom_margin = 6 * mm
        frame_width = ticket_width - left_margin - right_margin
        story = []

        # Estilos boleta (compacto)
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=9.0, alignment=TA_CENTER, spaceAfter=3, leading=10)
        subtitle_style = ParagraphStyle('CustomSubtitle', parent=styles['Heading2'], fontSize=9.0, alignment=TA_CENTER, spaceAfter=3, leading=10)
        header_style = ParagraphStyle('Header', parent=styles['Normal'], fontSize=6.8, alignment=TA_CENTER, spaceAfter=2, leading=8.0)
        normal_style = ParagraphStyle('Normal', parent=styles['Normal'], fontSize=6.6, spaceAfter=1.5, leading=7.8)
        error_style = ParagraphStyle('Error', parent=styles['Normal'], fontSize=12, alignment=TA_CENTER, textColor=colors.red, spaceAfter=10)

        if not datos.get("numero") or datos.get("estado_sunat") == "Error procesando datos":
            story.append(Paragraph("BOLETA ELECTRÓNICA", title_style))
            story.append(Paragraph("ERROR", error_style))
            story.append(Paragraph("Error procesando datos del comprobante", error_style))
            story.append(Spacer(1, 20))
            emisor = datos.get('emisor', {})
            if emisor.get('razon_social'):
                story.append(Paragraph(f"<b>{emisor['razon_social']}</b>", header_style))
            if emisor.get('ruc'):
                story.append(Paragraph(f"RUC: {emisor['ruc']}", normal_style))
            story.append(Paragraph(f"FECHA: {datos.get('fecha_emision', '')} {datos.get('hora_emision', '')}", normal_style))
            story.append(Paragraph("TOTALES:", normal_style))
            story.append(Paragraph(f"Subtotal: S/ {datos.get('totales', {}).get('subtotal', '0.00')}", normal_style))
            story.append(Paragraph(f"IGV (18%): S/ {datos.get('totales', {}).get('igv', '0.00')}", normal_style))
            story.append(Paragraph(f"TOTAL: S/ {datos.get('totales', {}).get('total', '0.00')}", normal_style))
            story.append(Paragraph("Error procesando datos", error_style))
        else:
            # Logo de la sede (opcional)
            ruta = resolver_logo(datos.get('logo_path'))
            if ruta:
                try:
                    story.append(Image(ruta, width=18 * mm, height=18 * mm))
                except Exception:
                    pass

            story.append(Paragraph(datos.get("tipo_documento", "BOLETA ELECTRÓNICA"), title_style))
            story.append(Paragraph(datos.get("numero", ""), subtitle_style))

            emisor = datos.get('emisor', {})
            if emisor.get('razon_social'):
                story.append(Paragraph(f"<b>{emisor['razon_social']}</b>", header_style))
            if emisor.get('ruc'):
                story.append(Paragraph(f"RUC: {emisor['ruc']}", header_style))
            if emisor.get('direccion'):
                story.append(Paragraph(emisor['direccion'], header_style))

            story.append(Spacer(1, 2))
            story.append(Paragraph("""<para alignment='center'>--------------------------------</para>""", normal_style))

            receptor = datos.get('receptor', {})
            story.append(Paragraph(f"{receptor.get('razon_social','')}", header_style))
            story.append(Paragraph("—", header_style))
            if receptor.get('numero_documento'):
                story.append(Paragraph(f"DNI {receptor.get('numero_documento')}", header_style))

            fecha = datos.get('fecha_emision','')
            hora = datos.get('hora_emision','')
            try:
                hora_display = datetime.strptime(hora[:8], '%H:%M:%S').strftime('%I:%M %p') if hora else ''
            except Exception:
                hora_display = hora
            try:
                fecha_display = datetime.strptime(fecha, '%Y-%m-%d').strftime('%d/%m/%Y') if fecha else ''
            except Exception:
                fecha_display = fecha
            story.append(Paragraph(f"FECHA: {fecha_display}    HORA: {hora_display}", ParagraphStyle('n', parent=normal_style, alignment=TA_LEFT)))
            story.append(Paragraph("""<para alignment='center'>--------------------------------</para>""", normal_style))

            items = datos.get('items', [])
            if items:
                col_widths = [10*mm, 12*mm, frame_width - (10*mm + 12*mm + 16*mm + 16*mm), 16*mm, 16*mm]
                header_tbl = Table([["Cant", "U.M", "Descripción", "P. Unit", "Importe"]], colWidths=col_widths)
                header_tbl.setStyle(TableStyle([
                    ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0,0), (-1,0), 6.5),
                    ('ALIGN', (0,0), (-1,0), 'CENTER'),
                    ('TOPPADDING', (0,0), (-1,0), 1),
                    ('BOTTOMPADDING', (0,0), (-1,0), 1),
                    ('LINEBELOW', (0,0), (-1,0), 0.5, colors.black),
                ]))
                story.append(header_tbl)

                rows = []
                for item in items:
                    rows.append([
                        str(item.get('cantidad','')),
                        item.get('unidad_medida',''),
                        Paragraph(item.get('descripcion',''), ParagraphStyle('desc', fontSize=6.6, leading=7.8)),
                        f"{Decimal(item.get('precio_unitario','0') or '0'):.2f}",
                        f"{Decimal(item.get('subtotal','0') or '0'):.2f}",
                    ])

                items_tbl = Table(rows, colWidths=col_widths, repeatRows=0)
                items_tbl.setStyle(TableStyle([
                    ('FONTSIZE', (0,0), (-1,-1), 6.6),
                    ('ALIGN', (0,0), (0,-1), 'RIGHT'),
                    ('ALIGN', (3,0), (-1,-1), 'RIGHT'),
                    ('TOPPADDING', (0,0), (-1,-1), 1),
                    ('BOTTOMPADDING', (0,0), (-1,-1), 1),
                    ('LINEBELOW', (0,0), (-1,-1), 0.25, colors.lightgrey),
                ]))
                story.append(items_tbl)
                story.append(Spacer(1, 3))

            totales = datos.get('totales', {})
            gravado = f"{Decimal(totales.get('subtotal','0') or '0'):.2f}"
            igv = f"{Decimal(totales.get('igv','0') or '0'):.2f}"
            total_str = f"{Decimal(totales.get('total','0') or '0'):.2f}"

            tot_tbl = Table([
                [Paragraph("TOTAL GRAVADO", ParagraphStyle('lab', fontSize=7.6)),
                 Paragraph(f"(S/) {gravado}", ParagraphStyle('val', alignment=TA_RIGHT, fontSize=7.6))],
                [Paragraph("I.G.V (18%)", ParagraphStyle('lab', fontSize=7.6)),
                 Paragraph(f"(S/) {igv}", ParagraphStyle('val', alignment=TA_RIGHT, fontSize=7.6))],
                [Paragraph("<b>TOTAL</b>", ParagraphStyle('lab_b', fontSize=10)),
                 Paragraph(f"<b>(S/) {total_str}</b>", ParagraphStyle('val_b', alignment=TA_RIGHT, fontSize=10))],
            ], colWidths=[frame_width-22*mm, 22*mm])
            tot_tbl.setStyle(TableStyle([
                ('LINEABOVE', (0,0), (-1,0), 0.5, colors.black),
                ('LINEBELOW', (0,1), (-1,1), 0.25, colors.black),
                ('TOPPADDING', (0,0), (-1,-1), 1),
                ('BOTTOMPADDING', (0,0), (-1,-1), 1),
            ]))
            story.append(tot_tbl)
            story.append(Spacer(1, 3))

            son = datos.get('leyenda_son', '')
            if son:
                story.append(Paragraph(f"SON: {son.replace('SON', '').strip()}", normal_style))
            else:
                story.append(Paragraph("SON: ", normal_style))
            story.append(Paragraph("FORMA DE PAGO: EFECTIVO", normal_style))
            story.append(Paragraph("COND.VENTA: CONTADO", normal_style))
            story.append(Paragraph("Observaciones:", normal_style))
            story.append(Spacer(1, 4))

            cadena_qr = datos.get('cadena_qr', '')
            if cadena_qr and cadena_qr.strip():
                try:
                    qr_buf = io.BytesIO()
                    qrcode.make(cadena_qr).save(qr_buf, format="PNG")
                    qr_buf.seek(0)
                    qr_img = Image(qr_buf, width=24*mm, height=24*mm)
                    story.append(qr_img)
                    story.append(Spacer(1, 4))
                except Exception as e:
                    print(f"Error generando QR: {e}")

            estado_sunat = datos.get('estado_sunat', 'Estado desconocido')
            story.append(Paragraph(f"<b>{estado_sunat}</b>", header_style))
            hash_cdr = datos.get('hash_cdr', '')
            if hash_cdr:
                story.append(Paragraph(f"Hash CDR: {hash_cdr}", normal_style))
            fecha_respuesta = datos.get('fecha_respuesta_sunat', '')
            if fecha_respuesta:
                story.append(Paragraph(f"Fecha respuesta SUNAT: {fecha_respuesta}", normal_style))

            story.append(Spacer(1, 4))
            story.append(Paragraph("Representación Impresa de la BOLETA DE VENTA ELECTRÓNICA", header_style))

        # ==== Construcción ticket altura dinámica ====
        if not es_factura:
            total_height = top_margin + bottom_margin
            for flow in story:
                try:
                    _, h = flow.wrap(frame_width, 0)
                except Exception:
                    h = 10
                try:
                    h += getattr(flow, 'getSpaceBefore', lambda: 0)()
                except Exception:
                    pass
                try:
                    h += getattr(flow, 'getSpaceAfter', lambda: 0)()
                except Exception:
                    pass
                total_height += h
            total_height += 6 * mm
            if total_height < 100 * mm:
                total_height = 100 * mm

            doc = SimpleDocTemplate(
                buffer,
                pagesize=(page_width, total_height),
                leftMargin=left_margin,
                rightMargin=right_margin,
                topMargin=top_margin,
                bottomMargin=bottom_margin,
            )
            doc.build(story)
            buffer.seek(0)
            return buffer.getvalue()

    except Exception as e:
        print(f"Error generando PDF: {e}")
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4)
        story = []
        styles = getSampleStyleSheet()
        error_style = ParagraphStyle('Error', parent=styles['Normal'], fontSize=14, alignment=TA_CENTER, textColor=colors.red)
        story.append(Paragraph("BOLETA ELECTRÓNICA", error_style))
        story.append(Paragraph("ERROR", error_style))
        story.append(Paragraph("Error generando representación impresa", error_style))
        story.append(Spacer(1, 20))
        story.append(Paragraph(f"Error: {str(e)}", styles['Normal']))
        doc.build(story)
        buffer.seek(0)
        return buffer.getvalue()
