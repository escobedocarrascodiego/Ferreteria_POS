from django.core.management.base import BaseCommand
from django.utils import timezone

from pos.models import SUNATEnvioQueue, ConfiguracionSUNAT, ComprobanteElectronico
from pos.sunat_service import SUNATService


class Command(BaseCommand):
    help = 'Procesa la cola de reintentos de envío a SUNAT (sendBill) con backoff.'

    def add_arguments(self, parser):
        parser.add_argument('--max', type=int, default=50, help='Máximo de elementos a procesar')

    def handle(self, *args, **options):
        max_items = options['max']
        ahora = timezone.now()
        qs = (SUNATEnvioQueue.objects
              .filter(estado='PENDIENTE', next_retry_at__lte=ahora)
              .order_by('next_retry_at', 'created_at')[:max_items])

        procesados = completados = reprogramados = cancelados = 0
        self.stdout.write(self.style.NOTICE(f"Procesando {qs.count()} elementos"))

        for entry in qs:
            entry.estado = 'ENVIANDO'
            entry.intentos += 1
            entry.save(update_fields=['estado', 'intentos', 'updated_at'])

            comp = entry.comprobante or ComprobanteElectronico.objects.filter(
                serie=entry.serie, numero=entry.numero, emisor_ruc=entry.emisor_ruc
            ).first()
            if not comp:
                entry.estado = 'CANCELADO'
                entry.fault_string = (entry.fault_string or '') + '\nComprobante no encontrado para reintento.'
                entry.save(update_fields=['estado', 'fault_string', 'updated_at'])
                cancelados += 1
                continue

            conf = ConfiguracionSUNAT.objects.filter(activo=True).first()
            if not conf:
                entry.estado = 'CANCELADO'
                entry.fault_string = (entry.fault_string or '') + '\nSin configuración SUNAT activa.'
                entry.save(update_fields=['estado', 'fault_string', 'updated_at'])
                cancelados += 1
                continue

            service = SUNATService(conf)
            response_text = service.reenviar_zip_b64(entry.zip_base64, entry.zip_nombre)
            entry.last_response_snippet = (response_text or '')[:1000]

            ok = False
            try:
                ok = service._procesar_respuesta(comp, response_text)
            except Exception:
                ok = False

            # Si quedó ENVIADO, intentamos getStatusCdr para promover a ACEPTADO si ya hay CDR
            if ok and comp.estado_sunat == 'ENVIADO':
                try:
                    code, cdr_xml = service.consultar_cdr_por_comprobante(comp)
                    if code == '0' and cdr_xml:
                        comp.cdr_sunat = cdr_xml
                        comp.estado_sunat = 'ACEPTADO'
                        comp.fecha_respuesta_sunat = timezone.now()
                        comp.save(update_fields=['cdr_sunat','estado_sunat','fecha_respuesta_sunat'])
                        ok = True
                except Exception:
                    pass

            if ok and comp.estado_sunat in ('ACEPTADO', 'ENVIADO'):
                entry.estado = 'COMPLETADO'
                entry.save(update_fields=['estado', 'last_response_snippet', 'updated_at'])
                completados += 1
            else:
                if hasattr(service, '_is_transient_fault') and service._is_transient_fault(response_text) and entry.intentos < entry.max_intentos:
                    entry.estado = 'PENDIENTE'
                    entry.schedule_next_retry()
                    entry.save(update_fields=['estado', 'next_retry_at', 'last_response_snippet', 'updated_at'])
                    reprogramados += 1
                else:
                    entry.estado = 'CANCELADO'
                    entry.save(update_fields=['estado', 'last_response_snippet', 'updated_at'])
                    cancelados += 1

            procesados += 1

        self.stdout.write(self.style.SUCCESS(
            f"Listo. Procesados={procesados} completados={completados} reprogramados={reprogramados} cancelados={cancelados}"
        ))


