import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, g, flash, redirect, url_for
from dotenv import load_dotenv
from decimal import Decimal

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'una-clave-secreta-por-defecto-para-desarrollo')

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

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/registrar_cliente', methods=['POST'])
def registrar_cliente():
    form_data = {k: v if v else None for k, v in request.form.items()}
    if not form_data.get('nombre_apellido') or not form_data.get('cedula'):
        flash("Error: Nombre y Cédula son campos obligatorios.", 'error')
        return redirect(url_for('index'))
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
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
    clientes_encontrados = []
    mensaje_error = None
    if request.method == 'POST':
        termino_busqueda = request.form.get('busqueda', '').strip()
    else:
        termino_busqueda = request.args.get('busqueda', '').strip()

    if termino_busqueda:
        conn = get_db()
        if not conn:
            mensaje_error = "Error de conexión a la base de datos."
        else:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    if termino_busqueda.isdigit():
                        query_clientes = "SELECT * FROM clientes WHERE cedula = %s ORDER BY nombre_apellido LIMIT 20;"
                        cur.execute(query_clientes, (termino_busqueda,))
                    else:
                        query_clientes = "SELECT * FROM clientes WHERE nombre_apellido ILIKE %s ORDER BY nombre_apellido LIMIT 20;"
                        patron = f'%{termino_busqueda}%'
                        cur.execute(query_clientes, (patron,))
                    
                    clientes_raw = cur.fetchall()
                    if not clientes_raw:
                        mensaje_error = "🚫 No se encontraron clientes que coincidan con su búsqueda."
                    else:
                        for cliente in clientes_raw:
                            cliente_dict = dict(cliente)
                            # Ordena los pagos para que los más recientes aparezcan primero
                            query_pagos = "SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha_pago DESC, id DESC"
                            cur.execute(query_pagos, (cliente_dict['id'],))
                            cliente_dict['pagos'] = cur.fetchall()
                            clientes_encontrados.append(cliente_dict)
            except psycopg2.Error as e:
                mensaje_error = f"Error al consultar la base de datos: {e}"
    return render_template('consulta.html', clientes=clientes_encontrados, mensaje_error=mensaje_error, busqueda=termino_busqueda)

