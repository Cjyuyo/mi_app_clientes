from flask import Flask, render_template, request, redirect, url_for, flash
import json
import os
import uuid
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'proyecto_perfecto_ahora_si'

DB_FILE = 'clientes_db.json'

# --- Funciones de Base de Datos ---
def cargar_clientes():
    if not os.path.exists(DB_FILE): return []
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError): return []

def guardar_clientes(clientes):
    with open(DB_FILE, 'w', encoding='utf-8') as f: json.dump(clientes, f, indent=4, ensure_ascii=False)

# --- FUNCIÓN CLAVE: Lógica de Cálculo Financiero ---
def calcular_estado_de_cuenta(cliente):
    """
    Calcula todos los valores dinámicos para la vista de consulta.
    """
    valor_cuota = float(cliente.get('valor_cuota', 0))
    cuotas_totales = int(cliente.get('cuotas_totales', 1))
    
    pagos_normales = [p['monto_usd'] for p in cliente.get('pagos', []) if p.get('tipo', 'normal') == 'normal']
    pagos_regresivos = [p for p in cliente.get('pagos', []) if p.get('tipo') == 'adelantada']
    
    total_pagado_progresivas = sum(pagos_normales)
    
    if valor_cuota > 0:
        cuotas_pagadas_float = total_pagado_progresivas / valor_cuota
        cuotas_progresivas = int(cuotas_pagadas_float)
        balance_a_favor = (cuotas_pagadas_float - cuotas_progresivas) * valor_cuota
        progreso_progresivas = (cuotas_progresivas / cuotas_totales) * 100
        progreso_balance = (balance_a_favor / valor_cuota) * 100
    else:
        cuotas_progresivas = 0
        balance_a_favor = 0
        progreso_progresivas = 0
        progreso_balance = 0
        
    valor_cancelado = float(cliente.get('inscripcion_monto', 0)) + total_pagado_progresivas + sum(p['monto_usd'] for p in pagos_regresivos)

    return {
        "cuotas_progresivas": cuotas_progresivas,
        "cuotas_regresivas": len(pagos_regresivos),
        "balance_a_favor": balance_a_favor,
        "valor_cuota_ideal": valor_cuota,
        "progreso_progresivas": progreso_progresivas,
        "progreso_balance": progreso_balance,
        "valor_cancelado": valor_cancelado
    }

# --- Rutas de la Aplicación ---

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        # Lógica para registrar un nuevo cliente
        clientes = cargar_clientes()
        nuevo_cliente = {
            'id': str(uuid.uuid4()),
            'nombre_apellido': request.form['nombre_apellido'],
            'cedula': request.form['cedula'],
            'contrato_nro': request.form.get('contrato_nro'),
            'telefono': request.form.get('telefono'),
            'asesor': request.form.get('asesor'),
            'responsable': request.form.get('responsable'),
            'fecha_ingreso': request.form.get('fecha_ingreso'),
            'grupo': request.form.get('grupo'),
            'bien_solicitado': request.form.get('bien_solicitado'),
            'plan_contratado': request.form.get('plan_contratado'),
            'cuotas_totales': request.form.get('cuotas_totales'),
            'moneda_pago': request.form.get('moneda_pago'),
            'valor_cuota': float(request.form.get('valor_cuota', 0)),
            'inscripcion_monto': float(request.form.get('inscripcion_monto', 0)),
            'proceso': 'INSCRITO', 'estatus': 'ACTIVO', 'pagos': []
        }
        clientes.append(nuevo_cliente)
        guardar_clientes(clientes)
        flash('Cliente registrado con éxito.', 'success')
        return redirect(url_for('consulta_clientes'))
    
    return render_template('index.html')


@app.route('/consulta')
def consulta_clientes():
    clientes = cargar_clientes()
    return render_template('lista_clientes.html', clientes=clientes)

@app.route('/cliente/<id_cliente>')
def detalle_cliente(id_cliente):
    clientes = cargar_clientes()
    cliente = next((c for c in clientes if c['id'] == id_cliente), None)
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('index'))

    # Usamos la nueva función para obtener todos los cálculos
    estado_cuenta = calcular_estado_de_cuenta(cliente)
    
    # Preparamos los pagos para mostrarlos en la tabla
    pagos_historial = []
    for p in cliente.get('pagos', []):
        pagos_historial.append({
            'fecha': p.get('fecha_pago', 'N/A')[:10],
            'monto': p.get('monto_usd', 0.0),
            'cuotas': p.get('tipo', 'normal'), # "normal" o "adelantada"
            'recibo': p.get('referencia', 'None'), # Asumiendo que 'recibo' es la referencia
            'forma_pago': p.get('forma_pago', 'Efectivo'),
            'id_pago': p.get('pago_id')
        })

    # El cliente de la captura tiene un valor cancelado, lo pasamos al template
    return render_template('consulta.html', 
                           cliente=cliente, 
                           pagos=pagos_historial,
                           **estado_cuenta)

# Aquí irían las otras rutas que ya teníamos: /editar, /registrar_pago, /recibo, etc.
# Las omito aquí para ser breve, pero deben estar en tu archivo final.

if __name__ == '__main__':
    app.run(host='0.0.0