"""
Métricas calculadas en pandas sobre los agregados que bajan de Impala
(nunca sobre datos fila a fila).

PSI
---
    PSI = sum_i (a_i - e_i) * ln(a_i / e_i)
con e_i, a_i las proporciones del bucket i en el periodo base (esperado) y el actual.
Los buckets numéricos se definen con cuantiles del periodo base; NULO e INF/NAN son
buckets propios (un cambio en la tasa de nulos es un cambio de población).

PSI-alpha (valor crítico)
-------------------------
Bajo H0 (misma distribución) y con buckets fijos, Yurdakul (2018) muestra que
    PSI ~ (1/N + 1/M) * chi2(B - 1)
=> PSI_alpha = chi2_{1-alpha}(B - 1) * (1/N + 1/M)
Si PSI > PSI_alpha el cambio es estadísticamente significativo al nivel alpha.
Es complementario a los umbrales fijos (0.10 / 0.25): con millones de filas un PSI de
0.02 puede ser "significativo" pero irrelevante en negocio; con pocas filas un PSI de 0.15
puede ser solo ruido muestral. Por eso se reportan ambos.
Supuesto a tener presente: los cortes salen del mismo periodo base, así que la
aproximación es algo optimista para el base vs. sí mismo (no aplica) y razonable para el resto.
"""
import math

import numpy as np
import pandas as pd

try:  # scipy es opcional; si no está, se usa Wilson-Hilferty
    from scipy.stats import chi2 as _chi2
except Exception:  # pragma: no cover
    _chi2 = None
from statistics import NormalDist

PERCENTILES = (1, 5, 10, 25, 50, 75, 90, 95, 99)

UMBRALES_DEFECTO = {
    "psi_moderado": 0.10,
    "psi_alto": 0.25,
    "alpha": 0.05,
    "psi_epsilon": 1e-4,          # reemplazo de proporciones 0 para evitar log(0)
    "nulos_alto": 0.95,           # % nulos para alertar variable casi vacía
    "salto_nulos_pp": 0.10,       # salto absoluto de % nulos vs. periodo anterior
    "variacion_volumen": 0.30,    # |delta %| de filas vs. periodo anterior
    "z_volumen": 3.0,             # |z robusto (MAD)| de filas
    "duplicados_pct": 0.0,        # % duplicados tolerado en la llave
    "card_max_categorica": 1000,  # sobre esto no se calcula PSI categórico (parece ID)
    # --- coherencia / outliers / tipos
    "outliers_pct": 0.01,         # % de valores fuera de Q1-3*IQR / Q3+3*IQR para alertar
    "salto_outliers_pp": 0.01,    # aumento de % fuera de 3*IQR vs. base
    "cola_ratio": 10.0,           # max / p99 para marcar cola pesada
    "centinela_pct": 0.05,        # masa mínima de un solo valor para considerarlo centinela
    "disfrazados_pct": 0.0,       # % de nulos disfrazados tolerado
    "texto_tipo_pct": 0.95,       # % de textos convertibles a número / con patrón de fecha
    "id_ratio": 0.95,             # ndv / no nulos para considerar identificador
    "id_min_n": 1000,
    "categorica_ndv_max": 20,     # entera con pocos valores distintos: posible código categórico
    "rara_pct": 0.01,             # categoría rara en el base
    "otros_salto": 0.05,          # aumento de la masa de OTROS (categorías nuevas)
    "parcial_ratio": 0.5,         # último periodo con menos de esta fracción de la mediana
    "cobertura_min": 0.70,        # % de llaves del periodo presentes en el anterior
    "regimen_pct": 0.20,          # % de variables con PSI vs. anterior >= psi_moderado en un periodo
    "fechas_pct": 0.0,            # % de fechas futuras / antiguas / centinela tolerado
    "psi_min_n": 100,             # filas mínimas en ambos periodos para alertar PSI
    "muestra_min_periodo": 100,   # mediana de filas por periodo bajo la cual se sugiere granularidad mayor
    "max_alertas_repetidas": 1,   # más alertas iguales (tipo+variable) que esto se agrupan en una
}

NULOS_DISFRAZADOS = ["NULL", "NUL", "NONE", "NAN", "N/A", "NA", "N.A.", "#N/A", "-", "--", ".", "?",
                     "SIN DATO", "SIN DATOS", "SIN INFORMACION", "SIN INFORMACIÓN", "NO APLICA", "NO DISPONIBLE",
                     "UNDEFINED", "NINGUNO", "XXX", "S/D", "S/I"]


# --------------------------------------------------------------------------- chi2
def chi2_ppf(p, k):
    if k <= 0:
        return float("nan")
    if _chi2 is not None:
        return float(_chi2.ppf(p, k))
    z = NormalDist().inv_cdf(p)
    return k * (1 - 2.0 / (9 * k) + z * math.sqrt(2.0 / (9 * k))) ** 3


def psi_critico(n_base, n_actual, n_bins, alpha=0.05):
    if not n_base or not n_actual or n_bins < 2:
        return float("nan")
    return chi2_ppf(1 - alpha, n_bins - 1) * (1.0 / n_base + 1.0 / n_actual)


