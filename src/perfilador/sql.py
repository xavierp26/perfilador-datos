"""
Constructores de SQL (dialecto Impala).

Principio: todo lo pesado (conteos, nulos, momentos, NTILE, buckets PSI) se calcula en
Impala y solo bajan agregados pequeños. Cada consulta por lote es un único
INSERT ... SELECT ... UNION ALL ..., que en Impala es atómico: o quedan todas las filas
del lote o ninguna, así que reintentar no duplica.

Las tablas Parquet son columnares: cada rama del UNION ALL lee solo su columna, por lo
que N ramas cuestan aprox. lo mismo que un escaneo de las N columnas.
"""

# --------------------------------------------------------------------------- DDL temporales
# Columnas de la fase stats en orden fijo: todas las ramas del UNION ALL las emiten igual.
COLS_STATS = [
    ("periodo", "BIGINT"), ("variable", "STRING"), ("tipo", "STRING"),
    ("n", "BIGINT"), ("n_nulos", "BIGINT"), ("n_inf", "BIGINT"), ("n_nan", "BIGINT"),
    ("n_ceros", "BIGINT"), ("n_negativos", "BIGINT"), ("n_vacios", "BIGINT"), ("ndv", "BIGINT"),
    ("minimo", "DOUBLE"), ("maximo", "DOUBLE"), ("media", "DOUBLE"), ("std", "DOUBLE"), ("mediana_appx", "DOUBLE"),
    ("long_min", "DOUBLE"), ("long_max", "DOUBLE"), ("long_media", "DOUBLE"),
    ("min_txt", "STRING"), ("max_txt", "STRING"),
    # calidad de texto / tipo
    ("n_disfrazados", "BIGINT"), ("n_espacios", "BIGINT"), ("ndv_norm", "BIGINT"),
    ("n_castable_num", "BIGINT"), ("n_patron_fecha", "BIGINT"),
    # fechas
    ("n_fecha_futura", "BIGINT"), ("n_fecha_antigua", "BIGINT"), ("n_fecha_centinela", "BIGINT"),
]
COLS_FORMA = [
    ("periodo", "BIGINT"), ("variable", "STRING"), ("n_validos", "BIGINT"),
    ("n_bajo_15", "BIGINT"), ("n_alto_15", "BIGINT"), ("n_bajo_3", "BIGINT"), ("n_alto_3", "BIGINT"),
    ("n_centinela", "BIGINT"), ("e1", "DOUBLE"), ("e2", "DOUBLE"), ("e3", "DOUBLE"), ("e4", "DOUBLE"),
]
DDL = {
    "stats": ", ".join("%s %s" % c for c in COLS_STATS),
    "pct": "periodo BIGINT, variable STRING, tile INT, vmin DOUBLE, vmax DOUBLE, n BIGINT",
    "bins": "periodo BIGINT, variable STRING, bin STRING, n BIGINT",
    "uniq": "periodo BIGINT, n BIGINT, n_distintos BIGINT",
    "forma": ", ".join("%s %s" % c for c in COLS_FORMA),
    "reglas": "periodo BIGINT, variable STRING, n BIGINT, n_violaciones BIGINT, n_indeterminado BIGINT",
    "cobert": "periodo BIGINT, periodo_ant BIGINT, n_actual BIGINT, n_anterior BIGINT, n_comunes BIGINT",
    "besp": "periodo BIGINT, variable STRING, bin STRING, n BIGINT",   # buckets de la distribución esperada
}
LLAVES = {
    "stats": ["periodo", "variable"],
    "pct": ["periodo", "variable", "tile"],
    "bins": ["periodo", "variable", "bin"],
    "uniq": ["periodo"],
    "forma": ["periodo", "variable"],
    "reglas": ["periodo", "variable"],   # variable = nombre de la regla
    "cobert": ["periodo"],
    "besp": ["periodo", "variable", "bin"],
}

# Valores de fecha usados como "sin dato" en sistemas core
FECHAS_CENTINELA = ("1900-01-01", "1899-12-31", "1753-01-01", "1970-01-01", "2999-12-31", "9999-12-31")
PATRON_FECHA = "^(19|20)[0-9]{2}-?(0[1-9]|1[0-2])-?(0[1-9]|[12][0-9]|3[01])"


