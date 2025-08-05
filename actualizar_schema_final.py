import os
import psycopg2
from dotenv import load_dotenv

def actualizar_base_de_datos_final():
    """
    Unifica las actualizaciones de esquema para la tabla 'pagos',
    asegurando que existan las columnas para el flujo de reportes y
    para registrar quién concilió cada pago.
    """
    load_dotenv()
    DATABASE_URL = os.getenv('DATABASE_URL')
    if not DATABASE_URL:
        print("ERROR: La variable de entorno DATABASE_URL no está configurada.")
        return

    conn = None
    try:
        print("Conectando a la base de datos para la actualización final del esquema...")
        conn = psycopg2.connect(DATABASE_URL)
        print("¡Conexión exitosa!")
        
        with conn.cursor() as cur:
            print("\nVerificando y añadiendo columnas a la tabla 'pagos'...")
            
            columnas_a_verificar = {
                "reportado_por_cliente": "BOOLEAN DEFAULT FALSE",
                "estado_reporte": "TEXT",
                "revisado_por_id": "INTEGER REFERENCES administradores(id)",
                "fecha_revision": "TIMESTAMPTZ",
                "conciliado_por_id": "INTEGER REFERENCES administradores(id)" # Nueva columna clave
            }

            for col, col_type in columnas_a_verificar.items():
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
            print("\n✅ ¡ÉXITO! La tabla 'pagos' ha sido actualizada correctamente.")

    except psycopg2.Error as e:
        print(f"\n❌ ERROR de base de datos: {e}")
        if conn: conn.rollback()
    finally:
        if conn:
            conn.close()
            print("Conexión a la base de datos cerrada.")

if __name__ == '__main__':
    actualizar_base_de_datos_final()