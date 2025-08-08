import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, g, flash, redirect, url_for, session, Response, jsonify
from dotenv import load_dotenv
from decimal import Decimal, InvalidOperation
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

# Configuración del logging para que sea visible en Render
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'una-clave-secreta-por-defecto-para-desarrollo')

# Definir la zona horaria de Venezuela
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

@app.template_filter('format_datetime')
def format_datetime_filter(value, format='%d/%m/%Y %I:%M %p'):
    """Filtro de Jinja para formatear fechas y horas a la zona horaria de Venezuela."""
    if isinstance(value, (datetime, date)):
        if value.tzinfo is None:
            value = pytz.utc.localize(value).astimezone(VENEZUELA_TZ)
        else:
            value = value.astimezone(VENEZUELA_TZ)
        return value.strftime(format)
    return value

def get_nombre_mes(month_number):
    """Convierte el número de un mes a su nombre en español."""
    meses = {1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"}
    return meses.get(month_number, "")

@app.template_filter('format_date')
def format_date_filter(value, format='%d/%m/%Y'):
    """Filtro de Jinja para formatear fechas."""
    if value == 'now':
        return get_venezuela_current_date().strftime(format)
    if isinstance(value, str):
        try:
            value = datetime.strptime(value, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return value
    if isinstance(value, (datetime, date)):
        return value.strftime(format)
    return value

# --- CONFIGURACIÓN DE LA SESIÓN Y CARGA DE USUARIO ---
@app.before_request
def setup_session_and_user():
    session.permanent = True
    app.permanent_session_lifetime = timedelta(minutes=10)
    
    g.admin = None
    g.cliente = None
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

# --- DECORADORES DE AUTENTICACIÓN ---
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if g.admin is None:
            flash('Acceso denegado. Debes iniciar sesión como administrador.', 'warning')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

def rol_requerido(*roles):
    def wrapper(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
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
        if 'cliente_id' not in session:
            flash('Por favor, inicia sesión para acceder a tu portal.', 'warning')
            return redirect(url_for('portal_login'))
        return f(*args, **kwargs)
    return decorated_function

# =================================================================================
# --- FUNCIONES AUXILIARES PARA EL MÓDULO COMERCIAL ---
# =================================================================================

def calcular_y_guardar_comisiones(contrato_nro, cliente_id, monto_plan, asesor_dueno, responsable_cierre):
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
                    INSERT INTO comisiones_generadas (contrato_nro, cliente_id, nombre_beneficiario, monto_comision, concepto, estado_nomina)
                    VALUES (%s, %s, %s, %s, %s, 'Pendiente')
                """
                for comision in comisiones_a_registrar:
                    if comision['monto'] > 0:
                         cur.execute(sql_comisiones, (contrato_nro, cliente_id, comision['beneficiario'], comision['monto'], comision['concepto']))
                sql_sobrante = "UPDATE caja_inscripciones SET sobrante_empresa = %s WHERE contrato_nro = %s"
                cur.execute(sql_sobrante, (sobrante_empresa, contrato_nro))
            logging.info(f"COMISIONES v2.3: Contrato {contrato_nro} procesado. Total a pagar: ${total_comisiones_pagadas:,.2f}. Sobrante: ${sobrante_empresa:,.2f}.")
        except psycopg2.Error as e:
            logging.error(f"COMISIONES v2.3: Error al guardar comisiones para contrato {contrato_nro}: {e}")
            raise e

# =================================================================================
# ===== MÓDULO DE TESORERÍA Y REBALANCEO =====
# =================================================================================

def calcular_balances_tesoreria(fecha_hasta=None):
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
            if not all([tipo_operacion, caja_origen, monto_origen > 0, nota]):
                flash("Error: Tipo, Nota, Caja Origen y Monto son obligatorios.", 'danger')
                return redirect(url_for('tesoreria_rebalanceo'))
            if balances_actuales.get(caja_origen, Decimal('0.0')) < monto_origen:
                flash(f"Error: Fondos insuficientes en '{caja_origen}'.", 'danger')
                return redirect(url_for('tesoreria_rebalanceo'))
            
            if tipo_operacion in ['PAGO_GASTO', 'PAGO_NOMINA', 'PAGO_SERVICIO']:
                caja_destino, monto_destino, moneda_destino = 'GASTO_OPERATIVO', monto_origen, moneda_origen
            else:
                caja_destino, monto_destino_str = form.get('caja_destino'), form.get('monto_destino', '0').replace(',', '.')
                moneda_destino = form.get('moneda_destino')
                monto_destino = Decimal(monto_destino_str) if monto_destino_str and monto_destino_str != '0' else None
                if not all([caja_destino, monto_destino, moneda_destino]):
                    flash("Error: Para transferencias, el destino es obligatorio.", 'danger')
                    return redirect(url_for('tesoreria_rebalanceo'))

            tasa_aplicada_str = form.get('tasa_aplicada', '0').replace(',', '.')
            tasa_aplicada = Decimal(tasa_aplicada_str) if tasa_aplicada_str and tasa_aplicada_str != '0' else None
            
            perdida_cambiaria = Decimal('0.0')
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

# --- Funciones de utilidad ---
def get_feriados_venezuela(year):
    """Devuelve una lista de fechas (date objects) para feriados en Venezuela."""
    feriados = [
        date(year, 1, 1), date(year, 5, 1), date(year, 6, 24), date(year, 7, 5),
        date(year, 7, 24), date(year, 10, 12), date(year, 12, 24), date(year, 12, 25),
        date(year, 12, 31)
    ]
    # Feriados moviles para 2024 y 2025
    if year == 2024:
        feriados.extend([date(2024, 2, 12), date(2024, 2, 13), date(2024, 3, 28), date(2024, 3, 29)])
    if year == 2025:
        feriados.extend([date(2025, 3, 3), date(2025, 3, 4), date(2025, 4, 17), date(2025, 4, 18)])
    feriados.append(date(year, 4, 19))
    return feriados

def get_fecha_vencimiento_ajustada(fecha_pago):
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

def registrar_accion_auditoria(accion, descripcion, cliente_id=None):
    if not g.admin:
        logging.warning(f"AUDITORIA-OMITIDA: Intento de registrar '{accion}' sin un g.admin establecido.")
        return
    conn = get_db()
    if not conn:
        logging.error("AUDITORIA-FALLO-CONEXION: No se pudo obtener conexión a la base de datos.")
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO registros_auditoria (usuario_id, usuario_nombre, accion, descripcion, cliente_afectado_id) VALUES (%s, %s, %s, %s, %s)",
                (g.admin['id'], g.admin['usuario'], accion, descripcion, cliente_id)
            )
        logging.info(f"AUDITORIA-PRE-REGISTRADA: Usuario '{g.admin['usuario']}' realizó '{accion}'.")
    except Exception as e:
        logging.error(f"AUDITORIA-FALLO-INSERCION: {e}")
        raise e

# --- RUTAS DEL PORTAL DE ADMINISTRACIÓN ---
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

@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    return redirect(url_for('consulta'))

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

# --- RUTAS PRINCIPALES Y DE NAVEGACIÓN ---
@app.route('/')
def home():
    return redirect(url_for('hub'))

@app.route('/hub')
@admin_required
def hub():
    conn = get_db()
    usuarios = []
    if conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT usuario, ultimo_login, (estatus_online AND ultimo_visto > NOW() - INTERVAL '5 minutes') AS esta_en_linea
                FROM administradores ORDER BY usuario
            """)
            usuarios = cur.fetchall()
    return render_template('hub.html', anio_actual=get_venezuela_current_date().year, usuarios=usuarios)

# =================================================================================
# --- INICIO: MÓDULO DE GESTIÓN ADMINISTRATIVA Y SOLICITUDES (REFACTORIZADO) ---
# =================================================================================

@app.route('/gestion_administrativa')
@admin_required
@rol_requerido('superadmin', 'gerente')
def gestion_administrativa():
    conn = get_db()
    counts = { 'pagos_pendientes': 0, 'citas': 0, 'congelamientos': 0, 'retiros': 0 }
    if conn:
        try:
            with conn.cursor() as cur:
                # Contador de pagos pendientes
                cur.execute("SELECT COUNT(*) FROM pagos WHERE estado_pago = 'Pendiente' AND (estado_reporte IS NULL OR estado_reporte != 'Inconsistente')")
                counts['pagos_pendientes'] = cur.fetchone()[0]
                
                # Contadores para cada tipo de solicitud pendiente
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
    """
    NUEVO: Hub central para todas las solicitudes. Muestra contadores y enlaza a las vistas dedicadas.
    """
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
    """
    NUEVO: Vista dedicada para gestionar solicitudes de Citas.
    """
    conn = get_db()
    solicitudes, administradores, citas_aprobadas = {'citas': []}, [], []
    if conn:
        try:
            with conn.cursor() as cur:
                # Obtener administradores para asignar a las citas
                cur.execute("SELECT id, usuario FROM administradores WHERE rol IN ('superadmin', 'gerente', 'administradora') ORDER BY usuario")
                administradores = cur.fetchall()
                
                # Obtener solicitudes de citas pendientes
                cur.execute("""
                    SELECT s.id, s.fecha_creacion, s.detalles, c.nombre || ' ' || c.apellido as nombre_cliente
                    FROM solicitudes s JOIN clientes c ON s.cliente_id = c.id
                    WHERE s.estado = 'Pendiente' AND s.tipo_solicitud = 'Cita' ORDER BY s.fecha_creacion ASC
                """)
                solicitudes['citas'] = cur.fetchall()

                # Obtener citas ya aprobadas y futuras para la agenda
                cur.execute("""
                    SELECT s.detalles, c.nombre || ' ' || c.apellido as nombre_cliente, a.usuario as nombre_asesor
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
    """
    NUEVO: Vista dedicada para gestionar solicitudes de Retiro.
    """
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
    """
    NUEVO: Vista dedicada para gestionar solicitudes de Congelamiento.
    """
    conn = get_db()
    solicitudes = {'congelamientos': []}
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.id, s.fecha_creacion, c.nombre || ' ' || c.apellido as nombre_cliente
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
    """
    MODIFICADO: Ahora redirige a la vista específica del tipo de solicitud procesada.
    """
    conn = get_db()
    accion = request.form.get('accion')
    tipo = request.form.get('tipo')

    # Mapa para redirigir a la página correcta después de procesar
    redirect_map = {
        'cita': 'gestion_citas',
        'retiro': 'gestion_retiros',
        'congelamiento': 'gestion_congelamientos'
    }
    redirect_url = url_for(redirect_map.get(tipo, 'solicitudes_hub'))

    if not all([conn, accion, tipo]):
        flash("Error en la solicitud.", "danger")
        return redirect(redirect_url)

    nuevo_estado_solicitud = 'Aprobada' if accion == 'aprobar' else 'Rechazada'
    
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cliente_id, detalles FROM solicitudes WHERE id = %s", (solicitud_id,))
            solicitud = cur.fetchone()
            if not solicitud:
                flash("La solicitud no existe.", "error")
                return redirect(redirect_url)

            detalles_actualizados = solicitud['detalles'] if solicitud['detalles'] is not None else {}

            # Lógica específica para aprobar citas (asignar asesor)
            if tipo == 'cita' and accion == 'aprobar':
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

            # Actualizar la solicitud
            cur.execute(
                "UPDATE solicitudes SET estado = %s, revisado_por_id = %s, fecha_revision = NOW(), detalles = %s WHERE id = %s",
                (nuevo_estado_solicitud, g.admin['id'], json.dumps(detalles_actualizados), solicitud_id)
            )
            
            # Si se aprueba, actualizar el estatus del cliente si es necesario
            if accion == 'aprobar':
                if tipo == 'retiro':
                    cur.execute("UPDATE clientes SET estatus = 'RETIRO' WHERE id = %s", (solicitud['cliente_id'],))
                elif tipo == 'congelamiento':
                    cur.execute("UPDATE clientes SET estatus = 'CONGELADO' WHERE id = %s", (solicitud['cliente_id'],))
            
            descripcion_audit = f"{accion.capitalize()} la solicitud de {tipo} N° {solicitud_id}."
            registrar_accion_auditoria('GESTION_SOLICITUD', descripcion_audit, solicitud['cliente_id'])
            
            conn.commit()
            flash(f"La solicitud de {tipo} ha sido marcada como '{nuevo_estado_solicitud}'.", 'success')
    except (psycopg2.Error, json.JSONDecodeError) as e:
        conn.rollback()
        logging.error(f"Error al procesar solicitud {solicitud_id}: {e}")
        flash(f"Error al procesar la solicitud: {e}", "error")
    
    return redirect(redirect_url)

# =================================================================================
# --- FIN: MÓDULO DE GESTIÓN ADMINISTRATIVA Y SOLICITUDES (REFACTORIZADO) ---
# =================================================================================

@app.route('/pagos_por_conciliar')
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def pagos_por_conciliar():
    conn = get_db()
    pagos_a_procesar = []
    if not conn:
        flash("Error de conexión con la base de datos.", "danger")
        return render_template('pagos_por_conciliar.html', pagos=[], anio_actual=get_venezuela_current_date().year)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.id, p.monto, p.tipo_pago, p.fecha_creacion AS fecha_reporte,
                       p.reportado_por_cliente, p.estado_reporte, p.cliente_id,
                       c.nombre, c.apellido, c.cedula,
                       COALESCE(a.usuario, 'Cliente') as registrado_por
                FROM pagos p
                LEFT JOIN clientes c ON p.cliente_id = c.id
                LEFT JOIN administradores a ON p.registrado_por_id = a.id
                WHERE p.estado_pago = 'Pendiente' ORDER BY p.fecha_creacion ASC;
            """)
            pagos_pendientes = cur.fetchall()
            now_vet, hora_corte = get_venezuela_current_datetime(), time(16, 30)
            for pago_row in pagos_pendientes:
                pago = dict(pago_row)
                fecha_reporte_naive = pago['fecha_reporte']
                if pago['estado_reporte'] == 'Inconsistente':
                    pago['status_display'], pago['status_class'] = 'Inconsistente', 'danger'
                else:
                    pago['status_display'], pago['status_class'] = 'Pendiente', 'warning'
                pago['disabled_reason'] = ''
                if not pago.get('nombre'):
                    pago['action_type'], pago['disabled_reason'] = 'Ninguna', 'El cliente asociado fue eliminado.'
                elif pago['estado_reporte'] == 'Inconsistente':
                    pago['action_type'], pago['disabled_reason'] = 'Rechazado', 'Este reporte fue marcado como inconsistente y no puede ser procesado.'
                elif fecha_reporte_naive:
                    fecha_reporte_vet = pytz.utc.localize(fecha_reporte_naive).astimezone(VENEZUELA_TZ) if fecha_reporte_naive.tzinfo is None else fecha_reporte_naive.astimezone(VENEZUELA_TZ)
                    if fecha_reporte_vet.date() == now_vet.date() and fecha_reporte_vet.time() >= hora_corte:
                        pago['action_type'] = 'Diferido'
                        proximo_dia = get_proximo_dia_habil(now_vet.date())
                        pago['disabled_reason'] = f'Reportado fuera de horario. Se habilitará el {proximo_dia.strftime("%d/%m")}.'
                    elif pago['reportado_por_cliente'] and pago['estado_reporte'] != 'Aprobado':
                        pago['action_type'] = 'Ver Reporte'
                    else:
                        pago['action_type'] = 'Conciliar'
                else:
                    pago['action_type'] = 'Conciliar'
                pagos_a_procesar.append(pago)
    except psycopg2.Error as e:
        logging.error(f"Error al obtener pagos por conciliar: {e}")
        flash("Error al cargar la lista de pagos pendientes.", "danger")
    return render_template('pagos_por_conciliar.html', pagos=pagos_a_procesar, anio_actual=get_venezuela_current_date().year)

# --- MÓDULO COMERCIAL ---
@app.route('/comercial/dashboard')
@admin_required
@rol_requerido('superadmin', 'gerente')
def dashboard_comercial():
    conn = get_db()
    contratos, resumen_asesores, balances_generales = [], [], {}
    stats = {'ingresos_brutos_inscripciones': Decimal('0.0'), 'total_comisiones_pendientes': Decimal('0.0'), 'total_sobrante_pendiente': Decimal('0.0')}
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT ci.contrato_nro, ci.fecha_registro, ci.monto_inscripcion, cli.asesor,
                           ci.responsable_cierre, ci.sobrante_empresa, cli.nombre, cli.apellido,
                           cli.plan_contratado, cli.id as cliente_id
                    FROM caja_inscripciones ci JOIN clientes cli ON ci.cliente_id = cli.id
                    ORDER BY ci.fecha_registro DESC;
                """)
                db_contratos = cur.fetchall()
                contratos = []
                for contrato_row in db_contratos:
                    contrato_dict = dict(contrato_row)
                    try:
                        contrato_dict['plan_contratado'] = Decimal(contrato_dict['plan_contratado'])
                    except (TypeError, InvalidOperation, ValueError):
                        contrato_dict['plan_contratado'] = Decimal('0.00')
                    contratos.append(contrato_dict)
                cur.execute("""
                    SELECT nombre_beneficiario, SUM(monto_comision) as total_pendiente
                    FROM comisiones_generadas WHERE estado_nomina = 'Pendiente'
                    GROUP BY nombre_beneficiario ORDER BY total_pendiente DESC;
                """)
                resumen_asesores = cur.fetchall()
                if contratos:
                    stats['ingresos_brutos_inscripciones'] = sum(c['monto_inscripcion'] for c in contratos)
                    stats['total_sobrante_pendiente'] = sum(c['sobrante_empresa'] or Decimal('0.0') for c in contratos)
                if resumen_asesores:
                    stats['total_comisiones_pendientes'] = sum(a['total_pendiente'] for a in resumen_asesores)
            balances_generales = calcular_balances_tesoreria()
        except psycopg2.Error as e:
            flash(f"Error al cargar el dashboard comercial: {e}", "danger")
    return render_template('dashboard_comercial.html', stats=stats, contratos=contratos, resumen_asesores=resumen_asesores, balances_generales=balances_generales, anio_actual=get_venezuela_current_date().year)

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
                cur.execute("SELECT COALESCE(SUM(monto_comision), 0) FROM comisiones_generadas WHERE estado_nomina = 'Pendiente' AND nombre_beneficiario = %s;", (nombre_beneficiario,))
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

                cur.execute("UPDATE comisiones_generadas SET estado_nomina = 'Pagada', fecha_pago_nomina = NOW() WHERE estado_nomina = 'Pendiente' AND nombre_beneficiario = %s;", (pago['beneficiario'],))
            
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
            cur.execute("SELECT nombre_beneficiario, concepto, monto_comision FROM comisiones_generadas WHERE contrato_nro = %s ORDER BY monto_comision DESC;", (contrato_nro,))
            comisiones = cur.fetchall()
            cur.execute("SELECT cli.plan_contratado, ci.sobrante_empresa FROM caja_inscripciones ci JOIN clientes cli ON ci.cliente_id = cli.id WHERE ci.contrato_nro = %s;", (contrato_nro,))
            contrato_info = cur.fetchone()
            if not contrato_info: return jsonify({'error': 'Contrato no encontrado'}), 404
            try:
                plan_contratado_decimal = Decimal(contrato_info['plan_contratado'])
            except (TypeError, InvalidOperation, ValueError):
                plan_contratado_decimal = Decimal('0.00')
            pool_total, total_pagado = plan_contratado_decimal * Decimal('0.16'), sum(c['monto_comision'] for c in comisiones)
            comisiones_json = [{'beneficiario': c['nombre_beneficiario'], 'concepto': c['concepto'], 'monto': f"{c['monto_comision']:,.2f}"} for c in comisiones]
            response_data = {
                'comisiones': comisiones_json,
                'resumen': {
                    'pool_total': f"{pool_total:,.2f}",
                    'total_comisiones': f"{total_pagado:,.2f}",
                    'sobrante_empresa': f"{contrato_info['sobrante_empresa']:,.2f}" if contrato_info['sobrante_empresa'] is not None else "N/A"
                }
            }
            return jsonify(response_data)
    except psycopg2.Error as e:
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
            cur.execute("""
                SELECT cg.concepto, cg.monto_comision, cg.contrato_nro, c.nombre, c.apellido, c.plan_contratado, ci.responsable_cierre
                FROM comisiones_generadas cg
                JOIN clientes c ON cg.cliente_id = c.id
                JOIN caja_inscripciones ci ON cg.contrato_nro = ci.contrato_nro
                WHERE cg.nombre_beneficiario = %s AND cg.estado_nomina = 'Pendiente'
                ORDER BY cg.id DESC;
            """, (nombre_beneficiario,))
            historial = cur.fetchall()
            historial_json = []
            for item in historial:
                historial_json.append({
                    'concepto': item['concepto'], 'monto': f"{item['monto_comision']:,.2f}",
                    'contrato_nro': item['contrato_nro'], 'cliente': f"{item['nombre']} {item['apellido']}",
                    'plan_contratado': f"{Decimal(item['plan_contratado']):,.2f}", 'responsable_cierre': item['responsable_cierre']
                })
            return jsonify(historial_json)
    except psycopg2.Error as e:
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
    clientes_en_mora, gestores, resumen = [], [], {'total_clientes_mora': 0, 'monto_total_mora': 0}
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, usuario FROM administradores ORDER BY usuario")
                gestores = cur.fetchall()
                first_day_of_month = today.replace(day=1)
                subquery_pagaron_mes = "SELECT DISTINCT cliente_id FROM pagos WHERE tipo_pago = 'Cuota' AND estado_pago = 'Conciliado' AND fecha_pago >= %s"
                query_morosos = f"""
                    SELECT c.id, c.nombre, c.apellido, c.cedula, c.telefono, c.valor_cuota, c.gestor_id,
                           a.usuario as gestor_asignado,
                           (SELECT MAX(p.fecha_pago) FROM pagos p WHERE p.cliente_id = c.id AND p.estado_pago = 'Conciliado') as ultimo_pago_fecha
                    FROM clientes c LEFT JOIN administradores a ON c.gestor_id = a.id
                    WHERE TRIM(UPPER(c.proceso)) = 'AHORRADOR' AND TRIM(UPPER(c.estatus)) = 'ACTIVO'
                    AND c.id NOT IN ({subquery_pagaron_mes}) ORDER BY c.nombre, c.apellido;
                """
                cur.execute(query_morosos, (first_day_of_month,))
                clientes_en_mora = cur.fetchall()
                if clientes_en_mora:
                    resumen['total_clientes_mora'] = len(clientes_en_mora)
                    resumen['monto_total_mora'] = sum(c['valor_cuota'] for c in clientes_en_mora if c['valor_cuota'])
        except psycopg2.Error as e:
            flash(f"No se pudo generar el reporte de morosidad: {e}", "error")
    return render_template('reporte_morosidad.html', clientes_en_mora=clientes_en_mora, gestores=gestores, resumen=resumen, mes_actual=get_nombre_mes(today.month), anio_actual=today.year)

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

# --- GESTIÓN DE CLIENTES Y PAGOS ---
@app.route('/cliente/<int:cliente_id>')
@admin_required
def perfil_cliente(cliente_id):
    conn = get_db()
    cliente, pagos, gestiones = None, [], []
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s", (cliente_id,))
                cliente = cur.fetchone()
                if not cliente:
                    flash("Cliente no encontrado.", "error")
                    return redirect(url_for('consulta'))
                cur.execute("SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha_pago DESC, id DESC", (cliente_id,))
                pagos = cur.fetchall()
                query_gestiones = """
                    SELECT g.nota, g.fecha_creacion, a.usuario as gestor_nombre FROM gestiones_cobranza g
                    JOIN administradores a ON g.gestor_id = a.id WHERE g.cliente_id = %s ORDER BY g.fecha_creacion DESC;
                """
                cur.execute(query_gestiones, (cliente_id,))
                gestiones = cur.fetchall()
        except psycopg2.Error as e:
            flash(f"Error al cargar el perfil del cliente: {e}", "error")
            return redirect(url_for('consulta'))
    return render_template('cliente_perfil.html', cliente=cliente, pagos=pagos, gestiones=gestiones, anio_actual=get_venezuela_current_date().year)

@app.route('/agregar_gestion/<int:cliente_id>', methods=['POST'])
@admin_required
def agregar_gestion(cliente_id):
    nota = request.form.get('nota')
    if not nota or not nota.strip():
        flash("La nota de gestión no puede estar vacía.", "warning")
        return redirect(url_for('perfil_cliente', cliente_id=cliente_id))
    conn = get_db()
    if conn and g.admin:
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO gestiones_cobranza (cliente_id, gestor_id, nota) VALUES (%s, %s, %s)", (cliente_id, g.admin['id'], nota.strip()))
                cur.execute("SELECT nombre, apellido FROM clientes WHERE id = %s", (cliente_id,))
                cliente = cur.fetchone()
                descripcion = f"Agregó nota de gestión para el cliente {cliente['nombre']} {cliente['apellido']}: '{nota[:50]}...'"
                registrar_accion_auditoria('AGREGAR_GESTION', descripcion, cliente_id)
                conn.commit()
                flash("Nota de gestión guardada exitosamente.", "success")
        except psycopg2.Error as e:
            conn.rollback()
            flash(f"Error al guardar la nota de gestión: {e}", "error")
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
    try:
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
            insert_dict = {'nombre': nombre, 'apellido': apellido, 'cedula': form_data.get('cedula').replace(' ', ''),
                           'cuotas_pagadas_progresivas': 0, 'cuotas_pagadas_regresivas': 0, 'firma_digital': firma_cliente,
                           'firma_empresa': firma_empresa, 'fecha_firma': datetime.now(VENEZUELA_TZ), 'proceso': 'RESERVA'}
            optional_fields = ['contrato_nro', 'telefono', 'asesor', 'responsable', 'fecha_ingreso', 'grupo', 'plan_contratado', 
                               'cuotas_totales', 'moneda_pago', 'valor_cuota', 'inscripcion_monto', 'ciclo_cobranza', 'foto_cliente', 
                               'foto_cedula', 'direccion', 'email', 'beneficiario_nombre', 'beneficiario_cedula', 'beneficiario_telefono']
            for field in optional_fields:
                if form_data.get(field): insert_dict[field] = form_data[field]
            columns, values = list(insert_dict.keys()), [insert_dict[col] for col in list(insert_dict.keys())]
            query = f"INSERT INTO clientes ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(values))}) RETURNING id"
            cur.execute(query, values)
            new_client_id = cur.fetchone()[0]
            if form_data['inscripcion_monto'] > 0:
                cur.execute("INSERT INTO caja_inscripciones (contrato_nro, cliente_id, monto_inscripcion, responsable_cierre) VALUES (%s, %s, %s, %s)",
                            (form_data.get('contrato_nro'), new_client_id, form_data['inscripcion_monto'], responsable_cierre))
            descripcion_audit = f"Registró y firmó contrato para nuevo cliente: {form_data.get('nombre_apellido')} (C.I. {form_data.get('cedula')})."
            registrar_accion_auditoria('REGISTRO_CLIENTE_FIRMADO', descripcion_audit, new_client_id)
            conn.commit()
            flash(f"¡Cliente '{form_data.get('nombre_apellido')}' registrado exitosamente como RESERVA! Ahora puede registrar su primer pago desde la consulta.", 'success')
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

@app.route('/generar_contrato/<int:client_id>')
def generar_contrato(client_id):
    is_admin = 'admin_id' in session
    is_correct_client = 'cliente_id' in session and session['cliente_id'] == client_id
    if not is_admin and not is_correct_client:
        flash('Acceso no autorizado.', 'error')
        return redirect(url_for('portal_dashboard')) if 'cliente_id' in session else redirect(url_for('home'))
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('home'))
    with conn.cursor() as cur:
        cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s", (client_id,))
        cliente = cur.fetchone()
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('home'))
    return render_template('contrato.html', cliente=cliente, modo_pre_registro=False, anio_actual=get_venezuela_current_date().year)

@app.route('/guardar_firma_cliente/<int:client_id>', methods=['POST'])
@admin_required
def guardar_firma_cliente(client_id):
    firma_cliente = request.form.get('firma_cliente')
    if not firma_cliente:
        flash('No se recibió la firma del cliente.', 'error')
        return redirect(url_for('generar_contrato', client_id=client_id))
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('generar_contrato', client_id=client_id))
    try:
        with conn.cursor() as cur:
            fecha_firma_utc = datetime.now(pytz.utc)
            fecha_firma_vet = fecha_firma_utc.astimezone(pytz.timezone('America/Caracas'))
            cur.execute("UPDATE clientes SET firma_digital = %s, fecha_firma = %s WHERE id = %s", (firma_cliente, fecha_firma_vet, client_id))
            conn.commit()
            flash('¡Firma del cliente guardada exitosamente!', 'success')
    except (psycopg2.Error, ConnectionError) as e:
        conn.rollback()
        flash(f'Error al guardar la firma del cliente: {e}', 'error')
    return redirect(url_for('generar_contrato', client_id=client_id))

@app.route('/guardar_firma_empresa/<int:client_id>', methods=['POST'])
@admin_required
def guardar_firma_empresa(client_id):
    firma_empresa = request.form.get('firma_empresa')
    if not firma_empresa:
        flash('No se recibió la firma de la empresa.', 'error')
        return redirect(url_for('generar_contrato', client_id=client_id))
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('generar_contrato', client_id=client_id))
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE clientes SET firma_empresa = %s WHERE id = %s", (firma_empresa, client_id))
            cur.execute("UPDATE clientes SET fecha_firma = %s WHERE id = %s AND fecha_firma IS NULL", (datetime.now(VENEZUELA_TZ), client_id))
            conn.commit()
            flash('¡Firma de la empresa guardada exitosamente!', 'success')
    except (psycopg2.Error, ConnectionError) as e:
        conn.rollback()
        flash(f'Error al guardar la firma de la empresa: {e}', 'error')
    return redirect(url_for('generar_contrato', client_id=client_id))

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
                        cur.execute("SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha_pago DESC, id DESC", (cliente_dict['id'],))
                        cliente_dict['pagos'] = cur.fetchall()
                        cur.execute("SELECT * FROM ofertas WHERE cliente_id = %s ORDER BY fecha_oferta DESC", (cliente_dict['id'],))
                        cliente_dict['ofertas'] = cur.fetchall()
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
        if pago_form.get('forma_pago') not in ['Efectivo', 'Binance', 'Nequi'] and not pago_form.get('referencia'):
            flash('Error: La referencia es obligatoria para pagos por Transferencia o Pago Móvil.', 'error')
            return render_template('registrar_pago.html', cliente=cliente, tasas_hoy=tasas_hoy)
        moneda_referencia, pago_en_valor = None, pago_form.get('pago_en')
        if pago_en_valor == 'Dolar/BCV': moneda_referencia = 'USD'
        elif pago_en_valor == 'Euro/BCV': moneda_referencia = 'EUR'
        try:
            with conn.cursor() as cur:
                pago_query = """
                    INSERT INTO pagos (cliente_id, monto, tipo_pago, forma_pago, fecha_pago, pago_en, por_concepto_de, referencia, banco, lugar_emision,
                                        tasa_dia, monto_bs, estado_pago, cuotas_cubiertas, moneda_referencia, fecha_creacion, registrado_por_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Pendiente', 0, %s, %s, %s);
                """
                cur.execute(pago_query, (client_id, pago_form['monto'], tipo_pago, pago_form['forma_pago'], pago_form['fecha_pago'], pago_form.get('pago_en'), 
                                         pago_form.get('por_concepto_de'), pago_form.get('referencia'), pago_form.get('banco'), pago_form.get('lugar_emision'), 
                                         pago_form.get('tasa_dia'), pago_form.get('monto_bs'), moneda_referencia, get_venezuela_current_datetime(), g.admin['id']))
                conn.commit()
                flash(f"¡Pago de {tipo_pago} registrado como PENDIENTE! Ahora debe ser conciliado.", 'success')
                return redirect(url_for('consulta', busqueda=cliente['cedula']))
        except (psycopg2.Error, ValueError, TypeError) as e:
            conn.rollback()
            flash(f'Ocurrió un error al registrar el pago: {e}', 'error')
            return render_template('registrar_pago.html', cliente=cliente, tasas_hoy=tasas_hoy)
    return render_template('registrar_pago.html', cliente=cliente, tasas_hoy=tasas_hoy)

@app.route('/conciliar_pago/<int:pago_id>', methods=['POST'])
@admin_required
def conciliar_pago(pago_id):
    conn, cedula_cliente_fallback = get_db(), request.args.get('cedula', '')
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta', busqueda=cedula_cliente_fallback))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM pagos WHERE id = %s", (pago_id,))
            pago_original = cur.fetchone()
            if not pago_original or pago_original['estado_pago'] != 'Pendiente':
                flash("El pago no se puede conciliar (ya está conciliado, anulado o no existe).", 'error')
                return redirect(url_for('pagos_por_conciliar'))
            pago = dict(pago_original)
            now_vet, hora_corte, fecha_conciliacion_actual = get_venezuela_current_datetime(), time(16, 30), get_venezuela_current_date()
            fecha_reporte_naive = pago['fecha_creacion']
            habilitado_para_conciliar = False
            if fecha_reporte_naive:
                fecha_reporte_vet = VENEZUELA_TZ.localize(fecha_reporte_naive) if fecha_reporte_naive.tzinfo is None else fecha_reporte_naive.astimezone(VENEZUELA_TZ)
                if fecha_reporte_vet.date() < fecha_conciliacion_actual or (fecha_reporte_vet.date() == fecha_conciliacion_actual and fecha_reporte_vet.time() < hora_corte):
                    habilitado_para_conciliar = True
                if fecha_reporte_vet.time() >= hora_corte and fecha_reporte_vet.date() < fecha_conciliacion_actual:
                    logging.info(f"AJUSTE DE FECHA: El pago {pago['id']} fue reportado el {fecha_reporte_vet.strftime('%Y-%m-%d')} fuera de horario. Se conciliará con fecha del día hábil: {fecha_conciliacion_actual.strftime('%Y-%m-%d')}.")
                    pago['fecha_pago'], habilitado_para_conciliar = fecha_conciliacion_actual, True
            else:
                habilitado_para_conciliar = True
            if not habilitado_para_conciliar:
                flash("Este pago no puede ser conciliado hoy debido a la hora de su reporte.", 'danger')
                return redirect(url_for('pagos_por_conciliar'))
            cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s FOR UPDATE", (pago['cliente_id'],))
            cliente = cur.fetchone()
            if not cliente:
                flash("Error: No se encontró el cliente asociado a este pago.", 'error')
                conn.rollback()
                return redirect(url_for('consulta', busqueda=cedula_cliente_fallback))
            monto_pagado, pago_final_id, flash_msg = Decimal(pago['monto']), None, ""
            admin_id = g.admin['id'] if g.admin else None
            if pago['tipo_pago'] == 'Inscripción':
                inscripcion_pagada_actual, inscripcion_total = Decimal(cliente.get('inscripcion_pagada') or 0), Decimal(cliente.get('inscripcion_monto') or 0)
                nueva_inscripcion_pagada = inscripcion_pagada_actual + monto_pagado
                if inscripcion_total > 0:
                    umbral_pago_comision = inscripcion_total * (Decimal('7.7') / Decimal('16'))
                    cur.execute("SELECT comisiones_generadas FROM caja_inscripciones WHERE cliente_id = %s", (cliente['id'],))
                    comisiones_ya_generadas = cur.fetchone()[0]
                    if nueva_inscripcion_pagada >= umbral_pago_comision and not comisiones_ya_generadas:
                        calcular_y_guardar_comisiones(contrato_nro=cliente['contrato_nro'], cliente_id=cliente['id'], monto_plan=Decimal(cliente['plan_contratado']), asesor_dueno=cliente['asesor'], responsable_cierre=cliente['responsable'])
                        cur.execute("UPDATE caja_inscripciones SET comisiones_generadas = TRUE WHERE cliente_id = %s", (cliente['id'],))
                        flash('¡Umbral de inscripción alcanzado! Comisiones generadas para la nómina.', 'success')
                if inscripcion_total > 0 and monto_pagado >= inscripcion_total and inscripcion_pagada_actual == 0:
                    if cliente['proceso'] == 'RESERVA': cur.execute("UPDATE clientes SET proceso = 'INSCRITO' WHERE id = %s", (cliente['id'],))
                    cur.execute("UPDATE clientes SET inscripcion_pagada = %s WHERE id = %s", (inscripcion_total, cliente['id']))
                    cur.execute("UPDATE pagos SET estado_pago = 'Conciliado', tipo_pago = 'Inscripción Finalizada', monto = %s, conciliado_por_id = %s, fecha_pago = %s WHERE id = %s", (inscripcion_total, admin_id, pago['fecha_pago'], pago_id))
                    pago_final_id = pago_id
                    descripcion_audit = f"Concilió pago único de inscripción (N°{pago_id}) por ${monto_pagado} para {cliente['nombre_apellido']}."
                    registrar_accion_auditoria('CONCILIACION_INSCRIPCION', descripcion_audit, cliente['id'])
                    flash_msg = "¡Inscripción completada en un solo pago! Se generó el recibo final."
                elif inscripcion_total > 0 and nueva_inscripcion_pagada >= inscripcion_total:
                    if cliente['proceso'] == 'RESERVA': cur.execute("UPDATE clientes SET proceso = 'INSCRITO' WHERE id = %s", (cliente['id'],))
                    cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE cliente_id = %s AND tipo_pago = 'Inscripción' AND estado_pago = 'Conciliado'", (cliente['id'],))
                    cur.execute("INSERT INTO pagos (cliente_id, monto, tipo_pago, forma_pago, fecha_pago, por_concepto_de, estado_pago, cuotas_cubiertas, lugar_emision, conciliado_por_id) VALUES (%s, %s, 'Inscripción Finalizada', %s, %s, %s, 'Conciliado', 0, %s, %s) RETURNING id;", (cliente['id'], inscripcion_total, pago['forma_pago'], pago['fecha_pago'], 'Pago total de inscripción', pago['lugar_emision'], admin_id))
                    pago_final_id = cur.fetchone()[0]
                    cur.execute("UPDATE clientes SET inscripcion_pagada = %s WHERE id = %s", (inscripcion_total, cliente['id']))
                    cur.execute("UPDATE pagos SET estado_pago = 'Anulado', fecha_pago = %s WHERE id = %s", (pago['fecha_pago'], pago_id,))
                    descripcion_audit = f"Concilió pago final de inscripción (N°{pago_id}) por ${monto_pagado} para {cliente['nombre_apellido']}."
                    registrar_accion_auditoria('CONCILIACION_INSCRIPCION', descripcion_audit, cliente['id'])
                    flash_msg = "¡Inscripción completada! Se generó el recibo final."
                else:
                    cur.execute("UPDATE clientes SET inscripcion_pagada = %s WHERE id = %s", (nueva_inscripcion_pagada, cliente['id']))
                    cur.execute("UPDATE pagos SET estado_pago = 'Conciliado', conciliado_por_id = %s, fecha_pago = %s WHERE id = %s", (admin_id, pago['fecha_pago'], pago_id))
                    descripcion_audit = f"Concilió abono de inscripción N° {pago_id} por ${monto_pagado} para {cliente['nombre_apellido']}."
                    registrar_accion_auditoria('CONCILIACION_INSCRIPCION', descripcion_audit, cliente['id'])
                    flash_msg = f"Abono de inscripción N° {pago_id} conciliado."
            elif pago['tipo_pago'] == 'Cuota':
                if cliente['proceso'] == 'INSCRITO':
                    cur.execute("UPDATE clientes SET proceso = 'Ahorrador', estatus = 'ACTIVO' WHERE id = %s", (cliente['id'],))
                    flash_msg += "¡Cliente actualizado a Ahorrador Activo!"

                puntualidad, fecha_vencimiento = 'Puntual', get_fecha_vencimiento_ajustada(pago['fecha_pago'])
                if pago['fecha_pago'] > fecha_vencimiento:
                    puntualidad = 'Impuntual'
                    cur.execute("UPDATE clientes SET meses_retraso_entrega = meses_retraso_entrega + 1 WHERE id = %s;", (cliente['id'],))
                
                valor_cuota = Decimal(cliente.get('valor_cuota') or 0)
                if valor_cuota <= 0: raise ValueError('El cliente no tiene un valor de cuota válido.')
                cpp, cpr, br = cliente.get('cuotas_pagadas_progresivas', 0), cliente.get('cuotas_pagadas_regresivas', 0), Decimal(cliente.get('balance_regresivo', 0))
                mtd, pph, rph = monto_pagado + br, 0, 0
                if mtd >= valor_cuota: pph, mtd = 1, mtd - valor_cuota
                bp = mtd
                while bp >= valor_cuota: rph, bp = rph + 1, bp - valor_cuota
                nbf, ncpp, ncpr, cch = bp, cpp + pph, cpr + rph, pph + rph
                cur.execute("UPDATE clientes SET cuotas_pagadas_progresivas = %s, cuotas_pagadas_regresivas = %s, balance_regresivo = %s WHERE id = %s;", (ncpp, ncpr, nbf, cliente['id']))
                cur.execute("UPDATE pagos SET estado_pago = 'Conciliado', fecha_pago = %s, puntualidad = %s, cuotas_cubiertas = %s, progresivas_cubiertas = %s, regresivas_cubiertas = %s, cuotas_progresivas_al_pagar = %s, cuotas_regresivas_al_pagar = %s, balance_al_pagar = %s, conciliado_por_id = %s WHERE id = %s;", (pago['fecha_pago'], puntualidad, cch, pph, rph, ncpp, ncpr, nbf, admin_id, pago_id))
                descripcion_audit = f"Concilió pago de cuota N° {pago_id} por ${monto_pagado} como '{puntualidad}' para {cliente['nombre_apellido']}."
                registrar_accion_auditoria('CONCILIACION_CUOTA', descripcion_audit, cliente['id'])
                flash_msg += f"¡Pago de cuota N° {pago_id} conciliado como '{puntualidad}'!"
            conn.commit()
            flash(flash_msg, 'success')
            if pago_final_id: return redirect(url_for('ver_recibo_inscripcion', pago_id=pago_final_id))
            return redirect(url_for('ver_recibo', pago_id=pago_id))
    except (psycopg2.Error, ValueError, TypeError, ConnectionError) as e:
        if conn: conn.rollback()
        flash(f'Ocurrió un error al conciliar el pago: {e}', 'error')
    return redirect(url_for('pagos_por_conciliar'))

@app.route('/recibo/<int:pago_id>')
def ver_recibo(pago_id):
    conn = get_db()
    if not conn:
        return redirect(url_for('portal_login')) if 'cliente_id' in session else redirect(url_for('consulta'))
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
        return redirect(url_for('portal_dashboard')) if 'cliente_id' in session else redirect(url_for('consulta'))
    return render_template('recibo.html', pago=pago, is_admin_view='admin_id' in session)

@app.route('/recibo_inscripcion/<int:pago_id>')
def ver_recibo_inscripcion(pago_id):
    conn = get_db()
    if not conn:
        return redirect(url_for('portal_login')) if 'cliente_id' in session else redirect(url_for('consulta'))
    with conn.cursor() as cur:
        cur.execute("SELECT p.*, (c.nombre || ' ' || c.apellido) as nombre_apellido, c.cedula, c.plan_contratado FROM pagos p JOIN clientes c ON p.cliente_id = c.id WHERE p.id = %s AND p.tipo_pago = 'Inscripción Finalizada';", (pago_id,))
        pago = cur.fetchone()
    if not pago:
        flash('Recibo de inscripción final no encontrado.', 'error')
        return redirect(url_for('portal_dashboard')) if 'cliente_id' in session else redirect(url_for('consulta'))
    return render_template('recibo_inscripcion.html', pago=pago, cliente=pago, is_admin_view='admin_id' in session)

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
                cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE id = %s", (pago_id,))
                cur.execute("UPDATE pagos SET estado_pago = 'Conciliado' WHERE cliente_id = %s AND tipo_pago = 'Inscripción' AND estado_pago = 'Anulado'", (cliente_id,))
                cur.execute("SELECT COALESCE(SUM(monto), 0) FROM pagos WHERE cliente_id = %s AND tipo_pago = 'Inscripción' AND estado_pago = 'Conciliado'", (cliente_id,))
                total_inscripcion_reactivada = cur.fetchone()[0]
                cur.execute("UPDATE clientes SET inscripcion_pagada = %s, proceso = 'RESERVA' WHERE id = %s", (total_inscripcion_reactivada, cliente_id))
                flash("¡Reinicio de inscripción completado! El cliente ha vuelto a 'RESERVA'.", 'success')
            descripcion_audit = f"Anuló el recibo N° {pago_id} (Tipo: {pago_a_anular['tipo_pago']}, ${pago_a_anular['monto']}) del cliente {nombre_cliente}."
            registrar_accion_auditoria('ANULACION_RECIBO', descripcion_audit, cliente_id)
            conn.commit()
            flash(f"¡Recibo N° {pago_id} anulado y saldo corregido exitosamente!", "success")
            return redirect(url_for('consulta', busqueda=cedula_cliente))
    except (psycopg2.Error, ValueError, ConnectionError) as e:
        conn.rollback()
        flash(f'Ocurrió un error al anular el recibo: {e}', 'error')
        return redirect(url_for('consulta', busqueda=cedula_cliente))

@app.route('/verificar_recibo/<int:pago_id>')
def verificar_recibo(pago_id):
    conn = get_db()
    if not conn: return "Error de conexión a la base de datos.", 500
    with conn.cursor() as cur:
        query = "SELECT p.id, p.monto, p.fecha_pago, p.estado_pago, p.tipo_pago, (c.nombre || ' ' || c.apellido) as nombre_apellido FROM pagos p JOIN clientes c ON p.cliente_id = c.id WHERE p.id = %s;"
        cur.execute(query, (pago_id,))
        pago = cur.fetchone()
    current_year = get_venezuela_current_date().year
    return render_template('verificacion_recibo.html', pago=pago, current_year=current_year)

@app.route('/recibo_anulado/<int:pago_id>')
@admin_required
def ver_recibo_anulado(pago_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta'))
    with conn.cursor() as cur:
        query = "SELECT p.*, (c.nombre || ' ' || c.apellido) as nombre_apellido, c.cedula FROM pagos p JOIN clientes c ON p.cliente_id = c.id WHERE p.id = %s AND p.estado_pago = 'Anulado';"
        cur.execute(query, (pago_id,))
        pago = cur.fetchone()
    if not pago:
        flash('Recibo anulado no encontrado.', 'error')
        return redirect(url_for('consulta'))
    return render_template('recibo_anulado.html', pago=pago)

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
        cliente = cur.fetchone()
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta'))
    if request.method == 'POST':
        try:
            update_data = dict(cliente)
            form_data = {k: v if v else None for k, v in request.form.items()}
            if 'nombre_apellido' in form_data:
                nombre_completo = form_data['nombre_apellido'].split(' ', 1)
                form_data['nombre'], form_data['apellido'] = nombre_completo[0], nombre_completo[1] if len(nombre_completo) > 1 else ''
            update_data.update(form_data)
            with conn.cursor() as cur:
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
                descripcion_audit = f"Editó los datos del cliente {update_data['nombre']} {update_data['apellido']} (C.I. {update_data['cedula']})."
                registrar_accion_auditoria('EDICION_CLIENTE', descripcion_audit, client_id)
                conn.commit()
                flash('¡Cliente actualizado exitosamente!', 'success')
                cedula_actualizada = update_data.get('cedula')
                return redirect(url_for('consulta', busqueda=cedula_actualizada))
        except (psycopg2.Error, ValueError, ConnectionError) as e: 
            conn.rollback()
            flash(f'Ocurrió un error al actualizar: {e}', 'error')
    return render_template('edit_cliente.html', cliente=cliente)

@app.route('/delete/<int:client_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin')
def delete_client(client_id):
    conn = get_db()
    if not conn: return redirect(url_for('consulta'))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT nombre, apellido, cedula FROM clientes WHERE id = %s", (client_id,))
            cliente_a_borrar = cur.fetchone()
            if not cliente_a_borrar:
                flash('El cliente que intenta eliminar no existe.', 'warning')
                return redirect(url_for('consulta'))
            tablas_relacionadas = ["pagos", "comisiones_generadas", "caja_inscripciones", "ofertas", "gestiones_cobranza"]
            for tabla in tablas_relacionadas:
                cur.execute(f"DELETE FROM {tabla} WHERE cliente_id = %s", (client_id,))
            descripcion_audit = f"Eliminó al cliente {cliente_a_borrar['nombre']} {cliente_a_borrar['apellido']} (C.I. {cliente_a_borrar['cedula']}) y todos sus datos asociados."
            registrar_accion_auditoria('ELIMINACION_CLIENTE', descripcion_audit, client_id)
            cur.execute("DELETE FROM clientes WHERE id = %s", (client_id,))
            conn.commit()
            flash('¡Cliente y todos sus registros asociados han sido eliminados exitosamente!', 'success')
    except (psycopg2.Error, ConnectionError) as e:
        conn.rollback()
        flash(f'Ocurrió un error al eliminar: {e}', 'error')
    return redirect(url_for('consulta'))

@app.route('/guardar_oferta/<int:client_id>', methods=['POST'])
@admin_required
def guardar_oferta(client_id):
    conn = get_db()
    cuotas_ofertadas, cedula_cliente = request.form.get('cuotas_ofertadas'), ''
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta'))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cedula, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s", (client_id,))
            cliente_info = cur.fetchone()
            if cliente_info:
                cedula_cliente, nombre_cliente = cliente_info['cedula'], cliente_info['nombre_apellido']
            hoy, inicio_mes = get_venezuela_current_date(), get_venezuela_current_date().replace(day=1)
            cur.execute("SELECT 1 FROM pagos WHERE cliente_id = %s AND tipo_pago = 'Cuota' AND puntualidad = 'Impuntual' AND fecha_pago >= %s", (client_id, inicio_mes))
            if cur.fetchone():
                flash("No se puede registrar la oferta: El cliente tiene un pago impuntual registrado en el mes actual.", 'error')
                return redirect(url_for('consulta', busqueda=cedula_cliente))
            if not cuotas_ofertadas or not cuotas_ofertadas.isdigit() or int(cuotas_ofertadas) <= 0:
                flash("Debe ingresar un número válido de cuotas para la oferta.", 'error')
                return redirect(url_for('consulta', busqueda=cedula_cliente))
            cur.execute("INSERT INTO ofertas (cliente_id, cuotas_ofertadas, fecha_oferta, estado_oferta) VALUES (%s, %s, %s, 'activa')", (client_id, int(cuotas_ofertadas), hoy))
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
            cur.execute("SELECT id, (nombre || ' ' || apellido) as nombre_apellido, cedula, cuotas_pagadas_progresivas, meses_retraso_entrega FROM clientes WHERE TRIM(UPPER(proceso)) = 'AHORRADOR' AND cuotas_pagadas_progresivas >= (12 + meses_retraso_entrega) AND TRIM(UPPER(estatus)) = 'ACTIVO' ORDER BY nombre, apellido;")
            clientes_elegibles_ahorro = cur.fetchall()
            cur.execute("SELECT o.cuotas_ofertadas, c.id, (c.nombre || ' ' || c.apellido) as nombre_apellido, c.cedula FROM ofertas o JOIN clientes c ON o.cliente_id = c.id WHERE o.estado_oferta = 'activa' AND TRIM(UPPER(c.proceso)) = 'AHORRADOR' ORDER BY o.cuotas_ofertadas DESC, o.fecha_oferta ASC;")
            ofertas_activas = cur.fetchall()
            cur.execute("SELECT a.id, a.fecha_adjudicacion, (gs.nombre || ' ' || gs.apellido) as nombre_ganador_sorteo, (go.nombre || ' ' || go.apellido) as nombre_ganador_oferta FROM adjudicaciones a LEFT JOIN clientes gs ON a.ganador_sorteo_id = gs.id LEFT JOIN clientes go ON a.ganador_oferta_id = go.id ORDER BY a.fecha_adjudicacion DESC;")
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
                    INSERT INTO pagos (cliente_id, monto, tipo_pago, por_concepto_de, estado_pago, reportado_por_cliente, estado_reporte, fecha_creacion, registrado_por_id, detalles_reporte)
                    VALUES (%s, %s, 'Pago Oferta', %s, 'Pendiente', FALSE, 'Generado por Sistema', %s, %s, %s::jsonb)
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

# --- RUTAS DEL PORTAL DEL CLIENTE ---
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
            cliente_dict = dict(cliente)
            
            cur.execute("SELECT *, detalles_reporte FROM pagos WHERE cliente_id = %s ORDER BY fecha_pago DESC, id DESC;", (session['cliente_id'],))
            pagos = cur.fetchall()
            cliente_dict['pagos'] = [dict(p) for p in pagos]
            
            cur.execute("SELECT * FROM ofertas WHERE cliente_id = %s ORDER BY fecha_oferta DESC", (session['cliente_id'],))
            ofertas = cur.fetchall()
            cliente_dict['ofertas'] = [dict(o) for o in ofertas]
            
            pago_pendiente_existente = any(pago['estado_pago'] == 'Pendiente' for pago in pagos)
            
            # Lógica para la tarjeta de Cita Confirmada
            cita_confirmada = None
            cur.execute("""
                SELECT s.*, a.usuario as nombre_asesor
                FROM solicitudes s
                LEFT JOIN administradores a ON (s.detalles->>'asesor_id')::int = a.id
                WHERE s.cliente_id = %s 
                AND s.tipo_solicitud = 'Cita' 
                AND s.estado = 'Aprobada'
                AND (s.detalles->>'fecha_cita')::date >= NOW()::date
                ORDER BY (s.detalles->>'fecha_cita')::date ASC, (s.detalles->>'hora_cita') ASC
                LIMIT 1;
            """, (session['cliente_id'],))
            cita_confirmada = cur.fetchone()

            hoy, dia_de_vencimiento = get_venezuela_current_date(), 3
            pago_del_mes_realizado = any(pago['tipo_pago'] == 'Cuota' and pago['fecha_pago'].year == hoy.year and pago['fecha_pago'].month == hoy.month and pago['estado_pago'] == 'Conciliado' for pago in pagos)
            estado_cuota = {}
            if pago_del_mes_realizado:
                estado_cuota = {'estado': 'Pagada', 'mes': get_nombre_mes(hoy.month), 'mensaje': 'Tu cuota de este mes ya fue procesada.'}
            elif hoy.day <= dia_de_vencimiento:
                estado_cuota = {'estado': 'Vigente', 'mes': get_nombre_mes(hoy.month), 'fecha_vencimiento': f"{dia_de_vencimiento:02d}/{hoy.month:02d}/{hoy.year}"}
            else:
                estado_cuota = {'estado': 'En Mora', 'mes': get_nombre_mes(hoy.month), 'fecha_vencimiento': f"{dia_de_vencimiento:02d}/{hoy.month:02d}/{hoy.year}"}
            
            return render_template('portal_dashboard.html', 
                                   cliente=cliente_dict, 
                                   cuota_status=estado_cuota, 
                                   puede_reportar_pago=(not pago_pendiente_existente),
                                   cita_confirmada=cita_confirmada)
    except psycopg2.Error as e:
        flash(f'Ocurrió un error al cargar su información: {e}', 'error')
        return redirect(url_for('portal_login'))

@app.route('/portal/reportar_pago', methods=['GET', 'POST'])
@portal_login_required
def portal_reportar_pago():
    conn, tasas_hoy = get_db(), None
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
    mes_actual = get_nombre_mes(get_venezuela_current_date().month)
    if request.method == 'POST':
        pago_form = {k: v if v else None for k, v in request.form.items()}
        if not all(pago_form.get(key) for key in ['monto', 'fecha_pago', 'forma_pago']):
            flash('Error: Monto, fecha y forma de pago son campos obligatorios.', 'error')
            return render_template('portal_reportar_pago.html', cliente=cliente, mes_actual=mes_actual, tasas_hoy=tasas_hoy, anio_actual=get_venezuela_current_date().year)
        if pago_form.get('forma_pago') not in ['Efectivo', 'Zelle'] and not pago_form.get('referencia'):
            flash('Error: La referencia es obligatoria para pagos que no son en efectivo o Zelle.', 'error')
            return render_template('portal_reportar_pago.html', cliente=cliente, mes_actual=mes_actual, tasas_hoy=tasas_hoy, anio_actual=get_venezuela_current_date().year)
        try:
            with conn.cursor() as cur:
                pago_query = """
                    INSERT INTO pagos (cliente_id, monto, tipo_pago, forma_pago, fecha_pago, pago_en, por_concepto_de, referencia, banco, tasa_dia, monto_bs, 
                                       estado_pago, cuotas_cubiertas, reportado_por_cliente, estado_reporte, fecha_creacion) 
                    VALUES (%s, %s, 'Cuota', %s, %s, %s, %s, %s, %s, %s, %s, 'Pendiente', 0, TRUE, 'Pendiente de Revision', %s);
                """
                fecha_actual_vet = get_venezuela_current_datetime()
                cur.execute(pago_query, (session['cliente_id'], pago_form['monto'], pago_form['forma_pago'], pago_form['fecha_pago'], pago_form.get('pago_en'), 
                                         pago_form.get('por_concepto_de'), pago_form.get('referencia'), pago_form.get('banco'), pago_form.get('tasa_dia'), 
                                         pago_form.get('monto_bs'), fecha_actual_vet))
                conn.commit()
                hora_corte = time(16, 30)
                if fecha_actual_vet.time() < hora_corte:
                    flash('✅ ¡Pago reportado a tiempo! Su reporte fue recibido y el recibo podrá generarse el día de hoy una vez sea verificado.', 'success')
                else:
                    proximo_dia = get_proximo_dia_habil(fecha_actual_vet.date())
                    flash(f'⚠️ ¡Pago reportado fuera de horario! Su reporte fue recibido, pero el recibo podrá generarse a partir del próximo día hábil ({proximo_dia.strftime("%d/%m/%Y")}).', 'warning')
                return redirect(url_for('portal_dashboard'))
        except (psycopg2.Error, ValueError, TypeError) as e:
            conn.rollback()
            flash(f'Ocurrió un error al reportar el pago: {e}', 'error')
    return render_template('portal_reportar_pago.html', cliente=cliente, mes_actual=mes_actual, tasas_hoy=tasas_hoy, anio_actual=get_venezuela_current_date().year)

@app.route('/portal/estado_cuenta')
@portal_login_required
def portal_estado_cuenta():
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
            cur.execute("SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha_pago DESC, id DESC;", (session['cliente_id'],))
            pagos = cur.fetchall()
            fecha_generacion = get_venezuela_current_date().strftime('%d/%m/%Y')
            return render_template('estado_cuenta.html', cliente=cliente, pagos=pagos, fecha_generacion=fecha_generacion)
    except psycopg2.Error as e:
        flash(f'Ocurrió un error al generar el estado de cuenta: {e}', 'error')
        return redirect(url_for('portal_dashboard'))

@app.route('/portal/logout')
def portal_logout():
    session.clear()
    flash('Has cerrado sesión exitosamente.', 'success')
    return redirect(url_for('portal_login'))

# =================================================================================
# ===== INICIO DE RUTAS AÑADIDAS PARA EL PORTAL DEL CLIENTE =====
# =================================================================================

@app.route('/citas/disponibilidad')
@portal_login_required
def citas_disponibilidad():
    fecha_str = request.args.get('fecha')
    if not fecha_str: return jsonify({'error': 'Fecha no proporcionada'}), 400
    try:
        fecha_obj = datetime.strptime(fecha_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Formato de fecha inválido'}), 400
    if fecha_obj.weekday() >= 5:
        return jsonify({'ocupados': [], 'es_habil': False, 'mensaje': 'No se pueden agendar citas los fines de semana.'})
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
    return jsonify({'ocupados': citas_ocupadas, 'es_habil': True})

@app.route('/portal/guardar_oferta', methods=['POST'])
@portal_login_required
def portal_guardar_oferta():
    conn, cuotas_ofertadas, cliente_id = get_db(), request.form.get('cuotas_ofertadas'), session['cliente_id']
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('portal_dashboard'))
    try:
        with conn.cursor() as cur:
            if not cuotas_ofertadas or not cuotas_ofertadas.isdigit() or int(cuotas_ofertadas) <= 0:
                flash("Debe ingresar un número válido de cuotas para la oferta.", 'error')
                return redirect(url_for('portal_dashboard'))
            cur.execute("INSERT INTO ofertas (cliente_id, cuotas_ofertadas, fecha_oferta, estado_oferta) VALUES (%s, %s, %s, 'activa')", (cliente_id, int(cuotas_ofertadas), get_venezuela_current_date()))
            conn.commit()
            flash(f"¡Tu oferta de {cuotas_ofertadas} cuotas ha sido registrada exitosamente!", 'success')
    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Ocurrió un error al registrar tu oferta: {e}", 'error')
    return redirect(url_for('portal_dashboard'))

@app.route('/portal/solicitar_cita', methods=['POST'])
@portal_login_required
def portal_solicitar_cita():
    conn, cliente_id = get_db(), session['cliente_id']
    fecha_cita, hora_cita, motivo_cita = request.form.get('fecha_cita'), request.form.get('hora_cita'), request.form.get('motivo_cita')
    if not all([fecha_cita, hora_cita, motivo_cita]):
        flash('Todos los campos son requeridos para solicitar la cita.', 'error')
        return redirect(url_for('portal_dashboard'))
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('portal_dashboard'))
    try:
        with conn.cursor() as cur:
            detalles = jsonify({'fecha_cita': fecha_cita, 'hora_cita': hora_cita, 'motivo': motivo_cita}).get_data(as_text=True)
            cur.execute("INSERT INTO solicitudes (cliente_id, tipo_solicitud, detalles, fecha_creacion, estado) VALUES (%s, 'Cita', %s, %s, 'Pendiente')",
                        (cliente_id, detalles, get_venezuela_current_datetime()))
            conn.commit()
            flash(f"Tu solicitud de cita para el {fecha_cita} a las {hora_cita} ha sido enviada. Un asesor te contactará para confirmar.", 'success')
    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Ocurrió un error al enviar tu solicitud: {e}", 'error')
    return redirect(url_for('portal_dashboard'))

@app.route('/portal/solicitar_congelamiento', methods=['POST'])
@portal_login_required
def portal_solicitar_congelamiento():
    conn, cliente_id = get_db(), session['cliente_id']
    if not conn:
        flash("Error de conexión.", 'error')
        return redirect(url_for('portal_dashboard'))
    try:
        with conn.cursor() as cur:
            detalles = jsonify({'motivo': request.form.get('motivo')}).get_data(as_text=True)
            cur.execute("INSERT INTO solicitudes (cliente_id, tipo_solicitud, detalles, fecha_creacion, estado) VALUES (%s, 'Congelamiento', %s, %s, 'Pendiente')",
                        (cliente_id, detalles, get_venezuela_current_datetime()))
            conn.commit()
            flash("Solicitud de congelamiento enviada. Un asesor te contactará.", 'success')
    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Error al enviar tu solicitud: {e}", 'error')
    return redirect(url_for('portal_dashboard'))

@app.route('/portal/solicitar_retiro', methods=['POST'])
@portal_login_required
def portal_solicitar_retiro():
    conn, cliente_id = get_db(), session['cliente_id']
    fecha_correo, email_origen = request.form.get('fecha_correo_retiro'), request.form.get('email_origen_retiro')
    if not fecha_correo or not email_origen:
        flash("Error: La fecha y el correo de origen son necesarios para procesar la solicitud de retiro.", 'danger')
        return redirect(url_for('portal_dashboard'))
    if not conn:
        flash("Error de conexión con la base de datos.", 'error')
        return redirect(url_for('portal_dashboard'))
    try:
        with conn.cursor() as cur:
            detalles = jsonify({
                'mensaje': 'Cliente confirma envío de correo para formalizar retiro.', 
                'fecha_envio_correo': fecha_correo, 
                'email_origen': email_origen
            }).get_data(as_text=True)
            cur.execute("INSERT INTO solicitudes (cliente_id, tipo_solicitud, detalles, fecha_creacion, estado) VALUES (%s, 'Retiro', %s, %s, 'Pendiente')",
                        (cliente_id, detalles, get_venezuela_current_datetime()))
            conn.commit()
            flash("Solicitud de retiro enviada. Un asesor se comunicará contigo para guiarte en los siguientes pasos.", 'warning')
    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Error al enviar tu solicitud: {e}", 'error')
    return redirect(url_for('portal_dashboard'))

@app.route('/portal/ver_reporte/<int:pago_id>')
@portal_login_required
def portal_ver_reporte(pago_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('portal_dashboard'))
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM pagos WHERE id = %s AND cliente_id = %s", (pago_id, session['cliente_id']))
        pago = cur.fetchone()
    if not pago:
        flash('El reporte de pago solicitado no existe o no tienes permiso para verlo.', 'danger')
        return redirect(url_for('portal_dashboard'))
    return render_template('ver_reporte.html', pago=pago, is_client_view=True)

# =================================================================================
# ===== FIN DE RUTAS AÑADIDAS =====
# =================================================================================

@app.route('/ver_reporte/<int:pago_id>')
@admin_required
def ver_reporte(pago_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta'))
    with conn.cursor() as cur:
        query = "SELECT p.*, (c.nombre || ' ' || c.apellido) as nombre_apellido, c.cedula FROM pagos p JOIN clientes c ON p.cliente_id = c.id WHERE p.id = %s;"
        cur.execute(query, (pago_id,))
        pago = cur.fetchone()
    if not pago:
        flash('El reporte de pago no fue encontrado.', 'error')
        return redirect(url_for('consulta'))
    return render_template('ver_reporte.html', pago=pago, is_client_view=False)

@app.route('/procesar_reporte/<int:pago_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def procesar_reporte(pago_id):
    conn = get_db()
    accion = request.form.get('accion')
    cedula_cliente = request.form.get('cedula_cliente', '')

    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta', busqueda=cedula_cliente))

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cliente_id FROM pagos WHERE id = %s", (pago_id,))
            pago = cur.fetchone()
            if not pago:
                flash("El pago no existe.", "error")
                return redirect(url_for('pagos_por_conciliar'))
            cliente_id = pago['cliente_id']

            nuevo_estado_reporte = ''
            detalles_json = None
            descripcion_audit = ''

            if accion == 'aprobar':
                nuevo_estado_reporte = 'Aprobado'
                descripcion_audit = f"Aprobó el reporte de pago N° {pago_id}."

            elif accion == 'rechazar':
                nuevo_estado_reporte = 'Inconsistente'
                motivo = request.form.get('motivo_rechazo')
                diferencia_str = request.form.get('diferencia_monto', '0').replace(',', '.')
                diferencia = Decimal(diferencia_str) if diferencia_str else Decimal('0')
                
                detalles_rechazo = {'motivo': motivo, 'diferencia': str(diferencia)}
                detalles_json = jsonify(detalles_rechazo).get_data(as_text=True)
                descripcion_audit = f"Marcó el reporte N° {pago_id} como Inconsistente. Motivo: {motivo}."

                if motivo == 'Diferencia de Monto' and diferencia > 0:
                    cur.execute("""
                        INSERT INTO pagos (cliente_id, monto, tipo_pago, por_concepto_de, estado_pago, reportado_por_cliente, estado_reporte, fecha_creacion, registrado_por_id)
                        VALUES (%s, %s, 'Ajuste', %s, 'Pendiente', FALSE, 'Generado por Sistema', %s, %s)
                    """, (cliente_id, diferencia, f"Diferencia del pago N° {pago_id}", get_venezuela_current_datetime(), g.admin['id']))
                    flash(f"Se ha generado una nueva solicitud de pago por la diferencia de ${diferencia}.", "info")
                    descripcion_audit += f" Se generó orden de pago por diferencia de ${diferencia}."

            else:
                flash('Acción no válida.', 'error')
                return redirect(url_for('pagos_por_conciliar'))

            cur.execute(
                """
                UPDATE pagos SET 
                    estado_reporte = %s, 
                    revisado_por_id = %s, 
                    fecha_revision = NOW(),
                    detalles_reporte = %s::jsonb
                WHERE id = %s
                """,
                (nuevo_estado_reporte, g.admin['id'], detalles_json, pago_id)
            )
            
            registrar_accion_auditoria('REVISION_REPORTE_PAGO', descripcion_audit, cliente_id)
            conn.commit()
            flash(f"El reporte de pago ha sido procesado exitosamente.", 'success')

    except (psycopg2.Error, ValueError) as e:
        conn.rollback()
        flash(f"Error al procesar el reporte: {e}", "error")
    
    return redirect(url_for('pagos_por_conciliar'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)