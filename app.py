import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, g, flash, redirect, url_for, session
from dotenv import load_dotenv
from decimal import Decimal
from datetime import datetime, timedelta
import random
from werkzeug.security import check_password_hash, generate_password_hash
from functools import wraps

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'una-clave-secreta-por-defecto-para-desarrollo')

# --- CONFIGURACIÓN DE LA SESIÓN Y CARGA DE USUARIO ---
@app.before_request
def setup_session_and_user():
    session.permanent = True
    app.permanent_session_lifetime = timedelta(minutes=30)
    g.admin = None
    g.cliente = None
    admin_id = session.get('admin_id')
    cliente_id = session.get('cliente_id')
    db = get_db()
    if db:
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if admin_id:
                cur.execute("SELECT * FROM administradores WHERE id = %s", (admin_id,))
                g.admin = cur.fetchone()
            elif cliente_id:
                cur.execute("SELECT id, nombre, apellido FROM clientes WHERE id = %s", (cliente_id,))
                g.cliente = cur.fetchone()

def get_db():
    if 'db' not in g:
        DATABASE_URL = os.getenv('DATABASE_URL')
        if not DATABASE_URL:
            raise ValueError("FATAL: La variable de entorno DATABASE_URL no está configurada.")
        try:
            g.db = psycopg2.connect(DATABASE_URL)
        except psycopg2.OperationalError as e:
            print(f"Error de conexión a la base de datos: {e}")
            g.db = None
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()

# --- DECORADOR DE AUTENTICACIÓN PARA ADMINISTRADORES ---
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if g.admin is None:
            flash('Acceso denegado. Debes iniciar sesión como administrador.', 'warning')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

# (Aquí van tus funciones de utilidad como get_nombre_mes, etc. Sin cambios)
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
# --- RUTAS DEL PORTAL DE ADMINISTRACIÓN ---
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if g.admin:
        return redirect(url_for('hub'))
    if request.method == 'POST':
        usuario = request.form.get('usuario')
        password = request.form.get('password')
        conn = get_db()
        if not conn:
            flash('Error de conexión con la base de datos.', 'danger')
            return render_template('admin_login.html', anio_actual=datetime.now().year)
        
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM administradores WHERE usuario = %s", (usuario,))
            admin = cur.fetchone()

            if admin and check_password_hash(admin['password_hash'], password):
                session.clear()
                session['admin_id'] = admin['id']
                session['admin_usuario'] = admin['usuario']
                
                # --- NUEVO: Actualizar estado al iniciar sesión ---
                cur.execute(
                    "UPDATE administradores SET ultimo_login = NOW(), estatus_online = TRUE WHERE id = %s",
                    (admin['id'],)
                )
                conn.commit()
                # ----------------------------------------------

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
    # --- NUEVO: Actualizar estado al cerrar sesión ---
    admin_id = session.get('admin_id')
    if admin_id:
        conn = get_db()
        if conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE administradores SET estatus_online = FALSE WHERE id = %s",
                    (admin_id,)
                )
                conn.commit()
    # ---------------------------------------------
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
    # --- NUEVO: Obtener lista de usuarios para mostrar en el hub ---
    conn = get_db()
    usuarios = []
    if conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT usuario, estatus_online, ultimo_login FROM administradores ORDER BY usuario")
            usuarios = cur.fetchall()
    # -------------------------------------------------------------
    return render_template('hub.html', anio_actual=datetime.now().year, usuarios=usuarios)

# (Aquí va el resto de tu código de la aplicación, desde /registrar hasta el final. No necesita cambios)
# PEGA EL RESTO DE TU CÓDIGO AQUÍ...
@app.route('/registrar')
@admin_required
def registrar():
    return render_template('registrar.html')
    