# --------------------------------------------------------------------------- PSI
def psi(esperado, actual, epsilon=1e-4):
    """esperado/actual: pd.Series de conteos indexadas por bucket."""
    idx = esperado.index.union(actual.index)
    e = esperado.reindex(idx, fill_value=0).astype(float)
    a = actual.reindex(idx, fill_value=0).astype(float)
    if e.sum() == 0 or a.sum() == 0:
        return float("nan"), 0
    e = (e / e.sum()).clip(lower=epsilon)
    a = (a / a.sum()).clip(lower=epsilon)
    bins_usados = int(((esperado.reindex(idx, fill_value=0) > 0) | (actual.reindex(idx, fill_value=0) > 0)).sum())
    return float(((a - e) * np.log(a / e)).sum()), bins_usados


# --------------------------------------------------------------------------- percentiles
def _cuantil_desde_tiles(g, qs):
    g = g.sort_values("tile")
    tot = g["n"].sum()
    if tot == 0:
        return {q: np.nan for q in qs}
    acum = g["n"].cumsum().values / float(tot)
    vmax = g["vmax"].values
    out = {}
    for qq in qs:
        i = int(np.searchsorted(acum, qq / 100.0 - 1e-12, side="left"))
        out[qq] = float(vmax[min(i, len(vmax) - 1)])
    return out


def percentiles_desde_tiles(df_pct, qs=PERCENTILES):
    """df_pct: periodo, variable, tile, vmin, vmax, n  ->  periodo, variable, p1..p99"""
    if df_pct is None or df_pct.empty:
        return pd.DataFrame(columns=["periodo", "variable"] + ["p%d" % x for x in qs])
    filas = []
    for (p, v), g in df_pct.groupby(["periodo", "variable"]):
        r = {"periodo": p, "variable": v}
        r.update({"p%d" % k: val for k, val in _cuantil_desde_tiles(g, qs).items()})
        filas.append(r)
    return pd.DataFrame(filas)


def cortes_psi(df_pct_var, n_bins):
    """Cortes interiores (k-1 valores únicos) a partir de los tiles del periodo base."""
    if df_pct_var is None or df_pct_var.empty:
        return []
    qs = [100.0 * j / n_bins for j in range(1, n_bins)]
    cuant = _cuantil_desde_tiles(df_pct_var, qs)
    vmax_total = float(df_pct_var["vmax"].max())
    cortes = sorted(set(round(v, 12) for v in cuant.values() if v is not None and not np.isnan(v)))
    return [c for c in cortes if c < vmax_total]


def etiquetas_bins(cortes):
    """Etiqueta legible de cada bucket numérico."""
    et = {"__NULO__": "NULO", "__INF_NAN__": "INF/NaN"}
    lims = [-np.inf] + list(cortes) + [np.inf]
    for i in range(len(lims) - 1):
        a = "-inf" if np.isinf(lims[i]) else "%.6g" % lims[i]
        b = "inf" if np.isinf(lims[i + 1]) else "%.6g" % lims[i + 1]
        et["b%02d" % (i + 1)] = "(%s, %s]" % (a, b) if not np.isinf(lims[i + 1]) else "(%s, %s)" % (a, b)
    return et


# --------------------------------------------------------------------------- consolidación
def enriquecer_stats(st):
    if st is None or st.empty:
        return st
    st = st.copy()
    n = st["n"].replace(0, np.nan).astype(float)
    for col in ("n_nulos", "n_inf", "n_nan", "n_ceros", "n_negativos", "n_vacios"):
        st["pct_" + col[2:]] = st[col].astype(float) / n
    st["cv"] = st["std"] / st["media"].abs().replace(0, np.nan)
    return st


def tabla_psi(df_bins, n_por_periodo, periodo_base, umbrales):
    """PSI de cada variable/periodo.

    PRINCIPAL: psi_anterior = periodo vs. periodo anterior con datos (el primero queda vacío).
    Referencia: psi_base = vs. el periodo con el que se definieron los buckets (informativo).
    psi_critico_* = valor crítico chi2 (Yurdakul 2018) para decidir si el cambio es más que ruido muestral.
    Los buckets son fijos (deciles / top-K del periodo de referencia) para todas las comparaciones.
    """
    cols = ["periodo", "variable", "periodo_anterior", "psi_anterior", "psi_critico_anterior", "signif_anterior",
            "n_anterior", "n_actual", "psi_base", "psi_critico_base", "signif_base", "n_base", "bins"]
    if df_bins is None or df_bins.empty:
        return pd.DataFrame(columns=cols)
    eps, alpha = umbrales["psi_epsilon"], umbrales["alpha"]
    filas = []
    for v, g in df_bins.groupby("variable"):
        piv = g.pivot_table(index="bin", columns="periodo", values="n", aggfunc="sum").fillna(0)
        periodos = sorted(piv.columns)
        base = periodo_base if periodo_base in piv.columns else None
        prev = None
        for p in periodos:
            r = {"periodo": p, "variable": v, "n_actual": float(piv[p].sum())}
            if prev is not None:
                val, b = psi(piv[prev], piv[p], eps)
                crit = psi_critico(piv[prev].sum(), piv[p].sum(), b, alpha)
                r.update(periodo_anterior=prev, psi_anterior=val, psi_critico_anterior=crit,
                         signif_anterior=bool(val > crit), n_anterior=float(piv[prev].sum()), bins=b)
            if base is not None and p != base:
                val, b = psi(piv[base], piv[p], eps)
                crit = psi_critico(piv[base].sum(), piv[p].sum(), b, alpha)
                r.update(psi_base=val, psi_critico_base=crit, signif_base=bool(val > crit),
                         n_base=float(piv[base].sum()))
            filas.append(r)
            prev = p
    out = pd.DataFrame(filas)
    for c in cols:
        if c not in out:
            out[c] = np.nan
    return out[cols].sort_values(["variable", "periodo"]).reset_index(drop=True)


