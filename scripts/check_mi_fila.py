import openpyxl, sys

wb = openpyxl.load_workbook(sys.argv[1], read_only=True, data_only=True)
ws = wb["Forecast Update"]
filas = list(ws.iter_rows(values_only=True))
h = next(i for i, f in enumerate(filas[:30])
         if any(str(c).strip() == "EID" for c in f if c))

objetivo = "maria.jose.matar"
encontrada = False
for f in filas[h + 1:]:
    eid = str(f[1]).strip() if len(f) > 1 and f[1] else ""
    if eid == objetivo:
        encontrada = True
        print("Fila encontrada. Todas las celdas con valor:")
        for j, v in enumerate(f):
            if v not in (None, ""):
                enc = str(filas[h][j] or "").replace(chr(10), " ").strip() if j < len(filas[h]) else ""
                print("   col %2d  %-18s = %r" % (j, enc, v))
        break

if not encontrada:
    print("NO esta en el archivo. EIDs que empiezan con maria:")
    for f in filas[h + 1:]:
        eid = str(f[1]).strip() if len(f) > 1 and f[1] else ""
        if eid.lower().startswith("maria"):
            print("   ", eid)
wb.close()
