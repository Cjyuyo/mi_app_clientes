import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, g, flash, redirect, url_for

app = Flask(__name__)
# Se necesita una clave secreta para los mensajes de confirmación (flash messages)
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

        except psycopg2.IntegrityError as e:
            db.rollback()
            mensaje = f"Error: La cédula '{form_data.get('cedula')}' ya existe en la base de datos."
        except Exception as e:
            db.rollback()
            mensaje = f"Ocurrió un error inesperado: {e}"
    
    return render_template('index.html', mensaje=mensaje)

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
            
            query = """
                SELECT * FROM clientes 
                WHERE cedula LIKE %s 
                OR nombre_apellido ILIKE %s 
                OR telefono LIKE %s
                LIMIT 20;
            """
            patron = f'%{termino_busqueda}%'
            
            cursor.execute(query, (patron, patron, patron))
            clientes_encontrados = cursor.fetchall()
            
            if not clientes_encontrados:
                mensaje_error = "🚫 No se encontraron clientes que coincidan con su búsqueda."
            
            cursor.close()
    
    return render_template('consulta.html', clientes=clientes_encontrados, mensaje_error=mensaje_error)

# --- NUEVA RUTA PARA ELIMINAR CLIENTES ---
@app.route('/delete/<int:client_id>', methods=['POST'])
def delete_client(client_id):
    """
    Elimina un cliente de la base de datos usando su ID único.
    """
    try:
        db = get_db()
        cursor = db.cursor()
        
        # Por ahora, solo eliminamos el cliente. En el futuro, podríamos archivar o eliminar pagos.
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