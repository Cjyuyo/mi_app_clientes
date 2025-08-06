import os
import psycopg2
from dotenv import load_dotenv

def agregar_columna_fecha_creacion():
    """
    Añade la columna 'fecha_creacion' a la tabla 'pagos' si no existe.
    Esta columna es necesaria para el nuevo flujo de conciliación y registro de pagos.
    """
    load_dotenv()
    DATABASE_URL = os.getenv('DATABASE_URL')
    if not DATABASE_URL:
        print("❌ ERROR: La variable de entorno DATABASE_URL no está configurada.")
        return

    conn = None
    try:
        print("Conectando a la base de datos para actualizar el esquema de 'pagos'...")
        conn = psycopg2.connect(DATABASE_URL)
        print("¡Conexión exitosa!")
        
        with conn.cursor() as cur:
            print("\nVerificando la existencia de la columna 'fecha_creacion' en la tabla 'pagos'...")
            
            cur.execute("""
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='pagos' AND column_name='fecha_creacion';
            """)
            
            if not cur.fetchone():
                print("La columna 'fecha_creacion' no existe. Añadiéndola ahora...")
                # Usamos TIMESTAMPTZ para guardar la fecha y hora con zona horaria.
                cur.execute("ALTER TABLE pagos ADD COLUMN fecha_creacion TIMESTAMPTZ;")
                print("¡Columna 'fecha_creacion' añadida exitosamente!")
            else:
                print("La columna 'fecha_creacion' ya existe. No se necesita ninguna acción.")

            conn.commit()
            print("\n✅ ¡ÉXITO! La tabla 'pagos' ha sido actualizada correctamente.")

    except psycopg2.Error as e:
        print(f"\n❌ ERROR de base de datos: {e}")
        if conn: conn.rollback()
    finally:
        if conn:
            conn.close()
            print("Conexión a la base de datos cerrada.")

if __name__ == '__main__':
    agregar_columna_fecha_creacion()
