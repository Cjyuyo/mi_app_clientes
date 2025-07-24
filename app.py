import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, g, flash, redirect, url_for
from decimal import Decimal, getcontext

# Configurar la precisión para los cálculos decimales
getcontext().prec = 10

app = Flask(__name__)
# Es crucial configurar una clave secreta para que los mensajes flash funcionen
app.secret_key = os.environ.get('SECRET_KEY', 'una-clave-secreta-muy-segura-y-dificil-de-adivinar')

# Obtener la URL de la base de datos desde las variables de entorno
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db():
    """
    Función para obtener una conexión a la base de datos.
    La conexión se reutiliza si ya existe en el contexto de la aplicación (g).
    """
    db = getattr(g, '_database', None)
    if db is None:
        if not DATABASE_URL:
            # Si no se encuentra la URL, la aplicación no puede funcionar.
            raise ValueError("La variable de entorno DATABASE_URL no está configurada.")
        # Se conecta a la base de datos PostgreSQL
        db = g._database = psycopg2.connect(DATABASE_URL)
    return db

@app.teardown_appcontext
def close_connection(exception):
    """
    Cierra la conexión a la base de datos al final de cada petición
    para liberar recursos.
    """
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

@app.route('/', methods=['GET', 'POST'])
def index():
    """
    Ruta para la página principal. Muestra el formulario de registro
    y procesa el envío de nuevos clientes.
    """
    if request.method == 'POST':
        db = get_db()
        cursor = db.cursor()
        try:
            # Recolecta los datos del formulario. Si un campo está vacío, se guarda como None.
            form_data = {k: (v if v != '' else None) for k, v in request.form.items()}
            
            # Validación de campos obligatorios
            if not form_data.get('nombre_apellido') or not form_data.get('cedula'):
                flash("Error: El nombre y la cédula son campos obligatorios.", 'error')
                return render_template('index.html')

            # Inserta el nuevo cliente en la base de datos
            query = """
            INSERT INTO clientes (
                nombre_apellido, cedula, contrato_nro, telefono, asesor, responsable, fecha_ingreso,
                grupo, bien_solicitado, plan_contratado, cuotas_totales, moneda_pago, valor_cuota,
                inscripcion_monto, proceso, reserva_monto_total, reserva_monto_pagado
            ) VALUES (
                %(nombre_apellido)s, %(cedula)s, %(contrato_nro)s, %(telefono)s, %(asesor)s, %(responsable)s, %(fecha_ingreso)s,
                %(grupo)s, %(bien_solicitado)s, %(plan_contratado)s, %(cuotas_totales)s, %(moneda_pago)s, %(valor_cuota)s,
                %(inscripcion_monto)s, %(proceso)s, %(reserva_monto_total)s, %(reserva_monto_pagado)s
            )
            """
            cursor.execute(query, form_data)
            db.commit()
            flash(f"¡Cliente '{form_data.get('nombre_apellido')}' registrado exitosamente!", 'success')
            return redirect(url_for('index'))
        except psycopg2.IntegrityError:
            # Maneja el error si la cédula ya existe (ya que es UNIQUE)
            db.rollback()
            flash(f"Registro fallido: La cédula '{form_data.get('cedula')}' ya existe.", 'error')
        except Exception as e:
            # Maneja cualquier otro error inesperado
            db.rollback()
            flash(f"Registro fallido: Ocurrió un error inesperado: {e}", 'error')
        finally:
            cursor.close()
            
    return render_template('index.html')

