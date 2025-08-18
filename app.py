import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, g, flash, redirect, url_for, session, Response, jsonify
from dotenv import load_dotenv
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import datetime, timedelta, date, time
from calendar import monthrange
import random
from werkzeug.security import check_password_hash, generate_password_hash
from functools import wraps
import io
import csv
import logging
import pytz
import json

# Imports para AWS S3
import boto3
from botocore.exceptions import NoCredentialsError

# >>> COMISIONES: BEGIN [imports]
from collections import defaultdict
import pandas as pd
from fpdf import FPDF
# >>> COMISIONES: END [imports]

# =================================================================================
# ===== CONFIGURACIÓN INICIAL Y DE ENTORNO =====
# =================================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'una-clave-secreta-por-defecto-para-desarrollo')

VENEZUELA_TZ = pytz.timezone('America/Caracas')

def get_venezuela_current_datetime():
    """Devuelve la fecha y hora actual en la zona horaria de Venezuela."""
    return datetime.now(VENEZUELA_TZ)

def get_venezuela_current_date():
    """Devuelve solo la fecha actual en la zona horaria de Venezuela."""
    return get_venezuela_current_datetime().date()

# =================================================================================
# ===== FUNCIONES DE UTILIDAD Y FILTROS JINJA =====
# =================================================================================

def get_proximo_dia_habil(fecha):
    """Calcula el próximo día hábil a partir de una fecha dada, saltando fines de semana."""
    proximo_dia = fecha + timedelta(days=1)
    while proximo_dia.weekday() >= 5:  # 5 = Sábado, 6 = Domingo
        proximo_dia += timedelta(days=1)
    return proximo_dia

def time_ago(time_value):
    """Convierte un datetime a un formato legible 'hace X tiempo'."""
    if not time_value:
        return "Nunca"
    
    now = datetime.now(pytz.utc)
    
    if time_value.tzinfo is None:
        time_value = pytz.utc.localize(time_value)
    
    diff = now - time_value
    
    seconds = diff.total_seconds()
    minutes = seconds / 60
    hours = minutes / 60
    days = hours / 24

    if seconds < 10:
        return "justo ahora"
    if seconds < 60:
        return f"hace {int(seconds)} segundos"
    elif minutes < 60:
        return f"hace {int(minutes)} minuto{'s' if int(minutes) > 1 else ''}"
    elif hours < 24:
        return f"hace {int(hours)} hora{'s' if int(hours) > 1 else ''}"
    else:
        return f"hace {int(days)} día{'s' if int(days) > 1 else ''}"


@app.template_filter('format_datetime')
def format_datetime_filter(value, format='%d/%m/%Y %I:%M %p'):
    """Filtro de Jinja para formatear fechas y horas a la zona horaria de Venezuela."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            try:
                value = datetime.strptime(value, '%Y-%m-%d %H:%M:%S.%f')
            except (ValueError, TypeError):
                 return value

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = pytz.utc.localize(value).astimezone(VENEZUELA_TZ)
        else:
            value = value.astimezone(VENEZUELA_TZ)
        return value.strftime(format)
    
    if isinstance(value, date):
        return value.strftime('%d/%m/%Y')
        
    return value

def get_nombre_mes(month_number):
    """Convierte el número de un mes a su nombre en español."""
    meses = {1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"}
    return meses.get(month_number, "")

@app.template_filter('format_date')
def format_date_filter(value, format='%d/%m/%Y'):
    """Filtro de Jinja para formatear fechas. Acepta formatos especiales como '%B' para el nombre del mes."""
    if value == 'now':
        return get_venezuela_current_date().strftime(format)
    if isinstance(value, str):
        try:
            value = datetime.strptime(value, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return value
    if isinstance(value, (datetime, date)):
        format_es = format.replace('%B', get_nombre_mes(value.month))
        dias_semana = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
        format_es = format_es.replace('%A', dias_semana[value.weekday()])
        return value.strftime(format_es)
    return value


# =================================================================================
# ===== CONEXIÓN A LA BASE DE DATOS =====
# =================================================================================

def get_db():
    if 'db' not in g:
        DATABASE_URL = os.getenv('DATABASE_URL')
        if not DATABASE_URL:
            raise ValueError("FATAL: La variable de entorno DATABASE_URL no está configurada.")
        try:
            g.db = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.DictCursor)
        except psycopg2.OperationalError as e:
            logging.error(f"Error de conexión a la base de datos: {e}")
            g.db = None
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()

# =================================================================================
# ===== GESTIÓN DE SESIÓN Y AUTENTICACIÓN =====
# =================================================================================

@app.before_request
def setup_session_and_user():
    session.permanent = True
    app.permanent_session_lifetime = timedelta(minutes=60)
    
    g.admin = None
    g.cliente = None
    g.anio_actual = get_venezuela_current_date().year
    g.get_venezuela_current_datetime = get_venezuela_current_datetime
    admin_id = session.get('admin_id')
    cliente_id = session.get('cliente_id')
    db = get_db()
    if db:
        with db.cursor() as cur:
            if admin_id:
                cur.execute("SELECT id, usuario, rol FROM administradores WHERE id = %s", (admin_id,))
                g.admin = cur.fetchone()
                if g.admin:
                    cur.execute("UPDATE administradores SET ultimo_visto = NOW() WHERE id = %s", (g.admin['id'],))
                    db.commit()
            elif cliente_id:
                cur.execute("SELECT id, nombre, apellido, estatus FROM clientes WHERE id = %s", (cliente_id,))
                g.cliente = cur.fetchone()

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if g.cliente is not None:
            flash('No puedes acceder al panel de administración con una sesión de cliente activa.', 'warning')
            return redirect(url_for('portal_dashboard'))
        if g.admin is None:
            flash('Acceso denegado. Debes iniciar sesión como administrador.', 'warning')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

def rol_requerido(*roles):
    def wrapper(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if g.cliente is not None:
                flash('No puedes acceder al panel de administración con una sesión de cliente activa.', 'warning')
                return redirect(url_for('portal_dashboard'))
            if g.admin is None:
                flash('Acceso denegado. Debes iniciar sesión como administrador.', 'warning')
                return redirect(url_for('admin_login'))
            if g.admin['rol'] not in roles:
                flash('No tienes los permisos necesarios para acceder a esta página.', 'danger')
                return redirect(url_for('hub'))
            return f(*args, **kwargs)
        return decorated_function
    return wrapper

def portal_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if g.admin is not None:
            flash('No puedes acceder al portal de clientes con una sesión de administrador activa.', 'warning')
            return redirect(url_for('hub'))
        if 'cliente_id' not in session:
            flash('Por favor, inicia sesión para acceder a tu portal.', 'warning')
            return redirect(url_for('portal_login'))
        return f(*args, **kwargs)
    return decorated_function

# =================================================================================
# ===== FUNCIONES AUXILIARES (AUDITORÍA, COMISIONES, TESORERÍA) =====
# =================================================================================

ef subir_archivo_a_s3(base64_data, nombre_en_s3):
    """Sube un archivo a S3 desde una cadena de datos Base64."""
    
    s3_client = boto3.client('s3')
    bucket_name = os.environ.get('AWS_STORAGE_BUCKET_NAME')
    
    if not bucket_name:
        logging.error("FATAL: La variable de entorno AWS_STORAGE_BUCKET_NAME no está configurada.")
        return False
        
    try:
        # Decodifica la cadena Base64 que viene del formulario (ej: "data:image/jpeg;base64,/9j/4AA...")
        header, encoded = base64_data.split(",", 1)
        image_data = base64.b64decode(encoded)
        
        # Crea un objeto de archivo en memoria para subirlo
        in_mem_file = io.BytesIO(image_data)
        
        # Sube el objeto a S3, haciéndolo público para que se pueda ver en el contrato
        s3_client.upload_fileobj(
            in_mem_file, 
            bucket_name, 
            nombre_en_s3,
            ExtraArgs={
                'ContentType': 'image/jpeg',
                'ACL': 'public-read'  # Permiso para que la imagen sea visible públicamente
            }
        )
        
        logging.info(f"Subida exitosa a S3: {nombre_en_s3}")
        return True
    except NoCredentialsError:
        logging.error("Credenciales de AWS no encontradas.")
        return False
    except Exception as e:
        logging.error(f"Error al subir archivo a S3: {e}")
        return False

import logging
import json
import psycopg2 # Se asume que se usa psycopg2 para la conexión a PostgreSQL
from decimal import Decimal
from datetime import datetime, date, timedelta
from flask import g, session, request, flash, redirect, url_for

# Asumimos que tienes funciones como estas definidas en otra parte de tu aplicación.
# -----------------------------------------------------------------------------
# def get_db():
#     """Abre una nueva conexión a la base de datos si no existe una para el contexto actual."""
#     if 'db' not in g:
#         g.db = conectar_a_la_base_de_datos() # Tu lógica de conexión
#     return g.db
#
# def get_venezuela_current_date():
#      """Obtiene la fecha actual en la zona horaria de Venezuela."""
#      # Tu lógica para obtener la fecha
#      return date.today()
# -----------------------------------------------------------------------------


def registrar_accion_auditoria(accion, descripcion, cliente_id=None, detalles_adicionales=None):
    """
    Registra una acción en la tabla de auditoría, verificando la conexión
    y la autenticación del usuario.
    """
    conn = get_db() 
    
    if not conn:
        logging.error(f"AUDITORIA-FALLO-CONEXION: No se pudo obtener la conexión a la BD para registrar '{accion}'.")
        return

    if not g.admin and 'cliente_id' not in session:
        logging.warning(f"AUDITORIA-OMITIDA: Intento de registrar '{accion}' sin un usuario autenticado.")
        return

    usuario_id = g.admin['id'] if g.admin else None
    usuario_nombre = g.admin['usuario'] if g.admin else f"Cliente ID {session.get('cliente_id')}"
    cliente_afectado = cliente_id if cliente_id else session.get('cliente_id')
    detalles_json = json.dumps(detalles_adicionales) if detalles_adicionales else None
    ip_address = request.remote_addr

    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO registros_auditoria (usuario_id, usuario_nombre, accion, descripcion, cliente_afectado_id, detalles, ip_address) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (usuario_id, usuario_nombre, accion, descripcion, cliente_afectado, detalles_json, ip_address)
            )
        conn.commit()
        logging.info(f"AUDITORIA-REGISTRADA: Usuario '{usuario_nombre}' realizó '{accion}'.")
    except Exception as e:
        logging.error(f"AUDITORIA-FALLO-INSERCION: {e}")
        conn.rollback()


def calcular_y_guardar_comisiones(contrato_nro, cliente_id, monto_plan, asesor_dueno, responsable_cierre):
    """Calcula y guarda las comisiones basadas en el escenario de venta."""
    conn = get_db()
    if not conn or monto_plan <= 0:
        logging.error(f"COMISIONES: No se pudo conectar a la BD o el monto del plan es cero para contrato {contrato_nro}.")
        return

    POOL_COMISIONES = monto_plan * Decimal('0.16')
    PRESIDENCIA = ['Carlos', 'Karielsy']
    YUSBELIS = 'Yusbelis'
    
    comisiones_a_registrar = []
    asesor_dueno_std = asesor_dueno.strip().title() if asesor_dueno else ''
    responsable_cierre_std = responsable_cierre.strip().title() if responsable_cierre else ''
    primer_nombre_responsable = responsable_cierre_std.split(' ')[0]

    if primer_nombre_responsable in PRESIDENCIA:
        logging.info(f"Contrato {contrato_nro}: Aplicando Escenario 2 (Cierre Presidencia).")
        monto_presidencia = monto_plan * Decimal('0.055')
        comisiones_a_registrar.append({'beneficiario': 'Carlos Ramirez', 'monto': monto_presidencia, 'concepto': 'Comisión Presidencia'})
        comisiones_a_registrar.append({'beneficiario': 'Karielsy Rios', 'monto': monto_presidencia, 'concepto': 'Comisión Presidencia'})
        monto_yusbelis = monto_plan * Decimal('0.005')
        comisiones_a_registrar.append({'beneficiario': 'Yusbelis Espinoza', 'monto': monto_yusbelis, 'concepto': 'Comisión Staff'})
        comisiones_a_registrar.append({'beneficiario': asesor_dueno_std, 'monto': Decimal('5.0'), 'concepto': 'Bono Asesor Dueño'})

    elif primer_nombre_responsable == YUSBELIS:
        logging.info(f"Contrato {contrato_nro}: Aplicando Escenario 3 (Cierre Yusbelis).")
        monto_presidencia = monto_plan * Decimal('0.055')
        comisiones_a_registrar.append({'beneficiario': 'Carlos Ramirez', 'monto': monto_presidencia, 'concepto': 'Comisión Presidencia'})
        comisiones_a_registrar.append({'beneficiario': 'Karielsy Rios', 'monto': monto_presidencia, 'concepto': 'Comisión Presidencia'})
        monto_yusbelis = monto_plan * Decimal('0.01')
        comisiones_a_registrar.append({'beneficiario': 'Yusbelis Espinoza', 'monto': monto_yusbelis, 'concepto': 'Comisión Cierre Staff'})

    else:
        logging.info(f"Contrato {contrato_nro}: Aplicando Escenario 1 (Cierre Asesor).")
        monto_presidencia = monto_plan * Decimal('0.03')
        comisiones_a_registrar.append({'beneficiario': 'Carlos Ramirez', 'monto': monto_presidencia, 'concepto': 'Comisión Presidencia'})
        comisiones_a_registrar.append({'beneficiario': 'Karielsy Rios', 'monto': monto_presidencia, 'concepto': 'Comisión Presidencia'})
        monto_yusbelis = monto_plan * Decimal('0.01')
        comisiones_a_registrar.append({'beneficiario': 'Yusbelis Espinoza', 'monto': monto_yusbelis, 'concepto': 'Comisión Staff'})
        monto_asesor_dueno = monto_plan * Decimal('0.02')
        comisiones_a_registrar.append({'beneficiario': asesor_dueno_std, 'monto': monto_asesor_dueno, 'concepto': 'Comisión Asesor Dueño'})
        if asesor_dueno_std != responsable_cierre_std:
            comisiones_a_registrar.append({'beneficiario': responsable_cierre_std, 'monto': Decimal('5.0'), 'concepto': 'Bono Cierre Asesor'})

    if comisiones_a_registrar:
        total_comisiones_pagadas = sum(c['monto'] for c in comisiones_a_registrar)
        sobrante_empresa = POOL_COMISIONES - total_comisiones_pagadas
        try:
            with conn.cursor() as cur:
                sql_comisiones = """
                    INSERT INTO comisiones (origen_id, origen_tipo, asesor_id, moneda, base, pct_comision, monto, estado, notas, fecha_origen)
                    SELECT c.id, 'Venta', a.id, 'USD', %s, 1, %s, 'pendiente', %s, c.fecha_ingreso
                    FROM clientes c, administradores a
                    WHERE c.id = %s AND a.usuario = %s
                """
                for comision in comisiones_a_registrar:
                    if comision['monto'] > 0:
                         cur.execute(sql_comisiones, (monto_plan, comision['monto'], comision['concepto'], cliente_id, comision['beneficiario']))

                sql_sobrante = "UPDATE caja_inscripciones SET sobrante_empresa = %s WHERE contrato_nro = %s"
                cur.execute(sql_sobrante, (sobrante_empresa, contrato_nro))
            logging.info(f"COMISIONES v3.0: Contrato {contrato_nro} procesado. Total a pagar: ${total_comisiones_pagadas:,.2f}. Sobrante: ${sobrante_empresa:,.2f}.")
        except psycopg2.Error as e:
            logging.error(f"COMISIONES v3.0: Error al guardar comisiones para contrato {contrato_nro}: {e}")
            raise e

def calcular_balances_tesoreria(fecha_hasta=None):
    """Calcula los balances de las cajas de tesorería hasta una fecha específica."""
    conn = get_db()
    cajas_generales = ['EFECTIVO_USD', 'BINANCE_USDT', 'CAJA_BS_USD', 'CAJA_BS_EUR']
    balances = {caja: Decimal('0.0') for caja in cajas_generales}
    balances['CAJA_BS_TOTAL'] = Decimal('0.0')
    if not conn: return balances
    if fecha_hasta is None: fecha_hasta = get_venezuela_current_date()
    fecha_fin_timestamp = datetime.combine(fecha_hasta, datetime.max.time())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(monto), 0) FROM pagos WHERE estado_pago = 'Conciliado' AND pago_en = 'Efectivo USD' AND fecha_pago <= %s", (fecha_hasta,))
            balances['EFECTIVO_USD'] += cur.fetchone()[0] or Decimal('0.0')
            cur.execute("SELECT COALESCE(SUM(monto_bs), 0) FROM pagos WHERE estado_pago = 'Conciliado' AND moneda_referencia = 'USD' AND monto_bs > 0 AND fecha_pago <= %s", (fecha_hasta,))
            balances['CAJA_BS_USD'] += cur.fetchone()[0] or Decimal('0.0')
            cur.execute("SELECT COALESCE(SUM(monto_bs), 0) FROM pagos WHERE estado_pago = 'Conciliado' AND moneda_referencia = 'EUR' AND monto_bs > 0 AND fecha_pago <= %s", (fecha_hasta,))
            balances['CAJA_BS_EUR'] += cur.fetchone()[0] or Decimal('0.0')
            cur.execute("SELECT caja_origen, caja_destino, monto_origen, monto_destino FROM operaciones_tesoreria WHERE fecha_operacion <= %s", (fecha_fin_timestamp,))
            movimientos = cur.fetchall()
            for mov in movimientos:
                if mov['caja_origen'] in balances: balances[mov['caja_origen']] -= mov['monto_origen']
                if mov['caja_destino'] in balances: balances[mov['caja_destino']] += mov['monto_destino']
            balances['CAJA_BS_TOTAL'] = balances['CAJA_BS_USD'] + balances['CAJA_BS_EUR']
    except psycopg2.Error as e:
        flash(f"Error calculando balances de tesorería: {e}", "danger")
    return balances

def get_feriados_venezuela(year):
    """Devuelve una lista de objetos date para los feriados en Venezuela para un año dado."""
    feriados = [
        date(year, 1, 1), date(year, 5, 1), date(year, 6, 24), date(year, 7, 5),
        date(year, 7, 24), date(year, 10, 12), date(year, 12, 24), date(year, 12, 25),
        date(year, 12, 31)
    ]
    if year == 2024:
        feriados.extend([date(2024, 2, 12), date(2024, 2, 13), date(2024, 3, 28), date(2024, 3, 29)])
    if year == 2025:
        feriados.extend([date(2025, 3, 3), date(2025, 3, 4), date(2025, 4, 17), date(2025, 4, 18)])
    feriados.append(date(year, 4, 19))
    return feriados

def get_fecha_vencimiento_ajustada(fecha_pago):
    """Ajusta la fecha de vencimiento al próximo día hábil si cae en fin de semana o feriado."""
    if fecha_pago.day < 15:
        mes_vencimiento, ano_vencimiento = fecha_pago.month, fecha_pago.year
    else:
        mes_vencimiento = fecha_pago.month + 1 if fecha_pago.month < 12 else 1
        ano_vencimiento = fecha_pago.year if fecha_pago.month < 12 else fecha_pago.year + 1
    vencimiento = datetime(ano_vencimiento, mes_vencimiento, 2).date()
    feriados = get_feriados_venezuela(vencimiento.year)
    while vencimiento.weekday() >= 5 or vencimiento in feriados:
        vencimiento += timedelta(days=1)
        if vencimiento.year != ano_vencimiento: feriados = get_feriados_venezuela(vencimiento.year)
    return vencimiento

def delete_client(client_id):
    """
    Elimina un cliente y todos sus registros asociados de la base de datos.
    """
    conn = get_db()
    if not conn: 
        flash('Error de conexión a la base de datos.', 'error')
        return redirect(url_for('consulta'))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT nombre, apellido, cedula FROM clientes WHERE id = %s", (client_id,))
            cliente_a_borrar = cur.fetchone()
            if not cliente_a_borrar:
                flash('El cliente que intenta eliminar no existe.', 'warning')
                return redirect(url_for('consulta'))
            
            # --- INICIO DE LA CORRECCIÓN ---
            # Lista definitiva y ordenada de tablas para eliminar dependencias.
            # Se eliminan primero las tablas que tienen claves foráneas apuntando a otras.
            tablas_relacionadas = [
                "comisiones_rebalanceos", # Apunta a 'comisiones'
                "comisiones_lotes_pago",   # Relacionada con 'comisiones'
                "adjudicaciones", "caja_inscripciones", "comisiones", "comisiones_legacy",
                "contratos_historicos", "gestiones_cobranza", "gestiones_cobranza_legacy",
                "historial_contratos", "ofertas", "ofertas_legacy", "pagos",
                "payment_bulks", "payment_orders", "receipts", "registros_auditoria",
                "solicitudes", "solicitudes_legacy", "transacciones_financieras"
            ]
            # --- FIN DE LA CORRECCIÓN ---
            
            logging.info(f"Iniciando proceso de eliminación para cliente ID: {client_id}")
            for tabla in tablas_relacionadas:
                logging.info(f"Intentando eliminar registros de la tabla: {tabla} para cliente_id {client_id}")
                if tabla == 'adjudicaciones':
                    cur.execute(f"DELETE FROM {tabla} WHERE ganador_sorteo_id = %s OR ganador_oferta_id = %s", (client_id, client_id))
                elif tabla == 'registros_auditoria':
                     cur.execute(f"DELETE FROM {tabla} WHERE cliente_afectado_id = %s", (client_id,))
                elif tabla == 'comisiones_rebalanceos':
                    # Esta tabla no tiene cliente_id, se limpia por comision_id_origen
                    cur.execute("DELETE FROM comisiones_rebalanceos WHERE comision_id_origen IN (SELECT id FROM comisiones WHERE origen_id = %s AND origen_tipo = 'Venta')", (client_id,))
                elif tabla == 'comisiones_lotes_pago':
                    # Esta tabla no tiene cliente_id, se limpia a través de las comisiones
                    cur.execute("UPDATE comisiones SET payment_batch_id = NULL WHERE origen_id = %s AND origen_tipo = 'Venta'", (client_id,))
                else:
                    # Se asume que la columna es 'cliente_id' para las demás tablas
                    cur.execute(f"DELETE FROM {tabla} WHERE cliente_id = %s", (client_id,))
                logging.info(f"Se eliminaron {cur.rowcount} registros de {tabla}.")

            # Registrar la acción en la auditoría ANTES de eliminar al cliente
            descripcion_audit = f"Eliminó al cliente {cliente_a_borrar['nombre']} {cliente_a_borrar['apellido']} (C.I. {cliente_a_borrar['cedula']}) y todos sus datos asociados."
            registrar_accion_auditoria('ELIMINACION_CLIENTE', descripcion_audit, client_id)
            
            # Finalmente, eliminar el registro del cliente
            logging.info(f"Intentando eliminar el registro principal del cliente ID: {client_id}...")
            cur.execute("DELETE FROM clientes WHERE id = %s", (client_id,))
            logging.info("Registro principal del cliente eliminado.")
            
            conn.commit()
            flash('¡Cliente y todos sus registros asociados han sido eliminados exitosamente!', 'success')

    except psycopg2.Error as e:
        conn.rollback()
        print(f"ERROR DE BASE DE DATOS AL ELIMINAR CLIENTE ID {client_id}: {e}")
        flash(f"ERROR DE INTEGRIDAD: La base de datos bloqueó la eliminación. Detalles: {e}", 'danger')
        
    except Exception as e:
        conn.rollback()
        print(f"ERROR INESPERADO AL ELIMINAR CLIENTE ID {client_id}: {e}")
        flash(f'Ocurrió un error inesperado al eliminar: {e}', 'error')

    return redirect(url_for('consulta'))


# =================================================================================
# ===== FUNCIONES HELPER PARA LÓGICA DE PAGOS POR DIFERENCIA =====
# =================================================================================

def recalcular_totales_bulk(bulk_id):
    """Recalcula los totales reportados y verificados de un bulk basado en sus líneas de pago."""
    conn = get_db()
    if not conn:
        logging.error(f"BULK_RECALC: Falla de conexión para bulk_id {bulk_id}")
        return
    try:
        with conn.cursor() as cur:
            # Suma los montos de todos los pagos asociados al bulk que no estén anulados
            cur.execute("""
                SELECT
                    COALESCE(SUM(monto_bs), 0) as total_reported,
                    COALESCE(SUM(verified_amount), 0) as total_verified
                FROM pagos
                WHERE bulk_id = %s AND estado_pago != 'Anulado'
            """, (bulk_id,))
            totales = cur.fetchone()

            # Actualiza la tabla payment_bulks con los nuevos totales
            cur.execute("""
                UPDATE payment_bulks
                SET total_reported = %s, total_verified = %s, updated_at = NOW()
                WHERE id = %s
            """, (totales['total_reported'], totales['total_verified'], bulk_id))
        conn.commit()
        logging.info(f"BULK_RECALC: Totales actualizados para bulk_id {bulk_id}")
    except psycopg2.Error as e:
        conn.rollback()
        logging.error(f"BULK_RECALC: Error recalculando totales para bulk_id {bulk_id}: {e}")

def simular_verificacion_bancaria(monto_reportado_bs):
    """
    Simula una verificación bancaria.
    Para pruebas, devuelve un monto menor si el reportado es mayor a 100.
    """
    if monto_reportado_bs > 100:
        diferencia = monto_reportado_bs * Decimal('0.05')
        # Redondea a 2 decimales, que es el estándar para moneda
        return (monto_reportado_bs - diferencia).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return monto_reportado_bs

# =================================================================================
# ===== RUTAS DE NAVEGACIÓN Y AUTENTICACIÓN =====
# =================================================================================

@app.route('/')
def home():
    # Si un administrador ya inició sesión, lo llevamos a su hub.
    if g.admin:
        return redirect(url_for('hub'))
    # Si un cliente ya inició sesión, lo llevamos a su portal.
    elif g.cliente:
        return redirect(url_for('portal_dashboard'))
    # Si nadie ha iniciado sesión, mostramos la nueva landing page.
    return render_template('landing.html', anio_actual=get_venezuela_current_date().year)

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if g.admin: return redirect(url_for('hub'))
    if request.method == 'POST':
        usuario, password = request.form.get('usuario'), request.form.get('password')
        conn = get_db()
        if not conn:
            flash('Error de conexión con la base de datos.', 'danger')
            return render_template('admin_login.html', anio_actual=get_venezuela_current_date().year)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM administradores WHERE usuario = %s", (usuario,))
            admin = cur.fetchone()
            if admin and check_password_hash(admin['password_hash'], password):
                session.clear()
                session['admin_id'] = admin['id']
                cur.execute("UPDATE administradores SET ultimo_login = NOW(), estatus_online = TRUE, ultimo_visto = NOW() WHERE id = %s", (admin['id'],))
                conn.commit()
                flash(f"¡Bienvenido de nuevo, {admin['usuario']}!", 'success')
                return redirect(url_for('hub'))
            else:
                flash('Usuario o contraseña incorrectos.', 'danger')
    return render_template('admin_login.html', anio_actual=get_venezuela_current_date().year)

@app.route('/admin/logout')
def admin_logout():
    admin_id = session.get('admin_id')
    if admin_id:
        conn = get_db()
        if conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE administradores SET estatus_online = FALSE WHERE id = %s", (admin_id,))
                conn.commit()
    session.clear()
    flash('Has cerrado la sesión exitosamente.', 'info')
    return redirect(url_for('admin_login'))

# =================================================================================
# ===== RUTAS DEL PANEL DE ADMINISTRADOR =====
# =================================================================================

@app.route('/hub')
@admin_required
def hub():
    stats = {
        'clientes_cartera': 0,
        'recaudado_mes': Decimal('0.0'),
        'solicitudes_pendientes': 0,
        'reportes_pendientes': 0, 
        'pagos_por_conciliar': 0,
        'tasa_bcv': 'N/A'
    }
    tasa_ocupacion = 0.0
    conn = get_db()
    if conn:
        try:
            with conn.cursor() as cur:
                if g.admin['rol'] in ['superadmin', 'gerente', 'administradora']:
                     cur.execute("SELECT COUNT(*) FROM clientes WHERE estatus = 'ACTIVO'")
                else:
                    cur.execute("SELECT COUNT(*) FROM clientes WHERE gestor_id = %s AND estatus = 'ACTIVO'", (g.admin['id'],))
                stats['clientes_cartera'] = cur.fetchone()[0]

                if g.admin['rol'] in ['superadmin', 'gerente']:
                    first_day_of_month = get_venezuela_current_date().replace(day=1)
                    cur.execute("SELECT COALESCE(SUM(monto), 0) FROM pagos WHERE estado_pago = 'Conciliado' AND fecha_pago >= %s", (first_day_of_month,))
                    stats['recaudado_mes'] = cur.fetchone()[0]
                
                if g.admin['rol'] in ['superadmin', 'gerente', 'administradora']:
                    cur.execute("SELECT COUNT(*) FROM solicitudes WHERE estado = 'Pendiente'")
                    stats['solicitudes_pendientes'] = cur.fetchone()[0]

                if g.admin['rol'] in ['superadmin', 'gerente', 'administradora']:
                    cur.execute("SELECT COUNT(*) FROM pagos WHERE reportado_por_cliente = TRUE AND estado_reporte = 'Pendiente de Revision'")
                    stats['reportes_pendientes'] = cur.fetchone()[0]

                    cur.execute("""
                        SELECT COUNT(*) FROM pagos 
                        WHERE estado_pago = 'Pendiente' 
                        AND (reportado_por_cliente = FALSE OR estado_reporte = 'Aprobado')
                    """)
                    stats['pagos_por_conciliar'] = cur.fetchone()[0]

                cur.execute("SELECT tasa FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (get_venezuela_current_date(),))
                tasa_row = cur.fetchone()
                if tasa_row and tasa_row['tasa']:
                    stats['tasa_bcv'] = f"{tasa_row['tasa']:,.2f} Bs"
                
                if g.admin['rol'] in ['superadmin', 'gerente', 'administradora']:
                    today_str = get_venezuela_current_date().isoformat()
                    cur.execute("SELECT COUNT(*) FROM solicitudes WHERE tipo_solicitud = 'Cita' AND estado IN ('Aprobada', 'Completada') AND (detalles->>'fecha_cita') = %s", (today_str,))
                    citas_totales_hoy = cur.fetchone()[0]
                    cur.execute("SELECT COUNT(*) FROM solicitudes WHERE tipo_solicitud = 'Cita' AND estado = 'Completada' AND (detalles->>'fecha_cita') = %s", (today_str,))
                    citas_completadas_hoy = cur.fetchone()[0]
                    if citas_totales_hoy > 0:
                        tasa_ocupacion = (citas_completadas_hoy / citas_totales_hoy) * 100

        except psycopg2.Error as e:
            logging.error(f"Error al calcular estadísticas para el HUB: {e}")
            flash("No se pudieron cargar todas las estadísticas del panel.", "warning")

    return render_template('hub.html', stats=stats, tasa_ocupacion=tasa_ocupacion)


@app.route('/api/get_active_sessions')
@admin_required
def get_active_sessions():
    conn = get_db()
    if not conn:
        return jsonify([])

    users_list = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT usuario, ultimo_visto, 
                       (ultimo_visto > NOW() - INTERVAL '2 minutes') AS is_online
                FROM administradores ORDER BY usuario
            """)
            usuarios_db = cur.fetchall()
            for user in usuarios_db:
                users_list.append({
                    "username": user['usuario'],
                    "is_online": user['is_online'],
                    "last_seen": time_ago(user['ultimo_visto'])
                })
    except psycopg2.Error as e:
        logging.error(f"Error en API get_active_sessions: {e}")
        return jsonify({"error": "Database error"}), 500
        
    return jsonify(users_list)

