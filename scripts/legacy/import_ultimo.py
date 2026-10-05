"""
import_ultimo.py
----------------
Importa el forecast del periodo actual sin que nadie tenga que pasar la ruta.

Busca en la carpeta de SharePoint sincronizada el archivo mas reciente y se lo
pasa a import_forecast.py. Evita el error de importar el archivo del mes pasado,
que es facil de cometer cuando el nombre cambia cada periodo.

Chequea antes de correr:
  - Que la carpeta exista (si no, hay que sincronizarla desde SharePoint)
  - Que el archivo este descargado y no sea un placeholder de OneDrive
  - Cual es el mas reciente y de que fecha es

Uso:
    python scripts/import_ultimo.py            # dry-run
    python scripts/import_ultimo.py --apply    # escribe
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Carpeta de SharePoint sincronizada. Si alguien la sincroniza en otra ruta,
# se agrega aca en vez de tocar el resto del script.
CARPETAS = [
    Path.home() / "Accenture" / "Forecast - 01 Forecast P Actual",
]

EXTENSIONES = (".xlsx", ".xlsm")

# Tamano minimo razonable. Un placeholder de OneDrive pesa unos pocos bytes.
MIN_BYTES = 50_000


def encontrar_carpeta() -> Path:
    for c in CARPETAS:
        if c.is_dir():
            return c
    print("No encuentro la carpeta de forecast. Busque en:")
    for c in CARPETAS:
        print(f"   {c}")
    print("\nSincronizala desde SharePoint (boton Sync) o agrega la ruta a CARPETAS.")
    sys.exit(1)


def archivo_mas_reciente(carpeta: Path) -> Path:
    archivos = [
        f for f in carpeta.iterdir()
        if f.is_file()
        and f.suffix.lower() in EXTENSIONES
        and not f.name.startswith("~$")   # temporales de Excel abierto
    ]
    if not archivos:
        print(f"No hay archivos {'/'.join(EXTENSIONES)} en {carpeta}")
        sys.exit(1)

    archivos.sort(key=lambda f: f.stat().st_mtime, reverse=True)

    print(f"Carpeta: {carpeta}")
    print(f"Archivos encontrados ({len(archivos)}):")
    for i, f in enumerate(archivos):
        marca = "  <-- se usa este" if i == 0 else ""
        fecha = datetime.fromtimestamp(f.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
        print(f"   {f.name}   modificado {fecha}{marca}")
    print()

    elegido = archivos[0]

    if elegido.stat().st_size < MIN_BYTES:
        print(f"'{elegido.name}' pesa {elegido.stat().st_size} bytes.")
        print("Parece un placeholder de OneDrive y no el archivo real.")
        print("Abrilo una vez desde el Explorador, o click derecho ->")
        print("'Mantener siempre en este dispositivo', y volve a correr.")
        sys.exit(1)

    return elegido


def main():
    carpeta = encontrar_carpeta()
    archivo = archivo_mas_reciente(carpeta)

    script = Path(__file__).parent / "import_forecast.py"
    if not script.exists():
        print(f"No encuentro {script}")
        sys.exit(1)

    cmd = [sys.executable, str(script), str(archivo)]
    if "--apply" in sys.argv:
        cmd.append("--apply")
    else:
        print("[DRY RUN] Agrega --apply para escribir.\n")

    sys.exit(subprocess.call(cmd))


if __name__ == "__main__":
    main()