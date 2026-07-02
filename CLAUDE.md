# ForecastOS — Backend (BE-SPForecast)

Internal capacity and chargeability management app for Accenture S&P Delivery (Argentina, Mexico, Costa Rica).

## Stack

- **Python 3.14** + **FastAPI** with `asynccontextmanager` lifespan
- **asyncpg** — raw parameterized SQL, no ORM, `$1/$2` style params
- **pydantic-settings** `BaseSettings` for config (`extra="ignore"` to allow Supabase vars)
- **loguru** — JSON logging in prod, text in dev; `request_id` propagated via `contextualize`
- **python-jose** — JWT, HS256, stored in HttpOnly cookie (`access_token`, SameSite=Lax, 8h TTL)
- **bcrypt** — password hashing directly (NOT passlib — incompatible with bcrypt 5.x)
- **Supabase PostgreSQL** — hosted DB, Session pooler (IPv4, `aws-0-us-east-1.pooler.supabase.com:5432`), SSL required

## Running locally

```bash
cd BE-SPForecast
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Server runs at `http://localhost:8000`. Requires a `.env` file (see `.env.example`).

## Environment variables (.env)

```
DB_HOST=aws-0-us-east-1.pooler.supabase.com
DB_PORT=5432
DB_USER=postgres.<project-ref>
DB_PASSWORD=<supabase-db-password>
DB_NAME=postgres
SECRET_KEY=<min-32-char-random-string>
CORS_ORIGIN=http://localhost:3000
LOG_LEVEL=INFO
LOG_FORMAT=text
```

## Project structure

```
app/
  main.py            # FastAPI app, lifespan, middleware registration, routers
  config.py          # pydantic-settings Settings singleton
  db.py              # asyncpg pool (create_pool / close_pool)
  logger.py          # loguru setup (JSON prod / text dev)
  errors.py          # AppError enum + ForecastException
  dependencies.py    # require_permission() Depends factory
  middleware/
    request_id.py    # generates UUID request_id, sets X-Request-ID header
    auth.py          # reads access_token cookie, sets request.state.user
    audit.py         # logs every request to audit_log table
  models/
    auth.py          # LoginRequest, UserCreate, UserUpdate, etc.
    employees.py     # EmployeeUpdate
    tickets.py       # TicketCreate, TicketUpdate, TicketAssignEID
    ppa.py           # PPACreate, PPAOut
  services/
    auth_service.py
    state_service.py
    ticket_service.py
    employee_service.py
    ppa_service.py
    recalculate_service.py
    permission_service.py
    audit_service.py
  routers/
    auth.py          # /api/auth
    state.py         # /api/state
    tickets.py       # /api/tickets
    employees.py     # /api/employees
    ppa.py           # /api/ppa
    recalculate.py   # /api/recalculate
    sync.py          # /api/sync
    admin.py         # /api/admin
```

## Middleware order (last added = first executed)

```python
app.add_middleware(CORSMiddleware, ...)   # 4th — outermost
app.add_middleware(AuditMiddleware)        # 3rd
app.add_middleware(AuthMiddleware)         # 2nd
app.add_middleware(RequestIDMiddleware)    # 1st — innermost, runs first
```

## Auth flow

- `POST /api/auth/login` → sets HttpOnly cookie `access_token`
- All protected routes read the cookie via `AuthMiddleware` → `request.state.user`
- Public routes (no auth): `/health`, `/api/auth/login`, `/api/auth/forgot-password`, `/api/auth/reset-password`
- Admin role bypasses all permission checks
- Roles: `admin`, `manager`, `viewer`

## API contract

### Base URL
`http://localhost:8000`

### HTTP status codes
- `GET` success → 200
- `POST` (resource created) → 201
- `PATCH` / action → 200
- Errors → JSON `{ "code": "FO-ERR-XXX", "detail": "...", "extra": "..." }`