def normalizar_psi_esperado(defs, clases):
    """Valida la configuración de PSI-alpha (distribución esperada por variable, definida por el modelador).

    Numérica:   {"cortes": [c1, ..., ck], "esperado": [p1, ..., pk+1], "nulo": 0.0}
                buckets (-inf, c1], (c1, c2], ..., (ck, inf)
    Categórica: {"categorias": {"A": 0.4, "B": 0.3}, "otros": 0.3, "nulo": 0.0}
    Las proporciones se normalizan a 1 si no suman exactamente 1.
    """
    out = {}
    for var, d in (defs or {}).items():
        v = var.lower()
        if v not in clases:
            raise ValueError("psi_esperado: la variable %s no se perfila" % var)
        nulo = float(d.get("nulo", 0.0))
        if "cortes" in d:
            cortes = [float(x) for x in d["cortes"]]
            esp = [float(x) for x in d["esperado"]]
            if len(esp) != len(cortes) + 1:
                raise ValueError("psi_esperado %s: 'esperado' debe tener len(cortes)+1 valores" % var)
            if cortes != sorted(cortes):
                raise ValueError("psi_esperado %s: los cortes deben ir en orden ascendente" % var)
            tot = sum(esp) + nulo
            dist = {"b%02d" % (i + 1): p / tot for i, p in enumerate(esp)}
            dist["__NULO__"] = nulo / tot
            dist["__INF_NAN__"] = 0.0
            out[v] = {"variable": v, "clase": "numerica", "cortes": cortes, "esperado": dist}
        elif "categorias" in d:
            cats = {str(k): float(p) for k, p in d["categorias"].items()}
            otros = float(d.get("otros", max(0.0, 1.0 - sum(cats.values()) - nulo)))
            tot = sum(cats.values()) + otros + nulo
            dist = {k: p / tot for k, p in cats.items()}
            dist.update({"__OTROS__": otros / tot, "__NULO__": nulo / tot})
            out[v] = {"variable": v, "clase": "categorica", "categorias": list(cats), "esperado": dist}
        else:
            raise ValueError("psi_esperado %s: use 'cortes'+'esperado' o 'categorias'" % var)
    return out


def tabla_psi_alpha(df_besp, defs, umbrales):
    """PSI-alpha: cada periodo contra la distribución ESPERADA del modelo (control).
    Valor crítico con referencia fija (sin error muestral en lo esperado): chi2_{1-a}(B-1) / M."""
    cols = ["periodo", "variable", "psi_alpha", "psi_alpha_critico", "signif_alpha"]
    if df_besp is None or df_besp.empty or not defs:
        return pd.DataFrame(columns=cols)
    eps, alpha = umbrales["psi_epsilon"], umbrales["alpha"]
    filas = []
    for (v, p), g in df_besp.groupby(["variable", "periodo"]):
        d = defs.get(v)
        if d is None:
            continue
        esp = pd.Series(d["esperado"]) * 1e6        # proporciones como "conteos" grandes
        act = g.set_index("bin")["n"].astype(float)
        val, b = psi(esp, act, eps)
        m = act.sum()
        crit = chi2_ppf(1 - alpha, b - 1) / m if m and b > 1 else float("nan")
        filas.append({"periodo": p, "variable": v, "psi_alpha": val, "psi_alpha_critico": crit,
                      "signif_alpha": bool(val > crit) if crit == crit else False})
    return pd.DataFrame(filas, columns=cols)


def volumen_enriquecido(vol, gran, huecos_list):
    vol = vol.copy().sort_values("periodo").reset_index(drop=True)
    vol["n"] = vol["n"].astype(float)
    vol["var_pct"] = vol["n"].pct_change()
    med = vol["n"].median()
    mad = (vol["n"] - med).abs().median()
    escala = 1.4826 * mad if mad and mad > 0 else vol["n"].std()  # MAD=0 (volumen casi constante) -> std
    vol["z_robusto"] = (vol["n"] - med) / escala if escala and escala > 0 else 0.0
    return vol


# --------------------------------------------------------------------------- alertas
def _a(nivel, tipo, detalle, variable=None, periodo=None, valor=None, umbral=None):
    return dict(nivel=nivel, tipo=tipo, variable=variable, periodo=periodo,
                valor=valor, umbral=umbral, detalle=detalle)


