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
            
            # Consulta actualizada para coincidir con el nuevo schema y formulario
            query = """
            INSERT INTO clientes (
                nombre_apellido, cedula, contrato_nro, telefono, asesor, responsable, fecha_inscripcion,
                grupo, bien_solicitado, plan_contratado, cuotas_totales, moneda_pago, valor_cuota,
                inscripcion_monto, proceso, reserva_monto_total, reserva_monto_pagado
            ) VALUES (
                %(nombre_apellido)s, %(cedula)s, %(contrato_nro)s, %(telefono)s, %(asesor)s, %(responsable)s, %(fecha_inscripcion)s,
                %(grupo)s, %(bien_solicitado)s, %(plan_contratado)s, %(cuotas_totales)s, %(moneda_pago)s, %(valor_cuota)s,
                %(inscripcion_monto)s, %(proceso)s, %(reserva_monto_total)s, %(reserva_monto_pagado)s
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

# --- El resto de las rutas (consulta, edit_client, etc.) no cambian ---

@app.route('/consulta', methods=['GET', 'POST'])
def consulta():
    # ... (código anterior)
    # Es importante asegurarse de que esta función esté completa en tu archivo real
    clientes_encontrados = []
    mensaje_error = None
    if request.method == 'POST':
        # ... (lógica de búsqueda)
        pass
    return render_template('consulta.html', clientes=clientes_encontrados, mensaje_error=mensaje_error)


@app.route('/edit/<int:client_id>', methods=['GET', 'POST'])
def edit_client(client_id):
    # ... (código anterior)
    # Es importante asegurarse de que esta función esté completa en tu archivo real
    return redirect(url_for('consulta'))

# ... (y así sucesivamente para el resto de las funciones)

if __name__ == '__main__':
    app.run(debug=True)
