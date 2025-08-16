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

# >>> CAMBIO ESPECIFICO: Corrección .date() con paréntesis
def get_venezuela_current_date():
    """Devuelve solo la fecha actual en la zona horaria de Venezuela."""
    return get_venezuela_current_datetime().date()
# <<< FIN CAMBIO

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
                # Intenta parsear sin zona horaria si falla
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
    # >>> CAMBIO ESPECIFICO: Citas del mismo día
    # Pasa la función al contexto global de Jinja para poder usarla en las plantillas.
    g.get_venezuela_current_datetime = get_venezuela_current_datetime
    # <<< FIN CAMBIO
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

def registrar_accion_auditoria(accion, descripcion, cliente_id=None, detalles_adicionales=None):
    if not g.admin and 'cliente_id' not in session:
        logging.warning(f"AUDITORIA-OMITIDA: Intento de registrar '{accion}' sin un usuario autenticado.")
        return

    conn = get_db()
    if not conn:
        logging.error("AUDITORIA-FALLO-CONEXION: No se pudo obtener conexión a la base de datos.")
        return

    usuario_id = g.admin['id'] if g.admin else None
    usuario_nombre = g.admin['usuario'] if g.admin else f"Cliente ID {session.get('cliente_id')}"
    cliente_afectado = cliente_id if cliente_id else session.get('cliente_id')
    detalles_json = json.dumps(detalles_adicionales) if detalles_adicionales else None

    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO registros_auditoria (usuario_id, usuario_nombre, accion, descripcion, cliente_afectado_id, detalles) VALUES (%s, %s, %s, %s, %s, %s)",
                (usuario_id, usuario_nombre, accion, descripcion, cliente_afectado, detalles_json)
            )
        conn.commit()
        logging.info(f"AUDITORIA-REGISTRADA: Usuario '{usuario_nombre}' realizó '{accion}'.")
    except Exception as e:
        logging.error(f"AUDITORIA-FALLO-INSERCION: {e}")
        conn.rollback()


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

def get_feriados_venezuela(year):
    """Devuelve una lista de fechas (date objects) para feriados en Venezuela."""
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

# =================================================================================
# ===== RUTAS DE NAVEGACIÓN Y AUTENTICACIÓN =====
# =================================================================================

