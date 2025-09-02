import pandas as pd
import pg8000.dbapi
from urllib.parse import urlparse
import os
from dotenv import load_dotenv

load_dotenv()

def to_date(date_str):
    try:
        return pd.to_datetime(date_str, dayfirst=True, errors='coerce').date() if pd.notna(date_str) else None
    except Exception:
        return None

def to_numeric(val):
    try:
        return pd.to_numeric(val, errors='coerce') if pd.notna(val) else None
    except Exception:
        return None

# --- Conexión a la Base de Datos ---
DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    raise ValueError("No se encontró la variable de entorno DATABASE_URL")

url = urlparse(DATABASE_URL)
conn_details = {
    "user": url.username,
    "password": url.password,
    "host": url.hostname,
    "port": url.port,
    "database": url.path[1:]
}

conn = pg8000.dbapi.connect(**conn_details)
cursor = conn.cursor()

# --- Creación de la Tabla ---
create_table_sql = """
DROP TABLE IF EXISTS clientes;
CREATE TABLE clientes (
    id SERIAL PRIMARY KEY,
    cedula VARCHAR(255) UNIQUE,
    nombre VARCHAR(255),
    apellido VARCHAR(255),
    grupo VARCHAR(255),
    plan VARCHAR(255),
    moneda_pago VARCHAR(255),
    asesor VARCHAR(255),
    responsable VARCHAR(255),
    numero_contrato VARCHAR(255) UNIQUE,
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
print("Tabla 'clientes' creada exitosamente.")

# --- Lectura y Preparación de Datos ---
df = pd.read_excel('SISTEMA INTEGRAL.xlsx') # Asegúrate que el nombre del archivo sea correcto
df.dropna(subset=['N⁰ CEDULA'], inplace=True)

# Normaliza los nombres de las columnas para el procesamiento interno
df.columns = [str(col).strip().upper() for col in df.columns]

# --- Inserción de Datos ---
data_to_insert = []
for _, row in df.iterrows():
    full_name = str(row.get('NOMBRE Y APELLIDO', ''))
    name_parts = full_name.strip().split(' ', 1)
    
    data_tuple = (
        str(row.get('N⁰ CEDULA', '')).split('.')[0],
        name_parts[0],
        name_parts[1] if len(name_parts) > 1 else '',
        row.get('GRUPO'),
        row.get('PLAN'),
        row.get('MONEDA DE PAGO'),
        row.get('ASESOR'),
        row.get('RESPONSABLE'),
        row.get('N⁰ CONTRATO'),
        row.get('PROCESO'),
        row.get('ESTATUS'),
        to_date(row.get('FECHA DE INGRESO')),
        row.get('NUMERO DE TLF'),
        # ===== CAMBIO APLICADO AQUÍ =====
        to_numeric(row.get('PORCENTAJE INSCRIPCION')),
        # ================================
        to_numeric(row.get('INSCRIPCION')),
        to_numeric(row.get('CUOTAS TOTALES')),
        to_numeric(row.get('CUOTAS PAGAS')),
        row.get(df.columns[16]), # Segunda columna ESTATUS por índice
        to_numeric(row.get('PAGOS IMPUNTUALES')),
        to_numeric(row.get('CUOTAS EN MORA')),
        row.get('OBSERVACIÓN'),
        to_numeric(row.get('VALOR DE CUOTA')),
        to_date(row.get('FECHA DE PAGO')),
        row.get('ESTATUS CUOTA'),
        to_numeric(row.get('VALOR CANCELADO'))
    )
    data_to_insert.append(data_tuple)

insert_query = """
INSERT INTO clientes (
    cedula, nombre, apellido, grupo, plan, moneda_pago, asesor, responsable,
    numero_contrato, proceso, estatus, fecha_ingreso, numero_telefono,
    porcentaje_inscripcion, inscripcion, cuotas_totales, cuotas_pagas,
    estatus_pago, pagos_impuntuales, cuotas_mora, observacion,
    valor_cuota, fecha_pago, estatus_cuota, valor_cancelado
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
cursor.executemany(insert_query, data_to_insert)
conn.commit()

print(f"¡Éxito! Se insertaron {cursor.rowcount} registros en la base de datos.")

# --- Cierre de Conexión ---
cursor.close()
conn.close()