@app.route('/hub_asesor')
@admin_required
def hub_asesor():
    conn = get_db()
    citas_pendientes = []
    citas_completadas = []
    clientes_en_mora = []
    if not conn:
        flash("Error de conexión con la base de datos.", "danger")
        return render_template('hub_asesor.html', citas_pendientes=citas_pendientes, citas_completadas=citas_completadas, clientes_en_mora=clientes_en_mora)

    asesor_id = g.admin['id']
    today = get_venezuela_current_date()
    today_str = today.isoformat()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE solicitudes 
                SET estado = 'Completada', detalles = jsonb_set(detalles, '{estado_final}', '"Inasistencia"')
                WHERE tipo_solicitud = 'Cita' AND estado = 'Aprobada' AND (detalles->>'asesor_id')::int = %s
                AND (detalles->>'fecha_cita')::date < %s
            """, (asesor_id, today_str))
            conn.commit()

            cur.execute("""
                SELECT s.id, s.detalles, c.nombre || ' ' || c.apellido as nombre_cliente
                FROM solicitudes s JOIN clientes c ON s.cliente_id = c.id
                WHERE s.tipo_solicitud = 'Cita' AND s.estado = 'Aprobada'
                AND (s.detalles->>'asesor_id')::int = %s
                AND (s.detalles->>'fecha_cita') = %s
                ORDER BY (s.detalles->>'hora_cita') ASC
            """, (asesor_id, today_str))
            citas_pendientes = cur.fetchall()

            cur.execute("""
                SELECT s.id, s.detalles, s.detalles->>'estado_final' as estado_final, c.nombre || ' ' || c.apellido as nombre_cliente
                FROM solicitudes s JOIN clientes c ON s.cliente_id = c.id
                WHERE s.tipo_solicitud = 'Cita' AND s.estado = 'Completada'
                AND (s.detalles->>'asesor_id')::int = %s
                ORDER BY (s.detalles->>'fecha_cita')::date DESC, (s.detalles->>'hora_cita') DESC
                LIMIT 10
            """, (asesor_id,))
            citas_completadas = cur.fetchall()

            first_day_of_month = today.replace(day=1)
            subquery_pagaron_mes = "SELECT DISTINCT cliente_id FROM pagos WHERE tipo_pago = 'Cuota' AND estado_pago = 'Conciliado' AND fecha_pago >= %s"
            query_morosos = f"""
                SELECT c.id, c.nombre, c.apellido, c.cedula
                FROM clientes c
                WHERE c.gestor_id = %s AND TRIM(UPPER(c.proceso)) = 'AHORRADOR' AND TRIM(UPPER(c.estatus)) = 'ACTIVO'
                AND c.id NOT IN ({subquery_pagaron_mes}) ORDER BY c.nombre, c.apellido;
            """
            cur.execute(query_morosos, (asesor_id, first_day_of_month))
            clientes_en_mora = cur.fetchall()

    except psycopg2.Error as e:
        flash(f"Error al cargar el hub de asesor: {e}", "danger")

    return render_template('hub_asesor.html', citas_pendientes=citas_pendientes, citas_completadas=citas_completadas, clientes_en_mora=clientes_en_mora)


@app.route('/citas/registrar_interaccion/<int:cita_id>', methods=['POST'])
@admin_required
def registrar_interaccion_cita(cita_id):
    conn = get_db()
    accion = request.form.get('accion')
    
    if not conn:
        return jsonify({'status': 'error', 'message': 'Error de conexión'}), 500

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cliente_id, detalles FROM solicitudes WHERE id = %s AND (detalles->>'asesor_id')::int = %s", (cita_id, g.admin['id']))
            cita = cur.fetchone()
            if not cita:
                return jsonify({'status': 'error', 'message': 'Cita no encontrada o sin permiso'}), 404

            detalles = cita['detalles']
            hora_actual_str = get_venezuela_current_datetime().isoformat()
            
            if accion == 'iniciar':
                detalles['hora_inicio'] = hora_actual_str
                cur.execute("UPDATE solicitudes SET detalles = %s WHERE id = %s", (json.dumps(detalles), cita_id))
                registrar_accion_auditoria('INICIO_ATENCION_CITA', f"Asesor {g.admin['usuario']} inició atención para cita N°{cita_id}.", cita['cliente_id'])
                conn.commit()
                return jsonify({'status': 'success', 'message': 'Inicio de atención registrado.'})

            elif accion == 'finalizar':
                reporte = request.form.get('reporte', '').strip()
                if not reporte:
                    flash("El reporte no puede estar vacío.", "error")
                    return redirect(url_for('hub_asesor'))
                
                detalles['hora_fin'] = hora_actual_str
                detalles['reporte'] = reporte
                detalles['estado_final'] = 'Atendida'
                cur.execute("UPDATE solicitudes SET estado = 'Completada', detalles = %s WHERE id = %s", (json.dumps(detalles), cita_id))
                registrar_accion_auditoria('FIN_ATENCION_CITA', f"Asesor {g.admin['usuario']} finalizó atención y registró reporte para cita N°{cita_id}.", cita['cliente_id'])
                flash("Atención finalizada y reporte guardado.", "success")
            
            conn.commit()
    except (psycopg2.Error, json.JSONDecodeError) as e:
        conn.rollback()
        if accion == 'iniciar':
            return jsonify({'status': 'error', 'message': str(e)}), 500
        flash(f"Error al registrar la interacción: {e}", "danger")

    return redirect(url_for('hub_asesor'))

@app.route('/ver_reporte_cita/<int:solicitud_id>')
def ver_reporte_cita(solicitud_id):
    is_admin = 'admin_id' in session
    is_cliente = 'cliente_id' in session

    if not is_admin and not is_cliente:
        flash('Acceso no autorizado.', 'error')
        return redirect(url_for('home'))

    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('home'))

    try:
        with conn.cursor() as cur:
            query = """
                SELECT s.id, s.detalles, s.estado,
                       c.nombre || ' ' || c.apellido as nombre_cliente, c.cedula,
                       a.usuario as nombre_asesor
                FROM solicitudes s
                JOIN clientes c ON s.cliente_id = c.id
                LEFT JOIN administradores a ON (s.detalles->>'asesor_id')::int = a.id
                WHERE s.id = %s AND s.tipo_solicitud = 'Cita'
            """
            cur.execute(query, (solicitud_id,))
            cita = cur.fetchone()

            if not cita:
                flash("Reporte de cita no encontrado.", "error")
                return redirect(url_for('home'))

            if is_cliente and cita['cliente_id'] != session['cliente_id']:
                flash('No tienes permiso para ver este reporte.', 'error')
                return redirect(url_for('portal_dashboard'))

            return render_template('ver_reporte_cita.html', cita=cita, is_admin_view=is_admin)

    except psycopg2.Error as e:
        flash(f"Error al cargar el reporte: {e}", "error")
        return redirect(url_for('home'))

# =================================================================================
# --- MÓDULO DE GESTIÓN ADMINISTRATIVA E SOLICITUDES ---
# =================================================================================

@app.route('/gestion_administrativa')
@admin_required
@rol_requerido('superadmin', 'gerente')
def gestion_administrativa():
    conn = get_db()
    counts = { 'pagos_pendientes': 0, 'reportes_pendientes': 0, 'citas': 0, 'congelamientos': 0, 'retiros': 0 }
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM pagos WHERE estado_pago = 'Pendiente' AND (reportado_por_cliente = FALSE OR estado_reporte = 'Aprobado')")
                counts['pagos_pendientes'] = cur.fetchone()[0]
                
                cur.execute("SELECT COUNT(*) FROM pagos WHERE reportado_por_cliente = TRUE AND estado_reporte = 'Pendiente de Revision'")
                counts['reportes_pendientes'] = cur.fetchone()[0]
                
                cur.execute("SELECT tipo_solicitud, COUNT(*) as total FROM solicitudes WHERE estado = 'Pendiente' GROUP BY tipo_solicitud")
                for row in cur.fetchall():
                    if row['tipo_solicitud'] == 'Cita': counts['citas'] = row['total']
                    elif row['tipo_solicitud'] == 'Congelamiento': counts['congelamientos'] = row['total']
                    elif row['tipo_solicitud'] == 'Retiro': counts['retiros'] = row['total']
        except psycopg2.Error as e:
            logging.error(f"Error al contar pendientes en gestion_administrativa: {e}")
            flash("No se pudo obtener el contador de pendientes.", "warning")
            
    return render_template('gestion_administrativa.html', anio_actual=get_venezuela_current_date().year, counts=counts)

@app.route('/solicitudes/hub')
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def solicitudes_hub():
    conn = get_db()
    counts = {'citas': 0, 'congelamientos': 0, 'retiros': 0}
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT tipo_solicitud, COUNT(*) as total FROM solicitudes WHERE estado = 'Pendiente' GROUP BY tipo_solicitud")
                for row in cur.fetchall():
                    if row['tipo_solicitud'] == 'Cita': counts['citas'] = row['total']
                    elif row['tipo_solicitud'] == 'Congelamiento': counts['congelamientos'] = row['total']
                    elif row['tipo_solicitud'] == 'Retiro': counts['retiros'] = row['total']
        except psycopg2.Error as e:
            logging.error(f"Error al contar solicitudes para el hub: {e}")
            flash("No se pudo obtener el contador de solicitudes.", "warning")
    return render_template('solicitudes_hub.html', counts=counts)

@app.route('/solicitudes/citas')
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def gestion_citas():
    conn = get_db()
    solicitudes, administradores, citas_aprobadas = {'citas': []}, [], []
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, usuario FROM administradores WHERE rol IN ('superadmin', 'gerente', 'administradora') ORDER BY usuario")
                administradores = cur.fetchall()
                
                cur.execute("""
                    SELECT s.id, s.fecha_creacion, s.detalles, c.nombre || ' ' || c.apellido as nombre_cliente
                    FROM solicitudes s JOIN clientes c ON s.cliente_id = c.id
                    WHERE s.estado = 'Pendiente' AND s.tipo_solicitud = 'Cita' ORDER BY s.fecha_creacion ASC
                """)
                solicitudes['citas'] = cur.fetchall()

                cur.execute("""
                    SELECT s.id, s.detalles, c.nombre || ' ' || c.apellido as nombre_cliente, a.usuario as nombre_asesor
                    FROM solicitudes s 
                    JOIN clientes c ON s.cliente_id = c.id
                    LEFT JOIN administradores a ON (s.detalles->>'asesor_id')::int = a.id
                    WHERE s.tipo_solicitud = 'Cita' AND s.estado = 'Aprobada' AND (s.detalles->>'fecha_cita')::date >= NOW()::date
                    ORDER BY (s.detalles->>'fecha_cita')::date ASC, (s.detalles->>'hora_cita') ASC
                """)
                citas_aprobadas = cur.fetchall()
        except psycopg2.Error as e:
            logging.error(f"Error al obtener datos para gestión de citas: {e}")
    return render_template('gestion_citas.html', solicitudes=solicitudes, administradores=administradores, citas_aprobadas=citas_aprobadas)

@app.route('/solicitudes/retiros')
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def gestion_retiros():
    conn = get_db()
    solicitudes = {'retiros': []}
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.id, s.fecha_creacion, c.nombre || ' ' || c.apellido as nombre_cliente
                    FROM solicitudes s JOIN clientes c ON s.cliente_id = c.id
                    WHERE s.estado = 'Pendiente' AND s.tipo_solicitud = 'Retiro' ORDER BY s.fecha_creacion ASC
                """)
                solicitudes['retiros'] = cur.fetchall()
        except psycopg2.Error as e:
            logging.error(f"Error al obtener solicitudes de retiro: {e}")
    return render_template('gestion_retiros.html', solicitudes=solicitudes)

@app.route('/solicitudes/congelamientos')
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def gestion_congelamientos():
    conn = get_db()
    solicitudes = {'congelamientos': []}
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.id, s.fecha_creacion, s.detalles, c.nombre || ' ' || c.apellido as nombre_cliente
                    FROM solicitudes s JOIN clientes c ON s.cliente_id = c.id
                    WHERE s.estado = 'Pendiente' AND s.tipo_solicitud = 'Congelamiento' ORDER BY s.fecha_creacion ASC
                """)
                solicitudes['congelamientos'] = cur.fetchall()
        except psycopg2.Error as e:
            logging.error(f"Error al obtener solicitudes de congelamiento: {e}")
    return render_template('gestion_congelamientos.html', solicitudes=solicitudes)

@app.route('/procesar_solicitud/<int:solicitud_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def procesar_solicitud(solicitud_id):
    conn = get_db()
    accion = request.form.get('accion')
    tipo = request.form.get('tipo')

    redirect_map = {
        'Cita': 'gestion_citas',
        'Retiro': 'gestion_retiros',
        'Congelamiento': 'gestion_congelamientos',
        'Descongelamiento': 'gestion_congelamientos'
    }
    
    if not all([conn, accion, tipo]):
        flash("Error en la solicitud.", "danger")
        return redirect(url_for('solicitudes_hub'))

    redirect_url = url_for(redirect_map.get(tipo, 'solicitudes_hub'))
    nuevo_estado_solicitud = 'Aprobada' if accion == 'aprobar' else 'Rechazada'
    
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cliente_id, detalles FROM solicitudes WHERE id = %s", (solicitud_id,))
            solicitud = cur.fetchone()
            if not solicitud:
                flash("La solicitud no existe.", "error")
                return redirect(redirect_url)

            detalles_actualizados = solicitud['detalles'] if solicitud['detalles'] is not None else {}
            cliente_id = solicitud['cliente_id']

            if tipo == 'Cita' and accion == 'aprobar':
                asesor_id = request.form.get('asesor_id')
                if not asesor_id:
                    flash("Debe asignar un asesor para aprobar la cita.", "error")
                    return redirect(redirect_url)
                
                cur.execute("SELECT usuario FROM administradores WHERE id = %s", (asesor_id,))
                asesor = cur.fetchone()
                if not asesor:
                    flash("Asesor no válido.", "error")
                    return redirect(redirect_url)
                
                detalles_actualizados['asesor_id'] = int(asesor_id)
                detalles_actualizados['nombre_asesor'] = asesor['usuario']

            cur.execute(
                "UPDATE solicitudes SET estado = %s, revisado_por_id = %s, fecha_revision = NOW(), detalles = %s WHERE id = %s",
                (nuevo_estado_solicitud, g.admin['id'], json.dumps(detalles_actualizados), solicitud_id)
            )
            
            if accion == 'aprobar':
                if tipo == 'Retiro':
                    cur.execute("UPDATE clientes SET estatus = 'RETIRO' WHERE id = %s", (cliente_id,))
                elif tipo == 'Congelamiento':
                    duracion = detalles_actualizados.get('tiempo_congelamiento', '1 mes')
                    meses = 2 if '2' in duracion else 1
                    fecha_fin = get_venezuela_current_date() + timedelta(days=meses * 30)
                    detalles_actualizados['fecha_fin_congelamiento'] = fecha_fin.isoformat()
                    cur.execute("UPDATE clientes SET estatus = 'CONGELADO' WHERE id = %s", (cliente_id,))
                    cur.execute("UPDATE solicitudes SET detalles = %s WHERE id = %s", (json.dumps(detalles_actualizados), solicitud_id))
                elif tipo == 'Descongelamiento':
                    cur.execute("UPDATE clientes SET estatus = 'ACTIVO' WHERE id = %s", (cliente_id,))

            descripcion_audit = f"{accion.capitalize()} la solicitud de {tipo} N° {solicitud_id}."
            registrar_accion_auditoria('GESTION_SOLICITUD', descripcion_audit, cliente_id)
            
            conn.commit()
            flash(f"La solicitud de {tipo} ha sido marcada como '{nuevo_estado_solicitud}'.", 'success')
    except (psycopg2.Error, json.JSONDecodeError) as e:
        conn.rollback()
        logging.error(f"Error al procesar solicitud {solicitud_id}: {e}")
        flash(f"Error al procesar la solicitud: {e}", "error")
    
    return redirect(redirect_url)

@app.route('/solicitudes/cancelar_cita_admin/<int:solicitud_id>', methods=['POST'])
@admin_required
def cancelar_cita_admin(solicitud_id):
    conn = get_db()
    origin = request.form.get('origin', 'gestion_citas')
    redirect_url = url_for(origin) if origin in ['hub_asesor', 'gestion_citas'] else url_for('gestion_citas')

    if not conn:
        flash("Error de conexión.", "danger")
        return redirect(redirect_url)
    
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cliente_id, estado FROM solicitudes WHERE id = %s", (solicitud_id,))
            solicitud = cur.fetchone()

            if not solicitud:
                flash("La cita que intenta cancelar no existe.", "error")
                return redirect(redirect_url)

            if solicitud['estado'] not in ['Aprobada', 'Pendiente']:
                flash("Esta cita no se puede cancelar porque ya ha sido procesada o cancelada.", "warning")
                return redirect(redirect_url)

            cur.execute("UPDATE solicitudes SET estado = 'Cancelada', revisado_por_id = %s, fecha_revision = NOW() WHERE id = %s", (g.admin['id'], solicitud_id))
            
            descripcion_audit = f"Canceló (admin) la solicitud de Cita N° {solicitud_id}."
            registrar_accion_auditoria('CANCELACION_CITA_ADMIN', descripcion_audit, solicitud['cliente_id'])
            
            conn.commit()
            flash("La cita ha sido cancelada exitosamente.", "success")

    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Error al cancelar la cita: {e}", "error")

    return redirect(redirect_url)

# =================================================================================
# ===== NUEVO FLUJO DE VALIDACIÓN Y CONCILIACIÓN =====
# =================================================================================

@app.route('/reportes_por_revisar')
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def reportes_por_revisar():
    from flask import current_app

    conn = get_db()
    reportes_a_revisar = []
    anio_actual = get_venezuela_current_date().year

    if not conn:
        flash("Error de conexión con la base de datos.", "danger")
        return render_template('reportes_por_revisar.html', reportes=reportes_a_revisar, anio_actual=anio_actual)

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.id, p.monto, p.monto_bs, p.tipo_pago, p.fecha_creacion,
                       p.cliente_id, c.nombre, c.apellido, c.cedula
                FROM pagos p
                JOIN clientes c ON p.cliente_id = c.id
                WHERE p.reportado_por_cliente = TRUE AND p.estado_reporte = 'Pendiente de Revision'
                ORDER BY p.fecha_creacion ASC;
            """)
            reportes_a_revisar = cur.fetchall()

    except psycopg2.Error as e:
        current_app.logger.exception("Error al obtener reportes por revisar")
        flash("Error al cargar la lista de reportes pendientes de revisión.", "danger")
        return render_template('reportes_por_revisar.html', reportes=[], anio_actual=anio_actual)

    return render_template('reportes_por_revisar.html', reportes=reportes_a_revisar, anio_actual=anio_actual)


@app.route('/procesar_reporte/<int:pago_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def procesar_reporte(pago_id):
    conn = get_db()
    accion = request.form.get('accion')
    
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('reportes_por_revisar'))

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cliente_id, monto, monto_bs, tasa_dia, tipo_pago FROM pagos WHERE id = %s", (pago_id,))
            pago = cur.fetchone()
            if not pago:
                flash("El pago no existe.", "error")
                return redirect(url_for('reportes_por_revisar'))
            cliente_id = pago['cliente_id']

            if accion == 'aprobar':
                descripcion_audit = f"Aprobó el reporte de pago N° {pago_id}."
                
                # --- CAMBIO INSERTADO ---
                # Ya no se crea un Bulk para un pago normal aprobado.
                # Simplemente se actualiza el estado del reporte, dejando el pago listo para conciliar.
                cur.execute(
                    "UPDATE pagos SET estado_reporte = 'Aprobado', revisado_por_id = %s, fecha_revision = NOW() WHERE id = %s",
                    (g.admin['id'], pago_id)
                )

            elif accion == 'rechazar':
                motivo = request.form.get('motivo_rechazo')
                
                if motivo == 'Diferencia de Monto':
                    monto_recibido_str = request.form.get('diferencia_monto', '0').replace(',', '.')
                    monto_recibido = Decimal(monto_recibido_str) if monto_recibido_str else Decimal('0')
                    
                    monto_reportado = pago['monto_bs'] or pago['monto']
                    
                    if monto_recibido <= 0 or monto_recibido >= monto_reportado:
                        flash("El monto recibido debe ser mayor a cero y menor que el monto reportado.", "error")
                        return redirect(url_for('reportes_por_revisar'))

                    # 1. Crear el Bulk que agrupará ambos pagos (ESTA LÓGICA SE MANTIENE IGUAL)
                    cur.execute("""
                        INSERT INTO payment_bulks (cliente_id, currency, expected_amount, status)
                        VALUES (%s, %s, %s, 'UNDER_REVIEW') RETURNING id
                    """, (cliente_id, 'VES' if pago['monto_bs'] else 'USD', monto_reportado))
                    bulk_id = cur.fetchone()[0]

                    # 2. Actualizar el pago original (parcial)
                    tasa = pago.get('tasa_dia') or Decimal('1.0')
                    monto_recibido_usd = monto_recibido / tasa if tasa > 0 and pago['monto_bs'] else monto_recibido
                    
                    detalles_rechazo = {
                        'motivo': motivo,
                        'monto_original_reportado': str(monto_reportado),
                        'monto_recibido_real': str(monto_recibido)
                    }
                    
                    cur.execute(
                        "UPDATE pagos SET monto = %s, monto_bs = %s, estado_reporte = 'Inconsistente', revisado_por_id = %s, fecha_revision = NOW(), detalles_reporte = %s, bulk_id = %s WHERE id = %s",
                        (monto_recibido_usd, monto_recibido if pago['monto_bs'] else None, g.admin['id'], json.dumps(detalles_rechazo), bulk_id, pago_id)
                    )

                    # 3. Crear la nueva orden de pago por la diferencia
                    monto_pendiente = monto_reportado - monto_recibido
                    monto_pendiente_usd = monto_pendiente / tasa if tasa > 0 and pago['monto_bs'] else monto_pendiente
                    
                    cur.execute("""
                        INSERT INTO payment_orders (bulk_id, cliente_id, amount, currency, status)
                        VALUES (%s, %s, %s, %s, 'ISSUED')
                    """, (bulk_id, cliente_id, monto_pendiente, 'VES' if pago['monto_bs'] else 'USD'))

                    # 4. Recalcular totales del Bulk
                    recalcular_totales_bulk(bulk_id)
                    
                    descripcion_audit = f"Rechazó reporte #{pago_id} por diferencia. Se creó Bulk #{bulk_id} y orden por la diferencia."

                else: # Otro motivo de rechazo
                    descripcion_audit = f"Rechazó el reporte N° {pago_id}. Motivo: {motivo}."
                    cur.execute(
                        "UPDATE pagos SET estado_reporte = 'Inconsistente', revisado_por_id = %s, fecha_revision = NOW(), detalles_reporte = %s WHERE id = %s",
                        (g.admin['id'], json.dumps({'motivo': motivo}), pago_id)
                    )
            else:
                flash('Acción no válida.', 'error')
                return redirect(url_for('reportes_por_revisar'))

            registrar_accion_auditoria('REVISION_REPORTE_PAGO', descripcion_audit, cliente_id)
            conn.commit()
            flash(f"El reporte de pago ha sido procesado exitosamente.", 'success')

    except (psycopg2.Error, ValueError, InvalidOperation) as e:
        conn.rollback()
        flash(f"Error al procesar el reporte: {e}", "error")
    
    return redirect(url_for('reportes_por_revisar'))


