"""
Asistente interactivo (Jupyter o consola) para perfilar un archivo.

    from perfilador import asistente
    perf = asistente()
"""


def _preguntar(entrada, texto, defecto=None, validar=None):
    sufijo = " [%s]" % defecto if defecto not in (None, "") else ""
    while True:
        r = entrada("%s%s: " % (texto, sufijo)).strip()
        if not r and defecto is not None:
            r = str(defecto)
        if validar is None:
            return r
        try:
            return validar(r)
        except Exception as exc:  # noqa: BLE001
            print("  ✗ %s" % exc)


def _si_no(entrada, texto, defecto="n"):
    return _preguntar(entrada, texto + " (s/n)", defecto).lower().startswith("s")


def asistente(entrada=input, ejecutar=True, **fijos):
    """Asistente interactivo para .xlsx / .xlsm / .csv / .txt: pregunta lo mínimo y lanza perfilar_archivo.

    Cualquier respuesta se puede fijar por parámetro (ruta=, campo_fecha=, granularidad=, llave=, ...).
    """
    import os
    from .archivos import leer_archivo, perfilar_archivo, EXT_EXCEL, _norm_nombre

    def _val_ruta(r):
        r = r.strip().strip('"').strip("'")
        if not os.path.exists(r):
            raise ValueError("no existe: %s" % r)
        return r

    ruta = fijos.pop("ruta", None) or _preguntar(entrada, "Ruta del archivo (.xlsx, .xlsm, .csv, .txt)", validar=_val_ruta)
    hoja = fijos.pop("hoja", None)
    if hoja is None and os.path.splitext(ruta)[1].lower() in EXT_EXCEL:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(ruta, read_only=True)
            visibles = [ws.title for ws in wb.worksheets if getattr(ws, "sheet_state", "visible") == "visible"]
            wb.close()
        except Exception:
            visibles = []
        if len(visibles) > 1:
            print("Hojas visibles: %s" % ", ".join(visibles))
            hoja = _preguntar(entrada, "Hoja", visibles[0])
    df, tipos, info, inferencia, al = leer_archivo(ruta, hoja)
    print("\n%d filas x %d columnas leídas%s" % (len(df), df.shape[1],
          (" (separador '%s', %s)" % (info["separador"], info["encoding"])) if "separador" in info else ""))
    print(inferencia[["columna", "tipo", "motivo"]].to_string(index=False))
    for a in al:
        print("  [%s] %s" % (a[0], a[2]))
    cols = set(df.columns)

    def _val_cols(r, vacio_ok=True):
        if not r.strip():
            if vacio_ok:
                return None
            raise ValueError("requerido")
        cs = [_norm_nombre(x) for x in r.split(",") if x.strip()]
        falt = [c for c in cs if c not in cols]
        if falt:
            raise ValueError("columnas inexistentes: %s" % falt)
        return cs

    sug = ",".join(c for c in df.columns if tipos[c] in ("date", "timestamp"))[:60]
    kw = {}
    if "campo_fecha" in fijos:
        campo = fijos.pop("campo_fecha")
    else:
        campo = _preguntar(entrada, "Campo fecha (Enter = foto única, para catálogos)", sug, _val_cols)
    if campo:
        if "granularidad" not in fijos:
            kw["granularidad"] = _preguntar(entrada, "Granularidad: anio | mes | dia", "mes",
                                            lambda r: {"anio": "anio", "año": "anio", "mes": "mes", "dia": "dia", "día": "dia"}[r.strip().lower()])
        modo = fijos.pop("modo", None) or _preguntar(entrada, "Periodo: 1) toda la información  2) rango", "1",
                                                      lambda r: {"1": "todo", "2": "rango"}[r])
        kw["modo"] = modo
        if modo == "rango":
            kw["inicio"] = _preguntar(entrada, "  Inicio (YYYYMM / YYYYMMDD)")
            kw["fin"] = _preguntar(entrada, "  Fin")
    if "llave" not in fijos:
        kw["llave"] = _preguntar(entrada, "Llave única (opcional, coma)", "", _val_cols)
    if "reglas_dominio" not in fijos:
        r = _preguntar(entrada, "Reglas de dominio (opcional, ej. tasa:0:1, edad:18:100)", "")
        dom = {}
        for parte in [x.strip() for x in r.split(",") if x.strip()]:
            v, mn, mx = (parte.split(":") + ["", ""])[:3]
            dom[_norm_nombre(v)] = {"min": float(mn) if mn else None, "max": float(mx) if mx else None}
        kw["reglas_dominio"] = dom or None
    if "reglas" not in fijos:
        r = _preguntar(entrada, "Reglas entre campos en SQL, separadas por ';' (opcional)", "")
        kw["reglas"] = {"regla_%d" % (i + 1): x.strip() for i, x in enumerate(r.split(";")) if x.strip()} or None
    kw.update(fijos)
    if ejecutar and not _si_no(entrada, "¿Ejecutar?", "s"):
        ejecutar = False
    return perfilar_archivo(ruta, campo_fecha=campo, hoja=hoja, ejecutar=ejecutar, **kw)