@app.route('/consulta', methods=['GET', 'POST'])
def consulta():
    """
    Ruta para consultar clientes por nombre, cédula o teléfono.
    """
    clientes_encontrados = []
    mensaje_error = None
    if request.method == 'POST':
        termino_busqueda = request.form.get('busqueda', '').strip()
        if not termino_busqueda:
            mensaje_error = "Por favor, ingrese un término para buscar."
        else:
            db = get_db()
            cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
            
            # Busca por cédula si el término es numérico, si no, por nombre o teléfono
            if termino_busqueda.isdigit():
                query_clientes = "SELECT * FROM clientes WHERE cedula = %s LIMIT 20;"
                cursor.execute(query_clientes, (termino_busqueda,))
            else:
                query_clientes = "SELECT * FROM clientes WHERE nombre_apellido ILIKE %s OR telefono LIKE %s LIMIT 20;"
                patron = f'%{termino_busqueda}%'
                cursor.execute(query_clientes, (patron, patron))

            clientes_encontrados_raw = cursor.fetchall()
            
            # Para cada cliente encontrado, busca también su historial de pagos
            for cliente_raw in clientes_encontrados_raw:
                cliente_dict = dict(cliente_raw)
                query_pagos = "SELECT * FROM pagos WHERE cliente_id = %s ORDER BY fecha_pago DESC"
                cursor.execute(query_pagos, (cliente_dict['id'],))
                cliente_dict['pagos'] = cursor.fetchall()
                clientes_encontrados.append(cliente_dict)
            
            if not clientes_encontrados:
                mensaje_error = "🚫 No se encontraron clientes que coincidan con su búsqueda."
            
            cursor.close()
    
    return render_template('consulta.html', clientes=clientes_encontrados, mensaje_error=mensaje_error)

@app.route('/edit/<int:client_id>', methods=['GET', 'POST'])
def edit_client(client_id):
    """
    Ruta para editar los datos de un cliente existente.
    """
    db = get_db()
    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    if request.method == 'POST':
        try:
            form_data = {k: (v if v != '' else None) for k, v in request.form.items()}
            form_data['id'] = client_id
            
            # Query para actualizar todos los campos del cliente
            update_query = """
            UPDATE clientes SET
                nombre_apellido = %(nombre_apellido)s, cedula = %(cedula)s, contrato_nro = %(contrato_nro)s,
                telefono = %(telefono)s, asesor = %(asesor)s, responsable = %(responsable)s,
                fecha_ingreso = %(fecha_ingreso)s, grupo = %(grupo)s, bien_solicitado = %(bien_solicitado)s,
                plan_contratado = %(plan_contratado)s, cuotas_totales = %(cuotas_totales)s, moneda_pago = %(moneda_pago)s,
                valor_cuota = %(valor_cuota)s, inscripcion_monto = %(inscripcion_monto)s,
                proceso = %(proceso)s, estatus = %(estatus)s, estatus_1 = %(estatus_1)s
            WHERE id = %(id)s;
            """
            cursor.execute(update_query, form_data)
            db.commit()
            flash('¡Cliente actualizado exitosamente!', 'success')
            return redirect(url_for('consulta'))
        except Exception as e:
            db.rollback()
            flash(f'Ocurrió un error al actualizar el cliente: {e}', 'error')
        finally:
            cursor.close()

    # Si es un GET, obtiene los datos del cliente para mostrarlos en el formulario
    cursor.execute("SELECT * FROM clientes WHERE id = %s", (client_id,))
    cliente = cursor.fetchone()
    cursor.close()
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta'))
        
    return render_template('edit_cliente.html', cliente=cliente)

