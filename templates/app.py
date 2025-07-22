import sqlite3
from flask import Flask, render_template, request, g

app = Flask(__name__)
DATABASE = 'clientes.db'

# --- Gestión de la conexión a la base de datos ---
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

# --- Rutas de la aplicación ---
@app.route('/', methods=['GET', 'POST'])
def index():
    mensaje = None
    if request.method == 'POST':
        try:
            form_data = {k: v for k, v in request.form.items()}
            if not form_data.get('cedula'):
                mensaje = "Error: La cédula es un campo obligatorio."
                return render_template('index.html', mensaje=mensaje)
            db = get_db()
            db.execute("""
                INSERT OR REPLACE INTO clientes (
                    cedula, contrato_nro, nombre_apellido, telefono, fecha_ingreso, grupo, plan,
                    moneda_pago, asesor, responsable, proceso, estatus, estatus_1,
                    inscripcion_porcentaje, inscripcion_monto, cuotas_pagas, pagos_impuntuales,
                    cuotas_mora, valor_cuota, fecha_pago_recurrente, estatus_cuota, valor_cancelado, observacion
                ) VALUES (
                    :cedula, :contrato_nro, :nombre_apellido, :telefono, :fecha_ingreso, :grupo, :plan,
                    :moneda_pago, :asesor, :responsable, :proceso, :estatus, :estatus_1,
                    :inscripcion_porcentaje, :inscripcion_monto, :cuotas_pagas, :pagos_impuntuales,
                    :cuotas_mora, :valor_cuota, :fecha_pago_recurrente, :estatus_cuota, :valor_cancelado, :observacion
                )
            """, form_data)
            db.commit()
            mensaje = "¡Cliente registrado/actualizado exitosamente!"
        except sqlite3.Error as err:
            mensaje = f"Error en la base de datos: {err}"
        except Exception as err:
            mensaje = f"Ha ocurrido un error inesperado: {err}"
    return render_template('index.html', mensaje=mensaje)

@app.route('/consulta', methods=['GET', 'POST'])
def consulta():
    cliente = None
    pagos = None
    mensaje_error = None
    if request.method == 'POST':
        cedula = request.form.get('cedula')
        if not cedula:
            mensaje_error = "Por favor, ingrese una cédula para buscar."
        else:
            db = get_db()
            cliente = db.execute('SELECT * FROM clientes WHERE cedula = ?', (cedula,)).fetchone()
            if not cliente:
                mensaje_error = "🚫 Cliente no registrado. Verifique la cédula e intente de nuevo."
    return render_template('consulta.html', cliente=cliente, pagos=pagos, mensaje_error=mensaje_error)

# --- Función para inicializar la BD ---
def init_db():
    with app.app_context():
        db = get_db()
        with app.open_resource('schema.sql', mode='r') as f:
            db.cursor().executescript(f.read())
        db.commit()
        print("Base de datos inicializada con el nuevo esquema.")

if __name__ == '__main__':
    # init_db() # Descomenta esta línea y ejecuta "python app.py" UNA SOLA VEZ para crear la BD
    app.run(debug=True)

