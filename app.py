import os
from flask import Flask, render_template, request, url_for, redirect, flash

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'a-very-secret-key')

# --- RUTAS DE PRUEBA SIMPLES ---

@app.route('/')
def index():
    # Esta ruta debe mostrar la página de registro.
    try:
        return render_template('index.html')
    except Exception as e:
        # Si hay un error cargando la plantilla, lo mostrará.
        return f"<h1>Error al cargar la plantilla index.html</h1><p>{e}</p>"

@app.route('/consulta', methods=['GET', 'POST'])
def consulta():
    # Esta ruta muestra la página de consulta con datos de ejemplo.
    # No se conecta a la base de datos.
    dummy_cliente = {
        'id': 1, 'nombre_apellido': 'Cliente de Prueba', 'cedula': '12345678',
        'contrato_nro': 'C-001', 'telefono': '0414-1234567', 'fecha_ingreso': None,
        'grupo': 'MP1', 'bien_solicitado': 'Moto XYZ', 'estatus': 'Activo',
        'estatus_1': 'Solvente', 'asesor': 'Asesor de Prueba', 'responsable': 'Responsable de Prueba',
        'proceso': 'INSCRITO', 'valor_cuota': 100.00, 'inscripcion_monto': 200.00,
        'inscripcion_pagada': 50.00, 'valor_cancelado': 50.00, 'cuotas_totales': 36,
        'cuotas_pagadas_progresivas': 0, 'balance_regresivo': 0.00, 'pagos': []
    }
    termino_busqueda = request.form.get('busqueda', '') or request.args.get('busqueda', '')
    clientes_encontrados = [dummy_cliente] if termino_busqueda else []
    return render_template('consulta.html', clientes=clientes_encontrados, busqueda=termino_busqueda, mensaje_error=None)

# --- RUTAS DE RELLENO PARA EVITAR ERRORES EN LAS PLANTILLAS ---
# Estas rutas no hacen nada, pero deben existir para que url_for() no falle.

@app.route('/registrar_cliente', methods=['POST'])
def registrar_cliente():
    flash('Cliente de prueba registrado (simulación).', 'success')
    return redirect(url_for('index'))

@app.route('/edit/<int:client_id>', methods=['GET', 'POST'])
def edit_client(client_id):
    return f"<h1>Simulación de Edición</h1><p>Editando cliente {client_id}.</p>"

@app.route('/registrar_pago/<int:client_id>', methods=['GET', 'POST'])
def registrar_pago(client_id):
    return f"<h1>Simulación de Registro de Pago</h1><p>Registrando pago para el cliente {client_id}.</p>"

@app.route('/delete/<int:client_id>', methods=['POST'])
def delete_client(client_id):
    return redirect(url_for('consulta'))

@app.route('/recibo/<int:pago_id>')
def ver_recibo(pago_id):
    return f"<h1>Viendo recibo de prueba {pago_id}</h1>"
    
@app.route('/conciliar_pago/<int:pago_id>', methods=['POST'])
def conciliar_pago(pago_id):
    return redirect(url_for('consulta'))

@app.route('/recibo_inscripcion/<int:pago_id>')
def ver_recibo_inscripcion(pago_id):
    return f"<h1>Viendo recibo de inscripción de prueba {pago_id}</h1>"

@app.route('/anular_recibo/<int:pago_id>', methods=['POST'])
def anular_recibo(pago_id):
    return redirect(url_for('consulta'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
