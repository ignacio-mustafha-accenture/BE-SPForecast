import sys
import json
from loguru import logger


def serialize(record) -> str:
    entry = {
        "timestamp":   record["time"].isoformat(),
        "level":       record["level"].name,
        "logger":      record["name"],
        "message":     record["message"],
        "request_id":  record["extra"].get("request_id"),
        "user_id":     record["extra"].get("user_id"),
        "user_email":  record["extra"].get("user_email"),
        "action":      record["extra"].get("action"),
        "duration_ms": record["extra"].get("duration_ms"),
        "extra": {
            k: v for k, v in record["extra"].items()
            if k not in ("request_id", "user_id", "user_email", "action", "duration_ms")
        },
    }
    if record["exception"]:
        entry["exception"] = str(record["exception"])
    return json.dumps(entry)


def setup_logging(log_level: str, log_format: str):
    logger.remove()
    # Provide defaults so format strings never raise KeyError outside a request context
    logger.configure(extra={"request_id": "-", "user_id": None, "user_email": None, "action": None, "duration_ms": None})
    if log_format == "json":
        logger.add(sys.stdout, level=log_level, format=serialize, serialize=False)
    else:
        logger.add(
            sys.stdout,
            level=log_level,
            format=(
                "<green>{time:HH:mm:ss}</green> | <level>{level}</level> | "
                "{extra[request_id]:.8s} | {name} | {message}"
            ),
        )
