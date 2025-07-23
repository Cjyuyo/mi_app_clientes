import psycopg2
import sys
from urllib.parse import urlparse

# --- CONFIGURACIÓN ---
DATABASE_URL = "postgresql://lientes_db_prod_user:FzmjghqgD9UPN3I3Ex3Q8KpLlgFDvUDI@dpg-d1vomdadbo4c73fnvsg-a.oregon-postgres.render.com/lientes_db_prod"

def actualizar_esquema():
    """
    Añade las nuevas columnas a la tabla 'pagos' de forma segura.
    """
    if "pega_aqui" in DATABASE_URL:
        print("Error: Debes editar este archivo y pegar tu External Database URL.")
        sys.exit(1)
        
    conn = None
    try:
        print("Conectando a la base de datos...")
        
        # --- CORRECCIÓN SSL (Método Alternativo y más robusto) ---
        # Se pasan los parámetros de conexión de forma explícita.
        result = urlparse(DATABASE_URL)
        conn = psycopg2.connect(
            dbname=result.path[1:],
            user=result.username,
            password=result.password,
            host=result.hostname,
            port=result.port,
            sslmode='require' # Forzar conexión segura
        )
        
        cursor = conn.cursor()
        print("Conexión exitosa. Aplicando cambios al esquema...")

        nuevas_columnas = {
            "pago_en": "TEXT",
            "cantidad_en_letras": "TEXT",
            "por_concepto_de": "TEXT",
            "referencia": "TEXT",
            "banco": "TEXT",
            "lugar_emision": "TEXT"
        }

        for columna, tipo in nuevas_columnas.items():
            try:
                cursor.execute(f"ALTER TABLE pagos ADD COLUMN {columna} {tipo};")
                print(f" - Columna '{columna}' añadida exitosamente.")
            except psycopg2.errors.DuplicateColumn:
                print(f" - Columna '{columna}' ya existe. Omitiendo.")
                conn.rollback()
        
        conn.commit()
        print("\n¡Esquema de la tabla 'pagos' actualizado exitosamente!")

    except Exception as e:
        print(f"\nOcurrió un error: {e}")
    finally:
        if conn:
            conn.close()
            print("Conexión cerrada.")

if __name__ == '__main__':
    actualizar_esquema()
