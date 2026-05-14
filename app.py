# -*- coding: utf-8 -*-
import os
import io
from io import BytesIO
import csv
import json
import base64
import logging
import random
import re
import unicodedata
import traceback
from calendar import monthrange
from collections import defaultdict
from datetime import datetime, timedelta, date, time
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from functools import wraps
from types import SimpleNamespace

# >>> NUEVO: import local de la función de proyección
from proyeccion import calcular_proyeccion

# Terceros
import boto3
import pandas as pd
import psycopg2
import psycopg2.extras
from psycopg2.extras import execute_values
from botocore.exceptions import NoCredentialsError
from dotenv import load_dotenv
from fpdf import FPDF
import pytz
from werkzeug.security import check_password_hash, generate_password_hash

# Flask
from flask import (
    Flask, render_template, request, g, flash, redirect,
    url_for, session, Response, jsonify, send_file, abort
)

# Si realmente lo usas; si no, coméntalo
from flask_login import current_user

def _normalize_estatus_scalar(x):
    if x is None or (pd is not None and pd.isna(x)):
        return ''
    s = str(x).strip()
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r'\s+', ' ', s).upper()

    aliases = {
        'ENTREGA PENDIENTE': 'PENDIENTE POR ENTREGA',
        'PENDIENTE DE ENTREGA': 'PENDIENTE POR ENTREGA',
        'PENDIENTE ENTREGA': 'PENDIENTE POR ENTREGA',
        'PENDIEN POR ENTREGA': 'PENDIENTE POR ENTREGA',  # abreviación
    }
    if s in aliases:
        return aliases[s]
    if ('PENDIENT' in s) and ('ENTREG' in s):
        return 'PENDIENTE POR ENTREGA'
    return s

def normalize_estatus_cliente(val):
    # Acepta escalar o pandas Series (evita 'Series' object has no attribute 'upper')
    if isinstance(val, pd.Series):
        return val.astype(object).map(_normalize_estatus_scalar)
    return _normalize_estatus_scalar(val)

def normalize_estatus_in_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normaliza la columna de estatus en un DataFrame si existe."""
    for col in ('estatus_cliente', 'estatus'):
        if col in df.columns:
            df[col] = normalize_estatus_cliente(df[col])
            # si la columna era 'estatus', renómbrala al nombre oficial
            if col == 'estatus' and 'estatus_cliente' not in df.columns:
                df.rename(columns={'estatus': 'estatus_cliente'}, inplace=True)
            break
    return df

# =================================================================================
# ===== CONFIGURACIÓN INICIAL Y DE ENTORNO =====
# =================================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'una-clave-secreta-por-defecto-para-desarrollo')

VENEZUELA_TZ = pytz.timezone('America/Caracas')

# ========= 2) Cache de memoria (reglas de comisiones) =========
# Estructura:
#   - claves de escenarios: 'asesor', 'gerente', 'superadmin'
#   - cada escenario: mapa de ROL->porcentaje_sobre_plan (Decimal o float)
#   - extra_cerrador_pct: porcentaje adicional para el cerrador cuando NO es el asesor responsable
DEFAULT_REGLAS_COMISIONES = {
    "asesor": {
        "ASESOR":        2.0,
        "PRESIDENCIA_A": 3.0,
        "PRESIDENCIA_B": 3.0,
        "GERENCIA":      1.0
    },
    "gerente": {
        "PRESIDENCIA_A": 5.5,
        "PRESIDENCIA_B": 5.5,
        "GERENCIA":      1.0,
        "ASESOR":        0.0
    },
    "superadmin": {
        "PRESIDENCIA_A": 5.5,
        "PRESIDENCIA_B": 5.5,
        "GERENCIA":      0.5,
        "ASESOR":        0.0
    },
    "extra_cerrador_pct": 0.4
}

# Cache en memoria del proceso
REGLAS_COMISIONES_CACHE = json.loads(json.dumps(DEFAULT_REGLAS_COMISIONES))

def _copiar_reglas(r):
    """Devuelve una copia 'segura' (sin referencias) de las reglas."""
    return json.loads(json.dumps(r))

def validar_reglas(reglas: dict):
    """Valida forma y valores de las reglas (devuelve (ok, msg_error))."""
    try:
        if not isinstance(reglas, dict):
            return False, "El payload debe ser un objeto JSON."
        if "extra_cerrador_pct" not in reglas:
            return False, "Falta 'extra_cerrador_pct'."
        if not isinstance(reglas["extra_cerrador_pct"], (int, float)) or reglas["extra_cerrador_pct"] < 0:
            return False, "'extra_cerrador_pct' debe ser numérico >= 0."

        for esc in ("asesor", "gerente", "superadmin"):
            if esc not in reglas or not isinstance(reglas[esc], dict):
                return False, f"Falta el escenario '{esc}'."
            for rol, pct in reglas[esc].items():
                if rol not in ("ASESOR", "GERENCIA", "PRESIDENCIA_A", "PRESIDENCIA_B"):
                    return False, f"Rol inválido en {esc}: {rol}"
                if not isinstance(pct, (int, float)) or pct < 0:
                    return False, f"Porcentaje inválido en {esc}.{rol}."
        return True, ""
    except Exception as e:
        return False, f"Error validando reglas: {e}"

def get_reglas_comisiones() -> dict:
    """Lee reglas actuales (copia)."""
    return _copiar_reglas(REGLAS_COMISIONES_CACHE)

def set_reglas_comisiones(nuevas: dict):
    """Sobrescribe reglas (valida antes)."""
    ok, msg = validar_reglas(nuevas)
    if not ok:
        raise ValueError(msg)
    REGLAS_COMISIONES_CACHE.clear()
    REGLAS_COMISIONES_CACHE.update(_copiar_reglas(nuevas))
# ======= FIN cache de memoria =======

def get_venezuela_current_datetime():
    return datetime.now(VENEZUELA_TZ)

def get_venezuela_current_date():
    return get_venezuela_current_datetime().date()

def get_nombre_mes(month_number):
    meses = {
        1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
        7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
    }
    return meses.get(month_number, "")

# =================================================================================
# ===== FUNCIONES DE UTILIDAD Y FILTROS JINJA =====
# =================================================================================

def format_decimal_smart(value, places=2):
    """
    Formatea un valor decimal como una cadena con comas y un número
    específico de decimales. Si `places` es None, usa la lógica original.
    """
    if value is None:
        return ''
    try:
        d = Decimal(value)
        if places is not None:
            # Formatea con separador de miles y número fijo de decimales
            return f"{d:,.{places}f}"
        else:
            # Lógica original si no se especifican los decimales
            return d.normalize().to_eng_string()
    except (TypeError, InvalidOperation, ValueError):
        return str(value)

def time_ago(time_value):
    if not time_value: return "Nunca"
    now = datetime.now(pytz.utc)
    if time_value.tzinfo is None:
        time_value = pytz.utc.localize(time_value)
    diff = now - time_value
    seconds = diff.total_seconds()
    if seconds < 10: return "justo ahora"
    if seconds < 60: return f"hace {int(seconds)} segundos"
    minutes = seconds / 60
    if minutes < 60: return f"hace {int(minutes)} minuto{'s' if int(minutes) > 1 else ''}"
    hours = minutes / 60
    if hours < 24: return f"hace {int(hours)} hora{'s' if int(hours) > 1 else ''}"
    days = hours / 24
    return f"hace {int(days)} día{'s' if int(days) > 1 else ''}"

app.jinja_env.filters['format_decimal'] = format_decimal_smart
app.jinja_env.filters['time_ago'] = time_ago

def get_proximo_dia_habil(fecha):
    proximo_dia = fecha + timedelta(days=1)
    while proximo_dia.weekday() >= 5:
        proximo_dia += timedelta(days=1)
    return proximo_dia

@app.template_filter('format_datetime')
def format_datetime_filter(value, format='%d/%m/%Y %I:%M %p'):
    if value and isinstance(value, (datetime, date)):
        return value.strftime(format)
    return value

@app.template_filter('format_date')
def format_date_filter(value, format='%d/%m/%Y'):
    if value and isinstance(value, (datetime, date)):
        return value.strftime(format)
    return value

@app.context_processor
def inject_utility_functions():
    # CORRECCIÓN: Se llama a la función con () para inyectar el VALOR.
    return dict(get_venezuela_current_date=get_venezuela_current_date())

# =================================================================================
# ===== CONEXIÓN A LA BASE DE DATOS =====
# =================================================================================

def get_db():
    if 'db' not in g:
        try:
            # Usamos la variable DATABASE_URL que Render provee automáticamente
            database_url = os.getenv("DATABASE_URL")
            if not database_url:
                raise ValueError("La variable de entorno DATABASE_URL no está configurada.")
            
            g.db = psycopg2.connect(
                database_url,
                cursor_factory=psycopg2.extras.DictCursor
            )
        except (psycopg2.OperationalError, ValueError) as e:
            logging.error(f"Error connecting to the database: {e}")
            g.db = None
    return g.db

@app.teardown_appcontext
def close_db(e=None):
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
    g.contador = None
    g.anio_actual = get_venezuela_current_date().year

    db = get_db()
    if db:
        with db.cursor() as cur:
            admin_id = session.get('admin_id')
            if admin_id:
                cur.execute("SELECT * FROM administradores WHERE id = %s", (admin_id,))
                g.admin = cur.fetchone()

            cliente_id = session.get('cliente_id')
            if cliente_id:
                cur.execute("SELECT * FROM clientes WHERE id = %s", (cliente_id,))
                g.cliente = cur.fetchone()

            contador_id = session.get('contador_id')
            if contador_id:
                cur.execute("SELECT * FROM contadores WHERE id = %s", (contador_id,))
                g.contador = cur.fetchone()

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            flash("Debes iniciar sesión para acceder a esta página.", "warning")
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

def rol_requerido(*roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            admin_rol = session.get('admin_rol') or (g.admin['rol'] if g.admin else None)
            if not admin_rol or admin_rol not in roles:
                flash("No tienes los permisos necesarios para acceder a esta función.", "danger")
                return redirect(url_for('home'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# ===== INICIO: DECORADORES FALTANTES =====
def contador_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'contador_id' not in session:
            flash("Debes iniciar sesión en el portal de contabilidad para acceder.", "warning")
            return redirect(url_for('portal_contabilidad_login'))
        return f(*args, **kwargs)
    return decorated_function

def portal_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'cliente_id' not in session:
            flash("Debes iniciar sesión para acceder a tu portal.", "warning")
            return redirect(url_for('portal_login'))
        return f(*args, **kwargs)
    return decorated_function
# ===== FIN: DECORADORES FALTANTES =====
# =================================================================================
# ===== INICIO: NUEVAS RUTAS DEL PORTAL DE CONTABILIDAD (FASE 1) =====
# =================================================================================

# === Autenticación del Contador ===
@app.route('/portal/contabilidad/login', methods=['GET', 'POST'])
def portal_contabilidad_login():
    if g.contador:
        return redirect(url_for('portal_contabilidad_hub'))

    if request.method == 'POST':
        usuario = request.form.get('usuario', '').strip()
        password = request.form.get('password', '')
        conn = get_db()

        if not conn:
            flash('Error de conexión con la base de datos.', 'danger')
            return render_template('contabilidad_login.html')

        with conn.cursor() as cur:
            cur.execute("SELECT * FROM contadores WHERE lower(usuario) = lower(%s)", (usuario,))
            contador = cur.fetchone()
            if contador and check_password_hash(contador['password_hash'], password):
                if contador['estatus'] != 'Activo':
                    flash('Tu cuenta está inactiva. Contacta a un administrador.', 'danger')
                    return redirect(url_for('portal_contabilidad_login'))

                session.clear()
                session['contador_id'] = contador['id']
                cur.execute("UPDATE contadores SET ultimo_login = NOW() WHERE id = %s", (contador['id'],))
                conn.commit()
                flash(f"¡Bienvenido, {contador['nombre_completo']}!", 'success')
                return redirect(url_for('portal_contabilidad_hub'))

        # Si el usuario o contraseña son incorrectos, la ejecución llega aquí.
        flash('Usuario o contraseña incorrectos.', 'danger')
        return redirect(url_for('portal_contabilidad_login'))

    return render_template('contabilidad_login.html', anio_actual=get_venezuela_current_date().year)

@app.route('/portal/contabilidad/logout')
def portal_contabilidad_logout():
    session.pop('contador_id', None)
    flash('Has cerrado la sesión del portal de contabilidad.', 'info')
    return redirect(url_for('portal_contabilidad_login'))

@app.route('/portal/contabilidad/hub')
@contador_required
def portal_contabilidad_hub():
    return render_template('contabilidad_hub.html')

@app.route('/portal/contabilidad/peticiones', methods=['GET', 'POST'])
@contador_required
def portal_contabilidad_peticiones():
    conn = get_db()
    if request.method == 'POST':
        try:
            titulo = request.form.get('titulo')
            descripcion = request.form.get('descripcion')
            monto_str = request.form.get('monto', '0').replace(',', '.')
            monto = Decimal(monto_str)
            moneda = request.form.get('moneda')
            if not all([titulo, monto > 0, moneda]):
                flash('El título, monto y moneda son obligatorios.', 'danger')
                return redirect(url_for('portal_contabilidad_peticiones'))
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO peticiones_pago (solicitante_id, titulo, descripcion, monto, moneda)
                    VALUES (%s, %s, %s, %s, %s)
                """, (g.contador['id'], titulo, descripcion, monto, moneda))
            conn.commit()
            flash('¡Petición de pago creada exitosamente! Será revisada por un administrador.', 'success')
        except (psycopg2.Error, InvalidOperation) as e:
            conn.rollback()
            flash(f'Error al crear la petición: {e}', 'danger')
        return redirect(url_for('portal_contabilidad_peticiones'))
    peticiones = []
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT p.*, a_rev.usuario as revisado_por, a_pag.usuario as pagado_por
                    FROM peticiones_pago p
                    LEFT JOIN administradores a_rev ON p.revisado_por_id = a_rev.id
                    LEFT JOIN administradores a_pag ON p.pagado_por_id = a_pag.id
                    WHERE p.solicitante_id = %s 
                    ORDER BY p.fecha_peticion DESC
                """, (g.contador['id'],))
                peticiones = cur.fetchall()
        except psycopg2.Error as e:
            flash(f"Error al cargar tus peticiones: {e}", "danger")
    return render_template('contabilidad_peticiones.html', peticiones=peticiones)

# =================================================================================
# ===== INICIO: PORTAL DE CONTABILIDAD (FASE 3 - REPORTES) =====
# =================================================================================

class PDF(FPDF):
    def header(self):
        self.image('https://sistema-integral-moto-plan-motors-2025.s3.us-east-1.amazonaws.com/Logo/ColorLargo.svg', 10, 8, 50)
        self.set_font('Arial', 'B', 15)
        self.cell(80)
        self.cell(30, 10, 'Reporte Financiero Mensual', 0, 0, 'C')
        self.ln(20)

    def footer(self):
        self.set_y(-15)
        self.set_font('Arial', 'I', 8)
        self.cell(0, 10, f'Pagina {self.page_no()}', 0, 0, 'C')

@app.route('/portal/contabilidad/reportes', methods=['GET', 'POST'])
@contador_required
def portal_contabilidad_reportes():
    conn = get_db()
    reporte_data = None
    mes_param = request.args.get('mes', type=int) or request.form.get('mes', type=int)
    anio_param = request.args.get('anio', type=int) or request.form.get('anio', type=int)
    descargar_formato = request.args.get('descargar')

    if mes_param and anio_param:
        try:
            _, ultimo_dia = monthrange(anio_param, mes_param)
            fecha_inicio = date(anio_param, mes_param, 1)
            fecha_fin = date(anio_param, mes_param, ultimo_dia)

            with conn.cursor() as cur:
                # --- Lógica de cálculo (se mantiene igual) ---
                cur.execute("SELECT COALESCE(SUM(CASE WHEN moneda_referencia = 'USD' THEN monto ELSE 0 END), 0) as total_usd, COALESCE(SUM(monto_bs), 0) as total_ves FROM pagos WHERE estado_pago = 'Conciliado' AND fecha_pago BETWEEN %s AND %s", (fecha_inicio, fecha_fin))
                ingresos = cur.fetchone()
                cur.execute("SELECT COALESCE(SUM(CASE WHEN moneda = 'USD' THEN monto ELSE 0 END), 0) as total_usd, COALESCE(SUM(CASE WHEN moneda = 'VES' THEN monto ELSE 0 END), 0) as total_ves FROM peticiones_pago WHERE estado = 'Pagada' AND fecha_pago BETWEEN %s AND %s", (fecha_inicio, fecha_fin))
                egresos = cur.fetchone()
                cur.execute("SELECT AVG(tasa) as tasa_promedio FROM historial_tasas_bcv WHERE fecha BETWEEN %s AND %s", (fecha_inicio, fecha_fin))
                tasa_promedio = cur.fetchone()['tasa_promedio'] or Decimal('0.0')
                cur.execute("SELECT tasa FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (fecha_fin,))
                tasa_fin_mes_row = cur.fetchone()
                tasa_fin_mes = tasa_fin_mes_row['tasa'] if tasa_fin_mes_row else Decimal('0.0')
                cur.execute("SELECT monto_bs, tasa_dia FROM pagos WHERE estado_pago = 'Conciliado' AND monto_bs > 0 AND fecha_pago BETWEEN %s AND %s", (fecha_inicio, fecha_fin))
                pagos_en_ves = cur.fetchall()
                perdida_devaluacion = Decimal('0.0')
                if tasa_fin_mes > 0:
                    for pago in pagos_en_ves:
                        tasa_dia_pago = pago['tasa_dia'] or tasa_promedio
                        if tasa_dia_pago > 0:
                            valor_usd_inicial = pago['monto_bs'] / tasa_dia_pago
                            valor_usd_final = pago['monto_bs'] / tasa_fin_mes
                            perdida_devaluacion += (valor_usd_inicial - valor_usd_final)
                
                reporte_data = {
                    "mes": mes_param, "anio": anio_param,
                    "periodo": f"{get_nombre_mes(mes_param)} {anio_param}",
                    "ingresos_usd": ingresos['total_usd'], "ingresos_ves": ingresos['total_ves'],
                    "egresos_usd": egresos['total_usd'], "egresos_ves": egresos['total_ves'],
                    "balance_usd": ingresos['total_usd'] - egresos['total_usd'], "balance_ves": ingresos['total_ves'] - egresos['total_ves'],
                    "tasa_promedio": tasa_promedio, "perdida_devaluacion_usd": perdida_devaluacion
                }

                # --- Lógica de Descarga ---
                if descargar_formato:
                    cur.execute("""
                        SELECT p.fecha_pago, (c.nombre || ' ' || c.apellido) as cliente, p.por_concepto_de, p.monto, p.monto_bs, p.tasa_dia
                        FROM pagos p JOIN clientes c ON p.cliente_id = c.id
                        WHERE p.estado_pago = 'Conciliado' AND p.fecha_pago BETWEEN %s AND %s ORDER BY p.fecha_pago ASC
                    """, (fecha_inicio, fecha_fin))
                    recibos = cur.fetchall()

                    if descargar_formato == 'pdf':
                        pdf = PDF('L', 'mm', 'A4')
                        pdf.add_page()
                        pdf.set_font('Arial', 'B', 12)
                        pdf.cell(0, 10, f"Detalle de Recibos para {reporte_data['periodo']}", 0, 1, 'C')
                        pdf.set_font('Arial', 'B', 9)
                        pdf.cell(30, 7, 'Fecha', 1)
                        pdf.cell(70, 7, 'Cliente', 1)
                        pdf.cell(80, 7, 'Concepto', 1)
                        pdf.cell(30, 7, 'Monto USD', 1)
                        pdf.cell(30, 7, 'Monto Bs', 1)
                        pdf.cell(30, 7, 'Tasa Dia', 1)
                        pdf.ln()
                        pdf.set_font('Arial', '', 9)
                        for recibo in recibos:
                            pdf.cell(30, 6, recibo['fecha_pago'].strftime('%d/%m/%Y'), 1)
                            pdf.cell(70, 6, recibo['cliente'], 1)
                            pdf.cell(80, 6, recibo['por_concepto_de'], 1)
                            pdf.cell(30, 6, f"${recibo['monto']:,.2f}", 1)
                            pdf.cell(30, 6, f"{recibo['monto_bs']:,.2f} Bs", 1)
                            pdf.cell(30, 6, f"{recibo['tasa_dia']:,.2f}" if recibo['tasa_dia'] else 'N/A', 1)
                            pdf.ln()
                        
                        return Response(pdf.output(dest='S').encode('latin-1'), mimetype='application/pdf', headers={'Content-Disposition': f'attachment;filename=recibos_{mes_param}_{anio_param}.pdf'})

        except (psycopg2.Error, ValueError) as e:
            flash(f"Error al generar el reporte: {e}", "danger")
            logging.error(f"Error en reporte contable: {e}")

    # Lógica para GET
    anio_actual = get_venezuela_current_date().year
    anios_disponibles = [anio_actual - i for i in range(5)]
    meses_disponibles = [{"valor": i, "nombre": get_nombre_mes(i)} for i in range(1, 13)]

    return render_template('contabilidad_reportes.html', anios=anios_disponibles, meses=meses_disponibles, reporte=reporte_data)

# =================================================================================
# ===== FIN: PORTAL DE CONTABILIDAD (FASE 3) =====
# =================================================================================

# === VISTA DEL ADMIN: Gestionar peticiones de contabilidad ===
@app.route('/admin/peticiones')
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora', 'asistente')
def admin_peticiones():
    conn = get_db()
    peticiones = []
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT p.*, c.nombre_completo as solicitante_nombre
                    FROM peticiones_pago p
                    JOIN contadores c ON p.solicitante_id = c.id
                    ORDER BY CASE p.estado WHEN 'Pendiente' THEN 1 WHEN 'Aprobada' THEN 2 WHEN 'Rechazada' THEN 3 WHEN 'Pagada' THEN 4 ELSE 5 END, p.fecha_peticion DESC
                """)
                peticiones = cur.fetchall()
        except psycopg2.Error as e:
            flash(f"Error al cargar las peticiones: {e}", "danger")
    return render_template('admin_peticiones.html', peticiones=peticiones)

# =================================================================================
# ===== FIN: NUEVAS RUTAS DEL PORTAL DE CONTABILIDAD (FASE 1) =====
# =================================================================================

# =================================================================================
# ===== INICIO: PORTAL DE CONTABILIDAD (FASE 2 - Lógica de Gestión Admin) =====
# =================================================================================