@app.route('/pagos_por_conciliar')
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def pagos_por_conciliar():
    conn = get_db()
    pagos_a_conciliar = []
    bulks_a_conciliar = []
    
    if not conn:
        flash("Error de conexión con la base de datos.", "danger")
        return render_template('pagos_por_conciliar.html', pagos=pagos_a_conciliar, bulks=bulks_a_conciliar, anio_actual=get_venezuela_current_date().year)
    
    try:
        with conn.cursor() as cur:
            # --- CAMBIO: OBTENER PAGOS INDIVIDUALES ---
            # Busca pagos normales que fueron aprobados y están listos.
            cur.execute("""
                SELECT p.*, c.nombre, c.apellido, c.cedula
                FROM pagos p JOIN clientes c ON p.cliente_id = c.id
                WHERE p.estado_reporte = 'Aprobado' AND p.estado_pago = 'Pendiente' AND p.bulk_id IS NULL
                ORDER BY p.fecha_creacion ASC;
            """)
            pagos_a_conciliar = cur.fetchall()

            # --- CAMBIO: OBTENER BULKS ---
            # Busca bulks que contienen diferencias y están listos.
            cur.execute("""
                SELECT b.*, c.nombre, c.apellido
                FROM payment_bulks b JOIN clientes c ON b.cliente_id = c.id
                WHERE b.status = 'READY_TO_RECONCILE' 
                ORDER BY b.updated_at ASC;
            """)
            bulks_a_conciliar = cur.fetchall()

    except psycopg2.Error as e:
        logging.error(f"Error al obtener datos para conciliar: {e}")
        flash("Error al cargar la lista de pagos y lotes por conciliar.", "danger")
    
    # Se envían ambas listas a la plantilla.
    return render_template('pagos_por_conciliar.html', 
                           pagos=pagos_a_conciliar, 
                           bulks=bulks_a_conciliar, 
                           anio_actual=get_venezuela_current_date().year)



# =================================================================================
# ===== MÓDULO DE TESORERÍA, COMERCIAL Y REPORTES =====
# =================================================================================

