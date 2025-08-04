import os
import psycopg2
from dotenv import load_dotenv

def actualizar_base_de_datos_v4():
    """
    Prepara la tabla 'pagos' para el nuevo flujo de verificación de pagos reportados por clientes.
    Añade las columnas:
    - reportado_por_cliente: BOOLEAN
    - estado_reporte: TEXT (e.g., 'Pendiente de Revision', 'Aprobado', 'Inconsistente')
    - revisado_por_id: INTEGER (FK a administradores)
    - fecha_revision: TIMESTAMPTZ
    Es seguro ejecutarlo múltiples veces.
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
            print("\nActualizando la tabla 'pagos' para el flujo de reportes de clientes...")
            
            # Columnas a añadir
            columnas = {
                "reportado_por_cliente": "BOOLEAN DEFAULT FALSE",
                "estado_reporte": "TEXT",
                "revisado_por_id": "INTEGER REFERENCES administradores(id)",
                "fecha_revision": "TIMESTAMPTZ"
            }

            for col, col_type in columnas.items():
                cur.execute(f"""
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='pagos' AND column_name='{col}';
                """)
                if not cur.fetchone():
                    print(f"Añadiendo columna '{col}'...")
                    cur.execute(f"ALTER TABLE pagos ADD COLUMN {col} {col_type};")
                    print(f"¡Columna '{col}' añadida!")
                else:
                    print(f"La columna '{col}' ya existe.")

            conn.commit()
            print("\n✅ ¡ÉXITO! La base de datos está actualizada para el nuevo flujo de verificación de pagos.")

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
    actualizar_base_de_datos_v4()
