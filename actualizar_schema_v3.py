import os
import psycopg2
from dotenv import load_dotenv

def actualizar_base_de_datos_v3():
    """
    Añade las columnas necesarias para las nuevas reglas de adjudicación por ahorro
    y los desempates por oferta.
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
            # --- 1. Añadir columna 'ignorar_penalidad_puntualidad' a 'clientes' ---
            print("\nVerificando columna 'ignorar_penalidad_puntualidad' en 'clientes'...")
            cur.execute("""
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='clientes' AND column_name='ignorar_penalidad_puntualidad';
            """)
            if not cur.fetchone():
                print("Añadiendo 'ignorar_penalidad_puntualidad'...")
                cur.execute("ALTER TABLE clientes ADD COLUMN ignorar_penalidad_puntualidad BOOLEAN DEFAULT FALSE;")
                print("¡Columna añadida!")
            else:
                print("La columna 'ignorar_penalidad_puntualidad' ya existe.")

            # --- 2. Añadir columna 'ganadores_ahorro_ids' a 'adjudicaciones' ---
            print("\nVerificando columna 'ganadores_ahorro_ids' en 'adjudicaciones'...")
            cur.execute("""
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='adjudicaciones' AND column_name='ganadores_ahorro_ids';
            """)
            if not cur.fetchone():
                print("Añadiendo 'ganadores_ahorro_ids'...")
                # Esta columna almacenará un array de IDs de los ganadores por ahorro.
                cur.execute("ALTER TABLE adjudicaciones ADD COLUMN ganadores_ahorro_ids INTEGER[];")
                print("¡Columna añadida!")
            else:
                print("La columna 'ganadores_ahorro_ids' ya existe.")
            
            conn.commit()
            print("\n¡La base de datos está actualizada para las nuevas reglas de adjudicación!")

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
    actualizar_base_de_datos_v3()
