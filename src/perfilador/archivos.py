"""
Perfilamiento de archivos (.xlsx, .xlsm, .csv, .txt) SIN dependencias nuevas.

Idea: el perfilador genera SQL en un dialecto estilo Impala/Hive (sql.py), pensado para poder correr también sobre
un motor distribuido. Para archivos ese SQL se ejecuta en SQLite (módulo sqlite3 de la librería estándar) con unas
pocas traducciones y funciones registradas en Python (NDV, APPX_MEDIAN, STDDEV_SAMP, is_inf, regexp_like, ...).

Lo propio de archivos se resuelve aquí:
  * lectura robusta: todo se lee como TEXTO (no se pierden ceros a la izquierda), codificación, separador,
    coma decimal, filas/columnas vacías, encabezados vacíos o duplicados, filas de totales;
  * inferencia de tipos explicada (cada decisión queda en el reporte);
  * "foto única": si no hay campo fecha (catálogos), todo se mide como un solo periodo.

Diferencias conocidas frente a un motor tipo Impala: NDV y APPX_MEDIAN son exactos (no aproximados), y SQLite guarda NaN
como NULL (un NaN del archivo cuenta como nulo).
"""
import csv
import datetime as dt
import hashlib
import io
import logging
import math
import os
import re
import sqlite3
import statistics
import unicodedata

import pandas as pd

ZONA = "archivo"
EXT_EXCEL = (".xlsx", ".xlsm", ".xls")
EXT_TEXTO = (".csv", ".txt", ".tsv")


# =============================================================================== motor SQLite
class _Ndv(object):
    def __init__(self):
        self.s = set()

    def step(self, v):
        if v is not None:
            self.s.add(v)

    def finalize(self):
        return len(self.s)


class _Mediana(object):
    def __init__(self):
        self.v = []

    def step(self, x):
        if x is not None:
            self.v.append(x)

    def finalize(self):
        return statistics.median(self.v) if self.v else None


class _StdSamp(object):
    """Welford: estable numéricamente."""
    def __init__(self):
        self.n, self.m, self.s2 = 0, 0.0, 0.0

    def step(self, x):
        if x is None:
            return
        self.n += 1
        d = x - self.m
        self.m += d / self.n
        self.s2 += d * (x - self.m)

    def finalize(self):
        return math.sqrt(self.s2 / (self.n - 1)) if self.n > 1 else None


def _fn_is_inf(x):
    return None if x is None else int(isinstance(x, float) and math.isinf(x))


def _fn_is_nan(x):
    return None if x is None else int(isinstance(x, float) and math.isnan(x))


def _fn_try_double(x):
    if x is None:
        return None
    try:
        return float(str(x).strip())
    except ValueError:
        return None


def _fn_regexp_like(x, pat):
    return None if x is None else int(re.search(pat, str(x)) is not None)


def _fn_concat(*a):
    return None if any(x is None for x in a) else "".join(str(x) for x in a)


def _fn_concat_ws(sep, *a):
    return str(sep).join(str(x) for x in a if x is not None)


def _parte_fecha(i):
    def f(x):
        if x is None:
            return None
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(x))
        return int(m.group(i)) if m else None
    return f


_TRAD = [
    (re.compile(r"\bAS STRING\b", re.I), "AS TEXT"),
    (re.compile(r"\bSTORED AS PARQUET\b", re.I), ""),
    (re.compile(r"\bPURGE\b", re.I), ""),
    (re.compile(r"\\'"), "''"),
    (re.compile(r"\bIS NOT DISTINCT FROM\b", re.I), "IS"),
    # CAST(TRIM(<texto>) AS DOUBLE): en SQLite un texto no numérico daría 0.0, en Impala NULL
    (re.compile(r"CAST\(TRIM\((CAST\(`[^`]+` AS (?:STRING|TEXT)\))\) AS DOUBLE\)", re.I), r"try_double(TRIM(\1))"),
]


