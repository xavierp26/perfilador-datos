"""
Propuesta de vista "limpia" en SQL (dialecto estilo Impala/Hive).

Solo corrige ERRORES DE DATO objetivos (inf/NaN -> NULL, nulos disfrazados -> NULL, espacios sobrantes)
y comenta (no aplica) lo que requiere confirmación: centinelas, fechas por defecto, mayúsculas,
columnas sugeridas para excluir y deduplicación por llave.
No imputa, no recorta outliers, no escala, no agrupa categorías: eso lo decide el científico.
El archivo se genera para REVISAR; el perfilador nunca lo ejecuta.
"""
import re

from . import sql as S

_EXCLUIR = ("100% nulos", "constante", "casi vacía", "duplicada de otra columna")


def vista_limpia(r):
    tabla = r["tabla"]
    corto = re.sub(r"[^a-z0-9]", "_", tabla.split(".")[1])[:40]
    nombre = "%s.vl_%s" % (r["zona_temporal"], corto)
    ficha = r["resumen_variables"].set_index("variable")
    st = r["stats"]
    tx = r["texto"].set_index("variable") if r["texto"] is not None and not r["texto"].empty else None
    fe = r["fechas"]
    cent = r["centinelas"]
    disfr = ", ".join(S.lit(x.upper()) for x in r["nulos_disfrazados"])
    perfiladas = {v for v, _ in r["variables"]}
    clases = dict(r["variables"])

    lineas = []

    def add(expr, nota=None, comentada=False):
        pre = "-- " if comentada else ""
        sep = "  " if not any(not l.startswith("--") and l.strip() for l in lineas) and not comentada else ", "
        lineas.append("%s%s%s%s" % (pre, sep, expr, ("   -- " + nota) if nota else ""))

    for col in r["columnas"]["columna"]:
        c = S.q(col)
        if col not in perfiladas:
            add(c, "sin cambios (fecha/partición/excluida del perfil)")
            continue
        motivos = str(ficha.loc[col, "motivos"]) if col in ficha.index else ""
        excl = [m for m in _EXCLUIR if m in motivos]
        if excl:
            add(c, "excluir (sugerido): %s" % ", ".join(excl), comentada=True)
            continue
        clase = clases[col]
        if clase == "numerica":
            g = st[st["variable"] == col]
            tiene_inf = (g["n_inf"].fillna(0).sum() + g["n_nan"].fillna(0).sum()) > 0
            d = "CAST(%s AS DOUBLE)" % c
            if tiene_inf:
                add("CASE WHEN is_inf(%s) OR is_nan(%s) THEN NULL ELSE %s END AS %s" % (d, d, c, c), "inf/NaN -> NULL")
            else:
                add(c)
            vals = cent[cent["variable"] == col]["valor"].tolist() if cent is not None and not cent.empty else []
            if vals:
                add("CASE WHEN %s IN (%s) THEN NULL ELSE %s END AS %s" % (d, ", ".join(S.num(v) for v in vals), c, c),
                    "REVISAR: posible centinela (reemplaza la línea anterior si se confirma)", comentada=True)
        elif clase == "categorica":
            t = "CAST(%s AS STRING)" % c
            sucio = False
            if tx is not None and col in tx.index:
                rr = tx.loc[col]
                sucio = (rr["pct_disfrazados_max"] or 0) > 0 or (rr["pct_espacios_max"] or 0) > 0 or (rr["pct_vacios_max"] or 0) > 0
            if sucio:
                add("CASE WHEN UPPER(TRIM(%s)) IN (%s) OR TRIM(%s) = '' THEN NULL ELSE TRIM(%s) END AS %s"
                    % (t, disfr, t, t, c), "nulos disfrazados/vacíos -> NULL, TRIM")
            else:
                add(c)
            if tx is not None and col in tx.index and (tx.loc[col, "colisiones"] or tx.loc[col, "periodos_colision"]):
                add("UPPER(TRIM(%s)) AS %s" % (t, c), "REVISAR: categorías que difieren solo en mayúsculas/espacios (reemplaza la línea anterior)",
                    comentada=True)
        else:
            add(c)
            if fe is not None and not fe.empty and (fe.loc[fe["variable"] == col, "n_fecha_centinela"].fillna(0) > 0).any():
                add("CASE WHEN substr(CAST(%s AS STRING), 1, 10) IN (%s) THEN NULL ELSE %s END AS %s"
                    % (c, ", ".join(S.lit(x) for x in S.FECHAS_CENTINELA), c, c),
                    "REVISAR: fechas por defecto (reemplaza la línea anterior)", comentada=True)

    cab = [
        "-- =====================================================================================",
        "-- Vista limpia PROPUESTA para %s   (run %s, %s)" % (tabla, r["run_key"], r["fecha_ejecucion"]),
        "-- Generada por perfilador. REVISAR antes de ejecutar; el perfilador no la ejecuta.",
        "-- Aplica solo correcciones de error de dato. Las líneas comentadas requieren confirmación.",
        "-- No imputa, no recorta outliers, no escala ni agrupa: esas decisiones son del científico.",
        "-- =====================================================================================",
        "CREATE VIEW IF NOT EXISTS %s AS" % nombre,
    ]
    cuerpo = ["SELECT"] + lineas + ["FROM %s;" % tabla]
    if r.get("llave"):
        cols_part = []
        for c in r["campo"]["columnas"] + [k.lower() for k in r["llave"]]:
            if c not in cols_part:
                cols_part.append(c)
        part = ", ".join(S.q(c) for c in cols_part)
        cuerpo += [
            "",
            "-- Deduplicación por llave (REVISAR: el ORDER BY define qué registro se conserva):",
            "-- SELECT * FROM (",
            "--   SELECT t.*, ROW_NUMBER() OVER (PARTITION BY %s ORDER BY <criterio>) AS rn_dup" % part,
            "--   FROM (<SELECT de arriba>) t",
            "-- ) x WHERE rn_dup = 1",
        ]
    return "\n".join(cab + cuerpo) + "\n"
