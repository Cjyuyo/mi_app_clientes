import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, g, flash, redirect, url_for, session, Response
from dotenv import load_dotenv
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, date
from calendar import monthrange
import random
from werkzeug.security import check_password_hash, generate_password_hash
from functools import wraps
import io
import csv
import logging
import pytz

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
    app.permanent_session_lifetime = timedelta(minutes=5)
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
                cur.execute("SELECT id, nombre, apellido FROM clientes WHERE id = %s", (cliente_id,))
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

# =================================================================================
# ===== MÓDULO DE TESORERÍA Y REBALANCEO =====
# =================================================================================

def calcular_balances_tesoreria(fecha_hasta=None):
    """
    Calcula los balances actuales de todas las cajas de la tesorería hasta una fecha dada.
    Si fecha_hasta es None, calcula los saldos totales hasta el momento actual.
    """
    conn = get_db()
    balances = {
        'EFECTIVO_USD': Decimal('0.0'),
        'BINANCE_USDT': Decimal('0.0'),
        'CAJA_BS_USD': Decimal('0.0'),
        'CAJA_BS_EUR': Decimal('0.0'),
        'CAJA_BS_TOTAL': Decimal('0.0')
    }
    if not conn:
        return balances

    if fecha_hasta is None:
        fecha_hasta = get_venezuela_current_date()
    
    fecha_fin_timestamp = datetime.combine(fecha_hasta, datetime.max.time())

    try:
        with conn.cursor() as cur:
            # 1. CALCULAR INGRESOS TOTALES HASTA LA FECHA
            cur.execute("""
                SELECT COALESCE(SUM(monto), 0) FROM pagos 
                WHERE estado_pago = 'Conciliado' AND pago_en = 'Efectivo USD' AND fecha_pago <= %s
            """, (fecha_hasta,))
            balances['EFECTIVO_USD'] += cur.fetchone()[0] or Decimal('0.0')

            cur.execute("""
                SELECT COALESCE(SUM(monto_bs), 0) FROM pagos 
                WHERE estado_pago = 'Conciliado' AND moneda_referencia = 'USD' AND monto_bs > 0 AND fecha_pago <= %s
            """, (fecha_hasta,))
            balances['CAJA_BS_USD'] += cur.fetchone()[0] or Decimal('0.0')

            cur.execute("""
                SELECT COALESCE(SUM(monto_bs), 0) FROM pagos 
                WHERE estado_pago = 'Conciliado' AND moneda_referencia = 'EUR' AND monto_bs > 0 AND fecha_pago <= %s
            """, (fecha_hasta,))
            balances['CAJA_BS_EUR'] += cur.fetchone()[0] or Decimal('0.0')

            # 2. PROCESAR MOVIMIENTOS INTERNOS (INGRESOS Y EGRESOS) HASTA LA FECHA
            cur.execute("""
                SELECT caja_origen, caja_destino, monto_origen, monto_destino FROM operaciones_tesoreria
                WHERE fecha_operacion <= %s
            """, (fecha_fin_timestamp,))
            movimientos = cur.fetchall()

            for mov in movimientos:
                # CORRECCIÓN: Verificar que las cajas no sean None antes de operar
                if mov['caja_origen'] and mov['caja_origen'] in balances:
                    balances[mov['caja_origen']] -= mov['monto_origen']
                
                if mov['caja_destino'] and mov['caja_destino'] in balances:
                    balances[mov['caja_destino']] += mov['monto_destino']

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
                SELECT op.*, admin.usuario as nombre_admin
                FROM operaciones_tesoreria op
                LEFT JOIN administradores admin ON op.realizada_por = admin.id
                ORDER BY op.fecha_operacion DESC LIMIT 30
            """)
            historial_movimientos = cur.fetchall()

    if request.method == 'POST':
        try:
            form = request.form
            tipo_operacion = form.get('tipo_operacion')
            caja_origen = form.get('caja_origen')
            monto_origen_str = form.get('monto_origen', '0').replace(',', '.')
            moneda_origen = form.get('moneda_origen')
            caja_destino = form.get('caja_destino')
            monto_destino_str = form.get('monto_destino', '0').replace(',', '.')
            moneda_destino = form.get('moneda_destino')
            tasa_aplicada_str = form.get('tasa_aplicada', '0').replace(',', '.')
            nota = form.get('nota')
            
            monto_origen = Decimal(monto_origen_str)
            monto_destino = Decimal(monto_destino_str) if monto_destino_str else None
            tasa_aplicada = Decimal(tasa_aplicada_str) if tasa_aplicada_str else None

            if not all([tipo_operacion, caja_origen, monto_origen > 0, moneda_origen, caja_destino, nota]):
                flash("Error: Todos los campos son obligatorios, y el monto debe ser mayor a cero.", 'danger')
                return redirect(url_for('tesoreria_rebalanceo'))

            if balances_actuales.get(caja_origen, Decimal('0.0')) < monto_origen:
                flash(f"Error: Fondos insuficientes en la caja '{caja_origen}'. Saldo actual: {balances_actuales.get(caja_origen, 0):,.2f}", 'danger')
                return redirect(url_for('tesoreria_rebalanceo'))
            
            if caja_destino == 'GASTO_OPERATIVO' and not monto_destino:
                monto_destino = monto_origen
                moneda_destino = moneda_origen

            if not monto_destino:
                 flash("Error: El monto destino es obligatorio para transferencias.", 'danger')
                 return redirect(url_for('tesoreria_rebalanceo'))

            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO operaciones_tesoreria 
                    (tipo_operacion, caja_origen, moneda_origen, monto_origen, caja_destino, moneda_destino, monto_destino, tasa_aplicada, nota, realizada_por, fecha_operacion, perdida_cambiaria)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), 0)
                """, (tipo_operacion, caja_origen, moneda_origen, monto_origen, caja_destino, moneda_destino, monto_destino, tasa_aplicada, nota, g.admin['id']))
            
            descripcion = f"Tesoreria: {tipo_operacion} de {monto_origen:,.2f} {moneda_origen} desde {caja_origen} hacia {caja_destino}."
            registrar_accion_auditoria('MOVIMIENTO_TESORERIA', descripcion)

            conn.commit()
            flash('Movimiento de tesorería registrado exitosamente.', 'success')
        except (InvalidOperation, ValueError):
            flash("Error: Verifique que los montos y tasas sean números válidos.", 'danger')
        except psycopg2.Error as e:
            conn.rollback()
            flash(f"Error de base de datos: {e}", "danger")
        
        return redirect(url_for('tesoreria_rebalanceo'))

    return render_template('tesoreria_rebalanceo.html', 
                           balances=balances_actuales, 
                           historial=historial_movimientos,
                           anio_actual=get_venezuela_current_date().year)