class MotorSQLite(object):
    """Motor SQL sobre sqlite3 (librería estándar): ejecuta el SQL estilo Impala del perfilador traduciendo funciones."""

    def __init__(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute("ATTACH DATABASE ':memory:' AS %s" % ZONA)
        for nombre, n, fn in (("is_inf", 1, _fn_is_inf), ("is_nan", 1, _fn_is_nan),
                              ("try_double", 1, _fn_try_double), ("regexp_like", 2, _fn_regexp_like),
                              ("concat", -1, _fn_concat), ("concat_ws", -1, _fn_concat_ws),
                              ("floor", 1, lambda x: None if x is None else math.floor(x)),
                              ("year", 1, _parte_fecha(1)), ("month", 1, _parte_fecha(2)),
                              ("day", 1, _parte_fecha(3))):
            self.con.create_function(nombre, n, fn)
        self.con.create_aggregate("ndv", 1, _Ndv)
        self.con.create_aggregate("appx_median", 1, _Mediana)
        self.con.create_aggregate("stddev_samp", 1, _StdSamp)
        self.logger = logging.getLogger("perfilador.archivo")
        self.tipos = {}   # tabla -> [(columna, tipo_impala)]

    def _traducir(self, q):
        for pat, rep in _TRAD:
            q = pat.sub(rep, q)
        if re.match(r"\s*CREATE TABLE", q, re.I):
            q = re.sub(r"\bSTRING\b", "TEXT", q)
        return q

    def obtener_dataframe(self, q):
        qs = q.strip()
        m = re.match(r"DESCRIBE\s+(\S+)", qs, re.I)
        if m:
            cols = self.tipos.get(m.group(1).lower())
            if cols is None:
                raise RuntimeError("AnalysisException: Could not resolve table reference: '%s'" % m.group(1))
            return pd.DataFrame(cols, columns=["name", "type"]).assign(comment="")
        cur = self.con.execute(self._traducir(q))
        return pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])

    def ejecutar_consulta(self, q):
        self.con.execute(self._traducir(q))
        self.con.commit()

    def cargar(self, tabla, df, tipos):
        """tabla 'archivo.nombre'; df ya tipado; tipos: dict columna -> tipo Impala (bigint/double/date/...)."""
        cols = list(df.columns)
        decl = {"bigint": "INTEGER", "double": "REAL", "date": "TEXT", "timestamp": "TEXT", "string": "TEXT"}
        self.con.execute("DROP TABLE IF EXISTS %s" % tabla)
        self.con.execute("CREATE TABLE %s (%s)" % (tabla, ", ".join("`%s` %s" % (c, decl[tipos[c]]) for c in cols)))
        filas = [tuple(None if (v is None or (isinstance(v, float) and math.isnan(v))) else v for v in r)
                 for r in df.itertuples(index=False, name=None)]
        self.con.executemany("INSERT INTO %s VALUES (%s)" % (tabla, ", ".join("?" * len(cols))), filas)
        self.con.commit()
        self.tipos[tabla.lower()] = [(c, tipos[c]) for c in cols]


# =============================================================================== lectura
def _norm_nombre(c):
    c = unicodedata.normalize("NFKD", str(c)).encode("ascii", "ignore").decode("ascii")
    c = re.sub(r"[^0-9a-zA-Z_]+", "_", c.strip().lower()).strip("_")
    return c or "col"


def _leer_texto(ruta, sep, encoding):
    crudo = open(ruta, "rb").read()
    encs = [encoding] if encoding else ["utf-8-sig", "cp1252", "latin-1"]
    texto = enc_ok = None
    for e in encs:
        try:
            texto, enc_ok = crudo.decode(e), e
            break
        except UnicodeDecodeError:
            continue
    if texto is None:
        raise ValueError("No se pudo decodificar %s con %s" % (ruta, encs))
    if not sep:
        muestra = texto[:20000]
        try:
            sep = csv.Sniffer().sniff(muestra, delimiters=",;\t|").delimiter
        except csv.Error:
            conteo = {s: muestra.count(s) for s in (",", ";", "\t", "|")}
            sep = max(conteo, key=conteo.get)
    df = pd.read_csv(io.StringIO(texto), sep=sep, dtype=str, keep_default_na=False, na_values=[""],
                     skip_blank_lines=False)
    return df, {"encoding": enc_ok, "separador": sep}


def _leer_excel(ruta, hoja):
    info = {}
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        raise ImportError("Para leer .xlsx/.xlsm se necesita openpyxl: pip install openpyxl "
                          "(o guarde el archivo como CSV, que no necesita nada)")
    try:
        import openpyxl
        wb = openpyxl.load_workbook(ruta, read_only=True, data_only=True)
        info["hojas"] = [{"hoja": ws.title, "estado": getattr(ws, "sheet_state", "visible")} for ws in wb.worksheets]
        visibles = [h["hoja"] for h in info["hojas"] if h["estado"] == "visible"]
        wb.close()
    except Exception:  # .xls u otro motor
        visibles = None
    if hoja is None:
        hoja = visibles[0] if visibles else 0
    df = pd.read_excel(ruta, sheet_name=hoja, dtype=str, keep_default_na=False, na_values=[""])
    info["hoja"] = hoja if isinstance(hoja, str) else str(hoja)
    return df, info


