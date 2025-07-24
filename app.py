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
    if not os.path.exists(DB_FILE):
        return []
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return []

def guardar_clientes(clientes):
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(clientes, f, indent=4, ensure_ascii=False)

# --- FUNCIÓN CLAVE: Lógica de Cálculo Financiero ---
def calcular_estado_de_cuenta(cliente):
    valor_cuota = float(cliente.get('valor_cuota', 0))
    cuotas_totales = int(cliente.get('cuotas_totales', 1))
    
    pagos_normales = [p.get('monto_usd', 0) for p in cliente.get('pagos', []) if p.get('tipo', 'normal') == 'normal']
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
        
    valor_cancelado = float(cliente.get('inscripcion_monto', 0)) + total_pagado_progresivas + sum(p.get('monto_usd', 0) for p in pagos_regresivos)

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
            # --- CAMBIO AQUÍ: Se lee el valor del nuevo campo ---
            'proceso': request.form.get('proceso', 'INSCRITO'),
            'estatus': 'ACTIVO',
            'pagos': []
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
    cliente = next((c for c in clientes if c.get('id') == id_cliente), None)
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('index'))

    estado_cuenta = calcular_estado_de_cuenta(cliente)
    
    pagos_historial = []
    for p in cliente.get('pagos', []):
        pagos_historial.append({
            'fecha': p.get('fecha_pago', 'N/A')[:10],
            'monto': p.get('monto_usd', 0.0),
            'cuotas': p.get('tipo', 'normal'),
            'recibo': p.get('referencia', 'None'),
            'forma_pago': p.get('forma_pago', 'Efectivo'),
            'id_pago': p.get('pago_id')
        })

    return render_template('consulta.html', 
                           cliente=cliente, 
                           pagos=pagos_historial,
                           **estado_cuenta)

@app.route('/editar/<id_cliente>', methods=['GET', 'POST'])
def editar_cliente(id_cliente):
    clientes = cargar_clientes()
    cliente = next((c for c in clientes if c.get('id') == id_cliente), None)
    if not cliente:
        return redirect(url_for('consulta_clientes'))

    if request.method == 'POST':
        for key in request.form:
            if key in cliente:
                cliente[key] = request.form[key]
        guardar_clientes(clientes)
        flash('Cliente actualizado correctamente', 'success')
        return redirect(url_for('detalle_cliente', id_cliente=id_cliente))

    return render_template('edit_cliente.html', cliente=cliente)

@app.route('/registrar_pago/<id_cliente>', methods=['GET', 'POST'])
def registrar_pago(id_cliente):
    clientes = cargar_clientes()
    cliente = next((c for c in clientes if c.get('id') == id_cliente), None)
    if not cliente:
        return redirect(url_for('consulta_clientes'))

    if request.method == 'POST':
        pago_id = str(uuid.uuid4())
        nuevo_pago = {
            'pago_id': pago_id,
            'fecha_pago': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'monto_usd': float(request.form.get('monto_usd', 0)),
            'tipo': request.form.get('tipo_pago', 'normal'),
            'tasa_dia': float(request.form.get('tasa_dia', 0)),
            'monto_bs': float(request.form.get('monto_bs', 0)),
            'pago_en': request.form.get('pago_en'),
            'forma_pago': request.form.get('forma_pago'),
            'banco': request.form.get('banco'),
            'referencia': request.form.get('referencia'),
            'lugar_emision': request.form.get('lugar_emision'),
            'cantidad_en_letras': request.form.get('cantidad_en_letras'),
            'por_concepto_de': request.form.get('por_concepto_de'),
            'estado': request.form.get('estado'),
        }
        cliente.setdefault('pagos', []).append(nuevo_pago)
        guardar_clientes(clientes)
        flash('Pago registrado con éxito. Puede generar el recibo.', 'success')
        return redirect(url_for('ver_recibo', id_cliente=id_cliente, id_pago=pago_id))

    return render_template('registrar_pago.html', cliente=cliente)

@app.route('/recibo/<id_cliente>/<id_pago>')
def ver_recibo(id_cliente, id_pago):
    clientes = cargar_clientes()
    cliente = next((c for c in clientes if c.get('id') == id_cliente), None)
    if not cliente:
        return redirect(url_for('consulta_clientes'))
        
    pago = next((p for p in cliente.get('pagos', []) if p.get('pago_id') == id_pago), None)
    if not pago:
        flash('Pago no encontrado.', 'error')
        return redirect(url_for('detalle_cliente', id_cliente=id_cliente))

    datos_recibo = {**cliente, **pago}
    datos_recibo['recibo'] = datos_recibo.get('recibo', pago.get('pago_id')[:8].upper())

    return render_template('recibo.html', pago=datos_recibo)