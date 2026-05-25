import os
import psycopg2
from dotenv import load_dotenv

def reparar_tablas_egresos():
    load_dotenv()
    DATABASE_URL = os.getenv('DATABASE_URL')
    
    if not DATABASE_URL:
        print("❌ ERROR: No se encontró DATABASE_URL")
        return

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            print("⏳ Añadiendo 'created_at' a egresos_planificados...")
            cur.execute("ALTER TABLE egresos_planificados ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();")
            
            print("⏳ Añadiendo 'created_at' a egresos_ocurrencias...")
            cur.execute("ALTER TABLE egresos_ocurrencias ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();")
            
            conn.commit()
            print("✅ ¡Éxito! Las columnas fueron creadas.")
            
    except Exception as e:
        print(f"❌ Error en la base de datos: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    reparar_tablas_egresos()