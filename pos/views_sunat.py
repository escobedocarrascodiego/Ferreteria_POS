"""
Vistas de facturacion electronica: emitir, enviar a SUNAT, notas de
credito, bajas, cola de reintentos, PDF y consultas DNI/RUC.

Vienen de AdminPOS (engine/views.py). Se quitaron los filtros por sede y
la configuracion SUNAT pasa a ser unica.
"""
from datetime import datetime, time, timedelta

import requests
from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import models
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.generic import DetailView, ListView
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import (
    ComprobanteElectronico,
    ConfiguracionSUNAT,
    SUNATEnvioQueue,
    Venta,
)


class ComprobantesListView(LoginRequiredMixin, ListView):
    """Listado de comprobantes electrónicos (boletas y facturas) con filtros"""
    model = ComprobanteElectronico
    template_name = 'pos/comprobantes_list.html'
    context_object_name = 'comprobantes'
    paginate_by = 20

    def get_queryset(self):
        queryset = super().get_queryset().select_related('venta_pos')

        # Filtros
        fecha_inicio = self.request.GET.get('fecha_inicio')
        fecha_fin = self.request.GET.get('fecha_fin')
        tipo = self.request.GET.get('tipo')
        estado = self.request.GET.get('estado')
        serie = (self.request.GET.get('serie') or '').strip()
        numero = (self.request.GET.get('numero') or '').strip()
        cliente = (self.request.GET.get('cliente') or '').strip()

        if fecha_inicio:
            try:
                from datetime import datetime
                fi = datetime.strptime(fecha_inicio, '%Y-%m-%d').date()
                queryset = queryset.filter(
                    fecha_emision__gte=timezone.make_aware(datetime.combine(fi, time.min)))
            except (ValueError, TypeError):
                pass
        if fecha_fin:
            try:
                from datetime import datetime
                ff = datetime.strptime(fecha_fin, '%Y-%m-%d').date()
                queryset = queryset.filter(
                    fecha_emision__lt=timezone.make_aware(datetime.combine(ff, time.min))
                    + timedelta(days=1))
            except (ValueError, TypeError):
                pass
        if tipo in {'01', '03', '07', '08'}:
            queryset = queryset.filter(tipo_comprobante=tipo)
        if estado in {'PENDIENTE', 'ENVIADO', 'ACEPTADO', 'RECHAZADO', 'ANULADO'}:
            queryset = queryset.filter(estado_sunat=estado)
        if serie:
            queryset = queryset.filter(serie__iexact=serie)
        if numero.isdigit():
            queryset = queryset.filter(numero=int(numero))
        if cliente:
            queryset = queryset.filter(
                Q(receptor_razon_social__icontains=cliente) |
                Q(receptor_numero_documento__icontains=cliente)
            )

        return queryset.order_by('-fecha_emision')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['estado_sunat_choices'] = getattr(ComprobanteElectronico, 'ESTADOS_SUNAT', [])

        # Resumen de cabecera: se calcula sobre el filtro aplicado, no sobre
        # la pagina visible, para que los numeros signifiquen algo.
        qs = self.get_queryset()
        conteos = dict(qs.values_list('estado_sunat').annotate(n=models.Count('id')))
        context['resumen_comprobantes'] = {
            'total': sum(conteos.values()),
            'aceptados': conteos.get('ACEPTADO', 0),
            'pendientes': conteos.get('PENDIENTE', 0) + conteos.get('ENVIADO', 0),
            'rechazados': conteos.get('RECHAZADO', 0),
            'anulados': conteos.get('ANULADO', 0),
            'importe': qs.filter(estado_sunat='ACEPTADO').aggregate(
                t=models.Sum('total'))['t'] or 0,
        }
        context['en_cola'] = SUNATEnvioQueue.objects.filter(estado='PENDIENTE').count()
        return context


class ComprobanteElectronicoView(LoginRequiredMixin, DetailView):
    """Vista para mostrar un comprobante electrónico"""
    model = ComprobanteElectronico
    template_name = 'pos/boleta_electronica.html'
    context_object_name = 'comprobante'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['sede_logo'] = None   # la version simple no maneja logo
        return context

