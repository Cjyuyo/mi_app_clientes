import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, g, flash, redirect, url_for
from dotenv import load_dotenv
from decimal import Decimal

# Cargar variables de entorno desde un archivo .env
load_dotenv()

app = Flask(__name__)
# Es muy importante que esta clave esté definida en tus variables de entorno en Render.
app.secret_key = os.getenv('SECRET_KEY', 'una-clave-secreta-por-defecto-para-desarrollo')

# --- Gestión de la Conexión a la Base de Datos (Método Robusto) ---
def get_db():
    """
    Abre una nueva conexión a la base de datos si no existe una para la solicitud actual.
    """
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
    """Cierra la conexión a la BD al finalizar la petición para liberar recursos."""
    db = g.pop('db', None)
    if db is not None:
        db.close()

# --- Rutas de la Aplicación ---

@app.route('/')
def index():
    """Página principal para registrar un nuevo cliente."""
    return render_template('index.html')

@app.route('/registrar_cliente', methods=['POST'])
def registrar_cliente():
    """Procesa el formulario de registro de un nuevo cliente."""
    form_data = {k: v if v else None for k, v in request.form.items()}

    if not form_data.get('nombre_apellido') or not form_data.get('cedula'):
        flash("Error: Nombre y Cédula son campos obligatorios.", 'error')
        return redirect(url_for('index'))

    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos. No se pudo registrar al cliente.", 'error')
        return redirect(url_for('index'))

    try:
        with conn.cursor() as cur:
            query = """
            INSERT INTO clientes (
                nombre_apellido, cedula, contrato_nro, telefono, asesor, responsable, fecha_ingreso,
                grupo, bien_solicitado, plan_contratado, cuotas_totales, moneda_pago, valor_cuota,
                inscripcion_monto, proceso
            ) VALUES (
                %(nombre_apellido)s, %(cedula)s, %(contrato_nro)s, %(telefono)s, %(asesor)s, %(responsable)s, %(fecha_ingreso)s,
                %(grupo)s, %(bien_solicitado)s, %(plan_contratado)s, %(cuotas_totales)s, %(moneda_pago)s, %(valor_cuota)s,
                %(inscripcion_monto)s, %(proceso)s
            )
            """
            cur.execute(query, form_data)
            conn.commit()
            flash(f"¡Cliente '{form_data.get('nombre_apellido')}' registrado exitosamente!", 'success')
    except psycopg2.IntegrityError:
        conn.rollback()
        flash(f"Registro fallido: La cédula '{form_data.get('cedula')}' ya existe.", 'error')
    except (psycopg2.Error, ValueError) as e:
        conn.rollback()
        flash(f"Registro fallido: Ocurrió un error de base de datos: {e}", 'error')
    
    return redirect(url_for('index'))


@app.route('/consulta', methods=['GET', 'POST'])
def consulta():
    """Página para buscar clientes y ver sus detalles completos."""
    clientes_encontrados = []
    mensaje_error = None
    termino_busqueda = request.form.get('busqueda', '').strip()

    if request.method == 'POST':
        if not termino_busqueda:
            mensaje_error = "Por favor, ingrese un término para buscar."
        else:
            conn = get_db()
            if not conn:
                mensaje_error = "Error de conexión a la base de datos."
            else:
                try:
                    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                        query_clientes = "SELECT * FROM clientes WHERE cedula LIKE %s OR nombre_apellido ILIKE %s ORDER BY nombre_apellido LIMIT 20;"
                        patron = f'%{termino_busqueda}%'
                        cur.execute(query_clientes, (patron, patron))
                        clientes_raw = cur.fetchall()

                        if not clientes_raw:
                            mensaje_error = "🚫 No se encontraron clientes que coincidan con su búsqueda."
                        else:
                            for cliente in clientes_raw:
                                cliente_dict = dict(cliente)
                                query_pagos = "SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha_pago DESC"
                                cur.execute(query_pagos, (cliente_dict['id'],))
                                cliente_dict['pagos'] = cur.fetchall()
                                clientes_encontrados.append(cliente_dict)
                except psycopg2.Error as e:
                    mensaje_error = f"Error al consultar la base de datos: {e}"

    return render_template('consulta.html', clientes=clientes_encontrados, mensaje_error=mensaje_error, busqueda=termino_busqueda)