@app.route('/tesoreria/rebalanceo', methods=['GET', 'POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def tesoreria_rebalanceo():
    conn = get_db()
    balances_actuales = calcular_balances_tesoreria() 
    historial_movimientos = []
    if conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT op.*, admin.usuario as nombre_admin, op.perdida_cambiaria
                FROM operaciones_tesoreria op
                LEFT JOIN administradores admin ON op.realizada_por = admin.id
                ORDER BY op.fecha_operacion DESC LIMIT 30
            """)
            historial_movimientos = cur.fetchall()
    if request.method == 'POST':
        try:
            form = request.form
            tipo_operacion, nota, caja_origen = form.get('tipo_operacion'), form.get('nota'), form.get('caja_origen')
            monto_origen_str, moneda_origen = form.get('monto_origen', '0').replace(',', '.'), form.get('moneda_origen')
            monto_origen = Decimal(monto_origen_str)
            if not all([tipo_operacion, nota, caja_origen, monto_origen > 0]):
                flash("Error: Tipo, Nota, Caja Origen y Monto son obligatorios.", 'danger')
                return redirect(url_for('tesoreria_rebalanceo'))
            if balances_actuales.get(caja_origen, Decimal('0.0')) < monto_origen:
                flash(f"Error: Fondos insuficientes en '{caja_origen}'.", 'danger')
                return redirect(url_for('tesoreria_rebalanceo'))
            
            tasa_aplicada_str = form.get('tasa_aplicada', '0').replace(',', '.'),
            tasa_aplicada = Decimal(tasa_aplicada_str) if tasa_aplicada_str and tasa_aplicada_str != '0' else None
            perdida_cambiaria = Decimal('0.0')

            if tipo_operacion in ['PAGO_GASTO', 'PAGO_NOMINA']:
                caja_destino, monto_destino, moneda_destino = 'GASTO_OPERATIVO', monto_origen, moneda_origen
                if tipo_operacion == 'PAGO_NOMINA' and moneda_origen == 'BS' and 'USD' in caja_origen:
                    with conn.cursor() as cur_tasa:
                        cur_tasa.execute("SELECT tasa FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (get_venezuela_current_date(),))
                        tasa_bcv_row = cur_tasa.fetchone()
                        tasa_bcv = tasa_bcv_row['tasa'] if tasa_bcv_row and tasa_bcv_row['tasa'] else None
                    if not tasa_bcv or tasa_bcv <= 0:
                        flash("Error: No se encontró una tasa BCV válida para hoy. No se puede procesar el pago de nómina en Bs.", "danger")
                        return redirect(url_for('tesoreria_rebalanceo'))
                    
                    monto_egreso_en_usd = monto_origen / tasa_bcv
                    perdida_cambiaria = (monto_origen / tasa_bcv) - (monto_origen / tasa_aplicada) if tasa_aplicada and tasa_aplicada > 0 else Decimal('0.0')
                    
                    monto_origen, moneda_origen = monto_egreso_en_usd, 'USD'
                    monto_destino, moneda_destino = monto_egreso_en_usd, 'USD'
                    nota += f" (Pago original: {monto_origen_str} Bs @ Tasa {tasa_bcv})"
            else: 
                caja_destino, monto_destino_str = form.get('caja_destino'), form.get('monto_destino', '0').replace(',', '.')
                moneda_destino = form.get('moneda_destino')
                monto_destino = Decimal(monto_destino_str) if monto_destino_str and monto_destino_str != '0' else None
                if not all([caja_destino, monto_destino, moneda_destino]):
                    flash("Error: Para transferencias, el destino es obligatorio.", 'danger')
                    return redirect(url_for('tesoreria_rebalanceo'))
                
                if tipo_operacion == 'COMPRA_DIVISAS' and moneda_origen == 'BS' and 'USD' in moneda_destino:
                    with conn.cursor() as cur_tasa:
                        cur_tasa.execute("SELECT tasa FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (get_venezuela_current_date(),))
                        tasa_bcv_row = cur_tasa.fetchone()
                        tasa_bcv = tasa_bcv_row['tasa'] if tasa_bcv_row and tasa_bcv_row['tasa'] else None
                    if tasa_bcv and tasa_bcv > 0:
                        valor_real_en_usd_bcv = monto_origen / tasa_bcv
                        valor_obtenido_en_usd = monto_destino
                        perdida_cambiaria = valor_real_en_usd_bcv - valor_obtenido_en_usd
            
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO operaciones_tesoreria 
                    (tipo_operacion, caja_origen, moneda_origen, monto_origen, caja_destino, moneda_destino, monto_destino, tasa_aplicada, nota, realizada_por, fecha_operacion, perdida_cambiaria)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
                """, (tipo_operacion, caja_origen, moneda_origen, monto_origen, caja_destino, moneda_destino, monto_destino, tasa_aplicada, nota, g.admin['id'], perdida_cambiaria))
            
            descripcion = f"Tesoreria: {tipo_operacion} de {monto_origen:,.2f} {moneda_origen} desde {caja_origen}."
            registrar_accion_auditoria('MOVIMIENTO_TESORERIA', descripcion)
            conn.commit()
            flash('Movimiento de tesorería registrado exitosamente.', 'success')
        except (InvalidOperation, ValueError):
            flash("Error: Verifique que los montos y tasas sean números válidos.", 'danger')
        except psycopg2.Error as e:
            conn.rollback()
            flash(f"Error de base de datos: {e}", "danger")
        return redirect(url_for('tesoreria_rebalanceo'))

    return render_template('tesoreria_rebalanceo.html', balances=balances_actuales, historial=historial_movimientos, anio_actual=get_venezuela_current_date().year)

# >>> COMISIONES: BEGIN [dashboard_comercial]
@app.route('/comercial/dashboard', methods=['GET'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def dashboard_comercial():
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", "danger")
        return render_template('dashboard_comercial.html', anio_actual=get_venezuela_current_date().year)

    # --- Obtener filtros de la URL ---
    args = request.args
    today = get_venezuela_current_date()
    # Rango de fechas de origen de la comisión
    fecha_desde_origen = args.get('fecha_desde_origen', (today - timedelta(days=30)).strftime('%Y-%m-%d'))
    fecha_hasta_origen = args.get('fecha_hasta_origen', today.strftime('%Y-%m-%d'))
    # Rango de fechas de pago
    fecha_desde_pago = args.get('fecha_desde_pago')
    fecha_hasta_pago = args.get('fecha_hasta_pago')
    # Otros filtros
    asesor_id = args.get('asesor_id')
    estado = args.get('estado')
    moneda = args.get('moneda')
    lote_id = args.get('lote_id')

    # --- Construcción de la consulta SQL ---
    base_query = """
        SELECT 
            c.id, c.fecha_origen, a.usuario as asesor, cl.nombre || ' ' || cl.apellido as cliente,
            cl.plan_contratado, c.moneda, c.tasa_bcv_usada, c.base, c.pct_comision, c.pct_split,
            c.monto, c.estado, c.payment_batch_id, c.notas,
            (SELECT SUM(monto_ajuste) FROM comisiones_rebalanceos cr WHERE cr.comision_id_origen = c.id) as total_ajustes
        FROM comisiones c
        JOIN administradores a ON c.asesor_id = a.id
        LEFT JOIN clientes cl ON c.origen_id = cl.id AND c.origen_tipo = 'Venta'
    """
    
    filters = []
    params = []

    if fecha_desde_origen:
        filters.append("c.fecha_origen >= %s")
        params.append(fecha_desde_origen)
    if fecha_hasta_origen:
        filters.append("c.fecha_origen <= %s")
        params.append(fecha_hasta_origen)
    if fecha_desde_pago:
        filters.append("c.paid_at >= %s")
        params.append(fecha_desde_pago)
    if fecha_hasta_pago:
        filters.append("c.paid_at <= %s")
        params.append(fecha_hasta_pago)
    if asesor_id:
        filters.append("c.asesor_id = %s")
        params.append(asesor_id)
    if estado:
        filters.append("c.estado = %s")
        params.append(estado)
    if moneda:
        filters.append("c.moneda = %s")
        params.append(moneda)
    if lote_id:
        filters.append("c.payment_batch_id = %s")
        params.append(lote_id)

    if filters:
        base_query += " WHERE " + " AND ".join(filters)
    
    base_query += " ORDER BY c.fecha_origen DESC"

    # --- Ejecución de consultas ---
    comisiones, asesores, lotes = [], [], []
    stats = defaultdict(lambda: {'monto': Decimal('0.0'), 'conteo': 0})
    
    try:
        with conn.cursor() as cur:
            cur.execute(base_query, tuple(params))
            comisiones = cur.fetchall()
            
            # Calcular estadísticas para las tarjetas
            cur.execute("SELECT estado, moneda, SUM(monto) as total_monto, COUNT(id) as total_conteo FROM comisiones GROUP BY estado, moneda")
            stats_db = cur.fetchall()
            for row in stats_db:
                # Se simplifica a USD para el ejemplo. Una versión real sumaría Bs con tasa.
                if row['moneda'] == 'USD':
                    stats[row['estado']]['monto'] += row['total_monto']
                    stats[row['estado']]['conteo'] += row['total_conteo']
            
            cur.execute("SELECT SUM(monto_ajuste) FROM comisiones_rebalanceos")
            total_rebalanceos = cur.fetchone()[0]
            stats['rebalanceos']['monto'] = total_rebalanceos or Decimal('0.0')

            # Obtener listas para filtros
            cur.execute("SELECT id, usuario FROM administradores ORDER BY usuario")
            asesores = cur.fetchall()
            cur.execute("SELECT id, created_at FROM comisiones_lotes_pago ORDER BY created_at DESC")
            lotes = cur.fetchall()
            
    except psycopg2.Error as e:
        flash(f"Error al cargar el dashboard de comisiones: {e}", "danger")

    return render_template(
        'dashboard_comercial.html',
        comisiones=comisiones,
        stats=stats,
        asesores=asesores,
        lotes=lotes,
        filters={
            'fecha_desde_origen': fecha_desde_origen, 'fecha_hasta_origen': fecha_hasta_origen,
            'fecha_desde_pago': fecha_desde_pago, 'fecha_hasta_pago': fecha_hasta_pago,
            'asesor_id': asesor_id, 'estado': estado, 'moneda': moneda, 'lote_id': lote_id
        },
        anio_actual=get_venezuela_current_date().year
    )
# >>> COMISIONES: END [dashboard_comercial]


# >>> COMISIONES: BEGIN [endpoints_api]
@app.route('/comercial/api/comision_detalle/<int:comision_id>')
@admin_required
@rol_requerido('superadmin', 'gerente')
def get_comision_detalle(comision_id):
    conn = get_db()
    if not conn:
        return jsonify({'error': 'Error de conexión'}), 500
    try:
        with conn.cursor() as cur:
            # Consulta para obtener el detalle principal
            cur.execute("""
                SELECT 
                    c.*, a.usuario as asesor_nombre, cl.nombre || ' ' || cl.apellido as cliente_nombre,
                    (SELECT usuario FROM administradores WHERE id = c.approved_by) as approver_name,
                    (SELECT usuario FROM administradores WHERE id = c.paid_by) as payer_name,
                    lp.id as lote_id, lp.created_at as lote_fecha
                FROM comisiones c
                JOIN administradores a ON c.asesor_id = a.id
                LEFT JOIN clientes cl ON c.origen_id = cl.id AND c.origen_tipo = 'Venta'
                LEFT JOIN comisiones_lotes_pago lp ON c.payment_batch_id = lp.id
                WHERE c.id = %s
            """, (comision_id,))
            comision = cur.fetchone()
            if not comision:
                return jsonify({'error': 'Comisión no encontrada'}), 404

            # Consulta para obtener el split (si aplica)
            cur.execute("""
                SELECT a.usuario as asesor_split, c_split.pct_split, c_split.monto
                FROM comisiones c_split
                JOIN administradores a ON c_split.asesor_id = a.id
                WHERE c_split.origen_id = %s AND c_split.origen_tipo = %s
            """, (comision['origen_id'], comision['origen_tipo']))
            splits = cur.fetchall()

            # Consulta para obtener el historial de auditoría
            cur.execute("""
                SELECT timestamp, usuario_nombre, accion, descripcion, ip_address
                FROM registros_auditoria
                WHERE detalles->>'comision_id' = %s
                ORDER BY timestamp ASC
            """, (str(comision_id),))
            auditoria = cur.fetchall()

            # Formatear la respuesta
            comision_dict = {k: str(v) if isinstance(v, (Decimal, datetime, date)) else v for k, v in dict(comision).items()}
            splits_list = [{k: str(v) if isinstance(v, Decimal) else v for k, v in dict(s).items()} for s in splits]
            auditoria_list = [{k: str(v) if isinstance(v, datetime) else v for k, v in dict(a).items()} for a in auditoria]
            
            return jsonify({
                'comision': comision_dict,
                'splits': splits_list,
                'auditoria': auditoria_list
            })
    except psycopg2.Error as e:
        logging.error(f"Error en API get_comision_detalle: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/comercial/api/aprobar_comisiones', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def aprobar_comisiones():
    conn = get_db()
    comision_ids = request.json.get('ids', [])
    if not conn or not comision_ids:
        return jsonify({'status': 'error', 'message': 'Datos inválidos'}), 400
    
    try:
        with conn.cursor() as cur:
            # Validar que los splits sumen 100% para cada origen
            placeholders = ','.join(['%s'] * len(comision_ids))
            cur.execute(f"""
                SELECT origen_id, origen_tipo, SUM(pct_split) as total_split
                FROM comisiones
                WHERE origen_id IN (SELECT origen_id FROM comisiones WHERE id IN ({placeholders}))
                GROUP BY origen_id, origen_tipo
            """, tuple(comision_ids))
            
            splits_a_validar = cur.fetchall()
            for split in splits_a_validar:
                if not (Decimal('99.99') <= split['total_split'] <= Decimal('100.01')):
                    msg = f"El split para el origen {split['origen_tipo']} #{split['origen_id']} suma {split['total_split']}%, no 100%. No se puede aprobar."
                    return jsonify({'status': 'error', 'message': msg}), 400

            # Actualizar estado
            cur.execute(f"""
                UPDATE comisiones
                SET estado = 'aprobado', approved_at = NOW(), approved_by = %s
                WHERE id IN ({placeholders}) AND estado = 'pendiente'
            """, (g.admin['id'],) + tuple(comision_ids))
            
            updated_rows = cur.rowcount
            
            # Registrar auditoría
            for com_id in comision_ids:
                registrar_accion_auditoria(
                    'APROBACION_COMISION', 
                    f"Aprobó la comisión ID {com_id}",
                    detalles_adicionales={'comision_id': com_id}
                )
            
            conn.commit()
            return jsonify({'status': 'success', 'message': f'{updated_rows} comisiones aprobadas.'})
    except psycopg2.Error as e:
        conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500
# >>> COMISIONES: END [endpoints_api]


# >>> COMISIONES: BEGIN [endpoints_lotes]
@app.route('/comercial/lotes/generar', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def generar_lote_pago():
    conn = get_db()
    comision_ids = request.json.get('ids', [])
    if not conn or not comision_ids:
        return jsonify({'status': 'error', 'message': 'Debe seleccionar comisiones para generar un lote.'}), 400

    try:
        with conn.cursor() as cur:
            placeholders = ','.join(['%s'] * len(comision_ids))
            
            # Verificar que todas las comisiones estén aprobadas y sin lote
            cur.execute(f"SELECT COUNT(*) FROM comisiones WHERE id IN ({placeholders}) AND (estado != 'aprobado' OR payment_batch_id IS NOT NULL)", tuple(comision_ids))
            if cur.fetchone()[0] > 0:
                return jsonify({'status': 'error', 'message': 'Solo se pueden incluir comisiones aprobadas y sin lote previo.'}), 400

            # Crear el lote
            cur.execute("""
                INSERT INTO comisiones_lotes_pago (created_by, notas)
                VALUES (%s, 'Lote generado automáticamente') RETURNING id
            """, (g.admin['id'],))
            lote_id = cur.fetchone()['id']
            
            # Asociar comisiones al lote y actualizar totales
            cur.execute(f"""
                UPDATE comisiones SET payment_batch_id = %s WHERE id IN ({placeholders})
            """, (lote_id,) + tuple(comision_ids))
            
            cur.execute("""
                UPDATE comisiones_lotes_pago lp
                SET total_items = agg.total_items,
                    total_monto_usd = agg.total_monto_usd,
                    total_monto_bs = agg.total_monto_bs,
                    periodo_desde = agg.min_fecha,
                    periodo_hasta = agg.max_fecha
                FROM (
                    SELECT
                        COUNT(id) as total_items,
                        COALESCE(SUM(CASE WHEN moneda = 'USD' THEN monto ELSE 0 END), 0) as total_monto_usd,
                        COALESCE(SUM(CASE WHEN moneda = 'Bs' THEN monto ELSE 0 END), 0) as total_monto_bs,
                        MIN(fecha_origen) as min_fecha,
                        MAX(fecha_origen) as max_fecha
                    FROM comisiones
                    WHERE payment_batch_id = %s
                ) as agg
                WHERE lp.id = %s
            """, (lote_id, lote_id))

            registrar_accion_auditoria('GENERACION_LOTE_PAGO', f"Generó el lote de pago #{lote_id} con {len(comision_ids)} comisiones.")
            conn.commit()
            return jsonify({'status': 'success', 'message': f'Lote de pago #{lote_id} generado exitosamente.', 'lote_id': lote_id})

    except psycopg2.Error as e:
        conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/comercial/lotes/detalle/<int:lote_id>')
@admin_required
@rol_requerido('superadmin', 'gerente')
def get_lote_detalle(lote_id):
    conn = get_db()
    if not conn:
        return jsonify({'error': 'Error de conexión'}), 500
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM comisiones_lotes_pago WHERE id = %s", (lote_id,))
            lote = cur.fetchone()
            if not lote:
                return jsonify({'error': 'Lote no encontrado'}), 404
            
            cur.execute("""
                SELECT a.usuario as asesor, c.moneda, SUM(c.monto) as total_monto
                FROM comisiones c
                JOIN administradores a ON c.asesor_id = a.id
                WHERE c.payment_batch_id = %s
                GROUP BY a.usuario, c.moneda
                ORDER BY a.usuario
            """, (lote_id,))
            resumen_por_asesor = cur.fetchall()

            lote_dict = {k: str(v) if isinstance(v, (Decimal, date)) else v for k, v in dict(lote).items()}
            resumen_list = [{k: str(v) if isinstance(v, Decimal) else v for k,v in dict(r).items()} for r in resumen_por_asesor]
            
            return jsonify({'lote': lote_dict, 'resumen': resumen_list})
    except psycopg2.Error as e:
        return jsonify({'error': str(e)}), 500

@app.route('/comercial/lotes/pagar/<int:lote_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def pagar_lote(lote_id):
    conn = get_db()
    data = request.json
    metodo_pago = data.get('metodo_pago')
    referencia = data.get('referencia')

    if not conn or not metodo_pago:
        return jsonify({'status': 'error', 'message': 'Datos inválidos'}), 400
    
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM comisiones_lotes_pago WHERE id = %s AND paid_at IS NULL", (lote_id,))
            if not cur.fetchone():
                return jsonify({'status': 'error', 'message': 'El lote no existe o ya fue pagado.'}), 400

            cur.execute("""
                UPDATE comisiones
                SET estado = 'pagado', paid_at = NOW(), paid_by = %s
                WHERE payment_batch_id = %s AND estado = 'aprobado'
            """, (g.admin['id'], lote_id))
            
            cur.execute("""
                UPDATE comisiones_lotes_pago
                SET paid_at = NOW(), paid_by = %s, payment_method = %s, payment_reference = %s
                WHERE id = %s
            """, (g.admin['id'], metodo_pago, referencia, lote_id))

            registrar_accion_auditoria('PAGO_LOTE_COMISIONES', f"Marcó como pagado el lote #{lote_id} (Método: {metodo_pago}).")
            conn.commit()
            return jsonify({'status': 'success', 'message': f'Lote #{lote_id} pagado exitosamente.'})

    except psycopg2.Error as e:
        conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500
# >>> COMISIONES: END [endpoints_lotes]


# >>> COMISIONES: BEGIN [endpoints_rebalanceo]
@app.route('/comercial/rebalanceo/crear', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def crear_rebalanceo():
    conn = get_db()
    data = request.form
    try:
        comision_id = int(data.get('comision_id_origen'))
        monto_ajuste = Decimal(data.get('monto_ajuste'))
        motivo = data.get('motivo')
        notas = data.get('notas')

        if not all([comision_id, motivo]):
            flash('Faltan datos para crear el rebalanceo.', 'danger')
            return redirect(url_for('dashboard_comercial'))

        with conn.cursor() as cur:
            cur.execute("SELECT asesor_id, moneda FROM comisiones WHERE id = %s", (comision_id,))
            comision_origen = cur.fetchone()
            if not comision_origen:
                flash('La comisión de origen no existe.', 'danger')
                return redirect(url_for('dashboard_comercial'))

            cur.execute("""
                INSERT INTO comisiones_rebalanceos (comision_id_origen, asesor_id, monto_ajuste, moneda, motivo, notas, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (comision_id, comision_origen['asesor_id'], monto_ajuste, comision_origen['moneda'], motivo, notas, g.admin['id']))
            
            # También se inserta un registro en la tabla principal de comisiones para visibilidad
            cur.execute("""
                INSERT INTO comisiones (origen_id, origen_tipo, asesor_id, moneda, monto, estado, notas, fecha_origen, pct_comision, pct_split, base)
                VALUES (%s, 'Rebalanceo', %s, %s, %s, 'aprobado', %s, NOW()::date, 0, 0, 0)
            """, (comision_id, comision_origen['asesor_id'], comision_origen['moneda'], monto_ajuste, f"Ajuste: {motivo} - {notas}"))

            registrar_accion_auditoria('CREACION_REBALANCEO', f"Creó ajuste de {monto_ajuste} {comision_origen['moneda']} para comisión #{comision_id}. Motivo: {motivo}")
            conn.commit()
            flash('Rebalanceo creado exitosamente.', 'success')

    except (psycopg2.Error, ValueError, InvalidOperation) as e:
        conn.rollback()
        flash(f'Error al crear rebalanceo: {e}', 'danger')
    
    return redirect(url_for('dashboard_comercial'))
# >>> COMISIONES: END [endpoints_rebalanceo]


# >>> COMISIONES: BEGIN [endpoints_exportacion]
@app.route('/comercial/exportar')
@admin_required
@rol_requerido('superadmin', 'gerente')
def exportar_comisiones():
    conn = get_db()
    if not conn:
        return "Error de DB", 500

    # Reutilizar la lógica de filtros del dashboard
    args = request.args
    today = get_venezuela_current_date()
    fecha_desde_origen = args.get('fecha_desde_origen', (today - timedelta(days=30)).strftime('%Y-%m-%d'))
    fecha_hasta_origen = args.get('fecha_hasta_origen', today.strftime('%Y-%m-%d'))
    asesor_id = args.get('asesor_id')
    estado = args.get('estado')
    lote_id = args.get('lote_id')
    formato = args.get('formato', 'csv').lower()

    base_query = """
        SELECT 
            c.id as "ID Comisión", c.fecha_origen as "Fecha Origen", a.usuario as "Asesor", 
            cl.nombre || ' ' || cl.apellido as "Cliente", cl.plan_contratado as "Plan",
            c.moneda as "Moneda", c.tasa_bcv_usada as "Tasa BCV", c.base as "Base Comisión",
            c.pct_comision as "% Comisión", c.pct_split as "% Split", c.monto as "Monto Comisión",
            c.estado as "Estado", c.payment_batch_id as "Lote Pago", c.paid_at as "Fecha Pago"
        FROM comisiones c
        JOIN administradores a ON c.asesor_id = a.id
        LEFT JOIN clientes cl ON c.origen_id = cl.id AND c.origen_tipo = 'Venta'
    """
    filters, params = [], []
    if fecha_desde_origen: filters.append("c.fecha_origen >= %s"); params.append(fecha_desde_origen)
    if fecha_hasta_origen: filters.append("c.fecha_origen <= %s"); params.append(fecha_hasta_origen)
    if asesor_id: filters.append("c.asesor_id = %s"); params.append(asesor_id)
    if estado: filters.append("c.estado = %s"); params.append(estado)
    if lote_id: filters.append("c.payment_batch_id = %s"); params.append(lote_id)

    if filters:
        base_query += " WHERE " + " AND ".join(filters)
    
    try:
        df = pd.read_sql_query(base_query, conn, params=tuple(params))
        
        # Calcular totales
        total_usd = df[df['Moneda'] == 'USD']['Monto Comisión'].sum()
        total_bs = df[df['Moneda'] == 'Bs']['Monto Comisión'].sum()
        
        filename = f"reporte_comisiones_{today.strftime('%Y%m%d')}"

        if formato == 'csv':
            output = io.StringIO()
            df.to_csv(output, index=False, decimal='.', sep=';', encoding='utf-8-sig')
            # Añadir totales al final
            output.write("\n\nResumen de Totales\n")
            output.write(f"Total USD;{total_usd:.2f}\n")
            output.write(f"Total Bs;{total_bs:.2f}\n")
            
            return Response(
                output.getvalue(),
                mimetype="text/csv",
                headers={"Content-disposition": f"attachment; filename={filename}.csv"}
            )
        elif formato == 'xlsx':
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                df.to_excel(writer, sheet_name='Comisiones', index=False)
                # Añadir totales
                workbook = writer.book
                worksheet = writer.sheets['Comisiones']
                worksheet.write(len(df) + 2, 0, 'Total USD')
                worksheet.write(len(df) + 2, 1, total_usd)
                worksheet.write(len(df) + 3, 0, 'Total Bs')
                worksheet.write(len(df) + 3, 1, total_bs)

            return Response(
                output.getvalue(),
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-disposition": f"attachment; filename={filename}.xlsx"}
            )
        elif formato == 'pdf':
            pdf = FPDF(orientation='L', unit='mm', format='A4')
            pdf.add_page()
            pdf.set_font('Arial', 'B', 12)
            pdf.cell(0, 10, 'Reporte de Comisiones', 0, 1, 'C')
            
            pdf.set_font('Arial', 'B', 8)
            col_widths = [15, 25, 35, 35, 20, 15, 20, 20, 15, 15, 25, 20, 15]
            for i, header in enumerate(df.columns):
                pdf.cell(col_widths[i], 10, header, 1, 0, 'C')
            pdf.ln()

            pdf.set_font('Arial', '', 8)
            for index, row in df.iterrows():
                for i, col in enumerate(df.columns):
                    val = str(row[col]) if pd.notna(row[col]) else ''
                    pdf.cell(col_widths[i], 6, val, 1)
                pdf.ln()

            pdf.ln(10)
            pdf.set_font('Arial', 'B', 10)
            pdf.cell(0, 10, f'Total USD: {total_usd:,.2f}', 0, 1)
            pdf.cell(0, 10, f'Total Bs: {total_bs:,.2f}', 0, 1)

            return Response(pdf.output(dest='S').encode('latin-1'),
                            mimetype='application/pdf',
                            headers={'Content-Disposition': f'attachment;filename={filename}.pdf'})

    except Exception as e:
        flash(f"Error al exportar: {e}", "danger")
        return redirect(url_for('dashboard_comercial'))
    
    return redirect(url_for('dashboard_comercial'))

# >>> COMISIONES: END [endpoints_exportacion]

@app.route('/comercial/pagar_nomina', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def pagar_nomina_comercial():
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", "danger")
        return redirect(url_for('dashboard_comercial'))
    try:
        beneficiarios_a_pagar = request.form.getlist('beneficiarios')
        cajas_origen_seleccionadas = request.form.getlist('cajas_origen')
        if not beneficiarios_a_pagar or len(beneficiarios_a_pagar) != len(cajas_origen_seleccionadas):
            flash("Error en los datos del formulario. Intente de nuevo.", "danger")
            return redirect(url_for('dashboard_comercial'))
        pagos_planificados, deducciones_por_caja = [], {}
        with conn.cursor() as cur:
            for i, nombre_beneficiario in enumerate(beneficiarios_a_pagar):
                caja_origen = cajas_origen_seleccionadas[i]
                if not caja_origen: continue
                # >>> COMISIONES: BEGIN [logica_pago_adaptada]
                # Se adapta para usar la nueva tabla y estados
                cur.execute("SELECT COALESCE(SUM(monto), 0) FROM comisiones WHERE estado = 'aprobado' AND asesor_id = (SELECT id FROM administradores WHERE usuario = %s);", (nombre_beneficiario,))
                # >>> COMISIONES: END [logica_pago_adaptada]
                monto_a_pagar = cur.fetchone()[0] or Decimal('0.0')
                if monto_a_pagar > 0:
                    pagos_planificados.append({'beneficiario': nombre_beneficiario, 'monto': monto_a_pagar, 'caja': caja_origen})
                    deducciones_por_caja[caja_origen] = deducciones_por_caja.get(caja_origen, Decimal('0.0')) + monto_a_pagar
        if not pagos_planificados:
            flash("No se seleccionó ninguna operación de pago válida.", "warning")
            return redirect(url_for('dashboard_comercial'))
        balances_reales = calcular_balances_tesoreria()
        for caja, monto_deducir in deducciones_por_caja.items():
            if balances_reales.get(caja, Decimal('0.0')) < monto_deducir:
                flash(f"Fondos insuficientes en la caja '{caja}'. Se necesita ${monto_deducir:,.2f} pero solo hay ${balances_reales.get(caja, 0):,.2f}. Operación cancelada.", "danger")
                return redirect(url_for('dashboard_comercial'))
        with conn.cursor() as cur:
            for pago in pagos_planificados:
                nota_gasto, moneda = f"Pago de nómina a {pago['beneficiario']}", 'BS' if 'BS' in pago['caja'] else 'USD'
                
                cur.execute("""
                    INSERT INTO operaciones_tesoreria 
                    (tipo_operacion, caja_origen, moneda_origen, monto_origen, caja_destino, moneda_destino, monto_destino, nota, realizada_por, fecha_operacion, perdida_cambiaria)
                    VALUES ('GASTO_OPERATIVO', %s, %s, %s, 'GASTO_NOMINA', %s, %s, %s, %s, NOW(), %s)
                """, (pago['caja'], moneda, pago['monto'], moneda, pago['monto'], nota_gasto, g.admin['id'], Decimal('0.0')))

                # >>> COMISIONES: BEGIN [logica_pago_adaptada_2]
                cur.execute("UPDATE comisiones SET estado = 'pagado', paid_at = NOW() WHERE estado = 'aprobado' AND asesor_id = (SELECT id FROM administradores WHERE usuario = %s);", (pago['beneficiario'],))
                # >>> COMISIONES: END [logica_pago_adaptada_2]
            
            total_pagado_general = sum(p['monto'] for p in pagos_planificados)
            descripcion_auditoria = f"Procesó pago de nómina por lotes por un total de ${total_pagado_general:,.2f}."
            registrar_accion_auditoria('PAGO_NOMINA_COMERCIAL_LOTE', descripcion_auditoria)
            
            conn.commit()
            flash(f"Nómina pagada exitosamente.", "success")
    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Error al procesar el pago de la nómina: {e}", "danger")
        return redirect(url_for('dashboard_comercial'))
    
    return redirect(url_for('reporte_flujo_caja'))

@app.route('/comercial/split_contrato/<string:contrato_nro>')
@admin_required
@rol_requerido('superadmin', 'gerente')
def get_split_contrato(contrato_nro):
    conn = get_db()
    if not conn: return jsonify({'error': 'Error de conexión'}), 500
    try:
        with conn.cursor() as cur:
            # >>> COMISIONES: BEGIN [logica_split_adaptada]
            cur.execute("""
                SELECT a.usuario as beneficiario, c.notas as concepto, c.monto
                FROM comisiones c
                JOIN administradores a ON c.asesor_id = a.id
                WHERE c.origen_tipo = 'Venta' AND c.origen_id = (SELECT id FROM clientes WHERE contrato_nro = %s)
                ORDER BY c.monto DESC;
            """, (contrato_nro,))
            comisiones = cur.fetchall()
            cur.execute("SELECT cli.plan_contratado, ci.sobrante_empresa FROM caja_inscripciones ci JOIN clientes cli ON ci.cliente_id = cli.id WHERE ci.contrato_nro = %s;", (contrato_nro,))
            contrato_info = cur.fetchone()
            # >>> COMISIONES: END [logica_split_adaptada]
            if not contrato_info: return jsonify({'error': 'Contrato no encontrado'}), 404
            
            try:
                plan_contratado_decimal = Decimal(contrato_info['plan_contratado'])
            except (TypeError, InvalidOperation, ValueError):
                plan_contratado_decimal = Decimal('0.00')

            pool_total = plan_contratado_decimal * Decimal('0.16')
            total_pagado = sum(c['monto'] for c in comisiones)
            
            comisiones_json = [{'beneficiario': c['beneficiario'], 'concepto': c['concepto'], 'monto': f"{c['monto']:,.2f}"} for c in comisiones]
            
            response_data = {
                'comisiones': comisiones_json,
                'resumen': {
                    'pool_total': f"{pool_total:,.2f}",
                    'total_comisiones': f"{total_pagado:,.2f}",
                    'sobrante_empresa': f"{contrato_info['sobrante_empresa']:,.2f}" if contrato_info['sobrante_empresa'] is not None else "0.00"
                }
            }
            return jsonify(response_data)
    except (psycopg2.Error, TypeError) as e:
        logging.error(f"Error en get_split_contrato para {contrato_nro}: {e}")
        return jsonify({'error': 'Error al consultar la base de datos'}), 500

@app.route('/comercial/historial_asesor/<string:nombre_beneficiario>')
@admin_required
@rol_requerido('superadmin', 'gerente')
def get_historial_asesor(nombre_beneficiario):
    conn = get_db()
    if not conn: return jsonify({'error': 'Error de conexión'}), 500
    try:
        with conn.cursor() as cur:
            # >>> COMISIONES: BEGIN [logica_historial_adaptada]
            cur.execute("""
                SELECT c.notas as concepto, c.monto, cli.contrato_nro, cli.nombre, cli.apellido, cli.plan_contratado, ci.responsable_cierre
                FROM comisiones c
                JOIN clientes cli ON c.origen_id = cli.id AND c.origen_tipo = 'Venta'
                JOIN administradores a ON c.asesor_id = a.id
                LEFT JOIN caja_inscripciones ci ON cli.contrato_nro = ci.contrato_nro
                WHERE a.usuario = %s AND c.estado = 'pendiente'
                ORDER BY c.id DESC;
            """, (nombre_beneficiario,))
            # >>> COMISIONES: END [logica_historial_adaptada]
            historial = cur.fetchall()
            historial_json = []
            for item in historial:
                try:
                    plan_contratado_val = Decimal(item['plan_contratado'])
                except (TypeError, InvalidOperation, ValueError):
                    plan_contratado_val = Decimal('0.00')

                historial_json.append({
                    'concepto': item['concepto'], 'monto': f"{item['monto']:,.2f}",
                    'contrato_nro': item['contrato_nro'], 'cliente': f"{item['nombre']} {item['apellido']}",
                    'plan_contratado': f"{plan_contratado_val:,.2f}", 'responsable_cierre': item['responsable_cierre']
                })
            return jsonify(historial_json)
    except (psycopg2.Error, TypeError) as e:
        logging.error(f"Error en get_historial_asesor para {nombre_beneficiario}: {e}")
        return jsonify({'error': 'Error al consultar la base de datos'}), 500

# --- RUTAS DE REPORTES Y GESTIÓN DE COBRANZA ---
@app.route('/mi_cartera')
@admin_required
def mi_cartera():
    conn = get_db()
    clientes_asignados = []
    if conn and g.admin:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, nombre, apellido, cedula, telefono, proceso FROM clientes WHERE gestor_id = %s ORDER BY nombre, apellido;", (g.admin['id'],))
                clientes_asignados = cur.fetchall()
        except psycopg2.Error as e:
            flash(f"No se pudo cargar tu cartera de clientes: {e}", "error")
    return render_template('mi_cartera.html', clientes=clientes_asignados, anio_actual=get_venezuela_current_date().year)

@app.route('/reportes/metricas')
@admin_required
@rol_requerido('superadmin', 'gerente')
def reporte_metricas():
    conn = get_db()
    today = get_venezuela_current_date()
    dashboard_metrics = {
        'ingresos_mes_conciliados': 0, 'indice_morosidad': 0.0,
        'mes_actual': get_nombre_mes(today.month), 'anio_actual': today.year,
        'ingresos_ultimos_meses': {'labels': [], 'values': []},
        'composicion_clientes': {'labels': [], 'values': []},
        'total_clientes': 0, 'clientes_activos': 0, 'clientes_inactivos': 0,
        'clientes_retirados': 0, 'clientes_adjudicados': 0
    }
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM clientes")
                dashboard_metrics['total_clientes'] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM clientes WHERE TRIM(UPPER(estatus)) = 'ACTIVO'")
                dashboard_metrics['clientes_activos'] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM clientes WHERE TRIM(UPPER(estatus)) = 'INACTIVO'")
                dashboard_metrics['clientes_inactivos'] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM clientes WHERE TRIM(UPPER(estatus)) = 'RETIRO'")
                dashboard_metrics['clientes_retirados'] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM clientes WHERE TRIM(UPPER(proceso)) = 'ADJUDICADO'")
                dashboard_metrics['clientes_adjudicados'] = cur.fetchone()[0]
                first_day_of_month = today.replace(day=1)
                cur.execute("SELECT COALESCE(SUM(monto), 0) FROM pagos WHERE estado_pago = 'Conciliado' AND fecha_pago >= %s", (first_day_of_month,))
                dashboard_metrics['ingresos_mes_conciliados'] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM clientes WHERE TRIM(UPPER(proceso)) = 'AHORRADOR' AND TRIM(UPPER(estatus)) = 'ACTIVO'")
                total_ahorradores = cur.fetchone()[0]
                if total_ahorradores > 0:
                    cur.execute("SELECT COUNT(DISTINCT p.cliente_id) FROM pagos p JOIN clientes c ON p.cliente_id = c.id WHERE p.tipo_pago = 'Cuota' AND p.estado_pago = 'Conciliado' AND TRIM(UPPER(c.proceso)) = 'AHORRADOR' AND TRIM(UPPER(c.estatus)) = 'ACTIVO' AND p.fecha_pago >= %s", (first_day_of_month,))
                    ahorradores_al_dia = cur.fetchone()[0]
                    clientes_en_mora = total_ahorradores - ahorradores_al_dia
                    dashboard_metrics['indice_morosidad'] = (clientes_en_mora / total_ahorradores) * 100 if total_ahorradores > 0 else 0
                income_labels, income_values = [], []
                current_date = today
                for _ in range(6):
                    month_start = current_date.replace(day=1)
                    _, days_in_month = monthrange(current_date.year, current_date.month)
                    month_end = current_date.replace(day=days_in_month)
                    cur.execute("SELECT COALESCE(SUM(monto), 0) FROM pagos WHERE estado_pago = 'Conciliado' AND fecha_pago BETWEEN %s AND %s", (month_start, month_end))
                    total = cur.fetchone()[0]
                    income_labels.insert(0, get_nombre_mes(current_date.month))
                    income_values.insert(0, float(total))
                    current_date = month_start - timedelta(days=1)
                dashboard_metrics['ingresos_ultimos_meses'] = {'labels': income_labels, 'values': income_values}
                cur.execute("SELECT COALESCE(TRIM(UPPER(proceso)), 'SIN PROCESO') as proceso, COUNT(*) FROM clientes WHERE TRIM(UPPER(estatus)) = 'ACTIVO' GROUP BY proceso")
                client_composition = cur.fetchall()
                comp_labels = [row['proceso'].capitalize() for row in client_composition]
                comp_values = [row['count'] for row in client_composition]
                dashboard_metrics['composicion_clientes'] = {'labels': comp_labels, 'values': comp_values}
        except psycopg2.Error as e:
            flash(f"No se pudieron cargar las métricas del dashboard: {e}", "error")
    return render_template('reporte_metricas.html', anio_actual=get_venezuela_current_date().year, metrics=dashboard_metrics)

@app.route('/reportes/morosidad')
@admin_required
@rol_requerido('superadmin', 'gerente')
def reporte_morosidad():
    conn = get_db()
    today = get_venezuela_current_date()
    clientes_en_mora, gestores = [], []
    
    resumen = {'total_clientes_mora': 0, 'monto_total_mora': Decimal('0.0')}

    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, usuario FROM administradores ORDER BY usuario")
                gestores = cur.fetchall()
                
                first_day_of_month = today.replace(day=1)
                
                query_morosos = """
                    SELECT c.id, c.nombre, c.apellido, c.cedula, c.telefono, c.valor_cuota, c.gestor_id,
                           a.usuario as gestor_asignado,
                           (SELECT MAX(p.fecha_pago) FROM pagos p WHERE p.cliente_id = c.id AND p.estado_pago = 'Conciliado') as ultimo_pago_fecha
                    FROM clientes c LEFT JOIN administradores a ON c.gestor_id = a.id
                    WHERE TRIM(UPPER(c.proceso)) = 'AHORRADOR' AND TRIM(UPPER(c.estatus)) = 'ACTIVO'
                    AND c.id NOT IN (SELECT DISTINCT cliente_id FROM pagos WHERE tipo_pago = 'Cuota' AND estado_pago = 'Conciliado' AND fecha_pago >= %s) 
                    ORDER BY c.nombre, c.apellido;
                """
                cur.execute(query_morosos, (first_day_of_month,))
                clientes_en_mora = cur.fetchall()

                if clientes_en_mora:
                    total_clientes_mora = len(clientes_en_mora)
                    monto_total_mora = sum(c['valor_cuota'] for c in clientes_en_mora if c['valor_cuota'])
                    resumen = {
                        'total_clientes_mora': total_clientes_mora,
                        'monto_total_mora': monto_total_mora
                    }

        except psycopg2.Error as e:
            flash(f"No se pudo generar el reporte de morosidad: {e}", "error")
            
    return render_template('reporte_morosidad.html', 
                           clientes_en_mora=clientes_en_mora, 
                           gestores=gestores, 
                           mes_actual=get_nombre_mes(today.month), 
                           anio_actual=today.year, 
                           resumen=resumen)

@app.route('/asignar_gestor/<int:cliente_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def asignar_gestor(cliente_id):
    conn = get_db()
    gestor_id = request.form.get('gestor_id')
    gestor_id_para_db = int(gestor_id) if gestor_id and gestor_id.isdigit() else None
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT nombre, apellido FROM clientes WHERE id = %s", (cliente_id,))
                cliente = cur.fetchone()
                nombre_gestor = "nadie"
                if gestor_id_para_db:
                    cur.execute("SELECT usuario FROM administradores WHERE id = %s", (gestor_id_para_db,))
                    gestor = cur.fetchone()
                    if gestor: nombre_gestor = gestor['usuario']
                cur.execute("UPDATE clientes SET gestor_id = %s WHERE id = %s", (gestor_id_para_db, cliente_id))
                descripcion = f"Asignó al cliente {cliente['nombre']} {cliente['apellido']} al gestor '{nombre_gestor}'."
                registrar_accion_auditoria('ASIGNACION_GESTOR', descripcion, cliente_id)
                conn.commit()
                flash(f"Cliente asignado al gestor '{nombre_gestor}' exitosamente.", 'success')
        except (psycopg2.Error, ValueError) as e:
            conn.rollback()
            flash(f"Error al asignar el gestor: {e}", "error")
    return redirect(url_for('reporte_morosidad'))

@app.route('/admin/tasa_bcv', methods=['GET', 'POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def admin_tasa_bcv():
    conn = get_db()
    now_vet, today_date = get_venezuela_current_datetime(), get_venezuela_current_date()
    tasas_de_hoy, historial_tasas = {'usd': None, 'eur': None}, []
    if conn:
        try:
            with conn.cursor() as cur:
                if request.method == 'POST':
                    tasa_usd_str, tasa_eur_str = request.form.get('tasa_usd', '').replace(',', '.'), request.form.get('tasa_eur', '').replace(',', '.')
                    tasa_usd, tasa_eur = Decimal(tasa_usd_str) if tasa_usd_str else Decimal('0'), Decimal(tasa_eur_str) if tasa_eur_str else Decimal('0')
                    if tasa_usd >= 0 and tasa_eur >= 0:
                        sql_upsert = """
                            INSERT INTO historial_tasas_bcv (fecha, tasa, tasa_euro, establecida_por_id) VALUES (%s, %s, %s, %s)
                            ON CONFLICT (fecha) DO UPDATE SET tasa = EXCLUDED.tasa, tasa_euro = EXCLUDED.tasa_euro, establecida_por_id = EXCLUDED.establecida_por_id;
                        """
                        cur.execute(sql_upsert, (today_date, tasa_usd, tasa_eur, g.admin['id']))
                        if now_vet.hour >= 17 and now_vet.weekday() < 5: 
                            if now_vet.weekday() == 4: 
                                for i in range(1, 4): 
                                    next_day = today_date + timedelta(days=i)
                                    cur.execute(sql_upsert, (next_day, tasa_usd, tasa_eur, g.admin['id']))
                                flash('Tasa de Viernes guardada para todo el fin de semana y el Lunes.', 'success')
                            else:
                                tomorrow_date = today_date + timedelta(days=1)
                                cur.execute(sql_upsert, (tomorrow_date, tasa_usd, tasa_eur, g.admin['id']))
                                flash('Tasa guardada para hoy y mañana.', 'success')
                        else:
                            flash('Tasa guardada para hoy.', 'success')
                        conn.commit()
                        return redirect(url_for('admin_tasa_bcv'))
                    else:
                        flash('Las tasas deben ser números positivos.', 'danger')
                cur.execute("SELECT tasa, tasa_euro FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (today_date,))
                resultado = cur.fetchone()
                if resultado:
                    tasas_de_hoy['usd'], tasas_de_hoy['eur'] = resultado['tasa'], resultado['tasa_euro']
                cur.execute("SELECT h.fecha, h.tasa, h.tasa_euro, a.usuario FROM historial_tasas_bcv h LEFT JOIN administradores a ON h.establecida_por_id = a.id ORDER BY h.fecha DESC LIMIT 30")
                historial_tasas = cur.fetchall()
        except InvalidOperation:
            flash('Por favor, introduce un número válido para las tasas.', 'danger')
        except psycopg2.Error as e:
            if conn: conn.rollback()
            flash(f'Error al procesar la solicitud: {e}', 'danger')
    return render_template('admin_tasa_bcv.html', tasas_de_hoy=tasas_de_hoy, historial_tasas=historial_tasas, anio_actual=get_venezuela_current_date().year)

@app.route('/reportes/flujo_caja', methods=['GET', 'POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def reporte_flujo_caja():
    conn, today = get_db(), get_venezuela_current_date()
    fecha_reporte_str = request.form.get('fecha_reporte') or request.args.get('fecha_reporte') or today.strftime('%Y-%m-%d')
    try:
        fecha_reporte_dt = datetime.strptime(fecha_reporte_str, '%Y-%m-%d').date()
    except ValueError:
        flash("Formato de fecha inválido. Usando fecha actual.", "warning")
        fecha_reporte_str, fecha_reporte_dt = today.strftime('%Y-%m-%d'), today
    tasas_del_dia = {'usd': Decimal('0.0'), 'eur': Decimal('0.0')}
    if conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tasa, tasa_euro FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (fecha_reporte_dt,))
            resultado_tasa = cur.fetchone()
            if resultado_tasa:
                tasas_del_dia['usd'], tasas_del_dia['eur'] = resultado_tasa['tasa'] or Decimal('0.0'), resultado_tasa['tasa_euro'] or Decimal('0.0')
    balances = calcular_balances_tesoreria(fecha_hasta=fecha_reporte_dt)
    resumen = {}
    resumen.update(balances)
    tasa_usd, tasa_eur = tasas_del_dia['usd'], tasas_del_dia['eur']
    resumen['balance_bs_usd_usd'] = balances['CAJA_BS_USD'] / tasa_usd if tasa_usd > 0 else Decimal('0.0')
    resumen['balance_bs_eur_eur'] = balances['CAJA_BS_EUR'] / tasa_eur if tasa_eur > 0 else Decimal('0.0')
    resumen['balance_bs_consolidado_bs'] = balances['CAJA_BS_TOTAL']
    balance_bs_eur_en_usd = balances['CAJA_BS_EUR'] / tasa_usd if tasa_usd > 0 else Decimal('0.0')
    resumen['balance_bs_consolidado_usd'] = resumen['balance_bs_usd_usd'] + balance_bs_eur_en_usd
    resumen['tasa_ponderada_bs'] = resumen['balance_bs_consolidado_bs'] / resumen['balance_bs_consolidado_usd'] if resumen['balance_bs_consolidado_usd'] > 0 else Decimal('0.0')
    resumen['balance_general_consolidado_usd'] = balances['EFECTIVO_USD'] + balances['BINANCE_USDT'] + resumen['balance_bs_consolidado_usd']
    resumen['acumulado_perdida_devaluacion'], resumen['acumulado_perdida_conversion'] = Decimal('0.0'), Decimal('0.0')
    if conn and tasa_usd > 0:
        try:
            with conn.cursor() as cur:
                fecha_fin_timestamp = datetime.combine(fecha_reporte_dt, datetime.max.time())
                cur.execute("SELECT COALESCE(SUM(CASE WHEN tasa_dia > 0 THEN monto_bs / tasa_dia ELSE 0 END), 0) FROM pagos WHERE estado_pago = 'Conciliado' AND monto_bs > 0 AND moneda_referencia = 'USD' AND fecha_pago <= %s", (fecha_reporte_dt,))
                valor_historico_ingresos_bs_usd = cur.fetchone()[0] or Decimal('0.0')
                cur.execute("SELECT COALESCE(SUM(CASE WHEN tasa_aplicada > 0 THEN monto_origen / tasa_aplicada ELSE 0 END), 0) FROM operaciones_tesoreria WHERE caja_origen = 'CAJA_BS_USD' AND fecha_operacion <= %s", (fecha_fin_timestamp,))
                valor_historico_egresos_bs_usd = cur.fetchone()[0] or Decimal('0.0')
                saldo_teorico_bs_usd_en_usd = valor_historico_ingresos_bs_usd - valor_historico_egresos_bs_usd
                resumen['acumulado_perdida_devaluacion'] = saldo_teorico_bs_usd_en_usd - resumen['balance_bs_usd_usd']
                cur.execute("SELECT COALESCE(SUM(perdida_cambiaria), 0) FROM operaciones_tesoreria WHERE fecha_operacion <= %s", (fecha_fin_timestamp,))
                resumen['acumulado_perdida_conversion'] = cur.fetchone()[0] or Decimal('0.0')
        except psycopg2.Error as e:
            flash(f"Error calculando las pérdidas financieras: {e}", "warning")
    historial_unificado = []
    if conn:
        try:
            with conn.cursor() as cur:
                fecha_inicio_periodo, fecha_fin_periodo = datetime.combine(fecha_reporte_dt, datetime.min.time()), datetime.combine(fecha_reporte_dt, datetime.max.time())
                query_unificada = """
                    SELECT p.fecha_pago AS timestamp, 'Pago Conciliado' AS tipo_operacion, (c.nombre || ' ' || c.apellido || ' (Ref: ' || COALESCE(p.referencia, 'N/A') || ')') AS detalle,
                           CASE WHEN p.monto_bs > 0 THEN p.monto_bs ELSE p.monto END AS monto_ingreso, CASE WHEN p.monto_bs > 0 THEN 'BS' ELSE 'USD' END AS moneda_ingreso,
                           NULL AS monto_egreso, NULL AS moneda_egreso, (SELECT usuario FROM administradores WHERE id = p.conciliado_por_id) AS usuario
                    FROM pagos p JOIN clientes c ON p.cliente_id = c.id WHERE p.estado_pago = 'Conciliado' AND p.fecha_pago = %s
                    UNION ALL
                    SELECT ot.fecha_operacion AS timestamp, ot.tipo_operacion, ot.nota AS detalle, CASE WHEN ot.tipo_operacion != 'PAGO_GASTO' THEN ot.monto_destino ELSE NULL END AS monto_ingreso,
                           CASE WHEN ot.tipo_operacion != 'PAGO_GASTO' THEN ot.moneda_destino ELSE NULL END AS moneda_ingreso, ot.monto_origen AS monto_egreso, ot.moneda_origen AS moneda_egreso, adm.usuario
                    FROM operaciones_tesoreria ot JOIN administradores adm ON ot.realizada_por = adm.id WHERE ot.fecha_operacion BETWEEN %s AND %s
                    ORDER BY timestamp ASC;
                """
                cur.execute(query_unificada, (fecha_reporte_dt, fecha_inicio_periodo, fecha_fin_periodo))
                historial_unificado = cur.fetchall()
        except (psycopg2.Error, ValueError) as e:
            flash(f"Error al obtener historial de movimientos: {e}", "error")
    return render_template('reporte_flujo_caja.html', fecha_reporte=fecha_reporte_str, resumen=resumen, tasas_del_dia=tasas_del_dia, historial=historial_unificado, anio_actual=today.year)

# =================================================================================
# --- GESTIÓN DE CLIENTES Y PAGOS ---
# =================================================================================

@app.route('/cliente/<int:cliente_id>')
@admin_required
def perfil_cliente(cliente_id):
    conn = get_db()
    cliente, pagos, gestiones, historial_eventos = None, [], [], []
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s", (cliente_id,))
                cliente = cur.fetchone()
                if not cliente:
                    flash("Cliente no encontrado.", "error")
                    return redirect(url_for('consulta'))
                
                cur.execute("SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha_creacion DESC, id DESC", (cliente_id,))
                pagos = cur.fetchall()
                
                cur.execute("""
                    SELECT g.nota, g.tipo_gestion, g.fecha_creacion, a.usuario as gestor_nombre FROM gestiones_cobranza g
                    JOIN administradores a ON g.gestor_id = a.id WHERE g.cliente_id = %s ORDER BY g.fecha_creacion DESC;
                """, (cliente_id,))
                gestiones = cur.fetchall()

                cur.execute("""
                    SELECT 
                        'solicitud' as origen, id, 'Solicitud de ' || tipo_solicitud AS tipo, 
                        detalles, fecha_creacion AS fecha, estado, revisado_por_id, 
                        (SELECT usuario FROM administradores WHERE id = revisado_por_id) as usuario
                    FROM solicitudes WHERE cliente_id = %s
                    UNION ALL
                    SELECT 
                        'gestion' as origen, g.id, 'Gestión de Cobranza' as tipo, 
                        json_build_object('nota', g.nota) as detalles, g.fecha_creacion as fecha, 
                        'Realizada' as estado, g.gestor_id as revisado_por_id, a.usuario
                    FROM gestiones_cobranza g JOIN administradores a ON g.gestor_id = a.id
                    WHERE g.cliente_id = %s
                    ORDER BY fecha DESC;
                """, (cliente_id, cliente_id))
                
                raw_events = cur.fetchall()
                for event in raw_events:
                    evento_fmt = {'id': event['id'], 'fecha': event['fecha'], 'origen': event['origen'], 'tipo': event['tipo'], 'estado': event['estado'], 'usuario': event.get('usuario', 'Sistema'), 'detalles': event.get('detalles')}
                    
                    detalles = event.get('detalles', {})
                    if isinstance(detalles, str):
                        try:
                            detalles = json.loads(detalles)
                        except json.JSONDecodeError:
                            detalles = {}
                    evento_fmt['detalles'] = detalles

                    if event['origen'] == 'solicitud':
                        descripcion = f"Estado: {event['estado']}"
                        if 'motivo' in detalles:
                            descripcion += f" - Motivo: {detalles.get('motivo', '')}"
                    elif event['origen'] == 'gestion':
                        descripcion = detalles.get('nota', 'Sin detalles.')
                    else:
                        descripcion = "Evento del sistema."
                    
                    evento_fmt['descripcion'] = descripcion
                    historial_eventos.append(evento_fmt)
        except (psycopg2.Error, json.JSONDecodeError) as e:
            flash(f"Error al cargar el perfil del cliente: {e}", "error")
            return redirect(url_for('consulta'))
    return render_template('cliente_perfil.html', cliente=cliente, pagos=pagos, gestiones=gestiones, historial_eventos=historial_eventos, anio_actual=get_venezuela_current_date().year)

@app.route('/agregar_gestion/<int:cliente_id>', methods=['POST'])
@admin_required
def agregar_gestion(cliente_id):
    nota = request.form.get('nota')
    tipo_gestion = request.form.get('tipo_gestion')
    if not nota or not nota.strip() or not tipo_gestion:
        flash("El tipo de gestión y la nota no pueden estar vacíos.", "warning")
        return redirect(url_for('perfil_cliente', cliente_id=cliente_id))
    conn = get_db()
    if conn and g.admin:
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO gestiones_cobranza (cliente_id, gestor_id, tipo_gestion, nota) VALUES (%s, %s, %s, %s)", (cliente_id, g.admin['id'], tipo_gestion, nota.strip()))
                cur.execute("SELECT nombre, apellido FROM clientes WHERE id = %s", (cliente_id,))
                cliente = cur.fetchone()
                descripcion = f"Agregó gestión '{tipo_gestion}' para el cliente {cliente['nombre']} {cliente['apellido']}: '{nota[:50]}...'"
                registrar_accion_auditoria('AGREGAR_GESTION', descripcion, cliente_id)
                conn.commit()
                flash("Gestión guardada exitosamente.", "success")
        except psycopg2.Error as e:
            conn.rollback()
            flash(f"Error al guardar la gestión: {e}", "error")
    return redirect(url_for('perfil_cliente', cliente_id=cliente_id))

@app.route('/registrar')
@admin_required
def registrar():
    return render_template('registrar.html')

@app.route('/registrar_cliente', methods=['POST'])
@admin_required
def registrar_cliente():
    form_data = {k: v.strip() if isinstance(v, str) else v for k, v in request.form.items()}
    if not all(form_data.get(key) for key in ['nombre_apellido', 'cedula', 'contrato_nro']):
        flash('Nombre, Cédula y N° de Contrato son obligatorios.', 'error')
        return redirect(url_for('registrar'))
    try:
        form_data['inscripcion_monto'] = Decimal(form_data.get('inscripcion_monto', '0').replace(',', '.'))
        form_data['valor_cuota'] = Decimal(form_data.get('valor_cuota', '0').replace(',', '.'))
        form_data['cuotas_totales'] = int(form_data.get('cuotas_totales', 0))
    except (InvalidOperation, ValueError):
        flash('Los valores para inscripción, cuota o número de cuotas no son números válidos.', 'error')
        return redirect(url_for('registrar'))
    if form_data.get('fecha_ingreso'):
        try:
            form_data['fecha_ingreso'] = datetime.strptime(form_data['fecha_ingreso'], '%Y-%m-%d').date()
        except ValueError:
            flash('El formato de la fecha de ingreso no es válido.', 'error')
            return redirect(url_for('registrar'))
    flash('Datos del cliente validados. Por favor, proceda con las firmas para finalizar el registro.', 'info')
    return render_template('contrato.html', cliente=form_data, modo_pre_registro=True, anio_actual=get_venezuela_current_date().year)

@app.route('/finalizar_registro', methods=['POST'])
@admin_required
def finalizar_registro():
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('registrar'))

    form_data = {k: v.strip() if isinstance(v, str) else v for k, v in request.form.items()}
    
    foto_cliente_base64 = form_data.get('foto_cliente')
    foto_cedula_base64 = form_data.get('foto_cedula')
    
    ruta_s3_cliente = None
    ruta_s3_cedula = None
    cedula_cliente_limpia = form_data.get('cedula', '').replace(' ', '').replace('.', '')

    if foto_cliente_base64 and foto_cliente_base64.startswith('data:image'):
        nombre_archivo_s3 = f"documentos/{cedula_cliente_limpia}/foto_cliente.jpg"
        if subir_archivo_a_s3(foto_cliente_base64, nombre_archivo_s3):
            ruta_s3_cliente = nombre_archivo_s3
        else:
            flash("Error crítico al subir la foto del cliente a S3. El registro ha sido cancelado.", "danger")
            return redirect(url_for('registrar'))

    if foto_cedula_base64 and foto_cedula_base64.startswith('data:image'):
        nombre_archivo_s3 = f"documentos/{cedula_cliente_limpia}/foto_cedula.jpg"
        if subir_archivo_a_s3(foto_cedula_base64, nombre_archivo_s3):
            ruta_s3_cedula = nombre_archivo_s3
        else:
            flash("Error crítico al subir la foto de la cédula a S3. El registro ha sido cancelado.", "danger")
            return redirect(url_for('registrar'))

    try:
        # ... (resto de la lógica de la función sin cambios)
        firma_cliente, firma_empresa = form_data.get('firma_cliente'), form_data.get('firma_empresa')
        if not firma_cliente or not firma_empresa:
            flash('Ambas firmas son obligatorias para registrar al cliente.', 'error')
            return redirect(url_for('registrar'))
            
        form_data['inscripcion_monto'] = Decimal(form_data.get('inscripcion_monto', '0.00').replace(',', '.'))
        form_data['valor_cuota'] = Decimal(form_data.get('valor_cuota', '0.00').replace(',', '.'))
        form_data['cuotas_totales'] = int(form_data.get('cuotas_totales')) if form_data.get('cuotas_totales') else None
        responsable_cierre = form_data.get('responsable', '') 

        with conn.cursor() as cur:
            nombre_completo = form_data.get('nombre_apellido').split(' ', 1)
            nombre, apellido = nombre_completo[0], nombre_completo[1] if len(nombre_completo) > 1 else ''
            
            insert_dict = {
                'nombre': nombre, 'apellido': apellido, 'cedula': cedula_cliente_limpia,
                'cuotas_pagadas_progresivas': 0, 'cuotas_pagadas_regresivas': 0, 
                'firma_digital': firma_cliente, 'firma_empresa': firma_empresa, 
                'fecha_firma': datetime.now(VENEZUELA_TZ), 'proceso': 'RESERVA',
                'ruta_foto_cliente_s3': ruta_s3_cliente,
                'ruta_foto_cedula_s3': ruta_s3_cedula
            }
            
            optional_fields = [
                'contrato_nro', 'telefono', 'asesor', 'responsable', 'fecha_ingreso', 'grupo', 
                'plan_contratado', 'cuotas_totales', 'moneda_pago', 'valor_cuota', 
                'inscripcion_monto', 'ciclo_cobranza', 'direccion', 'email', 
                'beneficiario_nombre', 'beneficiario_cedula', 'beneficiario_telefono'
            ]

            for field in optional_fields:
                if form_data.get(field): insert_dict[field] = form_data[field]
                
            columns = insert_dict.keys()
            values = insert_dict.values()
            query = f"INSERT INTO clientes ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(values))}) RETURNING id"
            
            cur.execute(query, list(values))
            new_client_id = cur.fetchone()[0]

            if form_data['inscripcion_monto'] > 0:
                cur.execute("INSERT INTO caja_inscripciones (contrato_nro, cliente_id, monto_inscripcion, responsable_cierre) VALUES (%s, %s, %s, %s)",
                            (form_data.get('contrato_nro'), new_client_id, form_data['inscripcion_monto'], responsable_cierre))
            
            descripcion_audit = f"Registró y firmó contrato para nuevo cliente: {form_data.get('nombre_apellido')} (C.I. {cedula_cliente_limpia})."
            registrar_accion_auditoria('REGISTRO_CLIENTE_FIRMADO', descripcion_audit, new_client_id)
            
            conn.commit()
            flash(f"¡Cliente '{form_data.get('nombre_apellido')}' registrado exitosamente como RESERVA!", 'success')
            return redirect(url_for('consulta', busqueda=form_data.get('cedula')))

    except psycopg2.IntegrityError:
        conn.rollback()
        flash(f"Registro fallido: La cédula '{form_data.get('cedula')}' ya existe.", 'error')
        return redirect(url_for('registrar'))
    except (psycopg2.Error, ValueError, ConnectionError, InvalidOperation) as e:
        conn.rollback()
        logging.error(f"Error en finalizar_registro: {e}")
        flash(f"Registro fallido: Ocurrió un error inesperado: {e}", 'error')
    
    return redirect(url_for('registrar'))

@app.route('/consulta', methods=['GET', 'POST'])
@admin_required
def consulta():
    clientes_encontrados, mensaje_error = [], None
    termino_busqueda_raw = request.form.get('busqueda', request.args.get('busqueda', ''))
    termino_busqueda = termino_busqueda_raw.strip()
    if termino_busqueda:
        conn = get_db()
        if not conn:
            mensaje_error = "Error de conexión a la base de datos."
        else:
            try:
                with conn.cursor() as cur:
                    query_clientes = "SELECT *, inscripcion_monto AS inscripcion FROM clientes WHERE cedula ILIKE %s OR nombre ILIKE %s OR apellido ILIKE %s ORDER BY nombre, apellido LIMIT 20;"
                    patron_busqueda = f'%{termino_busqueda}%'
                    cur.execute(query_clientes, (patron_busqueda, patron_busqueda, patron_busqueda))
                    for cliente in cur.fetchall():
                        cliente_dict = dict(cliente)
                        cliente_dict['nombre_apellido'] = f"{cliente.get('nombre', '')} {cliente.get('apellido', '')}".strip()
                        
                        cur.execute("SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha_creacion DESC, id DESC", (cliente_dict['id'],))
                        cliente_dict['pagos'] = cur.fetchall()
                        cur.execute("SELECT * FROM ofertas WHERE cliente_id = %s ORDER BY fecha_oferta DESC", (cliente_dict['id'],))
                        cliente_dict['ofertas'] = cur.fetchall()

                        cur.execute("SELECT COUNT(*) FROM pagos WHERE cliente_id = %s", (cliente_dict['id'],))
                        cliente_dict['conteo_pagos'] = cur.fetchone()[0]
                        cur.execute("SELECT COUNT(*) FROM ofertas WHERE cliente_id = %s", (cliente_dict['id'],))
                        cliente_dict['conteo_ofertas'] = cur.fetchone()[0]
                        cur.execute("SELECT COUNT(*) FROM gestiones_cobranza WHERE cliente_id = %s", (cliente_dict['id'],))
                        cliente_dict['conteo_gestiones'] = cur.fetchone()[0]

                        clientes_encontrados.append(cliente_dict)
                        
                    if not clientes_encontrados:
                        mensaje_error = "🚫 No se encontraron clientes que coincidan con su búsqueda."
            except psycopg2.Error as e:
                mensaje_error = f"Error al consultar la base de datos: {e}"
    return render_template('consulta.html', clientes=clientes_encontrados, mensaje_error=mensaje_error, busqueda=termino_busqueda_raw)

@app.route('/registrar_pago/<int:client_id>', methods=['GET', 'POST'])
@admin_required
def registrar_pago(client_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta'))
    with conn.cursor() as cur:
        cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s", (client_id,))
        cliente = cur.fetchone()
        today_str = get_venezuela_current_date().strftime('%Y-%m-%d')
        cur.execute("SELECT tasa, tasa_euro FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (today_str,))
        tasas_hoy = cur.fetchone()
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta'))
    if request.method == 'POST':
        pago_form = {k: v if v else None for k, v in request.form.items()}
        tipo_pago = pago_form.get('tipo_pago')
        inscripcion_total = Decimal(cliente.get('inscripcion_monto') or 0)
        inscripcion_pagada = Decimal(cliente.get('inscripcion_pagada') or 0)
        if tipo_pago == 'Cuota' and inscripcion_pagada < inscripcion_total:
            flash('Error: No se puede registrar un pago de cuota hasta que la inscripción esté 100% pagada.', 'error')
            return render_template('registrar_pago.html', cliente=cliente, tasas_hoy=tasas_hoy)
        if tipo_pago == 'Inscripción' and inscripcion_pagada >= inscripcion_total:
            flash('Error: La inscripción para este cliente ya ha sido completada.', 'error')
            return render_template('registrar_pago.html', cliente=cliente, tasas_hoy=tasas_hoy)
        
        forma_pago = pago_form.get('forma_pago')
        referencia = pago_form.get('referencia')
        
        if forma_pago != 'Efectivo' and not referencia:
            flash('Error: La referencia es obligatoria para pagos que no son en efectivo.', 'error')
            return render_template('registrar_pago.html', cliente=cliente, tasas_hoy=tasas_hoy)

        moneda_referencia, pago_en_valor = None, pago_form.get('pago_en')
        if pago_en_valor == 'Dolar/BCV': moneda_referencia = 'USD'
        elif pago_en_valor == 'Euro/BCV': moneda_referencia = 'EUR'
        try:
            with conn.cursor() as cur:
                detalles_pago = {}
                if forma_pago == 'Pago Móvil':
                    detalles_pago['telefono_emisor'] = pago_form.get('pago_movil_telefono')
                    detalles_pago['cedula_emisor'] = pago_form.get('pago_movil_cedula')
                elif forma_pago == 'Binance':
                    detalles_pago['usuario_binance'] = pago_form.get('binance_user')
                detalles_json = json.dumps(detalles_pago) if detalles_pago else None

                pago_query = """
                    INSERT INTO pagos (cliente_id, monto, tipo_pago, forma_pago, fecha_pago, pago_en, por_concepto_de, referencia, banco, lugar_emision,
                                        tasa_dia, monto_bs, estado_pago, cuotas_cubiertas, moneda_referencia, fecha_creacion, registrado_por_id, detalles_reporte)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Pendiente', 0, %s, %s, %s, %s);
                """
                cur.execute(pago_query, (client_id, pago_form['monto'], tipo_pago, forma_pago, pago_form['fecha_pago'], pago_form.get('pago_en'), 
                                         pago_form.get('por_concepto_de'), referencia, pago_form.get('banco'), pago_form.get('lugar_emision'), 
                                         pago_form.get('tasa_dia'), pago_form.get('monto_bs'), moneda_referencia, get_venezuela_current_datetime(), g.admin['id'], detalles_json))
                conn.commit()
                flash(f"¡Pago de {tipo_pago} registrado como PENDIENTE! Ahora debe ser conciliado.", 'success')
                return redirect(url_for('consulta', busqueda=cliente['cedula']))
        except (psycopg2.Error, ValueError, TypeError) as e:
            conn.rollback()
            flash(f'Ocurrió un error al registrar el pago: {e}', 'error')
            return render_template('registrar_pago.html', cliente=cliente, tasas_hoy=tasas_hoy)
    return render_template('registrar_pago.html', cliente=cliente, tasas_hoy=tasas_hoy)

# --- LÓGICA DE CONCILIACIÓN CON CONSOLIDACIÓN ---
@app.route('/conciliar_pago/<int:pago_id>', methods=['POST'])
@admin_required
def conciliar_pago(pago_id):
    conn = get_db()
    cedula_cliente_para_redirect = None
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('pagos_por_conciliar'))
    
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM pagos WHERE id = %s FOR UPDATE", (pago_id,))
            pago_actual = cur.fetchone()

            if pago_actual:
                cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s FOR UPDATE", (pago_actual['cliente_id'],))
                cliente = cur.fetchone()
                cedula_cliente_para_redirect = cliente['cedula'] if cliente else None
            else:
                cliente = None

            if not pago_actual or pago_actual['estado_pago'] != 'Pendiente':
                flash("El pago no se puede conciliar porque no está pendiente.", 'error')
                if cedula_cliente_para_redirect:
                    return redirect(url_for('consulta', busqueda=cedula_cliente_para_redirect))
                return redirect(url_for('pagos_por_conciliar'))
            
            admin_id = g.admin['id']

            flash_msg = ""
            if pago_actual['tipo_pago'] == 'Inscripción':
                inscripcion_pagada_actual = Decimal(cliente.get('inscripcion_pagada') or 0)
                inscripcion_total_requerida = Decimal(cliente.get('inscripcion_monto') or 0)
                nueva_inscripcion_pagada = inscripcion_pagada_actual + pago_actual['monto']

                if nueva_inscripcion_pagada >= inscripcion_total_requerida:
                    # --- INICIO DE LA CORRECCIÓN ---
                    cur.execute("SELECT * FROM pagos WHERE cliente_id = %s AND tipo_pago = 'Inscripción' AND estado_pago = 'Conciliado'", (cliente['id'],))
                    abonos_anteriores = cur.fetchall()
                    
                    monto_total_consolidado = nueva_inscripcion_pagada
                    pago_final_id = None

                    if not abonos_anteriores:
                        # Caso 1: Es un pago único que cubre el 100%.
                        # Se convierte este mismo pago en el recibo final.
                        cur.execute(
                            "UPDATE pagos SET tipo_pago = 'Inscripción Finalizada', por_concepto_de = 'Pago total de inscripción', estado_pago = 'Conciliado', conciliado_por_id = %s WHERE id = %s RETURNING id",
                            (admin_id, pago_id)
                        )
                        pago_final_id = cur.fetchone()[0]
                    else:
                        # Caso 2: Es el último abono. Se consolidan todos los pagos.
                        pagos_a_consolidar = abonos_anteriores + [pago_actual]
                        ids_a_anular = [p['id'] for p in pagos_a_consolidar]
                        
                        pagos_individuales = [
                            {"id": p['id'], "monto": str(p['monto']), "fecha": p['fecha_pago'].isoformat()}
                            for p in pagos_a_consolidar
                        ]
                        detalles_consolidados = {"pagos_individuales": pagos_individuales}
                        
                        cur.execute("""
                            INSERT INTO pagos (cliente_id, monto, tipo_pago, forma_pago, estado_pago, por_concepto_de, detalles_reporte, conciliado_por_id, fecha_pago, cuotas_cubiertas) 
                            VALUES (%s, %s, 'Inscripción Finalizada', 'Consolidado', 'Conciliado', 'Pago total de inscripción consolidado', %s, %s, %s, 0) RETURNING id
                        """, (cliente['id'], monto_total_consolidado, json.dumps(detalles_consolidados), admin_id, pago_actual['fecha_pago']))
                        pago_final_id = cur.fetchone()[0]

                        detalle_anulacion = json.dumps({"motivo": "Consolidado en recibo final", "recibo_final_id": pago_final_id})
                        cur.execute("UPDATE pagos SET estado_pago = 'Anulado', detalles_reporte = %s WHERE id = ANY(%s)", (detalle_anulacion, ids_a_anular))
                    
                    # --- FIN DE LA CORRECCIÓN ---

                    # Lógica común para ambos casos de inscripción completada
                    cur.execute("UPDATE clientes SET inscripcion_pagada = %s, proceso = 'INSCRITO' WHERE id = %s", (monto_total_consolidado, cliente['id']))
                    
                    try:
                        cur.execute("SELECT asesor, responsable, plan_contratado, contrato_nro FROM clientes WHERE id = %s", (cliente['id'],))
                        info_cliente_comercial = cur.fetchone()
                        if info_cliente_comercial and info_cliente_comercial['plan_contratado']:
                            calcular_y_guardar_comisiones(
                                contrato_nro=info_cliente_comercial['contrato_nro'],
                                cliente_id=cliente['id'],
                                monto_plan=Decimal(info_cliente_comercial['plan_contratado']),
                                asesor_dueno=info_cliente_comercial['asesor'],
                                responsable_cierre=info_cliente_comercial['responsable']
                            )
                    except Exception as e:
                        logging.error(f"FALLO AL CALCULAR COMISIONES para contrato {cliente['contrato_nro']}: {e}")
                        flash(f"Advertencia: El pago fue conciliado, pero hubo un error al generar las comisiones: {e}", "warning")

                    descripcion_audit = f"Consolidó pagos de inscripción. Recibo final N°{pago_final_id} por ${monto_total_consolidado} para {cliente['nombre_apellido']}."
                    registrar_accion_auditoria('CONSOLIDACION_INSCRIPCION', descripcion_audit, cliente['id'])
                    
                    url_recibo = url_for('ver_recibo_inscripcion', pago_id=pago_final_id)
                    flash_msg = f"¡Inscripción consolidada! El cliente ha sido notificado para pagar su primera cuota y activar el plan. <a href='{url_recibo}' target='_blank' class='alert-link'>Ver Recibo Final</a>."
                
                else:
                    # Es un abono parcial que no completa el 100%.
                    cur.execute("UPDATE clientes SET inscripcion_pagada = %s WHERE id = %s", (nueva_inscripcion_pagada, cliente['id']))
                    cur.execute("UPDATE pagos SET estado_pago = 'Conciliado', conciliado_por_id = %s WHERE id = %s", (admin_id, pago_id))
                    url_recibo = url_for('generar_recibo_pago', pago_id=pago_id)
                    flash_msg = f"Abono de inscripción N° {pago_id} conciliado. <a href='{url_recibo}' target='_blank' class='alert-link'>Ver Recibo</a>."

            elif pago_actual['tipo_pago'] == 'Cuota':
                if cliente['proceso'] == 'INSCRITO':
                    cur.execute("UPDATE clientes SET proceso = 'Ahorrador', estatus = 'ACTIVO' WHERE id = %s", (cliente['id'],))
                
                valor_cuota = Decimal(cliente.get('valor_cuota') or 0)
                if valor_cuota <= 0: raise ValueError('El cliente no tiene un valor de cuota válido.')
                cpp, cpr, br = cliente.get('cuotas_pagadas_progresivas', 0), cliente.get('cuotas_pagadas_regresivas', 0), Decimal(cliente.get('balance_regresivo', 0))
                mtd, pph, rph = pago_actual['monto'] + br, 0, 0
                if mtd >= valor_cuota: pph, mtd = 1, mtd - valor_cuota
                bp = mtd
                while bp >= valor_cuota: rph, bp = rph + 1, bp - valor_cuota
                nbf, ncpp, ncpr, cch = bp, cpp + pph, cpr + rph, pph + rph
                cur.execute("UPDATE clientes SET cuotas_pagadas_progresivas = %s, cuotas_pagadas_regresivas = %s, balance_regresivo = %s WHERE id = %s;", (ncpp, ncpr, nbf, cliente['id']))
                cur.execute("UPDATE pagos SET cuotas_cubiertas = %s, progresivas_cubiertas = %s, regresivas_cubiertas = %s, cuotas_progresivas_al_pagar = %s, cuotas_regresivas_al_pagar = %s, balance_al_pagar = %s WHERE id = %s;", (cch, pph, rph, ncpp, ncpr, nbf, pago_id))
                cur.execute("UPDATE pagos SET estado_pago = 'Conciliado', conciliado_por_id = %s WHERE id = %s", (admin_id, pago_id))
                url_recibo = url_for('generar_recibo_pago', pago_id=pago_id)
                flash_msg = f"¡Pago de cuota N° {pago_id} conciliado! <a href='{url_recibo}' target='_blank' class='alert-link'>Ver Recibo</a>."

            conn.commit()
            flash(flash_msg, 'success')
            return redirect(url_for('consulta', busqueda=cedula_cliente_para_redirect))

    except (psycopg2.Error, ValueError, TypeError) as e:
        if conn: conn.rollback()
        from flask import current_app
        current_app.logger.error(f"Error al conciliar el pago {pago_id}: {e}")
        flash(f'Ocurrió un error al conciliar el pago: {e}', 'error')
        if cedula_cliente_para_redirect:
            return redirect(url_for('consulta', busqueda=cedula_cliente_para_redirect))
    return redirect(url_for('pagos_por_conciliar'))


@app.route('/recibo/<int:pago_id>')
def generar_recibo_pago(pago_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('home'))
    
    with conn.cursor() as cur:
        query = """
            SELECT p.*, c.nombre, c.apellido, (c.nombre || ' ' || c.apellido) as nombre_apellido, c.cedula, c.cuotas_totales, c.valor_cuota, c.inscripcion_monto, c.inscripcion_pagada,
                   COALESCE(p.cuotas_progresivas_al_pagar, c.cuotas_pagadas_progresivas) AS cuotas_pagadas_progresivas, 
                   COALESCE(p.cuotas_regresivas_al_pagar, c.cuotas_pagadas_regresivas) AS cuotas_pagadas_regresivas,
                   COALESCE(p.balance_al_pagar, c.balance_regresivo) AS balance_regresivo
            FROM pagos p JOIN clientes c ON p.cliente_id = c.id WHERE p.id = %s;"""
        cur.execute(query, (pago_id,))
        pago = cur.fetchone()

    if not pago:
        flash('Recibo no encontrado.', 'error')
        return redirect(url_for('home'))

    if pago['estado_pago'] not in ['Conciliado', 'Anulado'] and pago['tipo_pago'] != 'Inscripción Finalizada':
        flash('Este recibo no puede ser visualizado porque el pago no ha sido conciliado.', 'warning')
        return redirect(url_for('home'))
    
    if pago['estado_pago'] == 'Anulado' and pago['detalles_reporte'] and 'recibo_final_id' in pago['detalles_reporte']:
        return render_template('recibo_anulado.html', pago=pago, is_admin_view='admin_id' in session)

    if pago['tipo_pago'] == 'Inscripción Finalizada':
        return redirect(url_for('ver_recibo_inscripcion', pago_id=pago_id))

    return render_template('recibo.html', pago=pago, is_admin_view='admin_id' in session)


@app.route('/recibo_inscripcion/<int:pago_id>')
def ver_recibo_inscripcion(pago_id):
    conn = get_db()
    if not conn:
        if 'cliente_id' in session:
            return redirect(url_for('portal_login'))
        else:
            return redirect(url_for('consulta'))

    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.*, (c.nombre || ' ' || c.apellido) as nombre_apellido, c.cedula, c.plan_contratado 
            FROM pagos p JOIN clientes c ON p.cliente_id = c.id 
            WHERE p.id = %s AND p.tipo_pago = 'Inscripción Finalizada'
        """, (pago_id,))
        pago = cur.fetchone()

    if not pago:
        flash('Recibo de inscripción final no encontrado.', 'error')
        if 'cliente_id' in session:
            return redirect(url_for('portal_dashboard'))
        else:
            return redirect(url_for('consulta'))
    
    detalles = pago['detalles_reporte']
    if isinstance(detalles, str):
        try:
            detalles = json.loads(detalles)
        except json.JSONDecodeError:
            detalles = {}
    
    pago_actualizado = dict(pago)
    pago_actualizado['detalles_reporte'] = detalles

    return render_template('recibo_inscripcion.html', pago=pago_actualizado, cliente=pago, is_admin_view='admin_id' in session)


@app.route('/anular_recibo/<int:pago_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def anular_recibo(pago_id):
    conn, cedula_cliente = get_db(), ''
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta'))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT p.*, c.nombre, c.apellido, c.cedula FROM pagos p JOIN clientes c ON p.cliente_id = c.id WHERE p.id = %s FOR UPDATE", (pago_id,))
            pago_a_anular = cur.fetchone()
            if not pago_a_anular or pago_a_anular['estado_pago'] == 'Anulado':
                flash("Este recibo ya está anulado o no se puede anular.", "warning")
                return redirect(url_for('consulta'))
            cliente_id, cedula_cliente, nombre_cliente = pago_a_anular['cliente_id'], pago_a_anular['cedula'], f"{pago_a_anular['nombre']} {pago_a_anular['apellido']}"
            if pago_a_anular['estado_pago'] == 'Pendiente':
                cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE id = %s", (pago_id,))
            elif pago_a_anular['tipo_pago'] == 'Inscripción':
                cur.execute("UPDATE clientes SET inscripcion_pagada = inscripcion_pagada - %s WHERE id = %s", (Decimal(pago_a_anular['monto']), cliente_id))
                cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE id = %s", (pago_id,))
            elif pago_a_anular['tipo_pago'] == 'Cuota':
                cur.execute("SELECT * FROM clientes WHERE id = %s FOR UPDATE", (cliente_id,))
                cliente = cur.fetchone()
                ppr, rpr, monto = pago_a_anular.get('progresivas_cubiertas', 0), pago_a_anular.get('regresivas_cubiertas', 0), Decimal(pago_a_anular['monto'])
                cpa, cra = cliente.get('cuotas_pagadas_progresivas', 0) - ppr, cliente.get('cuotas_pagadas_regresivas', 0) - rpr
                valor_cuota = Decimal(cliente.get('valor_cuota', 0))
                if valor_cuota <= 0: raise ValueError("El cliente no tiene un valor de cuota válido.")
                vtr = (ppr + rpr) * valor_cuota
                ba = Decimal(cliente.get('balance_regresivo', 0))
                ban = (ba + vtr) - monto
                cur.execute("UPDATE clientes SET cuotas_pagadas_progresivas = %s, cuotas_pagadas_regresivas = %s, balance_regresivo = %s WHERE id = %s", (cpa, cra, ban, cliente_id))
                cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE id = %s", (pago_id,))
            elif pago_a_anular['tipo_pago'] == 'Inscripción Finalizada':
                detalles = pago_a_anular.get('detalles_reporte')
                if detalles and 'pagos_individuales' in detalles:
                    ids_a_restaurar = [p['id'] for p in detalles['pagos_individuales']]
                    cur.execute("UPDATE pagos SET estado_pago = 'Conciliado', detalles_reporte = NULL WHERE id = ANY(%s)", (ids_a_restaurar,))
                    cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE id = %s", (pago_id,))
                    cur.execute("SELECT COALESCE(SUM(monto), 0) FROM pagos WHERE id = ANY(%s)", (ids_a_restaurar,))
                    total_inscripcion_reactivada = cur.fetchone()[0]
                    cur.execute("UPDATE clientes SET inscripcion_pagada = %s, proceso = 'RESERVA' WHERE id = %s", (total_inscripcion_reactivada, cliente_id))
                    flash("¡Reversión de consolidación completada! Los abonos han sido restaurados.", 'success')
                else:
                    cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE id = %s", (pago_id,))
                    flash("Recibo final anulado, pero no se encontraron detalles para restaurar abonos.", 'warning')

            descripcion_audit = f"Anuló el recibo N° {pago_id} (Tipo: {pago_a_anular['tipo_pago']}, ${pago_a_anular['monto']}) del cliente {nombre_cliente}."
            registrar_accion_auditoria('ANULACION_RECIBO', descripcion_audit, cliente_id)
            conn.commit()
            flash(f"¡Recibo N° {pago_id} anulado y saldo corregido exitosamente!", "success")
            return redirect(url_for('consulta', busqueda=cedula_cliente))
    except (psycopg2.Error, ValueError, ConnectionError) as e:
        conn.rollback()
        flash(f'Ocurrió un error al anular el recibo: {e}', 'error')
        return redirect(url_for('consulta', busqueda=cedula_cliente))

@app.route('/verificar_recibo', methods=['GET', 'POST'])
def verificar_recibo():
    pago = None
    if request.method == 'POST':
        pago_id = request.form.get('pago_id')
        if pago_id and pago_id.isdigit():
            conn = get_db()
            if not conn:
                flash("Error de conexión a la base de datos.", "error")
            else:
                with conn.cursor() as cur:
                    query = "SELECT p.id, p.monto, p.fecha_pago, p.estado_pago, p.tipo_pago, (c.nombre || ' ' || c.apellido) as nombre_apellido FROM pagos p JOIN clientes c ON p.cliente_id = c.id WHERE p.id = %s;"
                    cur.execute(query, (int(pago_id),))
                    pago = cur.fetchone()
                    if not pago:
                        flash(f"No se encontró ningún recibo con el ID {pago_id}.", "warning")
        else:
            flash("Por favor, ingrese un número de recibo válido.", "error")

    return render_template('verificacion_recibo.html', pago=pago)

@app.route('/edit/<int:client_id>', methods=['GET', 'POST'])
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def edit_client(client_id):
    conn = get_db()
    if not conn:
        flash('Error de conexión a la base de datos.', 'error')
        return redirect(url_for('consulta'))
    
    with conn.cursor() as cur:
        cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s", (client_id,))
        cliente_actual = cur.fetchone()
    
    if not cliente_actual:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta'))

    if request.method == 'POST':
        try:
            form_data = {k: v.strip() if isinstance(v, str) else v for k, v in request.form.items()}
            
            cambios = []
            campos_a_monitorear = ['plan_contratado', 'valor_cuota', 'cuotas_totales', 'inscripcion_monto']
            for campo in campos_a_monitorear:
                valor_antiguo = cliente_actual.get(campo)
                valor_nuevo_str = form_data.get(campo)
                
                try:
                    if '.' in str(valor_antiguo) or (valor_nuevo_str and '.' in valor_nuevo_str):
                        valor_antiguo = Decimal(valor_antiguo or 0)
                        valor_nuevo = Decimal(valor_nuevo_str.replace(',', '.') if valor_nuevo_str else 0)
                    else:
                        valor_antiguo = int(valor_antiguo or 0)
                        valor_nuevo = int(valor_nuevo_str or 0)
                except (InvalidOperation, ValueError, TypeError):
                    continue

                if valor_antiguo != valor_nuevo:
                    cambios.append(f"{campo.replace('_', ' ').title()} de '{valor_antiguo}' a '{valor_nuevo}'")

            with conn.cursor() as cur:
                if 'nombre_apellido' in form_data:
                    nombre_completo = form_data['nombre_apellido'].split(' ', 1)
                    form_data['nombre'] = nombre_completo[0]
                    form_data['apellido'] = nombre_completo[1] if len(nombre_completo) > 1 else ''

                update_data = dict(cliente_actual)
                update_data.update(form_data)
                
                update_query = """
                UPDATE clientes SET
                    nombre = %(nombre)s, apellido = %(apellido)s, cedula = %(cedula)s, contrato_nro = %(contrato_nro)s,
                    telefono = %(telefono)s, asesor = %(asesor)s, responsable = %(responsable)s, fecha_ingreso = %(fecha_ingreso)s,
                    grupo = %(grupo)s, plan_contratado = %(plan_contratado)s,
                    cuotas_totales = %(cuotas_totales)s, moneda_pago = %(moneda_pago)s, valor_cuota = %(valor_cuota)s,
                    inscripcion_monto = %(inscripcion_monto)s, proceso = %(proceso)s, estatus = %(estatus)s,
                    cuotas_pagadas_progresivas = %(cuotas_pagadas_progresivas)s,
                    cuotas_pagadas_regresivas = %(cuotas_pagadas_regresivas)s
                WHERE id = %(id)s;
                """
                cur.execute(update_query, update_data)
                
                if cambios:
                    descripcion_audit = f"Modificó el plan del cliente. Cambios: {'; '.join(cambios)}."
                    registrar_accion_auditoria('MODIFICACION_PLAN_CLIENTE', descripcion_audit, client_id)
                else:
                    descripcion_audit = f"Editó los datos del cliente {update_data['nombre']} {update_data['apellido']} (C.I. {update_data['cedula']})."
                    registrar_accion_auditoria('EDICION_CLIENTE', descripcion_audit, client_id)

                conn.commit()
                flash('¡Cliente actualizado exitosamente!', 'success')
                return redirect(url_for('consulta', busqueda=update_data.get('cedula')))
        except (psycopg2.Error, ValueError, ConnectionError, InvalidOperation) as e: 
            conn.rollback()
            flash(f'Ocurrió un error al actualizar: {e}', 'error')
            
    return render_template('edit_cliente.html', cliente=cliente_actual)

@app.route('/delete/<int:client_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin')
def delete_client(client_id):
    """
    Elimina un cliente y todos sus registros asociados de la base de datos.
    """
    conn = get_db()
    if not conn: 
        flash('Error de conexión a la base de datos.', 'error')
        return redirect(url_for('consulta'))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT nombre, apellido, cedula FROM clientes WHERE id = %s", (client_id,))
            cliente_a_borrar = cur.fetchone()
            if not cliente_a_borrar:
                flash('El cliente que intenta eliminar no existe.', 'warning')
                return redirect(url_for('consulta'))
            
            # Lista definitiva y ordenada de tablas para eliminar dependencias.
            tablas_a_limpiar = [
                # Primero, tablas que dependen de 'comisiones'
                ("comisiones_rebalanceos", "comision_id_origen IN (SELECT id FROM comisiones WHERE origen_id = %s AND origen_tipo = 'Venta')"),
                # Tablas que dependen directamente de 'clientes'
                ("adjudicaciones", "(ganador_sorteo_id = %s OR ganador_oferta_id = %s)"),
                ("caja_inscripciones", "cliente_id = %s"),
                ("comisiones", "origen_id = %s AND origen_tipo = 'Venta'"),
                ("comisiones_legacy", "cliente_id = %s"),
                ("gestiones_cobranza", "cliente_id = %s"),
                ("ofertas", "cliente_id = %s"),
                ("pagos", "cliente_id = %s"),
                ("payment_bulks", "cliente_id = %s"),
                ("payment_orders", "cliente_id = %s"),
                ("receipts", "cliente_id = %s"),
                ("solicitudes", "cliente_id = %s"),
                ("registros_auditoria", "cliente_afectado_id = %s")
            ]
            
            logging.info(f"Iniciando proceso de eliminación para cliente ID: {client_id}")
            for tabla, condicion in tablas_a_limpiar:
                # El número de parámetros en la condición determina cómo se pasan los argumentos
                params = (client_id, client_id) if '%s' in condicion and condicion.count('%s') > 1 else (client_id,)
                sql = f"DELETE FROM {tabla} WHERE {condicion}"
                cur.execute(sql, params)
                logging.info(f"Se eliminaron {cur.rowcount} registros de {tabla}.")

            # Registrar la acción en la auditoría ANTES de eliminar al cliente
            descripcion_audit = f"Eliminó al cliente {cliente_a_borrar['nombre']} {cliente_a_borrar['apellido']} (C.I. {cliente_a_borrar['cedula']}) y todos sus datos asociados."
            registrar_accion_auditoria('ELIMINACION_CLIENTE', descripcion_audit, client_id)
            
            # Finalmente, eliminar el registro del cliente
            logging.info(f"Intentando eliminar el registro principal del cliente ID: {client_id}...")
            cur.execute("DELETE FROM clientes WHERE id = %s", (client_id,))
            logging.info("Registro principal del cliente eliminado.")
            
            conn.commit()
            flash('¡Cliente y todos sus registros asociados han sido eliminados exitosamente!', 'success')

    except psycopg2.Error as e:
        conn.rollback()
        print(f"ERROR DE BASE DE DATOS AL ELIMINAR CLIENTE ID {client_id}: {e}")
        flash(f"ERROR DE INTEGRIDAD: La base de datos bloqueó la eliminación. Detalles: {e}", 'danger')
        
    except Exception as e:
        conn.rollback()
        print(f"ERROR INESPERADO AL ELIMINAR CLIENTE ID {client_id}: {e}")
        flash(f'Ocurrió un error inesperado al eliminar: {e}', 'error')

    return redirect(url_for('consulta'))
    
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cedula, (nombre || ' ' || apellido) as nombre_apellido, ciclo_cobranza, cuotas_pagadas_progresivas FROM clientes WHERE id = %s", (client_id,))
            cliente_info = cur.fetchone()
            if cliente_info:
                cedula_cliente = cliente_info['cedula']
                nombre_cliente = cliente_info['nombre_apellido']
                ciclo_cliente = cliente_info['ciclo_cobranza']
                cuotas_pagadas = cliente_info['cuotas_pagadas_progresivas'] or 0
            else:
                flash("Cliente no encontrado.", "error")
                return redirect(url_for('consulta'))

            # --- INICIO DE LA CORRECCIÓN ---
            # Se aplican las mismas reglas que en el portal del cliente.
            if cuotas_pagadas < 1:
                flash("No se puede registrar la oferta: El cliente aún no ha pagado su primera cuota.", 'error')
                return redirect(url_for('consulta', busqueda=cedula_cliente))

            if cuotas_pagadas > 1:
                today = get_venezuela_current_date()
                
                fecha_vencimiento_ciclo = None
                if ciclo_cliente == '15 al 02':
                    fecha_vencimiento_ciclo = today.replace(day=2)
                elif ciclo_cliente == '20 al 10':
                    fecha_vencimiento_ciclo = today.replace(day=10)

                if fecha_vencimiento_ciclo:
                    cur.execute("""
                        SELECT 1 FROM pagos 
                        WHERE cliente_id = %s 
                        AND tipo_pago = 'Cuota' AND estado_pago = 'Conciliado'
                        AND fecha_pago > %s
                        AND EXTRACT(MONTH FROM fecha_pago) = %s
                        AND EXTRACT(YEAR FROM fecha_pago) = %s
                        LIMIT 1
                    """, (client_id, fecha_vencimiento_ciclo, today.month, today.year))
                    pago_impuntual_mes_actual = cur.fetchone() is not None
                    
                    if pago_impuntual_mes_actual:
                        flash("No se puede registrar la oferta: El cliente tiene un pago impuntual registrado en el mes actual.", 'error')
                        return redirect(url_for('consulta', busqueda=cedula_cliente))
            # --- FIN DE LA CORRECCIÓN ---

            if not cuotas_ofertadas or not cuotas_ofertadas.isdigit() or int(cuotas_ofertadas) <= 0:
                flash("Debe ingresar un número válido de cuotas para la oferta.", 'error')
                return redirect(url_for('consulta', busqueda=cedula_cliente))

            cur.execute("INSERT INTO ofertas (cliente_id, cuotas_ofertadas, fecha_oferta, estado_oferta) VALUES (%s, %s, %s, 'activa')", (client_id, int(cuotas_ofertadas), get_venezuela_current_date()))
            
            descripcion_audit = f"Registró una oferta de {cuotas_ofertadas} cuotas para el cliente {nombre_cliente}."
            registrar_accion_auditoria('REGISTRO_OFERTA', descripcion_audit, client_id)
            
            conn.commit()
            flash(f"¡Oferta de {cuotas_ofertadas} cuotas registrada exitosamente!", 'success')

    except (psycopg2.Error, ConnectionError) as e:
        conn.rollback()
        flash(f"Ocurrió un error al registrar la oferta: {e}", 'error')
    
    return redirect(url_for('consulta', busqueda=cedula_cliente))

@app.route('/adjudicacion', methods=['GET'])
@admin_required
def adjudicacion():
    conn = get_db()
    clientes_elegibles_ahorro, ofertas_activas, historial = [], [], []
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return render_template('adjudicacion.html', clientes_elegibles_ahorro=clientes_elegibles_ahorro, ofertas_activas=ofertas_activas, historial=historial)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, (nombre || ' ' || apellido) as nombre_apellido, cedula, cuotas_pagadas_progresivas, 
                       meses_retraso_entrega, plan_contratado 
                FROM clientes 
                WHERE TRIM(UPPER(proceso)) = 'AHORRADOR' AND cuotas_pagadas_progresivas >= (12 + meses_retraso_entrega) 
                AND TRIM(UPPER(estatus)) = 'ACTIVO' ORDER BY nombre, apellido;
            """)
            clientes_elegibles_ahorro = cur.fetchall()
            
            cur.execute("""
                SELECT o.cuotas_ofertadas, c.id, (c.nombre || ' ' || c.apellido) as nombre_apellido, 
                       c.cedula, c.plan_contratado
                FROM ofertas o JOIN clientes c ON o.cliente_id = c.id 
                WHERE o.estado_oferta = 'activa' AND TRIM(UPPER(c.proceso)) = 'AHORRADOR' 
                ORDER BY o.cuotas_ofertadas DESC, o.fecha_oferta ASC;
            """)
            ofertas_activas_raw = cur.fetchall()

            ofertas_activas = []
            for oferta in ofertas_activas_raw:
                oferta_dict = dict(oferta)
                
                cur.execute("SELECT COUNT(*) FROM pagos WHERE cliente_id = %s AND tipo_pago = 'Cuota' AND estado_pago = 'Conciliado'", (oferta['id'],))
                cuotas_pagadas = cur.fetchone()[0]
                oferta_dict['cuotas_pagadas'] = cuotas_pagadas if cuotas_pagadas is not None else 0

                cur.execute("SELECT COUNT(*) FROM ofertas WHERE cliente_id = %s", (oferta['id'],))
                frecuencia_ofertas = cur.fetchone()[0]
                oferta_dict['frecuencia_ofertas'] = frecuencia_ofertas if frecuencia_ofertas is not None else 0
                
                ofertas_activas.append(oferta_dict)

            cur.execute("""
                SELECT a.id, a.fecha_adjudicacion, 
                       (gs.nombre || ' ' || gs.apellido) as nombre_ganador_sorteo, 
                       (go.nombre || ' ' || go.apellido) as nombre_ganador_oferta 
                FROM adjudicaciones a 
                LEFT JOIN clientes gs ON a.ganador_sorteo_id = gs.id 
                LEFT JOIN clientes go ON a.ganador_oferta_id = go.id 
                ORDER BY a.fecha_adjudicacion DESC;
            """)
            historial = cur.fetchall()
    except psycopg2.Error as e:
        flash(f"Error al cargar datos para la adjudicación: {e}", "error")
        clientes_elegibles_ahorro, ofertas_activas, historial = [], [], []
    return render_template('adjudicacion.html', clientes_elegibles_ahorro=clientes_elegibles_ahorro, ofertas_activas=ofertas_activas, historial=historial)

@app.route('/realizar_adjudicacion', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def realizar_adjudicacion():
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('adjudicacion'))
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE clientes SET ignorar_penalidad_puntualidad = FALSE;")
            ids_ya_ganadores = set()
            cur.execute("SELECT id, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE TRIM(UPPER(proceso)) = 'AHORRADOR' AND cuotas_pagadas_progresivas >= (12 + meses_retraso_entrega) AND TRIM(UPPER(estatus)) = 'ACTIVO';")
            ganadores_ahorro = cur.fetchall()
            ids_ganadores_ahorro = [g['id'] for g in ganadores_ahorro]
            ids_ya_ganadores.update(ids_ganadores_ahorro)
            ganador_oferta = None
            cur.execute("SELECT c.id, (c.nombre || ' ' || c.apellido) as nombre_apellido, c.ignorar_penalidad_puntualidad, o.cuotas_ofertadas FROM ofertas o JOIN clientes c ON o.cliente_id = c.id WHERE o.estado_oferta = 'activa' AND TRIM(UPPER(c.proceso)) = 'AHORRADOR' AND c.id NOT IN %s;", (tuple(ids_ya_ganadores) if ids_ya_ganadores else (0,),))
            candidatos_oferta_raw = cur.fetchall()
            candidatos_oferta = []
            for c in candidatos_oferta_raw:
                cur.execute("SELECT COUNT(*) FROM ofertas WHERE cliente_id = %s;", (c['id'],))
                frecuencia = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM pagos WHERE cliente_id = %s AND puntualidad = 'Impuntual';", (c['id'],))
                impuntualidades = cur.fetchone()[0]
                candidatos_oferta.append({**c, 'frecuencia': frecuencia, 'impuntualidades': impuntualidades})
            if candidatos_oferta:
                candidatos_oferta.sort(key=lambda x: (-x['cuotas_ofertadas'], -x['frecuencia'], x['impuntualidades'] if not x['ignorar_penalidad_puntualidad'] else 0))
                ganador_oferta = candidatos_oferta[0]
                ids_ya_ganadores.add(ganador_oferta['id'])
                for perdedor in candidatos_oferta[1:]:
                    if (perdedor['cuotas_ofertadas'] == ganador_oferta['cuotas_ofertadas'] and perdedor['frecuencia'] == ganador_oferta['frecuencia'] and perdedor['impuntualidades'] > ganador_oferta['impuntualidades']):
                        cur.execute("UPDATE clientes SET ignorar_penalidad_puntualidad = TRUE WHERE id = %s;", (perdedor['id'],))
            if not ids_ya_ganadores:
                flash("No hay clientes que cumplan los criterios para ser adjudicados este ciclo.", "warning")
                return redirect(url_for('adjudicacion'))
            cur.execute("UPDATE clientes SET proceso = 'ADJUDICADO' WHERE id = ANY(%s);", (list(ids_ya_ganadores),))
            if ganador_oferta:
                cur.execute("UPDATE ofertas SET estado_oferta = 'ganadora' WHERE cliente_id = %s AND estado_oferta = 'activa';", (ganador_oferta['id'],))
                cur.execute("SELECT valor_cuota FROM clientes WHERE id = %s", (ganador_oferta['id'],))
                valor_cuota = cur.fetchone()['valor_cuota']
                monto_oferta = ganador_oferta['cuotas_ofertadas'] * valor_cuota
                fecha_limite = get_proximo_dia_habil(get_venezuela_current_date())
                
                cur.execute("""
                    INSERT INTO pagos (cliente_id, monto, tipo_pago, por_concepto_de, estado_pago, reportado_por_cliente, estado_reporte, fecha_creacion, registrado_por_id, detalles_reporte, cuotas_cubiertas)
                    VALUES (%s, %s, 'Pago Oferta', %s, 'Pendiente', FALSE, 'Generado por Sistema', %s, %s, %s::jsonb, 0)
                """, (ganador_oferta['id'], monto_oferta, f"Pago de oferta ganadora ({ganador_oferta['cuotas_ofertadas']} cuotas)", get_venezuela_current_datetime(), g.admin['id'], jsonify({'fecha_limite': fecha_limite.isoformat()}).get_data(as_text=True)))
                flash(f"¡Se generó una orden de pago por ${monto_oferta} para el ganador de la oferta!", "info")

            cur.execute("UPDATE ofertas SET estado_oferta = 'perdida' WHERE estado_oferta = 'activa';")
            ganador_oferta_id = ganador_oferta['id'] if ganador_oferta else None
            cur.execute("INSERT INTO adjudicaciones (ganador_oferta_id, ganador_sorteo_id) VALUES (%s, %s);", (ganador_oferta_id, None))
            nombres_ganadores = [g['nombre_apellido'] for g in ganadores_ahorro]
            if ganador_oferta: nombres_ganadores.append(ganador_oferta['nombre_apellido'])
            descripcion_audit = f"Ejecutó el proceso de adjudicación. Ganadores: {', '.join(nombres_ganadores)}."
            registrar_accion_auditoria('EJECUCION_ADJUDICACION', descripcion_audit)
            conn.commit()
            for g in ganadores_ahorro: flash(f"🏆 ¡Ganador por Ahorro: {g['nombre_apellido']}!", 'success')
            if ganador_oferta: flash(f"🏆 ¡Ganador por Oferta: {ganador_oferta['nombre_apellido']}!", 'success')
    except (psycopg2.Error, IndexError, KeyError, ConnectionError) as e:
        conn.rollback()
        flash(f"Ocurrió un error durante el proceso de adjudicación: {e}", 'error')
    return redirect(url_for('adjudicacion'))

@app.route('/auditoria', methods=['GET'])
@admin_required
@rol_requerido('superadmin')
def auditoria():
    conn, logs = get_db(), []
    fecha_actual_vet = get_venezuela_current_date().strftime('%Y-%m-%d')
    fecha_filtro_str = request.args.get('fecha', fecha_actual_vet)
    fecha_para_sql = fecha_filtro_str
    try:
        fecha_para_sql = datetime.strptime(fecha_filtro_str, '%m/%d/%Y').strftime('%Y-%m-%d')
    except ValueError: pass
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
    else:
        try:
            with conn.cursor() as cur:
                cur.execute("SET TIME ZONE 'America/Caracas';")
                sql = """
                    SELECT r.id, r.usuario_nombre, r.accion, r.descripcion, r.timestamp AS fecha_registro, 
                           c.nombre, c.apellido, c.cedula
                    FROM registros_auditoria r LEFT JOIN clientes c ON r.cliente_afectado_id = c.id
                    WHERE r.timestamp >= %s::date AND r.timestamp < (%s::date + '1 day'::interval)
                    ORDER BY r.timestamp DESC;
                """
                cur.execute(sql, (fecha_para_sql, fecha_para_sql))
                logs = cur.fetchall()
        except (Exception, psycopg2.Error) as e:
            flash(f"Error al consultar los registros de auditoría: {e}", "error")
    return render_template('auditoria.html', logs=logs, anio_actual=get_venezuela_current_date().year, fecha_filtro=fecha_filtro_str)

@app.route('/descargar_reporte_auditoria')
@admin_required
@rol_requerido('superadmin')
def descargar_reporte_auditoria():
    fecha_actual_vet = get_venezuela_current_date().strftime('%Y-%m-%d')
    fecha_reporte_str = request.args.get('fecha', fecha_actual_vet)
    fecha_para_sql = fecha_reporte_str
    try:
        fecha_para_sql = datetime.strptime(fecha_reporte_str, '%m/%d/%Y').strftime('%Y-%m-%d')
    except ValueError: pass
    conn = get_db()
    if not conn: return "Error de conexión a la base de datos", 500
    try:
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'America/Caracas';")
            sql = """
                SELECT r.timestamp AS fecha_registro, r.usuario_nombre, r.accion, r.descripcion, 
                       (c.nombre || ' ' || c.apellido) as cliente_nombre, c.cedula
                FROM registros_auditoria r LEFT JOIN clientes c ON r.cliente_afectado_id = c.id
                WHERE r.timestamp >= %s::date AND r.timestamp < (%s::date + '1 day'::interval)
                ORDER BY r.timestamp ASC;
            """
            cur.execute(sql, (fecha_para_sql, fecha_para_sql))
            logs = cur.fetchall()
            output, writer = io.StringIO(), csv.writer(io.StringIO())
            writer.writerow(['Fecha y Hora (VET)', 'Usuario Admin', 'Accion', 'Descripcion', 'Cliente Afectado', 'Cedula Cliente'])
            for log in logs:
                writer.writerow([log['fecha_registro'].strftime('%Y-%m-%d %H:%M:%S'), log['usuario_nombre'], log['accion'], log['descripcion'], log['cliente_nombre'] or 'N/A', log['cedula'] or 'N/A'])
            output.seek(0)
            return Response(output, mimetype="text/csv", headers={"Content-Disposition": f"attachment;filename=reporte_auditoria_{fecha_reporte_str}.csv"})
    except (psycopg2.Error, ValueError) as e:
        flash(f"Error al generar el reporte: {e}", "error")
        return redirect(url_for('auditoria'))

# =================================================================================
# ===== RUTAS DEL PORTAL Y ADMIN PARA EL FLUJO DE PAGO POR DIFERENCIA =====
# =================================================================================

@app.route('/portal/dashboard')
@portal_login_required
def portal_dashboard():
    if g.cliente is None:
        session.clear()
        flash('No se encontró su información de cliente.', 'error')
        return redirect(url_for('portal_login'))
    
    conn = get_db()
    if not conn:
        flash('No se pudo conectar con la base de datos.', 'error')
        return redirect(url_for('portal_login'))
    
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s;", (session['cliente_id'],))
            cliente = cur.fetchone()
            if not cliente:
                session.clear()
                flash('Cliente no encontrado.', 'error')
                return redirect(url_for('portal_login'))
            
            cliente_dict = dict(cliente)
            
            cur.execute("SELECT * FROM pagos WHERE cliente_id = %s AND estado_pago != 'Anulado' ORDER BY fecha_pago DESC, id DESC LIMIT 5;", (session['cliente_id'],))
            cliente_dict['pagos'] = cur.fetchall()

            # Lógica de Notificaciones
            ordenes_pendientes = []
            reportes_rechazados = [] 
            estado_principal = {} 
            cita_confirmada = None 
            
            # --- INICIO DE LA CORRECCIÓN DE LÓGICA DE COBRANZA ---
            today = get_venezuela_current_date()
            ciclo_cobranza_activo = False
            ciclo_cliente = cliente_dict.get('ciclo_cobranza')
            cuotas_pagadas_total = (cliente_dict.get('cuotas_pagadas_progresivas', 0) or 0) + (cliente_dict.get('cuotas_pagadas_regresivas', 0) or 0)

            es_periodo_de_cobranza = False
            if ciclo_cliente == '15 al 02':
                # Activo desde el día 15 hasta fin de mes, O los días 1 y 2 del mes siguiente.
                if today.day >= 15 or today.day <= 2:
                    es_periodo_de_cobranza = True
            elif ciclo_cliente == '20 al 10':
                # Activo desde el día 20 hasta fin de mes, O del día 1 al 10 del mes siguiente.
                if today.day >= 20 or today.day <= 10:
                    es_periodo_de_cobranza = True

            if es_periodo_de_cobranza:
                if cuotas_pagadas_total > 1:
                    ciclo_cobranza_activo = True
                elif cuotas_pagadas_total == 1:
                    cur.execute("""
                        SELECT fecha_pago FROM pagos 
                        WHERE cliente_id = %s AND tipo_pago = 'Cuota' AND estado_pago = 'Conciliado' 
                        ORDER BY fecha_pago ASC LIMIT 1
                    """, (session['cliente_id'],))
                    primer_pago = cur.fetchone()
                    
                    if primer_pago:
                        fecha_primer_pago = primer_pago['fecha_pago']
                        # Si el ciclo cruza al siguiente mes (ej. 15 al 02) y el pago fue el mes pasado,
                        # pero el ciclo actual corresponde a ese pago, no se debe activar.
                        # La forma más simple es: no activar el ciclo en el mismo mes calendario del primer pago.
                        if today.year > fecha_primer_pago.year or (today.year == fecha_primer_pago.year and today.month > fecha_primer_pago.month):
                            ciclo_cobranza_activo = True
            # --- FIN DE LA CORRECCIÓN ---

            CUOTAS_MINIMAS_PARA_OFERTAR = 1
            cuotas_pagadas = cliente_dict.get('cuotas_pagadas_progresivas', 0) or 0
            puede_registrar_oferta = False

            if cuotas_pagadas >= CUOTAS_MINIMAS_PARA_OFERTAR:
                if cuotas_pagadas == 1:
                    puede_registrar_oferta = True
                else:
                    fecha_vencimiento_ciclo = None
                    if ciclo_cliente == '15 al 02':
                        fecha_vencimiento_ciclo = today.replace(day=2)
                    elif ciclo_cliente == '20 al 10':
                        fecha_vencimiento_ciclo = today.replace(day=10)

                    pago_impuntual_mes_actual = False
                    if fecha_vencimiento_ciclo:
                        cur.execute("""
                            SELECT 1 FROM pagos 
                            WHERE cliente_id = %s 
                            AND tipo_pago = 'Cuota' AND estado_pago = 'Conciliado'
                            AND fecha_pago > %s
                            AND EXTRACT(MONTH FROM fecha_pago) = %s
                            AND EXTRACT(YEAR FROM fecha_pago) = %s
                            LIMIT 1
                        """, (session['cliente_id'], fecha_vencimiento_ciclo, today.month, today.year))
                        pago_impuntual_mes_actual = cur.fetchone() is not None
                    
                    puede_registrar_oferta = not pago_impuntual_mes_actual

            historial_gestiones = [] # Aquí puedes añadir la lógica para cargar las gestiones si es necesario

            if cliente_dict.get('proceso') == 'RESERVA':
                inscripcion_pagada = cliente_dict.get('inscripcion_pagada', Decimal('0.0')) or Decimal('0.0')
                inscripcion_total = cliente_dict.get('inscripcion_monto', Decimal('0.0')) or Decimal('0.0')
                
                if inscripcion_pagada < inscripcion_total:
                    monto_restante = inscripcion_total - inscripcion_pagada
                    
                    cur.execute("SELECT 1 FROM pagos WHERE cliente_id = %s AND tipo_pago = 'Inscripción' AND estado_pago = 'Pendiente'", (session['cliente_id'],))
                    pago_inscripcion_pendiente = cur.fetchone()

                    estado_principal = {
                        'titulo': 'Completa tu Inscripción',
                        'mensaje': f"¡Bienvenido a Moto Plan! Para activar tu plan, por favor completa el pago de tu inscripción. Monto restante: ${monto_restante:,.2f}",
                        'boton_texto': 'Pagar Inscripción',
                        'boton_url': url_for('portal_pagar_inscripcion'),
                        'boton_activo': not pago_inscripcion_pendiente
                    }
            
            elif cliente_dict.get('proceso') == 'INSCRITO':
                cur.execute("SELECT 1 FROM pagos WHERE cliente_id = %s AND tipo_pago = 'Cuota' AND estado_pago = 'Pendiente'", (session['cliente_id'],))
                pago_cuota_pendiente = cur.fetchone()

                estado_principal = {
                    'titulo': '¡Felicitaciones! Es hora de activar tu plan',
                    'mensaje': f"Tu inscripción ha sido completada. Para comenzar a sumar cuotas a tu plan, por favor realiza el pago de tu primera cuota por un monto de ${cliente_dict.get('valor_cuota', 0):,.2f}.",
                    'boton_texto': 'Pagar Primera Cuota',
                    'boton_url': url_for('portal_reportar_pago'),
                    'boton_activo': not pago_cuota_pendiente
                }
            
            cur.execute("SELECT * FROM payment_orders WHERE cliente_id = %s AND status = 'ISSUED'", (session['cliente_id'],))
            ordenes_pendientes = cur.fetchall()

            return render_template('portal_dashboard.html', 
                                   cliente=cliente_dict, 
                                   ordenes_pendientes=ordenes_pendientes,
                                   reportes_rechazados=reportes_rechazados,
                                   estado_principal=estado_principal,
                                   cita_confirmada=cita_confirmada,
                                   puede_registrar_oferta=puede_registrar_oferta,
                                   historial_gestiones=historial_gestiones,
                                   ciclo_cobranza_activo=ciclo_cobranza_activo
                                   )
            
    except (psycopg2.Error, KeyError) as e:
        logging.error(f"Error en portal_dashboard: {e}")
        flash('Ocurrió un error inesperado al cargar tu portal.', 'error')
        return redirect(url_for('portal_login'))


@app.route('/portal/reportar_pago', methods=['GET', 'POST'])
@portal_login_required
def portal_reportar_pago():
    conn = get_db()
    if not conn:
        flash('No se pudo conectar con la base de datos.', 'error')
        return redirect(url_for('portal_dashboard'))

    with conn.cursor() as cur:
        cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s;", (session['cliente_id'],))
        cliente = cur.fetchone()
        today_str = get_venezuela_current_date().strftime('%Y-%m-%d')
        cur.execute("SELECT tasa FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (today_str,))
        tasa_hoy = cur.fetchone()

    if not cliente:
        session.clear(); flash('No se encontró su información de cliente.', 'error'); return redirect(url_for('portal_login'))
    
    cliente_dict = dict(cliente)
    mes_actual = get_nombre_mes(get_venezuela_current_date().month)

    # --- INICIO DE LA MODIFICACIÓN ---
    monto_a_pagar_usd = Decimal('0.00')
    concepto_pago = ''
    
    # Determinar si el pago es de inscripción o de cuota
    inscripcion_pagada = cliente_dict.get('inscripcion_pagada') or Decimal('0.0')
    inscripcion_total = cliente_dict.get('inscripcion_monto') or Decimal('0.0')

    if inscripcion_pagada < inscripcion_total:
        monto_a_pagar_usd = inscripcion_total - inscripcion_pagada
        concepto_pago = f"Abono a Inscripción (restan ${monto_a_pagar_usd:,.2f})"
    else:
        monto_a_pagar_usd = cliente_dict.get('valor_cuota') or Decimal('0.0')
        concepto_pago = f"Cuota del mes de {mes_actual}"
    # --- FIN DE LA MODIFICACIÓN ---

    if request.method == 'POST':
        pago_form = {k: v.strip() if isinstance(v, str) else v for k, v in request.form.items()}
        
        try:
            with conn.cursor() as cur:
                is_diferencia = pago_form.get('is_diferencia') == 'true'
                monto_reportado_bs = Decimal(pago_form.get('monto_bs', '0.00'))
                monto_equivalente_usd = Decimal(pago_form.get('monto', '0.00'))

                if is_diferencia:
                    bulk_id, order_id = pago_form.get('bulk_id'), pago_form.get('order_id')
                    cur.execute("SELECT * FROM payment_orders WHERE id = %s AND status = 'ISSUED'", (order_id,))
                    order = cur.fetchone()
                    
                    if not order or monto_reportado_bs != order['amount']:
                        flash("La orden de pago no es válida o el monto reportado es incorrecto.", "error")
                        return redirect(url_for('portal_dashboard'))

                    pago_diferencia_query = """
                        INSERT INTO pagos (cliente_id, monto, monto_bs, tipo_pago, forma_pago, fecha_pago, referencia, banco, tasa_dia,
                                           estado_reporte, fecha_creacion, bulk_id, order_id, is_diferencia, por_concepto_de, verified_amount, reportado_por_cliente)
                        VALUES (%s, %s, %s, 'Diferencia', %s, %s, %s, %s, %s, 'DIFF_REPORTED', %s, %s, %s, TRUE, %s, %s, TRUE);"""
                    cur.execute(pago_diferencia_query, (
                        cliente['id'], monto_equivalente_usd, monto_reportado_bs, pago_form.get('forma_pago'), pago_form.get('fecha_pago'), pago_form.get('referencia'),
                        pago_form.get('banco'), pago_form.get('tasa_dia'), get_venezuela_current_datetime(), bulk_id, order_id,
                        f"Pago diferencia orden #{order_id}", simular_verificacion_bancaria(monto_reportado_bs)))
                    
                    cur.execute("UPDATE payment_orders SET status = 'PAID', paid_at = NOW() WHERE id = %s", (order_id,))
                    recalcular_totales_bulk(bulk_id)
                    flash('✅ ¡Pago de diferencia reportado! Será verificado.', 'success')
                else:
                    monto_verificado_bs = simular_verificacion_bancaria(monto_reportado_bs)
                    tipo_pago_inicial = 'Inscripción' if not (cliente_dict.get('inscripcion_pagada') or 0) >= (cliente_dict.get('inscripcion_monto') or 0) else 'Cuota'

                    pago_inicial_query = """
                        INSERT INTO pagos (cliente_id, monto, monto_bs, tipo_pago, forma_pago, fecha_pago, referencia, banco, tasa_dia,
                                        estado_reporte, fecha_creacion, verified_amount, reportado_por_cliente, por_concepto_de)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'REPORTED', %s, %s, TRUE, %s) RETURNING id;"""
                    cur.execute(pago_inicial_query, (
                        cliente['id'], monto_equivalente_usd, monto_reportado_bs, tipo_pago_inicial, pago_form.get('forma_pago'), pago_form.get('fecha_pago'),
                        pago_form.get('referencia'), pago_form.get('banco'), pago_form.get('tasa_dia'),
                        get_venezuela_current_datetime(), monto_verificado_bs, pago_form.get('por_concepto_de')))
                    pago_id = cur.fetchone()[0]

                    if monto_verificado_bs < monto_reportado_bs:
                        diferencia = monto_reportado_bs - monto_verificado_bs
                        cur.execute("INSERT INTO payment_bulks (cliente_id, currency, expected_amount, status) VALUES (%s, 'VES', %s, 'OPEN') RETURNING id", (cliente['id'], monto_reportado_bs))
                        bulk_id = cur.fetchone()[0]
                        cur.execute("UPDATE pagos SET bulk_id = %s, estado_reporte = 'TEMP_REJECTED_DIFF', rejection_reason = 'Monto verificado menor' WHERE id = %s", (bulk_id, pago_id))
                        cur.execute("INSERT INTO payment_orders (bulk_id, cliente_id, amount, status) VALUES (%s, %s, %s, 'ISSUED') RETURNING id", (bulk_id, cliente['id'], diferencia))
                        order_id = cur.fetchone()[0]
                        cur.execute("UPDATE pagos SET order_id = %s WHERE id = %s", (order_id, pago_id))
                        recalcular_totales_bulk(bulk_id)
                        flash(f"Tu pago fue recibido, pero se verificó un monto menor. Se generó una orden de pago por la diferencia de Bs. {diferencia}. Por favor, págala y repórtala.", 'warning')
                    else:
                        flash('✅ ¡Pago reportado! Será verificado.', 'success')

                conn.commit()
                return redirect(url_for('portal_dashboard'))
        except (psycopg2.Error, ValueError, InvalidOperation) as e:
            conn.rollback(); logging.error(f"Error en portal_reportar_pago: {e}"); flash(f'Ocurrió un error: {e}', 'error')

    # --- MODIFICACIÓN EN RENDER_TEMPLATE ---
    return render_template('portal_reportar_pago.html', 
                           cliente=cliente, 
                           tasas_hoy=tasa_hoy, 
                           mes_actual=mes_actual,
                           monto_a_pagar_usd=monto_a_pagar_usd,
                           concepto_pago=concepto_pago)

@app.route('/portal/diferencia/reportar/<int:bulk_id>/<int:order_id>', methods=['GET'])
@portal_login_required
def portal_diferencia_reportar(bulk_id, order_id):
    conn = get_db()
    if not conn: flash("Error de conexión.", "error"); return redirect(url_for('portal_dashboard'))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM payment_orders WHERE id = %s AND bulk_id = %s AND cliente_id = %s AND status = 'ISSUED'", (order_id, bulk_id, session['cliente_id']))
            order = cur.fetchone()
            if not order: flash("Orden de pago no encontrada o ya procesada.", "error"); return redirect(url_for('portal_dashboard'))
            
            cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s", (session['cliente_id'],))
            cliente = cur.fetchone()
            today_str = get_venezuela_current_date().strftime('%Y-%m-%d')
            cur.execute("SELECT tasa FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (today_str,))
            tasa_hoy = cur.fetchone()
    except psycopg2.Error as e:
        flash(f"Error al cargar la página de reporte: {e}", "error"); return redirect(url_for('portal_dashboard'))

    return render_template('portal_reportar_pago.html', cliente=cliente, tasas_hoy=tasa_hoy, is_diferencia=True, bulk_id=bulk_id, order_id=order_id, monto_precargado_bs=order['amount'], concepto_pago=f"Pago de diferencia (Orden #{order_id})")

@app.route('/admin/pagos/por-revisar')
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def admin_pagos_por_revisar():
    conn = get_db()
    bulks_a_revisar = []
    if not conn:
        flash("Error de conexión con la base de datos.", "danger")
        return render_template('admin_pagos_por_revisar.html', bulks=bulks_a_revisar)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT b.*, c.nombre, c.apellido
                FROM payment_bulks b JOIN clientes c ON b.cliente_id = c.id
                WHERE b.status IN ('OPEN', 'UNDER_REVIEW') 
                AND b.total_verified >= b.expected_amount
                ORDER BY b.created_at ASC;
            """)
            bulks_db = cur.fetchall()
            for bulk in bulks_db:
                bulk_dict = dict(bulk)
                cur.execute("SELECT * FROM pagos WHERE bulk_id = %s ORDER BY fecha_creacion ASC", (bulk['id'],))
                bulk_dict['lineas'] = cur.fetchall()
                bulks_a_revisar.append(bulk_dict)
    except psycopg2.Error as e:
        logging.error(f"Error al obtener bulks por revisar: {e}")
        flash("Error al cargar la lista de pagos por revisar.", "danger")
    return render_template('admin_pagos_por_revisar.html', bulks=bulks_a_revisar)

@app.route('/admin/pagos/<int:bulk_id>/aprobar', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def aprobar_bulk(bulk_id):
    conn = get_db()
    if not conn: flash("Error de conexión.", "danger"); return redirect(url_for('admin_pagos_por_revisar'))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM payment_bulks WHERE id = %s", (bulk_id,))
            bulk = cur.fetchone()
            if not bulk or bulk['total_verified'] < bulk['expected_amount']:
                flash("No se puede aprobar este bulk, el monto verificado es insuficiente.", "error")
                return redirect(url_for('admin_pagos_por_revisar'))
            
            cur.execute("UPDATE pagos SET estado_reporte = 'APPROVED' WHERE bulk_id = %s", (bulk_id,))
            cur.execute("UPDATE payment_bulks SET status = 'READY_TO_RECONCILE' WHERE id = %s", (bulk_id,))
            registrar_accion_auditoria('APROBACION_BULK', f"Aprobó el bulk #{bulk_id}.", bulk['cliente_id'])
            conn.commit()
            flash(f"Bulk #{bulk_id} aprobado y listo para conciliación.", "success")
    except psycopg2.Error as e:
        conn.rollback(); flash(f"Error al aprobar: {e}", "error")
    return redirect(url_for('admin_pagos_por_revisar'))

@app.route('/admin/conciliacion')
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def admin_conciliacion():
    conn = get_db()
    bulks_a_conciliar = []
    if not conn:
        flash("Error de conexión.", "danger")
        return render_template('admin_conciliacion.html', bulks=bulks_a_conciliar)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT b.*, c.nombre, c.apellido
                FROM payment_bulks b JOIN clientes c ON b.cliente_id = c.id
                WHERE b.status = 'READY_TO_RECONCILE' ORDER BY b.updated_at ASC;
            """)
            bulks_a_conciliar = cur.fetchall()
    except psycopg2.Error as e:
        flash(f"Error al cargar bulks para conciliar: {e}", "danger")
    return render_template('admin_conciliacion.html', bulks=bulks_a_conciliar)

@app.route('/api/admin/conciliacion/<int:bulk_id>')
@admin_required
def get_detalle_conciliacion(bulk_id):
    conn = get_db()
    if not conn: return jsonify({'error': 'Error de conexión'}), 500
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM payment_bulks WHERE id = %s", (bulk_id,))
            bulk = cur.fetchone()
            if not bulk: return jsonify({'error': 'Bulk no encontrado'}), 404
            
            cur.execute("SELECT * FROM pagos WHERE bulk_id = %s ORDER BY fecha_creacion ASC", (bulk_id,))
            lineas = cur.fetchall()
            
            bulk_dict = {k: (str(v) if isinstance(v, (Decimal, datetime, date)) else v) for k, v in dict(bulk).items()}
            lineas_list = [{k: (str(v) if isinstance(v, (Decimal, datetime, date)) else v) for k, v in dict(linea).items()} for linea in lineas]
            
            return jsonify({'bulk': bulk_dict, 'lineas': lineas_list})
    except psycopg2.Error as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/conciliacion/<int:bulk_id>/conciliar', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def conciliar_bulk(bulk_id):
    conn = get_db()
    if not conn: flash("Error de conexión.", "danger"); return redirect(url_for('admin_conciliacion'))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM payment_bulks WHERE id = %s AND status = 'READY_TO_RECONCILE'", (bulk_id,))
            bulk = cur.fetchone()
            if not bulk:
                flash("El bulk no está listo para conciliar.", "error")
                return redirect(url_for('admin_conciliacion'))
            
            cur.execute("SELECT * FROM pagos WHERE bulk_id = %s", (bulk_id,))
            lineas = cur.fetchall()
            
            lineas_data = [
                {
                    "id": l['id'], 
                    "monto_verificado": str(l['verified_amount']), 
                    "referencia": l['referencia'], 
                    "banco": l['banco'],
                    "fecha": l['fecha_pago'].isoformat(), 
                    "es_diferencia": l['is_diferencia']
                } for l in lineas
            ]
            receipt_data = json.dumps({"lineas_consolidadas": lineas_data})
            
            cur.execute("INSERT INTO receipts (bulk_id, cliente_id, currency, total, data) VALUES (%s, %s, %s, %s, %s) RETURNING id", 
                        (bulk_id, bulk['cliente_id'], bulk['currency'], bulk['total_verified'], receipt_data))
            receipt_id = cur.fetchone()[0]
            
            cur.execute("UPDATE pagos SET estado_pago = 'Conciliado', estado_reporte = 'RECONCILED', conciliado_por_id = %s WHERE bulk_id = %s", (g.admin['id'], bulk_id))
            cur.execute("UPDATE payment_orders SET status = 'CLOSED' WHERE bulk_id = %s", (bulk_id,))
            cur.execute("UPDATE payment_bulks SET status = 'RECONCILED' WHERE id = %s", (bulk_id,))
            
            registrar_accion_auditoria('CONCILIACION_BULK', f"Concilió bulk #{bulk_id}, recibo consolidado #{receipt_id}", bulk['cliente_id'])
            conn.commit()
            flash(f"Bulk #{bulk_id} conciliado. Recibo consolidado #{receipt_id} generado.", "success")
    except psycopg2.Error as e:
        conn.rollback(); flash(f"Error al conciliar: {e}", "error")
    return redirect(url_for('admin_conciliacion'))

@app.route('/recibo_consolidado/<int:receipt_id>')
def ver_recibo_consolidado(receipt_id):
    conn = get_db()
    if not conn: flash("Error de conexión.", "error"); return redirect(url_for('home'))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT r.*, c.nombre, c.apellido, c.cedula FROM receipts r JOIN clientes c ON r.cliente_id = c.id WHERE r.id = %s", (receipt_id,))
            receipt = cur.fetchone()
        if not receipt: flash("Recibo no encontrado.", "error"); return redirect(url_for('home'))
        
        receipt_dict = dict(receipt)
        if isinstance(receipt_dict['data'], str):
            receipt_dict['data'] = json.loads(receipt_dict['data'])
        else:
            receipt_dict['data'] = receipt_dict['data']

        return render_template('recibo_consolidado.html', receipt=receipt_dict)
    except (psycopg2.Error, json.JSONDecodeError) as e:
        flash(f"Error al cargar recibo: {e}", "error"); return redirect(url_for('home'))

# =================================================================================
# ===== RUTAS DEL PORTAL DEL CLIENTE (EXISTENTES) =====
# =================================================================================

@app.route('/portal/login', methods=['GET', 'POST'])
def portal_login():
    if 'cliente_id' in session:
        flash('Ya tienes una sesión activa. Redirigiendo a tu portal.', 'info')
        return redirect(url_for('portal_dashboard'))
    if request.method == 'POST':
        cedula, contrato_nro = request.form.get('cedula', '').strip().replace('V-', '').replace('v-', ''), request.form.get('contrato_nro', '').strip().upper().replace('MP-', '')
        if not cedula or not contrato_nro:
            flash('La cédula y el número de contrato son obligatorios.', 'error')
            return render_template('portal_login.html', anio_actual=get_venezuela_current_date().year)
        conn = get_db()
        if not conn:
            flash('Error de conexión con el servidor. Intente más tarde.', 'error')
            return render_template('portal_login.html', anio_actual=get_venezuela_current_date().year)
        try:
            with conn.cursor() as cur:
                sql_query = "SELECT id, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE TRIM(cedula) = %s AND SPLIT_PART(REPLACE(TRIM(contrato_nro), 'MP-', ''), '.', 1) = %s;"
                cur.execute(sql_query, (cedula, contrato_nro))
                cliente = cur.fetchone()
            if cliente:
                session.clear()
                session['cliente_id'], session['cliente_nombre'] = cliente['id'], cliente['nombre_apellido']
                return redirect(url_for('portal_dashboard'))
            else:
                flash('Credenciales incorrectas. Verifique sus datos e intente de nuevo.', 'error')
        except psycopg2.Error as e:
            flash(f'Error de base de datos: {e}', 'error')
    return render_template('portal_login.html', anio_actual=get_venezuela_current_date().year)

@app.route('/portal/pagos')
@portal_login_required
def portal_pagos():
    if g.cliente is None:
        return redirect(url_for('portal_login'))
    
    conn = get_db()
    if not conn:
        flash('No se pudo conectar con la base de datos.', 'error')
        return redirect(url_for('portal_dashboard'))
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM pagos 
                WHERE cliente_id = %s AND estado_pago != 'Anulado'
                ORDER BY fecha_pago DESC, id DESC;
            """, (session['cliente_id'],))
            pagos = cur.fetchall()

            cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s;", (session['cliente_id'],))
            cliente = cur.fetchone()

        return render_template('portal_pagos.html', cliente=cliente, pagos=pagos)

    except psycopg2.Error as e:
        logging.error(f"Error en portal_pagos: {e}")
        flash('Ocurrió un error al cargar tu historial de pagos.', 'error')
        return redirect(url_for('portal_dashboard'))

@app.route('/portal/documentos')
@portal_login_required
def portal_documentos():
    if g.cliente is None:
        return redirect(url_for('portal_login'))

    conn = get_db()
    if not conn:
        flash('No se pudo conectar con la base de datos.', 'error')
        return redirect(url_for('portal_dashboard'))
        
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s;", (session['cliente_id'],))
            cliente = cur.fetchone()

        documentos = [
            {
                'nombre': f"Contrato N° {cliente['contrato_nro']}",
                'descripcion': 'Copia digital de tu contrato de plan de ahorro.',
                'url': url_for('generar_contrato', client_id=cliente['id']),
                'icono': 'bi-file-text-fill'
            }
        ]
        
        return render_template('portal_documentos.html', cliente=cliente, documentos=documentos)
    
    except psycopg2.Error as e:
        logging.error(f"Error en portal_documentos: {e}")
        flash('Ocurrió un error al cargar tus documentos.', 'error')
        return redirect(url_for('portal_dashboard'))

@app.route('/generar_contrato/<int:client_id>')
def generar_contrato(client_id):
    # Verifica si es un admin o el cliente correcto
    is_admin = 'admin_id' in session
    is_correct_client = 'cliente_id' in session and session['cliente_id'] == client_id
    
    if not is_admin and not is_correct_client:
        flash('Acceso no autorizado.', 'error')
        return redirect(url_for('home'))

    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('home'))

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s", (client_id,))
            cliente = cur.fetchone()
        
        if not cliente:
            flash('Cliente no encontrado.', 'error')
            return redirect(url_for('home'))

        # Renderiza la plantilla del contrato
        # Asumo que tienes una función como get_venezuela_current_date() definida en otra parte de tu código.
        return render_template('contrato.html', cliente=cliente, modo_pre_registro=False, anio_actual=get_venezuela_current_date().year)
    except psycopg2.Error as e:
        flash(f"Error al generar el contrato: {e}", "error")
        return redirect(url_for('home'))

@app.route('/portal/citas/cancelar/<int:solicitud_id>', methods=['POST'])
@portal_login_required
def cancelar_cita_cliente(solicitud_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión.", "danger")
        return redirect(url_for('portal_dashboard'))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, cliente_id, estado FROM solicitudes WHERE id = %s", (solicitud_id,))
            solicitud = cur.fetchone()
            if not solicitud or solicitud['cliente_id'] != session['cliente_id']:
                flash("No tienes permiso para cancelar esta cita.", "error")
                return redirect(url_for('portal_dashboard'))
            if solicitud['estado'] != 'Aprobada':
                flash("Esta cita no se puede cancelar porque no está aprobada.", "warning")
                return redirect(url_for('portal_dashboard'))
            
            cur.execute("UPDATE solicitudes SET estado = 'Cancelada' WHERE id = %s", (solicitud_id,))
            registrar_accion_auditoria('CANCELACION_CITA_CLIENTE', f"Cliente canceló su cita N°{solicitud_id}.")
            conn.commit()
            flash("Tu cita ha sido cancelada exitosamente.", "success")
    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Error al cancelar la cita: {e}", "error")
    return redirect(url_for('portal_dashboard'))

@app.route('/portal/pagar_diferencia/<int:pago_original_id>')
@portal_login_required
def pagar_diferencia(pago_original_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('portal_dashboard'))

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM pagos WHERE id = %s AND cliente_id = %s", (pago_original_id, session['cliente_id']))
        pago_original = cur.fetchone()
        
        cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s;", (session['cliente_id'],))
        cliente = cur.fetchone()
        
        today_str = get_venezuela_current_date().strftime('%Y-%m-%d')
        cur.execute("SELECT tasa, tasa_euro FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (today_str,))
        tasas_hoy = cur.fetchone()

    if not pago_original or pago_original['estado_reporte'] != 'Inconsistente':
        flash('No se puede procesar el pago de esta diferencia.', 'error')
        return redirect(url_for('portal_dashboard'))

    detalles = {}
    if pago_original['detalles_reporte']:
        if isinstance(pago_original['detalles_reporte'], str):
            detalles = json.loads(pago_original['detalles_reporte'])
        else:
            detalles = pago_original['detalles_reporte']
    
    monto_diferencia = detalles.get('monto_pendiente')

    if not monto_diferencia:
        flash('No hay un monto de diferencia registrado para este pago.', 'error')
        return redirect(url_for('portal_dashboard'))
    
    if pago_original['tipo_pago'] == 'Inscripción':
        return render_template(
            'portal_pagar_inscripcion.html',
            cliente=cliente,
            tasas_hoy=tasas_hoy,
            monto_precargado=monto_diferencia,
            pago_origen_id=pago_original_id,
            concepto_pago=f"Diferencia del pago de inscripción #{pago_original_id}"
        )
    else:
        return render_template(
            'portal_reportar_pago.html',
            cliente=cliente,
            tasas_hoy=tasas_hoy,
            monto_precargado=monto_diferencia,
            pago_origen_id=pago_original_id,
            concepto_pago=f"Diferencia del pago #{pago_original_id}"
        )

@app.route('/portal/corregir_reporte/<int:pago_id>', methods=['GET'])
@portal_login_required
def portal_corregir_reporte(pago_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión.", 'error')
        return redirect(url_for('portal_dashboard'))
    
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM pagos WHERE id = %s AND cliente_id = %s", (pago_id, session['cliente_id']))
        pago_a_corregir = cur.fetchone()
        
        cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s;", (session['cliente_id'],))
        cliente = cur.fetchone()
        
        today_str = get_venezuela_current_date().strftime('%Y-%m-%d')
        cur.execute("SELECT tasa, tasa_euro FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (today_str,))
        tasas_hoy = cur.fetchone()

    if not pago_a_corregir or pago_a_corregir['estado_reporte'] != 'Inconsistente':
        flash('Este reporte no se puede corregir.', 'error')
        return redirect(url_for('portal_dashboard'))
    
    return render_template('portal_reportar_pago.html', 
                           cliente=cliente,
                           tasas_hoy=tasas_hoy,
                           pago_a_corregir=pago_a_corregir, 
                           modo_correccion=True,
                           mes_actual=get_nombre_mes(get_venezuela_current_date().month))


@app.route('/portal/pagar_inscripcion', methods=['GET', 'POST'])
@portal_login_required
def portal_pagar_inscripcion():
    conn = get_db()
    if not conn:
        flash('No se pudo conectar con la base de datos.', 'error')
        return redirect(url_for('portal_dashboard'))

    with conn.cursor() as cur:
        cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s;", (session['cliente_id'],))
        cliente = cur.fetchone()
        
        today_str = get_venezuela_current_date().strftime('%Y-%m-%d')
        cur.execute("SELECT tasa, tasa_euro FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (today_str,))
        tasas_hoy = cur.fetchone()

    if not cliente:
        session.clear()
        flash('No se encontró su información de cliente.', 'error')
        return redirect(url_for('portal_login'))

    inscripcion_monto = cliente.get('inscripcion_monto') or Decimal('0.0')
    inscripcion_pagada = cliente.get('inscripcion_pagada') or Decimal('0.0')

    if inscripcion_pagada >= inscripcion_monto:
        flash('Tu inscripción ya está completa.', 'info')
        return redirect(url_for('portal_dashboard'))
    
    monto_restante = inscripcion_monto - inscripcion_pagada

    if request.method == 'POST':
        pago_form = {k: v if v else None for k, v in request.form.items()}
        if not all(pago_form.get(key) for key in ['monto', 'fecha_pago']):
            flash('Error: Monto y fecha de pago son campos obligatorios.', 'error')
            return render_template('portal_pagar_inscripcion.html', cliente=cliente, monto_restante=monto_restante, tasas_hoy=tasas_hoy)

        forma_pago = pago_form.get('forma_pago')
        if forma_pago != 'Efectivo' and not pago_form.get('referencia'):
            flash('Error: La referencia es obligatoria para este método de pago.', 'error')
            return render_template('portal_pagar_inscripcion.html', cliente=cliente, monto_restante=monto_restante, tasas_hoy=tasas_hoy)

        try:
            with conn.cursor() as cur:
                pago_origen_id = pago_form.get('pago_origen_id')

                pago_query = """
                    INSERT INTO pagos (cliente_id, monto, tipo_pago, forma_pago, fecha_pago, pago_en, por_concepto_de, referencia, banco, tasa_dia, monto_bs, 
                                       estado_pago, cuotas_cubiertas, reportado_por_cliente, estado_reporte, fecha_creacion, detalles_reporte, pago_padre_id) 
                    VALUES (%s, %s, 'Inscripción', %s, %s, %s, %s, %s, %s, %s, %s, 'Pendiente', 0, TRUE, 'Pendiente de Revision', %s, %s::jsonb, %s);
                """
                fecha_actual_vet = get_venezuela_current_datetime()
                
                detalles_pago = {}
                if forma_pago == 'Pago Móvil':
                    detalles_pago['telefono_emisor'] = pago_form.get('pago_movil_telefono')
                    detalles_pago['cedula_emisor'] = pago_form.get('pago_movil_cedula')
                elif forma_pago == 'Binance':
                    detalles_pago['usuario_binance'] = pago_form.get('binance_user')
                detalles_json = json.dumps(detalles_pago) if detalles_pago else None

                concepto = "Pago de Diferencia de Inscripción" if pago_origen_id else "Pago de Inscripción"

                cur.execute(pago_query, (
                    session['cliente_id'], pago_form['monto'], forma_pago, pago_form['fecha_pago'], pago_form.get('pago_en'), 
                    concepto, pago_form.get('referencia'), pago_form.get('banco'), pago_form.get('tasa_dia'), 
                    pago_form.get('monto_bs'), fecha_actual_vet, detalles_json, pago_origen_id
                ))
                conn.commit()
                flash('✅ ¡Pago de inscripción reportado! Será verificado por un administrador.', 'success')
                return redirect(url_for('portal_dashboard'))
        except (psycopg2.Error, ValueError, TypeError) as e:
            conn.rollback()
            flash(f'Ocurrió un error al reportar el pago: {e}', 'error')

    return render_template('portal_pagar_inscripcion.html', cliente=cliente, monto_restante=monto_restante, tasas_hoy=tasas_hoy)

@app.route('/portal/estado_cuenta')
@portal_login_required
def portal_estado_cuenta():
    if g.cliente is None:
        return redirect(url_for('portal_login'))
    
    cliente_id = session['cliente_id']
    datos_cuenta = _get_estado_cuenta_data(cliente_id)
    if not datos_cuenta:
        return redirect(url_for('portal_dashboard'))

    return render_template('portal_estado_cuenta.html', **datos_cuenta)

@app.route('/admin/estado_cuenta/<int:cliente_id>')
@admin_required
def admin_estado_cuenta(cliente_id):
    datos_cuenta = _get_estado_cuenta_data(cliente_id)
    if not datos_cuenta:
        return redirect(url_for('consulta'))
        
    return render_template('admin_estado_cuenta.html', **datos_cuenta)

def _get_estado_cuenta_data(cliente_id):
    conn = get_db()
    if not conn:
        flash('No se pudo conectar con la base de datos.', 'error')
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s;", (cliente_id,))
            cliente = cur.fetchone()
            if not cliente:
                flash('Cliente no encontrado.', 'error')
                return None
            
            cur.execute("""
                SELECT id, fecha_creacion as fecha, 'Pago' as tipo_base, 
                       json_build_object(
                           'monto', monto, 'estado', estado_pago, 'concepto', por_concepto_de, 
                           'tasa_dia', tasa_dia, 'monto_bs', monto_bs, 'moneda_ref', moneda_referencia,
                           'detalles', detalles_reporte, 'es_credito', TRUE
                       ) as data
                FROM pagos WHERE cliente_id = %s
                UNION ALL
                SELECT id, fecha_creacion as fecha, 'Gestion' as tipo_base,
                       json_build_object('nota', nota, 'tipo_gestion', tipo_gestion) as data
                FROM gestiones_cobranza WHERE cliente_id = %s
                UNION ALL
                SELECT id, fecha_oferta::timestamp as fecha, 'Oferta' as tipo_base,
                       json_build_object('cuotas', cuotas_ofertadas, 'estado', estado_oferta) as data
                FROM ofertas WHERE cliente_id = %s
                UNION ALL
                SELECT id, fecha_creacion as fecha, 'Solicitud' as tipo_base,
                       json_build_object('tipo', tipo_solicitud, 'estado', estado, 'detalles', detalles) as data
                FROM solicitudes WHERE cliente_id = %s
                ORDER BY fecha DESC;
            """, (cliente_id, cliente_id, cliente_id, cliente_id))
            
            historial_raw = cur.fetchall()
            historial_unificado = []

            for item in historial_raw:
                data = item['data']
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except json.JSONDecodeError:
                        data = {}
                
                evento = {'fecha': item['fecha']}
                
                if item['tipo_base'] == 'Pago':
                    estado = data.get('estado', 'N/A')
                    if estado == 'Conciliado':
                        evento['tipo'] = 'Pago Conciliado'
                        evento['clase_css'] = 'bg-green-100 text-green-800'
                    elif estado == 'Pendiente':
                        evento['tipo'] = 'Pago en Revisión'
                        evento['clase_css'] = 'bg-yellow-100 text-yellow-800'
                    else:
                        evento['tipo'] = f'Pago {estado}'
                        evento['clase_css'] = 'bg-red-100 text-red-800'
                    
                    evento['descripcion'] = data.get('concepto', 'Pago general')
                    evento['monto'] = data.get('monto')
                    evento['es_credito'] = data.get('es_credito', False)
                    detalles = []
                    if data.get('monto_bs') and data.get('tasa_dia'):
                        detalles.append(f"Tasa: {data['tasa_dia']:,.2f} Bs/{data.get('moneda_ref', 'USD')}")
                    if data.get('detalles'):
                        if 'motivo' in data['detalles']: detalles.append(f"Motivo Rechazo: {data['detalles']['motivo']}")
                        if 'diferencia' in data['detalles'] and float(data['detalles']['diferencia']) > 0:
                            detalles.append(f"Monto Diferencia: ${float(data['detalles']['diferencia']):,.2f}")
                    evento['detalles'] = ' | '.join(detalles)
                    if estado == 'Conciliado':
                        evento['url'] = url_for('generar_recibo_pago', pago_id=item['id'])

                elif item['tipo_base'] == 'Gestion':
                    evento['tipo'] = data.get('tipo_gestion', 'Gestión')
                    evento['clase_css'] = 'bg-slate-100 text-slate-800'
                    evento['descripcion'] = data.get('nota', 'Sin descripción.')
                
                elif item['tipo_base'] == 'Oferta':
                    evento['tipo'] = 'Oferta de Adjudicación'
                    evento['clase_css'] = 'bg-blue-100 text-blue-800'
                    evento['descripcion'] = f"Ofertó {data.get('cuotas', 0)} cuotas. Resultado: {data.get('estado', 'N/A').capitalize()}"

                elif item['tipo_base'] == 'Solicitud':
                    tipo_solicitud = data.get('tipo', 'General')
                    evento['tipo'] = f"Solicitud de {tipo_solicitud}"
                    evento['clase_css'] = 'bg-indigo-100 text-indigo-800'
                    evento['descripcion'] = f"Estado: {data.get('estado', 'N/A')}"
                    detalles_sol = data.get('detalles', {})
                    if tipo_solicitud == 'Cita' and detalles_sol.get('reporte'):
                        evento['url'] = url_for('ver_reporte_cita', solicitud_id=item['id'])

                historial_unificado.append(evento)

            cur.execute("SELECT COALESCE(SUM(monto), 0) FROM pagos WHERE cliente_id = %s AND estado_pago = 'Conciliado'", (cliente_id,))
            total_pagado = cur.fetchone()[0]
            
            valor_plan = cliente.get('plan_contratado')
            try:
                valor_plan_decimal = Decimal(valor_plan) if valor_plan else Decimal('0.0')
            except (InvalidOperation, TypeError):
                valor_plan_decimal = Decimal('0.0')

            saldo_pendiente = valor_plan_decimal - total_pagado

            return {
                'cliente': cliente,
                'historial': historial_unificado,
                'total_pagado': total_pagado,
                'valor_plan': valor_plan_decimal,
                'saldo_pendiente': saldo_pendiente
            }

    except (psycopg2.Error, json.JSONDecodeError, KeyError) as e:
        logging.error(f"Error getting account statement for client {cliente_id}: {e}")
        flash('Ocurrió un error al generar el estado de cuenta.', 'error')
        return None

@app.route('/portal/logout')
def portal_logout():    
    session.clear()
    flash('Has cerrado sesión exitosamente.', 'success')
    return redirect(url_for('portal_login'))

@app.route('/citas/disponibilidad')
@portal_login_required
def citas_disponibilidad():
    fecha_str = request.args.get('fecha')
    if not fecha_str: return jsonify({'error': 'Fecha no proporcionada'}), 400
    try:
        fecha_obj = datetime.strptime(fecha_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Formato de fecha inválido'}), 400
    
    ahora_dt = get_venezuela_current_datetime()
    ahora_fecha = ahora_dt.date()

    if fecha_obj < ahora_fecha:
        return jsonify({'ocupados': [], 'es_habil': False, 'mensaje': 'No se pueden agendar citas en fechas pasadas.'})

    if fecha_obj.weekday() >= 5:
        return jsonify({'ocupados': [], 'es_habil': False, 'mensaje': 'No se agendan citas los fines de semana.'})
    
    feriados = get_feriados_venezuela(fecha_obj.year)
    if fecha_obj in feriados:
        return jsonify({'ocupados': [], 'es_habil': False, 'mensaje': 'El día seleccionado es feriado.'})
    
    conn = get_db()
    if not conn: return jsonify({'error': 'Error de conexión con la base de datos'}), 500
    
    citas_ocupadas = []
    try:
        with conn.cursor() as cur:
            query = "SELECT detalles->>'hora_cita' as hora_ocupada FROM solicitudes WHERE tipo_solicitud = 'Cita' AND estado = 'Aprobada' AND (detalles->>'fecha_cita')::date = %s"
            cur.execute(query, (fecha_obj,))
            resultados = cur.fetchall()
            citas_ocupadas = [row['hora_ocupada'] for row in resultados if row['hora_ocupada']]
    except psycopg2.Error as e:
        logging.error(f"Error al consultar disponibilidad de citas: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500
        
    return jsonify({'ocupados': list(set(citas_ocupadas)), 'es_habil': True})

@app.route('/portal/guardar_oferta', methods=['POST'])
@portal_login_required
def portal_guardar_oferta():
    conn = get_db()
    cuotas_ofertadas = request.form.get('cuotas_ofertadas')
    cliente_id = session['cliente_id']

    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('portal_dashboard'))
    
    try:
        with conn.cursor() as cur:
            if not cuotas_ofertadas or not cuotas_ofertadas.isdigit() or int(cuotas_ofertadas) <= 0:
                flash("Debe ingresar un número válido de cuotas para la oferta.", 'error')
                return redirect(url_for('portal_dashboard'))

            cur.execute("INSERT INTO ofertas (cliente_id, cuotas_ofertadas, fecha_oferta, estado_oferta) VALUES (%s, %s, %s, 'activa')", 
                        (cliente_id, int(cuotas_ofertadas), get_venezuela_current_date()))
            
            conn.commit()
            registrar_accion_auditoria('REGISTRO_OFERTA_CLIENTE', f"Cliente ofertó {cuotas_ofertadas} cuota(s).")
            flash(f"¡Tu oferta de {cuotas_ofertadas} cuotas ha sido registrada exitosamente!", 'success')
    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Ocurrió un error al registrar tu oferta: {e}", 'error')
        
    return redirect(url_for('portal_dashboard'))

@app.route('/portal/solicitar_cita', methods=['POST'])
@portal_login_required
def portal_solicitar_cita():
    conn = get_db()
    cliente_id = session['cliente_id']
    fecha_cita = request.form.get('fecha_cita')
    hora_cita = request.form.get('hora_cita')
    motivo_cita_select = request.form.get('motivo_cita')
    motivo_otro_texto = request.form.get('motivo_otro_texto')

    if not all([fecha_cita, hora_cita, motivo_cita_select]):
        flash('Todos los campos son requeridos para solicitar la cita.', 'error')
        return redirect(url_for('portal_dashboard'))

    motivo_final = motivo_otro_texto if motivo_cita_select == 'Otro' else motivo_cita_select

    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('portal_dashboard'))
    
    try:
        with conn.cursor() as cur:
            detalles = json.dumps({'fecha_cita': fecha_cita, 'hora_cita': hora_cita, 'motivo': motivo_final})
            cur.execute("INSERT INTO solicitudes (cliente_id, tipo_solicitud, detalles, fecha_creacion, estado) VALUES (%s, 'Cita', %s, %s, 'Pendiente')",
                        (cliente_id, detalles, get_venezuela_current_datetime()))
            conn.commit()
            registrar_accion_auditoria('SOLICITUD_CITA', f"Cliente solicitó cita para el {fecha_cita} a las {hora_cita}.")
            flash(f"Tu solicitud de cita para el {fecha_cita} a las {hora_cita} ha sido enviada. Un asesor te contactará para confirmar.", 'success')
    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Error al enviar tu solicitud: {e}", 'error')
        
    return redirect(url_for('portal_dashboard'))

@app.route('/portal/solicitar_congelamiento', methods=['POST'])
@portal_login_required
def portal_solicitar_congelamiento():
    conn = get_db()
    cliente_id = session['cliente_id']
    motivo = request.form.get('motivo')
    tiempo = request.form.get('tiempo_congelamiento')

    if not motivo or not tiempo:
        flash("El motivo y el tiempo son requeridos para la solicitud.", "error")
        return redirect(url_for('portal_dashboard'))

    if not conn:
        flash("Error de conexión.", 'error')
        return redirect(url_for('portal_dashboard'))
        
    try:
        with conn.cursor() as cur:
            detalles = json.dumps({'motivo': motivo, 'tiempo_congelamiento': tiempo})
            cur.execute("INSERT INTO solicitudes (cliente_id, tipo_solicitud, detalles, fecha_creacion, estado) VALUES (%s, 'Congelamiento', %s, %s, 'Pendiente')",
                        (cliente_id, detalles, get_venezuela_current_datetime()))
        conn.commit()
        registrar_accion_auditoria(
            'SOLICITUD_CONGELAMIENTO',
            f"Cliente solicitó congelar por {tiempo}. Motivo: {motivo[:50]}..."
        )
        flash("Solicitud de congelamiento enviada. Un asesor la revisará.", 'success')
    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Error al enviar tu solicitud: {e}", 'error')

    return redirect(url_for('portal_dashboard'))

@app.route('/portal/solicitar_descongelamiento', methods=['POST'])
@portal_login_required
def portal_solicitar_descongelamiento():
    conn, cliente_id = get_db(), session['cliente_id']
    motivo = request.form.get('motivo_reactivacion')
    if not motivo:
        flash("Debes proporcionar un motivo para la reactivación.", "error")
        return redirect(url_for('portal_dashboard'))
    try:
        with conn.cursor() as cur:
            detalles = json.dumps({'motivo': motivo})
            cur.execute(
                "INSERT INTO solicitudes (cliente_id, tipo_solicitud, detalles, fecha_creacion, estado) VALUES (%s, 'Descongelamiento', %s, %s, 'Pendiente')",
                (cliente_id, detalles, get_venezuela_current_datetime())
            )
            conn.commit()
            registrar_accion_auditoria(
                'SOLICITUD_DESCONGELAMIENTO',
                f"Cliente solicitó reactivar. Motivo: {motivo[:50]}..."
            )
            flash("Solicitud de reactivación enviada. Un asesor la revisará.", 'success')
    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Error al enviar tu solicitud: {e}", 'error')
    return redirect(url_for('portal_dashboard'))

@app.route('/portal/solicitar_retiro', methods=['POST'])
@portal_login_required
def portal_solicitar_retiro():
    conn = get_db()
    cliente_id = session['cliente_id']
    fecha_correo = request.form.get('fecha_correo_retiro')
    email_origen = request.form.get('email_origen_retiro')

    if not fecha_correo or not email_origen:
        flash("Error: La fecha y el correo de origen son necesarios para procesar la solicitud de retiro.", 'danger')
        return redirect(url_for('portal_dashboard'))

    if not conn:
        flash("Error de conexión con la base de datos.", 'error')
        return redirect(url_for('portal_dashboard'))

    try:
        with conn.cursor() as cur:
            detalles = json.dumps({
                'mensaje': 'Cliente confirma envío de correo para formalizar retiro.',
                'fecha_envio_correo': fecha_correo,
                'email_origen': email_origen
            })
            cur.execute(
                "INSERT INTO solicitudes (cliente_id, tipo_solicitud, detalles, fecha_creacion, estado) VALUES (%s, 'Retiro', %s, %s, 'Pendiente')",
                (cliente_id, detalles, get_venezuela_current_datetime())
            )
            conn.commit()
            registrar_accion_auditoria(
                'SOLICITUD_RETIRO',
                f"Cliente confirma envío de correo de retiro desde {email_origen}."
            )
            flash(
                "Solicitud de retiro enviada. Un asesor se comunicará contigo para guiarte en los siguientes pasos.",
                'warning'
            )
    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Error al enviar tu solicitud: {e}", 'error')

    return redirect(url_for('portal_dashboard'))

@app.route('/ver_reporte/<int:pago_id>')
@app.route('/portal/ver_reporte/<int:pago_id>')
def ver_reporte(pago_id):
    is_client_view = 'cliente_id' in session and g.cliente is not None
    is_admin_view = 'admin_id' in session and g.admin is not None

    if not is_client_view and not is_admin_view:
        flash('Acceso no autorizado. Por favor, inicie sesión.', 'error')
        return redirect(url_for('home'))

    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('home'))

    try:
        with conn.cursor() as cur:
            query = """
                SELECT p.*, 
                       c.nombre || ' ' || c.apellido as nombre_apellido, 
                       c.cedula, 
                       c.id as cliente_id
                FROM pagos p
                JOIN clientes c ON p.cliente_id = c.id
                WHERE p.id = %s
            """
            cur.execute(query, (pago_id,))
            pago = cur.fetchone()

            if not pago:
                flash("Reporte de pago no encontrado.", "error")
                return redirect(url_for('home'))

            if is_client_view and pago['cliente_id'] != session['cliente_id']:
                flash("No tienes permiso para ver este reporte.", "error")
                return redirect(url_for('portal_dashboard'))

            return render_template('ver_reporte.html', pago=pago, is_client_view=is_client_view, is_admin_view=is_admin_view)

    except psycopg2.Error as e:
        logging.error(f"Error al cargar el reporte de pago {pago_id}: {e}")
        flash("Ocurrió un error al cargar el reporte.", "error")
        return redirect(url_for('home'))


@app.route('/api/pago_detalle/<int:pago_id>')
@admin_required
def get_pago_detalle(pago_id):
    conn = get_db()
    if not conn:
        return jsonify({'error': 'Error de conexión a la base de datos.'}), 500

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.*, c.nombre, c.apellido, c.cedula 
                FROM pagos p JOIN clientes c ON p.cliente_id = c.id 
                WHERE p.id = %s
            """, (pago_id,))
            pago = cur.fetchone()
            if not pago:
                return jsonify({'error': 'Pago no encontrado'}), 404
            
            pago_dict = {k: str(v) if isinstance(v, (Decimal, datetime, date)) else v for k, v in dict(pago).items()}
            return jsonify(pago_dict)

    except psycopg2.Error as e:
        logging.error(f"Error en API get_pago_detalle: {e}")
        return jsonify({'error': 'Error interno del servidor al consultar el pago.'}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)