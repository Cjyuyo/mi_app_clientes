import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, g, flash, redirect, url_for
from dotenv import load_dotenv
from decimal import Decimal

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'una-clave-secreta-por-defecto-para-desarrollo')

def get_db():
    if 'db' not in g:
        DATABASE_URL = os.getenv('DATABASE_URL')
        if not DATABASE_URL:
            raise ValueError("FATAL: La variable de entorno DATABASE_URL no está configurada.")
        try:
            g.db = psycopg2.connect(DATABASE_URL)
        except psycopg2.OperationalError as e:
            print(f"Error de conexión a la base de datos: {e}")
            g.db = None
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()

# --- Las rutas / y /registrar_cliente no cambian ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/registrar_cliente', methods=['POST'])
def registrar_cliente():
    form_data = {k: v if v else None for k, v in request.form.items()}
    if not form_data.get('nombre_apellido') or not form_data.get('cedula'):
        flash("Error: Nombre y Cédula son campos obligatorios.", 'error')
        return redirect(url_for('index'))
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('index'))
    try:
        with conn.cursor() as cur:
            query = """
            INSERT INTO clientes (
                nombre_apellido, cedula, contrato_nro, telefono, asesor, responsable, fecha_ingreso,
                grupo, bien_solicitado, plan_contratado, cuotas_totales, moneda_pago, valor_cuota,
                inscripcion_monto, proceso
            ) VALUES (
                %(nombre_apellido)s, %(cedula)s, %(contrato_nro)s, %(telefono)s, %(asesor)s, %(responsable)s, %(fecha_ingreso)s,
                %(grupo)s, %(bien_solicitado)s, %(plan_contratado)s, %(cuotas_totales)s, %(moneda_pago)s, %(valor_cuota)s,
                %(inscripcion_monto)s, %(proceso)s
            )
            """
            cur.execute(query, form_data)
            conn.commit()
            flash(f"¡Cliente '{form_data.get('nombre_apellido')}' registrado exitosamente!", 'success')
    except psycopg2.IntegrityError:
        conn.rollback()
        flash(f"Registro fallido: La cédula '{form_data.get('cedula')}' ya existe.", 'error')
    except (psycopg2.Error, ValueError) as e:
        conn.rollback()
        flash(f"Registro fallido: Ocurrió un error de base de datos: {e}", 'error')
    return redirect(url_for('index'))

@app.route('/consulta', methods=['GET', 'POST'])
def consulta():
    clientes_encontrados = []
    mensaje_error = None
    if request.method == 'POST':
        termino_busqueda = request.form.get('busqueda', '').strip()
    else:
        termino_busqueda = request.args.get('busqueda', '').strip()

    if termino_busqueda:
        conn = get_db()
        if not conn:
            mensaje_error = "Error de conexión a la base de datos."
        else:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    if termino_busqueda.isdigit():
                        query_clientes = "SELECT * FROM clientes WHERE cedula = %s ORDER BY nombre_apellido LIMIT 20;"
                        cur.execute(query_clientes, (termino_busqueda,))
                    else:
                        query_clientes = "SELECT * FROM clientes WHERE nombre_apellido ILIKE %s ORDER BY nombre_apellido LIMIT 20;"
                        patron = f'%{termino_busqueda}%'
                        cur.execute(query_clientes, (patron,))
                    
                    clientes_raw = cur.fetchall()
                    if not clientes_raw:
                        mensaje_error = "🚫 No se encontraron clientes que coincidan con su búsqueda."
                    else:
                        for cliente in clientes_raw:
                            cliente_dict = dict(cliente)
                            query_pagos = "SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha_pago DESC, id DESC"
                            cur.execute(query_pagos, (cliente_dict['id'],))
                            cliente_dict['pagos'] = cur.fetchall()
                            clientes_encontrados.append(cliente_dict)
            except psycopg2.Error as e:
                mensaje_error = f"Error al consultar la base de datos: {e}"
    return render_template('consulta.html', clientes=clientes_encontrados, mensaje_error=mensaje_error, busqueda=termino_busqueda)

