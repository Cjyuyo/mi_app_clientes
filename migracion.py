import pandas as pd
import psycopg2
import sys

# --- CONFIGURACIÓN ---
DATABASE_URL = "postgresql://lientes_db_prod_user:FzmjghqgD9UPN3I3Ex3Q8KpLlgFDvUDI@dpg-d1vomdadbo4c73fnv9sg-a.oregon-postgres.render.com/lientes_db_prod"
CSV_FILE = 'MI APP CLIENTES - MOTO PLAN.csv'
# CORRECCIÓN: Ahora busca el archivo llamado 'schema' sin la extensión .sql
SCHEMA_FILE = 'schema' 

def crear_tablas_si_no_existen(conn):
    print("Verificando y creando tablas si es necesario...")
    try:
        with open(SCHEMA_FILE, 'r') as f:
            schema_sql = f.read()
        cursor = conn.cursor()
        cursor.execute(schema_sql)
        conn.commit()
        cursor.close()
        print("Tablas creadas o verificadas exitosamente.")
    except Exception as e:
        print(f"Ocurrió un error al crear las tablas: {e}")
        raise

def migrar_datos():
    if "postgresql://" not in DATABASE_URL:
        print("Error: La variable DATABASE_URL no parece correcta.")
        sys.exit(1)

    try:
        print(f"Leyendo datos desde {CSV_FILE}...")
        tipos_de_datos = {'N⁰ CEDULA': str, 'N⁰ CONTRATO': str, 'NUMERO DE TLF': str}
        df = pd.read_csv(CSV_FILE, dtype=tipos_de_datos)
        df.columns = [str(col).strip().lower() for col in df.columns]
        
        print("Limpiando datos...")
        columnas_numericas = [
            '% inscripcion', 'inscripcion', 'cuotas pagas', 'pagos impuntuales', 
            'cuotas en mora', 'valor de cuota', 'valor cancelado', 'cuotas totales'
        ]
        for col in columnas_numericas:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        df = df.where(pd.notna(df), None)
        print(f"Se encontraron {len(df)} filas para migrar.")

    except Exception as e:
        print(f"Ocurrió un error leyendo o limpiando el archivo CSV: {e}")
        return

    conn = None
    try:
        print("Conectando a la base de datos...")
        conn = psycopg2.connect(DATABASE_URL)
        print("Conexión exitosa.")
        crear_tablas_si_no_existen(conn)
        
        cursor = conn.cursor()
        print("Iniciando inserción de datos...")
        for index, row in df.iterrows():
            query = """
            INSERT INTO clientes (
                cedula, contrato_nro, nombre_apellido, telefono, fecha_ingreso, grupo, bien_solicitado,
                moneda_pago, asesor, responsable, proceso, estatus, estatus_1,
                inscripcion_porcentaje, inscripcion_monto, valor_cuota, fecha_pago_recurrente, 
                estatus_cuota, valor_cancelado, observacion,
                cuotas_totales, cuotas_pagadas_progresivas, pagos_impuntuales, cuotas_en_mora
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (cedula) DO UPDATE SET
                contrato_nro = EXCLUDED.contrato_nro,
                nombre_apellido = EXCLUDED.nombre_apellido;
            """
            
            values = (
                row.get('n⁰ cedula'), row.get('n⁰ contrato'), row.get('nombre y apellido'), row.get('numero de tlf'), row.get('fecha de ingreso'),
                row.get('grupo'), row.get('plan'), # Mapea la columna 'plan' del CSV a 'bien_solicitado'
                row.get('moneda de pago'), row.get('asesor'), row.get('responsable'),
                row.get('proceso'), row.get('estatus'), row.get('estatus.1'),
                row.get('% inscripcion'), row.get('inscripcion'), row.get('valor de cuota'), row.get('fecha de pago'), 
                row.get('estatus cuota'), row.get('valor cancelado'), row.get('observación'),
                row.get('cuotas totales'), row.get('cuotas pagas'), 
                row.get('pagos impuntuales'), row.get('cuotas en mora')
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
