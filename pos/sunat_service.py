"""
Servicio para integración con SUNAT - Facturación Electrónica Peruana (UBL 2.1)
Cumple la guía de SUNAT para Boletas/Facturas:
  1. Firma digital XAdES-BES dentro de UBLExtensions.
  2. Inserta bloque <cac:Signature> externo.
  3. Agrega nodos UBL obligatorios.
  4. Comprime (ZIP) y codifica (Base-64) antes de enviar.
  5. Envía vía SOAP con WS-Security.
"""
from __future__ import annotations

import base64
import logging
import os
import zipfile
from datetime import datetime
import traceback
from django.utils import timezone
from decimal import Decimal
from io import BytesIO
from typing import Tuple

import requests
from django.conf import settings
from lxml import etree
try:
    from signxml import XMLSigner, methods
except Exception as _signxml_exc:  # pragma: no cover - import-time guard
    XMLSigner = None  # type: ignore
    methods = None  # type: ignore
    _SIGNXML_IMPORT_ERROR = _signxml_exc
else:
    _SIGNXML_IMPORT_ERROR = None


def _ensure_signxml():
    """Reintenta importar signxml y entrega detalles útiles si falla."""
    global XMLSigner, methods, _SIGNXML_IMPORT_ERROR
    if XMLSigner is not None and methods is not None:
        return XMLSigner, methods
    try:
        from signxml import XMLSigner as _XMLSigner, methods as _methods
        XMLSigner, methods = _XMLSigner, _methods
        _SIGNXML_IMPORT_ERROR = None
        return XMLSigner, methods
    except Exception as exc:
        _SIGNXML_IMPORT_ERROR = exc
        import sys
        detail = (
            "No se pudo importar 'signxml'. "
            "Verifique que el servidor esté usando el mismo Python/venv.\n"
            f"python: {sys.executable}\n"
            f"error: {exc}"
        )
        raise ImportError(detail) from exc
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    pkcs12,
)

logger = logging.getLogger(__name__)

# Namespaces usados por UBL 2.1 Perú
NSMAP = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "ext": "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
}


