from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / '.env')

SECRET_KEY = env('DJANGO_SECRET_KEY')
DEBUG = env.bool('DJANGO_DEBUG', default=False)
ALLOWED_HOSTS = env.list('DJANGO_ALLOWED_HOSTS', default=['localhost', '127.0.0.1'])

INSTALLED_APPS = [
    'pos',
    'rest_framework',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'ferreteria.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'pos.context.negocio',
            ],
        },
    },
]

WSGI_APPLICATION = 'ferreteria.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': env('DB_NAME'),
        'USER': env('DB_USER'),
        'PASSWORD': env('DB_PASSWORD', default=''),
        'HOST': env('DB_HOST', default='localhost'),
        'PORT': env('DB_PORT', default='3306'),
        'OPTIONS': {
            'charset': 'utf8mb4',
            # InnoDB para que las transacciones de la venta funcionen de verdad
            'init_command': (
                "SET sql_mode='STRICT_TRANS_TABLES', default_storage_engine=INNODB"
            ),
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
]

LANGUAGE_CODE = 'es'
TIME_ZONE = 'America/Lima'
USE_I18N = True
# El locale 'es' usa coma decimal (formato de Espana); esto lo deja a la
# peruana: 1,234.56
FORMAT_MODULE_PATH = ['ferreteria.formats']
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'pos'
LOGOUT_REDIRECT_URL = 'login'

# ----------------------------------------------------------------------
# Datos del negocio (salen en la boleta)
# ----------------------------------------------------------------------
NEGOCIO = {
    'nombre': env('NEGOCIO_NOMBRE', default='Ferretería'),
    'ruc': env('NEGOCIO_RUC', default=''),
    'direccion': env('NEGOCIO_DIRECCION', default=''),
    'telefono': env('NEGOCIO_TELEFONO', default=''),
}

# Serie del ticket INTERNO (el que se imprime sin pasar por SUNAT). Las
# series de boleta y factura electronica (B001/F001) se configuran en
# Administracion > Configuraciones SUNAT.
SERIE_TICKET = env('SERIE_TICKET', default='T001')

# ----------------------------------------------------------------------
# Facturacion electronica
# ----------------------------------------------------------------------
# Los precios se digitan con IGV incluido; el comprobante lo desglosa.
IGV_TASA = env('IGV_TASA', default='0.18')

# Certificado digital y PDFs de comprobantes. No se publica por URL: el
# certificado es privado y los PDF se sirven desde su propia vista.
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'
COMPROBANTES_ROOT = MEDIA_ROOT / 'comprobantes'

# Token de apis.net.pe para autocompletar nombre por DNI / RUC (opcional)
APIS_NET_PE_TOKEN = env('APIS_NET_PE_TOKEN', default='')

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
}