# --- Funciones de utilidad ---
def get_feriados_venezuela(year):
    return [datetime(year, 1, 1).date(), datetime(year, 4, 19).date(), datetime(year, 5, 1).date(), datetime(year, 6, 24).date(), datetime(year, 7, 5).date(), datetime(year, 7, 24).date(), datetime(year, 10, 12).date(), datetime(year, 12, 24).date(), datetime(year, 12, 25).date(), datetime(year, 12, 31).date()]

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
        if vencimiento.year != ano_vencimiento:
            feriados = get_feriados_venezuela(vencimiento.year)
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
                """
                INSERT INTO registros_auditoria (usuario_id, usuario_nombre, accion, descripcion, cliente_afectado_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (g.admin['id'], g.admin['usuario'], accion, descripcion, cliente_id)
            )
        conn.commit()
        logging.info(f"AUDITORIA-REGISTRADA: Usuario '{g.admin['usuario']}' realizó '{accion}'.")
    except Exception as e:
        logging.error(f"AUDITORIA-FALLO-INSERCION: {e}")
        if conn:
            conn.rollback()

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
                SELECT 
                    usuario, 
                    ultimo_login,
                    (estatus_online AND ultimo_visto > NOW() - INTERVAL '5 minutes') AS esta_en_linea
                FROM administradores 
                ORDER BY usuario
            """)
            usuarios = cur.fetchall()
    return render_template('hub.html', anio_actual=get_venezuela_current_date().year, usuarios=usuarios)

# --- MÓDULO DE GESTIÓN ADMINISTRATIVA Y COBRANZA ---
@app.route('/gestion_administrativa')
@admin_required
@rol_requerido('superadmin', 'gerente')
def gestion_administrativa():
    return render_template('gestion_administrativa.html', anio_actual=get_venezuela_current_date().year)

@app.route('/mi_cartera')
@admin_required
def mi_cartera():
    conn = get_db()
    clientes_asignados = []
    if conn and g.admin:
        try:
            with conn.cursor() as cur:
                query = """
                    SELECT id, nombre, apellido, cedula, telefono, proceso
                    FROM clientes
                    WHERE gestor_id = %s
                    ORDER BY nombre, apellido;
                """
                cur.execute(query, (g.admin['id'],))
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
        'ingresos_mes_conciliados': 0,
        'indice_morosidad': 0.0,
        'mes_actual': get_nombre_mes(today.month),
        'anio_actual': today.year,
        'ingresos_ultimos_meses': {'labels': [], 'values': []},
        'composicion_clientes': {'labels': [], 'values': []},
        'total_clientes': 0,
        'clientes_activos': 0,
        'clientes_inactivos': 0,
        'clientes_retirados': 0,
        'clientes_adjudicados': 0
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
    clientes_en_mora = []
    gestores = []
    resumen = {'total_clientes_mora': 0, 'monto_total_mora': 0}

    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, usuario FROM administradores ORDER BY usuario")
                gestores = cur.fetchall()

                first_day_of_month = today.replace(day=1)
                
                subquery_pagaron_mes = "SELECT DISTINCT cliente_id FROM pagos WHERE tipo_pago = 'Cuota' AND estado_pago = 'Conciliado' AND fecha_pago >= %s"
                
                query_morosos = f"""
                    SELECT 
                        c.id, c.nombre, c.apellido, c.cedula, c.telefono, c.valor_cuota, c.gestor_id,
                        a.usuario as gestor_asignado,
                        (SELECT MAX(p.fecha_pago) FROM pagos p WHERE p.cliente_id = c.id AND p.estado_pago = 'Conciliado') as ultimo_pago_fecha
                    FROM clientes c
                    LEFT JOIN administradores a ON c.gestor_id = a.id
                    WHERE TRIM(UPPER(c.proceso)) = 'AHORRADOR' 
                    AND TRIM(UPPER(c.estatus)) = 'ACTIVO'
                    AND c.id NOT IN ({subquery_pagaron_mes})
                    ORDER BY c.nombre, c.apellido;
                """
                
                cur.execute(query_morosos, (first_day_of_month,))
                clientes_en_mora = cur.fetchall()

                if clientes_en_mora:
                    resumen['total_clientes_mora'] = len(clientes_en_mora)
                    resumen['monto_total_mora'] = sum(c['valor_cuota'] for c in clientes_en_mora if c['valor_cuota'])

        except psycopg2.Error as e:
            flash(f"No se pudo generar el reporte de morosidad: {e}", "error")

    return render_template(
        'reporte_morosidad.html', 
        clientes_en_mora=clientes_en_mora,
        gestores=gestores,
        resumen=resumen,
        mes_actual=get_nombre_mes(today.month),
        anio_actual=today.year
    )

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
                    if gestor:
                        nombre_gestor = gestor['usuario']

                cur.execute("UPDATE clientes SET gestor_id = %s WHERE id = %s", (gestor_id_para_db, cliente_id))
                
                descripcion = f"Asignó al cliente {cliente['nombre']} {cliente['apellido']} al gestor '{nombre_gestor}'."
                registrar_accion_auditoria('ASIGNACION_GESTOR', descripcion, cliente_id)
                
                conn.commit()
                flash(f"Cliente asignado al gestor '{nombre_gestor}' exitosamente.", 'success')
        except (psycopg2.Error, ValueError) as e:
            conn.rollback()
            flash(f"Error al asignar el gestor: {e}", "error")
    
    return redirect(url_for('reporte_morosidad'))