@app.route('/')
def home():
    if g.admin:
        return redirect(url_for('hub'))
    elif g.cliente:
        return redirect(url_for('portal_dashboard'))
    return redirect(url_for('admin_login'))

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
    # INICIO CAMBIO HOJA DE RUTA PUNTO 9
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
                # La consulta de clientes en cartera debe ser para todos los roles que gestionan clientes
                if g.admin['rol'] in ['superadmin', 'gerente', 'administradora']:
                     cur.execute("SELECT COUNT(*) FROM clientes WHERE estatus = 'ACTIVO'")
                else: # Asesores solo ven su cartera
                    cur.execute("SELECT COUNT(*) FROM clientes WHERE gestor_id = %s AND estatus = 'ACTIVO'", (g.admin['id'],))
                stats['clientes_cartera'] = cur.fetchone()[0]

                # Recaudado del mes solo para roles gerenciales
                if g.admin['rol'] in ['superadmin', 'gerente']:
                    first_day_of_month = get_venezuela_current_date().replace(day=1)
                    cur.execute("SELECT COALESCE(SUM(monto), 0) FROM pagos WHERE estado_pago = 'Conciliado' AND fecha_pago >= %s", (first_day_of_month,))
                    stats['recaudado_mes'] = cur.fetchone()[0]
                
                # Solicitudes pendientes para roles administrativos y gerenciales
                if g.admin['rol'] in ['superadmin', 'gerente', 'administradora']:
                    cur.execute("SELECT COUNT(*) FROM solicitudes WHERE estado = 'Pendiente'")
                    stats['solicitudes_pendientes'] = cur.fetchone()[0]

                # Reportes y pagos por conciliar para roles administrativos y gerenciales
                if g.admin['rol'] in ['superadmin', 'gerente', 'administradora']:
                    cur.execute("SELECT COUNT(*) FROM pagos WHERE reportado_por_cliente = TRUE AND estado_reporte = 'Pendiente de Revision'")
                    stats['reportes_pendientes'] = cur.fetchone()[0]

                    cur.execute("""
                        SELECT COUNT(*) FROM pagos 
                        WHERE estado_pago = 'Pendiente' 
                        AND (reportado_por_cliente = FALSE OR estado_reporte = 'Aprobado')
                    """)
                    stats['pagos_por_conciliar'] = cur.fetchone()[0]

                # Tasa BCV para todos
                cur.execute("SELECT tasa FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (get_venezuela_current_date(),))
                tasa_row = cur.fetchone()
                if tasa_row and tasa_row['tasa']:
                    stats['tasa_bcv'] = f"{tasa_row['tasa']:,.2f} Bs"
                
                # Tasa de ocupación para roles que gestionan citas
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
    # FIN CAMBIO HOJA DE RUTA PUNTO 9


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
            # Marcar inasistencias de días anteriores
            cur.execute("""
                UPDATE solicitudes 
                SET estado = 'Completada', detalles = jsonb_set(detalles, '{estado_final}', '"Inasistencia"')
                WHERE tipo_solicitud = 'Cita' AND estado = 'Aprobada' AND (detalles->>'asesor_id')::int = %s
                AND (detalles->>'fecha_cita')::date < %s
            """, (asesor_id, today_str))
            conn.commit()

            # Obtener citas pendientes para hoy
            cur.execute("""
                SELECT s.id, s.detalles, c.nombre || ' ' || c.apellido as nombre_cliente
                FROM solicitudes s JOIN clientes c ON s.cliente_id = c.id
                WHERE s.tipo_solicitud = 'Cita' AND s.estado = 'Aprobada'
                AND (s.detalles->>'asesor_id')::int = %s
                AND (s.detalles->>'fecha_cita') = %s
                ORDER BY (s.detalles->>'hora_cita') ASC
            """, (asesor_id, today_str))
            citas_pendientes = cur.fetchall()

            # Obtener historial de citas completadas
            cur.execute("""
                SELECT s.id, s.detalles, s.detalles->>'estado_final' as estado_final, c.nombre || ' ' || c.apellido as nombre_cliente
                FROM solicitudes s JOIN clientes c ON s.cliente_id = c.id
                WHERE s.tipo_solicitud = 'Cita' AND s.estado = 'Completada'
                AND (s.detalles->>'asesor_id')::int = %s
                ORDER BY (s.detalles->>'fecha_cita')::date DESC, (s.detalles->>'hora_cita') DESC
                LIMIT 10
            """, (asesor_id,))
            citas_completadas = cur.fetchall()

            # Obtener clientes en mora asignados al asesor
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
        # >>> CAMBIO ESPECIFICO: Contador y estado para gestión de cita
        # Devolver una respuesta JSON en lugar de redireccionar para que el fetch de JS funcione
        return jsonify({'status': 'error', 'message': 'Error de conexión'}), 500
        # <<< FIN CAMBIO

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cliente_id, detalles FROM solicitudes WHERE id = %s AND (detalles->>'asesor_id')::int = %s", (cita_id, g.admin['id']))
            cita = cur.fetchone()
            if not cita:
                # >>> CAMBIO ESPECIFICO: Contador y estado para gestión de cita
                return jsonify({'status': 'error', 'message': 'Cita no encontrada o sin permiso'}), 404
                # <<< FIN CAMBIO

            detalles = cita['detalles']
            hora_actual_str = get_venezuela_current_datetime().isoformat()
            
            if accion == 'iniciar':
                detalles['hora_inicio'] = hora_actual_str
                cur.execute("UPDATE solicitudes SET detalles = %s WHERE id = %s", (json.dumps(detalles), cita_id))
                registrar_accion_auditoria('INICIO_ATENCION_CITA', f"Asesor {g.admin['usuario']} inició atención para cita N°{cita_id}.", cita['cliente_id'])
                conn.commit()
                # >>> CAMBIO ESPECIFICO: Contador y estado para gestión de cita
                return jsonify({'status': 'success', 'message': 'Inicio de atención registrado.'})
                # <<< FIN CAMBIO

            elif accion == 'finalizar':
                # >>> CAMBIO ESPECIFICO: Bloquear reporte si descripción está vacía
                reporte = request.form.get('reporte', '').strip()
                if not reporte:
                    flash("El reporte no puede estar vacío.", "error")
                    return redirect(url_for('hub_asesor'))
                # <<< FIN CAMBIO
                
                detalles['hora_fin'] = hora_actual_str
                detalles['reporte'] = reporte
                detalles['estado_final'] = 'Atendida'
                cur.execute("UPDATE solicitudes SET estado = 'Completada', detalles = %s WHERE id = %s", (json.dumps(detalles), cita_id))
                registrar_accion_auditoria('FIN_ATENCION_CITA', f"Asesor {g.admin['usuario']} finalizó atención y registró reporte para cita N°{cita_id}.", cita['cliente_id'])
                flash("Atención finalizada y reporte guardado.", "success")
            
            conn.commit()
    except (psycopg2.Error, json.JSONDecodeError) as e:
        conn.rollback()
        # >>> CAMBIO ESPECIFICO: Contador y estado para gestión de cita
        if accion == 'iniciar':
            return jsonify({'status': 'error', 'message': str(e)}), 500
        # <<< FIN CAMBIO
        flash(f"Error al registrar la interacción: {e}", "danger")

    return redirect(url_for('hub_asesor'))

# >>> CAMBIO ESPECIFICO: Ver reporte de cita
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

            # Seguridad: Verificar que el cliente solo vea sus propias citas
            if is_cliente and cita['cliente_id'] != session['cliente_id']:
                flash('No tienes permiso para ver este reporte.', 'error')
                return redirect(url_for('portal_dashboard'))

            return render_template('ver_reporte_cita.html', cita=cita, is_admin_view=is_admin)

    except psycopg2.Error as e:
        flash(f"Error al cargar el reporte: {e}", "error")
        return redirect(url_for('home'))
# <<< FIN CAMBIO

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
    # >>> CAMBIO ESPECIFICO: Notificación "Cancelar Cita"
    # Se ajusta la lógica para que la cancelación se refleje correctamente
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
    # <<< FIN CAMBIO

# =================================================================================
# ===== NUEVO FLUJO DE VALIDACIÓN Y CONCILIACIÓN =====
# =================================================================================

@app.route('/reportes_por_revisar')
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def reportes_por_revisar():
    conn = get_db()
    reportes_a_revisar = []
    if not conn:
        flash("Error de conexión con la base de datos.", "danger")
        return render_template('reportes_por_revisar.html', reportes=[], anio_actual=get_venezuela_current_date().year)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.id, p.monto, p.tipo_pago, p.fecha_creacion,
                       p.cliente_id, c.nombre, c.apellido, c.cedula
                FROM pagos p
                JOIN clientes c ON p.cliente_id = c.id
                WHERE p.reportado_por_cliente = TRUE AND p.estado_reporte = 'Pendiente de Revision'
                ORDER BY p.fecha_creacion ASC;
            """)
            reportes_a_revisar = cur.fetchall()
    except psycopg2.Error as e:
        logging.error(f"Error al obtener reportes por revisar: {e}")
        flash("Error al cargar la lista de reportes pendientes de revisión.", "danger")
    
    return render_template('reportes_por_revisar.html', reportes=reportes_a_revisar, anio_actual=get_venezuela_current_date().year)

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
            cur.execute("SELECT cliente_id, monto FROM pagos WHERE id = %s", (pago_id,))
            pago = cur.fetchone()
            if not pago:
                flash("El pago no existe.", "error")
                return redirect(url_for('reportes_por_revisar'))
            cliente_id = pago['cliente_id']

            nuevo_estado_reporte = ''
            detalles_json = None
            descripcion_audit = ''

            if accion == 'aprobar':
                nuevo_estado_reporte = 'Aprobado'
                descripcion_audit = f"Aprobó el reporte de pago N° {pago_id}."
                cur.execute(
                    "UPDATE pagos SET estado_reporte = %s, revisado_por_id = %s, fecha_revision = NOW() WHERE id = %s",
                    (nuevo_estado_reporte, g.admin['id'], pago_id)
                )

            elif accion == 'rechazar':
                nuevo_estado_reporte = 'Inconsistente'
                motivo = request.form.get('motivo_rechazo')
                
                if motivo == 'Diferencia de Monto':
                    monto_recibido_str = request.form.get('diferencia_monto', '0').replace(',', '.')
                    monto_recibido = Decimal(monto_recibido_str) if monto_recibido_str else Decimal('0')
                    monto_reportado = pago['monto']
                    
                    if monto_recibido <= 0 or monto_recibido >= monto_reportado:
                        flash("El monto recibido debe ser mayor a cero y menor que el monto reportado.", "error")
                        return redirect(url_for('reportes_por_revisar'))

                    monto_pendiente = monto_reportado - monto_recibido
                    
                    detalles_rechazo = {
                        'motivo': motivo,
                        'mensaje_estandar': "El monto reportado no coincide con el movimiento bancario, debe pagar diferencia.",
                        'monto_original_reportado': str(monto_reportado),
                        'monto_recibido_real': str(monto_recibido),
                        'monto_pendiente': str(monto_pendiente)
                    }
                    detalles_json = json.dumps(detalles_rechazo)

                    # Actualizar el pago original con el monto real recibido
                    cur.execute(
                        "UPDATE pagos SET monto = %s, estado_reporte = %s, revisado_por_id = %s, fecha_revision = NOW(), detalles_reporte = %s WHERE id = %s",
                        (monto_recibido, nuevo_estado_reporte, g.admin['id'], detalles_json, pago_id)
                    )
                    
                    # Crear la nueva orden de pago por la diferencia (CORREGIDO)
                    # Ahora nace como 'Pendiente de Cliente' para que no aparezca en conciliación.
                    concepto_orden = f"Diferencia pendiente del reporte #{pago_id}"
                    cur.execute("""
                        INSERT INTO pagos (cliente_id, monto, tipo_pago, forma_pago, por_concepto_de, fecha_pago, estado_pago, reportado_por_cliente, estado_reporte, fecha_creacion, registrado_por_id, pago_padre_id, cuotas_cubiertas)
                        VALUES (%s, %s, 'Diferencia', 'Diferencia', %s, %s, 'Pendiente', FALSE, 'Pendiente de Cliente', %s, %s, %s, 0)
                    """, (cliente_id, monto_pendiente, concepto_orden, get_venezuela_current_date(), get_venezuela_current_datetime(), g.admin['id'], pago_id))
                    
                    descripcion_audit = f"Rechazó reporte #{pago_id} por diferencia. Monto real: ${monto_recibido}. Se generó orden por diferencia de ${monto_pendiente}."

                else: # Otros motivos de rechazo
                    detalles_rechazo = {'motivo': motivo}
                    detalles_json = json.dumps(detalles_rechazo)
                    descripcion_audit = f"Rechazó el reporte N° {pago_id}. Motivo: {motivo}."
                    cur.execute(
                        "UPDATE pagos SET estado_reporte = %s, revisado_por_id = %s, fecha_revision = NOW(), detalles_reporte = %s WHERE id = %s",
                        (nuevo_estado_reporte, g.admin['id'], detalles_json, pago_id)
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
                WHERE p.estado_pago = 'Pendiente' 
                AND (p.reportado_por_cliente = FALSE OR p.estado_reporte = 'Aprobado')
                ORDER BY p.fecha_creacion ASC;
            """)
            pagos_pendientes = cur.fetchall()
            
            for pago_row in pagos_pendientes:
                pago = dict(pago_row)
                
                pago['status_display'] = 'Listo para Conciliar'
                pago['status_class'] = 'primary'
                pago['action_type'] = 'Conciliar'
                pago['disabled_reason'] = ''

                if not pago.get('nombre'):
                    pago['action_type'] = 'Ninguna'
                    pago['disabled_reason'] = 'El cliente asociado fue eliminado.'
                
                pagos_a_procesar.append(pago)
    except psycopg2.Error as e:
        logging.error(f"Error al obtener pagos por conciliar: {e}")
        flash("Error al cargar la lista de pagos pendientes.", "danger")
        
    return render_template('pagos_por_conciliar.html', pagos=pagos_a_procesar, anio_actual=get_venezuela_current_date().year)


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
                # >>> CAMBIO ESPECIFICO: sum(..., 0) para listas vacías
                if contratos:
                    stats['ingresos_brutos_inscripciones'] = sum((c['monto_inscripcion'] for c in contratos), 0)
                    stats['total_sobrante_pendiente'] = sum((c['sobrante_empresa'] or Decimal('0.0') for c in contratos), 0)
                if resumen_asesores:
                    stats['total_comisiones_pendientes'] = sum((a['total_pendiente'] for a in resumen_asesores), 0)
                # <<< FIN CAMBIO
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
    # INICIO CAMBIO HOJA DE RUTA PUNTO 7
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

            pool_total = plan_contratado_decimal * Decimal('0.16')
            total_pagado = sum(c['monto_comision'] for c in comisiones)
            
            comisiones_json = [{'beneficiario': c['nombre_beneficiario'], 'concepto': c['concepto'], 'monto': f"{c['monto_comision']:,.2f}"} for c in comisiones]
            
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
    # FIN CAMBIO HOJA DE RUTA PUNTO 7

