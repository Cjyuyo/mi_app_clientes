import os
import psycopg2
from dotenv import load_dotenv

def actualizar_tabla_auditoria():
    """
    Verifica y añade la columna 'usuario_id' a la tabla 'registros_auditoria' si no existe.
    """
    load_dotenv()
    DATABASE_URL = os.getenv('DATABASE_URL')
    if not DATABASE_URL:
        print("ERROR: La variable de entorno DATABASE_URL no está configurada.")
        return

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            # Verificar si la columna 'usuario_id' ya existe
            cur.execute("""
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='registros_auditoria' AND column_name='usuario_id';
            """)
            if not cur.fetchone():
                print("La columna 'usuario_id' no existe. Añadiéndola...")
                # Añade la columna con una referencia a la tabla de administradores
                cur.execute("""
                    ALTER TABLE registros_auditoria 
                    ADD COLUMN usuario_id INTEGER REFERENCES administradores(id);
                """)
                conn.commit()
                print("¡Columna 'usuario_id' añadida exitosamente!")
            else:
                print("La columna 'usuario_id' ya existe. No se necesita ninguna acción.")

    except psycopg2.Error as e:
        print(f"ERROR de base de datos: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()
            print("Conexión a la base de datos cerrada.")

if __name__ == '__main__':
    actualizar_tabla_auditoria()