class SUNATService:
    """Servicio para manejar la integración con SUNAT"""
    
    def __init__(self, configuracion_sunat):
        self.config = configuracion_sunat
        self.ambiente = configuracion_sunat.ambiente_sunat
        self.url_base = self._get_url_base()
    
    # ---------------------------------------------------------------------
    # API público ----------------------------------------------------------
    # ---------------------------------------------------------------------
    def enviar_comprobante(self, comprobante):
        """Genera, firma, comprime y envía un comprobante a SUNAT"""
        try:
            # 1) Generar XML base desde el comprobante
            xml_generado = comprobante.generar_xml()
            print(f"📄 XML base generado: {len(xml_generado)} caracteres")
            
            # 2) Firmar e insertar nodos según guía
            xml_firmado = self._firmar_xml(xml_generado, comprobante)
            print(f"🖋️  XML firmado listo: {len(xml_firmado)} caracteres")
            
            # 3) Guardar XML firmado en el comprobante
            comprobante.xml_firmado = xml_firmado
            comprobante.save(update_fields=["xml_firmado"])
            print(f"💾 XML firmado guardado en la base de datos")
            
            # 4) Enviar a SUNAT
            respuesta = self._enviar_a_sunat(xml_firmado, comprobante)
            
            # 5) Procesar respuesta
            if not self._procesar_respuesta(comprobante, respuesta):
                # Si fue una falla transitoria ya se encoló. Mensaje amable para UI.
                if self._is_transient_fault(respuesta):
                    try:
                        comprobante.respuesta_sunat = (comprobante.respuesta_sunat or '') + "\nSUNAT no disponible. Se reintentará automáticamente."
                        comprobante.save(update_fields=["respuesta_sunat"])
                    except Exception:
                        pass
                return False
            return True
            
        except Exception as exc:
            print("❌ ERROR ENVIANDO COMPROBANTE A SUNAT:")
            print(traceback.format_exc())
            logger.exception("Error enviando comprobante a SUNAT: %s", exc)

            # Si es una falla transitoria (sin internet, DNS, timeout, etc.), encolar reintento
            err_text = str(exc) or ""
            if self._is_transient_fault(err_text):
                try:
                    # Encolar usando el último ZIP preparado
                    self._queue_retry(comprobante, err_text)
                    # Marcar como ENVIADO en espera/reintento, y dejar mensaje amable
                    comprobante.estado_sunat = "ENVIADO"
                    append_msg = "SUNAT no disponible. Se reintentará automáticamente."
                    comprobante.respuesta_sunat = ((comprobante.respuesta_sunat or "") + f"\n{append_msg}\n{err_text}").strip()
                    comprobante.save(update_fields=["estado_sunat", "respuesta_sunat"])
                except Exception:
                    # Si falla el encolado, mantener estado como ENVIADO con el error
                    comprobante.estado_sunat = "ENVIADO"
                    comprobante.respuesta_sunat = ((comprobante.respuesta_sunat or "") + f"\n{err_text}").strip()
                    comprobante.save(update_fields=["estado_sunat", "respuesta_sunat"])
                return False

            # Si no es transitorio, marcar rechazado
            comprobante.estado_sunat = "RECHAZADO"
            comprobante.respuesta_sunat = err_text
            comprobante.save(update_fields=["estado_sunat", "respuesta_sunat"])
            return False

    def generar_resumen_diario_baja(self, boletas):
        """
        Genera y envía un Resumen Diario (RC) marcando como BAJA las boletas indicadas.
        Devuelve el ticket de SUNAT para su posterior consulta con getStatus.

        Notas SUNAT:
        - Para boletas (tipo 03) la baja se comunica mediante SummaryDocuments (RC) usando
          <cac:Status><cbc:ConditionCode>3</cbc:ConditionCode></cac:Status> en cada línea.
        - El servicio SOAP es sendSummary y retorna un ticket.
        """
        try:
            boletas = [b for b in boletas if getattr(b, 'tipo_comprobante', '') == '03']
            if not boletas:
                raise ValueError("Debe proporcionar al menos una boleta (tipo 03) para baja")

            # 1) Construir XML RC (mismo día) o RA (fecha anterior)
            from django.utils.timezone import localtime
            emision_dt = getattr(boletas[0], 'fecha_emision', None)
            if emision_dt is not None:
                try:
                    ref_date = localtime(emision_dt).date()
                except Exception:
                    ref_date = timezone.localdate()
            else:
                ref_date = timezone.localdate()

            # Para boletas (03), la baja SIEMPRE es por Resumen Diario (RC)
            xml_resumen, resumen_id, sede = self._crear_xml_resumen_baja_boletas(boletas)

            # 2) Firmar RC
            xml_firmado = self._firmar_xml_resumen(xml_resumen)
            # 3) ZIP + B64 y SOAP sendSummary
            content_b64, zip_name = self._zip_and_encode_summary(xml_firmado, resumen_id)
            soap = self._crear_soap_envelope_summary(content_b64, zip_name)

            endpoint = self.url_base
            headers = {
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": '"urn:sendSummary"',
            }
            print("🚀 Enviando Resumen Diario (BAJA) a SUNAT…")
            print(f"   Archivo: {zip_name}")
            resp = requests.post(endpoint, data=soap.encode('utf-8'), headers=headers, timeout=60)
            print(f"📥 SUNAT respondió HTTP {resp.status_code}")

            # 4) Extraer ticket del XML SOAP
            ticket = self._parse_ticket_from_sendSummary(resp.text)
            if not ticket:
                print("[DEBUG] Respuesta sendSummary sin ticket (primeros 2000 chars):\n" + resp.text[:2000])
                raise ValueError("No se pudo obtener ticket de SUNAT en sendSummary")
            print(f"🎫 Ticket recibido: {ticket}")
            # Guardar ticket en tabla de control si existe
            try:
                from .models import ResumenDiario  # type: ignore
                rd = ResumenDiario.objects.filter(resumen_id=resumen_id).first()
                if rd:
                    rd.ticket = ticket
                    rd.estado = 'ENVIADO'
                    rd.save(update_fields=['ticket','estado'])
            except Exception:
                pass
            return ticket
        except Exception as exc:
            print("❌ Error generando/enviando RC de baja:")
            print(traceback.format_exc())
            logger.exception("Error RC baja: %s", exc)
            raise
    
    # ------------------------------------------------------------------
    # FASE 1 – firma y preparación del XML ----------------------------
    # ------------------------------------------------------------------
    def _firmar_xml(self, xml_string: str, comprobante) -> str:
        """Aplica firma XAdES-BES al documento completo según SUNAT."""
        print("🔑 Iniciando firma del XML…")
        _ensure_signxml()
        
        # ---------- Obtener certificado (.pfx) ----------
        pfx_field = self.config.certificado_digital
        pfx_path = pfx_field.path if pfx_field else None
        if not pfx_path or not os.path.exists(pfx_path):
            raise ValueError(
                f"No se encontró el certificado digital en la ruta indicada: {pfx_path}"
            )

        pfx_pass = (
            os.getenv("PFX_PASS") or (self.config.password_certificado or "")
        ).encode()

        with open(pfx_path, "rb") as fh:
            pfx_data = fh.read()
        private_key, certificate, _ = pkcs12.load_key_and_certificates(pfx_data, pfx_pass)
        if private_key is None or certificate is None:
            raise ValueError("El archivo .pfx no contiene llave privada/certificado válidos")

        key_pem = private_key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=NoEncryption(),
        )
        cert_pem = certificate.public_bytes(Encoding.PEM)
        print(f"   Certificado leído: {len(cert_pem)} bytes – PFX: {os.path.basename(pfx_path)}")

        # a) Parsear XML base
        xml_doc = etree.fromstring(xml_string.encode("utf-8"))

        # b) AÑADIR NODOS OBLIGATORIOS *ANTES* DE FIRMAR
        self._ensure_ubl_required_nodes(xml_doc, comprobante)
        
        # c) INSERTAR cac:Signature DESPUÉS de los nodos cbc obligatorios
        self._insert_cac_signature(xml_doc, comprobante)
        
        # d) FIRMAR el documento completo (Invoice) con URI="" y enveloped-signature
        signer = XMLSigner(
            method=methods.enveloped,
            signature_algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
            digest_algorithm="http://www.w3.org/2001/04/xmlenc#sha256",
            c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
        )
        
        # Firmar el documento completo (Invoice)
        signed_doc = signer.sign(xml_doc, key=key_pem, cert=cert_pem)
        
        # DEBUG: Imprimir XML firmado antes de comprimir y enviar
        print("\n===== XML FIRMADO QUE SE ENVÍA A SUNAT =====")
        print(etree.tostring(signed_doc, pretty_print=True, encoding="unicode"))
        print("============================================\n")
        
        # e) Establecer Id="signatureKG" y mover la firma a ext:ExtensionContent
        signature_el = signed_doc.find(".//ds:Signature", namespaces=NSMAP)
        if signature_el is None:
            raise ValueError("No se encontró elemento ds:Signature tras la firma")
        signature_el.attrib["Id"] = "signatureKG"
        
        # f) Mover la firma a ext:ExtensionContent
        ext_content = signed_doc.find(".//ext:ExtensionContent", namespaces=NSMAP)
        if ext_content is None:
            raise ValueError("No se encontró ext:ExtensionContent en el XML")
        
        # Remover la firma de su posición actual y moverla a ext:ExtensionContent
        signature_el.getparent().remove(signature_el)
        ext_content.append(signature_el)

        result = etree.tostring(signed_doc, encoding="utf-8", xml_declaration=True, pretty_print=False).decode()
        
        print("   ✅ Firma XAdES-BES aplicada al documento completo")
        
        # Debug: Verificar estructura final del XML
        self._debug_xml_structure(signed_doc)
        
        return result

    # --------------------------- Helpers firma ---------------------------



    def _debug_xml_structure(self, root: etree._Element):
        """Debug: Verifica la estructura del XML antes del envío."""
        print("   🔍 Verificando estructura XML:")
        
        # Verificar nodos obligatorios - buscar directamente en el root para evitar confusión con cac:Signature
        required_nodes = [
            ("cbc:UBLVersionID", "2.1"),
            ("cbc:CustomizationID", "2.0"), 
            ("cbc:ProfileID", "0101"),
            ("cbc:ID", None),  # Se verificará específicamente
            ("cbc:IssueDate", None),  # Se verificará específicamente
            ("cbc:IssueTime", None),  # Se verificará específicamente
            ("cbc:DocumentCurrencyCode", "PEN")
        ]
        
        for node_name, expected_value in required_nodes:
            # Buscar directamente en los hijos del root para evitar cac:Signature
            node = None
            for child in root:
                if child.tag == etree.QName(NSMAP["cbc"], node_name.split(":")[1]):
                    node = child
                    break
            
            if node is not None:
                print(f"      ✓ {node_name}: {node.text}")
            else:
                print(f"      ❌ {node_name}: FALTANTE")
        
        # Verificar cac:Signature
        cac_sig = root.find(".//cac:Signature", namespaces=NSMAP)
        if cac_sig is not None:
            print(f"      ✓ cac:Signature: Presente")
        else:
            print(f"      ❌ cac:Signature: FALTANTE")
        
        # Verificar ds:Signature
        ds_sig = root.find(".//ds:Signature", namespaces=NSMAP)
        if ds_sig is not None:
            sig_id = ds_sig.get("Id", "SIN_ID")
            print(f"      ✓ ds:Signature: Presente (Id={sig_id})")
        else:
            print(f"      ❌ ds:Signature: FALTANTE")

    def _insert_cac_signature(self, root: etree._Element, comprobante):
        """Añade bloque <cac:Signature> referenciando la firma ANTES de firmar."""
        # Verificar si ya existe cac:Signature para no duplicar
        existing_signature = root.find(".//cac:Signature", namespaces=NSMAP)
        if existing_signature is not None:
            print("   ✓ Bloque cac:Signature ya existe")
            return
        
        root_localname = etree.QName(root.tag).localname

        cac_signature = etree.Element(etree.QName(NSMAP["cac"], "Signature"), nsmap=NSMAP)
        cbc_id = etree.SubElement(cac_signature, etree.QName(NSMAP["cbc"], "ID"))
        cbc_id.text = "IDSignKG"

        # SignatoryParty
        cac_signatory = etree.SubElement(cac_signature, etree.QName(NSMAP["cac"], "SignatoryParty"))
        cac_party_id = etree.SubElement(
            cac_signatory, etree.QName(NSMAP["cac"], "PartyIdentification")
        )
        cbc_pid = etree.SubElement(cac_party_id, etree.QName(NSMAP["cbc"], "ID"))
        cbc_pid.text = comprobante.emisor_ruc

        cac_party_name = etree.SubElement(cac_signatory, etree.QName(NSMAP["cac"], "PartyName"))
        cbc_name = etree.SubElement(cac_party_name, etree.QName(NSMAP["cbc"], "Name"))
        cbc_name.text = comprobante.emisor_razon_social

        # DigitalSignatureAttachment
        cac_digital_attach = etree.SubElement(
            cac_signature, etree.QName(NSMAP["cac"], "DigitalSignatureAttachment")
        )
        cac_ext_ref = etree.SubElement(
            cac_digital_attach, etree.QName(NSMAP["cac"], "ExternalReference")
        )
        cbc_uri = etree.SubElement(cac_ext_ref, etree.QName(NSMAP["cbc"], "URI"))
        cbc_uri.text = "#signatureKG"  # Debe coincidir con el ID de la firma

        # CreditNote/DebitNote: el XSD exige cac:Signature después de DiscrepancyResponse
        # y BillingReference, justo antes de cac:AccountingSupplierParty.
        if root_localname in ("CreditNote", "DebitNote"):
            for idx, child in enumerate(root):
                if child.tag == etree.QName(NSMAP["cac"], "AccountingSupplierParty"):
                    root.insert(idx, cac_signature)
                    print(f"   ➕ Bloque cac:Signature insertado en posición {idx} ({root_localname})")
                    return
            # Si no hay AccountingSupplierParty (no debería pasar), continuar con la lógica estándar

        # Insertar cac:Signature DESPUÉS de cbc:DocumentCurrencyCode
        # Orden correcto: ext:UBLExtensions, cbc:*, cac:Signature, cac:*
        insertion_index = 0

        # Buscar cbc:DocumentCurrencyCode para insertar cac:Signature después
        for idx, child in enumerate(root):
            if child.tag == etree.QName(NSMAP["cbc"], "DocumentCurrencyCode"):
                insertion_index = idx + 1
                break
        
        # Si no se encuentra DocumentCurrencyCode, buscar el último nodo cbc
        if insertion_index == 0:
            for idx, child in enumerate(root):
                if child.tag.startswith(NSMAP["cbc"]):
                    insertion_index = idx + 1
        
        # Si no hay nodos cbc, insertar después de UBLExtensions
        if insertion_index == 0:
            for idx, child in enumerate(root):
                if child.tag == etree.QName(NSMAP["ext"], "UBLExtensions"):
                    insertion_index = idx + 1
                    break
        
        root.insert(insertion_index, cac_signature)
        print(f"   ➕ Bloque cac:Signature insertado en posición {insertion_index}")

    def _ensure_ubl_required_nodes(self, root: etree._Element, comprobante):
        """Asegura la presencia y valores de nodos UBL obligatorios en el orden correcto."""
        required = [
            ("UBLVersionID", "2.1"),
            ("CustomizationID", "2.0"),
            ("ProfileID", "0101"),
            (
                "ID",
                f"{comprobante.serie}-{str(comprobante.numero).zfill(8)}",
            ),
            ("IssueDate", datetime.now().strftime("%Y-%m-%d")),
            ("IssueTime", datetime.now().strftime("%H:%M:%S")),
            ("DocumentCurrencyCode", "PEN"),
        ]

        # Insertar nodos cbc en el orden correcto después de UBLExtensions
        insertion_index = 0
        for idx, child in enumerate(root):
            if child.tag == etree.QName(NSMAP["ext"], "UBLExtensions"):
                insertion_index = idx + 1
                break
        
        # Solo agregar nodos que NO existan para no alterar el digest
        tipo_doc = getattr(comprobante, 'tipo_comprobante', '')
        for tag_name, value in required:
            # Las Notas de Crédito/Débito no llevan cbc:ProfileID
            if tag_name == 'ProfileID' and tipo_doc in ('07', '08'):
                continue
            existing = root.find(f"cbc:{tag_name}", namespaces=NSMAP)
            if existing is None:
                el = etree.Element(etree.QName(NSMAP["cbc"], tag_name), nsmap=NSMAP)
                el.text = value
                root.insert(insertion_index, el)
                insertion_index += 1
                print(f"   ➕ Agregado nodo obligatorio: {tag_name}")
            else:
                print(f"   ✓ Nodo ya existe: {tag_name} = {existing.text}")

    # Eliminar _insert_signature_in_extension_content

    # ------------------------------------------------------------------
    # FASE 2 – ZIP + Base64 + SOAP ------------------------------------
    # ------------------------------------------------------------------
    def _zip_and_encode(self, xml_firmado: str, comprobante) -> Tuple[str, str]:
        """Devuelve contenido Base-64 del ZIP y nombre de archivo."""
        # Formato requerido por SUNAT: RUC-TIPO_SERIE-NUMERO.zip
        # Ejemplo: 20610391550-03-B001-00000008.zip
        zip_name = (
            f"{comprobante.emisor_ruc}-{comprobante.tipo_comprobante}-"
            f"{comprobante.serie}-{comprobante.numero:08d}.zip"
        )
        xml_name = zip_name.replace(".zip", ".xml")

        # Debug: Imprimir nombre de archivo y hash SHA1 del XML firmado
        import hashlib
        xml_bytes = xml_firmado.encode("utf-8")
        sha1_hash = hashlib.sha1(xml_bytes).hexdigest()
        print(f"[DEBUG] Nombre XML en ZIP: {xml_name}")
        print(f"[DEBUG] SHA1 XML firmado: {sha1_hash}")

        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr(xml_name, xml_bytes)
        encoded_content = base64.b64encode(buffer.getvalue()).decode()
        print(f"📦 ZIP generado: {zip_name} – tamaño Base64: {len(encoded_content)} caracteres")
        # Guardar último contexto de ZIP para reintentos (auxiliar; no cambia el flujo de envío)
        try:
            self._last_zip_b64 = encoded_content
            self._last_zip_name = zip_name
            self._last_xml_sha1 = sha1_hash
            # cbc:ID del comprobante
            self._last_cbc_id = f"{getattr(comprobante, 'serie', '')}-{int(getattr(comprobante, 'numero', 0)):08d}"
            self._last_tipo = getattr(comprobante, 'tipo_comprobante', '')
            self._last_emisor_ruc = getattr(comprobante, 'emisor_ruc', '')
        except Exception:
            pass
        return encoded_content, zip_name

    def _crear_soap_envelope(self, content_b64: str, file_name: str) -> str:
        """Crea envelope SOAP con WS-Security y contenido Base64."""
        # Obtener credenciales de la configuración
        ruc = self.config.emisor_ruc or ""
        usuario_sol = self.config.usuario_sunat or ""
        password_sol = self.config.password_sunat or ""
        
        # SUNAT concatena RUC + UsuarioSOL para el campo Username
        username = f"{ruc}{usuario_sol}"
        
        print(f"🔐 Usando credenciales WS-Security:")
        print(f"   Username: {username}")
        print(f"   Password: {'*' * len(password_sol) if password_sol else 'NO CONFIGURADO'}")

        soap_template = f"""<?xml version="1.0" encoding="UTF-8"?>
        <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ser="http://service.sunat.gob.pe" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
            <soapenv:Header>
                <wsse:Security>
                    <wsse:UsernameToken>
                        <wsse:Username>{username}</wsse:Username>
                        <wsse:Password>{password_sol}</wsse:Password>
                    </wsse:UsernameToken>
                </wsse:Security>
            </soapenv:Header>
            <soapenv:Body>
                <ser:sendBill>
                    <fileName>{file_name}</fileName>
                    <contentFile>{content_b64}</contentFile>
                </ser:sendBill>
            </soapenv:Body>
        </soapenv:Envelope>"""
        return soap_template

    def consultar_ticket_resumen(self, ticket: str) -> Tuple[str, str]:
        """Consulta el estado de un ticket (RC/RA) vía getStatus. Retorna (codigo_respuesta, cdr_xml)."""
        import requests
        from lxml import etree

        # Credenciales WS-Security
        ruc = (self.config.emisor_ruc or "").strip()
        usuario_sol = (self.config.usuario_sunat or "").strip()
        password_sol = (self.config.password_sunat or "").strip()
        username = f"{ruc}{usuario_sol}"

        soap = f"""<?xml version="1.0" encoding="UTF-8"?>
            <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ser="http://service.sunat.gob.pe" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
            <soapenv:Header>
                <wsse:Security>
                <wsse:UsernameToken>
                    <wsse:Username>{username}</wsse:Username>
                    <wsse:Password>{password_sol}</wsse:Password>
                </wsse:UsernameToken>
                </wsse:Security>
            </soapenv:Header>
            <soapenv:Body>
                <ser:getStatus>
                <ticket>{ticket}</ticket>
                </ser:getStatus>
            </soapenv:Body>
            </soapenv:Envelope>"""

        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '"urn:getStatus"'}
        endpoint = self.url_base
        resp = requests.post(endpoint, data=soap.encode('utf-8'), headers=headers, timeout=60)
        print(f"📥 getStatus HTTP {resp.status_code}")
        try:
            print("[DEBUG] getStatus body (first 1200 chars):\n" + (resp.text or '')[:1200])
        except Exception:
            pass

        try:
            root = etree.fromstring((resp.text or '').encode('utf-8'))

            # 1) statusCode (para RC/RA): 0=aceptado, 98=en proceso, otros=error
            status_nodes = root.xpath('//*[local-name()="statusCode"]')
            status_code = (status_nodes[0].text or '').strip() if status_nodes else ''

            # 2) applicationResponse (CDR) si disponible
            app_nodes = root.xpath('//*[local-name()="applicationResponse"]')
            cdr_xml = ""
            code = ""

            def _pick_code_from_cdr(xml_text: str) -> str:
                """Extrae ResponseCode del CDR con namespaces o por fallback."""
                from lxml import etree as ET
                try:
                    cdr_root = ET.fromstring(xml_text.encode('utf-8'))
                except Exception:
                    return ""
                # Intento con namespace conocido
                code_elem = cdr_root.find('.//cbc:ResponseCode', namespaces=NSMAP)
                if code_elem is None:
                    elems = cdr_root.xpath('//*[local-name()="ResponseCode"]')
                    if elems:
                        code_elem = elems[0]
                if code_elem is not None and (code_elem.text or '').strip():
                    return code_elem.text.strip()
                # Fallback regex
                try:
                    import re as _re
                    m = _re.search(r'<(?:[^:>]*:)?ResponseCode[^>]*>\s*(\d{1,4})\s*<', xml_text)
                    if m:
                        return m.group(1)
                except Exception:
                    pass
                return ""

            def _pick_desc_from_cdr(xml_text: str) -> str:
                """Saca Description para logs (útil cuando code != 0)."""
                from lxml import etree as ET
                try:
                    cdr_root = ET.fromstring(xml_text.encode('utf-8'))
                    nodes = cdr_root.xpath('//*[local-name()="Description"]')
                    return (nodes[0].text or '').strip() if nodes else ""
                except Exception:
                    return ""

            if app_nodes and (app_nodes[0].text or '').strip():
                app_b64 = app_nodes[0].text.strip()
                cdr_xml = self._decodificar_cdr(app_b64)
                if cdr_xml:
                    code = _pick_code_from_cdr(cdr_xml)

            # 2b) Algunos endpoints exponen el ZIP en 'content'
            if not cdr_xml:
                content_nodes = root.xpath('//*[local-name()="content"]')
                if content_nodes and (content_nodes[0].text or '').strip():
                    app_b64 = content_nodes[0].text.strip()
                    cdr_xml = self._decodificar_cdr(app_b64)
                    if cdr_xml and not code:
                        code = _pick_code_from_cdr(cdr_xml)

            # Si no hay code del CDR, usa statusCode (sin ceros a la izquierda)
            if not code and status_code:
                norm = status_code.lstrip('0')
                code = norm if norm != '' else '0'

            if code:
                print(f"[DEBUG] getStatus parsed code: {code}")
                if cdr_xml and code != '0':
                    desc = _pick_desc_from_cdr(cdr_xml)
                    if desc:
                        print(f"[DEBUG] getStatus CDR description: {desc}")
            else:
                print("[DEBUG] getStatus code not found; cdr len=" + str(len(cdr_xml)))

            # Actualizar el registro de control del resumen (si existe) con el resultado
            try:
                from .models import ResumenDiario  # type: ignore
                rd = ResumenDiario.objects.filter(ticket=ticket).first()
                if rd is not None and code:
                    if code == '0':
                        rd.estado = 'ACEPTADO'
                    elif code == '98':
                        rd.estado = 'ENVIADO'  # aún en proceso
                    else:
                        rd.estado = 'RECHAZADO'
                    rd.codigo_respuesta = code[:10]
                    if cdr_xml:
                        rd.respuesta_sunat = cdr_xml
                    rd.save(update_fields=['estado', 'codigo_respuesta', 'respuesta_sunat', 'updated_at'])
            except Exception:
                pass

            return (code, cdr_xml)
        except Exception:
            return ("", "")


    def consultar_cdr_por_comprobante(self, comprobante) -> Tuple[str, str]:
        """Consulta CDR de un comprobante enviado (sendBill) usando getStatusCdr.
        Retorna (codigo_respuesta, cdr_xml_decodificado). Si no hay CDR, devuelve ("", "").
        """
        try:
            import requests as _req
            from lxml import etree as _ET
            ruc = (self.config.emisor_ruc or "").strip()
            usuario_sol = (self.config.usuario_sunat or "").strip()
            password_sol = (self.config.password_sunat or "").strip()
            username = f"{ruc}{usuario_sol}"

            tipo = getattr(comprobante, 'tipo_comprobante', '') or ''
            serie = getattr(comprobante, 'serie', '') or ''
            try:
                numero = int(getattr(comprobante, 'numero', 0))
            except Exception:
                numero = 0
            if not (ruc and tipo and serie and numero):
                return ("", "")

            soap = f"""<?xml version="1.0" encoding="UTF-8"?>
                <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ser="http://service.sunat.gob.pe" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
                <soapenv:Header>
                    <wsse:Security>
                        <wsse:UsernameToken>
                            <wsse:Username>{username}</wsse:Username>
                            <wsse:Password>{password_sol}</wsse:Password>
                        </wsse:UsernameToken>
                    </wsse:Security>
                </soapenv:Header>
                <soapenv:Body>
                    <ser:getStatusCdr>
                        <rucComprobante>{ruc}</rucComprobante>
                        <tipoComprobante>{tipo}</tipoComprobante>
                        <serieComprobante>{serie}</serieComprobante>
                        <numeroComprobante>{numero:08d}</numeroComprobante>
                    </ser:getStatusCdr>
                </soapenv:Body>
                </soapenv:Envelope>"""

            headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '"urn:getStatusCdr"'}
            endpoint = self.url_base
            resp = _req.post(endpoint, data=soap.encode('utf-8'), headers=headers, timeout=60)
            print(f"📥 getStatusCdr HTTP {resp.status_code}")

            try:
                root = _ET.fromstring((resp.text or '').encode('utf-8'))
            except Exception:
                return ("", "")

            # statusCode si lo hubiera
            status_nodes = root.xpath('//*[local-name()="statusCode"]')
            status_code = (status_nodes[0].text or '').strip() if status_nodes else ''

            # El CDR puede venir en <content> (zip b64) o <applicationResponse>
            app_nodes = root.xpath('//*[local-name()="applicationResponse"]')
            content_nodes = root.xpath('//*[local-name()="content"]')
            app_b64 = ''
            if app_nodes and (app_nodes[0].text or '').strip():
                app_b64 = app_nodes[0].text.strip()
            elif content_nodes and (content_nodes[0].text or '').strip():
                app_b64 = content_nodes[0].text.strip()

            cdr_xml = self._decodificar_cdr(app_b64) if app_b64 else ""

            # Extraer ResponseCode del CDR si está disponible
            code = ""
            if cdr_xml:
                try:
                    cdr_root = _ET.fromstring(cdr_xml.encode('utf-8'))
                    node = cdr_root.find('.//cbc:ResponseCode', namespaces=NSMAP)
                    if node is None:
                        elems = cdr_root.xpath('//*[local-name()="ResponseCode"]')
                        if elems:
                            node = elems[0]
                    if node is not None and (node.text or '').strip():
                        code = node.text.strip()
                except Exception:
                    code = ""

            # Si no hay code en CDR, usa statusCode (normalizado)
            if not code and status_code:
                norm = status_code.lstrip('0')
                code = norm if norm != '' else '0'

            return (code, cdr_xml)
        except Exception as _e:
            print(f"❌ Error en getStatusCdr: {_e}")
            return ("", "")


    def _crear_soap_envelope_summary(self, content_b64: str, file_name: str) -> str:
        """Crea envelope SOAP para enviar Resumen Diario (sendSummary)."""
        # Obtener credenciales de la configuración
        ruc = self.config.emisor_ruc or ""
        usuario_sol = self.config.usuario_sunat or ""
        password_sol = self.config.password_sunat or ""
        username = f"{ruc}{usuario_sol}"

        print("🔐 Usando credenciales WS-Security (sendSummary):")
        print(f"   Username: {username}")
        print(f"   Password: {'*' * len(password_sol) if password_sol else 'NO CONFIGURADO'}")

        # Algunos validadores son estrictos con prefijos y SOAPAction; usar envelope simple
        soap = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
            <soapenv:Envelope xmlns:soapenv=\"http://schemas.xmlsoap.org/soap/envelope/\" xmlns:ser=\"http://service.sunat.gob.pe\" xmlns:wsse=\"http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd\">
            <soapenv:Header>
                <wsse:Security>
                <wsse:UsernameToken>
                    <wsse:Username>{username}</wsse:Username>
                    <wsse:Password>{password_sol}</wsse:Password>
                </wsse:UsernameToken>
                </wsse:Security>
            </soapenv:Header>
            <soapenv:Body>
                <ser:sendSummary>
                <fileName>{file_name}</fileName>
                <contentFile>{content_b64}</contentFile>
                </ser:sendSummary>
            </soapenv:Body>
            </soapenv:Envelope>"""
        return soap
    
    # ------------------------------------------------------------------
    # FASE 3 – Envío ----------------------------------------------------
    # ------------------------------------------------------------------
    def _enviar_a_sunat(self, xml_firmado: str, comprobante):
        """Comprime, codifica y envía al servicio SOAP de SUNAT."""
        content_b64, zip_name = self._zip_and_encode(xml_firmado, comprobante)
        soap_envelope = self._crear_soap_envelope(content_b64, zip_name)

        endpoint = self.url_base
        logger.info("Enviando a SUNAT (%s) – Tamaño SOAP: %s caracteres", endpoint, len(soap_envelope))

        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": '"urn:sendBill"',
        }

        print("🚀 Enviando request SOAP a SUNAT…")
        print(f"   Endpoint: {endpoint}")
        print(f"   Tamaño SOAP: {len(soap_envelope)} caracteres")
        
        response = requests.post(
            endpoint,
            data=soap_envelope.encode("utf-8"),
            headers=headers,
            timeout=60,
        )
        print(f"📥 SUNAT respondió HTTP {response.status_code}")
        
        # Log de respuesta para debugging
        if response.status_code != 200:
            print(f"❌ Error HTTP {response.status_code}")
            print(f"   Respuesta: {response.text}")
        else:
            print(f"✅ Respuesta exitosa: {response.text}...")
            
        # SUNAT suele responder HTTP 500 con un Fault en el body. Devolvemos siempre el cuerpo para depurar.
        return response.text

    # ------------------------------------------------------------------
    # Helpers varios ----------------------------------------------------
    # ------------------------------------------------------------------
    def _get_url_base(self):
        """Determina URL base por ambiente (permite override en configuración)."""
        try:
            custom_url = (self.config.url_sunat or "").strip()
        except Exception:
            custom_url = ""
        if custom_url:
            return custom_url
        return (
            "https://e-factura.sunat.gob.pe/ol-ti-itcpfegem/billService"
            if self.ambiente == "1"
            else "https://e-beta.sunat.gob.pe/ol-ti-itcpfegem/billService"
        )

    # ------------------------------------------------------------------
    # Construcción y firma del Resumen Diario de Baja ------------------
    # ------------------------------------------------------------------
    def _crear_xml_resumen_baja_boletas(self, boletas, ref_date=None) -> Tuple[str, str, object]:
        """Construye SummaryDocuments (RC) con ConditionCode=3 (baja) para boletas (03), SIN depender de ResumenDiario."""
        from lxml import etree as ET
        from datetime import datetime
        from typing import Optional
        import re

        # ===== Helpers de fecha (Lima) =====
        def _local_today() -> datetime.date:
            try:
                from django.utils import timezone as dj_tz
                return dj_tz.localdate()
            except Exception:
                pass
            try:
                from zoneinfo import ZoneInfo
                return datetime.now(ZoneInfo("America/Lima")).date()
            except Exception:
                pass
            return datetime.now().date()

        def _to_local_date(dt: Optional[datetime]) -> datetime.date:
            if dt is None:
                return _local_today()
            try:
                from django.utils.timezone import localtime as dj_localtime, is_aware as dj_is_aware
                if dj_is_aware(dt):
                    return dj_localtime(dt).date()
            except Exception:
                pass
            try:
                return dt.date()
            except Exception:
                return _local_today()

        if not boletas:
            raise ValueError("No hay boletas para incluir en el Resumen Diario (RC).")

        # ==== Namespaces ====
        NS_SUM = 'urn:sunat:names:specification:ubl:peru:schema:xsd:SummaryDocuments-1'
        NS_SAC = 'urn:sunat:names:specification:ubl:peru:schema:xsd:SunatAggregateComponents-1'
        nsmap = {None: NS_SUM, 'cbc': NSMAP['cbc'], 'cac': NSMAP['cac'], 'ext': NSMAP['ext'], 'sac': NS_SAC}

        # ==== Fechas ====
        if ref_date is None:
            ref_date = _to_local_date(getattr(boletas[0], 'fecha_emision', None))
        issue_date = _local_today()
        # SUNAT exige que la fecha del ID (RC-YYYYMMDD-###) coincida con IssueDate
        ymd = issue_date.strftime('%Y%m%d')

        # ==== Emisor ====
        ruc = (getattr(self.config, 'emisor_ruc', None) or getattr(self.config, 'ruc', None) or '').strip()
        razon = (getattr(self.config, 'emisor_razon_social', None) or getattr(self.config, 'razon_social', None) or '').strip()
        if not (ruc.isdigit() and len(ruc) == 11):
            raise ValueError(f"RUC emisor inválido/ausente en config: '{ruc}'")

        # ==== Sede (opcional, solo para devolverla) ====
        try:
            sede = getattr(getattr(boletas[0], 'venta_pos', None), 'caja', None)
            sede = getattr(sede, 'sede', None)
        except Exception:
            sede = None

        # ==== Correlativo del día (fuente de verdad: modelo ResumenDiario) ====
        resumen_id = None
        try:
            from .models import ResumenDiario  # type: ignore
            corr = ResumenDiario.siguiente_correlativo(ruc, issue_date)
            if corr > 999:
                corr = corr % 999 or 1
            rd = ResumenDiario.objects.create(
                resumen_id=f"RC-{ymd}-{corr:03d}",
                emisor_ruc=ruc,
                fecha_referencia=ref_date,
                fecha_emision=issue_date,
                correlativo=corr,
                estado='GENERADO',
            )
            try:
                rd.comprobantes.set(list(boletas))
            except Exception:
                pass
            resumen_id = rd.resumen_id
        except Exception:
            logger.exception("No se pudo registrar ResumenDiario en BD; se usará correlativo de respaldo")
            resumen_id = None

        # Fallback (BD no disponible): segundos del día para no repetir correlativo
        if not resumen_id:
            now = datetime.now()
            sod = (now.hour * 3600 + now.minute * 60 + now.second) % 999 or 1
            resumen_id = f"RC-{ymd}-{sod:03d}"

        # Guarda para logs externos si quieres
        try:
            self.last_resumen_id = resumen_id
        except Exception:
            pass

        # ==== XML SummaryDocuments ====
        root = ET.Element('SummaryDocuments', nsmap=nsmap)

        # UBLExtensions
        ubl_exts = ET.SubElement(root, ET.QName(nsmap['ext'], 'UBLExtensions'))
        ET.SubElement(ubl_exts, ET.QName(nsmap['ext'], 'UBLExtension')).append(
            ET.Element(ET.QName(nsmap['ext'], 'ExtensionContent'))
        )

        # Cabecera
        ET.SubElement(root, ET.QName(nsmap['cbc'], 'UBLVersionID')).text = '2.0'
        ET.SubElement(root, ET.QName(nsmap['cbc'], 'CustomizationID')).text = '1.1'
        ET.SubElement(root, ET.QName(nsmap['cbc'], 'ID')).text = resumen_id
        ET.SubElement(root, ET.QName(nsmap['cbc'], 'ReferenceDate')).text = ref_date.strftime('%Y-%m-%d')
        ET.SubElement(root, ET.QName(nsmap['cbc'], 'IssueDate')).text = issue_date.strftime('%Y-%m-%d')

        # Firma (metadatos) – antes del emisor
        cac_signature = ET.SubElement(root, ET.QName(nsmap['cac'], 'Signature'))
        ET.SubElement(cac_signature, ET.QName(nsmap['cbc'], 'ID')).text = 'IDSignKG'
        sig_party = ET.SubElement(cac_signature, ET.QName(nsmap['cac'], 'SignatoryParty'))
        sig_pid = ET.SubElement(sig_party, ET.QName(nsmap['cac'], 'PartyIdentification'))
        ET.SubElement(sig_pid, ET.QName(nsmap['cbc'], 'ID'), schemeID='6').text = ruc
        sig_pname = ET.SubElement(sig_party, ET.QName(nsmap['cac'], 'PartyName'))
        ET.SubElement(sig_pname, ET.QName(nsmap['cbc'], 'Name')).text = razon or ruc
        dig_attach = ET.SubElement(cac_signature, ET.QName(nsmap['cac'], 'DigitalSignatureAttachment'))
        ext_ref = ET.SubElement(dig_attach, ET.QName(nsmap['cac'], 'ExternalReference'))
        ET.SubElement(ext_ref, ET.QName(nsmap['cbc'], 'URI')).text = '#signatureKG'

        # Emisor (SUNAT valida RUC aquí)
        sup = ET.SubElement(root, ET.QName(nsmap['cac'], 'AccountingSupplierParty'))
        ET.SubElement(sup, ET.QName(nsmap['cbc'], 'CustomerAssignedAccountID')).text = ruc
        ET.SubElement(sup, ET.QName(nsmap['cbc'], 'AdditionalAccountID')).text = '6'
        party = ET.SubElement(sup, ET.QName(nsmap['cac'], 'Party'))
        ple = ET.SubElement(party, ET.QName(nsmap['cac'], 'PartyLegalEntity'))
        ET.SubElement(ple, ET.QName(nsmap['cbc'], 'RegistrationName')).text = razon or ruc

        # Líneas (ConditionCode 3 + IGV 0.00) – sin BillingPayment
        for idx, c in enumerate(boletas, 1):
            moneda = (getattr(c, 'moneda', None) or getattr(c, 'moneda_codigo', None) or 'PEN').strip().upper()
            serie = (str(getattr(c, 'serie', '')).strip().upper())
            try:
                numero = int(getattr(c, 'numero', 0))
            except Exception:
                numero = 0
            if not serie or numero <= 0:
                raise ValueError(f"Serie/numero inválidos para boleta id={getattr(c,'id',None)}: serie='{serie}' numero='{numero}'")

            line = ET.SubElement(root, ET.QName(nsmap['sac'], 'SummaryDocumentsLine'))
            ET.SubElement(line, ET.QName(nsmap['cbc'], 'LineID')).text = str(idx)
            ET.SubElement(line, ET.QName(nsmap['cbc'], 'DocumentTypeCode')).text = '03'
            ET.SubElement(line, ET.QName(nsmap['cbc'], 'ID')).text = f"{serie}-{numero:08d}"

            status = ET.SubElement(line, ET.QName(nsmap['cac'], 'Status'))
            ET.SubElement(status, ET.QName(nsmap['cbc'], 'ConditionCode')).text = '3'

            ET.SubElement(line, ET.QName(nsmap['sac'], 'TotalAmount'), currencyID=moneda).text = '0.00'

            tax_total = ET.SubElement(line, ET.QName(nsmap['cac'], 'TaxTotal'))
            ET.SubElement(tax_total, ET.QName(nsmap['cbc'], 'TaxAmount'), currencyID=moneda).text = '0.00'
            tax_sub = ET.SubElement(tax_total, ET.QName(nsmap['cac'], 'TaxSubtotal'))
            ET.SubElement(tax_sub, ET.QName(nsmap['cbc'], 'TaxAmount'), currencyID=moneda).text = '0.00'
            tax_cat = ET.SubElement(tax_sub, ET.QName(nsmap['cac'], 'TaxCategory'))
            tax_scheme = ET.SubElement(tax_cat, ET.QName(nsmap['cac'], 'TaxScheme'))
            ET.SubElement(tax_scheme, ET.QName(nsmap['cbc'], 'ID')).text = '1000'
            ET.SubElement(tax_scheme, ET.QName(nsmap['cbc'], 'Name')).text = 'IGV'
            ET.SubElement(tax_scheme, ET.QName(nsmap['cbc'], 'TaxTypeCode')).text = 'VAT'

        xml_str = ET.tostring(root, encoding='utf-8', xml_declaration=True, pretty_print=False).decode('utf-8')
        return xml_str, resumen_id, sede



    def generar_comunicacion_baja(self, boletas, motivo: str):
        """
        Genera y envía Comunicación de Baja (RA) para boletas (03) con motivo.
        Retorna: ticket (str)
        """
        import io, zipfile, types
        import base64

        # 1) Construir XML RA
        xml_str, resumen_id, _ = self._crear_xml_baja_boletas_ra(boletas, motivo=motivo)

        # 2) Firmar RA (usar firmador de resúmenes; fallback al firmador general)
        xml_bytes = xml_str.encode('utf-8')
        try:
            xml_firmado = self._firmar_xml_resumen(xml_str)
            xml_bytes = xml_firmado.encode('utf-8')
        except Exception:
            if hasattr(self, '_firmar_xml') and callable(getattr(self, '_firmar_xml')):
                try:
                    xml_firmado = self._firmar_xml(xml_str)
                except TypeError:
                    # Tu firmador requiere 'comprobante'; usamos la primera boleta como contexto
                    xml_firmado = self._firmar_xml(xml_str, boletas[0])
                if isinstance(xml_firmado, (bytes, bytearray)):
                    xml_bytes = xml_firmado
                elif isinstance(xml_firmado, str):
                    xml_bytes = xml_firmado.encode('utf-8')
                else:
                    raise RuntimeError("La firma devolvió un tipo no soportado (esperado bytes o str).")

        # 3) ZIP con nombre SUNAT
        ruc = getattr(self.config, 'emisor_ruc', None) or getattr(self.config, 'ruc', '00000000000')
        xml_filename = f"{ruc}-{resumen_id}.xml"
        zip_filename = f"{ruc}-{resumen_id}.zip"

        mem = io.BytesIO()
        with zipfile.ZipFile(mem, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(xml_filename, xml_bytes)
        zip_bytes = mem.getvalue()

        # 4) Enviar via sendSummary
        ticket = self.sendSummary(resumen_id, zip_bytes, zip_filename)
        if not ticket:
            raise RuntimeError(f"No se recibió ticket de SUNAT/OSE al enviar RA {resumen_id}.")
        return ticket

    def generar_comunicacion_baja_factura(self, facturas, motivo: str):
        """
        Genera y envía Comunicación de Baja (RA) para facturas (01) con motivo.
        Retorna: ticket (str)
        """
        import io, zipfile
        try:
            # 1) Construir XML RA para facturas
            xml_str, resumen_id, _ = self._crear_xml_baja_facturas_ra(facturas, motivo=motivo)

            # 2) Firmar (reutiliza firmador del servicio)
            try:
                xml_firmado = self._firmar_xml_resumen(xml_str)
                xml_bytes = xml_firmado.encode('utf-8')
            except Exception:
                # Fallback: sin firmar si hubiera problemas (no debería ocurrir)
                xml_bytes = xml_str.encode('utf-8')

            # 3) ZIP con nombre SUNAT
            ruc = getattr(self.config, 'emisor_ruc', None) or getattr(self.config, 'ruc', '00000000000')
            xml_filename = f"{ruc}-{resumen_id}.xml"
            zip_filename = f"{ruc}-{resumen_id}.zip"

            mem = io.BytesIO()
            with zipfile.ZipFile(mem, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(xml_filename, xml_bytes)
            zip_bytes = mem.getvalue()

            # 4) Enviar via sendSummary
            ticket = self.sendSummary(resumen_id, zip_bytes, zip_filename)
            if not ticket:
                raise RuntimeError(f"No se recibió ticket de SUNAT/OSE al enviar RA {resumen_id}.")
            return ticket
        except Exception as e:
            print(f"❌ Error generando/enviando RA de facturas: {e}")
            raise

    def sendSummary(self, resumen_id: str, zip_bytes: bytes, zip_filename: str) -> str:
        """
        Envía un Resumen (RC o RA) por SOAP: sendSummary(fileName, contentFile) y retorna el ticket.
        Usa self.url_base determinado por el ambiente y credenciales de self.config.
        """
        try:
            import base64 as _b64
            content_b64 = _b64.b64encode(zip_bytes).decode('utf-8')
            soap = self._crear_soap_envelope_summary(content_b64, zip_filename)
            headers = {
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": '"urn:sendSummary"',
            }
            endpoint = self.url_base
            resp = requests.post(endpoint, data=soap.encode('utf-8'), headers=headers, timeout=60)
            print(f"📥 sendSummary HTTP {resp.status_code}")
            return self._parse_ticket_from_sendSummary(resp.text)
        except Exception as e:
            print(f"❌ Error en sendSummary: {e}")
            return ""

    def _crear_xml_baja_boletas_ra(self, boletas, ref_date=None, motivo: str = "Anulación por solicitud del cliente") -> tuple[str, str, object]:
        """
        Construye Comunicación de Baja (RA) para boletas (03) de fecha ANTERIOR.
        Estructura (orden estricto) según SUNAT:
        UBLExtensions, UBLVersionID(2.0), CustomizationID(1.0), ID(RA-YYYYMMDD-###),
        ReferenceDate(fecha doc), IssueDate(hoy),
        cac:Signature, cac:AccountingSupplierParty,
        sac:VoidedDocumentsLine*
        Cada línea: cbc:LineID, cbc:DocumentTypeCode(03),
                    sac:DocumentSerialID, sac:DocumentNumberID,
                    cbc:VoidReasonDescription
        Retorna: (xml_str, resumen_id, sede=None)
        """
        from lxml import etree as ET
        from datetime import datetime
        try:
            from django.utils.timezone import is_aware, localtime
            from django.utils import timezone as dj_timezone
        except Exception:
            # Fallbacks en caso de no tener Django timezone
            is_aware = lambda x: False
            localtime = None
            class _TZ:
                @staticmethod
                def localdate():
                    try:
                        from zoneinfo import ZoneInfo
                        return datetime.now(ZoneInfo("America/Lima")).date()
                    except Exception:
                        return datetime.now().date()
            dj_timezone = _TZ()

        if not boletas:
            raise ValueError("No hay boletas para Comunicación de Baja (RA).")

        # === Namespaces ===
        NS_RA  = 'urn:sunat:names:specification:ubl:peru:schema:xsd:VoidedDocuments-1'
        NS_SAC = 'urn:sunat:names:specification:ubl:peru:schema:xsd:SunatAggregateComponents-1'
        nsmap = {
            None: NS_RA,
            'cbc': NSMAP['cbc'],
            'cac': NSMAP['cac'],
            'ext': NSMAP['ext'],
            'sac': NS_SAC,
        }

        # === Fechas ===
        em_dt = boletas[0].fecha_emision
        em_local = localtime(em_dt) if is_aware(em_dt) and localtime else em_dt
        ref_date = (ref_date or em_local.date())            # fecha del documento (obligatoria)
        issue_date = dj_timezone.localdate()                # fecha de generación RA (hoy Lima)

        # === Emisor ===
        ruc = (getattr(self.config, 'emisor_ruc', None) or getattr(self.config, 'ruc', None) or '').strip()
        razon = (getattr(self.config, 'emisor_razon_social', None) or getattr(self.config, 'razon_social', None) or '').strip()
        if not (ruc.isdigit() and len(ruc) == 11):
            raise ValueError(f"RUC emisor inválido/ausente en config: '{ruc}'")

        # === Correlativo RA-YYYYMMDD-### (en memoria; sin tabla) ===
        ymd_issue = issue_date.strftime('%Y%m%d')
        try:
            if not hasattr(self, '_seq_cache'):
                self._seq_cache = {}
            key = f"ra:seq:{ruc}:{ymd_issue}"
            seq = self._seq_cache.get(key, 0) + 1
            self._seq_cache[key] = seq
            resumen_id = f"RA-{ymd_issue}-{seq:03d}"
        except Exception:
            resumen_id = f"RA-{ymd_issue}-001"
        self.last_resumen_id = resumen_id  # útil para logs

        # === XML RA (orden estricto) ===
        root = ET.Element('VoidedDocuments', nsmap=nsmap)

        # 1) UBLExtensions
        ubl_exts = ET.SubElement(root, ET.QName(nsmap['ext'], 'UBLExtensions'))
        ET.SubElement(ubl_exts, ET.QName(nsmap['ext'], 'UBLExtension')).append(
            ET.Element(ET.QName(nsmap['ext'], 'ExtensionContent'))
        )

        # 2) UBLVersionID (2.0)
        ET.SubElement(root, ET.QName(nsmap['cbc'], 'UBLVersionID')).text = '2.0'
        # 3) CustomizationID (1.0)
        ET.SubElement(root, ET.QName(nsmap['cbc'], 'CustomizationID')).text = '1.0'
        # 4) ID (RA-YYYYMMDD-###) — basado en IssueDate
        ET.SubElement(root, ET.QName(nsmap['cbc'], 'ID')).text = resumen_id
        # 5) ReferenceDate — fecha del doc
        ET.SubElement(root, ET.QName(nsmap['cbc'], 'ReferenceDate')).text = ref_date.strftime('%Y-%m-%d')
        # 6) IssueDate — hoy
        ET.SubElement(root, ET.QName(nsmap['cbc'], 'IssueDate')).text = issue_date.strftime('%Y-%m-%d')

        # 7) cac:Signature (metadatos firma)
        sig = ET.SubElement(root, ET.QName(nsmap['cac'], 'Signature'))
        ET.SubElement(sig, ET.QName(nsmap['cbc'], 'ID')).text = 'IDSignKG'
        sp = ET.SubElement(sig, ET.QName(nsmap['cac'], 'SignatoryParty'))
        spid = ET.SubElement(sp, ET.QName(nsmap['cac'], 'PartyIdentification'))
        ET.SubElement(spid, ET.QName(nsmap['cbc'], 'ID'), schemeID='6').text = ruc
        spname = ET.SubElement(sp, ET.QName(nsmap['cac'], 'PartyName'))
        ET.SubElement(spname, ET.QName(nsmap['cbc'], 'Name')).text = razon or ruc
        da = ET.SubElement(sig, ET.QName(nsmap['cac'], 'DigitalSignatureAttachment'))
        er = ET.SubElement(da, ET.QName(nsmap['cac'], 'ExternalReference'))
        ET.SubElement(er, ET.QName(nsmap['cbc'], 'URI')).text = '#signatureKG'

        # 8) cac:AccountingSupplierParty
        sup = ET.SubElement(root, ET.QName(nsmap['cac'], 'AccountingSupplierParty'))
        ET.SubElement(sup, ET.QName(nsmap['cbc'], 'CustomerAssignedAccountID')).text = ruc
        ET.SubElement(sup, ET.QName(nsmap['cbc'], 'AdditionalAccountID')).text = '6'
        party = ET.SubElement(sup, ET.QName(nsmap['cac'], 'Party'))
        ple = ET.SubElement(party, ET.QName(nsmap['cac'], 'PartyLegalEntity'))
        ET.SubElement(ple, ET.QName(nsmap['cbc'], 'RegistrationName')).text = razon or ruc

        # 9) Líneas sac:VoidedDocumentsLine
        motivo_txt = (motivo or 'Anulación')
        for idx, c in enumerate(boletas, 1):
            serie = (str(getattr(c, 'serie', '')).strip().upper())
            try:
                numero = int(getattr(c, 'numero', 0))
            except Exception:
                numero = 0
            if not serie or numero <= 0:
                raise ValueError(f"Serie/numero inválidos para RA: serie='{serie}' numero='{numero}'")

            line = ET.SubElement(root, ET.QName(nsmap['sac'], 'VoidedDocumentsLine'))
            ET.SubElement(line, ET.QName(nsmap['cbc'], 'LineID')).text = str(idx)
            ET.SubElement(line, ET.QName(nsmap['cbc'], 'DocumentTypeCode')).text = '03'  # Boleta
            ET.SubElement(line, ET.QName(nsmap['sac'], 'DocumentSerialID')).text = serie
            ET.SubElement(line, ET.QName(nsmap['sac'], 'DocumentNumberID')).text = f"{numero:08d}"
            ET.SubElement(line, ET.QName(nsmap['sac'], 'VoidReasonDescription')).text = motivo_txt

        xml_str = ET.tostring(root, encoding='utf-8', xml_declaration=True, pretty_print=False).decode('utf-8')
        return xml_str, resumen_id, None
        


    def _crear_xml_baja_facturas_ra(self, facturas, ref_date=None, motivo: str = "Anulación por solicitud del cliente") -> tuple[str, str, object]:
        """
        Construye Comunicación de Baja (RA) para facturas (01).
        Retorna: (xml_str, resumen_id, sede=None)
        """
        from lxml import etree as ET
        from datetime import datetime
        import re

        # timezone seguro (Django o fallback)
        try:
            from django.utils.timezone import is_aware, localtime  # type: ignore
            from django.utils import timezone as dj_timezone      # .localdate()
        except Exception:
            is_aware = lambda x: False  # type: ignore
            localtime = None            # type: ignore
            class _TZ:
                @staticmethod
                def localdate():
                    try:
                        from zoneinfo import ZoneInfo
                        return datetime.now(ZoneInfo("America/Lima")).date()
                    except Exception:
                        return datetime.now().date()
            dj_timezone = _TZ()  # type: ignore

        if not facturas:
            raise ValueError("No hay facturas para Comunicación de Baja (RA).")

        NS_RA  = 'urn:sunat:names:specification:ubl:peru:schema:xsd:VoidedDocuments-1'
        NS_SAC = 'urn:sunat:names:specification:ubl:peru:schema:xsd:SunatAggregateComponents-1'
        nsmap = {
            None: NS_RA,
            'cbc': NSMAP['cbc'],
            'cac': NSMAP['cac'],
            'ext': NSMAP['ext'],
            'sac': NS_SAC,
        }

        # Fechas
        em_dt = facturas[0].fecha_emision
        em_local = localtime(em_dt) if is_aware(em_dt) and localtime else em_dt
        ref_date = (ref_date or em_local.date())
        issue_date = dj_timezone.localdate()  # hoy (Lima)

        # Emisor
        ruc = (getattr(self.config, 'emisor_ruc', None) or getattr(self.config, 'ruc', None) or '').strip()
        razon = (getattr(self.config, 'emisor_razon_social', None) or getattr(self.config, 'razon_social', None) or '').strip()
        if not (ruc.isdigit() and len(ruc) == 11):
            raise ValueError(f"RUC emisor inválido/ausente en config: '{ruc}'")

        # ===== correlativo RA único por día (evita 2324) =====
        ymd_issue = issue_date.strftime('%Y%m%d')
        key = f"ra:seq:{ruc}:{ymd_issue}"

        # cache en memoria
        if not hasattr(self, '_seq_cache'):
            self._seq_cache = {}
        max_seq = int(self._seq_cache.get(key, 0))  # base desde cache

        # leer de BD cualquier RA del mismo día ya enviada (boletas o facturas)
        try:
            from .models import ComprobanteElectronico  # type: ignore
            qs = (ComprobanteElectronico.objects
                .filter(emisor_ruc=ruc, respuesta_sunat__icontains=f"RA-{ymd_issue}-")
                .values_list('respuesta_sunat', flat=True)[:5000])
            pat = re.compile(rf"RA-{ymd_issue}-(\d{{3}})")
            for txt in qs:
                if not txt:
                    continue
                for m in pat.finditer(txt):
                    try:
                        n = int(m.group(1))
                        if n > max_seq:
                            max_seq = n
                    except Exception:
                        pass
        except Exception:
            # si no hay BD o falla, seguimos solo con cache
            pass

        seq = max_seq + 1
        self._seq_cache[key] = seq
        resumen_id = f"RA-{ymd_issue}-{seq:03d}"
        self.last_resumen_id = resumen_id
        # =====================================================

        # XML RA
        root = ET.Element('VoidedDocuments', nsmap=nsmap)

        ubl_exts = ET.SubElement(root, ET.QName(nsmap['ext'], 'UBLExtensions'))
        ET.SubElement(ubl_exts, ET.QName(nsmap['ext'], 'UBLExtension')).append(
            ET.Element(ET.QName(nsmap['ext'], 'ExtensionContent'))
        )

        ET.SubElement(root, ET.QName(nsmap['cbc'], 'UBLVersionID')).text = '2.0'
        ET.SubElement(root, ET.QName(nsmap['cbc'], 'CustomizationID')).text = '1.0'
        ET.SubElement(root, ET.QName(nsmap['cbc'], 'ID')).text = resumen_id
        ET.SubElement(root, ET.QName(nsmap['cbc'], 'ReferenceDate')).text = ref_date.strftime('%Y-%m-%d')
        ET.SubElement(root, ET.QName(nsmap['cbc'], 'IssueDate')).text = issue_date.strftime('%Y-%m-%d')

        sig = ET.SubElement(root, ET.QName(nsmap['cac'], 'Signature'))
        ET.SubElement(sig, ET.QName(nsmap['cbc'], 'ID')).text = 'IDSignKG'
        sp = ET.SubElement(sig, ET.QName(nsmap['cac'], 'SignatoryParty'))
        spid = ET.SubElement(sp, ET.QName(nsmap['cac'], 'PartyIdentification'))
        ET.SubElement(spid, ET.QName(nsmap['cbc'], 'ID'), schemeID='6').text = ruc
        spname = ET.SubElement(sp, ET.QName(nsmap['cac'], 'PartyName'))
        ET.SubElement(spname, ET.QName(nsmap['cbc'], 'Name')).text = razon or ruc
        da = ET.SubElement(sig, ET.QName(nsmap['cac'], 'DigitalSignatureAttachment'))
        er = ET.SubElement(da, ET.QName(nsmap['cac'], 'ExternalReference'))
        ET.SubElement(er, ET.QName(nsmap['cbc'], 'URI')).text = '#signatureKG'

        sup = ET.SubElement(root, ET.QName(nsmap['cac'], 'AccountingSupplierParty'))
        ET.SubElement(sup, ET.QName(nsmap['cbc'], 'CustomerAssignedAccountID')).text = ruc
        ET.SubElement(sup, ET.QName(nsmap['cbc'], 'AdditionalAccountID')).text = '6'
        party = ET.SubElement(sup, ET.QName(nsmap['cac'], 'Party'))
        ple = ET.SubElement(party, ET.QName(nsmap['cac'], 'PartyLegalEntity'))
        ET.SubElement(ple, ET.QName(nsmap['cbc'], 'RegistrationName')).text = razon or ruc

        motivo_txt = (motivo or 'Anulación').strip()
        for idx, c in enumerate(facturas, 1):
            serie = (str(getattr(c, 'serie', '')).strip().upper())
            try:
                numero = int(getattr(c, 'numero', 0))
            except Exception:
                numero = 0
            if not serie or numero <= 0:
                raise ValueError(f"Serie/numero inválidos para RA (factura): serie='{serie}' numero='{numero}'")

            line = ET.SubElement(root, ET.QName(nsmap['sac'], 'VoidedDocumentsLine'))
            ET.SubElement(line, ET.QName(nsmap['cbc'], 'LineID')).text = str(idx)
            ET.SubElement(line, ET.QName(nsmap['cbc'], 'DocumentTypeCode')).text = '01'  # Factura
            ET.SubElement(line, ET.QName(nsmap['sac'], 'DocumentSerialID')).text = serie
            ET.SubElement(line, ET.QName(nsmap['sac'], 'DocumentNumberID')).text = f"{numero:08d}"
            ET.SubElement(line, ET.QName(nsmap['sac'], 'VoidReasonDescription')).text = motivo_txt

        xml_str = ET.tostring(root, encoding='utf-8', xml_declaration=True, pretty_print=False).decode('utf-8')
        return xml_str, resumen_id, None


    def _firmar_xml_resumen(self, xml_string: str) -> str:
        _ensure_signxml()
        pfx_field = self.config.certificado_digital
        pfx_path = pfx_field.path if pfx_field else None
        if not pfx_path or not os.path.exists(pfx_path):
            raise ValueError("No se encontró el certificado digital para firmar el resumen")
        pfx_pass = (os.getenv('PFX_PASS') or (self.config.password_certificado or '')).encode()

        with open(pfx_path, 'rb') as fh:
            pfx_data = fh.read()
        private_key, certificate, _ = pkcs12.load_key_and_certificates(pfx_data, pfx_pass)
        if private_key is None or certificate is None:
            raise ValueError('El archivo .pfx no contiene llave/cert válidos')

        key_pem = private_key.private_bytes(encoding=Encoding.PEM, format=PrivateFormat.PKCS8, encryption_algorithm=NoEncryption())
        cert_pem = certificate.public_bytes(Encoding.PEM)

        doc = etree.fromstring(xml_string.encode('utf-8'))
        signer = XMLSigner(method=methods.enveloped, signature_algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256", digest_algorithm="http://www.w3.org/2001/04/xmlenc#sha256", c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#")
        signed = signer.sign(doc, key=key_pem, cert=cert_pem)

        signature_el = signed.find('.//ds:Signature', namespaces=NSMAP)
        if signature_el is None:
            raise ValueError('No se encontró ds:Signature en el resumen firmado')
        signature_el.attrib['Id'] = 'signatureKG'
        ext_content = signed.find('.//ext:ExtensionContent', namespaces=NSMAP)
        if ext_content is None:
            raise ValueError('No se encontró ext:ExtensionContent en el resumen')
        signature_el.getparent().remove(signature_el)
        ext_content.append(signature_el)
        return etree.tostring(signed, encoding='utf-8', xml_declaration=True, pretty_print=False).decode()

    def _zip_and_encode_summary(self, xml_firmado: str, resumen_id: str) -> Tuple[str, str]:
        zip_name = f"{self.config.emisor_ruc}-{resumen_id}.zip"
        # SUNAT espera que el XML interno coincida con el nombre del ZIP (sin .zip)
        xml_name = f"{self.config.emisor_ruc}-{resumen_id}.xml"
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_STORED) as zf:
            zf.writestr(xml_name, xml_firmado.encode('utf-8'))
        encoded_content = base64.b64encode(buffer.getvalue()).decode()
        print(f"📦 ZIP (RC) generado: {zip_name} – tamaño Base64: {len(encoded_content)} caracteres")
        return encoded_content, zip_name

    def _parse_ticket_from_sendSummary(self, soap_response: str) -> str:
        try:
            root = etree.fromstring(soap_response.encode('utf-8'))
            # Usar XPath completo (no .find) para soportar funciones como local-name()
            elems = root.xpath('//*[local-name()="ticket"]')
            if elems:
                text = (elems[0].text or '').strip()
                if text:
                    return text

            # Fallback: etiqueta sin namespace explícito
            elems = root.xpath('//ticket')
            if elems:
                text = (elems[0].text or '').strip()
                if text:
                    return text

            # Fallback final: regex directa sobre el XML (robusto ante namespaces variados)
            import re
            m = re.search(r'<(?:[^:>]*:)?ticket\s*>\s*(\d+)\s*</(?:[^:>]*:)?ticket\s*>', soap_response)
            if m:
                return m.group(1)

            # Trazas de Fault si existiera
            faults = root.xpath('//*[local-name()="Fault"]')
            if faults:
                print("[DEBUG] SOAP Fault en sendSummary:\n" + etree.tostring(faults[0], encoding='unicode'))
            return ''
        except Exception:
            return ''

    # -------------------- Procesamiento de la respuesta ----------------
    def _decodificar_cdr(self, application_response: str) -> str:
        """Decodifica el CDR (Constancia de Recepción) de SUNAT desde Base64"""
        try:
            # Decodificar Base64
            cdr_bytes = base64.b64decode(application_response)
            
            # Descomprimir ZIP
            with zipfile.ZipFile(BytesIO(cdr_bytes), 'r') as zip_file:
                # Buscar archivo XML dentro del ZIP
                xml_files = [f for f in zip_file.namelist() if f.endswith('.xml')]
                if xml_files:
                    # Leer el primer archivo XML encontrado
                    with zip_file.open(xml_files[0]) as xml_file:
                        cdr_xml = xml_file.read().decode('utf-8')
                        print(f"✅ CDR decodificado exitosamente: {len(cdr_xml)} caracteres")
                        return cdr_xml
                else:
                    print("⚠️  No se encontraron archivos XML en el CDR")
                    return ""
        except Exception as e:
            print(f"❌ Error decodificando CDR: {e}")
            return ""

    def _procesar_respuesta(self, comprobante, respuesta_sunat: str):
        """Procesa la respuesta de SUNAT y actualiza el estado del comprobante."""
        try:
            # Auxiliar: detectar fallas transitorias y encolar reintentos
            if self._is_transient_fault(respuesta_sunat):
                try:
                    self._queue_retry(comprobante, respuesta_sunat)
                except Exception:
                    logger.exception("No se pudo encolar reintento SUNAT")
            # Verificar si hay error 2335 (documento alterado)
            if "2335" in respuesta_sunat or "alterado" in respuesta_sunat.lower():
                print("❌ SUNAT devolvió fault 2335 – Documento alterado")
                logger.error("SUNAT fault 2335: Documento electrónico alterado")
                comprobante.estado_sunat = "RECHAZADO"
                comprobante.codigo_respuesta_sunat = "2335"
                comprobante.respuesta_sunat = respuesta_sunat
                comprobante.fecha_respuesta_sunat = timezone.now()
                comprobante.save(
                    update_fields=[
                        "estado_sunat",
                        "codigo_respuesta_sunat",
                        "respuesta_sunat",
                        "fecha_respuesta_sunat",
                    ]
                )
                return False
            elif "applicationResponse" in respuesta_sunat:
                print("✅ Respuesta exitosa de SUNAT (CDR recibido)")
                
                # Extraer el CDR de la respuesta SOAP
                try:
                    # Parsear la respuesta SOAP para extraer applicationResponse
                    root = etree.fromstring(respuesta_sunat.encode('utf-8'))
                    app_response_elem = root.find(".//applicationResponse")
                    
                    if app_response_elem is not None:
                        application_response = app_response_elem.text
                        print(f"📦 CDR Base64 extraído: {len(application_response)} caracteres")
                        
                        # Decodificar el CDR
                        cdr_decodificado = self._decodificar_cdr(application_response)
                        
                        if cdr_decodificado:
                            # Verificar el ResponseCode en el CDR
                            try:
                                cdr_root = etree.fromstring(cdr_decodificado.encode('utf-8'))
                                response_code_elem = cdr_root.find(".//cbc:ResponseCode", namespaces=NSMAP)
                                
                                if response_code_elem is not None:
                                    response_code = response_code_elem.text
                                    if response_code == "0":
                                        estado = "ACEPTADO"
                                        print(f"✅ CDR indica ACEPTADO (ResponseCode: {response_code})")
                                    else:
                                        estado = "RECHAZADO"
                                        print(f"❌ CDR indica RECHAZADO (ResponseCode: {response_code})")
                                else:
                                    estado = "ENVIADO"
                                    print("⚠️ No se encontró ResponseCode en el CDR")
                            except Exception as e:
                                print(f"⚠️ Error verificando ResponseCode: {e}")
                                estado = "ENVIADO"
                            
                            # Guardar el CDR decodificado y el estado correcto
                            comprobante.cdr_sunat = cdr_decodificado
                            comprobante.estado_sunat = estado
                            comprobante.respuesta_sunat = respuesta_sunat
                            comprobante.fecha_respuesta_sunat = timezone.now()
                            comprobante.save(
                                update_fields=[
                                    "estado_sunat",
                                    "respuesta_sunat",
                                    "fecha_respuesta_sunat",
                                    "cdr_sunat",
                                ]
                            )
                            print("✅ CDR guardado correctamente en el comprobante")
                            return True
                        else:
                            print("❌ No se pudo decodificar el CDR")
                            comprobante.estado_sunat = "ENVIADO"
                            comprobante.respuesta_sunat = respuesta_sunat
                            comprobante.fecha_respuesta_sunat = timezone.now()
                            comprobante.save(
                                update_fields=[
                                    "estado_sunat",
                                    "respuesta_sunat",
                                    "fecha_respuesta_sunat",
                                ]
                            )
                            return False
                    else:
                        print("⚠️  No se encontró applicationResponse en la respuesta")
                        comprobante.estado_sunat = "ENVIADO"
                        comprobante.respuesta_sunat = respuesta_sunat
                        comprobante.fecha_respuesta_sunat = timezone.now()
                        comprobante.save(
                            update_fields=[
                                "estado_sunat",
                                "respuesta_sunat",
                                "fecha_respuesta_sunat",
                            ]
                        )
                        return False
                        
                except Exception as e:
                    print(f"❌ Error procesando applicationResponse: {e}")
                    comprobante.estado_sunat = "ENVIADO"
                    comprobante.respuesta_sunat = respuesta_sunat
                    comprobante.fecha_respuesta_sunat = timezone.now()
                    comprobante.save(
                        update_fields=[
                            "estado_sunat",
                            "respuesta_sunat",
                            "fecha_respuesta_sunat",
                        ]
                    )
                    return False
            else:
                print("⚠️  Respuesta no reconocida de SUNAT")
                # Si es no reconocida pero parece transitoria, ya se encoló arriba
                comprobante.estado_sunat = "ENVIADO"
                comprobante.respuesta_sunat = respuesta_sunat
                comprobante.fecha_respuesta_sunat = timezone.now()
                comprobante.save(
                    update_fields=[
                        "estado_sunat",
                        "respuesta_sunat",
                        "fecha_respuesta_sunat",
                    ]
                )
                return True
        except Exception as exc:
            logger.error("Error procesando respuesta SUNAT: %s", exc)
            comprobante.estado_sunat = "ERROR"
            comprobante.respuesta_sunat = f"Error procesando respuesta: {exc}"
            comprobante.save(update_fields=["estado_sunat", "respuesta_sunat"])
            return False

    # -------------------- Auxiliares de reintento ---------------------
    def _is_transient_fault(self, response_text: str) -> bool:
        """Heurística para detectar fallas transitorias (servicio no disponible, timeouts, auth temporal)."""
        if not response_text:
            return False
        text = response_text.lower()
        transient_markers = [
            'servicio de autenticación no está disponible',
            'autenticación no está disponible',
            'no puede responder su solicitud',
            'temporarily unavailable',
            'service unavailable',
            'read timed out',
            'connect timeout',
            'connection refused',
            'java.net.connectexception',
            'socket timeout',
            'http 503',
            'faultcode>soap-env:client.0109',
            'client.0109',
            '0109',
            'name resolution error',
            'failed to resolve',
            'getaddrinfo failed',
            'max retries exceeded',
            'dns',
        ]
        return any(marker in text for marker in transient_markers)

    def _queue_retry(self, comprobante, respuesta_sunat: str):
        """Encola un reintento con backoff sin modificar el flujo de envío principal."""
        try:
            from .models import SUNATEnvioQueue  # type: ignore
        except Exception:
            return

        # Evitar duplicados: si ya hay un pendiente/enviando con mismo ZIP o cbc_id, no crear otro
        zip_nombre = getattr(self, '_last_zip_name', '') or (
            f"{getattr(comprobante, 'emisor_ruc', '')}-{getattr(comprobante, 'tipo_comprobante', '')}-"
            f"{getattr(comprobante, 'serie', '')}-{int(getattr(comprobante, 'numero', 0)):08d}.zip"
        )
        cbc_id = getattr(self, '_last_cbc_id', '') or f"{comprobante.serie}-{comprobante.numero:08d}"

        try:
            existing = SUNATEnvioQueue.objects.filter(
                cbc_id=cbc_id, estado__in=['PENDIENTE','ENVIANDO']
            ).first()
            if existing:
                # Ya existe. Solo refrescar próxima fecha si está muy lejos
                if not existing.next_retry_at:
                    existing.schedule_next_retry()
                    existing.save(update_fields=['next_retry_at'])
                return
        except Exception:
            pass

        # Preparar payload: si no tenemos el b64 del último ZIP, reconstruir desde xml_firmado
        zip_b64 = getattr(self, '_last_zip_b64', '')
        xml_sha1 = getattr(self, '_last_xml_sha1', '')
        if not zip_b64 or not xml_sha1:
            # reconstruir
            try:
                # Si no hay xml_firmado, firmar de nuevo a partir de xml_generado
                xml_src = comprobante.xml_firmado or comprobante.xml_generado or ''
                if not xml_src and hasattr(comprobante, 'generar_xml'):
                    try:
                        xml_src = comprobante.generar_xml()
                    except Exception:
                        xml_src = ''
                content_b64, _zip_name = self._zip_and_encode(xml_src or '', comprobante)
                zip_b64 = content_b64
                zip_nombre = _zip_name
                import hashlib as _h
                xml_sha1 = _h.sha1((xml_src or '').encode('utf-8')).hexdigest()
            except Exception:
                return

        # Crear entrada
        entry = SUNATEnvioQueue(
            comprobante=comprobante,
            cbc_id=cbc_id,
            emisor_ruc=getattr(self, '_last_emisor_ruc', '') or comprobante.emisor_ruc,
            tipo=getattr(self, '_last_tipo', '') or comprobante.tipo_comprobante,
            serie=comprobante.serie,
            numero=comprobante.numero,
            zip_nombre=zip_nombre,
            zip_base64=zip_b64,
            xml_sha1=xml_sha1,
            estado='PENDIENTE',
            intentos=0,
            max_intentos=5,
            fault_string=respuesta_sunat[:1000] if respuesta_sunat else '',
        )
        entry.schedule_next_retry()
        entry.save()
        print(f"🕒 Envío encolado para reintento: {entry.zip_nombre} a las {entry.next_retry_at}")

    def reenviar_zip_b64(self, zip_b64: str, zip_name: str) -> str:
        """Envía un ZIP ya preparado (Base64) usando sendBill; retorna el cuerpo SOAP."""
        soap_envelope = self._crear_soap_envelope(zip_b64, zip_name)
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": '"urn:sendBill"',
        }
        endpoint = self.url_base
        try:
            resp = requests.post(endpoint, data=soap_envelope.encode('utf-8'), headers=headers, timeout=60)
            print(f"📥 Reenvío sendBill HTTP {resp.status_code}")
            return resp.text
        except Exception as exc:
            print(f"❌ Error reenviando ZIP a SUNAT: {exc}")
            return ""


# ----------------------------------------------------------------------
# Servicio auxiliar – creación de comprobantes desde ventas POS
# (Mantiene la funcionalidad existente; no se modifica la lógica interna)
# ----------------------------------------------------------------------
class ComprobanteElectronicoService:
    """Servicio para crear y gestionar comprobantes electrónicos"""
    
    @staticmethod
    def crear_desde_venta_pos(
        venta_pos,
        configuracion_sunat,
        tipo: str | None = None,
        receptor_dni: str | None = None,
        receptor_nombre: str | None = None,
        receptor_ruc: str | None = None,
        receptor_razon_social: str | None = None,
        receptor_direccion: str | None = None,
    ):
        from .models import ComprobanteElectronico, DetalleComprobanteElectronico  # type: ignore

        # Normalizar tipo: '01' Factura, '03' Boleta (por defecto)
        tipo_comprobante = (tipo or "03").strip()
        if tipo_comprobante not in ("01", "03"):
            tipo_comprobante = "03"

        # Serie y numeración según tipo
        siguiente_numero = configuracion_sunat.get_siguiente_numero(tipo_comprobante)
        serie = (
            configuracion_sunat.serie_boleta if tipo_comprobante == "03" else configuracion_sunat.serie_factura
        )

        # Determinar datos del receptor (cliente)
        if tipo_comprobante == "01":  # Factura: requiere RUC y razón social
            numero_documento = (receptor_ruc or "").strip()
            razon_social = (receptor_razon_social or "").strip()
            if not (numero_documento.isdigit() and len(numero_documento) == 11):
                raise ValueError("El RUC del cliente es obligatorio y debe tener 11 dígitos para emitir factura")
            if not razon_social:
                raise ValueError("La Razón Social del cliente es obligatoria para emitir factura")
            tipo_doc_receptor = "6"  # RUC
            nombre_receptor = razon_social
        else:  # Boleta
            dni_cliente = (receptor_dni or venta_pos.cliente_dni or "").strip()
            nombre_cliente = (
                (receptor_nombre or venta_pos.cliente_nombre or "CLIENTE GENERAL").strip()
                or "CLIENTE GENERAL"
            )
            tipo_doc_receptor = "1"
            numero_documento = dni_cliente if dni_cliente else "00000000"
            nombre_receptor = nombre_cliente

        comprobante = ComprobanteElectronico.objects.create(
            tipo_comprobante=tipo_comprobante,
            serie=serie,
            numero=siguiente_numero,
            emisor_ruc=configuracion_sunat.emisor_ruc or "",
            emisor_razon_social=configuracion_sunat.emisor_razon_social or "",
            emisor_direccion=configuracion_sunat.emisor_direccion or "",
            emisor_ubigeo=configuracion_sunat.emisor_ubigeo or "",
            receptor_tipo_documento=tipo_doc_receptor,
            receptor_numero_documento=numero_documento,
            receptor_razon_social=nombre_receptor,
            receptor_direccion=(receptor_direccion or None),
            subtotal=venta_pos.subtotal,
            igv=venta_pos.igv,
            total=venta_pos.total,
            venta_pos=venta_pos,
        )
        
        for detalle_pos in venta_pos.detalles.all():
            DetalleComprobanteElectronico.objects.create(
                comprobante=comprobante,
                descripcion=detalle_pos.descripcion,
                cantidad=detalle_pos.cantidad,
                unidad_medida='NIU',
                precio_unitario=detalle_pos.precio_unitario,
                subtotal=detalle_pos.subtotal,
                igv=detalle_pos.subtotal * Decimal("0.18"),
                detalle_pos=detalle_pos,
            )
        
        return comprobante
    
    @staticmethod
    def crear_nota_credito(
        comprobante_original,
        configuracion_sunat,
        tipo_nota: str = '01',
        motivo: str | None = None,
    ):
        """Crea una Nota de Crédito (07) que anula/devuelve el total del comprobante original.

        Requisitos SUNAT: el documento modificado debe ser boleta (03) o factura (01)
        con CDR ACEPTADO. La serie de la NC es la misma del documento modificado y
        lleva numeración propia.
        """
        from .models import ComprobanteElectronico, DetalleComprobanteElectronico  # type: ignore

        MOTIVOS_CATALOGO_09 = {
            '01': 'Anulación de la operación',
            '02': 'Anulación por error en el RUC',
            '03': 'Corrección por error en la descripción',
            '04': 'Descuento global',
            '05': 'Descuento por ítem',
            '06': 'Devolución total',
            '07': 'Devolución por ítem',
        }

        if comprobante_original.tipo_comprobante not in ('01', '03'):
            raise ValueError("Solo se puede emitir NC sobre boletas (03) o facturas (01)")
        if comprobante_original.estado_sunat != 'ACEPTADO':
            raise ValueError(
                "El comprobante debe estar ACEPTADO por SUNAT para emitir una Nota de Crédito "
                f"(estado actual: {comprobante_original.estado_sunat})"
            )

        tipo_nota = (tipo_nota or '01').strip().zfill(2)
        if tipo_nota not in MOTIVOS_CATALOGO_09:
            raise ValueError(f"Tipo de nota inválido (Catálogo 09): {tipo_nota}")

        # Evitar NC duplicadas sobre el mismo documento
        nc_existente = comprobante_original.notas_asociadas.filter(
            tipo_comprobante='07'
        ).exclude(estado_sunat='RECHAZADO').first()
        if nc_existente:
            raise ValueError(
                f"Ya existe la Nota de Crédito {nc_existente.numero_completo} "
                f"({nc_existente.estado_sunat}) para este comprobante"
            )

        serie = (
            configuracion_sunat.serie_boleta
            if comprobante_original.tipo_comprobante == '03'
            else configuracion_sunat.serie_factura
        )
        numero = configuracion_sunat.get_siguiente_numero_nota(comprobante_original.tipo_comprobante)

        nota = ComprobanteElectronico.objects.create(
            tipo_comprobante='07',
            serie=serie,
            numero=numero,
            emisor_ruc=comprobante_original.emisor_ruc,
            emisor_razon_social=comprobante_original.emisor_razon_social,
            emisor_direccion=comprobante_original.emisor_direccion,
            emisor_ubigeo=comprobante_original.emisor_ubigeo,
            receptor_tipo_documento=comprobante_original.receptor_tipo_documento,
            receptor_numero_documento=comprobante_original.receptor_numero_documento,
            receptor_razon_social=comprobante_original.receptor_razon_social,
            receptor_direccion=comprobante_original.receptor_direccion,
            subtotal=comprobante_original.subtotal,
            igv=comprobante_original.igv,
            total=comprobante_original.total,
            venta_pos=comprobante_original.venta_pos,
            documento_modificado=comprobante_original,
            tipo_nota=tipo_nota,
            motivo_nota=(motivo or MOTIVOS_CATALOGO_09[tipo_nota]).strip()[:250],
            moneda=comprobante_original.moneda,
        )

        for det in comprobante_original.detalles.all():
            DetalleComprobanteElectronico.objects.create(
                comprobante=nota,
                descripcion=det.descripcion,
                cantidad=det.cantidad,
                unidad_medida=det.unidad_medida,
                precio_unitario=det.precio_unitario,
                subtotal=det.subtotal,
                igv=det.igv,
                detalle_pos=det.detalle_pos,
            )

        return nota

    @staticmethod
    def enviar_a_sunat(comprobante):
        from .models import ConfiguracionSUNAT  # type: ignore

        configuracion = ConfiguracionSUNAT.objects.filter(activo=True).first()
        if not configuracion:
            raise ValueError("No hay configuración SUNAT activa")

        sunat_service = SUNATService(configuracion)
        return sunat_service.enviar_comprobante(comprobante)
        