@app.route('/registrar_cliente', methods=['POST'])
@admin_required
def registrar_cliente():
    form_data = {k: v.strip() if isinstance(v, str) else v for k, v in request.form.items()}
    cedula = form_data.get('cedula')
    nombre_apellido = form_data.get('nombre_apellido')

    if not nombre_apellido or not cedula:
        flash("Error: Nombre y Cédula son campos obligatorios.", 'error')
        return redirect(url_for('registrar'))
    
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('registrar'))
    try:
        with conn.cursor() as cur:
            nombre_completo = nombre_apellido.split(' ', 1)
            nombre = nombre_completo[0]
            apellido = nombre_completo[1] if len(nombre_completo) > 1 else ''

            insert_dict = {
                'nombre': nombre, 'apellido': apellido, 'cedula': cedula.replace(' ', '')
            }
            optional_fields = [
                'contrato_nro', 'telefono', 'asesor', 'responsable', 'fecha_ingreso',
                'grupo', 'plan_contratado', 'cuotas_totales', 'moneda_pago', 'valor_cuota',
                'inscripcion_monto', 'proceso'
            ]
            for field in optional_fields:
                if form_data.get(field) and form_data.get(field) != '':
                    insert_dict[field] = form_data[field]

            columns = insert_dict.keys()
            values = [insert_dict[col] for col in columns]
            query = f"INSERT INTO clientes ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(values))})"
            cur.execute(query, values)
            conn.commit()
            flash(f"¡Cliente '{nombre_apellido}' registrado exitosamente!", 'success')

    except psycopg2.IntegrityError:
        conn.rollback()
        flash(f"Registro fallido: La cédula '{cedula}' ya existe.", 'error')
    except (psycopg2.Error, ValueError) as e:
        conn.rollback()
        flash(f"Registro fallido: Ocurrió un error de base de datos: {e}", 'error')
    return redirect(url_for('registrar'))


