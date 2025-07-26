import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, redirect, url_for, flash
# Ya no se importa num2words

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
        print(f"Error al conectar a la base de datos: {e}")
        return None

# --- NUEVA FUNCIÓN INTERNA PARA CONVERTIR NÚMEROS A LETRAS ---
def convertir_numero_a_letras(numero):
    """
    Convierte un número entero a su representación en palabras en español.
    Función interna para no depender de librerías externas.
    """
    if not 0 <= numero < 1000000:
        return "Número fuera de rango"
    if numero == 0:
        return "cero"

    unidades = ["", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve"]
    decenas = ["", "diez", "veinte", "treinta", "cuarenta", "cincuenta", "sesenta", "setenta", "ochenta", "noventa"]
    centenas = ["", "ciento", "doscientos", "trescientos", "cuatrocientos", "quinientos", "seiscientos", "setecientos", "ochocientos", "novecientos"]
    
    especiales_diez = {
        11: "once", 12: "doce", 13: "trece", 14: "catorce", 15: "quince",
        16: "dieciséis", 17: "diecisiete", 18: "dieciocho", 19: "diecinueve",
        21: "veintiuno", 22: "veintidós", 23: "veintitrés", 24: "veinticuatro", 
        25: "veinticinco", 26: "veintiséis", 27: "veintisiete", 28: "veintiocho", 29: "veintinueve"
    }

    def _convertir(n):
        if n < 10:
            return unidades[n]
        if n in especiales_diez:
            return especiales_diez[n]
        if n < 30:
            if n == 20: return "veinte"
            return "veinti" + unidades[n % 10]
        if n < 100:
            if n % 10 == 0:
                return decenas[n // 10]
            return decenas[n // 10] + " y " + unidades[n % 10]
        if n < 1000:
            if n % 100 == 0:
                if n == 100: return "cien"
                return centenas[n // 100]
            return centenas[n // 100] + " " + _convertir(n % 100)
        if n < 2000:
            return "mil " + _convertir(n % 1000)
        if n < 1000000:
            return _convertir(n // 1000) + " mil " + _convertir(n % 1000)
        return ""

    return _convertir(numero)


# --- RUTAS DE LA APLICACIÓN ---

@app.route('/')
def index():
    return redirect(url_for('consultar_clientes'))

@app.route('/consultar_clientes', methods=['GET', 'POST'])
def consultar_clientes():
    conn = get_db_connection()
    if conn is None:
        flash('No se pudo conectar a la base de datos.', 'danger')
        # CORRECCIÓN: Apuntar al nombre de archivo correcto
        return render_template('consulta.html', clientes=[])
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
    cur.execute("SELECT * FROM clientes ORDER BY nombre_apellido ASC")
    clientes = cur.fetchall()
    cur.close()
    conn.close()
    # CORRECCIÓN: Apuntar al nombre de archivo correcto
    return render_template('consulta.html', clientes=clientes)


@app.route('/cliente/<int:cliente_id>')
def consulta_cliente(cliente_id):
    conn = get_db_connection()
    if conn is None:
        flash('No se pudo conectar a la base de datos.', 'danger')
        return redirect(url_for('consultar_clientes'))
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT * FROM clientes WHERE id = %s", (cliente_id,))
    cliente = cur.fetchone()
    if not cliente:
        flash('Cliente no encontrado.', 'danger')
        cur.close()
        conn.close()
        return redirect(url_for('consultar_clientes'))
    cur.execute("""
        SELECT p.*, r.id as recibo_id 
        FROM pagos p 
        LEFT JOIN recibos r ON p.pago_id = r.pago_id 
        WHERE p.cliente_id = %s 
        ORDER BY p.fecha_pago DESC, p.pago_id DESC
    """, (cliente_id,))
    pagos_historial = cur.fetchall()
    pagos_info = {'cuotas_pagadas': 0, 'balance': 0.00}
    cur.close()
    conn.close()
    return render_template('consulta_cliente.html', cliente=cliente, pagos_historial=pagos_historial, pagos_info=pagos_info)

@app.route('/recibo/<int:recibo_id>')
def generar_recibo(recibo_id):
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
    monto_en_letras = convertir_numero_a_letras(monto_entero).upper()
    centavos_en_letras = convertir_numero_a_letras(monto_decimal).upper() if monto_decimal > 0 else "CERO"

    return render_template('recibo.html', recibo=recibo, cliente=cliente, monto_en_letras=monto_en_letras, centavos_en_letras=centavos_en_letras)


@app.route('/anular_recibo/<int:recibo_id>', methods=['POST'])
def anular_recibo(recibo_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT pago_id FROM recibos WHERE id = %s", (recibo_id,))
    pago_a_anular = cur.fetchone()
    if not pago_a_anular:
        flash('Recibo no encontrado para anular.', 'danger')
        cur.close()
        conn.close()
        return redirect(request.referrer)
    pago_id = pago_a_anular['pago_id']
    cur.execute("UPDATE pagos SET estado = 'Anulado' WHERE pago_id = %s RETURNING cliente_id", (pago_id,))
    cliente_id_afectado = cur.fetchone()['cliente_id']
    conn.commit()
    cur.close()
    conn.close()
    flash(f'Recibo N° {recibo_id} ha sido anulado exitosamente.', 'success')
    return redirect(url_for('consulta_cliente', cliente_id=cliente_id_afectado))

@app.route('/recibo_anulado/<int:recibo_id>')
def ver_recibo_anulado(recibo_id):
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
    monto_en_letras = convertir_numero_a_letras(monto_entero).upper()
    centavos_en_letras = convertir_numero_a_letras(monto_decimal).upper() if monto_decimal > 0 else "CERO"

    return render_template(
        'recibo_anulado.html', 
        recibo=recibo, 
        cliente=cliente,
        monto_en_letras=monto_en_letras,
        centavos_en_letras=centavos_en_letras
    )

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
