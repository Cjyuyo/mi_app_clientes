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
    # ... (código de registro de clientes sin cambios)
    return render_template('index.html')

@app.route('/consulta', methods=['GET', 'POST'])
def consulta():
    # ... (código de consulta de clientes sin cambios)
    return render_template('consulta.html', clientes=clientes_encontrados, mensaje_error=mensaje_error)

# --- NUEVA RUTA PARA EDITAR CLIENTES ---
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
                fecha_inscripcion = %(fecha_inscripcion)s, grupo = %(grupo)s, plan = %(plan)s,
                plan_contratado = %(plan_contratado)s, duracion_plan = %(duracion_plan)s,
                cuotas_totales = %(cuotas_totales)s, moneda_pago = %(moneda_pago)s,
                valor_cuota = %(valor_cuota)s, inscripcion_monto = %(inscripcion_monto)s,
                proceso = %(proceso)s
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

# ... (el resto de tu código como registrar_pago, ver_recibo, delete_client no cambia)