def q(nombre):
    """Cita un identificador con backticks (columnas con nombres raros)."""
    return "`%s`" % str(nombre).replace("`", "")


def lit(texto):
    """Literal de texto SQL escapado."""
    return "'%s'" % str(texto).replace("\\", "\\\\").replace("'", "\\'")


def num(x):
    return repr(float(x))


def crear_tabla(nombre, fase):
    return "CREATE TABLE IF NOT EXISTS %s (%s) STORED AS PARQUET" % (nombre, " ".join(DDL[fase].split()))


def borrar_tabla(nombre):
    return "DROP TABLE IF EXISTS %s PURGE" % nombre


def materializar(nombre, tabla, columnas, where):
    cols = ", ".join(q(c) for c in columnas)
    return "CREATE TABLE %s STORED AS PARQUET AS SELECT %s FROM %s WHERE %s" % (nombre, cols, tabla, where)


# --------------------------------------------------------------------------- utilidades
def _vf(col):
    """Valor finito como DOUBLE (NULL si inf/nan)."""
    d = "CAST(%s AS DOUBLE)" % q(col)
    return "CASE WHEN is_inf(%s) OR is_nan(%s) THEN NULL ELSE %s END" % (d, d, d)


def _txt(col):
    return "CAST(%s AS STRING)" % q(col)


# --------------------------------------------------------------------------- consultas
def describe(tabla):
    return "DESCRIBE %s" % tabla


def muestra_campo(tabla, col, n=5):
    return "SELECT %s AS v FROM %s WHERE %s IS NOT NULL LIMIT %d" % (q(col), tabla, q(col), n)


def volumen(tabla, expr_periodo, where):
    return (
        "SELECT %s AS periodo, CAST(COUNT(*) AS BIGINT) AS n FROM %s WHERE %s GROUP BY 1 ORDER BY 1"
        % (expr_periodo, tabla, where)
    )


def _emitir(cols, valores):
    """Arma el SELECT respetando el orden de columnas; lo que falta va como NULL tipado."""
    partes = []
    for nombre, tipo in cols:
        expr = valores.get(nombre)
        partes.append("%s AS %s" % (expr if expr is not None else "CAST(NULL AS %s)" % tipo, nombre))
    return ", ".join(partes)


def _sum(cond):
    return "CAST(SUM(CASE WHEN %s THEN 1 ELSE 0 END) AS BIGINT)" % cond


