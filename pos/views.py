import json
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Producto, Venta, VentaDetalle


# ----------------------------------------------------------------------
# Punto de venta
# ----------------------------------------------------------------------
@login_required
def pos(request):
    # El catalogo es chico (cientos de nombres): se manda entero a la
    # pagina y la busqueda se hace en el navegador, sin esperas.
    catalogo = list(Producto.objects.filter(activo=True)
                    .values('id', 'nombre', 'unidad'))
    return render(request, 'pos/pos.html', {
        'catalogo': catalogo,
        'metodos_pago': Venta.METODOS_PAGO,
    })


class DatoInvalido(Exception):
    pass


def _decimal(valor, campo, fila, decimales):
    try:
        numero = Decimal(str(valor).replace(',', '.').strip())
    except (InvalidOperation, AttributeError):
        raise DatoInvalido(f'Fila {fila}: {campo} no es un número válido.')
    if not numero.is_finite() or numero <= 0:
        raise DatoInvalido(f'Fila {fila}: {campo} debe ser mayor a cero.')
    return numero.quantize(Decimal(1).scaleb(-decimales))


@login_required
@require_POST
def crear_venta(request):
    """
    Recibe la boleta armada en pantalla:

        {"cliente_nombre": "", "cliente_documento": "", "metodo_pago": "EFECTIVO",
         "items": [{"producto_id": 5, "descripcion": "...", "unidad": "UND",
                    "cantidad": "2", "precio": "15.50"}]}
    """
    try:
        datos = json.loads(request.body)
        items = datos.get('items') or []
        if not items:
            raise DatoInvalido('La boleta no tiene productos.')

        metodo = datos.get('metodo_pago') or 'EFECTIVO'
        if metodo not in dict(Venta.METODOS_PAGO):
            raise DatoInvalido('Método de pago no válido.')

        ids = [i.get('producto_id') for i in items if i.get('producto_id')]
        catalogo = Producto.objects.in_bulk(ids)

        detalles = []
        for fila, item in enumerate(items, start=1):
            producto = catalogo.get(item.get('producto_id'))
            descripcion = (producto.nombre if producto
                           else str(item.get('descripcion') or '').strip())
            if not descripcion:
                raise DatoInvalido(f'Fila {fila}: falta la descripción.')

            detalle = VentaDetalle(
                producto=producto,
                descripcion=descripcion[:200],
                unidad=(producto.unidad if producto
                        else str(item.get('unidad') or 'UND').strip()[:20] or 'UND'),
                cantidad=_decimal(item.get('cantidad'), 'la cantidad', fila, 3),
                precio_unitario=_decimal(item.get('precio'), 'el precio', fila, 2),
            )
            detalle.calcular_subtotal()
            detalles.append(detalle)
    except DatoInvalido as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)
    except (ValueError, TypeError, AttributeError):
        return JsonResponse({'ok': False, 'error': 'Datos mal formados.'}, status=400)

    with transaction.atomic():
        venta = Venta.objects.create(
            cliente_nombre=str(datos.get('cliente_nombre') or '').strip()[:200],
            cliente_documento=str(datos.get('cliente_documento') or '').strip()[:11],
            metodo_pago=metodo,
            total=sum(d.subtotal for d in detalles),
            vendedor=request.user,
        )
        for d in detalles:
            d.venta = venta
        VentaDetalle.objects.bulk_create(detalles)

    return JsonResponse({
        'ok': True,
        'id': venta.pk,
        'numero': venta.numero,
        'total': str(venta.total),
        'boleta_url': reverse('boleta', args=[venta.pk]),
    })


@login_required
def boleta(request, pk):
    venta = get_object_or_404(Venta.objects.select_related('vendedor'), pk=pk)
    return render(request, 'pos/boleta.html', {'venta': venta})


# ----------------------------------------------------------------------
# Historial de ventas
# ----------------------------------------------------------------------
@login_required
def ventas(request):
    try:
        dia = datetime.strptime(request.GET.get('fecha', ''), '%Y-%m-%d').date()
    except ValueError:
        dia = timezone.localdate()

    # Rango explicito en vez de fecha__date: asi no depende de que MySQL
    # tenga cargadas sus tablas de zonas horarias.
    inicio = timezone.make_aware(datetime.combine(dia, time.min))
    lista = (Venta.objects
             .filter(fecha__gte=inicio, fecha__lt=inicio + timedelta(days=1))
             .select_related('vendedor')
             .prefetch_related('detalles', 'comprobantes_electronicos'))
    validas = lista.filter(anulada=False)

    return render(request, 'pos/ventas.html', {
        'dia': dia,
        'ventas': lista,
        'cantidad': validas.count(),
        'total_dia': validas.aggregate(t=Sum('total'))['t'] or Decimal('0.00'),
        'por_metodo': (validas.values('metodo_pago')
                       .annotate(t=Sum('total')).order_by('-t')),
    })


@login_required
@require_POST
def anular_venta(request, pk):
    venta = get_object_or_404(Venta, pk=pk)
    if not request.user.is_staff:
        messages.error(request, 'Solo un administrador puede anular ventas.')
    elif venta.comprobante and venta.comprobante.estado_sunat not in ('RECHAZADO', 'ANULADO'):
        # Lo ya informado a SUNAT no se deshace solo con marcar la venta
        messages.error(request, (
            f'La venta tiene el comprobante {venta.comprobante.numero_completo}. '
            'Anúlalo primero en Comprobantes (baja o nota de crédito).'))
    elif not venta.anulada:
        venta.anulada = True
        venta.save(update_fields=['anulada'])
        messages.success(request, f'Venta {venta.numero} anulada.')
    return redirect(f"{reverse('ventas')}?fecha={timezone.localtime(venta.fecha):%Y-%m-%d}")


# ----------------------------------------------------------------------
# Catalogo de productos (solo nombres)
# ----------------------------------------------------------------------
def _guardar_producto(request, producto):
    nombre = ' '.join(request.POST.get('nombre', '').split())
    unidad = request.POST.get('unidad', '').strip().upper()[:20] or 'UND'
    if not nombre:
        messages.error(request, 'El nombre no puede estar vacío.')
        return
    repetido = Producto.objects.filter(nombre__iexact=nombre)
    if producto.pk:
        repetido = repetido.exclude(pk=producto.pk)
    if repetido.exists():
        messages.error(request, f'Ya existe un producto llamado "{nombre}".')
        return
    producto.nombre = nombre[:200]
    producto.unidad = unidad
    producto.save()
    messages.success(request, f'Producto "{producto.nombre}" guardado.')


@login_required
def productos(request):
    if request.method == 'POST':
        _guardar_producto(request, Producto())
        return redirect('productos')
    return render(request, 'pos/productos.html', {
        'productos': Producto.objects.all(),
    })


@login_required
@require_POST
def editar_producto(request, pk):
    _guardar_producto(request, get_object_or_404(Producto, pk=pk))
    return redirect('productos')


@login_required
@require_POST
def cambiar_estado_producto(request, pk):
    producto = get_object_or_404(Producto, pk=pk)
    producto.activo = not producto.activo
    producto.save(update_fields=['activo'])
    return redirect('productos')
