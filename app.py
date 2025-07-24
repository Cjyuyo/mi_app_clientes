from flask import Flask, render_template, request, redirect, url_for, flash
import json, os, uuid
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'proyecto_perfecto_final_unificado'

DB_FILE = 'clientes_db.json'

# (Las funciones cargar_clientes, guardar_clientes y calcular_estado_de_cuenta permanecen igual)
# ...

@app.route('/', methods=['GET', 'POST'])
def index():
    # (Sin cambios aquí)
    # ...
    return render_template('index.html')

@app.route('/consulta', methods=['GET', 'POST'])
def consulta_clientes():
    if request.method == 'POST':
        termino = request.form['termino_busqueda'].lower()
        clientes = cargar_clientes()
        resultados = [c for c in clientes if termino in c['cedula'].lower() or termino in c['nombre_apellido'].lower() or (c.get('telefono') and termino in c.get('telefono', ''))]
        
        if len(resultados) == 1:
            # Si solo hay un resultado, vamos directo a su detalle
            return redirect(url_for('detalle_cliente', id_cliente=resultados[0]['id']))
        elif len(resultados) > 1:
            # Si hay varios, los mostramos en la misma página de consulta
            return render_template('consulta.html', clientes_encontrados=resultados)
        else:
            flash('No se encontró ningún cliente.', 'error')
            # Si no hay resultados, mostramos la página de consulta con un error
            return render_template('consulta.html')

    # Si es GET, solo muestra la página de consulta con el buscador
    return render_template('consulta.html')

@app.route('/cliente/<id_cliente>')
def detalle_cliente(id_cliente):
    clientes = cargar_clientes()
    cliente = next((c for c in clientes if c.get('id') == id_cliente), None)
    if not cliente:
        flash('Cliente no encontrado.', 'error')
        return redirect(url_for('consulta_clientes'))
    
    estado_cuenta = calcular_estado_de_cuenta(cliente)
    
    # IMPORTANTE: El detalle del cliente ahora también usa 'consulta.html'
    return render_template('consulta.html', cliente=cliente, **estado_cuenta)

# (Las demás rutas no cambian)
# ...