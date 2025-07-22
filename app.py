import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, g

app = Flask(__name__)

# Lee la URL de la base de datos desde las variables de entorno de Render
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
    Maneja el registro de nuevos clientes en la base de datos PostgreSQL.
    """
    mensaje = None
    if request.method == 'POST':
        try:
            form_data = {k: v for k, v in request.form.items()}
            if not form_data.get('nombre_apellido'):
                mensaje = "Error: El nombre es un campo obligatorio."
                return render_template('index.html', mensaje=mensaje)

            db = get_db()
            cursor = db.cursor()
            
            # Si se proporciona una cédula, se intenta actualizar. Si no, se inserta.
            query = """
            INSERT INTO clientes (
                cedula, contrato_nro, nombre_apellido, telefono, fecha_ingreso, grupo, plan,
                moneda_pago, asesor, responsable, proceso, estatus, estatus_1,
                inscripcion_porcentaje, inscripcion_monto, cuotas_pagas, pagos_impuntuales,
                cuotas_mora, valor_cuota, fecha_pago_recurrente, estatus_cuota, valor_cancelado, observacion
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (cedula) WHERE cedula IS NOT NULL DO UPDATE SET
                contrato_nro = EXCLUDED.contrato_nro,
                nombre_apellido = EXCLUDED.nombre_apellido,
                telefono = EXCLUDED.telefono;
            """
            
            values = (
                form_data.get('cedula') or None, form_data.get('contrato_nro'), form_data.get('nombre_apellido'), 
                form_data.get('telefono'), form_data.get('fecha_ingreso'), form_data.get('grupo'), 
                form_data.get('plan'), form_data.get('moneda_pago'), form_data.get('asesor'), 
                form_data.get('responsable'), form_data.get('proceso'), form_data.get('estatus'), 
                form_data.get('estatus_1'), form_data.get('inscripcion_porcentaje'), form_data.get('inscripcion_monto'), 
                form_data.get('cuotas_pagas'), form_data.get('pagos_impuntuales'), form_data.get('cuotas_mora'), 
                form_data.get('valor_cuota'), form_data.get('fecha_pago_recurrente'), form_data.get('estatus_cuota'), 
                form_data.get('valor_cancelado'), form_data.get('observacion')
            )
            
            cursor.execute(query, values)
            db.commit()
            cursor.close()
            mensaje = "¡Cliente registrado/actualizado exitosamente!"

        except Exception as e:
            mensaje = f"Ocurrió un error: {e}"
    
    return render_template('index.html', mensaje=mensaje)

@app.route('/consulta', methods=['GET', 'POST'])
def consulta():
    """
    Maneja la búsqueda de clientes por cédula, nombre o teléfono.
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
            
            # Búsqueda flexible: ILIKE es insensible a mayúsculas/minúsculas
            # Se buscan coincidencias parciales con %%
            query = """
                SELECT * FROM clientes 
                WHERE cedula LIKE %s 
                OR nombre_apellido ILIKE %s 
                OR telefono LIKE %s
                LIMIT 20;
            """
            # El patrón de búsqueda busca coincidencias en cualquier parte del texto
            patron = f'%{termino_busqueda}%'
            
            cursor.execute(query, (patron, patron, patron))
            clientes_encontrados = cursor.fetchall()
            
            if not clientes_encontrados:
                mensaje_error = "🚫 No se encontraron clientes que coincidan con su búsqueda."
            
            cursor.close()
    
    return render_template('consulta.html', clientes=clientes_encontrados, mensaje_error=mensaje_error)

if __name__ == '__main__':
    app.run(debug=True)