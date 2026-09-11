import openpyxl, sys

wb = openpyxl.load_workbook(sys.argv[1], read_only=True, data_only=True)
ws = wb["Forecast Update"]
filas = list(ws.iter_rows(values_only=True))

h = next(i for i, f in enumerate(filas[:30])
         if any(str(c).strip() == "EID" for c in f if c))
print("Encabezado en fila", h + 1)
print()

print("Columnas de la 18 en adelante:")
etiquetas = filas[h - 8] if h >= 8 else []
for j in range(18, min(45, len(filas[h]))):
    enc = str(filas[h][j] or "").replace(chr(10), " ").strip()
    per = str(etiquetas[j] or "").strip() if j < len(etiquetas) else ""
    if enc or per:
        print("   col %2d  header=%-16r periodo=%r" % (j, enc, per))

print()
print("Fila de maria.jose.matar:")
for f in filas[h + 1:]:
    if len(f) > 1 and str(f[1]).strip() == "maria.jose.matar":
        for j in range(18, min(35, len(f))):
            if f[j] not in (None, ""):
                print("   col %2d = %r" % (j, f[j]))
        break
wb.close()
