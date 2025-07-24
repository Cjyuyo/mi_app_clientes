from flask import Flask, render_template, request, redirect, url_for, flash
import json
import os
import uuid
from datetime import datetime

app = Flask(__name__)
# Se mantiene tu secret_key para los mensajes flash.
app.secret_key = 'solucion_definitiva_contra_el_404'

# Constante para el nombre del archivo de la base de datos.
DB_FILE = 'clientes.json'

# --- FUNCIONES AUXILIARES PARA MANEJAR LA BASE DE DATOS (JSON) ---

def cargar_clientes():
    """Carga los clientes desde el archivo JSON de forma segura."""
    if not os.path.exists(DB_FILE):
        return []
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return []

def guardar_clientes(clientes):
    """Guarda la lista de clientes en el archivo JSON."""
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(clientes, f, indent=4, ensure_ascii=False)

# --- RUTAS DE LA APLICACIÓN ---

@app.route('/')
def index():
    """
    Página principal que muestra una lista de todos los clientes registrados.
    """
    clientes = cargar_clientes()
    # Se renderiza el nuevo index.html que sí muestra una lista.
    return render_template('index.html', clientes=clientes)

@app.route('/registrar', methods=['GET', 'POST'])
def registrar_cliente():
    """
    Maneja el registro de un nuevo cliente.
    GET: Muestra el formulario de registro.
    POST: Procesa los datos del formulario y crea el cliente.
    """
    if request.method == 'POST':
        clientes = cargar_clientes()
        
        # Se leen los datos del formulario usando los nombres correctos de los campos.
        nuevo_cliente = {
            'id': str(uuid.uuid4()),
            'nombre': request.form['nombre'],
            'cedula': request.form.get('cedula', 'N/A'),
            'contrato': request.form.get('contrato', 'N/A'),
            'telefono': request.form.get('telefono', 'N/A'),
            'fecha_ingreso': request.form.get('fecha_ingreso', datetime.now().strftime('%Y-%m-%d')),
            'monto_total': float(request.form.get('monto_total', 0)),
            'numero_cuotas': int(request.form.get('numero_cuotas', 24)),
            'monto_inscripcion': float(request.form.get('monto_inscripcion', 0)),
            'bien_solicitado': request.form.get('bien_solicitado', 'N/A'),
            'estatus': 'Activo',
            'estatus_detallado': 'Al día',
            'asesor': request.form.get('asesor', 'N/A'),
            'responsable': request.form.get('responsable', 'N/A'),
            'pagos': []  # Lista para registrar futuros pagos
        }
        
        clientes.append(nuevo_cliente)
        guardar_clientes(clientes)
        
        flash(f'Cliente "{nuevo_cliente["nombre"]}" registrado con éxito.', 'success')
        return redirect(url_for('index'))
    
    # Si es GET, simplemente muestra el formulario de registro.
    return render_template('registrar.html')

@app.route('/cliente/<id_cliente>')
def ver_cliente(id_cliente):
    """
    Muestra el estado de cuenta detallado de un cliente específico.
    """
    clientes = cargar_clientes()
    cliente = next((c for c in clientes if c['id'] == id_cliente), None)

    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('index'))

    # Cálculos para el estado de cuenta
    pagos = cliente.get('pagos', [])
    cuotas_pagadas = sum(1 for p in pagos if p.get('tipo') == 'normal')
    total_pagado = sum(p.get('monto', 0) for p in pagos)
    monto_total_plan = float(cliente.get('monto_total', 0))
    total_cuotas_plan = int(cliente.get('numero_cuotas', 1))

    saldo_pendiente = monto_total_plan - total_pagado
    progreso = (total_pagado / monto_total_plan) * 100 if monto_total_plan > 0 else 0
    valor_cuota_ideal = monto_total_plan / total_cuotas_plan if total_cuotas_plan > 0 else 0

    return render_template(
        'consulta.html', 
        cliente=cliente, 
        pagos=sorted(pagos, key=lambda p: p.get('fecha', ''), reverse=True),
        total_pagado=total_pagado,
        saldo_pendiente=saldo_pendiente,
        progreso=progreso,
        cuotas_pagadas=cuotas_pagadas,
        valor_cuota_ideal=valor_cuota_ideal
    )
    
@app.route('/editar/<id_cliente>', methods=['GET', 'POST'])
def editar_cliente(id_cliente):
    """
    Maneja la edición de un cliente existente.
    GET: Muestra el formulario con los datos actuales del cliente.
    POST: Actualiza los datos del cliente.
    """
    clientes = cargar_clientes()
    cliente = next((c for c in clientes if c['id'] == id_cliente), None)

    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('index'))

    if request.method == 'POST':
        # Actualizar datos del cliente
        cliente['nombre'] = request.form['nombre']
        cliente['cedula'] = request.form.get('cedula')
        cliente['contrato'] = request.form.get('contrato')
        # ... puedes añadir todos los demás campos que quieras que sean editables
        guardar_clientes(clientes)
        flash('Cliente actualizado con éxito.', 'success')
        return redirect(url_for('ver_cliente', id_cliente=id_cliente))

    return render_template('edit_cliente.html', cliente=cliente)


@app.route('/pagar/<id_cliente>', methods=['POST'])
def registrar_pago(id_cliente):
    """Registra un nuevo pago para un cliente."""
    clientes = cargar_clientes()
    cliente_encontrado = next((c for c in clientes if c['id'] == id_cliente), None)

    if not cliente_encontrado:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('index'))

    try:
        monto = float(request.form['monto'])
        tipo_pago = request.form['tipo_pago']
    except (ValueError, KeyError):
        flash('Datos de pago inválidos.', 'error')
        return redirect(url_for('ver_cliente', id_cliente=id_cliente))

    nuevo_pago = {
        'id': str(uuid.uuid4()),
        'monto': monto,
        'fecha': datetime.now().strftime('%Y-%m-%d'),
        'tipo': tipo_pago,
        'forma_pago': request.form.get('forma_pago', 'Efectivo'),
        'recibo': request.form.get('recibo_nro', 'N/A')
    }
    
    cliente_encontrado.setdefault('pagos', []).append(nuevo_pago)
    guardar_clientes(clientes)
    
    flash('Pago registrado con éxito.', 'success')
    return redirect(url_for('ver_cliente', id_cliente=id_cliente))

@app.route('/eliminar/<id_cliente>', methods=['POST'])
def eliminar_cliente(id_cliente):
    """Elimina un cliente de la lista."""
    clientes = cargar_clientes()
    clientes_actualizados = [c for c in clientes if c['id'] != id_cliente]

    if len(clientes) == len(clientes_actualizados):
        flash('Error: No se pudo encontrar el cliente para eliminar.', 'error')
    else:
        guardar_clientes(clientes_actualizados)
        flash('Cliente eliminado con éxito.', 'success')
        
    return redirect(url_for('index'))


if __name__ == '__main__':
    # Usar debug=True para desarrollo, facilita la detección de errores.
    app.run(host='0.0.0.0', port=5000, debug=True)