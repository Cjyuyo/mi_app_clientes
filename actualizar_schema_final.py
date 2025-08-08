import os
import psycopg2
from dotenv import load_dotenv

# Lista de todas las columnas que deben existir en cada tabla.
# Esto nos ayuda a verificar y añadir solo lo que falta.
SCHEMA = {
    'administradores': [
        ('id', 'SERIAL PRIMARY KEY'),
        ('usuario', 'VARCHAR(80) UNIQUE NOT NULL'),
        ('password_hash', 'VARCHAR(255) NOT NULL'),
        ('rol', 'VARCHAR(50) NOT NULL'),
        ('ultimo_login', 'TIMESTAMPTZ'),
        ('estatus_online', 'BOOLEAN DEFAULT FALSE'),
        ('ultimo_visto', 'TIMESTAMPTZ')
    ],
    'clientes': [
        ('id', 'SERIAL PRIMARY KEY'),
        ('nombre', 'VARCHAR(100) NOT NULL'),
        ('apellido', 'VARCHAR(100) NOT NULL'),
        ('cedula', 'VARCHAR(20) UNIQUE NOT NULL'),
        ('contrato_nro', 'VARCHAR(50)'),
        ('telefono', 'VARCHAR(50)'),
        ('email', 'VARCHAR(100)'),
        ('direccion', 'TEXT'),
        ('asesor', 'VARCHAR(100)'),
        ('responsable', 'VARCHAR(100)'),
        ('fecha_ingreso', 'DATE'),
        ('grupo', 'VARCHAR(50)'),
        ('plan_contratado', 'VARCHAR(100)'),
        ('cuotas_totales', 'INTEGER'),
        ('moneda_pago', 'VARCHAR(10)'),
        ('valor_cuota', 'DECIMAL(10, 2)'),
        ('inscripcion_monto', 'DECIMAL(10, 2)'),
        ('inscripcion_pagada', 'DECIMAL(10, 2) DEFAULT 0'),
        ('proceso', 'VARCHAR(50)'),
        ('estatus', 'VARCHAR(50)'),
        ('cuotas_pagadas_progresivas', 'INTEGER DEFAULT 0'),
        ('cuotas_pagadas_regresivas', 'INTEGER DEFAULT 0'),
        ('balance_regresivo', 'DECIMAL(10, 2) DEFAULT 0'),
        ('meses_retraso_entrega', 'INTEGER DEFAULT 0'),
        ('gestor_id', 'INTEGER REFERENCES administradores(id)'),
        ('ignorar_penalidad_puntualidad', 'BOOLEAN DEFAULT FALSE'),
        ('firma_digital', 'TEXT'),
        ('firma_empresa', 'TEXT'),
        ('fecha_firma', 'TIMESTAMPTZ'),
        ('ciclo_cobranza', 'VARCHAR(50)'),
        ('foto_cliente', 'TEXT'),
        ('foto_cedula', 'TEXT'),
        ('beneficiario_nombre', 'VARCHAR(200)'),
        ('beneficiario_cedula', 'VARCHAR(20)'),
        ('beneficiario_telefono', 'VARCHAR(50)')
    ],
    'pagos': [
        ('id', 'SERIAL PRIMARY KEY'),
        ('cliente_id', 'INTEGER REFERENCES clientes(id) ON DELETE CASCADE'),
        ('monto', 'DECIMAL(10, 2) NOT NULL'),
        ('tipo_pago', 'VARCHAR(50)'),
        ('forma_pago', 'VARCHAR(50)'),
        ('fecha_pago', 'DATE'),
        ('referencia', 'VARCHAR(100)'),
        ('banco', 'VARCHAR(100)'),
        ('pago_en', 'VARCHAR(50)'),
        ('lugar_emision', 'VARCHAR(100)'),
        ('por_concepto_de', 'TEXT'),
        ('tasa_dia', 'DECIMAL(15, 4)'),
        ('monto_bs', 'DECIMAL(15, 2)'),
        ('moneda_referencia', 'VARCHAR(10)'),
        ('estado_pago', 'VARCHAR(50)'),
        ('cuotas_cubiertas', 'INTEGER'),
        ('progresivas_cubiertas', 'INTEGER'),
        ('regresivas_cubiertas', 'INTEGER'),
        ('cuotas_progresivas_al_pagar', 'INTEGER'),
        ('cuotas_regresivas_al_pagar', 'INTEGER'),
        ('balance_al_pagar', 'DECIMAL(10, 2)'),
        ('puntualidad', 'TEXT'),
        ('reportado_por_cliente', 'BOOLEAN DEFAULT FALSE'),
        ('estado_reporte', 'VARCHAR(50)'),
        ('detalles_reporte', 'JSONB'),
        ('registrado_por_id', 'INTEGER REFERENCES administradores(id)'),
        ('revisado_por_id', 'INTEGER REFERENCES administradores(id)'),
        ('conciliado_por_id', 'INTEGER REFERENCES administradores(id)'),
        ('fecha_creacion', 'TIMESTAMPTZ DEFAULT NOW()'),
        ('fecha_revision', 'TIMESTAMPTZ')
    ],
    'registros_auditoria': [
        ('id', 'SERIAL PRIMARY KEY'),
        ('usuario_id', 'INTEGER REFERENCES administradores(id)'),
        ('usuario_nombre', 'VARCHAR(80)'),
        ('accion', 'VARCHAR(100)'),
        ('descripcion', 'TEXT'),
        ('cliente_afectado_id', 'INTEGER REFERENCES clientes(id)'),
        ('timestamp', 'TIMESTAMPTZ DEFAULT NOW()'),
        ('detalles', 'JSONB')
    ],
    'solicitudes': [
        ('id', 'SERIAL PRIMARY KEY'),
        ('cliente_id', 'INTEGER REFERENCES clientes(id) ON DELETE CASCADE'),
        ('tipo_solicitud', 'VARCHAR(50) NOT NULL'),
        ('detalles', 'JSONB'),
        ('fecha_creacion', 'TIMESTAMPTZ DEFAULT NOW()'),
        ('estado', 'VARCHAR(50) DEFAULT \'Pendiente\''),
        ('revisado_por_id', 'INTEGER REFERENCES administradores(id)'),
        ('fecha_revision', 'TIMESTAMPTZ')
    ],
    'caja_inscripciones': [
        ('id', 'SERIAL PRIMARY KEY'),
        ('contrato_nro', 'VARCHAR(50) UNIQUE NOT NULL'),
        ('cliente_id', 'INTEGER REFERENCES clientes(id) ON DELETE CASCADE'),
        ('monto_inscripcion', 'DECIMAL(10, 2)'),
        ('responsable_cierre', 'VARCHAR(100)'),
        ('sobrante_empresa', 'DECIMAL(10, 2)'),
        ('comisiones_generadas', 'BOOLEAN DEFAULT FALSE'),
        ('fecha_registro', 'TIMESTAMPTZ DEFAULT NOW()')
    ],
    'comisiones_generadas': [
        ('id', 'SERIAL PRIMARY KEY'),
        ('contrato_nro', 'VARCHAR(50)'),
        ('cliente_id', 'INTEGER REFERENCES clientes(id) ON DELETE CASCADE'),
        ('nombre_beneficiario', 'VARCHAR(100)'),
        ('monto_comision', 'DECIMAL(10, 2)'),
        ('concepto', 'VARCHAR(255)'),
        ('estado_nomina', 'VARCHAR(50) DEFAULT \'Pendiente\''),
        ('fecha_pago_nomina', 'DATE')
    ],
    'ofertas': [
        ('id', 'SERIAL PRIMARY KEY'),
        ('cliente_id', 'INTEGER REFERENCES clientes(id) ON DELETE CASCADE'),
        ('fecha_oferta', 'DATE NOT NULL DEFAULT CURRENT_DATE'),
        ('cuotas_ofertadas', 'INTEGER NOT NULL'),
        ('estado_oferta', 'TEXT NOT NULL DEFAULT \'activa\'')
    ],
    'adjudicaciones': [
        ('id', 'SERIAL PRIMARY KEY'),
        ('fecha_adjudicacion', 'DATE NOT NULL DEFAULT CURRENT_DATE'),
        ('ganador_sorteo_id', 'INTEGER REFERENCES clientes(id)'),
        ('ganador_oferta_id', 'INTEGER REFERENCES clientes(id)')
    ],
    'gestiones_cobranza': [
        ('id', 'SERIAL PRIMARY KEY'),
        ('cliente_id', 'INTEGER REFERENCES clientes(id) ON DELETE CASCADE'),
        ('gestor_id', 'INTEGER REFERENCES administradores(id)'),
        ('nota', 'TEXT NOT NULL'),
        ('fecha_creacion', 'TIMESTAMPTZ DEFAULT NOW()')
    ],
    'historial_tasas_bcv': [
        ('fecha', 'DATE PRIMARY KEY'),
        ('tasa', 'DECIMAL(15, 4)'),
        ('tasa_euro', 'DECIMAL(15, 4)'),
        ('establecida_por_id', 'INTEGER REFERENCES administradores(id)')
    ],
    'operaciones_tesoreria': [
        ('id', 'SERIAL PRIMARY KEY'),
        ('tipo_operacion', 'VARCHAR(50) NOT NULL'),
        ('caja_origen', 'VARCHAR(50)'),
        ('moneda_origen', 'VARCHAR(10)'),
        ('monto_origen', 'DECIMAL(15, 2)'),
        ('caja_destino', 'VARCHAR(50)'),
        ('moneda_destino', 'VARCHAR(10)'),
        ('monto_destino', 'DECIMAL(15, 2)'),
        ('tasa_aplicada', 'DECIMAL(15, 4)'),
        ('perdida_cambiaria', 'DECIMAL(15, 2) DEFAULT 0'),
        ('nota', 'TEXT'),
        ('realizada_por', 'INTEGER REFERENCES administradores(id)'),
        ('fecha_operacion', 'TIMESTAMPTZ DEFAULT NOW()')
    ]
}