@app.route('/comercial/historial_asesor/<string:nombre_beneficiario>')
@admin_required
@rol_requerido('superadmin', 'gerente')
def get_historial_asesor(nombre_beneficiario):
    # INICIO CAMBIO HOJA DE RUTA PUNTO 7
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
                try:
                    plan_contratado_val = Decimal(item['plan_contratado'])
                except (TypeError, InvalidOperation, ValueError):
                    plan_contratado_val = Decimal('0.00')

                historial_json.append({
                    'concepto': item['concepto'], 'monto': f"{item['monto_comision']:,.2f}",
                    'contrato_nro': item['contrato_nro'], 'cliente': f"{item['nombre']} {item['apellido']}",
                    'plan_contratado': f"{plan_contratado_val:,.2f}", 'responsable_cierre': item['responsable_cierre']
                })
            return jsonify(historial_json)
    except (psycopg2.Error, TypeError) as e:
        logging.error(f"Error en get_historial_asesor para {nombre_beneficiario}: {e}")
        return jsonify({'error': 'Error al consultar la base de datos'}), 500
    # FIN CAMBIO HOJA DE RUTA PUNTO 7

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
                
                # >>> CAMBIO ESPECIFICO: Parametrización segura en reporte_morosidad
                # >>> CAMBIO ESPECIFICO: Consulta multilínea legible
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
                # <<< FIN CAMBIO
                # <<< FIN CAMBIO
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

                # >>> CAMBIO ESPECIFICO: Historial Unificado (versión simplificada para perfil)
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
                        # >>> CAMBIO ESPECIFICO: Manejo defensivo JSONDecodeError
                        try:
                            detalles = json.loads(detalles)
                        except json.JSONDecodeError:
                            detalles = {}
                        # <<< FIN CAMBIO
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
                # <<< FIN CAMBIO
        except (psycopg2.Error, json.JSONDecodeError) as e:
            flash(f"Error al cargar el perfil del cliente: {e}", "error")
            return redirect(url_for('consulta'))
    return render_template('cliente_perfil.html', cliente=cliente, pagos=pagos, gestiones=gestiones, historial_eventos=historial_eventos, anio_actual=get_venezuela_current_date().year)

