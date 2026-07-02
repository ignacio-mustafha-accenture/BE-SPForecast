# ForecastOS — FastAPI Backend

Backend de la aplicación interna de gestión de capacidad y chargeabilidad del equipo S&P Delivery de Accenture (Argentina, México, Costa Rica).

## Stack

| Componente | Tecnología |
|---|---|
| Framework | FastAPI + Uvicorn |
| Base de datos | PostgreSQL (asyncpg, raw SQL) |
| Auth | JWT (python-jose) + HttpOnly cookies |
| Hashing | bcrypt (passlib) |
| Email | fastapi-mail |
| Logging | loguru (JSON en prod, texto en local) |
| Config | pydantic-settings (.env) |

---

## Requisitos previos

| Herramienta | Versión mínima | Verificar |
|---|---|---|
| Python | 3.10+ | `python --version` |
| pip | — | `pip --version` |
| PostgreSQL | 14+ | `psql --version` |

---

## Instalación

### 1. Clonar el repositorio

```bash
git clone https://github.com/ignacio-mustafha-accenture/BE-SPForecast.git
cd BE-SPForecast
```

### 2. Crear entorno virtual e instalar dependencias

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Configurar variables de entorno

```bash
cp .env.example .env
```

Editar `.env` con los valores reales:

```env
# Database
DB_HOST=localhost
DB_PORT=5432
DB_USER=postgres
DB_PASSWORD=tu_password
DB_NAME=forecast

# Server
PORT=8000

# Auth — SECRET_KEY debe tener mínimo 32 caracteres
SECRET_KEY=genera_una_clave_segura_de_al_menos_32_chars
ACCESS_TOKEN_EXPIRE_MINUTES=480
RESET_TOKEN_EXPIRE_MINUTES=30

# Email (Outlook / Office 365)
MAIL_USERNAME=tu_email@accenture.com
MAIL_PASSWORD=tu_password
MAIL_FROM=tu_email@accenture.com
MAIL_SERVER=smtp.office365.com
MAIL_PORT=587

# CORS
CORS_ORIGIN=http://localhost:3000

# Script de sincronización de datos
LOAD_DATA_SCRIPT=../db/load_data.py
LOAD_DATA_CWD=../db

# Logging: json (prod) | text (local dev)
LOG_LEVEL=INFO
LOG_FORMAT=text

# Performance
SLOW_QUERY_THRESHOLD_MS=500
```

> **Generar SECRET_KEY segura:**
> ```bash
> python -c "import secrets; print(secrets.token_hex(32))"
> ```

### 4. Preparar la base de datos

```bash
# Crear la base de datos (si no existe)
psql -U postgres -c "CREATE DATABASE forecast;"

# Aplicar schema principal
psql -U postgres -d forecast -f ../db/schema.sql

# Aplicar patch (unique constraint en forecast_periods)
psql -U postgres -d forecast -f schema_patch.sql

# Definir el stored procedure de recálculo
psql -U postgres -d forecast -f recalculate.sql

# Cargar datos iniciales (empleados, calendario, etc.)
# psql -U postgres -d forecast -f ../db/employees_dump_utf8.sql

# Aplicar tablas de auth + seed de permisos
psql -U postgres -d forecast -f schema_auth.sql
```

---

## Levantar el servidor

### Desarrollo (con hot reload)

```bash
uvicorn app.main:app --reload --port 8000
```

### Producción

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

El servidor queda disponible en: `http://localhost:8000`

---

## Verificar instalación

### Health check

```bash
curl http://localhost:8000/health
# {"ok":true,"ts":"2026-07-02T14:30:00.123Z"}
```

### Documentación interactiva (Swagger)

```
http://localhost:8000/docs
```

### Validar conexión a DB

```bash
python -c "
import asyncio, asyncpg
async def test():
    conn = await asyncpg.connect('postgresql://postgres:password@localhost/forecast')
    v = await conn.fetchval('SELECT version()')
    print('DB OK:', v[:40])
    await conn.close()
asyncio.run(test())
"
```

---

## Endpoints

### Auth (públicos)
| Método | Endpoint | Descripción |
|---|---|---|
| POST | `/api/auth/login` | Login → HttpOnly cookie |
| POST | `/api/auth/logout` | Limpia cookie |
| GET | `/api/auth/me` | Usuario actual |
| POST | `/api/auth/forgot-password` | Solicitar reset |
| POST | `/api/auth/reset-password` | Resetear con token |

