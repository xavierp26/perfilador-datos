"""
Manejo de periodos y del campo fecha.

Un "periodo" es siempre un entero:
    anio -> YYYY        mes -> YYYYMM        dia -> YYYYMMDD

El campo fecha puede venir como:
    * particiones   : lista ordenada [anio, mes(, dia)]  ej. ["anio", "mes"]
    * timestamp     : columna TIMESTAMP / DATE
    * int_yyyymmdd  : entero 20240131          * int_yyyymm : entero 202401     * int_yyyy : 2024
    * str_iso       : '2024-01-31[ hh:mm:ss]'  * str_yyyymmdd : '20240131'      * str_yyyymm : '202401'
    * str_yyyy_mm   : '2024-01'                * str_mm_yyyy  : '01/2024' 

Cada tipo sabe generar (a) la expresión SQL del periodo y (b) un filtro de rango que,
cuando es posible, compara la columna "cruda" para no perder partition pruning.
"""
import calendar
import datetime as dt
import re

GRANULARIDADES = ("anio", "mes", "dia")
_ORDEN = {"anio": 0, "mes": 1, "dia": 2}

# granularidad máxima que soporta cada tipo de campo
_MAX_GRAN = {
    "timestamp": "dia",
    "int_yyyymmdd": "dia",
    "str_iso": "dia",
    "str_yyyymmdd": "dia",
    "int_yyyymm": "mes",
    "str_yyyymm": "mes",
    "str_yyyy_mm": "mes",
    "str_mm_yyyy": "mes",
    "int_yyyy": "anio",
}

TIPOS_NUMERICOS = ("tinyint", "smallint", "int", "integer", "bigint", "float", "double", "real", "decimal")
TIPOS_TEXTO = ("string", "varchar", "char")
TIPOS_FECHA = ("timestamp", "date")


# ----------------------------------------------------------------------------- periodos
def normalizar_periodo(valor, gran, extremo="inicio"):
    """Convierte '2024-01', 202401, '20240115', date(...) ... a periodo entero en `gran`.

    Si el valor es menos granular que `gran` (p.ej. 202401 con gran='dia'), se completa con
    el primer día (extremo='inicio') o el último (extremo='fin').
    """
    if gran not in GRANULARIDADES:
        raise ValueError("granularidad inválida: %s" % gran)
    if isinstance(valor, (dt.datetime, dt.date)):
        y, m, d = valor.year, valor.month, valor.day
    else:
        s = re.sub(r"[^0-9]", "", str(valor).strip())[:8]
        if len(s) == 4:
            y, m, d = int(s), (1 if extremo == "inicio" else 12), None
        elif len(s) == 6:
            y, m, d = int(s[:4]), int(s[4:6]), None
        elif len(s) == 8:
            y, m, d = int(s[:4]), int(s[4:6]), int(s[6:8])
        else:
            raise ValueError("No se reconoce el periodo '%s' (use YYYY, YYYYMM o YYYYMMDD)" % valor)
        if d is None:
            d = 1 if extremo == "inicio" else calendar.monthrange(y, m)[1]
    dt.date(y, m, d)  # valida
    if gran == "anio":
        return y
    if gran == "mes":
        return y * 100 + m
    return y * 10000 + m * 100 + d


