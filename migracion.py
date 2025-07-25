import os
import sys
import pandas as pd
import psycopg2
from tqdm import tqdm # Importar la librería para la barra de progreso

def migrar_datos():
    """
    Se conecta a la BD de Render usando una URL externa y migra los datos
    desde un archivo CSV. Este script está diseñado para ejecutarse una sola vez
    desde tu computadora local.
    """
    # --- 1. Configuración ---
    # URL Externa de la nueva base de datos de Render.
    DATABASE_URL = "postgresql://clientes_prod_86od_user:c9HerXPZBQRpjtmXNPmLqWoA8KQSFAye@dpg-d21foofgi27c73ds6oig-a.oregon-postgres.render.com/clientes_prod_86od"
    CSV_FILE = 'MI APP CLIENTES - MOTO PLAN.csv'
    SCHEMA_FILE = 'schema' # El archivo con la estructura de la BD

    # --- 2. Leer y preparar datos del CSV ---
    try:
        print(f"Leyendo datos desde '{CSV_FILE}'...")
        tipos_de_datos = {'cedula': str, 'contrato_nro': str, 'telefono': str}
        df = pd.read_csv(CSV_FILE, dtype=tipos_de_datos)
        
        # Limpiar nombres de columnas
        df.columns = df.columns.str.strip().str.lower()
        
        # Reemplazar celdas que solo contienen espacios en blanco por un valor nulo (NaN).
        print("Limpiando datos y espacios en blanco...")
        df.replace(r'^\s*$', pd.NA, regex=True, inplace=True)

        # Renombrar columnas del CSV para que coincidan con la base de datos
        df.rename(columns={
            'n⁰ cedula': 'cedula', 'n⁰ contrato': 'contrato_nro',
            'nombre y apellido': 'nombre_apellido', 'numero de tlf': 'telefono',
            'fecha de ingreso': 'fecha_ingreso', 'plan': 'bien_solicitado',
            'moneda de pago': 'moneda_pago', 'valor de cuota': 'valor_cuota',
            'cuotas totales': 'cuotas_totales', 'cuotas pagas': 'cuotas_pagadas_progresivas',
            'estatus.1': 'estatus_1', 'inscripcion': 'inscripcion_monto'
        }, inplace=True)

        # Asegurarse de que todas las columnas esperadas existan
        columnas_esperadas = [
            'cedula', 'contrato_nro', 'nombre_apellido', 'telefono', 'fecha_ingreso', 'grupo', 
            'bien_solicitado', 'moneda_pago', 'asesor', 'responsable', 'proceso', 'estatus', 
            'estatus_1', 'inscripcion_monto', 'valor_cuota', 'valor_cancelado', 'observacion', 
            'cuotas_totales', 'cuotas_pagadas_progresivas'
        ]
        for col in columnas_esperadas:
            if col not in df.columns:
                df[col] = None

        # Convertir fechas y números, manejando errores
        print("Convirtiendo formatos de fecha y números...")
        if 'fecha_ingreso' in df.columns:
            df['fecha_ingreso'] = pd.to_datetime(df['fecha_ingreso'], dayfirst=True, errors='coerce')
        columnas_numericas = ['valor_cuota', 'inscripcion_monto', 'valor_cancelado', 'cuotas_totales']
        for col in columnas_numericas:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # Limpieza final: convertir todos los valores inválidos (NaN, NaT) a None
        df = df.astype(object).where(pd.notna(df), None)
        
        print(f"Se encontraron {len(df)} filas para migrar.")

    except Exception as e:
        print(f"\nERROR al leer o procesar el CSV: {e}")
        sys.exit(1)

    # --- 3. Conectar a la BD y migrar ---
    conn = None
    try:
        print("\nConectando a la base de datos en Render...")
        conn = psycopg2.connect(DATABASE_URL)
        print("¡Conexión exitosa!")
        
        with conn.cursor() as cursor:
            print("Aplicando el schema (creando tablas)...")
            script_dir = os.path.dirname(os.path.abspath(__file__))
            schema_path = os.path.join(script_dir, SCHEMA_FILE)
            with open(schema_path, 'r') as f:
                schema_sql = f.read()
            cursor.execute(schema_sql)
            print("Tablas creadas/verificadas.")

            print("Iniciando inserción de datos...")
            registros_saltados = 0
            
            # Usar tqdm para mostrar una barra de progreso
            for index, row in tqdm(df.iterrows(), total=df.shape[0], desc="Insertando clientes"):
                # Si la cédula está vacía, saltar esta fila y continuar con la siguiente.
                if not row.get('cedula'):
                    registros_saltados += 1
                    continue

                query = """
                INSERT INTO clientes (
                    cedula, contrato_nro, nombre_apellido, telefono, fecha_ingreso, grupo, bien_solicitado,
                    moneda_pago, asesor, responsable, proceso, estatus, estatus_1,
                    inscripcion_monto, valor_cuota, valor_cancelado, observacion,
                    cuotas_totales, cuotas_pagadas_progresivas
                ) VALUES (
                    %(cedula)s, %(contrato_nro)s, %(nombre_apellido)s, %(telefono)s, %(fecha_ingreso)s, %(grupo)s, %(bien_solicitado)s,
                    %(moneda_pago)s, %(asesor)s, %(responsable)s, %(proceso)s, %(estatus)s, %(estatus_1)s,
                    %(inscripcion_monto)s, %(valor_cuota)s, %(valor_cancelado)s, %(observacion)s,
                    %(cuotas_totales)s, %(cuotas_pagadas_progresivas)s
                )
                ON CONFLICT (cedula) DO UPDATE SET
                    contrato_nro = EXCLUDED.contrato_nro, nombre_apellido = EXCLUDED.nombre_apellido,
                    telefono = EXCLUDED.telefono, valor_cuota = EXCLUDED.valor_cuota, estatus = EXCLUDED.estatus;
                """
                cursor.execute(query, row.to_dict())
            conn.commit()
            print(f"\n¡Migración completada! Se procesaron {len(df) - registros_saltados} filas.")
            if registros_saltados > 0:
                print(f"Se saltaron {registros_saltados} registros por no tener cédula.")

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
    migrar_datos()