@app.route('/agregar_gestion/<int:cliente_id>', methods=['POST'])
@admin_required
def agregar_gestion(cliente_id):
    nota = request.form.get('nota')
    tipo_gestion = request.form.get('tipo_gestion') # Capturar el nuevo campo
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
        if 'cliente_id' in session:
            return redirect(url_for('portal_dashboard'))
        else:
            return redirect(url_for('home'))
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
                # >>> CAMBIO ESPECIFICO: Reportes/recibos en VES
                detalles_pago = {}
                if forma_pago == 'Pago Móvil':
                    detalles_pago['telefono_emisor'] = pago_form.get('pago_movil_telefono')
                    detalles_pago['cedula_emisor'] = pago_form.get('pago_movil_cedula')
                elif forma_pago == 'Binance':
                    detalles_pago['usuario_binance'] = pago_form.get('binance_user')
                detalles_json = json.dumps(detalles_pago) if detalles_pago else None
                # <<< FIN CAMBIO

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
            # Bloquear las filas para evitar race conditions
            cur.execute("SELECT * FROM pagos WHERE id = %s FOR UPDATE", (pago_id,))
            pago_actual = cur.fetchone()
            if not pago_actual or pago_actual['estado_pago'] != 'Pendiente':
                flash("El pago no se puede conciliar porque no está pendiente.", 'error')
                return redirect(url_for('pagos_por_conciliar'))
            
            cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s FOR UPDATE", (pago_actual['cliente_id'],))
            cliente = cur.fetchone()
            cedula_cliente_para_redirect = cliente['cedula']
            admin_id = g.admin['id']

            flash_msg = ""
            if pago_actual['tipo_pago'] == 'Inscripción':
                inscripcion_pagada_actual = Decimal(cliente.get('inscripcion_pagada') or 0)
                inscripcion_total_requerida = Decimal(cliente.get('inscripcion_monto') or 0)
                nueva_inscripcion_pagada = inscripcion_pagada_actual + pago_actual['monto']

                # --- INICIO DE LA NUEVA LÓGICA DE CONSOLIDACIÓN DE INSCRIPCIÓN ---
                if nueva_inscripcion_pagada >= inscripcion_total_requerida:
                    # 1. Obtener todos los abonos de inscripción anteriores más el actual
                    cur.execute("SELECT * FROM pagos WHERE cliente_id = %s AND tipo_pago = 'Inscripción' AND estado_pago = 'Conciliado'", (cliente['id'],))
                    abonos_anteriores = cur.fetchall()
                    
                    pagos_a_consolidar = abonos_anteriores + [pago_actual]
                    ids_a_anular = [p['id'] for p in pagos_a_consolidar]
                    
                    # 2. Preparar detalles para el nuevo recibo final
                    monto_total_consolidado = sum(p['monto'] for p in pagos_a_consolidar)
                    pagos_individuales = [
                        {"id": p['id'], "monto": str(p['monto']), "fecha": p['fecha_pago'].isoformat()}
                        for p in pagos_a_consolidar
                    ]
                    detalles_consolidados = {
                        "pagos_individuales": pagos_individuales
                    }
                    
                    # 3. Crear el único recibo final de "Inscripción Finalizada"
                    cur.execute("""
                        INSERT INTO pagos (cliente_id, monto, tipo_pago, forma_pago, estado_pago, por_concepto_de, detalles_reporte, conciliado_por_id, fecha_pago, cuotas_cubiertas) 
                        VALUES (%s, %s, 'Inscripción Finalizada', 'Consolidado', 'Conciliado', 'Pago total de inscripción consolidado', %s, %s, %s, 0) RETURNING id
                    """, (cliente['id'], monto_total_consolidado, json.dumps(detalles_consolidados), admin_id, pago_actual['fecha_pago']))
                    pago_final_id = cur.fetchone()[0]

                    # 4. Anular todos los recibos de abono (anteriores y el actual)
                    detalle_anulacion = json.dumps({
                        "motivo": "Consolidado en recibo final",
                        "recibo_final_id": pago_final_id
                    })
                    cur.execute(
                        "UPDATE pagos SET estado_pago = 'Anulado', detalles_reporte = %s WHERE id = ANY(%s)",
                        (detalle_anulacion, ids_a_anular)
                    )
                    
                    # 5. Actualizar el estado del cliente
                    cur.execute("UPDATE clientes SET inscripcion_pagada = %s, proceso = 'INSCRITO' WHERE id = %s", (monto_total_consolidado, cliente['id']))
                    
                    # 6. Registrar auditoría
                    descripcion_audit = f"Consolidó pagos de inscripción. Recibo final N°{pago_final_id} por ${monto_total_consolidado} para {cliente['nombre_apellido']}."
                    registrar_accion_auditoria('CONSOLIDACION_INSCRIPCION', descripcion_audit, cliente['id'])
                    
                    url_recibo = url_for('ver_recibo_inscripcion', pago_id=pago_final_id)
                    flash_msg = f"¡Inscripción completada y consolidada! <a href='{url_recibo}' target='_blank' class='alert-link'>Ver Recibo Final</a>."
                
                # --- FIN DE LA NUEVA LÓGICA DE CONSOLIDACIÓN ---
                else: # Si es solo un abono que no completa el total
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
        logging.error(f"Error al conciliar el pago {pago_id}: {e}")
        flash(f'Ocurrió un error al conciliar el pago: {e}', 'error')
        if cedula_cliente_para_redirect:
            return redirect(url_for('consulta', busqueda=cedula_cliente_para_redirect))
    return redirect(url_for('pagos_por_conciliar'))


