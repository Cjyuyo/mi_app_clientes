import os
import psycopg2
from dotenv import load_dotenv

def actualizar_base_de_datos():
    """
    Se conecta a la base de datos y añade las columnas y tablas necesarias.
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
            # --- 1. Añadir columna 'puntualidad' a la tabla 'pagos' ---
            print("\nVerificando la tabla 'pagos'...")
            cur.execute("""
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='pagos' AND column_name='puntualidad';
            """)
            if not cur.fetchone():
                print("La columna 'puntualidad' no existe. Añadiéndola...")
                cur.execute("ALTER TABLE pagos ADD COLUMN puntualidad TEXT;")
                print("¡Columna 'puntualidad' añadida exitosamente!")
            else:
                print("La columna 'puntualidad' ya existe.")

            # --- 2. Añadir columna 'meses_retraso_entrega' a la tabla 'clientes' ---
            print("\nVerificando la tabla 'clientes'...")
            cur.execute("""
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='clientes' AND column_name='meses_retraso_entrega';
            """)
            if not cur.fetchone():
                print("La columna 'meses_retraso_entrega' no existe. Añadiéndola...")
                cur.execute("ALTER TABLE clientes ADD COLUMN meses_retraso_entrega INTEGER DEFAULT 0;")
                print("¡Columna 'meses_retraso_entrega' añadida exitosamente!")
            else:
                print("La columna 'meses_retraso_entrega' ya existe.")

            # --- 3. Crear la tabla 'ofertas' si no existe ---
            print("\nVerificando la tabla 'ofertas'...")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ofertas (
                    id SERIAL PRIMARY KEY,
                    cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
                    fecha_oferta DATE NOT NULL DEFAULT CURRENT_DATE,
                    cuotas_ofertadas INTEGER NOT NULL,
                    estado_oferta TEXT NOT NULL DEFAULT 'activa' -- Ej: activa, ganadora, perdida
                );
            """)
            print("Tabla 'ofertas' verificada/creada exitosamente.")

            # --- 4. Añadir columna 'detalles' a la tabla 'registros_auditoria' ---
            print("\nVerificando la tabla 'registros_auditoria'...")
            cur.execute("""
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='registros_auditoria' AND column_name='detalles';
            """)
            if not cur.fetchone():
                print("La columna 'detalles' no existe. Añadiéndola...")
                cur.execute("ALTER TABLE registros_auditoria ADD COLUMN detalles JSONB;")
                print("¡Columna 'detalles' añadida exitosamente!")
            else:
                print("La columna 'detalles' ya existe.")


            conn.commit()
            print("\n¡La base de datos está actualizada y lista para las nuevas reglas de negocio!")

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
   