def actualizar_base_de_datos():
    load_dotenv()
    DATABASE_URL = os.getenv('DATABASE_URL')
    if not DATABASE_URL:
        print("ERROR: La variable de entorno DATABASE_URL no está configurada.")
        return

    conn = None
    try:
        print("🔌 Conectando a la base de datos...")
        conn = psycopg2.connect(DATABASE_URL)
        print("✅ ¡Conexión exitosa!")
        
        with conn.cursor() as cur:
            for table_name, columns in SCHEMA.items():
                print(f"\nVerificando tabla '{table_name}'...")
                
                # Crear la tabla si no existe
                create_sql = f"CREATE TABLE IF NOT EXISTS {table_name} ({', '.join([f'{col} {dtype}' for col, dtype in columns])});"
                cur.execute(create_sql)
                
                # Verificar y añadir columnas que falten
                for col_name, col_type in columns:
                    cur.execute("""
                        SELECT 1 FROM information_schema.columns 
                        WHERE table_name=%s AND column_name=%s;
                    """, (table_name, col_name))
                    if not cur.fetchone():
                        print(f"  -> La columna '{col_name}' no existe en '{table_name}'. Añadiéndola...")
                        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type};")
                        print(f"  -> ¡Columna '{col_name}' añadida exitosamente!")
                
                print(f"✔️  Tabla '{table_name}' verificada y actualizada.")
            
            conn.commit()
            print("\n\n🎉 ¡ÉXITO! La base de datos está completamente sincronizada con el esquema de la aplicación.")

    except psycopg2.Error as e:
        print(f"\n❌ ERROR de base de datos: {e}")
        if conn: conn.rollback()
    except Exception as e:
        print(f"\n❌ ERROR inesperado: {e}")
        if conn: conn.rollback()
    finally:
        if conn:
            conn.close()
            print("\n🔌 Conexión a la base de datos cerrada.")

if __name__ == '__main__':
    actualizar_base_de_datos()