@app.route('/registrar_pago/<int:client_id>', methods=['GET', 'POST'])
def registrar_pago(client_id):
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
            monto_pagado_usd = Decimal(request.form['monto'])
            valor_cuota = Decimal(cliente['valor_cuota'] or 0)

            if valor_cuota <= 0:
                flash('Error: El cliente no tiene un valor de cuota válido.', 'error')
                return render_template('registrar_pago.html', cliente=cliente)

            cuotas_progresivas_actuales = cliente['cuotas_pagadas_progresivas'] or 0
            balance_regresivo_actual = Decimal(cliente['balance_regresivo'] or 0)
            cuotas_regresivas_actuales = cliente['cuotas_pagadas_regresivas'] or 0
            
            monto_necesario_progresiva = valor_cuota - balance_regresivo_actual
            nuevas_cuotas_progresivas = cuotas_progresivas_actuales
            nuevo_balance_regresivo = balance_regresivo_actual
            cuotas_cubiertas_este_pago = 0

            if monto_pagado_usd >= monto_necesario_progresiva:
                nuevas_cuotas_progresivas += 1
                excedente = monto_pagado_usd - monto_necesario_progresiva
                nuevo_balance_regresivo = excedente
                cuotas_cubiertas_este_pago = 1
            else:
                nuevo_balance_regresivo += monto_pagado_usd
                cuotas_cubiertas_este_pago = 0

            nuevas_cuotas_regresivas = cuotas_regresivas_actuales
            while nuevo_balance_regresivo >= valor_cuota:
                nuevo_balance_regresivo -= valor_cuota
                nuevas_cuotas_regresivas += 1

            with conn.cursor() as cur:
                update_query = "UPDATE clientes SET cuotas_pagadas_progresivas = %s, balance_regresivo = %s, cuotas_pagadas_regresivas = %s WHERE id = %s;"
                cur.execute(update_query, (nuevas_cuotas_progresivas, nuevo_balance_regresivo, nuevas_cuotas_regresivas, client_id))

                pago_form = {k: v if v else None for k, v in request.form.items()}
                
                pago_query = """
                    INSERT INTO pagos (cliente_id, monto, cuotas_cubiertas, forma_pago, fecha_pago, 
                                        pago_en, por_concepto_de, referencia, banco, lugar_emision,
                                        tasa_dia, monto_bs, estado_pago,
                                        cuotas_progresivas_al_pagar, cuotas_regresivas_al_pagar, balance_al_pagar)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
                """
                cur.execute(pago_query, (
                    client_id, pago_form['monto'], cuotas_cubiertas_este_pago, pago_form['forma_pago'], pago_form['fecha_pago'],
                    pago_form.get('pago_en'), pago_form['por_concepto_de'],
                    pago_form['referencia'], pago_form['banco'], pago_form['lugar_emision'],
                    pago_form.get('tasa_dia'), pago_form.get('monto_bs'), pago_form.get('estado_pago'),
                    nuevas_cuotas_progresivas, nuevas_cuotas_regresivas, nuevo_balance_regresivo
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
        query = """
            SELECT p.*, 
                   c.nombre_apellido, c.cedula, c.cuotas_totales, c.valor_cuota,
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
        return redirect(url_for('consulta'))
    return render_template('recibo.html', pago=pago)

# --- NUEVA RUTA PARA ANULAR RECIBOS ---
@app.route('/anular_recibo/<int:pago_id>', methods=['POST'])
def anular_recibo(pago_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta'))
    
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # 1. Obtener el pago que se va a anular
            cur.execute("SELECT * FROM pagos WHERE id = %s", (pago_id,))
            pago_a_anular = cur.fetchone()

            if not pago_a_anular:
                flash("El recibo que intenta anular no existe.", "error")
                return redirect(url_for('consulta'))

            if pago_a_anular['estado_pago'] == 'Anulado':
                flash("Este recibo ya ha sido anulado.", "warning")
                return redirect(url_for('ver_recibo', pago_id=pago_id))

            cliente_id = pago_a_anular['cliente_id']

            # 2. Encontrar el estado del cliente ANTES de este pago
            # Se busca el pago válido anterior a este.
            query_pago_anterior = """
                SELECT cuotas_progresivas_al_pagar, cuotas_regresivas_al_pagar, balance_al_pagar 
                FROM pagos 
                WHERE cliente_id = %s AND id != %s AND estado_pago != 'Anulado' AND fecha_pago <= %s
                ORDER BY fecha_pago DESC, id DESC LIMIT 1
            """
            cur.execute(query_pago_anterior, (cliente_id, pago_id, pago_a_anular['fecha_pago']))
            pago_anterior = cur.fetchone()

            # 3. Determinar el estado al que se debe revertir la cuenta del cliente
            if pago_anterior:
                revertir_a_progresivas = pago_anterior['cuotas_progresivas_al_pagar']
                revertir_a_regresivas = pago_anterior['cuotas_regresivas_al_pagar']
                revertir_a_balance = pago_anterior['balance_al_pagar']
            else:
                # Si no hay pago anterior, es el primer pago, se revierte a cero.
                revertir_a_progresivas = 0
                revertir_a_regresivas = 0
                revertir_a_balance = Decimal('0.00')

            # 4. Actualizar el estado del cliente a los valores anteriores
            update_cliente_query = """
                UPDATE clientes 
                SET cuotas_pagadas_progresivas = %s, cuotas_pagadas_regresivas = %s, balance_regresivo = %s
                WHERE id = %s
            """
            cur.execute(update_cliente_query, (revertir_a_progresivas, revertir_a_regresivas, revertir_a_balance, cliente_id))

            # 5. Marcar el pago como Anulado
            update_pago_query = "UPDATE pagos SET estado_pago = 'Anulado' WHERE id = %s"
            cur.execute(update_pago_query, (pago_id,))

            conn.commit()
            flash(f"¡Recibo N° {pago_a_anular['id']} anulado exitosamente! El estado del cliente ha sido revertido.", "success")
            
            # Obtener la cédula para redirigir a la consulta
            cur.execute("SELECT cedula FROM clientes WHERE id = %s", (cliente_id,))
            cedula_cliente = cur.fetchone()['cedula']
            return redirect(url_for('consulta', busqueda=cedula_cliente))

    except psycopg2.Error as e:
        conn.rollback()
        flash(f"Ocurrió un error al anular el recibo: {e}", 'error')
        return redirect(url_for('consulta'))


@app.route('/edit/<int:client_id>', methods=['GET', 'POST'])
def edit_client(client_id):
    conn = get_db()
    if not conn: return redirect(url_for('consulta'))
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM clientes WHERE id = %s", (client_id,))
        cliente = cur.fetchone()
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta'))

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
                return redirect(url_for('consulta', busqueda=cliente['cedula']))
        except (psycopg2.Error, ValueError) as e:
            conn.rollback()
            flash(f'Ocurrió un error al actualizar: {e}', 'error')
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
