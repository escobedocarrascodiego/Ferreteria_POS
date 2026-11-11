from django.conf import settings


def negocio(request):
    """Deja los datos del negocio disponibles en todas las plantillas."""
    return {'negocio': settings.NEGOCIO}