def stats(src, where, periodo, variables, limite_futuro=None, disfrazados=()):
    """variables: lista de (columna, clase) con clase in numerica|categorica|fecha.

    limite_futuro: 'YYYY-MM-DD' día siguiente al fin del periodo; una fecha >= a ese día es futura.
    disfrazados: textos que en realidad significan nulo ('N/A', 'NULL', '-', ...).
    """
    ramas = []
    for col, clase in variables:
        c = q(col)
        v = {
            "periodo": "CAST(%d AS BIGINT)" % periodo, "variable": lit(col), "tipo": lit(clase),
            "n": "CAST(COUNT(*) AS BIGINT)", "n_nulos": "CAST(COUNT(*) - COUNT(%s) AS BIGINT)" % c,
        }
        if clase == "numerica":
            d = "CAST(%s AS DOUBLE)" % c
            vf = _vf(col)
            v.update({
                "n_inf": _sum("is_inf(%s)" % d), "n_nan": _sum("is_nan(%s)" % d),
                "n_ceros": _sum("%s = 0" % d), "n_negativos": _sum("%s < 0" % d),
                "ndv": "CAST(NDV(%s) AS BIGINT)" % c,
                "minimo": "CAST(MIN(%s) AS DOUBLE)" % vf, "maximo": "CAST(MAX(%s) AS DOUBLE)" % vf,
                "media": "CAST(AVG(%s) AS DOUBLE)" % vf, "std": "CAST(STDDEV_SAMP(%s) AS DOUBLE)" % vf,
                "mediana_appx": "CAST(APPX_MEDIAN(%s) AS DOUBLE)" % vf,
            })
        elif clase == "categorica":
            t = _txt(col)
            ut = "UPPER(TRIM(%s))" % t
            v.update({
                "n_inf": "CAST(0 AS BIGINT)", "n_nan": "CAST(0 AS BIGINT)",
                "n_vacios": _sum("TRIM(%s) = ''" % t),
                "ndv": "CAST(NDV(%s) AS BIGINT)" % t,
                "long_min": "CAST(MIN(LENGTH(%s)) AS DOUBLE)" % t,
                "long_max": "CAST(MAX(LENGTH(%s)) AS DOUBLE)" % t,
                "long_media": "CAST(AVG(LENGTH(%s)) AS DOUBLE)" % t,
                "min_txt": "CAST(MIN(%s) AS STRING)" % t, "max_txt": "CAST(MAX(%s) AS STRING)" % t,
                "n_disfrazados": _sum("%s IN (%s)" % (ut, ", ".join(lit(x.upper()) for x in disfrazados)))
                if disfrazados else "CAST(0 AS BIGINT)",
                "n_espacios": _sum("%s <> TRIM(%s)" % (t, t)),
                "ndv_norm": "CAST(NDV(%s) AS BIGINT)" % ut,
                "n_castable_num": _sum("CAST(TRIM(%s) AS DOUBLE) IS NOT NULL" % t),
                "n_patron_fecha": _sum("regexp_like(TRIM(%s), %s)" % (t, lit(PATRON_FECHA))),
            })
        else:  # fecha
            ct = "CAST(%s AS STRING)" % c
            v.update({
                "n_inf": "CAST(0 AS BIGINT)", "n_nan": "CAST(0 AS BIGINT)",
                "ndv": "CAST(NDV(%s) AS BIGINT)" % ct,
                "min_txt": "CAST(MIN(%s) AS STRING)" % c, "max_txt": "CAST(MAX(%s) AS STRING)" % c,
                "n_fecha_futura": _sum("%s >= %s" % (c, lit(limite_futuro))) if limite_futuro else None,
                "n_fecha_antigua": _sum("%s < '1900-01-01'" % c),
                "n_fecha_centinela": _sum("substr(%s, 1, 10) IN (%s)" % (ct, ", ".join(lit(x) for x in FECHAS_CENTINELA))),
            })
        ramas.append("SELECT %s FROM %s WHERE %s" % (_emitir(COLS_STATS, v), src, where))
    return "\nUNION ALL\n".join(ramas)


def percentiles(src, where, periodo, columnas, n_tiles):
    """NTILE(n_tiles) por variable numérica. Devuelve min/max/conteo por tile.

    Con n_tiles=100 cada tile es un percentil; Impala no tiene percentile_cont, y esto
    además sirve como base para los cortes del PSI.
    """
    ramas = []
    for col in columnas:
        vf = _vf(col)
        ramas.append(
            "SELECT CAST(%d AS BIGINT) AS periodo, %s AS variable, CAST(tile AS INT) AS tile, "
            "CAST(MIN(v) AS DOUBLE) AS vmin, CAST(MAX(v) AS DOUBLE) AS vmax, CAST(COUNT(*) AS BIGINT) AS n "
            "FROM (SELECT %s AS v, NTILE(%d) OVER (ORDER BY %s) AS tile FROM %s WHERE (%s) AND (%s) IS NOT NULL) t "
            "GROUP BY tile" % (periodo, lit(col), vf, int(n_tiles), vf, src, where, vf)
        )
    return "\nUNION ALL\n".join(ramas)


def top_categorias(src, where, columnas, k):
    ramas = []
    for col in columnas:
        t = _txt(col)
        ramas.append(
            "SELECT * FROM (SELECT %s AS variable, %s AS valor, CAST(COUNT(*) AS BIGINT) AS n FROM %s "
            "WHERE (%s) AND %s IS NOT NULL GROUP BY 2 ORDER BY 3 DESC LIMIT %d) t%d"
            % (lit(col), t, src, where, q(col), int(k), len(ramas))
        )
    return "\nUNION ALL\n".join(ramas)


