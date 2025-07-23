import pandas as pd
import psycopg2
import sys

# --- CONFIGURACIÓN ---
DATABASE_URL = "postgresql://lientes_db_prod_user:FzmjghqgD9UPN3I3Ex3Q8KpLlgFDvUDI@dpg-d1vomdadbo4c73fnv9sg-a.oregon-postgres.render.com/lientes_db_prod"
CSV_FILE = 'MI APP CLIENTES - MOTO PLAN.csv'
SCHEMA_FILE = 'schema'

def crear_tablas_si_no_existen(conn):
    """
    Lee el archivo schema y ejecuta los comandos para crear las tablas.
    """
    print("Verificando y creando tablas si es necesario...")
    try:
        with open(SCHEMA_FILE, 'r') as f:
            schema_sql = f.read()
        
        cursor = conn.cursor()
        cursor.execute(schema_sql)
        conn.commit()
        cursor.close()
        print("Tablas creadas o verificadas exitosamente.")
    except FileNotFoundError:
        print(f"Error: No se encontró el archivo '{SCHEMA_FILE}'. Asegúrate de que se llame exactamente así.")
        raise
    except Exception as e:
        print(f"Ocurrió un error al crear las tablas: {e}")
        raise

def migrar_datos():
    """
    Lee datos de un archivo CSV, los limpia y los inserta en la base de datos.
    """
    if "postgresql://" not in DATABASE_URL:
        print("Error: La variable DATABASE_URL no parece correcta. Por favor, revísala.")
        sys.exit(1)

    try:
        print(f"Leyendo datos desde {CSV_FILE}...")
        tipos_de_datos = {'N⁰ CEDULA': str, 'N⁰ CONTRATO': str, 'NUMERO DE TLF': str}
        df = pd.read_csv(CSV_FILE, dtype=tipos_de_datos)
        df.columns = [str(col).strip().lower() for col in df.columns]
        
        print("Limpiando datos numéricos...")
        columnas_numericas = ['% inscripcion', 'inscripcion', 'cuotas pagas', 'pagos impuntuales', 
                              'cuotas en mora', 'valor de cuota', 'valor cancelado']
        for col in columnas_numericas:
            if col in df.columns:
                df[col] = df[col].astype(str).str.replace(r'[^\d.]', '', regex=True)
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        df = df.where(pd.notna(df), None)
        print(f"Se encontraron {len(df)} filas para migrar.")

    except Exception as e:
        print(f"Ocurrió un error leyendo o limpiando el archivo CSV: {e}")
        return

    conn = None
    try:
        print("Conectando a la base de datos de PostgreSQL en Render...")
        conn = psycopg2.connect(DATABASE_URL)
        print("Conexión exitosa.")

        crear_tablas_si_no_existen(conn)
        
        cursor = conn.cursor()
        print("Iniciando inserción de datos...")
        for index, row in df.iterrows():
            # CORRECCIÓN: La consulta ahora usa las nuevas columnas del schema
            query = """
            INSERT INTO clientes (
                cedula, contrato_nro, nombre_apellido, telefono, fecha_ingreso, grupo, plan,
                moneda_pago, asesor, responsable, proceso, estatus, estatus_1,
                inscripcion_porcentaje, inscripcion_monto, valor_cuota, fecha_pago_recurrente, 
                estatus_cuota, valor_cancelado, observacion, fecha_inscripcion, plan_contratado, 
                duracion_plan, reserva_monto_total, reserva_monto_pagado,
                cuotas_totales, cuotas_pagadas_progresivas, cuotas_pagadas_regresivas, balance_regresivo
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (cedula) DO NOTHING;
            """
            
            # CORRECCIÓN: Los valores ahora coinciden con la nueva consulta
            values = (
                row.get('n⁰ cedula'), row.get('n⁰ contrato'), row.get('nombre y apellido'), row.get('numero de tlf'), row.get('fecha de ingreso'),
                row.get('grupo'), row.get('plan'), row.get('moneda de pago'), row.get('asesor'), row.get('responsable'),
                row.get('proceso'), row.get('estatus'), row.get('estatus.1'), row.get('% inscripcion'),
                row.get('inscripcion'), row.get('valor de cuota'), row.get('fecha de pago'), 
                row.get('estatus cuota'), row.get('valor cancelado'), row.get('observación'),
                row.get('fecha de inscripcion'), row.get('plan contratado'), row.get('duracion del plan'),
                row.get('reserva_monto_total'), row.get('reserva_monto_pagado'),
                0, # cuotas_totales (default)
                row.get('cuotas pagas'), # cuotas_pagadas_progresivas
                0, # cuotas_pagadas_regresivas (default)
                0.0 # balance_regresivo (default)
            )
            
            cursor.execute(query, values)
            if (index + 1) % 50 == 0:
                print(f"Procesando fila {index + 1}/{len(df)}...")

        conn.commit()
        print(f"\n¡Migración completada exitosamente! Se procesaron todas las {len(df)} filas.")

    except Exception as e:
        print(f"\nOcurrió un error durante la migración: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()
            print("Conexión cerrada.")

if __name__ == '__main__':
    migrar_datos()
