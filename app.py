import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, g, flash, redirect, url_for, session, Response
from dotenv import load_dotenv
from decimal import Decimal
from datetime import datetime, timedelta
import random
from werkzeug.security import check_password_hash, generate_password_hash
from functools import wraps
import io
import csv
import logging

# Configuración del logging para que sea visible en Render
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'una-clave-secreta-por-defecto-para-desarrollo')

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
                # Si el admin está logueado, actualizamos su última hora de actividad
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

# --- Funciones de utilidad ---
def get_nombre_mes(month_number):
    meses = {1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"}
    return meses.get(month_number, "")

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
        raise ConnectionError("No se pudo establecer conexión con la base de datos para la auditoría.")
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
            return render_template('admin_login.html', anio_actual=datetime.now().year)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM administradores WHERE usuario = %s", (usuario,))
            admin = cur.fetchone()
            if admin and check_password_hash(admin['password_hash'], password):
                session.clear()
                session['admin_id'] = admin['id']
                # Se establece el estado como EN LÍNEA al iniciar sesión
                cur.execute("UPDATE administradores SET ultimo_login = NOW(), estatus_online = TRUE WHERE id = %s", (admin['id'],))
                conn.commit()
                flash(f"¡Bienvenido de nuevo, {admin['usuario']}!", 'success')
                return redirect(url_for('hub'))
            else:
                flash('Usuario o contraseña incorrectos.', 'danger')
    return render_template('admin_login.html', anio_actual=datetime.now().year)

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
                # Se establece el estado como DESCONECTADO al cerrar sesión
                cur.execute("UPDATE administradores SET estatus_online = FALSE WHERE id = %s", (admin_id,))
                conn.commit()
    session.clear()
    flash('Has cerrado la sesión exitosamente.', 'info')
    return redirect(url_for('admin_login'))

# --- RUTAS PRINCIPALES ---
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
            # Consulta híbrida: un usuario está en línea si su estatus es TRUE
            # Y su última actividad fue en los últimos 5 minutos.
            cur.execute("""
                SELECT 
                    usuario, 
                    ultimo_login,
                    (estatus_online AND ultimo_visto > NOW() - INTERVAL '5 minutes') AS esta_en_linea
                FROM administradores 
                ORDER BY usuario
            """)
            usuarios = cur.fetchall()
    return render_template('hub.html', anio_actual=datetime.now().year, usuarios=usuarios)

# --- (Aquí comienza el resto de tu código, que no necesita cambios) ---

@app.route('/registrar')
@admin_required
def registrar():
    return render_template('registrar.html')
    
@app.route('/registrar_cliente', methods=['POST'])
@admin_required
def registrar_cliente():
    form_data = {k: v.strip() if isinstance(v, str) else v for k, v in request.form.items()}
    
    campos_obligatorios = {
        'nombre_apellido': 'Nombre y Apellido',
        'cedula': 'Cédula',
        'contrato_nro': 'Número de Contrato',
        'telefono': 'Teléfono',
        'plan_contratado': 'Plan Contratado',
        'valor_cuota': 'Valor de la Cuota'
    }
    campos_faltantes = [nombre_legible for campo_html, nombre_legible in campos_obligatorios.items() if not form_data.get(campo_html)]
    if campos_faltantes:
        mensaje_error = f"Error: Los siguientes campos son obligatorios: {', '.join(campos_faltantes)}."
        flash(mensaje_error, 'error')
        return redirect(url_for('registrar'))

    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('registrar'))
    try:
        with conn.cursor() as cur:
            nombre_completo = form_data.get('nombre_apellido').split(' ', 1)
            nombre = nombre_completo[0]
            apellido = nombre_completo[1] if len(nombre_completo) > 1 else ''
            
            insert_dict = {
                'nombre': nombre, 
                'apellido': apellido, 
                'cedula': form_data.get('cedula').replace(' ', ''),
                'cuotas_pagadas_progresivas': 0,
                'cuotas_pagadas_regresivas': 0
            }
            optional_fields = ['contrato_nro', 'telefono', 'asesor', 'responsable', 'fecha_ingreso', 'grupo', 'plan_contratado', 'cuotas_totales', 'moneda_pago', 'valor_cuota', 'inscripcion_monto', 'proceso']
            for field in optional_fields:
                if form_data.get(field):
                    insert_dict[field] = form_data[field]
            
            columns = list(insert_dict.keys())
            values = [insert_dict[col] for col in columns]
            
            query = f"INSERT INTO clientes ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(values))}) RETURNING id"
            cur.execute(query, values)
            new_client_id = cur.fetchone()[0]
            
            descripcion_audit = f"Registró al nuevo cliente: {form_data.get('nombre_apellido')} (C.I. {form_data.get('cedula')})."
            registrar_accion_auditoria('REGISTRO_CLIENTE', descripcion_audit, new_client_id)
            
            conn.commit()
            flash(f"¡Cliente '{form_data.get('nombre_apellido')}' registrado exitosamente!", 'success')

    except psycopg2.IntegrityError:
        conn.rollback()
        flash(f"Registro fallido: La cédula '{form_data.get('cedula')}' ya existe.", 'error')
    except (psycopg2.Error, ValueError, ConnectionError) as e:
        conn.rollback()
        flash(f"Registro fallido: Ocurrió un error de base de datos: {e}", 'error')
        
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
            return render_template('registrar_pago.html', cliente=cliente)
        if tipo_pago == 'Inscripción' and inscripcion_pagada >= inscripcion_total:
            flash('Error: La inscripción para este cliente ya ha sido completada.', 'error')
            return render_template('registrar_pago.html', cliente=cliente)
        if pago_form.get('forma_pago') != 'Efectivo' and not pago_form.get('referencia'):
            flash('Error: La referencia es obligatoria para pagos por transferencia.', 'error')
            return render_template('registrar_pago.html', cliente=cliente)
        try:
            with conn.cursor() as cur:
                pago_query = """
                    INSERT INTO pagos (cliente_id, monto, tipo_pago, forma_pago, fecha_pago, 
                                        pago_en, por_concepto_de, referencia, banco, lugar_emision,
                                        tasa_dia, monto_bs, estado_pago, cuotas_cubiertas)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Pendiente', 0);
                """
                cur.execute(pago_query, (client_id, pago_form['monto'], tipo_pago, pago_form['forma_pago'], pago_form['fecha_pago'], pago_form.get('pago_en'), pago_form.get('por_concepto_de'), pago_form.get('referencia'), pago_form.get('banco'), pago_form.get('lugar_emision'), pago_form.get('tasa_dia'), pago_form.get('monto_bs')))
                conn.commit()
                flash(f"¡Pago de {tipo_pago} registrado como PENDIENTE! Ahora debe ser conciliado.", 'success')
                return redirect(url_for('consulta', busqueda=cliente['cedula']))
        except (psycopg2.Error, ValueError, TypeError) as e:
            conn.rollback()
            flash(f'Ocurrió un error al registrar el pago: {e}', 'error')
            return render_template('registrar_pago.html', cliente=cliente)
    return render_template('registrar_pago.html', cliente=cliente)

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
        flash(f"Ocurrió un error al anular el recibo: {e}", 'error')
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
    current_year = datetime.now().year
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

            hoy = datetime.now().date()
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
            cur.execute("SELECT id, (nombre || ' ' || apellido) as nombre_apellido, cedula, cuotas_pagadas_progresivas, meses_retraso_entrega FROM clientes WHERE proceso ILIKE 'Ahorrador' AND cuotas_pagadas_progresivas >= (12 + meses_retraso_entrega) AND estatus ILIKE 'activo' ORDER BY nombre, apellido;")
            clientes_elegibles_ahorro = cur.fetchall()
            
            cur.execute("SELECT o.cuotas_ofertadas, c.id, (c.nombre || ' ' || apellido) as nombre_apellido, c.cedula FROM ofertas o JOIN clientes c ON o.cliente_id = c.id WHERE o.estado_oferta = 'activa' AND c.proceso ILIKE 'Ahorrador' ORDER BY o.cuotas_ofertadas DESC, o.fecha_oferta ASC;")
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
            
            cur.execute("SELECT id, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE proceso ILIKE 'Ahorrador' AND cuotas_pagadas_progresivas >= (12 + meses_retraso_entrega) AND estatus ILIKE 'activo';")
            ganadores_ahorro = cur.fetchall()
            ids_ganadores_ahorro = [g['id'] for g in ganadores_ahorro]
            ids_ya_ganadores.update(ids_ganadores_ahorro)
            
            ganador_oferta = None
            cur.execute("SELECT c.id, (c.nombre || ' ' || apellido) as nombre_apellido, c.ignorar_penalidad_puntualidad, o.cuotas_ofertadas FROM ofertas o JOIN clientes c ON o.cliente_id = c.id WHERE o.estado_oferta = 'activa' AND c.proceso ILIKE 'Ahorrador' AND c.id NOT IN %s;", (tuple(ids_ya_ganadores) if ids_ya_ganadores else (0,),))
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
    try:
        import pytz
        vz_tz = pytz.timezone('America/Caracas')
        fecha_actual_vet = datetime.now(vz_tz).strftime('%Y-%m-%d')
    except ImportError:
        fecha_actual_vet = datetime.now().strftime('%Y-%m-%d')

    fecha_filtro_str = request.args.get('fecha', fecha_actual_vet)
    
    fecha_para_sql = fecha_filtro_str
    try:
        fecha_obj = datetime.strptime(fecha_filtro_str, '%m/%d/%Y')
        fecha_para_sql = fecha_obj.strftime('%Y-%m-%d')
    except ValueError:
        pass
    
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return render_template('auditoria.html', logs=logs, anio_actual=datetime.now().year, fecha_filtro=fecha_filtro_str)
    
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

    return render_template('auditoria.html', logs=logs, anio_actual=datetime.now().year, fecha_filtro=fecha_filtro_str)

