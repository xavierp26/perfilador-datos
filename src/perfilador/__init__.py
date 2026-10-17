"""perfilador: perfilamiento y validación de calidad de archivos tabulares (.csv, .txt, .xlsx, .xlsm)."""
__all__ = ["perfilar_archivo", "leer_archivo", "asistente", "Perfilador", "__version__"]
from ._version import __version__              # noqa: F401
from .archivos import perfilar_archivo, leer_archivo   # noqa: F401
from .asistente import asistente               # noqa: F401
from .perfilador import Perfilador             # noqa: F401
