"""Conversión de montos a letras según el formato de leyenda SUNAT (Catálogo 52, código 1000).

Ejemplo: Decimal('125.50') -> "CIENTO VEINTICINCO Y 50/100 SOLES"
"""
from decimal import Decimal, ROUND_HALF_UP

_UNIDADES = (
    '', 'UNO', 'DOS', 'TRES', 'CUATRO', 'CINCO', 'SEIS', 'SIETE', 'OCHO', 'NUEVE',
    'DIEZ', 'ONCE', 'DOCE', 'TRECE', 'CATORCE', 'QUINCE', 'DIECISEIS',
    'DIECISIETE', 'DIECIOCHO', 'DIECINUEVE', 'VEINTE', 'VEINTIUNO', 'VEINTIDOS',
    'VEINTITRES', 'VEINTICUATRO', 'VEINTICINCO', 'VEINTISEIS', 'VEINTISIETE',
    'VEINTIOCHO', 'VEINTINUEVE',
)

_DECENAS = (
    '', '', '', 'TREINTA', 'CUARENTA', 'CINCUENTA', 'SESENTA', 'SETENTA',
    'OCHENTA', 'NOVENTA',
)

_CENTENAS = (
    '', 'CIENTO', 'DOSCIENTOS', 'TRESCIENTOS', 'CUATROCIENTOS', 'QUINIENTOS',
    'SEISCIENTOS', 'SETECIENTOS', 'OCHOCIENTOS', 'NOVECIENTOS',
)

_MONEDAS = {
    'PEN': 'SOLES',
    'USD': 'DOLARES AMERICANOS',
    'EUR': 'EUROS',
}


def _hasta_99(n: int) -> str:
    if n < 30:
        return _UNIDADES[n]
    decena, unidad = divmod(n, 10)
    if unidad == 0:
        return _DECENAS[decena]
    return f"{_DECENAS[decena]} Y {_UNIDADES[unidad]}"


def _hasta_999(n: int) -> str:
    if n == 100:
        return 'CIEN'
    centena, resto = divmod(n, 100)
    if centena == 0:
        return _hasta_99(resto)
    if resto == 0:
        return _CENTENAS[centena]
    return f"{_CENTENAS[centena]} {_hasta_99(resto)}"


def numero_a_letras(n: int) -> str:
    """Convierte un entero de 0 a 999,999,999 a letras (mayúsculas, sin tildes)."""
    if n < 0 or n > 999_999_999:
        raise ValueError(f"Número fuera de rango para conversión a letras: {n}")
    if n == 0:
        return 'CERO'

    millones, resto = divmod(n, 1_000_000)
    miles, unidades = divmod(resto, 1_000)

    partes = []
    if millones == 1:
        partes.append('UN MILLON')
    elif millones > 1:
        partes.append(f"{_hasta_999(millones)} MILLONES")

    if miles == 1:
        partes.append('MIL')
    elif miles > 1:
        partes.append(f"{_hasta_999(miles)} MIL")

    if unidades:
        partes.append(_hasta_999(unidades))

    return ' '.join(partes)


def monto_en_letras(monto, moneda: str = 'PEN') -> str:
    """Formato SUNAT: 'CIENTO VEINTICINCO Y 50/100 SOLES'."""
    valor = Decimal(str(monto)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    entero = int(valor)
    centimos = int((valor - entero) * 100)
    nombre_moneda = _MONEDAS.get((moneda or 'PEN').upper(), (moneda or 'PEN').upper())
    return f"{numero_a_letras(entero)} Y {centimos:02d}/100 {nombre_moneda}"