_RE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?$")
_RE_MY = re.compile(r"^(\d{1,2})[/-](\d{4})$")      # 09/2002
_RE_YM = re.compile(r"^(\d{4})[/-](\d{1,2})$")      # 2002-09
_RE_MONEDA = re.compile(r"^\(?-?\$\s?-?[\d,]*\.?\d+\)?$")   # $24295, $-77.00, -$1,234.50
_RE_DMY = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$")
_RE_MILES_PUNTO = re.compile(r"^-?\d{1,3}(\.\d{3})+(,\d+)?$|^-?\d+,\d+$")
_RE_MILES_COMA = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")


def _como_numero(s, decimal):
    """Serie de textos -> (serie float, formato) o (None, motivo)."""
    if decimal == ",":
        t = s.str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
        return pd.to_numeric(t, errors="coerce"), "coma decimal"
    num = pd.to_numeric(s, errors="coerce")
    if num.notna().all():
        return num, "punto decimal"
    if s.str.contains("$", regex=False).mean() > 0.5 and s.str.match(_RE_MONEDA).all():
        neg = s.str.startswith("(") & s.str.endswith(")")          # (1,234.00) contable = negativo
        t = s.str.replace(r"[\$,()\s]", "", regex=True)
        val = pd.to_numeric(t, errors="coerce")
        return val.where(~neg, -val.abs()), "moneda ($ y separadores removidos)"
    if decimal is None:
        if s.str.match(_RE_MILES_PUNTO).mean() > 0.5:
            t = s.str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
            return pd.to_numeric(t, errors="coerce"), "coma decimal (detectada)"
        if s.str.match(_RE_MILES_COMA).any():
            t = s.str.replace(",", "", regex=False)
            return pd.to_numeric(t, errors="coerce"), "separador de miles con coma (detectado)"
    return num, "punto decimal"


def _como_fecha(s):
    """Serie de textos -> (serie ISO texto, tipo 'date'|'timestamp', nota) o None si no todas son fecha."""
    iso = s.str.match(_RE_ISO)
    if iso.all():
        con_hora = s.str.len().gt(10) & ~s.str.endswith("00:00:00")
        if con_hora.any():
            return s.str.slice(0, 19).str.replace("T", " ", regex=False), "timestamp", "ISO con hora"
        return s.str.slice(0, 10), "date", "ISO"
    for rx, orden, nota in ((_RE_MY, (1, 0), "mm/aaaa (se toma el día 1)"), (_RE_YM, (0, 1), "aaaa-mm (se toma el día 1)")):
        mm = s.str.extract(rx)
        if mm[0].notna().all():
            y, mth = mm[orden[0]].astype(int), mm[orden[1]].astype(int)
            if mth.between(1, 12).all() and y.between(1800, 2999).all():
                out = ["%04d-%02d-01" % (a, b) for a, b in zip(y, mth)]
                return pd.Series(out, index=s.index), "date", nota
    m = s.str.extract(_RE_DMY)
    if m[0].notna().all():
        a, b = m[0].astype(int), m[1].astype(int)
        if (a > 12).any() and (b <= 12).all():
            d, mth, nota = a, b, "dd/mm/aaaa"
        elif (b > 12).any() and (a <= 12).all():
            d, mth, nota = b, a, "mm/dd/aaaa"
        elif (a <= 12).all() and (b <= 12).all():
            d, mth, nota = a, b, "dd/mm/aaaa (AMBIGUO: se asumió día/mes)"
        else:
            return None
        try:
            out = [dt.date(int(y), int(mm), int(dd)).isoformat() for y, mm, dd in zip(m[2], mth, d)]
        except ValueError:
            return None
        return pd.Series(out, index=s.index), "date", nota
    return None


