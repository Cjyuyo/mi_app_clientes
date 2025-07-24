from flask import Flask, render_template, request, redirect, url_for, flash
import json
import os
import uuid
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'proyecto_perfecto_final_unificado'

DB_FILE = 'clientes_db.json'

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
    """Calcula todos los valores dinámicos para la vista de detalle de forma robusta."""
    try:
        valor_cuota = float(cliente.get('valor_cuota', 0))
    except (ValueError, TypeError):
        valor_cuota = 0.0

    try:
        cuotas_totales_str = cliente.get('cuotas_totales', '1')
        cuotas_totales = int(float(cuotas_totales_str)) if cuotas_totales_str and cuotas_totales_str != 'nan' else 1
    except (ValueError, TypeError):
        cuotas_totales = 1

    pagos = cliente.get('pagos', [])
    total_pagado = 0
    for p in pagos:
        try:
            total_pagado += float(p.get('monto_usd', 0))
        except (ValueError, TypeError):
            continue

    cuotas_progresivas = 0
    balance_a_favor = 0.0
    progreso_progresivas = 0.0
    progreso_balance = 0.0

    if valor_cuota > 0 and cuotas_totales > 0:
        cuotas_pagadas_float = total_pagado / valor_cuota
        cuotas_progresivas = int(cuotas_pagadas_float)
        balance_a_favor = (cuotas_pagadas_float - cuotas_progresivas) * valor_cuota
        progreso_progresivas = (cuotas_progresivas / cuotas_totales) * 100
        progreso_balance = (balance_a_favor / valor_cuota) * 100 if valor_cuota > 0 else 0
    
    try:
        inscripcion_monto = float(cliente.get('inscripcion_monto', 0))
    except (ValueError, TypeError):
        inscripcion_monto = 0.0

    valor_cancelado = inscripcion_monto + total_pagado
    
    return {
        "cuotas_progresivas": cuotas_progresivas,
        "balance_a_favor": balance_a_favor,
        "progreso_progresivas": min(progreso_progresivas, 100),
        "progreso_balance": min(progreso_balance, 100),
        "valor_cancelado": valor_cancelado,
        "cuotas_totales": cuotas_totales
    }

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/consulta', methods=['GET', 'POST'])
def consulta_clientes():
    if request.method == 'POST':
        termino = request.form.get('termino_busqueda', '').lower().strip()
        clientes = cargar_clientes()
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

@app.route('/cliente/<id_cliente>')
def detalle_cliente(id_cliente):
    clientes = cargar_clientes()
    cliente = next((c for c in clientes if c.get('id') == id_cliente), None)
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta_clientes'))
    
    if cliente.get('pagos'):
        try:
            cliente['pagos'] = sorted(cliente['pagos'], key=lambda p: p.get('fecha_pago', ''), reverse=True)
        except:
            pass

    estado_cuenta = calcular_estado_de_cuenta(cliente)
    return render_template('consulta.html', cliente=cliente, **estado_cuenta)

@app.route('/editar/<id_cliente>', methods=['GET', 'POST'])
def editar_cliente(id_cliente):
    clientes = cargar_clientes()
    cliente_idx, cliente = next(((i, c) for i, c in enumerate(clientes) if c.get('id') == id_cliente), (None, None))
    if not cliente:
        return redirect(url_for('consulta_clientes'))
    
    if request.method == 'POST':
        for key, value in request.form.items():
            if key in cliente:
                cliente[key] = value
        clientes[cliente_idx] = cliente
        guardar_clientes(clientes)
        flash('Cliente actualizado correctamente', 'success')
        return redirect(url_for('detalle_cliente', id_cliente=id_cliente))
    
    return render_template('edit_cliente.html', cliente=cliente)

@app.route('/registrar_pago/<id_cliente>', methods=['GET', 'POST'])
def registrar_pago(id_cliente):
    clientes = cargar_clientes()
    cliente_idx, cliente = next(((i, c) for i, c in enumerate(clientes) if c.get('id') == id_cliente), (None, None))
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta_clientes'))

    if request.method == 'POST':
        nuevo_pago = {
            'id': str(uuid.uuid4()),
            'monto_usd': request.form.get('monto_usd'),
            'forma_pago': request.form.get('forma_pago'),
            'recibo': request.form.get('recibo'),
            'fecha_pago': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'tasa_dia': request.form.get('tasa_dia'),
            'monto_bs': request.form.get('monto_bs'),
            'pago_en': request.form.get('pago_en'),
            'cantidad_en_letras': request.form.get('cantidad_en_letras'),
            'por_concepto_de': request.form.get('por_concepto_de'),
            'referencia': request.form.get('referencia'),
            'banco': request.form.get('banco'),
            'lugar_emision': request.form.get('lugar_emision'),
            'estado': request.form.get('estado')
        }
        if 'pagos' not in cliente:
            cliente['pagos'] = []
        cliente['pagos'].append(nuevo_pago)
        clientes[cliente_idx] = cliente
        guardar_clientes(clientes)
        flash('Pago registrado con éxito.', 'success')
        return redirect(url_for('detalle_cliente', id_cliente=id_cliente))

    return render_template('registrar_pago.html', cliente=cliente)

@app.route('/eliminar/<id_cliente>')
def eliminar_cliente(id_cliente):
    """Elimina un cliente de la base de datos."""
    clientes = cargar_clientes()
    clientes_actualizados = [c for c in clientes if c.get('id') != id_cliente]
    
    if len(clientes_actualizados) < len(clientes):
        guardar_clientes(clientes_actualizados)
        flash('Cliente eliminado exitosamente.', 'success')
    else:
        flash('No se pudo encontrar al cliente para eliminar.', 'error')
        
    return redirect(url_for('consulta_clientes'))

@app.route('/recibo/<id_cliente>/<id_pago>')
def ver_recibo(id_cliente, id_pago):
    """Muestra un recibo de pago específico."""
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

    return render_template('recibo.html', pago=datos_recibo, id_cliente=id_cliente)

if __name__ == '__main__':
    app.run(debug=True)
