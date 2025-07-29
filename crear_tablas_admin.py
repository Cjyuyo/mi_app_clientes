import os
import psycopg2
from dotenv import load_dotenv

def crear_tablas_admin():
    """
    Se conecta a la base de datos y crea las tablas 'administradores' y
    'registros_auditoria' si no existen, para el portal de administración.
    """
    load_dotenv()
    DATABASE_URL = os.getenv('DATABASE_URL')
    if not DATABASE_URL:
        print("ERROR: La variable de entorno DATABASE_URL no está configurada.")
        return

    conn = None
    try:
        print("Conectando a la base de datos para crear tablas de administración...")
        conn = psycopg2.connect(DATABASE_URL)
        print("¡Conexión exitosa!")
        
        with conn.cursor() as cur:
            # --- 1. Crear la tabla 'administradores' ---
            # Almacenará los usuarios del sistema, sus contraseñas hasheadas y su rol.
            print("\nVerificando/Creando la tabla 'administradores'...")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS administradores (
                    id SERIAL PRIMARY KEY,
                    usuario TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    rol TEXT NOT NULL,
                    fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            print("Tabla 'administradores' lista.")

            # --- 2. Crear la tabla 'registros_auditoria' ---
            # Registrará acciones importantes realizadas por los administradores.
            print("\nVerificando/Creando la tabla 'registros_auditoria'...")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS registros_auditoria (
                    id SERIAL PRIMARY KEY,
                    admin_id INTEGER REFERENCES administradores(id),
                    admin_usuario TEXT,
                    accion TEXT NOT NULL,
                    detalles TEXT,
                    ip_address TEXT,
                    fecha_registro TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            print("Tabla 'registros_auditoria' lista.")

            conn.commit()
            print("\n✅ ¡Éxito! Las tablas de administración se han verificado/creado correctamente.")

    except psycopg2.Error as e:
        print(f"\n❌ ERROR de base de datos: {e}")
        if conn: conn.rollback()
    except Exception as e:
        print(f"\n❌ ERROR inesperado: {e}")
        if conn: conn.rollback()
    finally:
        if conn:
            conn.close()
            print("Conexión a la base de datos cerrada.")

if __name__ == '__main__':
    crear_tablas_admin()