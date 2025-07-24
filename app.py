from flask import Flask, render_template, request, redirect, url_for, flash
import json
import os
from datetime import datetime
import uuid

app = Flask(__name__)
app.secret_key = 'clave_super_secreta_para_flash_v3_a_prueba_de_balas'

DB_FILE = 'clientes.json'

def cargar_clientes():
    """Carga los clientes desde el archivo JSON."""
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

@app.route('/')
def index():
    """Página principal que muestra la lista de clientes."""
    try:
        clientes = cargar_clientes()
        return render_template('index.html', clientes=clientes)
    except Exception as e:
        print(f"ERROR CRÍTICO en la ruta principal '/': {e}")
        flash('Error fatal al cargar la aplicación. Contacte a soporte.', 'error')
        # Devuelve una plantilla vacía para evitar el crash total
        return render_template('index.html', clientes=[])

@app.route('/registrar', methods=['POST'])
def registrar_cliente():
    """Registra un nuevo cliente con todos los detalles."""
    try:
        clientes = cargar_clientes()
        
        nuevo_cliente = {
            'id': str(uuid.uuid4()),
            'nombre': request.form['nombre'],
            'cedula': request.form.get('cedula', 'N/A'),
            'contrato': request.form.get('contrato', 'N/A'),
            'telefono': request.form.get('telefono', 'N/A'),
            'fecha_ingreso': datetime.now().strftime('%Y-%m-%d'),
            'monto_total': float(request.form.get('monto_total', 0)),
            'numero_cuotas': int(request.form.get('numero_cuotas', 24)),
            'monto_inscripcion': float(request.form.get('monto_inscripcion', 0)),
            'bien_solicitado': 'N/A', 'estatus': 'Activo', 'estatus_detallado': 'Al día',
            'asesor': 'N/A', 'responsable': 'N/A', 'pagos': []
        }
        
        clientes.append(nuevo_cliente)
        guardar_clientes(clientes)
        
        flash(f'Cliente "{nuevo_cliente["nombre"]}" registrado con éxito.', 'success')
        return redirect(url_for('index'))
    except Exception as e:
        print(f"ERROR CRÍTICO en /registrar: {e}")
        flash('Error inesperado al registrar el cliente.', 'error')
        return redirect(url_for('index'))

@app.route('/consulta/<id_cliente>')
def consulta(id_cliente):
    """Muestra el estado de cuenta detallado de un cliente."""
    try:
        clientes = cargar_clientes()
        cliente = next((c for c in clientes if c['id'] == id_cliente), None)

        if not cliente:
            flash('Cliente no encontrado.', 'error')
            return redirect(url_for('index'))

        pagos = cliente.get('pagos', [])
        
        cuotas_progresivas = sum(1 for p in pagos if p.get('tipo') == 'normal')
        cuotas_regresivas = sum(1 for p in pagos if p.get('tipo') == 'adelantada')
        total_pagado = sum(p.get('monto', 0) for p in pagos)
        
        monto_total = float(cliente.get('monto_total', 0))
        monto_inscripcion = float(cliente.get('monto_inscripcion', 0))
        total_cuotas = int(cliente.get('numero_cuotas', 1))

        valor_cancelado = monto_inscripcion + total_pagado
        saldo_pendiente = monto_total - total_pagado
        
        progreso_progresivas = (cuotas_progresivas / total_cuotas) * 100 if total_cuotas > 0 else 0
        
        valor_cuota_ideal = monto_total / total_cuotas if total_cuotas > 0 else 0
        balance_a_favor = total_pagado % valor_cuota_ideal if valor_cuota_ideal > 0 and total_pagado > 0 else 0
        progreso_balance = (balance_a_favor / valor_cuota_ideal) * 100 if valor_cuota_ideal > 0 else 0

        return render_template(
            'consulta.html', cliente=cliente, pagos=sorted(pagos, key=lambda p: p.get('fecha', ''), reverse=True),
            total_pagado=total_pagado, valor_cancelado=valor_cancelado, saldo_pendiente=saldo_pendiente,
            cuotas_progresivas=cuotas_progresivas, cuotas_regresivas=cuotas_regresivas,
            progreso_progresivas=progreso_progresivas, balance_a_favor=balance_a_favor,
            progreso_balance=progreso_balance, valor_cuota_ideal=valor_cuota_ideal
        )
    except Exception as e:
        print(f"ERROR CRÍTICO en /consulta/{id_cliente}: {e}")
        flash(f'Error al cargar la consulta: {e}', 'error')
        return redirect(url_for('index'))

@app.route('/pagar/<id_cliente>', methods=['POST'])
def pagar(id_cliente):
    """Registra un nuevo pago para un cliente con validación robusta."""
    try:
        clientes = cargar_clientes()
        cliente_encontrado = next((c for c in clientes if c['id'] == id_cliente), None)

        if not cliente_encontrado:
            flash('Cliente no encontrado.', 'error')
            return redirect(url_for('index'))

        monto_str = request.form.get('monto')
        tipo_pago = request.form.get('tipo_pago')

        if not monto_str or not tipo_pago:
            flash('Error: El monto y el tipo de pago son obligatorios.', 'error')
            return redirect(url_for('consulta', id_cliente=id_cliente))

        monto = float(monto_str)
        if monto <= 0:
            flash('El monto debe ser un número positivo.', 'error')
            return redirect(url_for('consulta', id_cliente=id_cliente))

        nuevo_pago = {
            'monto': monto, 'fecha': datetime.now().strftime('%Y-%m-%d'), 'tipo': tipo_pago,
            'forma_pago': request.form.get('forma_pago', 'Efectivo'),
            'recibo': request.form.get('recibo_nro', 'N/A')
        }
        
        cliente_encontrado.setdefault('pagos', []).append(nuevo_pago)
        guardar_clientes(clientes)
        
        flash('Pago registrado con éxito.', 'success')
        return redirect(url_for('consulta', id_cliente=id_cliente))
    except Exception as e:
        print(f"ERROR CRÍTICO en /pagar/{id_cliente}: {e}")
        flash(f'Error inesperado al procesar el pago: {e}', 'error')
        return redirect(url_for('consulta', id_cliente=id_cliente))


if __name__ == '__main__':
    app.run(debug=True, port=5001)