@api_view(['GET'])
def consulta_dni_api(request):
    """Consulta DNI en apis.net.pe y devuelve nombre. Requiere query param 'dni' (8 dígitos)."""
    dni = (request.GET.get('dni') or '').strip()
    if not dni or not dni.isdigit() or len(dni) != 8:
        return Response({'error': 'DNI inválido (8 dígitos)'}, status=400)
    token = getattr(settings, 'APIS_NET_PE_TOKEN', '')
    if not token:
        return Response({'error': 'Token de API no configurado'}, status=500)
    try:
        url = f'https://api.apis.net.pe/v2/reniec/dni?numero={dni}'
        headers = {
            'Referer': 'https://apis.net.pe/consulta-dni-api',
            'Authorization': f'Bearer {token}'
        }
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            if resp.status_code == 404:
                return Response({'error': 'No se encontró información para ese DNI.'}, status=404)
            if resp.status_code == 422:
                return Response({'error': 'DNI inválido. Debe tener 8 dígitos.'}, status=400)
            return Response({'error': 'Servicio de consulta DNI no disponible. Intente nuevamente.'}, status=502)
        data = resp.json()
        # La API suele retornar nombres y apellidos por separado
        nombre_completo = None
        if isinstance(data, dict):
            nombres = data.get('nombres') or ''
            apell_pat = data.get('apellidoPaterno') or ''
            apell_mat = data.get('apellidoMaterno') or ''
            if any([nombres, apell_pat, apell_mat]):
                nombre_completo = ' '.join([nombres, apell_pat, apell_mat]).strip()
            # A veces la API retorna 'nombre' ya concatenado
            nombre_completo = data.get('nombre') or nombre_completo
        return Response({'dni': dni, 'nombre': nombre_completo, 'raw': data})
    except requests.Timeout:
        return Response({'error': 'Timeout consultando API'}, status=504)
    except Exception as e:
        return Response({'error': str(e)}, status=500)

