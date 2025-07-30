import os
import psycopg2
from dotenv import load_dotenv

def actualizar_foreign_key():
    """
    Modifica la foreign key en 'registros_auditoria' para permitir
    el borrado de clientes usando ON DELETE SET NULL.
    """
    load_dotenv()
    DATABASE_URL = os.getenv('DATABASE_URL')
    if not DATABASE_URL:
        print("ERROR: La variable de entorno DATABASE_URL no está configurada.")
        return

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            constraint_name = "registros_auditoria_cliente_afectado_id_fkey"
            
            print(f"Intentando modificar la restricción: '{constraint_name}'...")

            print("Paso 1: Eliminando la restricción existente...")
            cur.execute(f"""
                ALTER TABLE registros_auditoria
                DROP CONSTRAINT IF EXISTS {constraint_name};
            """)
            print("¡Restricción anterior eliminada (si existía)!")

            print("Paso 2: Añadiendo la nueva restricción con ON DELETE SET NULL...")
            cur.execute("""
                ALTER TABLE registros_auditoria
                ADD CONSTRAINT registros_auditoria_cliente_afectado_id_fkey
                FOREIGN KEY (cliente_afectado_id)
                REFERENCES clientes(id)
                ON DELETE SET NULL;
            """)
            
            conn.commit()
            print("\n¡ÉXITO! La base de datos ha sido actualizada.")
            print("Ahora puedes borrar clientes y el historial de auditoría se conservará.")

    except psycopg2.Error as e:
        print(f"\nERROR de base de datos: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()
            print("\nConexion a la base de datos cerrada.")

if __name__ == '__main__':
    actualizar_foreign_key()