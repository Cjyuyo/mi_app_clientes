import subprocess
import sys
import os

def install_and_import(package):
    """Installs a package and then imports it."""
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])
    except subprocess.CalledProcessError as e:
        print(f"Failed to install {package}: {e}")
        # Exit if a critical package fails
        if package in ['pg8000', 'pandas', 'openpyxl']:
            sys.exit(1)
    
    __import__(package)

# --- 1. Install Dependencies ---
print("Installing dependencies...")
install_and_import('pg8000')
install_and_import('pandas')
install_and_import('openpyxl') # Required for reading .xlsx files

# Now import them for use in the script
import pandas as pd
import pg8000.dbapi
from urllib.parse import urlparse

# --- 2. Main Migration Logic ---
def run_migration():
    print("\nStarting migration process from Excel file...")
    try:
        # --- Get Database Credentials ---
        database_url = "postgresql://clientes_prod_86od_user:c9HerXPZBQRpjtmXNPmLqWoA8KQSFAye@dpg-d21foofgi27c73ds6oig-a.oregon-postgres.render.com/clientes_prod_86od"
        print("Using provided DATABASE_URL.")

        url = urlparse(database_url)
        port = url.port or 5432 # Use default port 5432 if not found
        
        conn_details = {
            "user": url.username,
            "password": url.password,
            "host": url.hostname,
            "port": port,
            "database": url.path[1:], # Remove leading '/'
            "ssl_context": True 
        }

        # --- Read Excel File ---
        excel_file_path = 'clientes_actualizado_final.xlsx'
        sheet1_name = 'CYK'
        sheet2_name = 'moto plan motors'
        
        print(f"Reading Excel file: {excel_file_path}")
        df_sheet1 = pd.read_excel(excel_file_path, sheet_name=sheet1_name)
        print(f"Read {len(df_sheet1)} rows from sheet '{sheet1_name}'.")
        
        df_sheet2 = pd.read_excel(excel_file_path, sheet_name=sheet2_name)
        print(f"Read {len(df_sheet2)} rows from sheet '{sheet2_name}'.")

        # --- Combine DataFrames ---
        df = pd.concat([df_sheet1, df_sheet2], ignore_index=True)
        df.dropna(subset=['N⁰ CEDULA'], inplace=True) # Clean empty rows
        print(f"Total combined rows to process: {len(df)}.")


        # --- Connect and Execute ---
        print(f"Connecting to host {conn_details['host']} on port {conn_details['port']}...")
        conn = pg8000.dbapi.connect(**conn_details)
        cursor = conn.cursor()
        print("Database connection successful.")

        # --- a. Recreate Table ---
        print("Recreating 'clientes' table...")
        create_table_sql = """
        DROP TABLE IF EXISTS clientes;
        CREATE TABLE clientes (
            id SERIAL PRIMARY KEY,
            cedula VARCHAR(255),
            nombre VARCHAR(255),
            apellido VARCHAR(255),
            grupo VARCHAR(255),
            plan VARCHAR(255),
            moneda_pago VARCHAR(255),
            asesor VARCHAR(255),
            responsable VARCHAR(255),
            numero_contrato VARCHAR(255),
            proceso VARCHAR(255),
            estatus VARCHAR(255),
            fecha_ingreso DATE,
            numero_telefono VARCHAR(255),
            porcentaje_inscripcion NUMERIC,
            inscripcion NUMERIC,
            cuotas_totales INTEGER,
            cuotas_pagas INTEGER,
            estatus_pago VARCHAR(255),
            pagos_impuntuales INTEGER,
            cuotas_mora INTEGER,
            observacion TEXT,
            valor_cuota NUMERIC,
            fecha_pago DATE,
            estatus_cuota VARCHAR(255),
            valor_cancelado NUMERIC
        );
        """
        cursor.execute(create_table_sql)
        print("Table 'clientes' created successfully.")

        # --- b. Insert Data ---
        print("Inserting data... This may take a moment.")
        
        # Normalize column names
        df.columns = [str(col).strip().lower() for col in df.columns]
        
        # Handle duplicated 'estatus' column
        cols = pd.Series(df.columns)
        estatus_indices = cols[cols == 'estatus'].index
        if len(estatus_indices) > 1:
            cols.iloc[estatus_indices[1]] = 'estatus_pago'
        df.columns = cols
        
        insert_query = """
        INSERT INTO clientes (
            cedula, nombre, apellido, grupo, plan, moneda_pago, asesor, responsable, 
            numero_contrato, proceso, estatus, fecha_ingreso, numero_telefono, 
            porcentaje_inscripcion, inscripcion, cuotas_totales, cuotas_pagas, 
            estatus_pago, pagos_impuntuales, cuotas_mora, observacion, 
            valor_cuota, fecha_pago, estatus_cuota, valor_cancelado
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        
        data_to_insert = []
        for _, row in df.iterrows():
            full_name = str(row.get('nombre y apellido', ''))
            name_parts = full_name.strip().split(' ', 1)
            nombre = name_parts[0]
            apellido = name_parts[1] if len(name_parts) > 1 else ''

            def to_date(date_str):
                try: return pd.to_datetime(date_str, dayfirst=True, errors='coerce').date() if pd.notna(date_str) else None
                except Exception: return None

            def to_numeric(val):
                try: return pd.to_numeric(val, errors='coerce') if pd.notna(val) else None
                except Exception: return None
            
            data_tuple = (
                str(row.get('n⁰ cedula', '')).split('.')[0], nombre, apellido,
                row.get('grupo'), row.get('plan'), row.get('moneda de pago'),
                row.get('asesor'), row.get('responsable'), row.get('n⁰ contrato'),
                row.get('proceso'), row.get('estatus'), to_date(row.get('fecha de ingreso')),
                row.get('numero de tlf'), to_numeric(row.get('% inscripcion')),
                to_numeric(row.get('inscripcion')), to_numeric(row.get('cuotas totales')),
                to_numeric(row.get('cuotas pagas')), row.get('estatus_pago'),
                to_numeric(row.get('pagos impuntuales')), to_numeric(row.get('cuotas en mora')),
                row.get('observación'), to_numeric(row.get('valor de cuota')),
                to_date(row.get('fecha de pago')), row.get('estatus cuota'),
                to_numeric(row.get('valor cancelado'))
            )
            data_to_insert.append(data_tuple)

        cursor.executemany(insert_query, data_to_insert)
        conn.commit()
        print(f"✅ ¡ÉXITO! Se insertaron {cursor.rowcount} registros de clientes.")

    except FileNotFoundError:
        print(f"FATAL ERROR: The file '{excel_file_path}' was not found. Please upload it and run again.")
    except Exception as e:
        print(f"AN ERROR OCCURRED: {e}")
        if 'conn' in locals() and conn:
            conn.rollback()
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()
            print("Database connection closed.")

if __name__ == "__main__":
    run_migration()