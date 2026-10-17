"""
Perfila un archivo sin instalar el paquete (equivale al comando `perfilador`):

    python perfilar_archivo.py ruta/archivo.csv --fecha fecha_corte --llave id
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from perfilador.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