@app.route('/recibo/<int:pago_id>')
def generar_recibo_pago(pago_id):
    # INICIO CAMBIO HOJA DE RUTA PUNTO 1
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

    # Verificación de seguridad
    if pago['estado_pago'] not in ['Conciliado', 'Anulado'] and pago['tipo_pago'] != 'Inscripción Finalizada':
        flash('Este recibo no puede ser visualizado porque el pago no ha sido conciliado.', 'warning')
        return redirect(url_for('home'))
    
    # FIN CAMBIO HOJA DE RUTA PUNTO 1

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
                # Lógica para revertir la consolidación
                detalles = pago_a_anular.get('detalles_reporte')
                if detalles and 'pagos_individuales' in detalles:
                    ids_a_restaurar = [p['id'] for p in detalles['pagos_individuales']]
                    # Restaurar abonos a 'Conciliado' y limpiar sus detalles
                    cur.execute("UPDATE pagos SET estado_pago = 'Conciliado', detalles_reporte = NULL WHERE id = ANY(%s)", (ids_a_restaurar,))
                    # Anular el recibo final
                    cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE id = %s", (pago_id,))
                    # Recalcular el total pagado de inscripción y actualizar cliente
                    cur.execute("SELECT COALESCE(SUM(monto), 0) FROM pagos WHERE id = ANY(%s)", (ids_a_restaurar,))
                    total_inscripcion_reactivada = cur.fetchone()[0]
                    cur.execute("UPDATE clientes SET inscripcion_pagada = %s, proceso = 'RESERVA' WHERE id = %s", (total_inscripcion_reactivada, cliente_id))
                    flash("¡Reversión de consolidación completada! Los abonos han sido restaurados.", 'success')
                else:
                    # Fallback si no hay detalles
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
    # INICIO CAMBIO HOJA DE RUTA PUNTO 8
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

            # >>> CAMBIO ESPECIFICO: Adjudicaciones / Ofertas activas
            ofertas_activas = []
            for oferta in ofertas_activas_raw:
                oferta_dict = dict(oferta)
                
                # Calcular cuotas_pagadas
                cur.execute("SELECT COUNT(*) FROM pagos WHERE cliente_id = %s AND tipo_pago = 'Cuota' AND estado_pago = 'Conciliado'", (oferta['id'],))
                cuotas_pagadas = cur.fetchone()[0]
                oferta_dict['cuotas_pagadas'] = cuotas_pagadas if cuotas_pagadas is not None else 0

                # Calcular frecuencia_ofertas
                cur.execute("SELECT COUNT(*) FROM ofertas WHERE cliente_id = %s", (oferta['id'],))
                frecuencia_ofertas = cur.fetchone()[0]
                oferta_dict['frecuencia_ofertas'] = frecuencia_ofertas if frecuencia_ofertas is not None else 0
                
                ofertas_activas.append(oferta_dict)
            # <<< FIN CAMBIO

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
    # FIN CAMBIO HOJA DE RUTA PUNTO 8

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
                
                # CORRECCIÓN: Añadir cuotas_cubiertas=0 en la inserción
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
# ===== RUTAS DEL PORTAL DEL CLIENTE (ACTUALIZADAS) =====
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

            # Lógica para autodescongelar si ha pasado la fecha
            if cliente_dict['estatus'] == 'CONGELADO':
                cur.execute("""
                    SELECT detalles->>'fecha_fin_congelamiento' as fecha_fin 
                    FROM solicitudes 
                    WHERE cliente_id = %s AND tipo_solicitud = 'Congelamiento' AND estado = 'Aprobada'
                    ORDER BY fecha_revision DESC LIMIT 1
                """, (cliente_dict['id'],))
                solicitud_cong = cur.fetchone()
                if solicitud_cong and solicitud_cong['fecha_fin']:
                    fecha_fin_dt = datetime.fromisoformat(solicitud_cong['fecha_fin']).date()
                    if get_venezuela_current_date() > fecha_fin_dt:
                        cur.execute("UPDATE clientes SET estatus = 'INACTIVO' WHERE id = %s", (cliente_dict['id'],))
                        conn.commit()
                        flash("El período de congelamiento de tu plan ha finalizado. Tu estatus ha cambiado a INACTIVO.", "warning")
                        cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s;", (session['cliente_id'],))
                        cliente_dict = dict(cur.fetchone())

            cur.execute("SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha_creacion DESC", (session['cliente_id'],))
            pagos = cur.fetchall()
            cliente_dict['pagos'] = [dict(p) for p in pagos]
            
            cur.execute("SELECT * FROM ofertas WHERE cliente_id = %s ORDER BY fecha_oferta DESC", (session['cliente_id'],))
            cliente_dict['ofertas'] = [dict(o) for o in cur.fetchall()]
            
            # >>> CAMBIO ESPECIFICO: Portal Cliente – rechazos
            reportes_rechazados = []
            for pago in cliente_dict['pagos']:
                if pago.get('estado_reporte') == 'Inconsistente':
                    if isinstance(pago.get('detalles_reporte'), str):
                        # >>> CAMBIO ESPECIFICO: Manejo defensivo JSONDecodeError
                        try:
                            pago['detalles_reporte'] = json.loads(pago['detalles_reporte'])
                        except json.JSONDecodeError:
                            pago['detalles_reporte'] = {}
                        # <<< FIN CAMBIO
                    
                    detalles = pago.get('detalles_reporte', {})
                    if detalles.get('motivo') == 'Diferencia de Monto' and Decimal(detalles.get('diferencia', 0)) > 0:
                        pago['accion_requerida'] = 'pagar_diferencia'
                    else:
                        pago['accion_requerida'] = 'corregir_reporte'
                    reportes_rechazados.append(pago)
            # <<< FIN CAMBIO

            pago_en_proceso = any(p.get('estado_pago') == 'Pendiente' for p in cliente_dict['pagos'])

            cita_confirmada = None
            cur.execute("""
                SELECT s.*, a.usuario as nombre_asesor FROM solicitudes s
                LEFT JOIN administradores a ON (s.detalles->>'asesor_id')::int = a.id
                WHERE s.cliente_id = %s AND s.tipo_solicitud = 'Cita' AND s.estado = 'Aprobada'
                AND (s.detalles->>'fecha_cita')::date >= NOW()::date
                ORDER BY (s.detalles->>'fecha_cita')::date ASC, (s.detalles->>'hora_cita') ASC LIMIT 1;
            """, (session['cliente_id'],))
            cita_confirmada = cur.fetchone()

            estado_principal = {}
            inscripcion_completa = (cliente_dict.get('inscripcion_pagada') or 0) >= (cliente_dict.get('inscripcion_monto') or 0)
            primera_cuota_pagada = any(p.get('tipo_pago') == 'Cuota' and p.get('estado_pago') == 'Conciliado' for p in cliente_dict['pagos'] if p.get('fecha_pago'))
            
            # **LÓGICA DE ETAPAS MEJORADA**
            if not inscripcion_completa:
                estado_principal = {
                    'tipo': 'inscripcion', 'titulo': 'Completa tu Inscripción',
                    'mensaje': "Realiza el pago de tu inscripción para poder activar tu plan.",
                    'boton_texto': 'Pagar Inscripción', 'boton_url': url_for('portal_pagar_inscripcion'),
                    'clase_borde': 'naranja-corporativo',
                    'boton_activo': not pago_en_proceso
                }
            elif inscripcion_completa and not primera_cuota_pagada:
                estado_principal = {
                    'tipo': 'activacion', 'titulo': '¡Inscripción Exitosa!',
                    'mensaje': 'Tu plan está listo para ser activado. Realiza el pago de tu primera cuota para comenzar.',
                    'boton_texto': 'Pagar 1ª Cuota', 'boton_url': url_for('portal_reportar_pago'),
                    'clase_borde': 'azul-info',
                    'boton_activo': not pago_en_proceso
                }
            else:
                hoy, dia_de_vencimiento = get_venezuela_current_date(), 3
                pago_del_mes_realizado = any(pago['tipo_pago'] == 'Cuota' and pago['fecha_pago'].year == hoy.year and pago['fecha_pago'].month == hoy.month and pago['estado_pago'] == 'Conciliado' for pago in cliente_dict['pagos'] if pago.get('fecha_pago'))
                estado_label, mensaje_cuota = ('Pagada', 'Tu cuota de este mes ya fue procesada.') if pago_del_mes_realizado else (('Vigente', f"Tu fecha de vencimiento es el <strong>{dia_de_vencimiento:02d}/{hoy.month:02d}/{hoy.year}</strong>.") if hoy.day <= dia_de_vencimiento else ('En Mora', 'Tu cuota ha vencido. Por favor, realiza tu pago lo antes posible.'))
                estado_principal = {
                    'tipo': 'cuota_mensual', 'titulo': f"Cuota de {get_nombre_mes(hoy.month)}",
                    'estado_label': estado_label, 'mensaje': mensaje_cuota,
                    'clase_borde': estado_label.lower().replace(' ', '.'),
                    'boton_texto': 'Reportar Pago de Cuota' if not pago_del_mes_realizado else None,
                    'boton_url': url_for('portal_reportar_pago'),
                    'boton_activo': not pago_del_mes_realizado and not pago_en_proceso
                }
            
            puede_registrar_oferta = inscripcion_completa and primera_cuota_pagada
            
            # INICIO DE LA CORRECCIÓN: Cargar historial de gestiones
            historial_gestiones = []
            cur.execute("SELECT * FROM solicitudes WHERE cliente_id = %s ORDER BY fecha_creacion DESC", (session['cliente_id'],))
            solicitudes = cur.fetchall()
            for s in solicitudes:
                detalles = s['detalles'] if s['detalles'] else {}
                if isinstance(detalles, str):
                    try: detalles = json.loads(detalles)
                    except json.JSONDecodeError: detalles = {}
                
                descripcion = f"Estado: {s['estado']}"
                if s['tipo_solicitud'] == 'Cita':
                    descripcion = f"Cita para el {detalles.get('fecha_cita')} a las {detalles.get('hora_cita')}. Estado: {s['estado']}"
                elif s['tipo_solicitud'] == 'Congelamiento':
                    descripcion = f"Congelar por {detalles.get('tiempo_congelamiento', 'N/A')}. Estado: {s['estado']}"
                
                historial_gestiones.append({
                    'id': s['id'],
                    'titulo': f"Solicitud de {s['tipo_solicitud']}",
                    'fecha': s['fecha_creacion'],
                    'descripcion': descripcion,
                    'estado': s['estado'],
                    'detalles': detalles,
                    'icono': 'bi-calendar-check' if s['tipo_solicitud'] == 'Cita' else 'bi-snow'
                })

            cur.execute("SELECT * FROM ofertas WHERE cliente_id = %s ORDER BY fecha_oferta DESC", (session['cliente_id'],))
            ofertas = cur.fetchall()
            for o in ofertas:
                 historial_gestiones.append({
                    'id': o['id'],
                    'titulo': f"Oferta Registrada",
                    'fecha': datetime.combine(o['fecha_oferta'], time.min), # Convertir date a datetime
                    'descripcion': f"Ofertaste {o['cuotas_ofertadas']} cuotas. Estado: {o['estado_oferta'].capitalize()}",
                    'estado': 'Aprobada' if o['estado_oferta'] == 'activa' else 'Rechazada',
                    'detalles': {},
                    'icono': 'bi-gem'
                })
            
            # Corrección para TypeError: se estandarizan las fechas a UTC antes de ordenar.
            for gestion in historial_gestiones:
                if gestion['fecha'].tzinfo is None:
                    gestion['fecha'] = pytz.utc.localize(gestion['fecha'])
                else:
                    gestion['fecha'] = gestion['fecha'].astimezone(pytz.utc)
            
            historial_gestiones.sort(key=lambda x: x['fecha'], reverse=True)
            # FIN DE LA CORRECCIÓN

            return render_template('portal_dashboard.html', 
                                   cliente=cliente_dict, 
                                   reportes_rechazados=reportes_rechazados,
                                   estado_principal=estado_principal,
                                   cita_confirmada=cita_confirmada,
                                   puede_registrar_oferta=puede_registrar_oferta,
                                   historial_gestiones=historial_gestiones)
    except (psycopg2.Error, KeyError) as e:
        logging.error(f"Error en portal_dashboard: {e}")
        flash('Ocurrió un error inesperado al cargar tu portal. Inténtalo de nuevo.', 'error')
        return redirect(url_for('portal_login'))