def bins(src, where, periodo, definiciones):
    """definiciones: lista de dict {'variable', 'clase', 'cortes' | 'categorias'}.

    Numéricas: buckets (-inf,c1], (c1,c2], ..., (ck,inf) + __NULO__ + __INF_NAN__.
    Categóricas: top-K del periodo base + __OTROS__ + __NULO__.
    """
    ramas = []
    for d in definiciones:
        col = d["variable"]
        if d["clase"] == "numerica":
            dd = "CAST(%s AS DOUBLE)" % q(col)
            casos = ["WHEN %s IS NULL THEN '__NULO__'" % q(col),
                     "WHEN is_inf(%s) OR is_nan(%s) THEN '__INF_NAN__'" % (dd, dd)]
            cortes = d.get("cortes") or []
            for i, c in enumerate(cortes):
                casos.append("WHEN %s <= %s THEN 'b%02d'" % (dd, num(c), i + 1))
            casos.append("ELSE 'b%02d'" % (len(cortes) + 1))
        else:
            t = _txt(col)
            casos = ["WHEN %s IS NULL THEN '__NULO__'" % q(col)]
            cats = d.get("categorias") or []
            if cats:
                casos.append("WHEN %s IN (%s) THEN %s" % (t, ", ".join(lit(x) for x in cats), t))
            casos.append("ELSE '__OTROS__'")
        expr = "CASE %s END" % " ".join(casos)
        ramas.append(
            "SELECT CAST(%d AS BIGINT) AS periodo, %s AS variable, %s AS bin, CAST(COUNT(*) AS BIGINT) AS n "
            "FROM %s WHERE %s GROUP BY 3" % (periodo, lit(col), expr, src, where)
        )
    return "\nUNION ALL\n".join(ramas)


def unicidad(src, where, periodo, llave):
    expr = _expr_llave(llave)
    return (
        "SELECT CAST(%d AS BIGINT) AS periodo, CAST(COUNT(*) AS BIGINT) AS n, "
        "CAST(COUNT(DISTINCT %s) AS BIGINT) AS n_distintos FROM %s WHERE %s" % (periodo, expr, src, where)
    )


def top_numericos(src, where, columnas, k=5):
    """Valores numéricos más frecuentes (para detectar centinelas como 999999 o -1)."""
    ramas = []
    for col in columnas:
        d = "CAST(%s AS DOUBLE)" % q(col)
        ramas.append(
            "SELECT * FROM (SELECT %s AS variable, %s AS valor, CAST(COUNT(*) AS BIGINT) AS n FROM %s "
            "WHERE (%s) AND %s IS NOT NULL GROUP BY 2 ORDER BY 3 DESC LIMIT %d) t%d"
            % (lit(col), d, src, where, q(col), int(k), len(ramas))
        )
    return "\nUNION ALL\n".join(ramas)


def forma(src, where, periodo, definiciones):
    """Outliers (cercas de Tukey del base), centinelas y momentos estandarizados.

    definiciones: dict con variable, centro, escala, lo15, hi15, lo3, hi3, centinelas.
    z = (x - centro) / escala con centro/escala del periodo BASE (mediana y desv. est.):
    centrar evita la cancelación numérica de los momentos crudos (E[x^4] con montos grandes).
    """
    ramas = []
    for d in definiciones:
        col = d["variable"]
        vf = _vf(col)
        v = {"periodo": "CAST(%d AS BIGINT)" % periodo, "variable": lit(col),
             "n_validos": "CAST(COUNT(%s) AS BIGINT)" % vf}
        if d.get("lo3") is not None:
            v.update({
                "n_bajo_15": _sum("%s < %s" % (vf, num(d["lo15"]))), "n_alto_15": _sum("%s > %s" % (vf, num(d["hi15"]))),
                "n_bajo_3": _sum("%s < %s" % (vf, num(d["lo3"]))), "n_alto_3": _sum("%s > %s" % (vf, num(d["hi3"]))),
            })
        cent = d.get("centinelas") or []
        v["n_centinela"] = _sum("CAST(%s AS DOUBLE) IN (%s)" % (q(col), ", ".join(num(x) for x in cent))) \
            if cent else "CAST(0 AS BIGINT)"
        if d.get("escala"):
            z = "((%s) - %s) / %s" % (vf, num(d["centro"]), num(d["escala"]))
            for k in (1, 2, 3, 4):
                pot = " * ".join(["(%s)" % z] * k)
                v["e%d" % k] = "CAST(AVG(%s) AS DOUBLE)" % pot
        ramas.append("SELECT %s FROM %s WHERE %s" % (_emitir(COLS_FORMA, v), src, where))
    return "\nUNION ALL\n".join(ramas)


