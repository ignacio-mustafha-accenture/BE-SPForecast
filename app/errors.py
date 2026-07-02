from enum import Enum


class AppError(Enum):
    # Auth (001–009)
    INVALID_CREDENTIALS = ("FO-ERR-001", 401, "Invalid credentials")
    TOKEN_EXPIRED       = ("FO-ERR-002", 401, "Token expired")
    TOKEN_MISSING       = ("FO-ERR-003", 401, "Not authenticated")
    PERMISSION_DENIED   = ("FO-ERR-004", 403, "Permission denied")
    RESET_TOKEN_INVALID = ("FO-ERR-005", 400, "Invalid or expired reset token")

    # Users (010–019)
    USER_NOT_FOUND      = ("FO-ERR-010", 404, "User not found")
    USER_ALREADY_EXISTS = ("FO-ERR-011", 409, "Email already registered")
    USER_INACTIVE       = ("FO-ERR-012", 403, "User account is inactive")

    # Employees (020–029)
    EMPLOYEE_NOT_FOUND  = ("FO-ERR-020", 404, "Employee not found")
    EMPLOYEE_EID_TAKEN  = ("FO-ERR-021", 409, "EID already in use")

    # Tickets (030–039)
    TICKET_NOT_FOUND    = ("FO-ERR-030", 404, "Ticket not found")
    TICKET_INVALID_TYPE = ("FO-ERR-031", 400, "Invalid ticket type")
    TICKET_MISSING_FIELDS = ("FO-ERR-032", 400, "Required fields missing for this ticket type")

    # PPA (040–049)
    PPA_MISSING_FIELDS     = ("FO-ERR-040", 400, "eid, from_period, to_period and hours are required")
    PPA_INSUFFICIENT_HOURS = ("FO-ERR-041", 400, "Insufficient hours in source period")

    # Periods (050–059)
    PERIOD_NOT_FOUND    = ("FO-ERR-050", 404, "Period not found")

    # Permissions (060–069)
    PERMISSION_NOT_FOUND = ("FO-ERR-060", 404, "Permission not found")

    # General (090–099)
    VALIDATION_ERROR    = ("FO-ERR-090", 422, "Validation error")
    DB_ERROR            = ("FO-ERR-091", 500, "Database error")
    INTERNAL_ERROR      = ("FO-ERR-099", 500, "Internal server error")

    @property
    def code(self):   return self.value[0]
    @property
    def status(self): return self.value[1]
    @property
    def detail(self): return self.value[2]


class ForecastException(Exception):
    def __init__(self, error: AppError, extra: str = None):
        self.error = error
        self.extra = extra
        super().__init__(error.detail)