@app.route('/registrar_pago/<int:client_id>', methods=['GET', 'POST'])
def registrar_pago(client_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta'))
    
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM clientes WHERE id = %s", (client_id,))
        cliente = cur.fetchone()
    
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta'))

    if request.method == 'POST':
        pago_form = {k: v if v else None for k, v in request.form.items()}
        tipo_pago = pago_form.get('tipo_pago')
        
        inscripcion_total = Decimal(cliente['inscripcion_monto'] or 0)
        inscripcion_pagada = Decimal(cliente['inscripcion_pagada'] or 0)
        
        if tipo_pago == 'Cuota' and inscripcion_pagada < inscripcion_total:
            flash('Error: No se puede registrar un pago de cuota hasta que la inscripción esté 100% pagada.', 'error')
            return render_template('registrar_pago.html', cliente=cliente)
        
        if tipo_pago == 'Inscripción' and inscripcion_pagada >= inscripcion_total:
            flash('Error: La inscripción para este cliente ya ha sido completada.', 'error')
            return render_template('registrar_pago.html', cliente=cliente)

        if pago_form.get('forma_pago') == 'Transferencia' and not pago_form.get('referencia'):
            flash('Error: La referencia es obligatoria para pagos por transferencia.', 'error')
            return render_template('registrar_pago.html', cliente=cliente)
        
        try:
            with conn.cursor() as cur:
                pago_query = """
                    INSERT INTO pagos (cliente_id, monto, tipo_pago, forma_pago, fecha_pago, 
                                        pago_en, por_concepto_de, referencia, banco, lugar_emision,
                                        tasa_dia, monto_bs, estado_pago, cuotas_cubiertas)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Pendiente', 0);
                """
                cur.execute(pago_query, (
                    client_id, pago_form['monto'], tipo_pago, pago_form['forma_pago'], pago_form['fecha_pago'],
                    pago_form.get('pago_en'), pago_form['por_concepto_de'],
                    pago_form['referencia'], pago_form['banco'], pago_form['lugar_emision'],
                    pago_form.get('tasa_dia'), pago_form.get('monto_bs')
                ))
                conn.commit()
                flash(f"¡Pago de {tipo_pago} registrado como PENDIENTE! Ahora debe ser conciliado.", 'success')
                return redirect(url_for('consulta', busqueda=cliente['cedula']))

        except (psycopg2.Error, ValueError, TypeError) as e:
            conn.rollback()
            flash(f'Ocurrió un error al registrar el pago: {e}', 'error')
            return render_template('registrar_pago.html', cliente=cliente)

    return render_template('registrar_pago.html', cliente=cliente)

@app.route('/conciliar_pago/<int:pago_id>', methods=['POST'])
def conciliar_pago(pago_id):
    conn = get_db()
    cedula_cliente_fallback = request.args.get('cedula', '')

    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta', busqueda=cedula_cliente_fallback))

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM pagos WHERE id = %s", (pago_id,))
            pago = cur.fetchone()
            if not pago or pago['estado_pago'] != 'Pendiente':
                flash("El pago no se puede conciliar.", 'error')
                return redirect(url_for('consulta', busqueda=cedula_cliente_fallback))

            cur.execute("SELECT * FROM clientes WHERE id = %s FOR UPDATE", (pago['cliente_id'],))
            cliente = cur.fetchone()
            if not cliente:
                flash("Error: No se encontró el cliente asociado a este pago.", 'error')
                return redirect(url_for('consulta', busqueda=cedula_cliente_fallback))

            monto_pagado = Decimal(pago['monto'])

            if pago['tipo_pago'] == 'Inscripción':
                inscripcion_pagada_actual = Decimal(cliente['inscripcion_pagada'] or 0)
                inscripcion_total = Decimal(cliente['inscripcion_monto'] or 0)
                nueva_inscripcion_pagada = inscripcion_pagada_actual + monto_pagado

                if inscripcion_total > 0 and nueva_inscripcion_pagada >= inscripcion_total:
                    cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE cliente_id = %s AND tipo_pago = 'Inscripción'", (cliente['id'],))
                    pago_final_query = """
                        INSERT INTO pagos (cliente_id, monto, tipo_pago, forma_pago, fecha_pago, por_concepto_de, estado_pago, cuotas_cubiertas)
                        VALUES (%s, %s, 'Inscripción Finalizada', %s, %s, %s, 'Conciliado', 0) RETURNING id;
                    """
                    cur.execute(pago_final_query, (cliente['id'], inscripcion_total, pago['forma_pago'], pago['fecha_pago'], 'Pago total de inscripción'))
                    pago_final_id = cur.fetchone()[0]
                    cur.execute("UPDATE clientes SET inscripcion_pagada = %s WHERE id = %s", (inscripcion_total, cliente['id']))
                    conn.commit()
                    flash("¡Inscripción completada! Se generó el recibo final y se anularon los abonos.", 'success')
                    return redirect(url_for('ver_recibo_inscripcion', pago_id=pago_final_id))
                else:
                    cur.execute("UPDATE clientes SET inscripcion_pagada = %s WHERE id = %s", (nueva_inscripcion_pagada, cliente['id']))
                    cur.execute("UPDATE pagos SET estado_pago = 'Conciliado' WHERE id = %s", (pago_id,))
                    conn.commit()
                    flash(f"Abono de inscripción N° {pago_id} conciliado.", 'success')
                    return redirect(url_for('ver_recibo', pago_id=pago_id))

            elif pago['tipo_pago'] == 'Cuota':
                valor_cuota = Decimal(cliente['valor_cuota'] or 0)
                if valor_cuota <= 0: raise ValueError('El cliente no tiene un valor de cuota válido.')
                cuotas_progresivas_actuales = cliente['cuotas_pagadas_progresivas'] or 0
                balance_regresivo_actual = Decimal(cliente['balance_regresivo'] or 0)
                cuotas_regresivas_actuales = cliente['cuotas_pagadas_regresivas'] or 0
                monto_necesario_progresiva = valor_cuota - balance_regresivo_actual
                nuevas_cuotas_progresivas = cuotas_progresivas_actuales
                nuevo_balance_regresivo = balance_regresivo_actual
                cuotas_cubiertas_este_pago = 0
                if monto_pagado >= monto_necesario_progresiva:
                    nuevas_cuotas_progresivas += 1
                    excedente = monto_pagado - monto_necesario_progresiva
                    nuevo_balance_regresivo = excedente
                    cuotas_cubiertas_este_pago = 1
                else:
                    nuevo_balance_regresivo += monto_pagado
                while nuevo_balance_regresivo >= valor_cuota:
                    nuevo_balance_regresivo -= valor_cuota
                    nuevas_cuotas_regresivas += 1
                update_cliente_query = "UPDATE clientes SET cuotas_pagadas_progresivas = %s, balance_regresivo = %s, cuotas_pagadas_regresivas = %s WHERE id = %s;"
                cur.execute(update_cliente_query, (nuevas_cuotas_progresivas, nuevo_balance_regresivo, nuevas_cuotas_regresivas, cliente['id']))
                update_pago_query = "UPDATE pagos SET estado_pago = 'Conciliado', cuotas_cubiertas = %s, cuotas_progresivas_al_pagar = %s, cuotas_regresivas_al_pagar = %s, balance_al_pagar = %s WHERE id = %s;"
                cur.execute(update_pago_query, (cuotas_cubiertas_este_pago, nuevas_cuotas_progresivas, nuevas_cuotas_regresivas, nuevo_balance_regresivo, pago_id))
                conn.commit()
                flash(f"¡Pago de cuota N° {pago_id} conciliado exitosamente!", 'success')
                return redirect(url_for('ver_recibo', pago_id=pago_id))

    except (psycopg2.Error, ValueError, TypeError) as e:
        conn.rollback()
        flash(f'Ocurrió un error al conciliar el pago: {e}', 'error')
        return redirect(url_for('consulta', busqueda=cedula_cliente_fallback))

@app.route('/recibo/<int:pago_id>')
def ver_recibo(pago_id):
    conn = get_db()
    if not conn: return redirect(url_for('consulta'))
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        query = """
            SELECT p.*, 
                   c.nombre_apellido, c.cedula, c.cuotas_totales, c.valor_cuota,
                   c.inscripcion_monto, c.inscripcion_pagada,
                   COALESCE(p.cuotas_progresivas_al_pagar, c.cuotas_pagadas_progresivas) AS cuotas_pagadas_progresivas, 
                   COALESCE(p.cuotas_regresivas_al_pagar, c.cuotas_pagadas_regresivas) AS cuotas_pagadas_regresivas,
                   COALESCE(p.balance_al_pagar, c.balance_regresivo) AS balance_regresivo
            FROM pagos p JOIN clientes c ON p.cliente_id = c.id 
            WHERE p.id = %s;
        """
        cur.execute(query, (pago_id,))
        pago = cur.fetchone()
    if not pago:
        flash('Recibo no encontrado.', 'error')
        return redirect(url_for('consulta'))
    
    return render_template('recibo.html', pago=pago)

@app.route('/recibo_inscripcion/<int:pago_id>')
def ver_recibo_inscripcion(pago_id):
    conn = get_db()
    if not conn: return redirect(url_for('consulta'))
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        query = """
            SELECT p.*, c.nombre_apellido, c.cedula, c.plan_contratado
            FROM pagos p JOIN clientes c ON p.cliente_id = c.id 
            WHERE p.id = %s AND p.tipo_pago = 'Inscripción Finalizada';
        """
        cur.execute(query, (pago_id,))
        pago = cur.fetchone()
    if not pago:
        flash('Recibo de inscripción final no encontrado.', 'error')
        return redirect(url_for('consulta'))
    
    return render_template('recibo_inscripcion.html', pago=pago, cliente=pago)

# --- RUTA ANULAR RECIBO (MODIFICADA) ---
@app.route('/anular_recibo/<int:pago_id>', methods=['POST'])
def anular_recibo(pago_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta'))
    
    cedula_cliente = ''
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM pagos WHERE id = %s FOR UPDATE", (pago_id,))
            pago_a_anular = cur.fetchone()

            if not pago_a_anular or pago_a_anular['estado_pago'] == 'Anulado':
                flash("Este recibo ya está anulado o no se puede anular.", "warning")
                return redirect(url_for('consulta'))

            cliente_id = pago_a_anular['cliente_id']
            cur.execute("SELECT cedula FROM clientes WHERE id = %s", (cliente_id,))
            cliente_info = cur.fetchone()
            if cliente_info:
                cedula_cliente = cliente_info['cedula']

            if pago_a_anular['estado_pago'] == 'Pendiente':
                cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE id = %s", (pago_id,))
            
            elif pago_a_anular['tipo_pago'] == 'Inscripción':
                monto_pago = Decimal(pago_a_anular['monto'])
                cur.execute("UPDATE clientes SET inscripcion_pagada = inscripcion_pagada - %s WHERE id = %s", (monto_pago, cliente_id))
                cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE id = %s", (pago_id,))
            
            # --- INICIO DE LA LÓGICA CORREGIDA PARA ANULAR CUOTAS ---
            elif pago_a_anular['tipo_pago'] == 'Cuota':
                # Este nuevo método recalcula el estado financiero completo del cliente para evitar errores.
                
                # 1. Obtener el estado financiero actual del cliente y el monto a revertir.
                cur.execute("SELECT cuotas_pagadas_progresivas, cuotas_pagadas_regresivas, balance_regresivo, valor_cuota FROM clientes WHERE id = %s FOR UPDATE", (cliente_id,))
                cliente = cur.fetchone()
                
                cuotas_p = Decimal(cliente['cuotas_pagadas_progresivas'] or 0)
                cuotas_r = Decimal(cliente['cuotas_pagadas_regresivas'] or 0)
                balance = Decimal(cliente['balance_regresivo'] or 0)
                valor_cuota = Decimal(cliente['valor_cuota'] or 0)
                monto_a_revertir = Decimal(pago_a_anular['monto'])
                
                if valor_cuota <= 0:
                    raise ValueError("El cliente no tiene un valor de cuota válido para recalcular.")
                    
                # 2. Convertir el estado actual a un valor monetario total.
                valor_total_actual = (cuotas_p * valor_cuota) + (cuotas_r * valor_cuota) + balance
                
                # 3. Restar el monto del pago que se está anulando.
                nuevo_valor_total = valor_total_actual - monto_a_revertir
                
                if nuevo_valor_total < 0:
                    flash("Advertencia: La anulación resultó en un saldo negativo, se ha ajustado a cero.", "warning")
                    nuevo_valor_total = Decimal('0.00')

                # 4. Convertir el nuevo valor monetario de vuelta al estado de cuotas y balance.
                #    Para simplificar y asegurar consistencia, consolidamos todo en cuotas progresivas.
                nuevas_cuotas_pagadas = int(nuevo_valor_total // valor_cuota)
                nuevo_balance = nuevo_valor_total % valor_cuota
                
                # 5. Actualizar el estado del cliente en la base de datos.
                cur.execute("""
                    UPDATE clientes 
                    SET cuotas_pagadas_progresivas = %s, cuotas_pagadas_regresivas = 0, balance_regresivo = %s 
                    WHERE id = %s
                """, (nuevas_cuotas_pagadas, nuevo_balance, cliente_id))
                
                # 6. Marcar el pago como 'Anulado'.
                cur.execute("UPDATE pagos SET estado_pago = 'Anulado' WHERE id = %s", (pago_id,))
            # --- FIN DE LA LÓGICA CORREGIDA ---
            
            elif pago_a_anular['tipo_pago'] == 'Inscripción Finalizada':
                 flash("Error: La anulación de un recibo de inscripción finalizada no está permitida.", 'error')
                 return redirect(url_for('consulta', busqueda=cedula_cliente))

            conn.commit()
            flash(f"¡Recibo N° {pago_id} anulado y saldo corregido exitosamente!", "success")
            return redirect(url_for('consulta', busqueda=cedula_cliente))

    except (psycopg2.Error, ValueError) as e:
        conn.rollback()
        flash(f"Ocurrió un error al anular el recibo: {e}", 'error')
        return redirect(url_for('consulta', busqueda=cedula_cliente))

# --- El resto de las rutas no cambian ---
@app.route('/recibo_anulado/<int:pago_id>')
def ver_recibo_anulado(pago_id):
    conn = get_db()
    if not conn:
        flash("Error de conexión a la base de datos.", 'error')
        return redirect(url_for('consulta'))
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        query = """
            SELECT p.*, c.nombre_apellido, c.cedula
            FROM pagos p 
            JOIN clientes c ON p.cliente_id = c.id 
            WHERE p.id = %s AND p.estado_pago = 'Anulado';
        """
        cur.execute(query, (pago_id,))
        pago = cur.fetchone()
    if not pago:
        flash('Recibo anulado no encontrado.', 'error')
        return redirect(url_for('consulta'))
    return render_template('recibo_anulado.html', pago=pago)

@app.route('/edit/<int:client_id>', methods=['GET', 'POST'])
def edit_client(client_id):
    conn = get_db()
    if not conn: return redirect(url_for('consulta'))
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM clientes WHERE id = %s", (client_id,))
        cliente = cur.fetchone()
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta'))
    if request.method == 'POST':
        form_data = {k: v if v else None for k, v in request.form.items()}
        form_data['id'] = client_id
        try:
            with conn.cursor() as cur:
                update_query = """
                UPDATE clientes SET
                    nombre_apellido = %(nombre_apellido)s, cedula = %(cedula)s, contrato_nro = %(contrato_nro)s,
                    telefono = %(telefono)s, asesor = %(asesor)s, responsable = %(responsable)s, fecha_ingreso = %(fecha_ingreso)s,
                    grupo = %(grupo)s, bien_solicitado = %(bien_solicitado)s, plan_contratado = %(plan_contratado)s,
                    cuotas_totales = %(cuotas_totales)s, moneda_pago = %(moneda_pago)s, valor_cuota = %(valor_cuota)s,
                    inscripcion_monto = %(inscripcion_monto)s, proceso = %(proceso)s, estatus = %(estatus)s, estatus_1 = %(estatus_1)s
                WHERE id = %(id)s;
                """
                cur.execute(update_query, form_data)
                conn.commit()
                flash('¡Cliente actualizado exitosamente!', 'success')
                return redirect(url_for('consulta', busqueda=cliente['cedula']))
        except (psycopg2.Error, ValueError) as e:
            conn.rollback()
            flash(f'Ocurrió un error al actualizar: {e}', 'error')
    return render_template('edit_cliente.html', cliente=cliente)

@app.route('/delete/<int:client_id>', methods=['POST'])
def delete_client(client_id):
    conn = get_db()
    if not conn: return redirect(url_for('consulta'))
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pagos WHERE cliente_id = %s", (client_id,))
            cur.execute("DELETE FROM clientes WHERE id = %s", (client_id,))
            conn.commit()
            flash('¡Cliente y sus pagos han sido eliminados exitosamente!', 'success')
    except psycopg2.Error as e:
        conn.rollback()
        flash(f'Ocurrió un error al eliminar: {e}', 'error')
    return redirect(url_for('consulta'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