def reglas(src, where, periodo, reglas_dict):
    """Reglas de negocio como SQL booleano. Violación = la expresión es FALSE; NULL = indeterminado."""
    ramas = []
    for nombre, expr in reglas_dict.items():
        ramas.append(
            "SELECT CAST(%d AS BIGINT) AS periodo, %s AS variable, CAST(COUNT(*) AS BIGINT) AS n, "
            "%s AS n_violaciones, %s AS n_indeterminado FROM %s WHERE %s"
            % (periodo, lit(nombre), _sum("NOT (%s)" % expr), _sum("(%s) IS NULL" % expr), src, where)
        )
    return "\nUNION ALL\n".join(ramas)


def _expr_llave(llave):
    """Expresión única por combinación de columnas.

    Una columna: el valor como texto ('NULL#' si es nulo).
    Varias: cada parte con prefijo de longitud ('5:12345'), unidas con '|'. El prefijo evita colisiones
    cuando un valor contiene el separador ('A|B' + 'C' frente a 'A' + 'B|C'). 'NULL#' no puede chocar con
    un valor real porque todo valor no nulo empieza con '<longitud>:'. Dos filas con la misma llave y un
    componente NULL en la misma posición cuentan como duplicadas (una llave con NULL ya es un problema).
    """
    if len(llave) == 1:
        return "COALESCE(CAST(%s AS STRING), 'NULL#')" % q(llave[0])
    partes = ["CASE WHEN %s IS NULL THEN 'NULL#' ELSE concat(CAST(length(CAST(%s AS STRING)) AS STRING), ':', "
              "CAST(%s AS STRING)) END" % (q(k), q(k), q(k)) for k in llave]
    return "concat_ws('|', %s)" % ", ".join(partes)


def cobertura(src, where, periodo, tabla, where_ant, periodo_ant, llave):
    """Cuántas llaves del periodo estaban en el periodo anterior (rotación del universo)."""
    k = _expr_llave(llave)
    a = "SELECT DISTINCT %s AS k FROM %s WHERE %s" % (k, src, where)
    b = "SELECT DISTINCT %s AS k FROM %s WHERE %s" % (k, tabla, where_ant)
    return (
        "SELECT CAST(%d AS BIGINT) AS periodo, CAST(%d AS BIGINT) AS periodo_ant, "
        "CAST(x.n_actual AS BIGINT) AS n_actual, CAST(y.n_anterior AS BIGINT) AS n_anterior, "
        "CAST(z.n_comunes AS BIGINT) AS n_comunes FROM "
        "(SELECT COUNT(*) AS n_actual FROM (%s) a0) x CROSS JOIN "
        "(SELECT COUNT(*) AS n_anterior FROM (%s) b0) y CROSS JOIN "
        "(SELECT COUNT(*) AS n_comunes FROM (%s) a1 JOIN (%s) b1 ON a1.k = b1.k) z"
        % (periodo, periodo_ant, a, b, a, b)
    )


def columnas_identicas(src, where, pares):
    """pares: lista de (col_a, col_b, clase). n_dif = filas donde difieren (NULL = NULL cuenta igual)."""
    ramas = []
    for a, b, clase in pares:
        t = "DOUBLE" if clase == "numerica" else "STRING"
        ramas.append(
            "SELECT %s AS col_a, %s AS col_b, CAST(COUNT(*) AS BIGINT) AS n, %s AS n_dif FROM %s WHERE %s"
            % (lit(a), lit(b), _sum("NOT (CAST(%s AS %s) IS NOT DISTINCT FROM CAST(%s AS %s))" % (q(a), t, q(b), t)),
               src, where)
        )
    return "\nUNION ALL\n".join(ramas)


def insertar(tabla_destino, select):
    return "INSERT INTO %s\n%s" % (tabla_destino, select)
