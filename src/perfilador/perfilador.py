"""
Motor del perfilador.

Flujo
-----
 1. Inventario  : columnas y tipos (DESCRIBE).
 2. Campo fecha : detecta tipo/formato y granularidad; arma expresión y filtros de periodo.
 3. Volumen     : filas por periodo en el rango -> historia, huecos, saltos.
 4. Perfilado   : por periodo (primero el BASE) y por lote de variables:
                    stats  -> nulos, inf, nan, ceros, negativos, vacíos, ndv, min, max, media, std, mediana
                    pct    -> NTILE(n_tiles) para percentiles y cortes PSI
                    topcat -> top-K categorías del base (definición de buckets categóricos)
                    bins   -> conteos por bucket (PSI)
                    uniq   -> duplicados de la llave (opcional)
                  Cada tarea = 1 INSERT ... SELECT a una tabla temporal + checkpoint CSV local.
                  Si una tarea falla se registra y se SIGUE con la siguiente.
 5. Consolidado : percentiles, PSI / PSI-alpha, alertas.
 6. Reporte     : Excel + HTML interactivo autocontenido.
 7. Limpieza    : borra las temporales si todo terminó bien (configurable).

Reanudar = volver a ejecutar con los mismos parámetros: el run_key es determinístico y las
tareas con estado "ok" se leen del checkpoint sin volver a consultar.

El SQL se genera en un dialecto estilo Impala/Hive (sql.py) y se ejecuta sobre un "motor": cualquier objeto con
obtener_dataframe(sql) -> DataFrame y ejecutar_consulta(sql). Para archivos el motor es archivos.MotorSQLite.
"""
import datetime as dt
import hashlib
import json
import logging
import os
import re
import time

import numpy as np
import pandas as pd

from . import metricas as M
from . import periodos as P
from . import sql as S


class _Motor(object):
    """Adaptador mínimo sobre el motor SQL (obtener_dataframe / ejecutar_consulta)."""

    def __init__(self, motor):
        if motor is None or not hasattr(motor, "obtener_dataframe"):
            raise ValueError("Se necesita un motor con obtener_dataframe(sql) y ejecutar_consulta(sql)")
        self.motor = motor

    def df(self, query, reintentable=True):
        return self.motor.obtener_dataframe(query)

    def ejecutar(self, query, reintentable=True):
        return self.motor.ejecutar_consulta(query)


def cargar_config():
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "config.json")
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)


def _leer_csv(ruta):
    """Lee checkpoints: solo la celda vacía es nulo. Por defecto pandas convierte 'N/A', 'NA', 'NULL',
    'None', 'nan'... en NaN, lo que borraría justo los nulos disfrazados y categorías reales como 'NA'."""
    try:
        return pd.read_csv(ruta, keep_default_na=False, na_values=[""])
    except pd.errors.EmptyDataError:
        # un DataFrame sin columnas (p. ej. archivo sin particiones) se guarda como CSV vacío
        return pd.DataFrame()


def _lista(x):
    if x is None:
        return None
    if isinstance(x, str):
        return [c.strip() for c in x.split(",") if c.strip()]
    return list(x)


def _lotes(lista, n):
    n = max(1, int(n))
    return [lista[i:i + n] for i in range(0, len(lista), n)]


