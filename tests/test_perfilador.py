"""Pruebas de punta a punta sobre archivos sintéticos con problemas sembrados.

    python -m pytest -q          (o:  python tests/test_perfilador.py)
"""
import os
import shutil
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from perfilador import asistente, perfilar_archivo  # noqa: E402
from perfilador import periodos as P  # noqa: E402
from perfilador.archivos import leer_archivo  # noqa: E402


def test_periodos():
    assert P.normalizar_periodo("2024-01", "dia", "fin") == 20240131
    assert P.normalizar_periodo(20240229, "mes") == 202402
    assert P.siguiente(202412, "mes") == 202501
    assert P.huecos([202301, 202303], "mes") == [202302]
    c = P.CampoFecha(["anio", "mes"], "particiones")
    assert c.filtro_periodo(202402) == "CAST(anio AS BIGINT) = 2024 AND CAST(mes AS BIGINT) = 2"


def _csv_sucio(ruta):
    """CSV exportado de un sistema con los problemas típicos: ';', coma decimal, dd/mm/aaaa, latin-1,
    códigos con cero a la izquierda, filas vacías, encabezado repetido y fila de totales."""
    rng = np.random.default_rng(3)
    lineas = ["id_cliente;fecha_corte;saldo;segmento;saldo;tasa"]
    for m in range(1, 7):
        for i in range(400):
            saldo = rng.lognormal(7, 0.8) * (1.5 if m >= 5 else 1)
            lineas.append("%07d;%02d/%02d/2024;%s;%s;%s;%s" % (
                rng.integers(1, 99999), 28 if m == 2 else 30 if m != 1 else 31, m,
                ("%.2f" % saldo).replace(".", ","), rng.choice(["Pequeña", "Mediana", "N/A"]),
                ("%.2f" % saldo).replace(".", ","), ("%.4f" % rng.uniform(0.05, 0.3)).replace(".", ",")))
        lineas.append(";;;;;")
    lineas.append("TOTAL;;999999999,00;;;")
    open(ruta, "w", encoding="latin-1").write("\n".join(lineas) + "\n")


def test_archivos():
    out = tempfile.mkdtemp()
    try:
        ruta = os.path.join(out, "saldos_mensuales.csv")
        _csv_sucio(ruta)
        df, tipos, info, inf, al = leer_archivo(ruta)
        assert info["separador"] == ";" and info["encoding"] in ("cp1252", "latin-1")
        assert info["filas_vacias"] == 6 and info["filas_total"] == 1 and len(df) == 2400
        assert tipos["id_cliente"] == "string"            # ceros a la izquierda conservados
        assert df["id_cliente"].str.len().eq(7).all()
        assert tipos["fecha_corte"] == "date" and tipos["saldo"] == "double" and tipos["tasa"] == "double"
        assert {"saldo", "saldo_1"} <= set(df.columns) and info["encabezados_duplicados"]
        p = perfilar_archivo(ruta, campo_fecha="fecha_corte", llave="id_cliente", dir_salida=out, verbose=False,
                             reglas_dominio={"tasa": {"min": 0, "max": 1}})
        r = p.resultado
        assert p.periodos == [202401, 202402, 202403, 202404, 202405, 202406]
        tipos_al = set(r["alertas"]["tipo"])
        for t in ("filas_total", "filas_vacias", "encabezado_duplicado", "ceros_izquierda", "nulos_disfrazados",
                  "psi", "columnas_identicas"):
            assert t in tipos_al, t
        assert os.path.exists(r["html"]) and os.path.exists(r["excel"])
        # catálogo sin fecha: foto única
        cat = os.path.join(out, "catalogo.csv")
        open(cat, "w").write("categoria_id,nivel_1,nivel_3,profundidad\n1,A,,1\n2,A,x,3\n3,B,,3\n4,B,y,3\n")
        p2 = perfilar_archivo(cat, llave="categoria_id", dir_salida=out, verbose=False,
                              reglas={"nivel3_si_profundidad3": "profundidad < 3 OR nivel_3 IS NOT NULL"})
        assert len(p2.periodos) == 1 and p2.resultado["lectura"]["info"]["foto_unica"]
        rg = p2.resultado["reglas"]
        assert int(rg["n_violaciones"].sum()) == 1
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_archivo_mm_yyyy_y_moneda():
    """Fecha mm/aaaa, montos con $, cohortes chicas (muchas alertas ruidosas)."""
    out = tempfile.mkdtemp()
    try:
        rng = np.random.default_rng(1)
        ruta = os.path.join(out, "cards.csv")
        with open(ruta, "w") as f:
            f.write("id,card_number,cvv,credit_limit,acct_open_date\n")
            for i in range(3000):
                f.write("%d,%d,%03d,$%d,%02d/%d\n" % (i, rng.integers(4e15, 5e15), rng.integers(0, 1000),
                                                    int(rng.lognormal(9, 1)), rng.integers(1, 13), rng.integers(2005, 2020)))
        p = perfilar_archivo(ruta, campo_fecha="acct_open_date", llave="id", dir_salida=out, verbose=False)
        r = p.resultado
        inf = r["lectura"]["inferencia"].set_index("columna")
        assert inf.loc["credit_limit", "tipo"] == "bigint" and inf.loc["acct_open_date", "tipo"] == "date"
        assert len(p.periodos) == 180
        al = r["alertas"]
        assert "muestra_pequena" in set(al["tipo"]) and "no_es_panel" in set(al["tipo"])
        assert len(al) < 25, len(al)                       # agrupadas, sin ruido de PSI no significativo
        assert "cvv" not in set(al.loc[al.tipo == "psi", "variable"])
        p2 = perfilar_archivo(ruta, campo_fecha="acct_open_date", llave="id", granularidad="anio", dir_salida=out,
                              verbose=False)
        al2 = p2.resultado["alertas"]
        assert set(al2.loc[al2.tipo == "parece_id", "variable"]) == {"card_number"}   # credit_limit y cvv no
    finally:
        shutil.rmtree(out, ignore_errors=True)


