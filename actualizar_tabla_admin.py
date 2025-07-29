import os
import psycopg2
from dotenv import load_dotenv

def actualizar_esquema_admin():
    """
    Añade las columnas 'nombre_completo' y 'estatus' a la tabla 'administradores'
    para una gestión de usuarios más robusta.
    """
    load_dotenv()
    DATABASE_URL = os.getenv('DATABASE_URL')
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            print("Verificando y añadiendo columnas 'nombre_completo' y 'estatus' a la tabla 'administradores'...")
            cur.execute("""
                ALTER TABLE administradores
                ADD COLUMN IF NOT EXISTS nombre_completo TEXT,
                ADD COLUMN IF NOT EXISTS estatus TEXT NOT NULL DEFAULT 'activo';
            """)
            print("¡Tabla 'administradores' actualizada con éxito!")
            conn.commit()
    except psycopg2.Error as e:
        print(f"ERROR de base de datos: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    actualizar_esquema_admin()