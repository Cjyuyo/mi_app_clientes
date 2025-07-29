import os
import psycopg2
from dotenv import load_dotenv

def actualizar_esquema_admin_v2():
    """
    Añade las columnas 'ultimo_login' y 'estatus_online' a la tabla 'administradores'
    para rastrear la actividad de los usuarios.
    """
    load_dotenv()
    DATABASE_URL = os.getenv('DATABASE_URL')
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            print("Añadiendo columnas 'ultimo_login' y 'estatus_online'...")
            # Usamos TIMESTAMPTZ para que la hora sea consistente sin importar el servidor
            cur.execute("""
                ALTER TABLE administradores
                ADD COLUMN IF NOT EXISTS ultimo_login TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS estatus_online BOOLEAN NOT NULL DEFAULT FALSE;
            """)
            print("¡Tabla 'administradores' actualizada con éxito!")
            conn.commit()
    except psycopg2.Error as e:
        print(f"ERROR de base de datos: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    actualizar_esquema_admin_v2()