def primer_dia(p, gran):
    p = int(p)
    if gran == "anio":
        return dt.date(p, 1, 1)
    if gran == "mes":
        return dt.date(p // 100, p % 100, 1)
    return dt.date(p // 10000, (p // 100) % 100, p % 100)


def ultimo_dia(p, gran):
    d = primer_dia(p, gran)
    if gran == "anio":
        return dt.date(d.year, 12, 31)
    if gran == "mes":
        return dt.date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
    return d


def a_periodo(fecha, gran):
    return normalizar_periodo(fecha, gran)


def siguiente(p, gran):
    if gran == "anio":
        return int(p) + 1
    return a_periodo(ultimo_dia(p, gran) + dt.timedelta(days=1), gran)


def anterior(p, gran):
    if gran == "anio":
        return int(p) - 1
    return a_periodo(primer_dia(p, gran) - dt.timedelta(days=1), gran)


def rango_periodos(ini, fin, gran):
    out, p = [], int(ini)
    fin = int(fin)
    while p <= fin:
        out.append(p)
        p = siguiente(p, gran)
    return out


def huecos(periodos_con_datos, gran, ini=None, fin=None):
    """Periodos sin datos entre ini y fin (por defecto min y max observados)."""
    obs = sorted(set(int(x) for x in periodos_con_datos))
    if not obs and (ini is None or fin is None):
        return []
    ini = obs[0] if ini is None else int(ini)
    fin = obs[-1] if fin is None else int(fin)
    s = set(obs)
    return [p for p in rango_periodos(ini, fin, gran) if p not in s]


def etiqueta(p, gran):
    p = int(p)
    if gran == "anio":
        return str(p)
    if gran == "mes":
        return "%04d-%02d" % (p // 100, p % 100)
    return "%04d-%02d-%02d" % (p // 10000, (p // 100) % 100, p % 100)


# ----------------------------------------------------------------------------- tipos de columna
def tipo_base(tipo_sql):
    t = str(tipo_sql).strip().lower()
    return re.split(r"[(<\s]", t)[0]


def clase_columna(tipo_sql):
    """numerica | categorica | fecha | compleja"""
    t = tipo_base(tipo_sql)
    if t in TIPOS_NUMERICOS:
        return "numerica"
    if t in TIPOS_TEXTO or t == "boolean":
        return "categorica"
    if t in TIPOS_FECHA:
        return "fecha"
    return "compleja"


# ----------------------------------------------------------------------------- campo fecha
class CampoFecha(object):
    """Describe cómo se obtiene el periodo de la tabla fuente."""

    def __init__(self, columnas, tipo, granularidad=None):
        if isinstance(columnas, str):
            columnas = [c.strip() for c in columnas.split(",") if c.strip()]
        self.columnas = list(columnas)
        self.tipo = tipo
        self.gran_max = self._gran_max()
        self.granularidad = granularidad or ("mes" if _ORDEN[self.gran_max] >= 1 else self.gran_max)
        if _ORDEN[self.granularidad] > _ORDEN[self.gran_max]:
            raise ValueError(
                "El campo %s (%s) solo permite granularidad hasta '%s'"
                % (self.columnas, self.tipo, self.gran_max)
            )

    def _gran_max(self):
        if self.tipo == "particiones":
            return {1: "anio", 2: "mes"}.get(len(self.columnas), "dia")
        return _MAX_GRAN[self.tipo]

    def __repr__(self):
        return "CampoFecha(%s, tipo=%s, granularidad=%s)" % (self.columnas, self.tipo, self.granularidad)

    def a_dict(self):
        return {"columnas": self.columnas, "tipo": self.tipo, "granularidad": self.granularidad}

    # --- expresión SQL del periodo (entero)
    def expr_periodo(self):
        g, c = self.granularidad, self.columnas[0]
        if self.tipo == "particiones":
            cols = ["CAST(%s AS BIGINT)" % x for x in self.columnas]
            if g == "anio":
                return cols[0]
            if g == "mes":
                return "(%s * 100 + %s)" % (cols[0], cols[1])
            return "(%s * 10000 + %s * 100 + %s)" % (cols[0], cols[1], cols[2])
        if self.tipo == "timestamp":
            if g == "anio":
                return "CAST(year(%s) AS BIGINT)" % c
            if g == "mes":
                return "CAST(year(%s) * 100 + month(%s) AS BIGINT)" % (c, c)
            return "CAST(year(%s) * 10000 + month(%s) * 100 + day(%s) AS BIGINT)" % (c, c, c)
        if self.tipo.startswith("int_"):
            div = {
                ("int_yyyymmdd", "dia"): 1, ("int_yyyymmdd", "mes"): 100, ("int_yyyymmdd", "anio"): 10000,
                ("int_yyyymm", "mes"): 1, ("int_yyyymm", "anio"): 100, ("int_yyyy", "anio"): 1,
            }[(self.tipo, g)]
            if div == 1:
                return "CAST(%s AS BIGINT)" % c
            return "CAST(FLOOR(%s / %d) AS BIGINT)" % (c, div)
        # textos: se toman los dígitos necesarios
        n = {"anio": 4, "mes": 6, "dia": 8}[g]
        if self.tipo in ("str_yyyy_mm", "str_mm_yyyy"):
            if self.tipo == "str_yyyy_mm":       # '2002-09'
                y, m = "substr(%s, 1, 4)" % c, "substr(%s, 6, 2)" % c
            else:                                 # '09/2002' o '9/2002'
                y, m = "split_part(%s, '/', 2)" % c, "lpad(split_part(%s, '/', 1), 2, '0')" % c
            if g == "anio":
                return "CAST(%s AS BIGINT)" % y
            return "CAST(concat(%s, %s) AS BIGINT)" % (y, m)
        if self.tipo == "str_iso":
            return "CAST(substr(regexp_replace(substr(%s, 1, 10), '-', ''), 1, %d) AS BIGINT)" % (c, n)
        return "CAST(substr(%s, 1, %d) AS BIGINT)" % (c, n)

    # --- filtro de rango [p_ini, p_fin] (en granularidad del campo)
    def filtro_rango(self, p_ini, p_fin):
        g, c = self.granularidad, self.columnas[0]
        d1, d2 = primer_dia(p_ini, g), ultimo_dia(p_fin, g)
        if self.tipo == "particiones":
            if int(p_ini) == int(p_fin):  # igualdades: el pruning más simple posible
                partes = [d1.year, d1.month, d1.day][: {"anio": 1, "mes": 2, "dia": 3}[g]]
                return " AND ".join(
                    "CAST(%s AS BIGINT) = %d" % (col, v) for col, v in zip(self.columnas, partes)
                )
            return "%s BETWEEN %d AND %d" % (self.expr_periodo(), int(p_ini), int(p_fin))
        d2_sig = d2 + dt.timedelta(days=1)
        if self.tipo in ("timestamp", "str_iso"):
            return "%s >= '%s' AND %s < '%s'" % (c, d1.isoformat(), c, d2_sig.isoformat())
        if self.tipo == "int_yyyymmdd":
            return "%s BETWEEN %s AND %s" % (c, d1.strftime("%Y%m%d"), d2.strftime("%Y%m%d"))
        if self.tipo == "int_yyyymm":
            return "%s BETWEEN %s AND %s" % (c, d1.strftime("%Y%m"), d2.strftime("%Y%m"))
        if self.tipo == "int_yyyy":
            return "%s BETWEEN %d AND %d" % (c, d1.year, d2.year)
        if self.tipo == "str_yyyymmdd":
            return "%s >= '%s' AND %s <= '%s'" % (c, d1.strftime("%Y%m%d"), c, d2.strftime("%Y%m%d"))
        if self.tipo in ("str_yyyy_mm", "str_mm_yyyy"):
            return "%s BETWEEN %d AND %d" % (self.expr_periodo(), int(p_ini), int(p_fin))
        if self.tipo == "str_yyyymm":
            return "%s >= '%s' AND %s <= '%s'" % (c, d1.strftime("%Y%m"), c, d2.strftime("%Y%m"))
        raise ValueError(self.tipo)

    def filtro_periodo(self, p):
        return self.filtro_rango(p, p)

    @property
    def usa_pruning_directo(self):
        """True si el filtro compara columnas de partición directamente."""
        return self.tipo == "particiones"


def detectar_tipo_campo(columnas, tipos, muestra=None):
    """Deduce el tipo de CampoFecha.

    columnas : lista de nombres
    tipos    : dict nombre -> tipo SQL (de DESCRIBE)
    muestra  : lista de valores no nulos de la columna (solo si es 1 columna no fecha)
    """
    if isinstance(columnas, str):
        columnas = [c.strip() for c in columnas.split(",") if c.strip()]
    if len(columnas) > 1:
        return "particiones"
    c = columnas[0]
    t = tipo_base(tipos.get(c.lower(), tipos.get(c, "")))
    if t in TIPOS_FECHA:
        return "timestamp"
    vals = [str(v).strip() for v in (muestra or []) if v is not None and str(v).strip() != ""]
    if not vals:
        raise ValueError("No se pudo muestrear el campo %s para detectar su formato" % c)
    v = vals[0]
    if t in TIPOS_NUMERICOS:
        v = v.split(".")[0]
        return {8: "int_yyyymmdd", 6: "int_yyyymm", 4: "int_yyyy"}.get(len(v)) or _err(c, v)
    if re.match(r"^\d{4}-\d{2}-\d{2}", v):
        return "str_iso"
    if re.match(r"^\d{8}$", v):
        return "str_yyyymmdd"
    if re.match(r"^\d{6}$", v):
        return "str_yyyymm"
    if re.match(r"^\d{4}[-/]\d{2}$", v):
        return "str_yyyy_mm"
    if re.match(r"^\d{1,2}/\d{4}$", v):
        return "str_mm_yyyy"
    return _err(c, v)


def _err(c, v):
    raise ValueError("Formato de fecha no reconocido en %s (ejemplo: %r). Formatos soportados: TIMESTAMP/DATE, "
                     "enteros YYYYMMDD/YYYYMM/YYYY y textos YYYY-MM-DD, YYYYMMDD, YYYYMM, YYYY-MM, MM/YYYY "
                     "(en archivos también DD/MM/YYYY)" % (c, v))