def _csv_mensual(ruta):
    """24 meses con: hueco (2023-08), drift de monto desde 2024-04, categoría nueva en 2024, centinela -1 en edad,
    nulos disfrazados, duplicados de llave en 2024-02 y caída de volumen en 2024-06."""
    rng = np.random.default_rng(7)
    filas = []
    for y in (2023, 2024):
        for m in range(1, 13):
            if (y, m) == (2023, 8):
                continue
            n = 300 if (y, m) == (2024, 6) else 800
            drift = 1.6 if y * 100 + m >= 202404 else 1.0
            seg = rng.choice(["A", "B", "C", "NUEVO"], n, p=[.3, .3, .3, .1]) if y == 2024 else \
                rng.choice(["A", "B", "C"], n, p=[.4, .3, .3])
            edad = rng.integers(18, 80, n)
            edad[rng.random(n) < 0.08] = -1
            canal = rng.choice(["APP", "WEB", "N/A"], n, p=[.5, .45, .05])
            ids = rng.choice(100000, n, replace=False)
            if (y, m) == (2024, 2):
                ids[:20] = ids[0]
            for i in range(n):
                filas.append("C%06d,%d-%02d-15,%.2f,%s,%d,%s" % (ids[i], y, m, rng.lognormal(6, 1) * drift,
                                                               seg[i], edad[i], canal[i]))
    with open(ruta, "w", encoding="utf-8") as f:
        f.write("id_cliente,fecha,monto,segmento,edad,canal\n" + "\n".join(filas) + "\n")


def test_mensual_alertas_y_reanudacion():
    out = tempfile.mkdtemp()
    try:
        ruta = os.path.join(out, "movimientos.csv")
        _csv_mensual(ruta)
        kw = dict(campo_fecha="fecha", llave="id_cliente", dir_salida=out, verbose=False,
                  reglas_dominio={"edad": {"min": 18, "max": 100}}, reglas={"monto_positivo": "monto > 0"})
        p = perfilar_archivo(ruta, **kw)
        r = p.resultado
        assert r["huecos"] == [202308] and len(r["periodos"]) == 23
        tipos = set(r["alertas"]["tipo"])
        for t in ("hueco_periodo", "variacion_volumen", "psi", "duplicados_llave", "centinela",
                  "nulos_disfrazados", "categorias_nuevas", "regla_violada"):
            assert t in tipos, t
        assert "monto" in set(r["alertas"].loc[r["alertas"].tipo == "psi", "variable"])
        assert os.path.exists(r["html"]) and os.path.exists(r["excel"])
        # reanudar: mismos parámetros -> mismo run_key, todas las tareas salen del checkpoint
        p2 = perfilar_archivo(ruta, **kw)
        assert p2.run_key == p.run_key
        assert len(p2.resultado["alertas"]) == len(r["alertas"])
        assert (p2.resultado["tareas"]["estado"] == "ok").all()
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_asistente():
    out = tempfile.mkdtemp()
    try:
        cat = os.path.join(out, "catalogo.csv")
        open(cat, "w").write("categoria_id,nivel_1,profundidad\n1,A,1\n2,A,2\n3,B,3\n")
        resp = iter([cat, "", "categoria_id", "", "", "s"])
        p = asistente(entrada=lambda _: next(resp), dir_salida=out, verbose=False)
        assert p.resultado["lectura"]["info"]["foto_unica"] and p.llave == ["categoria_id"]
        ruta = os.path.join(out, "movimientos.csv")
        _csv_mensual(ruta)
        resp = iter([ruta, "fecha", "anio", "1", "id_cliente", "", "", "s"])
        p = asistente(entrada=lambda _: next(resp), dir_salida=out, verbose=False)
        assert p.periodos == [2023, 2024]
    finally:
        shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    test_periodos()
    test_archivos()
    test_archivo_mm_yyyy_y_moneda()
    test_mensual_alertas_y_reanudacion()
    test_asistente()
    print("OK")
