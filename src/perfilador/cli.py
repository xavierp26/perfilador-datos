"""
Línea de comandos:

    perfilador ruta/archivo.xlsx
    perfilador ruta/saldos.csv --fecha fecha_corte --llave id_cliente
    perfilador ruta/transacciones.csv --fecha fecha --llave id --granularidad anio
    perfilador ruta/archivo.xlsx --hoja "Hoja 2" --dominio tasa:0:1 --regla "saldo <= limite"

También: python -m perfilador ...  Al terminar abre el reporte HTML (use --no-abrir para evitarlo).
"""
import argparse
import os
import webbrowser

from .archivos import perfilar_archivo


def main():
    ap = argparse.ArgumentParser(prog="perfilador", description="Perfilamiento y validación de calidad de archivos tabulares")
    ap.add_argument("ruta", help="archivo .xlsx / .xlsm / .xls / .csv / .txt")
    ap.add_argument("--fecha", help="campo fecha (sin esto: foto única)")
    ap.add_argument("--llave", help="columna(s) únicas, separadas por coma")
    ap.add_argument("--granularidad", choices=["anio", "mes", "dia"], help="periodo de análisis (por defecto mes)")
    ap.add_argument("--hoja", help="hoja de Excel (por defecto la primera visible)")
    ap.add_argument("--sep", help="separador del CSV si la detección falla")
    ap.add_argument("--decimal", help="',' si los números usan coma decimal y la detección falla")
    ap.add_argument("--dominio", action="append", default=[], help="var:min:max (repetible)")
    ap.add_argument("--regla", action="append", default=[], help="expresión SQL que debe ser verdadera (repetible)")
    ap.add_argument("--salida", default=None, help="carpeta de salida (por defecto junto al archivo)")
    ap.add_argument("--no-abrir", action="store_true", help="no abrir el reporte al terminar")
    a = ap.parse_args()

    dominio = {}
    for d in a.dominio:
        v, mn, mx = (d.split(":") + ["", ""])[:3]
        dominio[v] = {"min": float(mn) if mn else None, "max": float(mx) if mx else None}
    reglas = {"regla_%d" % (i + 1): r for i, r in enumerate(a.regla)}
    salida = a.salida or os.path.join(os.path.dirname(os.path.abspath(a.ruta)), "perfilador_salidas")

    extra = {"granularidad": a.granularidad} if a.granularidad else {}
    perf = perfilar_archivo(a.ruta, campo_fecha=a.fecha, llave=a.llave, hoja=a.hoja, sep=a.sep, decimal=a.decimal, **extra,
                            reglas_dominio=dominio or None, reglas=reglas or None, dir_salida=salida)
    html = perf.resultado.get("html")
    if html and not a.no_abrir:
        webbrowser.open("file:///" + os.path.abspath(html).replace("\\", "/"))


if __name__ == "__main__":
    main()