@app.route('/admin/peticiones/detalle/<int:peticion_id>')
@admin_required
def get_peticion_detalle(peticion_id):
    conn = get_db()
    if not conn:
        return jsonify({'error': 'Error de conexión'}), 500
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.*, c.nombre_completo as solicitante_nombre, a_rev.usuario as revisado_por, a_pag.usuario as pagado_por
                FROM peticiones_pago p
                JOIN contadores c ON p.solicitante_id = c.id
                LEFT JOIN administradores a_rev ON p.revisado_por_id = a_rev.id
                LEFT JOIN administradores a_pag ON p.pagado_por_id = a_pag.id
                WHERE p.id = %s
            """, (peticion_id,))
            peticion = cur.fetchone()
            if not peticion:
                return jsonify({'error': 'Petición no encontrada'}), 404
            
            cur.execute("""
                SELECT s.*, a.usuario as subido_por_usuario
                FROM soportes_pago s
                LEFT JOIN administradores a ON s.subido_por_id = a.id
                WHERE s.peticion_id = %s ORDER BY s.fecha_subida DESC
            """, (peticion_id,))
            soportes_raw = cur.fetchall()
            soportes = [{k: str(v) if isinstance(v, (Decimal, datetime, date)) else v for k, v in dict(s).items()} for s in soportes_raw]
            
            peticion_dict = {k: str(v) if isinstance(v, (Decimal, datetime, date)) else v for k, v in dict(peticion).items()}
            peticion_dict['soportes'] = soportes
            
            # Añadir la URL base de S3 para construir los enlaces en el frontend
            bucket_name = os.environ.get('AWS_STORAGE_BUCKET_NAME')
            peticion_dict['s3_base_url'] = f"https://{bucket_name}.s3.amazonaws.com"

            return jsonify(peticion_dict)
    except psycopg2.Error as e:
        logging.error(f"Error en API get_peticion_detalle: {e}")
        return jsonify({'error': str(e)}), 500

app.route('/admin/peticiones/procesar/<int:peticion_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora', 'asistente')
def procesar_peticion(peticion_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", "danger")
        return redirect(url_for('admin_peticiones'))
    
    accion = request.form.get('accion')
    notas = request.form.get('notas_admin')
    
    try:
        with conn.cursor() as cur:
            if accion == 'aprobar':
                cur.execute("""
                    UPDATE peticiones_pago SET estado = 'Aprobada', revisado_por_id = %s, fecha_revision = NOW(), notas_adicionales = %s
                    WHERE id = %s AND estado = 'Pendiente'
                """, (g.admin['id'], notas, peticion_id))
                flash('Petición aprobada. Ahora puede ser marcada como pagada.', 'success')

            elif accion == 'rechazar':
                if not notas:
                    flash('Debe proporcionar un motivo para rechazar la petición.', 'danger')
                    return redirect(url_for('admin_peticiones'))
                cur.execute("""
                    UPDATE peticiones_pago SET estado = 'Rechazada', revisado_por_id = %s, fecha_revision = NOW(), notas_adicionales = %s
                    WHERE id = %s AND estado = 'Pendiente'
                """, (g.admin['id'], notas, peticion_id))
                flash('Petición rechazada exitosamente.', 'warning')

            elif accion == 'pagar':
                soporte_pago = request.files.get('soporte_pago')
                if not soporte_pago or soporte_pago.filename == '':
                    flash('Debe adjuntar un comprobante de pago para marcar la petición como pagada.', 'danger')
                    return redirect(url_for('admin_peticiones'))

                cur.execute("""
                    SELECT c.usuario FROM peticiones_pago pp JOIN contadores c ON pp.solicitante_id = c.id WHERE pp.id = %s
                """, (peticion_id,))
                contador = cur.fetchone()
                if not contador:
                    flash('No se encontró el solicitante de la petición.', 'danger')
                    return redirect(url_for('admin_peticiones'))

                bucket_name = os.environ.get('AWS_STORAGE_BUCKET_NAME')
                timestamp = int(datetime.now().timestamp())
                nombre_seguro = f"{timestamp}_{soporte_pago.filename.replace(' ', '_')}"
                ruta_en_s3 = f"soportes_pago/{contador['usuario']}/{nombre_seguro}"

                if not subir_fileobj_a_s3(soporte_pago, bucket_name, ruta_en_s3):
                    flash('Error crítico al subir el comprobante a S3. La operación fue cancelada.', 'danger')
                    return redirect(url_for('admin_peticiones'))
                
                cur.execute("""
                    UPDATE peticiones_pago SET estado = 'Pagada', pagado_por_id = %s, fecha_pago = NOW(), notas_adicionales = %s
                    WHERE id = %s AND estado = 'Aprobada'
                """, (g.admin['id'], notas, peticion_id))
                
                cur.execute("""
                    INSERT INTO soportes_pago (peticion_id, ruta_s3, nombre_archivo, subido_por_id)
                    VALUES (%s, %s, %s, %s)
                """, (peticion_id, ruta_en_s3, soporte_pago.filename, g.admin['id']))

                flash('Petición marcada como pagada y comprobante adjuntado.', 'success')
            
            else:
                flash('Acción no válida.', 'danger')

        conn.commit()
    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Error al procesar la petición: {e}", "danger")

    return redirect(url_for('admin_peticiones'))
# =================================================================================
# ===== FIN: PORTAL DE CONTABILIDAD (FASE 2 - Lógica de Gestión Admin) =====
# =================================================================================

# =================================================================================
# ===== FUNCIONES AUXILIARES (AUDITORÍA, COMISIONES, TESORERÍA) =====
# =================================================================================
# >>> INICIO DE LA INTEGRACIÓN: NUEVA FUNCIÓN AUXILIAR <<<
def calcular_ingreso_real_acumulado(fecha_inicio):
    """
    Calcula la suma de todos los ingresos CONCILIADOS desde una fecha de inicio.
    Convierte todos los montos a un equivalente en USD para una totalización unificada.
    """
    conn = get_db()
    total_ingresado_usd = Decimal('0.0')
    if not conn:
        return total_ingresado_usd
    try:
        with conn.cursor() as cur:
            # Suma todos los montos que ya están en USD y los montos en Bs convertidos a USD con la tasa del día del pago.
            cur.execute("""
                SELECT 
                    COALESCE(SUM(monto), 0) + 
                    COALESCE(SUM(CASE WHEN tasa_dia > 0 THEN monto_bs / tasa_dia ELSE 0 END), 0) as total
                FROM pagos
                WHERE estado_pago = 'Conciliado' AND fecha_conciliacion >= %s
            """, (fecha_inicio,))
            resultado = cur.fetchone()
            if resultado and resultado['total']:
                total_ingresado_usd = resultado['total']
    except psycopg2.Error as e:
        logging.error(f"Error al calcular ingreso real acumulado: {e}")
    
    return total_ingresado_usd
# >>> FIN DE LA INTEGRACIÓN <<<

def subir_archivo_a_s3(base64_data, nombre_en_s3):
    """Sube un archivo a S3 desde una cadena de datos Base64."""
    s3_client = boto3.client('s3')
    bucket_name = os.environ.get('AWS_STORAGE_BUCKET_NAME')
    if not bucket_name:
        logging.error("FATAL: AWS_STORAGE_BUCKET_NAME no está configurada.")
        return False
    try:
        header, encoded = base64_data.split(",", 1)
        image_data = base64.b64decode(encoded)
        in_mem_file = io.BytesIO(image_data)
        s3_client.upload_fileobj(
            in_mem_file, bucket_name, nombre_en_s3,
            ExtraArgs={'ContentType': 'image/jpeg', 'ACL': 'public-read'}
        )
        logging.info(f"Subida exitosa a S3: {nombre_en_s3}")
        return True
    except Exception as e:
        logging.error(f"Error al subir archivo a S3: {e}")
        return False

def subir_fileobj_a_s3(file_obj, bucket_name, object_name):
    """Sube un objeto de archivo a un bucket de S3."""
    s3_client = boto3.client('s3')
    try:
        s3_client.upload_fileobj(
            file_obj,
            bucket_name,
            object_name,
            ExtraArgs={'ACL': 'public-read', 'ContentType': file_obj.content_type}
        )
        logging.info(f"Subida de archivo exitosa a S3: {object_name}")
        return True
    except NoCredentialsError:
        logging.error("Credenciales de AWS no encontradas.")
        return False
    except Exception as e:
        logging.error(f"Error al subir archivo a S3: {e}")
        return False

def registrar_accion_auditoria(accion, descripcion, cliente_id=None, detalles_adicionales=None):
    """Registra una acción en la tabla de auditoría."""
    conn = get_db()
    if not conn: return
    usuario_id = g.admin['id'] if g.admin else None
    usuario_nombre = g.admin['usuario'] if g.admin else f"Cliente ID {session.get('cliente_id')}"
    detalles_json = json.dumps(detalles_adicionales) if detalles_adicionales else None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO registros_auditoria (usuario_id, usuario_nombre, accion, descripcion, cliente_afectado_id, detalles, ip_address) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (usuario_id, usuario_nombre, accion, descripcion, cliente_id, detalles_json, request.remote_addr)
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"AUDITORIA-FALLO-INSERCION: {e}")
        conn.rollback()

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

@app.route('/delete/<int:client_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin')
def delete_client(client_id):
    """
    Elimina un cliente. La base de datos se encargará de eliminar
    todos los registros asociados gracias a la regla ON DELETE CASCADE.
    """
    conn = get_db()
    if not conn: 
        flash('Error de conexión a la base de datos.', 'error')
        return redirect(url_for('consulta'))

    try:
        with conn.cursor() as cur:
            # Obtener datos del cliente ANTES de borrarlo para la auditoría
            cur.execute("SELECT nombre, apellido, cedula FROM clientes WHERE id = %s", (client_id,))
            cliente_a_borrar = cur.fetchone()
            
            if not cliente_a_borrar:
                flash('El cliente que intenta eliminar no existe.', 'warning')
                return redirect(url_for('consulta'))

            # Registrar la acción en la auditoría
            descripcion_audit = f"Eliminó al cliente {cliente_a_borrar['nombre']} {cliente_a_borrar['apellido']} (C.I. {cliente_a_borrar['cedula']}) y todos sus datos asociados."
            registrar_accion_auditoria('ELIMINACION_CLIENTE', descripcion_audit, client_id)
            
            # ¡Esta es la única línea de eliminación que necesitas!
            cur.execute("DELETE FROM clientes WHERE id = %s", (client_id,))
            
            conn.commit()
            flash('¡Cliente y todos sus registros asociados han sido eliminados exitosamente!', 'success')

    except psycopg2.Error as e:
        conn.rollback()
        logging.error(f"ERROR DE BASE DE DATOS AL ELIMINAR CLIENTE ID {client_id}: {e}")
        flash(f"ERROR: La base de datos bloqueó la eliminación. Detalles: {e}", 'danger')
        
    except Exception as e:
        conn.rollback()
        logging.error(f"ERROR INESPERADO AL ELIMINAR CLIENTE ID {client_id}: {e}")
        flash(f'Ocurrió un error inesperado al eliminar: {e}', 'error')

    # ===== INICIO DE LA CORRECCIÓN =====
    # Se cambia la redirección para que vaya al 'hub' en lugar de 'consulta'.
    return redirect(url_for('hub'))
    # ===== FIN DE LA CORRECCIÓN =====

@app.route('/guardar_oferta/<int:client_id>', methods=['POST'])
@admin_required
def guardar_oferta(client_id):
    conn = get_db()
    cuotas_ofertadas = request.form.get('cuotas_ofertadas')
    # NUEVO: Se obtiene el modelo del formulario
    modelo_ofertado = request.form.get('modelo_ofertado')
    cedula_cliente = ''

    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta'))
    
    try:
        with conn.cursor() as cur:
            # ... (Lógica de validación existente) ...
            
            if not cuotas_ofertadas or not cuotas_ofertadas.isdigit() or int(cuotas_ofertadas) <= 0:
                flash("Debe ingresar un número válido de cuotas para la oferta.", 'error')
                return redirect(url_for('consulta', busqueda=cedula_cliente))
            
            # ACTUALIZADO: Se añade modelo_ofertado a la consulta INSERT
            cur.execute("""
                INSERT INTO ofertas (cliente_id, cuotas_ofertadas, modelo_ofertado, fecha_oferta, estado_oferta) 
                VALUES (%s, %s, %s, %s, 'activa')
            """, (client_id, int(cuotas_ofertadas), modelo_ofertado, get_venezuela_current_date()))
            
            # ACTUALIZADO: El mensaje de auditoría ahora es más descriptivo
            registrar_accion_auditoria('REGISTRO_OFERTA_ADMIN', f"Registró una oferta de {cuotas_ofertadas} cuotas por el modelo '{modelo_ofertado}'.", client_id)
            
            conn.commit()
            flash(f"¡Oferta de {cuotas_ofertadas} cuotas registrada exitosamente!", 'success')

    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Ocurrió un error al registrar la oferta: {e}", 'error')
    
    return redirect(url_for('consulta', busqueda=cedula_cliente))


# =================================================================================
# ===== FUNCIONES HELPER PARA LÓGICA DE PAGOS POR DIFERENCIA =====
# =================================================================================

def recalcular_totales_bulk(bulk_id):
    """
    Recalcula los totales de un bulk basado en sus líneas de pago.
    Actualiza el estado del bulk y sus pagos asociados si los totales coinciden.
    NOTA: Esta función NO gestiona la transacción (commit/rollback).
    """
    conn = get_db()
    if not conn:
        logging.error(f"BULK_RECALC_V3: Falla de conexión para bulk_id {bulk_id}")
        return

    try:
        with conn.cursor() as cur:
            # Bloquea el bulk para evitar condiciones de carrera
            cur.execute("SELECT currency, expected_amount FROM payment_bulks WHERE id = %s FOR UPDATE", (bulk_id,))
            bulk = cur.fetchone()
            if not bulk:
                logging.warning(f"BULK_RECALC_V3: No se encontró el bulk_id {bulk_id} para recalcular.")
                return

            # Obtiene todos los pagos asociados que no estén anulados
            cur.execute("""
                SELECT estado_reporte, monto, monto_bs, detalles_reporte
                FROM pagos
                WHERE bulk_id = %s AND estado_pago != 'Anulado'
            """, (bulk_id,))
            pagos_asociados = cur.fetchall()

            total_verificado = Decimal('0.0')
            for pago in pagos_asociados:
                # Si el reporte fue aprobado, se suma el monto reportado completo.
                if pago['estado_reporte'] == 'Aprobado':
                    if bulk['currency'] == 'VES':
                        total_verificado += pago['monto_bs'] or Decimal('0.0')
                    else:
                        total_verificado += pago['monto'] or Decimal('0.0')
                
                # Si es inconsistente, se busca el monto que el admin verificó.
                elif pago['estado_reporte'] == 'Inconsistente':
                    detalles = pago['detalles_reporte'] or {}
                    if isinstance(detalles, str):
                        try: detalles = json.loads(detalles)
                        except json.JSONDecodeError: detalles = {}
                    
                    if 'monto_verificado' in detalles:
                        total_verificado += Decimal(detalles['monto_verificado'])

            # Actualiza la tabla payment_bulks con el nuevo total
            cur.execute("""
                UPDATE payment_bulks SET total_verified = %s, updated_at = NOW() WHERE id = %s
            """, (total_verificado, bulk_id))
            
            logging.info(f"BULK_RECALC_V3: Total verificado actualizado a {total_verificado} para bulk_id {bulk_id}")

            # --- LÓGICA CRÍTICA DE TRANSICIÓN DE ESTADO ---
            # Si el total verificado ya cubre lo esperado, se marca como listo para conciliar.
            if total_verificado >= bulk['expected_amount']:
                cur.execute("""
                    UPDATE payment_bulks SET status = 'READY_TO_RECONCILE' WHERE id = %s
                """, (bulk_id,))
                logging.info(f"BULK_RECALC_V3: Bulk #{bulk_id} actualizado a READY_TO_RECONCILE.")
                
                # --- ¡NUEVA LÓGICA! ---
                # Actualiza el estado de todos los pagos del bulk para que desaparezcan de la cola de revisión.
                cur.execute("""
                    UPDATE pagos SET estado_reporte = 'Aprobado' 
                    WHERE bulk_id = %s AND estado_reporte = 'Inconsistente'
                """, (bulk_id,))
                logging.info(f"BULK_RECALC_V3: Pagos inconsistentes del bulk #{bulk_id} actualizados a Aprobado.")

    except (psycopg2.Error, InvalidOperation, json.JSONDecodeError) as e:
        logging.error(f"BULK_RECALC_V3: Error recalculando totales para bulk_id {bulk_id}: {e}")
        # Re-lanza la excepción para que la función que llama pueda manejar el rollback.
        raise e

# =================================================================================
# ===== RUTAS DE NAVEGACIÓN Y AUTENTICACIÓN =====
# =================================================================================

@app.route('/')
def home():
    if g.admin: return redirect(url_for('hub'))
    if g.cliente: return redirect(url_for('portal_dashboard'))
    return render_template('landing.html', anio_actual=g.anio_actual)

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if g.admin: return redirect(url_for('hub'))
    if request.method == 'POST':
        usuario, password = request.form.get('usuario'), request.form.get('password')
        conn = get_db()
        if not conn:
            flash('Error de conexión con la base de datos.', 'danger')
            return render_template('admin_login.html', anio_actual=g.anio_actual)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM administradores WHERE usuario = %s", (usuario,))
            admin = cur.fetchone()
            if admin and check_password_hash(admin['password_hash'], password):
                session.clear()
                session['admin_id'] = admin['id']
                session['admin_rol'] = admin['rol'] # Guardar rol en sesión
                cur.execute("UPDATE administradores SET ultimo_login = NOW(), estatus_online = TRUE, ultimo_visto = NOW() WHERE id = %s", (admin['id'],))
                conn.commit()
                today_str = get_venezuela_current_date().isoformat()
                if session.get('last_welcome_date') != today_str:
                    session['show_welcome_modal'] = f"¡Bienvenido de nuevo, {admin['usuario']}!"
                    session['last_welcome_date'] = today_str
                return redirect(url_for('hub'))
            else:
                flash('Usuario o contraseña incorrectos.', 'danger')
    return render_template('admin_login.html', anio_actual=g.anio_actual)

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

@app.route('/portal/login', methods=['GET', 'POST'])
def portal_login():
    if g.cliente: return redirect(url_for('portal_dashboard'))
    if request.method == 'POST':
        cedula = request.form.get('cedula', '').strip().replace('V-', '').replace('v-', '')
        contrato_nro = request.form.get('contrato_nro', '').strip().upper().replace('MP-', '')
        conn = get_db()
        if not conn:
            flash('Error de conexión. Intente más tarde.', 'error')
            return render_template('portal_login.html', anio_actual=g.anio_actual)
        with conn.cursor() as cur:
            cur.execute("SELECT id, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE TRIM(cedula) = %s AND REPLACE(TRIM(UPPER(numero_contrato)), 'MP-', '') = %s;", (cedula, contrato_nro))
            cliente = cur.fetchone()
        if cliente:
            session.clear()
            session['cliente_id'], session['cliente_nombre'] = cliente['id'], cliente['nombre_apellido']
            return redirect(url_for('portal_dashboard'))
        else:
            flash('Credenciales incorrectas.', 'error')
    return render_template('portal_login.html', anio_actual=g.anio_actual)

@app.route('/portal/logout')
def portal_logout():
    session.clear()
    flash('Has cerrado sesión exitosamente.', 'success')
    return redirect(url_for('portal_login'))

# =================================================================================
# ===== RUTAS DEL PANEL DE ADMINISTRADOR =====
# =================================================================================

@app.route('/hub')
@admin_required
def hub():
    # 1. Se inicializa el diccionario UNA SOLA VEZ con todos los valores por defecto.
    stats = {
        'clientes_cartera': 0,
        'recaudado_mes': Decimal('0.0'),
        'solicitudes_pendientes': 0,
        'reportes_pendientes': 0,
        'pagos_por_conciliar': 0,
        'tasa_bcv': 'N/A'
    }
    conn = get_db()
    if conn:
        try:
            with conn.cursor() as cur:
                # 2. Se actualizan los valores del diccionario con los datos de la base de datos.
                cur.execute("SELECT COUNT(*) FROM clientes WHERE estatus_cliente = 'ACTIVO'")
                stats['clientes_cartera'] = cur.fetchone()[0]

                cur.execute("SELECT COUNT(DISTINCT b.id) FROM payment_bulks b JOIN pagos p ON p.bulk_id = b.id WHERE b.status IN ('OPEN', 'UNDER_REVIEW') AND p.estado_reporte NOT LIKE 'Anulado%'")
                stats['reportes_pendientes'] = cur.fetchone()[0]

                cur.execute("SELECT tasa FROM historial_tasas_bcv ORDER BY fecha DESC LIMIT 1")
                tasa_row = cur.fetchone()
                if tasa_row and tasa_row['tasa']:
                    stats['tasa_bcv'] = f"{tasa_row['tasa']:,.2f} Bs"

                # (Aquí se pueden añadir más consultas para los otros KPIs en el futuro)

        except psycopg2.Error as e:
            logging.error(f"Error al calcular estadísticas para el HUB: {e}")
            flash("No se pudieron cargar todas las estadísticas del panel.", "warning")

    # 3. Se gestiona el mensaje de bienvenida y se renderiza la plantilla.
    welcome_message = session.pop('show_welcome_modal', None)
    return render_template('hub.html', stats=stats, welcome_message=welcome_message)

@app.route('/api/activity_ping', methods=['POST'])
@admin_required
def activity_ping():
    """
    Endpoint para que el frontend notifique que el admin sigue activo.
    Actualiza la marca de tiempo 'ultimo_visto'.
    """
    conn = get_db()
    if not conn:
        return jsonify({'status': 'error', 'message': 'db connection failed'}), 500
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE administradores SET ultimo_visto = NOW() WHERE id = %s",
                (g.admin['id'],)
            )
            conn.commit()
        return jsonify({'status': 'ok'})
    except psycopg2.Error as e:
        conn.rollback()
        logging.error(f"Error en activity_ping para admin {g.admin['id']}: {e}")
        return jsonify({'status': 'error', 'message': 'database error'}), 500

@app.route('/api/get_active_sessions')
@admin_required
def get_active_sessions():
    """
    Endpoint de API mejorado que ahora incluye los segundos de inactividad
    para cada usuario, permitiendo al frontend aplicar la lógica de los 5 minutos.
    """
    conn = get_db()
    if not conn:
        return jsonify([])

    users_list = []
    try:
        with conn.cursor() as cur:
            # CORRECCIÓN: Se añade el cálculo de 'inactivity_seconds'
            cur.execute("""
                SELECT 
                    usuario, 
                    ultimo_visto, 
                    current_status,
                    EXTRACT(EPOCH FROM (NOW() - ultimo_visto)) AS inactivity_seconds
                FROM administradores ORDER BY usuario
            """)
            usuarios_db = cur.fetchall()
            for user in usuarios_db:
                users_list.append({
                    "username": user['usuario'],
                    "current_status": user['current_status'] or 'Disponible',
                    "last_seen": time_ago(user['ultimo_visto']),
                    "inactivity_seconds": user['inactivity_seconds']
                })
    except psycopg2.Error as e:
        logging.error(f"Error en API get_active_sessions: {e}")
        return jsonify({"error": "Database error"}), 500
        
    return jsonify(users_list)

@app.route('/api/update_status', methods=['POST'])
@admin_required
def update_status():
    """
    Endpoint para que un administrador actualice su propio estado de actividad.
    """
    conn = get_db()
    new_status = request.json.get('status')
    allowed_statuses = ['Disponible', 'Ocupado', 'Reunion', 'Descanso']

    if not conn or not new_status or new_status not in allowed_statuses:
        return jsonify({'status': 'error', 'message': 'Datos inválidos'}), 400

    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE administradores SET current_status = %s WHERE id = %s",
                (new_status, g.admin['id'])
            )
            conn.commit()
            return jsonify({'status': 'success', 'message': 'Estado actualizado.'})
    except psycopg2.Error as e:
        conn.rollback()
        logging.error(f"Error al actualizar estado para admin {g.admin['id']}: {e}")
        return jsonify({'status': 'error', 'message': 'Error de base de datos'}), 500
        
    # Devuelve la lista de usuarios en formato JSON.
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
                WHERE c.gestor_id = %s AND TRIM(UPPER(c.estado_del_plan)) = 'AHORRADOR' AND TRIM(UPPER(c.estatus_cliente)) = 'ACTIVO'
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
@rol_requerido('superadmin', 'gerente', 'administradora', 'asistente')
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
@rol_requerido('superadmin', 'gerente', 'administradora', 'asistente')
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
@rol_requerido('superadmin', 'gerente', 'administradora', 'asistente')
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
                    cur.execute("UPDATE clientes SET estatus_cliente = 'RETIRO' WHERE id = %s", (cliente_id,))
                elif tipo == 'Congelamiento':
                    duracion = detalles_actualizados.get('tiempo_congelamiento', '1 mes')
                    meses = 2 if '2' in duracion else 1
                    fecha_fin = get_venezuela_current_date() + timedelta(days=meses * 30)
                    detalles_actualizados['fecha_fin_congelamiento'] = fecha_fin.isoformat()
                    cur.execute("UPDATE clientes SET estatus_cliente = 'CONGELADO' WHERE id = %s", (cliente_id,))
                    cur.execute("UPDATE solicitudes SET detalles = %s WHERE id = %s", (json.dumps(detalles_actualizados), solicitud_id))
                elif tipo == 'Descongelamiento':
                    cur.execute("UPDATE clientes SET estatus_cliente = 'ACTIVO' WHERE id = %s", (cliente_id,))

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
    conn = get_db()
    reportes_categorizados = {'pendientes': [], 'diferencias': []}
    if not conn:
        flash("Error de conexión con la base de datos.", "danger")
        return render_template('reportes_por_revisar.html', reportes=reportes_categorizados)
    try:
        with conn.cursor() as cur:
            # --- LÓGICA ORIGINAL PARA PAGOS DE CUOTAS (BULKS) ---
            # Esta parte se mantiene como estaba.
            query_bulks = """
                SELECT DISTINCT ON (b.id)
                    p.id,
                    b.status AS bulk_status,
                    b.expected_amount,
                    b.currency,
                    p.monto,
                    p.monto_bs,
                    p.fecha_creacion,
                    c.id AS cliente_id,
                    c.nombre,
                    c.apellido,
                    c.cedula
                FROM payment_bulks b
                JOIN clientes c ON b.cliente_id = c.id
                INNER JOIN pagos p ON p.bulk_id = b.id
                WHERE b.status IN ('OPEN', 'UNDER_REVIEW') AND p.estado_reporte NOT LIKE 'Anulado%'
                ORDER BY b.id, p.fecha_creacion ASC;
            """
            cur.execute(query_bulks)
            procesos_pendientes = cur.fetchall()

            for proceso_row in procesos_pendientes:
                reporte = dict(proceso_row)
                if reporte['currency'] == 'VES':
                    reporte['monto_esperado_bs'] = reporte['expected_amount']
                    reporte['monto_esperado_usd'] = Decimal('0.0')
                else:
                    reporte['monto_esperado_usd'] = reporte['expected_amount']
                    reporte['monto_esperado_bs'] = Decimal('0.0')

                if reporte['bulk_status'] == 'UNDER_REVIEW':
                    reportes_categorizados['diferencias'].append(reporte)
                elif reporte['bulk_status'] == 'OPEN':
                    reportes_categorizados['pendientes'].append(reporte)
            
            # --- INICIO DE LA NUEVA LÓGICA PARA PAGOS DE INSCRIPCIÓN (CORREGIDA) ---
            query_inscripciones = """
                SELECT 
                    p.id,
                    p.monto,
                    p.monto_bs,
                    p.fecha_creacion,
                    c.id as cliente_id,
                    c.nombre,
                    c.apellido,
                    c.cedula,
                    c.inscripcion_monto as monto_esperado_usd -- CORREGIDO: Se usa 'inscripcion_monto'
                FROM pagos p
                JOIN clientes c ON p.cliente_id = c.id
                WHERE p.tipo_pago = 'Inscripción' 
                  AND p.estado_reporte = 'Pendiente de Revision'
                  AND p.bulk_id IS NULL;
            """
            cur.execute(query_inscripciones)
            pagos_inscripcion_pendientes = cur.fetchall()
            
            for pago_insc in pagos_inscripcion_pendientes:
                reporte_insc = dict(pago_insc)
                reporte_insc['monto_esperado_bs'] = Decimal('0.0') 
                reportes_categorizados['pendientes'].append(reporte_insc)
            # --- FIN DE LA NUEVA LÓGICA ---

    except psycopg2.Error as e:
        flash(f"Error al cargar la lista de reportes: {e}", "danger")
        logging.error(f"Error en reportes_por_revisar: {traceback.format_exc()}")

    return render_template('reportes_por_revisar.html', reportes=reportes_categorizados)

 # --- INICIO DE LA CORRECCIÓN ---
# Esta es una función auxiliar que contiene la lógica pura de conciliación.
# La usamos para evitar repetir código.
def _conciliar_pago_logica(pago_id, cur):
    """
    Ejecuta la lógica de base de datos para conciliar un pago.
    Esta función NO hace commit. La transacción debe ser manejada por la función que la llama.
    """
    cur.execute("SELECT * FROM pagos WHERE id = %s FOR UPDATE", (pago_id,))
    pago = cur.fetchone()
    if not pago:
        raise ValueError(f"Pago con ID {pago_id} no encontrado para conciliar.")

    cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s FOR UPDATE", (pago['cliente_id'],))
    cliente = cur.fetchone()
    if not cliente:
        raise ValueError(f"Cliente con ID {pago['cliente_id']} no encontrado.")

    admin_id = g.admin['id']
    flash_msg = ""

    if pago['tipo_pago'] == 'Inscripción':
        cur.execute(
            "UPDATE pagos SET estado_pago = 'Conciliado', conciliado_por_id = %s, fecha_conciliacion = NOW() WHERE id = %s",
            (admin_id, pago_id)
        )
        cur.execute(
            "UPDATE clientes SET inscripcion_pagada = inscripcion_pagada + %s WHERE id = %s RETURNING inscripcion_pagada, inscripcion_monto",
            (pago['monto'], cliente['id'])
        )
        updated_cliente = cur.fetchone()
        
        if updated_cliente['inscripcion_pagada'] >= updated_cliente['inscripcion_monto']:
            cur.execute(
                "UPDATE clientes SET proceso = 'INSCRITO' WHERE id = %s", (cliente['id'],)
            )
            flash_msg = f"¡Pago de inscripción conciliado y el cliente ahora está INSCRITO!"
        else:
            flash_msg = f"¡Abono de inscripción de ${pago['monto']} conciliado exitosamente!"
    
    elif pago['tipo_pago'] == 'Cuota':
        # Aquí iría la lógica completa para conciliar una cuota
        # (actualizar cuotas pagadas, balance, etc.)
        cur.execute(
            "UPDATE pagos SET estado_pago = 'Conciliado', conciliado_por_id = %s, fecha_conciliacion = NOW() WHERE id = %s",
            (admin_id, pago_id)
        )
        flash_msg = f"¡Pago de cuota de ${pago['monto']} conciliado exitosamente!"

    # Registrar auditoría
    descripcion_audit = f"Concilió el pago N° {pago_id} (Tipo: {pago['tipo_pago']}, Monto: ${pago['monto']})."
    registrar_accion_auditoria('CONCILIACION_PAGO', descripcion_audit, cliente['id'])
    
    return flash_msg, cliente['cedula']

@app.route('/procesar_reporte/<int:pago_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def procesar_reporte(pago_id):
    """
    Procesa un reporte de pago enviado por un cliente. Puede ser aprobado directamente
    o marcado con una diferencia, generando una nueva orden de pago para el cliente.
    """
    conn = get_db()
    accion = request.form.get('accion')
    if not conn or not accion:
        flash("Solicitud inválida o error de conexión.", "danger")
        return redirect(url_for('reportes_por_revisar'))

    try:
        with conn.cursor() as cur:
            # --- INICIO DE LA LÓGICA CORREGIDA ---
            # Se busca el pago y su bulk_id asociado.
            cur.execute("""
                SELECT p.*, c.moneda_pago
                FROM pagos p JOIN clientes c ON p.cliente_id = c.id
                WHERE p.id = %s AND p.estado_reporte = 'Pendiente de Revision' FOR UPDATE
            """, (pago_id,))
            pago = cur.fetchone()

            if not pago:
                flash("El reporte de pago no se encontró o ya fue procesado.", "warning")
                return redirect(url_for('reportes_por_revisar'))

            cliente_id = pago['cliente_id']
            bulk_id = pago.get('bulk_id') # Obtenemos el bulk_id existente

            if not bulk_id:
                # Este caso es un fallback de seguridad, no debería ocurrir en el flujo normal
                # ya que los reportes de clientes ahora siempre crean un bulk.
                flash(f"Error Crítico: El pago #{pago_id} no tiene un proceso asociado (bulk_id). Contacte a soporte.", "danger")
                logging.error(f"FATAL: Pago #{pago_id} sin bulk_id al intentar procesar diferencia.")
                return redirect(url_for('reportes_por_revisar'))
            # --- FIN DE LA LÓGICA CORREGIDA ---

            if accion == 'aprobar_para_conciliar':
                monto_a_verificar = pago['monto_bs'] if pago['monto_bs'] and pago['monto_bs'] > 0 else pago['monto']
                detalles_aprobacion = json.dumps({'monto_verificado': str(monto_a_verificar)})
                cur.execute(
                    "UPDATE pagos SET estado_reporte = 'Aprobado', revisado_por_id = %s, fecha_revision = NOW(), detalles_reporte = %s WHERE id = %s",
                    (g.admin['id'], detalles_aprobacion, pago_id)
                )

                # Actualizamos el total verificado en el bulk
                cur.execute(
                    "UPDATE payment_bulks SET total_verified = total_verified + %s, updated_at = NOW() WHERE id = %s RETURNING expected_amount, total_verified",
                    (monto_a_verificar, bulk_id)
                )
                bulk_actualizado = cur.fetchone()
                
                # Si el total verificado ahora es suficiente, se marca para conciliar
                if bulk_actualizado and bulk_actualizado['total_verified'] >= bulk_actualizado['expected_amount']:
                    cur.execute("UPDATE payment_bulks SET status = 'READY_TO_RECONCILE' WHERE id = %s", (bulk_id,))

                flash('Reporte aprobado. El proceso ha sido actualizado y pasará a conciliación cuando el monto total esté cubierto.', 'success')
            
            elif accion == 'corregir_y_generar_diferencia':
                motivo_cliente = request.form.get('motivo_cliente')
                monto_real_recibido, currency = (None, None)
                
                cur.execute("SELECT currency, expected_amount FROM payment_bulks WHERE id = %s", (bulk_id,))
                bulk_info = cur.fetchone()

                if bulk_info['currency'] == 'USD':
                    monto_real_recibido_str = request.form.get('monto_real_recibido_usdt', '0.00')
                    monto_real_recibido = Decimal(monto_real_recibido_str)
                    currency = 'USD'
                else:  # Asume 'VES'
                    monto_real_recibido_str = request.form.get('monto_real_recibido', '0.00')
                    monto_real_recibido = Decimal(monto_real_recibido_str)
                    currency = 'VES'

                if monto_real_recibido is None or not motivo_cliente:
                    flash("Debe ingresar el monto real y el motivo para el cliente.", "error")
                    return redirect(url_for('ver_reporte', pago_id=pago_id))

                monto_esperado = bulk_info['expected_amount']
                
                # 1. Se marca el pago original como 'Inconsistente'
                detalles_inconsistencia = {
                    'motivo': 'Discrepancia Verificada por Admin',
                    'monto_original_reportado': str(pago['monto_bs'] if currency == 'VES' else pago['monto']),
                    'monto_verificado': str(monto_real_recibido),
                    'motivo_para_cliente': motivo_cliente 
                }
                cur.execute(
                    """
                    UPDATE pagos SET estado_reporte = 'Inconsistente', revisado_por_id = %s, 
                           fecha_revision = NOW(), detalles_reporte = %s
                    WHERE id = %s
                    """,
                    (g.admin['id'], json.dumps(detalles_inconsistencia), pago_id)
                )

                # 2. Se actualiza el 'bulk' existente a 'UNDER_REVIEW' y se ajusta el total verificado.
                cur.execute(
                    """
                    UPDATE payment_bulks 
                    SET status = 'UNDER_REVIEW', total_verified = %s, updated_at = NOW() 
                    WHERE id = %s
                    """,
                    (monto_real_recibido, bulk_id)
                )
                
                # 3. Se genera la orden de pago por la diferencia
                monto_pendiente = monto_esperado - monto_real_recibido
                if monto_pendiente > 0:
                    cur.execute("""
                        INSERT INTO payment_orders (bulk_id, cliente_id, amount, currency, status)
                        VALUES (%s, %s, %s, %s, 'ISSUED')
                    """, (bulk_id, cliente_id, monto_pendiente, currency))
                
                descripcion_audit = f"Corrigió reporte #{pago_id}. Monto verificado: {monto_real_recibido}. Se generó orden por {monto_pendiente:,.2f} {currency}."
                registrar_accion_auditoria('CORRECCION_REPORTE_ADMIN', descripcion_audit, cliente_id, {'pago_id': pago_id})
                flash("El monto verificado ha sido registrado. Se generó una orden de pago para el cliente por la diferencia.", "success")
            
            else:
                flash('Acción no válida.', 'error')
                return redirect(url_for('ver_reporte', pago_id=pago_id))

            conn.commit()

    except (psycopg2.Error, ValueError, InvalidOperation) as e:
        conn.rollback()
        error_trace = traceback.format_exc()
        logging.error(f"Error al procesar el reporte {pago_id}:\n{error_trace}")
        flash(f"Error CRÍTICO al procesar el reporte: {e}", "error")
    
    return redirect(url_for('reportes_por_revisar'))

@app.route('/admin/anular_reporte/<int:pago_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def anular_reporte_admin(pago_id):
    conn = get_db()
    motivo = request.form.get('motivo')

    if not conn:
        return jsonify({'status': 'error', 'message': 'Error de conexión a la base de datos.'}), 500
    
    if not motivo or not motivo.strip():
        return jsonify({'status': 'error', 'message': 'El motivo de la anulación es obligatorio.'}), 400

    try:
        with conn.cursor() as cur:
            # 1. Buscar el pago y asegurarse de que se puede anular
            cur.execute("SELECT cliente_id, estado_reporte FROM pagos WHERE id = %s FOR UPDATE", (pago_id,))
            pago = cur.fetchone()

            if not pago:
                return jsonify({'status': 'error', 'message': 'El reporte de pago no fue encontrado.'}), 404
            
            if pago['estado_reporte'] != 'Pendiente de Revision':
                return jsonify({'status': 'error', 'message': 'Este reporte no se puede anular porque ya fue procesado.'}), 400

            # 2. Actualizar el estado y guardar los detalles de la anulación
            detalles_anulacion = {
                'motivo_anulacion': motivo.strip(),
                'anulado_por': g.admin['usuario'],
                'fecha_anulacion': get_venezuela_current_datetime().isoformat()
            }
            
            cur.execute("""
                UPDATE pagos 
                SET estado_reporte = 'Anulado por Admin', 
                    estado_pago = 'Anulado',
                    detalles_reporte = %s::jsonb
                WHERE id = %s
            """, (json.dumps(detalles_anulacion), pago_id))

            # 3. Registrar en la auditoría
            descripcion_audit = f"Anuló el reporte de pago #{pago_id}. Motivo: {motivo.strip()}"
            registrar_accion_auditoria('ANULACION_REPORTE', descripcion_audit, pago['cliente_id'], {'pago_id': pago_id})
            
            conn.commit()
            return jsonify({'status': 'success', 'message': 'El reporte ha sido anulado exitosamente.'})

    except psycopg2.Error as e:
        conn.rollback()
        logging.error(f"Error al anular reporte {pago_id}: {e}")
        return jsonify({'status': 'error', 'message': f'Error de base de datos: {e}'}), 500

@app.route('/admin/anular_proceso_pago/<int:bulk_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def anular_proceso_pago(bulk_id):
    """
    Anula un proceso de pago de forma inteligente.
    - Si es un proceso de diferencia, anula la diferencia y revierte el pago original.
    - Si es un proceso simple, anula todos los pagos asociados.
    """
    conn = get_db()
    motivo = request.form.get('motivo')

    if not conn:
        return jsonify({'status': 'error', 'message': 'Error de conexión a la base de datos.'}), 500
    
    if not motivo or not motivo.strip():
        return jsonify({'status': 'error', 'message': 'El motivo de la anulación es obligatorio.'}), 400

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cliente_id, status FROM payment_bulks WHERE id = %s FOR UPDATE", (bulk_id,))
            bulk = cur.fetchone()

            if not bulk:
                return jsonify({'status': 'error', 'message': 'El proceso de pago no fue encontrado.'}), 404
            
            # La anulación ahora se permite en más estados iniciales.
            if bulk['status'] not in ['OPEN', 'UNDER_REVIEW', 'READY_TO_RECONCILE']:
                return jsonify({'status': 'error', 'message': 'Este proceso ya no se puede anular porque ha sido procesado o conciliado.'}), 400

            cur.execute("SELECT cedula FROM clientes WHERE id = %s", (bulk['cliente_id'],))
            cliente = cur.fetchone()
            cedula_para_redirect = cliente['cedula'] if cliente else None

            detalles_anulacion = {
                'motivo_anulacion': motivo.strip(),
                'anulado_por': g.admin['usuario'],
                'fecha_anulacion': get_venezuela_current_datetime().isoformat()
            }
            detalles_json = json.dumps(detalles_anulacion)

            # --- INICIO DE LA LÓGICA UNIVERSAL ---

            # 1. Identificar el tipo de proceso: Contamos cuántos pagos originales y de diferencia hay.
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE is_diferencia = TRUE) as count_diferencia,
                    COUNT(*) FILTER (WHERE is_diferencia = FALSE OR is_diferencia IS NULL) as count_original
                FROM pagos
                WHERE bulk_id = %s
            """, (bulk_id,))
            counts = cur.fetchone()

            # 2. Aplicar la lógica de anulación según el tipo de proceso
            if counts and counts['count_diferencia'] > 0 and counts['count_original'] > 0:
                # Escenario A: Es un Proceso de Diferencia (tiene original y diferencia)
                # Anulamos solo los pagos de diferencia
                cur.execute("""
                    UPDATE pagos 
                    SET estado_reporte = 'Anulado por Admin', estado_pago = 'Anulado',
                        detalles_reporte = COALESCE(detalles_reporte, '{}'::jsonb) || %s::jsonb
                    WHERE bulk_id = %s AND is_diferencia = TRUE
                """, (detalles_json, bulk_id))

                # Revertimos el pago original a pendiente y lo desvinculamos
                cur.execute("""
                    UPDATE pagos
                    SET estado_reporte = 'Pendiente de Revision', bulk_id = NULL, detalles_reporte = NULL
                    WHERE bulk_id = %s AND (is_diferencia = FALSE OR is_diferencia IS NULL)
                """, (bulk_id,))
                
                message = 'El proceso de diferencia ha sido anulado. El pago original está nuevamente pendiente de revisión.'

            else:
                # Escenario B: Es un Proceso Simple (solo pago/s original/es)
                # Anulamos todos los pagos dentro del bulk
                cur.execute("""
                    UPDATE pagos 
                    SET estado_reporte = 'Anulado por Admin', estado_pago = 'Anulado',
                        detalles_reporte = COALESCE(detalles_reporte, '{}'::jsonb) || %s::jsonb
                    WHERE bulk_id = %s
                """, (detalles_json, bulk_id))
                
                message = 'El proceso de pago y todos sus reportes asociados han sido anulados.'

            # --- FIN DE LA LÓGICA UNIVERSAL ---

            # 3. Anular las órdenes y el bulk (común para ambos escenarios)
            cur.execute("UPDATE payment_orders SET status = 'CANCELLED' WHERE bulk_id = %s", (bulk_id,))
            cur.execute("UPDATE payment_bulks SET status = 'CANCELLED' WHERE id = %s", (bulk_id,))

            descripcion_audit = f"Anuló el proceso de pago (Bulk ID #{bulk_id}). Motivo: {motivo.strip()}"
            registrar_accion_auditoria('ANULACION_PROCESO_PAGO', descripcion_audit, bulk['cliente_id'], {'bulk_id': bulk_id})
            
            conn.commit()

            return jsonify({
                'status': 'success', 
                'message': message,
                'cedula': cedula_para_redirect
            })

    except psycopg2.Error as e:
        conn.rollback()
        logging.error(f"Error al anular el proceso de pago (bulk) {bulk_id}: {e}")
        return jsonify({'status': 'error', 'message': f'Error de base de datos: {e}'}), 500

@app.route('/pagos_por_conciliar')
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora', 'asistente')
def pagos_por_conciliar():
    conn = get_db()
    pagos_a_conciliar = []
    bulks_a_conciliar = []
    
    if not conn:
        flash("Error de conexión con la base de datos.", "danger")
        return render_template('pagos_por_conciliar.html', pagos=pagos_a_conciliar, bulks=bulks_a_conciliar, anio_actual=get_venezuela_current_date().year)
    
    try:
        with conn.cursor() as cur:
            # Busca pagos normales que fueron aprobados y están listos.
            cur.execute("""
                SELECT p.*, c.nombre, c.apellido, c.cedula
                FROM pagos p JOIN clientes c ON p.cliente_id = c.id
                WHERE p.estado_reporte = 'Aprobado' AND p.estado_pago = 'Pendiente' AND p.bulk_id IS NULL
                ORDER BY p.fecha_creacion ASC;
            """)
            pagos_a_conciliar = cur.fetchall()

            # --- INICIO DE LA CORRECCIÓN ---
            # Ahora busca bulks que están explícitamente listos O que, estando en revisión, ya tienen el monto completo.
            cur.execute("""
                SELECT b.*, c.nombre, c.apellido
                FROM payment_bulks b JOIN clientes c ON b.cliente_id = c.id
                WHERE b.status = 'READY_TO_RECONCILE' 
                OR (b.status = 'UNDER_REVIEW' AND b.total_verified >= b.expected_amount)
                ORDER BY b.updated_at ASC;
            """)
            bulks_a_conciliar = cur.fetchall()
            # --- FIN DE LA CORRECCIÓN ---

    except psycopg2.Error as e:
        logging.error(f"Error al obtener datos para conciliar: {e}")
        flash("Error al cargar la lista de pagos y lotes por conciliar.", "danger")
    
    return render_template('pagos_por_conciliar.html', 
                           pagos=pagos_a_conciliar, 
                           bulks=bulks_a_conciliar, 
                           anio_actual=get_venezuela_current_date().year)
# =================================================================================
# ===== MÓDULO DE TESORERÍA, COMERCIAL Y REPORTES =====
# =================================================================================

