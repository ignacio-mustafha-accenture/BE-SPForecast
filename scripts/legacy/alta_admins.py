import asyncio, asyncpg, bcrypt
from app.config import settings

USUARIOS = [
    ("maria.jose.matar@accenture.com", "Dev12345!", "Maria Jose Matar", "admin"),
    ("ezequiel.ferrante@accenture.com", "Password1q2w3e4R?", "Ezequiel Ferrante", "admin"),
]

async def main():
    conn = await asyncpg.connect(host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        database=settings.DB_NAME, ssl="require")
    for email, pwd, name, role in USUARIOS:
        existe = await conn.fetchval("SELECT id FROM users WHERE email=$1", email)
        hashed = bcrypt.hashpw(pwd.encode(), bcrypt.gensalt()).decode()
        if existe:
            await conn.execute(
                "UPDATE users SET hashed_password=$1, role=$2, full_name=$3, is_active=TRUE, updated_at=NOW() WHERE email=$4",
                hashed, role, name, email)
            print(f"  {email:40} actualizado -> {role}")
        else:
            await conn.execute(
                "INSERT INTO users (email, hashed_password, full_name, role, is_active) VALUES ($1,$2,$3,$4,TRUE)",
                email, hashed, name, role)
            print(f"  {email:40} creado -> {role}")
    await conn.close()

asyncio.run(main())