### Error codes
| Code | HTTP | Meaning |
|---|---|---|
| FO-ERR-001 | 401 | Invalid credentials |
| FO-ERR-002 | 401 | Token expired |
| FO-ERR-003 | 401 | Not authenticated |
| FO-ERR-004 | 403 | Permission denied |
| FO-ERR-005 | 400 | Invalid/expired reset token |
| FO-ERR-010 | 404 | User not found |
| FO-ERR-011 | 409 | Email already registered |
| FO-ERR-012 | 403 | User inactive |
| FO-ERR-020 | 404 | Employee not found |
| FO-ERR-021 | 409 | EID already in use |
| FO-ERR-030 | 404 | Ticket not found |
| FO-ERR-031 | 400 | Invalid ticket type |
| FO-ERR-032 | 400 | Required ticket fields missing |
| FO-ERR-040 | 400 | PPA fields missing |
| FO-ERR-041 | 400 | Insufficient PPA hours |
| FO-ERR-050 | 404 | Period not found |
| FO-ERR-060 | 404 | Permission not found |
| FO-ERR-090 | 422 | Validation error |
| FO-ERR-091 | 500 | Database error |
| FO-ERR-099 | 500 | Internal server error |

### Endpoints

#### Auth `/api/auth`
| Method | Path | Permission | Description |
|---|---|---|---|
| POST | `/login` | public | Login. Returns user + sets `access_token` cookie |
| POST | `/logout` | any | Clears cookie |
| GET | `/me` | any | Returns current user |
| POST | `/forgot-password` | public | Sends reset token by email |
| POST | `/reset-password` | public | Resets password with token |
| POST | `/users` | admin:users | Create user |
| GET | `/users` | admin:users | List all users |
| PATCH | `/users/{id}` | admin:users | Update user |

**Login request:**
```json
{ "email": "user@accenture.com", "password": "..." }
```
**Login response (201):**
```json
{ "id": 1, "email": "...", "full_name": "...", "role": "admin" }
```
**Me response:**
```json
{ "id": 1, "email": "...", "full_name": "...", "role": "admin", "eid": "ramos.lucas" }
```

#### State `/api/state`
| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/?window_offset=0` | state:read | Full app state |

**Response shape:**
```json
{
  "periods": [{ "period_name": "Jun-P1", "start_date": "...", "end_date": "..." }],
  "employees": [{
    "EID": "ramos.lucas",
    "Name": "Lucas Ramos",
    "Country": "Argentina",
    "CL": "7",
    "FTE": 1.0,
    "Client": "Google",
    "ChargeabilityPct": 100.0,
    "RollOn": "02/02/26",
    "RollOff": "31/12/26",
    "FAD": "01/01/27",
    "DaysToAvailable": 234.0,
    "NewJoiner": false,
    "chg": [80, 72, 80, 72, 80, 72],
    "sah": [80, 72, 80, 72, 80, 72],
    "cp": [100, 100, 100, 100, 100, 100],
    "sickDays": [0, 0, 0, 0, 0, 0],
    "ppaAdj": [0, 0, 0, 0, 0, 0]
  }],
  "targets": { ... },
  "tickets": [],
  "ppa_log": []
}
```
- `chg/sah/cp/sickDays/ppaAdj` are arrays of 6 values, one per period in the window
- Periods format: `"Jun-P1"`, `"Jun-P2"`, `"Jul-P1"`, etc.
- Dates format: `"DD/MM/YY"`

#### Tickets `/api/tickets`
| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/` | tickets:read | List all tickets |
| POST | `/` | tickets:create | Create ticket (201) |
| PATCH | `/{id}` | tickets:update | Update ticket |
| PATCH | `/{id}/eid` | tickets:assign_eid | Assign EID to NJ ticket |

**Ticket types:** `newproj`, `ongoing`, `pto`, `sick`, `nj`, `baja`

**Create ticket body:**
```json
{
  "type": "newproj",
  "eid": "ramos.lucas",
  "detail": "New project description",
  "status": "Abierto",
  "client_name": "Google",
  "offering_type": "CTO",
  "chargeability_pct": 100,
  "start_date": "2026-07-01",
  "end_date": "2026-12-31"
}
```

#### Employees `/api/employees`
| Method | Path | Permission | Description |
|---|---|---|---|
| PATCH | `/{eid}` | employees:update | Update employee |

