from flask import Flask, render_template, request, redirect, url_for, flash
import json
import os
from datetime import datetime
import uuid

app = Flask(__name__)
app.secret_key = 'clave_super_secreta_para_flash_v2'

DB_FILE = 'clientes.json'

def cargar_clientes():
    """Carga los clientes desde el archivo JSON."""
    if not os.path.exists(DB_FILE):
        return []
    with open(DB_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []

def guardar_clientes(clientes):
    """Guarda la lista de clientes en el archivo JSON."""
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(clientes, f, indent=4, ensure_ascii=False)

@app.route('/')
def index():
    """Página principal que muestra la lista de clientes."""
    clientes = cargar_clientes()
    return render_template('index.html', clientes=clientes)

@app.route('/registrar', methods=['POST'])
def registrar_cliente():
    """Registra un nuevo cliente con todos los detalles."""
    clientes = cargar_clientes()
    
    # Añadimos más campos al registro, con valores por defecto si no se proporcionan
    nuevo_cliente = {
        'id': str(uuid.uuid4()),
        'nombre': request.form['nombre'],
        'cedula': request.form.get('cedula', 'N/A'),
        'contrato': request.form.get('contrato', 'N/A'),
        'telefono': request.form.get('telefono', 'N/A'),
        'fecha_ingreso': datetime.now().strftime('%Y-%m-%d'),
        'monto_total': float(request.form['monto_total']),
        'numero_cuotas': int(request.form.get('numero_cuotas', 24)), # Default a 24 cuotas
        'monto_inscripcion': float(request.form.get('monto_inscripcion', 0)),
        'bien_solicitado': 'N/A',
        'estatus': 'Activo',
        'estatus_detallado': 'Al día',
        'asesor': 'N/A',
        'responsable': 'N/A',
        'pagos': []
    }
    
    clientes.append(nuevo_cliente)
    guardar_clientes(clientes)
    
    flash(f'Cliente "{nuevo_cliente["nombre"]}" registrado con éxito.', 'success')
    return redirect(url_for('index'))

@app.route('/consulta/<id_cliente>')
def consulta(id_cliente):
    """Muestra el estado de cuenta detallado de un cliente."""
    clientes = cargar_clientes()
    cliente = next((c for c in clientes if c['id'] == id_cliente), None)

    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('index'))

    pagos = cliente.get('pagos', [])
    
    # --- LÓGICA DE CÁLCULO PARA EL NUEVO DISEÑO ---
    cuotas_progresivas = sum(1 for p in pagos if p.get('tipo') == 'normal')
    cuotas_regresivas = sum(1 for p in pagos if p.get('tipo') == 'adelantada')
    
    total_pagado = sum(p['monto'] for p in pagos)
    valor_cancelado = cliente.get('monto_inscripcion', 0) + total_pagado
    saldo_pendiente = cliente['monto_total'] - total_pagado

    # Calcular porcentajes para las barras de progreso
    total_cuotas = cliente.get('numero_cuotas', 1) # Evitar división por cero
    progreso_progresivas = (cuotas_progresivas / total_cuotas) * 100 if total_cuotas > 0 else 0
    
    # Lógica para "Balance a favor"
    valor_cuota_ideal = cliente['monto_total'] / total_cuotas if total_cuotas > 0 else 0
    balance_a_favor = total_pagado % valor_cuota_ideal if valor_cuota_ideal > 0 else 0
    progreso_balance = (balance_a_favor / valor_cuota_ideal) * 100 if valor_cuota_ideal > 0 else 0


    return render_template(
        'consulta.html', 
        cliente=cliente, 
        pagos=sorted(pagos, key=lambda p: p.get('fecha', ''), reverse=True),
        total_pagado=total_pagado,
        valor_cancelado=valor_cancelado,
        saldo_pendiente=saldo_pendiente,
        cuotas_progresivas=cuotas_progresivas,
        cuotas_regresivas=cuotas_regresivas,
        progreso_progresivas=progreso_progresivas,
        balance_a_favor=balance_a_favor,
        progreso_balance=progreso_balance,
        valor_cuota_ideal=valor_cuota_ideal
    )

@app.route('/pagar/<id_cliente>', methods=['POST'])
def pagar(id_cliente):
    """Registra un nuevo pago para un cliente."""
    clientes = cargar_clientes()
    cliente_encontrado = next((c for c in clientes if c['id'] == id_cliente), None)

    if not cliente_encontrado:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('index'))

    try:
        monto = float(request.form['monto'])
        tipo_pago = request.form['tipo_pago']
        forma_pago = request.form.get('forma_pago', 'Efectivo')
        # CORRECCIÓN: Usar 'recibo_nro' para que coincida con el HTML
        recibo_nro = request.form.get('recibo_nro', 'None') 
    except (ValueError, KeyError):
        flash('Datos de pago inválidos.', 'error')
        return redirect(url_for('consulta', id_cliente=id_cliente))

    nuevo_pago = {
        'monto': monto,
        'fecha': datetime.now().strftime('%Y-%m-%d'),
        'tipo': tipo_pago,
        'forma_pago': forma_pago,
        'recibo': recibo_nro
    }
    
    cliente_encontrado.setdefault('pagos', []).append(nuevo_pago)
    guardar_clientes(clientes)
    
    flash('Pago registrado con éxito.', 'success')
    return redirect(url_for('consulta', id_cliente=id_cliente))


if __name__ == '__main__':
    app.run(debug=True, port=5001)
