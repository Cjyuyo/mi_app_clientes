import os
import psycopg2
from dotenv import load_dotenv

def crear_tablas_modulo_comercial():
    """
    Crea las tablas necesarias para el nuevo módulo comercial si no existen.
    - caja_inscripciones: Para llevar un registro separado de los ingresos por inscripción.
    - comisiones_generadas: Para registrar las comisiones de la nómina.
    Es seguro ejecutar este script múltiples veces.
    """
    load_dotenv()
    DATABASE_URL = os.getenv('DATABASE_URL')
    if not DATABASE_URL:
        print("ERROR: La variable de entorno DATABASE_URL no está configurada.")
        return

    conn = None
    try:
        print("Conectando a la base de datos para crear tablas del módulo comercial...")
        conn = psycopg2.connect(DATABASE_URL)
        print("¡Conexión exitosa!")
        
        with conn.cursor() as cur:
            # --- 1. Crear la tabla 'caja_inscripciones' ---
            print("\nVerificando/Creando la tabla 'caja_inscripciones'...")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS caja_inscripciones (
                    id SERIAL PRIMARY KEY,
                    contrato_nro TEXT,
                    cliente_id INTEGER REFERENCES clientes(id) ON DELETE SET NULL,
                    monto_inscripcion NUMERIC(12, 2) NOT NULL,
                    responsable_cierre TEXT,
                    fecha_registro TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            print("Tabla 'caja_inscripciones' lista.")

            # --- 2. Crear la tabla 'comisiones_generadas' ---
            print("\nVerificando/Creando la tabla 'comisiones_generadas'...")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS comisiones_generadas (
                    id SERIAL PRIMARY KEY,
                    contrato_nro TEXT,
                    cliente_id INTEGER REFERENCES clientes(id) ON DELETE SET NULL,
                    nombre_beneficiario TEXT NOT NULL,
                    monto_comision NUMERIC(12, 2) NOT NULL,
                    concepto TEXT,
                    fecha_generacion DATE DEFAULT CURRENT_DATE,
                    estado_nomina TEXT DEFAULT 'Pendiente'
                );
            """)
            print("Tabla 'comisiones_generadas' lista.")

            conn.commit()
            print("\n✅ ¡ÉXITO! Las tablas para el módulo comercial se han verificado/creado correctamente.")

    except psycopg2.Error as e:
        print(f"\n❌ ERROR de base de datos: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()
            print("Conexión a la base de datos cerrada.")

if __name__ == '__main__':
    crear_tablas_modulo_comercial()
