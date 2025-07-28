import os
import sys
import pandas as pd
import psycopg2
from tqdm import tqdm
from dotenv import load_dotenv

def migrar_datos():
    """
    Se conecta a la BD usando la variable de entorno y migra los datos
    desde un archivo CSV. Es seguro ejecutarlo múltiples veces gracias a
    la cláusula ON CONFLICT.
    """
    # --- 1. Configuración ---
    load_dotenv()
    DATABASE_URL = os.getenv('DATABASE_URL')
    if not DATABASE_URL:
        print("FATAL: La variable de entorno DATABASE_URL no está configurada en tu archivo .env")
        sys.exit(1)

    CSV_FILE = 'MI APP CLIENTES - MOTO PLAN.csv'
    
    # Verificar si el archivo CSV existe
    if not os.path.exists(CSV_FILE):
        print(f"FATAL: No se encontró el archivo '{CSV_FILE}'. Asegúrate de que esté en la misma carpeta.")
        sys.exit(1)

    # --- 2. Leer y preparar datos del CSV ---
    try:
        print(f"Leyendo datos desde '{CSV_FILE}'...")
        tipos_de_datos = {'cedula': str, 'contrato_nro': str, 'telefono': str}
        df = pd.read_csv(CSV_FILE, dtype=tipos_de_datos)
        
        print("Limpiando nombres de columnas...")
        df.columns = df.columns.str.strip().str.lower()
        
        print("Limpiando datos y espacios en blanco en todas las celdas...")
        for col in df.columns:
            if df[col].dtype == 'object':
                df[col] = df[col].str.strip()
        df.replace(r'^\s*$', pd.NA, regex=True, inplace=True)

        df.rename(columns={
            'n⁰ cedula': 'cedula', 'n⁰ contrato': 'contrato_nro',
            'nombre y apellido': 'nombre_apellido', 'numero de tlf': 'telefono',
            'fecha de ingreso': 'fecha_ingreso', 'plan': 'bien_solicitado',
            'moneda de pago': 'moneda_pago', 'valor de cuota': 'valor_cuota',
            'cuotas totales': 'cuotas_totales', 'cuotas pagas': 'cuotas_pagadas_progresivas',
            'estatus.1': 'estatus_1', 'inscripcion': 'inscripcion_monto'
        }, inplace=True)

        columnas_esperadas = [
            'cedula', 'contrato_nro', 'nombre_apellido', 'telefono', 'fecha_ingreso', 'grupo', 
            'bien_solicitado', 'moneda_pago', 'asesor', 'responsable', 'proceso', 'estatus', 
            'estatus_1', 'inscripcion_monto', 'valor_cuota', 'valor_cancelado', 'observacion', 
            'cuotas_totales', 'cuotas_pagadas_progresivas'
        ]
        for col in columnas_esperadas:
            if col not in df.columns:
                df[col] = None

        print("Convirtiendo formatos de fecha y números...")
        if 'fecha_ingreso' in df.columns:
            df['fecha_ingreso'] = pd.to_datetime(df['fecha_ingreso'], dayfirst=True, errors='coerce')
        columnas_numericas = ['valor_cuota', 'inscripcion_monto', 'valor_cancelado', 'cuotas_totales']
        for col in columnas_numericas:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        df = df.astype(object).where(pd.notna(df), None)
        
        print(f"Se encontraron {len(df)} filas para procesar.")

    except Exception as e:
        print(f"\nERROR al leer o procesar el CSV: {e}")
        sys.exit(1)

    # --- 3. Conectar a la BD y migrar ---
    conn = None
    try:
        print("\nConectando a la base de datos...")
        conn = psycopg2.connect(DATABASE_URL)
        print("¡Conexión exitosa!")
        
        with conn.cursor() as cursor:
            print("Iniciando inserción de datos. Esto puede tardar unos minutos...")
            registros_saltados = 0
            
            for index, row in tqdm(df.iterrows(), total=df.shape[0], desc="Insertando clientes"):
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
