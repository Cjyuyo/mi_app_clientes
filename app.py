import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, g, flash, redirect, url_for

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'un-secreto-muy-seguro')

DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        if not DATABASE_URL:
            raise ValueError("No se ha configurado la variable de entorno DATABASE_URL")
        db = g._database = psycopg2.connect(DATABASE_URL)
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        try:
            form_data = {k: (v if v != '' else None) for k, v in request.form.items()}
            if not form_data.get('nombre_apellido') or not form_data.get('cedula'):
                flash("Error: El nombre y la cédula son campos obligatorios.", 'error')
                return render_template('index.html')

            db = get_db()
            cursor = db.cursor()
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
            cursor.execute(query, form_data)
            db.commit()
            cursor.close()
            flash(f"¡Cliente '{form_data.get('nombre_apellido')}' registrado exitosamente!", 'success')
            return redirect(url_for('index'))
        except psycopg2.IntegrityError:
            db.rollback()
            flash(f"Registro fallido: La cédula '{form_data.get('cedula')}' ya existe.", 'error')
        except Exception as e:
            db.rollback()
            flash(f"Registro fallido: Ocurrió un error inesperado: {e}", 'error')
    return render_template('index.html')

@app.route('/consulta', methods=['GET', 'POST'])
def consulta():
    clientes_encontrados = []
    mensaje_error = None
    if request.method == 'POST':
        termino_busqueda = request.form.get('busqueda', '').strip()
        if not termino_busqueda:
            mensaje_error = "Por favor, ingrese un término para buscar."
        else:
            db = get_db()
            cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
            query_clientes = "SELECT * FROM clientes WHERE cedula LIKE %s OR nombre_apellido ILIKE %s OR telefono LIKE %s LIMIT 20;"
            patron = f'%{termino_busqueda}%'
            cursor.execute(query_clientes, (patron, patron, patron))
            clientes_encontrados_raw = cursor.fetchall()
            for cliente in clientes_encontrados_raw:
                cliente_dict = dict(cliente)
                query_pagos = "SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha_pago DESC"
                cursor.execute(query_pagos, (cliente_dict['id'],))
                cliente_dict['pagos'] = cursor.fetchall()
                clientes_encontrados.append(cliente_dict)
            if not clientes_encontrados:
                mensaje_error = "🚫 No se encontraron clientes que coincidan con su búsqueda."
            cursor.close()
    return render_template('consulta.html', clientes=clientes_encontrados, mensaje_error=mensaje_error)

@app.route('/edit/<int:client_id>', methods=['GET', 'POST'])
def edit_client(client_id):
    db = get_db()
    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    if request.method == 'POST':
        try:
            form_data = {k: (v if v != '' else None) for k, v in request.form.items()}
            form_data['id'] = client_id
            update_query = """
            UPDATE clientes SET
                nombre_apellido = %(nombre_apellido)s, cedula = %(cedula)s, contrato_nro = %(contrato_nro)s,
                telefono = %(telefono)s, asesor = %(asesor)s, responsable = %(responsable)s,
                fecha_ingreso = %(fecha_ingreso)s, grupo = %(grupo)s, bien_solicitado = %(bien_solicitado)s,
                plan_contratado = %(plan_contratado)s, cuotas_totales = %(cuotas_totales)s, moneda_pago = %(moneda_pago)s,
                valor_cuota = %(valor_cuota)s, inscripcion_monto = %(inscripcion_monto)s,
                proceso = %(proceso)s, estatus = %(estatus)s, estatus_1 = %(estatus_1)s
            WHERE id = %(id)s;
            """
            cursor.execute(update_query, form_data)
            db.commit()
            flash('¡Cliente actualizado exitosamente!', 'success')
            return redirect(url_for('consulta'))
        except Exception as e:
            db.rollback()
            flash(f'Ocurrió un error al actualizar el cliente: {e}', 'error')
    cursor.execute("SELECT * FROM clientes WHERE id = %s", (client_id,))
    cliente = cursor.fetchone()
    cursor.close()
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta'))
    return render_template('edit_cliente.html', cliente=cliente)

