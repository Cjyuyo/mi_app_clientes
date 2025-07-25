import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, g, flash, redirect, url_for
import uuid
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'un-secreto-muy-seguro-y-dificil-de-adivinar')

# Usamos la URL de la base de datos que siempre ha funcionado
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://lientes_db_prod_user:FzmjghqgD9UPN3I3Ex3Q8KpLlgFDvUDI@dpg-d1vomdadbo4c73fnv9sg-a/lientes_db_prod')

def get_db():
    """Abre una nueva conexión a la base de datos si no existe una para la petición actual."""
    if 'db' not in g:
        g.db = psycopg2.connect(DATABASE_URL)
    return g.db

@app.teardown_appcontext
def close_db(exception):
    """Cierra la conexión a la base de datos al final de la petición."""
    db = g.pop('db', None)
    if db is not None:
        db.close()

def calcular_estado_de_cuenta(cliente, pagos):
    """Calcula los valores dinámicos para la vista de detalle."""
    valor_cuota = cliente['valor_cuota'] or 0
    cuotas_totales = cliente['cuotas_totales'] or 1
    total_pagado = sum(p['monto_usd'] for p in pagos if p['monto_usd'])
    
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
    
    inscripcion_monto = cliente['inscripcion_monto'] or 0
    valor_cancelado = inscripcion_monto + total_pagado
    cuotas_pendientes = cuotas_totales - cuotas_progresivas
    
    return {
        "cuotas_progresivas": cuotas_progresivas, "balance_a_favor": balance_a_favor,
        "progreso_progresivas": min(progreso_progresivas, 100), "progreso_balance": min(progreso_balance, 100),
        "valor_cancelado": valor_cancelado, "cuotas_totales": cuotas_totales, "cuotas_pendientes": cuotas_pendientes
    }

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        db = get_db()
        cursor = db.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO clientes (nombre_apellido, cedula, contrato_nro, telefono, asesor, responsable, fecha_ingreso, grupo, bien_solicitado, plan_contratado, cuotas_totales, moneda_pago, valor_cuota, inscripcion_monto, proceso, estatus)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    request.form.get('nombre_apellido'), request.form.get('cedula'), request.form.get('contrato_nro'),
                    request.form.get('telefono'), request.form.get('asesor'), request.form.get('responsable'),
                    request.form.get('fecha_ingreso'), request.form.get('grupo'), request.form.get('bien_solicitado'),
                    request.form.get('plan_contratado'), int(request.form.get('cuotas_totales', 0)), request.form.get('moneda_pago'),
                    float(request.form.get('valor_cuota', 0)), float(request.form.get('inscripcion_monto', 0)),
                    request.form.get('proceso', 'INSCRITO'), 'ACTIVO'
                )
            )
            db.commit()
            flash('Cliente registrado con éxito.', 'success')
        except psycopg2.Error as e:
            db.rollback()
            flash(f'Error al registrar el cliente: {e}', 'error')
        finally:
            cursor.close()
        return redirect(url_for('consulta_clientes'))
    return render_template('index.html')

@app.route('/consulta', methods=['GET', 'POST'])
def consulta_clientes():
    if request.method == 'POST':
        termino = request.form.get('termino_busqueda', '').lower().strip()
        db = get_db()
        cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute(
            "SELECT * FROM clientes WHERE lower(cedula) LIKE %s OR lower(nombre_apellido) LIKE %s OR lower(telefono) LIKE %s",
            (f'%{termino}%', f'%{termino}%', f'%{termino}%')
        )
        resultados = cursor.fetchall()
        cursor.close()
        
        if len(resultados) == 1:
            return redirect(url_for('detalle_cliente', id_cliente=resultados[0]['id']))
        return render_template('consulta.html', clientes_encontrados=resultados)
    return render_template('consulta.html')

@app.route('/cliente/<int:id_cliente>')
def detalle_cliente(id_cliente):
    db = get_db()
    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    # Obtener datos del cliente
    cursor.execute("SELECT * FROM clientes WHERE id = %s", (id_cliente,))
    cliente = cursor.fetchone()
    
    # Obtener pagos del cliente
    cursor.execute("SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha_pago DESC", (id_cliente,))
    pagos = cursor.fetchall()
    
    cursor.close()
    
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta_clientes'))
        
    estado_cuenta = calcular_estado_de_cuenta(cliente, pagos)
    return render_template('consulta.html', cliente=cliente, pagos=pagos, **estado_cuenta)

# Las rutas para editar, registrar pago y eliminar necesitarían una adaptación similar.
# Esta es la base sólida que funcionaba.

if __name__ == '__main__':
    app.run(debug=True)