def inferir_tipos(df, decimal=None):
    """Infere tipos con criterio conservador. Devuelve (df_tipado, tipos, tabla_inferencia)."""
    out, tipos, filas = {}, {}, []
    for c in df.columns:
        s = df[c]
        nn = s.dropna().astype(str).str.strip()
        nn = nn[nn != ""]
        reg = {"columna": c, "no_nulos": int(len(nn))}
        if nn.empty:
            out[c], tipos[c] = s.where(s.notna() & (s.astype(str).str.strip() != ""), None), "string"
            reg.update(tipo="string", motivo="sin valores")
            filas.append(reg)
            continue
        ceros = int(nn.str.match(r"^-?0\d+$").sum())   # solo códigos numéricos, ej. 02002727
        num, formato = _como_numero(nn, decimal)
        pct_num = float(num.notna().mean())
        fecha = _como_fecha(nn)
        if fecha is not None:
            iso, t, nota = fecha
            col = pd.Series([None] * len(s), index=s.index, dtype=object)
            col.loc[iso.index] = iso
            out[c], tipos[c] = col, t
            reg.update(tipo=t, motivo="fecha " + nota)
        elif pct_num == 1.0 and ceros == 0:
            col = pd.Series([None] * len(s), index=s.index, dtype=object)
            entero = bool(((num % 1) == 0).all()) and num.abs().max() < 2 ** 53
            col.loc[num.index] = num.astype("int64").astype(object) if entero else num.astype(object)
            out[c], tipos[c] = col, ("bigint" if entero else "double")
            reg.update(tipo=tipos[c], motivo="numérico, " + formato)
        else:
            col = s.astype(object).where(s.notna(), None)
            out[c], tipos[c] = col, "string"
            if ceros:
                motivo = "texto: %d valores con cero a la izquierda (se conserva como texto)" % ceros
            elif pct_num >= 0.95:
                motivo = "texto: %.1f%% parece número; hay valores no numéricos" % (100 * pct_num)
            else:
                motivo = "texto"
            reg.update(tipo="string", motivo=motivo, ceros_izquierda=int(ceros), pct_numerico=pct_num)
        filas.append(reg)
    return pd.DataFrame(out), tipos, pd.DataFrame(filas)


def leer_archivo(ruta, hoja=None, sep=None, encoding=None, decimal=None, excluir_totales=True):
    """Lee, limpia la ESTRUCTURA (no los valores) e infiere tipos. Devuelve (df, tipos, info, alertas)."""
    ext = os.path.splitext(ruta)[1].lower()
    if ext in EXT_EXCEL:
        df, info = _leer_excel(ruta, hoja)
    elif ext in EXT_TEXTO:
        df, info = _leer_texto(ruta, sep, encoding)
    else:
        raise ValueError("Extensión no soportada: %s (use .xlsx, .xlsm, .xls, .csv, .txt)" % ext)
    info.update(archivo=os.path.basename(ruta), filas_leidas=int(len(df)), columnas_leidas=int(df.shape[1]))
    al = []

    # encabezados vacíos / duplicados (pandas los renombra como 'Unnamed: 3' y 'col.1')
    originales = [str(c) for c in df.columns]
    vacios = [c for c in originales if re.match(r"^Unnamed: \d+$", c)]
    dups = [c for c in originales if re.match(r"^.+\.\d+$", c) and re.sub(r"\.\d+$", "", c) in originales]
    # filas y columnas totalmente vacías
    vacia = df.apply(lambda col: col.isna() | (col.astype(str).str.strip() == ""))
    filas_vacias = int(vacia.all(axis=1).sum())
    # solo se omiten columnas vacías SIN encabezado (restos de Excel); una columna con nombre y vacía es un
    # hallazgo (ej. la llave de cruce vacía) y se perfila para que salga la alerta variable_vacia
    cols_vacias = [c for c in df.columns if vacia[c].all() and re.match(r"^Unnamed: \d+$", str(c))]
    df = df.loc[~vacia.all(axis=1), [c for c in df.columns if c not in cols_vacias]]
    vacios = [c for c in vacios if c not in cols_vacias]
    # filas de totales al final (primeras dos columnas)
    totales = pd.Series(False, index=df.index)
    for c in list(df.columns)[:2]:
        totales |= df[c].astype(str).str.match(r"^\s*(gran\s+)?total(es)?\b", case=False, na=False)
    n_tot = int(totales.sum())
    if excluir_totales and n_tot:
        df = df.loc[~totales]
    dup_filas = int(df.duplicated().sum())

    # nombres de columna seguros para SQL (se guarda el mapeo)
    nombres, usados = {}, set()
    for c in df.columns:
        n = _norm_nombre(c)
        base, k = n, 2
        while n in usados:
            n, k = "%s_%d" % (base, k), k + 1
        usados.add(n)
        nombres[c] = n
    df = df.rename(columns=nombres)
    df, tipos, inferencia = inferir_tipos(df, decimal)
    inferencia.insert(0, "columna_original", [k for k in nombres])

    info.update(filas_vacias=filas_vacias, columnas_vacias=[str(c) for c in cols_vacias], encabezados_vacios=vacios,
                encabezados_duplicados=dups, filas_total=n_tot, totales_excluidos=bool(excluir_totales and n_tot),
                filas_duplicadas=dup_filas, filas_finales=int(len(df)))
    if vacios:
        al.append(("MEDIA", "encabezado_vacio", "Columnas sin encabezado: %s" % ", ".join(vacios)))
    if dups:
        al.append(("MEDIA", "encabezado_duplicado", "Encabezados repetidos (pandas los renombró): %s" % ", ".join(dups)))
    if filas_vacias:
        al.append(("INFO", "filas_vacias", "%d filas totalmente vacías (se omitieron)" % filas_vacias))
    if cols_vacias:
        al.append(("INFO", "columnas_vacias", "%d columnas sin encabezado y vacías (se omitieron)" % len(cols_vacias)))
    if n_tot:
        al.append(("MEDIA", "filas_total", "%d fila(s) de totales %s" % (n_tot, "excluidas" if excluir_totales else "INCLUIDAS")))
    if dup_filas:
        al.append(("MEDIA", "filas_duplicadas", "%d filas idénticas en todas las columnas" % dup_filas))
    for _, r in inferencia.iterrows():
        if r.get("ceros_izquierda", 0) and r["ceros_izquierda"] > 0:
            al.append(("INFO", "ceros_izquierda", "%s: %d valores con cero a la izquierda; se conserva como texto "
                       "(leerlo como número lo corrompe)" % (r["columna"], r["ceros_izquierda"]), r["columna"]))
        if "AMBIGUO" in str(r.get("motivo", "")):
            al.append(("INFO", "fecha_ambigua", "%s: fechas ambiguas, se asumió día/mes/año" % r["columna"], r["columna"]))
    return df, tipos, info, inferencia, al


