import subprocess
import sys
import os
from urllib.parse import urlparse

def install_and_import(package):
    """Installs a package."""
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])
    except Exception as e:
        print(f"Failed to install {package}: {e}")
        sys.exit(1)

def run_migration():
    """Main function to run the data migration."""
    # Move imports inside the function to ensure they run after installation
    import pandas as pd
    import pg8000.dbapi

    print("\nStarting migration process from Excel file...")
    try:
        database_url = "postgresql://clientes_prod_86od_user:c9HerXPZBQRpjtmXNPmLqWoA8KQSFAye@dpg-d21foofgi27c73ds6oig-a.oregon-postgres.render.com/clientes_prod_86od"
        print("Using provided DATABASE_URL.")
        
        url = urlparse(database_url)
        port = url.port or 5432
        conn_details = {"user": url.username, "password": url.password, "host": url.hostname, "port": port, "database": url.path[1:], "ssl_context": True}

        excel_file_path = 'clientes_actualizado_final.xlsx'
        print(f"Reading Excel file: {excel_file_path}")
        df_sheet1 = pd.read_excel(excel_file_path, sheet_name='CYK')
        df_sheet2 = pd.read_excel(excel_file_path, sheet_name='Moto Plan Motors')
        df = pd.concat([df_sheet1, df_sheet2], ignore_index=True)
        df.dropna(subset=['N⁰ CEDULA'], inplace=True, how='all')
        print(f"Total combined rows to process: {len(df)}.")

        print("Codificando la columna 'grupo'...")
        df['GRUPO'] = df['GRUPO'].astype(str)
        unique_groups = df['GRUPO'].unique()
        group_mapping = {group: f"MP-{i+1}" for i, group in enumerate(unique_groups)}
        df['GRUPO'] = df['GRUPO'].map(group_mapping)
        print("Columna 'grupo' codificada.")

        conn = pg8000.dbapi.connect(**conn_details)
        cursor = conn.cursor()
        print("Database connection successful.")
        
        print("Recreating 'clientes' table...")
        create_table_sql = """
        DROP TABLE IF EXISTS clientes CASCADE;
        CREATE TABLE clientes (
            id SERIAL PRIMARY KEY, cedula VARCHAR(255), nombre VARCHAR(255), apellido VARCHAR(255),
            grupo VARCHAR(255), plan_contratado VARCHAR(255), moneda_pago VARCHAR(255), asesor VARCHAR(255),
            responsable VARCHAR(255), contrato_nro VARCHAR(255), proceso VARCHAR(255), estatus VARCHAR(255),
            fecha_ingreso DATE, telefono VARCHAR(255), porcentaje_inscripcion NUMERIC, inscripcion_monto NUMERIC,
            cuotas_totales INTEGER, cuotas_pagas INTEGER, estatus_pago VARCHAR(255), pagos_impuntuales INTEGER,
            cuotas_mora INTEGER, observacion TEXT, valor_cuota NUMERIC, fecha_pago DATE,
            estatus_cuota VARCHAR(255), valor_cancelado NUMERIC, inscripcion_pagada NUMERIC(12, 2) DEFAULT 0.00,
            cuotas_pagadas_progresivas INTEGER DEFAULT 0, cuotas_pagadas_regresivas INTEGER DEFAULT 0,
            balance_regresivo NUMERIC(12, 2) DEFAULT 0.00, meses_retraso_entrega INTEGER DEFAULT 0,
            ignorar_penalidad_puntualidad BOOLEAN DEFAULT FALSE
        );
        """
        cursor.execute(create_table_sql)
        print("Table 'clientes' created successfully.")

        print("Inserting data with business logic...")
        df.columns = [str(col).strip().lower() for col in df.columns]
        
        insert_query = """
        INSERT INTO clientes (
            cedula, nombre, apellido, grupo, plan_contratado,
            contrato_nro, proceso, estatus, fecha_ingreso, telefono, 
            inscripcion_monto, cuotas_totales, cuotas_pagas, valor_cuota,
            inscripcion_pagada, cuotas_pagadas_progresivas
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        
        data_to_insert = []
        
        def to_date(date_val):
            if pd.isna(date_val): return None
            dt = pd.to_datetime(date_val, errors='coerce')
            return None if pd.isna(dt) else dt.date()

        def to_numeric(val):
            if pd.isna(val): return None
            try: return pd.to_numeric(val)
            except (ValueError, TypeError): return None

        def to_integer(val):
            if pd.isna(val): return None
            try: return int(pd.to_numeric(val))
            except (ValueError, TypeError): return None

        for _, row in df.iterrows():
            full_name = str(row.get('nombre y apellido', ''))
            name_parts = full_name.strip().split(' ', 1)
            nombre = name_parts[0]
            apellido = name_parts[1] if len(name_parts) > 1 else ''
            
            proceso = str(row.get('proceso', '')).upper() if pd.notna(row.get('proceso')) else None
            inscripcion_monto = to_numeric(row.get('inscripcion'))
            cuotas_pagas = to_integer(row.get('cuotas pagas'))
            inscripcion_pagada = inscripcion_monto if proceso != 'RESERVA' else 0
            
            data_tuple = (
                str(row.get('n⁰ cedula', '')).split('.')[0], nombre, apellido, row.get('grupo'), row.get('plan'),
                str(row.get('n⁰ contrato')), proceso, row.get('estatus'), to_date(row.get('fecha de ingreso')),
                str(row.get('numero de tlf')), inscripcion_monto, to_integer(row.get('cuotas totales')),
                cuotas_pagas, to_numeric(row.get('valor de cuota')),
                inscripcion_pagada,
                cuotas_pagas
            )
            data_to_insert.append(data_tuple)

        cursor.executemany(insert_query, data_to_insert)
        conn.commit()
        print(f"✅ ¡ÉXITO! Se insertaron {cursor.rowcount} registros de clientes.")

    except Exception as e:
        print(f"AN ERROR OCCURRED: {e}")
        if 'conn' in locals(): conn.rollback()
    finally:
        if 'cursor' in locals(): cursor.close()
        if 'conn' in locals(): conn.close()
        print("Database connection closed.")

# --- Punto de Entrada ---
if __name__ == "__main__":
    print("Installing dependencies...")
    install_and_import('pg8000')
    install_and_import('pandas')
    install_and_import('openpyxl')
    
    # Run the main migration function
    run_migration()