# =================================================================================
# ===== RUTA DEDICADA PARA GESTIONAR TASAS BCV (USD Y EUR) =====
# =================================================================================
@app.route('/admin/tasa_bcv', methods=['GET', 'POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def admin_tasa_bcv():
    conn = get_db()
    now_vet = get_venezuela_current_datetime()
    today_date = now_vet.date()
    
    tasas_de_hoy = {'usd': None, 'eur': None}
    historial_tasas = []

    if conn:
        try:
            with conn.cursor() as cur:
                if request.method == 'POST':
                    tasa_usd_str = request.form.get('tasa_usd', '').replace(',', '.')
                    tasa_eur_str = request.form.get('tasa_eur', '').replace(',', '.')
                    
                    tasa_usd = Decimal(tasa_usd_str) if tasa_usd_str else Decimal('0')
                    tasa_eur = Decimal(tasa_eur_str) if tasa_eur_str else Decimal('0')

                    if tasa_usd >= 0 and tasa_eur >= 0:
                        sql_upsert = """
                            INSERT INTO historial_tasas_bcv (fecha, tasa, tasa_euro, establecida_por_id) 
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (fecha) DO UPDATE SET
                                tasa = EXCLUDED.tasa,
                                tasa_euro = EXCLUDED.tasa_euro,
                                establecida_por_id = EXCLUDED.establecida_por_id;
                        """
                        # Guardar para hoy
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
                    tasas_de_hoy['usd'] = resultado['tasa']
                    tasas_de_hoy['eur'] = resultado['tasa_euro']
                
                cur.execute("""
                    SELECT h.fecha, h.tasa, h.tasa_euro, a.usuario 
                    FROM historial_tasas_bcv h
                    LEFT JOIN administradores a ON h.establecida_por_id = a.id
                    ORDER BY h.fecha DESC
                    LIMIT 30
                """)
                historial_tasas = cur.fetchall()
        except InvalidOperation:
            flash('Por favor, introduce un número válido para las tasas.', 'danger')
        except psycopg2.Error as e:
            if conn: conn.rollback()
            flash(f'Error al procesar la solicitud: {e}', 'danger')

    return render_template('admin_tasa_bcv.html', 
                           tasas_de_hoy=tasas_de_hoy, 
                           historial_tasas=historial_tasas,
                           anio_actual=get_venezuela_current_date().year)

# =================================================================================
# ===== LÓGICA DE FLUJO DE CAJA (REFACTORIZADA) =====
# =================================================================================
@app.route('/reportes/flujo_caja', methods=['GET', 'POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def reporte_flujo_caja():
    conn = get_db()
    today = get_venezuela_current_date()
    
    fecha_reporte_str = request.form.get('fecha_reporte') or request.args.get('fecha_reporte') or today.strftime('%Y-%m-%d')
    
    try:
        fecha_reporte_dt = datetime.strptime(fecha_reporte_str, '%Y-%m-%d').date()
    except ValueError:
        flash("Formato de fecha inválido. Usando fecha actual.", "warning")
        fecha_reporte_str = today.strftime('%Y-%m-%d')
        fecha_reporte_dt = today

    # Obtener tasas del día del reporte
    tasas_del_dia = {'usd': Decimal('0.0'), 'eur': Decimal('0.0')}
    if conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tasa, tasa_euro FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (fecha_reporte_dt,))
            resultado_tasa = cur.fetchone()
            if resultado_tasa:
                tasas_del_dia['usd'] = resultado_tasa['tasa'] or Decimal('0.0')
                tasas_del_dia['eur'] = resultado_tasa['tasa_euro'] or Decimal('0.0')

    # Calcular balances de tesorería HASTA la fecha del reporte
    balances = calcular_balances_tesoreria(fecha_hasta=fecha_reporte_dt)
    
    # Preparar el resumen detallado para la plantilla
    resumen = {}
    resumen.update(balances) # Copia los balances base
    
    tasa_usd = tasas_del_dia['usd']
    tasa_eur = tasas_del_dia['eur']

    resumen['balance_bs_usd_usd'] = balances['CAJA_BS_USD'] / tasa_usd if tasa_usd > 0 else Decimal('0.0')
    resumen['balance_bs_eur_eur'] = balances['CAJA_BS_EUR'] / tasa_eur if tasa_eur > 0 else Decimal('0.0')
    
    resumen['balance_bs_consolidado_bs'] = balances['CAJA_BS_TOTAL']
    
    balance_bs_eur_en_usd = balances['CAJA_BS_EUR'] / tasa_usd if tasa_usd > 0 else Decimal('0.0')
    resumen['balance_bs_consolidado_usd'] = resumen['balance_bs_usd_usd'] + balance_bs_eur_en_usd
    
    if resumen['balance_bs_consolidado_usd'] > 0:
        resumen['tasa_ponderada_bs'] = resumen['balance_bs_consolidado_bs'] / resumen['balance_bs_consolidado_usd']
    else:
        resumen['tasa_ponderada_bs'] = Decimal('0.0')
        
    resumen['balance_general_consolidado_usd'] = balances['EFECTIVO_USD'] + balances['BINANCE_USDT'] + resumen['balance_bs_consolidado_usd']

    # --- INICIO CÁLCULO DE PÉRDIDAS ---
    resumen['acumulado_perdida_devaluacion'] = Decimal('0.0')
    resumen['acumulado_perdida_conversion'] = Decimal('0.0')

    if conn and tasa_usd > 0:
        try:
            with conn.cursor() as cur:
                fecha_fin_timestamp = datetime.combine(fecha_reporte_dt, datetime.max.time())

                # 1. Valor histórico en USD de todos los ingresos en Bs (Ref. USD)
                cur.execute("""
                    SELECT COALESCE(SUM(CASE WHEN tasa_dia > 0 THEN monto_bs / tasa_dia ELSE 0 END), 0)
                    FROM pagos
                    WHERE estado_pago = 'Conciliado' AND monto_bs > 0 AND moneda_referencia = 'USD' AND fecha_pago <= %s
                """, (fecha_reporte_dt,))
                valor_historico_ingresos_bs_usd = cur.fetchone()[0] or Decimal('0.0')

                # 2. Valor histórico en USD de todos los egresos de la caja Bs (Ref. USD)
                cur.execute("""
                    SELECT COALESCE(SUM(CASE WHEN tasa_aplicada > 0 THEN monto_origen / tasa_aplicada ELSE 0 END), 0)
                    FROM operaciones_tesoreria
                    WHERE caja_origen = 'CAJA_BS_USD' AND fecha_operacion <= %s
                """, (fecha_fin_timestamp,))
                valor_historico_egresos_bs_usd = cur.fetchone()[0] or Decimal('0.0')
                
                # 3. Saldo teórico en USD si no hubiera habido devaluación
                saldo_teorico_bs_usd_en_usd = valor_historico_ingresos_bs_usd - valor_historico_egresos_bs_usd
                
                # 4. Pérdida por devaluación es la diferencia entre el valor teórico y el valor real actual
                resumen['acumulado_perdida_devaluacion'] = saldo_teorico_bs_usd_en_usd - resumen['balance_bs_usd_usd']

                # 5. Pérdida por conversión (spread cambiario)
                cur.execute("""
                    SELECT COALESCE(SUM(perdida_cambiaria), 0) 
                    FROM operaciones_tesoreria 
                    WHERE fecha_operacion <= %s
                """, (fecha_fin_timestamp,))
                resumen['acumulado_perdida_conversion'] = cur.fetchone()[0] or Decimal('0.0')
                
        except psycopg2.Error as e:
            flash(f"Error calculando las pérdidas financieras: {e}", "warning")
    # --- FIN CÁLCULO DE PÉRDIDAS ---

    # Obtener el historial de movimientos de tesorería PARA la fecha del reporte
    historial_movimientos = []
    if conn:
        try:
            with conn.cursor() as cur:
                fecha_inicio_periodo = datetime.combine(fecha_reporte_dt, datetime.min.time())
                fecha_fin_periodo = datetime.combine(fecha_reporte_dt, datetime.max.time())
                
                cur.execute("""
                    SELECT op.*, admin.usuario as nombre_admin
                    FROM operaciones_tesoreria op
                    LEFT JOIN administradores admin ON op.realizada_por = admin.id
                    WHERE op.fecha_operacion BETWEEN %s AND %s
                    ORDER BY op.fecha_operacion DESC
                """, (fecha_inicio_periodo, fecha_fin_periodo))
                historial_movimientos = cur.fetchall()
        except (psycopg2.Error, ValueError) as e:
            flash(f"Error al obtener historial de movimientos: {e}", "error")

    return render_template(
        'reporte_flujo_caja.html',
        fecha_reporte=fecha_reporte_str,
        resumen=resumen,
        tasas_del_dia=tasas_del_dia,
        historial=historial_movimientos,
        anio_actual=today.year
    )
    
# --- GESTIÓN DE CLIENTES Y PAGOS ---

@app.route('/cliente/<int:cliente_id>')
@admin_required
def perfil_cliente(cliente_id):
    conn = get_db()
    cliente = None
    pagos = []
    gestiones = []

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
                    SELECT g.nota, g.fecha_creacion, a.usuario as gestor_nombre
                    FROM gestiones_cobranza g
                    JOIN administradores a ON g.gestor_id = a.id
                    WHERE g.cliente_id = %s
                    ORDER BY g.fecha_creacion DESC;
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
                cur.execute(
                    "INSERT INTO gestiones_cobranza (cliente_id, gestor_id, nota) VALUES (%s, %s, %s)",
                    (cliente_id, g.admin['id'], nota.strip())
                )
                
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
        if form_data.get('inscripcion_monto'):
            form_data['inscripcion_monto'] = Decimal(form_data['inscripcion_monto'].replace(',', '.'))
        else:
            form_data['inscripcion_monto'] = Decimal('0.00')

        if form_data.get('valor_cuota'):
            form_data['valor_cuota'] = Decimal(form_data['valor_cuota'].replace(',', '.'))
        else:
            form_data['valor_cuota'] = Decimal('0.00')

        if form_data.get('cuotas_totales'):
            form_data['cuotas_totales'] = int(form_data['cuotas_totales'])
        else:
            form_data['cuotas_totales'] = 0
            
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

    try:
        form_data = {k: v.strip() if isinstance(v, str) else v for k, v in request.form.items()}
        
        firma_cliente = form_data.get('firma_cliente')
        firma_empresa = form_data.get('firma_empresa')
        if not firma_cliente or not firma_empresa:
            flash('Ambas firmas son obligatorias para registrar al cliente.', 'error')
            return redirect(url_for('registrar'))

        try:
            if form_data.get('inscripcion_monto'):
                form_data['inscripcion_monto'] = Decimal(form_data['inscripcion_monto'].replace(',', '.'))
            else:
                form_data['inscripcion_monto'] = Decimal('0.00')

            if form_data.get('valor_cuota'):
                form_data['valor_cuota'] = Decimal(form_data['valor_cuota'].replace(',', '.'))
            else:
                form_data['valor_cuota'] = Decimal('0.00')
                
            if form_data.get('cuotas_totales'):
                form_data['cuotas_totales'] = int(form_data['cuotas_totales']) if form_data['cuotas_totales'] else None
            else:
                 form_data['cuotas_totales'] = None

        except (InvalidOperation, ValueError):
            flash('Los valores numéricos del contrato (inscripción, cuota) no son válidos.', 'error')
            return redirect(url_for('registrar'))

        with conn.cursor() as cur:
            nombre_completo = form_data.get('nombre_apellido').split(' ', 1)
            nombre = nombre_completo[0]
            apellido = nombre_completo[1] if len(nombre_completo) > 1 else ''
            
            insert_dict = {
                'nombre': nombre, 
                'apellido': apellido, 
                'cedula': form_data.get('cedula').replace(' ', ''),
                'cuotas_pagadas_progresivas': 0,
                'cuotas_pagadas_regresivas': 0,
                'firma_digital': firma_cliente,
                'firma_empresa': firma_empresa,
                'fecha_firma': datetime.now(VENEZUELA_TZ)
            }
            
            optional_fields = [
                'contrato_nro', 'telefono', 'asesor', 'responsable', 'fecha_ingreso', 
                'grupo', 'plan_contratado', 'cuotas_totales', 'moneda_pago', 
                'valor_cuota', 'inscripcion_monto', 'proceso', 'ciclo_cobranza',
                'foto_cliente', 'foto_cedula',
                'direccion', 'email', 'beneficiario_nombre', 
                'beneficiario_cedula', 'beneficiario_telefono'
            ]
            
            for field in optional_fields:
                if form_data.get(field):
                    insert_dict[field] = form_data[field]
            
            columns = list(insert_dict.keys())
            values = [insert_dict[col] for col in columns]
            
            query = f"INSERT INTO clientes ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(values))}) RETURNING id"
            cur.execute(query, values)
            new_client_id = cur.fetchone()[0]
            
            descripcion_audit = f"Registró y firmó contrato para nuevo cliente: {form_data.get('nombre_apellido')} (C.I. {form_data.get('cedula')})."
            registrar_accion_auditoria('REGISTRO_CLIENTE_FIRMADO', descripcion_audit, new_client_id)
            
            conn.commit()
            flash(f"¡Cliente '{form_data.get('nombre_apellido')}' registrado y contrato guardado exitosamente!", 'success')
            return redirect(url_for('consulta', busqueda=form_data.get('cedula')))

    except psycopg2.IntegrityError:
        conn.rollback()
        flash(f"Registro fallido: La cédula '{form_data.get('cedula')}' ya existe.", 'error')
    except (psycopg2.Error, ValueError, ConnectionError) as e:
        conn.rollback()
        flash(f"Registro fallido: Ocurrió un error de base de datos: {e}", 'error')
        
    return redirect(url_for('registrar'))

@app.route('/generar_contrato/<int:client_id>')
def generar_contrato(client_id):
    is_admin = 'admin_id' in session
    is_correct_client = 'cliente_id' in session and session['cliente_id'] == client_id

    if not is_admin and not is_correct_client:
        flash('Acceso no autorizado.', 'error')
        if 'cliente_id' in session:
            return redirect(url_for('portal_dashboard'))
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

            cur.execute(
                "UPDATE clientes SET firma_digital = %s, fecha_firma = %s WHERE id = %s",
                (firma_cliente, fecha_firma_vet, client_id)
            )
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
            cur.execute(
                "UPDATE clientes SET firma_empresa = %s WHERE id = %s",
                (firma_empresa, client_id)
            )
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
        
        moneda_referencia = None
        pago_en_valor = pago_form.get('pago_en')

        if pago_en_valor == 'Dolar/BCV':
            moneda_referencia = 'USD'
        elif pago_en_valor == 'Euro/BCV':
            moneda_referencia = 'EUR'
        
        try:
            with conn.cursor() as cur:
                pago_query = """
                    INSERT INTO pagos (cliente_id, monto, tipo_pago, forma_pago, fecha_pago, 
                                        pago_en, por_concepto_de, referencia, banco, lugar_emision,
                                        tasa_dia, monto_bs, estado_pago, cuotas_cubiertas, moneda_referencia)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Pendiente', 0, %s);
                """
                cur.execute(pago_query, (
                    client_id, pago_form['monto'], tipo_pago, pago_form['forma_pago'], 
                    pago_form['fecha_pago'], pago_form.get('pago_en'), pago_form.get('por_concepto_de'), 
                    pago_form.get('referencia'), pago_form.get('banco'), pago_form.get('lugar_emision'), 
                    pago_form.get('tasa_dia'), pago_form.get('monto_bs'), moneda_referencia
                ))
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
            pago = cur.fetchone()
            if not pago or pago['estado_pago'] != 'Pendiente':
                flash("El pago no se puede conciliar (ya está conciliado, anulado o no existe).", 'error')
                return redirect(url_for('consulta', busqueda=cedula_cliente_fallback))
            cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s FOR UPDATE", (pago['cliente_id'],))
            cliente = cur.fetchone()
            if not cliente:
                flash("Error: No se encontró el cliente asociado a este pago.", 'error')
                conn.rollback()
                return redirect(url_for('consulta', busqueda=cedula_cliente_fallback))

            monto_pagado, pago_final_id, flash_msg = Decimal(pago['monto']), None, ""
            if pago['tipo_pago'] == 'Inscripción':
                inscripcion_pagada_actual = Decimal(cliente.get('inscripcion_pagada') or 0)
                inscripcion_total = Decimal(cliente.get('inscripcion_monto') or 0)
                nueva_inscripcion_pagada = inscripcion_pagada_actual + monto_pagado
                if inscripcion_total > 0 and nueva_inscripcion_pagada >= inscripcion_total:
                    if cliente['proceso'] == 'RESERVA':
                        cur.execute("UPDATE clientes SET proceso = 'INSCRITO' WHERE id = %s", (cliente['id'],))
                    cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE cliente_id = %s AND tipo_pago = 'Inscripción' AND estado_pago = 'Conciliado'", (cliente['id'],))
                    cur.execute("INSERT INTO pagos (cliente_id, monto, tipo_pago, forma_pago, fecha_pago, por_concepto_de, estado_pago, cuotas_cubiertas, lugar_emision) VALUES (%s, %s, 'Inscripción Finalizada', %s, %s, %s, 'Conciliado', 0, %s) RETURNING id;", (cliente['id'], inscripcion_total, pago['forma_pago'], pago['fecha_pago'], 'Pago total de inscripción', pago['lugar_emision']))
                    pago_final_id = cur.fetchone()[0]
                    cur.execute("UPDATE clientes SET inscripcion_pagada = %s WHERE id = %s", (inscripcion_total, cliente['id']))
                    cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE id = %s", (pago_id,))
                    descripcion_audit = f"Concilió pago final de inscripción (N°{pago_id}) por ${monto_pagado} para {cliente['nombre_apellido']}."
                    registrar_accion_auditoria('CONCILIACION_INSCRIPCION', descripcion_audit, cliente['id'])
                    flash_msg = "¡Inscripción completada! Se generó el recibo final."
                else:
                    cur.execute("UPDATE clientes SET inscripcion_pagada = %s WHERE id = %s", (nueva_inscripcion_pagada, cliente['id']))
                    cur.execute("UPDATE pagos SET estado_pago = 'Conciliado' WHERE id = %s", (pago_id,))
                    descripcion_audit = f"Concilió abono de inscripción N° {pago_id} por ${monto_pagado} para {cliente['nombre_apellido']}."
                    registrar_accion_auditoria('CONCILIACION_INSCRIPCION', descripcion_audit, cliente['id'])
                    flash_msg = f"Abono de inscripción N° {pago_id} conciliado."
            elif pago['tipo_pago'] == 'Cuota':
                puntualidad, fecha_vencimiento = 'Puntual', get_fecha_vencimiento_ajustada(pago['fecha_pago'])
                if pago['fecha_pago'] > fecha_vencimiento:
                    puntualidad = 'Impuntual'
                    cur.execute("UPDATE clientes SET meses_retraso_entrega = meses_retraso_entrega + 1 WHERE id = %s;", (cliente['id'],))
                if cliente['proceso'] == 'INSCRITO':
                    cur.execute("UPDATE clientes SET proceso = 'Ahorrador' WHERE id = %s", (cliente['id'],))
                valor_cuota = Decimal(cliente.get('valor_cuota') or 0)
                if valor_cuota <= 0: raise ValueError('El cliente no tiene un valor de cuota válido.')
                cpp, cpr, br = cliente.get('cuotas_pagadas_progresivas', 0), cliente.get('cuotas_pagadas_regresivas', 0), Decimal(cliente.get('balance_regresivo', 0))
                mtd, pph, rph = monto_pagado + br, 0, 0
                if mtd >= valor_cuota: pph, mtd = 1, mtd - valor_cuota
                bp = mtd
                while bp >= valor_cuota: rph, bp = rph + 1, bp - valor_cuota
                nbf, ncpp, ncpr, cch = bp, cpp + pph, cpr + rph, pph + rph
                cur.execute("UPDATE clientes SET cuotas_pagadas_progresivas = %s, cuotas_pagadas_regresivas = %s, balance_regresivo = %s WHERE id = %s;", (ncpp, ncpr, nbf, cliente['id']))
                cur.execute("UPDATE pagos SET estado_pago = 'Conciliado', puntualidad = %s, cuotas_cubiertas = %s, progresivas_cubiertas = %s, regresivas_cubiertas = %s, cuotas_progresivas_al_pagar = %s, cuotas_regresivas_al_pagar = %s, balance_al_pagar = %s WHERE id = %s;", (puntualidad, cch, pph, rph, ncpp, ncpr, nbf, pago_id))
                descripcion_audit = f"Concilió pago de cuota N° {pago_id} por ${monto_pagado} como '{puntualidad}' para {cliente['nombre_apellido']}."
                registrar_accion_auditoria('CONCILIACION_CUOTA', descripcion_audit, cliente['id'])
                flash_msg = f"¡Pago de cuota N° {pago_id} conciliado como '{puntualidad}'!"
            
            conn.commit()
            flash(flash_msg, 'success')
            if pago_final_id: return redirect(url_for('ver_recibo_inscripcion', pago_id=pago_final_id))
            return redirect(url_for('ver_recibo', pago_id=pago_id))
    except (psycopg2.Error, ValueError, TypeError, ConnectionError) as e:
        if conn: conn.rollback()
        flash(f'Ocurrió un error al conciliar el pago: {e}', 'error')
    return redirect(url_for('consulta', busqueda=cedula_cliente_fallback))

@app.route('/recibo/<int:pago_id>')
def ver_recibo(pago_id):
    conn = get_db()
    if not conn:
        if 'cliente_id' in session: return redirect(url_for('portal_login'))
        return redirect(url_for('consulta'))
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
        if 'cliente_id' in session: return redirect(url_for('portal_dashboard'))
        return redirect(url_for('consulta'))
    return render_template('recibo.html', pago=pago, is_admin_view='admin_id' in session)

@app.route('/recibo_inscripcion/<int:pago_id>')
def ver_recibo_inscripcion(pago_id):
    conn = get_db()
    if not conn:
        if 'cliente_id' in session: return redirect(url_for('portal_login'))
        return redirect(url_for('consulta'))
    with conn.cursor() as cur:
        cur.execute("SELECT p.*, (c.nombre || ' ' || c.apellido) as nombre_apellido, c.cedula, c.plan_contratado FROM pagos p JOIN clientes c ON p.cliente_id = c.id WHERE p.id = %s AND p.tipo_pago = 'Inscripción Finalizada';", (pago_id,))
        pago = cur.fetchone()
    if not pago:
        flash('Recibo de inscripción final no encontrado.', 'error')
        if 'cliente_id' in session: return redirect(url_for('portal_dashboard'))
        return redirect(url_for('consulta'))
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
    if not conn:
        return "Error de conexión a la base de datos.", 500
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
                form_data['nombre'] = nombre_completo[0]
                form_data['apellido'] = nombre_completo[1] if len(nombre_completo) > 1 else ''
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

            descripcion_audit = f"Eliminó al cliente {cliente_a_borrar['nombre']} {cliente_a_borrar['apellido']} (C.I. {cliente_a_borrar['cedula']})."
            
            registrar_accion_auditoria('ELIMINACION_CLIENTE', descripcion_audit, client_id)
            
            cur.execute("DELETE FROM clientes WHERE id = %s", (client_id,))
            
            conn.commit()
            flash('¡Cliente y sus registros asociados han sido eliminados exitosamente!', 'success')
    except (psycopg2.Error, ConnectionError) as e:
        conn.rollback()
        flash(f'Ocurrió un error al eliminar: {e}', 'error')
    return redirect(url_for('consulta'))

@app.route('/registrar_oferta/<int:client_id>', methods=['GET'])
@admin_required
def registrar_oferta(client_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta'))
    with conn.cursor() as cur:
        cur.execute("SELECT id, (nombre || ' ' || apellido) as nombre_apellido, cedula FROM clientes WHERE id = %s", (client_id,))
        cliente = cur.fetchone()
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta'))
    return render_template('registrar_oferta.html', cliente=cliente)

@app.route('/guardar_oferta/<int:client_id>', methods=['POST'])
@admin_required
def guardar_oferta(client_id):
    conn = get_db()
    cuotas_ofertadas = request.form.get('cuotas_ofertadas')
    cedula_cliente = ''
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta'))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cedula, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s", (client_id,))
            cliente_info = cur.fetchone()
            if cliente_info:
                cedula_cliente = cliente_info['cedula']
                nombre_cliente = cliente_info['nombre_apellido']

            hoy = get_venezuela_current_date()
            inicio_mes = hoy.replace(day=1)
            
            cur.execute("SELECT 1 FROM pagos WHERE cliente_id = %s AND tipo_pago = 'Cuota' AND puntualidad = 'Impuntual' AND fecha_pago >= %s", (client_id, inicio_mes))
            if cur.fetchone():
                flash("No se puede registrar la oferta: El cliente tiene un pago impuntual registrado en el mes actual.", 'error')
                return redirect(url_for('consulta', busqueda=cedula_cliente))

            if not cuotas_ofertadas or not cuotas_ofertadas.isdigit() or int(cuotas_ofertadas) <= 0:
                flash("Debe ingresar un número válido de cuotas para la oferta.", 'error')
                return redirect(url_for('registrar_oferta', client_id=client_id))

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
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return render_template('adjudicacion.html', clientes_elegibles_ahorro=[], ofertas_activas=[], historial=[])
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
            cur.execute("UPDATE ofertas SET estado_oferta = 'perdida' WHERE estado_oferta = 'activa';")
            
            ganador_oferta_id = ganador_oferta['id'] if ganador_oferta else None
            
            cur.execute("INSERT INTO adjudicaciones (ganador_oferta_id, ganador_sorteo_id) VALUES (%s, %s);", (ganador_oferta_id, None))
            
            nombres_ganadores = [g['nombre_apellido'] for g in ganadores_ahorro]
            if ganador_oferta:
                nombres_ganadores.append(ganador_oferta['nombre_apellido'])
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
    conn = get_db()
    logs = []
    
    fecha_actual_vet = get_venezuela_current_date().strftime('%Y-%m-%d')
    fecha_filtro_str = request.args.get('fecha', fecha_actual_vet)
    
    fecha_para_sql = fecha_filtro_str
    try:
        fecha_obj = datetime.strptime(fecha_filtro_str, '%m/%d/%Y')
        fecha_para_sql = fecha_obj.strftime('%Y-%m-%d')
    except ValueError:
        pass
    
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return render_template('auditoria.html', logs=logs, anio_actual=get_venezuela_current_date().year, fecha_filtro=fecha_filtro_str)
    
    try:
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'America/Caracas';")

            sql = """
                SELECT r.id, r.usuario_nombre, r.accion, r.descripcion, r.timestamp AS fecha_registro, 
                       c.nombre, c.apellido, c.cedula
                FROM registros_auditoria r
                LEFT JOIN clientes c ON r.cliente_afectado_id = c.id
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
        fecha_obj = datetime.strptime(fecha_reporte_str, '%m/%d/%Y')
        fecha_para_sql = fecha_obj.strftime('%Y-%m-%d')
    except ValueError:
        pass

    conn = get_db()
    if not conn:
        return "Error de conexión a la base de datos", 500
    try:
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'America/Caracas';")
            
            sql = """
                SELECT r.timestamp AS fecha_registro, r.usuario_nombre, r.accion, r.descripcion, 
                       (c.nombre || ' ' || c.apellido) as cliente_nombre, c.cedula
                FROM registros_auditoria r
                LEFT JOIN clientes c ON r.cliente_afectado_id = c.id
                WHERE r.timestamp >= %s::date AND r.timestamp < (%s::date + '1 day'::interval)
                ORDER BY r.timestamp ASC;
            """
            cur.execute(sql, (fecha_para_sql, fecha_para_sql))
            logs = cur.fetchall()

            output = io.StringIO()
            writer = csv.writer(output)
            
            writer.writerow(['Fecha y Hora (VET)', 'Usuario Admin', 'Accion', 'Descripcion', 'Cliente Afectado', 'Cedula Cliente'])
            
            for log in logs:
                writer.writerow([
                    log['fecha_registro'].strftime('%Y-%m-%d %H:%M:%S'),
                    log['usuario_nombre'],
                    log['accion'],
                    log['descripcion'],
                    log['cliente_nombre'] or 'N/A',
                    log['cedula'] or 'N/A'
                ])
            
            output.seek(0)
            
            return Response(
                output,
                mimetype="text/csv",
                headers={"Content-Disposition": f"attachment;filename=reporte_auditoria_{fecha_reporte_str}.csv"}
            )
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
        cedula = request.form.get('cedula', '').strip().replace('V-', '').replace('v-', '')
        contrato_nro = request.form.get('contrato_nro', '').strip().upper().replace('MP-', '')
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
                session['cliente_id'] = cliente['id']
                session['cliente_nombre'] = cliente['nombre_apellido']
                return redirect(url_for('portal_dashboard'))
            else:
                flash('Credenciales incorrectas. Verifique sus datos e intente de nuevo.', 'error')
        except psycopg2.Error as e:
            flash(f'Error de base de datos: {e}', 'error')
    return render_template('portal_login.html', anio_actual=get_venezuela_current_date().year)

@app.route('/portal/dashboard')
def portal_dashboard():
    if 'cliente_id' not in session:
        flash('Debe iniciar sesión para acceder a su portal.', 'error')
        return redirect(url_for('portal_login'))
    conn = get_db()
    if not conn:
        flash('No se pudo conectar con la base de datos.', 'error')
        return redirect(url_for('portal_dashboard'))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s;", (session['cliente_id'],))
            cliente = cur.fetchone()
            if not cliente:
                session.clear()
                flash('No se encontró su información de cliente.', 'error')
                return redirect(url_for('portal_login'))

            cliente_dict = dict(cliente)
            cur.execute("SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha_pago DESC, id DESC;", (session['cliente_id'],))
            pagos = cur.fetchall()
            cliente_dict['pagos'] = pagos

            hoy = get_venezuela_current_date()
            dia_de_vencimiento = 3
            pago_del_mes_realizado = any(pago['tipo_pago'] == 'Cuota' and pago['fecha_pago'].year == hoy.year and pago['fecha_pago'].month == hoy.month for pago in pagos)
            
            estado_cuota = {}
            if pago_del_mes_realizado:
                estado_cuota = {'estado': 'Pagada', 'mes': get_nombre_mes(hoy.month), 'mensaje': 'Tu cuota de este mes ya fue procesada.'}
            elif hoy.day <= dia_de_vencimiento:
                estado_cuota = {'estado': 'Vigente', 'mes': get_nombre_mes(hoy.month), 'fecha_vencimiento': f"{dia_de_vencimiento:02d}/{hoy.month:02d}/{hoy.year}"}
            else:
                estado_cuota = {'estado': 'En Mora', 'mes': get_nombre_mes(hoy.month), 'fecha_vencimiento': f"{dia_de_vencimiento:02d}/{hoy.month:02d}/{hoy.year}"}
            
            return render_template('portal_dashboard.html', cliente=cliente_dict, cuota_status=estado_cuota)
    except psycopg2.Error as e:
        flash(f'Ocurrió un error al cargar su información: {e}', 'error')
        return redirect(url_for('portal_login'))

@app.route('/portal/reportar_pago', methods=['GET', 'POST'])
def portal_reportar_pago():
    if 'cliente_id' not in session:
        flash('Debe iniciar sesión para acceder a su portal.', 'error')
        return redirect(url_for('portal_login'))
    conn = get_db()
    if not conn:
        flash('No se pudo conectar con la base de datos.', 'error')
        return redirect(url_for('portal_dashboard'))
    with conn.cursor() as cur:
        cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s;", (session['cliente_id'],))
        cliente = cur.fetchone()
    if not cliente:
        session.clear()
        flash('No se encontró su información de cliente.', 'error')
        return redirect(url_for('portal_login'))

    mes_actual = get_nombre_mes(get_venezuela_current_date().month)
    if request.method == 'POST':
        pago_form = {k: v if v else None for k, v in request.form.items()}
        if not all(pago_form.get(key) for key in ['monto', 'fecha_pago', 'forma_pago']):
            flash('Error: Monto, fecha y forma de pago son campos obligatorios.', 'error')
            return render_template('portal_reportar_pago.html', cliente=cliente, mes_actual=mes_actual)
        if pago_form.get('forma_pago') != 'Efectivo' and not pago_form.get('referencia'):
            flash('Error: La referencia es obligatoria para pagos que no son en efectivo.', 'error')
            return render_template('portal_reportar_pago.html', cliente=cliente, mes_actual=mes_actual)
        try:
            with conn.cursor() as cur:
                pago_query = """
                    INSERT INTO pagos (
                        cliente_id, monto, tipo_pago, forma_pago, fecha_pago, pago_en, 
                        por_concepto_de, referencia, banco, lugar_emision, tasa_dia, monto_bs, 
                        estado_pago, cuotas_cubiertas, reportado_por_cliente, estado_reporte
                    ) VALUES (%s, %s, 'Cuota', %s, %s, %s, %s, %s, %s, 'Acarigua', %s, %s, 'Pendiente', 0, TRUE, 'Pendiente de Revision');
                """
                cur.execute(pago_query, (
                    session['cliente_id'], pago_form['monto'], pago_form['forma_pago'], pago_form['fecha_pago'], 
                    pago_form.get('pago_en'), pago_form.get('por_concepto_de'), pago_form.get('referencia'), 
                    pago_form.get('banco'), pago_form.get('tasa_dia'), pago_form.get('monto_bs')
                ))
                conn.commit()
                flash('¡Pago reportado exitosamente! Será verificado a la brevedad.', 'success')
                return redirect(url_for('portal_dashboard'))
        except (psycopg2.Error, ValueError, TypeError) as e:
            conn.rollback()
            flash(f'Ocurrió un error al reportar el pago: {e}', 'error')
    return render_template('portal_reportar_pago.html', cliente=cliente, mes_actual=mes_actual)

@app.route('/portal/estado_cuenta')
def portal_estado_cuenta():
    if 'cliente_id' not in session:
        flash('Debe iniciar sesión para acceder a su portal.', 'error')
        return redirect(url_for('portal_login'))
    conn = get_db()
    if not conn:
        flash('No se pudo conectar con la base de datos.', 'error')
        return redirect(url_for('portal_dashboard'))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s;", (session['cliente_id'],))
            cliente = cur.fetchone()
            if not cliente:
                session.clear()
                flash('No se encontró su información de cliente.', 'error')
                return redirect(url_for('portal_login'))
            cur.execute("SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha_pago ASC, id ASC;", (session['cliente_id'],))
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

# --- INICIO DE CAMBIO v4: Nuevas rutas para el flujo de verificación ---
@app.route('/ver_reporte/<int:pago_id>')
@admin_required
def ver_reporte(pago_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta'))
    
    with conn.cursor() as cur:
        query = """
            SELECT p.*, (c.nombre || ' ' || c.apellido) as nombre_apellido, c.cedula
            FROM pagos p JOIN clientes c ON p.cliente_id = c.id
            WHERE p.id = %s;
        """
        cur.execute(query, (pago_id,))
        pago = cur.fetchone()

    if not pago:
        flash('El reporte de pago no fue encontrado.', 'error')
        return redirect(url_for('consulta'))

    return render_template('ver_reporte.html', pago=pago)

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

    nuevo_estado = ''
    if accion == 'aprobar':
        nuevo_estado = 'Aprobado'
    elif accion == 'rechazar':
        nuevo_estado = 'Inconsistente'
    else:
        flash('Acción no válida.', 'error')
        return redirect(url_for('consulta', busqueda=cedula_cliente))

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pagos 
                SET estado_reporte = %s, revisado_por_id = %s, fecha_revision = NOW()
                WHERE id = %s
                """,
                (nuevo_estado, g.admin['id'], pago_id)
            )
            
            cur.execute("SELECT cliente_id FROM pagos WHERE id = %s", (pago_id,))
            cliente_id = cur.fetchone()['cliente_id']

            descripcion_audit = f"Revisó el reporte de pago N° {pago_id} y lo marcó como '{nuevo_estado}'."
            registrar_accion_auditoria('REVISION_REPORTE_PAGO', descripcion_audit, cliente_id)
            
            conn.commit()
            flash(f"El reporte de pago ha sido marcado como '{nuevo_estado}' exitosamente.", 'success')
    except (psycopg2.Error, ValueError) as e:
        conn.rollback()
        flash(f"Error al procesar el reporte: {e}", "error")
    
    return redirect(url_for('consulta', busqueda=cedula_cliente))
# --- FIN DE CAMBIO v4 ---

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
