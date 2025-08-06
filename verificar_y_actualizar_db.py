import os
import psycopg2
from dotenv import load_dotenv

def verificar_y_actualizar_db_completo():
    """
    Script maestro que se conecta a la base de datos, verifica la existencia de todas
    las tablas y columnas necesarias para la versión más reciente de la aplicación,
    y las crea si no existen.

    Este script es seguro para ejecutarse múltiples veces.
    """
    load_dotenv()
    DATABASE_URL = os.getenv('DATABASE_URL')
    if not DATABASE_URL:
        print("❌ ERROR: La variable de entorno DATABASE_URL no está configurada.")
        return

    conn = None
    try:
        print("Conectando a la base de datos para la verificación integral del esquema...")
        conn = psycopg2.connect(DATABASE_URL)
        print("¡Conexión exitosa!")
        
        with conn.cursor() as cur:
            
            # --- 1. Definir todas las columnas requeridas por tabla ---
            columnas_requeridas = {
                "pagos": {
                    "puntualidad": "TEXT",
                    "fecha_creacion": "TIMESTAMPTZ",
                    "registrado_por_id": "INTEGER REFERENCES administradores(id)",
                    "reportado_por_cliente": "BOOLEAN DEFAULT FALSE",
                    "estado_reporte": "TEXT",
                    "revisado_por_id": "INTEGER REFERENCES administradores(id)",
                    "fecha_revision": "TIMESTAMPTZ",
                    "conciliado_por_id": "INTEGER REFERENCES administradores(id)"
                },
                "clientes": {
                    "meses_retraso_entrega": "INTEGER DEFAULT 0"
                },
                "caja_inscripciones": {
                    "comisiones_generadas": "BOOLEAN DEFAULT FALSE"
                }
            }

            # --- 2. Iterar y verificar/añadir cada columna ---
            for tabla, columnas in columnas_requeridas.items():
                print(f"\n--- Verificando tabla: '{tabla}' ---")
                for col, col_type in columnas.items():
                    cur.execute(f"""
                        SELECT 1 FROM information_schema.columns 
                        WHERE table_name='{tabla}' AND column_name='{col}';
                    """)
                    if not cur.fetchone():
                        print(f"  -> La columna '{col}' no existe. Añadiéndola...")
                        cur.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {col_type};")
                        print(f"     ¡Columna '{col}' añadida exitosamente!")
                    else:
                        print(f"  -> La columna '{col}' ya existe.")

            # --- 3. Verificar/Crear tablas adicionales ---
            print("\n--- Verificando tablas adicionales ---")
            print("  -> Verificando la tabla 'ofertas'...")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ofertas (
                    id SERIAL PRIMARY KEY,
                    cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
                    fecha_oferta DATE NOT NULL DEFAULT CURRENT_DATE,
                    cuotas_ofertadas INTEGER NOT NULL,
                    estado_oferta TEXT NOT NULL DEFAULT 'activa'
                );
            """)
            print("     Tabla 'ofertas' verificada/creada exitosamente.")

            conn.commit()
            print("\n\n✅ ¡ÉXITO! La base de datos ha sido verificada y actualizada a la última versión.")

    except psycopg2.Error as e:
        print(f"\n❌ ERROR de base de datos: {e}")
        if conn: conn.rollback()
    finally:
        if conn:
            conn.close()
            print("Conexión a la base de datos cerrada.")

if __name__ == '__main__':
    verificar_y_actualizar_db_completo()