@app.route('/registrar_pago/<int:client_id>', methods=['GET', 'POST'])
def registrar_pago(client_id):
    db = get_db()
    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cursor.execute("SELECT * FROM clientes WHERE id = %s", (client_id,))
    cliente = cursor.fetchone()
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta'))
    if request.method == 'POST':
        try:
            monto_pagado = float(request.form['monto'])
            form_data = {k: v for k, v in request.form.items()}
            form_data['cliente_id'] = client_id
            valor_cuota = float(cliente['valor_cuota'] or 0)
            if valor_cuota <= 0:
                flash('Error: El cliente no tiene un valor de cuota válido.', 'error')
                return redirect(url_for('registrar_pago', client_id=client_id))
            cuotas_progresivas = int(cliente['cuotas_pagadas_progresivas'] or 0)
            balance_regresivo_actual = float(cliente['balance_regresivo'] or 0)
            cuotas_regresivas = int(cliente['cuotas_pagadas_regresivas'] or 0)
            cuotas_enteras_pagadas = int(monto_pagado // valor_cuota)
            excedente = monto_pagado % valor_cuota
            nuevas_cuotas_progresivas = cuotas_progresivas + cuotas_enteras_pagadas
            nuevo_balance_regresivo = balance_regresivo_actual + excedente
            while nuevo_balance_regresivo >= valor_cuota:
                nuevo_balance_regresivo -= valor_cuota
                cuotas_regresivas += 1
            update_query = "UPDATE clientes SET cuotas_pagadas_progresivas = %s, balance_regresivo = %s, cuotas_pagadas_regresivas = %s WHERE id = %s;"
            cursor.execute(update_query, (nuevas_cuotas_progresivas, nuevo_balance_regresivo, cuotas_regresivas, client_id))
            pago_query = "INSERT INTO pagos (cliente_id, monto, cuotas, forma_pago, pago_en, cantidad_en_letras, por_concepto_de, referencia, banco, lugar_emision) VALUES (%(cliente_id)s, %(monto)s, %(cuotas)s, %(forma_pago)s, %(pago_en)s, %(cantidad_en_letras)s, %(por_concepto_de)s, %(referencia)s, %(banco)s, %(lugar_emision)s) RETURNING id;"
            cursor.execute(pago_query, form_data)
            nuevo_pago_id = cursor.fetchone()['id']
            db.commit()
            cursor.close()
            return redirect(url_for('ver_recibo', pago_id=nuevo_pago_id))
        except Exception as e:
            db.rollback()
            flash(f'Ocurrió un error al registrar el pago: {e}', 'error')
            return redirect(url_for('registrar_pago', client_id=client_id))
    cursor.close()
    return render_template('registrar_pago.html', cliente=cliente)

@app.route('/recibo/<int:pago_id>')
def ver_recibo(pago_id):
    db = get_db()
    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    query = "SELECT p.*, c.nombre_apellido, c.cedula FROM pagos p JOIN clientes c ON p.cliente_id = c.id WHERE p.id = %s;"
    cursor.execute(query, (pago_id,))
    pago = cursor.fetchone()
    cursor.close()
    if not pago:
        flash('Recibo no encontrado.', 'error')
        return redirect(url_for('consulta'))
    return render_template('recibo.html', pago=pago)

@app.route('/delete/<int:client_id>', methods=['POST'])
def delete_client(client_id):
    try:
        db = get_db()
        cursor = db.cursor()
        cursor.execute("DELETE FROM clientes WHERE id = %s", (client_id,))
        db.commit()
        cursor.close()
        flash('¡Cliente eliminado exitosamente!', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Ocurrió un error al eliminar el cliente: {e}', 'error')
    return redirect(url_for('consulta'))

if __name__ == '__main__':
    app.run(debug=True)
