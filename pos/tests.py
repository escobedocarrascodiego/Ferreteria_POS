"""
Pruebas del flujo completo: venta con precio digitado -> comprobante
electronico -> firma -> envio a SUNAT -> PDF.

El envio real se sustituye por una respuesta SUNAT simulada (CDR aceptado) y
el certificado es uno autofirmado que se genera aqui mismo, asi que se
ejercita todo menos la conexion con SUNAT.
"""
import base64
import io
import shutil
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone as tz
from decimal import Decimal
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from .models import (
    ComprobanteElectronico,
    ConfiguracionSUNAT,
    Producto,
    Venta,
)

CLAVE_CERT = 'clave-de-prueba'
MEDIA_TMP = tempfile.mkdtemp(prefix='ferreteria_test_')


def certificado_p12():
    llave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'FERRETERIA DE PRUEBA')])
    ahora = datetime.now(tz.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(nombre).issuer_name(nombre)
            .public_key(llave.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(ahora - timedelta(days=1))
            .not_valid_after(ahora + timedelta(days=30))
            .sign(llave, hashes.SHA256()))
    return pkcs12.serialize_key_and_certificates(
        b'prueba', llave, cert, None,
        serialization.BestAvailableEncryption(CLAVE_CERT.encode()))


def respuesta_sunat(codigo='0'):
    """Sobre SOAP como el que devuelve sendBill, con el CDR dentro de un ZIP."""
    cdr = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ar:ApplicationResponse '
        'xmlns:ar="urn:oasis:names:specification:ubl:schema:xsd:ApplicationResponse-2" '
        'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" '
        'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">'
        '<cac:DocumentResponse><cac:Response>'
        f'<cbc:ResponseCode>{codigo}</cbc:ResponseCode>'
        '<cbc:Description>El comprobante ha sido aceptado</cbc:Description>'
        '</cac:Response></cac:DocumentResponse></ar:ApplicationResponse>')
    paquete = io.BytesIO()
    with zipfile.ZipFile(paquete, 'w') as z:
        z.writestr('R-cdr.xml', cdr)
    b64 = base64.b64encode(paquete.getvalue()).decode()

    class Respuesta:
        status_code = 200
        text = (
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
            '<soapenv:Body><br:sendBillResponse xmlns:br="http://service.sunat.gob.pe">'
            f'<applicationResponse>{b64}</applicationResponse>'
            '</br:sendBillResponse></soapenv:Body></soapenv:Envelope>')
    return Respuesta()


