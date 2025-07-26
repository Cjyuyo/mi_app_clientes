import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, redirect, url_for, flash
from num2words import num2words # Asegúrate de instalar esta librería: pip install num2words

app = Flask(__name__)

# Configuración de la clave secreta para mensajes flash
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'una-clave-secreta-muy-segura')

def get_db_connection():
    """
    Establece la conexión con la base de datos PostgreSQL usando la URL de Render.
    """
    try:
        conn = psycopg2.connect(os.environ.get('DATABASE_URL'))
        return conn
    except psycopg2.OperationalError as e:
        # Este error es común si la base de datos no está lista o las credenciales son incorrectas.
        print(f"Error al conectar a la base de datos: {e}")
        return None

def num_to_words_es(n):
    """
    Convierte un número a su representación en palabras en español.
    Utiliza la librería num2words.
    """
    try:
        return num2words(n, lang='es')
    except Exception as e:
        print(f"Error en num2words: {e}")
        return "Error de conversion"

# --- RUTAS PRINCIPALES DE LA APLICACIÓN ---

@app.route('/')
def index():
    """
    Página principal, redirige a la consulta de clientes.
    """
    return redirect(url_for('consultar_clientes'))

@app.route('/consultar_clientes', methods=['GET', 'POST'])
def consultar_clientes():
    """
    Muestra la lista de clientes y permite buscar por cédula.
    """
    conn = get_db_connection()
    if conn is None:
        flash('No se pudo conectar a la base de datos.', 'danger')
        return render_template('consultar_clientes.html', clientes=[])

    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    if request.method == 'POST':
        cedula_busqueda = request.form.get('cedula')
        cur.execute("SELECT * FROM clientes WHERE cedula = %s", (cedula_busqueda,))
        cliente = cur.fetchone()
        cur.close()
        conn.close()
        if cliente:
            return redirect(url_for('consulta_cliente', cliente_id=cliente['id']))
        else:
            flash('Cliente no encontrado con esa cédula.', 'warning')
            return redirect(url_for('consultar_clientes'))

    # Si es GET, muestra todos los clientes
    cur.execute("SELECT * FROM clientes ORDER BY nombre_apellido ASC")
    clientes = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('consultar_clientes.html', clientes=clientes)


@app.route('/cliente/<int:cliente_id>')
def consulta_cliente(cliente_id):
    """
    Muestra el perfil detallado de un cliente, incluyendo su historial de pagos.
    """
    conn = get_db_connection()
    if conn is None:
        flash('No se pudo conectar a la base de datos.', 'danger')
        return redirect(url_for('consultar_clientes'))
        
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # Obtener datos del cliente
    cur.execute("SELECT * FROM clientes WHERE id = %s", (cliente_id,))
    cliente = cur.fetchone()

    if not cliente:
        flash('Cliente no encontrado.', 'danger')
        cur.close()
        conn.close()
        return redirect(url_for('consultar_clientes'))

    # Obtener historial de pagos
    cur.execute("""
        SELECT p.*, r.id as recibo_id 
        FROM pagos p 
        LEFT JOIN recibos r ON p.pago_id = r.pago_id 
        WHERE p.cliente_id = %s 
        ORDER BY p.fecha_pago DESC, p.pago_id DESC
    """, (cliente_id,))
    pagos_historial = cur.fetchall()
    
    # Simulación de cálculo de cuotas (deberías tener esta lógica más desarrollada)
    pagos_info = {
        'cuotas_pagadas': 0, # Este valor debería calcularse correctamente
        'balance': 0.00      # Este valor debería calcularse correctamente
    }

    cur.close()
    conn.close()

    return render_template('consulta_cliente.html', cliente=cliente, pagos_historial=pagos_historial, pagos_info=pagos_info)

@app.route('/recibo/<int:recibo_id>')
def generar_recibo(recibo_id):
    """
    Genera y muestra un recibo de pago para imprimir.
    """
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    cur.execute("""
        SELECT p.*, r.id as recibo_id_tabla_recibo
        FROM pagos p
        JOIN recibos r ON p.pago_id = r.pago_id
        WHERE r.id = %s AND p.estado = 'Conciliado'
    """, (recibo_id,))
    recibo = cur.fetchone()

    if not recibo:
        flash('Recibo no encontrado o no está conciliado.', 'danger')
        return redirect(request.referrer or url_for('consultar_clientes'))

    cur.execute("SELECT * FROM clientes WHERE id = %s", (recibo['cliente_id'],))
    cliente = cur.fetchone()
    
    cur.close()
    conn.close()

    monto_entero = int(recibo['monto_usd'])
    monto_decimal = int(round((recibo['monto_usd'] - monto_entero) * 100))
    monto_en_letras = num_to_words_es(monto_entero).upper()
    centavos_en_letras = num_to_words_es(monto_decimal).upper() if monto_decimal > 0 else "CERO"

    return render_template('recibo.html', recibo=recibo, cliente=cliente, monto_en_letras=monto_en_letras, centavos_en_letras=centavos_en_letras)


