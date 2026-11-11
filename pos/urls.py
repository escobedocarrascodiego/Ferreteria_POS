from django.urls import path

from . import views
from . import views_sunat as sunat

urlpatterns = [
    path('', views.pos, name='pos'),
    path('api/venta/', views.crear_venta, name='api_venta'),
    path('boleta/<int:pk>/', views.boleta, name='boleta'),

    path('ventas/', views.ventas, name='ventas'),
    path('ventas/<int:pk>/anular/', views.anular_venta, name='venta_anular'),

    path('productos/', views.productos, name='productos'),
    path('productos/<int:pk>/editar/', views.editar_producto, name='producto_editar'),
    path('productos/<int:pk>/estado/', views.cambiar_estado_producto, name='producto_estado'),

    # --- Facturacion electronica (SUNAT) ---
    path('comprobantes/', sunat.ComprobantesListView.as_view(), name='comprobantes_list'),
    path('comprobante/<uuid:pk>/', sunat.ComprobanteElectronicoView.as_view(),
         name='comprobante_electronico'),
    path('api/comprobante/crear/', sunat.crear_comprobante_electronico_api,
         name='crear_comprobante_electronico_api'),
    path('api/comprobante/enviar-sunat/', sunat.enviar_comprobante_sunat_api,
         name='enviar_comprobante_sunat_api'),
    path('api/comprobante/nota-credito/', sunat.crear_nota_credito_api,
         name='crear_nota_credito_api'),
    path('api/comprobante/anular/', sunat.anular_boleta_sunat_api,
         name='anular_boleta_sunat_api'),
    path('api/comprobante/anular-factura/', sunat.anular_factura_sunat_api,
         name='anular_factura_sunat_api'),
    path('api/comprobante/consultar-baja/', sunat.consultar_ticket_baja_api,
         name='consultar_ticket_baja_api'),
    path('api/comprobante/procesar-cola/', sunat.procesar_cola_envio_sunat_api,
         name='procesar_cola_envio_sunat_api'),
    path('api/comprobante/obtener/', sunat.obtener_comprobante_api,
         name='obtener_comprobante_api'),
    path('api/comprobante/<uuid:comprobante_id>/pdf/', sunat.generar_boleta_pdf,
         name='pdf_boleta'),

    # Autocompletar cliente por DNI / RUC
    path('api/consulta/dni/', sunat.consulta_dni_api, name='consulta_dni_api'),
    path('api/consulta/ruc/', sunat.consulta_ruc_api, name='consulta_ruc_api'),
]