def generar_alertas(ctx, umbrales):
    """ctx: dict con vol, huecos, stats, psi, uniq, tareas_fallidas, periodo_base."""
    al = []
    for p in ctx.get("huecos", []):
        al.append(_a("ALTA", "hueco_periodo", "Periodo sin datos dentro del rango", periodo=p))
    vol = ctx.get("vol")
    if vol is not None and not vol.empty:
        for _, r in vol.iterrows():
            if pd.notna(r["var_pct"]) and abs(r["var_pct"]) > umbrales["variacion_volumen"]:
                al.append(_a("MEDIA", "variacion_volumen", "Cambio de filas vs. periodo anterior",
                             periodo=int(r["periodo"]), valor=float(r["var_pct"]),
                             umbral=umbrales["variacion_volumen"]))
            if abs(float(r["z_robusto"])) > umbrales["z_volumen"]:
                al.append(_a("MEDIA", "volumen_atipico", "Volumen atípico (z robusto con MAD)",
                             periodo=int(r["periodo"]), valor=float(r["z_robusto"]),
                             umbral=umbrales["z_volumen"]))
    st = ctx.get("stats")
    if st is not None and not st.empty:
        for v, g in st.sort_values("periodo").groupby("variable"):
            g = g.reset_index(drop=True)
            if (g["pct_nulos"] >= 1).all():
                al.append(_a("ALTA", "variable_vacia", "100% nulos en todos los periodos", variable=v))
                continue
            if (g["pct_nulos"] >= umbrales["nulos_alto"]).all():
                al.append(_a("MEDIA", "casi_vacia", "% nulos sobre umbral en todos los periodos",
                             variable=v, valor=float(g["pct_nulos"].mean()), umbral=umbrales["nulos_alto"]))
            if (g["ndv"].fillna(0) <= 1).all():
                al.append(_a("MEDIA", "constante", "Un solo valor distinto en todos los periodos", variable=v))
            if (g["n_inf"].fillna(0) + g["n_nan"].fillna(0) > 0).any():
                ps = g.loc[g["n_inf"].fillna(0) + g["n_nan"].fillna(0) > 0, "periodo"].tolist()
                al.append(_a("MEDIA", "inf_nan", "Valores inf/NaN en %d periodo(s)" % len(ps),
                             variable=v, periodo=int(ps[0]), valor=float((g["n_inf"] + g["n_nan"]).sum())))
            dif = g["pct_nulos"].diff()
            for i in dif.index[dif.abs() > umbrales["salto_nulos_pp"]]:
                al.append(_a("MEDIA", "salto_nulos", "Salto en % nulos vs. periodo anterior",
                             variable=v, periodo=int(g.loc[i, "periodo"]), valor=float(dif[i]),
                             umbral=umbrales["salto_nulos_pp"]))
    ps = ctx.get("psi")
    if ps is not None and not ps.empty:
        for _, r in ps.iterrows():
            val = r.get("psi_anterior")
            if val is not None and not pd.isna(val) and val >= umbrales["psi_moderado"]:
                # magnitud sin significancia = ruido muestral: no se alerta (sigue visible en el mapa)
                n_ok = min(r.get("n_anterior") or 0, r.get("n_actual") or 0) >= umbrales["psi_min_n"]
                if r.get("signif_anterior") and n_ok:
                    nivel = "ALTA" if val >= umbrales["psi_alto"] else "MEDIA"
                    al.append(_a(nivel, "psi", "PSI vs. periodo anterior (%s), significativo a alpha=%.2f"
                                 % (int(r["periodo_anterior"]), umbrales["alpha"]),
                                 variable=r["variable"], periodo=int(r["periodo"]), valor=float(val),
                                 umbral=umbrales["psi_moderado"]))
            va = r.get("psi_alpha")
            if va is not None and not pd.isna(va) and va >= umbrales["psi_moderado"] \
                    and (r.get("n_actual") or 0) >= umbrales["psi_min_n"]:
                nivel = "ALTA" if va >= umbrales["psi_alto"] else "MEDIA"
                al.append(_a(nivel, "psi_alpha", "PSI-alpha vs. distribución esperada del modelo",
                             variable=r["variable"], periodo=int(r["periodo"]), valor=float(va),
                             umbral=umbrales["psi_moderado"]))
    un = ctx.get("uniq")
    if un is not None and not un.empty:
        for _, r in un.iterrows():
            dup = (r["n"] - r["n_distintos"]) / float(r["n"]) if r["n"] else 0
            if dup > umbrales["duplicados_pct"]:
                al.append(_a("ALTA", "duplicados_llave", "Registros duplicados en la llave",
                             periodo=int(r["periodo"]), valor=float(dup), umbral=umbrales["duplicados_pct"]))
    for t in ctx.get("tareas_fallidas", []):
        al.append(_a("ALTA", "tarea_fallida", "Falló: %s" % t.get("error", "")[:200],
                     periodo=t.get("periodo"), variable=t.get("fase")))
    for v in ctx.get("alta_cardinalidad", []):
        al.append(_a("INFO", "alta_cardinalidad", "Categórica con demasiados valores: sin PSI (¿es un ID?)", variable=v))
    orden = {"ALTA": 0, "MEDIA": 1, "INFO": 2}
    df = pd.DataFrame(al, columns=["nivel", "tipo", "variable", "periodo", "valor", "umbral", "detalle"])
    if not df.empty:
        df = df.assign(_o=df["nivel"].map(orden)).sort_values(["_o", "tipo", "variable", "periodo"]).drop(columns="_o")
    return df.reset_index(drop=True)


# =========================================================================== coherencia
_CENTINELAS_EXACTOS = {-1, -9, -99, -999, -9999, -99999, -999999, 19000101, 18991231, 99991231, 29991231}


