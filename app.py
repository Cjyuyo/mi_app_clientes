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
    # Lógica para registrar un nuevo cliente
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
                nombre_apellido, cedula, contrato_nro, telefono, asesor, responsable, fecha_inscripcion,
                grupo, plan, plan_contratado, duracion_plan, cuotas_totales, moneda_pago, valor_cuota,
                inscripcion_monto, proceso, estatus, estatus_1
            ) VALUES (
                %(nombre_apellido)s, %(cedula)s, %(contrato_nro)s, %(telefono)s, %(asesor)s, %(responsable)s, %(fecha_inscripcion)s,
                %(grupo)s, %(plan)s, %(plan_contratado)s, %(duracion_plan)s, %(cuotas_totales)s, %(moneda_pago)s, %(valor_cuota)s,
                %(inscripcion_monto)s, %(proceso)s, %(estatus)s, %(estatus_1)s
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
    # ... (código de consulta sin cambios)
    return render_template('consulta.html', clientes=[], mensaje_error=None)


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
            form_data = {k: (v if v != '' else None) for k, v in request.form.items()}
            form_data['cliente_id'] = client_id
            
            # Lógica de cuotas
            # ... (código de cuotas)

            # Guardar el pago
            pago_query = """
            INSERT INTO pagos (
                cliente_id, monto, cuotas, forma_pago, pago_en, cantidad_en_letras, 
                por_concepto_de, referencia, banco, lugar_emision
            ) VALUES (
                %(cliente_id)s, %(monto)s, %(cuotas)s, %(forma_pago)s, %(pago_en)s, 
                %(cantidad_en_letras)s, %(por_concepto_de)s, %(referencia)s, %(banco)s, %(lugar_emision)s
            ) RETURNING id;
            """
            cursor.execute(pago_query, form_data)
            nuevo_pago_id = cursor.fetchone()['id']
            
            db.commit()
            
            flash('¡Pago registrado exitosamente!', 'success')
            return redirect(url_for('ver_recibo', pago_id=nuevo_pago_id))

        except Exception as e:
            db.rollback()
            flash(f'Registro fallido. Ocurrió un error: {e}', 'error')
            return redirect(url_for('registrar_pago', client_id=client_id))

    cursor.close()
    return render_template('registrar_pago.html', cliente=cliente)

# ... (El resto de las rutas no cambian)