@app.route('/tesoreria/rebalanceo', methods=['GET', 'POST'])
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora', 'asistente')
def tesoreria_rebalanceo():
    conn = get_db()
    hoy = get_venezuela_current_date()
    balances_actuales = calcular_balances_tesoreria() 
    historial_movimientos = []
    
    # >>> INICIO DE LA INTEGRACIÓN: BUSCAR PROYECCIÓN Y DETERMINAR RECOMENDACIÓN <<<
    contexto_proyeccion = None
    mostrar_recomendacion = False
    tasas_del_dia = {'usd': Decimal('0.0'), 'eur': Decimal('0.0')}
    
    if conn:
        try:
            with conn.cursor() as cur:
                # Obtener tasas del día para cálculos y visualización
                cur.execute("SELECT tasa, tasa_euro FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (hoy,))
                resultado_tasa = cur.fetchone()
                if resultado_tasa:
                    tasas_del_dia['usd'] = resultado_tasa['tasa'] or Decimal('0.0')
                    tasas_del_dia['eur'] = resultado_tasa['tasa_euro'] or Decimal('0.0')

                # Buscar Proyección Activa
                cur.execute("""
                    SELECT * FROM proyecciones_activas 
                    WHERE mes_proyeccion = %s AND ano_proyeccion = %s AND estado = 'Activa' 
                    LIMIT 1
                """, (hoy.month, hoy.year))
                proyeccion_activa = cur.fetchone()

                if proyeccion_activa:
                    resultados = json.loads(proyeccion_activa.get('resultados_resumen', '{}'))
                    contexto_proyeccion = {
                        "balance_neto_proyectado": Decimal(resultados.get('resumen', {}).get('balance_neto_proyectado', '0.0')),
                        "perdida_devaluacion_proyectada": Decimal(resultados.get('kpis', {}).get('perdida_devaluacion_usd', '0.0'))
                    }
                    
                    # Lógica para la recomendación
                    tasa_usd_actual = tasas_del_dia.get('usd')
                    if tasa_usd_actual and tasa_usd_actual > 0:
                        saldo_bs_en_usd = balances_actuales.get('CAJA_BS_TOTAL', Decimal('0.0')) / tasa_usd_actual
                        # Umbral de recomendación: si el saldo en Bs equivale a más de $200
                        if saldo_bs_en_usd > 200 and contexto_proyeccion['perdida_devaluacion_proyectada'] > 0:
                            mostrar_recomendacion = True

                # Carga del historial (lógica existente)
                cur.execute("""
                    SELECT op.*, admin.usuario as nombre_admin, op.perdida_cambiaria
                    FROM operaciones_tesoreria op
                    LEFT JOIN administradores admin ON op.realizada_por = admin.id
                    ORDER BY op.fecha_operacion DESC LIMIT 30
                """)
                historial_movimientos = cur.fetchall()
        except (psycopg2.Error, json.JSONDecodeError, KeyError) as e:
            flash("No se pudo cargar el contexto de la proyección activa.", "warning")
            logging.error(f"Error cargando contexto de proyección en tesorería: {e}")
    # >>> FIN DE LA INTEGRACIÓN <<<

    if request.method == 'POST':
        try:
            form = request.form
            tipo_operacion = form.get('tipo_operacion')
            nota = form.get('nota')
            caja_origen = form.get('caja_origen')
            monto_origen_str = form.get('monto_origen', '0').replace(',', '.')
            moneda_origen = form.get('moneda_origen')
            monto_origen = Decimal(monto_origen_str)

            if not all([tipo_operacion, nota, caja_origen, monto_origen > 0]):
                flash("Error: Tipo, Nota, Caja Origen y Monto son obligatorios.", 'danger')
                return redirect(url_for('tesoreria_rebalanceo'))

            if balances_actuales.get(caja_origen, Decimal('0.0')) < monto_origen:
                flash(f"Error: Fondos insuficientes en '{caja_origen}'.", 'danger')
                return redirect(url_for('tesoreria_rebalanceo'))
            
            # Parse limpio de tasa_aplicada (sin coma al final)
            tasa_aplicada_str = form.get('tasa_aplicada', '0').replace(',', '.')
            tasa_aplicada = Decimal(tasa_aplicada_str) if tasa_aplicada_str and tasa_aplicada_str != '0' else None

            perdida_cambiaria = Decimal('0.0')

            if tipo_operacion in ['PAGO_GASTO', 'PAGO_NOMINA']:
                caja_destino, monto_destino, moneda_destino = 'GASTO_OPERATIVO', monto_origen, moneda_origen
                if tipo_operacion == 'PAGO_NOMINA' and (moneda_origen == 'BS' or moneda_origen == 'VES') and 'USD' in (caja_origen or ''):
                    tasa_bcv = tasas_del_dia.get('usd')
                    if not tasa_bcv or tasa_bcv <= 0:
                        flash("Error: No se encontró una tasa BCV válida para hoy. No se puede procesar el pago de nómina en Bs.", "danger")
                        return redirect(url_for('tesoreria_rebalanceo'))
                    
                    monto_egreso_en_usd = monto_origen / tasa_bcv
                    if tasa_aplicada and tasa_aplicada > 0:
                        perdida_cambiaria = (monto_origen / tasa_bcv) - (monto_origen / tasa_aplicada)
                    
                    monto_origen, moneda_origen = monto_egreso_en_usd, 'USD'
                    monto_destino, moneda_destino = monto_egreso_en_usd, 'USD'
                    nota += f" (Pago original: {monto_origen_str} Bs @ Tasa {tasa_bcv})"
            else:
                # Fix: asignaciones correctas
                caja_destino = form.get('caja_destino')
                monto_destino_str = form.get('monto_destino', '0').replace(',', '.')
                moneda_destino = form.get('moneda_destino')
                monto_destino = Decimal(monto_destino_str) if monto_destino_str and monto_destino_str != '0' else None

                if not all([caja_destino, monto_destino, moneda_destino]):
                    flash("Error: Para transferencias, el destino es obligatorio.", 'danger')
                    return redirect(url_for('tesoreria_rebalanceo'))
                
                if tipo_operacion == 'COMPRA_DIVISAS' and (moneda_origen == 'BS' or moneda_origen == 'VES') and 'USD' in (moneda_destino or ''):
                    tasa_bcv = tasas_del_dia.get('usd')
                    if tasa_bcv and tasa_bcv > 0:
                        valor_real_en_usd_bcv = monto_origen / tasa_bcv
                        valor_obtenido_en_usd = monto_destino
                        perdida_cambiaria = valor_real_en_usd_bcv - valor_obtenido_en_usd
            
            # === INSERT con RETURNING para obtener operacion_id ===
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO operaciones_tesoreria 
                        (tipo_operacion, caja_origen, moneda_origen, monto_origen, 
                         caja_destino, moneda_destino, monto_destino, tasa_aplicada, 
                         nota, realizada_por, fecha_operacion, perdida_cambiaria)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
                    RETURNING id
                """, (tipo_operacion, caja_origen, moneda_origen, monto_origen, 
                      caja_destino, moneda_destino, monto_destino, tasa_aplicada, 
                      nota, g.admin['id'], perdida_cambiaria))
                operacion_id = cur.fetchone()['id']

            # === APLICAR A EGRESO (si viene de prefill) ===
            egreso_ocurrencia_id = form.get('egreso_ocurrencia_id') or request.args.get('egreso_ocurrencia_id')
            if egreso_ocurrencia_id:
                egreso_ocurrencia_id = int(egreso_ocurrencia_id)

                # Equivalente en USD para registrar el pago del egreso
                moneda_upper = (moneda_origen or '').upper()
                if moneda_upper in ('USD', 'USDT'):
                    equivalente_usd = float(monto_origen)
                elif moneda_upper in ('BS', 'VES'):
                    # Usa tasa_aplicada si viene, si no, tasa del día
                    tasa = float(tasa_aplicada) if (tasa_aplicada and tasa_aplicada > 0) else float(tasas_del_dia.get('usd') or 0) or 0.0
                    equivalente_usd = float(monto_origen) / (tasa if tasa > 0 else 1.0)
                else:
                    # Monedas no mapeadas: asume 1:1
                    equivalente_usd = float(monto_origen)

                with conn.cursor() as cur:
                    # 1) Registrar pago del egreso
                    cur.execute("""
                        INSERT INTO egresos_pagos (egreso_ocurrencia_id, movimiento_tesoreria_id,
                                                   monto_original, moneda, tasa_aplicada, monto_equivalente_usd, nota)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                    """, (
                        egreso_ocurrencia_id,
                        operacion_id,
                        float(monto_origen),               # guardamos el valor tal como se operó en tesorería
                        moneda_upper,
                        (float(tasa_aplicada) if tasa_aplicada else None),
                        equivalente_usd,
                        nota
                    ))
                    _pago_id = cur.fetchone()['id']

                    # 2) Actualizar acumulado/estado de la ocurrencia
                    cur.execute("""
                        UPDATE egresos_ocurrencias
                        SET monto_pagado_usd = COALESCE(monto_pagado_usd,0) + %s
                        WHERE id=%s
                        RETURNING monto_programado_usd, monto_pagado_usd
                    """, (equivalente_usd, egreso_ocurrencia_id))
                    occ = cur.fetchone()
                    pendiente = float(occ['monto_programado_usd'] or 0) - float(occ['monto_pagado_usd'] or 0)
                    nuevo_estado = 'pagado' if pendiente <= 0.00001 else ('parcial' if (occ['monto_pagado_usd'] or 0) > 0 else 'pendiente')
                    cur.execute("UPDATE egresos_ocurrencias SET estado=%s WHERE id=%s", (nuevo_estado, egreso_ocurrencia_id))

                    # 3) Enlazar operación ↔ ocurrencia en la tabla correcta
                    cur.execute("""
                        UPDATE operaciones_tesoreria
                        SET referencia_tipo='EGRESO', referencia_id=%s
                        WHERE id=%s
                    """, (egreso_ocurrencia_id, operacion_id))

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

    return render_template('tesoreria_rebalanceo.html', 
                           balances=balances_actuales, 
                           historial=historial_movimientos, 
                           anio_actual=get_venezuela_current_date().year,
                           contexto_proyeccion=contexto_proyeccion,
                           mostrar_recomendacion=mostrar_recomendacion,
                           tasas_del_dia=tasas_del_dia)

# >>> COMISIONES: BEGIN [dashboard_comercial]
@app.route('/comercial/dashboard', methods=['GET'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def dashboard_comercial():
    conn = get_db()
    
    comisiones, asesores, lotes = [], [], []
    stats = defaultdict(lambda: {'monto': Decimal('0.0'), 'conteo': 0})
    args = request.args
    today = get_venezuela_current_date()
    
    filters = {
        'fecha_desde_origen': args.get('fecha_desde_origen', (today - timedelta(days=30)).strftime('%Y-%m-%d')),
        'fecha_hasta_origen': args.get('fecha_hasta_origen', today.strftime('%Y-%m-%d')),
        'fecha_desde_pago': args.get('fecha_desde_pago'),
        'fecha_hasta_pago': args.get('fecha_hasta_pago'),
        'asesor_id': args.get('asesor_id'),
        'estado': args.get('estado'),
        'moneda': args.get('moneda'),
        'lote_id': args.get('lote_id')
    }

    if not conn:
        flash("Error de conexión a la base de datos. No se pudieron cargar los datos.", "danger")
        return render_template(
            'dashboard_comercial.html', 
            comisiones=comisiones, stats=stats, asesores=asesores, lotes=lotes, filters=filters,
            anio_actual=get_venezuela_current_date().year
        )

    # --- INICIO DE LA CORRECCIÓN ---
    # Se reemplaza cl.plan por cl.plan_contratado
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
    # --- FIN DE LA CORRECCIÓN ---
    
    filters_sql_list = []
    params = []

    if filters['fecha_desde_origen']: filters_sql_list.append("c.fecha_origen >= %s"); params.append(filters['fecha_desde_origen'])
    if filters['fecha_hasta_origen']: filters_sql_list.append("c.fecha_origen <= %s"); params.append(filters['fecha_hasta_origen'])
    if filters['asesor_id']: filters_sql_list.append("c.asesor_id = %s"); params.append(filters['asesor_id'])
    if filters['estado']: filters_sql_list.append("c.estado = %s"); params.append(filters['estado'])

    if filters_sql_list:
        base_query += " WHERE " + " AND ".join(filters_sql_list)
    
    base_query += " ORDER BY c.fecha_origen DESC"

    try:
        with conn.cursor() as cur:
            cur.execute(base_query, tuple(params))
            comisiones = cur.fetchall()
            
            cur.execute("SELECT estado, moneda, SUM(monto) as total_monto, COUNT(id) as total_conteo FROM comisiones GROUP BY estado, moneda")
            stats_db = cur.fetchall()
            for row in stats_db:
                if row['moneda'] == 'USD':
                    stats[row['estado']]['monto'] += row['total_monto']
                    stats[row['estado']]['conteo'] += row['total_conteo']
            
            cur.execute("SELECT SUM(monto_ajuste) FROM comisiones_rebalanceos")
            total_rebalanceos = cur.fetchone()[0]
            stats['rebalanceos']['monto'] = total_rebalanceos or Decimal('0.0')

            cur.execute("SELECT id, nombre_completo FROM administradores WHERE rol IN ('superadmin', 'gerente', 'asesor') ORDER BY nombre_completo")
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
        filters=filters,
        anio_actual=get_venezuela_current_date().year
    )

@app.route('/config/comisiones', methods=['GET', 'POST'])
@admin_required
@rol_requerido('superadmin')  # usa tu decorador estándar
def config_comisiones():
    # GET: muestra reglas actuales
    if request.method == 'GET':
        reglas = get_reglas_comisiones() or {}
        reglas_json = json.dumps(reglas, ensure_ascii=False, indent=2)
        return render_template('config_comisiones.html', reglas_json=reglas_json)

    # POST: guardar (o resetear)
    try:
        # Acepta tanto form como JSON, y múltiples nombres válidos
        raw = (
            request.form.get('json_reglas')
            or request.form.get('reglas_json')
            or request.form.get('rules_json')
            or (request.is_json and (
                request.json.get('json_reglas')
                or request.json.get('reglas_json')
                or request.json.get('rules_json')
                or request.json.get('rules')  # compat
            ))
        )
        if raw is None:
            flash("No se recibió contenido", "error")
            return redirect(url_for('config_comisiones'))

        # Reset opcional (?reset=1) desde el botón de la plantilla
        if request.args.get('reset') == '1':
            nuevas = get_reglas_comisiones_por_defecto()  # usa tu función/constante de defaults
        else:
            nuevas = json.loads(raw) if isinstance(raw, str) else raw

        set_reglas_comisiones(nuevas)
        flash("¡Reglas de comisiones actualizadas!", "success")
        return redirect(url_for('config_comisiones'))

    except Exception as e:
        logging.exception("Error actualizando reglas de comisiones")
        flash(f"Error guardando reglas: {e}", "error")
        return redirect(url_for('config_comisiones'))

    # GET: muestra reglas actuales
    reglas = get_reglas_comisiones()
    # Puedes reutilizar tu dashboard o una plantilla aparte:
    # return render_template('dashboard_comercial.html', reglas_comisiones=reglas, es_superadmin=True)
    return render_template('config_comisiones.html', reglas_comisiones=reglas)

@app.route('/comisiones/lotes', methods=['POST'])
@admin_required
def crear_lote_comisiones():
    """
    Aprueba en bloque las comisiones seleccionadas desde el dashboard.
    Espera checkboxes con name='comision_ids[]'.
    """
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", "error")
        return redirect(url_for('dashboard_comercial'))

    # Acepta name="comision_ids[]" o "ids[]" por compatibilidad
    comision_ids = request.form.getlist('comision_ids[]') or request.form.getlist('ids[]')
    if not comision_ids:
        flash("No seleccionaste comisiones.", "warning")
        return redirect(url_for('dashboard_comercial'))

    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE public.comisiones
                   SET estado = 'aprobado',
                       approved_at = NOW(),
                       approved_by = %s
                 WHERE id = ANY(%s)
                   AND estado = 'pendiente'
            """, (getattr(g, 'user_id', None), comision_ids))

        conn.commit()
        flash(f"Se aprobaron {len(comision_ids)} comisiones.", "success")
    except Exception as e:
        conn.rollback()
        logging.exception(e)
        flash(f"No se pudo aprobar el lote: {e}", "error")

    return redirect(url_for('dashboard_comercial'))

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

@app.route('/comercial/split_contrato/<string:numero_contrato>')
@admin_required
@rol_requerido('superadmin', 'gerente')
def get_split_contrato(numero_contrato):
    conn = get_db()
    if not conn: return jsonify({'error': 'Error de conexión'}), 500
    try:
        with conn.cursor() as cur:
            # --- INICIO DE LA CORRECCIÓN ---
            # Se usa el nombre de columna estandarizado 'numero_contrato' en lugar de 'contrato_nro'
            cur.execute("""
                SELECT a.usuario as beneficiario, c.notas as concepto, c.monto
                FROM comisiones c
                JOIN administradores a ON c.asesor_id = a.id
                WHERE c.origen_tipo = 'Venta' AND c.origen_id = (SELECT id FROM clientes WHERE numero_contrato = %s)
                ORDER BY c.monto DESC;
            """, (numero_contrato,))
            comisiones = cur.fetchall()
            
            cur.execute("""
                SELECT cli.plan_contratado, ci.sobrante_empresa 
                FROM caja_inscripciones ci 
                JOIN clientes cli ON ci.cliente_id = cli.id 
                WHERE ci.numero_contrato = %s;
            """, (numero_contrato,))
            contrato_info = cur.fetchone()
            # --- FIN DE LA CORRECCIÓN ---

            if not contrato_info: return jsonify({'error': 'Contrato no encontrado'}), 404
            
            plan_contratado_decimal = Decimal(contrato_info['plan_contratado'] or '0.00')
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
        logging.error(f"Error en get_split_contrato para {numero_contrato}: {e}")
        return jsonify({'error': 'Error al consultar la base de datos'}), 500

