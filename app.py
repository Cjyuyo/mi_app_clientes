# ... (código anterior de app.py sin cambios hasta la función index)

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        try:
            form_data = {k: (v if v != '' else None) for k, v in request.form.items()}
            if not form_data.get('nombre_apellido') or not form_data.get('cedula'):
                flash("Error: El nombre y la cédula son campos obligatorios.", 'error')
                return render_template('index.html')

            db = get_db()
            cursor = db.cursor()
            # CORRECCIÓN: Se actualiza la consulta con los nombres de campo correctos
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
            cursor.execute(query, form_data)
            db.commit()
            cursor.close()
            flash(f"¡Cliente '{form_data.get('nombre_apellido')}' registrado exitosamente!", 'success')
            return redirect(url_for('index'))
        except Exception as e:
            # ... (manejo de errores sin cambios)
            pass
    return render_template('index.html')

# ... (El resto de las rutas no cambian)