**Update body (all optional):**
```json
{
  "new_eid": "new.eid",
  "name": "New Name",
  "cl": 9.0,
  "client": "Google",
  "offering": "CTO",
  "roll_on": "2026-01-01",
  "roll_off": "2026-12-31",
  "chargeability_pct": 100.0,
  "account_manager": "daniela.robles",
  "notes": "...",
  "next_client": "..."
}
```

#### PPA `/api/ppa`
| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/` | ppa:read | List PPA log |
| POST | `/` | ppa:create | Create PPA entry (201) |

**Create PPA body:**
```json
{
  "eid": "ramos.lucas",
  "from_period": "Jun-P1",
  "to_period": "Jun-P2",
  "hours": 16,
  "reason": "Project overlap"
}
```

#### Recalculate `/api/recalculate`
| Method | Path | Permission | Description |
|---|---|---|---|
| POST | `/employee/{eid}` | recalculate:employee | Recalculate all periods for one employee |
| POST | `/{period_name}` | recalculate:period | Recalculate all employees for a period |

#### Sync `/api/sync`
| Method | Path | Permission | Description |
|---|---|---|---|
| POST | `/` | admin:sync | Run LOAD_DATA_SCRIPT subprocess |

#### Admin `/api/admin`
| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/audit-log` | admin:audit_log | Paginated audit log |
| GET | `/permissions` | admin:permissions | List all permissions |
| GET | `/roles/{role}/permissions` | admin:permissions | Get role permissions |
| PATCH | `/roles/{role}/permissions` | admin:permissions | Update role permissions |
| GET | `/users/{id}/permissions` | admin:permissions | Get user permission overrides |
| PATCH | `/users/{id}/permissions` | admin:permissions | Set user permission overrides |
| DELETE | `/users/{id}/permissions/{perm_id}` | admin:permissions | Remove user permission override |

## Permissions system

Permissions are resolved in order:
1. `user_permissions` for `(user_id, action)` — takes priority
2. `role_permissions` for `(role, action)` — fallback
3. Deny if neither found

`admin` role bypasses all checks.

**Available permissions:**
`state:read`, `tickets:read`, `tickets:create`, `tickets:update`, `tickets:assign_eid`,
`employees:update`, `ppa:read`, `ppa:create`, `recalculate:employee`, `recalculate:period`,
`admin:users`, `admin:audit_log`, `admin:permissions`, `admin:sync`

**Default role grants:**
- `viewer` → `state:read`, `tickets:read`, `ppa:read`
- `manager` → all except `admin:*`

## Database schema (key tables)

- `employees` — `eid` (PK-like), `name`, `country`, `cl`, `active`, `new_joiner`, `people_lead` (self-ref FK to eid)
- `forecast_update` — one row per active employee, latest forecast state (`client`, `roll_on`, `roll_off`, `chargeability_pct`, etc.)
- `forecast_periods` — one row per (eid, period_name), historical `chg`/`sah`/`chg_pct`
- `periods` — calendar periods (`period_name`, `start_date`, `end_date`)
- `absences` — PTO/sick absences per employee
- `tickets` — change requests (`type`, `eid`, `status`, etc.)
- `ppa_log` — PPA transfer log
- `users` — app users with `role` (admin/manager/viewer), `hashed_password`, optional `eid` link
- `audit_log` — immutable request log (method, path, status, duration, user_id)

## Known issues / gotchas

- `\restrict` line in pg_dump exports from Supabase — strip before running in SQL Editor
- Supabase direct connection (`db.*.supabase.co`) only has AAAA (IPv6) — use Session pooler on Windows
- asyncpg Session pooler needs `ssl="require"` — do not remove
- Self-referential FK on `employees.people_lead` — insert with NULL first, then UPDATE
- `recalculate_forecast_period(eid, period_name)` stored proc must be called sequentially (never parallel)
- `/api/recalculate/employee/{eid}` route must be declared BEFORE `/{period_name}` to avoid routing conflict
- bcrypt 5.x is incompatible with passlib — use `import bcrypt` directly