@app.route('/registrar_pago/<int:client_id>', methods=['GET', 'POST'])
def registrar_pago(client_id):
    """Muestra el formulario para registrar un pago y procesa el envío."""
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta'))

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM clientes WHERE id = %s", (client_id,))
        cliente = cur.fetchone()

    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta'))

    if request.method == 'POST':
        try:
            monto_pagado = Decimal(request.form['monto'])
            valor_cuota = Decimal(cliente['valor_cuota'] or 0)

            if valor_cuota <= 0:
                flash('Error: El cliente no tiene un valor de cuota válido. Edite el cliente para añadirlo.', 'error')
                return render_template('registrar_pago.html', cliente=cliente)

            cuotas_progresivas_actuales = cliente['cuotas_pagadas_progresivas'] or 0
            balance_regresivo_actual = Decimal(cliente['balance_regresivo'] or 0)

            monto_total_disponible = monto_pagado + balance_regresivo_actual
            cuotas_cubiertas_con_pago = int(monto_total_disponible // valor_cuota)
            nuevo_balance_regresivo = monto_total_disponible % valor_cuota
            nuevas_cuotas_progresivas = cuotas_progresivas_actuales + cuotas_cubiertas_con_pago

            with conn.cursor() as cur:
                update_query = "UPDATE clientes SET cuotas_pagadas_progresivas = %s, balance_regresivo = %s WHERE id = %s;"
                cur.execute(update_query, (nuevas_cuotas_progresivas, nuevo_balance_regresivo, client_id))

                pago_form = {k: v if v else None for k, v in request.form.items()}
                pago_query = """
                    INSERT INTO pagos (cliente_id, monto, cuotas_cubiertas, forma_pago, fecha_pago, pago_en, cantidad_en_letras, por_concepto_de, referencia, banco, lugar_emision)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
                """
                cur.execute(pago_query, (
                    client_id, pago_form['monto'], cuotas_cubiertas_con_pago, pago_form['forma_pago'], pago_form['fecha_pago'],
                    pago_form['pago_en'], pago_form['cantidad_en_letras'], pago_form['por_concepto_de'],
                    pago_form['referencia'], pago_form['banco'], pago_form['lugar_emision']
                ))
                nuevo_pago_id = cur.fetchone()[0]
                conn.commit()
                flash("¡Pago registrado y estado del cliente actualizado!", 'success')
                return redirect(url_for('ver_recibo', pago_id=nuevo_pago_id))

        except (psycopg2.Error, ValueError, TypeError) as e:
            conn.rollback()
            flash(f'Ocurrió un error al registrar el pago: {e}', 'error')
            return render_template('registrar_pago.html', cliente=cliente)

    return render_template('registrar_pago.html', cliente=cliente)


@app.route('/recibo/<int:pago_id>')
def ver_recibo(pago_id):
    conn = get_db()
    if not conn: return redirect(url_for('consulta'))
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        query = "SELECT p.*, c.nombre_apellido, c.cedula FROM pagos p JOIN clientes c ON p.cliente_id = c.id WHERE p.id = %s;"
        cur.execute(query, (pago_id,))
        pago = cur.fetchone()
    if not pago:
        flash('Recibo no encontrado.', 'error')
        return redirect(url_for('consulta'))
    return render_template('recibo.html', pago=pago)

@app.route('/edit/<int:client_id>', methods=['GET', 'POST'])
def edit_client(client_id):
    conn = get_db()
    if not conn: return redirect(url_for('consulta'))
    if request.method == 'POST':
        form_data = {k: v if v else None for k, v in request.form.items()}
        form_data['id'] = client_id
        try:
            with conn.cursor() as cur:
                update_query = """
                UPDATE clientes SET
                    nombre_apellido = %(nombre_apellido)s, cedula = %(cedula)s, contrato_nro = %(contrato_nro)s,
                    telefono = %(telefono)s, asesor = %(asesor)s, responsable = %(responsable)s, fecha_ingreso = %(fecha_ingreso)s,
                    grupo = %(grupo)s, bien_solicitado = %(bien_solicitado)s, plan_contratado = %(plan_contratado)s,
                    cuotas_totales = %(cuotas_totales)s, moneda_pago = %(moneda_pago)s, valor_cuota = %(valor_cuota)s,
                    inscripcion_monto = %(inscripcion_monto)s, proceso = %(proceso)s, estatus = %(estatus)s, estatus_1 = %(estatus_1)s
                WHERE id = %(id)s;
                """
                cur.execute(update_query, form_data)
                conn.commit()
                flash('¡Cliente actualizado exitosamente!', 'success')
                return redirect(url_for('consulta'))
        except (psycopg2.Error, ValueError) as e:
            conn.rollback()
            flash(f'Ocurrió un error al actualizar: {e}', 'error')
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM clientes WHERE id = %s", (client_id,))
        cliente = cur.fetchone()
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta'))
    return render_template('edit_cliente.html', cliente=cliente)

@app.route('/delete/<int:client_id>', methods=['POST'])
def delete_client(client_id):
    conn = get_db()
    if not conn: return redirect(url_for('consulta'))
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM clientes WHERE id = %s", (client_id,))
            conn.commit()
            flash('¡Cliente eliminado exitosamente!', 'success')
    except psycopg2.Error as e:
        conn.rollback()
        flash(f'Ocurrió un error al eliminar: {e}', 'error')
    return redirect(url_for('consulta'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
