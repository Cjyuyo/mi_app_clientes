import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, g, flash, redirect, url_for

app = Flask(__name__)
# Se necesita una clave secreta para los mensajes de confirmación (flash messages)
app.secret_key = os.environ.get('SECRET_KEY', 'un-secreto-muy-seguro')

DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db():
    """
    Abre una conexión a la base de datos PostgreSQL.
    """
    db = getattr(g, '_database', None)
    if db is None:
        if not DATABASE_URL:
            raise ValueError("No se ha configurado la variable de entorno DATABASE_URL")
        db = g._database = psycopg2.connect(DATABASE_URL)
    return db

@app.teardown_appcontext
def close_connection(exception):
    """
    Cierra la conexión al final de la petición.
    """
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

@app.route('/', methods=['GET', 'POST'])
def index():
    """
    Maneja el registro completo de nuevos clientes.
    """
    mensaje = None
    if request.method == 'POST':
        try:
            form_data = {k: (v if v != '' else None) for k, v in request.form.items()}

            if not form_data.get('nombre_apellido') or not form_data.get('cedula'):
                mensaje = "Error: El nombre y la cédula son campos obligatorios."
                return render_template('index.html', mensaje=mensaje)

            db = get_db()
            cursor = db.cursor()
            
            query = """
            INSERT INTO clientes (
                nombre_apellido, cedula, contrato_nro, telefono, asesor, responsable, fecha_inscripcion,
                grupo, plan, plan_contratado, duracion_plan, moneda_pago, valor_cuota,
                inscripcion_porcentaje, inscripcion_monto, proceso, 
                reserva_monto_total, reserva_monto_pagado
            ) VALUES (
                %(nombre_apellido)s, %(cedula)s, %(contrato_nro)s, %(telefono)s, %(asesor)s, %(responsable)s, %(fecha_inscripcion)s,
                %(grupo)s, %(plan)s, %(plan_contratado)s, %(duracion_plan)s, %(moneda_pago)s, %(valor_cuota)s,
                %(inscripcion_porcentaje)s, %(inscripcion_monto)s, %(proceso)s,
                %(reserva_monto_total)s, %(reserva_monto_pagado)s
            )
            """
            
            cursor.execute(query, form_data)
            db.commit()
            cursor.close()
            mensaje = f"¡Cliente '{form_data.get('nombre_apellido')}' registrado exitosamente!"

        except psycopg2.IntegrityError:
            db.rollback()
            mensaje = f"Error: La cédula '{form_data.get('cedula')}' ya existe en la base de datos."
        except Exception as e:
            db.rollback()
            mensaje = f"Ocurrió un error inesperado: {e}"
    
    return render_template('index.html', mensaje=mensaje)

@app.route('/consulta', methods=['GET', 'POST'])
def consulta():
    """
    Maneja la búsqueda de clientes y la visualización de su historial de pagos.
    """
    clientes_encontrados = []
    mensaje_error = None
    if request.method == 'POST':
        termino_busqueda = request.form.get('busqueda', '').strip()
        if not termino_busqueda:
            mensaje_error = "Por favor, ingrese un término para buscar."
        else:
            db = get_db()
            cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
            
            query_clientes = """
                SELECT * FROM clientes 
                WHERE cedula LIKE %s OR nombre_apellido ILIKE %s OR telefono LIKE %s
                LIMIT 20;
            """
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

@app.route('/registrar_pago/<int:client_id>', methods=['GET', 'POST'])
def registrar_pago(client_id):
    """
    Maneja el registro de un nuevo pago para un cliente específico.
    """
    db = get_db()
    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cursor.execute("SELECT id, nombre_apellido FROM clientes WHERE id = %s", (client_id,))
    cliente = cursor.fetchone()

    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta'))

    if request.method == 'POST':
        try:
            monto = request.form['monto']
            cuotas = request.form['cuotas']
            recibo_nro = request.form['recibo']
            forma_pago = request.form['forma_pago']

            query = """
            INSERT INTO pagos (cliente_id, monto, cuotas, recibo, forma_pago)
            VALUES (%s, %s, %s, %s, %s) RETURNING id;
            """
            cursor.execute(query, (client_id, monto, cuotas, recibo_nro, forma_pago))
            nuevo_pago_id = cursor.fetchone()['id']
            db.commit()
            cursor.close()
            
            return redirect(url_for('ver_recibo', pago_id=nuevo_pago_id))

        except Exception as e:
            db.rollback()
            flash(f'Ocurrió un error al registrar el pago: {e}', 'error')

    cursor.close()
    return render_template('registrar_pago.html', cliente=cliente)

@app.route('/recibo/<int:pago_id>')
def ver_recibo(pago_id):
    """
    Muestra una página de recibo para un pago específico.
    """
    db = get_db()
    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    query = """
        SELECT p.*, c.nombre_apellido, c.cedula 
        FROM pagos p 
        JOIN clientes c ON p.cliente_id = c.id 
        WHERE p.id = %s;
    """
    cursor.execute(query, (pago_id,))
    pago = cursor.fetchone()
    cursor.close()

    if not pago:
        flash('Recibo no encontrado.', 'error')
        return redirect(url_for('consulta'))
        
    return render_template('recibo.html', pago=pago)

@app.route('/delete/<int:client_id>', methods=['POST'])
def delete_client(client_id):
    """
    Elimina un cliente y todos sus pagos asociados de la base de datos.
    """
    try:
        db = get_db()
        cursor = db.cursor()
        
        # Gracias a "ON DELETE CASCADE" en el schema, al borrar un cliente,
        # sus pagos asociados se borrarán automáticamente.
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