@override_settings(MEDIA_ROOT=MEDIA_TMP, COMPROBANTES_ROOT=MEDIA_TMP + '/comprobantes')
class FacturacionTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user('admin_test', password='x', is_staff=True)
        cls.cemento = Producto.objects.create(nombre='CEMENTO YURA')
        cls.config = ConfiguracionSUNAT.objects.create(
            emisor_ruc='20123456789', emisor_razon_social='FERRETERIA DE PRUEBA SAC',
            emisor_direccion='AV. PRINCIPAL 123', emisor_ubigeo='150101',
            usuario_sunat='MODDATOS', password_sunat='moddatos',
            password_certificado=CLAVE_CERT)
        cls.config.certificado_digital.save('prueba.p12', ContentFile(certificado_p12()))

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_TMP, ignore_errors=True)

    def setUp(self):
        self.client.force_login(self.admin)

    # ---------------- ayudas ----------------
    def vender(self, **extra):
        cuerpo = {'metodo_pago': 'EFECTIVO', 'items': [
            {'producto_id': self.cemento.id, 'cantidad': '2.5', 'precio': '28.50'}]}
        cuerpo.update(extra)
        r = self.client.post('/api/venta/', cuerpo, content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        return Venta.objects.get(pk=r.json()['id'])

    def crear(self, venta, **datos):
        return self.client.post('/api/comprobante/crear/', {'venta_id': venta.pk, **datos},
                                content_type='application/json')

    def enviar(self, comprobante_id):
        with patch('pos.sunat_service.requests.post', return_value=respuesta_sunat()):
            return self.client.post('/api/comprobante/enviar-sunat/',
                                    {'comprobante_id': comprobante_id},
                                    content_type='application/json')

    # ---------------- boleta ----------------
    def test_boleta_de_punta_a_punta(self):
        venta = self.vender(cliente_nombre='JUAN PEREZ', cliente_documento='12345678')
        self.assertEqual(venta.total, Decimal('71.25'))
        self.assertEqual(venta.subtotal + venta.igv, venta.total)

        r = self.crear(venta, tipo='03')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['comprobante']['numero_completo'], 'B001-00000001')
        comp_id = r.json()['comprobante']['id']

        r = self.enviar(comp_id)
        self.assertEqual(r.status_code, 200, r.content)

        comp = ComprobanteElectronico.objects.get(pk=comp_id)
        self.assertEqual(comp.estado_sunat, 'ACEPTADO')
        self.assertEqual(comp.receptor_numero_documento, '12345678')
        self.assertEqual(comp.receptor_razon_social, 'JUAN PEREZ')
        # Lo declarado a SUNAT coincide con lo cobrado
        self.assertEqual(comp.total, Decimal('71.25'))
        self.assertEqual(comp.subtotal + comp.igv, comp.total)
        self.assertIn('SignatureValue', comp.xml_firmado)
        self.assertIn('CEMENTO YURA', comp.xml_firmado)
        self.assertIn('ResponseCode', comp.cdr_sunat)

        pdf = self.client.get(f'/api/comprobante/{comp_id}/pdf/')
        self.assertEqual(pdf.status_code, 200, pdf.content[:300])
        self.assertTrue(pdf.content.startswith(b'%PDF'))

    def test_boleta_sin_cliente_va_a_cliente_general(self):
        r = self.crear(self.vender(), tipo='03')
        comp = ComprobanteElectronico.objects.get(pk=r.json()['comprobante']['id'])
        self.assertEqual(comp.receptor_numero_documento, '00000000')
        self.assertEqual(comp.receptor_razon_social, 'CLIENTE GENERAL')

    def test_los_correlativos_avanzan(self):
        primero = self.crear(self.vender(), tipo='03').json()['comprobante']['numero_completo']
        segundo = self.crear(self.vender(), tipo='03').json()['comprobante']['numero_completo']
        self.assertEqual((primero, segundo), ('B001-00000001', 'B001-00000002'))

    # ---------------- factura ----------------
    def test_factura_de_punta_a_punta(self):
        venta = self.vender()
        r = self.crear(venta, tipo='01', receptor_ruc='20100070970',
                       receptor_razon_social='CLIENTE EMPRESA SAC',
                       receptor_direccion='JR. LIMA 456')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['comprobante']['numero_completo'], 'F001-00000001')
        comp_id = r.json()['comprobante']['id']
        self.assertEqual(self.enviar(comp_id).status_code, 200)

        comp = ComprobanteElectronico.objects.get(pk=comp_id)
        self.assertEqual(comp.estado_sunat, 'ACEPTADO')
        self.assertEqual(comp.receptor_tipo_documento, '6')
        pdf = self.client.get(f'/api/comprobante/{comp_id}/pdf/')
        self.assertTrue(pdf.content.startswith(b'%PDF'), pdf.content[:300])

    def test_factura_sin_ruc_se_rechaza_y_no_gasta_numero_de_venta(self):
        r = self.crear(self.vender(), tipo='01', receptor_razon_social='SIN RUC')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(ComprobanteElectronico.objects.exists())

    # ---------------- reglas ----------------
    def test_una_venta_no_admite_dos_comprobantes(self):
        venta = self.vender()
        self.assertEqual(self.crear(venta, tipo='03').status_code, 200)
        self.assertEqual(self.crear(venta, tipo='03').status_code, 400)
        self.assertEqual(ComprobanteElectronico.objects.count(), 1)

    def test_sin_configuracion_sunat_avisa(self):
        ConfiguracionSUNAT.objects.update(activo=False)
        r = self.crear(self.vender(), tipo='03')
        self.assertEqual(r.status_code, 400)
        self.assertIn('configuración SUNAT', r.json()['error'])

    def test_no_se_anula_una_venta_con_comprobante_vigente(self):
        venta = self.vender()
        self.enviar(self.crear(venta, tipo='03').json()['comprobante']['id'])
        self.client.post(f'/ventas/{venta.pk}/anular/')
        venta.refresh_from_db()
        self.assertFalse(venta.anulada)

    def test_clave_de_certificado_mala_se_explica_y_no_se_culpa_a_sunat(self):
        ConfiguracionSUNAT.objects.update(password_certificado='otra-clave')
        comp_id = self.crear(self.vender(), tipo='03').json()['comprobante']['id']
        with patch('pos.sunat_service.requests.post') as post:
            r = self.client.post('/api/comprobante/enviar-sunat/', {'comprobante_id': comp_id},
                                 content_type='application/json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('clave del certificado', r.json()['error'])
        self.assertIn('NO llegó a SUNAT', r.json()['error'])
        post.assert_not_called()

    def test_el_admin_avisa_si_la_clave_no_abre_el_certificado_pero_guarda(self):
        from .admin import problema_del_certificado

        self.assertIsNone(problema_del_certificado(self.config))
        self.config.password_certificado = 'otra-clave'
        self.assertIn('no abre', problema_del_certificado(self.config))

        # Guardar desde el admin no se bloquea: solo sale el aviso
        jefe = User.objects.create_superuser('jefe', password='x')
        self.client.force_login(jefe)
        url = f'/admin/pos/configuracionsunat/{self.config.pk}/change/'
        campos = self.client.get(url).context['adminform'].form.initial
        datos = {k: ('' if v is None else v) for k, v in campos.items()
                 if k not in ('certificado_digital', 'logo')}
        datos['password_certificado'] = 'otra-clave'
        if datos.get('activo'):
            datos['activo'] = 'on'
        r = self.client.post(url, datos, follow=True)
        self.assertContains(r, 'Guardado, pero ojo')
        self.config.refresh_from_db()
        self.assertEqual(self.config.password_certificado, 'otra-clave')

    # ---------------- notas de credito y anulaciones ----------------
    def aceptado(self, tipo='03', **datos):
        comp_id = self.crear(self.vender(), tipo=tipo, **datos).json()['comprobante']['id']
        self.enviar(comp_id)
        return ComprobanteElectronico.objects.get(pk=comp_id)

    def test_nota_de_credito_sobre_boleta(self):
        boleta = self.aceptado()
        with patch('pos.sunat_service.requests.post', return_value=respuesta_sunat()):
            r = self.client.post('/api/comprobante/nota-credito/', {
                'comprobante_id': str(boleta.id), 'tipo_nota': '01', 'motivo': 'Error en el pedido',
            }, content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()['enviado'], r.json())

        nota = ComprobanteElectronico.objects.get(pk=r.json()['nota_id'])
        self.assertEqual(nota.tipo_comprobante, '07')
        self.assertEqual(nota.estado_sunat, 'ACEPTADO')
        self.assertEqual(nota.documento_modificado, boleta)
        self.assertEqual(nota.total, boleta.total)
        self.assertIn('CreditNote', nota.xml_firmado)
        self.assertIn(boleta.numero_completo, nota.xml_firmado)
        # Aparece en el listado y no admite una segunda nota
        self.assertContains(self.client.get('/comprobantes/?tipo=07'), nota.numero_completo)
        r2 = self.client.post('/api/comprobante/nota-credito/', {'comprobante_id': str(boleta.id)},
                              content_type='application/json')
        self.assertEqual(r2.status_code, 400)

    def test_nota_de_credito_sobre_factura(self):
        factura = self.aceptado('01', receptor_ruc='20100070970', receptor_razon_social='EMPRESA SAC')
        with patch('pos.sunat_service.requests.post', return_value=respuesta_sunat()):
            r = self.client.post('/api/comprobante/nota-credito/', {
                'comprobante_id': str(factura.id), 'tipo_nota': '06'}, content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        nota = ComprobanteElectronico.objects.get(pk=r.json()['nota_id'])
        self.assertEqual(nota.estado_sunat, 'ACEPTADO')
        self.assertEqual(nota.serie, 'F001')

    def _anular_y_consultar(self, comprobante, url):
        class ConTicket:
            status_code = 200
            text = ('<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
                    '<soapenv:Body><br:sendSummaryResponse xmlns:br="http://service.sunat.gob.pe">'
                    '<ticket>1700000000001</ticket></br:sendSummaryResponse>'
                    '</soapenv:Body></soapenv:Envelope>')

        class BajaAceptada:
            status_code = 200
            text = ('<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
                    '<soapenv:Body><br:getStatusResponse xmlns:br="http://service.sunat.gob.pe">'
                    '<status><statusCode>0</statusCode></status></br:getStatusResponse>'
                    '</soapenv:Body></soapenv:Envelope>')

        with patch('pos.sunat_service.requests.post', return_value=ConTicket()) as envio:
            r = self.client.post(url, {'comprobante_id': str(comprobante.id), 'modo': 'RC',
                                       'motivo': 'Anulación de la operación'})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['ticket'], '1700000000001')
        resumen = envio.call_args.kwargs['data'].decode()
        self.assertIn('sendSummary', resumen)
        comprobante.refresh_from_db()
        self.assertEqual(comprobante.estado_sunat, 'ENVIADO')     # baja en proceso

        with patch('requests.post', return_value=BajaAceptada()):
            r = self.client.post('/api/comprobante/consultar-baja/',
                                 {'comprobante_id': str(comprobante.id)})
        self.assertEqual(r.status_code, 200, r.content)
        comprobante.refresh_from_db()
        self.assertEqual(comprobante.estado_sunat, 'ANULADO')

    def test_baja_de_boleta_por_resumen_diario(self):
        boleta = self.aceptado()
        self._anular_y_consultar(boleta, '/api/comprobante/anular/')
        # Ya anulado el comprobante, la venta si se puede anular
        self.client.post(f'/ventas/{boleta.venta_pos.pk}/anular/')
        boleta.venta_pos.refresh_from_db()
        self.assertTrue(boleta.venta_pos.anulada)

    def test_baja_de_factura_por_comunicacion_de_baja(self):
        factura = self.aceptado('01', receptor_ruc='20100070970', receptor_razon_social='EMPRESA SAC')
        self._anular_y_consultar(factura, '/api/comprobante/anular-factura/')

    def test_las_acciones_aparecen_en_el_listado(self):
        self.aceptado()
        html = self.client.get('/comprobantes/').content.decode()
        for accion in ('Nota de crédito', 'Anular', 'Descargar PDF'):
            self.assertIn(accion, html)

    def test_el_admin_lista_ventas_y_comprobantes(self):
        self.aceptado()
        self.client.force_login(User.objects.create_superuser('jefe2', password='x'))
        for modelo in ('venta', 'comprobanteelectronico', 'configuracionsunat',
                       'resumendiario', 'sunatenvioqueue'):
            with self.subTest(modelo=modelo):
                self.assertEqual(self.client.get(f'/admin/pos/{modelo}/').status_code, 200)

    def test_las_apis_exigen_sesion(self):
        venta = self.vender()
        self.client.logout()
        self.assertIn(self.crear(venta, tipo='03').status_code, (401, 403))

    # ---------------- pantallas ----------------
    def test_las_pantallas_cargan(self):
        venta = self.vender()
        comp_id = self.crear(venta, tipo='03').json()['comprobante']['id']
        self.enviar(comp_id)
        for url in ('/', '/ventas/', '/comprobantes/', '/productos/',
                    f'/boleta/{venta.pk}/', f'/comprobante/{comp_id}/',
                    '/comprobantes/?fecha_inicio=2020-01-01&fecha_fin=2099-01-01&tipo=03'):
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertEqual(r.status_code, 200)
        self.assertContains(self.client.get('/ventas/'), 'B001-00000001')
        self.assertContains(self.client.get('/comprobantes/'), 'B001-00000001')