class Perfilador(object):
    """Perfilamiento por periodo de una tabla expuesta por un motor SQL (ver archivos.perfilar_archivo).

    Parámetros principales
    ----------------------
    tabla        : 'zona.tabla'
    campo_fecha  : columna o lista de columnas de partición en orden [anio, mes(, dia)]
                   ej. 'fa' | 'f_corte' | ['anio', 'mes']
    modo         : 'rango' (inicio/fin) | 'puntual' (fecha) | 'todo'
    granularidad : 'anio' | 'mes' | 'dia'  (por defecto 'mes' si el campo lo permite)
    variables    : lista a perfilar (por defecto todas menos fecha/particiones)
    excluir      : lista a excluir
    llave        : columna(s) que deberían ser únicas por periodo (opcional)
    zona_temporal: zona donde crear las temporales (por defecto static/config.json)
    periodo_base : periodo de referencia PSI (por defecto el primero con datos)
    limpiar      : 'exito' (borra temporales si no hubo fallas) | 'siempre' | 'nunca'
    """

    def __init__(self, tabla, campo_fecha, motor=None, modo="rango", inicio=None, fin=None,
                 fecha=None, granularidad=None, variables=None, excluir=None, llave=None,
                 zona_temporal=None, periodo_base=None, comparar_anterior=True, n_bins=None, n_tiles=None,
                 top_categorias=None, lote_variables=None, lote_percentiles=None, materializar=None,
                 filtro_extra=None, incluir_particiones=False, umbrales=None,
                 dir_salida="perfilador_salidas", reiniciar=False, limpiar="exito",
                 logger=None, verbose=True, reglas=None, reglas_dominio=None,
                 nulos_disfrazados=None, cobertura=None, huella=None, info_lectura=None, alertas_extra=None,
                 psi_esperado=None):
        cfg = cargar_config().get("PerfParametros", {})
        if "." not in tabla:
            raise ValueError("La tabla debe venir como zona.tabla")
        if modo not in ("rango", "puntual", "todo"):
            raise ValueError("modo debe ser 'rango', 'puntual' o 'todo'")
        if modo == "rango" and (inicio is None or fin is None):
            raise ValueError("modo='rango' requiere inicio y fin")
        if modo == "puntual" and fecha is None:
            raise ValueError("modo='puntual' requiere fecha")

        self.tabla = tabla.strip().lower()
        self.campo_input = _lista(campo_fecha)
        self.modo, self.inicio, self.fin, self.fecha = modo, inicio, fin, fecha
        self.gran_input = granularidad
        self.variables_input = _lista(variables)
        self.excluir = [x.lower() for x in (_lista(excluir) or [])]
        self.llave = _lista(llave)
        self.zona_tmp = zona_temporal or cfg.get("zona_temporal", "tmp")
        self.periodo_base_input = periodo_base
        self.comparar_anterior = comparar_anterior
        self.n_bins = int(n_bins or cfg.get("n_bins", 10))
        self.n_tiles = int(n_tiles or cfg.get("n_tiles", 100))
        self.top_k = int(top_categorias or cfg.get("top_categorias", 20))
        self.lote_vars = int(lote_variables or cfg.get("lote_variables", 25))
        self.lote_pct = int(lote_percentiles or cfg.get("lote_percentiles", 10))
        self.materializar_input = materializar if materializar is not None else cfg.get("materializar", "auto")
        self.filtro_extra = filtro_extra
        self.incluir_particiones = incluir_particiones
        self.umbrales = dict(M.UMBRALES_DEFECTO)
        self.umbrales.update(cfg.get("umbrales", {}))
        self.umbrales.update(umbrales or {})
        self.limpiar = limpiar
        self.verbose = verbose
        self.dir_salida = dir_salida
        self.reiniciar = reiniciar
        # reglas de negocio: {"nombre": "expresion SQL booleana"} y dominios {"var": {"min":..,"max":..}}
        self.reglas = dict(cfg.get("reglas", {}))
        self.reglas.update(reglas or {})
        self.reglas_dominio = dict(cfg.get("reglas_dominio", {}))
        self.reglas_dominio.update(reglas_dominio or {})
        self.nulos_disfrazados = list(nulos_disfrazados if nulos_disfrazados is not None
                                      else cfg.get("nulos_disfrazados", M.NULOS_DISFRAZADOS))
        self.cobertura = bool(self.llave) if cobertura is None else bool(cobertura and self.llave)
        # origen archivo: huella del contenido (invalida checkpoints si el archivo cambia), info de lectura
        # PSI-alpha: distribución esperada por variable (cortes + % esperados), definida por el modelador
        self.psi_esperado = dict(cfg.get("psi_esperado", {}))
        self.psi_esperado.update(psi_esperado or {})
        self.huella = huella
        self.info_lectura = info_lectura
        self.alertas_extra = alertas_extra or []

        self.con = _Motor(motor)
        self.run_key = self._calcular_run_key()
        nombre_corto = re.sub(r"[^a-z0-9]", "_", self.tabla.split(".")[1])[:24]
        self.prefijo_tmp = "%s.tmp_perf_%s_%s" % (self.zona_tmp, nombre_corto, self.run_key)
        self.dir_run = os.path.join(dir_salida, self.tabla, self.run_key)
        self.dir_tareas = os.path.join(self.dir_run, "tareas")
        self.log = self._crear_logger()
        self.estado = None
        self.resultado = {}

    # ================================================================== utilidades
    def _calcular_run_key(self):
        clave = json.dumps({
            "t": self.tabla, "c": self.campo_input, "m": self.modo, "i": str(self.inicio), "f": str(self.fin),
            "d": str(self.fecha), "g": self.gran_input, "v": self.variables_input, "x": self.excluir,
            "k": self.llave, "b": str(self.periodo_base_input), "nb": self.n_bins, "nt": self.n_tiles,
            "tk": self.top_k, "fe": self.filtro_extra, "ip": self.incluir_particiones,
            "ca": self.comparar_anterior, "r": self.reglas, "rd": self.reglas_dominio,
            "nd": self.nulos_disfrazados, "co": self.cobertura, "h": self.huella, "pe": self.psi_esperado,
        }, sort_keys=True, default=str)
        return hashlib.md5(clave.encode("utf-8")).hexdigest()[:8]

    def _crear_logger(self):
        os.makedirs(self.dir_tareas, exist_ok=True)
        log = logging.getLogger("perfilador.%s" % self.run_key)
        log.setLevel(logging.INFO)
        log.propagate = False
        if not log.handlers:
            fmt = logging.Formatter("%(asctime)s - [%(levelname)s] - %(message)s", "%Y-%m-%d %H:%M:%S")
            fh = logging.FileHandler(os.path.join(self.dir_run, "perfilador.log"), encoding="utf-8")
            fh.setFormatter(fmt)
            log.addHandler(fh)
        return log

    def _p(self, msg, nivel="info"):
        getattr(self.log, nivel)(msg)
        if self.verbose:
            print(msg)

    def _ruta(self, nombre):
        return os.path.join(self.dir_run, nombre)

    def _guardar_estado(self):
        tmp = self._ruta("estado.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.estado, f, indent=1, default=str, ensure_ascii=False)
        os.replace(tmp, self._ruta("estado.json"))

    def _cargar_estado(self):
        ruta = self._ruta("estado.json")
        if os.path.exists(ruta) and not self.reiniciar:
            with open(ruta, encoding="utf-8") as f:
                return json.load(f)
        return {"run_key": self.run_key, "tabla": self.tabla, "creado": str(dt.datetime.now()),
                "tareas": {}, "temporales": [], "meta": {}}

    def _cache_df(self, nombre, fn):
        """Guarda/lee DataFrames auxiliares (inventario, volumen) en la carpeta del run."""
        ruta = self._ruta(nombre + ".csv")
        if os.path.exists(ruta) and not self.reiniciar:
            return _leer_csv(ruta)
        df = fn()
        df.to_csv(ruta, index=False)
        return df

    # ================================================================== API pública
    def ejecutar(self):
        t0 = time.time()
        self._p("=" * 78)
        self._p("PERFILADOR  %s   run_key=%s" % (self.tabla, self.run_key))
        self._p("salidas: %s" % os.path.abspath(self.dir_run))
        self._p("=" * 78)
        self.estado = self._cargar_estado()
        if self.reiniciar:
            for f in os.listdir(self.dir_tareas):
                os.remove(os.path.join(self.dir_tareas, f))
        self._inventario()
        self._definir_campo()
        self._volumen()
        self._definir_variables()
        self._crear_temporales()
        self._procesar_periodos()
        self._consolidar()
        self.resultado["duracion_seg"] = round(time.time() - t0, 1)
        self._reporte()
        self._limpiar_temporales()
        self._resumen_final()
        return self

    reanudar = ejecutar

    # ================================================================== 1. inventario
    def _inventario(self):
        self._p("[1/7] Inventario de %s" % self.tabla)
        desc = self._cache_df("inventario_columnas", lambda: self.con.df(S.describe(self.tabla)))
        desc = desc.iloc[:, :2].copy()
        desc.columns = ["columna", "tipo"]
        desc = desc[desc["columna"].notna() & ~desc["columna"].astype(str).str.startswith("#")]
        desc["columna"] = desc["columna"].astype(str).str.strip().str.lower()
        desc["tipo"] = desc["tipo"].astype(str).str.strip().str.lower()
        desc = desc[desc["columna"] != ""].drop_duplicates("columna")
        desc["clase"] = desc["tipo"].map(P.clase_columna)
        self.columnas = desc.reset_index(drop=True)
        self.tipos = dict(zip(desc["columna"], desc["tipo"]))

        self.cols_particion = []
        self.particiones = pd.DataFrame()
        self._p("      %d columnas" % len(self.columnas))

    # ================================================================== 2. campo fecha
    def _definir_campo(self):
        cols = [c.lower() for c in self.campo_input]
        faltan = [c for c in cols if c not in self.tipos]
        if faltan:
            raise ValueError("Columnas de fecha no existen en la tabla: %s" % faltan)
        meta = self.estado["meta"]
        if "campo" in meta and not self.reiniciar:
            tipo = meta["campo"]["tipo"]
        else:
            muestra = None
            if len(cols) == 1 and P.clase_columna(self.tipos[cols[0]]) != "fecha":
                muestra = self.con.df(S.muestra_campo(self.tabla, cols[0]))["v"].tolist()
            tipo = P.detectar_tipo_campo(cols, self.tipos, muestra)
        self.campo = P.CampoFecha(cols, tipo, self.gran_input)
        meta["campo"] = self.campo.a_dict()
        g = self.campo.granularidad
        if self.materializar_input == "auto":
            self.materializar = not self.campo.usa_pruning_directo and not set(cols) <= set(self.cols_particion)
        else:
            self.materializar = bool(self.materializar_input)
        self._p("[2/7] Campo fecha: %s  tipo=%s  granularidad=%s  materializar_por_periodo=%s"
                % (cols, tipo, g, self.materializar))

    def _where(self, filtro):
        return "(%s) AND (%s)" % (filtro, self.filtro_extra) if self.filtro_extra else filtro

    # ================================================================== 3. volumen
    def _volumen(self):
        g = self.campo.granularidad
        if self.modo == "rango":
            ini = P.normalizar_periodo(self.inicio, g, "inicio")
            fin = P.normalizar_periodo(self.fin, g, "fin")
            if ini > fin:
                raise ValueError("inicio > fin")
            filtro = self.campo.filtro_rango(ini, fin)
        elif self.modo == "puntual":
            fin = P.normalizar_periodo(self.fecha, g, "inicio")
            ini = P.anterior(fin, g) if self.comparar_anterior else fin
            filtro = self.campo.filtro_rango(ini, fin)
        else:
            ini = fin = None
            filtro = "1=1"
        self._p("[3/7] Volumen por periodo (%s)" % (
            "todo el histórico" if self.modo == "todo" else "%s a %s" % (P.etiqueta(ini, g), P.etiqueta(fin, g))))
        q = S.volumen(self.tabla, self.campo.expr_periodo(), self._where(filtro))
        try:
            vol = self._cache_df("volumen", lambda: self.con.df(q))
            vol.columns = ["periodo", "n"]
            vol = vol[vol["periodo"].notna()].copy()
            vol["periodo"] = vol["periodo"].astype("int64")
        except Exception as exc:
            if self.modo == "todo":
                raise
            self._p("      No se pudo calcular volumen (%s). Se procesará cada periodo del rango." % str(exc)[:150], "warning")
            vol = pd.DataFrame({"periodo": P.rango_periodos(ini, fin, g), "n": float("nan")})
        if vol.empty:
            raise ValueError("La tabla no tiene filas en el rango solicitado (revise campo/rango/filtro_extra)")
        if self.modo == "todo":
            ini, fin = int(vol["periodo"].min()), int(vol["periodo"].max())
        self.rango = (ini, fin)
        con_datos = sorted(vol.loc[vol["n"].fillna(1) > 0, "periodo"].astype(int).tolist())
        self.huecos = P.huecos(con_datos, g, ini, fin)
        self.periodos = con_datos
        base = self.periodo_base_input
        if base is not None:
            base = P.normalizar_periodo(base, g, "inicio")
            if base not in con_datos:
                raise ValueError("El periodo base %s no tiene datos" % base)
        else:
            # base = primer periodo con filas suficientes: un PSI contra una base de 3 filas no significa nada
            # (pasa con fechas de apertura: los primeros años casi no tienen cuentas)
            n_min = self.umbrales["psi_min_n"]
            nper = dict(zip(vol["periodo"].astype(int), vol["n"]))
            suficientes = [p for p in con_datos if (nper.get(p) or 0) >= n_min]
            if suficientes:
                base = suficientes[0]
            else:
                base = max(con_datos, key=lambda p: nper.get(p) or 0)
            if base != con_datos[0]:
                self._p("      Base PSI = %s (primer periodo con >= %d filas; los anteriores son muy chicos)"
                        % (P.etiqueta(base, g), n_min))
        self.periodo_base = base
        self.vol = vol
        self._p("      %d periodos con datos, %d huecos, base PSI = %s, filas totales = %s"
                % (len(con_datos), len(self.huecos), P.etiqueta(base, g),
                   "{:,.0f}".format(vol["n"].sum()) if vol["n"].notna().all() else "n/d"))

    # ================================================================== variables
    def _definir_variables(self):
        excl = set(self.excluir) | set(self.campo.columnas)
        if not self.incluir_particiones:
            excl |= set(self.cols_particion)
        if self.variables_input:
            pedidas = [v.lower() for v in self.variables_input]
            faltan = [v for v in pedidas if v not in self.tipos]
            if faltan:
                raise ValueError("Variables inexistentes: %s" % faltan)
            cand = [v for v in pedidas if v not in set(self.excluir)]
        else:
            cand = [c for c in self.columnas["columna"] if c not in excl]
        clases = dict(zip(self.columnas["columna"], self.columnas["clase"]))
        self.variables = [(c, clases[c]) for c in cand if clases[c] != "compleja"]
        omitidas = [c for c in cand if clases[c] == "compleja"]
        if not self.variables:
            raise ValueError("No hay variables para perfilar")
        self.lotes = _lotes(self.variables, self.lote_vars)
        n_num = sum(1 for _, c in self.variables if c == "numerica")
        self._p("      Variables: %d (%d numéricas, %d categóricas, %d fecha) en %d lote(s)%s"
                % (len(self.variables), n_num, sum(1 for _, c in self.variables if c == "categorica"),
                   sum(1 for _, c in self.variables if c == "fecha"), len(self.lotes),
                   ("; omitidas por tipo complejo: %s" % omitidas) if omitidas else ""))
        self._definir_llave()
        self._definir_reglas(clases)
        self.psi_esp_def = M.normalizar_psi_esperado(self.psi_esperado, dict(self.variables))
        if self.psi_esp_def:
            self._p("      PSI-alpha (vs. distribución esperada) para: %s" % ", ".join(self.psi_esp_def))

    def _definir_llave(self):
        """Valida la llave y define la versión sin columnas de periodo para la cobertura.

        La unicidad se mide DENTRO de cada periodo, así que incluir el campo fecha (ej. fa) en la llave
        no cambia el resultado. Para la cobertura entre periodos sí hay que quitarlo: si no, ninguna llave
        de un periodo coincide con la del anterior y la cobertura daría 0%.
        """
        if not self.llave:
            self.llave_cobertura = []
            return
        self.llave = [k.strip().lower() for k in self.llave]
        faltan = [k for k in self.llave if k not in self.tipos]
        if faltan:
            raise ValueError("Columnas de la llave inexistentes: %s" % faltan)
        periodo_cols = set(self.campo.columnas) | set(self.cols_particion)
        self.llave_cobertura = [k for k in self.llave if k not in periodo_cols]
        quitadas = [k for k in self.llave if k in periodo_cols]
        if self.cobertura and not self.llave_cobertura:
            self.cobertura = False
            self._p("      Cobertura omitida: la llave solo tiene columnas de periodo (%s)" % quitadas)
        self._p("      Llave de unicidad: %s%s" % (", ".join(self.llave),
                ("  (cobertura entre periodos usa: %s)" % ", ".join(self.llave_cobertura)) if quitadas and self.cobertura else ""))

    def _definir_reglas(self, clases):
        """Une reglas entre campos y reglas de dominio en un dict nombre -> SQL booleano."""
        sql_reglas = {}
        for var, dom in self.reglas_dominio.items():
            v = var.lower()
            if v not in self.tipos:
                raise ValueError("reglas_dominio: la columna %s no existe" % var)
            c = S.q(v)
            if clases.get(v) == "numerica":
                x = "CAST(%s AS DOUBLE)" % c
                conds = (["%s >= %s" % (x, S.num(dom["min"]))] if dom.get("min") is not None else []) + \
                        (["%s <= %s" % (x, S.num(dom["max"]))] if dom.get("max") is not None else [])
            else:
                conds = (["%s >= %s" % (c, S.lit(dom["min"]))] if dom.get("min") is not None else []) + \
                        (["%s <= %s" % (c, S.lit(dom["max"]))] if dom.get("max") is not None else [])
            if dom.get("valores"):
                conds.append("CAST(%s AS STRING) IN (%s)" % (c, ", ".join(S.lit(x) for x in dom["valores"])))
            if conds:
                sql_reglas["dominio_%s" % v] = "%s IS NULL OR (%s)" % (c, " AND ".join(conds))
        for nombre, expr in self.reglas.items():
            sql_reglas[re.sub(r"[^a-zA-Z0-9_]", "_", nombre)] = expr
        self.sql_reglas = sql_reglas
        # columnas que mencionan las reglas (para incluirlas al materializar)
        texto = " ".join(sql_reglas.values()).lower()
        self.cols_reglas = sorted(c for c in self.tipos if re.search(r"\b%s\b" % re.escape(c), texto))
        if sql_reglas:
            self._p("      Reglas de negocio: %d (%s)" % (len(sql_reglas), ", ".join(sql_reglas)))

    # ================================================================== temporales
    def _tmp(self, fase):
        return "%s_%s" % (self.prefijo_tmp, fase)

    def _crear_temporales(self):
        self._p("[4/7] Tablas temporales en %s (prefijo %s_*)" % (self.zona_tmp, self.prefijo_tmp.split(".")[1]))
        fases = ["stats", "pct", "bins", "forma"] + (["uniq"] if self.llave else []) + \
            (["reglas"] if self.sql_reglas else []) + (["cobert"] if self.cobertura else []) + \
            (["besp"] if self.psi_esp_def else [])
        for f in fases:
            nombre = self._tmp(f)
            self.con.ejecutar(S.crear_tabla(nombre, f))
            if nombre not in self.estado["temporales"]:
                self.estado["temporales"].append(nombre)
        if self.materializar and self._tmp("src") not in self.estado["temporales"]:
            self.estado["temporales"].append(self._tmp("src"))
        self._guardar_estado()

    # ================================================================== tareas
    def _tid(self, fase, periodo, lote):
        return "%s|%s|%s" % (fase, periodo, lote)

    def _archivo(self, fase, periodo, lote):
        return os.path.join(self.dir_tareas, "%s_%s_%s.csv" % (fase, periodo, lote))

    def _ok(self, fase, periodo, lote):
        t = self.estado["tareas"].get(self._tid(fase, periodo, lote), {})
        return t.get("estado") == "ok" and os.path.exists(self._archivo(fase, periodo, lote))

    def _registrar(self, tid, **kw):
        t = self.estado["tareas"].setdefault(tid, {"intentos": 0})
        t.update(kw)
        self._guardar_estado()

    def _leer_tmp(self, fase, periodo, variables):
        w = "periodo = %d" % periodo
        if variables is not None:
            w += " AND variable IN (%s)" % ", ".join(S.lit(v) for v in variables)
        df = self.con.df("SELECT * FROM %s WHERE %s" % (self._tmp(fase), w))
        df.columns = [c.lower() for c in df.columns]
        return df.drop_duplicates(S.LLAVES[fase])

    def _tarea(self, fase, periodo, lote, select, variables):
        """INSERT a temporal + lectura + checkpoint. Devuelve True/False."""
        tid = self._tid(fase, periodo, lote)
        if self._ok(fase, periodo, lote):
            return True
        previo = self.estado["tareas"].get(tid, {})
        t0 = time.time()
        try:
            ya_insertado = False
            if previo.get("intentos", 0) > 0:  # pudo insertarse y fallar después: no duplicar
                w = "periodo = %d" % periodo
                if variables is not None:
                    w += " AND variable IN (%s)" % ", ".join(S.lit(v) for v in variables)
                c = self.con.df("SELECT COUNT(*) AS c FROM %s WHERE %s" % (self._tmp(fase), w))
                ya_insertado = int(c.iloc[0, 0]) > 0
            if not ya_insertado:
                self.con.ejecutar(S.insertar(self._tmp(fase), select))
            df = self._leer_tmp(fase, periodo, variables)
            df.to_csv(self._archivo(fase, periodo, lote), index=False)
            self._registrar(tid, estado="ok", intentos=previo.get("intentos", 0) + 1,
                            duracion=round(time.time() - t0, 1), error=None, fin=str(dt.datetime.now()))
            self._p("      ✓ %-6s %s lote %s  (%.1fs)" % (fase, periodo, lote, time.time() - t0))
            return True
        except Exception as exc:
            self._registrar(tid, estado="error", intentos=previo.get("intentos", 0) + 1,
                            duracion=round(time.time() - t0, 1), error=str(exc)[:1000], fin=str(dt.datetime.now()))
            self._p("      ✗ %-6s %s lote %s  FALLÓ: %s" % (fase, periodo, lote, str(exc)[:200]), "error")
            return False

    def _tarea_df(self, fase, periodo, lote, query):
        """Tarea de solo lectura (sin temporal)."""
        tid = self._tid(fase, periodo, lote)
        if self._ok(fase, periodo, lote):
            return True
        previo = self.estado["tareas"].get(tid, {})
        t0 = time.time()
        try:
            df = self.con.df(query)
            df.columns = [c.lower() for c in df.columns]
            df.to_csv(self._archivo(fase, periodo, lote), index=False)
            self._registrar(tid, estado="ok", intentos=previo.get("intentos", 0) + 1,
                            duracion=round(time.time() - t0, 1), error=None)
            self._p("      ✓ %-6s %s lote %s  (%.1fs)" % (fase, periodo, lote, time.time() - t0))
            return True
        except Exception as exc:
            self._registrar(tid, estado="error", intentos=previo.get("intentos", 0) + 1, error=str(exc)[:1000])
            self._p("      ✗ %-6s %s lote %s  FALLÓ: %s" % (fase, periodo, lote, str(exc)[:200]), "error")
            return False

    def _leer_checkpoint(self, fase):
        dfs = []
        for tid, t in self.estado["tareas"].items():
            f, p, l = tid.split("|")
            if f == fase and t.get("estado") == "ok" and os.path.exists(self._archivo(f, p, l)):
                d = _leer_csv(self._archivo(f, p, l))
                if not d.empty:
                    dfs.append(d)
        if not dfs:
            return pd.DataFrame()
        df = pd.concat(dfs, ignore_index=True)
        if fase in S.LLAVES:
            df = df.drop_duplicates(S.LLAVES[fase])
        return df

    # ================================================================== 4. procesamiento
    def _tareas_periodo(self, p, con_bins):
        """Lista de (fase, lote, carga) pendientes para el periodo p."""
        pend = []
        for i, lote in enumerate(self.lotes):
            if not self._ok("stats", p, i):
                pend.append(("stats", i, lote))
            nums = [c for c, k in lote if k == "numerica"]
            for j, sub in enumerate(_lotes(nums, self.lote_pct)):
                lid = "%d_%d" % (i, j)
                if sub and not self._ok("pct", p, lid):
                    pend.append(("pct", lid, sub))
            if con_bins:
                defs = [self.bins_def[c] for c, _ in lote if c in self.bins_def]
                if defs and not self._ok("bins", p, i):
                    pend.append(("bins", i, defs))
                fdefs = [self.forma_def[c] for c, _ in lote if c in self.forma_def]
                if fdefs and not self._ok("forma", p, i):
                    pend.append(("forma", i, fdefs))
        for i, lote in enumerate(self.lotes):
            edefs = [self.psi_esp_def[c] for c, _ in lote if c in self.psi_esp_def]
            if edefs and not self._ok("besp", p, i):
                pend.append(("besp", i, edefs))
        if self.llave and not self._ok("uniq", p, 0):
            pend.append(("uniq", 0, None))
        if self.sql_reglas and not self._ok("reglas", p, 0):
            pend.append(("reglas", 0, None))
        if self.cobertura:
            previos = [x for x in self.periodos if x < p]
            if previos and not self._ok("cobert", p, 0):
                pend.append(("cobert", 0, previos[-1]))
        return pend

    def _correr_periodo(self, p, con_bins, extra=None):
        pend = self._tareas_periodo(p, con_bins) + (extra or [])
        if not pend:
            return
        g = self.campo.granularidad
        filtro = self._where(self.campo.filtro_periodo(p))
        limite_futuro = (P.ultimo_dia(p, g) + dt.timedelta(days=1)).isoformat()
        src, where = self.tabla, filtro
        if self.materializar:
            cols = sorted(set([c for c, _ in self.variables] + (self.llave or []) + self.cols_reglas))
            try:
                self.con.ejecutar(S.borrar_tabla(self._tmp("src")))
                self.con.ejecutar(S.materializar(self._tmp("src"), self.tabla, cols, filtro))
                src, where = self._tmp("src"), "1=1"
            except Exception as exc:
                self._p("      materialización falló (%s); se consulta la fuente directo" % str(exc)[:150], "warning")
        for fase, lote, carga in pend:
            if fase == "stats":
                self._tarea("stats", p, lote, S.stats(src, where, p, carga, limite_futuro, self.nulos_disfrazados),
                            [c for c, _ in carga])
            elif fase == "pct":
                self._tarea("pct", p, lote, S.percentiles(src, where, p, carga, self.n_tiles), carga)
            elif fase == "bins":
                self._tarea("bins", p, lote, S.bins(src, where, p, carga), [d["variable"] for d in carga])
            elif fase == "besp":
                self._tarea("besp", p, lote, S.bins(src, where, p, carga), [d["variable"] for d in carga])
            elif fase == "forma":
                self._tarea("forma", p, lote, S.forma(src, where, p, carga), [d["variable"] for d in carga])
            elif fase == "uniq":
                self._tarea("uniq", p, 0, S.unicidad(src, where, p, self.llave), None)
            elif fase == "reglas":
                self._tarea("reglas", p, 0, S.reglas(src, where, p, self.sql_reglas), list(self.sql_reglas))
            elif fase == "cobert":
                ant = carga
                w_ant = self._where(self.campo.filtro_periodo(ant))
                self._tarea("cobert", p, 0, S.cobertura(src, where, p, self.tabla, w_ant, ant, self.llave_cobertura), None)
        if self.materializar and src != self.tabla:
            try:
                self.con.ejecutar(S.borrar_tabla(self._tmp("src")))
            except Exception:
                pass

    def _procesar_periodos(self):
        g = self.campo.granularidad
        base = self.periodo_base
        orden = [base] + [p for p in self.periodos if p != base]
        self._p("[5/7] Perfilado de %d periodos (base %s primero)" % (len(orden), P.etiqueta(base, g)))
        self.bins_def, self.forma_def = {}, {}

        # --- periodo base: stats + pct, luego definiciones que dependen del base (cortes, cercas, centinelas)
        self._p("  ▸ %s (BASE)" % P.etiqueta(base, g))
        self._correr_periodo(base, con_bins=False)
        self._definir_bins(base)
        self._correr_periodo(base, con_bins=self.base_completo)

        for k, p in enumerate(orden[1:], start=2):
            self._p("  ▸ %s  (%d/%d)" % (P.etiqueta(p, g), k, len(orden)))
            self._correr_periodo(p, con_bins=self.base_completo)
        if not self.base_completo:
            self._p("  ! El periodo base tuvo tareas fallidas: PSI/outliers pendientes. Re-ejecute para reanudar.",
                    "warning")

    # fases del base de las que dependen cortes PSI, cercas y centinelas
    _FASES_BASE = ("stats", "pct", "topcat", "topnum")

    def _definir_bins(self, base):
        """Definiciones que salen del periodo base y se persisten en estado.json:
        cortes PSI numéricos (NTILE), top-K categórico, cercas de Tukey, centro/escala para momentos,
        candidatos a valor centinela y pares de columnas posiblemente idénticas."""
        meta = self.estado["meta"]
        st = self._leer_checkpoint("stats")
        st_base = st[st["periodo"] == base] if not st.empty else st
        cats_base = [c for c, k in self.variables if k == "categorica"]
        nums = [c for c, k in self.variables if k == "numerica"]
        ndv = dict(zip(st_base["variable"], st_base["ndv"])) if not st_base.empty else {}
        lim = self.umbrales["card_max_categorica"]
        # PSI categórico solo con categorías que se repiten: con casi un valor por fila (códigos, nombres)
        # los buckets del top-K tienen 1-2 casos y el PSI es puro ruido
        nn_base = dict(zip(st_base["variable"], st_base["n"] - st_base["n_nulos"])) if not st_base.empty else {}
        # (con una base chica no se puede juzgar: 3 filas con 3 marcas distintas no hacen a la variable "dispersa")
        dispersa = lambda c: ndv.get(c, 0) > lim or (nn_base.get(c, 0) >= 100 and ndv.get(c, 0) > 0.5 * nn_base.get(c, 0))
        self.alta_card = [c for c in cats_base if c in ndv and dispersa(c)]
        cats_psi = [c for c in cats_base if c in ndv and not dispersa(c)]
        filtro = self._where(self.campo.filtro_periodo(base))
        for i, sub in enumerate(_lotes(cats_psi, self.lote_vars)):
            self._tarea_df("topcat", base, i, S.top_categorias(self.tabla, filtro, sub, self.top_k))
        for i, sub in enumerate(_lotes([c for c in nums if c in ndv], self.lote_vars)):
            self._tarea_df("topnum", base, i, S.top_numericos(self.tabla, filtro, sub, 5))
        # llave de cobertura: quitar columnas constantes dentro del periodo base (ej. fa como columna aparte).
        # No distinguen filas dentro del periodo y, si cambian entre periodos, impiden que las llaves coincidan.
        if self.llave:
            if "llave_cobertura" in meta:
                self.llave_cobertura = meta["llave_cobertura"]
            elif not st_base.empty:
                const = set(st_base.loc[st_base["ndv"].fillna(0) <= 1, "variable"])
                quitadas = [k for k in self.llave_cobertura if k in const]
                self.llave_cobertura = [k for k in self.llave_cobertura if k not in const]
                meta["llave_cobertura"] = self.llave_cobertura
                if quitadas:
                    self._p("      Cobertura entre periodos sin %s (constantes dentro del periodo): usa %s"
                            % (quitadas, self.llave_cobertura or "-"))
            if self.cobertura and not self.llave_cobertura:
                self.cobertura = False
                self._p("      Cobertura omitida: la llave no tiene columnas que identifiquen entidades entre periodos")
        # columnas idénticas: candidatos por firma de estadísticos, verificación exacta en Impala
        pares = M.pares_candidatos_identicas(st_base, dict(self.variables))
        if pares:
            self._tarea_df("dupcol", base, 0, S.columnas_identicas(self.tabla, filtro, pares))
        fallidas_base = [tid for tid, t in self.estado["tareas"].items()
                         if tid.split("|")[0] in self._FASES_BASE and tid.split("|")[1] == str(base)
                         and t.get("estado") != "ok"]
        self.base_completo = not fallidas_base
        if meta.get("bins_def_completo") and "forma_def" in meta:
            self.bins_def, self.forma_def = meta["bins_def"], meta["forma_def"]
            return
        if not self.base_completo:
            return
        pct = self._leer_checkpoint("pct")
        pct_base = pct[pct["periodo"] == base] if not pct.empty else pct
        topnum = self._leer_checkpoint("topnum")
        defs, fdefs = {}, {}
        for c in nums:
            g = pct_base[pct_base["variable"] == c] if not pct_base.empty else pct_base
            defs[c] = {"variable": c, "clase": "numerica", "cortes": M.cortes_psi(g, self.n_bins)}
            sb = st_base[st_base["variable"] == c]
            tn = topnum[topnum["variable"] == c] if not topnum.empty else topnum
            fdefs[c] = M.definir_forma(c, g, sb.iloc[0] if not sb.empty else None, tn, self.umbrales)
        top = self._leer_checkpoint("topcat")
        for c in cats_psi:
            vals = top.loc[top["variable"] == c, "valor"].astype(str).tolist() if not top.empty else []
            defs[c] = {"variable": c, "clase": "categorica", "categorias": vals}
        self.bins_def, self.forma_def = defs, fdefs
        meta.update(bins_def=defs, forma_def=fdefs, bins_def_completo=True, alta_cardinalidad=self.alta_card)
        self._guardar_estado()

    # ================================================================== 5. consolidado
    def _consolidar(self):
        self._p("[6/7] Consolidando métricas")
        g = self.campo.granularidad
        st = M.enriquecer_stats(self._leer_checkpoint("stats"))
        pct = self._leer_checkpoint("pct")
        perc = M.percentiles_desde_tiles(pct)
        if st is not None and not st.empty and not perc.empty:
            st = st.merge(perc, on=["periodo", "variable"], how="left")
        bins = self._leer_checkpoint("bins")
        n_per = st.groupby("periodo")["n"].max().to_dict() if st is not None and not st.empty else {}
        psi = M.tabla_psi(bins, n_per, self.periodo_base, self.umbrales)
        pa = M.tabla_psi_alpha(self._leer_checkpoint("besp"), getattr(self, "psi_esp_def", {}), self.umbrales)
        if not pa.empty:
            psi = psi.merge(pa, on=["periodo", "variable"], how="outer").sort_values(["variable", "periodo"])
        for c in ("psi_alpha", "psi_alpha_critico", "signif_alpha"):
            if c not in psi:
                psi[c] = np.nan
        uniq = self._leer_checkpoint("uniq")
        if not uniq.empty:
            uniq["duplicados"] = uniq["n"] - uniq["n_distintos"]
            uniq["pct_duplicados"] = uniq["duplicados"] / uniq["n"].replace(0, float("nan"))
        vol = M.volumen_enriquecido(self.vol, g, self.huecos)
        fallidas = []
        for tid, t in self.estado["tareas"].items():
            if t.get("estado") != "ok":
                f, p, l = tid.split("|")
                fallidas.append({"fase": f, "periodo": int(p), "lote": l, "error": t.get("error", ""),
                                 "intentos": t.get("intentos")})
        # --- coherencia
        clases = dict(self.variables)
        forma = M.consolidar_forma(self._leer_checkpoint("forma"), st)
        reglas = M.tabla_reglas(self._leer_checkpoint("reglas"), getattr(self, "sql_reglas", {}))
        cobert = M.tabla_cobertura(self._leer_checkpoint("cobert"))
        topcat = self._leer_checkpoint("topcat")
        texto = M.tabla_texto(st, topcat, clases)
        fechas = M.tabla_fechas(st, clases)
        regimen = M.tabla_regimen(psi, self.umbrales)
        otros = M.tabla_otros(bins, self.bins_def, self.periodo_base)
        centinelas = []
        for v, d in (self.forma_def or {}).items():
            for c in d.get("centinelas_info", []):
                centinelas.append(dict(c, variable=v))
        dup = self._leer_checkpoint("dupcol")
        identicas = [(r["col_a"], r["col_b"]) for _, r in dup.iterrows() if r["n_dif"] == 0] if not dup.empty else []
        parece_id, posible_cat = [], []
        if st is not None and not st.empty:
            u = self.umbrales
            for v, g in st.groupby("variable"):
                nn = (g["n"] - g["n_nulos"]).astype(float)
                # la proporción distintos/filas solo es confiable con periodos grandes: con 16 filas cualquier
                # código de 3 dígitos parece único
                grandes = nn >= 100
                ratio = (g["ndv"][grandes] / nn[grandes]).max() if grandes.any() else 0.0
                es_entera = P.tipo_base(self.tipos.get(v, "")) in ("tinyint", "smallint", "int", "integer", "bigint")
                # un monto entero también puede tener casi un valor distinto por fila: en numéricas se exige
                # además que el nombre sugiera un identificador
                nombre_id = re.search(r"(^|_)(id|cod|codigo|num|numero|nro|number|key|llave|cuenta|tarjeta|dpi|nit)(_|$)",
                                      v) is not None
                if clases.get(v) == "categorica" or (es_entera and nombre_id):
                    if ratio >= u["id_ratio"] and nn.sum() >= u["id_min_n"]:
                        parece_id.append(v)
                if es_entera and 2 < g["ndv"].max() <= u["categorica_ndv_max"]:
                    posible_cat.append(v)
        alertas = M.generar_alertas({
            "vol": vol, "huecos": self.huecos, "stats": st, "psi": psi, "uniq": uniq,
            "tareas_fallidas": fallidas, "periodo_base": self.periodo_base,
            "alta_cardinalidad": getattr(self, "alta_card", []),
        }, self.umbrales)
        extra = M.alertas_coherencia({
            "forma": forma, "stats": st, "centinelas": centinelas, "texto": texto, "fechas": fechas,
            "reglas": reglas, "cobertura": cobert, "regimen": regimen, "otros": otros, "parece_id": parece_id,
            "posible_categorica": posible_cat, "identicas": identicas, "vol": vol,
            "periodo_base": self.periodo_base,
        }, self.umbrales)
        vol_n = vol["n"].dropna()
        if len(vol_n) > 3 and vol_n.median() < self.umbrales["muestra_min_periodo"]:
            mayor = {"dia": "mes", "mes": "anio"}.get(self.campo.granularidad)
            extra.append(dict(nivel="INFO", tipo="muestra_pequena", variable=None, periodo=None,
                              valor=float(vol_n.median()), umbral=self.umbrales["muestra_min_periodo"],
                              detalle="Mediana de %.0f filas por periodo: PSI y variaciones son poco confiables%s"
                              % (vol_n.median(), (". Pruebe granularidad='%s'" % mayor) if mayor else "")))
        for a in self.alertas_extra:   # alertas de lectura de archivo: (nivel, tipo, detalle[, variable])
            extra.append(dict(nivel=a[0], tipo=a[1], detalle=a[2], variable=a[3] if len(a) > 3 else None,
                              periodo=None, valor=None, umbral=None))
        # ruido que no aplica: en la llave declarada no interesan outliers, PSI ni "¿categórica?";
        # un código con ceros a la izquierda es texto a propósito (no es "tipo mal declarado")
        no_llave = {"outliers_extremos", "salto_outliers", "cola_pesada", "posible_categorica", "masa_en_extremo",
                    "texto_numerico", "parece_id", "psi", "centinela", "categorias_nuevas", "alta_cardinalidad"}
        ceros = set()
        if self.info_lectura is not None:
            inf = self.info_lectura["inferencia"]
            if "ceros_izquierda" in inf:
                ceros = set(inf.loc[inf["ceros_izquierda"].fillna(0) > 0, "columna"])
        llave_set = set(self.llave or [])
        ids = set(parece_id) - llave_set            # identificadores detectados: se avisa parece_id, no su PSI
        no_id = no_llave - {"parece_id", "alta_cardinalidad", "texto_numerico"}

        def _ruido(var, tipo):
            return (var in llave_set and tipo in no_llave) or (var in ids and tipo in no_id) or \
                (var in ceros and tipo == "texto_numerico")
        extra = [a for a in extra if not _ruido(a["variable"], a["tipo"])]
        if not alertas.empty:
            alertas = alertas[[not _ruido(v, t) for v, t in zip(alertas["variable"], alertas["tipo"])]]
        if extra:
            alertas = pd.concat([alertas, pd.DataFrame(extra, columns=alertas.columns)], ignore_index=True)
            orden = {"ALTA": 0, "MEDIA": 1, "INFO": 2}
            alertas = alertas.assign(_o=alertas["nivel"].map(orden)).sort_values(
                ["_o", "tipo", "variable", "periodo"], na_position="first").drop(columns="_o").reset_index(drop=True)
        alertas = M.agrupar_alertas(alertas, self.umbrales)
        self.resultado.update({

            "tabla": self.tabla, "run_key": self.run_key, "campo": self.campo.a_dict(),
            "modo": self.modo, "rango": self.rango, "periodo_base": self.periodo_base,
            "periodos": self.periodos, "huecos": self.huecos, "columnas": self.columnas,
            "particiones": self.particiones, "cols_particion": self.cols_particion,
            "volumen": vol, "stats": st, "percentiles": perc, "bins": bins, "psi": psi, "uniq": uniq,
            "alertas": alertas, "tareas": self._tabla_tareas(), "fallidas": fallidas,
            "bins_def": self.bins_def, "forma_def": self.forma_def, "umbrales": self.umbrales,
            "variables": self.variables, "tipos": self.tipos,
            "llave": self.llave, "fecha_ejecucion": str(dt.datetime.now())[:19],
            "zona_temporal": self.zona_tmp, "n_tiles": self.n_tiles, "n_bins": self.n_bins,
            "filtro_extra": self.filtro_extra,
            "forma": forma, "reglas": reglas, "sql_reglas": getattr(self, "sql_reglas", {}), "cobertura": cobert,
            "texto": texto, "fechas": fechas, "regimen": regimen, "otros": otros,
            "centinelas": pd.DataFrame(centinelas, columns=["variable", "valor", "pct_base", "motivo"]),
            "identicas": pd.DataFrame(identicas, columns=["col_a", "col_b"]),
            "nulos_disfrazados": self.nulos_disfrazados,
            "lectura": self.info_lectura,
            "psi_esp_def": getattr(self, "psi_esp_def", {}), "besp": self._leer_checkpoint("besp"),
        })
        self.resultado["resumen_variables"] = M.ficha_variables(self._resumen_variables(), alertas, self.llave)
        from .limpieza import vista_limpia
        self.resultado["vista_limpia_sql"] = vista_limpia(self.resultado)

    def _tabla_tareas(self):
        filas = []
        for tid, t in self.estado["tareas"].items():
            f, p, l = tid.split("|")
            filas.append(dict(fase=f, periodo=int(p), lote=l, estado=t.get("estado"), intentos=t.get("intentos"),
                              duracion=t.get("duracion"), error=t.get("error")))
        return pd.DataFrame(filas, columns=["fase", "periodo", "lote", "estado", "intentos", "duracion", "error"])

    def _resumen_variables(self):
        st, ps, al = self.resultado["stats"], self.resultado["psi"], self.resultado["alertas"]
        filas = []
        clases = dict(self.variables)
        for v, clase in self.variables:
            r = {"variable": v, "clase": clase, "tipo_sql": self.tipos.get(v)}
            g = st[st["variable"] == v].sort_values("periodo") if st is not None and not st.empty else pd.DataFrame()
            if not g.empty:
                u = g.iloc[-1]
                r.update(periodos=len(g), pct_nulos_prom=g["pct_nulos"].mean(), pct_nulos_ult=u["pct_nulos"],
                         n_inf_total=g["n_inf"].fillna(0).sum() + g["n_nan"].fillna(0).sum(),
                         ndv_ult=u["ndv"], media_ult=u.get("media"), std_ult=u.get("std"),
                         min_hist=g["minimo"].min(), max_hist=g["maximo"].max(),
                         p50_ult=u.get("p50") if "p50" in g else None)
            gp = ps[ps["variable"] == v] if ps is not None and not ps.empty else pd.DataFrame()
            if not gp.empty:
                gs = gp.sort_values("periodo")
                r.update(psi_max=gs["psi_anterior"].max(), psi_ult=gs["psi_anterior"].iloc[-1],
                         periodos_psi_signif=int((gs["signif_anterior"] == True).sum()),  # noqa: E712
                         psi_alpha_max=gs["psi_alpha"].max(), psi_alpha_ult=gs["psi_alpha"].iloc[-1])
            fo = self.resultado.get("forma")
            gf = fo[fo["variable"] == v].sort_values("periodo") if fo is not None and not fo.empty else pd.DataFrame()
            if not gf.empty:
                r.update(pct_fuera_3_max=gf["pct_fuera_3"].max(), pct_fuera_3_ult=gf["pct_fuera_3"].iloc[-1],
                         asimetria_ult=gf["asimetria"].iloc[-1], curtosis_ult=gf["curtosis_exceso"].iloc[-1],
                         pct_centinela_max=gf["pct_centinela"].max())
            if not g.empty and clase == "categorica":
                nn = (g["n"] - g["n_nulos"]).replace(0, float("nan")).astype(float)
                r["pct_disfrazados_max"] = (g["n_disfrazados"] / nn).max()
            if not g.empty:
                nn = (g["n"] - g["n_nulos"]).replace(0, float("nan")).astype(float)
                r["ndv_ratio_max"] = (g["ndv"] / nn).max()
            r["alertas"] = int((al["variable"] == v).sum()) if al is not None and not al.empty else 0
            filas.append(r)
        return pd.DataFrame(filas)

    # ================================================================== 6. reporte / limpieza
    def _reporte(self):
        from .reporte import generar_reportes
        self._p("[7/7] Generando reportes")
        rutas = generar_reportes(self.resultado, self.dir_run)
        self.resultado.update(rutas)
        for k, v in rutas.items():
            self._p("      %s: %s" % (k, os.path.abspath(v)))

    def _limpiar_temporales(self, forzar=False):
        hay_fallas = bool(self.resultado.get("fallidas"))
        if not forzar and (self.limpiar == "nunca" or (self.limpiar == "exito" and hay_fallas)):
            self._p("      Temporales conservadas (%s) para poder reanudar: %s"
                    % ("hubo fallas" if hay_fallas else "limpiar='nunca'", ", ".join(self.estado["temporales"])))
            return
        for t in list(self.estado["temporales"]):
            try:
                self.con.ejecutar(S.borrar_tabla(t))
                self.estado["temporales"].remove(t)
            except Exception as exc:
                self._p("      No se pudo borrar %s: %s" % (t, str(exc)[:150]), "warning")
        self._guardar_estado()
        self._p("      Temporales eliminadas")

    def limpiar_temporales(self):
        """Borra las temporales de este run aunque haya fallas."""
        if self.estado is None:
            self.estado = self._cargar_estado()
        self.resultado.setdefault("fallidas", [])
        self._limpiar_temporales(forzar=True)

    def _resumen_final(self):
        f = self.resultado.get("fallidas", [])
        al = self.resultado.get("alertas")
        self._p("-" * 78)
        self._p("Terminado en %.1fs | tareas fallidas: %d | alertas: %s"
                % (self.resultado.get("duracion_seg", 0), len(f),
                   al["nivel"].value_counts().to_dict() if al is not None and not al.empty else 0))
        if f:
            self._p("Para reanudar solo lo pendiente: vuelva a ejecutar con los mismos parámetros.")

    # ================================================================== notebook
    def mostrar(self, alto=900):
        """Muestra el reporte HTML dentro del notebook."""
        from IPython.display import IFrame, display
        ruta = self.resultado.get("html")
        if not ruta:
            raise RuntimeError("Primero ejecute .ejecutar()")
        display(IFrame(os.path.relpath(ruta), width="100%", height=alto))
