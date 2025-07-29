import os
import psycopg2
from dotenv import load_dotenv

def crear_tabla_auditoria():
    """Crea la tabla de registros de auditoría si no existe."""
    load_dotenv()
    DATABASE_URL = os.getenv('DATABASE_URL')
    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS registros_auditoria (
                    id SERIAL PRIMARY KEY,
                    fecha_hora TIMESTAMPTZ DEFAULT NOW(),
                    usuario_id INTEGER REFERENCES administradores(id),
                    usuario_nombre TEXT,
                    accion TEXT NOT NULL,
                    descripcion TEXT,
                    cliente_afectado_id INTEGER REFERENCES clientes(id) ON DELETE SET NULL
                );
            """)
            conn.commit()
            print("¡Tabla 'registros_auditoria' verificada o creada exitosamente!")
    except psycopg2.Error as e:
        print(f"ERROR de base de datos: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    crear_tabla_auditoria()