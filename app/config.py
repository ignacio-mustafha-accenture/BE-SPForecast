from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USER: str = "postgres"
    DB_PASSWORD: str = ""
    DB_NAME: str = "forecast"

    PORT: int = 8000

    SECRET_KEY: str = ""
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    RESET_TOKEN_EXPIRE_MINUTES: int = 30

    MAIL_USERNAME: str = ""
    MAIL_PASSWORD: str = ""
    MAIL_FROM: str = ""
    MAIL_SERVER: str = "smtp.office365.com"
    MAIL_PORT: int = 587

    CORS_ORIGIN: str = "http://localhost:3000"

    LOAD_DATA_SCRIPT: str = "../db/load_data.py"
    LOAD_DATA_CWD: str = "../db"

    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "json"

    SLOW_QUERY_THRESHOLD_MS: int = 500

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