@api_view(['GET'])
def consulta_ruc_api(request):
    """Consulta RUC en apis.net.pe y devuelve razón social y dirección. Requiere 'ruc' (11 dígitos)."""
    ruc = (request.GET.get('ruc') or '').strip()
    if not ruc or not ruc.isdigit() or len(ruc) != 11:
        return Response({'error': 'RUC inválido (11 dígitos)'}, status=400)
    token = getattr(settings, 'APIS_NET_PE_TOKEN', '')
    try:
        url = f'https://api.apis.net.pe/v1/ruc?numero={ruc}'
        headers = {'Referer': 'https://apis.net.pe/api-ruc'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            if resp.status_code == 404:
                return Response({'error': 'No se encontró información para ese RUC.'}, status=404)
            if resp.status_code == 422:
                return Response({'error': 'RUC inválido. Debe tener 11 dígitos.'}, status=400)
            return Response({'error': 'Servicio de consulta RUC no disponible. Intente nuevamente.'}, status=502)
        data = resp.json()
        razon_social = None
        direccion = None
        if isinstance(data, dict):
            razon_social = data.get('razonSocial') or data.get('nombre')
            direccion = data.get('direccion') or data.get('domicilioFiscal')
        return Response({'ruc': ruc, 'razon_social': razon_social, 'direccion': direccion, 'raw': data})
    except requests.Timeout:
        return Response({'error': 'Timeout consultando API'}, status=504)
    except Exception as e:
        return Response({'error': str(e)}, status=500)

@api_view(['POST'])
def crear_comprobante_electronico_api(request):
    """API para crear un comprobante electrónico desde una venta POS"""
    try:
        venta_id = request.data.get('venta_id')
        receptor_dni = (request.data.get('receptor_dni') or '').strip()
        receptor_nombre = (request.data.get('receptor_nombre') or '').strip()
        
        if not venta_id:
            return Response({'error': 'ID de venta requerido'}, status=400)
        
        # Obtener venta POS
        try:
            venta_pos = Venta.objects.get(id=venta_id)
        except Venta.DoesNotExist:
            return Response({'error': 'Venta no encontrada'}, status=404)
        
        
        # Obtener configuración SUNAT
        configuracion_sunat = ConfiguracionSUNAT.objects.filter(activo=True).first()
        
        if not configuracion_sunat:
            return Response({
                'error': 'No hay configuración SUNAT activa. Configure los datos de SUNAT primero.'
            }, status=400)
        
        # Verificar si ya existe un comprobante para esta venta
        comprobante_existente = ComprobanteElectronico.objects.filter(venta_pos=venta_pos).first()
        if comprobante_existente:
            return Response({
                'error': 'Ya existe un comprobante electrónico para esta venta',
                'comprobante_id': str(comprobante_existente.id)
            }, status=400)
        
        # Crear comprobante electrónico
        from .sunat_service import ComprobanteElectronicoService
        # Permitir emitir Factura ('01') u optar por Boleta ('03' por defecto)
        tipo = (request.data.get('tipo') or '').strip() or '03'

        # Datos posibles para factura
        receptor_ruc = (request.data.get('receptor_ruc') or '').strip()
        receptor_razon_social = (request.data.get('receptor_razon_social') or '').strip()
        receptor_direccion = (request.data.get('receptor_direccion') or '').strip()

        comprobante = ComprobanteElectronicoService.crear_desde_venta_pos(
            venta_pos,
            configuracion_sunat,
            tipo=tipo,
            receptor_dni=receptor_dni if receptor_dni else None,
            receptor_nombre=receptor_nombre if receptor_nombre else None,
            receptor_ruc=receptor_ruc if receptor_ruc else None,
            receptor_razon_social=receptor_razon_social if receptor_razon_social else None,
            receptor_direccion=receptor_direccion if receptor_direccion else None,
        )
        
        return Response({
            'success': True,
            'comprobante': {
                'id': str(comprobante.id),
                'numero_completo': comprobante.numero_completo,
                'tipo_comprobante': comprobante.get_tipo_comprobante_display(),
                'estado_sunat': comprobante.get_estado_sunat_display(),
                'total': float(comprobante.total)
            }
        })
        
    except Exception as e:
        return Response({'error': str(e)}, status=400)

def _motivo_del_fallo(comprobante):
    """
    Explica por que no salio el envio.

    El servicio marca RECHAZADO tambien cuando el fallo es local (certificado,
    clave, configuracion) y el comprobante nunca llego a SUNAT. Sin este
    detalle, en pantalla todo se ve como "rechazado por SUNAT".
    """
    import re

    comprobante.refresh_from_db()
    texto = (comprobante.respuesta_sunat or '').strip()

    if 'Invalid password or PKCS12 data' in texto:
        return ('No se pudo abrir el certificado digital: la clave del certificado no '
                'coincide con el archivo .p12/.pfx cargado. El comprobante NO llegó a '
                'SUNAT. Corrige la clave en Administración > Configuraciones SUNAT y '
                'reenvíalo desde Comprobantes.')
    if 'No se encontró el certificado digital' in texto:
        return ('Falta el archivo del certificado digital. Súbelo en Administración > '
                'Configuraciones SUNAT. El comprobante NO llegó a SUNAT.')

    # Respuesta SOAP de SUNAT: lo util es el faultstring / codigo
    falla = re.search(r'<faultstring[^>]*>(.*?)</faultstring>', texto, flags=re.S)
    codigo = re.search(r'<faultcode[^>]*>(.*?)</faultcode>', texto, flags=re.S)
    if falla:
        detalle = falla.group(1).strip()
        if codigo:
            detalle = f'{codigo.group(1).strip()}: {detalle}'
        return f'SUNAT respondió: {detalle[:300]}'

    if texto and '<' not in texto:
        return texto.splitlines()[-1][:300]
    return 'Error al enviar comprobante a SUNAT. Revisa el detalle en Comprobantes.'


@api_view(['POST'])
def enviar_comprobante_sunat_api(request):
    """API para enviar un comprobante a SUNAT"""
    try:
        comprobante_id = request.data.get('comprobante_id')
        
        if not comprobante_id:
            return Response({'error': 'ID de comprobante requerido'}, status=400)
        
        # Obtener comprobante
        try:
            comprobante = ComprobanteElectronico.objects.get(id=comprobante_id)
        except ComprobanteElectronico.DoesNotExist:
            return Response({'error': 'Comprobante no encontrado'}, status=404)
        
        
        # Enviar a SUNAT
        from .sunat_service import ComprobanteElectronicoService
        resultado = ComprobanteElectronicoService.enviar_a_sunat(comprobante)
        
        if resultado:
            return Response({
                'success': True,
                'aceptado': comprobante.estado_sunat == 'ACEPTADO',
                'mensaje': 'Comprobante enviado a SUNAT exitosamente',
                'estado_sunat': comprobante.get_estado_sunat_display()
            })
        else:
            return Response({
                'success': False,
                'aceptado': False,
                'error': _motivo_del_fallo(comprobante),
                'estado_sunat': comprobante.get_estado_sunat_display()
            }, status=400)
        
    except Exception as e:
        return Response({'error': str(e)}, status=400)

@api_view(['POST'])
def crear_nota_credito_api(request):
    """Crea una Nota de Crédito (07) sobre un comprobante ACEPTADO y la envía a SUNAT."""
    try:
        comprobante_id = request.data.get('comprobante_id')
        tipo_nota = (request.data.get('tipo_nota') or '01').strip()
        motivo = (request.data.get('motivo') or '').strip() or None

        if not comprobante_id:
            return Response({'error': 'ID de comprobante requerido'}, status=400)

        try:
            original = ComprobanteElectronico.objects.get(id=comprobante_id)
        except ComprobanteElectronico.DoesNotExist:
            return Response({'error': 'Comprobante no encontrado'}, status=404)

        configuracion = ConfiguracionSUNAT.objects.filter(activo=True).first()
        if not configuracion:
            return Response({'error': 'No hay configuración SUNAT activa'}, status=400)

        from .sunat_service import ComprobanteElectronicoService, SUNATService
        nota = ComprobanteElectronicoService.crear_nota_credito(
            original, configuracion, tipo_nota=tipo_nota, motivo=motivo
        )

        enviado = SUNATService(configuracion).enviar_comprobante(nota)

        return Response({
            'success': True,
            'enviado': bool(enviado),
            'nota_id': str(nota.id),
            'numero_completo': nota.numero_completo,
            'estado_sunat': nota.get_estado_sunat_display(),
            'mensaje': (
                f'Nota de Crédito {nota.numero_completo} '
                + ('aceptada por SUNAT' if enviado else f'creada (estado: {nota.get_estado_sunat_display()})')
            ),
        })

    except ValueError as e:
        return Response({'error': str(e)}, status=400)
    except Exception as e:
        return Response({'error': str(e)}, status=400)


@api_view(['POST'])
def anular_boleta_sunat_api(request):
    """
    Anula boleta (tipo 03):
      - Si la fecha de emisión (hora local Lima) es HOY → RC (Resumen Diario)
      - Si es de fecha ANTERIOR → RA (Comunicación de Baja)
    Body:
      comprobante_id (uuid/int) [obligatorio]
      modo?: 'AUTO'|'RC'|'RA' (por defecto 'AUTO')
      motivo?: str (solo aplica si resulta 'RA'; si no llega, se usa uno genérico)
    """
    try:
        comprobante_id = request.data.get('comprobante_id')
        modo = (request.data.get('modo') or 'AUTO').upper()
        motivo = (request.data.get('motivo') or '').strip()

        if not comprobante_id:
            return Response({'error': 'ID de comprobante requerido'}, status=400)

        from .models import ComprobanteElectronico, ConfiguracionSUNAT
        try:
            comprobante = ComprobanteElectronico.objects.get(id=comprobante_id)
        except ComprobanteElectronico.DoesNotExist:
            return Response({'error': 'Comprobante no encontrado'}, status=404)

        # Solo boletas
        if comprobante.tipo_comprobante != '03':
            return Response({'error': 'Solo boletas (03) pueden anularse con RC/RA aquí.'}, status=400)


        # Config SUNAT
        configuracion = ConfiguracionSUNAT.objects.filter(activo=True).first()
        if not configuracion:
            return Response({'error': 'No hay configuración SUNAT activa'}, status=400)

        # Fechas en hora local Lima
        from django.utils import timezone
        from django.utils.timezone import is_aware, localtime
        em_dt = comprobante.fecha_emision
        em_local = localtime(em_dt) if is_aware(em_dt) else em_dt
        fecha_doc_local = em_local.date()
        hoy_local = timezone.localdate()

        # Resolver modo (boletas 03 se anulan SOLO con RC, incluso fuera de fecha)
        if modo == 'AUTO':
            modo_resuelto = 'RC'
        elif modo == 'RC':
            modo_resuelto = 'RC'
        elif modo == 'RA':
            return Response({'error': 'Para boletas (03) use Resumen Diario (RC); RA no aplica.'}, status=400)
        else:
            return Response({'error': "Valor de 'modo' inválido. Use 'AUTO'|'RC'|'RA'."}, status=400)

        # Enviar a SUNAT
        from .sunat_service import SUNATService
        service = SUNATService(configuracion)

        if modo_resuelto == 'RC':
            ticket = service.generar_resumen_diario_baja([comprobante])
        else:
            if not motivo:
                motivo = 'Anulación de boleta fuera de fecha (Comunicación de Baja).'
            ticket = service.generar_comunicacion_baja([comprobante], motivo=motivo)

        # Log y estado
        resumen_id = getattr(service, 'last_resumen_id', None)
        log_line = f"{modo_resuelto}_ENVIADO ticket:{ticket}"
        if resumen_id:
            log_line += f" resumen_id:{resumen_id}"
        if modo_resuelto == 'RA':
            log_line += f" motivo:{motivo}"

        comprobante.respuesta_sunat = (comprobante.respuesta_sunat or '') + f"\n{log_line}"
        comprobante.estado_sunat = 'ENVIADO'  # en proceso
        comprobante.fecha_respuesta_sunat = timezone.now()
        comprobante.save(update_fields=['estado_sunat', 'respuesta_sunat', 'fecha_respuesta_sunat'])

        return Response({
            'success': True,
            'modo': modo_resuelto,
            'ticket': ticket,
            'estado': comprobante.get_estado_sunat_display(),
            'emision_local': str(fecha_doc_local),
            'hoy_local': str(hoy_local),
        })

    except Exception as e:
        return Response({'error': str(e)}, status=400)


@api_view(['POST'])
def anular_factura_sunat_api(request):
    """
    Anula factura (tipo 01) mediante Comunicación de Baja (RA) siempre.
    Body:
      comprobante_id (uuid/int) [obligatorio]
      motivo?: str (opcional, se usa genérico si falta)
    """
    try:
        comprobante_id = request.data.get('comprobante_id')
        motivo = (request.data.get('motivo') or '').strip()

        if not comprobante_id:
            return Response({'error': 'ID de comprobante requerido'}, status=400)

        from .models import ComprobanteElectronico, ConfiguracionSUNAT
        try:
            comprobante = ComprobanteElectronico.objects.get(id=comprobante_id)
        except ComprobanteElectronico.DoesNotExist:
            return Response({'error': 'Comprobante no encontrado'}, status=404)

        # Solo facturas
        if comprobante.tipo_comprobante != '01':
            return Response({'error': 'Solo facturas (01) pueden anularse aquí.'}, status=400)


        # Config SUNAT
        configuracion = ConfiguracionSUNAT.objects.filter(activo=True).first()
        if not configuracion:
            return Response({'error': 'No hay configuración SUNAT activa'}, status=400)

        from django.utils import timezone
        from .sunat_service import SUNATService
        service = SUNATService(configuracion)

        if not motivo:
            motivo = 'Anulación de factura (Comunicación de Baja).'
        ticket = service.generar_comunicacion_baja_factura([comprobante], motivo=motivo)

        # Log y estado
        resumen_id = getattr(service, 'last_resumen_id', None)
        log_line = f"RA_ENVIADO ticket:{ticket}"
        if resumen_id:
            log_line += f" resumen_id:{resumen_id}"
        log_line += f" motivo:{motivo}"

        comprobante.respuesta_sunat = (comprobante.respuesta_sunat or '') + f"\n{log_line}"
        comprobante.estado_sunat = 'ENVIADO'
        comprobante.fecha_respuesta_sunat = timezone.now()
        comprobante.save(update_fields=['estado_sunat', 'respuesta_sunat', 'fecha_respuesta_sunat'])

        return Response({
            'success': True,
            'modo': 'RA',
            'ticket': ticket,
            'estado': comprobante.get_estado_sunat_display(),
        })

    except Exception as e:
        return Response({'error': str(e)}, status=400)

        
@api_view(['POST'])
def consultar_ticket_baja_api(request):
    """API para consultar ticket de baja (RC). Si SUNAT responde 0, marca ANULADO; 98 = en proceso."""
    try:
        comprobante_id = request.data.get('comprobante_id')
        ticket = request.data.get('ticket')
        if not comprobante_id:
            return Response({'error': 'comprobante_id es requerido'}, status=400)

        from .models import ComprobanteElectronico, ConfiguracionSUNAT
        try:
            comprobante = ComprobanteElectronico.objects.get(id=comprobante_id)
        except ComprobanteElectronico.DoesNotExist:
            return Response({'error': 'Comprobante no encontrado'}, status=404)


        # Config
        configuracion = ConfiguracionSUNAT.objects.filter(activo=True).first()
        if not configuracion:
            return Response({'error': 'No hay configuración SUNAT activa'}, status=400)

        from .sunat_service import SUNATService
        service = SUNATService(configuracion)

        # Ticket: si no viene, lo extraemos de respuesta_sunat
        if not ticket:
            txt = comprobante.respuesta_sunat or ''
            idx = txt.rfind('ticket:')
            if idx != -1:
                ticket = ''.join(ch for ch in txt[idx+7:idx+7+30] if ch.isdigit())
        if not ticket:
            return Response({'error': 'No se pudo determinar el ticket para este comprobante'}, status=400)

        code, cdr_xml = service.consultar_ticket_resumen(ticket)

        # Persistencia
        if cdr_xml:
            comprobante.cdr_sunat = cdr_xml

        mensaje = None
        if code == '0':
            comprobante.estado_sunat = 'ANULADO'
        elif code == '2987':
            comprobante.estado_sunat = 'ANULADO'
            mensaje = "SUNAT indica que el comprobante ya fue informado y está anulado o rechazado."
        elif code in ('98', None, ''):
            # En proceso
            comprobante.estado_sunat = 'ENVIADO'
            if not code:
                code = '98'  # evitar "N/A" en el front
        else:
            comprobante.estado_sunat = 'RECHAZADO'
            if code == '2957':
                mensaje = (
                    "SUNAT rechazó la baja porque excede el plazo permitido "
                    "desde la fecha de emisión (7 días)."
                )

        resumen = f"Resultado BAJA ticket:{ticket} code:{code}"
        if mensaje:
            resumen += f" aviso:{mensaje}"
        comprobante.respuesta_sunat = (comprobante.respuesta_sunat or '') + f"\n{resumen}"
        comprobante.fecha_respuesta_sunat = timezone.now()

        fields = ['estado_sunat','respuesta_sunat','fecha_respuesta_sunat']
        if cdr_xml:
            fields.append('cdr_sunat')
        comprobante.save(update_fields=fields)

        payload = {'success': True, 'code': code, 'estado': comprobante.get_estado_sunat_display()}
        if mensaje:
            payload['mensaje'] = mensaje
        return Response(payload)
    except Exception as e:
        return Response({'error': str(e)}, status=400)

@api_view(['GET'])
def obtener_comprobante_api(request):
    """API para obtener detalles de un comprobante electrónico"""
    try:
        comprobante_id = request.GET.get('comprobante_id')
        
        if not comprobante_id:
            return Response({'error': 'ID de comprobante requerido'}, status=400)
        
        # Obtener comprobante
        try:
            comprobante = ComprobanteElectronico.objects.select_related('venta_pos').get(id=comprobante_id)
        except ComprobanteElectronico.DoesNotExist:
            return Response({'error': 'Comprobante no encontrado'}, status=404)
        
        
        # Obtener detalles
        detalles = []
        for detalle in comprobante.detalles.all():
            detalles.append({
                'descripcion': detalle.descripcion,
                'cantidad': float(detalle.cantidad),
                'precio_unitario': float(detalle.precio_unitario),
                'subtotal': float(detalle.subtotal),
                'igv': float(detalle.igv)
            })
        
        return Response({
            'comprobante': {
                'id': str(comprobante.id),
                'numero_completo': comprobante.numero_completo,
                'tipo_comprobante': comprobante.get_tipo_comprobante_display(),
                'fecha_emision': comprobante.fecha_emision.strftime('%d/%m/%Y %H:%M:%S'),
                'estado_sunat': comprobante.get_estado_sunat_display(),
                'emisor_razon_social': comprobante.emisor_razon_social,
                'emisor_ruc': comprobante.emisor_ruc,
                'receptor_razon_social': comprobante.receptor_razon_social,
                'receptor_numero_documento': comprobante.receptor_numero_documento,
                'subtotal': float(comprobante.subtotal),
                'igv': float(comprobante.igv),
                'total': float(comprobante.total)
            },
            'detalles': detalles
        })
        
    except Exception as e:
        return Response({'error': str(e)}, status=400)


@api_view(['POST'])
def procesar_cola_envio_sunat_api(request):
    """Procesa la cola de reintentos de envíos a SUNAT con backoff.

    Solo usuarios superusuarios o con permisos de sede del comprobante podrán ejecutarlo.
    Procesa como máximo 20 elementos por invocación para evitar timeouts.
    """
    try:
        from django.utils import timezone
        from .models import SUNATEnvioQueue, ConfiguracionSUNAT, ComprobanteElectronico
        from .sunat_service import SUNATService

        user = request.user
        if not user.is_authenticated:
            return Response({'error': 'No autenticado'}, status=401)

        ahora = timezone.now()
        # Permitir forzar el procesamiento ignorando next_retry_at (como solicitó UX)
        force = False
        try:
            force = bool(request.data.get('force') or request.POST.get('force'))
        except Exception:
            force = False
        base_qs = SUNATEnvioQueue.objects.filter(estado='PENDIENTE')
        if not force:
            base_qs = base_qs.filter(next_retry_at__lte=ahora)
        # Alinear con el comando por defecto (hasta 50 elementos)
        qs = base_qs.order_by('next_retry_at', 'created_at')[:50]

        procesados = 0
        completados = 0
        reprogramados = 0
        cancelados = 0

        for entry in qs:
            entry.estado = 'ENVIANDO'
            entry.intentos += 1
            entry.save(update_fields=['estado', 'intentos', 'updated_at'])

            # Configuración de la sede del comprobante
            try:
                comprobante = entry.comprobante or ComprobanteElectronico.objects.filter(
                    serie=entry.serie, numero=entry.numero, emisor_ruc=entry.emisor_ruc
                ).first()
                if not comprobante:
                    entry.estado = 'CANCELADO'
                    entry.fault_string = (entry.fault_string or '') + '\nComprobante no encontrado para reintento.'
                    entry.save(update_fields=['estado', 'fault_string', 'updated_at'])
                    cancelados += 1
                    continue
            except Exception:
                entry.estado = 'CANCELADO'
                entry.fault_string = (entry.fault_string or '') + '\nError obteniendo comprobante.'
                entry.save(update_fields=['estado', 'fault_string', 'updated_at'])
                cancelados += 1
                continue

            configuracion = ConfiguracionSUNAT.objects.filter(activo=True).first()
            if not configuracion:
                # No se puede enviar; cancelar
                entry.estado = 'CANCELADO'
                entry.fault_string = (entry.fault_string or '') + '\nSin configuración SUNAT activa.'
                entry.save(update_fields=['estado', 'fault_string', 'updated_at'])
                cancelados += 1
                continue

            service = SUNATService(configuracion)
            # Reenviar ZIP exacto
            response_text = service.reenviar_zip_b64(entry.zip_base64, entry.zip_nombre)

            # Persistir snippet de respuesta
            snippet = (response_text or '')[:1000]
            entry.last_response_snippet = snippet

            # Procesar respuesta con la misma lógica del servicio
            ok = False
            try:
                ok = service._procesar_respuesta(comprobante, response_text)
            except Exception:
                ok = False

            # Si quedó ENVIADO, intentar getStatusCdr para promover a ACEPTADO si ya hay CDR
            if ok and comprobante.estado_sunat == 'ENVIADO':
                try:
                    code, cdr_xml = service.consultar_cdr_por_comprobante(comprobante)
                    if code == '0' and cdr_xml:
                        comprobante.cdr_sunat = cdr_xml
                        comprobante.estado_sunat = 'ACEPTADO'
                        comprobante.fecha_respuesta_sunat = timezone.now()
                        comprobante.save(update_fields=['cdr_sunat','estado_sunat','fecha_respuesta_sunat'])
                        ok = True
                except Exception:
                    pass

            if ok and comprobante.estado_sunat in ('ACEPTADO', 'ENVIADO'):
                entry.estado = 'COMPLETADO'
                entry.save(update_fields=['estado', 'last_response_snippet', 'updated_at'])
                completados += 1
            else:
                # Transitoria? reprogramar si quedan intentos; si no, cancelar
                try:
                    if hasattr(service, '_is_transient_fault') and service._is_transient_fault(response_text) and entry.intentos < entry.max_intentos:
                        entry.estado = 'PENDIENTE'
                        entry.schedule_next_retry()
                        entry.save(update_fields=['estado', 'next_retry_at', 'last_response_snippet', 'updated_at'])
                        reprogramados += 1
                    else:
                        entry.estado = 'CANCELADO'
                        entry.save(update_fields=['estado', 'last_response_snippet', 'updated_at'])
                        cancelados += 1
                except Exception:
                    entry.estado = 'CANCELADO'
                    entry.save(update_fields=['estado', 'last_response_snippet', 'updated_at'])
                    cancelados += 1

            procesados += 1

        # También informar cuántos comprobantes están en estado ENVIADO (sin CDR aceptado)
        enviados_qs = ComprobanteElectronico.objects.filter(estado_sunat='ENVIADO')
        # Y cuántos NO están finalizados (no ACEPTADO/ANULADO)
        no_finalizados_qs = ComprobanteElectronico.objects.exclude(estado_sunat__in=['ACEPTADO','ANULADO'])

        return Response({
            'success': True,
            'procesados': procesados,
            'completados': completados,
            'reprogramados': reprogramados,
            'cancelados': cancelados,
            'pendientes': base_qs.count(),
            'enviados': enviados_qs.count(),
            'no_finalizados': no_finalizados_qs.count(),
        })
    except Exception as e:
        # Si hay un error pero no existen pendientes, devolver success con ceros para UX
        try:
            from .models import SUNATEnvioQueue
            if not SUNATEnvioQueue.objects.filter(estado='PENDIENTE').exists():
                return Response({'success': True, 'procesados': 0, 'completados': 0, 'reprogramados': 0, 'cancelados': 0, 'pendientes': 0})
        except Exception:
            pass
        return Response({'error': str(e)}, status=200)


@api_view(['GET'])
def generar_boleta_pdf(request, comprobante_id):
    """Genera el PDF de la representación impresa (boleta en ticket, factura en A4)"""
    try:
        from .pdf_utils import generar_pdf_boleta
        import os
        from pathlib import Path
        
        # Obtener el comprobante
        comprobante = get_object_or_404(ComprobanteElectronico, pk=comprobante_id)
        
        # Obtener XML firmado y CDR
        xml_firmado = comprobante.xml_firmado
        cdr = comprobante.cdr_sunat if hasattr(comprobante, 'cdr_sunat') else None
        
        # El logo sale de la configuracion SUNAT (en AdminPOS era el de la sede)
        config = ConfiguracionSUNAT.objects.filter(activo=True).first()
        logo_path = config.logo.path if config and config.logo else None

        # Generar PDF (función detecta si es factura o boleta)
        pdf_bytes = generar_pdf_boleta(xml_firmado, cdr, logo_path=logo_path)
        
        # Crear carpeta si no existe
        folder_name = "facturas" if comprobante.tipo_comprobante == '01' else "boletas"
        pdf_folder = Path(settings.COMPROBANTES_ROOT) / folder_name
        pdf_folder.mkdir(parents=True, exist_ok=True)
        
        # Generar nombre del archivo
        filename = f"{comprobante.serie}-{comprobante.numero:08d}.pdf"
        filepath = pdf_folder / filename
        
        # Guardar PDF en la carpeta
        with open(filepath, 'wb') as f:
            f.write(pdf_bytes)
        
        print(f"PDF guardado en: {filepath}")
        
        # Crear respuesta
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        
        return response
        
    except Exception as e:
        print(f"Error generando PDF de boleta: {e}")
        return HttpResponse(f'Error al generar PDF: {str(e)}', status=500)