@app.route('/anular_recibo/<int:recibo_id>', methods=['POST'])
def anular_recibo(recibo_id):
    """
    Anula un recibo y revierte los cambios en la cuenta del cliente.
    """
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # Lógica para anular el recibo
    # 1. Encontrar el pago asociado al recibo
    cur.execute("SELECT pago_id FROM recibos WHERE id = %s", (recibo_id,))
    pago_a_anular = cur.fetchone()

    if not pago_a_anular:
        flash('Recibo no encontrado para anular.', 'danger')
        cur.close()
        conn.close()
        return redirect(request.referrer)

    pago_id = pago_a_anular['pago_id']
    
    # 2. Actualizar el estado del pago a 'Anulado'
    cur.execute("UPDATE pagos SET estado = 'Anulado' WHERE pago_id = %s RETURNING cliente_id", (pago_id,))
    cliente_id_afectado = cur.fetchone()['cliente_id']

    # 3. Aquí iría la lógica para revertir el balance, cuotas pagadas, etc.
    #    (Esta parte es crucial y depende de tu modelo de datos)

    conn.commit()
    cur.close()
    conn.close()

    flash(f'Recibo N° {recibo_id} ha sido anulado exitosamente.', 'success')
    return redirect(url_for('consulta_cliente', cliente_id=cliente_id_afectado))


# --- NUEVA RUTA PARA VER RECIBOS ANULADOS ---
@app.route('/recibo_anulado/<int:recibo_id>')
def ver_recibo_anulado(recibo_id):
    """
    Muestra un recibo que ha sido previamente anulado, con una marca de agua.
    """
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cur.execute("""
        SELECT p.*, r.id as recibo_id_tabla_recibo 
        FROM pagos p
        LEFT JOIN recibos r ON p.pago_id = r.pago_id
        WHERE r.id = %s AND p.estado = 'Anulado'
    """, (recibo_id,))
    recibo = cur.fetchone()

    if not recibo:
        flash('El recibo anulado que intenta ver no existe o no se encontró.', 'danger')
        cur.close()
        conn.close()
        return redirect(url_for('consultar_clientes'))

    cur.execute("SELECT * FROM clientes WHERE id = %s", (recibo['cliente_id'],))
    cliente = cur.fetchone()

    cur.close()
    conn.close()
    
    if not cliente:
        flash('No se encontró el cliente asociado a este recibo.', 'danger')
        return redirect(url_for('consultar_clientes'))

    monto_entero = int(recibo['monto_usd'])
    monto_decimal = int(round((recibo['monto_usd'] - monto_entero) * 100))
    monto_en_letras = num_to_words_es(monto_entero).upper()
    centavos_en_letras = num_to_words_es(monto_decimal).upper() if monto_decimal > 0 else "CERO"

    return render_template(
        'recibo_anulado.html', 
        recibo=recibo, 
        cliente=cliente,
        monto_en_letras=monto_en_letras,
        centavos_en_letras=centavos_en_letras
    )

# --- OTRAS RUTAS (Ejemplos para CRUD de Clientes) ---
# Estas son plantillas, necesitarías crear sus respectivos archivos HTML y lógica.

@app.route('/cliente/nuevo', methods=['GET', 'POST'])
def agregar_cliente():
    if request.method == 'POST':
        # Lógica para guardar nuevo cliente
        flash('Cliente agregado exitosamente.', 'success')
        return redirect(url_for('consultar_clientes'))
    return render_template('agregar_cliente.html') # Debes crear esta plantilla

@app.route('/cliente/editar/<int:cliente_id>', methods=['GET', 'POST'])
def editar_cliente(cliente_id):
    # Lógica para buscar y editar cliente
    if request.method == 'POST':
        # Lógica para actualizar cliente
        flash('Cliente actualizado exitosamente.', 'success')
        return redirect(url_for('consulta_cliente', cliente_id=cliente_id))
    # Lógica para obtener datos del cliente y pasarlos a la plantilla
    return render_template('editar_cliente.html') # Debes crear esta plantilla

if __name__ == '__main__':
    # El puerto se obtiene de la variable de entorno PORT, común en servicios como Render.
    port = int(os.environ.get('PORT', 5000))
    # app.run() es para desarrollo. Para producción, Render usa un servidor WSGI como Gunicorn.
    app.run(host='0.0.0.0', port=port, debug=True)