@app.route('/registrar_pago/<int:client_id>', methods=['GET', 'POST'])
def registrar_pago(client_id):
    """
    Ruta para registrar un nuevo pago. Ahora distingue entre 'guardar' y 'conciliar'.
    """
    db = get_db()
    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cursor.execute("SELECT * FROM clientes WHERE id = %s", (client_id,))
    cliente = cursor.fetchone()
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        cursor.close()
        return redirect(url_for('consulta'))

    # Se inicializa form_data vacío para el caso GET
    form_data_for_template = {}

    if request.method == 'POST':
        try:
            form_data = {k: (v if v != '' else None) for k, v in request.form.items()}
            form_data['cliente_id'] = client_id
            accion = form_data.get('accion')

            # --- VALIDACIÓN (Solo si se intenta conciliar) ---
            if accion == 'conciliar':
                if (form_data.get('forma_pago') in ['Transferencia', 'Pago Móvil']) and not form_data.get('referencia'):
                    flash('Error: La referencia es obligatoria para conciliar transferencias o pago móvil.', 'error')
                    # Se devuelve form_data para no perder lo que el usuario ya escribió
                    return render_template('registrar_pago.html', cliente=cliente, form_data=form_data)

            # --- INSERCIÓN DEL PAGO ---
            estado = 'Conciliado' if accion == 'conciliar' else 'Pendiente'
            form_data['estado'] = estado
            
            query_pago = """
            INSERT INTO pagos (
                cliente_id, monto, monto_usd, monto_bs, tasa_dia, cuotas, forma_pago, 
                pago_en, cantidad_en_letras, por_concepto_de, referencia, banco, lugar_emision,
                estado
            ) VALUES (
                %(cliente_id)s, %(monto)s, %(monto_usd)s, %(monto_bs)s, %(tasa_dia)s, %(cuotas)s, %(forma_pago)s,
                %(pago_en)s, %(cantidad_en_letras)s, %(por_concepto_de)s, %(referencia)s, %(banco)s, %(lugar_emision)s,
                %(estado)s
            ) RETURNING id;
            """
            cursor.execute(query_pago, form_data)
            nuevo_pago_id = cursor.fetchone()['id']
            
            # --- LÓGICA DE ACTUALIZACIÓN FINANCIERA (Solo si se concilia) ---
            if accion == 'conciliar':
                monto_pagado = Decimal(form_data.get('monto_usd', 0))
                valor_cuota = Decimal(cliente['valor_cuota'] or 0)
                balance_anterior = Decimal(cliente['balance_regresivo'] or 0)
                
                cuotas_pagadas_progresivas = int(cliente['cuotas_pagadas_progresivas'] or 0)
                cuotas_totales = int(cliente['cuotas_totales'] or 0)

                if valor_cuota > 0 and cuotas_totales > 0:
                    total_disponible = monto_pagado + balance_anterior
                    cuotas_pagadas_en_transaccion = int(total_disponible // valor_cuota)
                    nuevo_balance = total_disponible % valor_cuota

                    # CORRECCIÓN: Se actualizan los contadores con la nueva lógica
                    nuevo_total_progresivas = cuotas_pagadas_progresivas + cuotas_pagadas_en_transaccion
                    nuevo_total_regresivas = cuotas_totales - nuevo_total_progresivas
                    
                    update_cliente_query = """
                    UPDATE clientes SET
                        cuotas_pagadas_progresivas = %s,
                        cuotas_pagadas_regresivas = %s,
                        balance_regresivo = %s,
                        valor_cancelado = COALESCE(valor_cancelado, 0) + %s
                    WHERE id = %s;
                    """
                    cursor.execute(update_cliente_query, (
                        str(nuevo_total_progresivas), 
                        str(nuevo_total_regresivas), 
                        float(nuevo_balance), 
                        float(monto_pagado), 
                        client_id
                    ))
                
                db.commit()
                flash('¡Pago conciliado y recibo generado exitosamente!', 'success')
                return redirect(url_for('ver_recibo', pago_id=nuevo_pago_id))

            else: # Si la acción es 'guardar'
                db.commit()
                flash('Pago guardado como pendiente exitosamente.', 'success')
                return redirect(url_for('consulta'))

        except Exception as e:
            db.rollback()
            flash(f'Ocurrió un error al registrar el pago: {e}', 'error')
            # Se devuelve form_data para no perder lo que el usuario ya escribió
            form_data_for_template = request.form
    
    cursor.close()
    return render_template('registrar_pago.html', cliente=cliente, form_data=form_data_for_template)


@app.route('/recibo/<int:pago_id>')
def ver_recibo(pago_id):
    """
    Muestra el recibo de un pago específico.
    """
    db = get_db()
    cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    query = """
    SELECT p.*, c.nombre_apellido, c.cedula 
    FROM pagos p 
    JOIN clientes c ON p.cliente_id = c.id 
    WHERE p.id = %s;
    """
    cursor.execute(query, (pago_id,))
    pago = cursor.fetchone()
    cursor.close()
    
    if not pago:
        flash('Recibo no encontrado.', 'error')
        return redirect(url_for('consulta'))
        
    return render_template('recibo.html', pago=pago)

@app.route('/delete/<int:client_id>', methods=['POST'])
def delete_client(client_id):
    """
    Ruta para eliminar un cliente y todos sus datos asociados (pagos).
    """
    try:
        db = get_db()
        cursor = db.cursor()
        cursor.execute("DELETE FROM clientes WHERE id = %s", (client_id,))
        db.commit()
        flash('¡Cliente eliminado exitosamente!', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Ocurrió un error al eliminar el cliente: {e}', 'error')
    finally:
        cursor.close()
        
    return redirect(url_for('consulta'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