# =============================================================================== API
def perfilar_archivo(ruta, campo_fecha=None, hoja=None, sep=None, encoding=None, decimal=None, modo="todo",
                     excluir_totales=True, ejecutar=True, **kw):
    """Perfila un archivo .csv / .txt / .xlsx / .xlsm y genera el reporte HTML + Excel.

    campo_fecha : columna(s) de periodo; None = foto única (catálogos)
    modo        : 'todo' (default), 'rango' (inicio/fin) o 'puntual' (fecha)
    hoja        : nombre de la hoja en Excel (default: la primera visible)
    sep/encoding/decimal : se detectan solos; páselos solo si la detección falla
    kw          : cualquier parámetro de Perfilador (llave, reglas, reglas_dominio, variables, umbrales, ...)
    """
    from .perfilador import Perfilador

    df, tipos, info, inferencia, al = leer_archivo(ruta, hoja, sep, encoding, decimal, excluir_totales)
    nombre = _norm_nombre(os.path.splitext(os.path.basename(ruta))[0])
    if hoja:
        nombre += "__" + _norm_nombre(hoja)
    tabla = "%s.%s" % (ZONA, nombre)
    foto = campo_fecha is None
    if foto:
        st = os.stat(ruta)
        p = dt.date.fromtimestamp(st.st_mtime)
        df["_foto"] = p.year * 100 + p.month
        tipos["_foto"] = "bigint"
        campo_fecha, modo = "_foto", "todo"
    else:
        cols = [c.strip() for c in (campo_fecha.split(",") if isinstance(campo_fecha, str) else campo_fecha)]
        campo_fecha = [_norm_nombre(c) for c in cols]
    for k in ("llave", "variables", "excluir"):
        v = kw.get(k)
        if v:
            v = v.split(",") if isinstance(v, str) else v
            kw[k] = [_norm_nombre(x) for x in v]
    motor = MotorSQLite()
    motor.cargar(tabla, df, tipos)
    h = hashlib.md5(open(ruta, "rb").read()).hexdigest()[:10]
    info.update(foto_unica=foto, huella=h)
    kw.setdefault("limpiar", "siempre")
    kw.setdefault("materializar", False)
    perf = Perfilador(tabla=tabla, campo_fecha=campo_fecha, motor=motor, modo=modo, zona_temporal=ZONA,
                      huella=h, info_lectura={"info": info, "inferencia": inferencia}, alertas_extra=al, **kw)
    if ejecutar:
        perf.ejecutar()
    return perf
