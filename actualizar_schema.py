import os
import psycopg2
from dotenv import load_dotenv

def actualizar_base_de_datos():
    """
    Se conecta a la base de datos de Render y añade las columnas necesarias
    a las tablas si no existen. Este script es seguro para ejecutarse
    múltiples veces sin causar problemas ni pérdida de datos.
    """
    load_dotenv()
    DATABASE_URL = os.getenv('DATABASE_URL')
    if not DATABASE_URL:
        print("ERROR: Asegúrate de que tu archivo .env contiene la variable DATABASE_URL.")
        return

    conn = None
    try:
        print("Conectando a la base de datos en Render...")
        conn = psycopg2.connect(DATABASE_URL)
        print("¡Conexión exitosa!")
        
        with conn.cursor() as cur:
            # --- 1. Verificar y añadir 'inscripcion_pagada' a la tabla 'clientes' ---
            print("\nVerificando la tabla 'clientes'...")
            cur.execute("""
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='clientes' AND column_name='inscripcion_pagada';
            """)
            if not cur.fetchone():
                print("La columna 'inscripcion_pagada' no existe. Añadiéndola...")
                cur.execute("ALTER TABLE clientes ADD COLUMN inscripcion_pagada NUMERIC(12, 2) DEFAULT 0.0;")
                print("¡Columna 'inscripcion_pagada' añadida exitosamente!")
            else:
                print("La columna 'inscripcion_pagada' ya existe. No se necesita ninguna acción.")

            # --- 2. Verificar y añadir 'tipo_pago' a la tabla 'pagos' ---
            print("\nVerificando la tabla 'pagos'...")
            cur.execute("""
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='pagos' AND column_name='tipo_pago';
            """)
            if not cur.fetchone():
                print("La columna 'tipo_pago' no existe. Añadiéndola...")
                cur.execute("ALTER TABLE pagos ADD COLUMN tipo_pago TEXT;")
                # Asignamos un valor por defecto a los pagos existentes para que la columna pueda ser NOT NULL
                cur.execute("UPDATE pagos SET tipo_pago = 'Cuota' WHERE tipo_pago IS NULL;")
                cur.execute("ALTER TABLE pagos ALTER COLUMN tipo_pago SET NOT NULL;")
                print("¡Columna 'tipo_pago' añadida exitosamente!")
            else:
                print("La columna 'tipo_pago' ya existe. No se necesita ninguna acción.")

            conn.commit()
            print("\n¡La base de datos está actualizada!")

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
    actualizar_base_de_datos()
