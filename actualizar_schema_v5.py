import os
import psycopg2
from dotenv import load_dotenv

def actualizar_base_de_datos_v5():
    """
    Añade la columna 'comisiones_generadas' a la tabla 'caja_inscripciones'
    para controlar cuándo se debe activar la generación de comisiones.
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
            print("\nVerificando columna 'comisiones_generadas' en 'caja_inscripciones'...")
            cur.execute("""
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='caja_inscripciones' AND column_name='comisiones_generadas';
            """)
            if not cur.fetchone():
                print("Añadiendo columna 'comisiones_generadas'...")
                cur.execute("ALTER TABLE caja_inscripciones ADD COLUMN comisiones_generadas BOOLEAN DEFAULT FALSE;")
                print("¡Columna añadida!")
            else:
                print("La columna 'comisiones_generadas' ya existe.")
            
            conn.commit()
            print("\n¡La base de datos está actualizada para la nueva lógica de comisiones!")

    except psycopg2.Error as e:
        print(f"\nERROR de base de datos: {e}")
        if conn: conn.rollback()
    finally:
        if conn:
            conn.close()
            print("Conexión a la base de datos cerrada.")

if __name__ == '__main__':
    actualizar_base_de_datos_v5()