# ===== INICIO DE LA SOLUCIÓN PARA LAS RUTAS DEL PORTAL =====
@app.route('/portal/pagos')
@portal_login_required
def portal_pagos():
    """
    Muestra el historial de pagos del cliente en el portal.
    """
    if g.cliente is None:
        return redirect(url_for('portal_login'))
    
    conn = get_db()
    if not conn:
        flash('No se pudo conectar con la base de datos.', 'error')
        return redirect(url_for('portal_dashboard'))
    
    try:
        with conn.cursor() as cur:
            # Obtener todos los pagos del cliente, mostrando los más recientes primero
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
    """
    Muestra los documentos relevantes del cliente, como su contrato.
    """
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

        # Preparamos una lista de documentos. Por ahora solo el contrato.
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
# ===== FIN DE LA SOLUCIÓN =====


# ===== NUEVA RUTA INTELIGENTE PARA PAGAR DIFERENCIAS =====
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
    
    monto_diferencia = detalles.get('diferencia')

    if not monto_diferencia:
        flash('No hay un monto de diferencia registrado para este pago.', 'error')
        return redirect(url_for('portal_dashboard'))
    
    # Lógica inteligente para dirigir al portal correcto
    if pago_original['tipo_pago'] == 'Inscripción':
        return render_template(
            'portal_pagar_inscripcion.html',
            cliente=cliente,
            tasas_hoy=tasas_hoy,
            monto_precargado=monto_diferencia,
            pago_origen_id=pago_original_id,
            concepto_pago=f"Diferencia del pago de inscripción #{pago_original_id}"
        )
    else: # Para 'Cuota' y otros tipos
        return render_template(
            'portal_reportar_pago.html',
            cliente=cliente,
            tasas_hoy=tasas_hoy,
            monto_precargado=monto_diferencia,
            pago_origen_id=pago_original_id,
            concepto_pago=f"Diferencia del pago #{pago_original_id}"
        )