### Auth (admin:users)
| Método | Endpoint | Descripción |
|---|---|---|
| POST | `/api/auth/users` | Crear usuario |
| GET | `/api/auth/users` | Listar usuarios |
| PATCH | `/api/auth/users/{id}` | Actualizar usuario |

### Negocio
| Método | Endpoint | Permiso | Descripción |
|---|---|---|---|
| GET | `/api/state` | state:read | Estado completo (períodos, empleados, tickets, PPA) |
| GET | `/api/tickets` | tickets:read | Listar tickets |
| POST | `/api/tickets` | tickets:create | Crear ticket |
| PATCH | `/api/tickets/{id}` | tickets:update | Actualizar ticket |
| PATCH | `/api/tickets/{id}/eid` | tickets:assign_eid | Promover New Joiner |
| PATCH | `/api/employees/{eid}` | employees:update | Actualizar empleado |
| GET | `/api/ppa` | ppa:read | Ver log PPA |
| POST | `/api/ppa` | ppa:create | Crear ajuste PPA |
| POST | `/api/recalculate/employee/{eid}` | recalculate:employee | Recalcular empleado |
| POST | `/api/recalculate/{period_name}` | recalculate:period | Recalcular período |
| POST | `/api/sync` | sync:run | Sincronizar datos |

### Admin
| Método | Endpoint | Descripción |
|---|---|---|
| GET | `/api/admin/audit-log` | Log de auditoría paginado |
| GET | `/api/admin/permissions` | Catálogo de permisos |
| GET/PATCH | `/api/admin/roles/{role}/permissions` | Permisos por rol |
| GET/PATCH | `/api/admin/users/{id}/permissions` | Overrides por usuario |
| DELETE | `/api/admin/users/{id}/permissions/{perm_id}` | Eliminar override |

---

## Roles y permisos

| Rol | Acceso |
|---|---|
| `admin` | Todo (bypasea checks) |
| `manager` | Todo excepto `admin:*` |
| `viewer` | `state:read`, `tickets:read`, `ppa:read` |

Los permisos individuales en `user_permissions` sobreescriben los defaults del rol.

---

## Tipos de tickets

| Tipo | Side-effect |
|---|---|
| `newproj` | Actualiza `forecast_update`, recalcula todos los períodos |
| `ongoing` | Actualiza `roll_off` / `chargeability_pct`, recalcula |
| `pto` | Inserta en `absences` tipo PTO |
| `sick` | Inserta en `absences` tipo SICK, recalcula |
| `nj` | Crea empleado con eid `NJ_nombre.apellido` |
| `baja` | Setea `termination_date` en `employees` |

---

## Logging

- **Producción** (`LOG_FORMAT=json`): JSON estructurado por stdout con `request_id`, `user_id`, `action`, `duration_ms`.
- **Desarrollo** (`LOG_FORMAT=text`): Texto legible con colores.
- Cada request genera un UUID (`X-Request-ID` header) trazable en todos los logs.
- Queries lentas (> `SLOW_QUERY_THRESHOLD_MS` ms) se loguean como WARNING.

---

## Estructura del proyecto

```
app/
├── main.py                  # FastAPI app, lifespan, middleware, routers
├── config.py                # Settings (pydantic-settings)
├── db.py                    # asyncpg pool
├── logger.py                # loguru setup
├── errors.py                # AppError enum + ForecastException
├── dependencies.py          # require_permission()
├── middleware/
│   ├── request_id.py        # UUID por request
│   ├── auth.py              # JWT validation global
│   └── audit.py             # DB audit log
├── models/                  # Pydantic schemas
├── routers/                 # Endpoints
└── services/                # Lógica de negocio
```

---

## Errores

Todos los errores tienen el formato:

```json
{ "code": "FO-ERR-020", "detail": "Employee not found" }
```

| Rango | Dominio |
|---|---|
| FO-ERR-001–009 | Auth |
| FO-ERR-010–019 | Usuarios |
| FO-ERR-020–029 | Empleados |
| FO-ERR-030–039 | Tickets |
| FO-ERR-040–049 | PPA |
| FO-ERR-050–059 | Períodos |
| FO-ERR-060–069 | Permisos |
| FO-ERR-090–099 | General / DB |
