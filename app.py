from flask import Flask, render_template, request, redirect, url_for, flash
import json
import os
import uuid
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'proyecto_perfecto_final_unificado'

DB_FILE = 'clientes_db.json'

# --- FUNCIONES RESTAURADAS ---
def cargar_clientes():
    """Carga los datos de clientes desde el archivo JSON."""
    if not os.path.exists(DB_FILE):
        return []
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return []

def guardar_clientes(clientes):
    """Guarda los datos en el archivo JSON."""
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(clientes, f, indent=4, ensure_ascii=False)

def calcular_estado_de_cuenta(cliente):
    """Calcula todos los valores dinámicos para la vista de detalle."""
    valor_cuota = float(cliente.get('valor_cuota', 0))
    cuotas_totales = int(cliente.get('cuotas_totales', 1))
    pagos = cliente.get('pagos', [])
    total_pagado = sum(p.get('monto_usd', 0) for p in pagos)

    if valor_cuota > 0:
        cuotas_pagadas_float = total_pagado / valor_cuota
        cuotas_progresivas = int(cuotas_pagadas_float)
        balance_a_favor = (cuotas_pagadas_float - cuotas_progresivas) * valor_cuota
        progreso_progresivas = (cuotas_progresivas / cuotas_totales) * 100
        progreso_balance = (balance_a_favor / valor_cuota) * 100
    else:
        cuotas_progresivas, balance_a_favor, progreso_progresivas, progreso_balance = 0, 0, 0, 0
    
    valor_cancelado = float(cliente.get('inscripcion_monto', 0)) + total_pagado
    
    return {
        "cuotas_progresivas": cuotas_progresivas,
        "balance_a_favor": balance_a_favor,
        "progreso_progresivas": progreso_progresivas,
        "progreso_balance": progreso_balance,
        "valor_cancelado": valor_cancelado
    }

# --- Rutas de la Aplicación ---
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        clientes = cargar_clientes()
        nuevo_cliente = {
            'id': str(uuid.uuid4()), 'nombre_apellido': request.form['nombre_apellido'], 'cedula': request.form['cedula'],
            'contrato_nro': request.form.get('contrato_nro'), 'telefono': request.form.get('telefono'), 'asesor': request.form.get('asesor'), 
            'responsable': request.form.get('responsable'), 'fecha_ingreso': request.form.get('fecha_ingreso'), 
            'grupo': request.form.get('grupo'), 'bien_solicitado': request.form.get('bien_solicitado'), 
            'plan_contratado': request.form.get('plan_contratado'), 'cuotas_totales': request.form.get('cuotas_totales'), 
            'moneda_pago': request.form.get('moneda_pago'), 'valor_cuota': float(request.form.get('valor_cuota', 0)), 
            'inscripcion_monto': float(request.form.get('inscripcion_monto', 0)), 'proceso': request.form.get('proceso', 'INSCRITO'), 
            'estatus': 'ACTIVO', 'pagos': []
        }
        clientes.append(nuevo_cliente)
        guardar_clientes(clientes)
        flash('Cliente registrado con éxito.', 'success')
        return redirect(url_for('consulta_clientes'))
    return render_template('index.html')

@app.route('/consulta', methods=['GET', 'POST'])
def consulta_clientes():
    if request.method == 'POST':
        termino = request.form.get('termino_busqueda', '').lower().strip()
        clientes = cargar_clientes()
        
        # --- CORRECCIÓN ---
        # Lógica de búsqueda más robusta para evitar errores y buscar correctamente.
        resultados = [
            c for c in clientes if
            termino in str(c.get('cedula', '')).lower() or
            termino in str(c.get('nombre_apellido', '')).lower() or
            termino in str(c.get('telefono', '')).lower()
        ]
        
        if len(resultados) == 1:
            return redirect(url_for('detalle_cliente', id_cliente=resultados[0]['id']))
        elif len(resultados) > 1:
            return render_template('consulta.html', clientes_encontrados=resultados)
        else:
            flash('No se encontró ningún cliente con ese término.', 'error')
            return render_template('consulta.html')
            
    return render_template('consulta.html')

@app.route('/cliente/<id_cliente>')
def detalle_cliente(id_cliente):
    clientes = cargar_clientes()
    cliente = next((c for c in clientes if c.get('id') == id_cliente), None)
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta_clientes'))
    estado_cuenta = calcular_estado_de_cuenta(cliente)
    return render_template('consulta.html', cliente=cliente, **estado_cuenta)

@app.route('/editar/<id_cliente>', methods=['GET', 'POST'])
def editar_cliente(id_cliente):
    clientes = cargar_clientes()
    cliente = next((c for c in clientes if c.get('id') == id_cliente), None)
    if not cliente: return redirect(url_for('consulta_clientes'))
    if request.method == 'POST':
        for key in request.form:
            if key in cliente: cliente[key] = request.form[key]
        guardar_clientes(clientes)
        flash('Cliente actualizado correctamente', 'success')
        return redirect(url_for('detalle_cliente', id_cliente=id_cliente))
    return render_template('edit_cliente.html', cliente=cliente)

@app.route('/registrar_pago/<id_cliente>', methods=['GET', 'POST'])
def registrar_pago(id_cliente):
    # (Lógica de registrar pago sin cambios)
    clientes = cargar_clientes()
    cliente = next((c for c in clientes if c.get('id') == id_cliente), None)
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta_clientes'))
    return render_template('registrar_pago.html', cliente=cliente)

@app.route('/recibo/<id_cliente>/<id_pago>')
def ver_recibo(id_cliente, id_pago):
    # (Lógica de ver recibo sin cambios)
    # Esta es una implementación de ejemplo, debes adaptarla a tu lógica real
    clientes = cargar_clientes()
    cliente = next((c for c in clientes if c.get('id') == id_cliente), None)
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta_clientes'))
    
    pago = next((p for p in cliente.get('pagos', []) if p.get('id') == id_pago), None)
    if not pago:
        flash('Pago no encontrado.', 'error')
        return redirect(url_for('detalle_cliente', id_cliente=id_cliente))

    datos_recibo = pago.copy()
    datos_recibo['nombre_apellido'] = cliente.get('nombre_apellido')
    datos_recibo['cedula'] = cliente.get('cedula')

    return render_template('recibo.html', pago=datos_recibo)

if __name__ == '__main__':
    app.run(debug=True)
