-- Auth tables for ForecastOS
-- Run after schema.sql and schema_patch.sql

-- USERS
CREATE TABLE IF NOT EXISTS users (
    id               SERIAL PRIMARY KEY,
    email            VARCHAR(255) UNIQUE NOT NULL,
    hashed_password  VARCHAR(255) NOT NULL,
    full_name        VARCHAR(200),
    role             VARCHAR(50) DEFAULT 'viewer',
    is_active        BOOLEAN DEFAULT TRUE,
    eid              VARCHAR(100) REFERENCES employees(eid),
    created_at       TIMESTAMP DEFAULT NOW(),
    updated_at       TIMESTAMP DEFAULT NOW()
);

-- PASSWORD_RESET_TOKENS
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id),
    token       VARCHAR(255) UNIQUE NOT NULL,
    expires_at  TIMESTAMP NOT NULL,
    used        BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMP DEFAULT NOW()
);

-- PERMISSIONS (catalog)
CREATE TABLE IF NOT EXISTS permissions (
    id          SERIAL PRIMARY KEY,
    action      VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    method      VARCHAR(10),
    endpoint    VARCHAR(200)
);

-- ROLE_PERMISSIONS
CREATE TABLE IF NOT EXISTS role_permissions (
    id            SERIAL PRIMARY KEY,
    role          VARCHAR(50) NOT NULL,
    permission_id INTEGER REFERENCES permissions(id),
    granted       BOOLEAN DEFAULT TRUE,
    updated_by    INTEGER REFERENCES users(id),
    updated_at    TIMESTAMP DEFAULT NOW(),
    UNIQUE (role, permission_id)
);

-- USER_PERMISSIONS (overrides role defaults)
CREATE TABLE IF NOT EXISTS user_permissions (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER REFERENCES users(id),
    permission_id INTEGER REFERENCES permissions(id),
    granted       BOOLEAN NOT NULL,
    updated_by    INTEGER REFERENCES users(id),
    updated_at    TIMESTAMP DEFAULT NOW(),
    UNIQUE (user_id, permission_id)
);

-- AUDIT_LOG (append-only)
CREATE TABLE IF NOT EXISTS audit_log (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER REFERENCES users(id),
    user_email      VARCHAR(255),
    action          VARCHAR(100),
    method          VARCHAR(10),
    endpoint        VARCHAR(500),
    request_body    JSONB,
    response_status INTEGER,
    success         BOOLEAN,
    error_message   TEXT,
    ip_address      VARCHAR(50),
    duration_ms     INTEGER,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS audit_log_user_idx    ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS audit_log_created_idx ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS audit_log_action_idx  ON audit_log(action);

-- Seed permissions
INSERT INTO permissions (action, description, method, endpoint) VALUES
  ('state:read',           'View full state',           'GET',   '/api/state'),
  ('tickets:read',         'View tickets',              'GET',   '/api/tickets'),
  ('tickets:create',       'Create ticket',             'POST',  '/api/tickets'),
  ('tickets:update',       'Update ticket',             'PATCH', '/api/tickets/:id'),
  ('tickets:assign_eid',   'Promote New Joiner',        'PATCH', '/api/tickets/:id/eid'),
  ('employees:update',     'Update employee data',      'PATCH', '/api/employees/:eid'),
  ('ppa:read',             'View PPA log',              'GET',   '/api/ppa'),
  ('ppa:create',           'Create PPA adjustment',     'POST',  '/api/ppa'),
  ('recalculate:period',   'Recalculate period',        'POST',  '/api/recalculate/:period'),
  ('recalculate:employee', 'Recalculate employee',      'POST',  '/api/recalculate/employee/:eid'),
  ('sync:run',             'Trigger data sync',         'POST',  '/api/sync'),
  ('admin:audit_log',      'View audit log',            'GET',   '/api/admin/audit-log'),
  ('admin:permissions',    'Manage permissions',        'ANY',   '/api/admin/permissions'),
  ('admin:users',          'Manage users',              'ANY',   '/api/auth/users')
ON CONFLICT (action) DO NOTHING;

-- Seed role_permissions for viewer (state:read, tickets:read, ppa:read)
INSERT INTO role_permissions (role, permission_id, granted)
SELECT 'viewer', id, TRUE FROM permissions
WHERE action IN ('state:read', 'tickets:read', 'ppa:read')
ON CONFLICT (role, permission_id) DO NOTHING;

-- Seed role_permissions for manager (all except admin:*)
INSERT INTO role_permissions (role, permission_id, granted)
SELECT 'manager', id, TRUE FROM permissions
WHERE action NOT LIKE 'admin:%'
ON CONFLICT (role, permission_id) DO NOTHING;