@app.route('/consulta', methods=['GET', 'POST'])
@admin_required
def consulta():
    clientes_encontrados = []
    mensaje_error = None
    termino_busqueda_raw = request.form.get('busqueda', request.args.get('busqueda', ''))
    termino_busqueda = termino_busqueda_raw.strip()

    if termino_busqueda:
        conn = get_db()
        if not conn:
            mensaje_error = "Error de conexión a la base de datos."
        else:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    query_clientes = "SELECT *, inscripcion_monto AS inscripcion FROM clientes WHERE cedula ILIKE %s OR nombre ILIKE %s OR apellido ILIKE %s ORDER BY nombre, apellido LIMIT 20;"
                    patron_busqueda = f'%{termino_busqueda}%'
                    cur.execute(query_clientes, (patron_busqueda, patron_busqueda, patron_busqueda))
                    clientes_raw = cur.fetchall()
                    
                    if not clientes_raw:
                        mensaje_error = "🚫 No se encontraron clientes que coincidan con su búsqueda."
                    else:
                        for cliente in clientes_raw:
                            cliente_dict = dict(cliente)
                            cliente_dict['nombre_apellido'] = f"{cliente.get('nombre', '')} {cliente.get('apellido', '')}".strip()
                            if 'cuotas_pagas' in cliente_dict:
                                cliente_dict['cuotas_pagadas_progresivas'] = cliente_dict['cuotas_pagas']

                            cur.execute("SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha_pago DESC, id DESC", (cliente_dict['id'],))
                            cliente_dict['pagos'] = cur.fetchall()
                            clientes_encontrados.append(cliente_dict)
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
    
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
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

        if pago_form.get('forma_pago') == 'Transferencia' and not pago_form.get('referencia'):
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
                cur.execute(pago_query, (
                    client_id, pago_form['monto'], tipo_pago, pago_form['forma_pago'], pago_form['fecha_pago'],
                    pago_form.get('pago_en'), pago_form['por_concepto_de'],
                    pago_form.get('referencia'), pago_form.get('banco'), pago_form.get('lugar_emision'),
                    pago_form.get('tasa_dia'), pago_form.get('monto_bs')
                ))
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
    conn = get_db()
    cedula_cliente_fallback = request.args.get('cedula', '')
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta', busqueda=cedula_cliente_fallback))

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM pagos WHERE id = %s", (pago_id,))
            pago = cur.fetchone()
            if not pago or pago['estado_pago'] != 'Pendiente':
                flash("El pago no se puede conciliar.", 'error')
                return redirect(url_for('consulta', busqueda=cedula_cliente_fallback))

            cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s FOR UPDATE", (pago['cliente_id'],))
            cliente = cur.fetchone()
            if not cliente:
                flash("Error: No se encontró el cliente asociado a este pago.", 'error')
                return redirect(url_for('consulta', busqueda=cedula_cliente_fallback))

            monto_pagado = Decimal(pago['monto'])
            
            puntualidad = 'Puntual'
            if pago['tipo_pago'] == 'Cuota':
                fecha_vencimiento = get_fecha_vencimiento_ajustada(pago['fecha_pago'])
                if pago['fecha_pago'] > fecha_vencimiento:
                    puntualidad = 'Impuntual'
                    cur.execute("UPDATE clientes SET meses_retraso_entrega = meses_retraso_entrega + 1 WHERE id = %s;", (cliente['id'],))
                cur.execute("UPDATE pagos SET puntualidad = %s WHERE id = %s", (puntualidad, pago_id))

            if pago['tipo_pago'] == 'Inscripción':
                inscripcion_pagada_actual = Decimal(cliente.get('inscripcion_pagada') or 0)
                inscripcion_total = Decimal(cliente.get('inscripcion_monto') or 0)
                nueva_inscripcion_pagada = inscripcion_pagada_actual + monto_pagado

                if inscripcion_total > 0 and nueva_inscripcion_pagada >= inscripcion_total:
                    if cliente['proceso'] == 'RESERVA':
                        cur.execute("UPDATE clientes SET proceso = 'INSCRITO' WHERE id = %s", (cliente['id'],))
                        flash("Cliente ha completado la inscripción y su proceso ha cambiado a 'INSCRITO'.", "info")

                    cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE cliente_id = %s AND tipo_pago = 'Inscripción'", (cliente['id'],))
                    pago_final_query = """
                        INSERT INTO pagos (cliente_id, monto, tipo_pago, forma_pago, fecha_pago, por_concepto_de, estado_pago, cuotas_cubiertas, lugar_emision)
                        VALUES (%s, %s, 'Inscripción Finalizada', %s, %s, %s, 'Conciliado', 0, %s) RETURNING id;
                    """
                    cur.execute(pago_final_query, (cliente['id'], inscripcion_total, pago['forma_pago'], pago['fecha_pago'], 'Pago total de inscripción', pago['lugar_emision']))
                    pago_final_id = cur.fetchone()[0]
                    cur.execute("UPDATE clientes SET inscripcion_pagada = %s WHERE id = %s", (inscripcion_total, cliente['id']))
                    conn.commit()
                    flash("¡Inscripción completada! Se generó el recibo final y se anularon los abonos.", 'success')
                    return redirect(url_for('ver_recibo_inscripcion', pago_id=pago_final_id))
                else:
                    cur.execute("UPDATE clientes SET inscripcion_pagada = %s WHERE id = %s", (nueva_inscripcion_pagada, cliente['id']))
                    cur.execute("UPDATE pagos SET estado_pago = 'Conciliado' WHERE id = %s", (pago_id,))
                    conn.commit()
                    flash(f"Abono de inscripción N° {pago_id} conciliado.", 'success')
                    return redirect(url_for('ver_recibo', pago_id=pago_id))

            elif pago['tipo_pago'] == 'Cuota':
                if cliente['proceso'] == 'INSCRITO':
                    cur.execute("UPDATE clientes SET proceso = 'Ahorrador' WHERE id = %s", (cliente['id'],))
                    flash("Primer pago de cuota registrado. El proceso del cliente ha cambiado a 'Ahorrador'.", "info")

                valor_cuota = Decimal(cliente.get('valor_cuota') or 0)
                if valor_cuota <= 0: raise ValueError('El cliente no tiene un valor de cuota válido.')
                
                cuotas_progresivas_actuales = cliente.get('cuotas_pagadas_progresivas') or 0
                cuotas_regresivas_actuales = cliente.get('cuotas_pagadas_regresivas') or 0
                balance_actual = Decimal(cliente.get('balance_regresivo') or 0)
                monto_total_disponible = monto_pagado + balance_actual
                cuotas_completas_adicionales = int(monto_total_disponible // valor_cuota)
                
                nuevas_cuotas_progresivas = cuotas_progresivas_actuales + cuotas_completas_adicionales
                nuevo_balance = monto_total_disponible % valor_cuota

                update_cliente_query = "UPDATE clientes SET cuotas_pagadas_progresivas = %s, balance_regresivo = %s, cuotas_pagadas_regresivas = %s WHERE id = %s;"
                cur.execute(update_cliente_query, (nuevas_cuotas_progresivas, nuevo_balance, cuotas_regresivas_actuales, cliente['id']))
                
                update_pago_query = """
                    UPDATE pagos 
                    SET estado_pago = 'Conciliado', cuotas_cubiertas = %s, 
                        cuotas_progresivas_al_pagar = %s, cuotas_regresivas_al_pagar = %s, balance_al_pagar = %s 
                    WHERE id = %s;
                """
                cur.execute(update_pago_query, (cuotas_completas_adicionales, nuevas_cuotas_progresivas, cuotas_regresivas_actuales, nuevo_balance, pago_id))
                
                conn.commit()
                flash(f"¡Pago de cuota N° {pago_id} conciliado como '{puntualidad}'!", 'success')
                return redirect(url_for('ver_recibo', pago_id=pago_id))

    except (psycopg2.Error, ValueError, TypeError) as e:
        conn.rollback()
        flash(f'Ocurrió un error al conciliar el pago: {e}', 'error')
        return redirect(url_for('consulta', busqueda=cedula_cliente_fallback))

@app.route('/recibo/<int:pago_id>')
def ver_recibo(pago_id):
    conn = get_db()
    if not conn:
        if 'cliente_id' in session:
            return redirect(url_for('portal_login'))
        return redirect(url_for('consulta'))

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        query = """
            SELECT p.*, 
                   (c.nombre || ' ' || c.apellido) as nombre_apellido, c.cedula, c.cuotas_totales, c.valor_cuota,
                   c.inscripcion_monto, c.inscripcion_pagada,
                   COALESCE(p.cuotas_progresivas_al_pagar, c.cuotas_pagadas_progresivas) AS cuotas_pagadas_progresivas, 
                   COALESCE(p.cuotas_regresivas_al_pagar, c.cuotas_pagadas_regresivas) AS cuotas_pagadas_regresivas,
                   COALESCE(p.balance_al_pagar, c.balance_regresivo) AS balance_regresivo
            FROM pagos p JOIN clientes c ON p.cliente_id = c.id 
            WHERE p.id = %s;
        """
        cur.execute(query, (pago_id,))
        pago = cur.fetchone()

    if not pago:
        flash('Recibo no encontrado.', 'error')
        if 'cliente_id' in session:
            return redirect(url_for('portal_dashboard'))
        return redirect(url_for('consulta'))
    
    is_admin_view = 'admin_id' in session
    
    return render_template('recibo.html', pago=pago, is_admin_view=is_admin_view)


@app.route('/recibo_inscripcion/<int:pago_id>')
def ver_recibo_inscripcion(pago_id):
    conn = get_db()
    if not conn:
        if 'cliente_id' in session:
            return redirect(url_for('portal_login'))
        return redirect(url_for('consulta'))

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        query = """
            SELECT p.*, (c.nombre || ' ' || c.apellido) as nombre_apellido, c.cedula, c.plan_contratado
            FROM pagos p JOIN clientes c ON p.cliente_id = c.id 
            WHERE p.id = %s AND p.tipo_pago = 'Inscripción Finalizada';
        """
        cur.execute(query, (pago_id,))
        pago = cur.fetchone()

    if not pago:
        flash('Recibo de inscripción final no encontrado.', 'error')
        if 'cliente_id' in session:
            return redirect(url_for('portal_dashboard'))
        return redirect(url_for('consulta'))
    
    is_admin_view = 'admin_id' in session
    
    return render_template('recibo_inscripcion.html', pago=pago, cliente=pago, is_admin_view=is_admin_view)

@app.route('/anular_recibo/<int:pago_id>', methods=['POST'])
@admin_required
def anular_recibo(pago_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta'))
    
    cedula_cliente = ''
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM pagos WHERE id = %s FOR UPDATE", (pago_id,))
            pago_a_anular = cur.fetchone()

            if not pago_a_anular or pago_a_anular['estado_pago'] == 'Anulado':
                flash("Este recibo ya está anulado o no se puede anular.", "warning")
                return redirect(url_for('consulta'))

            cliente_id = pago_a_anular['cliente_id']
            cur.execute("SELECT cedula FROM clientes WHERE id = %s", (cliente_id,))
            cliente_info = cur.fetchone()
            if cliente_info:
                cedula_cliente = cliente_info['cedula']

            if pago_a_anular['estado_pago'] == 'Pendiente':
                cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE id = %s", (pago_id,))
            
            elif pago_a_anular['tipo_pago'] == 'Inscripción':
                monto_pago = Decimal(pago_a_anular['monto'])
                cur.execute("UPDATE clientes SET inscripcion_pagada = inscripcion_pagada - %s WHERE id = %s", (monto_pago, cliente_id))
                cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE id = %s", (pago_id,))
            
            elif pago_a_anular['tipo_pago'] == 'Cuota':
                cur.execute("SELECT cuotas_pagadas_progresivas, cuotas_pagadas_regresivas, balance_regresivo, valor_cuota FROM clientes WHERE id = %s FOR UPDATE", (cliente_id,))
                cliente = cur.fetchone()
                
                cuotas_p = Decimal(cliente.get('cuotas_pagadas_progresivas') or 0)
                cuotas_r = Decimal(cliente.get('cuotas_pagadas_regresivas') or 0)
                balance = Decimal(cliente.get('balance_regresivo') or 0)
                valor_cuota = Decimal(cliente.get('valor_cuota') or 0)
                monto_a_revertir = Decimal(pago_a_anular['monto'])
                
                if valor_cuota <= 0:
                    raise ValueError("El cliente no tiene un valor de cuota válido para recalcular.")
                
                valor_total_actual = (cuotas_p * valor_cuota) + (cuotas_r * valor_cuota) + balance
                nuevo_valor_total = valor_total_actual - monto_a_revertir
                
                if nuevo_valor_total < 0:
                    flash("Advertencia: La anulación resultó en un saldo negativo, se ha ajustado a cero.", "warning")
                    nuevo_valor_total = Decimal('0.00')

                nuevas_cuotas_pagadas = int(nuevo_valor_total // valor_cuota)
                nuevo_balance = nuevo_valor_total % valor_cuota
                
                cur.execute("""
                    UPDATE clientes 
                    SET cuotas_pagadas_progresivas = %s, cuotas_pagadas_regresivas = 0, balance_regresivo = %s 
                    WHERE id = %s
                """, (nuevas_cuotas_pagadas, nuevo_balance, cliente_id))
                
                cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE id = %s", (pago_id,))
            
            elif pago_a_anular['tipo_pago'] == 'Inscripción Finalizada':
                cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE id = %s", (pago_id,))
                
                cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE cliente_id = %s AND tipo_pago = 'Inscripción'", (cliente_id,))
                
                cur.execute("UPDATE clientes SET inscripcion_pagada = 0, proceso = 'RESERVA' WHERE id = %s", (cliente_id,))
                
                flash("¡Reinicio de inscripción completado! El cliente ha vuelto a 'RESERVA' con saldo de inscripción en cero.", 'success')

            conn.commit()
            flash(f"¡Recibo N° {pago_id} anulado y saldo corregido exitosamente!", "success")
            return redirect(url_for('consulta', busqueda=cedula_cliente))

    except (psycopg2.Error, ValueError) as e:
        conn.rollback()
        flash(f"Ocurrió un error al anular el recibo: {e}", 'error')
        return redirect(url_for('consulta', busqueda=cedula_cliente))

@app.route('/verificar_recibo/<int:pago_id>')
def verificar_recibo(pago_id):
    conn = get_db()
    if not conn:
        return "Error de conexión a la base de datos.", 500
    
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        query = """
            SELECT p.id, p.monto, p.fecha_pago, p.estado_pago, p.tipo_pago,
                   (c.nombre || ' ' || c.apellido) as nombre_apellido
            FROM pagos p 
            JOIN clientes c ON p.cliente_id = c.id 
            WHERE p.id = %s;
        """
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
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        query = """
            SELECT p.*, (c.nombre || ' ' || c.apellido) as nombre_apellido, c.cedula
            FROM pagos p 
            JOIN clientes c ON p.cliente_id = c.id 
            WHERE p.id = %s AND p.estado_pago = 'Anulado';
        """
        cur.execute(query, (pago_id,))
        pago = cur.fetchone()
    if not pago:
        flash('Recibo anulado no encontrado.', 'error')
        return redirect(url_for('consulta'))
    return render_template('recibo_anulado.html', pago=pago)

@app.route('/edit/<int:client_id>', methods=['GET', 'POST'])
@admin_required
def edit_client(client_id):
    conn = get_db()
    if not conn: 
        flash('Error de conexión a la base de datos.', 'error')
        return redirect(url_for('consulta'))

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
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
                    inscripcion_monto = %(inscripcion_monto)s, proceso = %(proceso)s, estatus = %(estatus)s
                WHERE id = %(id)s;
                """
                cur.execute(update_query, update_data)
                conn.commit()
                flash('¡Cliente actualizado exitosamente!', 'success')
                
                cedula_actualizada = update_data.get('cedula')
                return redirect(url_for('consulta', busqueda=cedula_actualizada))
        except (psycopg2.Error, ValueError) as e: 
            conn.rollback()
            flash(f'Ocurrió un error al actualizar: {e}', 'error')
    
    return render_template('edit_cliente.html', cliente=cliente)

@app.route('/delete/<int:client_id>', methods=['POST'])
@admin_required
def delete_client(client_id):
    conn = get_db()
    if not conn: return redirect(url_for('consulta'))
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM clientes WHERE id = %s", (client_id,))
            conn.commit()
            flash('¡Cliente y sus registros asociados han sido eliminados exitosamente!', 'success')
    except psycopg2.Error as e:
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
    
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
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
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT cedula FROM clientes WHERE id = %s", (client_id,))
            cliente_info = cur.fetchone()
            if cliente_info:
                cedula_cliente = cliente_info['cedula']

            hoy = datetime.now().date()
            inicio_mes = hoy.replace(day=1)
            
            cur.execute("""
                SELECT 1 FROM pagos 
                WHERE cliente_id = %s 
                  AND tipo_pago = 'Cuota' 
                  AND puntualidad = 'Impuntual'
                  AND fecha_pago >= %s
            """, (client_id, inicio_mes))
            
            pago_impuntual_reciente = cur.fetchone()

            if pago_impuntual_reciente:
                flash("No se puede registrar la oferta: El cliente tiene un pago impuntual registrado en el mes actual.", 'error')
                return redirect(url_for('consulta', busqueda=cedula_cliente))

            if not cuotas_ofertadas or not cuotas_ofertadas.isdigit() or int(cuotas_ofertadas) <= 0:
                flash("Debe ingresar un número válido de cuotas para la oferta.", 'error')
                return redirect(url_for('registrar_oferta', client_id=client_id))

            cur.execute("""
                INSERT INTO ofertas (cliente_id, cuotas_ofertadas, fecha_oferta, estado_oferta)
                VALUES (%s, %s, %s, 'activa')
            """, (client_id, int(cuotas_ofertadas), hoy))
            
            conn.commit()
            flash(f"¡Oferta de {cuotas_ofertadas} cuotas registrada exitosamente!", 'success')

    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Ocurrió un error al registrar la oferta: {e}", 'error')

    return redirect(url_for('consulta', busqueda=cedula_cliente))

@app.route('/adjudicacion', methods=['GET'])
@admin_required
def adjudicacion():
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return render_template('adjudicacion.html', clientes_elegibles_ahorro=[], clientes_elegibles_sorteo=[], ofertas_activas=[], historial=[])

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT id, (nombre || ' ' || apellido) as nombre_apellido, cedula, cuotas_pagadas_progresivas, meses_retraso_entrega
                FROM clientes 
                WHERE proceso ILIKE 'Ahorrador' 
                  AND cuotas_pagadas_progresivas >= (12 + meses_retraso_entrega)
                  AND estatus ILIKE 'activo'
                ORDER BY nombre, apellido;
            """)
            clientes_elegibles_ahorro = cur.fetchall()
            
            clientes_elegibles_sorteo = [] 
            
            cur.execute("""
                SELECT o.cuotas_ofertadas, c.id, (c.nombre || ' ' || c.apellido) as nombre_apellido, c.cedula
                FROM ofertas o JOIN clientes c ON o.cliente_id = c.id
                WHERE o.estado_oferta = 'activa' AND c.proceso ILIKE 'Ahorrador'
                ORDER BY o.cuotas_ofertadas DESC, o.fecha_oferta ASC;
            """)
            ofertas_activas = cur.fetchall()
            
            cur.execute("""
                SELECT 
                    a.id, a.fecha_adjudicacion, 
                    (gs.nombre || ' ' || gs.apellido) as nombre_ganador_sorteo,
                    (go.nombre || ' ' || go.apellido) as nombre_ganador_oferta
                FROM adjudicaciones a
                LEFT JOIN clientes gs ON a.ganador_sorteo_id = gs.id
                LEFT JOIN clientes go ON a.ganador_oferta_id = go.id
                ORDER BY a.fecha_adjudicacion DESC;
            """)
            historial = cur.fetchall()
    except psycopg2.Error as e:
        flash(f"Error al cargar datos para la adjudicación: {e}", 'error')
        clientes_elegibles_ahorro, clientes_elegibles_sorteo, ofertas_activas, historial = [], [], [], []
    return render_template('adjudicacion.html', 
                           clientes_elegibles_ahorro=clientes_elegibles_ahorro,
                           clientes_elegibles_sorteo=clientes_elegibles_sorteo, 
                           ofertas_activas=ofertas_activas,
                           historial=historial)

@app.route('/realizar_adjudicacion', methods=['POST'])
@admin_required
def realizar_adjudicacion():
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('adjudicacion'))
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("UPDATE clientes SET ignorar_penalidad_puntualidad = FALSE;")
            ids_ya_ganadores = set()
            
            cur.execute("""
                SELECT id, (nombre || ' ' || apellido) as nombre_apellido FROM clientes 
                WHERE proceso ILIKE 'Ahorrador' 
                  AND cuotas_pagadas_progresivas >= (12 + meses_retraso_entrega)
                  AND estatus ILIKE 'activo';
            """)
            ganadores_ahorro = cur.fetchall()
            ids_ganadores_ahorro = [g['id'] for g in ganadores_ahorro]
            ids_ya_ganadores.update(ids_ganadores_ahorro)
            
            ganador_oferta = None
            cur.execute("""
                SELECT c.id, (c.nombre || ' ' || c.apellido) as nombre_apellido, c.ignorar_penalidad_puntualidad, o.cuotas_ofertadas
                FROM ofertas o JOIN clientes c ON o.cliente_id = c.id
                WHERE o.estado_oferta = 'activa' AND c.proceso ILIKE 'Ahorrador' AND c.id NOT IN %s;
            """, (tuple(ids_ya_ganadores) if ids_ya_ganadores else (0,),))
            candidatos_oferta_raw = cur.fetchall()
            
            candidatos_oferta = []
            for c in candidatos_oferta_raw:
                cur.execute("SELECT COUNT(*) FROM ofertas WHERE cliente_id = %s;", (c['id'],))
                frecuencia = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM pagos WHERE cliente_id = %s AND puntualidad = 'Impuntual';", (c['id'],))
                impuntualidades = cur.fetchone()[0]
                candidatos_oferta.append({**c, 'frecuencia': frecuencia, 'impuntualidades': impuntualidades})
            
            if candidatos_oferta:
                candidatos_oferta.sort(key=lambda x: (
                    -x['cuotas_ofertadas'], 
                    -x['frecuencia'], 
                    x['impuntualidades'] if not x['ignorar_penalidad_puntualidad'] else 0
                ))
                ganador_oferta = candidatos_oferta[0]
                ids_ya_ganadores.add(ganador_oferta['id'])
                for perdedor in candidatos_oferta[1:]:
                    if (perdedor['cuotas_ofertadas'] == ganador_oferta['cuotas_ofertadas'] and
                        perdedor['frecuencia'] == ganador_oferta['frecuencia'] and
                        perdedor['impuntualidades'] > ganador_oferta['impuntualidades']):
                        cur.execute("UPDATE clientes SET ignorar_penalidad_puntualidad = TRUE WHERE id = %s;", (perdedor['id'],))
            
            if not ids_ya_ganadores:
                flash("No hay clientes que cumplan los criterios para ser adjudicados este ciclo.", "warning")
                return redirect(url_for('adjudicacion'))

            cur.execute("UPDATE clientes SET proceso = 'ADJUDICADO' WHERE id = ANY(%s);", (list(ids_ya_ganadores),))
            if ganador_oferta:
                cur.execute("UPDATE ofertas SET estado_oferta = 'ganadora' WHERE cliente_id = %s AND estado_oferta = 'activa';", (ganador_oferta['id'],))
            cur.execute("UPDATE ofertas SET estado_oferta = 'perdida' WHERE estado_oferta = 'activa';")
            
            ganador_sorteo_id = None 
            ganador_oferta_id = ganador_oferta['id'] if ganador_oferta else None
            
            cur.execute("""
                INSERT INTO adjudicaciones (ganador_oferta_id, ganador_sorteo_id)
                VALUES (%s, %s);
            """, (ganador_oferta_id, ganador_sorteo_id))
            
            conn.commit()
            for g in ganadores_ahorro: flash(f"🏆 ¡Ganador por Ahorro: {g['nombre_apellido']}!", 'success')
            if ganador_oferta: flash(f"🏆 ¡Ganador por Oferta: {ganador_oferta['nombre_apellido']}!", 'success')
    except (psycopg2.Error, IndexError, KeyError) as e:
        conn.rollback()
        flash(f"Ocurrió un error durante el proceso de adjudicación: {e}", 'error')
    return redirect(url_for('adjudicacion'))

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
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                sql_query = """
                    SELECT id, (nombre || ' ' || apellido) as nombre_apellido 
                    FROM clientes 
                    WHERE TRIM(cedula) = %s 
                    AND SPLIT_PART(REPLACE(TRIM(contrato_nro), 'MP-', ''), '.', 1) = %s;
                """
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
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
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

            hoy = datetime.now().date()
            dia_de_vencimiento = 3
            
            pago_del_mes_realizado = False
            for pago in pagos:
                if pago['tipo_pago'] == 'Cuota' and pago['fecha_pago'].year == hoy.year and pago['fecha_pago'].month == hoy.month:
                    pago_del_mes_realizado = True
                    break

            estado_cuota = {}
            if pago_del_mes_realizado:
                estado_cuota['estado'] = 'Pagada'
                estado_cuota['mes'] = get_nombre_mes(hoy.month)
                estado_cuota['mensaje'] = 'Tu cuota de este mes ya fue procesada.'
            elif hoy.day <= dia_de_vencimiento:
                estado_cuota['estado'] = 'Vigente'
                estado_cuota['mes'] = get_nombre_mes(hoy.month)
                estado_cuota['fecha_vencimiento'] = f"{dia_de_vencimiento:02d}/{hoy.month:02d}/{hoy.year}"
            else:
                estado_cuota['estado'] = 'En Mora'
                estado_cuota['mes'] = get_nombre_mes(hoy.month)
                estado_cuota['fecha_vencimiento'] = f"{dia_de_vencimiento:02d}/{hoy.month:02d}/{hoy.year}"

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

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s;", (session['cliente_id'],))
        cliente = cur.fetchone()

    if not cliente:
        session.clear()
        flash('No se encontró su información de cliente.', 'error')
        return redirect(url_for('portal_login'))

    mes_actual = get_nombre_mes(datetime.now().date().month)

    if request.method == 'POST':
        pago_form = {k: v if v else None for k, v in request.form.items()}
        
        if not pago_form.get('monto') or not pago_form.get('fecha_pago') or not pago_form.get('forma_pago'):
            flash('Error: Monto, fecha y forma de pago son campos obligatorios.', 'error')
            return render_template('portal_reportar_pago.html', cliente=cliente, mes_actual=mes_actual)

        if pago_form.get('forma_pago') != 'Efectivo' and not pago_form.get('referencia'):
            flash('Error: La referencia es obligatoria para pagos por transferencia, pago móvil o Zelle.', 'error')
            return render_template('portal_reportar_pago.html', cliente=cliente, mes_actual=mes_actual)
            
        try:
            with conn.cursor() as cur:
                pago_query = """
                    INSERT INTO pagos (cliente_id, monto, tipo_pago, forma_pago, fecha_pago, 
                                        pago_en, por_concepto_de, referencia, banco, lugar_emision,
                                        tasa_dia, monto_bs, estado_pago, cuotas_cubiertas)
                    VALUES (%s, %s, 'Cuota', %s, %s, %s, %s, %s, %s, 'Acarigua', %s, %s, 'Pendiente', 0);
                """
                cur.execute(pago_query, (
                    session['cliente_id'], pago_form['monto'], pago_form['forma_pago'], pago_form['fecha_pago'],
                    pago_form.get('pago_en'), pago_form['por_concepto_de'],
                    pago_form.get('referencia'), pago_form.get('banco'),
                    pago_form.get('tasa_dia'), pago_form.get('monto_bs')
                ))
                conn.commit()
                flash('¡Pago reportado exitosamente! Será verificado por un administrador a la brevedad.', 'success')
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
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT *, (nombre || ' ' || apellido) as nombre_apellido FROM clientes WHERE id = %s;", (session['cliente_id'],))
            cliente = cur.fetchone()

            if not cliente:
                session.clear()
                flash('No se encontró su información de cliente.', 'error')
                return redirect(url_for('portal_login'))

            cur.execute("SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha_pago ASC, id ASC;", (session['cliente_id'],))
            pagos = cur.fetchall()

            fecha_generacion = datetime.now().strftime('%d/%m/%Y')

            return render_template('estado_cuenta.html',
                                   cliente=cliente,
                                   pagos=pagos,
                                   fecha_generacion=fecha_generacion)

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