def _es_patron_centinela(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    if not np.isfinite(f) or f != int(f):
        return False
    iv = int(f)
    if iv in _CENTINELAS_EXACTOS:
        return True
    s = str(abs(iv))
    return len(s) >= 3 and set(s) == {"9"}


def definir_forma(var, pct_var, st_row, topnum_var, umbrales):
    """Cercas de Tukey, centro/escala y centinelas a partir del periodo base."""
    d = {"variable": var, "centro": None, "escala": None, "lo15": None, "hi15": None, "lo3": None, "hi3": None,
         "centinelas": [], "centinelas_info": []}
    if pct_var is not None and not pct_var.empty:
        qv = _cuantil_desde_tiles(pct_var, (25, 50, 75))
        q1, q2, q3 = qv[25], qv[50], qv[75]
        iqr = q3 - q1
        d["centro"] = q2
        if iqr > 0:
            d.update(lo15=q1 - 1.5 * iqr, hi15=q3 + 1.5 * iqr, lo3=q1 - 3 * iqr, hi3=q3 + 3 * iqr)
    if st_row is not None:
        std = st_row.get("std")
        if std is not None and np.isfinite(std) and std > 0 and d["centro"] is not None:
            d["escala"] = float(std)
        no_nulos = float(st_row["n"] - st_row["n_nulos"])
        if topnum_var is not None and not topnum_var.empty and no_nulos > 0:
            tops = topnum_var.sort_values("n", ascending=False)
            shares = (tops["n"].astype(float) / no_nulos).tolist()
            for i, (_, r) in enumerate(tops.iterrows()):
                v, share = float(r["valor"]), float(r["n"]) / no_nulos
                if v == 0 or share < umbrales["centinela_pct"]:
                    continue
                # masa anómala: el valor pesa al menos 3 veces la mediana de los demás del top
                otros = [x for j, x in enumerate(shares) if j != i]
                anomalo = bool(otros) and share >= 3 * float(np.median(otros))
                if _es_patron_centinela(v) and anomalo:
                    motivo = "patron"
                elif (v == st_row.get("maximo") or v == st_row.get("minimo")) and anomalo and (st_row.get("ndv") or 0) > 2:
                    motivo = "extremo"
                else:
                    continue
                d["centinelas"].append(v)
                d["centinelas_info"].append({"valor": v, "pct_base": share, "motivo": motivo})
    return d


def consolidar_forma(df_forma, st):
    if df_forma is None or df_forma.empty:
        return pd.DataFrame()
    f = df_forma.copy()
    nv = f["n_validos"].replace(0, np.nan).astype(float)
    f["pct_fuera_15"] = (f["n_bajo_15"] + f["n_alto_15"]) / nv
    f["pct_fuera_3"] = (f["n_bajo_3"] + f["n_alto_3"]) / nv
    f["pct_centinela"] = f["n_centinela"] / nv
    mu = f["e1"]
    m2 = f["e2"] - mu ** 2
    m3 = f["e3"] - 3 * mu * f["e2"] + 2 * mu ** 3
    m4 = f["e4"] - 4 * mu * f["e3"] + 6 * mu ** 2 * f["e2"] - 3 * mu ** 4
    m2 = m2.where(m2 > 1e-12)
    f["asimetria"] = m3 / m2 ** 1.5
    f["curtosis_exceso"] = m4 / m2 ** 2 - 3
    return f[["periodo", "variable", "n_validos", "pct_fuera_15", "pct_fuera_3", "n_bajo_3", "n_alto_3",
              "pct_centinela", "n_centinela", "asimetria", "curtosis_exceso"]]


def pares_candidatos_identicas(st_base, clases, max_pares=50):
    """Pares de columnas con la misma firma de estadísticos en el base (luego se verifica fila a fila)."""
    if st_base is None or st_base.empty:
        return []
    grupos = {}
    for _, r in st_base.iterrows():
        v, k = r["variable"], clases.get(r["variable"])
        if k not in ("numerica", "categorica") or not r["ndv"] or r["ndv"] <= 1:
            continue
        if k == "numerica":
            firma = (k, int(r["n_nulos"]), round(float(r["minimo"]), 9), round(float(r["maximo"]), 9),
                     round(float(r["media"]), 6), round(float(r["std"] or 0), 6))
        else:
            firma = (k, int(r["n_nulos"]), int(r["ndv"]), str(r["min_txt"]), str(r["max_txt"]),
                     round(float(r["long_media"] or 0), 6))
        grupos.setdefault(firma, []).append(v)
    pares = []
    for firma, vs in grupos.items():
        for i in range(len(vs)):
            for j in range(i + 1, len(vs)):
                pares.append((vs[i], vs[j], firma[0]))
    return pares[:max_pares]


def tabla_reglas(df, sql_reglas):
    if df is None or df.empty:
        return pd.DataFrame()
    r = df.rename(columns={"variable": "regla"}).copy()
    r["pct_violaciones"] = r["n_violaciones"] / r["n"].replace(0, np.nan)
    r["pct_indeterminado"] = r["n_indeterminado"] / r["n"].replace(0, np.nan)
    r["expresion"] = r["regla"].map(sql_reglas)
    return r.sort_values(["regla", "periodo"]).reset_index(drop=True)


def tabla_cobertura(df):
    if df is None or df.empty:
        return pd.DataFrame()
    c = df.copy().sort_values("periodo")
    c["pct_en_anterior"] = c["n_comunes"] / c["n_actual"].replace(0, np.nan)   # de los de hoy, cuántos ya estaban
    c["pct_retenidos"] = c["n_comunes"] / c["n_anterior"].replace(0, np.nan)   # de los de antes, cuántos siguen
    return c.reset_index(drop=True)


def tabla_texto(st, topcat, clases):
    """Calidad de texto y de tipo por variable categórica (sobre el último y el peor periodo)."""
    if st is None or st.empty:
        return pd.DataFrame()
    filas = []
    for v, g in st[st["variable"].map(clases) == "categorica"].groupby("variable"):
        nn = (g["n"] - g["n_nulos"]).replace(0, np.nan).astype(float)
        colis = []
        if topcat is not None and not topcat.empty:
            vals = topcat.loc[topcat["variable"] == v, "valor"].astype(str)
            norm = vals.str.strip().str.upper()
            for k, grp in vals.groupby(norm):
                if grp.nunique() > 1:
                    colis.append(" / ".join(repr(x) for x in sorted(grp.unique())))
        # NDV (HyperLogLog) es prácticamente exacto con baja cardinalidad: ahí basta 1 de diferencia;
        # con alta cardinalidad se exige una diferencia relativa para no confundir ruido del estimador.
        dif = g["ndv"] - g["ndv_norm"]
        sospechoso = ((g["ndv"] <= 100) & (dif >= 1)) | ((g["ndv"] > 100) & (dif >= 0.02 * g["ndv"]))
        per_colision = g.loc[sospechoso, "periodo"].astype(int).tolist()
        filas.append({
            "variable": v,
            "periodos_colision": per_colision,
            "pct_disfrazados_max": (g["n_disfrazados"] / nn).max(),
            "pct_espacios_max": (g["n_espacios"] / nn).max(),
            "pct_vacios_max": (g["n_vacios"] / nn).max(),
            "pct_castable_num": (g["n_castable_num"].sum() / nn.sum()) if nn.sum() > 0 else np.nan,
            "pct_patron_fecha": (g["n_patron_fecha"].sum() / nn.sum()) if nn.sum() > 0 else np.nan,
            "ndv_ult": g.sort_values("periodo")["ndv"].iloc[-1],
            "ndv_norm_ult": g.sort_values("periodo")["ndv_norm"].iloc[-1],
            "colisiones": "; ".join(colis),
        })
    return pd.DataFrame(filas)


def tabla_fechas(st, clases):
    if st is None or st.empty:
        return pd.DataFrame()
    g = st[st["variable"].map(clases) == "fecha"].copy()
    if g.empty:
        return pd.DataFrame()
    nn = (g["n"] - g["n_nulos"]).replace(0, np.nan).astype(float)
    g["pct_futura"] = g["n_fecha_futura"] / nn
    g["pct_antigua"] = g["n_fecha_antigua"] / nn
    g["pct_centinela"] = g["n_fecha_centinela"] / nn
    return g[["periodo", "variable", "min_txt", "max_txt", "pct_futura", "pct_antigua", "pct_centinela",
              "n_fecha_futura", "n_fecha_antigua", "n_fecha_centinela"]].reset_index(drop=True)


def tabla_regimen(psi, umbrales):
    """Periodos donde muchas variables cambian a la vez frente al periodo anterior."""
    if psi is None or psi.empty:
        return pd.DataFrame()
    d = psi.dropna(subset=["psi_anterior"])
    # solo comparaciones creíbles: con periodos chicos "todas cambian" por puro ruido muestral
    d = d[(d[["n_anterior", "n_actual"]].min(axis=1) >= umbrales["psi_min_n"])]
    if d.empty:
        return pd.DataFrame()
    d = d.assign(psi_anterior=d["psi_anterior"].where(d["signif_anterior"] == True, 0.0))  # noqa: E712
    g = d.groupby("periodo").agg(
        n_variables=("variable", "nunique"),
        n_cambian=("psi_anterior", lambda x: int((x >= umbrales["psi_moderado"]).sum())))
    g["pct_cambian"] = g["n_cambian"] / g["n_variables"]
    cambian = d[d["psi_anterior"] >= umbrales["psi_moderado"]].groupby("periodo")["variable"].apply(
        lambda x: ", ".join(sorted(x)))
    g["variables"] = cambian
    g["corte_sugerido"] = (g["pct_cambian"] >= umbrales["regimen_pct"]) & (g["n_cambian"] >= 2)
    return g.reset_index()


def tabla_otros(bins, bins_def, periodo_base):
    """Masa de OTROS (categorías fuera del top del base) y categorías raras del base."""
    if bins is None or bins.empty:
        return pd.DataFrame()
    filas = []
    cats = [v for v, d in bins_def.items() if d.get("clase") == "categorica"]
    for v in cats:
        g = bins[bins["variable"] == v]
        if g.empty:
            continue
        tot = g.groupby("periodo")["n"].sum()
        otros = g[g["bin"] == "__OTROS__"].groupby("periodo")["n"].sum().reindex(tot.index, fill_value=0) / tot
        b = g[(g["periodo"] == periodo_base) & (~g["bin"].isin(["__OTROS__", "__NULO__"]))]
        tb = tot.get(periodo_base, np.nan)
        raras = b[b["n"] / tb < 0.01]["bin"].tolist() if tb == tb else []
        filas.append({"variable": v, "pct_otros_base": otros.get(periodo_base, np.nan),
                      "pct_otros_max": otros.max(), "periodo_otros_max": int(otros.idxmax()),
                      "n_categorias_raras_base": len(raras), "categorias_raras_base": ", ".join(raras[:10])})
    return pd.DataFrame(filas)


def alertas_coherencia(ctx, u):
    """Alertas de la segunda ola: outliers, centinelas, texto, tipos, fechas, reglas, cobertura, etc."""
    al = []
    fo = ctx.get("forma")
    if fo is not None and not fo.empty:
        base = ctx.get("periodo_base")
        for v, g in fo.sort_values("periodo").groupby("variable"):
            if (g["pct_fuera_3"] > u["outliers_pct"]).any():
                p = g.loc[g["pct_fuera_3"].idxmax()]
                al.append(_a("INFO", "outliers_extremos", "Valores fuera de Q1-3*IQR / Q3+3*IQR (cercas del base); "
                             "en variables de cola larga es esperable, lo relevante es si crece",
                             variable=v, periodo=int(p["periodo"]), valor=float(p["pct_fuera_3"]), umbral=u["outliers_pct"]))
            gb = g[g["periodo"] == base]
            if not gb.empty:
                dif = g["pct_fuera_3"] - float(gb["pct_fuera_3"].iloc[0])
                if (dif > u["salto_outliers_pp"]).any():
                    p = g.loc[dif.idxmax()]
                    al.append(_a("MEDIA", "salto_outliers", "Aumentó el % fuera de 3*IQR respecto al base",
                                 variable=v, periodo=int(p["periodo"]), valor=float(dif.max()), umbral=u["salto_outliers_pp"]))
    st = ctx.get("stats")
    if st is not None and not st.empty and "p99" in st:
        for v, g in st.dropna(subset=["p99"]).groupby("variable"):
            ratio = g["maximo"] / g["p99"].where(g["p99"].abs() > 0)
            if (ratio > u["cola_ratio"]).any():
                p = g.loc[ratio.idxmax()]
                al.append(_a("INFO", "cola_pesada", "Máximo muy por encima del p99 (cola pesada o error de captura)",
                             variable=v, periodo=int(p["periodo"]), valor=float(ratio.max()), umbral=u["cola_ratio"]))
    for c in ctx.get("centinelas", []):
        if c["motivo"] == "patron":
            al.append(_a("MEDIA", "centinela", "El valor %g tiene patrón de 'sin dato' y concentra masa" % c["valor"],
                         variable=c["variable"], valor=float(c["pct_base"]), umbral=u["centinela_pct"]))
        else:
            al.append(_a("INFO", "masa_en_extremo", "El valor %g (mínimo o máximo del base) concentra masa: "
                         "posible tope/piso o centinela" % c["valor"],
                         variable=c["variable"], valor=float(c["pct_base"]), umbral=u["centinela_pct"]))
    tx = ctx.get("texto")
    if tx is not None and not tx.empty:
        for _, r in tx.iterrows():
            if r["pct_disfrazados_max"] > u["disfrazados_pct"]:
                al.append(_a("MEDIA", "nulos_disfrazados", "Textos como 'N/A', 'NULL', '-' que no son NULL",
                             variable=r["variable"], valor=float(r["pct_disfrazados_max"]), umbral=u["disfrazados_pct"]))
            if r["pct_espacios_max"] > 0:
                al.append(_a("MEDIA", "espacios", "Valores con espacios al inicio o al final",
                             variable=r["variable"], valor=float(r["pct_espacios_max"])))
            if r["colisiones"] or r["periodos_colision"]:
                det = ("en el top del base: %s" % r["colisiones"][:150]) if r["colisiones"] else \
                    "NDV baja al normalizar mayúsculas/espacios"
                al.append(_a("MEDIA", "categorias_duplicadas", "Categorías iguales salvo mayúsculas/espacios (%s)" % det,
                             variable=r["variable"],
                             periodo=(r["periodos_colision"][0] if r["periodos_colision"] else None)))
            if r["pct_castable_num"] >= u["texto_tipo_pct"]:
                al.append(_a("MEDIA", "texto_numerico", "Columna de texto con valores numéricos (¿tipo mal declarado?)",
                             variable=r["variable"], valor=float(r["pct_castable_num"]), umbral=u["texto_tipo_pct"]))
            elif r["pct_patron_fecha"] >= u["texto_tipo_pct"]:
                al.append(_a("MEDIA", "texto_fecha", "Columna de texto con fechas (¿tipo mal declarado?)",
                             variable=r["variable"], valor=float(r["pct_patron_fecha"]), umbral=u["texto_tipo_pct"]))
    fe = ctx.get("fechas")
    if fe is not None and not fe.empty:
        for v, g in fe.groupby("variable"):
            for col, tipo, det in (("pct_futura", "fecha_futura", "Fechas posteriores al fin del periodo"),
                                   ("pct_antigua", "fecha_antigua", "Fechas anteriores a 1900"),
                                   ("pct_centinela", "fecha_centinela", "Fechas por defecto (1900-01-01, 9999-12-31, ...)")):
                if (g[col].fillna(0) > u["fechas_pct"]).any():
                    p = g.loc[g[col].idxmax()]
                    al.append(_a("MEDIA", tipo, det, variable=v, periodo=int(p["periodo"]), valor=float(p[col])))
    rg = ctx.get("reglas")
    if rg is not None and not rg.empty:
        for regla, g in rg.groupby("regla"):
            if (g["n_violaciones"] > 0).any():
                p = g.loc[g["pct_violaciones"].idxmax()]
                al.append(_a("ALTA", "regla_violada", "Regla '%s' no se cumple: %s" % (regla, str(p["expresion"])[:120]),
                             variable=regla.replace("dominio_", "") if regla.startswith("dominio_") else regla,
                             periodo=int(p["periodo"]), valor=float(p["pct_violaciones"])))
    co = ctx.get("cobertura")
    if co is not None and not co.empty:
        bajos = co[co["pct_en_anterior"] < u["cobertura_min"]]
        if co["n_comunes"].sum() == 0:
            al.append(_a("INFO", "no_es_panel", "Ninguna llave se repite entre periodos: los datos no son de panel "
                         "(cada entidad aparece una vez, ej. fecha de apertura); la cobertura no aplica"))
        elif not bajos.empty:
            peor = bajos.loc[bajos["pct_en_anterior"].idxmin()]
            al.append(_a("MEDIA", "cobertura_baja", "Pocas llaves del periodo existían en el anterior (%d de %d periodos); "
                         "en tablas de panel indica rotación o cambio de universo" % (len(bajos), len(co)),
                         periodo=int(peor["periodo"]), valor=float(peor["pct_en_anterior"]), umbral=u["cobertura_min"]))
    re_ = ctx.get("regimen")
    if re_ is not None and not re_.empty:
        for _, r in re_[re_["corte_sugerido"]].iterrows():
            al.append(_a("INFO", "cambio_regimen", "%d de %d variables cambian a la vez: %s"
                         % (r["n_cambian"], r["n_variables"], str(r["variables"])[:150]),
                         periodo=int(r["periodo"]), valor=float(r["pct_cambian"]), umbral=u["regimen_pct"]))
    ot = ctx.get("otros")
    if ot is not None and not ot.empty:
        for _, r in ot.iterrows():
            if r["pct_otros_max"] - (r["pct_otros_base"] or 0) > u["otros_salto"]:
                al.append(_a("MEDIA", "categorias_nuevas", "Creció la masa fuera de las categorías del base",
                             variable=r["variable"], periodo=int(r["periodo_otros_max"]),
                             valor=float(r["pct_otros_max"]), umbral=u["otros_salto"]))
    for v in ctx.get("parece_id", []):
        al.append(_a("INFO", "parece_id", "Casi un valor distinto por fila: identificador, no variable", variable=v))
    for v in ctx.get("posible_categorica", []):
        al.append(_a("INFO", "posible_categorica", "Entera con pocos valores distintos: ¿código categórico?", variable=v))
    for a, b in ctx.get("identicas", []):
        al.append(_a("INFO", "columnas_identicas", "Idéntica fila a fila a %s en el periodo base" % a, variable=b))
    vol = ctx.get("vol")
    if vol is not None and len(vol) >= 4:
        ult = vol.sort_values("periodo").iloc[-1]
        med = vol.sort_values("periodo").iloc[:-1]["n"].median()
        if med and ult["n"] < u["parcial_ratio"] * med:
            al.append(_a("MEDIA", "periodo_parcial", "El último periodo tiene mucho menos filas que la mediana (¿carga incompleta?)",
                         periodo=int(ult["periodo"]), valor=float(ult["n"] / med), umbral=u["parcial_ratio"]))
    return al


def ficha_variables(resumen, alertas, llave=None):
    """Recomendación SUGERIDA por variable. No se aplica: la decisión es del científico."""
    excluir = {"variable_vacia": "100% nulos", "constante": "constante", "casi_vacia": "casi vacía",
               "parece_id": "identificador", "columnas_identicas": "duplicada de otra columna"}
    revisar = {"psi": "PSI", "outliers_extremos": "outliers", "salto_outliers": "outliers crecientes",
               "centinela": "valor centinela", "masa_en_extremo": "masa en extremo", "nulos_disfrazados": "nulos disfrazados",
               "espacios": "espacios", "categorias_duplicadas": "categorías duplicadas", "texto_numerico": "tipo texto/número",
               "texto_fecha": "tipo texto/fecha", "salto_nulos": "salto de nulos", "inf_nan": "inf/NaN",
               "regla_violada": "regla de negocio", "fecha_futura": "fechas futuras", "fecha_antigua": "fechas antiguas",
               "fecha_centinela": "fechas por defecto", "categorias_nuevas": "categorías nuevas",
               "posible_categorica": "¿categórica?", "cola_pesada": "cola pesada"}
    rec, mot = [], []
    llave = set(llave or [])
    for v in resumen["variable"]:
        if v in llave:
            rec.append("llave")
            mot.append("identificador declarado: se valida su unicidad, no se usa como variable")
            continue
        tipos = set(alertas.loc[alertas["variable"] == v, "tipo"]) if alertas is not None and not alertas.empty else set()
        ex = [excluir[t] for t in excluir if t in tipos]
        rv = [revisar[t] for t in revisar if t in tipos]
        if ex:
            rec.append("excluir (sugerido)")
        elif rv:
            rec.append("revisar")
        else:
            rec.append("usar")
        mot.append(", ".join(ex + rv))
    out = resumen.copy()
    out["recomendacion"] = rec
    out["motivos"] = mot
    return out


def agrupar_alertas(al, u):
    """Agrupa alertas repetidas por periodo (mismo tipo y variable) en una sola: la peor + el conteo."""
    if al is None or al.empty:
        return al
    k = int(u["max_alertas_repetidas"])
    filas = []
    for (tipo, var), g in al.groupby(["tipo", al["variable"].fillna("")], sort=False):
        if len(g) <= k or g["periodo"].isna().all():
            filas.extend(g.to_dict("records"))
            continue
        orden = g.assign(_v=g["valor"].abs()).sort_values("_v", ascending=False, na_position="last")
        peor = orden.iloc[0].drop(labels="_v").to_dict()
        per = g["periodo"].dropna().astype(int)
        niveles = {"ALTA": 0, "MEDIA": 1, "INFO": 2}
        peor["nivel"] = min(g["nivel"], key=lambda x: niveles.get(x, 9))
        peor["detalle"] = "%s · en %d periodos (%s a %s); se muestra el peor" % (
            peor["detalle"], len(g), per.min(), per.max())
        filas.append(peor)
    out = pd.DataFrame(filas, columns=al.columns)
    orden = {"ALTA": 0, "MEDIA": 1, "INFO": 2}
    return out.assign(_o=out["nivel"].map(orden)).sort_values(["_o", "tipo", "variable", "periodo"],
                                                             na_position="first").drop(columns="_o").reset_index(drop=True)