# ===== NUEVA RUTA PARA CORREGIR REPORTES =====
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
    
    # Reutilizamos la plantilla de reportar pago, pasándole los datos del pago a corregir.
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
        
        # Obtener la tasa del día
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


    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cliente_id, monto FROM pagos WHERE id = %s", (pago_id,))
            pago = cur.fetchone()
            if not pago:
                flash("El pago no existe.", "error")
                return redirect(url_for('reportes_por_revisar'))
            cliente_id = pago['cliente_id']

            nuevo_estado_reporte = ''
            detalles_json = None
            descripcion_audit = ''

            if accion == 'aprobar':
                nuevo_estado_reporte = 'Aprobado'
                descripcion_audit = f"Aprobó el reporte de pago N° {pago_id}."
                cur.execute(
                    "UPDATE pagos SET estado_reporte = %s, revisado_por_id = %s, fecha_revision = NOW() WHERE id = %s",
                    (nuevo_estado_reporte, g.admin['id'], pago_id)
                )

            elif accion == 'rechazar':
                nuevo_estado_reporte = 'Inconsistente'
                motivo = request.form.get('motivo_rechazo')
                
                if motivo == 'Diferencia de Monto':
                    monto_recibido_str = request.form.get('diferencia_monto', '0').replace(',', '.')
                    monto_recibido = Decimal(monto_recibido_str) if monto_recibido_str else Decimal('0')
                    monto_reportado = pago['monto']
                    
                    if monto_recibido <= 0 or monto_recibido >= monto_reportado:
                        flash("El monto recibido debe ser mayor a cero y menor que el monto reportado.", "error")
                        return redirect(url_for('reportes_por_revisar'))

                    monto_pendiente = monto_reportado - monto_recibido
                    
                    detalles_rechazo = {
                        'motivo': motivo,
                        'mensaje_estandar': "El monto reportado no coincide con el movimiento bancario, debe pagar diferencia.",
                        'monto_original_reportado': str(monto_reportado),
                        'monto_recibido_real': str(monto_recibido),
                        'monto_pendiente': str(monto_pendiente)
                    }
                    detalles_json = json.dumps(detalles_rechazo)

                    # Actualizar el pago original con el monto real recibido
                    cur.execute(
                        "UPDATE pagos SET monto = %s, estado_reporte = %s, revisado_por_id = %s, fecha_revision = NOW(), detalles_reporte = %s WHERE id = %s",
                        (monto_recibido, nuevo_estado_reporte, g.admin['id'], detalles_json, pago_id)
                    )
                    
                    # >>> CAMBIO ESPECIFICO: Fix NOT NULL cuotas_cubiertas en pagos por diferencia
                    # Crear la nueva orden de pago por la diferencia
                    concepto_orden = f"Diferencia pendiente del reporte #{pago_id}"
                    cur.execute("""
                        INSERT INTO pagos (cliente_id, monto, tipo_pago, por_concepto_de, estado_pago, reportado_por_cliente, estado_reporte, fecha_creacion, registrado_por_id, pago_padre_id, cuotas_cubiertas)
                        VALUES (%s, %s, 'Diferencia', %s, 'Pendiente', FALSE, 'Generado por Sistema', %s, %s, %s, 0)
                    """, (cliente_id, monto_pendiente, concepto_orden, get_venezuela_current_datetime(), g.admin['id'], pago_id))
                    # <<< FIN CAMBIO
                    
                    descripcion_audit = f"Rechazó reporte #{pago_id} por diferencia. Monto real: ${monto_recibido}. Se generó orden por diferencia de ${monto_pendiente}."

                else: # Otros motivos de rechazo
                    detalles_rechazo = {'motivo': motivo}
                    detalles_json = json.dumps(detalles_rechazo)
                    descripcion_audit = f"Rechazó el reporte N° {pago_id}. Motivo: {motivo}."
                    cur.execute(
                        "UPDATE pagos SET estado_reporte = %s, revisado_por_id = %s, fecha_revision = NOW(), detalles_reporte = %s WHERE id = %s",
                        (nuevo_estado_reporte, g.admin['id'], detalles_json, pago_id)
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
        cur.execute("SELECT tasa, tasa_euro FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (today_str,))
        tasas_hoy = cur.fetchone()
        
    if not cliente:
        session.clear()
        flash('No se encontró su información de cliente.', 'error')
        return redirect(url_for('portal_login'))

    inscripcion_monto = cliente.get('inscripcion_monto') or Decimal('0.0')
    inscripcion_pagada = cliente.get('inscripcion_pagada') or Decimal('0.0')
    inscripcion_completa = inscripcion_pagada >= inscripcion_monto
    contrato_en_dolares = cliente.get('moneda_pago', '').upper() == 'USD'
    mes_actual = get_nombre_mes(get_venezuela_current_date().month)

    if request.method == 'POST':
        pago_form = {k: v if v else None for k, v in request.form.items()}
        
        if not inscripcion_completa:
            flash('Error: No puedes reportar pagos de cuotas hasta que tu inscripción esté completa.', 'error')
            return redirect(url_for('portal_dashboard'))

        if not all(pago_form.get(key) for key in ['monto', 'fecha_pago']):
            flash('Error: Monto y fecha de pago son campos obligatorios.', 'error')
            return render_template('portal_reportar_pago.html', cliente=cliente, mes_actual=mes_actual, inscripcion_completa=inscripcion_completa, contrato_en_dolares=contrato_en_dolares, tasas_hoy=tasas_hoy)

        forma_pago = pago_form.get('forma_pago')
        if forma_pago != 'Efectivo' and not pago_form.get('referencia'):
            flash('Error: La referencia es obligatoria para este método de pago.', 'error')
            return render_template('portal_reportar_pago.html', cliente=cliente, mes_actual=mes_actual, inscripcion_completa=inscripcion_completa, contrato_en_dolares=contrato_en_dolares, tasas_hoy=tasas_hoy)
        
        try:
            with conn.cursor() as cur:
                pago_id_correccion = pago_form.get('pago_id_correccion')
                detalles_pago = {}
                if forma_pago == 'Pago Móvil':
                    detalles_pago['telefono_emisor'] = pago_form.get('pago_movil_telefono')
                    detalles_pago['cedula_emisor'] = pago_form.get('pago_movil_cedula')
                elif forma_pago == 'Binance':
                    detalles_pago['usuario_binance'] = pago_form.get('binance_user')
                detalles_json = json.dumps(detalles_pago) if detalles_pago else None

                if pago_id_correccion:
                    update_query = """
                        UPDATE pagos SET
                            monto = %s, forma_pago = %s, fecha_pago = %s, pago_en = %s, por_concepto_de = %s, 
                            referencia = %s, banco = %s, tasa_dia = %s, monto_bs = %s,
                            estado_reporte = 'Pendiente de Revision', fecha_creacion = %s, detalles_reporte = %s::jsonb
                        WHERE id = %s AND cliente_id = %s
                    """
                    cur.execute(update_query, (
                        pago_form['monto'], forma_pago, pago_form['fecha_pago'], pago_form.get('pago_en'),
                        pago_form.get('por_concepto_de'), pago_form.get('referencia'), pago_form.get('banco'),
                        pago_form.get('tasa_dia'), pago_form.get('monto_bs'),
                        get_venezuela_current_datetime(), detalles_json,
                        pago_id_correccion, session['cliente_id']
                    ))
                    flash('✅ ¡Reporte corregido y enviado! Será verificado nuevamente.', 'success')
                else:
                    pago_query = """
                        INSERT INTO pagos (cliente_id, monto, tipo_pago, forma_pago, fecha_pago, pago_en, por_concepto_de, referencia, banco, tasa_dia, monto_bs, 
                                           estado_pago, cuotas_cubiertas, reportado_por_cliente, estado_reporte, fecha_creacion, detalles_reporte) 
                        VALUES (%s, %s, 'Cuota', %s, %s, %s, %s, %s, %s, %s, %s, 'Pendiente', 0, TRUE, 'Pendiente de Revision', %s, %s::jsonb);
                    """
                    cur.execute(pago_query, (
                        session['cliente_id'], pago_form['monto'], forma_pago, pago_form['fecha_pago'], pago_form.get('pago_en'), 
                        pago_form.get('por_concepto_de'), pago_form.get('referencia'), pago_form.get('banco'), pago_form.get('tasa_dia'), 
                        pago_form.get('monto_bs'), get_venezuela_current_datetime(), detalles_json
                    ))
                    flash('✅ ¡Pago reportado! Será verificado por un administrador.', 'success')

                conn.commit()
                return redirect(url_for('portal_dashboard'))
        except (psycopg2.Error, ValueError, TypeError) as e:
            conn.rollback()
            flash(f'Ocurrió un error al reportar el pago: {e}', 'error')
    
    return render_template('portal_reportar_pago.html', cliente=cliente, mes_actual=mes_actual, inscripcion_completa=inscripcion_completa, contrato_en_dolares=contrato_en_dolares, tasas_hoy=tasas_hoy)

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
            
            resumen = {
                'total_pagado': total_pagado,
                'saldo_pendiente': saldo_pendiente if saldo_pendiente > 0 else 0,
                'cuotas_pagadas': (cliente.get('cuotas_pagadas_progresivas', 0) or 0) + (cliente.get('cuotas_pagadas_regresivas', 0) or 0)
            }

            return {
                'cliente': cliente,
                'historial': historial_unificado,
                'resumen': resumen
            }
    except (psycopg2.Error, json.JSONDecodeError, KeyError) as e:
        logging.error(f"Error al generar estado de cuenta para cliente {cliente_id}: {e}")
        flash(f'Ocurrió un error al generar el estado de cuenta: {e}', 'error')
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

    if is_client_view and g.admin is not None:
        flash('No puedes acceder al portal de clientes con una sesión de administrador activa.', 'warning')
        return redirect(url_for('hub'))
    if is_admin_view and g.cliente is not None:
        flash('No puedes acceder al panel de administración con una sesión de cliente activa.', 'warning')
        return redirect(url_for('portal_dashboard'))

    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('home'))

    pago = None
    try:
        with conn.cursor() as cur:
            if is_client_view:
                cur.execute(
                    "SELECT * FROM pagos WHERE id = %s AND cliente_id = %s",
                    (pago_id, session['cliente_id'])
                )
                pago = cur.fetchone()
                if not pago:
                    flash('El reporte de pago solicitado no existe o no tienes permiso para verlo.', 'danger')
                    return redirect(url_for('portal_dashboard'))
            
            else: # is_admin_view
                query = """
                    SELECT p.*, (c.nombre || ' ' || c.apellido) as nombre_apellido, c.cedula
                    FROM pagos p
                    JOIN clientes c ON p.cliente_id = c.id
                    WHERE p.id = %s;
                """
                cur.execute(query, (pago_id,))
                pago = cur.fetchone()
                if not pago:
                    flash('El reporte de pago no fue encontrado.', 'error')
                    return redirect(url_for('consulta'))

        pago_dict = dict(pago)
        if pago_dict.get('detalles_reporte'):
            if isinstance(pago_dict['detalles_reporte'], str):
                try:
                    pago_dict['detalles_reporte'] = json.loads(pago_dict['detalles_reporte'])
                except json.JSONDecodeError:
                    pago_dict['detalles_reporte'] = {}

        if is_client_view:
            return render_template('ver_reporte.html', pago=pago_dict, is_client_view=True)
        else: # is_admin_view
            origin = request.args.get('origin', 'consulta')
            return render_template('ver_reporte.html', pago=pago_dict, is_client_view=False, origin=origin)

    except psycopg2.Error as e:
        logging.error(f"Error al obtener reporte de pago {pago_id}: {e}")
        flash("Error al cargar el reporte.", "danger")
        return redirect(url_for('home'))


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)