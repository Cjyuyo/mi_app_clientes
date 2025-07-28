import os
import psycopg2
from dotenv import load_dotenv

def actualizar_base_de_datos_v2():
    """
    Se conecta a la base de datos y añade la tabla 'adjudicaciones'
    y la tabla 'historial_adjudicaciones'.
    Es seguro para ejecutarse múltiples veces.
    """
    load_dotenv()
    DATABASE_URL = os.getenv('DATABASE_URL')
    if not DATABASE_URL:
        print("ERROR: La variable de entorno DATABASE_URL no está configurada.")
        return

    conn = None
    try:
        print("Conectando a la base de datos...")
        conn = psycopg2.connect(DATABASE_URL)
        print("¡Conexión exitosa!")
        
        with conn.cursor() as cur:
            # --- 1. Crear la tabla 'adjudicaciones' si no existe ---
            print("\nVerificando la tabla 'adjudicaciones'...")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS adjudicaciones (
                    id SERIAL PRIMARY KEY,
                    fecha_adjudicacion DATE NOT NULL DEFAULT CURRENT_DATE,
                    ganador_sorteo_id INTEGER REFERENCES clientes(id),
                    ganador_oferta_id INTEGER REFERENCES clientes(id),
                    participantes_sorteo_ids INTEGER[],
                    participantes_oferta_ids INTEGER[]
                );
            """)
            print("Tabla 'adjudicaciones' verificada/creada exitosamente.")

            conn.commit()
            print("\n¡La base de datos está actualizada para el sistema de adjudicación!")

    except psycopg2.Error as e:
        print(f"\nERROR de base de datos: {e}")
        if conn: conn.rollback()
    except Exception as e:
        print(f"\nERROR inesperado: {e}")
        if conn: conn.rollback()
    finally:
        if conn:
            conn.close()
            print("Conexión a la base de datos cerrada.")

if __name__ == '__main__':
    actualizar_base_de_datos_v2()