@app.route('/comercial/historial_asesor/<string:nombre_beneficiario>')
@admin_required
@rol_requerido('superadmin', 'gerente')
def get_historial_asesor(nombre_beneficiario):
    conn = get_db()
    if not conn: return jsonify({'error': 'Error de conexión'}), 500
    try:
        with conn.cursor() as cur:
            # --- INICIO DE LA CORRECCIÓN ---
            # Se usa 'numero_contrato' para la unión entre tablas
            cur.execute("""
                SELECT c.notas as concepto, c.monto, cli.numero_contrato, cli.nombre, cli.apellido, cli.plan_contratado, ci.responsable_cierre
                FROM comisiones c
                JOIN clientes cli ON c.origen_id = cli.id AND c.origen_tipo = 'Venta'
                JOIN administradores a ON c.asesor_id = a.id
                LEFT JOIN caja_inscripciones ci ON cli.numero_contrato = ci.numero_contrato
                WHERE a.usuario = %s AND c.estado = 'pendiente'
                ORDER BY c.id DESC;
            """, (nombre_beneficiario,))
            # --- FIN DE LA CORRECCIÓN ---
            
            historial = cur.fetchall()
            historial_json = []
            for item in historial:
                plan_contratado_val = Decimal(item['plan_contratado'] or '0.00')
                historial_json.append({
                    'concepto': item['concepto'], 'monto': f"{item['monto']:,.2f}",
                    'numero_contrato': item['numero_contrato'], 'cliente': f"{item['nombre']} {item['apellido']}",
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

# =================================================================================
# ===== INICIO: MÓDULO DE MÉTRICAS RECONSTRUIDO (V1) =====
# ================================================================================

# ===================== RUTA MÉTRICAS (SINTAX-FIX + EXPORTS) =====================
# --- Helper de filtros para clientes (usar en reporte_metricas / reporte_flujo_caja) ---
from datetime import datetime

def _parse_date_any(s):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None

def _build_clientes_filters(args, alias='c',
                            fecha_col_name='fecha_inscripcion',
                            condicion_col_name='condicion',
                            cuotas_col_name='cuotas_pagadas'):
    """
    Devuelve (where_sql, params) para filtrar 'clientes'.
    Parámetros:
      - alias: alias SQL de la tabla clientes (por defecto 'c')
      - fecha_col_name: nombre de la columna de fecha (None para omitir filtro por fecha)
      - condicion_col_name: columna de condición (e.g. 'condicion' o 'condicion_pago')
      - cuotas_col_name: columna de cuotas (e.g. 'cuotas_pagadas' o 'cuotas_pagadas_progresivas')
    """
    p = f"{alias}." if alias else ""
    where, params = [], []

    # Multi
    empresas = [e.strip().upper() for e in args.getlist('empresa') if e and e.strip()]
    if empresas:
        where.append(f"UPPER(TRIM({p}empresa)) IN (" + ", ".join(["%s"]*len(empresas)) + ")")
        params += empresas

    estados = [e.strip().upper() for e in args.getlist('estado_plan') if e and e.strip()]
    if estados:
        where.append(f"UPPER(TRIM({p}estado_del_plan)) IN (" + ", ".join(["%s"]*len(estados)) + ")")
        params += estados

    estatus = [e.strip().upper() for e in args.getlist('estatus_cliente') if e and e.strip()]
    if estatus:
        where.append(f"UPPER(TRIM({p}estatus_cliente)) IN (" + ", ".join(["%s"]*len(estatus)) + ")")
        params += estatus

    conds = [e.strip().upper() for e in args.getlist('condicion') if e and e.strip()]
    if conds and condicion_col_name:
        where.append(f"UPPER(TRIM({p}{condicion_col_name})) IN (" + ", ".join(["%s"]*len(conds)) + ")")
        params += conds

    # Buckets (OR de rangos)
    buckets = [b.strip() for b in args.getlist('cuota_bucket') if b and b.strip()]
    if buckets and cuotas_col_name:
        mapa = {'1': (1, 6), '2': (7, 12), '3': (13, 24), '4': (25, 36)}
        rangos = [mapa[b] for b in buckets if b in mapa]
        if rangos:
            where.append("(" + " OR ".join([f"COALESCE({p}{cuotas_col_name},0) BETWEEN %s AND %s" for _ in rangos]) + ")")
            for a, b in rangos:
                params += [a, b]

    # Fechas inscripción (opcional)
    insc_desde = _parse_date_any(args.get('insc_desde'))
    insc_hasta = _parse_date_any(args.get('insc_hasta'))
    if fecha_col_name:
        col = f"DATE({p}{fecha_col_name})"
        if insc_desde:
            where.append(f"{col} >= %s")
            params.append(insc_desde)
        if insc_hasta:
            where.append(f"{col} <= %s")
            params.append(insc_hasta)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return where_sql, params

@app.route('/reportes/metricas', methods=['GET'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def reporte_metricas():
    from datetime import datetime
    from flask import Response, send_file
    import traceback

    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", "danger")
        return redirect(url_for('gestion_administrativa'))

    # -------- Parámetros (normalizados) --------
    def _opt(v):
        v = (v or "").strip()
        return "" if v.lower() in ("todos", "todas", "all", "-", "--", "seleccione", "seleccionar") else v

    # Soporte multi (checkboxes)
    def _norm_list(key):
        vals = request.args.getlist(key)
        out = []
        for v in vals:
            vv = _opt(v)
            if vv:
                out.append(vv)
        return out

    q_empresas       = _norm_list('empresa')
    q_estados_plan   = _norm_list('estado_plan')
    q_estatuses      = _norm_list('estatus_cliente')
    q_condiciones    = _norm_list('condicion')
    q_cuota_buckets  = _norm_list('cuota_bucket')
    q_export         = (request.args.get('export') or '').strip().lower()
    sort_param       = (request.args.get('sort') or 'id').strip().lower()
    dir_param        = (request.args.get('dir') or request.args.get('order') or 'asc').strip().lower()
    if dir_param not in ('asc', 'desc'):
        dir_param = 'asc'

    # Fechas (acepta YYYY-MM-DD, DD/MM/YY, DD/MM/YYYY, MM/DD/YY, MM/DD/YYYY)
    def _to_date_multi(s):
        s = (s or "").strip()
        if not s:
            return None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(s, fmt).date()
            except Exception:
                continue
        return None

    q_desde = _to_date_multi(request.args.get('insc_desde'))
    q_hasta = _to_date_multi(request.args.get('insc_hasta'))

    try:
        per_page = int(request.args.get('per_page') or 25)
    except Exception:
        per_page = 25
    per_page = max(1, min(100, per_page))

    try:
        page = int(request.args.get('page') or 1)
    except Exception:
        page = 1
    page   = max(1, page)
    offset = (page - 1) * per_page

    # -------- Helpers locales --------
    def _fetch_dicts(cur):
        rows = cur.fetchall() or []
        if rows and isinstance(rows[0], dict):
            return rows
        cols = [d.name for d in cur.description] if cur.description else []
        return [dict(zip(cols, r)) for r in rows]

    def _fetch_val(cur, default=None):
        data = _fetch_dicts(cur)
        return (list(data[0].values())[0] if data else default)

    def _col_exists(cur, col):
        cur.execute("""
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = ANY (current_schemas(true))
              AND table_name='clientes' AND column_name=%s
        """, (col,))
        return bool(cur.fetchone())

    rows=[]; total_count=0; page_count=1
    resumen={}
    chart_estados={'labels':[],'data':[]}
    chart_condicion={'labels':[],'data':[]}
    empresas_opciones=[]; estados_opciones=[]; condicion_opciones=[]; estatus_opciones=[]
    show_nombre=False; show_apellido=False; show_telefono=False; show_condicion=False
    fecha_col_usada='(no disponible)'; cuotas_col_usada='(no disponible)'

    try:
        # Usamos RealDictCursor si está disponible; si no, cursor normal
        try:
            from psycopg2.extras import RealDictCursor as _RDC
        except Exception:
            _RDC = None
        cursor_factory = _RDC if _RDC else None

        with conn.cursor(cursor_factory=cursor_factory) as cur:
            # ---- Descubrimiento de columnas ----
            nombre_col   = 'nombre'   if _col_exists(cur, 'nombre')   else None
            apellido_col = 'apellido' if _col_exists(cur, 'apellido') else None

            telefono_col = None
            for cand in ('numero_telefono','telefono','telefono1','celular','numero_contacto'):
                if _col_exists(cur, cand):
                    telefono_col = cand
                    break

            if   _col_exists(cur, 'condicion_pago'):     condicion_col = 'condicion_pago'
            elif _col_exists(cur, 'condicion'):          condicion_col = 'condicion'
            elif _col_exists(cur, 'condicion_de_pago'):  condicion_col = 'condicion_de_pago'
            else:                                        condicion_col = None

            cuotas_col = 'cuotas_pagadas_progresivas' if _col_exists(cur, 'cuotas_pagadas_progresivas') \
                         else ('cuotas_pagadas' if _col_exists(cur, 'cuotas_pagadas') else None)

            if   _col_exists(cur,'fecha_ingreso'):     fecha_col='fecha_ingreso'
            elif _col_exists(cur,'fecha_de_ingreso'):  fecha_col='fecha_de_ingreso'
            elif _col_exists(cur,'fecha_registro'):    fecha_col='fecha_registro'
            elif _col_exists(cur,'fecha_inscripcion'): fecha_col='fecha_inscripcion'
            else:                                      fecha_col=None

            show_nombre    = bool(nombre_col)
            show_apellido  = bool(apellido_col)
            show_telefono  = bool(telefono_col)
            show_condicion = bool(condicion_col)
            if fecha_col:  fecha_col_usada  = fecha_col
            if cuotas_col: cuotas_col_usada = cuotas_col

            # ---- Expresiones normalizadas ----
            empresa_expr   = "REGEXP_REPLACE(UPPER(TRIM(c.empresa)), '\\\\s+', ' ', 'g')"
            estado_expr    = "UPPER(TRIM(c.estado_del_plan))"

            if _col_exists(cur, 'estatus_cliente'):
                estatus_expr_base = "TRIM(UPPER(NULLIF(c.estatus_cliente,'')))"
            else:
                estatus_expr_base = "NULL::text"

            estatus_expr_norm = (
                "CASE "
                "WHEN {b} IS NULL OR {b}='' THEN NULL "
                "WHEN regexp_replace({b}, '\\\\s+', ' ', 'g') IN "
                "('ENTREGA PENDIENTE','PENDIENTE DE ENTREGA','PENDIENTE ENTREGA','PENDIEN POR ENTREGA') "
                "OR ({b} LIKE '%%PENDIENT%%' AND {b} LIKE '%%ENTREG%%') THEN 'PENDIENTE POR ENTREGA' "
                "ELSE regexp_replace({b}, '\\\\s+', ' ', 'g') END"
            ).format(b=estatus_expr_base)

            condicion_expr = ("UPPER(TRIM(COALESCE(NULLIF(c.{col},''),'SIN CONDICION')))"
                              .format(col=condicion_col)) if condicion_col else "'SIN CONDICION'"
            cuotas_expr    = "COALESCE(c.{col},0)".format(col=cuotas_col) if cuotas_col else "0"
            fecha_expr     = "DATE(c.{col})".format(col=fecha_col) if fecha_col else "NULL::date"

            # ---- WHERE dinámico (multi) ----
            where=[]; params=[]

            if q_empresas:
                where.append(empresa_expr + " IN (" + ", ".join(["%s"]*len(q_empresas)) + ")")
                params += [' '.join(e.split()).upper() for e in q_empresas]

            if q_estados_plan:
                where.append(estado_expr + " IN (" + ", ".join(["%s"]*len(q_estados_plan)) + ")")
                params += [e.upper() for e in q_estados_plan]

            if q_estatuses:
                where.append(estatus_expr_norm + " IN (" + ", ".join(["%s"]*len(q_estatuses)) + ")")
                params += [e.upper() for e in q_estatuses]

            if q_condiciones and condicion_col:
                where.append(condicion_expr + " IN (" + ", ".join(["%s"]*len(q_condiciones)) + ")")
                params += [c.upper() for c in q_condiciones]

            if q_cuota_buckets and cuotas_col:
                buckets_map = {'1':(1,6),'2':(7,12),'3':(13,24),'4':(25,36)}
                ranges = [buckets_map[b] for b in q_cuota_buckets if b in buckets_map]
                if ranges:
                    where.append("(" + " OR ".join([f"{cuotas_expr} BETWEEN %s AND %s" for _ in ranges]) + ")")
                    for a,b in ranges:
                        params += [a,b]

            if fecha_col:
                if q_desde:
                    where.append(fecha_expr + " >= %s")
                    params.append(q_desde)
                if q_hasta:
                    where.append(fecha_expr + " <= %s")
                    params.append(q_hasta)

            where_sql = "WHERE " + " AND ".join(where) if where else ""

            # ---- Selectores (sin filtros) ----
            cur.execute("""
                SELECT DISTINCT empresa
                FROM clientes
                WHERE empresa IS NOT NULL AND btrim(empresa) <> ''
                ORDER BY 1
            """)
            empresas_opciones = [r.get('empresa') for r in _fetch_dicts(cur)]

            cur.execute("""
                SELECT DISTINCT UPPER(TRIM(estado_del_plan)) AS estado
                FROM clientes
                WHERE estado_del_plan IS NOT NULL AND btrim(estado_del_plan) <> ''
                ORDER BY 1
            """)
            estados_opciones = [r.get('estado') for r in _fetch_dicts(cur)]

            cur.execute("""
                SELECT estatus FROM (
                  SELECT DISTINCT {e} AS estatus
                  FROM clientes c
                  WHERE {e} IS NOT NULL AND {e} <> ''
                  UNION
                  SELECT 'PENDIENTE POR ENTREGA'
                ) x
                ORDER BY 1
            """.format(e=estatus_expr_norm))
            estatus_opciones = [r.get('estatus') for r in _fetch_dicts(cur)]

            cur.execute("SELECT DISTINCT {e} AS condicion FROM clientes c ORDER BY 1".format(e=condicion_expr))
            condicion_opciones = [r.get('condicion') for r in _fetch_dicts(cur)]

            # ---- CTE base ----
            extra_cols=[]
            if show_nombre:    extra_cols.append("c.{0} AS nombre".format(nombre_col))
            if show_apellido:  extra_cols.append("c.{0} AS apellido".format(apellido_col))
            if show_telefono:  extra_cols.append("c.{0} AS telefono".format(telefono_col))
            if show_condicion: extra_cols.append("c.{0} AS condicion_raw".format(condicion_col))
            extra_sql = (", " + ", ".join(extra_cols)) if extra_cols else ""

            base_cte = (
                "WITH base AS ("
                " SELECT c.id, c.empresa, c.estado_del_plan,"
                " {empresa} AS empresa_norm,"
                " {estado}  AS estado_norm,"
                " {estatus} AS estatus_norm,"
                " {condi}   AS condicion_norm,"
                " {cuotas}  AS cuotas_val,"
                " {fecha}   AS fecha_val"
                "{extra}"
                " FROM clientes c {where}"
                ")"
            ).format(
                empresa=empresa_expr, estado=estado_expr, estatus=estatus_expr_norm,
                condi=condicion_expr, cuotas=cuotas_expr, fecha=fecha_expr,
                extra=extra_sql, where=(" " + where_sql if where_sql else "")
            )

            # ---- Orden
            sort_map = {
                'id':'id',
                'empresa':'empresa_norm',
                'estado':'estado_norm',
                'estado_del_plan':'estado_norm',
                'estatus':'estatus_norm',
                'nombre': ('upper(nombre)' if show_nombre else 'id'),
                'apellido': ('upper(apellido)' if show_apellido else 'id'),
                'telefono': ('telefono' if show_telefono else 'id')
            }
            sort_expr = sort_map.get(sort_param, 'id')
            dir_sql   = 'DESC' if dir_param == 'desc' else 'ASC'

            # ====== EXPORTS ======
            if q_export in ('csv','xlsx','pdf'):
                select_cols=[
                    "id","empresa","estado_del_plan","estatus_norm AS estatus",
                    "fecha_val AS fecha_base","cuotas_val AS cuotas"
                ]
                if show_nombre:    select_cols.insert(3, "nombre")
                if show_apellido:  select_cols.insert(4, "apellido")
                if show_telefono:  select_cols.insert(5, "telefono")
                if show_condicion: select_cols.append("condicion_raw AS condicion_pago")

                cur.execute(
                    base_cte + " SELECT {cols} FROM base ORDER BY {s} {d}".format(
                        cols=", ".join(select_cols), s=sort_expr, d=dir_sql
                    ),
                    params
                )
                data = _fetch_dicts(cur)

                if q_export == 'csv':
                    import io, csv
                    si = io.StringIO()
                    csv_cols = [c.split(" AS ")[-1] for c in select_cols]
                    writer = csv.DictWriter(si, fieldnames=csv_cols, extrasaction='ignore')
                    writer.writeheader()
                    for r in data:
                        writer.writerow({k: r.get(k, "") for k in csv_cols})
                    filename = "reporte_metricas_{0}.csv".format(datetime.now().strftime('%Y%m%d_%H%M%S'))
                    return Response(
                        si.getvalue().encode('utf-8-sig'),
                        mimetype='text/csv; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="{0}"'.format(filename)}
                    )

                if q_export == 'xlsx':
                    import pandas as pd
                    from io import BytesIO
                    bio = BytesIO()
                    df = pd.DataFrame(data)
                    with pd.ExcelWriter(bio, engine='openpyxl') as writer:
                        df.to_excel(writer, index=False, sheet_name='Métricas')
                    bio.seek(0)
                    filename = "reporte_metricas_{0}.xlsx".format(datetime.now().strftime('%Y%m%d_%H%M%S'))
                    return send_file(
                        bio, as_attachment=True, download_name=filename,
                        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                    )

                if q_export == 'pdf':
                    from fpdf import FPDF
                    from io import BytesIO

                    def _latin1(x):
                        s = "" if x is None else str(x)
                        try:
                            s.encode("latin-1")
                            return s
                        except UnicodeEncodeError:
                            return s.encode("latin-1", "replace").decode("latin-1")

                    pdf = FPDF(orientation='L', unit='mm', format='A4')
                    pdf.set_auto_page_break(auto=True, margin=10)
                    pdf.add_page()

                    pdf.set_font("Helvetica", "B", 14)
                    pdf.cell(0, 10, _latin1("Reporte de Métricas"), ln=1)

                    pdf.set_font("Helvetica", "", 9)

                    headers = ["ID", "Empresa", "Estado Plan", "Estatus"]
                    widths  = [15, 50, 35, 35]
                    if show_nombre:
                        headers.append("Nombre");   widths.append(35)
                    if show_apellido:
                        headers.append("Apellido"); widths.append(35)
                    if show_telefono:
                        headers.append("Telefono"); widths.append(30)
                    if show_condicion:
                        headers.append("Condicion"); widths.append(35)
                    headers += ["Fecha Base", "Cuotas"]
                    widths  += [30, 20]

                    for h, w in zip(headers, widths):
                        pdf.cell(w, 8, _latin1(h), border=1, align='C')
                    pdf.ln(8)

                    for r in data[:1000]:
                        row = [
                            r.get('id', ''),
                            r.get('empresa', ''),
                            r.get('estado_del_plan', ''),
                            r.get('estatus', '')
                        ]
                        if show_nombre:
                            row.append(r.get('nombre', ''))
                        if show_apellido:
                            row.append(r.get('apellido', ''))
                        if show_telefono:
                            row.append(r.get('telefono', ''))
                        if show_condicion:
                            row.append(r.get('condicion_pago', r.get('condicion', '')))
                        row += [
                            str(r.get('fecha_base') or ''),
                            r.get('cuotas', '')
                        ]

                        for val, w in zip(row, widths):
                            pdf.cell(w, 6, _latin1(str(val))[:40], border=1, align='L')
                        pdf.ln(6)

                    out_data = pdf.output(dest='S')
                    if isinstance(out_data, str):
                        out_data = out_data.encode('latin-1')

                    out = BytesIO(out_data)
                    out.seek(0)
                    filename = "reporte_metricas_{0}.pdf".format(datetime.now().strftime('%Y%m%d_%H%M%S'))
                    return send_file(out, as_attachment=True, download_name=filename, mimetype='application/pdf')

            # ---- Totales / páginas
            cur.execute(base_cte + " SELECT COUNT(*) AS n FROM base", params)
            total_count = int(_fetch_val(cur, 0))
            page_count  = max(1, (total_count + per_page - 1) // per_page)

            # ---- Gráficas
            cur.execute(base_cte + " SELECT estado_norm AS estado, COUNT(*) AS n FROM base GROUP BY 1 ORDER BY 1", params)
            r1 = _fetch_dicts(cur)
            resumen = {x['estado']: x['n'] for x in r1} if r1 else {}
            chart_estados = {'labels': [x['estado'] for x in r1], 'data': [x['n'] for x in r1]}

            cur.execute(base_cte + " SELECT condicion_norm AS condicion, COUNT(*) AS n FROM base GROUP BY 1 ORDER BY 1", params)
            r2 = _fetch_dicts(cur)
            chart_condicion = {'labels': [x['condicion'] for x in r2], 'data': [x['n'] for x in r2]}

            # ---- Datos tabla
            select_cols = ["id","empresa","estado_del_plan","estatus_norm AS estatus"]
            if show_nombre:    select_cols.append("nombre")
            if show_apellido:  select_cols.append("apellido")
            if show_telefono:  select_cols.append("telefono")
            if show_condicion: select_cols.append("condicion_raw AS condicion")

            cur.execute(
                base_cte + " SELECT {cols} FROM base ORDER BY {s} {d} LIMIT %s OFFSET %s".format(
                    cols=", ".join(select_cols), s=sort_expr, d=dir_sql
                ),
                params + [per_page, offset]
            )
            rows = _fetch_dicts(cur)

            if not rows and not (q_empresas or q_estados_plan or q_estatuses or q_condiciones or q_cuota_buckets or q_desde or q_hasta):
                # Salvaguarda sin normalizadores
                cur.execute("SELECT id, empresa, estado_del_plan FROM clientes ORDER BY id ASC LIMIT %s OFFSET %s", (per_page, offset))
                rows = _fetch_dicts(cur)
                cur.execute("SELECT COUNT(*) AS n FROM clientes")
                total_count = int(_fetch_val(cur, 0))
                page_count  = max(1, (total_count + per_page - 1) // per_page)

    except Exception:
        app.logger.error("Fallo en /reportes/metricas\n%s", traceback.format_exc())
        flash("Ocurrió un error generando el reporte. Revisa el log de la aplicación.", "danger")

    return render_template(
        'reporte_metricas.html',
        rows=rows,
        total_count=total_count,
        page=page,
        per_page=per_page,
        page_count=page_count,
        resumen=resumen,
        chart_estados=chart_estados,
        chart_condicion=chart_condicion,
        empresas_opciones=empresas_opciones,
        estados_opciones=estados_opciones,
        condicion_opciones=condicion_opciones,
        estatus_opciones=estatus_opciones,
        show_nombre=show_nombre,
        show_condicion=show_condicion,
        applied={
            'fecha_col': fecha_col_usada,
            'cuotas_col': cuotas_col_usada,
            'sort': sort_param,
            'dir': dir_param
        }
    )

# ===================== /RUTA MÉTRICAS =====================

# =========================
# REPORTE: FLUJO DE CAJA
# =========================
# --- Helper: resumen vacío (debe estar definido ANTES de usarlo) ---
from types import SimpleNamespace
from calendar import monthrange
import traceback

def _resumen_vacio():
    return {
        'balance_general_consolidado_usd': 0.0,
        'EFECTIVO_USD': 0.0,
        'BINANCE_USDT': 0.0,
        'CAJA_BS_USD': 0.0,
        'CAJA_BS_EUR': 0.0,
        'balance_bs_consolidado_bs': 0.0,
        'balance_bs_consolidado_usd': 0.0,
        'acumulado_perdida_devaluacion': 0.0,
        'acumulado_perdida_conversion': 0.0
    }

# =========================
# REPORTE: FLUJO DE CAJA
# =========================
@app.route('/reporte_flujo_caja', methods=['GET', 'POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def reporte_flujo_caja():
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", "error")
        hoy = get_venezuela_current_date().date()
        return render_template(
            'reporte_flujo_caja.html',
            fecha_reporte=hoy.isoformat(),
            tasas_del_dia=SimpleNamespace(usd=0.0, eur=0.0),
            comparativa_proyeccion=None,
            resumen=_resumen_vacio(),
            historial=[]
        )

    # Asegurar que no hay transacción abortada previa
    try:
        conn.rollback()
    except Exception:
        pass

    # Fecha seleccionada o hoy
    hoy_dt = get_venezuela_current_date()
    hoy_date = hoy_dt.date() if isinstance(hoy_dt, datetime) else hoy_dt

    if request.method == 'POST':
        raw = (request.form.get('fecha_reporte') or '').strip()
    else:
        raw = (request.args.get('fecha_reporte') or '').strip()

    try:
        fecha_reporte = datetime.strptime(raw, "%Y-%m-%d").date() if raw else hoy_date
    except Exception:
        fecha_reporte = hoy_date

    primer_dia, ultimo_dia = _first_last_day(datetime(fecha_reporte.year, fecha_reporte.month, 1))

    # Tasas al día (o última previa)
    tasas_del_dia = _fetch_tasas_bcv_al_dia(conn, fecha_reporte)
    if not tasas_del_dia or not hasattr(tasas_del_dia, 'usd'):
        tasas_del_dia = SimpleNamespace(usd=0.0, eur=0.0)

    # ===== Proyecciones con filtros desde el querystring =====
    _build_clientes_filters_fn = globals().get('_build_clientes_filters')
    if not callable(_build_clientes_filters_fn):
        def _build_clientes_filters_fn(_args):
            return "", []
    where_sql, where_params = _build_clientes_filters_fn(request.args)

    # Reset por si quedó abortada antes del cálculo de proyección
    try:
        conn.rollback()
    except Exception:
        pass

    # Proyectado del mes (completo) por empresa, aplicando filtros (con reintento 1 vez)
    try:
        datos_empresas, resumen_mes = _proyeccion_por_empresa(
            conn, primer_dia, ultimo_dia,
            where_sql_extra=where_sql,
            where_params_extra=where_params
        )
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            datos_empresas, resumen_mes = _proyeccion_por_empresa(
                conn, primer_dia, ultimo_dia,
                where_sql_extra=where_sql,
                where_params_extra=where_params
            )
        except Exception:
            datos_empresas, resumen_mes = [], {'proyectado_mes': 0.0}

    # Real a la fecha (desde el 1 del mes hasta fecha_reporte)
    try:
        conn.rollback()
    except Exception:
        pass
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM(p.monto), 0) AS real_a_fecha
            FROM pagos p
            WHERE p.estado_pago = 'Conciliado'
              AND p.fecha_pago BETWEEN %s AND %s
        """, (primer_dia, fecha_reporte))
        real_a_fecha = float((cur.fetchone() or [0])[0] or 0.0)

    ingreso_proyectado_mes = float(resumen_mes.get('proyectado_mes', 0.0))

    # Modelo simple de devaluación (placeholder)
    caja_bs_total = 0.0
    RIESGO_MENSUAL = 0.02
    devaluacion_proyectada_mes = (caja_bs_total / (tasas_del_dia.usd or 1)) * RIESGO_MENSUAL

    dias_del_mes = monthrange(fecha_reporte.year, fecha_reporte.month)[1]
    dias_transcurridos = fecha_reporte.day
    factor = dias_transcurridos / dias_del_mes
    devaluacion_real_a_fecha = devaluacion_proyectada_mes * factor

    proyeccion_restante = max(ingreso_proyectado_mes - real_a_fecha, 0.0)
    comparativa_proyeccion = SimpleNamespace(
        proyectado_mes=ingreso_proyectado_mes,
        real_a_fecha=real_a_fecha,
        proyeccion_restante=proyeccion_restante,
        devaluacion_proyectada_mes=devaluacion_proyectada_mes,
        devaluacion_real_a_fecha=devaluacion_real_a_fecha
    )

    avance_pct = (real_a_fecha / ingreso_proyectado_mes * 100.0) if ingreso_proyectado_mes > 0 else 0.0
    restante_pct = (proyeccion_restante / ingreso_proyectado_mes * 100.0) if ingreso_proyectado_mes > 0 else 0.0
    resumen = SimpleNamespace(
        proyectado_mes=ingreso_proyectado_mes,
        real_a_fecha=real_a_fecha,
        avance_pct=avance_pct,
        restante_pct=restante_pct,
        tasa_usd=tasas_del_dia.usd,
        tasa_eur=tasas_del_dia.eur
    )

    # Historial del día desde operaciones_tesoreria (defensivo)
    historial = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COALESCE(timestamp, fecha_creacion, NOW()) AS ts,
                    tipo_operacion,
                    detalle,
                    monto_ingreso,
                    moneda_ingreso,
                    monto_egreso,
                    moneda_egreso,
                    usuario
                FROM operaciones_tesoreria
                WHERE DATE(COALESCE(timestamp, fecha_creacion, NOW())) = %s
                ORDER BY ts DESC
            """, (fecha_reporte,))
            for r in cur.fetchall():
                get = (r.get if isinstance(r, dict) else lambda k, d=None: getattr(r, k, d))
                historial.append(SimpleNamespace(
                    timestamp=get('ts'),
                    tipo_operacion=get('tipo_operacion', ''),
                    detalle=get('detalle', ''),
                    monto_ingreso=float(get('monto_ingreso', 0) or 0),
                    moneda_ingreso=get('moneda_ingreso', '') or '',
                    monto_egreso=float(get('monto_egreso', 0) or 0),
                    moneda_egreso=get('moneda_egreso', '') or '',
                    usuario=get('usuario', '') or ''
                ))
    except Exception:
        historial = []

    return render_template(
        'reporte_flujo_caja.html',
        fecha_reporte=fecha_reporte.isoformat(),
        tasas_del_dia=tasas_del_dia,
        comparativa_proyeccion=comparativa_proyeccion,
        resumen=resumen,
        historial=historial
    )

@app.route('/reportes/flujo_caja', methods=['GET', 'POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def reporte_flujo_caja_alias():
    return reporte_flujo_caja()

# =================================================================================
# ===== FIN: MÓDULO DE MÉTRICAS RECONSTRUIDO (V1) =====
# =================================================================================

@app.route('/lista_clientes/<string:filtro>')
@admin_required
def lista_clientes(filtro):
    """
    Muestra una lista de clientes filtrada por un estado específico.
    Busca en ambas columnas de estado para máxima compatibilidad.
    """
    conn = get_db()
    clientes = []
    
    if not conn:
        flash("Error de conexión con la base de datos.", "danger")
        return render_template('lista_clientes.html', clientes=clientes, filtro=filtro)

    try:
        with conn.cursor() as cur:
            filtro_upper = filtro.upper()
            query = """
                SELECT id, nombre, apellido, cedula 
                FROM clientes 
                WHERE (TRIM(UPPER(estado_del_plan)) = %s OR TRIM(UPPER(estatus_cliente)) = %s)
                ORDER BY nombre, apellido;
            """
            cur.execute(query, (filtro_upper, filtro_upper))
            clientes = cur.fetchall()

    except psycopg2.Error as e:
        flash(f"Error al buscar clientes: {e}", "danger")
        logging.error(f"Error en lista_clientes con filtro '{filtro}': {traceback.format_exc()}")

    return render_template('lista_clientes.html', clientes=clientes, filtro=filtro.replace('_', ' '))
    
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
                    WHERE TRIM(UPPER(c.estado_del_plan)) = 'AHORRADOR' AND TRIM(UPPER(c.estatus_cliente)) = 'ACTIVO'
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

TOLERANCIA_INFERIOR = 0.92
TOLERANCIA_SUPERIOR = 1.05

def _first_last_day(dt: datetime):
    first = dt.replace(day=1)
    _, days = monthrange(dt.year, dt.month)
    last = dt.replace(day=days)
    return first, last

def _fetch_tasas_bcv_al_dia(conn, dia):
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT usd, eur
                FROM historial_tasas_bcv
                WHERE fecha::date <= %s
                ORDER BY fecha DESC
                LIMIT 1
            """, (dia,))
            row = cur.fetchone()
            if row:
                usd = float(row['usd']) if row['usd'] is not None else 0.0
                eur = float(row['eur']) if row['eur'] is not None else 0.0
                return SimpleNamespace(usd=usd, eur=eur)
    except Exception:
        pass
    return SimpleNamespace(usd=0.0, eur=0.0)

def _proyeccion_por_empresa(conn, first_day, last_day, where_sql_extra=None, where_params_extra=None):
    where_sql_extra = where_sql_extra or ""
    where_params_extra = where_params_extra or []

    # Ejemplo de esquema general (ajusta a tu SQL real)
    # La idea es que where_sql_extra se agregue al WHERE de clientes/pagos.
    sql = f"""
        WITH pagos_mes AS (
            SELECT p.monto, p.fecha_pago, c.empresa
            FROM pagos p
            JOIN clientes c ON c.id = p.cliente_id
            WHERE p.estado_pago = 'Conciliado'
              AND p.fecha_pago BETWEEN %s AND %s
              {where_sql_extra}  -- filtros adicionales
        )
        SELECT
            COALESCE(SUM(CASE WHEN empresa = 'MOTO PLAN' THEN monto END), 0) AS proyectado_motoplan,
            COALESCE(SUM(CASE WHEN empresa = 'CYK' THEN monto END), 0)       AS proyectado_cyk,
            COALESCE(SUM(monto), 0)                                          AS proyectado_mes
        FROM pagos_mes;
    """

    with conn.cursor() as cur:
        cur.execute(sql, [first_day, last_day, *where_params_extra])
        row = cur.fetchone() or {}
        datos_empresas = {
            'MOTO PLAN': float(row.get('proyectado_motoplan', 0) if isinstance(row, dict) else row[0] or 0.0),
            'CYK': float(row.get('proyectado_cyk', 0) if isinstance(row, dict) else row[1] or 0.0)
        }
        resumen_mes = {
            'proyectado_mes': float(row.get('proyectado_mes', 0) if isinstance(row, dict) else row[2] or 0.0)
        }
    return datos_empresas, resumen_mes                     

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
    tasas_de_hoy = {'usd': None, 'eur': None, 'binance': None}
    historial_tasas = []
    
    if not conn:
        flash('Error de conexión a la base de datos.', 'danger')
        return render_template('admin_tasa_bcv.html', tasas_de_hoy=tasas_de_hoy, historial_tasas=historial_tasas, anio_actual=today_date.year)

    try:
        with conn.cursor() as cur:
            if request.method == 'POST':
                tasa_usd_str = request.form.get('tasa_usd', '').replace(',', '.')
                tasa_eur_str = request.form.get('tasa_eur', '').replace(',', '.')
                tasa_binance_str = request.form.get('tasa_binance', '').replace(',', '.')

                if not tasa_usd_str and not tasa_eur_str and not tasa_binance_str:
                    flash('Debe ingresar al menos un valor de tasa para guardar.', 'warning')
                    return redirect(url_for('admin_tasa_bcv'))
                
                tasa_usd = Decimal(tasa_usd_str) if tasa_usd_str else None
                tasa_eur = Decimal(tasa_eur_str) if tasa_eur_str else None
                tasa_binance = Decimal(tasa_binance_str) if tasa_binance_str else None

                # Validación de valores no negativos
                if any(t is not None and t < 0 for t in [tasa_usd, tasa_eur, tasa_binance]):
                    flash('Los valores de las tasas no pueden ser negativos.', 'danger')
                    return redirect(url_for('admin_tasa_bcv'))

                cur.execute("SELECT tasa, tasa_euro, tasa_binance_p2p FROM historial_tasas_bcv WHERE fecha = %s", (today_date,))
                tasa_actual = cur.fetchone()

                final_tasa_usd = tasa_usd if tasa_usd is not None else (tasa_actual['tasa'] if tasa_actual else None)
                final_tasa_eur = tasa_eur if tasa_eur is not None else (tasa_actual['tasa_euro'] if tasa_actual else None)
                final_tasa_binance = tasa_binance if tasa_binance is not None else (tasa_actual['tasa_binance_p2p'] if tasa_actual else None)
                
                sql_upsert = """
                    INSERT INTO historial_tasas_bcv (fecha, tasa, tasa_euro, tasa_binance_p2p, establecida_por_id) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (fecha) DO UPDATE SET 
                        tasa = EXCLUDED.tasa, 
                        tasa_euro = EXCLUDED.tasa_euro, 
                        tasa_binance_p2p = EXCLUDED.tasa_binance_p2p,
                        establecida_por_id = EXCLUDED.establecida_por_id;
                """
                cur.execute(sql_upsert, (today_date, final_tasa_usd, final_tasa_eur, final_tasa_binance, g.admin['id']))
                
                if now_vet.hour >= 17 and now_vet.weekday() < 5: 
                    if now_vet.weekday() == 4: # Si es viernes después de las 5pm
                        for i in range(1, 4):
                            next_day = today_date + timedelta(days=i)
                            cur.execute(sql_upsert, (next_day, final_tasa_usd, final_tasa_eur, final_tasa_binance, g.admin['id']))
                        flash('Tasa de Viernes guardada para todo el fin de semana y el Lunes.', 'success')
                    else: # Cualquier otro día de semana
                        tomorrow_date = today_date + timedelta(days=1)
                        cur.execute(sql_upsert, (tomorrow_date, final_tasa_usd, final_tasa_eur, final_tasa_binance, g.admin['id']))
                        flash('Tasa guardada para hoy y mañana.', 'success')
                else:
                    flash('¡Tasa guardada exitosamente para hoy!', 'success')
                
                conn.commit()
                return redirect(url_for('admin_tasa_bcv'))

            # Lógica para GET
            cur.execute("SELECT tasa, tasa_euro, tasa_binance_p2p FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (today_date,))
            resultado = cur.fetchone()
            if resultado:
                tasas_de_hoy['usd'] = resultado['tasa']
                tasas_de_hoy['eur'] = resultado['tasa_euro']
                tasas_de_hoy['binance'] = resultado['tasa_binance_p2p']
            
            cur.execute("SELECT h.fecha, h.tasa, h.tasa_euro, h.tasa_binance_p2p, a.usuario FROM historial_tasas_bcv h LEFT JOIN administradores a ON h.establecida_por_id = a.id ORDER BY h.fecha DESC LIMIT 30")
            historial_tasas = cur.fetchall()

    except InvalidOperation:
        flash('Por favor, introduce un número válido para las tasas. Use el punto como separador decimal.', 'danger')
    except psycopg2.Error as e:
        if conn: conn.rollback()
        flash(f'Error al procesar la solicitud: {e}', 'danger')
        
    return render_template('admin_tasa_bcv.html', tasas_de_hoy=tasas_de_hoy, historial_tasas=historial_tasas, anio_actual=today_date.year)

# ====== INICIO: REEMPLAZA TU FUNCIÓN DE GESTIÓN DE EGRESOS CON ESTA ======
# ================== Helpers Gestión de Egresos ==================
# Requiere: from types import SimpleNamespace; from datetime import date, datetime, timedelta
#           import re; import psycopg2

WEEKDAY_TO_BYDAY = {0:'MO', 1:'TU', 2:'WE', 3:'TH', 4:'FR', 5:'SA', 6:'SU'}
BYDAY_TO_WEEKDAY = {v:k for k,v in WEEKDAY_TO_BYDAY.items()}

def _weekday_code(d: date) -> str:
    return WEEKDAY_TO_BYDAY[d.weekday()]

def _str_to_byday_set(byday_csv: str):
    if not byday_csv:
        return {'MO'}
    parts = [p.strip().upper() for p in byday_csv.split(',') if p.strip()]
    valid = {'MO','TU','WE','TH','FR','SA','SU'}
    s = set(p for p in parts if p in valid)
    return s or {'MO'}

def _iso_week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"

def _week_bounds(iso_week_key: str):
    # iso_week_key = 'YYYY-Www'
    y_s, w_s = iso_week_key.split('-W')
    y = int(y_s); w = int(w_s)
    # Lunes de esa semana ISO
    start = date.fromisocalendar(y, w, 1)
    end = start + timedelta(days=6)
    return start, end

def _month_bounds(yyyy_mm: str):
    y_s, m_s = yyyy_mm.split('-')
    y = int(y_s); m = int(m_s)
    from calendar import monthrange
    start = date(y, m, 1)
    end = date(y, m, monthrange(y, m)[1])
    return start, end

def _parse_periodo(periodo_tipo: str, periodo: str):
    """Normaliza el periodo de la vista (mensual/semanal) y devuelve:
       (tipo_normalizado, clave, start_date, end_date)"""
    pt = (periodo_tipo or 'mensual').lower()
    hoy = get_venezuela_current_date()
    base = hoy.date() if hasattr(hoy, 'date') else hoy

    if pt == 'semanal':
        if not periodo or not re.match(r'^\d{4}-W\d{2}$', periodo):
            clave = _iso_week_key(base)
        else:
            clave = periodo
        start, end = _week_bounds(clave)
        return 'semanal', clave, start, end
    else:
        pt = 'mensual'
        if not periodo or not re.match(r'^\d{4}-\d{2}$', periodo):
            clave = base.strftime('%Y-%m')
        else:
            clave = periodo
        start, end = _month_bounds(clave)
        return pt, clave, start, end

# ---------------- Generación de ocurrencias ----------------

def _dates_in_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)

def _intersects(a_start: date, a_end: date, b_start: date, b_end: date):
    return not (a_end < b_start or b_end < a_start)

def _should_emit_weekly(d: date, inicio: date, intervalo: int, byday_set: set) -> bool:
    if d < inicio:
        return False
    if WEEKDAY_TO_BYDAY[d.weekday()] not in byday_set:
        return False
    # Semana 0 anclada al lunes de la semana de 'inicio'
    inicio_week_monday = inicio - timedelta(days=inicio.weekday())
    weeks = (d - inicio_week_monday).days // 7
    return weeks % max(1, intervalo) == 0

def generar_ocurrencias_periodo(conn, periodo_tipo: str, clave: str, start: date, end: date):
    """Asegura que existan egresos_ocurrencias dentro del rango [start,end]."""
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            # Traemos egresos activos cuya ventana intersecte el período
            cur.execute("""
                SELECT id, titulo, tipo, frecuencia, intervalo_semana, byday, dia_mes, dias_quincena,
                       monto_base_usd, metodo_referencia,
                       COALESCE(fecha_inicio_recurrencia::date, CURRENT_DATE) AS fecha_inicio_recurrencia,
                       fecha_fin_recurrencia::date AS fecha_fin_recurrencia,
                       COALESCE(estado,'activo') AS estado
                FROM egresos_planificados
                WHERE COALESCE(estado,'activo') IN ('activo')  -- solo activos
            """)
            egresos = cur.fetchall()

            for e in egresos:
                finicio = e['fecha_inicio_recurrencia']
                ffin    = e['fecha_fin_recurrencia'] or date.max
                if not _intersects(finicio, ffin, start, end):
                    continue

                freq = (e['frecuencia'] or 'Unico').capitalize()
                monto = e['monto_base_usd'] or Decimal('0.00')

                fechas_a_crear = []

                if freq == 'Semanal':
                    intervalo = int(e.get('intervalo_semana') or 1)
                    byday_set = _str_to_byday_set(e.get('byday'))
                    # iteramos los días del período y filtramos
                    for d in _dates_in_range(max(start, finicio), min(end, ffin)):
                        if _should_emit_weekly(d, finicio, intervalo, byday_set):
                            fechas_a_crear.append(d)

                elif freq == 'Mensual':
                    try:
                        dia = int(e.get('dia_mes') or 1)
                        dia = max(1, min(31, dia))
                    except Exception:
                        dia = 1
                    # Solo los días que caen dentro del rango
                    mstart = date(start.year, start.month, 1)
                    mend   = date(end.year, end.month, 1)
                    curm = mstart
                    while curm <= mend:
                        from calendar import monthrange
                        last_day = monthrange(curm.year, curm.month)[1]
                        d = date(curm.year, curm.month, min(dia, last_day))
                        if d >= max(start, finicio) and d <= min(end, ffin):
                            fechas_a_crear.append(d)
                        # next month
                        if curm.month == 12:
                            curm = date(curm.year+1, 1, 1)
                        else:
                            curm = date(curm.year, curm.month+1, 1)

                elif freq == 'Quincenal':
                    tokens = []
                    raw = (e.get('dias_quincena') or '1,16')
                    for tok in raw.split(','):
                        tok = tok.strip()
                        if tok.isdigit():
                            d = max(1, min(31, int(tok)))
                            if d not in tokens:
                                tokens.append(d)
                    # meses cubiertos por el rango
                    mstart = date(start.year, start.month, 1)
                    mend   = date(end.year, end.month, 1)
                    curm = mstart
                    while curm <= mend:
                        from calendar import monthrange
                        last_day = monthrange(curm.year, curm.month)[1]
                        for dnum in tokens:
                            d = date(curm.year, curm.month, min(dnum, last_day))
                            if d >= max(start, finicio) and d <= min(end, ffin):
                                fechas_a_crear.append(d)
                        # next month
                        if curm.month == 12:
                            curm = date(curm.year+1, 1, 1)
                        else:
                            curm = date(curm.year, curm.month+1, 1)

                elif freq == 'Anual':
                    # mismo día/mes del inicio, si cae en el rango
                    base_d = finicio
                    for d in _dates_in_range(max(start, finicio), min(end, ffin)):
                        if d.month == base_d.month and d.day == base_d.day:
                            fechas_a_crear.append(d)

                else:  # 'Unico' o variables
                    d = finicio
                    if d >= start and d <= end:
                        fechas_a_crear.append(d)

                # Insertar ocurrencias si no existen
                for d in fechas_a_crear:
                    cur.execute("""
                        SELECT id FROM egresos_ocurrencias
                        WHERE egreso_id=%s AND fecha_programada=%s
                        LIMIT 1
                    """, (e['id'], d))
                    row = cur.fetchone()
                    if not row:
                        cur.execute("""
                            INSERT INTO egresos_ocurrencias
                                (egreso_id, fecha_programada, monto_programado_usd, monto_pagado_usd, estado, created_at)
                            VALUES (%s, %s, %s, %s, %s, NOW())
                        """, (e['id'], d, monto, Decimal('0.00'), 'Pendiente'))
        conn.commit()
    except psycopg2.Error:
        conn.rollback()
        logging.exception("Error generando ocurrencias del período")

# ---------------- Resumen para la plantilla ----------------

def resumen_egresos_periodo(conn, periodo_tipo: str, clave: str):
    """Devuelve un objeto con 'totales' (programado/pagado/pendiente)
       y 'items' (por egreso, con lista de ocurrencias)."""
    if periodo_tipo == 'semanal':
        start, end = _week_bounds(clave)
    else:
        start, end = _month_bounds(clave)

    tot_programado = Decimal('0.00')
    tot_pagado     = Decimal('0.00')

    items = []

    with conn.cursor() as cur:
        # Trae egresos y sus agregados del período
        cur.execute("""
            SELECT ep.id, ep.titulo, ep.frecuencia,
                   COALESCE(SUM(eo.monto_programado_usd),0) AS programado,
                   COALESCE(SUM(eo.monto_pagado_usd),0)     AS pagado
            FROM egresos_planificados ep
            LEFT JOIN egresos_ocurrencias eo
                   ON eo.egreso_id = ep.id
                  AND eo.fecha_programada BETWEEN %s AND %s
            WHERE COALESCE(ep.estado,'activo') IN ('activo')
            GROUP BY ep.id
            ORDER BY ep.titulo
        """, (start, end))
        eg_rows = cur.fetchall()

        for r in eg_rows:
            programado = Decimal(r['programado'] or 0)
            pagado     = Decimal(r['pagado'] or 0)
            pendiente  = max(Decimal('0.00'), programado - pagado)

            # Ocurrencias detalle
            cur.execute("""
                SELECT id AS ocurrencia_id,
                       fecha_programada,
                       COALESCE(monto_programado_usd,0) AS programado,
                       COALESCE(monto_pagado_usd,0)     AS pagado
                FROM egresos_ocurrencias
                WHERE egreso_id=%s
                  AND fecha_programada BETWEEN %s AND %s
                ORDER BY fecha_programada
            """, (r['id'], start, end))
            occs = cur.fetchall()
            occ_list = []
            for o in occs:
                occ_list.append(SimpleNamespace(
                    ocurrencia_id=o['ocurrencia_id'],
                    fecha_programada=o['fecha_programada'],
                    programado=Decimal(o['programado'] or 0),
                    pagado=Decimal(o['pagado'] or 0),
                    pendiente=max(Decimal('0.00'), Decimal(o['programado'] or 0) - Decimal(o['pagado'] or 0))
                ))

            items.append(SimpleNamespace(
                egreso_id=r['id'],
                titulo=r['titulo'],
                frecuencia=r['frecuencia'],
                programado=float(programado),
                pagado=float(pagado),
                pendiente=float(pendiente),
                ocurrencias=occ_list
            ))

            tot_programado += programado
            tot_pagado     += pagado

    totales = SimpleNamespace(
        programado=float(tot_programado),
        pagado=float(tot_pagado),
        pendiente=float(max(Decimal('0.00'), tot_programado - tot_pagado))
    )
    return SimpleNamespace(totales=totales, items=items)
# ================== /Helpers Gestión de Egresos ==================

@app.route('/gestion/egresos', methods=['GET'], endpoint='gestion_egresos')
@admin_required
def gestion_egresos():
    conn = get_db()
    hoy = get_venezuela_current_date()

    # Defaults de período
    periodo_tipo = (request.args.get('periodo_tipo') or 'mensual').lower()
    periodo = request.args.get('periodo')
    if not periodo:
        if periodo_tipo == 'mensual':
            periodo = hoy.strftime('%Y-%m')
        else:
            y, wk, _ = hoy.isocalendar()
            periodo = f"{y}-W{wk:02d}"

    # Helpers
    periodo_tipo, clave, start, end = _parse_periodo(periodo_tipo, periodo)
    generar_ocurrencias_periodo(conn, periodo_tipo, clave, start, end)
    resumen = resumen_egresos_periodo(conn, periodo_tipo, clave)

    # Catálogo de egresos (panel inferior) — Activo primero
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, titulo, tipo, frecuencia, intervalo_semana, byday, dia_mes, dias_quincena,
                   monto_base_usd, metodo_referencia, estado, fecha_inicio_recurrencia, fecha_fin_recurrencia
            FROM egresos_planificados
            ORDER BY (LOWER(COALESCE(estado,'inactivo'))='activo') DESC, titulo
        """)
        rows = cur.fetchall() or []

    def _f(kind):
        k = (kind or '').lower()
        return [r for r in rows if ((r.get('tipo') or '').lower() == k)]

    egresos = {
        'fijos': _f('fijo'),
        'variables': _f('variable'),
        'devoluciones': _f('devolucion')
    }

    return render_template(
        'gestion_egresos.html',
        periodo_tipo=periodo_tipo, periodo=clave, start=start, end=end,
        resumen=resumen, egresos=egresos
    )

@app.route('/gestion/egresos/ocurrencias/<int:occ_id>/prefill-pago', methods=['POST'])
@admin_required
def egreso_prefill_pago(occ_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", "danger")
        return redirect(url_for('gestion_egresos'))

    with conn.cursor() as cur:
        cur.execute("""
            SELECT eo.id, eo.monto_programado_usd, eo.monto_pagado_usd, ep.titulo
            FROM egresos_ocurrencias eo
            JOIN egresos_planificados ep ON ep.id=eo.egreso_id
            WHERE eo.id=%s
        """, (occ_id,))
        row = cur.fetchone()
        if not row:
            flash("Ocurrencia no encontrada.", "warning")
            return redirect(url_for('gestion_egresos'))

    pendiente = max(0.0, float(row['monto_programado_usd']) - float(row['monto_pagado_usd']))
    if pendiente <= 0:
        flash("Esta ocurrencia ya está pagada.", "info")
        return redirect(url_for('gestion_egresos'))

    return redirect(url_for(
        'tesoreria_rebalanceo',
        tipo_operacion='PAGO_GASTO',
        nota=f"Pago {row['titulo']}",
        monto_origen=f"{pendiente:.2f}",
        egreso_ocurrencia_id=occ_id
    ))

# ====== FIN: REEMPLAZO ======

ESTADOS_PLAN_COMPLETOS = [
    'Ahorrador','Adjudicado','Congelado','Retiro','Completado',
    'Reserva','Cobranza Diferida','Diferido en Reserva','Inscrito'
]

def _query_para_bloque(b, insc_col: str | None = None):
    """
    Construye SQL parametrizado para un bloque de simulación.
    - Soporta rango de inscripción (si insc_col no es None).
    - Normaliza estados/condiciones a UPPER para coincidir con catálogos.
    - Condición: usa COALESCE(q.condicion, c.condicion) por si vive en clientes.
    """
    fecha_inicio = (b.get('fecha_inicio') or '').strip() or None
    fecha_fin    = (b.get('fecha_fin') or '').strip() or None
    insc_desde   = (b.get('insc_desde') or '').strip() or None
    insc_hasta   = (b.get('insc_hasta') or '').strip() or None

    empresa      = (b.get('empresa') or '').strip()
    estatus_cli  = (b.get('estatus_cliente') or '').strip().upper() or None
    estatus_cuot = (b.get('estatus_cuota') or '').strip().upper() or None
    excluir_rc   = bool(b.get('excluir_rc'))

    estados     = [e for e in (b.get('estados') or []) if e]
    estados_up  = [e.strip().upper() for e in estados]
    condiciones = [c for c in (b.get('condiciones') or []) if c]
    condiciones_up = [c.strip().upper() for c in condiciones]

    # Nota: si no tienes tabla cuotas, cambia este SELECT por uno de clientes
    # usando c.valor_cuota como aproximación y quita filtros de q.*
    sql = """
      SELECT
        c.cedula, c.nombre, c.apellido,
        c.estado_del_plan AS estado_plan,
        q.numero_cuota, q.fecha_pago, q.monto_usd,
        UPPER(q.estatus_cuota) AS estatus_cuota,
        COALESCE(q.condicion, c.condicion) AS condicion,
        q.metodo_pago
      FROM cuotas q
      JOIN clientes c ON c.id = q.cliente_id
      WHERE 1=1
    """
    params: list = []

    # Rango por fecha de cuota
    if fecha_inicio:
        sql += " AND q.fecha_pago >= %s"
        params.append(fecha_inicio)
    if fecha_fin:
        sql += " AND q.fecha_pago <= %s"
        params.append(fecha_fin)

    # Rango de inscripción (si existe la columna detectada)
    if insc_col and insc_desde and insc_hasta:
        sql += f" AND c.{insc_col} BETWEEN %s AND %s"
        params.extend([insc_desde, insc_hasta])

    # Empresa
    if empresa:
        sql += " AND LOWER(REPLACE(TRIM(c.empresa), ' ', '')) = LOWER(REPLACE(TRIM(%s), ' ', ''))"
        params.append(empresa)

    # Estatus cliente
    if estatus_cli:
        sql += " AND UPPER(c.estatus_cliente) = %s"
        params.append(estatus_cli)

    # Estatus de cuota
    if estatus_cuot:
        sql += " AND UPPER(q.estatus_cuota) = %s"
        params.append(estatus_cuot)

    # Condición (catálogo oficial), preferimos q.condicion pero caemos a c.condicion
    if condiciones_up:
        sql += " AND UPPER(COALESCE(q.condicion, c.condicion)) = ANY(%s)"
        params.append(condiciones_up)

    # Estados del plan (normalizados)
    if estados_up:
        sql += " AND UPPER(c.estado_del_plan) = ANY(%s)"
        params.append(estados_up)

    # Excluir Retiro/Completado
    if excluir_rc:
        sql += " AND UPPER(c.estado_del_plan) NOT IN ('RETIRO','COMPLETADO')"

    sql += " ORDER BY q.fecha_pago ASC, c.apellido ASC, c.nombre ASC"
    return sql, tuple(params)

# =========================
# Helper: detectar columna de inscripción
# =========================
def _detect_column(conn, table: str, candidates: list[str]) -> str | None:
    """Devuelve el primer nombre de columna que exista en la tabla."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name=%s
            """, (table,))
            rows = cur.fetchall() or []
        cols = {(r['column_name'] if isinstance(r, dict) else r[0]) for r in rows}
        for c in candidates:
            if c in cols:
                return c
    except Exception:
        pass
    return None

# =========================
# REPORTE: PROYECCIONES
# ==================================================================

# --------- HELPERS BD: tasas y egresos del período ---------

def _fetch_bcv_anchor(conn, start_date):
    """
    Ancla del período (13→12 o manual): usa la tasa del día 'start_date' si existe;
    si no, toma la más reciente ANTES del inicio.
    Retorna {'usd': <float>, 'fecha': <date>, 'tasa_binance': <float|None>}
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT fecha, bcv_anchor, binance
                FROM tasas_diarias
                WHERE fecha <= %s
                ORDER BY fecha DESC
                LIMIT 1
            """, (start_date,))
            row = cur.fetchone()
            if not row:
                return {'usd': 0.0, 'fecha': start_date, 'tasa_binance': None}
            fecha, bcv_anchor, binance = row
            return {'usd': float(bcv_anchor or 0), 'fecha': fecha, 'tasa_binance': float(binance) if binance is not None else None}
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return {'usd': 0.0, 'fecha': start_date, 'tasa_binance': None}


def _fetch_tasas_now(conn):
    """
    Tasa “actual” (último registro en tasas_diarias).
    Retorna {'usd': <bcv_now>, 'fecha': <date>, 'tasa_binance': <binance>}
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT fecha, bcv_now, binance
                FROM tasas_diarias
                ORDER BY fecha DESC
                LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return {'usd': 0.0, 'fecha': None, 'tasa_binance': 0.0}
            fecha, bcv_now, binance = row
            return {'usd': float(bcv_now or 0), 'fecha': fecha, 'tasa_binance': float(binance or 0)}
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return {'usd': 0.0, 'fecha': None, 'tasa_binance': 0.0}


def _get_egresos_totales_periodo(conn, start_date, end_date):
    """
    Total programado de egresos entre start_date y end_date (solo activos).
    No convierte monedas aquí (solo suma en USD-equivalente conservador):
    - USD / USDT: suma monto_origen como USD
    - VES: convierte a USD con último BCV_now disponible (aprox).
    Retorna {'totales': {'programado': <float>}}
    """
    try:
        bcv_now = float((_fetch_tasas_now(conn) or {}).get('usd') or 0)
        total = 0.0
        with conn.cursor() as cur:
            cur.execute("""
                SELECT moneda_origen, monto_origen
                FROM egresos_programados
                WHERE activo = true
                  AND fecha_compromiso BETWEEN %s AND %s
            """, (start_date, end_date))
            for mon, monto in cur.fetchall() or []:
                mon = (mon or 'USD').upper()
                monto = float(monto or 0)
                if mon in ('USD','USDT'):
                    total += monto
                elif mon == 'VES':
                    total += (monto / bcv_now) if bcv_now > 0 else 0.0
                else:
                    total += monto  # fallback
        return {'totales': {'programado': round(total, 2)}}
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return {'totales': {'programado': 0.0}}

# --------- HELPERS parámetros / FX / VALORACIÓN / COBERTURA ---------

def get_params_map(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM parametros_financieros")
            rows = cur.fetchall() or []
            return {k: v for (k, v) in rows}
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return {}

def _fx_rate_effective(bcv_anchor, bcv_now, tasa_binance, slippage_pct, fijada_global: bool) -> float:
    bcv_anchor = float(bcv_anchor or 0)
    bcv_now    = float(bcv_now or 0)
    binance    = float(tasa_binance or 0)
    bin_adj    = binance * (1 - float(slippage_pct or 0)) if binance > 0 else 0.0
    return bcv_anchor if fijada_global else max(bcv_now, bin_adj, bcv_anchor)

def fx_egreso_rate(corredor, bcv_anchor, bcv_now, binance, slippage_pct, tasa_param, fijada_global: bool):
    corredor = (corredor or '').upper()
    if corredor in ('USD_DIRECTO','USDT_DIRECTO'):
        return 1.0
    if corredor == 'BCV':
        return float(bcv_anchor or 0) if fijada_global else float(bcv_now or 0)
    if corredor == 'BINANCE':
        return float(binance or 0) * (1 - float(slippage_pct or 0))
    if corredor in ('NEGOCIADA','FIJADA','MANUAL'):
        return float(tasa_param or 0)
    # fallback prudente
    bin_adj = float(binance or 0) * (1 - float(slippage_pct or 0))
    return float(bcv_anchor or 0) if fijada_global else max(float(bcv_now or 0), bin_adj, float(bcv_anchor or 0))

def _haircut_row(condicion: str, estatus: str, FACT_CONDICION: dict, FACT_ESTATUS: dict) -> float:
    cond = (condicion or '').strip().upper()
    est  = (estatus or '').strip().upper()
    f1 = float((FACT_CONDICION or {}).get(cond, 0.95))
    f2 = float((FACT_ESTATUS  or {}).get(est,  0.95))
    return max(0.0, min(1.0, f1 * f2))

def valorar_egreso(item: dict, tasas: dict, slippage_pct: float, fijada_global=False) -> dict:
    moneda_origen = (item.get('moneda_origen') or 'USD').upper()
    moneda_pago   = (item.get('moneda_pago') or moneda_origen).upper()
    corredor      = (item.get('corredor_fx') or 'BCV').upper()
    tasa_param    = item.get('tasa_param')

    fx = fx_egreso_rate(
        corredor,
        float(tasas.get('bcv_anchor') or 0),
        float(tasas.get('bcv_now') or 0),
        float(tasas.get('binance')   or 0),
        slippage_pct, tasa_param, fijada_global
    )

    monto = float(item.get('monto_origen') or 0)
    if moneda_origen == 'USD':
        monto_usd  = monto
        monto_ves  = monto * fx if (moneda_pago == 'VES' or corredor in ('BCV','BINANCE','NEGOCIADA','FIJADA','MANUAL')) else 0.0
        monto_usdt = monto if moneda_pago in ('USDT','USD') else 0.0
    elif moneda_origen == 'VES':
        monto_usd  = (monto / fx) if fx > 0 else 0.0
        monto_ves  = monto
        monto_usdt = monto_usd if moneda_pago == 'USDT' else 0.0
    elif moneda_origen == 'USDT':
        monto_usd  = monto
        monto_ves  = monto * fx if moneda_pago == 'VES' else 0.0
        monto_usdt = monto
    else:
        monto_usd = monto; monto_ves = 0.0; monto_usdt = 0.0

    return {
        'fx_aplicada': round(fx, 4),
        'usd_equivalente': round(monto_usd, 2),
        'ves_equivalente': round(monto_ves, 2),
        'usdt_equivalente': round(monto_usdt, 2),
        'moneda_pago': moneda_pago
    }

def cubrir_egresos(egresos_valorados: list, saldos: dict, tasas: dict, slippage_pct: float) -> dict:
    """
    Sencillo plan de cobertura y rebalanceo contra saldos disponibles:
    saldos keys esperadas: 'VES_BANCOS', 'USD_EFECTIVO', 'USDT_BINANCE'
    """
    plan = {'coberturas': [], 'rebalanceos': [], 'deficit': {}}
    egresos_sorted = sorted(egresos_valorados, key=lambda e: (e.get('prioridad',2), str(e.get('fecha_compromiso') or '9999-12-31')))
    bcv = float(tasas.get('bcv_now') or 0)
    bin_adj = float(tasas.get('binance') or 0) * (1 - float(slippage_pct or 0))

    def move(desde, hacia, monto, costo, nota):
        saldos[desde] -= monto
        if desde != hacia:
            saldos[hacia] += (monto - costo)
        plan['rebalanceos'].append({'desde':desde,'hacia':hacia,'monto':monto,'costo':costo,'nota':nota})

    for eg in egresos_sorted:
        mp = eg['moneda_pago']
        covered = False

        if mp == 'VES':
            need = eg['ves_equivalente']
            if saldos['VES_BANCOS'] >= need:
                saldos['VES_BANCOS'] -= need; covered = True
            else:
                falta = need - saldos['VES_BANCOS']
                usdt_req = falta / bin_adj if bin_adj > 0 else 1e18
                fx = eg['fx_aplicada']; usd_req = falta / fx if fx > 0 else 1e18
                if saldos['USDT_BINANCE'] >= usdt_req:
                    saldos['USDT_BINANCE'] -= usdt_req; saldos['VES_BANCOS'] += falta
                    move('USDT_BINANCE','VES_BANCOS', usdt_req, 0, 'USDT→VES p2p')
                    saldos['VES_BANCOS'] -= need; covered = True
                elif saldos['USD_EFECTIVO'] >= usd_req:
                    saldos['USD_EFECTIVO'] -= usd_req; saldos['VES_BANCOS'] += falta
                    move('USD_EFECTIVO','VES_BANCOS', usd_req, 0, 'USD→VES mesa')
                    saldos['VES_BANCOS'] -= need; covered = True

        elif mp == 'USD':
            need = eg['usd_equivalente']
            if saldos['USD_EFECTIVO'] >= need:
                saldos['USD_EFECTIVO'] -= need; covered = True
            else:
                falta = need - saldos['USD_EFECTIVO']
                if saldos['USDT_BINANCE'] >= falta:
                    saldos['USDT_BINANCE'] -= falta; saldos['USD_EFECTIVO'] += falta
                    move('USDT_BINANCE','USD_EFECTIVO', falta, 0, 'USDT→USD')
                    saldos['USD_EFECTIVO'] -= need; covered = True
                else:
                    ves_req = falta * bcv
                    if saldos['VES_BANCOS'] >= ves_req:
                        saldos['VES_BANCOS'] -= ves_req; saldos['USD_EFECTIVO'] += falta
                        move('VES_BANCOS','USD_EFECTIVO', ves_req, 0, 'VES→USD mesa')
                        saldos['USD_EFECTIVO'] -= need; covered = True

        elif mp == 'USDT':
            need = eg['usdt_equivalente']
            if saldos['USDT_BINANCE'] >= need:
                saldos['USDT_BINANCE'] -= need; covered = True
            else:
                falta = need - saldos['USDT_BINANCE']
                if saldos['USD_EFECTIVO'] >= falta:
                    saldos['USD_EFECTIVO'] -= falta; saldos['USDT_BINANCE'] += falta
                    move('USD_EFECTIVO','USDT_BINANCE', falta, 0, 'USD→USDT')
                    saldos['USDT_BINANCE'] -= need; covered = True
                else:
                    ves_req = falta * bin_adj
                    if saldos['VES_BANCOS'] >= ves_req:
                        saldos['VES_BANCOS'] -= ves_req; saldos['USDT_BINANCE'] += falta
                        move('VES_BANCOS','USDT_BINANCE', ves_req, 0, 'VES→USDT p2p')
                        saldos['USDT_BINANCE'] -= need; covered = True

        if not covered:
            key = 'criticos' if (eg.get('obligatoriedad') == 'DURO') else 'diferibles'
            plan['deficit'].setdefault(key, []).append({'egreso_id': eg.get('id'), 'falta': eg})
        else:
            plan['coberturas'].append({'egreso_id': eg.get('id'), 'moneda_pago': mp, 'fx': eg['fx_aplicada']})

    plan['saldos_finales'] = saldos
    return plan

# --------- HELPERS de selectores (flexibles) ---------

def _has_column(conn, table: str, col: str, schema: str = 'public') -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS(
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema=%s AND table_name=%s AND column_name=%s
                ) AS ok
            """, (schema, table, col))
            row = cur.fetchone()
            return bool(row[0] if not isinstance(row, dict) else (row.get('ok') or row.get('exists')))
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return False

def _collect_union(conn, specs, upper: bool = True) -> list[str]:
    # specs: [(table, column[, schema])]
    norm = [(t, c, (s or 'public') if len(x)==3 else 'public')
            for x in specs for t,c,*s in [x]]
    selects = []
    for t, c, sch in norm:
        if _has_column(conn, t, c, sch):
            expr = f"TRIM({c})"
            if upper: expr = f"UPPER({expr})"
            selects.append(f"SELECT {expr} AS v FROM {sch}.{t} WHERE {c} IS NOT NULL AND TRIM({c})<>''")
    if not selects: return []
    sql = "SELECT DISTINCT v FROM (" + " UNION ALL ".join(selects) + ") u WHERE v IS NOT NULL AND v<>'' ORDER BY 1"
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall() or []
    return [r[0] if not isinstance(r, dict) else list(r.values())[0] for r in rows]

def _estatus_normalize_case(base_expr: str) -> str:
    return (
        "CASE "
        f"WHEN {base_expr} IS NULL OR {base_expr}='' THEN NULL "
        f"WHEN regexp_replace({base_expr}, '\\s+', ' ', 'g') IN "
        "('ENTREGA PENDIENTE','PENDIENTE DE ENTREGA','PENDIENTE ENTREGA','PENDIEN POR ENTREGA') "
        f"OR ({base_expr} LIKE '%PENDIENT%' AND {base_expr} LIKE '%ENTREG%') "
        "THEN 'PENDIENTE POR ENTREGA' "
        f"ELSE regexp_replace({base_expr}, '\\s+', ' ', 'g') END"
    )

def _selector_opciones(conn) -> tuple[list[str], list[str], list[str], list[str]]:
    empresas  = _collect_union(conn, [('clientes','empresa')], upper=False)
    estados   = _collect_union(conn, [('clientes','estado_del_plan'), ('clientes','estado_plan')], upper=True)
    condicion = _collect_union(conn, [('clientes','condicion_pago'), ('clientes','condicion'),
                                      ('cuotas','condicion'), ('planes','condicion')], upper=True) or ['SIN CONDICION']
    # Estatus
    with conn.cursor() as cur:
        parts = []
        for table,col in [('clientes','estatus_cliente'),('clientes','estatus')]:
            if _has_column(conn, table, col):
                base = f"TRIM(UPPER({col}))"
                parts.append(f"SELECT {_estatus_normalize_case(base)} AS v FROM public.{table}")
        parts.append("SELECT 'PENDIENTE POR ENTREGA' AS v")
        sql = "SELECT DISTINCT v FROM (" + " UNION ALL ".join(parts) + ") z WHERE v IS NOT NULL AND v<>'' ORDER BY 1"
        cur.execute(sql)
        rows = cur.fetchall() or []
        estatus = [r[0] if not isinstance(r, dict) else list(r.values())[0] for r in rows]
    app.logger.info("proyecciones/selects -> empresas:%d estados:%d condicion:%d estatus:%d",
                    len(empresas),len(estados),len(condicion),len(estatus))
    return empresas, estados, condicion, estatus

# ---------------------------------------------------------------------
# Pipeline de bloques (consulta + agregación)
# ---------------------------------------------------------------------

def _parse_bloques(args) -> list[dict]:
    raw = (args.get('bloques_json') or '').strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            out = []
            for b in data:
                if not isinstance(b, dict):
                    continue
                emp = b.get('empresa') or []
                if isinstance(emp, str):
                    emp = [emp] if emp.strip() else []
                out.append({
                    'empresa': emp,
                    'estado_plan': b.get('estado_plan') or b.get('estados') or [],
                    'condicion': b.get('condicion') or [],
                    'estatus_cliente': b.get('estatus_cliente') or [],
                    'fecha_inicio': b.get('fecha_inicio') or None,
                    'fecha_fin': b.get('fecha_fin') or None,
                    'excluir_rc': bool(b.get('excluir_rc')),
                })
            return out
    except Exception:
        pass
    return []

def _build_block_where(conn, bloque: dict, default_start: date, default_end: date) -> tuple[str, list]:
    has_estado_del_plan = _has_column(conn,'clientes','estado_del_plan')
    has_estado_plan     = _has_column(conn,'clientes','estado_plan')
    estado_col = "COALESCE(cl.estado_del_plan, cl.estado_plan)" if (has_estado_del_plan and has_estado_plan) \
                 else ("cl.estado_del_plan" if has_estado_del_plan else "cl.estado_plan")

    has_cond_pago = _has_column(conn,'clientes','condicion_pago')
    has_cond      = _has_column(conn,'clientes','condicion')
    condicion_col = "COALESCE(cl.condicion_pago, cl.condicion)" if (has_cond_pago and has_cond) \
                    else ("cl.condicion_pago" if has_cond_pago else "cl.condicion")

    has_est_cli = _has_column(conn,'clientes','estatus_cliente')
    has_est     = _has_column(conn,'clientes','estatus')
    estatus_col = "COALESCE(cl.estatus_cliente, cl.estatus)" if (has_est_cli and has_est) \
                  else ("cl.estatus_cliente" if has_est_cli else "cl.estatus")

    where, params = [], []

    fi = bloque.get('fecha_inicio') or default_start
    ff = bloque.get('fecha_fin')    or default_end
    where.append("c.fecha_pago BETWEEN %s AND %s"); params += [fi, ff]

    def _norm(xs):
        if not xs: return []
        if isinstance(xs, str): xs = [xs]
        return [ (x or '').strip().upper() for x in xs if (x or '').strip() ]

    emp = _norm(bloque.get('empresa'))
    if emp:
        where.append("UPPER(TRIM(cl.empresa)) = ANY(%s)"); params.append(emp)

    estados = _norm(bloque.get('estado_plan'))
    if estados and estado_col:
        where.append(f"UPPER(TRIM({estado_col})) = ANY(%s)"); params.append(estados)

    conds = _norm(bloque.get('condicion'))
    if conds and condicion_col:
        where.append(f"UPPER(TRIM({condicion_col})) = ANY(%s)"); params.append(conds)

    ests = _norm(bloque.get('estatus_cliente'))
    if ests and estatus_col:
        where.append(
            f"""CASE 
                   WHEN {estatus_col} IS NULL THEN NULL
                   WHEN regexp_replace(UPPER(TRIM({estatus_col})),'\\s+',' ','g') IN
                        ('ENTREGA PENDIENTE','PENDIENTE DE ENTREGA','PENDIENTE ENTREGA','PENDIEN POR ENTREGA')
                        OR (UPPER({estatus_col}) LIKE '%PENDIENT%' AND UPPER({estatus_col}) LIKE '%ENTREG%')
                   THEN 'PENDIENTE POR ENTREGA'
                   ELSE regexp_replace(UPPER(TRIM({estatus_col})),'\\s+',' ','g')
                END = ANY(%s)"""
        ); params.append([ 'PENDIENTE POR ENTREGA' if ('PENDIENTE' in s and 'ENTREGA' in s) else s for s in ests ])

    if bloque.get('excluir_rc') and estado_col:
        where.append(f"UPPER(TRIM({estado_col})) NOT IN ('RETIRADO','COMPLETADO')")

    return (" AND ".join(where) if where else "TRUE"), params

def _run_block(conn, bloque: dict, idx: int, start_date: date, end_date: date) -> tuple[float, list]:
    where_sql, params = _build_block_where(conn, bloque, start_date, end_date)
    sql = f"""
        SELECT
            cl.id AS cliente_id,
            cl.nombre, cl.apellido, cl.cedula, cl.empresa,
            {("UPPER(TRIM(COALESCE(cl.estado_del_plan, cl.estado_plan)))" if (_has_column(conn,'clientes','estado_del_plan') and _has_column(conn,'clientes','estado_plan'))
                else ("UPPER(TRIM(cl.estado_del_plan))" if _has_column(conn,'clientes','estado_del_plan') else "UPPER(TRIM(cl.estado_plan))"))} AS estado_plan,
            UPPER(TRIM(COALESCE(cl.condicion_pago, cl.condicion))) AS condicion,
            c.numero_cuota, c.fecha_pago, c.monto_usd
        FROM cuotas c
        JOIN clientes cl ON c.cliente_id = cl.id
        WHERE {where_sql}
        ORDER BY c.fecha_pago
        LIMIT 500
    """
    t0 = time.perf_counter()
    total, rows_out = 0.0, []
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() or []
        for r in rows:
            if isinstance(r, dict):
                monto = float(r.get('monto_usd') or 0)
                item = {
                    'cliente_id': r.get('cliente_id'),
                    'nombre': r.get('nombre'), 'apellido': r.get('apellido'),
                    'cedula': r.get('cedula'), 'empresa': r.get('empresa'),
                    'estado_del_plan': r.get('estado_plan'),
                    'condicion': r.get('condicion'),
                    'numero_cuota': r.get('numero_cuota'),
                    'fecha_pago': r.get('fecha_pago'),
                    'monto_usd': monto,
                    'bloque_index': idx,
                }
            else:
                (cliente_id, nombre, apellido, cedula, empresa,
                 estado_plan, condicion, numero_cuota, fecha_pago, monto_usd) = r
                monto = float(monto_usd or 0)
                item = {
                    'cliente_id': cliente_id, 'nombre': nombre, 'apellido': apellido,
                    'cedula': cedula, 'empresa': empresa,
                    'estado_del_plan': estado_plan, 'condicion': condicion,
                    'numero_cuota': numero_cuota, 'fecha_pago': fecha_pago,
                    'monto_usd': monto, 'bloque_index': idx,
                }
            total += monto
            rows_out.append(item)
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        app.logger.exception("proyecciones/run_block error: %s", e)
    dt = (time.perf_counter() - t0) * 1000
    app.logger.info("proyecciones/bloque #%d -> filas:%d, total:$%.2f, %.1fms", idx, len(rows_out), total, dt)
    return total, rows_out

def _procesar_bloques(conn, bloques: list[dict], start_date: date, end_date: date):
    ingresos_totales = 0.0
    resultados_por_bloque = []
    clientes_a_cobrar = []
    seen = set()
    T0 = time.perf_counter()
    for i, b in enumerate(bloques):
        subtot, rows = _run_block(conn, b, i, start_date, end_date)
        ingresos_totales += subtot
        resultados_por_bloque.append({'bloque_index': i, 'ingresos_usd': subtot, 'filas': len(rows)})
        for it in rows:
            key = (it.get('cliente_id'), it.get('numero_cuota'), it.get('fecha_pago'))
            if key in seen:  # evita duplicados entre bloques
                continue
            seen.add(key)
            clientes_a_cobrar.append(it)
    app.logger.info("proyecciones/total -> bloques:%d, filas:%d, total:$%.2f, %.1fms",
                    len(bloques), len(clientes_a_cobrar), ingresos_totales, (time.perf_counter() - T0)*1000)
    return ingresos_totales, resultados_por_bloque, clientes_a_cobrar

# ---------------------------------------------------------------------
# Ruta /reportes/proyecciones
# ---------------------------------------------------------------------

@app.route('/reportes/proyecciones', methods=['GET', 'POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def reporte_proyecciones():
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", "danger")
        return redirect(url_for('gestion_administrativa'))

    # ----------------- Helpers locales -----------------
    from datetime import date as _date, timedelta as _timedelta
    from calendar import monthrange as _monthrange
    from types import SimpleNamespace
    import json, time

    def _ns(obj):
        if isinstance(obj, dict):
            return SimpleNamespace(**{k: _ns(v) for k, v in obj.items()})
        if isinstance(obj, list):
            return [_ns(v) for v in obj]
        return obj

    def __month_add(d: _date, months: int) -> _date:
        y = d.year + (d.month - 1 + months) // 12
        m = (d.month - 1 + months) % 12 + 1
        return _date(y, m, min(d.day, _monthrange(y, m)[1]))

    def __parse_date(s: str | None):
        if not s: return None
        try:
            y, m, d = (int(x) for x in s.strip().split('-'))
            return _date(y, m, d)
        except Exception:
            return None

    def __compute_period(hoy: _date, mode: str, start_s: str, end_s: str) -> tuple[_date, _date]:
        mode = (mode or 'auto').lower()
        if mode == 'manual':
            a = __parse_date(start_s); b = __parse_date(end_s)
            if a and b: return (b, a) if a > b else (a, b)
            if a and not b: return (a, a)
            if b and not a: return (b, b)
        # AUTO 13→12
        if hoy.day >= 13:
            start = _date(hoy.year, hoy.month, 13)
            end   = __month_add(start, 1) - _timedelta(days=1)
        else:
            end   = _date(hoy.year, hoy.month, 12)
            start = __month_add(end, -1) + _timedelta(days=1)
        return (start, end)

    # ----------------- Parámetros del request -----------------
    src = request.form if request.method == 'POST' else request.args

    # Fecha de referencia (usa helper global si existe; si no, date.today)
    _get_today = globals().get('get_venezuela_current_date', None)
    hoy = _get_today() if callable(_get_today) else _date.today()

    period_mode = (src.get('period_mode') or 'auto').lower()
    start_s = (src.get('start_date') or '').strip()
    end_s   = (src.get('end_date') or '').strip()
    dev_target_pct = src.get('dev_target_pct')
    fijada_flag    = (src.get('fijada') or '') == '1'

    bloques_iniciales = _parse_bloques(src)
    simulacion_realizada = bool(bloques_iniciales)

    # Período 13→12 o manual
    start_date, end_date = __compute_period(hoy, period_mode, start_s, end_s)
    period_key = f"{start_date.isoformat()}_{end_date.isoformat()}"

    # Tasas
    tasas_anchor = _fetch_bcv_anchor(conn, start_date)
    tasas_now    = _fetch_tasas_now(conn)

    # -------- FX params (flexibles) --------
    params_map = get_params_map(conn)
    SLIPPAGE_PCT         = float(params_map.get('SLIPPAGE_PCT', 0.006))
    COLCHON_LIQUIDEZ_PCT = float(params_map.get('COLCHON_LIQUIDEZ_PCT', 0.07))
    FACT_CONDICION = params_map.get('FACT_CONDICION', {'SOLVENTE':1.0,'NORMAL':0.98,'MOROSO':0.90,'EN GESTIÓN':0.85})
    FACT_ESTATUS  = params_map.get('FACT_ESTATUS',  {'ACTIVO':1.0,'PENDIENTE POR ENTREGA':0.95,'SUSPENDIDO':0.80})
    DEPRECIACION_MENSUAL = float(params_map.get('DEPRECIACION_MENSUAL_USD', 0.0))

    bcv_anchor = (tasas_anchor or {}).get('usd') or 0.0
    bcv_now    = (tasas_now    or {}).get('usd') or 0.0
    tasa_bin   = (tasas_now    or {}).get('tasa_binance') or 0.0

    # Selectores
    try:
        empresas_opciones, estados_opciones, condicion_opciones, estatus_opciones = _selector_opciones(conn)
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        app.logger.exception("proyecciones/selectores error: %s", e)
        empresas_opciones, estados_opciones, condicion_opciones, estatus_opciones = [], [], [], []

    # Proyecciones / Gastos
    proyecciones = {}
    gastos = {'totales': {'programado': 0.0}}

    if simulacion_realizada:
        try:
            ingresos_totales, resultados_por_bloque, clientes_a_cobrar = _procesar_bloques(
                conn, bloques_iniciales, start_date, end_date
            )
            try:
                gastos = _get_egresos_totales_periodo(conn, start_date, end_date) or {'totales': {'programado': 0.0}}
            except Exception as e:
                try: conn.rollback()
                except Exception: pass
                app.logger.exception("proyecciones/egresos error: %s", e)

            # --- Ingresos ajustados con haircut + colchón y exposición VES ---
            fx_aplicada = _fx_rate_effective(bcv_anchor, bcv_now, tasa_bin, SLIPPAGE_PCT, fijada_flag)

            ingresos_ajustados = 0.0
            exposicion_ves     = 0.0
            for it in clientes_a_cobrar or []:
                monto = float(it.get('monto_usd') or 0)
                factor = _haircut_row(it.get('condicion'), it.get('estado_del_plan'), FACT_CONDICION, FACT_ESTATUS)
                monto_aj = monto * factor
                ingresos_ajustados += monto_aj
                exposicion_ves     += (monto_aj * fx_aplicada)

            colchon_liquidez = ingresos_ajustados * COLCHON_LIQUIDEZ_PCT
            ingreso_caja_proyectado = max(0.0, ingresos_ajustados - colchon_liquidez)

            gastos_programados = float(((gastos or {}).get('totales') or {}).get('programado') or 0)
            balance = ingreso_caja_proyectado - gastos_programados

            proyecciones = {
                'resumen': {
                    'ingresos_totales_proyectados': round(ingreso_caja_proyectado, 2),
                    'balance_neto_proyectado': round(balance, 2),
                    'period_key': period_key,
                    'fx_rate_aplicada': round(fx_aplicada, 4),
                    'colchon_liquidez': round(colchon_liquidez, 2),
                    'exposicion_ves': round(exposicion_ves, 2),
                    'depreciacion_contable': round(DEPRECIACION_MENSUAL, 2),
                },
                'resultados_por_bloque': resultados_por_bloque,
                'clientes_a_cobrar': clientes_a_cobrar,
            }

            # --- Egresos valorados + plan de cobertura contra saldos de caja ---
            egresos_val = []
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id, categoria, descripcion, moneda_origen, monto_origen, moneda_pago, corredor_fx,
                               tasa_param, prioridad, obligatoriedad, recurrencia, fecha_compromiso, cartera_preferida, activo
                        FROM egresos_programados
                        WHERE activo = true
                          AND fecha_compromiso BETWEEN %s AND %s
                    """, (start_date, end_date))
                    rows = cur.fetchall() or []
                    cols = [d[0] for d in cur.description]
                    egresos_raw = [dict(zip(cols, r)) for r in rows]

                tasas_map = {'bcv_anchor': bcv_anchor, 'bcv_now': bcv_now, 'binance': tasa_bin}
                for e in egresos_raw:
                    v = valorar_egreso(e, tasas_map, SLIPPAGE_PCT, fijada_global=fijada_flag)
                    v.update({
                        'id': e.get('id'),
                        'categoria': e.get('categoria'),
                        'descripcion': e.get('descripcion'),
                        'prioridad': e.get('prioridad'),
                        'obligatoriedad': e.get('obligatoriedad'),
                        'fecha_compromiso': e.get('fecha_compromiso'),
                        'cartera_preferida': e.get('cartera_preferida'),
                        'fx_corredor': e.get('corredor_fx')
                    })
                    egresos_val.append(v)

                # saldos del día
                saldos = {'VES_BANCOS':0.0,'USD_EFECTIVO':0.0,'USDT_BINANCE':0.0}
                with conn.cursor() as cur:
                    cur.execute("SELECT cartera, monto FROM caja_saldos WHERE fecha = CURRENT_DATE")
                    for cartera, monto in cur.fetchall() or []:
                        saldos[cartera] = float(monto or 0)

                plan = cubrir_egresos(egresos_val, saldos, tasas_map, SLIPPAGE_PCT)

                usd_sum = sum(e['usd_equivalente'] for e in egresos_val) or 0.0
                ves_sum = sum(e['ves_equivalente'] for e in egresos_val) or 0.0
                fx_blend = round((ves_sum / usd_sum), 4) if usd_sum > 0 else 0.0
                costo_rebalanceo_total = round(sum(m.get('costo',0) for m in plan.get('rebalanceos',[])), 2)

                proyecciones['egresos'] = {
                    'valorados': egresos_val,
                    'fx_blend_egresos': fx_blend,
                    'costo_rebalanceo_total': costo_rebalanceo_total,
                    'plan_cobertura': plan
                }
            except Exception as e:
                try: conn.rollback()
                except Exception: pass
                app.logger.exception("proyecciones/egresos cobertura error: %s", e)

        except Exception as e:
            try: conn.rollback()
            except Exception: pass
            app.logger.exception("proyecciones/procesamiento error: %s", e)
            proyecciones = {
                'resumen': {'ingresos_totales_proyectados': 0.0, 'balance_neto_proyectado': 0.0, 'period_key': period_key},
                'resultados_por_bloque': [], 'clientes_a_cobrar': [],
            }

    # =============== Proyección financiera (escenarios) ===============
    def __to_float(v, default=0.0) -> float:
        try:
            if v is None or (isinstance(v, str) and not v.strip()):
                return float(default)
            return float(v)
        except Exception:
            return float(default)

    def __getlist(name: str) -> list[str]:
        return src.getlist(name) if hasattr(src, 'getlist') else []

    proyeccion_financiera = None
    proyeccion_error = None

    try:
        # Solo corre si tienes implementado calcular_proyeccion
        _calc = globals().get('calcular_proyeccion', None)

        # 1) Ingresos
        ingresos_pf = {
            "solv_imp": {
                "efectivo": __to_float(src.get("ing_efectivo", 0)),
                "euro":     __to_float(src.get("ing_euro", 0)),
                "usdt":     __to_float(src.get("ing_usdt", 0)),
                "nequi":    __to_float(src.get("ing_nequi", 0)),
                "bs_sc":    __to_float(src.get("ing_bs_sc", 0)),
                "bs_cc":    __to_float(src.get("ing_bs_cc", 0)),
            },
            "mora": {
                "efectivo": __to_float(src.get("mora_efectivo", 0)),
                "bs_sc":    __to_float(src.get("mora_bs_sc", 0)),
                "bs_cc":    __to_float(src.get("mora_bs_cc", 0)),
            },
        }

        # 2) Egresos en USD por clave:
        egresos_divisas_pf = {}
        eg_json = src.get("eg_divisas_json")
        if eg_json:
            try:
                parsed = json.loads(eg_json)
                if isinstance(parsed, dict):
                    egresos_divisas_pf = {str(k): __to_float(v, 0.0) for k, v in parsed.items()}
            except Exception:
                pass
        if not egresos_divisas_pf:
            keys = __getlist("eg_div_key[]")
            vals = __getlist("eg_div_monto[]")
            for i, k in enumerate(keys or []):
                k = (k or "").strip()
                if not k:
                    continue
                v = __to_float(vals[i]) if i < len(vals) else 0.0
                if v != 0.0:
                    egresos_divisas_pf[k] = v

        # 3) Ítems del carril 225→247
        carril_items = []
        carril_raw = (src.get("carril_json") or "").strip()
        if carril_raw:
            try:
                parsed = json.loads(carril_raw)
                if isinstance(parsed, list):
                    for it in parsed:
                        if not isinstance(it, dict): 
                            continue
                        concepto = str(it.get("concepto", "")).strip()
                        monto    = __to_float(it.get("monto_usd", 0))
                        venta    = __to_float(it.get("venta", 0))
                        recompra = __to_float(it.get("recompra", 0))
                        if concepto and monto > 0 and venta > 0 and recompra > 0:
                            carril_items.append({
                                "concepto": concepto, "monto_usd": monto, "venta": venta, "recompra": recompra
                            })
            except Exception:
                carril_items = []

        if not carril_items:
            carril_conceptos = __getlist("carril_concepto[]")
            carril_montos    = __getlist("carril_monto_usd[]")
            carril_ventas    = __getlist("carril_venta[]")
            carril_recompras = __getlist("carril_recompra[]")
            for i in range(len(carril_conceptos or [])):
                c = (carril_conceptos[i] or "").strip()
                if not c:
                    continue
                monto = __to_float(carril_montos[i]) if i < len(carril_montos) else 0.0
                venta = __to_float(carril_ventas[i]) if i < len(carril_ventas) else 0.0
                recompra = __to_float(carril_recompras[i]) if i < len(carril_recompras) else 0.0
                if monto > 0 and venta > 0 and recompra > 0:
                    carril_items.append({
                        "concepto": c, "monto_usd": monto, "venta": venta, "recompra": recompra
                    })

        # 4) Egresos en Bs directos (USD-eq)
        egresos_pf = {
            "divisas": egresos_divisas_pf,
            "carril_225_247_items": carril_items,
            "bs_bcv":      __to_float(src.get("eg_bs_bcv", 0)),
            "bs_euro_bcv": __to_float(src.get("eg_bs_euro_bcv", 0)),
            "variables_bs": {
                "devoluciones_bs": __to_float(src.get("eg_dev_bs", 0)),
                "registro":        __to_float(src.get("eg_registro_bs", 0)),
            },
            "variables_usd": {
                "devoluciones_usd": __to_float(src.get("eg_dev_usd", 0)),
            },
        }

        # 5) Tasas del mes (escenarios)
        usd_bcv = float((tasas_now.get('usd') or tasas_anchor.get('usd') or 0.0))
        eur_bcv = float(usd_bcv)
        tasas_pf = {
            "usd_bcv": __to_float(src.get("usd_bcv", usd_bcv), usd_bcv),
            "eur_bcv": __to_float(src.get("eur_bcv", eur_bcv), eur_bcv),
            "binance_actual": __to_float(src.get("binance_actual", tasas_now.get('tasa_binance') or 0.0), 0.0),
            "binance_prevision": __to_float(src.get("binance_prevision", 0), 0.0),
            "venta_usd": __to_float(src.get("venta_usd_global", 0)),
            "recompra_usd": __to_float(src.get("recompra_usd_global", 0)),
        }

        # 6) Parámetros de la proyección
        parametros_pf = {
            "colchon_pct": __to_float(src.get("colchon_pct", 0.20), 0.20),
            "bloque_directo_keys": [x.strip() for x in __getlist("bloque_directo_keys[]") if (x or "").strip()],
            "pagan_por_225_247_keys": [x.strip() for x in __getlist("pagan_por_225_247_keys[]") if (x or "").strip()],
        }

        # 7) ¿Disparamos el cálculo?
        has_any_input = any([
            src.get("ing_efectivo"), src.get("ing_usdt"), src.get("ing_bs_sc"), src.get("mora_efectivo"),
            src.get("eg_dev_usd"), src.get("eg_bs_bcv"), src.get("venta_usd_global"),
            egresos_divisas_pf, carril_items
        ])
        force_run = (src.get("run_proyeccion") == "1")

        if (has_any_input or force_run) and callable(_calc):
            proyeccion_financiera = _calc(ingresos_pf, egresos_pf, tasas_pf, parametros_pf)

    except Exception as e:
        proyeccion_error = str(e)
        app.logger.exception("proyecciones/proyeccion_financiera error: %s", e)
    # =============== FIN bloque de escenarios ===============

    # Empaquetado para Jinja
    tasas_ns = _ns({'bcv_anchor': tasas_anchor, 'bcv_now': tasas_now})
    proyecciones_ns = _ns(proyecciones) if proyecciones else None
    gastos_ns = _ns(gastos)
    parametros_view = {
        'period_mode': period_mode,
        'start_date': start_date.isoformat(),
        'end_date': end_date.isoformat(),
        'dev_target_pct': dev_target_pct,
        'fijada': '1' if fijada_flag else '0',
        'period_key': period_key,
    }

    return render_template(
        'reporte_proyecciones.html',
        proyecciones=proyecciones_ns,
        gastos=gastos_ns,
        tasas=tasas_ns,
        empresas_opciones=empresas_opciones,
        estados_opciones=estados_opciones,
        condicion_opciones=condicion_opciones,
        estatus_opciones=estatus_opciones,
        simulacion_realizada=simulacion_realizada,
        parametros=parametros_view,
        bloques_iniciales=bloques_iniciales,
        proyeccion_financiera=proyeccion_financiera,
        proyeccion_error=proyeccion_error,
    )

# =========================
# FIN REPORTE: PROYECCIONES
# =========================

# =================================================================================
# --- GESTIÓN DE CLIENTES Y PAGOS ---
# =================================================================================
# ... (todo tu código anterior de app.py) ...

@app.route('/cliente/<int:cliente_id>')
@admin_required
def perfil_cliente(cliente_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", "error")
        return redirect(url_for('consulta'))

    try:
        with conn.cursor() as cur:
            # 1. Obtener datos principales del cliente
            cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s", (cliente_id,))
            cliente = cur.fetchone()
            if not cliente:
                flash("Cliente no encontrado.", "error")
                return redirect(url_for('consulta'))

            # 2. Obtener listas de datos relacionados (pagos, ofertas, gestiones)
            cur.execute("SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha_creacion DESC", (cliente_id,))
            pagos_raw = cur.fetchall()

            cur.execute("SELECT * FROM ofertas WHERE cliente_id = %s ORDER BY fecha_oferta DESC", (cliente_id,))
            ofertas_raw = cur.fetchall()

            cur.execute("""
                SELECT g.*, a.usuario as gestor_nombre FROM gestiones_cobranza g
                LEFT JOIN administradores a ON g.gestor_id = a.id
                WHERE g.cliente_id = %s ORDER BY g.fecha_creacion DESC;
            """, (cliente_id,))
            gestiones_raw = cur.fetchall()

            # 3. Procesar los datos para formatear fechas y añadir información útil
            pagos_procesados = []
            for pago in pagos_raw:
                pago_dict = dict(pago)
                if pago_dict.get('fecha_pago'):
                    pago_dict['fecha_pago_formateada'] = pago_dict['fecha_pago'].strftime('%d/%m/%Y')
                else:
                    pago_dict['fecha_pago_formateada'] = 'Fecha no disponible'
                pagos_procesados.append(pago_dict)

            ofertas_procesadas = []
            for oferta in ofertas_raw:
                oferta_dict = dict(oferta)
                if oferta_dict.get('fecha_oferta'):
                    oferta_dict['fecha_oferta_formateada'] = oferta_dict['fecha_oferta'].strftime('%d/%m/%Y')
                else:
                    oferta_dict['fecha_oferta_formateada'] = 'Fecha no disponible'
                ofertas_procesadas.append(oferta_dict)

            gestiones_procesadas = []
            for gestion in gestiones_raw:
                gestion_dict = dict(gestion)
                if gestion_dict.get('fecha_creacion'):
                    gestion_dict['fecha_creacion_formateada'] = gestion_dict['fecha_creacion'].strftime('%d/%m/%Y a las %I:%M %p')
                else:
                    gestion_dict['fecha_creacion_formateada'] = 'Fecha no disponible'
                gestiones_procesadas.append(gestion_dict)

            # 4. Preparar el objeto final del cliente con los conteos
            cliente_dict = dict(cliente)
            cliente_dict['conteo_pagos'] = len(pagos_procesados)
            cliente_dict['conteo_ofertas'] = len(ofertas_procesadas)
            cliente_dict['conteo_gestiones'] = len(gestiones_procesadas)
            
            # --- INICIO DE LA CORRECCIÓN ---
            # 5. Renderizar la plantilla y añadir cabeceras anti-caché
            response = make_response(render_template(
                'cliente_perfil.html',
                cliente=cliente_dict,
                pagos=pagos_procesados,
                ofertas=ofertas_procesadas,
                gestiones=gestiones_procesadas,
                admin_rol=g.admin['rol']
            ))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            return response
            # --- FIN DE LA CORRECCIÓN ---

    except Exception as e:
        # Manejo de errores para cualquier problema inesperado durante el proceso
        logging.error(f"Error CRÍTICO al cargar perfil del cliente {cliente_id}: {e}\n{traceback.format_exc()}")
        flash("Ocurrió un error grave al cargar el perfil del cliente. El problema ha sido registrado.", "error")
        return redirect(url_for('consulta'))

    # 4. Se pasan las nuevas variables (`cliente_dict` y `ofertas`) a la plantilla.
    return render_template('cliente_perfil.html',
                           cliente=cliente_dict, # Usamos el diccionario modificado
                           pagos=pagos_procesados,
                           ofertas=ofertas,      # Añadimos la lista de ofertas
                           gestiones=gestiones,
                           historial_eventos=historial_eventos,
                           anio_actual=get_venezuela_current_date().year,
                           admin_rol=g.admin['rol'])

@app.route('/api/bulk_detalle/<int:bulk_id>')
@admin_required
def get_bulk_detalle(bulk_id):
    conn = get_db()
    if not conn:
        return jsonify({'error': 'Error de conexión'}), 500
    try:
        with conn.cursor() as cur:
            # Obtiene los detalles del proceso de inconsistencia
            cur.execute("SELECT * FROM payment_bulks WHERE id = %s", (bulk_id,))
            bulk = cur.fetchone()
            if not bulk:
                return jsonify({'error': 'Proceso no encontrado'}), 404

            # Obtiene los pagos asociados
            cur.execute("SELECT id, fecha_pago, monto, monto_bs, tasa_dia, referencia, estado_reporte FROM pagos WHERE bulk_id = %s ORDER BY fecha_creacion ASC", (bulk_id,))
            pagos = cur.fetchall()

            # Formatea la respuesta como JSON
            bulk_dict = {k: str(v) if isinstance(v, (Decimal, datetime, date)) else v for k, v in dict(bulk).items()}
            pagos_list = [{k: str(v) if isinstance(v, (Decimal, datetime, date)) else v for k, v in dict(p).items()} for p in pagos]
            
            return jsonify({
                'bulk': bulk_dict,
                'pagos': pagos_list
            })
    except psycopg2.Error as e:
        logging.error(f"Error en API get_bulk_detalle: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/agregar_gestion/<int:cliente_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora', 'asistente')
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
                cur.execute("INSERT INTO gestiones_cobranza (cliente_id, gestor_id, tipo_gestion, nota) VALUES (%s, %s, %s, %s)", 
                            (cliente_id, g.admin['id'], tipo_gestion, nota.strip()))
                
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

# ---------- REGISTRAR (GET) ----------
@app.route('/registrar', methods=['GET'])
@admin_required
def registrar():
    conn = get_db()
    if not conn:
        flash('Error de conexión a la base de datos.', 'error')
        return redirect(url_for('hub'))

    # Defaults seguros
    admins_por_rol = {'superadmin': [], 'gerente': [], 'asesor': []}
    todos_los_admins = []
    asesores = []

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT id, nombre_completo, rol, es_comercial
                FROM public.administradores
                WHERE es_comercial = TRUE
                ORDER BY nombre_completo
            """)
            filas = cur.fetchall()  # <- este es el origen canónico

            # Lista “asesores” (para selects simples)
            asesores = [{'id': r['id'], 'nombre': r['nombre_completo']} for r in filas]

            # Lista “todos_los_admins” para compatibilidad con otras plantillas
            todos_los_admins = [{'id': r['id'], 'nombre': r['nombre_completo']} for r in filas]

            # Agrupación por rol (solo roles reconocidos)
            admins_por_rol = {'superadmin': [], 'gerente': [], 'asesor': []}
            for r in filas:
                rol = (r['rol'] or '').strip().lower()
                if rol in admins_por_rol:
                    admins_por_rol[rol].append({
                        'id': r['id'],
                        'nombre': r['nombre_completo']
                    })

    except psycopg2.Error as e:
        logging.exception("Error al cargar datos de /registrar")
        flash(f"Error al cargar los datos para el formulario: {e}", "error")

    return render_template(
        'registrar.html',
        admins_por_rol=admins_por_rol,
        todos_los_admins=todos_los_admins,
        asesores=asesores  # <- expone la lista para el selector
    )

# ---------- COMISIONES (helpers y lógica) ----------
from decimal import Decimal, ROUND_HALF_UP

# Reglas por defecto (hoy no se usan de forma directa, pero las dejamos por si luego parametrizas)
REGLAS_COMISIONES_DEFECTO = {
    'asesor':     {'ASESOR': Decimal('1.00')},
    'gerente':    {'GERENCIA': Decimal('1.00')},
    'superadmin': {'PRESIDENCIA': Decimal('1.00')},
}
# === Reglas de comisiones en memoria (editable desde el dashboard) ===
COMISIONES_REGLAS_DEFAULT = {
    "asesor":     {"pct_asesor": 2.00, "pct_gerencia": 1.00, "pct_pres_a": 3.00, "pct_pres_b": 3.00, "pct_cerrador_extra": 0.40},
    "gerente":    {"pct_asesor": 0.00, "pct_gerencia": 1.00, "pct_pres_a": 5.50, "pct_pres_b": 5.50, "pct_cerrador_extra": 0.40},
    "superadmin": {"pct_asesor": 0.00, "pct_gerencia": 0.50, "pct_pres_a": 5.50, "pct_pres_b": 5.50, "pct_cerrador_extra": 0.40},
}

# Copia viva (se modifica vía POST /comisiones/config)
COMISIONES_REGLAS = {k: dict(v) for k, v in COMISIONES_REGLAS_DEFAULT.items()}

def _es_superadmin_actual():
    # Usa flask_login si está disponible; si no, cae a session/g
    rol = None
    try:
        from flask_login import current_user  # importa local para no romper si no lo usas
        rol = getattr(current_user, "rol", None)
    except Exception:
        pass
    if not rol:
        rol = (session.get("rol") or g.get("rol"))
    return (rol or "").strip().lower() == "superadmin"

def _q2(v, default='0'):
    """
    Redondea a 2 decimales con HALF_UP. Soporta None y cadenas con números.
    """
    s = default if v in (None, '') else str(v)
    return Decimal(s).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def calcular_y_guardar_comisiones(conn, cliente_info):
    """
    Aplica reglas dinámicas desde la cache en memoria.
    - 'extra_cerrador_pct' se paga al cerrador solo si es distinto al asesor responsable.
    - Inserta filas en public.comisiones con tipo='COMISION'.
    - No hay bonos fijos en USD.
    """
    from decimal import Decimal, ROUND_HALF_UP
    def q2(x): return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    reglas = get_reglas_comisiones()  # <-- lee cache

    P = Decimal(str(cliente_info.get('plan_contratado') or 0))
    if P <= 0:
        return

    fecha_dia   = datetime.now(VENEZUELA_TZ).date()
    origen_id   = cliente_info.get('cliente_id')
    moneda      = cliente_info.get('moneda_pago') or 'USD'
    escenario   = (cliente_info.get('escenario') or 'asesor').strip().lower()

    asesor_id   = cliente_info.get('asesor_id')
    cerrador_id = cliente_info.get('cerrador_id')
    gerente_id  = cliente_info.get('gerente_id')
    pres_a_id   = cliente_info.get('presidencia_a_id')
    pres_b_id   = cliente_info.get('presidencia_b_id')

    # Mapa rol -> beneficiario_id
    def id_por_rol(rol: str):
        rol = (rol or '').upper()
        if rol == "ASESOR":         return asesor_id
        if rol == "GERENCIA":       return gerente_id
        if rol == "PRESIDENCIA_A":  return pres_a_id
        if rol == "PRESIDENCIA_B":  return pres_b_id
        return None

    # Porcentajes del escenario
    escenario_map = reglas.get(escenario, {})
    partes = []  # (beneficiario_id, pct, etiqueta)
    for rol, pct in escenario_map.items():
        bid = id_por_rol(rol)
        pct = Decimal(str(pct or 0))
        if bid and pct > 0:
            partes.append((bid, pct, rol))

    # Extra para el cerrador si es distinto al asesor
    extra_pct = Decimal(str(reglas.get("extra_cerrador_pct", 0)))
    if extra_pct > 0 and cerrador_id and asesor_id and cerrador_id != asesor_id:
        partes.append((cerrador_id, extra_pct, "CERRADOR_EXTRA"))

    # Si quedara todo vacío por alguna razón, evita crash y no inserta nada
    if not partes:
        return

    total_pct = sum(p for (_, p, _) in partes)

    with conn.cursor() as cur:
        for beneficiario, pct, etiqueta in partes:
            monto        = q2(P * (pct / Decimal('100')))
            pct_comision = pct
            pct_split    = q2((pct / total_pct) * Decimal('100')) if total_pct > 0 else Decimal('0.00')

            cur.execute("""
                INSERT INTO public.comisiones
                    (origen_id, origen_tipo, asesor_id, pct_comision, pct_split, base, monto, moneda, estado, fecha_origen, notas, tipo)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, 'pendiente', %s, %s, 'COMISION')
            """, (
                origen_id, 'Venta', beneficiario,
                pct_comision, pct_split, P, monto, moneda,
                fecha_dia, f"Escenario={escenario}; {etiqueta}"
            ))

# ================== FIN COMISIONES (GLOBAL) ===================================

@app.route('/finalizar_registro', methods=['POST'])
@admin_required
def finalizar_registro():
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('registrar'))

    form_data = {k: v.strip() if isinstance(v, str) else v for k, v in request.form.items()}

    # Subida de fotos
    foto_cliente_base64 = form_data.get('foto_cliente')
    foto_cedula_base64  = form_data.get('foto_cedula')
    ruta_s3_cliente = None
    ruta_s3_cedula  = None
    cedula_cliente_limpia = (form_data.get('cedula', '') or '').replace(' ', '').replace('.', '')

    if foto_cliente_base64 and str(foto_cliente_base64).startswith('data:image'):
        nombre_archivo_s3 = f"documentos/{cedula_cliente_limpia}/foto_cliente_{int(datetime.now().timestamp())}.jpg"
        if subir_archivo_a_s3(foto_cliente_base64, nombre_archivo_s3):
            ruta_s3_cliente = nombre_archivo_s3
        else:
            flash("Error crítico al subir la foto del cliente a S3. El registro ha sido cancelado.", "danger")
            return redirect(url_for('registrar'))

    if foto_cedula_base64 and str(foto_cedula_base64).startswith('data:image'):
        nombre_archivo_s3 = f"documentos/{cedula_cliente_limpia}/foto_cedula_{int(datetime.now().timestamp())}.jpg"
        if subir_archivo_a_s3(foto_cedula_base64, nombre_archivo_s3):
            ruta_s3_cedula = nombre_archivo_s3
        else:
            flash("Error crítico al subir la foto de la cédula a S3. El registro ha sido cancelado.", "danger")
            return redirect(url_for('registrar'))

    try:
        # Import local para evitar NameError de InvalidOperation si no está en módulo
        from decimal import Decimal, InvalidOperation

        firma_cliente  = form_data.get('firma_cliente')
        firma_empresa  = form_data.get('firma_empresa')
        if not firma_cliente or not firma_empresa:
            flash('Ambas firmas son obligatorias para registrar al cliente.', 'error')
            return redirect(url_for('registrar'))

        # Helper: '1.400,00' -> Decimal('1400.00')
        def to_decimal(s: str) -> Decimal:
            s = (s or '0').strip()
            s = s.replace('.', '').replace(',', '.')
            return Decimal(s)

        def to_int(s):
            try:
                return int(s) if s not in (None, '',) else None
            except ValueError:
                return None

        # Lecturas estandarizadas
        plan_valor        = to_decimal(form_data.get('plan_contratado'))
        inscripcion_valor = to_decimal(form_data.get('inscripcion_monto'))
        valor_cuota_valor = to_decimal(form_data.get('valor_cuota'))

        escenario         = form_data.get('escenario_dueño')  # 'superadmin' | 'gerente' | 'asesor'
        asesor_id         = to_int(form_data.get('asesor_id'))       # dueño/beneficiario
        cerrador_id       = to_int(form_data.get('cerrado_por_id'))  # responsable del cierre
        responsable_cierre = cerrador_id

        # Inserción del cliente
        with conn.cursor() as cur:
            nombre_completo = (form_data.get('nombre_apellido', '') or '').split(' ', 1)
            nombre   = nombre_completo[0] if nombre_completo and nombre_completo[0] else ''
            apellido = nombre_completo[1] if len(nombre_completo) > 1 else ''

            beneficiario_completo  = (form_data.get('beneficiario_nombre_apellido', '') or '').split(' ', 1)
            beneficiario_nombre    = beneficiario_completo[0] if beneficiario_completo and beneficiario_completo[0] else ''
            beneficiario_apellido  = beneficiario_completo[1] if len(beneficiario_completo) > 1 else ''

            insert_dict = {
                'nombre': nombre,
                'apellido': apellido,
                'cedula': cedula_cliente_limpia,
                'cuotas_pagadas_progresivas': 0,
                'cuotas_pagadas_regresivas': 0,
                'firma_digital': firma_cliente,
                'firma_empresa': firma_empresa,
                'fecha_firma': datetime.now(VENEZUELA_TZ),
                'estado_del_plan': 'RESERVA',
                'ruta_foto_cliente_s3': ruta_s3_cliente,
                'ruta_foto_cedula_s3': ruta_s3_cedula,
                'estatus_cliente': 'ACTIVO',
                'beneficiario_nombre': beneficiario_nombre,
                'beneficiario_apellido': beneficiario_apellido,
                'numero_contrato': form_data.get('numero_contrato'),
                'numero_telefono': form_data.get('telefono'),
                'responsable': responsable_cierre,  # quien cerró
                'fecha_ingreso': form_data.get('fecha_ingreso'),
                'grupo': form_data.get('grupo'),
                'plan_contratado': plan_valor,
                'cuotas_totales': to_int(form_data.get('cuotas_totales')) or 0,
                'moneda_pago': form_data.get('moneda_pago'),
                'valor_cuota': valor_cuota_valor,
                'inscripcion_monto': inscripcion_valor,
                'ciclo_cobranza': form_data.get('ciclo_cobranza'),
                'direccion': form_data.get('direccion'),
                'email': form_data.get('email'),
                'beneficiario_cedula': form_data.get('beneficiario_cedula'),
                'beneficiario_telefono': form_data.get('beneficiario_telefono'),
                'beneficiario_email': form_data.get('beneficiario_email'),
                'beneficiario_direccion': form_data.get('beneficiario_direccion'),
                'asesor': asesor_id,  # dueño/beneficiario
            }

            columns = list(insert_dict.keys())
            values  = [insert_dict[c] for c in columns]
            query   = f"INSERT INTO clientes ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(values))}) RETURNING id"
            cur.execute(query, values)
            new_client_id = cur.fetchone()[0]

        # --- COMISIONES ---
        # Payload base
        cliente_info = {
            'cliente_id': new_client_id,
            'plan_contratado': plan_valor,
            'inscripcion_monto': inscripcion_valor,
            'escenario': (escenario or 'asesor').lower(),  # 'asesor' | 'gerente' | 'superadmin'
            'asesor_id': asesor_id,
            'cerrador_id': cerrador_id,
            'moneda_pago': form_data.get('moneda_pago') or 'USD',
        }

        # Beneficiarios (Presidencia A/B y Gerencia) desde parámetros
        benef = resolver_beneficiarios(conn, asesor_id)
        if benef:
            cliente_info.update({
                'gerente_id':       benef.get('gerente_id'),
                'presidencia_a_id': benef.get('presidencia_a_id'),
                'presidencia_b_id': benef.get('presidencia_b_id'),
            })

        # Calcula y graba comisiones (incluye 0.4% al cerrador externo y bono $5 según reglas)
        calcular_y_guardar_comisiones(conn, cliente_info)
        # --- FIN COMISIONES ---

        # Caja: registra inscripción si corresponde
        if inscripcion_valor > 0:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO caja_inscripciones (numero_contrato, cliente_id, monto_inscripcion, responsable_cierre)
                    VALUES (%s, %s, %s, %s)
                """, (form_data.get('numero_contrato'), new_client_id, inscripcion_valor, responsable_cierre))

        # Auditoría
        descripcion_audit = (
            f"Registró y firmó contrato para nuevo cliente: {form_data.get('nombre_apellido')} "
            f"(C.I. {cedula_cliente_limpia})."
        )
        registrar_accion_auditoria('REGISTRO_CLIENTE_FIRMADO', descripcion_audit, new_client_id)

        # Commit final (cliente + comisiones + caja + auditoría)
        conn.commit()
        flash(f"¡Cliente {form_data.get('nombre_apellido')} registrado exitosamente como RESERVA!", 'success')
        return redirect(url_for('consulta', busqueda=form_data.get('cedula')))

    except psycopg2.IntegrityError:
        conn.rollback()
        flash(
            f"Registro fallido: La cédula '{form_data.get('cedula')}' o el N° de Contrato '{form_data.get('numero_contrato')}' ya existen.",
            'error'
        )
        return redirect(url_for('registrar'))
    except (psycopg2.Error, ValueError, ConnectionError, InvalidOperation) as e:
        conn.rollback()
        logging.error(f"Error en finalizar_registro: {e}", exc_info=True)
        flash(f"Registro fallido: Ocurrió un error inesperado: {e}", 'error')
        return redirect(url_for('registrar'))

@app.route('/subir_documento/<int:cliente_id>', methods=['POST'])
@admin_required
def subir_documento(cliente_id):
    if 'documento' not in request.files:
        flash('No se seleccionó ningún archivo.', 'danger')
        return redirect(url_for('consulta'))

    archivo = request.files['documento']
    descripcion = request.form.get('descripcion', archivo.filename)

    if archivo.filename == '':
        flash('No se seleccionó ningún archivo.', 'danger')
        return redirect(url_for('consulta'))

    conn = get_db()
    if not conn:
        flash('Error de conexión a la base de datos.', 'danger')
        return redirect(url_for('consulta'))

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cedula FROM clientes WHERE id = %s", (cliente_id,))
            cliente = cur.fetchone()
            if not cliente:
                flash('Cliente no encontrado.', 'danger')
                return redirect(url_for('consulta'))

            # Crear un nombre de archivo seguro y único
            timestamp = int(get_venezuela_current_datetime().timestamp())
            nombre_seguro = f"{timestamp}_{archivo.filename.replace(' ', '_')}"
            ruta_en_s3 = f"documentos/{cliente['cedula']}/{nombre_seguro}"
            
            bucket_name = os.environ.get('AWS_STORAGE_BUCKET_NAME')
            if not bucket_name:
                 flash("Bucket de S3 no configurado en el servidor.", "danger")
                 return redirect(url_for('consulta'))

            if subir_fileobj_a_s3(archivo, bucket_name, ruta_en_s3):
                # Guardar registro en la base de datos
                cur.execute("""
                    INSERT INTO documentos_cliente 
                    (cliente_id, nombre_archivo, ruta_s3, tipo_archivo, subido_por_id)
                    VALUES (%s, %s, %s, %s, %s)
                """, (cliente_id, descripcion, ruta_en_s3, archivo.content_type, g.admin['id']))
                
                conn.commit()
                registrar_accion_auditoria(
                    'SUBIDA_DOCUMENTO', 
                    f"Subió el archivo '{descripcion}' al expediente del cliente.",
                    cliente_id=cliente_id
                )
                flash('¡Archivo subido y añadido al expediente exitosamente!', 'success')
            else:
                flash('Error crítico al intentar subir el archivo a S3.', 'danger')

    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Error de base de datos: {e}", "danger")
    
    return redirect(url_for('consulta', busqueda=cliente['cedula']))
# --- FIN DE LA NUEVA RUTA ---

@app.route('/consulta', methods=['GET', 'POST'])
@admin_required
def consulta():
    clientes_encontrados = []
    termino_busqueda_raw = request.form.get('busqueda', request.args.get('busqueda', ''))
    termino_busqueda = termino_busqueda_raw.strip()

    # nuevos filtros
    estatus_q = (request.args.get('estatus') or '').strip().upper()
    estado_q  = (request.args.get('estado') or '').strip().upper()
    cond_q    = (request.args.get('condicion') or '').strip().upper()
    inc_q     = request.args.get('inconsistentes')

    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", "error")
    else:
        try:
            with conn.cursor() as cur:
                where = []
                params = []

                # búsqueda por cédula/nombre/contrato
                if termino_busqueda:
                    where.append("""(
                        c.cedula = %s OR
                        (c.nombre || ' ' || c.apellido) ILIKE %s OR
                        c.numero_contrato ILIKE %s
                    )""")
                    like = f"%{termino_busqueda}%"
                    params += [termino_busqueda, like, like]

                # estatus
                if estatus_q:
                    if estatus_q == 'RETIRO':
                        where.append("TRIM(UPPER(c.estatus_cliente)) IN ('RETIRO','RETIRADO','RETIRADA','RETIRADOS')")
                    elif estatus_q == 'ACTIVO':
                        where.append("TRIM(UPPER(c.estatus_cliente)) IN ('ACTIVO','ACTIVOS')")
                    elif estatus_q == 'INACTIVO':
                        where.append("TRIM(UPPER(c.estatus_cliente)) IN ('INACTIVO','INACTIVOS')")
                    else:
                        where.append("TRIM(UPPER(c.estatus_cliente)) = %s")
                        params.append(estatus_q)

                # estado del plan
                if estado_q:
                    where.append("TRIM(UPPER(c.estado_del_plan)) = %s")
                    params.append(estado_q)

                # condición de pago
                if cond_q:
                    where.append("TRIM(UPPER(COALESCE(c.condicion, c.condicion_pago))) = %s")
                    params.append(cond_q)

                # inconsistentes
                if inc_q == '1':
                    where.append("""EXISTS (
                        SELECT 1 FROM pagos p
                        WHERE p.cliente_id = c.id
                          AND TRIM(UPPER(p.estado_reporte)) = 'INCONSISTENTE'
                    )""")

                # armar query final
                sql = """
                    SELECT c.*
                    FROM clientes c
                    {where_clause}
                    ORDER BY c.nombre, c.apellido
                    LIMIT 2000;
                """.format(where_clause=("WHERE " + " AND ".join(where)) if where else "")

                cur.execute(sql, params)

                for cliente_row in cur.fetchall():
                    cliente_dict = dict(cliente_row)
                    cliente_id = cliente_dict['id']

                    # pagos
                    cur.execute("SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha_creacion DESC", (cliente_id,))
                    cliente_dict['pagos'] = cur.fetchall()
                    cliente_dict['conteo_pagos'] = len(cliente_dict['pagos'])

                    # ofertas
                    cur.execute("SELECT * FROM ofertas WHERE cliente_id = %s ORDER BY fecha_oferta DESC", (cliente_id,))
                    cliente_dict['ofertas'] = cur.fetchall()
                    cliente_dict['conteo_ofertas'] = len(cliente_dict['ofertas'])

                    # gestiones
                    cur.execute("""
                        SELECT g.*, a.usuario as gestor_nombre
                        FROM gestiones_cobranza g
                        LEFT JOIN administradores a ON g.gestor_id = a.id
                        WHERE g.cliente_id = %s
                        ORDER BY g.fecha_creacion DESC;
                    """, (cliente_id,))
                    cliente_dict['gestiones'] = cur.fetchall()
                    cliente_dict['conteo_gestiones'] = len(cliente_dict['gestiones'])

                    # documentos
                    cur.execute("""
                        SELECT d.*, a.usuario as subido_por_nombre
                        FROM documentos_cliente d
                        LEFT JOIN administradores a ON d.subido_por_id = a.id
                        WHERE d.cliente_id = %s
                        ORDER BY d.fecha_subida DESC;
                    """, (cliente_id,))
                    cliente_dict['documentos'] = cur.fetchall()

                    clientes_encontrados.append(cliente_dict)

        except psycopg2.Error as e:
            flash(f"Error al consultar la base de datos: {e}", "error")

    return render_template(
        'consulta.html',
        clientes=clientes_encontrados,
        busqueda=termino_busqueda,
        admin_rol=g.admin['rol']
    )
# Catálogo oficial de estados (clave en UPPER -> valor final capitalizado)
ESTADOS_VALIDOS = {
    'AHORRADOR': 'Ahorrador',
    'ADJUDICADO': 'Adjudicado',
    'CONGELADO': 'Congelado',
    'RETIRO': 'Retiro',
    'COMPLETADO': 'Completado',
    'RESERVA': 'Reserva',
    'COBRANZA DIFERIDA': 'Cobranza Diferida',
    'DIFERIDO EN RESERVA': 'Diferido en Reserva',
    'INSCRITO': 'Inscrito',
    'INACTIVO': 'Inactivo',  # solo como entrada; la regla puede convertirlo a Inscrito
}

def _normaliza_estado(valor_raw):
    """Devuelve el estado capitalizado del catálogo o None si no calza."""
    if valor_raw is None:
        return None
    return ESTADOS_VALIDOS.get(str(valor_raw).strip().upper())

def _pagos_aplicados_por_cedula(conn, cedulas):
    """
    Devuelve {cedula: total_aplicado} para las cédulas indicadas.
    Ajusta nombres de tabla/columnas si en tu esquema difieren.
    """
    if not cedulas:
        return {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.cedula, COALESCE(SUM(p.monto), 0) AS total
            FROM clientes c
            LEFT JOIN pagos p
              ON p.cliente_id = c.id
             AND p.estado = 'aplicado'
            WHERE c.cedula = ANY(%s)
            GROUP BY c.cedula
        """, (cedulas,))
        rows = cur.fetchall()

    res = {}
    for r in rows:
        ced = r['cedula'] if isinstance(r, dict) else r[0]
        total = r['total'] if isinstance(r, dict) else r[1]
        res[str(ced)] = Decimal(total)
    return res

# --- REEMPLAZA TU FUNCIÓN ACTUAL CON ESTA VERSIÓN CORREGIDA ---
def _pagos_aplicados_por_cedula(conn, cedulas):
    """
    Devuelve dict {cedula: total_aplicado} detectando columnas reales en pagos:
    - estado de pago: estado_pago | estado | estatus_pago | estatus
    - monto: monto | valor | importe
    - fk cliente: cliente_id | clienteid
    Filtra por estado='aplicado' si existe la columna de estado; si no existe, suma todos.
    """
    if not cedulas:
        return {}

    try:
        from psycopg2.extras import RealDictCursor
    except Exception:
        RealDictCursor = None

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Detectar columnas disponibles en 'pagos'
        cur.execute("""
            SELECT LOWER(column_name) AS col
            FROM information_schema.columns
            WHERE table_schema = ANY (current_schemas(true))
              AND LOWER(table_name) = 'pagos'
        """)
        cols = { (r['col'] if isinstance(r, dict) else r[0]) for r in cur.fetchall() }

        status_col = next((c for c in ['estado_pago','estado','estatus_pago','estatus'] if c in cols), None)
        amount_col = next((c for c in ['monto','valor','importe'] if c in cols), 'monto')
        fk_col     = next((c for c in ['cliente_id','clienteid'] if c in cols), 'cliente_id')

        # Armar SQL dinámico seguro
        where_extra = ""
        params = [cedulas]
        if status_col:
            where_extra = f" AND TRIM(LOWER(p.{status_col})) = %s"
            params.append('aplicado')

        sql = f"""
            SELECT c.cedula, COALESCE(SUM(p.{amount_col}), 0) AS total
            FROM pagos p
            JOIN clientes c ON c.id = p.{fk_col}
            WHERE c.cedula = ANY(%s)
            {where_extra}
            GROUP BY c.cedula
        """

        cur.execute(sql, params)
        rows = cur.fetchall() or []

    # Construir diccionario resultado
    out = {}
    for r in rows:
        if isinstance(r, dict):
            out[str(r['cedula'])] = r['total']
        else:
            out[str(r[0])] = r[1]
    return out

@app.route('/upload_clientes', methods=['GET', 'POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def upload_clientes():
    if request.method == 'POST':
        archivo = request.files.get('archivo_excel')
        if not archivo or archivo.filename == '':
            flash('No se seleccionó ningún archivo.', 'warning')
            return redirect(request.url)

        if not (archivo.filename.endswith('.xlsx') or archivo.filename.endswith('.xls')):
            flash('Formato de archivo no válido. Por favor, sube un archivo Excel (.xlsx o .xls).', 'danger')
            return redirect(request.url)

        conn = get_db()
        if not conn:
            flash("Error de conexión a la base de datos.", "danger")
            return redirect(request.url)

        try:
            # Lee TODAS las hojas (dict: nombre_hoja -> DataFrame)
            data = archivo.read()
            sheets = pd.read_excel(BytesIO(data), engine='openpyxl', dtype=str, sheet_name=None)

            if not sheets:
                flash("El archivo no contiene hojas legibles.", "warning")
                return redirect(request.url)

            # Normaliza nombres esperados de hojas
            target_names = set()
            for name in sheets.keys():
                nup = (name or '').strip().upper()
                if nup == 'CYK':
                    target_names.add(name)
                if nup in ('COPY OF MOTO PLAN MOTORS', 'MOTO PLAN', 'MOTO PLAN MOTORS'):
                    target_names.add(name)

            # Si no encontró las esperadas, procesa todas las hojas no vacías
            if not target_names:
                target_names = {n for n, df in sheets.items() if df is not None and not df.empty}

            # Mapeo de columnas (en mayúsculas) -> nombres internos
            column_map = {
                'NUMERO DE CONTRATO': 'numero_contrato',
                'N⁰ CONTRATO': 'numero_contrato',
                'N° CONTRATO': 'numero_contrato',
                'NUMERO CONTRATO': 'numero_contrato',
                'NUMERO DE CEDULA': 'cedula',
                'N⁰ CEDULA': 'cedula',
                'N° CEDULA': 'cedula',
                'CEDULA': 'cedula',
                'CÉDULA': 'cedula',
                'NOMBRE Y APELLIDO': 'nombre_completo',
                'NOMBRE': 'nombre',
                'APELLIDO': 'apellido',
                'ESTADO DEL PLAN': 'estado_del_plan',
                'ESTADO DEL PLAN ': 'estado_del_plan',  # a veces con espacio extra
                'ESTATUS': 'estatus_cliente',
                'ESTATUS CLIENTE': 'estatus_cliente',
                'CONDICION': 'condicion_pago',
                'CONDICIÓN': 'condicion_pago',
                'CONDICION PAGO': 'condicion_pago',
                'CONDICION DE PAGO': 'condicion_pago',
                'CONDICION_DE_PAGO': 'condicion_pago',
            }

            # Acumuladores (upper para valores crudos de excel en estado/estatus/condición)
            # (cedula, nombre, apellido, estado_upper, estatus_upper, numero_contrato, condicion_upper, empresa_label)
            all_rows = []

            for sheet_name in target_names:
                df = sheets.get(sheet_name)
                if df is None or df.empty:
                    continue

                df = df.fillna('')
                df.columns = [str(c).strip().upper() for c in df.columns]
                df = df.rename(columns={k: v for k, v in column_map.items() if k in df.columns})

                # Asegura columnas mínimas
                for col in ['cedula', 'estado_del_plan', 'estatus_cliente', 'numero_contrato', 'condicion_pago']:
                    if col not in df.columns:
                        df[col] = ''

                # Si viene "NOMBRE Y APELLIDO", sepáralo en nombre y apellido
                if 'nombre_completo' in df.columns and ('nombre' not in df.columns or 'apellido' not in df.columns):
                    def split_nombre(nc):
                        nc = (nc or '').strip()
                        if not nc:
                            return '', ''
                        partes = nc.split()
                        nombre = partes[0]
                        apellido = ' '.join(partes[1:]) if len(partes) > 1 else ''
                        return nombre, apellido
                    tmp = df['nombre_completo'].apply(split_nombre).tolist()
                    nombres = [t[0] for t in tmp]
                    apellidos = [t[1] for t in tmp]
                    if 'nombre' not in df.columns:
                        df['nombre'] = nombres
                    if 'apellido' not in df.columns:
                        df['apellido'] = apellidos

                if 'nombre' not in df.columns:
                    df['nombre'] = ''
                if 'apellido' not in df.columns:
                    df['apellido'] = ''

                # Empresa por hoja
                sn_up = (sheet_name or '').strip().upper()
                empresa_label = 'CYK' if sn_up == 'CYK' else 'MOTO PLAN'

                # ===== Normaliza strings con .str.*, evita AttributeError en Series =====
                df['cedula'] = (
                    df['cedula']
                    .astype('string').str.strip()
                )
                df['nombre'] = (
                    df['nombre']
                    .astype('string').str.strip()
                )
                df['apellido'] = (
                    df['apellido']
                    .astype('string').str.strip()
                )
                df['numero_contrato'] = (
                    df['numero_contrato']
                    .astype('string').str.strip()
                )
                df['estado_del_plan'] = (
                    df['estado_del_plan']
                    .astype('string').str.strip()
                    .str.replace(r'\s+', ' ', regex=True)
                    .str.upper()
                )
                df['estatus_cliente'] = (
                    df['estatus_cliente']
                    .astype('string').str.strip()
                    .str.replace(r'\s+', ' ', regex=True)
                    .str.upper()
                )
                df['condicion_pago'] = (
                    df['condicion_pago']
                    .astype('string').str.strip()
                    .str.replace(r'\s+', ' ', regex=True)
                    .str.upper()
                )
                # ===== Normaliza estatus a valor canónico (incluye "pendien por entrega") =====
                df = normalize_estatus_in_df(df)

                # Filtra sin cédula
                df = df[df['cedula'] != '']

                for _, r in df.iterrows():
                    all_rows.append((
                        r['cedula'],
                        r['nombre'],
                        r['apellido'],
                        r['estado_del_plan'],   # upper original (para normalizar más adelante)
                        r['estatus_cliente'],   # YA normalizado a 'PENDIENTE POR ENTREGA' si aplica
                        r['numero_contrato'],
                        r['condicion_pago'],    # upper
                        empresa_label
                    ))

            if not all_rows:
                flash("No se encontraron filas válidas (con cédula) en las hojas procesadas.", "warning")
                return redirect(request.url)

            cedulas_unicas = list({t[0] for t in all_rows})

            with conn.cursor() as cur:
                # Clientes existentes
                cur.execute("SELECT id, cedula FROM clientes WHERE cedula = ANY(%s)", (cedulas_unicas,))
                existentes = {row['cedula']: row['id'] for row in cur.fetchall()}

                # Totales de pagos aplicados por cédula (batch)
                pagos_totales = _pagos_aplicados_por_cedula(conn, cedulas_unicas)

                inserts_data = []
                updates_data = []

                for t in all_rows:
                    cedula, nombre, apellido, estado_upper, estatus_upper, numero_contrato, condicion_upper, empresa_label = t

                    # Normaliza al catálogo o capitaliza si es desconocido (para no romper carga)
                    estado_norm = _normaliza_estado(estado_upper) or estado_upper.title().strip()

                    # Regla: Inactivo -> Inscrito solo si NO tiene pagos aplicados
                    if estado_norm == 'Inactivo' and pagos_totales.get(str(cedula), Decimal('0')) == 0:
                        estado_norm = 'Inscrito'

                    final_tuple = (
                        cedula,
                        nombre,
                        apellido,
                        estado_norm,        # estado final tras la regla
                        estatus_upper,
                        numero_contrato,
                        condicion_upper,
                        empresa_label
                    )

                    if cedula in existentes:
                        updates_data.append(final_tuple)
                    else:
                        inserts_data.append(final_tuple)

                # Asegura columna empresa por si falta en algunos entornos
                cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS empresa TEXT;")

                # INSERT masivo
                if inserts_data:
                    insert_query = """
                        INSERT INTO clientes (
                            cedula, nombre, apellido, estado_del_plan, estatus_cliente,
                            numero_contrato, condicion_pago, empresa
                        )
                        VALUES %s
                    """
                    execute_values(cur, insert_query, inserts_data)

                # UPDATE masivo con VALUES
                if updates_data:
                    update_query = """
                        UPDATE clientes AS c SET
                            nombre          = NULLIF(data.nombre, '')          ,
                            apellido        = NULLIF(data.apellido, '')        ,
                            estado_del_plan = NULLIF(data.estado_del_plan, '') ,
                            estatus_cliente = NULLIF(data.estatus_cliente, '') ,
                            numero_contrato = NULLIF(data.numero_contrato, '') ,
                            condicion_pago  = NULLIF(data.condicion_pago, '')  ,
                            empresa         = NULLIF(data.empresa, '')
                        FROM (VALUES %s) AS data (
                            cedula, nombre, apellido, estado_del_plan, estatus_cliente,
                            numero_contrato, condicion_pago, empresa
                        )
                        WHERE c.cedula = data.cedula
                    """
                    execute_values(cur, update_query, updates_data)

                conn.commit()

            # Métrica de conversión Inactivo→Inscrito (sin pagos)
            convertidos = sum(
                1 for t in (inserts_data + updates_data)
                if t[3] == 'Inscrito'  # índice 3 = estado_del_plan final
            )

            flash(
                f"Carga completada. Creados: {len(inserts_data)}, Actualizados: {len(updates_data)}. "
                f"Inactivo→Inscrito (sin pagos): {convertidos}. Hojas procesadas: {len(target_names)}.",
                "success"
            )

        except Exception as e:
            conn.rollback()
            logging.error("Error crítico al procesar el archivo: %s\n%s", e, traceback.format_exc())
            flash(f"Error crítico al procesar el archivo: {e}", "danger")

        return redirect(url_for('upload_clientes'))

    # GET
    return render_template('upload_clientes.html')

# ====== FIN BLOQUE ======

@app.route('/registrar_pago/<int:client_id>', methods=['GET', 'POST'])
@admin_required
def registrar_pago(client_id):
    """
    Ruta para que un administrador registre un pago manualmente.
    """
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
        pago_form = {k: v.strip() if v else None for k, v in request.form.items()}
        
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT tasa FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (get_venezuela_current_date(),))
                tasa_del_dia_row = cur.fetchone()
                tasa_bcv_dia = tasa_del_dia_row['tasa'] if tasa_del_dia_row and tasa_del_dia_row['tasa'] else Decimal('0.0')

                if tasa_bcv_dia == Decimal('0.0') and pago_form.get('forma_pago') not in ['Efectivo', 'Binance']:
                    flash('No se pudo registrar el pago porque no hay una tasa de cambio configurada para hoy.', 'danger')
                    return render_template('registrar_pago.html', cliente=cliente, tasas_hoy=tasas_hoy)

                monto_bs_str = pago_form.get('monto_bs', '0').replace(',', '.')
                monto_bs = Decimal(monto_bs_str) if monto_bs_str else Decimal('0.0')
                
                if pago_form['tipo_pago'] == 'Inscripción':
                    monto_usd_a_guardar = cliente.get('inscripcion_monto', Decimal('0.0'))
                else:
                    monto_usd_a_guardar = cliente.get('valor_cuota', Decimal('0.0'))
                
                if pago_form.get('pago_en') in ['Efectivo USD', 'Binance']:
                    monto_usd_str = pago_form.get('monto', '0').replace(',', '.')
                    monto_usd_a_guardar = Decimal(monto_usd_str) if monto_usd_str else Decimal('0.0')

                pago_query = """
                    INSERT INTO pagos (cliente_id, monto, tipo_pago, forma_pago, fecha_pago, pago_en, por_concepto_de, referencia, banco, lugar_emision,
                                        tasa_dia, monto_bs, estado_pago, cuotas_cubiertas, moneda_referencia, fecha_creacion, registrado_por_id, detalles_reporte)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Pendiente', 0, %s, %s, %s, %s);
                """
                cur.execute(pago_query, (
                    client_id, 
                    monto_usd_a_guardar,
                    pago_form['tipo_pago'], pago_form['forma_pago'], pago_form['fecha_pago'], pago_form.get('pago_en'), 
                    pago_form.get('por_concepto_de'), pago_form.get('referencia'), pago_form.get('banco'), pago_form.get('lugar_emision'), 
                    tasa_bcv_dia, 
                    monto_bs,
                    'USD', get_venezuela_current_datetime(), g.admin['id'], None
                ))
                conn.commit()
                flash(f"¡Pago de {pago_form['tipo_pago']} registrado como PENDIENTE! Ahora debe ser conciliado.", 'success')
                return redirect(url_for('consulta', busqueda=cliente['cedula']))
        except (psycopg2.Error, ValueError, TypeError) as e:
            conn.rollback()
            flash(f'Ocurrió un error al registrar el pago: {e}', 'error')
            return render_template('registrar_pago.html', cliente=cliente, tasas_hoy=tasas_hoy)
            
    return render_template('registrar_pago.html', cliente=cliente, tasas_hoy=tasas_hoy)

# --- LÓGICA DE CONCILIACIÓN (AHORA SIN APROBACIÓN AUTOMÁTICA) ---
@app.route('/conciliar_pago/<int:pago_id>', methods=['POST'])
@admin_required
def conciliar_pago(pago_id):
    def calcular_y_guardar_comisiones(cliente_info):
        conn = get_db()
        if not conn:
            logging.error("No se pudo obtener la conexión a la base de datos para calcular comisiones.")
            return
        try:
            with conn.cursor() as cur:
                # La lógica interna de comisiones se asume correcta, pero nos aseguramos
                # de que use el campo 'plan_contratado'.
                plan_contratado = Decimal(cliente_info.get('plan_contratado', '0'))
                # ... (resto de la lógica de cálculo de comisiones) ...
        except Exception as e:
            if conn: conn.rollback()
            logging.error(f"Error CRÍTICO al calcular comisiones para Contrato {cliente_info.get('numero_contrato', 'N/A')}: {e}")
            raise e

    conn = get_db()
    cedula_cliente_para_redirect = None
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('pagos_por_conciliar'))
        
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM pagos WHERE id = %s FOR UPDATE", (pago_id,))
            pago_a_conciliar = cur.fetchone()
            if not pago_a_conciliar:
                flash("El pago que intenta conciliar no existe.", "error")
                return redirect(url_for('pagos_por_conciliar'))

            cur.execute("SELECT * FROM clientes WHERE id = %s FOR UPDATE", (pago_a_conciliar['cliente_id'],))
            cliente = cur.fetchone()
            cedula_cliente_para_redirect = cliente['cedula']

            # Lógica de conciliación principal
            flash_msg = ""
            tipo_pago = pago_a_conciliar['tipo_pago']

            # Caso especial: Si es un cliente migrado con inscripción pendiente, cualquier 'Cuota' se abona a la inscripción.
            if cliente.get('es_migrado') and cliente.get('inscripcion_pagada', Decimal('0.0')) < cliente.get('inscripcion_monto', Decimal('0.0')) and tipo_pago == 'Cuota':
                tipo_pago = 'Inscripción'
                flash_msg += "Pago de cuota aplicado a la inscripción del cliente migrado. "

            if tipo_pago == 'Inscripción':
                cur.execute(
                    "UPDATE clientes SET inscripcion_pagada = inscripcion_pagada + %s WHERE id = %s RETURNING inscripcion_pagada, inscripcion_monto",
                    (pago_a_conciliar['monto'], cliente['id'])
                )
                updated_cliente = cur.fetchone()
                
                if updated_cliente['inscripcion_pagada'] >= updated_cliente['inscripcion_monto']:
                    cur.execute("UPDATE clientes SET estado_del_plan = 'INSCRITO' WHERE id = %s", (cliente['id'],))
                    flash_msg += "¡Pago de inscripción conciliado y cliente INSCRITO!"
                    
                    try:
                        cur.execute("SELECT * FROM clientes WHERE id = %s", (cliente['id'],))
                        cliente_actualizado_para_comision = cur.fetchone()
                        calcular_y_guardar_comisiones(dict(cliente_actualizado_para_comision))
                        flash_msg += " ¡Comisiones generadas!"
                    except Exception as e:
                        flash_msg += " ¡ADVERTENCIA: Error al generar comisiones!"
                else:
                    flash_msg += f"¡Abono de inscripción de ${pago_a_conciliar['monto']} conciliado!"

            elif tipo_pago in ['Cuota', 'Pago Oferta']:
                if tipo_pago == 'Cuota':
                     cur.execute(
                        "UPDATE clientes SET cuotas_pagadas_progresivas = cuotas_pagadas_progresivas + 1 WHERE id = %s",
                        (cliente['id'],)
                    )
                flash_msg = f"¡Pago de {pago_a_conciliar['tipo_pago']} de ${pago_a_conciliar['monto']} conciliado!"
            
            # Actualiza el estado del pago a 'Conciliado'
            cur.execute(
                "UPDATE pagos SET estado_pago = 'Conciliado', conciliado_por_id = %s, fecha_conciliacion = NOW() WHERE id = %s",
                (g.admin['id'], pago_id)
            )
            
            # Registrar auditoría
            descripcion_audit = f"Concilió el pago N° {pago_id} (Tipo: {pago_a_conciliar['tipo_pago']}, Monto: ${pago_a_conciliar['monto']})."
            registrar_accion_auditoria('CONCILIACION_PAGO', descripcion_audit, cliente['id'])

            conn.commit()
            flash(flash_msg, 'success')
            return redirect(url_for('consulta', busqueda=cedula_cliente_para_redirect))
            
    except (psycopg2.Error, ValueError, TypeError, InvalidOperation) as e:
        if conn: conn.rollback()
        logging.error(f"Error al conciliar el pago {pago_id}: {traceback.format_exc()}")
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
    SELECT p.*, c.nombre, c.apellido, (c.nombre || ' ' || c.apellido) as nombre_apellido, c.cedula, c.cuotas_totales, c.valor_cuota, c.inscripcion, c.inscripcion_pagada,
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
        cur.execute("""
            SELECT *, 
                   (nombre || ' ' || apellido) as nombre_apellido,
                   (beneficiario_nombre || ' ' || beneficiario_apellido) as beneficiario_nombre_apellido
            FROM clientes 
            WHERE id = %s
        """, (client_id,))
        cliente_actual = cur.fetchone()
    
    if not cliente_actual:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta'))

    edicion_de_estado_bloqueada = cliente_actual['estado_del_plan'] == 'RESERVA'

    if request.method == 'POST':
        try:
            form_data = {k: v.strip() if isinstance(v, str) else v for k, v in request.form.items()}
            
            cambios = []
            # ... (código de auditoría sin cambios)

            with conn.cursor() as cur:
                if 'nombre_apellido' in form_data:
                    nombre_completo = form_data['nombre_apellido'].split(' ', 1)
                    form_data['nombre'] = nombre_completo[0]
                    form_data['apellido'] = nombre_completo[1] if len(nombre_completo) > 1 else ''

                if 'beneficiario_nombre_apellido' in form_data:
                    beneficiario_completo = form_data['beneficiario_nombre_apellido'].split(' ', 1)
                    form_data['beneficiario_nombre'] = beneficiario_completo[0]
                    form_data['beneficiario_apellido'] = beneficiario_completo[1] if len(beneficiario_completo) > 1 else ''
                
                update_data = dict(cliente_actual)
                update_data.update(form_data)
                
                if edicion_de_estado_bloqueada:
                    update_data['estatus_cliente'] = cliente_actual['estatus_cliente']

                # --- INICIO DE LA CORRECCIÓN ---
                # La consulta UPDATE ahora usa todos los nombres de columna estandarizados y en un orden lógico
                update_query = """
                UPDATE clientes SET
                    -- Datos Personales
                    nombre = %(nombre)s, 
                    apellido = %(apellido)s, 
                    cedula = %(cedula)s, 
                    numero_telefono = %(numero_telefono)s, 
                    email = %(email)s, 
                    direccion = %(direccion)s,
                    
                    -- Datos del Plan y Contrato
                    numero_contrato = %(numero_contrato)s,
                    plan_contratado = %(plan_contratado)s,
                    grupo = %(grupo)s,
                    cuotas_totales = %(cuotas_totales)s,
                    fecha_ingreso = %(fecha_ingreso)s,
                    asesor = %(asesor)s, 
                    responsable = %(responsable)s,
                    
                    -- Datos Financieros
                    moneda_pago = %(moneda_pago)s, 
                    valor_cuota = %(valor_cuota)s,
                    inscripcion_monto = %(inscripcion_monto)s,
                    cuotas_pagadas_progresivas = %(cuotas_pagadas_progresivas)s,
                    cuotas_pagadas_regresivas = %(cuotas_pagadas_regresivas)s,

                    -- Estados
                    estado_del_plan = %(estado_del_plan)s, 
                    estatus_cliente = %(estatus_cliente)s,
                    
                    -- Datos del Beneficiario
                    beneficiario_nombre = %(beneficiario_nombre)s,
                    beneficiario_apellido = %(beneficiario_apellido)s,
                    beneficiario_cedula = %(beneficiario_cedula)s,
                    beneficiario_telefono = %(beneficiario_telefono)s,
                    beneficiario_email = %(beneficiario_email)s,
                    beneficiario_direccion = %(beneficiario_direccion)s
                WHERE id = %(id)s;
                """
                # --- FIN DE LA CORRECCIÓN ---
                cur.execute(update_query, update_data)
                
                # ... (lógica de auditoría sin cambios)

                conn.commit()
                flash('¡Cliente actualizado exitosamente!', 'success')
                return redirect(url_for('consulta', busqueda=update_data.get('cedula')))
        except (psycopg2.Error, ValueError, ConnectionError, InvalidOperation) as e: 
            conn.rollback()
            flash(f'Ocurrió un error al actualizar: {e}', 'error')
            
    return render_template('edit_cliente.html', cliente=cliente_actual, edicion_de_estado_bloqueada=edicion_de_estado_bloqueada)

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
                WHERE TRIM(UPPER(estado_del_plan)) = 'AHORRADOR' AND cuotas_pagadas_progresivas >= (12 + meses_retraso_entrega) 
                AND TRIM(UPPER(estatus_cliente)) = 'ACTIVO' ORDER BY nombre, apellido;
            """)
            clientes_elegibles_ahorro = cur.fetchall()
            
            cur.execute("""
                SELECT o.cuotas_ofertadas, o.modelo_ofertado, c.id, (c.nombre || ' ' || c.apellido) as nombre_apellido, 
                       c.cedula, c.plan_contratado
                FROM ofertas o JOIN clientes c ON o.cliente_id = c.id 
                WHERE o.estado_oferta = 'activa' AND TRIM(UPPER(c.estado_del_plan)) = 'AHORRADOR' 
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
                return redirect(url_for('portal_logout'))
            
            cliente_dict = dict(cliente)
            
            cur.execute("""
                SELECT p.*, b.status as bulk_status, b.expected_amount as bulk_expected_amount, b.currency as bulk_currency
                FROM pagos p
                LEFT JOIN payment_bulks b ON p.bulk_id = b.id
                WHERE p.cliente_id = %s AND p.estado_pago != 'Anulado'
                ORDER BY p.fecha_creacion DESC;
            """, (session['cliente_id'],))
            todos_los_pagos = cur.fetchall()

            procesos_agrupados = {}
            for pago in todos_los_pagos:
                bulk_id = pago.get('bulk_id')
                id_proceso = bulk_id if bulk_id else f"solo_{pago['id']}"

                if id_proceso not in procesos_agrupados:
                    procesos_agrupados[id_proceso] = {
                        'id_proceso': id_proceso, 'pagos': [], 'fecha_inicio': pago['fecha_creacion'],
                        'concepto_principal': pago['por_concepto_de'] or pago['tipo_pago'],
                        'estado_general': '', 'monto_total': Decimal('0.0'), 'moneda': 'USD'
                    }
                procesos_agrupados[id_proceso]['pagos'].append(dict(pago))

            for id_proceso, proceso in procesos_agrupados.items():
                proceso['pagos'].sort(key=lambda p: p['fecha_creacion'], reverse=True)
                
                if isinstance(id_proceso, str) and id_proceso.startswith('solo_'):
                    pago_unico = proceso['pagos'][0]
                    proceso['moneda'] = 'Bs.' if pago_unico.get('pago_en') == 'Dolar/BCV' else 'USD'
                    proceso['monto_total'] = pago_unico['monto_bs'] if proceso['moneda'] == 'Bs.' else pago_unico['monto']
                    proceso['estado_general'] = 'Conciliado' if pago_unico['estado_pago'] == 'Conciliado' else pago_unico['estado_reporte']
                else:
                    primer_pago_del_grupo = sorted(proceso['pagos'], key=lambda p: p['fecha_creacion'])[0]
                    proceso['monto_total'] = primer_pago_del_grupo['bulk_expected_amount'] or Decimal('0.0')
                    proceso['moneda'] = 'Bs.' if primer_pago_del_grupo['bulk_currency'] == 'VES' else 'USD'
                    proceso['concepto_principal'] = primer_pago_del_grupo['por_concepto_de']
                    
                    bulk_status = primer_pago_del_grupo['bulk_status']
                    if bulk_status == 'RECONCILED': proceso['estado_general'] = 'Conciliado'
                    elif any(p['estado_reporte'] == 'Inconsistente' for p in proceso['pagos']): proceso['estado_general'] = 'Inconsistente'
                    elif any(p['estado_reporte'] == 'Pendiente de Revision' for p in proceso['pagos']): proceso['estado_general'] = 'Pendiente de Revision'
                    elif bulk_status == 'READY_TO_RECONCILE' or all(p.get('estado_reporte') == 'Aprobado' for p in proceso['pagos']): proceso['estado_general'] = 'Aprobado'
                    else: proceso['estado_general'] = 'En Proceso'

            lista_procesos = sorted(procesos_agrupados.values(), key=lambda p: p['fecha_inicio'], reverse=True)
            cliente_dict['procesos_de_pago'] = lista_procesos

            cur.execute("SELECT * FROM payment_orders WHERE cliente_id = %s AND status = 'ISSUED'", (session['cliente_id'],))
            ordenes_pendientes_raw = cur.fetchall()
            ordenes_pendientes = [dict(orden) for orden in ordenes_pendientes_raw]

            # --- INICIO DE LA CORRECCIÓN DE LÓGICA ---
            # Se refina la condición para mostrar el botón de pago.
            # No solo se verifica si hay órdenes pendientes, sino también si hay algún proceso que no esté conciliado.
            hay_proceso_activo = any(p['estado_general'] not in ['Conciliado', 'Anulado'] for p in lista_procesos)
            
            estado_principal = {}
            if cliente_dict.get('proceso') == 'RESERVA' and not hay_proceso_activo:
                inscripcion_pagada = cliente_dict.get('inscripcion_pagada', Decimal('0.0')) or Decimal('0.0')
                inscripcion_total = cliente_dict.get('inscripcion_monto', Decimal('0.0')) or Decimal('0.0')
                if inscripcion_pagada < inscripcion_total:
                    monto_restante = inscripcion_total - inscripcion_pagada
                    estado_principal = { 'titulo': 'Completa tu Inscripción', 'mensaje': f"¡Bienvenido a Moto Plan! Para activar tu plan, por favor completa el pago de tu inscripción. Monto restante: ${monto_restante:,.2f}", 'boton_texto': 'Pagar Inscripción', 'boton_url': url_for('portal_pagar_inscripcion'), 'boton_activo': True }
            elif cliente_dict.get('proceso') == 'INSCRITO' and not hay_proceso_activo:
                estado_principal = { 'titulo': '¡Felicitaciones! Es hora de activar tu plan', 'mensaje': f"Tu inscripción ha sido completada. Para comenzar a sumar cuotas, realiza el pago de tu primera cuota por ${cliente_dict.get('valor_cuota', 0):,.2f}.", 'boton_texto': 'Pagar Primera Cuota', 'boton_url': url_for('portal_reportar_pago'), 'boton_activo': True }
            
            return render_template('portal_dashboard.html', 
                                   cliente=cliente_dict, 
                                   ordenes_pendientes=ordenes_pendientes,
                                   estado_principal=estado_principal,
                                   # ... (otras variables que ya estaban) ...
                                   )
            # --- FIN DE LA CORRECCIÓN DE LÓGICA ---
            
    except (psycopg2.Error, KeyError) as e:
        error_trace = traceback.format_exc()
        logging.error(f"Error en portal_dashboard:\n{error_trace}")
        flash('Ocurrió un error inesperado al cargar tu portal.', 'error')
        return redirect(url_for('portal_login'))

@app.route('/portal/reportar_pago', methods=['GET', 'POST'])
@portal_login_required
def portal_reportar_pago():
    conn = get_db()
    if not conn:
        flash('No se pudo conectar con la base de datos.', 'error')
        return redirect(url_for('portal_dashboard'))
    
    try:
        with conn.cursor() as cur:
            # ... (Lógica del método GET) ...
            cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s;", (session['cliente_id'],))
            cliente = cur.fetchone()
            if not cliente:
                flash('No se pudo encontrar tu perfil de cliente.', 'error')
                return redirect(url_for('portal_logout'))

            monto_a_pagar_usd = cliente.get('valor_cuota') or Decimal('0.0')
            
            cur.execute("SELECT COUNT(*) FROM pagos WHERE cliente_id = %s AND tipo_pago = 'Cuota' AND estado_pago = 'Conciliado'", (session['cliente_id'],))
            cuotas_pagadas_conciliadas = cur.fetchone()[0]

            concepto_pago = "Pago de Cuota de Activación" if cuotas_pagadas_conciliadas == 0 else "Pago de Cuota Mensual"

            today_str = get_venezuela_current_date().strftime('%Y-%m-%d')
            cur.execute("SELECT tasa FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (today_str,))
            tasa_hoy = cur.fetchone()
            tasa_bcv_calculo = tasa_hoy['tasa'] if tasa_hoy and tasa_hoy['tasa'] else Decimal('0.0')
            monto_a_pagar_bs = (monto_a_pagar_usd * tasa_bcv_calculo).quantize(Decimal('0.01'))


            if request.method == 'POST':
                pago_form = {k: v.strip() if v else None for k, v in request.form.items()}
                
                pago_en_final = pago_form.get('pago_en')
                monto_reportado_bs = Decimal('0.0')
                monto_usd_a_guardar = Decimal('0.0')
                forma_pago_final = None
                referencia_final = None # Inicializado en None
                banco_final = None
                fecha_pago_final = pago_form.get('fecha_pago')
                currency_bulk = ''

                if pago_en_final == 'USDT':
                    monto_usd_a_guardar = Decimal(pago_form.get('monto_usdt', '0.00').replace(',', '.'))
                    forma_pago_final = 'Binance'
                    tasa_bcv_calculo = None
                    currency_bulk = 'USD'
                    referencia_final = pago_form.get('referencia_usdt')
                else: 
                    pago_en_final = 'Dolar/BCV'
                    monto_bs_str = pago_form.get('monto_bs', '0.00').replace(',', '.')
                    monto_reportado_bs = Decimal(monto_bs_str).quantize(Decimal('0.02'))
                    monto_usd_a_guardar = cliente.get('valor_cuota') or Decimal('0.0') 
                    forma_pago_final = pago_form.get('forma_pago_bs')
                    banco_final = pago_form.get('banco')
                    currency_bulk = 'VES'
                    referencia_final = pago_form.get('referencia')

                # --- INICIO DE LA CORRECCIÓN ---
                # Se selecciona el monto ESPERADO correcto según la moneda del proceso (bulk).
                # Para Bolívares (VES), usamos el monto calculado por el sistema (monto_a_pagar_bs).
                # Para Dólares (USD), usamos el valor de la cuota (monto_a_pagar_usd).
                # Esto replica la lógica de USDT para los pagos en Bs.
                expected_amount_for_bulk = monto_a_pagar_bs if currency_bulk == 'VES' else monto_a_pagar_usd
                
                cur.execute("""
                    INSERT INTO payment_bulks (cliente_id, currency, expected_amount, status, total_verified)
                    VALUES (%s, %s, %s, 'OPEN', 0) RETURNING id
                """, (cliente['id'], currency_bulk, expected_amount_for_bulk))
                # --- FIN DE LA CORRECCIÓN ---
                
                new_bulk_id = cur.fetchone()[0]

                pago_query = """
                    INSERT INTO pagos (cliente_id, monto, monto_bs, tipo_pago, forma_pago, fecha_pago, referencia, banco, tasa_dia,
                                    estado_reporte, fecha_creacion, reportado_por_cliente, por_concepto_de, pago_en, cuotas_cubiertas, bulk_id, estado_pago)
                    VALUES (%s, %s, %s, 'Cuota', %s, %s, %s, %s, %s, 'Pendiente de Revision', %s, TRUE, %s, %s, 1, %s, 'Pendiente');
                """
                cur.execute(pago_query, (
                    cliente['id'], monto_usd_a_guardar, monto_reportado_bs,
                    forma_pago_final, fecha_pago_final, referencia_final, 
                    banco_final, tasa_bcv_calculo,
                    get_venezuela_current_datetime(), concepto_pago, pago_en_final,
                    new_bulk_id
                ))
                
                flash('✅ ¡Pago de cuota reportado! Será verificado por un administrador.', 'success')
                conn.commit()
                return redirect(url_for('portal_dashboard'))

    except (psycopg2.Error, ValueError, InvalidOperation) as e:
        if conn: conn.rollback()
        flash(f'Ocurrió un error al reportar el pago: {e}', 'error')
        traceback.print_exc()
        return redirect(url_for('portal_dashboard'))

    return render_template('portal_pago_unificado.html', 
                           cliente=cliente, 
                           tasa_hoy=tasa_hoy, 
                           monto_a_pagar_usd=monto_a_pagar_usd,
                           monto_a_pagar_bs=monto_a_pagar_bs,
                           concepto_pago=concepto_pago,
                           is_enrollment_payment=False,
                           monto_restante=Decimal('0.0'))

# NUEVO: Esta es la nueva ruta completa para manejar el pago de diferencias.
@app.route('/portal/diferencia/reportar/<int:bulk_id>/<int:order_id>', methods=['GET', 'POST'])
@portal_login_required
def portal_diferencia_reportar(bulk_id, order_id):
    """
    Gestiona el reporte de pago de una diferencia generada por una inconsistencia.
    """
    conn = get_db()
    if not conn: 
        flash("Error de conexión.", "error")
        return redirect(url_for('portal_dashboard'))
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT b.status as bulk_status, o.status as order_status, o.currency, o.amount
                FROM payment_orders o
                JOIN payment_bulks b ON o.bulk_id = b.id
                WHERE o.id = %s AND o.bulk_id = %s AND o.cliente_id = %s
            """, (order_id, bulk_id, session['cliente_id']))
            
            order = cur.fetchone()

            if not order or order['bulk_status'] == 'CANCELLED' or order['order_status'] != 'ISSUED':
                flash("Esta orden de pago ya no es válida o ha sido procesada. Si cree que es un error, contacte a soporte.", "error")
                return redirect(url_for('portal_dashboard'))
            
            cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s", (session['cliente_id'],))
            cliente = cur.fetchone()

            cur.execute("SELECT 1 FROM pagos WHERE cliente_id = %s AND estado_reporte = 'Pendiente de Revision' LIMIT 1", (session['cliente_id'],))
            hay_pago_pendiente_general = cur.fetchone() is not None

            today_str = get_venezuela_current_date().strftime('%Y-%m-%d')
            cur.execute("SELECT tasa FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (today_str,))
            tasa_hoy = cur.fetchone()

            tasa_bcv = tasa_hoy['tasa'] if tasa_hoy and tasa_hoy['tasa'] else Decimal('0.0')
            monto_diferencia = order['amount']
            moneda_orden = order['currency']
            
            monto_a_pagar_bs = Decimal('0.0')
            monto_a_pagar_usd = Decimal('0.0')

            if moneda_orden == 'VES':
                monto_a_pagar_bs = monto_diferencia
                if tasa_bcv > 0:
                    monto_a_pagar_usd = (monto_a_pagar_bs / tasa_bcv)
            else: # Asume USD
                monto_a_pagar_usd = monto_diferencia
                monto_a_pagar_bs = (monto_a_pagar_usd * tasa_bcv)
            
            concepto_pago = f"Pago de diferencia (Orden #{order_id})"
            monto_restante = monto_diferencia

    except psycopg2.Error as e:
        flash(f"Error al cargar la página de reporte: {e}", "error")
        return redirect(url_for('portal_dashboard'))

    if request.method == 'POST':
        pago_form = {k: v.strip() if v else None for k, v in request.form.items()}
        try:
            with conn.cursor() as cur:
                tasa_bcv_dia = tasa_hoy['tasa'] if tasa_hoy and tasa_hoy['tasa'] else Decimal('0.0')
                moneda_orden = order['currency']
                
                monto_bs_final = Decimal('0.0')
                monto_usd_final = Decimal('0.0')
                pago_en_final = ''
                forma_pago_final = ''
                banco_final = None
                referencia_final = None # Inicializamos la variable

                if moneda_orden == 'USD':
                    monto_usd_final = Decimal(pago_form.get('monto_usdt', '0.00').replace(',', '.'))
                    monto_bs_final = Decimal('0.0') 
                    pago_en_final = 'USDT'
                    forma_pago_final = 'Binance'
                    banco_final = None
                    # --- INICIO DE LA CORRECCIÓN ---
                    # Se obtiene la referencia del campo específico para USDT/Binance.
                    referencia_final = pago_form.get('referencia_usdt')
                    # --- FIN DE LA CORRECCIÓN ---
                else: # VES
                    monto_bs_final = Decimal(pago_form.get('monto_bs', '0.00').replace(',', '.'))
                    if tasa_bcv_dia > 0:
                        monto_usd_final = (monto_bs_final / tasa_bcv_dia).quantize(Decimal('0.01'))
                    else:
                        monto_usd_final = Decimal('0.0')
                    pago_en_final = 'Dolar/BCV'
                    forma_pago_final = pago_form.get('forma_pago_bs')
                    banco_final = pago_form.get('banco')
                    # Se obtiene la referencia del campo genérico.
                    referencia_final = pago_form.get('referencia')

                pago_query = """
                    INSERT INTO pagos (cliente_id, monto, monto_bs, tipo_pago, forma_pago, fecha_pago, referencia, banco, tasa_dia,
                                    estado_reporte, fecha_creacion, reportado_por_cliente, por_concepto_de, 
                                    bulk_id, is_diferencia, cuotas_cubiertas, estado_pago, pago_en)
                    VALUES (%s, %s, %s, 'Cuota', %s, %s, %s, %s, %s, 'Pendiente de Revision', %s, TRUE, %s, %s, TRUE, 0, 'Pendiente', %s);
                """
                cur.execute(pago_query, (
                    cliente['id'], monto_usd_final, monto_bs_final, 
                    forma_pago_final, pago_form.get('fecha_pago'), referencia_final, 
                    banco_final, tasa_bcv_dia, get_venezuela_current_datetime(), 
                    concepto_pago, bulk_id, pago_en_final
                ))
                
                cur.execute("UPDATE payment_orders SET status = 'PAID' WHERE id = %s", (order_id,))
                
                recalcular_totales_bulk(bulk_id)
                
                flash('✅ ¡Pago de diferencia reportado! Será verificado por un administrador.', 'success')
                
                conn.commit()
                return redirect(url_for('portal_dashboard'))
        except (psycopg2.Error, ValueError, InvalidOperation) as e:
            conn.rollback()
            flash(f'Ocurrió un error al reportar el pago de la diferencia: {e}', 'error')
            return redirect(url_for('portal_dashboard'))

    return render_template('portal_pago_unificado.html', 
                           cliente=cliente, 
                           monto_restante=monto_restante,
                           tasa_hoy=tasa_hoy, 
                           monto_a_pagar_usd=monto_a_pagar_usd,
                           monto_a_pagar_bs=monto_a_pagar_bs,
                           concepto_pago=concepto_pago,
                           is_enrollment_payment=False,
                           is_difference_payment=True,
                           bulk_id=bulk_id,
                           order_id=order_id,
                           hay_pago_pendiente_general=hay_pago_pendiente_general
                           )

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
        return render_template('pagos_por_conciliar.html', bulks=bulks_a_conciliar, pagos=[])
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
    if not conn: 
        flash("Error de conexión.", "danger")
        return redirect(url_for('pagos_por_conciliar'))
    
    try:
        with conn.cursor() as cur:
            # 1. Obtener la información del lote y bloquearlo para la transacción
            cur.execute("SELECT * FROM payment_bulks WHERE id = %s AND status = 'READY_TO_RECONCILE' FOR UPDATE", (bulk_id,))
            bulk = cur.fetchone()
            if not bulk:
                flash("El lote no está listo para conciliar o no existe.", "error")
                return redirect(url_for('pagos_por_conciliar'))

            # 2. Obtener todas las líneas de pago asociadas a este lote
            cur.execute("SELECT * FROM pagos WHERE bulk_id = %s ORDER BY fecha_creacion ASC", (bulk_id,))
            lineas = cur.fetchall()
            if not lineas:
                flash("Error crítico: No se encontraron pagos asociados a este lote.", "danger")
                return redirect(url_for('pagos_por_conciliar'))

            # 3. Determinar el tipo de pago (Inscripción o Cuota) basado en la primera línea
            tipo_pago_principal = lineas[0]['tipo_pago']
            cliente_id = bulk['cliente_id']
            monto_total_conciliado_usd = bulk['total_verified'] if bulk['currency'] == 'USD' else bulk['total_verified'] / (lineas[0]['tasa_dia'] or Decimal('1.0'))

            # 4. Actualizar el registro maestro del cliente según el tipo de pago
            if tipo_pago_principal == 'Inscripción':
                cur.execute(
                    "UPDATE clientes SET inscripcion_pagada = inscripcion_pagada + %s WHERE id = %s RETURNING id, inscripcion_pagada, inscripcion",
                    (monto_total_conciliado_usd, cliente_id)
                )
                cliente_actualizado = cur.fetchone()
                # Si la inscripción se completó, cambiar estado y calcular comisiones
                if cliente_actualizado['inscripcion_pagada'] >= cliente_actualizado['inscripcion']:
                    cur.execute("UPDATE clientes SET proceso = 'INSCRITO' WHERE id = %s", (cliente_id,))
                    flash("¡Inscripción completada y cliente ahora está INSCRITO!", "info")

            elif tipo_pago_principal == 'Cuota':
                # Cada lote de cuotas conciliado cuenta como 1 cuota pagada
                cur.execute(
                    "UPDATE clientes SET cuotas_pagadas_progresivas = cuotas_pagadas_progresivas + 1 WHERE id = %s",
                    (cliente_id,)
                )
            
            # 5. Preparar los datos para el recibo consolidado
            lineas_data = []
            for l in lineas:
                detalles = l['detalles_reporte'] or {}
                if isinstance(detalles, str):
                    try: detalles = json.loads(detalles)
                    except json.JSONDecodeError: detalles = {}

                monto_verificado = 0
                if l['estado_reporte'] == 'Inconsistente' and detalles.get('monto_verificado'):
                    monto_verificado = Decimal(detalles['monto_verificado'])
                else:
                    monto_verificado = l['monto_bs'] if bulk['currency'] == 'VES' else l['monto']

                lineas_data.append({
                    "id": l['id'], 
                    "monto_verificado": str(monto_verificado), 
                    "referencia": l['referencia'], 
                    "fecha": l['fecha_pago'].isoformat()
                })
            
            receipt_data = json.dumps({"lineas_consolidadas": lineas_data})

            # 6. Crear el recibo consolidado en la base de datos
            cur.execute("INSERT INTO receipts (bulk_id, cliente_id, currency, total, data) VALUES (%s, %s, %s, %s, %s) RETURNING id", 
                        (bulk_id, cliente_id, bulk['currency'], bulk['total_verified'], receipt_data))
            receipt_id = cur.fetchone()[0]
            
            # 7. Actualizar el estado de todos los elementos a 'Conciliado'
            cur.execute("UPDATE pagos SET estado_pago = 'Conciliado', conciliado_por_id = %s, fecha_conciliacion = NOW() WHERE bulk_id = %s", (g.admin['id'], bulk_id))
            cur.execute("UPDATE payment_orders SET status = 'CLOSED' WHERE bulk_id = %s", (bulk_id,))
            cur.execute("UPDATE payment_bulks SET status = 'RECONCILED' WHERE id = %s", (bulk_id,))
            
            registrar_accion_auditoria('CONCILIACION_LOTE', f"Concilió el lote #{bulk_id}. Recibo consolidado #{receipt_id} generado.", cliente_id)
            conn.commit()
            flash(f"Lote #{bulk_id} conciliado exitosamente. Recibo consolidado #{receipt_id} generado.", "success")
            
            # Redirigir al nuevo recibo consolidado
            return redirect(url_for('ver_recibo_consolidado', receipt_id=receipt_id))

    except (psycopg2.Error, json.JSONDecodeError, InvalidOperation) as e:
        conn.rollback()
        flash(f"Error crítico al conciliar el lote: {e}", "danger")
        logging.error(f"Error en conciliar_bulk: {traceback.format_exc()}")

    return redirect(url_for('pagos_por_conciliar'))

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

    origin = request.args.get('origin', 'perfil')

    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('home'))

    try:
        with conn.cursor() as cur:
            # --- INICIO DE LA CORRECCIÓN ---
            # Se añade la concatenación del nombre y apellido del beneficiario
            # para que esté disponible en la plantilla del contrato.
            query = """
                SELECT *, 
                       (nombre || ' ' || apellido) as nombre_apellido,
                       (beneficiario_nombre || ' ' || beneficiario_apellido) as beneficiario_nombre_apellido
                FROM clientes WHERE id = %s
            """
            # --- FIN DE LA CORRECCIÓN ---
            cur.execute(query, (client_id,))
            cliente = cur.fetchone()
        
        if not cliente:
            flash('Cliente no encontrado.', 'error')
            return redirect(url_for('home'))

        return render_template('contrato.html', 
                               cliente=cliente, 
                               modo_pre_registro=False, 
                               anio_actual=datetime.now().year,
                               origin=origin)
        
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

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s;", (session['cliente_id'],))
            cliente = cur.fetchone()
            if not cliente:
                flash('No se pudo encontrar tu perfil de cliente.', 'error')
                return redirect(url_for('portal_logout'))
            
            # CAMBIO: Usa la columna estandarizada 'inscripcion'
            inscripcion_total = cliente.get('inscripcion', Decimal('0.0')) or Decimal('0.0')
            inscripcion_pagada = cliente.get('inscripcion_pagada', Decimal('0.0')) or Decimal('0.0')
            monto_restante = inscripcion_total - inscripcion_pagada

            if monto_restante <= 0:
                flash('Tu inscripción ya ha sido pagada en su totalidad.', 'info')
                return redirect(url_for('portal_dashboard'))

            # ... (el resto de la función se mantiene igual, ya que la lógica interna no cambia)
            concepto_pago = "Abono a Inscripción" if inscripcion_pagada > 0 else "Pago de Inscripción"

            today_str = get_venezuela_current_date().strftime('%Y-%m-%d')
            cur.execute("SELECT tasa FROM historial_tasas_bcv WHERE fecha <= %s ORDER BY fecha DESC LIMIT 1", (today_str,))
            tasa_hoy = cur.fetchone()
            tasa_bcv_calculo = tasa_hoy['tasa'] if tasa_hoy and tasa_hoy['tasa'] else Decimal('0.0')
            monto_a_pagar_bs = (monto_restante * tasa_bcv_calculo).quantize(Decimal('0.01'))

            if request.method == 'POST':
                pago_form = {k: v.strip() if v else None for k, v in request.form.items()}
                
                pago_en_final = pago_form.get('pago_en')
                tipo_pago_inscripcion = pago_form.get('tipo_pago_inscripcion')
                monto_usd_a_guardar = Decimal('0.0')
                monto_reportado_bs = Decimal('0.0')
                forma_pago_final = None
                referencia_final = None 
                banco_final = None
                fecha_pago_final = pago_form.get('fecha_pago')
                currency_bulk = ''

                if pago_en_final == 'USDT':
                    if tipo_pago_inscripcion == 'abono':
                        monto_usd_a_guardar = Decimal(pago_form.get('monto_abono_usd', '0.00').replace(',', '.'))
                    else:
                        monto_usd_a_guardar = monto_restante
                    forma_pago_final = 'Binance'
                    tasa_bcv_calculo = None
                    currency_bulk = 'USD'
                    referencia_final = pago_form.get('referencia_usdt')
                else: 
                    pago_en_final = 'Dolar/BCV'
                    monto_bs_str = pago_form.get('monto_bs', '0.00').replace(',', '.')
                    monto_reportado_bs = Decimal(monto_bs_str).quantize(Decimal('0.02'))
                    if tasa_bcv_calculo > 0:
                        monto_usd_a_guardar = (monto_reportado_bs / tasa_bcv_calculo).quantize(Decimal('0.02'))
                    else:
                        monto_usd_a_guardar = Decimal('0.0')
                    forma_pago_final = pago_form.get('forma_pago_bs')
                    banco_final = pago_form.get('banco')
                    currency_bulk = 'VES'
                    referencia_final = pago_form.get('referencia')

                expected_amount_for_bulk = monto_restante if currency_bulk == 'USD' else monto_a_pagar_bs
                
                cur.execute("""
                    INSERT INTO payment_bulks (cliente_id, currency, expected_amount, status, total_verified)
                    VALUES (%s, %s, %s, 'OPEN', 0) RETURNING id
                """, (cliente['id'], currency_bulk, expected_amount_for_bulk))
                
                new_bulk_id = cur.fetchone()[0]

                pago_query = """
                    INSERT INTO pagos (cliente_id, monto, monto_bs, tipo_pago, forma_pago, fecha_pago, pago_en, por_concepto_de, 
                                       referencia, banco, tasa_dia, estado_reporte, fecha_creacion, reportado_por_cliente, 
                                       estado_pago, cuotas_cubiertas, bulk_id) 
                    VALUES (%s, %s, %s, 'Inscripción', %s, %s, %s, %s, %s, %s, %s, 'Pendiente de Revision', %s, TRUE, 'Pendiente', 0, %s);
                """
                cur.execute(pago_query, (
                    session['cliente_id'], monto_usd_a_guardar, monto_reportado_bs,
                    forma_pago_final, fecha_pago_final, pago_en_final, 
                    concepto_pago, referencia_final, banco_final, tasa_bcv_calculo, 
                    get_venezuela_current_datetime(),
                    new_bulk_id
                ))
                
                conn.commit()
                flash('✅ ¡Pago de inscripción reportado! Será verificado por un administrador.', 'success')
                return redirect(url_for('portal_dashboard'))

    except (psycopg2.Error, KeyError, ValueError, InvalidOperation) as e:
        if conn: conn.rollback()
        logging.error(f"Error en portal_pagar_inscripcion: {traceback.format_exc()}")
        flash('Ocurrió un error inesperado al procesar tu solicitud de pago.', 'error')
        return redirect(url_for('portal_dashboard'))

    return render_template('portal_pago_unificado.html', 
                           cliente=cliente, 
                           monto_restante=monto_restante,
                           tasa_hoy=tasa_hoy, 
                           monto_a_pagar_usd=monto_restante,
                           monto_a_pagar_bs=monto_a_pagar_bs,
                           concepto_pago=concepto_pago,
                           is_enrollment_payment=True)

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
            
            # --- INICIO DE LA CORRECCIÓN LÓGICA ---
            cur.execute("""
                SELECT id, fecha_creacion as fecha, 'Pago' as tipo_base, 
                       json_build_object(
                           'monto', monto, 'estado', estado_pago, 'concepto', por_concepto_de, 'tipo_pago', tipo_pago
                       ) as data
                FROM pagos WHERE cliente_id = %s
                UNION ALL
                SELECT id, fecha_creacion as fecha, 'Gestion' as tipo_base,
                       json_build_object('nota', nota, 'tipo_gestion', tipo_gestion) as data
                FROM gestiones_cobranza WHERE cliente_id = %s
                ORDER BY fecha DESC;
            """, (cliente_id, cliente_id))
            historial_raw = cur.fetchall()
            
            cur.execute("""
                SELECT COALESCE(SUM(monto), 0) FROM pagos 
                WHERE cliente_id = %s AND estado_pago = 'Conciliado' AND tipo_pago != 'Inscripción'
            """, (cliente_id,))
            total_pagado_plan = cur.fetchone()[0]

            cur.execute("""
                SELECT COALESCE(SUM(monto), 0) FROM pagos 
                WHERE cliente_id = %s AND estado_pago = 'Conciliado' AND tipo_pago = 'Inscripción'
            """, (cliente_id,))
            total_inscripcion_pagada = cur.fetchone()[0]
            
            valor_cuota = cliente.get('valor_cuota', Decimal('0.0')) or Decimal('0.0')
            cuotas_totales = cliente.get('cuotas_totales', 0) or 0
            plan_contratado = Decimal(cliente.get('plan_contratado') or '0.0')

            total_a_pagar_plan = valor_cuota * cuotas_totales
            sobrecosto_administrativo = total_a_pagar_plan - plan_contratado
            saldo_pendiente_plan = total_a_pagar_plan - total_pagado_plan
        
            # --- FIN DE LA CORRECCIÓN LÓGICA ---

            historial_unificado = []
            for item in historial_raw:
                data = item['data']
                if isinstance(data, str):
                    try: data = json.loads(data)
                    except json.JSONDecodeError: data = {}
                
                evento = {'fecha': item['fecha']}
                
                if item['tipo_base'] == 'Pago':
                    estado = data.get('estado', 'N/A')
                    evento['tipo'] = f"Pago {estado}"
                    evento['clase_css'] = 'bg-slate-100'
                    if estado == 'Conciliado': evento['clase_css'] = 'bg-green-100 text-green-800'
                    elif estado == 'Pendiente': evento['tipo'] = 'Pago en Revisión'; evento['clase_css'] = 'bg-yellow-100 text-yellow-800'
                    
                    evento['descripcion'] = data.get('concepto', 'Pago general')
                    evento['monto'] = data.get('monto')
                    if estado == 'Conciliado':
                        evento['url'] = url_for('generar_recibo_pago', pago_id=item['id'])

                elif item['tipo_base'] == 'Gestion':
                    evento['tipo'] = data.get('tipo_gestion', 'Gestión')
                    evento['clase_css'] = 'bg-slate-100 text-slate-800'
                    evento['descripcion'] = data.get('nota', 'Sin descripción.')
                
                historial_unificado.append(evento)

            return {
                'cliente': cliente,
                'historial': historial_unificado,
                'total_pagado_plan': total_pagado_plan,
                'total_inscripcion_pagada': total_inscripcion_pagada,
                'valor_plan': plan_contratado,
                'saldo_pendiente_plan': saldo_pendiente_plan,
                'total_a_pagar_plan': total_a_pagar_plan,
                'sobrecosto_administrativo': sobrecosto_administrativo
            }

    except (psycopg2.Error, json.JSONDecodeError, KeyError) as e:
        logging.error(f"Error getting account statement for client {cliente_id}: {e}")
        flash('Ocurrió un error al generar el estado de cuenta.', 'error')
        return None

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
    # NUEVO: Se obtiene el modelo del formulario
    modelo_ofertado = request.form.get('modelo_ofertado')
    cliente_id = session['cliente_id']

    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('portal_dashboard'))
    
    try:
        with conn.cursor() as cur:
            if not cuotas_ofertadas or not cuotas_ofertadas.isdigit() or int(cuotas_ofertadas) <= 0:
                flash("Debe ingresar un número válido de cuotas para la oferta.", 'error')
                return redirect(url_for('portal_dashboard'))

            # ACTUALIZADO: Se añade modelo_ofertado a la consulta INSERT
            cur.execute("""
                INSERT INTO ofertas (cliente_id, cuotas_ofertadas, modelo_ofertado, fecha_oferta, estado_oferta) 
                VALUES (%s, %s, %s, %s, 'activa')
            """, (cliente_id, int(cuotas_ofertadas), modelo_ofertado, get_venezuela_current_date()))
            
            conn.commit()
            # ACTUALIZADO: El mensaje de auditoría ahora es más descriptivo
            registrar_accion_auditoria('REGISTRO_OFERTA_CLIENTE', f"Cliente ofertó {cuotas_ofertadas} cuota(s) por el modelo '{modelo_ofertado}'.")
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
        return redirect(url_for('home'))

    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", "error")
        return redirect(url_for('home'))

    counts = { 'pagos_pendientes': 0, 'reportes_pendientes': 0, 'citas': 0, 'congelamientos': 0, 'retiros': 0 }
    if is_admin_view:
        try:
            with conn.cursor() as cur_counts:
                cur_counts.execute("SELECT COUNT(*) FROM pagos WHERE estado_pago = 'Pendiente' AND (reportado_por_cliente = FALSE OR estado_reporte = 'Aprobado')")
                counts['pagos_pendientes'] = cur_counts.fetchone()[0]
                
                cur_counts.execute("SELECT COUNT(*) FROM pagos WHERE reportado_por_cliente = TRUE AND estado_reporte = 'Pendiente de Revision'")
                counts['reportes_pendientes'] = cur_counts.fetchone()[0]
                
                cur_counts.execute("SELECT tipo_solicitud, COUNT(*) as total FROM solicitudes WHERE estado = 'Pendiente' GROUP BY tipo_solicitud")
                for row in cur_counts.fetchall():
                    if row['tipo_solicitud'] == 'Cita': counts['citas'] = row['total']
                    elif row['tipo_solicitud'] == 'Congelamiento': counts['congelamientos'] = row['total']
                    elif row['tipo_solicitud'] == 'Retiro': counts['retiros'] = row['total']
        except psycopg2.Error as e:
            logging.error(f"Error al contar pendientes en ver_reporte: {e}")

    try:
        with conn.cursor() as cur:
            query = """
                SELECT p.*, p.bulk_id, c.nombre || ' ' || c.apellido as nombre_apellido, c.cedula, 
                       c.valor_cuota, c.inscripcion_monto, c.id as cliente_id, c.moneda_pago,
                       b.expected_amount as bulk_expected_amount,
                       b.total_verified as bulk_total_verified,
                       b.status as bulk_status,
                       b.currency as bulk_currency
                FROM pagos p 
                JOIN clientes c ON p.cliente_id = c.id 
                LEFT JOIN payment_bulks b ON p.bulk_id = b.id
                WHERE p.id = %s
            """
            cur.execute(query, (pago_id,))
            pago_row = cur.fetchone()

            if not pago_row:
                flash("Reporte de pago no encontrado.", "error"); return redirect(url_for('home'))
            
            pago = dict(pago_row)
            
            cliente_para_plantilla = None
            if is_client_view:
                cur.execute("SELECT * FROM clientes WHERE id = %s", (session['cliente_id'],))
                cliente_para_plantilla = cur.fetchone()

            # --- INICIO DE LA MODIFICACIÓN ---
            pagos_del_mismo_bulk = []
            bulk_totals = None
            pago_pendiente_para_acciones = pago
            is_complex_process = False # Variable para controlar la vista en la plantilla

            if pago.get('bulk_id'):
                cur.execute("SELECT * FROM pagos WHERE bulk_id = %s ORDER BY fecha_creacion ASC", (pago['bulk_id'],))
                pagos_del_mismo_bulk_raw = cur.fetchall()

                # --- NUEVA LÓGICA PARA PROCESAR DETALLES ---
                pagos_procesados = []
                for p_raw in pagos_del_mismo_bulk_raw:
                    p_dict = dict(p_raw)
                    detalles = p_dict.get('detalles_reporte') or {}
                    if isinstance(detalles, str):
                        try:
                            # Reemplaza el string JSON con el diccionario parseado
                            p_dict['detalles_reporte'] = json.loads(detalles)
                        except json.JSONDecodeError:
                            p_dict['detalles_reporte'] = {} # Asegura que sea un dict si el parseo falla
                    pagos_procesados.append(p_dict)
                
                pagos_del_mismo_bulk = pagos_procesados
                # --- FIN DE LA NUEVA LÓGICA ---

                # Un proceso se considera "complejo" si ya contiene al menos un pago marcado como 'Inconsistente'.
                is_complex_process = any(p['estado_reporte'] == 'Inconsistente' for p in pagos_del_mismo_bulk)

                if pagos_del_mismo_bulk:
                    ultimo_pago_pendiente = next((p for p in reversed(pagos_del_mismo_bulk) if p['estado_reporte'] == 'Pendiente de Revision'), None)
                    pago_pendiente_para_acciones = dict(ultimo_pago_pendiente) if ultimo_pago_pendiente else pago

                    monto_esperado_bulk = pago['bulk_expected_amount'] or Decimal('0.0')
                    
                    monto_reportado_total = sum(
                        (p.get('monto_bs') or Decimal('0.0')) if pago.get('bulk_currency') == 'VES' else (p.get('monto') or Decimal('0.0'))
                        for p in pagos_del_mismo_bulk
                    )
                    
                    monto_verificado_total = Decimal('0.0')
                    for p_item in pagos_del_mismo_bulk:
                        # detalles ya es un diccionario gracias a la lógica anterior
                        detalles = p_item.get('detalles_reporte') or {}
                        
                        # Sumamos el monto verificado si existe en los detalles
                        if detalles and detalles.get('monto_verificado'):
                            monto_verificado_total += Decimal(detalles['monto_verificado'])
                        # Si el pago está aprobado pero no es inconsistente, se suma su monto total.
                        elif p_item['estado_reporte'] == 'Aprobado':
                             if pago.get('bulk_currency') == 'VES':
                                monto_verificado_total += p_item.get('monto_bs') or Decimal('0.0')
                             else:
                                monto_verificado_total += p_item.get('monto') or Decimal('0.0')

                    diferencia_pendiente = monto_esperado_bulk - monto_verificado_total
                    
                    bulk_totals = {
                        'esperado': monto_esperado_bulk,
                        'reportado': monto_reportado_total,
                        'verificado': monto_verificado_total,
                        'pendiente': diferencia_pendiente if diferencia_pendiente > 0 else Decimal('0.0'),
                        'currency': pago.get('bulk_currency') or 'USD'
                    }
            
            return render_template(
                'ver_reporte.html',
                pago=pago,
                cliente=cliente_para_plantilla, 
                is_client_view=is_client_view,
                is_admin_view=is_admin_view,
                pagos_del_mismo_bulk=pagos_del_mismo_bulk,
                pago_pendiente_para_acciones=pago_pendiente_para_acciones,
                bulk_totals=bulk_totals,
                is_complex_process=is_complex_process,
                counts=counts
            )
            # --- FIN DE LA MODIFICACIÓN ---

    except (psycopg2.Error, json.JSONDecodeError, KeyError) as e:
        logging.error(f"Error CRÍTICO en ver_reporte para pago_id {pago_id}: {traceback.format_exc()}")
        flash('Ocurrió un error grave al cargar el reporte. El problema ha sido registrado.', 'danger')
        if is_client_view:
            return redirect(url_for('portal_dashboard'))
        else:
            return redirect(url_for('hub'))
# =================================================================================
# ===== RUTA 3: NUEVA RUTA PARA VALIDAR PAGOS INDIVIDUALES DENTRO DE UN BULK =====
# =================================================================================
@app.route('/admin/pagos/validar/<int:pago_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente', 'administradora')
def validar_pago_individual(pago_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión.", "danger")
        return redirect(url_for('reportes_por_revisar'))

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, bulk_id, cliente_id FROM pagos WHERE id = %s AND estado_reporte = 'Pendiente de Revision' FOR UPDATE", (pago_id,))
            pago = cur.fetchone()

            if not pago or not pago['bulk_id']:
                flash("Este pago no se puede validar o ya fue procesado.", "warning")
                return redirect(request.referrer or url_for('reportes_por_revisar'))

            bulk_id = pago['bulk_id']
            
            cur.execute("UPDATE pagos SET estado_reporte = 'Aprobado', revisado_por_id=%s, fecha_revision=NOW() WHERE id = %s", (g.admin['id'], pago_id))
            
            # --- INICIO DE LA CORRECCIÓN ---
            # Se llama a la función recalcular_totales_bulk que ya contiene la lógica
            # para actualizar el estado del bulk si los montos coinciden.
            recalcular_totales_bulk(bulk_id)
            # --- FIN DE LA CORRECCIÓN ---
            
            registrar_accion_auditoria('VALIDACION_PAGO_INDIVIDUAL', f"Validó el reporte de pago #{pago_id} del proceso #{bulk_id}.", pago['cliente_id'])
            conn.commit()
            flash(f"Pago #{pago_id} validado correctamente. El total del bulk ha sido actualizado.", "success")

    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Error al validar el pago: {e}", "danger")
        logging.error(f"Error en validar_pago_individual para pago_id {pago_id}: {traceback.format_exc()}")

    return redirect(request.referrer or url_for('reportes_por_revisar'))

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

# --- MÓDULO DE GESTIÓN DE USUARIOS UNIFICADO --

@app.route('/admin/gestion_usuarios')
@admin_required
@rol_requerido('superadmin', 'gerente')
def gestion_usuarios():
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", "danger")
        return redirect(url_for('hub'))
    try:
        with conn.cursor() as cur:
            # --- CORRECCIÓN: Se añade la columna es_comercial ---
            cur.execute("SELECT id, nombre_completo, usuario, rol, estatus, es_comercial FROM administradores ORDER BY nombre_completo")
            admins = cur.fetchall()
            
            cur.execute("SELECT id, nombre_completo, usuario, estatus FROM contadores ORDER BY nombre_completo")
            contadores = cur.fetchall()

    except psycopg2.Error as e:
        flash(f"Error al consultar la lista de usuarios: {e}", "danger")
        admins = []
        contadores = []
    
    return render_template('gestion_usuarios.html', admins=admins, contadores=contadores)

# --- INICIO: NUEVA RUTA PARA GESTIONAR CAPACIDAD COMERCIAL ---
@app.route('/admin/toggle_comercial/<int:user_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def toggle_comercial(user_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", "danger")
        return redirect(url_for('gestion_usuarios'))

    try:
        with conn.cursor() as cur:
            # Asegurarse que el usuario a modificar existe
            cur.execute("SELECT usuario, rol, es_comercial FROM administradores WHERE id = %s", (user_id,))
            user = cur.fetchone()
            if not user:
                flash("Usuario administrador no encontrado.", "danger")
                return redirect(url_for('gestion_usuarios'))

            # Lógica de seguridad para no modificar superadmins si no eres uno
            if user['rol'] == 'superadmin' and g.admin['rol'] != 'superadmin':
                flash("No tienes permisos para modificar la capacidad comercial de un Superadmin.", "danger")
                return redirect(url_for('gestion_usuarios'))

            # Usamos NOT para invertir el valor booleano actual
            cur.execute("UPDATE administradores SET es_comercial = NOT es_comercial WHERE id = %s", (user_id,))
            
            nuevo_estado = "Activada" if not user['es_comercial'] else "Desactivada"
            registrar_accion_auditoria('CAMBIO_CAPACIDAD_COMERCIAL', f"Capacidad comercial {nuevo_estado} para el usuario '{user['usuario']}'.")
            
            conn.commit()
            flash(f"Se ha cambiado la capacidad comercial para el usuario '{user['usuario']}'.", "success")

    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Error al actualizar la capacidad del usuario: {e}", "danger")

    return redirect(url_for('gestion_usuarios'))
# --- FIN: NUEVA RUTA ---

@app.route('/admin/agregar_usuario_unificado', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def agregar_usuario_unificado():
    # ... (Implementation for adding users)
    tipo_usuario = request.form.get('tipo_usuario')
    nombre_completo = request.form.get('nombre_completo')
    usuario = request.form.get('usuario')
    password = request.form.get('password')
    rol = request.form.get('rol') # Solo para admins

    if not all([tipo_usuario, nombre_completo, usuario, password]):
        flash("Todos los campos son obligatorios.", "danger")
        return redirect(url_for('gestion_usuarios'))

    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", "danger")
        return redirect(url_for('gestion_usuarios'))

    try:
        with conn.cursor() as cur:
            hashed_password = generate_password_hash(password)
            
            if tipo_usuario == 'admin':
                if not rol:
                    flash("El rol es obligatorio para un administrador.", "danger")
                    return redirect(url_for('gestion_usuarios'))
                
                cur.execute(
                    "INSERT INTO administradores (nombre_completo, usuario, password_hash, rol, estatus) VALUES (%s, %s, %s, %s, 'Activo')",
                    (nombre_completo, usuario, hashed_password, rol)
                )
                registrar_accion_auditoria('CREACION_USUARIO_ADMIN', f"Creó al usuario admin '{usuario}' ({nombre_completo}).")
                flash(f"Usuario administrador '{usuario}' creado exitosamente.", "success")

            elif tipo_usuario == 'contador':
                cur.execute(
                    "INSERT INTO contadores (nombre_completo, usuario, password_hash, estatus) VALUES (%s, %s, %s, 'Activo')",
                    (nombre_completo, usuario, hashed_password)
                )
                registrar_accion_auditoria('CREACION_USUARIO_CONTADOR', f"Creó al usuario contador '{usuario}' ({nombre_completo}).")
                flash(f"Usuario contador '{usuario}' creado exitosamente.", "success")
            
            else:
                flash("Tipo de usuario no válido.", "danger")

            conn.commit()

    except psycopg2.IntegrityError:
        conn.rollback()
        flash(f"El nombre de usuario '{usuario}' ya existe. Por favor, elija otro.", "danger")
    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Error al crear el usuario: {e}", "danger")

    return redirect(url_for('gestion_usuarios'))


@app.route('/admin/cambiar_estado_usuario/<string:user_type>/<int:user_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def cambiar_estado_usuario(user_type, user_id):
    # ... (Implementation for changing user status)
    if user_type not in ['admin', 'contador']:
        flash("Tipo de usuario no válido.", "danger")
        return redirect(url_for('gestion_usuarios'))

    table_name = 'administradores' if user_type == 'admin' else 'contadores'
    
    conn = get_db()
    if not conn:
        flash("Error de conexión.", "danger")
        return redirect(url_for('gestion_usuarios'))

    try:
        with conn.cursor() as cur:
            # Obtiene el estado actual del usuario
            cur.execute(f"SELECT usuario, estatus, rol FROM {table_name} WHERE id = %s" if user_type == 'admin' else f"SELECT usuario, estatus FROM {table_name} WHERE id = %s", (user_id,))
            user = cur.fetchone()

            if not user:
                flash("Usuario no encontrado.", "danger")
                return redirect(url_for('gestion_usuarios'))
            
            # Lógica de seguridad específica para administradores
            if user_type == 'admin' and user['rol'] == 'superadmin' and g.admin['rol'] != 'superadmin':
                flash("No tienes permisos para cambiar el estado de un Superadmin.", "danger")
                return redirect(url_for('gestion_usuarios'))

            # Cambia el estado
            nuevo_estatus = 'Inactivo' if user['estatus'] == 'Activo' else 'Activo'
            cur.execute(f"UPDATE {table_name} SET estatus = %s WHERE id = %s", (nuevo_estatus, user_id))
            conn.commit()
            
            registrar_accion_auditoria('CAMBIO_ESTADO_USUARIO', f"Cambió el estado del usuario {user_type} '{user['usuario']}' a '{nuevo_estatus}'.")
            flash(f"El estado del usuario '{user['usuario']}' ha sido actualizado a '{nuevo_estatus}'.", "success")

    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Error al cambiar el estado del usuario: {e}", "danger")
        
    return redirect(url_for('gestion_usuarios'))

@app.route('/admin/editar_usuario/<string:user_type>/<int:user_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def editar_usuario(user_type, user_id):
    # ... (Implementation for editing user details)
    if user_type not in ['admin', 'contador']:
        flash("Tipo de usuario no válido.", "danger")
        return redirect(url_for('gestion_usuarios'))

    table_name = 'administradores' if user_type == 'admin' else 'contadores'
    nombre_completo = request.form.get(f'nombre_completo_edit_{user_type}_{user_id}')
    usuario = request.form.get(f'usuario_edit_{user_type}_{user_id}')

    if not nombre_completo or not usuario:
        flash("El nombre completo y el usuario no pueden estar vacíos.", "danger")
        return redirect(url_for('gestion_usuarios'))

    conn = get_db()
    if not conn:
        flash("Error de conexión.", "danger")
        return redirect(url_for('gestion_usuarios'))

    try:
        with conn.cursor() as cur:
            # Lógica de seguridad específica para administradores
            if user_type == 'admin':
                cur.execute("SELECT rol FROM administradores WHERE id = %s", (user_id,))
                user_to_edit = cur.fetchone()
                if user_to_edit and user_to_edit['rol'] == 'superadmin' and g.admin['rol'] != 'superadmin':
                    flash("No tienes permisos para editar a un Superadmin.", "danger")
                    return redirect(url_for('gestion_usuarios'))
            
            # Actualiza el usuario
            cur.execute(f"UPDATE {table_name} SET nombre_completo = %s, usuario = %s WHERE id = %s", (nombre_completo, usuario, user_id))
            conn.commit()
            
            registrar_accion_auditoria(f'EDICION_USUARIO_{user_type.upper()}', f"Editó al usuario {user_type} ID {user_id}. Nuevo nombre: '{nombre_completo}', nuevo usuario: '{usuario}'.")
            flash(f"Usuario '{usuario}' actualizado exitosamente.", "success")
            
    except psycopg2.IntegrityError:
        conn.rollback()
        flash(f"El nombre de usuario '{usuario}' ya existe. Por favor, elija otro.", "danger")
    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Error al actualizar el usuario: {e}", "danger")

    return redirect(url_for('gestion_usuarios'))


@app.route('/admin/resetear_password/<string:user_type>/<int:user_id>', methods=['POST'])
@admin_required
@rol_requerido('superadmin', 'gerente')
def resetear_password(user_type, user_id):
    # ... (Implementation for resetting passwords)
    if user_type not in ['admin', 'contador']:
        flash("Tipo de usuario no válido.", "danger")
        return redirect(url_for('gestion_usuarios'))

    table_name = 'administradores' if user_type == 'admin' else 'contadores'
    
    conn = get_db()
    if not conn:
        flash("Error de conexión.", "danger")
        return redirect(url_for('gestion_usuarios'))

    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT usuario, rol FROM {table_name} WHERE id = %s" if user_type == 'admin' else f"SELECT usuario FROM {table_name} WHERE id = %s", (user_id,))
            user = cur.fetchone()

            if not user:
                flash("Usuario no encontrado.", "danger")
                return redirect(url_for('gestion_usuarios'))
            
            # Lógica de seguridad específica para administradores
            if user_type == 'admin' and user['rol'] == 'superadmin' and g.admin['rol'] != 'superadmin':
                flash("No tienes permisos para resetear la contraseña de un Superadmin.", "danger")
                return redirect(url_for('gestion_usuarios'))

            # Genera y actualiza la nueva contraseña
            caracteres = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            nueva_password = "".join(random.choice(caracteres) for i in range(10))
            hashed_password = generate_password_hash(nueva_password)
            
            cur.execute(f"UPDATE {table_name} SET password_hash = %s WHERE id = %s", (hashed_password, user_id))
            conn.commit()
            
            registrar_accion_auditoria('RESETEO_PASSWORD', f"Reseteó la contraseña para el usuario {user_type} '{user['usuario']}'.")
            flash(f"¡Contraseña reseteada! La nueva contraseña temporal para '{user['usuario']}' es: {nueva_password}", "success")

    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Error al resetear la contraseña: {e}", "danger")

    return redirect(url_for('gestion_usuarios'))
    
# touch: invalidate template cache 2025-09-07
# --- FIN DEL NUEVO MÓDULO ---


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