@app.route('/descargar_reporte_auditoria')
@admin_required
@rol_requerido('superadmin')
def descargar_reporte_auditoria():
    try:
        import pytz
        vz_tz = pytz.timezone('America/Caracas')
        fecha_actual_vet = datetime.now(vz_tz).strftime('%Y-%m-%d')
    except ImportError:
        fecha_actual_vet = datetime.now().strftime('%Y-%m-%d')
        
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
            return render_template('portal_login.html', anio_actual=datetime.now().year)
        conn = get_db()
        if not conn:
            flash('Error de conexión con el servidor. Intente más tarde.', 'error')
            return render_template('portal_login.html', anio_actual=datetime.now().year)
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
    return render_template('portal_login.html', anio_actual=datetime.now().year)

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

            hoy, dia_de_vencimiento = datetime.now().date(), 3
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

    mes_actual = get_nombre_mes(datetime.now().date().month)
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
                pago_query = "INSERT INTO pagos (cliente_id, monto, tipo_pago, forma_pago, fecha_pago, pago_en, por_concepto_de, referencia, banco, lugar_emision, tasa_dia, monto_bs, estado_pago, cuotas_cubiertas) VALUES (%s, %s, 'Cuota', %s, %s, %s, %s, %s, %s, 'Acarigua', %s, %s, 'Pendiente', 0);"
                cur.execute(pago_query, (session['cliente_id'], pago_form['monto'], pago_form['forma_pago'], pago_form['fecha_pago'], pago_form.get('pago_en'), pago_form.get('por_concepto_de'), pago_form.get('referencia'), pago_form.get('banco'), pago_form.get('tasa_dia'), pago_form.get('monto_bs')))
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
            fecha_generacion = datetime.now().strftime('%d/%m/%Y')
            return render_template('estado_cuenta.html', cliente=cliente, pagos=pagos, fecha_generacion=fecha_generacion)
    except psycopg2.Error as e:
        flash(f'Ocurrió un error al generar el estado de cuenta: {e}', 'error')
        return redirect(url_for('portal_dashboard'))

@app.route('/portal/logout')
def portal_logout():
    session.clear()
    flash('Has cerrado sesión exitosamente.', 'success')
    return redirect(url_for('portal_login'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)